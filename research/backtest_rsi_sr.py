#!/usr/bin/env python3
"""
backtest_rsi_sr.py — Compare RSI momentum + S/R TP/SL strategies.
==================================================================
Replicates the strategy comparison format:
  - RSI momentum as entry signal
  - S/R model provides TP and SL
  - Multiple variants: simple, hysteresis, top-N

Usage:
    python3 research/backtest_rsi_sr.py
    python3 research/backtest_rsi_sr.py --tokens BTC ETH SOL
    python3 research/backtest_rsi_sr.py --days 365 --start 2025-02-01
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

# Path setup
_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)

from core.fetcher import fetch_data
from core.sr_analysis import analyze_token
from core.tpsl import compute_tp_sl
from core.models import fmt_price
from core import config
from research.rsi_filter import compute_rsi, rsi_qualifies, rsi_hysteresis_state
from research.regime import compute_indicators


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    take_profit: float
    stop_loss: float
    rsi_at_entry: float
    adx_at_entry: float = 0.0
    di_diff_at_entry: float = 0.0
    size_usd: float = 100.0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # "TP", "SL", "TIMEOUT", "OPEN"
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    hold_days: int = 0


@dataclass
class StrategyConfig:
    name: str
    rsi_period: int = 10
    rsi_threshold: float = 50       # simple threshold
    use_hysteresis: bool = False
    enter_threshold: float = 60     # hysteresis enter
    exit_threshold: float = 50      # hysteresis exit
    top_n: int = 0                  # 0 = no top-N filter
    max_hold_days: int = 10
    diversified_top: int = 0        # 0 = single (1 trade per signal), >0 = max concurrent
    # ADX/DI filter
    use_adx: bool = False
    adx_min: float = 25             # minimum ADX for trend confirmation
    require_di_positive: bool = False  # require DI+ > DI-
    # R:R filter
    min_rr: float = 0               # 0 = no R:R filter
    # BTC RSI floor
    btc_rsi_floor: float = 0        # 0 = disabled; e.g. 50 = skip all trades when BTC RSI < 50
    # Compounding
    compound: bool = False          # True = invest full capital each trade


# ─────────────────────────────────────────────────────────────
# Strategies to compare
# ─────────────────────────────────────────────────────────────

STRATEGIES = [
    # Reference
    StrategyConfig(name="Conservative all tokens", rsi_threshold=60, btc_rsi_floor=50),
    StrategyConfig(name="Conservative top 8", rsi_threshold=60, btc_rsi_floor=50, top_n=8),

    # Top 1: highest RSI (default sort)
    StrategyConfig(name="Top1 highest RSI", rsi_threshold=60, btc_rsi_floor=50, top_n=1),

    # Top 1: RSI>50 (more trades)
    StrategyConfig(name="Top1 RSI>50 + BTC floor", rsi_threshold=50, btc_rsi_floor=50, top_n=1),

    # Top 1: RSI>70 (only very strong momentum)
    StrategyConfig(name="Top1 RSI>70 + BTC floor", rsi_threshold=70, btc_rsi_floor=50, top_n=1),

    # Top 1: with RR filter
    StrategyConfig(name="Top1 RSI>60 + RR>=1.0", rsi_threshold=60, btc_rsi_floor=50,
                   top_n=1, min_rr=1.0),
    StrategyConfig(name="Top1 RSI>60 + RR>=1.5", rsi_threshold=60, btc_rsi_floor=50,
                   top_n=1, min_rr=1.5),

    # Top 1: with ADX
    StrategyConfig(name="Top1 RSI>60 + ADX>20 DI+", rsi_threshold=60, btc_rsi_floor=50,
                   top_n=1, use_adx=True, adx_min=20, require_di_positive=True),

    # Top 1: RSI>60 + ADX + RR
    StrategyConfig(name="Top1 RSI>60 + ADX>20 + RR>=1.0", rsi_threshold=60, btc_rsi_floor=50,
                   top_n=1, use_adx=True, adx_min=20, require_di_positive=True, min_rr=1.0),

    # Top 1: no BTC floor (how much does the floor matter?)
    StrategyConfig(name="Top1 RSI>60 no BTC floor", rsi_threshold=60, btc_rsi_floor=0, top_n=1),

    # ── Compounding (all-in, reinvest full capital) ──
    StrategyConfig(name="COMPOUND Top1 RSI>60", rsi_threshold=60, btc_rsi_floor=50,
                   top_n=1, compound=True),
    StrategyConfig(name="COMPOUND Top1 RSI>70", rsi_threshold=70, btc_rsi_floor=50,
                   top_n=1, compound=True),
    StrategyConfig(name="COMPOUND Top1 RSI>60 + ADX>20 DI+", rsi_threshold=60, btc_rsi_floor=50,
                   top_n=1, compound=True, use_adx=True, adx_min=20, require_di_positive=True),
]

MIN_HISTORY = 60
TRADE_SIZE = 100.0


# ─────────────────────────────────────────────────────────────
# Backtest Engine
# ─────────────────────────────────────────────────────────────

def run_strategy(strat: StrategyConfig, all_data: Dict[str, pd.DataFrame],
                 start_date: str = None) -> List[Trade]:
    """Run one strategy across all tokens."""
    trades = []
    open_trades: Dict[str, Trade] = {}  # symbol → trade
    hysteresis_state: Dict[str, bool] = {}  # symbol → signal on/off
    exited_today: set = set()  # symbols that exited today (can't re-enter same day)
    capital = 1000.0  # track capital for compounding

    # Get all dates from first token
    first_sym = list(all_data.keys())[0]
    all_dates = all_data[first_sym].index
    n = len(all_dates)

    # Track pending entries: signals generated today, entered tomorrow at open
    pending_entries: List[tuple] = []  # [(sym, tp, sl, rsi, adx, di_diff, trade_size)]

    for day in range(MIN_HISTORY, n):
        current_date = str(all_dates[day])[:10]

        # Skip until start date
        if start_date and current_date < start_date:
            pending_entries = []
            continue

        # ── Execute pending entries from yesterday's signals ──
        for sym, tp, sl, rsi, adx_val, di_diff_val, trade_size in pending_entries:
            if sym in open_trades:
                # Already got a position (shouldn't happen with top-1, safety check)
                if strat.compound:
                    capital += trade_size  # return reserved capital
                continue
            df = all_data[sym]
            if day >= len(df):
                if strat.compound:
                    capital += trade_size
                continue
            entry_price = float(df["open"].iloc[day])  # enter at today's OPEN

            trade = Trade(
                symbol=sym,
                entry_date=current_date,
                entry_price=entry_price,
                take_profit=tp,
                stop_loss=sl,
                rsi_at_entry=rsi,
                adx_at_entry=adx_val,
                di_diff_at_entry=di_diff_val,
                size_usd=trade_size,
            )
            open_trades[sym] = trade
        pending_entries = []

        # ── Check open trades for exit ────────────────────────
        exited_today = set()
        to_remove = []
        for sym, t in open_trades.items():
            if sym not in all_data:
                continue
            df = all_data[sym]
            if day >= len(df):
                continue

            t.hold_days += 1
            current_high = float(df["high"].iloc[day])
            current_low = float(df["low"].iloc[day])
            current_close = float(df["close"].iloc[day])

            tp_hit = current_high >= t.take_profit
            sl_hit = current_low <= t.stop_loss

            if tp_hit and sl_hit:
                # Conservative: assume loss
                t.exit_date = current_date
                t.exit_price = t.stop_loss
                t.exit_reason = "SL"
            elif tp_hit:
                t.exit_date = current_date
                t.exit_price = t.take_profit
                t.exit_reason = "TP"
            elif sl_hit:
                t.exit_date = current_date
                t.exit_price = t.stop_loss
                t.exit_reason = "SL"
            elif t.hold_days >= strat.max_hold_days:
                t.exit_date = current_date
                t.exit_price = current_close
                t.exit_reason = "TIMEOUT"

            if t.exit_reason:
                t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                t.pnl_usd = t.size_usd * t.pnl_pct / 100
                if strat.compound:
                    capital += t.size_usd + t.pnl_usd
                trades.append(t)
                to_remove.append(sym)
                exited_today.add(sym)

        for sym in to_remove:
            del open_trades[sym]

        # ── Generate signals (for execution TOMORROW) ─────────
        if day >= n - 1:
            continue

        # BTC RSI floor: check once for the day
        if strat.btc_rsi_floor > 0 and "BTC" in all_data:
            btc_df = all_data["BTC"]
            if day < len(btc_df):
                btc_history = btc_df.iloc[:day + 1]
                btc_rsi = compute_rsi(btc_history, strat.rsi_period)
                if btc_rsi < strat.btc_rsi_floor:
                    continue  # skip all tokens today

        # Collect all qualifying candidates with their RSI
        candidates = []
        for sym, df in all_data.items():
            if day >= len(df):
                continue
            if sym in open_trades or sym in exited_today:
                continue

            history = df.iloc[:day + 1]
            if len(history) < MIN_HISTORY:
                continue

            # Compute RSI
            rsi = compute_rsi(history, strat.rsi_period)

            # Check RSI filter
            if strat.use_hysteresis:
                prev = hysteresis_state.get(sym, False)
                signal_on = rsi_hysteresis_state(rsi, prev,
                                                  strat.enter_threshold,
                                                  strat.exit_threshold)
                hysteresis_state[sym] = signal_on
                if not signal_on:
                    continue
            else:
                if rsi <= strat.rsi_threshold:
                    continue

            # Check ADX/DI filter
            adx_val, di_diff_val = 0.0, 0.0
            if strat.use_adx:
                try:
                    indicators = compute_indicators(history)
                    if "error" in indicators:
                        continue
                    adx_val = indicators.get("adx", 0)
                    plus_di = indicators.get("plus_di", 0)
                    minus_di = indicators.get("minus_di", 0)
                    di_diff_val = plus_di - minus_di
                    if adx_val < strat.adx_min:
                        continue
                    if strat.require_di_positive and di_diff_val <= 0:
                        continue
                except Exception:
                    continue

            candidates.append((sym, rsi, adx_val, di_diff_val, history))

        # Top-N filter: only keep the N highest RSI tokens
        if strat.top_n > 0:
            open_count = len(open_trades)
            slots = max(0, strat.top_n - open_count)
            if slots == 0:
                continue
            candidates.sort(key=lambda x: x[1], reverse=True)  # sort by RSI desc
            candidates = candidates[:slots]

        # Enter trades for qualifying candidates
        for sym, rsi, adx_val, di_diff_val, history in candidates:
            df = all_data[sym]

            # Compute TP/SL from S/R model
            try:
                analysis = analyze_token(sym, history)
                result = compute_tp_sl(analysis)
            except Exception:
                continue

            if result is None:
                continue

            tp = result.get("take_profit")
            sl = result.get("stop_loss")
            if tp is None or sl is None:
                continue

            current_close = float(df["close"].iloc[day])

            # R:R filter (use current close as estimate — actual entry is tomorrow's open)
            if strat.min_rr > 0:
                gain = tp - current_close
                loss = current_close - sl
                if loss <= 0 or (gain / loss) < strat.min_rr:
                    continue

            # Determine trade size
            if strat.compound:
                trade_size = capital
                capital = 0  # reserve capital
            else:
                trade_size = TRADE_SIZE

            # Queue for execution tomorrow at open
            pending_entries.append((sym, tp, sl, rsi, adx_val, di_diff_val, trade_size))

    # Close remaining open trades
    for sym, t in open_trades.items():
        df = all_data[sym]
        t.exit_date = str(df.index[-1])[:10]
        t.exit_price = float(df["close"].iloc[-1])
        t.exit_reason = "OPEN"
        t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
        t.pnl_usd = t.size_usd * t.pnl_pct / 100
        trades.append(t)

    return trades


# ─────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────

def print_comparison(results: Dict[str, List[Trade]], start_date: str, end_date: str):
    """Print strategy comparison table."""
    print(f"\nSTRATEGY COMPARISON  ({start_date} -> {end_date})")
    print("─" * 135)
    print(f"{'Strategy':<50} {'Trades':>6}  {'TP hit':>12}  {'SL hit':>12}  "
          f"{'Timeout':>12}  {'Win rate':>8}  {'Avg P&L':>8}  {'Total$':>8}  {'Final$':>8}")
    print("─" * 135)

    for name, trades in results.items():
        total = len(trades)
        if total == 0:
            print(f"{name:<50} {'0':>6}")
            continue

        tp = [t for t in trades if t.exit_reason == "TP"]
        sl = [t for t in trades if t.exit_reason == "SL"]
        to = [t for t in trades if t.exit_reason in ("TIMEOUT", "OPEN")]
        closed = [t for t in trades if t.exit_reason in ("TP", "SL")]
        win_rate = len(tp) / len(closed) * 100 if closed else 0
        avg_pnl = np.mean([t.pnl_pct for t in trades])
        total_pnl = sum(t.pnl_usd for t in trades)

        # Compute final capital (for compounding: track through trades)
        final_capital = 1000.0
        for t in trades:
            final_capital += t.pnl_usd

        print(f"{name:<50} {total:>6}  "
              f"{len(tp):>3} ({len(tp)/total*100:>4.1f}%)  "
              f"{len(sl):>3} ({len(sl)/total*100:>4.1f}%)  "
              f"{len(to):>3} ({len(to)/total*100:>4.1f}%)  "
              f"{win_rate:>7.1f}%  "
              f"{avg_pnl:>+7.2f}%  "
              f"${total_pnl:>+7.0f}  "
              f"${final_capital:>7.0f}")

    print("─" * 135)

    # Fee impact analysis — simulate compounding with fees baked in
    print(f"\nFEE IMPACT (final capital after fees, starting $1,000)")
    print("─" * 110)
    print(f"{'Strategy':<50} {'Trades':>6}  {'No fee':>8}  {'0.1%':>8}  {'0.25%':>8}  {'0.5%':>8}  {'1.0%':>8}")
    print("─" * 110)
    for name, trades in results.items():
        if not trades:
            continue
        n_trades = len(trades)
        is_compound = "COMPOUND" in name

        results_by_fee = {}
        for fee_rate in [0, 0.001, 0.0025, 0.005, 0.01]:
            if is_compound:
                # Compound: replay trades, deduct fee from each trade's PnL
                cap = 1000.0
                for t in trades:
                    trade_pnl_pct = t.pnl_pct / 100  # e.g. 0.0114
                    fee_pct = fee_rate * 2  # round trip
                    net_pct = trade_pnl_pct - fee_pct
                    cap *= (1 + net_pct)
                results_by_fee[fee_rate] = cap
            else:
                # Fixed $100: just subtract fee per trade
                total_pnl = sum(t.pnl_usd for t in trades)
                total_fees = n_trades * TRADE_SIZE * fee_rate * 2
                results_by_fee[fee_rate] = 1000 + total_pnl - total_fees

        print(f"{name:<50} {n_trades:>6}  "
              f"${results_by_fee[0]:>7.0f}  ${results_by_fee[0.001]:>7.0f}  "
              f"${results_by_fee[0.0025]:>7.0f}  ${results_by_fee[0.005]:>7.0f}  "
              f"${results_by_fee[0.01]:>7.0f}")
    print("─" * 110)

    # Per-symbol breakdown for each strategy
    all_symbols = set()
    for trades in results.values():
        for t in trades:
            all_symbols.add(t.symbol)

    print(f"\nPER SYMBOL BREAKDOWN")
    for name, trades in results.items():
        if not trades:
            continue
        print(f"\n  {name}:")
        print(f"  {'Symbol':<8} {'Trades':>6} {'TP':>4} {'SL':>4} {'TO':>4} {'Win%':>6} {'PnL$':>8}")
        print(f"  {'─'*48}")
        for sym in sorted(all_symbols):
            st = [t for t in trades if t.symbol == sym]
            if not st:
                continue
            stp = len([t for t in st if t.exit_reason == "TP"])
            ssl = len([t for t in st if t.exit_reason == "SL"])
            sto = len([t for t in st if t.exit_reason in ("TIMEOUT", "OPEN")])
            closed = [t for t in st if t.exit_reason in ("TP", "SL")]
            wr = stp / len(closed) * 100 if closed else 0
            pnl = sum(t.pnl_usd for t in st)
            print(f"  {sym:<8} {len(st):>6} {stp:>4} {ssl:>4} {sto:>4} {wr:>5.1f}% ${pnl:>+7.0f}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RSI + S/R backtest comparison")
    parser.add_argument("--tokens", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", type=str, default=None,
                        help="Start date for signals (YYYY-MM-DD), data fetched from --days before end")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.tokens]

    print(f"\n{'='*70}", file=sys.stderr)
    print(f"  RSI + S/R STRATEGY COMPARISON", file=sys.stderr)
    print(f"  Tokens: {', '.join(symbols)}  |  Days: {args.days}", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)

    # Fetch all data
    all_data = {}
    for sym in symbols:
        print(f"  Fetching {sym}...", file=sys.stderr)
        df = fetch_data(sym, args.days)
        all_data[sym] = df
        print(f"  {sym}: {len(df)} candles ({str(df.index[0])[:10]} to {str(df.index[-1])[:10]})",
              file=sys.stderr)

    start_date = args.start or str(list(all_data.values())[0].index[MIN_HISTORY])[:10]
    end_date = str(list(all_data.values())[0].index[-1])[:10]

    # Run all strategies
    results = {}
    for strat in STRATEGIES:
        print(f"  Running: {strat.name}...", file=sys.stderr)
        trades = run_strategy(strat, all_data, start_date=start_date)
        results[strat.name] = trades
        tp = len([t for t in trades if t.exit_reason == "TP"])
        sl = len([t for t in trades if t.exit_reason == "SL"])
        print(f"    {len(trades)} trades ({tp} TP, {sl} SL)", file=sys.stderr)

    print_comparison(results, start_date, end_date)


if __name__ == "__main__":
    main()
