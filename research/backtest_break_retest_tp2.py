#!/usr/bin/env python3
"""
backtest_break_retest_tp2.py — TP1/TP2 research mode.
======================================================

Same signal detection + filtering as v2 (all levels, not Major-only).
Different EVALUATION: holds the position from fill through to TP2
(with SL still active), tracking the timestamps of TP1, TP2, SL hits
independently.

Final exit rule (drives pnl_pct):
  - SL hit before TP2 → exit at SL (loss), even if TP1 was hit on the way
  - TP2 hit before SL → exit at TP2 (big win)
  - Neither within max_hold (default 30d) → TIMEOUT at last close

Separately measured for every filled signal:
  - tp1_hit_ts / tp1_hold_hours (only counts if TP1 reached without SL first)
  - tp2_hit_ts / tp2_hold_hours (same, for TP2)
  - sl_hit_ts (when SL would have stopped us)

The diagnostic answers: would a "partial at TP1, runner to TP2" structure
be profitable? Key indicators:
  - TP1-before-SL rate (interim hit rate)
  - TP2-before-SL rate (full-RR hit rate)
  - Time-to-TP1 vs time-to-TP2 (path efficiency)

Usage:
  python3 research/backtest_break_retest_tp2.py --universe selected17
  python3 research/backtest_break_retest_tp2.py --universe selected17 --max-hold 45
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
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
    UNIVERSES,
    TRADING_FEE,
)

from backtest_break_retest_v2 import (  # noqa: E402
    Signal,
    detect_breakouts,
    filter_signals,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bnr_tp2")

REPORT_DIR = ROOT / "reports" / "backtest_break_retest_v2"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"


# ─────────────────────────────────────────────────────────────────────────────
# TP2-aware evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_signal_tp2(s: Signal, bars: pd.DataFrame, max_hold_days: int) -> None:
    """Track TP1, TP2, SL independently. Position holds to TP2/SL/timeout."""
    idx = bars.index
    start_pos = idx.searchsorted(s.breakout_ts)
    if start_pos >= len(idx) - 1:
        return

    # Find fill within retest TTL (= s.expires_at, set at detection)
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
    has_tp2 = s.tp2 > 0.0

    sl_hit_ts: pd.Timestamp | None = None
    tp1_hit_ts: pd.Timestamp | None = None
    tp2_hit_ts: pd.Timestamp | None = None

    last_ts = s.fill_ts
    last_close = entry_price

    for pos in range(fill_pos, len(idx)):
        ts = idx[pos]
        bar = bars.iloc[pos]
        high = float(bar["high"]); low = float(bar["low"]); close = float(bar["close"])
        elapsed_days = (ts - s.fill_ts).total_seconds() / 86400
        last_ts = ts
        last_close = close

        if pos == fill_pos:
            # On the fill bar itself, skip TP/SL check (we filled on the way down)
            if elapsed_days >= max_hold_days:
                break
            continue

        # SL conservative: same-bar tie goes to SL
        if sl_hit_ts is None and low <= s.sl:
            sl_hit_ts = ts

        # TP1 only counts as a "hit before SL"
        if tp1_hit_ts is None and sl_hit_ts is None and high >= s.tp:
            tp1_hit_ts = ts

        # TP2 only counts as a "hit before SL"
        if has_tp2 and tp2_hit_ts is None and sl_hit_ts is None and high >= s.tp2:
            tp2_hit_ts = ts

        # Termination
        if sl_hit_ts is not None:
            break
        if tp2_hit_ts is not None:
            break
        if elapsed_days >= max_hold_days:
            break

    s.tp1_hit_ts = tp1_hit_ts
    s.tp2_hit_ts = tp2_hit_ts
    s.sl_hit_ts = sl_hit_ts
    if tp1_hit_ts is not None:
        s.tp1_hold_hours = int((tp1_hit_ts - s.fill_ts).total_seconds() / 3600)
    if tp2_hit_ts is not None:
        s.tp2_hold_hours = int((tp2_hit_ts - s.fill_ts).total_seconds() / 3600)

    # Final outcome for pnl
    if sl_hit_ts is not None:
        s.exit_ts, s.exit_price, s.exit_reason = sl_hit_ts, s.sl, "SL"
    elif tp2_hit_ts is not None:
        s.exit_ts, s.exit_price, s.exit_reason = tp2_hit_ts, s.tp2, "TP2"
    else:
        s.exit_ts, s.exit_price, s.exit_reason = last_ts, last_close, "TIMEOUT"

    s.pnl_pct = (s.exit_price - entry_price) / entry_price * 100 - TRADING_FEE * 200
    s.hold_hours = int((s.exit_ts - s.fill_ts).total_seconds() / 3600)


def evaluate_all(signals: list[Signal], hourly: dict[str, pd.DataFrame],
                 max_hold_days: int) -> None:
    bars_4h = aggregate_to_4h(hourly)
    for s in signals:
        bars = bars_4h.get(s.symbol)
        if bars is None:
            continue
        evaluate_signal_tp2(s, bars, max_hold_days)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def tp2_metrics(signals: list[Signal]) -> dict:
    pub = [s for s in signals if s.published]
    nP = len(pub)
    filled = [s for s in pub if s.filled]
    nF = len(filled)

    n_has_tp2 = sum(1 for s in pub if s.tp2 > 0)
    filled_with_tp2 = [s for s in filled if s.tp2 > 0]

    n_tp1 = sum(1 for s in filled if s.tp1_hit_ts is not None)
    n_tp2 = sum(1 for s in filled_with_tp2 if s.tp2_hit_ts is not None)
    n_sl_first = sum(1 for s in filled if s.exit_reason == "SL")
    n_timeout = sum(1 for s in filled if s.exit_reason == "TIMEOUT")
    n_sl_after_tp1 = sum(
        1 for s in filled
        if s.exit_reason == "SL" and s.tp1_hit_ts is not None
    )

    tp1_durations = [s.tp1_hold_hours for s in filled if s.tp1_hit_ts is not None]
    tp2_durations = [s.tp2_hold_hours for s in filled_with_tp2 if s.tp2_hit_ts is not None]
    sl_durations = [s.hold_hours for s in filled if s.exit_reason == "SL"]

    wins = [s for s in filled if s.pnl_pct > 0]
    losses = [s for s in filled if s.pnl_pct <= 0]
    avg_win = float(np.mean([s.pnl_pct for s in wins])) if wins else 0.0
    avg_loss = float(np.mean([s.pnl_pct for s in losses])) if losses else 0.0
    avg_pnl_filled = float(np.mean([s.pnl_pct for s in filled])) if filled else 0.0
    expectancy = avg_pnl_filled * (nF / nP) if nP else 0.0

    return {
        "n_published": nP,
        "n_with_tp2_defined": n_has_tp2,
        "n_filled": nF,
        "fill_rate_pct": round(100 * nF / max(nP, 1), 2),
        "n_tp1_before_sl": n_tp1,
        "n_tp2_before_sl": n_tp2,
        "n_sl_first": n_sl_first - n_sl_after_tp1,
        "n_sl_after_tp1": n_sl_after_tp1,
        "n_timeout": n_timeout,
        "tp1_rate_filled_pct": round(100 * n_tp1 / max(nF, 1), 2),
        "tp2_rate_filled_pct": round(100 * n_tp2 / max(len(filled_with_tp2), 1), 2),
        "tp2_rate_of_tp1_pct": round(100 * n_tp2 / max(n_tp1, 1), 2),
        "avg_tp1_hours": round(float(np.mean(tp1_durations)), 1) if tp1_durations else 0.0,
        "avg_tp2_hours": round(float(np.mean(tp2_durations)), 1) if tp2_durations else 0.0,
        "avg_sl_hours": round(float(np.mean(sl_durations)), 1) if sl_durations else 0.0,
        "median_tp1_hours": round(float(np.median(tp1_durations)), 1) if tp1_durations else 0.0,
        "median_tp2_hours": round(float(np.median(tp2_durations)), 1) if tp2_durations else 0.0,
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "avg_pnl_filled_pct": round(avg_pnl_filled, 3),
        "expectancy_per_signal_pct": round(expectancy, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def render_report(universe: str, m: dict, started: datetime, finished: datetime,
                  max_hold_days: int) -> str:
    L = []
    L.append(f"# Break-and-retest TP2 research — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append("**Strategy**: hold from fill to TP2 (with SL active).")
    L.append("TP1 measured as interim event only (not an exit trigger).")
    L.append("")
    L.append(f"- Universe: `{universe}`   All levels (Major+Minor)")
    L.append(f"- Max hold: **{max_hold_days} days**   Retest TTL: 72h")
    L.append(f"- TP1 = next R above broken; TP2 = next R above TP1")
    L.append(f"- SL = nearest level below broken")
    L.append(f"- Wall: {(finished-started).total_seconds()/60:.1f} min")
    L.append("")
    L.append("## Volume")
    L.append("")
    L.append(f"- Published signals: **{m['n_published']}**")
    L.append(f"- With TP2 defined: {m['n_with_tp2_defined']}  "
             f"({100*m['n_with_tp2_defined']/max(m['n_published'],1):.1f}%)")
    L.append(f"- Filled: **{m['n_filled']}**  ({m['fill_rate_pct']:.1f}% fill rate)")
    L.append("")
    L.append("## Event hit rates (of filled signals)")
    L.append("")
    L.append("| Event | Count | % of filled |")
    L.append("|-------|-------|-------------|")
    L.append(f"| **TP1 hit before SL** | {m['n_tp1_before_sl']} | {m['tp1_rate_filled_pct']:.1f}% |")
    L.append(f"| **TP2 hit before SL** | {m['n_tp2_before_sl']} | {m['tp2_rate_filled_pct']:.1f}% (of signals with TP2 defined) |")
    L.append(f"| SL hit (no TP1) | {m['n_sl_first']} | — |")
    L.append(f"| SL hit (after TP1) | {m['n_sl_after_tp1']} | — |")
    L.append(f"| Timeout ({max_hold_days}d) | {m['n_timeout']} | — |")
    L.append("")
    L.append(f"**Of trades that hit TP1, {m['tp2_rate_of_tp1_pct']:.1f}% also reached TP2 before SL.**")
    L.append("")
    L.append("## Timing")
    L.append("")
    L.append("| Event | Avg hours | Median hours | Avg days |")
    L.append("|-------|-----------|--------------|----------|")
    L.append(f"| Time to TP1 | {m['avg_tp1_hours']:.1f} | {m['median_tp1_hours']:.1f} | {m['avg_tp1_hours']/24:.1f} |")
    L.append(f"| Time to TP2 | {m['avg_tp2_hours']:.1f} | {m['median_tp2_hours']:.1f} | {m['avg_tp2_hours']/24:.1f} |")
    L.append(f"| Time to SL | {m['avg_sl_hours']:.1f} | — | {m['avg_sl_hours']/24:.1f} |")
    L.append("")
    L.append("## Per-signal P&L (strategy = hold to TP2 with SL)")
    L.append("")
    L.append(f"- Avg win: {m['avg_win_pct']:+.2f}%   Avg loss: {m['avg_loss_pct']:+.2f}%")
    L.append(f"- Avg pnl per filled: {m['avg_pnl_filled_pct']:+.3f}%")
    L.append(f"- **Expectancy per published signal: {m['expectancy_per_signal_pct']:+.3f}%**")
    L.append("")
    L.append(f"_Signals CSV: `reports/backtest_break_retest_v2/{universe}_tp2_signals.csv`_")
    return "\n".join(L)


def write_signals_csv(signals: list[Signal], path: Path) -> None:
    rows = []
    for s in signals:
        rows.append({
            "symbol": s.symbol,
            "breakout_ts": s.breakout_ts,
            "level": s.level, "tp1": s.tp, "tp2": s.tp2, "sl": s.sl,
            "expires_at": s.expires_at,
            "tier": s.tier, "confluence": s.confluence, "touches": s.touches,
            "breakout_magnitude_atr": s.breakout_magnitude_atr,
            "token_rsi": s.token_rsi, "btc_rsi": s.btc_rsi,
            "raw_rr": s.raw_rr,
            "published": s.published, "skip_reason": s.skip_reason,
            "filled": s.filled, "fill_ts": s.fill_ts,
            "tp1_hit_ts": s.tp1_hit_ts, "tp2_hit_ts": s.tp2_hit_ts,
            "sl_hit_ts": s.sl_hit_ts,
            "tp1_hold_hours": s.tp1_hold_hours if s.tp1_hit_ts else None,
            "tp2_hold_hours": s.tp2_hold_hours if s.tp2_hit_ts else None,
            "exit_ts": s.exit_ts, "exit_price": s.exit_price,
            "exit_reason": s.exit_reason,
            "pnl_pct": s.pnl_pct, "hold_hours": s.hold_hours,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="BNR TP1/TP2 research")
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()), default="selected17")
    parser.add_argument("--max-hold", type=int, default=30,
                        help="Max hold days for the test (default 30)")
    parser.add_argument("--rebuild-sr", action="store_true")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"tp2_research_{stamp}.md"

    needed = UNIVERSES[args.universe]["tokens"]
    log.info("Loading hourly for %d tokens...", len(needed))
    hourly = load_hourly(needed)
    log.info("Loaded hourly for %d tokens", len(hourly))

    daily_ohlcv = aggregate_to_daily(hourly)

    SR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    if args.rebuild_sr and cache_path.exists():
        cache_path.unlink()
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    try:
        signals, funnel = detect_breakouts(args.universe, hourly, sr_cache, daily_ohlcv)
    except Exception as e:
        log.error("Detection failed: %s", e)
        traceback.print_exc()
        return 1
    if "error" in funnel:
        log.error("Detection error: %s", funnel["error"])
        return 1

    log.info("Detected %d raw signals", len(signals))

    published, _ = filter_signals(signals)
    log.info("Published %d signals (after filters)", len(published))

    evaluate_all(signals, hourly, args.max_hold)
    log.info("Evaluated %d signals with %d-day max hold", len(signals), args.max_hold)

    metrics = tp2_metrics(signals)

    sig_csv = REPORT_DIR / f"{args.universe}_tp2_signals.csv"
    write_signals_csv(signals, sig_csv)
    log.info("Wrote signals to %s", sig_csv.name)

    finished = datetime.now(timezone.utc)
    report = render_report(args.universe, metrics, started, finished, args.max_hold)
    report_path.write_text(report)
    log.info("Report: %s", report_path)
    print("\n" + "=" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
