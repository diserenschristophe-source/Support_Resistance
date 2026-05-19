#!/usr/bin/env python3
"""
bnr_sweep.py — Full grid sweep for break-and-retest signal-service.
====================================================================

Mirrors trading-system/research/ta_5_7_filter_sweep.py methodology
adapted for the BNR strategy:

  - Six sweep knobs (Cartesian grid)
  - Detection cached per breakout_atr_mult (only 4 detection runs needed
    even though there are thousands of combos)
  - Score each combo by per-signal expectancy; rank
  - Per-knob monotonicity check (does tightening monotonically improve?)
  - Output: all_combos_ranked.csv + markdown summary

Knobs swept (default grid = 3,072 combos):

  breakout_atr_mult     in {0.5, 1.0, 1.5, 2.0}   ← 4
  min_confluence        in {0, 30, 50, 70}        ← 4
  min_touches           in {0, 2, 3, 5}           ← 4
  min_token_rsi         in {0, 50, 55, 60}        ← 4
  retest_ttl_hours      in {48, 72, 96, 168}      ← 4
  max_hold_days         in {3, 7, 14}             ← 3

Held constant (matching v2 baseline):
  btc_rsi_floor = 50.0
  per_token_cap = 1
  sl_atr_mult_fallback = 1.0
  trading_fee = 3 bps × 2 sides

Reuses from backtest_intraday + backtest_break_retest_v2.

Output:
  reports/backtest_break_retest_v2/bnr_sweep_results.csv
  reports/backtest_break_retest_v2/bnr_sweep_summary.md

Usage:
  python3 research/bnr_sweep.py --universe selected17
  python3 research/bnr_sweep.py --universe selected17 --quick    # ~64 combos
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
import time
from dataclasses import dataclass, field
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
log = logging.getLogger("bnr_sweep")

REPORT_DIR = ROOT / "reports" / "backtest_break_retest_v2"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"

BTC_RSI_FLOOR = 50.0


# ─────────────────────────────────────────────────────────────────────────────
# Config + grids
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    breakout_atr_mult: float
    min_confluence: float
    min_touches: int
    min_token_rsi: float
    retest_ttl_hours: int
    max_hold_days: int
    major_only: bool
    tp_cascade: bool
    min_rr: float = 2.0          # only used when tp_cascade=True


# Reduced from earlier wide grid based on findings:
#   - min_confluence: 30/70 showed no monotonic effect; keep 0 vs 50
#   - min_touches: 2/5 same
#   - min_token_rsi: 50/60 dropped; keep 0 vs 55
#   - retest_ttl_hours: 48/96 dropped; keep 72 vs 168
#   - max_hold_days: extended to {7,14,21,30}
#   - Added major_only and tp_cascade as binary knobs
DEFAULT_GRID = {
    "breakout_atr_mult": [0.5, 1.0, 1.5, 2.0],     # 4
    "min_confluence":    [0.0, 50.0],              # 2
    "min_touches":       [0, 3],                   # 2
    "min_token_rsi":     [0.0, 55.0],              # 2
    "retest_ttl_hours":  [72, 168],                # 2
    "max_hold_days":     [7, 14, 21, 30],          # 4
    "major_only":        [False, True],            # 2
    "tp_cascade":        [False, True],            # 2
}                                                  # = 1024 combos

QUICK_GRID = {
    "breakout_atr_mult": [0.5, 1.5],
    "min_confluence":    [0.0, 50.0],
    "min_touches":       [0, 3],
    "min_token_rsi":     [0.0, 55.0],
    "retest_ttl_hours":  [72, 168],
    "max_hold_days":     [7, 14],
    "major_only":        [False, True],
    "tp_cascade":        [False, True],
}                                                  # = 256 combos


def expand_grid(grid: dict) -> list[Config]:
    keys = list(grid.keys())
    vals = [grid[k] for k in keys]
    combos = list(itertools.product(*vals))
    return [Config(**dict(zip(keys, c))) for c in combos]


# ─────────────────────────────────────────────────────────────────────────────
# Parametrized detect / filter / evaluate
# ─────────────────────────────────────────────────────────────────────────────

def detect_for_atr(
    universe_name: str,
    hourly: dict[str, pd.DataFrame],
    sr_cache: dict,
    daily_ohlcv: dict[str, pd.DataFrame],
    breakout_atr_mult: float,
    major_only: bool = False,
    sl_atr_mult_fallback: float = 1.0,
) -> list[Signal]:
    """Detect breakouts under a given ATR magnitude threshold. TTL not set here
    — applied later by the filter/eval to allow TTL to be a sweep knob."""
    uconf = UNIVERSES[universe_name]
    available = [t for t in uconf["tokens"] if t in hourly]
    if "BTC" not in hourly:
        return []

    bars_4h = aggregate_to_4h(hourly)
    atr_daily = daily_atr_series(daily_ohlcv)
    close_4h = {s: bars_4h[s]["close"] for s in available}
    close_prev_4h = {s: close_4h[s].shift(1) for s in available}

    common_idx = bars_4h[available[0]].index
    for sym in available[1:]:
        common_idx = common_idx.intersection(bars_4h[sym].index)
    common_idx = common_idx.sort_values()

    daily_close_df = pd.DataFrame(
        {s: d["close"] for s, d in daily_ohlcv.items() if s in available}
    ).sort_index()
    daily_close_np = {s: daily_close_df[s].to_numpy(dtype=float) for s in available}
    daily_index = daily_close_df.index

    def last_closed_daily_idx(ts: pd.Timestamp) -> int:
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
        if pd.isna(btc_rsi) or btc_rsi < BTC_RSI_FLOOR:
            continue

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
            if not resistances:
                continue
            atr_val = atr_daily.get(sym, pd.Series()).get(d_date, np.nan)
            if pd.isna(atr_val) or atr_val <= 0:
                continue

            cn = float(close_4h[sym].loc[ts])
            cp_raw = close_prev_4h[sym].loc[ts]
            if pd.isna(cp_raw):
                continue
            cp = float(cp_raw)

            broken: dict | None = None
            for r_dict in resistances:
                r = r_dict["price"]
                if cn > r + breakout_atr_mult * atr_val and cp > r:
                    broken = r_dict
                else:
                    break
            if broken is None:
                continue

            r_price = broken["price"]
            above = sorted([d for d in resistances if d["price"] > r_price],
                           key=lambda d: d["price"])
            tp = above[0]["price"] if above else None
            tp2 = above[1]["price"] if len(above) > 1 else 0.0
            below = [d["price"] for d in (supports + resistances) if d["price"] < r_price]
            sl = max(below) if below else r_price - sl_atr_mult_fallback * atr_val

            if tp is None or tp <= r_price or sl >= r_price:
                continue

            sym_hist = daily_close_np[sym][:d_idx + 1][-RSI_HISTORY_BARS:]
            tok_rsi = partial_bar_rsi(sym_hist, cn, RSI_PERIOD)

            signals.append(Signal(
                symbol=sym,
                breakout_ts=ts,
                breakout_close=cn,
                level=float(r_price),
                tp=float(tp),
                sl=float(sl),
                expires_at=ts,                   # placeholder; set in filter
                confluence=float(broken["confluence"]),
                touches=int(broken["touches"]),
                tier=str(broken.get("tier", "")),
                breakout_magnitude_atr=float((cn - r_price) / atr_val),
                token_rsi=float(tok_rsi) if not pd.isna(tok_rsi) else 0.0,
                btc_rsi=float(btc_rsi),
                raw_rr=float((tp - r_price) / max(r_price - sl, 1e-9)),
                tp2=float(tp2),
            ))
    return signals


def fresh_copies(signals: list[Signal], ttl_hours: int) -> list[Signal]:
    """Build a fresh list with reset outcome state and TTL applied."""
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
        )
        for s in signals
    ]


def apply_cascade(signals: list[Signal], min_rr: float) -> tuple[list[Signal], int]:
    """Apply TP cascade in place. Returns (kept_signals, n_skipped).

    For each signal:
      - RR1 = (tp - level) / (level - sl)
      - If RR1 >= min_rr: keep, tp_source = "tp1"
      - Else if tp2 > 0 and RR2 >= min_rr: tp = tp2, tp_source = "tp2"
      - Else: drop
    """
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
    signals.sort(key=lambda s: s.breakout_ts)
    active_until: dict[str, pd.Timestamp] = {}
    published: list[Signal] = []
    for s in signals:
        s.passed_quality = (s.confluence >= cfg.min_confluence) or (s.touches >= cfg.min_touches)
        s.passed_momentum = s.token_rsi >= cfg.min_token_rsi
        if not s.passed_quality:
            s.skip_reason = "quality"
            continue
        if not s.passed_momentum:
            s.skip_reason = "momentum"
            continue
        last_exp = active_until.get(s.symbol)
        if last_exp is not None and s.breakout_ts < last_exp:
            s.skip_reason = "per_token_cap"
            continue
        s.published = True
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

def score_combo(
    cfg: Config,
    cached_signals: list[Signal],
    bars_4h: dict[str, pd.DataFrame],
) -> dict:
    sigs = fresh_copies(cached_signals, cfg.retest_ttl_hours)
    n_cascade_skipped = 0
    if cfg.tp_cascade:
        sigs, n_cascade_skipped = apply_cascade(sigs, cfg.min_rr)
    published = filter_signals(sigs, cfg)

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

    # Composite: expectancy × sqrt(filled) — penalize tiny samples
    score = expectancy * np.sqrt(max(nf, 1))

    return {
        "breakout_atr_mult": cfg.breakout_atr_mult,
        "min_confluence": cfg.min_confluence,
        "min_touches": cfg.min_touches,
        "min_token_rsi": cfg.min_token_rsi,
        "retest_ttl_hours": cfg.retest_ttl_hours,
        "max_hold_days": cfg.max_hold_days,
        "major_only": cfg.major_only,
        "tp_cascade": cfg.tp_cascade,
        "min_rr": cfg.min_rr,
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


# ─────────────────────────────────────────────────────────────────────────────
# Per-knob monotonicity check
# ─────────────────────────────────────────────────────────────────────────────

def monotonicity_per_knob(rows: list[dict], knob: str) -> str:
    """For each value of `knob`, the avg expectancy across all other combos.
    Reveals whether tightening this knob monotonically improves the metric."""
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
    parser = argparse.ArgumentParser(description="BNR full grid sweep")
    parser.add_argument("--universe", default="selected17",
                        choices=list(UNIVERSES.keys()))
    parser.add_argument("--quick", action="store_true",
                        help="Use 64-combo coarse grid instead of full 3,072")
    parser.add_argument("--top", type=int, default=20,
                        help="How many top combos to highlight in summary")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")

    grid = QUICK_GRID if args.quick else DEFAULT_GRID
    combos = expand_grid(grid)
    log.info("Sweep: %d combos (universe=%s, %s grid)",
             len(combos), args.universe, "quick" if args.quick else "full")

    # Load data + cache
    needed = UNIVERSES[args.universe]["tokens"]
    hourly = load_hourly(needed)
    daily_ohlcv = aggregate_to_daily(hourly)
    bars_4h = aggregate_to_4h(hourly)

    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    # Detect once per (breakout_atr_mult, major_only) pair
    detect_keys = sorted({(c.breakout_atr_mult, c.major_only) for c in combos})
    detection: dict[tuple[float, bool], list[Signal]] = {}
    for am, mo in detect_keys:
        t0 = time.time()
        detection[(am, mo)] = detect_for_atr(
            args.universe, hourly, sr_cache, daily_ohlcv, am, major_only=mo
        )
        log.info("Detection atr_mult=%.1f major_only=%s: %d raw signals (%.1fs)",
                 am, mo, len(detection[(am, mo)]), time.time() - t0)

    # Score each combo
    rows: list[dict] = []
    t0 = time.time()
    for i, cfg in enumerate(combos):
        cached = detection[(cfg.breakout_atr_mult, cfg.major_only)]
        row = score_combo(cfg, cached, bars_4h)
        rows.append(row)
        if (i + 1) % 200 == 0 or (i + 1) == len(combos):
            log.info("Scored %d/%d (%.0f%%)  elapsed=%.1fs",
                     i + 1, len(combos), 100 * (i + 1) / len(combos), time.time() - t0)

    # Rank + write CSV
    rows.sort(key=lambda r: (r["expectancy_per_signal_pct"], r["score"]), reverse=True)
    csv_path = REPORT_DIR / f"bnr_sweep_results_{stamp}.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    log.info("Wrote %d combo rows to %s", len(rows), csv_path.name)

    # Markdown summary
    md = []
    md.append(f"# BNR sweep summary — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    md.append("")
    md.append(f"- Universe: `{args.universe}`")
    md.append(f"- Combos evaluated: **{len(rows)}**")
    md.append(f"- Grid: {'quick (64 combos)' if args.quick else 'full (3,072 combos)'}")
    md.append("- Score: `expectancy_per_signal × sqrt(n_filled)` (penalizes tiny samples)")
    md.append("")

    md.append(f"## Top {args.top} by expectancy per signal")
    md.append("")
    md.append("| Rank | atr | maj | cas | conf | tch | rsi | ttl | hold | n_pub | n_fill | fill% | WR% | avg_w/l | **exp%/sig** | score |")
    md.append("|------|-----|-----|-----|------|-----|-----|-----|------|-------|--------|-------|-----|---------|--------------|-------|")
    for i, r in enumerate(rows[:args.top], 1):
        md.append(
            f"| {i} | {r['breakout_atr_mult']} | "
            f"{'Y' if r['major_only'] else 'N'} | "
            f"{'Y' if r['tp_cascade'] else 'N'} | "
            f"{r['min_confluence']} | {r['min_touches']} | {r['min_token_rsi']} | "
            f"{r['retest_ttl_hours']} | {r['max_hold_days']} | "
            f"{r['n_published']} | {r['n_filled']} | "
            f"{r['fill_rate_pct']:.1f}% | {r['win_rate_pct']:.1f}% | "
            f"{r['avg_win_pct']:+.2f}/{r['avg_loss_pct']:+.2f} | "
            f"**{r['expectancy_per_signal_pct']:+.3f}%** | {r['score']:.2f} |"
        )
    md.append("")

    # Bottom 5 (worst)
    md.append("## Bottom 5 (worst expectancy)")
    md.append("")
    md.append("| atr | maj | cas | conf | tch | rsi | ttl | hold | n_pub | n_fill | WR% | exp%/sig |")
    md.append("|-----|-----|-----|------|-----|-----|-----|------|-------|--------|-----|---------|")
    for r in rows[-5:]:
        md.append(
            f"| {r['breakout_atr_mult']} | "
            f"{'Y' if r['major_only'] else 'N'} | "
            f"{'Y' if r['tp_cascade'] else 'N'} | "
            f"{r['min_confluence']} | {r['min_touches']} | {r['min_token_rsi']} | "
            f"{r['retest_ttl_hours']} | {r['max_hold_days']} | "
            f"{r['n_published']} | {r['n_filled']} | "
            f"{r['win_rate_pct']:.1f}% | {r['expectancy_per_signal_pct']:+.3f}% |"
        )
    md.append("")

    # Per-knob monotonicity
    md.append("## Per-knob effect (avg expectancy at each value)")
    md.append("")
    md.append("Reveals whether tightening a knob monotonically improves the metric.")
    md.append("If yes → that knob matters. If flat → that knob is noise.")
    md.append("")
    for knob in ("breakout_atr_mult", "min_confluence", "min_touches",
                 "min_token_rsi", "retest_ttl_hours", "max_hold_days",
                 "major_only", "tp_cascade"):
        md.append(monotonicity_per_knob(rows, knob))
        md.append("")

    # How many combos had positive expectancy?
    positive = sum(1 for r in rows if r["expectancy_per_signal_pct"] > 0)
    md.append("## Sanity")
    md.append("")
    md.append(f"- Combos with positive expectancy: **{positive} / {len(rows)} "
              f"({100*positive/len(rows):.1f}%)**")
    md.append(f"- Best expectancy: {rows[0]['expectancy_per_signal_pct']:+.3f}%/signal")
    md.append(f"- Worst expectancy: {rows[-1]['expectancy_per_signal_pct']:+.3f}%/signal")
    md.append("")
    md.append(f"_Full results: `{csv_path.name}`_")

    md_path = REPORT_DIR / f"bnr_sweep_summary_{stamp}.md"
    md_text = "\n".join(md)
    md_path.write_text(md_text)

    finished = datetime.now(timezone.utc)
    log.info("DONE %.1fmin — summary: %s", (finished - started).total_seconds() / 60, md_path)
    print("\n" + "=" * 70)
    print(md_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
