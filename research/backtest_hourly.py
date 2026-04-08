#!/usr/bin/env python3
"""
backtest_hourly.py — Hourly entry with daily S/R levels.
=========================================================
- S/R levels computed on DAILY candles (structural, recalculated once per day)
- RSI/ADX/DI computed on HOURLY candles (fast signal)
- Entry on hourly candle open (24 opportunities per day)
- TP/SL checked on hourly candles (precise exit)
- Timeout: 10 days (240 hourly candles)

Usage:
    python3 research/backtest_hourly.py
    python3 research/backtest_hourly.py --tokens BTC ETH SOL
"""

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)

from core.sr_analysis import analyze_token
from core.tpsl import compute_tp_sl
from research.rsi_filter import compute_rsi
from research.regime import compute_indicators
from research.fetch_hourly import load_hourly


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
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    hold_hours: int = 0


@dataclass
class StrategyConfig:
    name: str
    rsi_period: int = 10
    rsi_threshold: float = 60
    btc_rsi_floor: float = 50
    use_adx: bool = False
    adx_min: float = 20
    require_di_positive: bool = False
    top_n: int = 0
    max_hold_hours: int = 240  # 10 days
    compound: bool = False


STRATEGIES = [
    # Baseline
    StrategyConfig(name="Hourly: no filter", rsi_threshold=0, btc_rsi_floor=0),

    # RSI on hourly
    StrategyConfig(name="Hourly: RSI>50 + BTC floor", rsi_threshold=50),
    StrategyConfig(name="Hourly: RSI>60 + BTC floor", rsi_threshold=60),

    # ADX on hourly
    StrategyConfig(name="Hourly: RSI>60 + ADX>20 DI+", rsi_threshold=60,
                   use_adx=True, adx_min=20, require_di_positive=True),

    # Top 1
    StrategyConfig(name="Hourly: Top1 RSI>60 + BTC floor", rsi_threshold=60, top_n=1),
    StrategyConfig(name="Hourly: Top1 RSI>60 + ADX>20 DI+", rsi_threshold=60,
                   top_n=1, use_adx=True, adx_min=20, require_di_positive=True),

    # Compound
    StrategyConfig(name="Hourly: COMPOUND Top1 RSI>60+ADX", rsi_threshold=60,
                   top_n=1, compound=True, use_adx=True, adx_min=20, require_di_positive=True),
]

MIN_DAILY_HISTORY = 60   # days of daily data before first signal
MIN_HOURLY_HISTORY = 24  # hours of hourly data for RSI
TRADE_SIZE = 100.0


def resample_to_daily(hourly_df: pd.DataFrame) -> pd.DataFrame:
    """Resample hourly OHLCV to daily."""
    daily = hourly_df.resample("1D").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return daily


def run_strategy(strat: StrategyConfig,
                 hourly_data: Dict[str, pd.DataFrame],
                 daily_data: Dict[str, pd.DataFrame]) -> List[Trade]:
    """Run one strategy on hourly data with daily S/R levels."""
    trades = []
    open_trades: Dict[str, Trade] = {}
    exited_recently: Dict[str, int] = {}  # symbol → hour index when exited
    capital = 1000.0

    # Cache daily S/R: recalculate once per day
    sr_cache: Dict[str, dict] = {}  # symbol → {date: (tp, sl)}

    # Use BTC hourly for floor
    btc_hourly = hourly_data.get("BTC")

    # Get shared hourly index from first token
    first_sym = list(hourly_data.keys())[0]
    hourly_index = hourly_data[first_sym].index
    n = len(hourly_index)

    # Find start: need MIN_DAILY_HISTORY days
    start_hour = MIN_DAILY_HISTORY * 24
    if start_hour >= n:
        return trades

    print(f"    {n} hourly bars, starting at hour {start_hour}", file=sys.stderr)

    pending_entries = []

    for hour in range(start_hour, n):
        current_ts = hourly_index[hour]
        current_date = str(current_ts)[:10]
        current_hour_str = str(current_ts)[:16]

        # ── Execute pending entries ──
        for sym, tp, sl, rsi, adx_val, di_val, trade_size in pending_entries:
            if sym in open_trades:
                if strat.compound:
                    capital += trade_size
                continue
            hdf = hourly_data[sym]
            if hour >= len(hdf):
                if strat.compound:
                    capital += trade_size
                continue
            entry_price = float(hdf["open"].iloc[hour])
            trade = Trade(
                symbol=sym, entry_date=current_hour_str,
                entry_price=entry_price, take_profit=tp, stop_loss=sl,
                rsi_at_entry=rsi, adx_at_entry=adx_val, di_diff_at_entry=di_val,
                size_usd=trade_size,
            )
            open_trades[sym] = trade
        pending_entries = []

        # ── Check open trades for exit ──
        to_remove = []
        for sym, t in open_trades.items():
            hdf = hourly_data[sym]
            if hour >= len(hdf):
                continue

            t.hold_hours += 1
            h_high = float(hdf["high"].iloc[hour])
            h_low = float(hdf["low"].iloc[hour])
            h_close = float(hdf["close"].iloc[hour])

            tp_hit = h_high >= t.take_profit
            sl_hit = h_low <= t.stop_loss

            if tp_hit and sl_hit:
                t.exit_date = current_hour_str
                t.exit_price = t.stop_loss
                t.exit_reason = "SL"
            elif tp_hit:
                t.exit_date = current_hour_str
                t.exit_price = t.take_profit
                t.exit_reason = "TP"
            elif sl_hit:
                t.exit_date = current_hour_str
                t.exit_price = t.stop_loss
                t.exit_reason = "SL"
            elif t.hold_hours >= strat.max_hold_hours:
                t.exit_date = current_hour_str
                t.exit_price = h_close
                t.exit_reason = "TIMEOUT"

            if t.exit_reason:
                t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
                t.pnl_usd = t.size_usd * t.pnl_pct / 100
                if strat.compound:
                    capital += t.size_usd + t.pnl_usd
                trades.append(t)
                to_remove.append(sym)
                exited_recently[sym] = hour

        for sym in to_remove:
            del open_trades[sym]

        # ── Generate signals ──
        if hour >= n - 1:
            continue

        # BTC RSI floor on hourly
        if strat.btc_rsi_floor > 0 and btc_hourly is not None:
            if hour < len(btc_hourly):
                btc_hist = btc_hourly.iloc[:hour + 1]
                if len(btc_hist) >= MIN_HOURLY_HISTORY:
                    btc_rsi = compute_rsi(btc_hist, strat.rsi_period)
                    if btc_rsi < strat.btc_rsi_floor:
                        continue

        # Collect candidates
        candidates = []
        for sym, hdf in hourly_data.items():
            if hour >= len(hdf):
                continue
            if sym in open_trades:
                continue
            # Don't re-enter within 24 hours of exit
            if sym in exited_recently and (hour - exited_recently[sym]) < 24:
                continue

            h_hist = hdf.iloc[:hour + 1]
            if len(h_hist) < MIN_HOURLY_HISTORY:
                continue

            # RSI on hourly
            rsi = compute_rsi(h_hist, strat.rsi_period)
            if rsi <= strat.rsi_threshold:
                continue

            # ADX/DI on hourly
            adx_val, di_val = 0.0, 0.0
            if strat.use_adx:
                try:
                    indicators = compute_indicators(h_hist)
                    if "error" in indicators:
                        continue
                    adx_val = indicators.get("adx", 0)
                    plus_di = indicators.get("plus_di", 0)
                    minus_di = indicators.get("minus_di", 0)
                    di_val = plus_di - minus_di
                    if adx_val < strat.adx_min:
                        continue
                    if strat.require_di_positive and di_val <= 0:
                        continue
                except Exception:
                    continue

            # Get daily S/R (cached per day)
            cache_key = f"{sym}_{current_date}"
            if cache_key not in sr_cache:
                ddf = daily_data.get(sym)
                if ddf is None or len(ddf) < MIN_DAILY_HISTORY:
                    continue
                # Use daily data up to yesterday (no look-ahead on today's daily close)
                daily_up_to = ddf[ddf.index < current_date]
                if len(daily_up_to) < MIN_DAILY_HISTORY:
                    continue
                try:
                    analysis = analyze_token(sym, daily_up_to)
                    result = compute_tp_sl(analysis)
                    if result and result.get("take_profit") and result.get("stop_loss"):
                        sr_cache[cache_key] = {
                            "tp": result["take_profit"],
                            "sl": result["stop_loss"],
                        }
                    else:
                        sr_cache[cache_key] = None
                except Exception:
                    sr_cache[cache_key] = None

            sr = sr_cache.get(cache_key)
            if sr is None:
                continue

            candidates.append((sym, rsi, adx_val, di_val, sr["tp"], sr["sl"]))

        # Top-N filter
        if strat.top_n > 0:
            open_count = len(open_trades)
            slots = max(0, strat.top_n - open_count)
            if slots == 0:
                continue
            candidates.sort(key=lambda x: x[1], reverse=True)
            candidates = candidates[:slots]

        # Queue entries
        for sym, rsi, adx_val, di_val, tp, sl in candidates:
            if strat.compound:
                trade_size = capital
                capital = 0
            else:
                trade_size = TRADE_SIZE
            pending_entries.append((sym, tp, sl, rsi, adx_val, di_val, trade_size))

    # Close remaining
    for sym, t in open_trades.items():
        hdf = hourly_data[sym]
        t.exit_date = str(hdf.index[-1])[:16]
        t.exit_price = float(hdf["close"].iloc[-1])
        t.exit_reason = "OPEN"
        t.pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
        t.pnl_usd = t.size_usd * t.pnl_pct / 100
        trades.append(t)

    return trades


def print_comparison(results: Dict[str, List[Trade]], start_date: str, end_date: str):
    print(f"\nHOURLY STRATEGY COMPARISON  ({start_date} -> {end_date})")
    print("─" * 140)
    print(f"{'Strategy':<50} {'Trades':>6}  {'TP hit':>12}  {'SL hit':>12}  "
          f"{'Timeout':>12}  {'Win rate':>8}  {'Avg P&L':>8}  {'Total$':>8}  {'Final$':>8}")
    print("─" * 140)

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
        final = 1000 + total_pnl
        avg_hours = np.mean([t.hold_hours for t in trades])

        print(f"{name:<50} {total:>6}  "
              f"{len(tp):>3} ({len(tp)/total*100:>4.1f}%)  "
              f"{len(sl):>3} ({len(sl)/total*100:>4.1f}%)  "
              f"{len(to):>3} ({len(to)/total*100:>4.1f}%)  "
              f"{win_rate:>7.1f}%  "
              f"{avg_pnl:>+7.2f}%  "
              f"${total_pnl:>+7.0f}  "
              f"${final:>7.0f}")

    print("─" * 140)


def main():
    parser = argparse.ArgumentParser(description="Hourly entry backtest with daily S/R")
    parser.add_argument("--tokens", nargs="+",
                        default=["BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE",
                                 "AVAX", "TAO", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI"])
    args = parser.parse_args()

    symbols = [s.upper() for s in args.tokens]

    print(f"\n{'='*70}", file=sys.stderr)
    print(f"  HOURLY BACKTEST — Daily S/R + Hourly Signals", file=sys.stderr)
    print(f"  Tokens: {len(symbols)}", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)

    # Load data
    hourly_data = {}
    daily_data = {}
    for sym in symbols:
        hdf = load_hourly(sym)
        if len(hdf) < MIN_DAILY_HISTORY * 24:
            print(f"  {sym}: insufficient hourly data ({len(hdf)} bars), skipping", file=sys.stderr)
            continue
        hourly_data[sym] = hdf
        daily_data[sym] = resample_to_daily(hdf)
        print(f"  {sym}: {len(hdf)} hourly -> {len(daily_data[sym])} daily", file=sys.stderr)

    if not hourly_data:
        print("No data available. Run fetch_hourly.py first.", file=sys.stderr)
        return

    start_date = str(list(hourly_data.values())[0].index[MIN_DAILY_HISTORY * 24])[:10]
    end_date = str(list(hourly_data.values())[0].index[-1])[:10]

    results = {}
    for strat in STRATEGIES:
        print(f"  Running: {strat.name}...", file=sys.stderr)
        strat_trades = run_strategy(strat, hourly_data, daily_data)
        results[strat.name] = strat_trades
        tp_count = len([t for t in strat_trades if t.exit_reason == "TP"])
        sl_count = len([t for t in strat_trades if t.exit_reason == "SL"])
        print(f"    {len(strat_trades)} trades ({tp_count} TP, {sl_count} SL)", file=sys.stderr)

    print_comparison(results, start_date, end_date)


if __name__ == "__main__":
    main()
