#!/usr/bin/env python3
"""
sr_buyback_walkforward.py — Walk-forward validation of the winning combo.
==========================================================================

Runs the sr_buyback_sweep winner across rolling time windows. Per-window
metrics tell us whether the +0.15%/signal edge generalizes across regimes
or was a quirk of the full-window backtest.

Winning combo (from sr_buyback_sweep):
  sr_max_distance_atr = 2.0
  sr_sl_atr_mult      = 1.0
  min_token_rsi       = 60
  min_confluence      = 0
  min_touches         = 0
  major_only          = True
  tp_cascade          = False
  max_hold_days       = 7

Stability criteria:
  ★★★ Deploy:        ≥70% windows positive AND mean exp > 0 AND Sharpe > 0.5
  ★★  Marginal:      50–70% windows positive AND mean exp > 0
  ★   Regime-fit:    <50% windows positive OR mean exp ≤ 0 → not robust

Usage:
  python3 research/sr_buyback_walkforward.py --universe selected17
  python3 research/sr_buyback_walkforward.py --universe selected17 --window-days 60
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
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
    UNIVERSES,
)
from sr_buyback_sweep import (  # noqa: E402
    Config,
    detect_sr_buyback,
    fresh_copies,
    apply_cascade,
    filter_signals,
    evaluate_signal,
    RETEST_TTL_HOURS_FIXED,
)
from backtest_break_retest_v2 import Signal  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wf")

REPORT_DIR = ROOT / "reports" / "backtest_sr_buyback"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"


# ─────────────────────────────────────────────────────────────────────────────
# Winning combo (frozen)
# ─────────────────────────────────────────────────────────────────────────────

# Selected17 winner (Phase 4 full sweep, 248k combos)
WINNER_SELECTED17 = Config(
    sr_max_distance_atr=1.5,
    sr_sl_atr_mult=0.5,
    major_only=True,
    btc_rsi_floor=0.0,
    mt_not_downtrend=True,
    relative_volume=False,
    btc_uptrend=True,                  # On — Sharpe 0.41 → 0.52 on selected17
    rsi_cap=0.0,
    min_token_rsi=60.0,
    min_confluence=0.0,
    min_touches=0,
    tp_cascade=False,
    min_rr=1.5,
    max_hold_days=21,
)

# hl44 winner candidate (Rank 20 from hl44 quick sweep — highest score, 714 fills)
# Notably DIFFERENT from selected17: btc_uptrend OFF, wider SL, rsi_cap=80, hold=7d.
# hl44 mid-caps trade on token-specific narratives more than BTC regime.
WINNER_HL44 = Config(
    sr_max_distance_atr=3.0,
    sr_sl_atr_mult=1.0,
    major_only=False,
    btc_rsi_floor=0.0,
    mt_not_downtrend=False,
    relative_volume=False,
    btc_uptrend=False,                 # OFF — opposite of selected17
    rsi_cap=80.0,
    min_token_rsi=60.0,
    min_confluence=0.0,
    min_touches=0,
    tp_cascade=False,
    min_rr=2.0,
    max_hold_days=7,
)

WINNERS = {
    "selected17": WINNER_SELECTED17,
    "hl44": WINNER_HL44,
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def window_metrics(
    window_sigs: list[Signal],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> dict:
    n_pub = len(window_sigs)
    filled = [s for s in window_sigs if s.filled]
    nf = len(filled)
    if nf == 0:
        return {
            "window_start": window_start.strftime("%Y-%m-%d"),
            "window_end": window_end.strftime("%Y-%m-%d"),
            "n_published": n_pub, "n_filled": 0,
            "fill_rate_pct": 0.0, "win_rate_pct": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "avg_pnl_filled_pct": 0.0, "expectancy_per_signal_pct": 0.0,
            "n_tp": 0, "n_sl": 0, "n_timeout": 0, "n_open": 0,
        }
    wins = [s for s in filled if s.pnl_pct > 0]
    losses = [s for s in filled if s.pnl_pct <= 0]
    avg_win = float(np.mean([s.pnl_pct for s in wins])) if wins else 0.0
    avg_loss = float(np.mean([s.pnl_pct for s in losses])) if losses else 0.0
    avg_pnl = float(np.mean([s.pnl_pct for s in filled]))
    exp_per_sig = avg_pnl * (nf / n_pub) if n_pub else 0.0
    return {
        "window_start": window_start.strftime("%Y-%m-%d"),
        "window_end": window_end.strftime("%Y-%m-%d"),
        "n_published": n_pub,
        "n_filled": nf,
        "fill_rate_pct": round(100 * nf / max(n_pub, 1), 2),
        "win_rate_pct": round(100 * len(wins) / max(nf, 1), 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "avg_pnl_filled_pct": round(avg_pnl, 3),
        "expectancy_per_signal_pct": round(exp_per_sig, 3),
        "n_tp": sum(1 for s in filled if s.exit_reason == "TP"),
        "n_sl": sum(1 for s in filled if s.exit_reason == "SL"),
        "n_timeout": sum(1 for s in filled if s.exit_reason == "TIMEOUT"),
        "n_open": sum(1 for s in filled if s.exit_reason == "OPEN"),
    }


def build_windows(
    signals: list[Signal],
    window_days: int,
    overlap_days: int = 0,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    if not signals:
        return []
    first = min(s.breakout_ts for s in signals).normalize()
    last = max(s.breakout_ts for s in signals).normalize()
    step = window_days - overlap_days
    windows = []
    cur = first
    while cur < last:
        end = cur + pd.Timedelta(days=window_days)
        windows.append((cur, end))
        cur = cur + pd.Timedelta(days=step)
    return windows


def stability_summary(rows: list[dict]) -> dict:
    """Aggregate stability metrics from per-window rows."""
    exps = np.array([r["expectancy_per_signal_pct"] for r in rows
                     if r["n_published"] > 0])
    wrs = np.array([r["win_rate_pct"] for r in rows if r["n_filled"] > 0])

    if len(exps) == 0:
        return {"verdict": "no data"}

    n = len(exps)
    n_positive = int((exps > 0).sum())
    pct_positive = round(100 * n_positive / n, 1)
    mean_exp = float(exps.mean())
    std_exp = float(exps.std(ddof=1)) if n > 1 else 0.0
    sharpe_like = (mean_exp / std_exp * np.sqrt(n)) if std_exp > 0 else float("nan")
    median_exp = float(np.median(exps))

    # Verdict
    if pct_positive >= 70 and mean_exp > 0 and (np.isnan(sharpe_like) or sharpe_like > 0.5):
        verdict = "★★★  DEPLOY  —  edge generalizes"
    elif pct_positive >= 50 and mean_exp > 0:
        verdict = "★★  MARGINAL  —  edge present but variable"
    else:
        verdict = "★  REGIME-FIT  —  not robust enough to deploy"

    return {
        "n_windows": n,
        "n_positive": n_positive,
        "pct_positive": pct_positive,
        "mean_expectancy": round(mean_exp, 3),
        "median_expectancy": round(median_exp, 3),
        "std_expectancy": round(std_exp, 3),
        "sharpe_like": round(sharpe_like, 2) if not np.isnan(sharpe_like) else None,
        "mean_wr": round(float(wrs.mean()), 2) if len(wrs) else 0.0,
        "min_expectancy": round(float(exps.min()), 3),
        "max_expectancy": round(float(exps.max()), 3),
        "verdict": verdict,
    }


def render_report(
    cfg: Config,
    rows: list[dict],
    summary: dict,
    overall_metrics: dict,
    window_days: int,
    overlap_days: int,
    started: datetime,
    finished: datetime,
) -> str:
    L = []
    L.append(f"# SR Buyback walk-forward validation — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append(f"**Window: {window_days}d {'rolling' if overlap_days > 0 else 'non-overlapping'}"
             f"  {('(' + str(overlap_days) + 'd overlap)') if overlap_days > 0 else ''}**")
    L.append("")
    L.append("**Frozen config (winner from sweep)**")
    L.append("")
    L.append(f"- sr_max_distance_atr: {cfg.sr_max_distance_atr}")
    L.append(f"- sr_sl_atr_mult:      {cfg.sr_sl_atr_mult}")
    L.append(f"- major_only:          {cfg.major_only}")
    L.append(f"- btc_rsi_floor:       {cfg.btc_rsi_floor}")
    L.append(f"- mt_not_downtrend:    {cfg.mt_not_downtrend}")
    L.append(f"- relative_volume:     {cfg.relative_volume}")
    L.append(f"- btc_uptrend:         {cfg.btc_uptrend}")
    L.append(f"- rsi_cap:             {cfg.rsi_cap}")
    L.append(f"- min_token_rsi:       {cfg.min_token_rsi}")
    L.append(f"- min_confluence:      {cfg.min_confluence}")
    L.append(f"- min_touches:         {cfg.min_touches}")
    L.append(f"- tp_cascade:          {cfg.tp_cascade}")
    L.append(f"- min_rr:              {cfg.min_rr}")
    L.append(f"- max_hold_days:       {cfg.max_hold_days}")
    L.append("")
    L.append(f"- Wall: {(finished-started).total_seconds()/60:.1f} min")
    L.append("")

    L.append("## Overall (full-window — for reference)")
    L.append("")
    om = overall_metrics
    L.append(f"- Published: {om['n_published']}   Filled: {om['n_filled']} "
             f"({om['fill_rate_pct']:.1f}%)")
    L.append(f"- Win rate: {om['win_rate_pct']:.1f}%   "
             f"Avg win/loss: {om['avg_win_pct']:+.2f}% / {om['avg_loss_pct']:+.2f}%")
    L.append(f"- **Expectancy per signal: {om['expectancy_per_signal_pct']:+.3f}%**")
    L.append("")

    L.append(f"## Per-window performance ({window_days}d windows)")
    L.append("")
    L.append("| Window | n_pub | n_fill | fill% | WR% | avg_w/l | exp%/sig | TP/SL/TO/Open |")
    L.append("|--------|-------|--------|-------|-----|---------|----------|---------------|")
    for r in rows:
        if r["n_published"] == 0:
            continue
        marker = "+" if r["expectancy_per_signal_pct"] > 0 else "−"
        L.append(
            f"| {r['window_start']} | {r['n_published']} | {r['n_filled']} | "
            f"{r['fill_rate_pct']:.1f}% | {r['win_rate_pct']:.1f}% | "
            f"{r['avg_win_pct']:+.2f}/{r['avg_loss_pct']:+.2f} | "
            f"**{marker}{abs(r['expectancy_per_signal_pct']):.3f}%** | "
            f"{r['n_tp']}/{r['n_sl']}/{r['n_timeout']}/{r['n_open']} |"
        )
    L.append("")

    L.append("## Stability summary")
    L.append("")
    s = summary
    if s.get("verdict") == "no data":
        L.append("No windows with data.")
    else:
        L.append(f"- Windows: **{s['n_windows']}**")
        L.append(f"- Positive: **{s['n_positive']} / {s['n_windows']} "
                 f"({s['pct_positive']:.1f}%)**")
        L.append(f"- Mean expectancy: **{s['mean_expectancy']:+.3f}%**   "
                 f"Median: {s['median_expectancy']:+.3f}%   "
                 f"Std: {s['std_expectancy']:.3f}%")
        L.append(f"- Min/Max: {s['min_expectancy']:+.3f}% / {s['max_expectancy']:+.3f}%")
        L.append(f"- Sharpe-like (mean/std × √n): **{s['sharpe_like']}**")
        L.append(f"- Mean per-window WR: {s['mean_wr']:.1f}%")
        L.append("")
        L.append(f"### Verdict: {s['verdict']}")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="SR Buyback walk-forward validation")
    parser.add_argument("--universe", default="selected17",
                        choices=list(UNIVERSES.keys()))
    parser.add_argument("--window-days", type=int, default=30,
                        help="Window size in days (default 30)")
    parser.add_argument("--overlap-days", type=int, default=0,
                        help="Overlap between windows (default 0 = non-overlapping)")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")

    WINNER = WINNERS[args.universe]
    log.info("Walk-forward — winner config (%s):\n  %s", args.universe, WINNER)
    log.info("Window: %dd  Overlap: %dd", args.window_days, args.overlap_days)

    # Load data + cache
    needed = UNIVERSES[args.universe]["tokens"]
    hourly = load_hourly(needed)
    daily_ohlcv = aggregate_to_daily(hourly)
    bars_4h = aggregate_to_4h(hourly)

    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"]
                        if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    # Run winning combo end-to-end
    try:
        raw = detect_sr_buyback(
            args.universe, hourly, sr_cache, daily_ohlcv,
            sr_max_distance_atr=WINNER.sr_max_distance_atr,
            sr_sl_atr_mult=WINNER.sr_sl_atr_mult,
            major_only=WINNER.major_only,
        )
    except Exception as e:
        log.error("Detect failed: %s", e)
        traceback.print_exc()
        return 1
    log.info("Detected %d raw signals", len(raw))

    sigs = fresh_copies(raw, RETEST_TTL_HOURS_FIXED)
    if WINNER.tp_cascade:
        sigs, _ = apply_cascade(sigs, WINNER.min_rr)
    published = filter_signals(sigs, WINNER)
    log.info("Published %d signals (after filters)", len(published))

    for s in published:
        bars = bars_4h.get(s.symbol)
        if bars is not None:
            evaluate_signal(s, bars, WINNER.max_hold_days)

    # Overall metrics for reference
    filled = [s for s in published if s.filled]
    nf = len(filled)
    n = len(published)
    wins = [s for s in filled if s.pnl_pct > 0]
    losses = [s for s in filled if s.pnl_pct <= 0]
    overall = {
        "n_published": n, "n_filled": nf,
        "fill_rate_pct": round(100 * nf / max(n, 1), 2),
        "win_rate_pct": round(100 * len(wins) / max(nf, 1), 2),
        "avg_win_pct": round(float(np.mean([s.pnl_pct for s in wins])) if wins else 0.0, 3),
        "avg_loss_pct": round(float(np.mean([s.pnl_pct for s in losses])) if losses else 0.0, 3),
        "expectancy_per_signal_pct": round(
            float(np.mean([s.pnl_pct for s in filled])) * (nf / n) if n else 0.0, 3
        ),
    }

    # Build windows and compute per-window metrics
    windows = build_windows(published, args.window_days, args.overlap_days)
    log.info("Built %d windows", len(windows))

    rows = []
    for w_start, w_end in windows:
        window_sigs = [s for s in published
                       if w_start <= s.breakout_ts < w_end]
        rows.append(window_metrics(window_sigs, w_start, w_end))

    summary = stability_summary(rows)

    # Write CSV
    csv_path = REPORT_DIR / f"sr_walkforward_{stamp}.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    log.info("Wrote %d per-window rows to %s", len(rows), csv_path.name)

    # Render report
    finished = datetime.now(timezone.utc)
    report = render_report(
        WINNER, rows, summary, overall,
        args.window_days, args.overlap_days, started, finished,
    )
    report_path = REPORT_DIR / f"sr_walkforward_{stamp}.md"
    report_path.write_text(report)
    log.info("DONE — report: %s", report_path)

    print("\n" + "=" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
