#!/usr/bin/env python3
"""
backtest_mtf.py — Backtest the MTF SMA Regime Gate filter.
===========================================================

When the MTF gate is ON → enter long at next day's open.
TP/SL from S/R analysis (computed with data available at entry time only).
Exit intrabar at TP/SL price, or at close on timeout day.

Full equity compounding: start with $1,000, reinvest everything.

Usage:
    python3 backtest_mtf.py BTC
    python3 backtest_mtf.py BTC ETH SOL
    python3 backtest_mtf.py BTC --days 730 --timeout 7
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)

from core.filters import (
    detect_regime_series,
    MTF_REGIME_CONFIG as REGIME_CONFIG,
    MTF_GATE_TABLE as GATE_TABLE,
)
from core.sr_analysis import ProfessionalSRAnalysis, analyze_token
from core.tpsl import compute_tp_sl
from core.models import fmt_price


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

INITIAL_EQUITY = 1000.0
DEFAULT_TIMEOUT = 7       # max hold days
MIN_DATA_FOR_ENTRY = 120  # need enough history for SMA100 + slope


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_date: str
    entry_price: float
    tp: float
    sl: float
    rr: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""    # "TP" | "SL" | "TIMEOUT"
    pnl_pct: float = 0.0
    pnl_abs: float = 0.0
    equity_after: float = 0.0
    hold_days: int = 0


@dataclass
class BacktestResult:
    symbol: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_return_pct: float
    final_equity: float
    max_drawdown_pct: float
    avg_return_pct: float
    avg_hold_days: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Gate series computation
# ─────────────────────────────────────────────────────────────

def compute_gate_series(df: pd.DataFrame) -> pd.Series:
    """Compute the ON/OFF gate for every bar."""
    close = df["close"]

    lt_cfg = REGIME_CONFIG["LT"]
    mt_cfg = REGIME_CONFIG["MT"]
    st_cfg = REGIME_CONFIG["ST"]

    lt = detect_regime_series(close, lt_cfg["sma"], lt_cfg["slope_bars"], lt_cfg["confirm"])
    mt = detect_regime_series(close, mt_cfg["sma"], mt_cfg["slope_bars"], mt_cfg["confirm"])
    st = detect_regime_series(close, st_cfg["sma"], st_cfg["slope_bars"], st_cfg["confirm"])

    gate = pd.Series(False, index=df.index)
    for i in range(len(df)):
        combo = (lt.iloc[i], mt.iloc[i], st.iloc[i])
        gate.iloc[i] = GATE_TABLE.get(combo, False)

    return gate


# ─────────────────────────────────────────────────────────────
# S/R analysis at a point in time (no lookahead)
# ─────────────────────────────────────────────────────────────

def get_tp_sl_at(df: pd.DataFrame, bar_idx: int, symbol: str) -> Optional[dict]:
    """Compute TP/SL using only data available up to bar_idx (inclusive).
    No lookahead bias."""
    if bar_idx < MIN_DATA_FOR_ENTRY:
        return None

    # Use only data up to and including bar_idx
    historical = df.iloc[:bar_idx + 1].copy()

    try:
        analysis = analyze_token(symbol, historical)
        return compute_tp_sl(analysis)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Backtest engine
# ─────────────────────────────────────────────────────────────

def run_backtest(symbol: str, df: pd.DataFrame,
                 timeout: int = DEFAULT_TIMEOUT) -> BacktestResult:
    """Run the MTF gate backtest on one token."""

    gate = compute_gate_series(df)
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    closes = df["close"].values
    dates = df.index

    equity = INITIAL_EQUITY
    trades = []
    equity_curve = [equity]
    peak_equity = equity

    in_position = False
    entry_price = 0.0
    tp = 0.0
    sl = 0.0
    entry_idx = 0
    entry_equity = 0.0

    for i in range(MIN_DATA_FOR_ENTRY, n):
        if not in_position:
            # Check if gate was ON at previous close (signal) → enter at today's open
            if i > 0 and gate.iloc[i - 1]:
                # Compute TP/SL using data up to previous close (no lookahead)
                tpsl = get_tp_sl_at(df, i - 1, symbol)
                if tpsl and tpsl.get("take_profit") and tpsl.get("stop_loss"):
                    entry_price = opens[i]
                    tp = tpsl["take_profit"]
                    sl = tpsl["stop_loss"]

                    # Sanity: TP must be above entry, SL must be below
                    if tp > entry_price and sl < entry_price:
                        in_position = True
                        entry_idx = i
                        entry_equity = equity
                        if i % 50 == 0:
                            print(f"    [{dates[i].date()}] Entry @ "
                                  f"{fmt_price(entry_price)} "
                                  f"TP={fmt_price(tp)} SL={fmt_price(sl)}",
                                  file=sys.stderr)

        if in_position:
            hold_days = i - entry_idx

            # Check intrabar: did price hit SL or TP during this bar?
            # SL checked first (conservative — assume worst case)
            hit_sl = lows[i] <= sl
            hit_tp = highs[i] >= tp

            exit_price = 0.0
            exit_reason = ""

            if hit_sl and hit_tp:
                # Both hit in same bar — assume SL hit first (conservative)
                exit_price = sl
                exit_reason = "SL"
            elif hit_sl:
                exit_price = sl
                exit_reason = "SL"
            elif hit_tp:
                exit_price = tp
                exit_reason = "TP"
            elif hold_days >= timeout:
                # Timeout — exit at this bar's close
                exit_price = closes[i]
                exit_reason = "TIMEOUT"

            if exit_reason:
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                pnl_abs = entry_equity * (exit_price - entry_price) / entry_price
                equity = entry_equity + pnl_abs

                rr = 0
                potential_loss = entry_price - sl
                if potential_loss > 0:
                    rr = (tp - entry_price) / potential_loss

                trades.append(Trade(
                    entry_date=str(dates[entry_idx].date()),
                    entry_price=round(entry_price, 2),
                    tp=round(tp, 2),
                    sl=round(sl, 2),
                    rr=round(rr, 2),
                    exit_date=str(dates[i].date()),
                    exit_price=round(exit_price, 2),
                    exit_reason=exit_reason,
                    pnl_pct=round(pnl_pct, 2),
                    pnl_abs=round(pnl_abs, 2),
                    equity_after=round(equity, 2),
                    hold_days=hold_days,
                ))

                in_position = False

        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)

    # Close any open position at last bar's close
    if in_position:
        exit_price = closes[-1]
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        pnl_abs = entry_equity * (exit_price - entry_price) / entry_price
        equity = entry_equity + pnl_abs
        potential_loss = entry_price - sl
        rr = (tp - entry_price) / potential_loss if potential_loss > 0 else 0

        trades.append(Trade(
            entry_date=str(dates[entry_idx].date()),
            entry_price=round(entry_price, 2),
            tp=round(tp, 2), sl=round(sl, 2), rr=round(rr, 2),
            exit_date=str(dates[-1].date()),
            exit_price=round(exit_price, 2),
            exit_reason="OPEN",
            pnl_pct=round(pnl_pct, 2),
            pnl_abs=round(pnl_abs, 2),
            equity_after=round(equity, 2),
            hold_days=len(df) - 1 - entry_idx,
        ))

    # Compute stats
    wins = len([t for t in trades if t.pnl_pct > 0])
    losses = len([t for t in trades if t.pnl_pct <= 0])
    total = len(trades)
    win_rate = wins / total * 100 if total > 0 else 0

    # Max drawdown from equity curve
    peak = INITIAL_EQUITY
    max_dd = 0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100
        max_dd = max(max_dd, dd)

    avg_ret = np.mean([t.pnl_pct for t in trades]) if trades else 0
    avg_hold = np.mean([t.hold_days for t in trades]) if trades else 0
    total_ret = (equity - INITIAL_EQUITY) / INITIAL_EQUITY * 100

    return BacktestResult(
        symbol=symbol,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 1),
        total_return_pct=round(total_ret, 2),
        final_equity=round(equity, 2),
        max_drawdown_pct=round(max_dd, 2),
        avg_return_pct=round(avg_ret, 2),
        avg_hold_days=round(avg_hold, 1),
        trades=trades,
        equity_curve=equity_curve,
    )


# ─────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────

def print_result(r: BacktestResult):
    sep = "=" * 80
    print(f"\n{sep}")
    print(f"  {r.symbol}  MTF Gate Backtest")
    print(sep)
    print(f"  Initial equity:  ${INITIAL_EQUITY:,.0f}")
    print(f"  Final equity:    ${r.final_equity:,.2f}")
    print(f"  Total return:    {r.total_return_pct:+.2f}%")
    print(f"  Max drawdown:    {r.max_drawdown_pct:.2f}%")
    print(f"  Total trades:    {r.total_trades}")
    print(f"  Wins / Losses:   {r.wins} / {r.losses}")
    print(f"  Win rate:        {r.win_rate:.1f}%")
    print(f"  Avg return:      {r.avg_return_pct:+.2f}%")
    print(f"  Avg hold:        {r.avg_hold_days:.1f} days")
    print(f"{'─' * 80}")

    if r.trades:
        print(f"  {'#':>3} {'Entry':>12} {'Exit':>12} {'Entry$':>10} {'Exit$':>10} "
              f"{'TP':>10} {'SL':>10} {'R:R':>5} {'Reason':>8} {'PnL%':>7} {'Equity':>10}")
        print(f"  {'─' * 76}")
        for i, t in enumerate(r.trades, 1):
            print(f"  {i:>3} {t.entry_date:>12} {t.exit_date:>12} "
                  f"{fmt_price(t.entry_price):>10} {fmt_price(t.exit_price):>10} "
                  f"{fmt_price(t.tp):>10} {fmt_price(t.sl):>10} "
                  f"{t.rr:>5.1f} {t.exit_reason:>8} {t.pnl_pct:>+6.1f}% "
                  f"${t.equity_after:>9,.2f}")
    print(sep)


def generate_equity_chart(r: BacktestResult, df: pd.DataFrame, output: str = None):
    """Generate equity curve chart."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 8), height_ratios=[2, 1],
                                    sharex=True)

    # Price chart
    ax1.plot(range(len(df)), df["close"].values, color="#666", linewidth=0.8, alpha=0.7)
    ax1.set_ylabel("Price")
    ax1.set_title(f"{r.symbol} — MTF Gate Backtest | "
                  f"Return: {r.total_return_pct:+.1f}% | "
                  f"Trades: {r.total_trades} | "
                  f"Win rate: {r.win_rate:.0f}%",
                  fontsize=10, fontweight="bold")

    # Mark trades on price chart
    for t in r.trades:
        try:
            entry_idx = df.index.get_loc(pd.Timestamp(t.entry_date))
            exit_idx = df.index.get_loc(pd.Timestamp(t.exit_date))
        except (KeyError, TypeError):
            continue

        color = "#4CAF50" if t.pnl_pct > 0 else "#E53935"
        ax1.axvspan(entry_idx, exit_idx, alpha=0.15, color=color)
        ax1.plot(entry_idx, t.entry_price, "^", color=color, markersize=6)
        ax1.plot(exit_idx, t.exit_price, "v", color=color, markersize=6)

    # Equity curve
    eq = r.equity_curve
    while len(eq) < len(df):
        eq.append(eq[-1])
    eq = eq[:len(df)]

    ax2.plot(range(len(eq)), eq, color="#1976D2", linewidth=1.2)
    ax2.axhline(y=INITIAL_EQUITY, color="#999", linewidth=0.5, linestyle="--")
    ax2.set_ylabel("Equity ($)")
    ax2.fill_between(range(len(eq)), INITIAL_EQUITY, eq,
                     where=[e >= INITIAL_EQUITY for e in eq],
                     alpha=0.15, color="#4CAF50")
    ax2.fill_between(range(len(eq)), INITIAL_EQUITY, eq,
                     where=[e < INITIAL_EQUITY for e in eq],
                     alpha=0.15, color="#E53935")

    plt.tight_layout()

    if output is None:
        os.makedirs("output", exist_ok=True)
        output = f"output/{r.symbol}_backtest_mtf.png"

    fig.savefig(output, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  Chart saved: {output}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest MTF SMA Regime Gate")
    parser.add_argument("tokens", nargs="+", help="Token symbols")
    parser.add_argument("--days", type=int, default=730, help="Days of data")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="Max hold days")
    parser.add_argument("--data-dir", default="data", help="Data directory")
    parser.add_argument("--chart", action="store_true", help="Generate equity chart")
    args = parser.parse_args()

    for symbol in args.tokens:
        symbol = symbol.upper()
        print(f"\n  Loading {symbol} ({args.days} days)...", file=sys.stderr)

        # Try 2y file first, then daily cache, then fetch
        df = None
        for path in [f"{args.data_dir}/{symbol}_2y_daily.csv",
                     f"{args.data_dir}/{symbol}_2y.csv",
                     f"{args.data_dir}/{symbol}_daily.csv"]:
            if os.path.exists(path):
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                df.columns = [c.lower().strip() for c in df.columns]
                print(f"  Loaded {len(df)} candles from {path}", file=sys.stderr)
                break

        if df is None:
            from core.fetcher import fetch_data
            df = fetch_data(symbol, args.days)
            print(f"  Fetched {len(df)} candles", file=sys.stderr)

        if len(df) < MIN_DATA_FOR_ENTRY + 30:
            print(f"  Not enough data for {symbol}", file=sys.stderr)
            continue

        print(f"  Running backtest (timeout={args.timeout}d)...", file=sys.stderr)
        result = run_backtest(symbol, df, timeout=args.timeout)
        print_result(result)

        if args.chart:
            generate_equity_chart(result, df)


if __name__ == "__main__":
    main()
