#!/usr/bin/env python3
"""
backtest_model.py — Backtest the S/R model with TP/SL/Timeout.
================================================================

Rules:
  - Entry: close price on the day the model qualifies (R:R >= 1.2)
  - TP/SL: limit orders at model's levels
  - Same-day hit: if both TP and SL touched → assume loss (conservative)
  - Timeout: 7 days, close at that day's close
  - Capital: $1,000 total, $100 per trade
  - No look-ahead: re-run analysis on data available up to that day

Usage:
    python3 backtest_model.py
    python3 backtest_model.py --tokens BTC ETH SOL --days 365
"""

import argparse
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.join(_THIS_DIR, '..')
sys.path.insert(0, _PARENT_DIR)

from core.fetcher import fetch_data
from core.sr_analysis import analyze_token
from core.tpsl import compute_tp_sl
from core.models import fmt_price, compute_atr
from core import config


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    take_profit: float
    stop_loss: float
    raw_rr: float
    size_usd: float
    # Filled on exit
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # "TP", "SL", "TIMEOUT"
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    hold_days: int = 0


# ─────────────────────────────────────────────────────────────
# Backtest Engine
# ─────────────────────────────────────────────────────────────

MIN_HISTORY = 60      # minimum candles before first signal
MAX_HOLD_DAYS = 7     # timeout
TRADE_SIZE = 100.0    # USD per trade
STARTING_CAPITAL = 1000.0


def run_backtest(symbol: str, df: pd.DataFrame) -> List[Trade]:
    """Walk through daily candles, generate signals, track trades."""
    n = len(df)
    trades = []
    open_trades: List[Trade] = []

    print(f"\n  [{symbol}] {n} candles, backtesting from day {MIN_HISTORY}...", file=sys.stderr)

    for day in range(MIN_HISTORY, n):
        current_date = str(df.index[day])[:10]
        current_close = float(df["close"].iloc[day])
        current_high = float(df["high"].iloc[day])
        current_low = float(df["low"].iloc[day])

        # ── Check open trades for exit ────────────────────────
        still_open = []
        for t in open_trades:
            t.hold_days += 1

            # Check if TP or SL hit today
            tp_hit = current_high >= t.take_profit
            sl_hit = current_low <= t.stop_loss

            if tp_hit and sl_hit:
                # Both hit same day → conservative: assume loss
                t.exit_date = current_date
                t.exit_price = t.stop_loss
                t.exit_reason = "SL"
                t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                t.pnl_usd = t.size_usd * t.pnl_pct / 100
                trades.append(t)
            elif tp_hit:
                t.exit_date = current_date
                t.exit_price = t.take_profit
                t.exit_reason = "TP"
                t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                t.pnl_usd = t.size_usd * t.pnl_pct / 100
                trades.append(t)
            elif sl_hit:
                t.exit_date = current_date
                t.exit_price = t.stop_loss
                t.exit_reason = "SL"
                t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                t.pnl_usd = t.size_usd * t.pnl_pct / 100
                trades.append(t)
            elif t.hold_days >= MAX_HOLD_DAYS:
                # Timeout: close at today's close
                t.exit_date = current_date
                t.exit_price = current_close
                t.exit_reason = "TIMEOUT"
                t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                t.pnl_usd = t.size_usd * t.pnl_pct / 100
                trades.append(t)
            else:
                still_open.append(t)

        open_trades = still_open

        # ── Generate signal (no look-ahead: use data up to today) ──
        # Only check for new signals if we're not on the last day
        if day >= n - 1:
            continue

        history = df.iloc[:day + 1]  # data up to and including today
        if len(history) < MIN_HISTORY:
            continue

        try:
            analysis = analyze_token(symbol, history)
            result = compute_tp_sl(analysis)
        except Exception:
            continue

        if result is None or not result.get("qualified"):
            continue

        tp = result["take_profit"]
        sl = result["stop_loss"]
        rr = result["raw_rr"]

        # Don't open duplicate: skip if we already have an open trade for this symbol
        if any(t.symbol == symbol for t in open_trades):
            continue

        # Open new trade
        trade = Trade(
            symbol=symbol,
            entry_date=current_date,
            entry_price=current_close,
            take_profit=tp,
            stop_loss=sl,
            raw_rr=rr,
            size_usd=TRADE_SIZE,
        )
        open_trades.append(trade)

        if len(trades) % 20 == 0 and trades:
            print(f"    day {day}/{n} — {len(trades)} closed, {len(open_trades)} open", file=sys.stderr)

    # Close any remaining open trades at last close
    last_close = float(df["close"].iloc[-1])
    last_date = str(df.index[-1])[:10]
    for t in open_trades:
        t.exit_date = last_date
        t.exit_price = last_close
        t.exit_reason = "TIMEOUT"
        t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
        t.pnl_usd = t.size_usd * t.pnl_pct / 100
        trades.append(t)

    return trades


# ─────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────

def print_report(all_trades: List[Trade], symbols: List[str]):
    """Print backtest results."""
    if not all_trades:
        print("\n  No trades generated.")
        return

    total = len(all_trades)
    tp_trades = [t for t in all_trades if t.exit_reason == "TP"]
    sl_trades = [t for t in all_trades if t.exit_reason == "SL"]
    to_trades = [t for t in all_trades if t.exit_reason == "TIMEOUT"]

    total_pnl = sum(t.pnl_usd for t in all_trades)
    win_rate = len(tp_trades) / total * 100 if total > 0 else 0
    avg_win = np.mean([t.pnl_pct for t in tp_trades]) if tp_trades else 0
    avg_loss = np.mean([t.pnl_pct for t in sl_trades]) if sl_trades else 0
    avg_hold = np.mean([t.hold_days for t in all_trades])

    print(f"\n{'='*70}")
    print(f"  BACKTEST RESULTS — {', '.join(symbols)}")
    print(f"{'='*70}")
    print(f"  Capital: ${STARTING_CAPITAL:,.0f}  |  Trade size: ${TRADE_SIZE:,.0f}")
    print(f"  Max hold: {MAX_HOLD_DAYS} days  |  Min R:R: {config.MIN_RAW_RR}")
    print(f"{'='*70}")

    print(f"\n  SUMMARY")
    print(f"  Total trades:    {total}")
    print(f"  TP hit:          {len(tp_trades)} ({len(tp_trades)/total*100:.0f}%)")
    print(f"  SL hit:          {len(sl_trades)} ({len(sl_trades)/total*100:.0f}%)")
    print(f"  Timeout:         {len(to_trades)} ({len(to_trades)/total*100:.0f}%)")
    print(f"  Win rate:        {win_rate:.1f}%")
    print(f"  Avg win:         {avg_win:+.2f}%")
    print(f"  Avg loss:        {avg_loss:+.2f}%")
    print(f"  Avg hold:        {avg_hold:.1f} days")
    print(f"  Total PnL:       ${total_pnl:+.2f} ({total_pnl/STARTING_CAPITAL*100:+.1f}%)")
    print(f"  Final capital:   ${STARTING_CAPITAL + total_pnl:,.2f}")

    # Per symbol
    print(f"\n  PER SYMBOL")
    print(f"  {'SYMBOL':<8} {'TRADES':>6} {'TP':>4} {'SL':>4} {'TO':>4} {'WIN%':>6} {'PNL':>10}")
    print(f"  {'─'*50}")
    for sym in symbols:
        sym_trades = [t for t in all_trades if t.symbol == sym]
        if not sym_trades:
            continue
        sym_tp = len([t for t in sym_trades if t.exit_reason == "TP"])
        sym_sl = len([t for t in sym_trades if t.exit_reason == "SL"])
        sym_to = len([t for t in sym_trades if t.exit_reason == "TIMEOUT"])
        sym_pnl = sum(t.pnl_usd for t in sym_trades)
        sym_wr = sym_tp / len(sym_trades) * 100 if sym_trades else 0
        print(f"  {sym:<8} {len(sym_trades):>6} {sym_tp:>4} {sym_sl:>4} {sym_to:>4} {sym_wr:>5.1f}% ${sym_pnl:>+8.2f}")

    # Monthly breakdown
    print(f"\n  MONTHLY BREAKDOWN")
    print(f"  {'MONTH':<10} {'TRADES':>6} {'TP':>4} {'SL':>4} {'TO':>4} {'PNL':>10}")
    print(f"  {'─'*45}")
    monthly = {}
    for t in all_trades:
        month = t.entry_date[:7]
        if month not in monthly:
            monthly[month] = {"trades": 0, "tp": 0, "sl": 0, "to": 0, "pnl": 0}
        monthly[month]["trades"] += 1
        monthly[month][t.exit_reason.lower()] = monthly[month].get(t.exit_reason.lower(), 0) + 1
        monthly[month]["pnl"] += t.pnl_usd

    for month in sorted(monthly.keys()):
        m = monthly[month]
        print(f"  {month:<10} {m['trades']:>6} {m.get('tp',0):>4} {m.get('sl',0):>4} "
              f"{m.get('timeout',0):>4} ${m['pnl']:>+8.2f}")

    # Trade log (last 20)
    print(f"\n  RECENT TRADES (last 20)")
    print(f"  {'DATE':>10} {'SYM':<5} {'ENTRY':>10} {'TP':>10} {'SL':>10} "
          f"{'EXIT':>10} {'REASON':>7} {'PNL%':>7} {'PNL$':>8} {'DAYS':>4}")
    print(f"  {'─'*90}")
    for t in all_trades[-20:]:
        print(f"  {t.entry_date:>10} {t.symbol:<5} {fmt_price(t.entry_price):>10} "
              f"{fmt_price(t.take_profit):>10} {fmt_price(t.stop_loss):>10} "
              f"{fmt_price(t.exit_price):>10} {t.exit_reason:>7} "
              f"{t.pnl_pct:>+6.2f}% ${t.pnl_usd:>+7.2f} {t.hold_days:>4}")

    # Equity curve
    print(f"\n  EQUITY CURVE")
    equity = STARTING_CAPITAL
    max_equity = equity
    max_drawdown = 0
    for t in all_trades:
        equity += t.pnl_usd
        max_equity = max(max_equity, equity)
        dd = (max_equity - equity) / max_equity * 100
        max_drawdown = max(max_drawdown, dd)

    print(f"  Start:         ${STARTING_CAPITAL:,.2f}")
    print(f"  End:           ${equity:,.2f}")
    print(f"  Max drawdown:  {max_drawdown:.1f}%")
    print(f"  Return:        {(equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100:+.1f}%")
    print(f"{'='*70}\n")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest S/R model")
    parser.add_argument("--tokens", nargs="+", default=["BTC", "ETH", "SOL"],
                        help="Tokens to backtest (default: BTC ETH SOL)")
    parser.add_argument("--days", type=int, default=365,
                        help="Days of history (default: 365)")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.tokens]
    all_trades = []

    print(f"\n{'='*70}", file=sys.stderr)
    print(f"  S/R MODEL BACKTEST", file=sys.stderr)
    print(f"  Tokens: {', '.join(symbols)}  |  Days: {args.days}", file=sys.stderr)
    print(f"  Capital: ${STARTING_CAPITAL:,.0f}  |  Per trade: ${TRADE_SIZE:,.0f}", file=sys.stderr)
    print(f"  Max hold: {MAX_HOLD_DAYS}d  |  Min R:R: {config.MIN_RAW_RR}", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)

    for symbol in symbols:
        print(f"\n  Fetching {symbol} ({args.days} days)...", file=sys.stderr)
        df = fetch_data(symbol, args.days)
        print(f"  Got {len(df)} candles: {str(df.index[0])[:10]} to {str(df.index[-1])[:10]}",
              file=sys.stderr)

        trades = run_backtest(symbol, df)
        all_trades.extend(trades)
        print(f"  [{symbol}] {len(trades)} trades completed", file=sys.stderr)

    # Sort all trades by entry date
    all_trades.sort(key=lambda t: t.entry_date)

    print_report(all_trades, symbols)


if __name__ == "__main__":
    main()
