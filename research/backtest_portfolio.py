#!/usr/bin/env python3
"""
backtest_portfolio.py — Backtest the SR Ranking Model as a Portfolio Strategy
==============================================================================

Simulates running the model daily over a historical period:
  - Each day: run the ranking model on data available up to that day
  - Place limit orders at model's entry prices (valid 1 day)
  - Exit only via TP, SL, or 7-day timeout
  - Position sizing: risk-first, confidence-weighted
  - Max 5 open positions, max 30% per position

Usage:
    python3 backtest_portfolio.py --days 90 --top 5
    python3 backtest_portfolio.py --days 60 --top 5 --tokens 20
    python3 backtest_portfolio.py --days 90 --top 5 --csv backtest_results.csv

Dependencies: same as sr-analyzer
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ── Path setup: add parent dir (core modules) to sys.path ────
_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)

from core.sr_analysis import analyze_token
from core.config import BINANCE_SYMBOL_MAP, FALLBACK_TOP_50
from research.regime import compute_regime
from core.tpsl import compute_tp_sl
from core.models import fmt_price
from core.fetcher import (
    get_top_tokens, load_from_cache,
    get_cache_path, get_cache_status
)


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

STARTING_CAPITAL = 1000.0
MAX_OPEN_POSITIONS = 5
MAX_POSITION_PCT = 0.30        # max 30% of equity in one trade
MAX_HOLD_DAYS = 7              # close after 7 days regardless

# Risk per trade by confidence level
RISK_HIGH = 0.025              # 2.5% of equity for high confidence (>0.75)
RISK_MED = 0.020               # 2.0% for medium (0.50-0.75)
RISK_LOW = 0.010               # 1.0% for low (<0.50)

# ─────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_date: str
    tp_price: float
    sl_price: float
    size_usd: float            # $ allocated
    risk_usd: float            # $ at risk
    confidence: float
    composite_score: float
    regime: str
    exit_price: float = 0.0
    exit_date: str = ""
    exit_reason: str = ""      # "TP", "SL", "TIMEOUT"
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    hold_days: int = 0


@dataclass
class DailySnapshot:
    date: str
    equity: float
    cash: float
    open_positions: int
    signals_generated: int
    orders_placed: int
    orders_filled: int
    positions_closed: int
    daily_pnl: float = 0.0


# ─────────────────────────────────────────────────────────────
# Backtest Engine
# ─────────────────────────────────────────────────────────────

class Backtester:

    def __init__(self, data_dir: str, tokens: List[str], top_n: int = 5):
        self.data_dir = data_dir
        self.tokens = tokens
        self.top_n = top_n

        # Portfolio state
        self.equity = STARTING_CAPITAL
        self.cash = STARTING_CAPITAL
        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []
        self.daily_snapshots: List[DailySnapshot] = []

        # Load all data upfront
        self.all_data: Dict[str, pd.DataFrame] = {}
        for sym in tokens:
            df = load_from_cache(sym, data_dir)
            if df is not None and len(df) >= 60:
                self.all_data[sym] = df

        print(f"  Loaded {len(self.all_data)} tokens with sufficient data", file=sys.stderr)

    def _get_risk_pct(self, confidence: float, regime: str = "TRANSITION") -> float:
        """
        Risk per trade based on regime × Setup Quality (confidence).

        Simplified 3-state model:
          BULL:       momentum confirms longs → full risk
          TRANSITION: everything else (RANGE, NEUTRAL, old TRANSITION) → reduced risk
          BEAR:       momentum against longs → skip unless exceptional
        """
        regime = regime.upper() if regime else "TRANSITION"

        # Consolidate: RANGE, NEUTRAL, UNDEFINED → TRANSITION
        if regime not in ("BULL", "BEAR"):
            regime = "TRANSITION"

        if regime == "BULL":
            if confidence > 0.75:
                return 0.030       # 3.0%
            elif confidence > 0.50:
                return 0.025       # 2.5%
            else:
                return 0.015       # 1.5% → BULL forgives low confidence
        elif regime == "BEAR":
            if confidence > 0.75:
                return 0.010       # 1.0% → minimal, counter-trend
            else:
                return 0            # SKIP → don't fight the trend
        else:  # TRANSITION (absorbs RANGE, NEUTRAL, unknown)
            if confidence > 0.75:
                return 0.025       # 2.5%
            elif confidence > 0.50:
                return 0.015       # 1.5%
            else:
                return 0            # SKIP → no edge

    def _compute_position_size(self, confidence: float, stop_pct: float, regime: str = "RANGE") -> float:
        """
        Risk-first position sizing.
        Size = (equity × risk%) / stop%
        Capped at MAX_POSITION_PCT of equity.
        Returns 0 if regime/confidence filter says SKIP.
        """
        if stop_pct <= 0:
            return 0

        risk_pct = self._get_risk_pct(confidence, regime)
        if risk_pct <= 0:
            return 0  # regime/confidence filter: don't enter

        risk_usd = self.equity * risk_pct
        size = risk_usd / (stop_pct / 100)

        # Cap at max allocation
        max_size = self.equity * MAX_POSITION_PCT
        return min(size, max_size)

    def _get_data_up_to(self, symbol: str, date) -> Optional[pd.DataFrame]:
        """Get OHLCV data up to (and including) a specific date. No future leakage."""
        if symbol not in self.all_data:
            return None
        df = self.all_data[symbol]
        mask = df.index.date <= date
        subset = df[mask]
        if len(subset) < 60:  # need minimum data for S/R analysis
            return None
        return subset

    def _get_candle(self, symbol: str, date) -> Optional[dict]:
        """Get the OHLCV for a specific date."""
        if symbol not in self.all_data:
            return None
        df = self.all_data[symbol]
        mask = df.index.date == date
        candles = df[mask]
        if len(candles) == 0:
            return None
        row = candles.iloc[-1]
        return {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        }

    def _run_model_for_date(self, date) -> List[dict]:
        """
        Run the ranking model using only data available up to `date`.
        Returns list of scored tokens, sorted by composite score.
        """
        scored = []
        for symbol in self.all_data.keys():
            try:
                df = self._get_data_up_to(symbol, date)
                if df is None:
                    continue

                analysis = analyze_token(symbol, df)
                regime_data = compute_regime(df)
                token_score = compute_tp_sl(analysis)

                if token_score and token_score.get("qualified"):
                    # Add fields expected by the backtester
                    token_score["entry"] = token_score["price"]
                    token_score["confidence"] = token_score.get("raw_rr", 0) / 5.0  # normalize RR to 0-1 confidence
                    token_score["confidence"] = min(1.0, max(0.0, token_score["confidence"]))
                    token_score["composite_score"] = token_score.get("raw_rr", 0)
                    token_score["regime"] = regime_data if isinstance(regime_data, dict) else {"regime": "TRANSITION"}
                    scored.append(token_score)
            except Exception:
                continue

        scored.sort(key=lambda s: s["composite_score"], reverse=True)
        return scored[:self.top_n]

    def _check_exits(self, date) -> int:
        """
        Check all open positions against today's candle.
        Close positions that hit TP, SL, or timeout.
        Returns number of positions closed.
        """
        closed_count = 0
        still_open = []

        for pos in self.open_positions:
            candle = self._get_candle(pos.symbol, date)
            date_str = str(date)

            # Count hold days
            entry_date = datetime.strptime(pos.entry_date, "%Y-%m-%d").date()
            pos.hold_days = (date - entry_date).days

            # Check timeout first (7 days)
            if pos.hold_days >= MAX_HOLD_DAYS:
                if candle:
                    pos.exit_price = candle["close"]
                else:
                    pos.exit_price = pos.entry_price  # fallback
                pos.exit_date = date_str
                pos.exit_reason = "TIMEOUT"
                pos.pnl_usd = (pos.exit_price - pos.entry_price) / pos.entry_price * pos.size_usd
                pos.pnl_pct = (pos.exit_price - pos.entry_price) / pos.entry_price * 100
                self.cash += pos.size_usd + pos.pnl_usd
                self.closed_positions.append(pos)
                closed_count += 1
                continue

            if not candle:
                still_open.append(pos)
                continue

            # Check SL hit (low <= SL during the day)
            if candle["low"] <= pos.sl_price:
                pos.exit_price = pos.sl_price
                pos.exit_date = date_str
                pos.exit_reason = "SL"
                pos.pnl_usd = (pos.exit_price - pos.entry_price) / pos.entry_price * pos.size_usd
                pos.pnl_pct = (pos.exit_price - pos.entry_price) / pos.entry_price * 100
                self.cash += pos.size_usd + pos.pnl_usd
                self.closed_positions.append(pos)
                closed_count += 1
                continue

            # Check TP hit (high >= TP during the day)
            if candle["high"] >= pos.tp_price:
                pos.exit_price = pos.tp_price
                pos.exit_date = date_str
                pos.exit_reason = "TP"
                pos.pnl_usd = (pos.exit_price - pos.entry_price) / pos.entry_price * pos.size_usd
                pos.pnl_pct = (pos.exit_price - pos.entry_price) / pos.entry_price * 100
                self.cash += pos.size_usd + pos.pnl_usd
                self.closed_positions.append(pos)
                closed_count += 1
                continue

            # Neither hit → hold
            still_open.append(pos)

        self.open_positions = still_open
        return closed_count

    def _place_orders(self, signals: List[dict], date) -> int:
        """
        Enter positions at market for qualifying signals.
        Entry = current close price (already set by compute_token_score).
        No limit orders — we evaluate and enter same day.
        Returns number of positions opened.
        """
        opened = 0

        # Symbols already in a position
        held_symbols = {pos.symbol for pos in self.open_positions}

        for sig in signals:
            sym = sig["symbol"]
            if sym in held_symbols:
                continue  # already holding

            if len(self.open_positions) + opened >= MAX_OPEN_POSITIONS:
                break  # no room

            entry = sig["entry"]  # = current price
            tp = sig["take_profit"]
            sl = sig["stop_loss"]
            confidence = sig["confidence"]
            stop_pct = sig["potential_loss_pct"]
            regime = sig.get("regime", {})
            regime_str = regime.get("regime", "TRANSITION") if isinstance(regime, dict) else "TRANSITION"

            # Normalize to 3-state model: BULL, BEAR, TRANSITION
            regime_str = regime_str.upper() if regime_str else "TRANSITION"
            if regime_str not in ("BULL", "BEAR"):
                regime_str = "TRANSITION"

            # TP must be above entry, SL must be below entry
            if tp <= entry or sl >= entry:
                continue

            # Regime × confidence filter is inside _compute_position_size
            # Returns 0 if the combination should be skipped
            size = self._compute_position_size(confidence, stop_pct, regime_str)
            if size < 10:
                continue  # filtered out or too small

            if size > self.cash:
                size = self.cash
                if size < 10:
                    continue

            risk_pct = self._get_risk_pct(confidence, regime_str)
            risk_usd = self.equity * risk_pct

            self.cash -= size

            pos = Position(
                symbol=sym,
                entry_price=entry,
                entry_date=str(date),
                tp_price=tp,
                sl_price=sl,
                size_usd=size,
                risk_usd=risk_usd,
                confidence=confidence,
                composite_score=sig["composite_score"],
                regime=regime_str,
            )
            self.open_positions.append(pos)
            opened += 1

        return opened

    def run(self, start_date, end_date):
        """
        Run the backtest from start_date to end_date.
        """
        current = start_date
        day_count = 0

        print(f"\n{'=' * 70}", file=sys.stderr)
        print(f"  BACKTEST: {start_date} → {end_date}", file=sys.stderr)
        print(f"  Capital: ${STARTING_CAPITAL:,.0f} | Max positions: {MAX_OPEN_POSITIONS}"
              f" | Timeout: {MAX_HOLD_DAYS}d", file=sys.stderr)
        print(f"  Tokens: {len(self.all_data)} | Top: {self.top_n}", file=sys.stderr)
        print(f"{'=' * 70}\n", file=sys.stderr)

        while current <= end_date:
            day_count += 1

            # Skip weekends (crypto trades 24/7 but some data sources skip)
            # Actually, crypto has data every day → proceed

            # Step 1: Check exits on today's candle
            closed = self._check_exits(current)

            # Step 2: Run model and enter new positions at market
            signals = self._run_model_for_date(current)
            entered = self._place_orders(signals, current)

            # Step 3: Update equity
            open_value = 0
            for pos in self.open_positions:
                candle = self._get_candle(pos.symbol, current)
                if candle:
                    current_price = candle["close"]
                    unrealized_pnl = (current_price - pos.entry_price) / pos.entry_price * pos.size_usd
                    open_value += pos.size_usd + unrealized_pnl
                else:
                    open_value += pos.size_usd

            self.equity = self.cash + open_value

            # Step 4: Record snapshot
            snapshot = DailySnapshot(
                date=str(current),
                equity=round(self.equity, 2),
                cash=round(self.cash, 2),
                open_positions=len(self.open_positions),
                signals_generated=len(signals),
                orders_placed=entered,
                orders_filled=entered,
                positions_closed=closed,
            )
            self.daily_snapshots.append(snapshot)

            # Progress
            if day_count % 7 == 0 or current == end_date:
                open_syms = ",".join(p.symbol for p in self.open_positions) or "-"
                print(f"  {current}  equity=${self.equity:,.0f}  "
                      f"open={len(self.open_positions)} [{open_syms}]  "
                      f"signals={len(signals)} entered={entered} closed={closed}",
                      file=sys.stderr)

            current += timedelta(days=1)

        # Close any remaining open positions at the last candle's close
        for pos in self.open_positions:
            candle = self._get_candle(pos.symbol, end_date)
            if candle:
                pos.exit_price = candle["close"]
            else:
                pos.exit_price = pos.entry_price
            pos.exit_date = str(end_date)
            pos.exit_reason = "END"
            pos.pnl_usd = (pos.exit_price - pos.entry_price) / pos.entry_price * pos.size_usd
            pos.pnl_pct = (pos.exit_price - pos.entry_price) / pos.entry_price * 100
            self.cash += pos.size_usd + pos.pnl_usd
            self.closed_positions.append(pos)

        self.open_positions = []
        self.equity = self.cash

    def print_results(self):
        """Print backtest results."""
        trades = self.closed_positions
        if not trades:
            print("No trades executed.")
            return

        # —— Trade Statistics ————————————————————————————
        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd <= 0]
        tp_exits = [t for t in trades if t.exit_reason == "TP"]
        sl_exits = [t for t in trades if t.exit_reason == "SL"]
        timeout_exits = [t for t in trades if t.exit_reason == "TIMEOUT"]
        end_exits = [t for t in trades if t.exit_reason == "END"]

        total_pnl = sum(t.pnl_usd for t in trades)
        total_return_pct = (self.equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100

        avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
        avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0
        avg_hold = np.mean([t.hold_days for t in trades])

        # Equity curve stats
        equities = [s.equity for s in self.daily_snapshots]
        peak = STARTING_CAPITAL
        max_dd = 0
        for eq in equities:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

        # Daily returns for Sharpe
        daily_returns = []
        for i in range(1, len(equities)):
            daily_returns.append((equities[i] - equities[i-1]) / equities[i-1])
        sharpe = 0
        if daily_returns and np.std(daily_returns) > 0:
            sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(365)

        print()
        print("=" * 70)
        print(f"  BACKTEST RESULTS")
        print("=" * 70)

        print(f"""
  PORTFOLIO
    Starting capital:   ${STARTING_CAPITAL:,.0f}
    Final equity:       ${self.equity:,.0f}
    Total return:       {total_return_pct:+.1f}%
    Total P&L:          ${total_pnl:+,.0f}
    Max drawdown:       {max_dd:.1f}%
    Sharpe ratio:       {sharpe:.2f} (annualized)

  TRADES
    Total trades:       {len(trades)}
    Win rate:           {len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.0f}%)
    Avg winner:         {avg_win:+.1f}%
    Avg loser:          {avg_loss:.1f}%
    Avg hold time:      {avg_hold:.1f} days

  EXIT REASONS
    Take profit (TP):   {len(tp_exits)}
    Stop loss (SL):     {len(sl_exits)}
    Timeout (7d):       {len(timeout_exits)}
    End of test:        {len(end_exits)}

  RISK RULES (3-state regime model)
    BULL + high conf:       3.0% risk    BULL + med: 2.5%    BULL + low: 1.5%
    TRANSITION + high:      2.5%         TRANSITION + med: 1.5%  TRANSITION + low: SKIP
    BEAR + high:            1.0%         BEAR + med/low: SKIP
    (RANGE, NEUTRAL, unknown → TRANSITION)
    Max position:       {MAX_POSITION_PCT*100:.0f}% of equity
    Max concurrent:     {MAX_OPEN_POSITIONS} positions
    Hold timeout:       {MAX_HOLD_DAYS} days
""")

        # —— Trade Journal ———————————————————————————————
        print("  TRADE JOURNAL")
        print(f"  {'SYM':<7} {'ENTRY':>8} {'EXIT':>8} {'P&L%':>7} {'P&L$':>8} {'DAYS':>4} {'EXIT':>7} {'REGIME':<12} {'CONF':>5}")
        print("  " + "─" * 78)

        for t in sorted(trades, key=lambda t: t.entry_date):
            print(f"  {t.symbol:<7} {fmt_price(t.entry_price):>8} {fmt_price(t.exit_price):>8}"
                  f" {t.pnl_pct:>+6.1f}% {t.pnl_usd:>+7.0f} {t.hold_days:>4}d"
                  f" {t.exit_reason:>7} {t.regime:<12} {t.confidence:>5.2f}")

        print()

        # —— Equity Curve Summary ————————————————————————
        print("  EQUITY CURVE (weekly)")
        print(f"  {'DATE':<12} {'EQUITY':>10} {'OPEN':>5} {'WEEK P&L':>10}")
        print("  " + "─" * 42)
        prev_eq = STARTING_CAPITAL
        for i, snap in enumerate(self.daily_snapshots):
            if i % 7 == 0 or i == len(self.daily_snapshots) - 1:
                week_pnl = snap.equity - prev_eq
                print(f"  {snap.date:<12} ${snap.equity:>9,.0f} {snap.open_positions:>5}"
                      f" ${week_pnl:>+9,.0f}")
                prev_eq = snap.equity
        print()

    def export_csv(self, path: str):
        """Export trade journal to CSV."""
        import csv
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Symbol", "Entry Date", "Entry Price", "Exit Date", "Exit Price",
                "P&L %", "P&L $", "Hold Days", "Exit Reason", "Size $", "Risk $",
                "Confidence", "Score", "Regime",
            ])
            for t in sorted(self.closed_positions, key=lambda t: t.entry_date):
                writer.writerow([
                    t.symbol, t.entry_date, round(t.entry_price, 6),
                    t.exit_date, round(t.exit_price, 6),
                    round(t.pnl_pct, 2), round(t.pnl_usd, 2),
                    t.hold_days, t.exit_reason,
                    round(t.size_usd, 2), round(t.risk_usd, 2),
                    round(t.confidence, 3), round(t.composite_score, 3),
                    t.regime,
                ])
        print(f"  Exported to {path}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest the SR ranking model as a portfolio strategy",
        epilog="Examples:\n"
               "  python3 backtest_portfolio.py --days 90 --top 5\n"
               "  python3 backtest_portfolio.py --days 60 --tokens 20 --csv results.csv\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=90,
                        help="Backtest period in days (default: 90)")
    parser.add_argument("--top", type=int, default=5,
                        help="Top N tokens per daily signal (default: 5)")
    parser.add_argument("--tokens", type=int, default=30,
                        help="Number of tokens to analyze (default: 30)")
    parser.add_argument("--data-dir", type=str, default="data",
                        help="Directory with cached CSV files")
    parser.add_argument("--csv", type=str, default=None,
                        help="Export trade journal to CSV")

    args = parser.parse_args()

    # Determine which tokens to backtest
    # Use cached data — must run fetch_daily.py first
    import glob
    pattern = os.path.join(args.data_dir, "*_daily.csv")
    files = glob.glob(pattern)
    all_symbols = [os.path.basename(f).replace("_daily.csv", "") for f in sorted(files)]

    if not all_symbols:
        print(f"No data in {args.data_dir}/. Run fetch_daily.py first.", file=sys.stderr)
        sys.exit(1)

    # Limit to top N tokens
    symbols = all_symbols[:args.tokens]
    print(f"  Backtesting {len(symbols)} tokens over {args.days} days", file=sys.stderr)

    # Date range: end = last date in data, start = end - days
    sample_df = load_from_cache(symbols[0], args.data_dir)
    if sample_df is None:
        print("Cannot determine date range.", file=sys.stderr)
        sys.exit(1)

    end_date = sample_df.index[-1].date()
    # Reserve 90 days of data before the backtest starts (for S/R analysis warmup)
    warmup_days = 90
    start_date = end_date - timedelta(days=args.days)

    # Check we have enough data
    earliest = sample_df.index[0].date()
    if start_date - timedelta(days=warmup_days) < earliest:
        available = (end_date - earliest).days - warmup_days
        if available < 14:
            print(f"Not enough data. Need {args.days + warmup_days} days, "
                  f"have {(end_date - earliest).days}.", file=sys.stderr)
            sys.exit(1)
        print(f"  Adjusting backtest to {available} days (data limit)", file=sys.stderr)
        start_date = earliest + timedelta(days=warmup_days)

    # Run backtest
    bt = Backtester(args.data_dir, symbols, top_n=args.top)
    bt.run(start_date, end_date)
    bt.print_results()

    if args.csv:
        bt.export_csv(args.csv)


if __name__ == "__main__":
    main()
