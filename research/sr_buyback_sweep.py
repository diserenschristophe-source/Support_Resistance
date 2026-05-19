#!/usr/bin/env python3
"""
sr_buyback_sweep.py — Full grid sweep for S/R Buyback signal-service.
======================================================================

Mirrors bnr_sweep.py but for the SR Buyback strategy. Adds knobs that
weren't in BNR:
  - sr_max_distance_atr (support proximity window)
  - sr_sl_atr_mult (SL distance below support)
  - min_rr (TP cascade RR threshold — was fixed at 2.0 in BNR)

Detection cached per (sr_max_distance_atr, sr_sl_atr_mult, major_only)
to avoid re-running 12 detections per combo.

Score = expectancy_per_signal × sqrt(n_filled).

Knobs swept (default = 2,304 combos):

  sr_max_distance_atr  ∈ {1.5, 2.0, 3.0}        ← 3
  sr_sl_atr_mult       ∈ {0.5, 1.0}             ← 2
  min_token_rsi        ∈ {50, 60}               ← 2
  min_confluence       ∈ {0, 50}                ← 2
  min_touches          ∈ {0, 3}                 ← 2
  max_hold_days        ∈ {7, 14, 21}            ← 3
  major_only           ∈ {False, True}          ← 2
  tp_cascade           ∈ {False, True}          ← 2
  min_rr               ∈ {1.5, 2.0, 2.5, 3.0}   ← 4

Held constant: sr_min_distance_atr=0.2, retest_ttl_hours=168, btc_rsi_floor=50

Output:
  reports/backtest_sr_buyback/sr_sweep_results_<UTC>.csv
  reports/backtest_sr_buyback/sr_sweep_summary_<UTC>.md

Usage:
  python3 research/sr_buyback_sweep.py --universe selected17
  python3 research/sr_buyback_sweep.py --universe selected17 --quick    # ~256 combos
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from backtest_intraday import (  # noqa: E402
    load_hourly,
    aggregate_to_4h,
    aggregate_to_daily,
    build_sr_cache_full,
    partial_bar_rsi,
    daily_mt_not_downtrend_mask,
    daily_relative_volume_mask,
    UNIVERSES,
    MIN_DAILY_HISTORY,
    RSI_PERIOD,
    RSI_HISTORY_BARS,
    TRADING_FEE,
)
from backtest_break_retest_v2 import (  # noqa: E402
    Signal,
    daily_atr_series,
    levels_with_meta,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sr_sweep")

REPORT_DIR = ROOT / "reports" / "backtest_sr_buyback"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"

BTC_RSI_FLOOR = 50.0
SR_MIN_DISTANCE_ATR_FIXED = 0.2
RETEST_TTL_HOURS_FIXED = 168
PER_TOKEN_CAP = 1


# ─────────────────────────────────────────────────────────────────────────────
# Config + grids
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    # Strategy structure
    sr_max_distance_atr: float
    sr_sl_atr_mult: float
    major_only: bool
    # Regime / TA-style filters (all SOFT — 0 / False = off)
    btc_rsi_floor: float          # 0 = off; require BTC RSI >= this
    mt_not_downtrend: bool        # require token NOT in confirmed downtrend
    relative_volume: bool         # require token volume >= 1.5x 20d avg
    btc_uptrend: bool             # NEW: require BTC > BTC_SMA(100) AND SMA rising
    rsi_cap: float                # 0 = off; require token_rsi <= rsi_cap
    min_token_rsi: float          # 0 = off; require token_rsi >= this
    # Quality
    min_confluence: float
    min_touches: int
    # TP cascade
    tp_cascade: bool
    min_rr: float
    # Position management
    max_hold_days: int


# Exhaustive sweep — every filter is a SOFT knob (off / on values in grid).
# = 4 × 4 × 2 × 3 × 2 × 2 × 3 × 3 × 2 × 2 × 2 × 3 × 3 = 248,832 combos
DEFAULT_GRID = {
    "sr_max_distance_atr": [1.5, 2.0, 2.5, 3.0],   # 4
    "sr_sl_atr_mult":      [0.3, 0.5, 0.75, 1.0],  # 4
    "major_only":          [False, True],          # 2
    "btc_rsi_floor":       [0.0, 50.0, 60.0],      # 3
    "mt_not_downtrend":    [False, True],          # 2
    "relative_volume":     [False, True],          # 2
    "btc_uptrend":         [False, True],          # 2  ← NEW (BTC-level regime)
    "rsi_cap":             [0.0, 75.0, 80.0],      # 3
    "min_token_rsi":       [0.0, 50.0, 60.0],      # 3
    "min_confluence":      [0.0, 50.0],            # 2
    "min_touches":         [0, 3],                 # 2
    "tp_cascade":          [False, True],          # 2
    "min_rr":              [1.5, 2.0, 2.5],        # 3
    "max_hold_days":       [7, 14, 21],            # 3
}

# Fast sanity grid (~4096 combos, ~2-5 min)
QUICK_GRID = {
    "sr_max_distance_atr": [2.0, 3.0],
    "sr_sl_atr_mult":      [0.5, 1.0],
    "major_only":          [False, True],
    "btc_rsi_floor":       [0.0, 50.0],
    "mt_not_downtrend":    [False, True],
    "relative_volume":     [False, True],
    "btc_uptrend":         [False, True],
    "rsi_cap":             [0.0, 80.0],
    "min_token_rsi":       [0.0, 60.0],
    "min_confluence":      [0.0, 50.0],
    "min_touches":         [0, 3],
    "tp_cascade":          [False, True],
    "min_rr":              [2.0],
    "max_hold_days":       [7, 14],
}                                                  # = 8,192 combos


def expand_grid(grid: dict) -> list[Config]:
    keys = list(grid.keys())
    vals = [grid[k] for k in keys]
    combos = list(itertools.product(*vals))
    return [Config(**dict(zip(keys, c))) for c in combos]


# ─────────────────────────────────────────────────────────────────────────────
# Parametrized detect / cascade / filter / evaluate
# ─────────────────────────────────────────────────────────────────────────────

def detect_sr_buyback(
    universe_name: str,
    hourly: dict[str, pd.DataFrame],
    sr_cache: dict,
    daily_ohlcv: dict[str, pd.DataFrame],
    sr_max_distance_atr: float,
    sr_sl_atr_mult: float,
    major_only: bool = False,
) -> list[Signal]:
    uconf = UNIVERSES[universe_name]
    available = [t for t in uconf["tokens"] if t in hourly]
    if "BTC" not in hourly:
        return []

    bars_4h = aggregate_to_4h(hourly)
    atr_daily = daily_atr_series(daily_ohlcv)
    close_4h = {s: bars_4h[s]["close"] for s in available}

    common_idx = bars_4h[available[0]].index
    for sym in available[1:]:
        common_idx = common_idx.intersection(bars_4h[sym].index)
    common_idx = common_idx.sort_values()

    daily_close_df = pd.DataFrame(
        {s: d["close"] for s, d in daily_ohlcv.items() if s in available}
    ).sort_index()
    daily_volume_df = pd.DataFrame(
        {s: d["volume"] for s, d in daily_ohlcv.items() if s in available}
    ).sort_index()
    daily_close_np = {s: daily_close_df[s].to_numpy(dtype=float) for s in available}
    daily_index = daily_close_df.index

    # Precompute TA-style daily masks (always — Signal stores both pass flags;
    # the filter step decides whether to enforce based on Config).
    mt_mask = daily_mt_not_downtrend_mask(daily_close_df, sma_period=40, slope_bars=20)
    rv_mask = daily_relative_volume_mask(daily_volume_df, period=20, threshold=1.5)
    # BTC-level uptrend mask: BTC close > BTC SMA(100) AND SMA rising 20d.
    # Indexed by daily date; same value applied to every token at that date.
    btc_close = daily_close_df["BTC"]
    btc_sma100 = btc_close.rolling(100).mean()
    btc_uptrend_mask = (btc_close > btc_sma100) & (btc_sma100.diff(20) > 0)

    def last_closed_daily_idx(ts):
        target = ts.normalize() - pd.Timedelta(days=1)
        return int(daily_index.searchsorted(target, side="right") - 1)

    signals: list[Signal] = []
    for hi in range(1, len(common_idx)):
        ts = common_idx[hi]
        d_idx = last_closed_daily_idx(ts)
        if d_idx < MIN_DAILY_HISTORY:
            continue

        btc_hist = daily_close_np["BTC"][:d_idx + 1][-RSI_HISTORY_BARS:]
        btc_now = float(close_4h["BTC"].loc[ts])
        btc_rsi = partial_bar_rsi(btc_hist, btc_now, RSI_PERIOD)
        if pd.isna(btc_rsi):
            continue
        # NOTE: BTC RSI floor is a SOFT filter applied at filter_signals time.
        # Detection emits the signal regardless; we just record btc_rsi for the
        # filter to use later.

        d_date = daily_index[d_idx]
        d_date_str = str(d_date)[:10]

        for sym in available:
            entry = sr_cache.get((sym, d_date_str))
            if entry is None:
                continue
            supports, resistances = levels_with_meta(entry)
            if major_only:
                supports = [l for l in supports if l.get("tier") == "Major"]
                resistances = [l for l in resistances if l.get("tier") == "Major"]
            if not supports:
                continue

            atr_val = atr_daily.get(sym, pd.Series()).get(d_date, np.nan)
            if pd.isna(atr_val) or atr_val <= 0:
                continue

            current_price = float(close_4h[sym].loc[ts])

            valid = []
            for s in supports:
                gap = current_price - s["price"]
                if gap <= 0:
                    continue
                gap_atr = gap / atr_val
                if SR_MIN_DISTANCE_ATR_FIXED <= gap_atr <= sr_max_distance_atr:
                    valid.append(s)
            if not valid:
                continue

            chosen = max(valid, key=lambda s: s["price"])
            entry_price = chosen["price"]

            above_now = sorted(
                [r for r in resistances if r["price"] > current_price],
                key=lambda r: r["price"],
            )
            if not above_now:
                continue
            tp = above_now[0]["price"]
            tp2 = above_now[1]["price"] if len(above_now) > 1 else 0.0

            sl = entry_price - sr_sl_atr_mult * atr_val

            if tp <= entry_price or sl >= entry_price:
                continue

            sym_hist = daily_close_np[sym][:d_idx + 1][-RSI_HISTORY_BARS:]
            tok_rsi = partial_bar_rsi(sym_hist, current_price, RSI_PERIOD)

            # Look up daily mask values (frozen at yesterday's close)
            mt_pass = bool(mt_mask.iloc[d_idx].get(sym, True))
            rv_pass = bool(rv_mask.iloc[d_idx].get(sym, False))
            _btc_up = btc_uptrend_mask.iloc[d_idx]
            btc_up_pass = bool(_btc_up) if pd.notna(_btc_up) else False

            signals.append(Signal(
                symbol=sym,
                breakout_ts=ts,
                breakout_close=current_price,
                level=float(entry_price),
                tp=float(tp),
                sl=float(sl),
                expires_at=ts,
                confluence=float(chosen["confluence"]),
                touches=int(chosen["touches"]),
                tier=str(chosen.get("tier", "")),
                breakout_magnitude_atr=float((current_price - entry_price) / atr_val),
                token_rsi=float(tok_rsi) if not pd.isna(tok_rsi) else 0.0,
                btc_rsi=float(btc_rsi),
                raw_rr=float((tp - entry_price) / max(entry_price - sl, 1e-9)),
                tp2=float(tp2),
                mt_not_downtrend_pass=mt_pass,
                relative_volume_pass=rv_pass,
                btc_uptrend_pass=btc_up_pass,
            ))
    return signals


def fresh_copies(signals: list[Signal], ttl_hours: int) -> list[Signal]:
    return [
        Signal(
            symbol=s.symbol,
            breakout_ts=s.breakout_ts,
            breakout_close=s.breakout_close,
            level=s.level, tp=s.tp, sl=s.sl,
            expires_at=s.breakout_ts + pd.Timedelta(hours=ttl_hours),
            confluence=s.confluence, touches=s.touches,
            tier=s.tier,
            breakout_magnitude_atr=s.breakout_magnitude_atr,
            token_rsi=s.token_rsi, btc_rsi=s.btc_rsi,
            raw_rr=s.raw_rr,
            tp2=s.tp2,
            mt_not_downtrend_pass=s.mt_not_downtrend_pass,
            relative_volume_pass=s.relative_volume_pass,
            btc_uptrend_pass=s.btc_uptrend_pass,
        )
        for s in signals
    ]


def apply_cascade(signals: list[Signal], min_rr: float) -> tuple[list[Signal], int]:
    kept = []
    skipped = 0
    for s in signals:
        sl_dist = max(s.level - s.sl, 1e-9)
        rr1 = (s.tp - s.level) / sl_dist
        if rr1 >= min_rr:
            s.tp_source = "tp1"
            kept.append(s)
            continue
        if s.tp2 > 0:
            rr2 = (s.tp2 - s.level) / sl_dist
            if rr2 >= min_rr:
                s.tp = s.tp2
                s.tp_source = "tp2"
                s.raw_rr = rr2
                kept.append(s)
                continue
        skipped += 1
    return kept, skipped


def filter_signals(signals: list[Signal], cfg: Config) -> list[Signal]:
    """All filters are SOFT — each applies only when its config value is enabled
    (>0 for numeric thresholds, True for boolean toggles). With all filters at
    their 'off' values, every emitted signal is published (only per-token cap
    still applies).
    """
    signals.sort(key=lambda s: s.breakout_ts)
    active_until: dict[str, pd.Timestamp] = {}
    published: list[Signal] = []
    for s in signals:
        # Regime: BTC RSI floor
        if cfg.btc_rsi_floor > 0 and s.btc_rsi < cfg.btc_rsi_floor:
            s.skip_reason = "btc_rsi_floor"
            continue
        # TA-style daily filters
        if cfg.mt_not_downtrend and not s.mt_not_downtrend_pass:
            s.skip_reason = "mt_not_downtrend"
            continue
        if cfg.relative_volume and not s.relative_volume_pass:
            s.skip_reason = "relative_volume"
            continue
        if cfg.btc_uptrend and not s.btc_uptrend_pass:
            s.skip_reason = "btc_uptrend"
            continue
        # Token momentum
        if cfg.min_token_rsi > 0 and s.token_rsi < cfg.min_token_rsi:
            s.skip_reason = "token_rsi_min"
            continue
        if cfg.rsi_cap > 0 and s.token_rsi > cfg.rsi_cap:
            s.skip_reason = "rsi_cap"
            continue
        # Level quality (confluence OR touches — same OR-semantics as v2)
        passes_quality = (
            (cfg.min_confluence > 0 and s.confluence >= cfg.min_confluence) or
            (cfg.min_touches > 0 and s.touches >= cfg.min_touches) or
            (cfg.min_confluence == 0 and cfg.min_touches == 0)
        )
        if not passes_quality:
            s.skip_reason = "quality"
            continue
        # Per-token cap (always on — structural, not a tuning knob)
        last_exp = active_until.get(s.symbol)
        if last_exp is not None and s.breakout_ts < last_exp:
            s.skip_reason = "per_token_cap"
            continue
        s.published = True
        s.passed_quality = passes_quality
        s.passed_momentum = True
        published.append(s)
        active_until[s.symbol] = s.expires_at
    return published


def evaluate_signal(s: Signal, bars: pd.DataFrame, max_hold_days: int) -> None:
    idx = bars.index
    start_pos = idx.searchsorted(s.breakout_ts)
    if start_pos >= len(idx) - 1:
        return

    for pos in range(start_pos + 1, len(idx)):
        ts = idx[pos]
        if ts > s.expires_at:
            break
        if float(bars.iloc[pos]["low"]) <= s.level:
            s.filled = True
            s.fill_ts = ts
            break

    if not s.filled:
        return

    fill_pos = idx.searchsorted(s.fill_ts)
    entry_price = s.level
    for pos in range(fill_pos, len(idx)):
        ts = idx[pos]
        bar = bars.iloc[pos]
        high = float(bar["high"]); low = float(bar["low"]); close = float(bar["close"])
        elapsed_days = (ts - s.fill_ts).total_seconds() / 86400

        if pos == fill_pos:
            if elapsed_days >= max_hold_days:
                s.exit_ts, s.exit_price, s.exit_reason = ts, close, "TIMEOUT"
                break
            continue

        if low <= s.sl:
            s.exit_ts, s.exit_price, s.exit_reason = ts, s.sl, "SL"
            break
        if high >= s.tp:
            s.exit_ts, s.exit_price, s.exit_reason = ts, s.tp, "TP"
            break
        if elapsed_days >= max_hold_days:
            s.exit_ts, s.exit_price, s.exit_reason = ts, close, "TIMEOUT"
            break

    if s.exit_ts is None:
        last_ts = idx[-1]
        last_close = float(bars.iloc[-1]["close"])
        s.exit_ts, s.exit_price, s.exit_reason = last_ts, last_close, "OPEN"

    s.pnl_pct = (s.exit_price - entry_price) / entry_price * 100 - TRADING_FEE * 200
    s.hold_hours = int((s.exit_ts - s.fill_ts).total_seconds() / 3600)


# ─────────────────────────────────────────────────────────────────────────────
# Score one combo
# ─────────────────────────────────────────────────────────────────────────────

def build_filter_arrays(signals: list[Signal]) -> dict:
    """Pre-compute numpy arrays of per-signal filter inputs. Built ONCE per
    detection cache entry so that combo scoring can vectorize the soft-filter
    step instead of iterating Python lists 250k times."""
    return {
        "btc_rsi":      np.array([s.btc_rsi for s in signals]),
        "token_rsi":    np.array([s.token_rsi for s in signals]),
        "confluence":   np.array([s.confluence for s in signals]),
        "touches":      np.array([s.touches for s in signals]),
        "mt_pass":      np.array([s.mt_not_downtrend_pass for s in signals]),
        "rv_pass":      np.array([s.relative_volume_pass for s in signals]),
        "btc_up_pass":  np.array([s.btc_uptrend_pass for s in signals]),
    }


def pre_filter_indices(arrays: dict, cfg: Config) -> np.ndarray:
    """Indices of signals passing every enabled soft filter."""
    n = len(arrays["btc_rsi"])
    mask = np.ones(n, dtype=bool)
    if cfg.btc_rsi_floor > 0:
        mask &= arrays["btc_rsi"] >= cfg.btc_rsi_floor
    if cfg.mt_not_downtrend:
        mask &= arrays["mt_pass"]
    if cfg.relative_volume:
        mask &= arrays["rv_pass"]
    if cfg.btc_uptrend:
        mask &= arrays["btc_up_pass"]
    if cfg.min_token_rsi > 0:
        mask &= arrays["token_rsi"] >= cfg.min_token_rsi
    if cfg.rsi_cap > 0:
        mask &= arrays["token_rsi"] <= cfg.rsi_cap
    if cfg.min_confluence > 0 or cfg.min_touches > 0:
        qmask = np.zeros(n, dtype=bool)
        if cfg.min_confluence > 0:
            qmask |= arrays["confluence"] >= cfg.min_confluence
        if cfg.min_touches > 0:
            qmask |= arrays["touches"] >= cfg.min_touches
        mask &= qmask
    return np.where(mask)[0]


def score_combo(
    cfg: Config,
    cached_signals: list[Signal],
    arrays: dict,
    bars_4h: dict[str, pd.DataFrame],
) -> dict:
    # 1. Vectorized soft-filter pass — cuts 40k+ signals to a few hundred fast
    indices = pre_filter_indices(arrays, cfg)
    candidates = [cached_signals[i] for i in indices]

    # 2. Fresh copies for combo-local mutation (only on survivors)
    sigs = fresh_copies(candidates, RETEST_TTL_HOURS_FIXED)

    # 3. TP cascade (may further reduce / re-target)
    n_cascade_skipped = 0
    if cfg.tp_cascade:
        sigs, n_cascade_skipped = apply_cascade(sigs, cfg.min_rr)

    # 4. Per-token cap (chronological — has to be a Python loop)
    sigs.sort(key=lambda s: s.breakout_ts)
    active_until: dict[str, pd.Timestamp] = {}
    published: list[Signal] = []
    for s in sigs:
        last_exp = active_until.get(s.symbol)
        if last_exp is not None and s.breakout_ts < last_exp:
            continue
        s.published = True
        published.append(s)
        active_until[s.symbol] = s.expires_at

    # 5. Evaluate (bar walk per surviving signal)
    for s in published:
        bars = bars_4h.get(s.symbol)
        if bars is None:
            continue
        evaluate_signal(s, bars, cfg.max_hold_days)

    n = len(published)
    filled = [s for s in published if s.filled]
    nf = len(filled)
    wins = [s for s in filled if s.pnl_pct > 0]
    losses = [s for s in filled if s.pnl_pct <= 0]

    avg_win = float(np.mean([s.pnl_pct for s in wins])) if wins else 0.0
    avg_loss = float(np.mean([s.pnl_pct for s in losses])) if losses else 0.0
    avg_pnl_filled = float(np.mean([s.pnl_pct for s in filled])) if filled else 0.0
    expectancy = avg_pnl_filled * (nf / n) if n else 0.0

    tp = sum(1 for s in filled if s.exit_reason == "TP")
    sl = sum(1 for s in filled if s.exit_reason == "SL")
    to_ = sum(1 for s in filled if s.exit_reason == "TIMEOUT")

    score = expectancy * np.sqrt(max(nf, 1))

    return {
        "sr_max_distance_atr": cfg.sr_max_distance_atr,
        "sr_sl_atr_mult": cfg.sr_sl_atr_mult,
        "major_only": cfg.major_only,
        "btc_rsi_floor": cfg.btc_rsi_floor,
        "mt_not_downtrend": cfg.mt_not_downtrend,
        "relative_volume": cfg.relative_volume,
        "btc_uptrend": cfg.btc_uptrend,
        "rsi_cap": cfg.rsi_cap,
        "min_token_rsi": cfg.min_token_rsi,
        "min_confluence": cfg.min_confluence,
        "min_touches": cfg.min_touches,
        "tp_cascade": cfg.tp_cascade,
        "min_rr": cfg.min_rr,
        "max_hold_days": cfg.max_hold_days,
        "cascade_skipped": n_cascade_skipped,
        "n_published": int(n),
        "n_filled": int(nf),
        "fill_rate_pct": round(100 * nf / max(n, 1), 2),
        "win_rate_pct": round(100 * len(wins) / max(nf, 1), 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "avg_pnl_filled_pct": round(avg_pnl_filled, 3),
        "expectancy_per_signal_pct": round(expectancy, 3),
        "score": round(score, 3),
        "n_tp": int(tp), "n_sl": int(sl), "n_timeout": int(to_),
    }


def monotonicity_per_knob(rows: list[dict], knob: str) -> str:
    df = pd.DataFrame(rows)
    agg = (df.groupby(knob)["expectancy_per_signal_pct"]
             .agg(["mean", "median", "count"])
             .reset_index()
             .sort_values(knob))
    lines = [f"**{knob}** — avg expectancy across combos at each value:", ""]
    lines.append("| value | n_combos | mean exp% | median exp% |")
    lines.append("|------|----------|-----------|-------------|")
    for _, r in agg.iterrows():
        lines.append(f"| {r[knob]} | {int(r['count'])} | "
                     f"{r['mean']:+.3f}% | {r['median']:+.3f}% |")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="SR Buyback full grid sweep")
    parser.add_argument("--universe", default="selected17",
                        choices=list(UNIVERSES.keys()))
    parser.add_argument("--quick", action="store_true",
                        help="Use ~256-combo coarse grid")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")

    grid = QUICK_GRID if args.quick else DEFAULT_GRID
    combos = expand_grid(grid)
    log.info("Sweep: %d combos (universe=%s, %s grid)",
             len(combos), args.universe, "quick" if args.quick else "full")

    needed = UNIVERSES[args.universe]["tokens"]
    hourly = load_hourly(needed)
    daily_ohlcv = aggregate_to_daily(hourly)
    bars_4h = aggregate_to_4h(hourly)

    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    # Cache detection per (sr_max_distance_atr, sr_sl_atr_mult, major_only)
    detect_keys = sorted({
        (c.sr_max_distance_atr, c.sr_sl_atr_mult, c.major_only)
        for c in combos
    })
    detection: dict[tuple, list[Signal]] = {}
    arrays_cache: dict[tuple, dict] = {}
    for md, slm, mo in detect_keys:
        t0 = time.time()
        sigs = detect_sr_buyback(
            args.universe, hourly, sr_cache, daily_ohlcv,
            sr_max_distance_atr=md, sr_sl_atr_mult=slm, major_only=mo,
        )
        detection[(md, slm, mo)] = sigs
        arrays_cache[(md, slm, mo)] = build_filter_arrays(sigs)
        log.info("Detect max_dist=%.1f sl_mult=%.1f major_only=%s: %d signals (%.1fs)",
                 md, slm, mo, len(sigs), time.time() - t0)

    rows: list[dict] = []
    t0 = time.time()
    for i, cfg in enumerate(combos):
        key = (cfg.sr_max_distance_atr, cfg.sr_sl_atr_mult, cfg.major_only)
        cached = detection[key]
        arrays = arrays_cache[key]
        row = score_combo(cfg, cached, arrays, bars_4h)
        rows.append(row)
        if (i + 1) % 500 == 0 or (i + 1) == len(combos):
            log.info("Scored %d/%d (%.0f%%)  elapsed=%.1fs",
                     i + 1, len(combos), 100 * (i + 1) / len(combos), time.time() - t0)

    rows.sort(key=lambda r: (r["expectancy_per_signal_pct"], r["score"]), reverse=True)
    csv_path = REPORT_DIR / f"sr_sweep_results_{stamp}.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    log.info("Wrote %d combo rows to %s", len(rows), csv_path.name)

    md = []
    md.append(f"# SR Buyback sweep summary — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    md.append("")
    md.append(f"- Universe: `{args.universe}`")
    md.append(f"- Combos evaluated: **{len(rows)}**")
    md.append(f"- Grid: {'quick (256 combos)' if args.quick else 'full (2,304 combos)'}")
    md.append("- Score: `expectancy_per_signal × sqrt(n_filled)`")
    md.append("- Fixed: sr_min_distance_atr=0.2, retest_ttl_hours=168, btc_rsi_floor=50")
    md.append("")

    md.append(f"## Top {args.top} by expectancy per signal")
    md.append("")
    md.append("| # | max_d | sl_m | maj | btcF | mtND | relV | rsiC | rsiMin | conf | tch | cas | minRR | hold | n_pub | n_fill | fill% | WR% | avg_w/l | **exp%/sig** | score |")
    md.append("|---|-------|------|-----|------|------|------|------|--------|------|-----|-----|-------|------|-------|--------|-------|-----|---------|--------------|-------|")
    for i, r in enumerate(rows[:args.top], 1):
        md.append(
            f"| {i} | {r['sr_max_distance_atr']} | {r['sr_sl_atr_mult']} | "
            f"{'Y' if r['major_only'] else 'N'} | "
            f"{r['btc_rsi_floor']} | "
            f"{'Y' if r['mt_not_downtrend'] else 'N'} | "
            f"{'Y' if r['relative_volume'] else 'N'} | "
            f"{r['rsi_cap']} | "
            f"{r['min_token_rsi']} | "
            f"{r['min_confluence']} | {r['min_touches']} | "
            f"{'Y' if r['tp_cascade'] else 'N'} | "
            f"{r['min_rr']} | "
            f"{r['max_hold_days']} | "
            f"{r['n_published']} | {r['n_filled']} | "
            f"{r['fill_rate_pct']:.1f}% | {r['win_rate_pct']:.1f}% | "
            f"{r['avg_win_pct']:+.2f}/{r['avg_loss_pct']:+.2f} | "
            f"**{r['expectancy_per_signal_pct']:+.3f}%** | {r['score']:.2f} |"
        )
    md.append("")

    md.append("## Bottom 5 (worst expectancy)")
    md.append("")
    md.append("| max_d | sl_m | maj | btcF | mtND | relV | rsiC | rsiMin | conf | tch | cas | minRR | hold | n_pub | n_fill | WR% | exp%/sig |")
    md.append("|-------|------|-----|------|------|------|------|--------|------|-----|-----|-------|------|-------|--------|-----|---------|")
    for r in rows[-5:]:
        md.append(
            f"| {r['sr_max_distance_atr']} | {r['sr_sl_atr_mult']} | "
            f"{'Y' if r['major_only'] else 'N'} | "
            f"{r['btc_rsi_floor']} | "
            f"{'Y' if r['mt_not_downtrend'] else 'N'} | "
            f"{'Y' if r['relative_volume'] else 'N'} | "
            f"{r['rsi_cap']} | "
            f"{r['min_token_rsi']} | "
            f"{r['min_confluence']} | {r['min_touches']} | "
            f"{'Y' if r['tp_cascade'] else 'N'} | "
            f"{r['min_rr']} | "
            f"{r['max_hold_days']} | "
            f"{r['n_published']} | {r['n_filled']} | "
            f"{r['win_rate_pct']:.1f}% | {r['expectancy_per_signal_pct']:+.3f}% |"
        )
    md.append("")

    md.append("## Per-knob effect (avg expectancy at each value)")
    md.append("")
    md.append("Reveals which knobs actually move the needle.")
    md.append("")
    for knob in ("sr_max_distance_atr", "sr_sl_atr_mult", "major_only",
                 "btc_rsi_floor", "mt_not_downtrend", "relative_volume",
                 "btc_uptrend",
                 "rsi_cap", "min_token_rsi", "min_confluence", "min_touches",
                 "tp_cascade", "min_rr", "max_hold_days"):
        md.append(monotonicity_per_knob(rows, knob))
        md.append("")

    positive = sum(1 for r in rows if r["expectancy_per_signal_pct"] > 0)
    md.append("## Sanity")
    md.append("")
    md.append(f"- Combos with positive expectancy: **{positive} / {len(rows)} "
              f"({100*positive/len(rows):.1f}%)**")
    md.append(f"- Best expectancy: {rows[0]['expectancy_per_signal_pct']:+.3f}%/signal")
    md.append(f"- Worst expectancy: {rows[-1]['expectancy_per_signal_pct']:+.3f}%/signal")
    md.append("")
    md.append(f"_Full results: `{csv_path.name}`_")

    md_path = REPORT_DIR / f"sr_sweep_summary_{stamp}.md"
    md_text = "\n".join(md)
    md_path.write_text(md_text)

    finished = datetime.now(timezone.utc)
    log.info("DONE %.1fmin — summary: %s",
             (finished - started).total_seconds() / 60, md_path)
    print("\n" + "=" * 70)
    print(md_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
