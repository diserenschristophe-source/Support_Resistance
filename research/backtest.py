#!/usr/bin/env python3
"""
backtest.py — SR Model Trade Data Recorder
==========================================

Records every qualifying trade with full entry context for post-analysis.
No capital management — flat $100 notional per trade, no position limits.

Entry filter:
  - If |entry - S1| < ATR → use S2 as stop loss
  - If |R1 - entry| < ATR → use R2 as take profit
  - Recalculate R:R after level selection
  - Only enter if R:R >= --min-rr (default 2.0)

Usage:
    python3 backtest.py --days 90 --top 5
    python3 backtest.py --days 90 --top 5 --min-rr 2.0 --csv results.csv
"""

import argparse
import csv
import glob
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── Path setup: add parent dir (core modules) to sys.path ────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.join(_THIS_DIR, '..')
sys.path.insert(0, _PARENT_DIR)
sys.path.insert(0, _THIS_DIR)

from roro_features import compute_roro_features
_RORO_AVAILABLE = True

from core.sr_analysis import analyze_token
from research.regime import compute_indicators, classify_regime
from core.tpsl import compute_token_score
from core.fetcher import load_from_cache


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

MAX_HOLD_DAYS = 7

CSV_COLUMNS = [
    # Trade basics
    "symbol", "entry_date", "entry_price", "exit_date", "exit_price",
    "tp", "sl", "rr", "gain_pct", "loss_pct", "pnl_pct", "hold_days", "exit_reason",
    # S/R level prices at entry
    "S1_price", "S2_price", "S3_price", "R1_price", "R2_price", "R3_price",
    # Entry quality
    "entry_quality_pct", "atr",
    # Regime indicators
    "adx", "adx_slope", "adx_dir", "rsi", "di_diff", "di_spread_slope", "di_dir",
    "raw_di_plus", "raw_di_minus", "trend",
    # RORO 7 features [-1, +1]
    "f_trend", "f_momentum", "f_vol_regime", "f_drawdown",
    "f_dir_volume", "f_adx_strength", "f_di_spread",
    # RORO aggregates & signals
    "roro_composite", "n_positive",
    "sig_and_tre_dir", "sig_and_mom_dir_di",
    "sig_vote_3of7", "sig_vote_4of7", "sig_vote_5of7",
    # Volume
    "volume_surge_ratio",
    # S/R hit tracking (during hold period)
    "S1_hit", "S1_days", "S2_hit", "S2_days", "S3_hit", "S3_days",
    "R1_hit", "R1_days", "R2_hit", "R2_days", "R3_hit", "R3_days",
    # Rebound: S1 touched then R1/R2/R3 hit afterward
    "S1_rebound_R1", "S1_rebound_R2", "S1_rebound_R3",
]


# ─────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────

class Backtester:

    def __init__(self, data_dir: str, tokens: List[str], top_n: int, min_rr: float):
        self.data_dir = data_dir
        self.tokens = tokens
        self.top_n = top_n
        self.min_rr = min_rr

        self.open_trades: List[dict] = []
        self.closed_trades: List[dict] = []

        # Load all OHLCV data upfront
        self.all_data: Dict[str, pd.DataFrame] = {}
        for sym in tokens:
            df = load_from_cache(sym, data_dir)
            if df is not None and len(df) >= 60:
                self.all_data[sym] = df

        print(f"  Loaded {len(self.all_data)} tokens with sufficient data", file=sys.stderr)

    # ── Data Access ───────────────────────────────────────────

    def _get_data_up_to(self, symbol: str, date) -> Optional[pd.DataFrame]:
        """Return OHLCV slice up to and including date. No future leakage."""
        if symbol not in self.all_data:
            return None
        df = self.all_data[symbol]
        subset = df[df.index.date <= date]
        return subset if len(subset) >= 60 else None

    def _get_candle(self, symbol: str, date) -> Optional[dict]:
        """Return OHLCV candle for a specific date."""
        if symbol not in self.all_data:
            return None
        df = self.all_data[symbol]
        candles = df[df.index.date == date]
        if candles.empty:
            return None
        row = candles.iloc[-1]
        return {
            "open":   float(row["open"]),
            "high":   float(row["high"]),
            "low":    float(row["low"]),
            "close":  float(row["close"]),
            "volume": float(row.get("volume", 0)),
        }

    # ── Entry Logic ───────────────────────────────────────────

    def _select_tp_sl(
        self,
        supports: list,
        resistances: list,
        entry: float,
        atr: float,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Select TP and SL with ATR proximity skip.

        Rules:
          - SL = S1; if |entry - S1| < ATR → skip to S2
          - TP = R1; if |R1 - entry| < ATR → skip to R2
          - Returns (tp, sl, rr) or None if no valid setup found.
        """
        # Filter to levels strictly below/above entry
        s_levels = [s["key_level"] for s in supports if s["key_level"] < entry]
        r_levels = [r["key_level"] for r in resistances if r["key_level"] > entry]

        if not s_levels or not r_levels:
            return None

        # Select SL
        sl = s_levels[0]
        if atr > 0 and abs(entry - sl) < atr:
            if len(s_levels) > 1:
                sl = s_levels[1]
            else:
                return None  # S1 too close, no S2 to fall back to

        # Select TP
        tp = r_levels[0]
        if atr > 0 and abs(tp - entry) < atr:
            if len(r_levels) > 1:
                tp = r_levels[1]
            else:
                return None  # R1 too close, no R2 to fall back to

        if tp <= entry or sl >= entry:
            return None

        loss = entry - sl
        if loss <= 0:
            return None

        rr = (tp - entry) / loss
        return tp, sl, rr

    def _compute_volume_surge(self, df: pd.DataFrame) -> Optional[float]:
        """Volume at last bar divided by 20-day average volume."""
        if "volume" not in df.columns or len(df) < 21:
            return None
        avg_vol = df["volume"].iloc[-21:-1].mean()
        if avg_vol == 0 or np.isnan(avg_vol):
            return None
        return round(float(df["volume"].iloc[-1] / avg_vol), 4)

    # ── Model ─────────────────────────────────────────────────

    def _run_model_for_date(self, date) -> List[dict]:
        """
        Score all tokens using data available up to `date`.
        Apply ATR proximity skip and R:R filter.
        Return top N qualifying signals sorted by composite score.
        """
        signals = []

        for symbol in self.all_data:
            try:
                df = self._get_data_up_to(symbol, date)
                if df is None:
                    continue

                # ── S/R analysis ──
                analysis = analyze_token(symbol, df)
                if not analysis:
                    continue

                entry = analysis.get("price", 0)
                if not entry or entry <= 0:
                    continue

                ms       = analysis.get("market_structure", {})
                atr      = ms.get("atr14", 0)
                trend    = ms.get("trend", "")
                supports    = analysis.get("support", [])
                resistances = analysis.get("resistance", [])

                # ── TP/SL selection with ATR proximity skip ──
                result = self._select_tp_sl(supports, resistances, entry, atr)
                if result is None:
                    continue
                tp, sl, rr = result

                if rr < self.min_rr:
                    continue

                # ── Composite score for ranking (uses its own TP/SL internally) ──
                indicators  = compute_indicators(df)
                regime_data = classify_regime(indicators)
                token_score = compute_token_score(analysis, regime_data=regime_data)
                composite   = token_score.get("composite_score", 0) if token_score else 0

                # ── S1/S2/S3 and R1/R2/R3 price levels ──
                s_prices = [s["key_level"] for s in supports if s["key_level"] < entry]
                r_prices = [r["key_level"] for r in resistances if r["key_level"] > entry]
                while len(s_prices) < 3:
                    s_prices.append(None)
                while len(r_prices) < 3:
                    r_prices.append(None)

                # entry_quality_pct: distance from price to nearest support (S1)
                entry_quality_pct = None
                if s_prices[0] is not None:
                    entry_quality_pct = round(abs(entry - s_prices[0]) / entry * 100, 4)

                # ── Volume surge ──
                volume_surge = self._compute_volume_surge(df)

                # ── RORO 7 features ──
                roro: dict = {}
                if _RORO_AVAILABLE:
                    try:
                        roro = compute_roro_features(df)
                    except Exception:
                        pass

                signals.append({
                    "symbol":       symbol,
                    "entry_price":  entry,
                    "tp":           tp,
                    "sl":           sl,
                    "rr":           round(rr, 4),
                    "composite":    composite,
                    # S/R levels
                    "S1_price": s_prices[0],
                    "S2_price": s_prices[1],
                    "S3_price": s_prices[2],
                    "R1_price": r_prices[0],
                    "R2_price": r_prices[1],
                    "R3_price": r_prices[2],
                    # Entry context
                    "entry_quality_pct": entry_quality_pct,
                    "atr":   round(atr, 8),
                    "trend": trend,
                    # Regime (from regime.py)
                    "adx":             regime_data.get("adx"),
                    "adx_slope":       indicators.get("adx_slope"),
                    "adx_dir":         regime_data.get("adx_direction"),
                    "rsi":             regime_data.get("rsi"),
                    "di_diff":         regime_data.get("di_diff"),
                    "di_spread_slope": indicators.get("di_spread_slope"),
                    "di_dir":          regime_data.get("di_direction"),
                    "raw_di_plus":     regime_data.get("plus_di"),
                    "raw_di_minus":    regime_data.get("minus_di"),
                    # RORO 7 features
                    "f_trend":        roro.get("f_trend"),
                    "f_momentum":     roro.get("f_momentum"),
                    "f_vol_regime":   roro.get("f_vol_regime"),
                    "f_drawdown":     roro.get("f_drawdown"),
                    "f_dir_volume":   roro.get("f_dir_volume"),
                    "f_adx_strength": roro.get("f_adx_strength"),
                    "f_di_spread":    roro.get("f_di_spread"),
                    # RORO aggregates & signals
                    "roro_composite":    roro.get("roro_composite"),
                    "n_positive":        roro.get("n_positive"),
                    "sig_and_tre_dir":   roro.get("sig_and_tre_dir"),
                    "sig_and_mom_dir_di":roro.get("sig_and_mom_dir_di"),
                    "sig_vote_3of7":     roro.get("sig_vote_3of7"),
                    "sig_vote_4of7":     roro.get("sig_vote_4of7"),
                    "sig_vote_5of7":     roro.get("sig_vote_5of7"),
                    # Volume
                    "volume_surge_ratio": volume_surge,
                })

            except Exception:
                continue

        signals.sort(key=lambda s: s["composite"], reverse=True)
        return signals[:self.top_n]

    # ── Trade Lifecycle ───────────────────────────────────────

    def _open_trade(self, sig: dict, date) -> dict:
        """Create and register a new trade record."""
        trade = {
            # Filled at entry
            "symbol":      sig["symbol"],
            "entry_date":  str(date),
            "entry_price": sig["entry_price"],
            "tp":          sig["tp"],
            "sl":          sig["sl"],
            "rr":          sig["rr"],
            # Filled at exit
            "exit_date":   None,
            "exit_price":  None,
            "gain_pct":    None,
            "loss_pct":    None,
            "pnl_pct":     None,
            "hold_days":   0,
            "exit_reason": None,
            # S/R level prices
            "S1_price": sig["S1_price"],
            "S2_price": sig["S2_price"],
            "S3_price": sig["S3_price"],
            "R1_price": sig["R1_price"],
            "R2_price": sig["R2_price"],
            "R3_price": sig["R3_price"],
            # Entry context
            "entry_quality_pct": sig["entry_quality_pct"],
            "atr":   sig["atr"],
            "trend": sig["trend"],
            # Regime
            "adx":             sig["adx"],
            "adx_slope":       sig["adx_slope"],
            "adx_dir":         sig["adx_dir"],
            "rsi":             sig["rsi"],
            "di_diff":         sig["di_diff"],
            "di_spread_slope": sig["di_spread_slope"],
            "di_dir":          sig["di_dir"],
            "raw_di_plus":     sig["raw_di_plus"],
            "raw_di_minus":    sig["raw_di_minus"],
            # RORO
            "f_trend":           sig["f_trend"],
            "f_momentum":        sig["f_momentum"],
            "f_vol_regime":      sig["f_vol_regime"],
            "f_drawdown":        sig["f_drawdown"],
            "f_dir_volume":      sig["f_dir_volume"],
            "f_adx_strength":    sig["f_adx_strength"],
            "f_di_spread":       sig["f_di_spread"],
            "roro_composite":    sig["roro_composite"],
            "n_positive":        sig["n_positive"],
            "sig_and_tre_dir":   sig["sig_and_tre_dir"],
            "sig_and_mom_dir_di":sig["sig_and_mom_dir_di"],
            "sig_vote_3of7":     sig["sig_vote_3of7"],
            "sig_vote_4of7":     sig["sig_vote_4of7"],
            "sig_vote_5of7":     sig["sig_vote_5of7"],
            # Volume
            "volume_surge_ratio": sig["volume_surge_ratio"],
            # S/R hit tracking (updated daily during hold)
            "S1_hit": False, "S1_days": None,
            "S2_hit": False, "S2_days": None,
            "S3_hit": False, "S3_days": None,
            "R1_hit": False, "R1_days": None,
            "R2_hit": False, "R2_days": None,
            "R3_hit": False, "R3_days": None,
            # Rebound flags (computed at close)
            "S1_rebound_R1": False,
            "S1_rebound_R2": False,
            "S1_rebound_R3": False,
        }
        self.open_trades.append(trade)
        return trade

    def _close_trade(
        self,
        trade: dict,
        exit_price: float,
        exit_date: str,
        hold_days: int,
        reason: str,
    ):
        """Finalize exit fields, compute rebound flags, move to closed."""
        entry    = trade["entry_price"]
        pnl_pct  = round((exit_price - entry) / entry * 100, 4)
        gain_pct = round(pnl_pct, 4) if pnl_pct > 0 else 0.0
        loss_pct = round(pnl_pct, 4) if pnl_pct < 0 else 0.0

        trade["exit_price"]  = exit_price
        trade["exit_date"]   = exit_date
        trade["hold_days"]   = hold_days
        trade["exit_reason"] = reason
        trade["pnl_pct"]     = pnl_pct
        trade["gain_pct"]    = gain_pct
        trade["loss_pct"]    = loss_pct

        # Rebound flags: S1 was touched first, then R1/R2/R3 hit afterward
        s1_days = trade["S1_days"]
        if trade["S1_hit"] and s1_days is not None:
            for rkey in ["R1", "R2", "R3"]:
                r_hit  = trade[f"{rkey}_hit"]
                r_days = trade[f"{rkey}_days"]
                trade[f"S1_rebound_{rkey}"] = (
                    r_hit and r_days is not None and r_days > s1_days
                )

        self.closed_trades.append(trade)

    def _track_sr_hits(self, trade: dict, candle: dict, hold_days: int):
        """Update S1-S3 and R1-R3 hit flags for a single candle."""
        high = candle["high"]
        low  = candle["low"]

        for key in ("S1", "S2", "S3"):
            level = trade.get(f"{key}_price")
            if level is not None and not trade[f"{key}_hit"]:
                if low <= level:
                    trade[f"{key}_hit"]  = True
                    trade[f"{key}_days"] = hold_days

        for key in ("R1", "R2", "R3"):
            level = trade.get(f"{key}_price")
            if level is not None and not trade[f"{key}_hit"]:
                if high >= level:
                    trade[f"{key}_hit"]  = True
                    trade[f"{key}_days"] = hold_days

    def _check_exits(self, date):
        """
        For each open trade:
          1. Track S/R hits from today's candle
          2. Check timeout → SL → TP (in that order)
          3. Close if any condition met
        """
        still_open = []
        date_str   = str(date)

        for trade in self.open_trades:
            entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d").date()
            hold_days  = (date - entry_date).days
            candle     = self._get_candle(trade["symbol"], date)

            # Track S/R hits before checking exits (price touched level even on exit day)
            if candle:
                self._track_sr_hits(trade, candle, hold_days)

            # ── Timeout (7 days hard limit) ──
            if hold_days >= MAX_HOLD_DAYS:
                exit_price = candle["close"] if candle else trade["entry_price"]
                self._close_trade(trade, exit_price, date_str, hold_days, "TIMEOUT")
                continue

            if not candle:
                still_open.append(trade)
                continue

            # ── SL hit ──
            if candle["low"] <= trade["sl"]:
                self._close_trade(trade, trade["sl"], date_str, hold_days, "SL")
                continue

            # ── TP hit ──
            if candle["high"] >= trade["tp"]:
                self._close_trade(trade, trade["tp"], date_str, hold_days, "TP")
                continue

            still_open.append(trade)

        self.open_trades = still_open

    # ── Main Loop ─────────────────────────────────────────────

    def run(self, start_date, end_date):
        """Run the backtest from start_date to end_date."""
        current   = start_date
        day_count = 0

        print(f"\n{'=' * 70}", file=sys.stderr)
        print(f"  BACKTEST: {start_date} → {end_date}", file=sys.stderr)
        print(f"  Tokens: {len(self.all_data)} | Top: {self.top_n} | Min R:R: {self.min_rr}", file=sys.stderr)
        print(f"{'=' * 70}\n", file=sys.stderr)

        while current <= end_date:
            day_count += 1

            # Step 1: Check exits (and track S/R hits inside)
            self._check_exits(current)

            # Step 2: Generate signals and open new trades
            signals      = self._run_model_for_date(current)
            held_symbols = {t["symbol"] for t in self.open_trades}

            entered = 0
            for sig in signals:
                if sig["symbol"] not in held_symbols:
                    self._open_trade(sig, current)
                    held_symbols.add(sig["symbol"])
                    entered += 1

            if day_count % 7 == 0 or current == end_date:
                print(
                    f"  {current}  open={len(self.open_trades):>3}  "
                    f"closed={len(self.closed_trades):>4}  "
                    f"signals={len(signals)}  entered={entered}",
                    file=sys.stderr,
                )

            current += timedelta(days=1)

        # Close any remaining open positions at end of test
        for trade in self.open_trades:
            candle     = self._get_candle(trade["symbol"], end_date)
            exit_price = candle["close"] if candle else trade["entry_price"]
            entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d").date()
            hold_days  = (end_date - entry_date).days
            self._close_trade(trade, exit_price, str(end_date), hold_days, "END")

        self.open_trades = []

    # ── Output ────────────────────────────────────────────────

    def print_summary(self):
        """Print a brief results summary to stdout."""
        trades = self.closed_trades
        if not trades:
            print("No trades executed.")
            return

        wins   = [t for t in trades if (t["pnl_pct"] or 0) > 0]
        losses = [t for t in trades if (t["pnl_pct"] or 0) <= 0]
        pnls   = [t["pnl_pct"] or 0 for t in trades]

        exits: dict = {}
        for t in trades:
            exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1

        print()
        print("=" * 60)
        print("  TRADE SUMMARY")
        print("=" * 60)
        print(f"  Total trades:   {len(trades)}")
        print(f"  Winners:        {len(wins)} ({len(wins)/len(trades)*100:.0f}%)")
        print(f"  Losers:         {len(losses)}")
        print(f"  Avg P&L:        {np.mean(pnls):+.2f}%")
        if wins:
            print(f"  Avg win:        {np.mean([t['pnl_pct'] for t in wins]):+.2f}%")
        if losses:
            print(f"  Avg loss:       {np.mean([t['pnl_pct'] for t in losses]):+.2f}%")
        print(f"  Exit reasons:   {exits}")
        print()

        # Trade journal table
        print(f"  {'SYM':<7} {'ENTRY':>12} {'ENTRY $':>10} {'EXIT':>12} {'EXIT $':>10}"
              f" {'P&L%':>7} {'DAYS':>4} {'REASON':>7} {'R:R':>5}")
        print("  " + "─" * 80)
        for t in sorted(trades, key=lambda t: t["entry_date"]):
            print(
                f"  {t['symbol']:<7} {t['entry_date']:>12} {t['entry_price']:>10.4f}"
                f" {t['exit_date']:>12} {t['exit_price']:>10.4f}"
                f" {(t['pnl_pct'] or 0):>+6.1f}% {t['hold_days']:>4}d"
                f" {t['exit_reason']:>7} {t['rr']:>5.2f}"
            )
        print()

    def export_csv(self, path: str):
        """Export all closed trades to CSV with full context columns."""
        trades = sorted(self.closed_trades, key=lambda t: t["entry_date"])
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for trade in trades:
                writer.writerow(trade)
        print(f"  Exported {len(trades)} trades → {path}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SR model trade data recorder",
        epilog=(
            "Examples:\n"
            "  python3 backtest.py --days 90 --top 5\n"
            "  python3 backtest.py --days 90 --top 5 --min-rr 2.0 --csv results.csv\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days",     type=int,   default=90,    help="Backtest period in days (default: 90)")
    parser.add_argument("--top",      type=int,   default=5,     help="Top N tokens per day (default: 5)")
    parser.add_argument("--tokens",   type=int,   default=30,    help="Max tokens to analyze (default: 30)")
    parser.add_argument("--min-rr",   type=float, default=2.0,   help="Minimum R:R to enter a trade (default: 2.0)")
    parser.add_argument("--data-dir", type=str,   default="data",help="Directory with cached CSV files")
    parser.add_argument("--csv",      type=str,   default=None,  help="Export trade journal to CSV")
    args = parser.parse_args()

    # Discover tokens from cached data files
    files = glob.glob(os.path.join(args.data_dir, "*_daily.csv"))
    all_symbols = sorted([os.path.basename(f).replace("_daily.csv", "") for f in files])

    if not all_symbols:
        print(f"No data in {args.data_dir}/. Run fetch_daily.py first.", file=sys.stderr)
        sys.exit(1)

    symbols = all_symbols[:args.tokens]
    print(f"  Backtesting {len(symbols)} tokens over {args.days} days", file=sys.stderr)

    # Determine date range from first available cached file
    sample_df = load_from_cache(symbols[0], args.data_dir)
    if sample_df is None:
        print("Cannot determine date range.", file=sys.stderr)
        sys.exit(1)

    end_date   = sample_df.index[-1].date()
    start_date = end_date - timedelta(days=args.days)

    # Ensure warmup data (90 days) is available before backtest start
    earliest    = sample_df.index[0].date()
    warmup_days = 90
    if start_date - timedelta(days=warmup_days) < earliest:
        available = (end_date - earliest).days - warmup_days
        if available < 14:
            print(
                f"Not enough data. Need {args.days + warmup_days} days, "
                f"have {(end_date - earliest).days}.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  Adjusting backtest to {available} days (data limit)", file=sys.stderr)
        start_date = earliest + timedelta(days=warmup_days)

    bt = Backtester(args.data_dir, symbols, top_n=args.top, min_rr=args.min_rr)
    bt.run(start_date, end_date)
    bt.print_summary()

    if args.csv:
        bt.export_csv(args.csv)


if __name__ == "__main__":
    main()
