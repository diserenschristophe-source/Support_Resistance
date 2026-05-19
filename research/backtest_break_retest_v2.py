#!/usr/bin/env python3
"""
backtest_break_retest_v2.py — Signal-service backtest for break-and-retest.
==========================================================================

Reframing from v1: this is a SIGNAL SERVICE, not a personal trading bot.

  1. Phase 1 — Generate every breakout signal (no slot constraint).
  2. Phase 2 — Filter by quality (level confluence/touches), token momentum,
              and per-token dedup (1 active signal per token).
  3. Phase 3 — Evaluate each published signal INDEPENDENTLY (per-signal
              fill + outcome). No capital coupling between signals.
  4. Phase 4 — Funnel + per-signal expectancy metrics.
  5. Phase 5 — "Typical investor" sensitivity: replay signals with a fixed
              capital + slot constraint starting at each month over the
              backtest window, to answer "performance depends when you start".

Output (under reports/backtest_break_retest_v2/):
  sweep_<UTC>.md                              — markdown report
  selected17_signals.csv                      — every published signal w/ outcome
  selected17_sensitivity.csv                  — start-month investor sim

Reuses from backtest_intraday:
  load_hourly, aggregate_to_4h, aggregate_to_daily, build_sr_cache_full,
  partial_bar_rsi, UNIVERSES, constants.

CLI:
  python3 research/backtest_break_retest_v2.py
  python3 research/backtest_break_retest_v2.py --universe hl44
  python3 research/backtest_break_retest_v2.py --rebuild-sr
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
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
    MAX_HOLD_DAYS,
    TRADING_FEE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bnr_v2")


# ─────────────────────────────────────────────────────────────────────────────
# Strategy params
# ─────────────────────────────────────────────────────────────────────────────

# Breakout detection
ATR_PERIOD = 14
BREAKOUT_ATR_MULT = 0.5
RETEST_TTL_HOURS = 72            # 3 days

# Regime
BTC_RSI_FLOOR = 50.0

# Quality filter (level)
MIN_CONFLUENCE = 50.0            # ≥ this OR touches ≥ MIN_TOUCHES
MIN_TOUCHES = 3

# Momentum filter (token)
MIN_TOKEN_RSI = 50.0

# Position management
SL_ATR_MULT_FALLBACK = 1.0       # if no level exists below broken

# Per-token dedup: only 1 active (unfilled, unexpired) signal per token
PER_TOKEN_CAP = 1

# Use only Major-tier levels (from sr_analysis2) for breakout, TP, SL.
# Filters out Minor levels (noise) — overrideable via --major-only CLI flag.
MAJOR_ONLY = False

# TP cascade rule: ensure every signal has at least MIN_RR risk-reward.
# If RR1 >= MIN_RR → use TP1 (closer, more likely to hit).
# Else if RR2 >= MIN_RR → escalate to TP2 (force adequate RR).
# Else → skip signal (no acceptable target).
TP_CASCADE = False
TP_CASCADE_MIN_RR = 2.0

# Override for max-hold days (default uses MAX_HOLD_DAYS from backtest_intraday)
MAX_HOLD_DAYS_OVERRIDE: int | None = None


def effective_max_hold() -> int:
    return MAX_HOLD_DAYS_OVERRIDE if MAX_HOLD_DAYS_OVERRIDE is not None else MAX_HOLD_DAYS

# Typical-investor sim
SIM_INITIAL_CAPITAL = 10_000.0
SIM_MAX_SLOTS = 4
SIM_PER_SLOT_FRACTION = 1.0 / SIM_MAX_SLOTS

REPORT_DIR = ROOT / "reports" / "backtest_break_retest_v2"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    # Identity
    symbol: str
    breakout_ts: pd.Timestamp
    breakout_close: float

    # Setup (what we publish to investors)
    level: float                      # broken resistance — buy limit goes here
    tp: float                         # TP1 — next resistance above broken
    sl: float
    expires_at: pd.Timestamp

    # Quality / context (used for filtering)
    confluence: float
    touches: int
    tier: str                         # "Major" / "Minor" from sr_analysis2
    breakout_magnitude_atr: float     # (close - R) / ATR
    token_rsi: float
    btc_rsi: float

    # Filter decisions
    passed_quality: bool = False
    passed_momentum: bool = False
    published: bool = False
    skip_reason: str = ""             # only set if not published

    # Outcome (filled when evaluated)
    filled: bool = False
    fill_ts: pd.Timestamp | None = None
    exit_ts: pd.Timestamp | None = None
    exit_price: float = 0.0
    exit_reason: str = ""             # "" if unfilled; else TP / SL / TIMEOUT / OPEN
    pnl_pct: float = 0.0
    hold_hours: int = 0
    raw_rr: float = 0.0

    # TP2 research mode — second target & event-tracking timestamps
    tp2: float = 0.0                     # 0.0 = no level above tp1 available
    tp1_hit_ts: pd.Timestamp | None = None
    tp2_hit_ts: pd.Timestamp | None = None
    sl_hit_ts: pd.Timestamp | None = None
    tp1_hold_hours: int = -1             # -1 = not hit
    tp2_hold_hours: int = -1             # -1 = not hit

    # Which target the cascade selected (only meaningful in TP_CASCADE mode)
    tp_source: str = "tp1"               # "tp1" or "tp2"

    # TA-style daily filter pass flags (computed at detection time; defaults
    # True so existing flows that don't compute them still pass through).
    mt_not_downtrend_pass: bool = True
    relative_volume_pass: bool = True
    # BTC-level regime filter: True iff BTC daily close > BTC SMA(100)
    # AND BTC SMA(100) rising over last 20 bars. Defaults True for
    # backward-compat with older detection paths.
    btc_uptrend_pass: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def daily_atr_series(daily_ohlcv: dict[str, pd.DataFrame], period: int = ATR_PERIOD) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for sym, df in daily_ohlcv.items():
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1).fillna(close)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        out[sym] = tr.rolling(period).mean()
    return out


def levels_with_meta(entry: dict) -> tuple[list[dict], list[dict]]:
    """Return (support, resistance) lists of normalized dicts:
       {price: float, confluence: float, touches: int, volume_confirmed: bool}
    """
    def _norm(lst):
        out = []
        for item in lst or []:
            if not isinstance(item, dict):
                continue
            v = item.get("key_level") or item.get("price") or item.get("level")
            if v is None:
                continue
            try:
                price = float(v)
            except (TypeError, ValueError):
                continue
            out.append({
                "price": price,
                "confluence": float(item.get("confluence", 0) or 0),
                "touches": int(item.get("touches", 0) or 0),
                "tier": str(item.get("tier", "") or ""),
                "volume_confirmed": bool(item.get("volume_confirmed", False)),
            })
        return sorted(out, key=lambda d: d["price"])
    return _norm(entry.get("support")), _norm(entry.get("resistance"))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: detect raw breakouts (no quality filtering yet)
# ─────────────────────────────────────────────────────────────────────────────

def detect_breakouts(
    universe_name: str,
    hourly: dict[str, pd.DataFrame],
    sr_cache: dict,
    daily_ohlcv: dict[str, pd.DataFrame],
) -> tuple[list[Signal], dict]:
    """Phase 1: emit a Signal for every (token, 4h bar) with a structural breakout.

    No quality / momentum filtering. No dedup. Multiple signals per bar
    are allowed (one per qualifying token).
    """
    uconf = UNIVERSES[universe_name]
    available = [t for t in uconf["tokens"] if t in hourly]
    missing = [t for t in uconf["tokens"] if t not in hourly]

    log.info("Phase 1 — detect breakouts  universe=%s  tokens=%d/%d",
             universe_name, len(available), len(uconf["tokens"]))
    if missing:
        log.info("  missing tokens: %s", missing)

    if "BTC" not in hourly:
        return [], {"error": "BTC hourly missing"}

    bars_4h = aggregate_to_4h(hourly)
    atr_daily = daily_atr_series(daily_ohlcv)

    close_4h = {s: bars_4h[s]["close"] for s in available}
    close_prev_4h = {s: close_4h[s].shift(1) for s in available}

    # Common 4h bar grid across available tokens
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
    btc_rsi_blocked = 0
    cache_miss = 0
    no_atr = 0
    tp_sl_sanity = 0
    cascade_skipped = 0
    bars_scanned = 0

    for hi in range(len(common_idx)):
        ts = common_idx[hi]
        if hi < 1:
            continue

        d_idx = last_closed_daily_idx(ts)
        if d_idx < MIN_DAILY_HISTORY:
            continue

        # BTC regime gate
        btc_hist = daily_close_np["BTC"][:d_idx + 1][-RSI_HISTORY_BARS:]
        btc_now = float(close_4h["BTC"].loc[ts])
        btc_rsi = partial_bar_rsi(btc_hist, btc_now, RSI_PERIOD)
        bars_scanned += 1
        if pd.isna(btc_rsi) or btc_rsi < BTC_RSI_FLOOR:
            btc_rsi_blocked += 1
            continue

        d_date = daily_index[d_idx]
        d_date_str = str(d_date)[:10]

        for sym in available:
            sr_entry = sr_cache.get((sym, d_date_str))
            if sr_entry is None:
                cache_miss += 1
                continue
            supports, resistances = levels_with_meta(sr_entry)
            if MAJOR_ONLY:
                supports = [l for l in supports if l.get("tier") == "Major"]
                resistances = [l for l in resistances if l.get("tier") == "Major"]
            if not resistances:
                continue

            atr_val = atr_daily.get(sym, pd.Series()).get(d_date, np.nan)
            if pd.isna(atr_val) or atr_val <= 0:
                no_atr += 1
                continue

            cn = float(close_4h[sym].loc[ts])
            cp_raw = close_prev_4h[sym].loc[ts]
            if pd.isna(cp_raw):
                continue
            cp = float(cp_raw)

            # Walk resistance levels ascending; pick highest where 2-bar
            # confirmation + ATR magnitude hold.
            broken: dict | None = None
            for r_dict in resistances:
                r = r_dict["price"]
                if cn > r + BREAKOUT_ATR_MULT * atr_val and cp > r:
                    broken = r_dict
                else:
                    break
            if broken is None:
                continue

            r_price = broken["price"]

            # TP1: next resistance above broken; TP2: next resistance above TP1
            above = sorted([d for d in resistances if d["price"] > r_price],
                           key=lambda d: d["price"])
            tp = above[0]["price"] if above else None
            tp2 = above[1]["price"] if len(above) > 1 else 0.0

            # SL: nearest level below broken from combined S+R
            all_levels_below = [d["price"] for d in (supports + resistances) if d["price"] < r_price]
            sl = max(all_levels_below) if all_levels_below else r_price - SL_ATR_MULT_FALLBACK * atr_val

            if tp is None or tp <= r_price or sl >= r_price:
                tp_sl_sanity += 1
                continue

            # TP cascade: ensure minimum RR by escalating TP1 → TP2 when needed
            tp_source = "tp1"
            if TP_CASCADE:
                sl_dist = max(r_price - sl, 1e-9)
                rr1 = (tp - r_price) / sl_dist
                if rr1 < TP_CASCADE_MIN_RR:
                    if tp2 > 0:
                        rr2 = (tp2 - r_price) / sl_dist
                        if rr2 >= TP_CASCADE_MIN_RR:
                            tp = tp2
                            tp_source = "tp2"
                        else:
                            cascade_skipped += 1
                            continue
                    else:
                        cascade_skipped += 1
                        continue

            # Token RSI at breakout (partial-bar)
            sym_hist = daily_close_np[sym][:d_idx + 1][-RSI_HISTORY_BARS:]
            tok_rsi = partial_bar_rsi(sym_hist, cn, RSI_PERIOD)

            signals.append(Signal(
                symbol=sym,
                breakout_ts=ts,
                breakout_close=cn,
                level=float(r_price),
                tp=float(tp),
                sl=float(sl),
                expires_at=ts + pd.Timedelta(hours=RETEST_TTL_HOURS),
                confluence=float(broken["confluence"]),
                touches=int(broken["touches"]),
                tier=str(broken.get("tier", "")),
                breakout_magnitude_atr=float((cn - r_price) / atr_val),
                token_rsi=float(tok_rsi) if not pd.isna(tok_rsi) else 0.0,
                btc_rsi=float(btc_rsi),
                raw_rr=float((tp - r_price) / max(r_price - sl, 1e-9)),
                tp2=float(tp2),
                tp_source=tp_source,
            ))

    funnel = {
        "bars_scanned": int(bars_scanned),
        "btc_regime_blocked": int(btc_rsi_blocked),
        "cache_miss": int(cache_miss),
        "atr_unavailable": int(no_atr),
        "tp_sl_sanity_failed": int(tp_sl_sanity),
        "cascade_skipped": int(cascade_skipped),
        "raw_breakouts": int(len(signals)),
        "first_bar": str(common_idx[0]) if len(common_idx) else None,
        "last_bar": str(common_idx[-1]) if len(common_idx) else None,
        "tokens_used": available,
        "tokens_missing": missing,
    }
    log.info("Phase 1 done — raw breakouts: %d", len(signals))
    return signals, funnel


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: filter + dedup
# ─────────────────────────────────────────────────────────────────────────────

def filter_signals(signals: list[Signal]) -> tuple[list[Signal], dict]:
    """Apply quality + momentum + per-token-cap filters in order.

    Mutates signals in place (sets passed_*, published, skip_reason).
    Returns (published_subset, funnel_counts).
    """
    # Quality + momentum (mark per-signal flags)
    for s in signals:
        s.passed_quality = (s.confluence >= MIN_CONFLUENCE) or (s.touches >= MIN_TOUCHES)
        s.passed_momentum = s.token_rsi >= MIN_TOKEN_RSI

    # Per-token cap (chronological order)
    signals.sort(key=lambda s: s.breakout_ts)
    active_until: dict[str, pd.Timestamp] = {}     # sym -> last expiry of an active signal

    n_quality_drop = 0
    n_momentum_drop = 0
    n_cap_drop = 0
    published: list[Signal] = []

    for s in signals:
        if not s.passed_quality:
            s.skip_reason = "quality"
            n_quality_drop += 1
            continue
        if not s.passed_momentum:
            s.skip_reason = "momentum"
            n_momentum_drop += 1
            continue
        # per-token cap: only allow if no active signal for this token at breakout_ts
        last_exp = active_until.get(s.symbol)
        if last_exp is not None and s.breakout_ts < last_exp:
            s.skip_reason = "per_token_cap"
            n_cap_drop += 1
            continue
        s.published = True
        published.append(s)
        active_until[s.symbol] = s.expires_at

    funnel = {
        "raw_breakouts": int(len(signals)),
        "quality_dropped": int(n_quality_drop),
        "momentum_dropped": int(n_momentum_drop),
        "per_token_cap_dropped": int(n_cap_drop),
        "published": int(len(published)),
    }
    log.info("Phase 2 done — published signals: %d  (q-drop=%d, m-drop=%d, cap-drop=%d)",
             len(published), n_quality_drop, n_momentum_drop, n_cap_drop)
    return published, funnel


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: per-signal independent evaluation (fill + outcome)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_signal(s: Signal, bars_4h: dict[str, pd.DataFrame]) -> Signal:
    """Independently simulate this signal: fill check, then outcome.

    Fill rule: any 4h bar from breakout_ts+1 to expires_at where low <= level
    fills at level (maker assumption).
    Outcome rule: from fill bar, iterate forward; SL if low <= sl, TP if
    high >= tp (SL wins on same bar), TIMEOUT after MAX_HOLD_DAYS, else OPEN
    if data runs out.
    """
    sym = s.symbol
    bars = bars_4h.get(sym)
    if bars is None:
        return s

    # Find fill bar
    idx = bars.index
    start_pos = idx.searchsorted(s.breakout_ts)
    if start_pos >= len(idx) - 1:
        return s
    # Bars strictly after breakout_ts, up to expiry
    for pos in range(start_pos + 1, len(idx)):
        ts = idx[pos]
        if ts > s.expires_at:
            break
        bar = bars.iloc[pos]
        if float(bar["low"]) <= s.level:
            s.filled = True
            s.fill_ts = ts
            break

    if not s.filled:
        return s

    # Outcome simulation from fill bar (inclusive of fill bar)
    fill_pos = idx.searchsorted(s.fill_ts)
    entry_price = s.level
    for pos in range(fill_pos, len(idx)):
        ts = idx[pos]
        bar = bars.iloc[pos]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        elapsed_days = (ts - s.fill_ts).total_seconds() / 86400

        tp_hit = high >= s.tp
        sl_hit = low <= s.sl

        max_hold = effective_max_hold()
        if pos == fill_pos:
            # On the fill bar itself, SL can't logically fire before fill
            # (we filled at level on the way down). But TP could fire if the
            # bar swept up after our fill. Conservative: ignore both on fill bar.
            if elapsed_days >= max_hold:
                s.exit_ts = ts
                s.exit_price = close
                s.exit_reason = "TIMEOUT"
                break
            continue

        if sl_hit:
            s.exit_ts, s.exit_price, s.exit_reason = ts, s.sl, "SL"
            break
        if tp_hit:
            s.exit_ts, s.exit_price, s.exit_reason = ts, s.tp, "TP"
            break
        if elapsed_days >= max_hold:
            s.exit_ts, s.exit_price, s.exit_reason = ts, close, "TIMEOUT"
            break

    if s.exit_ts is None and s.filled:
        # Ran past end of data
        last_ts = idx[-1]
        last_close = float(bars.iloc[-1]["close"])
        s.exit_ts = last_ts
        s.exit_price = last_close
        s.exit_reason = "OPEN"

    if s.exit_ts is not None:
        # Gross pnl pct, then subtract round-trip fees
        s.pnl_pct = (s.exit_price - entry_price) / entry_price * 100
        s.pnl_pct -= TRADING_FEE * 200       # 2 sides
        s.hold_hours = int((s.exit_ts - s.fill_ts).total_seconds() / 3600)

    return s


def evaluate_signals(signals: list[Signal], hourly: dict[str, pd.DataFrame]) -> None:
    log.info("Phase 3 — evaluating %d signals", len(signals))
    bars_4h = aggregate_to_4h(hourly)
    for s in signals:
        evaluate_signal(s, bars_4h)
    log.info("Phase 3 done")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: per-signal metrics
# ─────────────────────────────────────────────────────────────────────────────

def per_signal_metrics(signals: list[Signal]) -> dict:
    n = len(signals)
    if n == 0:
        return {"n_published": 0}
    filled = [s for s in signals if s.filled]
    nf = len(filled)
    tp = [s for s in filled if s.exit_reason == "TP"]
    sl = [s for s in filled if s.exit_reason == "SL"]
    to = [s for s in filled if s.exit_reason == "TIMEOUT"]
    op = [s for s in filled if s.exit_reason == "OPEN"]

    wins = [s for s in filled if s.pnl_pct > 0]
    losses = [s for s in filled if s.pnl_pct <= 0]

    avg_win = float(np.mean([s.pnl_pct for s in wins])) if wins else 0.0
    avg_loss = float(np.mean([s.pnl_pct for s in losses])) if losses else 0.0
    win_rate = 100 * len(wins) / nf if nf else 0.0
    fill_rate = 100 * nf / n if n else 0.0
    avg_pnl_filled = float(np.mean([s.pnl_pct for s in filled])) if filled else 0.0
    expectancy_per_signal = avg_pnl_filled * (nf / n) if n else 0.0
    avg_rr_entry = float(np.mean([s.raw_rr for s in signals])) if signals else 0.0
    avg_rr_filled = float(np.mean([s.raw_rr for s in filled])) if filled else 0.0
    avg_hold = float(np.mean([s.hold_hours for s in filled])) if filled else 0.0

    return {
        "n_published": int(n),
        "n_filled": int(nf),
        "n_unfilled_expired": int(n - nf),
        "fill_rate_pct": round(fill_rate, 2),
        "n_tp": int(len(tp)), "n_sl": int(len(sl)),
        "n_timeout": int(len(to)), "n_open": int(len(op)),
        "win_rate_filled_pct": round(win_rate, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_pnl_filled_pct": round(avg_pnl_filled, 2),
        "expectancy_per_signal_pct": round(expectancy_per_signal, 2),
        "avg_rr_entry": round(avg_rr_entry, 2),
        "avg_rr_filled": round(avg_rr_filled, 2),
        "avg_hold_hours": round(avg_hold, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: typical-investor sim by start month
# ─────────────────────────────────────────────────────────────────────────────

def typical_investor_sim(
    signals: list[Signal],
    start_ts: pd.Timestamp,
    initial_capital: float = SIM_INITIAL_CAPITAL,
    max_slots: int = SIM_MAX_SLOTS,
    per_slot_fraction: float = SIM_PER_SLOT_FRACTION,
) -> dict:
    """Replay signals chronologically with FIFO slot allocation.

    Free slots take new published signals in order. When a slot closes
    (TP/SL/TIMEOUT/OPEN), its capital returns and is available.
    """
    eligible = [s for s in signals if s.published and s.breakout_ts >= start_ts]
    if not eligible:
        return {"start_ts": str(start_ts)[:10], "n_signals": 0, "final_equity": initial_capital,
                "return_pct": 0.0, "n_taken": 0}

    eligible.sort(key=lambda s: s.breakout_ts)

    # Track active slot occupants and their committed capital
    active: list[tuple[Signal, float]] = []   # (signal, slot_capital_at_fill)
    capital = initial_capital
    realized = 0.0
    n_taken = 0
    n_skipped_full = 0

    # We process events in time order:
    #   - each published signal: at breakout_ts, try to take a slot
    #   - if taken: at fill_ts (if filled) commits the slot's capital
    #   - at exit_ts (if filled): release slot capital + realize pnl
    # Simplification: take-decision at breakout_ts uses # active *positions*
    # (slot is reserved only on fill — between breakout and fill, no capital
    # is locked, so multiple "pending" allowed up to slot limit at fill time).
    # In practice: each signal claims a "future slot" at publish; if at fill
    # time slots are full, the signal is dropped from the sim (skipped).

    # Build event list
    events: list[tuple[pd.Timestamp, str, Signal]] = []
    for s in eligible:
        events.append((s.breakout_ts, "publish", s))
        if s.filled and s.fill_ts is not None:
            events.append((s.fill_ts, "fill", s))
        if s.exit_ts is not None:
            events.append((s.exit_ts, "exit", s))
    events.sort(key=lambda x: (x[0], {"publish": 0, "fill": 1, "exit": 2}[x[1]]))

    # Track per-signal: pending_taken? filled_taken?
    pending_committed: dict[int, float] = {}     # id(signal) -> slot_capital
    open_committed: dict[int, float] = {}

    for ts, kind, s in events:
        sid = id(s)
        if kind == "publish":
            # Try to reserve a slot (FIFO)
            in_use = len(pending_committed) + len(open_committed)
            if in_use < max_slots:
                slot_cap = capital * per_slot_fraction
                pending_committed[sid] = slot_cap
                n_taken += 1
            else:
                n_skipped_full += 1
                continue
        elif kind == "fill":
            if sid in pending_committed:
                cap = pending_committed.pop(sid)
                open_committed[sid] = cap
                # capital is not changed at fill (paper-style sim — slot capital
                # was reserved at publish; we only realize on exit)
        elif kind == "exit":
            if sid in open_committed:
                cap = open_committed.pop(sid)
                pnl = cap * (s.pnl_pct / 100)
                realized += pnl
                capital += pnl
            elif sid in pending_committed:
                # signal published but never filled — release reserved slot, no pnl
                pending_committed.pop(sid)

    # Mark to market: anything still open at end is treated as last-known close
    # (Signal.pnl_pct already reflects OPEN exit_reason)
    final_equity = initial_capital + realized
    return_pct = (final_equity - initial_capital) / initial_capital * 100

    return {
        "start_ts": str(start_ts)[:10],
        "n_signals_eligible": int(len(eligible)),
        "n_taken": int(n_taken),
        "n_skipped_slots_full": int(n_skipped_full),
        "final_equity": round(final_equity, 2),
        "return_pct": round(return_pct, 2),
    }


def sensitivity_by_start_month(signals: list[Signal]) -> list[dict]:
    """Run typical-investor sim starting at the 1st of each month over the
    span of published signals (less the last 30 days to give some runway)."""
    pub = [s for s in signals if s.published]
    if not pub:
        return []
    first = min(s.breakout_ts for s in pub).normalize()
    last = max(s.breakout_ts for s in pub).normalize() - pd.Timedelta(days=30)

    starts = []
    month = first.replace(day=1)
    while month <= last:
        starts.append(month)
        # next month
        if month.month == 12:
            month = month.replace(year=month.year + 1, month=1)
        else:
            month = month.replace(month=month.month + 1)

    rows = []
    for start in starts:
        rows.append(typical_investor_sim(signals, start))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def render_report(
    universe: str,
    funnel_p1: dict,
    funnel_p2: dict,
    metrics: dict,
    sensitivity: list[dict],
    started: datetime,
    finished: datetime,
) -> str:
    L = []
    L.append(f"# Break-and-retest SIGNAL SERVICE — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append("**v2 framing**: per-signal evaluation, no single-slot artifact.")
    L.append("Each published signal is independently scored on fill + outcome.")
    L.append("")
    L.append("**Params**")
    L.append("")
    L.append(f"- Breakout: 4h close > R + {BREAKOUT_ATR_MULT}×ATR(14d), prior 4h close > R")
    L.append(f"- Retest TTL: {RETEST_TTL_HOURS}h ({RETEST_TTL_HOURS//24}d)")
    L.append(f"- Quality: confluence ≥ {MIN_CONFLUENCE} OR touches ≥ {MIN_TOUCHES}")
    L.append(f"- Momentum: token RSI ≥ {MIN_TOKEN_RSI} (partial-bar daily SMA)")
    L.append(f"- Regime: btc_rsi_floor = {BTC_RSI_FLOOR}")
    L.append(f"- Per-token cap: {PER_TOKEN_CAP} active signal at a time")
    L.append(f"- Major-only levels: **{MAJOR_ONLY}**")
    L.append(f"- TP cascade: **{TP_CASCADE}**  (min RR = {TP_CASCADE_MIN_RR})")
    L.append(f"- Max hold (post-fill): {effective_max_hold()}d")
    L.append("")
    L.append(f"- Started:  {started.isoformat(timespec='seconds')}")
    L.append(f"- Finished: {finished.isoformat(timespec='seconds')}")
    L.append(f"- Wall:     {(finished-started).total_seconds()/60:.1f} min")
    L.append("")
    L.append(f"## Universe `{universe}`")
    L.append("")
    L.append(f"- Tokens used: **{len(funnel_p1['tokens_used'])}** "
             f"of {len(UNIVERSES[universe]['tokens'])}")
    if funnel_p1.get("tokens_missing"):
        L.append(f"- Missing: `{funnel_p1['tokens_missing']}`")
    L.append(f"- Bars: {funnel_p1['first_bar']} → {funnel_p1['last_bar']}")
    L.append("")
    L.append("## Signal funnel")
    L.append("")
    L.append(f"- Bars scanned (after warmup): {funnel_p1['bars_scanned']}")
    L.append(f"- BTC regime blocked: {funnel_p1['btc_regime_blocked']}")
    L.append(f"- SR cache miss: {funnel_p1['cache_miss']}")
    L.append(f"- ATR unavailable: {funnel_p1['atr_unavailable']}")
    L.append(f"- TP/SL sanity failed: {funnel_p1['tp_sl_sanity_failed']}")
    if TP_CASCADE:
        L.append(f"- TP cascade skipped (RR<{TP_CASCADE_MIN_RR}): {funnel_p1.get('cascade_skipped', 0)}")
    L.append(f"- **Raw breakouts:** {funnel_p1['raw_breakouts']}")
    L.append(f"- Quality filter dropped: {funnel_p2['quality_dropped']}")
    L.append(f"- Momentum filter dropped: {funnel_p2['momentum_dropped']}")
    L.append(f"- Per-token cap dropped: {funnel_p2['per_token_cap_dropped']}")
    L.append(f"- **Published signals:** {funnel_p2['published']}")
    L.append("")
    L.append("## Per-signal performance")
    L.append("")
    if metrics.get("n_published", 0) == 0:
        L.append("**No published signals.**")
    else:
        m = metrics
        L.append(f"- Published: {m['n_published']}    Filled: {m['n_filled']} "
                 f"({m['fill_rate_pct']:.1f}%)   Unfilled-expired: {m['n_unfilled_expired']}")
        L.append(f"- Of filled: TP {m['n_tp']}, SL {m['n_sl']}, TIMEOUT {m['n_timeout']}, OPEN {m['n_open']}")
        L.append(f"- Win rate (of filled): **{m['win_rate_filled_pct']:.1f}%**")
        L.append(f"- Avg win / avg loss: {m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%")
        L.append(f"- **Expectancy per published signal: {m['expectancy_per_signal_pct']:+.2f}%**")
        L.append(f"- Avg R:R at entry (all): {m['avg_rr_entry']}    Filled: {m['avg_rr_filled']}")
        L.append(f"- Avg hold (filled): {m['avg_hold_hours']}h ({m['avg_hold_hours']/24:.1f}d)")
    L.append("")
    L.append("## Sensitivity by start month (typical-investor sim)")
    L.append("")
    L.append(f"- Capital ${SIM_INITIAL_CAPITAL:,.0f}, {SIM_MAX_SLOTS} slots × "
             f"{SIM_PER_SLOT_FRACTION*100:.0f}% per slot, FIFO allocation")
    L.append("")
    if not sensitivity:
        L.append("_No data._")
    else:
        L.append("| Start | Eligible | Taken | Skipped (full) | Return % | Final $ |")
        L.append("|-------|----------|-------|---------------|----------|---------|")
        for row in sensitivity:
            L.append(f"| {row['start_ts']} | {row['n_signals_eligible']} | "
                     f"{row['n_taken']} | {row['n_skipped_slots_full']} | "
                     f"**{row['return_pct']:+.1f}%** | ${row['final_equity']:,.0f} |")
    L.append("")
    L.append(f"_Signals CSV: `reports/backtest_break_retest_v2/{universe}_signals.csv`_  ")
    L.append(f"_Sensitivity CSV: `reports/backtest_break_retest_v2/{universe}_sensitivity.csv`_")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def write_signals_csv(signals: list[Signal], path: Path) -> None:
    rows = []
    for s in signals:
        rows.append({
            "symbol": s.symbol,
            "breakout_ts": s.breakout_ts,
            "level": s.level, "tp": s.tp, "sl": s.sl, "tp_source": s.tp_source,
            "expires_at": s.expires_at,
            "confluence": s.confluence, "touches": s.touches,
            "tier": s.tier,
            "breakout_magnitude_atr": s.breakout_magnitude_atr,
            "token_rsi": s.token_rsi, "btc_rsi": s.btc_rsi,
            "raw_rr": s.raw_rr,
            "passed_quality": s.passed_quality,
            "passed_momentum": s.passed_momentum,
            "published": s.published, "skip_reason": s.skip_reason,
            "filled": s.filled,
            "fill_ts": s.fill_ts,
            "exit_ts": s.exit_ts, "exit_price": s.exit_price,
            "exit_reason": s.exit_reason,
            "pnl_pct": s.pnl_pct, "hold_hours": s.hold_hours,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal-service backtest v2")
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()), default="selected17")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--rebuild-sr", action="store_true")
    parser.add_argument("--major-only", action="store_true",
                        help="Use only Major-tier levels (filter out Minors)")
    parser.add_argument("--tp-cascade", action="store_true",
                        help="Apply TP cascade: TP1 if RR>=min, else TP2")
    parser.add_argument("--min-rr", type=float, default=2.0,
                        help="Minimum RR for TP cascade (default 2.0)")
    parser.add_argument("--max-hold", type=int, default=None,
                        help="Override max hold days (default 7 from backtest_intraday)")
    args = parser.parse_args()
    if args.max_hold is not None:
        global MAX_HOLD_DAYS_OVERRIDE
        MAX_HOLD_DAYS_OVERRIDE = args.max_hold
        log.info("MAX_HOLD override: %d days", MAX_HOLD_DAYS_OVERRIDE)
    if args.major_only:
        global MAJOR_ONLY
        MAJOR_ONLY = True
        log.info("MAJOR_ONLY mode: filtering to Major-tier levels only")
    if args.tp_cascade:
        global TP_CASCADE, TP_CASCADE_MIN_RR
        TP_CASCADE = True
        TP_CASCADE_MIN_RR = args.min_rr
        log.info("TP_CASCADE mode: min_rr=%.2f (TP1 if RR>=min, else TP2)",
                 TP_CASCADE_MIN_RR)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"sweep_{stamp}.md"

    log.info("Universe: %s   Report: %s", args.universe, report_path)

    needed = UNIVERSES[args.universe]["tokens"]
    log.info("Loading hourly for %d tokens...", len(needed))
    hourly = load_hourly(needed)
    log.info("Loaded hourly for %d tokens", len(hourly))

    if args.start or args.end:
        start_ts = pd.Timestamp(args.start, tz="UTC") if args.start else None
        end_ts = (pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)) if args.end else None
        for sym in list(hourly.keys()):
            df = hourly[sym]
            if start_ts is not None:
                df = df.loc[df.index >= start_ts]
            if end_ts is not None:
                df = df.loc[df.index < end_ts]
            hourly[sym] = df
        log.info("Date window applied: %s → %s", args.start, args.end)

    daily_ohlcv = aggregate_to_daily(hourly)

    # Shared SR cache with the intraday backtest
    SR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    if args.rebuild_sr and cache_path.exists():
        cache_path.unlink()
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    try:
        signals, funnel_p1 = detect_breakouts(args.universe, hourly, sr_cache, daily_ohlcv)
    except Exception as e:
        log.error("Phase 1 failed: %s", e)
        traceback.print_exc()
        return 1

    if "error" in funnel_p1:
        log.error("Phase 1 error: %s", funnel_p1["error"])
        return 1

    published, funnel_p2 = filter_signals(signals)
    evaluate_signals(signals, hourly)        # evaluate all (incl. unpublished, for CSV)
    metrics = per_signal_metrics(published)
    sensitivity = sensitivity_by_start_month(signals)

    # CSV outputs
    sig_csv = REPORT_DIR / f"{args.universe}_signals.csv"
    write_signals_csv(signals, sig_csv)
    log.info("Wrote %d signals to %s", len(signals), sig_csv.name)

    if sensitivity:
        sens_csv = REPORT_DIR / f"{args.universe}_sensitivity.csv"
        pd.DataFrame(sensitivity).to_csv(sens_csv, index=False)
        log.info("Wrote sensitivity to %s", sens_csv.name)

    finished = datetime.now(timezone.utc)
    report = render_report(args.universe, funnel_p1, funnel_p2, metrics, sensitivity, started, finished)
    report_path.write_text(report)

    log.info("=" * 70)
    log.info("DONE — report: %s", report_path)
    print("\n" + "=" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
