#!/usr/bin/env python3
"""
sr_buyback_portfolio.py — Portfolio simulation for the SR Buyback signal service.
=================================================================================

Three analyses (Option β per-token cap — counter-based):

  A1. Filter A/B
      Same portfolio config run with btc_uptrend ON vs OFF.
      Compares PnL and max DD on single-slot AND multi-slot.

  A2. Slot count × per-token cap sweep (filter ON)
      max_slots ∈ {1..5} × per_token_cap ∈ {1, 2, 3}
      Capital per slot = current_equity / max_slots at publish time.

  A3. Start-date sensitivity (filter ON)
      Monthly start dates × max_slots ∈ {1, 4}
      Shows the spread an investor would have experienced depending on when
      they started.

Filter step uses a portfolio-specific version that does NOT enforce
per-token cap (that's applied at portfolio simulation time per Option β).

Frozen winner config (Phase 4):
  sr_max_distance_atr=1.5, sr_sl_atr_mult=0.5, major_only=True,
  btc_rsi_floor=0, mt_not_downtrend=True, relative_volume=False,
  btc_uptrend=True (toggleable for A1), rsi_cap=0, min_token_rsi=60,
  min_confluence=0, min_touches=0, tp_cascade=False, max_hold_days=21.

Output:
  reports/backtest_sr_buyback/portfolio_<UTC>.md  (full report)
  reports/backtest_sr_buyback/portfolio_filter_ab_<UTC>.csv
  reports/backtest_sr_buyback/portfolio_slot_sweep_<UTC>.csv
  reports/backtest_sr_buyback/portfolio_start_sens_<UTC>.csv
  reports/backtest_sr_buyback/portfolio_equity_<UTC>.csv  (best of each)

Usage:
  python3 research/sr_buyback_portfolio.py --universe selected17
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, replace
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
    evaluate_signal,
    filter_signals as filter_signals_strict,   # enforces per_token_cap=1
    RETEST_TTL_HOURS_FIXED,
)
from backtest_break_retest_v2 import Signal  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sr_portfolio")

REPORT_DIR = ROOT / "reports" / "backtest_sr_buyback"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"

# Selected17 winner — Phase 4 full sweep (248k combos)
WINNER_SELECTED17 = Config(
    sr_max_distance_atr=1.5,
    sr_sl_atr_mult=0.5,
    major_only=True,
    btc_rsi_floor=0.0,
    mt_not_downtrend=True,
    relative_volume=False,
    btc_uptrend=True,
    rsi_cap=0.0,
    min_token_rsi=60.0,
    min_confluence=0.0,
    min_touches=0,
    tp_cascade=False,
    min_rr=1.5,
    max_hold_days=21,
)

# hl44 winner — Rank 20 from hl44 quick sweep (714 fills, score 7.82)
# DEPLOY-grade walk-forward (60d: 71% positive, Sharpe 0.53)
WINNER_HL44 = Config(
    sr_max_distance_atr=3.0,
    sr_sl_atr_mult=1.0,
    major_only=False,
    btc_rsi_floor=0.0,
    mt_not_downtrend=False,
    relative_volume=False,
    btc_uptrend=False,
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
# Portfolio config + filter (no per-token cap — applied at portfolio sim time)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PortfolioConfig:
    max_slots: int = 4
    per_slot_fraction: float = 0.25     # of equity at publish time
    per_token_cap: int = 1               # Option β: max concurrent positions per token
    initial_capital: float = 10_000.0
    start_ts: pd.Timestamp | None = None
    end_ts: pd.Timestamp | None = None


def filter_signals_no_token_cap(signals: list[Signal], cfg: Config) -> list[Signal]:
    """Apply all soft filters EXCEPT per-token cap — the portfolio simulator
    enforces concurrency caps based on PortfolioConfig.per_token_cap."""
    published = []
    for s in signals:
        if cfg.btc_rsi_floor > 0 and s.btc_rsi < cfg.btc_rsi_floor:
            continue
        if cfg.mt_not_downtrend and not s.mt_not_downtrend_pass:
            continue
        if cfg.relative_volume and not s.relative_volume_pass:
            continue
        if cfg.btc_uptrend and not s.btc_uptrend_pass:
            continue
        if cfg.min_token_rsi > 0 and s.token_rsi < cfg.min_token_rsi:
            continue
        if cfg.rsi_cap > 0 and s.token_rsi > cfg.rsi_cap:
            continue
        passes_quality = (
            (cfg.min_confluence > 0 and s.confluence >= cfg.min_confluence) or
            (cfg.min_touches > 0 and s.touches >= cfg.min_touches) or
            (cfg.min_confluence == 0 and cfg.min_touches == 0)
        )
        if not passes_quality:
            continue
        s.published = True
        published.append(s)
    return published


# ─────────────────────────────────────────────────────────────────────────────
# Signal feed builder (detect → filter → evaluate, cached per cfg)
# ─────────────────────────────────────────────────────────────────────────────

_FEED_CACHE: dict[tuple, tuple] = {}


def build_signal_feed(
    universe: str,
    cfg: Config,
    hourly: dict,
    daily_ohlcv: dict,
    bars_4h: dict,
    sr_cache: dict,
    filter_per_token_cap: bool = True,
) -> list[Signal]:
    """Run detect → filter → evaluate.

    filter_per_token_cap=True (default, Path B):
        Uses the strict filter from sr_buyback_sweep — enforces 1 active
        signal per token at filter time. Matches walk-forward conditions.
        Yields ~157 signals on selected17 with the winner config.

    filter_per_token_cap=False (Path A / Option β at filter):
        No per-token dedup at filter; portfolio simulator enforces caps.
        Yields ~2,556 signals (16× more) but most get FIFO-skipped at
        slot allocation time.
    """
    key = (
        cfg.sr_max_distance_atr, cfg.sr_sl_atr_mult, cfg.major_only,
        cfg.btc_uptrend, cfg.max_hold_days,
        cfg.btc_rsi_floor, cfg.mt_not_downtrend, cfg.relative_volume,
        cfg.rsi_cap, cfg.min_token_rsi, cfg.min_confluence, cfg.min_touches,
        cfg.tp_cascade, cfg.min_rr,
        filter_per_token_cap,
    )
    if key in _FEED_CACHE:
        return _FEED_CACHE[key]

    raw = detect_sr_buyback(
        universe, hourly, sr_cache, daily_ohlcv,
        sr_max_distance_atr=cfg.sr_max_distance_atr,
        sr_sl_atr_mult=cfg.sr_sl_atr_mult,
        major_only=cfg.major_only,
    )
    sigs = fresh_copies(raw, RETEST_TTL_HOURS_FIXED)
    if cfg.tp_cascade:
        sigs, _ = apply_cascade(sigs, cfg.min_rr)
    if filter_per_token_cap:
        published = filter_signals_strict(sigs, cfg)
    else:
        published = filter_signals_no_token_cap(sigs, cfg)

    for s in published:
        bars = bars_4h.get(s.symbol)
        if bars is not None:
            evaluate_signal(s, bars, cfg.max_hold_days)

    published.sort(key=lambda s: s.breakout_ts)
    _FEED_CACHE[key] = published
    return published


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio simulator
# ─────────────────────────────────────────────────────────────────────────────

def portfolio_backtest(signals: list[Signal], pcfg: PortfolioConfig) -> dict:
    """Replay signals chronologically; simulate slot-based investor.

    Slot semantics:
      - pending + open count against max_slots
      - pending + open per-token count against per_token_cap (Option β)
      - per_slot_fraction × current_capital reserved at publish; converted to
        active on fill; PnL realized + capital updated on exit; reservation
        released on TTL-expire (signal unfilled).
    """
    if not signals:
        return _empty_result(pcfg)

    eligible = [s for s in signals
                if (pcfg.start_ts is None or s.breakout_ts >= pcfg.start_ts)
                and (pcfg.end_ts is None or s.breakout_ts < pcfg.end_ts)]
    if not eligible:
        return _empty_result(pcfg)

    # Event stream
    events: list[tuple[pd.Timestamp, str, Signal]] = []
    for s in eligible:
        events.append((s.breakout_ts, "publish", s))
        if s.filled and s.fill_ts is not None:
            events.append((s.fill_ts, "fill", s))
        if s.exit_ts is not None:
            events.append((s.exit_ts, "exit", s))
    events.sort(key=lambda x: (x[0], {"publish": 0, "fill": 1, "exit": 2}[x[1]]))

    capital = pcfg.initial_capital
    pending: dict[int, float] = {}   # id(signal) -> slot_capital reserved
    open_pos: dict[int, float] = {}
    token_count: dict[str, int] = {}  # active+pending count per token

    n_taken = 0
    n_skipped_slots = 0
    n_skipped_token_cap = 0
    n_realized = 0
    n_wins = 0
    sum_win_pct = 0.0
    sum_loss_pct = 0.0
    n_losses = 0

    start_t = pcfg.start_ts or eligible[0].breakout_ts
    equity_curve: list[tuple[pd.Timestamp, float]] = [(start_t, capital)]

    for ts, kind, s in events:
        sid = id(s)
        sym = s.symbol

        if kind == "publish":
            if (len(pending) + len(open_pos)) >= pcfg.max_slots:
                n_skipped_slots += 1
                continue
            if token_count.get(sym, 0) >= pcfg.per_token_cap:
                n_skipped_token_cap += 1
                continue
            slot_cap = capital * pcfg.per_slot_fraction
            pending[sid] = slot_cap
            token_count[sym] = token_count.get(sym, 0) + 1
            n_taken += 1

        elif kind == "fill":
            if sid in pending:
                open_pos[sid] = pending.pop(sid)

        elif kind == "exit":
            if sid in open_pos:
                cap = open_pos.pop(sid)
                pnl = cap * (s.pnl_pct / 100)
                capital += pnl
                equity_curve.append((ts, capital))
                token_count[sym] -= 1
                n_realized += 1
                if pnl > 0:
                    n_wins += 1
                    sum_win_pct += s.pnl_pct
                else:
                    n_losses += 1
                    sum_loss_pct += s.pnl_pct
            elif sid in pending:
                # Limit expired without filling — release the reservation
                pending.pop(sid)
                token_count[sym] -= 1

    # Mark any still-open positions at end (close at last known equity)
    if not equity_curve or equity_curve[-1][0] < eligible[-1].breakout_ts:
        equity_curve.append((eligible[-1].breakout_ts, capital))

    # Metrics
    eq_arr = np.array([e[1] for e in equity_curve])
    total_return = (capital - pcfg.initial_capital) / pcfg.initial_capital * 100
    running_max = np.maximum.accumulate(eq_arr)
    drawdowns = (eq_arr - running_max) / running_max * 100
    max_dd = float(drawdowns.min()) if len(drawdowns) else 0.0

    eq_df = pd.DataFrame(equity_curve, columns=["timestamp", "equity"]).set_index("timestamp")
    eq_df = eq_df[~eq_df.index.duplicated(keep="last")]
    monthly = eq_df["equity"].resample("ME").last().ffill().pct_change().dropna() * 100
    if len(monthly) > 1 and monthly.std() > 0:
        monthly_sharpe = float(monthly.mean() / monthly.std() * np.sqrt(12))
    else:
        monthly_sharpe = 0.0

    if len(equity_curve) >= 2:
        days = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds() / 86400
        years = max(days / 365.25, 0.01)
        cagr = ((capital / pcfg.initial_capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0

    win_rate = (100 * n_wins / n_realized) if n_realized else 0.0
    avg_win = sum_win_pct / n_wins if n_wins else 0.0
    avg_loss = sum_loss_pct / n_losses if n_losses else 0.0

    return {
        "max_slots": pcfg.max_slots,
        "per_token_cap": pcfg.per_token_cap,
        "per_slot_fraction": pcfg.per_slot_fraction,
        "start_ts": str(pcfg.start_ts)[:10] if pcfg.start_ts else "all",
        "n_eligible": len(eligible),
        "n_taken": n_taken,
        "n_realized": n_realized,
        "n_skipped_slots": n_skipped_slots,
        "n_skipped_token_cap": n_skipped_token_cap,
        "initial_capital": pcfg.initial_capital,
        "final_equity": round(capital, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_dd_pct": round(max_dd, 2),
        "monthly_sharpe": round(monthly_sharpe, 2),
        "calmar": round(calmar, 2),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "equity_curve": equity_curve,
        "monthly_returns": [(str(t)[:7], round(v, 2)) for t, v in monthly.items()],
    }


def _empty_result(pcfg: PortfolioConfig) -> dict:
    return {
        "max_slots": pcfg.max_slots, "per_token_cap": pcfg.per_token_cap,
        "per_slot_fraction": pcfg.per_slot_fraction,
        "start_ts": str(pcfg.start_ts)[:10] if pcfg.start_ts else "all",
        "n_eligible": 0, "n_taken": 0, "n_realized": 0,
        "n_skipped_slots": 0, "n_skipped_token_cap": 0,
        "initial_capital": pcfg.initial_capital,
        "final_equity": pcfg.initial_capital, "total_return_pct": 0.0,
        "cagr_pct": 0.0, "max_dd_pct": 0.0, "monthly_sharpe": 0.0, "calmar": 0.0,
        "win_rate_pct": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
        "equity_curve": [], "monthly_returns": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Analyses
# ─────────────────────────────────────────────────────────────────────────────

def run_filter_ab(universe: str, hourly, daily_ohlcv, bars_4h, sr_cache,
                  filter_per_token_cap: bool) -> list[dict]:
    log.info("ANALYSIS 1 — BTC filter A/B (single-slot + multi-slot)")
    rows = []
    for btc_on in [False, True]:
        cfg = replace(WINNERS[universe], btc_uptrend=btc_on)
        signals = build_signal_feed(universe, cfg, hourly, daily_ohlcv, bars_4h, sr_cache,
                                    filter_per_token_cap=filter_per_token_cap)
        log.info("  btc_uptrend=%s: %d published signals", btc_on, len(signals))

        # Single-slot
        pcfg = PortfolioConfig(max_slots=1, per_slot_fraction=1.0, per_token_cap=1)
        r = portfolio_backtest(signals, pcfg)
        r["btc_filter"] = "ON" if btc_on else "OFF"
        r["mode"] = "single-slot"
        rows.append(r)

        # Multi-slot 4×1 (selected17 sweet spot)
        pcfg2 = PortfolioConfig(max_slots=4, per_slot_fraction=0.25, per_token_cap=1)
        r2 = portfolio_backtest(signals, pcfg2)
        r2["btc_filter"] = "ON" if btc_on else "OFF"
        r2["mode"] = "multi-slot(4×1)"
        rows.append(r2)

        # Higher-slot multi (12×1, for signal-heavy universes like hl44)
        pcfg3 = PortfolioConfig(max_slots=12, per_slot_fraction=1.0/12, per_token_cap=1)
        r3 = portfolio_backtest(signals, pcfg3)
        r3["btc_filter"] = "ON" if btc_on else "OFF"
        r3["mode"] = "multi-slot(12×1)"
        rows.append(r3)
    return rows


def run_slot_sweep(universe: str, hourly, daily_ohlcv, bars_4h, sr_cache,
                   filter_per_token_cap: bool) -> list[dict]:
    log.info("ANALYSIS 2 — Slot count × BTC filter sweep")
    # Slot range extended to test hl44 saturation hypothesis. With
    # filter_per_token_cap=True (Path B), portfolio per_token_cap is
    # redundant beyond 1 (filter already dedupes per-token); we keep
    # tok=1 only and focus the sweep on slot count.
    SLOT_RANGE = [2, 4, 6, 8, 10, 12, 15, 20]
    rows = []
    for btc_on in [False, True]:
        cfg = replace(WINNERS[universe], btc_uptrend=btc_on)
        signals = build_signal_feed(universe, cfg, hourly, daily_ohlcv, bars_4h, sr_cache,
                                    filter_per_token_cap=filter_per_token_cap)
        log.info("  btc_uptrend=%s: %d signals", btc_on, len(signals))
        for max_slots in SLOT_RANGE:
            pcfg = PortfolioConfig(
                max_slots=max_slots,
                per_slot_fraction=1.0 / max_slots,
                per_token_cap=1,
            )
            r = portfolio_backtest(signals, pcfg)
            r["btc_filter"] = "ON" if btc_on else "OFF"
            rows.append(r)
    return rows


def run_start_date_sensitivity(universe: str, hourly, daily_ohlcv, bars_4h, sr_cache,
                               filter_per_token_cap: bool) -> list[dict]:
    log.info("ANALYSIS 3 — Start-date sensitivity × BTC filter")
    rows = []
    for btc_on in [False, True]:
        cfg = replace(WINNERS[universe], btc_uptrend=btc_on)
        signals = build_signal_feed(universe, cfg, hourly, daily_ohlcv, bars_4h, sr_cache,
                                    filter_per_token_cap=filter_per_token_cap)
        if not signals:
            continue

        first = min(s.breakout_ts for s in signals).normalize().replace(day=1)
        last = max(s.breakout_ts for s in signals).normalize() - pd.Timedelta(days=30)
        starts = []
        m = first
        while m <= last:
            starts.append(m)
            m = (m.replace(year=m.year + 1, month=1) if m.month == 12
                 else m.replace(month=m.month + 1))

        for start in starts:
            for slots in [1, 4, 12]:
                pcfg = PortfolioConfig(
                    max_slots=slots,
                    per_slot_fraction=1.0 / slots,
                    per_token_cap=1,            # Option α at portfolio (matches filter)
                    start_ts=start,
                )
                r = portfolio_backtest(signals, pcfg)
                r["btc_filter"] = "ON" if btc_on else "OFF"
                rows.append(r)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Report rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_report(
    universe: str,
    filter_ab: list[dict],
    slot_sweep: list[dict],
    start_sens: list[dict],
    started: datetime,
    finished: datetime,
    filter_per_token_cap: bool,
) -> str:
    L: list[str] = []
    L.append(f"# SR Buyback Portfolio Analysis — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append(f"- Universe: `{universe}`")
    L.append(f"- Strategy: SR Buyback Phase 4 winner "
             f"(`max_d=1.5, sl=0.5, major=Y, mtND=Y, rsiMin=60, hold=21d`)")
    if filter_per_token_cap:
        L.append("- Filter-level per-token cap: **ON (=1)** — walk-forward conditions / Path B")
        L.append("  Portfolio's `per_token_cap` is therefore redundant beyond 1 (only 1 signal per token published).")
    else:
        L.append("- Filter-level per-token cap: **OFF** — Path A / Option β")
        L.append("  Portfolio's `per_token_cap` controls concurrent positions per token.")
    L.append(f"- Wall: {(finished - started).total_seconds() / 60:.1f} min")
    L.append("")

    # A1 — Filter A/B
    L.append("## A1. BTC filter A/B comparison")
    L.append("")
    L.append("Same portfolio config (single-slot AND 4-slot multi), btc_uptrend ON vs OFF.")
    L.append("Best PnL+DD combo for each is highlighted by Sharpe and Calmar.")
    L.append("")
    L.append("| Filter | Mode | n_eligible | n_taken | n_real | Return% | MaxDD% | CAGR% | Sharpe | Calmar | WR% |")
    L.append("|--------|------|------------|---------|--------|---------|--------|-------|--------|--------|-----|")
    for r in filter_ab:
        L.append(
            f"| {r['btc_filter']} | {r['mode']} | {r['n_eligible']} | "
            f"{r['n_taken']} | {r['n_realized']} | "
            f"{r['total_return_pct']:+.1f}% | {r['max_dd_pct']:.1f}% | "
            f"{r['cagr_pct']:+.1f}% | {r['monthly_sharpe']:.2f} | "
            f"{r['calmar']:.2f} | {r['win_rate_pct']:.1f}% |"
        )
    L.append("")

    # A2 — Slot sweep × BTC filter
    L.append("## A2. Slot count × BTC filter")
    L.append("")
    L.append("Sweep `max_slots ∈ {2, 4, 6, 8, 10, 12, 15, 20}` with `per_slot_fraction = 1/N`")
    L.append("and `per_token_cap = 1` (filter already dedupes per token).")
    L.append("")
    for btc_label in ["OFF", "ON"]:
        L.append(f"### BTC filter {btc_label}")
        L.append("")
        L.append("| Slots | per-slot | n_taken | n_real | Return% | MaxDD% | CAGR% | Sharpe | Calmar | WR% |")
        L.append("|-------|----------|---------|--------|---------|--------|-------|--------|--------|-----|")
        for r in slot_sweep:
            if r.get("btc_filter") != btc_label:
                continue
            L.append(
                f"| {r['max_slots']} | "
                f"{r['per_slot_fraction']*100:.1f}% | "
                f"{r['n_taken']} | {r['n_realized']} | "
                f"{r['total_return_pct']:+.1f}% | {r['max_dd_pct']:.1f}% | "
                f"{r['cagr_pct']:+.1f}% | {r['monthly_sharpe']:.2f} | "
                f"{r['calmar']:.2f} | {r['win_rate_pct']:.1f}% |"
            )
        L.append("")

    # Best across BOTH filter modes
    if slot_sweep:
        L.append("### Cross-filter best-of")
        L.append("")
        best_sharpe = max(slot_sweep, key=lambda r: r["monthly_sharpe"])
        best_calmar = max(slot_sweep, key=lambda r: r["calmar"])
        best_ret = max(slot_sweep, key=lambda r: r["total_return_pct"])
        L.append(f"**Best Sharpe**: filter={best_sharpe['btc_filter']} "
                 f"slots={best_sharpe['max_slots']} → "
                 f"Sharpe {best_sharpe['monthly_sharpe']:.2f}, "
                 f"Return {best_sharpe['total_return_pct']:+.1f}%, "
                 f"DD {best_sharpe['max_dd_pct']:.1f}%")
        L.append(f"**Best Calmar**: filter={best_calmar['btc_filter']} "
                 f"slots={best_calmar['max_slots']} → "
                 f"Calmar {best_calmar['calmar']:.2f}, "
                 f"Return {best_calmar['total_return_pct']:+.1f}%, "
                 f"DD {best_calmar['max_dd_pct']:.1f}%")
        L.append(f"**Best Return**: filter={best_ret['btc_filter']} "
                 f"slots={best_ret['max_slots']} → "
                 f"Return {best_ret['total_return_pct']:+.1f}%, "
                 f"DD {best_ret['max_dd_pct']:.1f}%, "
                 f"Sharpe {best_ret['monthly_sharpe']:.2f}")
    L.append("")

    # A3 — Start sensitivity × BTC filter
    L.append("## A3. Start-date sensitivity × BTC filter")
    L.append("")
    L.append("Investor return depending on when they started. Slots ∈ {1, 4, 12}, tok_cap=1.")
    L.append("")
    for btc_label in ["OFF", "ON"]:
        sub = [r for r in start_sens if r.get("btc_filter") == btc_label]
        if not sub:
            continue
        L.append(f"### BTC filter {btc_label}")
        L.append("")
        L.append("| Start | Slots | n_taken | Return% | MaxDD% | Sharpe |")
        L.append("|-------|-------|---------|---------|--------|--------|")
        for r in sub:
            L.append(
                f"| {r['start_ts']} | {r['max_slots']} | {r['n_taken']} | "
                f"{r['total_return_pct']:+.1f}% | {r['max_dd_pct']:.1f}% | "
                f"{r['monthly_sharpe']:.2f} |"
            )
        rets = [r["total_return_pct"] for r in sub]
        dds = [r["max_dd_pct"] for r in sub]
        sharpes = [r["monthly_sharpe"] for r in sub]
        positive = sum(1 for r in rets if r > 0)
        L.append("")
        L.append(f"**{btc_label} spread**: {positive}/{len(rets)} positive  "
                 f"(min {min(rets):+.1f}%, max {max(rets):+.1f}%, "
                 f"median {sorted(rets)[len(rets)//2]:+.1f}%)  "
                 f"|  DD worst {min(dds):.1f}%, median {sorted(dds)[len(dds)//2]:.1f}%  "
                 f"|  Sharpe range {min(sharpes):.2f}..{max(sharpes):.2f}")
        L.append("")

    # Best config summary
    L.append("## Verdict")
    L.append("")
    L.append("Pick the (filter, slots, token_cap) combo with the best DD-adjusted return")
    L.append("(Sharpe + Calmar). If A1 shows ON > OFF on Sharpe, the BTC filter is")
    L.append("doing real work at the portfolio level. If A2's best is at higher slot")
    L.append("counts, capital efficiency rewards diversification.")
    L.append("")
    L.append("_CSV exports: `portfolio_filter_ab_<UTC>.csv`, "
             "`portfolio_slot_sweep_<UTC>.csv`, `portfolio_start_sens_<UTC>.csv`_")
    L.append("_Equity curves for best configs in `portfolio_equity_<UTC>.csv`._")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _strip_curve(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("equity_curve", "monthly_returns")}


def _equity_to_df(r: dict, label: str) -> pd.DataFrame:
    rows = [{"label": label, "timestamp": t, "equity": e} for t, e in r["equity_curve"]]
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="SR Buyback portfolio analysis")
    parser.add_argument("--universe", default="selected17",
                        choices=list(UNIVERSES.keys()))
    parser.add_argument("--no-filter-token-cap", action="store_true",
                        help="Disable per-token cap at filter level (Option β at filter)")
    args = parser.parse_args()
    filter_per_token_cap = not args.no_filter_token_cap
    log.info("Filter per-token cap: %s (Path %s)",
             filter_per_token_cap, "B" if filter_per_token_cap else "A")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")

    # Load shared infrastructure once
    log.info("Loading hourly + SR cache for %s", args.universe)
    hourly = load_hourly(UNIVERSES[args.universe]["tokens"])
    daily_ohlcv = aggregate_to_daily(hourly)
    bars_4h = aggregate_to_4h(hourly)
    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    u_tokens = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    sr_cache = build_sr_cache_full({t: daily_ohlcv[t] for t in u_tokens}, cache_path)

    filter_ab = run_filter_ab(args.universe, hourly, daily_ohlcv, bars_4h, sr_cache,
                              filter_per_token_cap)
    slot_sweep = run_slot_sweep(args.universe, hourly, daily_ohlcv, bars_4h, sr_cache,
                                filter_per_token_cap)
    start_sens = run_start_date_sensitivity(args.universe, hourly, daily_ohlcv,
                                            bars_4h, sr_cache, filter_per_token_cap)

    # CSVs (universe in filename so parallel runs don't collide)
    u = args.universe
    pd.DataFrame([_strip_curve(r) for r in filter_ab]).to_csv(
        REPORT_DIR / f"portfolio_{u}_filter_ab_{stamp}.csv", index=False)
    pd.DataFrame([_strip_curve(r) for r in slot_sweep]).to_csv(
        REPORT_DIR / f"portfolio_{u}_slot_sweep_{stamp}.csv", index=False)
    pd.DataFrame([_strip_curve(r) for r in start_sens]).to_csv(
        REPORT_DIR / f"portfolio_{u}_start_sens_{stamp}.csv", index=False)

    # Equity curves for best configs
    eq_frames = []
    if filter_ab:
        best_ab = max(filter_ab, key=lambda r: r["monthly_sharpe"])
        eq_frames.append(_equity_to_df(best_ab, f"A1_best_{best_ab['btc_filter']}_{best_ab['mode']}"))
    if slot_sweep:
        best_sw = max(slot_sweep, key=lambda r: r["monthly_sharpe"])
        eq_frames.append(_equity_to_df(best_sw,
            f"A2_best_slots{best_sw['max_slots']}_tok{best_sw['per_token_cap']}"))
    if eq_frames:
        pd.concat(eq_frames, ignore_index=True).to_csv(
            REPORT_DIR / f"portfolio_{u}_equity_{stamp}.csv", index=False)

    # Report
    finished = datetime.now(timezone.utc)
    report = render_report(args.universe, filter_ab, slot_sweep, start_sens,
                           started, finished, filter_per_token_cap)
    suffix = "B" if filter_per_token_cap else "A"
    report_path = REPORT_DIR / f"portfolio_{u}_path{suffix}_{stamp}.md"
    report_path.write_text(report)

    log.info("DONE — report: %s", report_path)
    print("\n" + "=" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
