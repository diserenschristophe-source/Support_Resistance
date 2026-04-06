#!/usr/bin/env python3
"""
backtest_model.py — Signal quality backtest for S/R model.
===========================================================

Records EVERY qualifying S/R signal across all tokens.
No TP exit — tracks how far price goes (R1/R2/R3) before SL or timeout.
All filter states recorded as boolean columns for post-hoc analysis.

Output: CSV trade log that can be sliced by any filter combination.

Usage:
    python3 backtest_model.py
    python3 backtest_model.py --tokens BTC ETH SOL --days 1000
    python3 backtest_model.py --all --days 730
"""

import argparse
import csv
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict

import numpy as np
import pandas as pd

from core.fetcher import fetch_data
from core.sr_analysis import analyze_token
from core.tpsl import compute_tp_sl
from core.models import compute_atr
from core.filters import (
    compute_rsi, compute_adx_di, compute_mt_regime,
    bollinger_pctb, relative_volume,
)
from core.config import ALL, TOP_3_SET, SELECTED_SET, TOP_20_SET, get_tier

ALL_TOKENS = ALL


# ─────────────────────────────────────────────────────────────
# Trade Record
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    tier: str
    entry_date: str
    entry_price: float
    stop_loss: float
    tp_level: float           # TP from tpsl model (R1)
    raw_rr: float
    # R-levels from resistance zones (nearest to farthest)
    r1: float = 0.0
    r2: float = 0.0
    r3: float = 0.0
    # Tracking — filled during the trade
    r1_hit: bool = False
    r1_days: int = 0
    r2_hit: bool = False
    r2_days: int = 0
    r3_hit: bool = False
    r3_days: int = 0
    sl_hit: bool = False
    sl_days: int = 0
    tp_hit_before_sl: bool = False   # would TP/SL rules have won?
    r1_hit_before_sl: bool = False   # did R1 get hit before SL?
    r2_hit_before_sl: bool = False   # did R2 get hit before SL?
    exit_date: str = ""
    exit_reason: str = ""            # "SL" or "TIMEOUT"
    exit_price: float = 0.0         # actual exit price
    hold_days: int = 0
    pnl_r1: float = 0.0             # PnL ($) if TP was at R1
    pnl_r2: float = 0.0             # PnL ($) if TP was at R2
    # Checkpoint close prices for timeout simulation
    close_d5: float = 0.0
    close_d7: float = 0.0
    close_d10: float = 0.0
    close_d14: float = 0.0
    close_d21: float = 0.0
    # Filter flags (all recorded regardless of pass/fail)
    f_btc_rsi_floor: bool = False
    f_token_rsi_momentum: bool = False
    f_rr_min: bool = False
    f_di_bullish: bool = False
    f_adx_trend: bool = False
    f_mt_regime: bool = False
    f_bollinger: bool = False
    f_rvol: bool = False
    f_rsi_cap: bool = False
    # Raw indicator values for deeper analysis
    btc_rsi: float = 0.0
    token_rsi: float = 0.0
    adx: float = 0.0
    di_plus: float = 0.0
    di_minus: float = 0.0
    mt_regime: str = ""


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

MIN_HISTORY = 60       # minimum candles before first signal
MAX_HOLD_DAYS = 30     # timeout
TRADE_SIZE = 100.0     # USD per trade for PnL calculation
OUTPUT_FILE = "backtest_results.csv"


# ─────────────────────────────────────────────────────────────
# Filter Evaluation
# ─────────────────────────────────────────────────────────────

def evaluate_filters(df: pd.DataFrame, btc_df: pd.DataFrame,
                     raw_rr: float) -> Dict:
    """Evaluate all filters and return flags + raw values."""
    # RSI — guard against empty BTC slice (date range mismatch)
    if len(btc_df) < 11:
        btc_rsi = 0.0
    else:
        btc_rsi = compute_rsi(btc_df["close"], 10)
    token_rsi = compute_rsi(df["close"], 10)

    # ADX / DI
    adx_di = compute_adx_di(df, 14)

    # MT regime
    mt_state, _ = compute_mt_regime(df["close"])

    # Bollinger %B
    boll_pass, _ = bollinger_pctb(df)

    # Relative volume
    rvol_pass, _ = relative_volume(df)

    return {
        "f_btc_rsi_floor": btc_rsi >= 50,
        "f_token_rsi_momentum": token_rsi > 60,
        "f_rr_min": raw_rr >= 1.2,
        "f_di_bullish": adx_di["di_plus"] > adx_di["di_minus"],
        "f_adx_trend": adx_di["adx"] > 20,
        "f_mt_regime": mt_state != "D",
        "f_bollinger": boll_pass,
        "f_rvol": rvol_pass,
        "f_rsi_cap": token_rsi <= 80,
        # Raw values
        "btc_rsi": round(btc_rsi, 2),
        "token_rsi": round(token_rsi, 2),
        "adx": round(adx_di["adx"], 2),
        "di_plus": round(adx_di["di_plus"], 2),
        "di_minus": round(adx_di["di_minus"], 2),
        "mt_regime": mt_state,
    }


# ─────────────────────────────────────────────────────────────
# PnL Computation
# ─────────────────────────────────────────────────────────────

def _compute_pnl(t: Trade):
    """Compute PnL for both R1 and R2 target scenarios after trade closes."""
    # Determine if R1/R2 was hit before SL
    t.r1_hit_before_sl = t.r1_hit and (not t.sl_hit or t.r1_days <= t.sl_days)
    t.r2_hit_before_sl = t.r2_hit and (not t.sl_hit or t.r2_days <= t.sl_days)

    # PnL if targeting R1
    if t.r1_hit_before_sl and t.r1 > 0:
        t.pnl_r1 = round(TRADE_SIZE * (t.r1 - t.entry_price) / t.entry_price, 2)
    elif t.sl_hit:
        t.pnl_r1 = round(TRADE_SIZE * (t.stop_loss - t.entry_price) / t.entry_price, 2)
    else:  # timeout, no R1 hit, no SL hit
        t.pnl_r1 = round(TRADE_SIZE * (t.exit_price - t.entry_price) / t.entry_price, 2)

    # PnL if targeting R2
    if t.r2_hit_before_sl and t.r2 > 0:
        t.pnl_r2 = round(TRADE_SIZE * (t.r2 - t.entry_price) / t.entry_price, 2)
    elif t.sl_hit:
        t.pnl_r2 = round(TRADE_SIZE * (t.stop_loss - t.entry_price) / t.entry_price, 2)
    else:  # timeout
        t.pnl_r2 = round(TRADE_SIZE * (t.exit_price - t.entry_price) / t.entry_price, 2)


# ─────────────────────────────────────────────────────────────
# Backtest Engine
# ─────────────────────────────────────────────────────────────

def run_backtest(symbol: str, df: pd.DataFrame,
                 btc_df: pd.DataFrame) -> List[Trade]:
    """Walk through daily candles, generate signals, track trades."""
    n = len(df)
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None

    print(f"  [{symbol}] {n} candles, backtesting from day {MIN_HISTORY}...",
          file=sys.stderr)

    for day in range(MIN_HISTORY, n):
        current_high = float(df["high"].iloc[day])
        current_low = float(df["low"].iloc[day])
        current_close = float(df["close"].iloc[day])
        current_open = float(df["open"].iloc[day])
        current_date = str(df.index[day])[:10]
        just_closed = False

        # ── Check open trade for R-level hits and exit ──────────
        if open_trade is not None:
            open_trade.hold_days += 1
            t = open_trade

            # Record checkpoint close prices
            if t.hold_days == 5:  t.close_d5 = current_close
            if t.hold_days == 7:  t.close_d7 = current_close
            if t.hold_days == 10: t.close_d10 = current_close
            if t.hold_days == 14: t.close_d14 = current_close
            if t.hold_days == 21: t.close_d21 = current_close

            # Track R-level hits (even if SL also hits this day)
            if not t.r1_hit and t.r1 > 0 and current_high >= t.r1:
                t.r1_hit = True
                t.r1_days = t.hold_days
            if not t.r2_hit and t.r2 > 0 and current_high >= t.r2:
                t.r2_hit = True
                t.r2_days = t.hold_days
            if not t.r3_hit and t.r3 > 0 and current_high >= t.r3:
                t.r3_hit = True
                t.r3_days = t.hold_days

            # Track TP hit (for win rate calculation)
            if not t.tp_hit_before_sl and not t.sl_hit:
                if current_high >= t.tp_level:
                    t.tp_hit_before_sl = True

            # Check SL
            sl_hit_today = current_low <= t.stop_loss
            if sl_hit_today and not t.sl_hit:
                t.sl_hit = True
                t.sl_days = t.hold_days

            # Exit conditions: SL or timeout
            if sl_hit_today:
                t.exit_date = current_date
                t.exit_reason = "SL"
                t.exit_price = t.stop_loss
                _compute_pnl(t)
                trades.append(t)
                open_trade = None
                just_closed = True
            elif t.hold_days >= MAX_HOLD_DAYS:
                t.exit_date = current_date
                t.exit_reason = "TIMEOUT"
                t.exit_price = current_close
                _compute_pnl(t)
                trades.append(t)
                open_trade = None
                just_closed = True

        # ── Generate signal (no look-ahead) ─────────────────────
        # Skip last day (no next-day open to enter on)
        if day >= n - 1:
            continue

        # No same-day close + reopen on the same token
        if just_closed:
            continue

        # Skip if we already have an open trade on this symbol
        if open_trade is not None:
            continue

        history = df.iloc[:day + 1]
        if len(history) < MIN_HISTORY:
            continue

        # Align BTC history to same date range
        btc_history = btc_df.loc[:df.index[day]]

        try:
            analysis = analyze_token(symbol, history)
            result = compute_tp_sl(analysis)
        except Exception:
            continue

        if result is None:
            continue
        if not result.get("take_profit") or not result.get("stop_loss"):
            continue

        tp = result["take_profit"]
        sl = result["stop_loss"]
        rr = result.get("raw_rr", 0.0)
        current_close = float(df["close"].iloc[day])

        # Enforce primary tpsl rule: both TP and SL must be >= 1 ATR from price
        # Reject fallback results where SL/TP are too close (causes garbage R:R)
        atr = compute_atr(history)
        if (tp - current_close) < atr or (current_close - sl) < atr:
            continue

        # ── HARD GATE: MT regime — skip D (downtrend) ──
        mt_state, _ = compute_mt_regime(history["close"])
        if mt_state == "D":
            continue

        # Entry at NEXT day's open (moved up — needed for R-level cascade)
        entry_price = float(df["open"].iloc[day + 1])
        entry_date = str(df.index[day + 1])[:10]

        # Skip if entry price <= SL (tpsl.py line 144-145: potential_loss <= 0)
        if entry_price <= sl:
            continue

        # Apply tpsl cascade to R-levels: skip resistances within 1 ATR of
        # entry price, exactly like tpsl.py lines 95-99.
        # When nearest resistance is < 1 ATR away, cascade to next — what was
        # R2 becomes R1, R3 becomes R2, etc.
        resistance_zones = result.get("resistance", [])
        r_levels = [z["key_level"] for z in resistance_zones
                    if z["key_level"] > entry_price
                    and (z["key_level"] - entry_price) >= atr]
        r_levels.sort()
        r1 = r_levels[0] if len(r_levels) >= 1 else 0.0
        r2 = r_levels[1] if len(r_levels) >= 2 else 0.0
        r3 = r_levels[2] if len(r_levels) >= 3 else 0.0

        # Skip if no valid R1 after cascade (no tradeable target)
        if r1 == 0.0:
            continue

        # Evaluate all filters (regime already gated, but still record the flag)
        filters = evaluate_filters(history, btc_history, rr)

        trade = Trade(
            symbol=symbol,
            tier=get_tier(symbol),
            entry_date=entry_date,
            entry_price=entry_price,
            stop_loss=sl,
            tp_level=tp,
            raw_rr=round(rr, 3),
            r1=r1,
            r2=r2,
            r3=r3,
            **filters,
        )
        open_trade = trade

        if len(trades) % 50 == 0 and trades:
            print(f"    day {day}/{n} — {len(trades)} closed",
                  file=sys.stderr)

    # Close remaining open trade at last available data
    if open_trade is not None:
        last_date = str(df.index[-1])[:10]
        last_close = float(df["close"].iloc[-1])
        open_trade.exit_date = last_date
        open_trade.exit_reason = "TIMEOUT"
        open_trade.exit_price = last_close
        open_trade.hold_days = max(open_trade.hold_days, 1)
        _compute_pnl(open_trade)
        trades.append(open_trade)

    return trades


# ─────────────────────────────────────────────────────────────
# CSV Output
# ─────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "symbol", "tier", "entry_date", "entry_price",
    "stop_loss", "tp_level", "raw_rr",
    "r1", "r2", "r3",
    "r1_hit", "r1_days", "r2_hit", "r2_days", "r3_hit", "r3_days",
    "sl_hit", "sl_days", "tp_hit_before_sl",
    "r1_hit_before_sl", "r2_hit_before_sl",
    "exit_date", "exit_reason", "exit_price", "hold_days",
    "pnl_r1", "pnl_r2",
    "close_d5", "close_d7", "close_d10", "close_d14", "close_d21",
    "f_btc_rsi_floor", "f_token_rsi_momentum", "f_rr_min",
    "f_di_bullish", "f_adx_trend", "f_mt_regime",
    "f_bollinger", "f_rvol", "f_rsi_cap",
    "btc_rsi", "token_rsi", "adx", "di_plus", "di_minus", "mt_regime",
]


def write_csv(trades: List[Trade], output_path: str):
    """Write trade log to CSV."""
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for t in trades:
            row = asdict(t)
            writer.writerow({k: row[k] for k in CSV_COLUMNS})

    print(f"\n  Wrote {len(trades)} trades to {output_path}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────
# Summary Stats (printed to stderr)
# ─────────────────────────────────────────────────────────────

def print_summary(trades: List[Trade]):
    """Print quick summary stats to stderr."""
    if not trades:
        print("\n  No trades generated.", file=sys.stderr)
        return

    total = len(trades)
    sl_exits = sum(1 for t in trades if t.exit_reason == "SL")
    timeouts = sum(1 for t in trades if t.exit_reason == "TIMEOUT")

    r1_hits = sum(1 for t in trades if t.r1_hit)
    r2_hits = sum(1 for t in trades if t.r2_hit)
    r3_hits = sum(1 for t in trades if t.r3_hit)
    tp_wins = sum(1 for t in trades if t.tp_hit_before_sl)

    avg_hold = np.mean([t.hold_days for t in trades])
    avg_r1_days = np.mean([t.r1_days for t in trades if t.r1_hit]) if r1_hits else 0
    avg_r2_days = np.mean([t.r2_days for t in trades if t.r2_hit]) if r2_hits else 0
    avg_r3_days = np.mean([t.r3_days for t in trades if t.r3_hit]) if r3_hits else 0

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  BACKTEST SUMMARY", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  Total trades:       {total}", file=sys.stderr)
    print(f"  SL exits:           {sl_exits} ({sl_exits/total*100:.1f}%)", file=sys.stderr)
    print(f"  Timeouts:           {timeouts} ({timeouts/total*100:.1f}%)", file=sys.stderr)
    print(f"  Avg hold days:      {avg_hold:.1f}", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"  TP win rate:        {tp_wins}/{total} = {tp_wins/total*100:.1f}%", file=sys.stderr)
    print(f"  R1 hit rate:        {r1_hits}/{total} = {r1_hits/total*100:.1f}%  (avg {avg_r1_days:.1f}d)", file=sys.stderr)
    print(f"  R2 hit rate:        {r2_hits}/{total} = {r2_hits/total*100:.1f}%  (avg {avg_r2_days:.1f}d)", file=sys.stderr)
    print(f"  R3 hit rate:        {r3_hits}/{total} = {r3_hits/total*100:.1f}%  (avg {avg_r3_days:.1f}d)", file=sys.stderr)

    # Per-tier breakdown
    print(f"\n  PER TIER", file=sys.stderr)
    print(f"  {'TIER':<10} {'TRADES':>6} {'SL%':>6} {'TP_WIN%':>8} {'R1%':>6} {'R2%':>6} {'R3%':>6}", file=sys.stderr)
    print(f"  {'─'*50}", file=sys.stderr)
    for tier in ["top_3", "selected", "top_20", "all"]:
        ut = [t for t in trades if t.tier == tier]
        if not ut:
            continue
        n = len(ut)
        print(f"  {tier:<10} {n:>6} "
              f"{sum(1 for t in ut if t.exit_reason=='SL')/n*100:>5.1f}% "
              f"{sum(1 for t in ut if t.tp_hit_before_sl)/n*100:>7.1f}% "
              f"{sum(1 for t in ut if t.r1_hit)/n*100:>5.1f}% "
              f"{sum(1 for t in ut if t.r2_hit)/n*100:>5.1f}% "
              f"{sum(1 for t in ut if t.r3_hit)/n*100:>5.1f}%",
              file=sys.stderr)

    # PnL summary
    def _pnl_stats(trades_list, pnl_field):
        pnls = [getattr(t, pnl_field) for t in trades_list]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_pnl = sum(pnls)
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        wl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        expectancy = total_pnl / len(pnls) if pnls else 0
        # Max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        return total_pnl, avg_win, avg_loss, wl_ratio, max_dd, expectancy, len(wins), len(losses)

    for label, pnl_field in [("TP1 (target R1)", "pnl_r1"), ("TP2 (target R2)", "pnl_r2")]:
        print(f"\n  PNL — {label} (per ${TRADE_SIZE:.0f} trade)", file=sys.stderr)
        print(f"  {'─'*55}", file=sys.stderr)
        print(f"  {'TIER':<10} {'TOTAL':>8} {'AVG_W':>7} {'AVG_L':>7} {'W/L':>5} {'MAX_DD':>8} {'EXP':>7} {'W':>4} {'L':>4}", file=sys.stderr)
        for tier in ["top_3", "selected", "top_20", "all", "TOTAL"]:
            if tier == "TOTAL":
                ut = trades
            else:
                ut = [t for t in trades if t.tier == tier]
            if not ut:
                continue
            total_pnl, avg_win, avg_loss, wl_ratio, max_dd, expectancy, nw, nl = _pnl_stats(ut, pnl_field)
            print(f"  {tier:<10} ${total_pnl:>+7.0f} ${avg_win:>5.1f} ${avg_loss:>5.1f} {wl_ratio:>5.2f} ${max_dd:>7.0f} ${expectancy:>+5.2f} {nw:>4} {nl:>4}",
                  file=sys.stderr)

    print(f"\n{'='*60}\n", file=sys.stderr)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="S/R Signal Quality Backtest")
    parser.add_argument("--tokens", nargs="+", default=None,
                        help="Tokens to backtest (default: all)")
    parser.add_argument("--all", action="store_true",
                        help="Backtest all 51 tokens")
    parser.add_argument("--days", type=int, default=730,
                        help="Days of history (default: 730)")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE,
                        help=f"Output CSV path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    if args.tokens:
        symbols = [s.upper() for s in args.tokens]
    elif args.all:
        symbols = ALL_TOKENS
    else:
        symbols = ALL_TOKENS

    all_trades: List[Trade] = []

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  S/R SIGNAL QUALITY BACKTEST", file=sys.stderr)
    print(f"  Tokens: {len(symbols)}  |  Days: {args.days}", file=sys.stderr)
    print(f"  Max hold: {MAX_HOLD_DAYS}d  |  No TP exit", file=sys.stderr)
    print(f"  Output: {args.output}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    # Fetch BTC first (needed for btc_rsi filter on all tokens)
    print(f"\n  Fetching BTC (reference for RSI filter)...", file=sys.stderr)
    btc_df = fetch_data("BTC", args.days)
    print(f"  BTC: {len(btc_df)} candles", file=sys.stderr)

    for i, symbol in enumerate(symbols):
        print(f"\n  [{i+1}/{len(symbols)}] {symbol} ({args.days}d)...",
              file=sys.stderr)

        # Reuse BTC data already fetched
        if symbol == "BTC":
            df = btc_df
        else:
            try:
                df = fetch_data(symbol, args.days)
            except Exception as e:
                print(f"  [{symbol}] FETCH FAILED: {e}", file=sys.stderr)
                continue

        if len(df) < MIN_HISTORY:
            print(f"  [{symbol}] Only {len(df)} candles, need {MIN_HISTORY}. Skipping.",
                  file=sys.stderr)
            continue

        print(f"  {symbol}: {len(df)} candles ({str(df.index[0])[:10]} to {str(df.index[-1])[:10]})",
              file=sys.stderr)

        trades = run_backtest(symbol, df, btc_df)
        all_trades.extend(trades)
        print(f"  [{symbol}] {len(trades)} trades", file=sys.stderr)

    # Sort by entry date
    all_trades.sort(key=lambda t: t.entry_date)

    # Output
    write_csv(all_trades, args.output)
    print_summary(all_trades)


if __name__ == "__main__":
    main()
