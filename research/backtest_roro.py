#!/usr/bin/env python3
"""
backtest_roro.py — SR Data Gatherer with RSI Momentum Flag
===========================================================

Trades ALL tokens with R:R >= 2.0 (no position limits, no gate blocking).
Records the RORO RSI Momentum Conservative signal as an informational
column (rsi_gate = True/False) for post-analysis.

RSI gate criteria (logged, not enforced):
  - Token RSI(10) > 60
  - BTC RSI(10) >= 50  (BTC floor)

Usage:
    python3 backtest_roro.py --days 90 --data-dir data49 --tokens 49
    python3 backtest_roro.py --days 90 --data-dir data49 --tokens 49 --csv results_roro.csv
"""

import argparse
import csv
import glob
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

# ── Path setup: add parent dir (core modules) to sys.path ────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.join(_THIS_DIR, '..')
sys.path.insert(0, _PARENT_DIR)
sys.path.insert(0, _THIS_DIR)

from roro_features import compute_roro_features
_RORO_AVAILABLE = True

try:
    from resample_daily import _compute_daily_rsi, _resample_to_daily
    _RESAMPLE_DAILY_AVAILABLE = True
except ImportError:
    _RESAMPLE_DAILY_AVAILABLE = False
    print("Warning: resample_daily not found. Falling back to built-in RSI.", file=sys.stderr)

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
    # RSI Momentum gate (informational, not enforced)
    "rsi_gate", "rsi_momentum_weight", "rsi_momentum_rsi", "btc_rsi",
    # S/R hit tracking (during hold period)
    "S1_hit", "S1_days", "S2_hit", "S2_days", "S3_hit", "S3_days",
    "R1_hit", "R1_days", "R2_hit", "R2_days", "R3_hit", "R3_days",
    # Rebound: S1 touched then R1/R2/R3 hit afterward
    "S1_rebound_R1", "S1_rebound_R2", "S1_rebound_R3",
]


# ─────────────────────────────────────────────────────────────
# RSI Momentum Gate (adapted from conservative RORO signal)
# ─────────────────────────────────────────────────────────────

def _compute_rsi_series(close: pd.Series, period: int) -> pd.Series:
    """Compute RSI — delegates to resample_daily (TA-Lib) when available,
    falls back to Wilder's smoothing otherwise."""
    if _RESAMPLE_DAILY_AVAILABLE:
        import talib
        return pd.Series(talib.RSI(close, period), index=close.index)
    # Fallback: manual Wilder's smoothing
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


class RSIMomentumGate:
    """
    Conservative RSI Momentum on/off gate.

    Mirrors rsi_momentum_new_universe_single_conservative_roro_signal
    from python_backtester/examples.py, adapted for daily data.

    For each day, produces a dict: {symbol: weight} for tokens that
    are "risk-on". Weight = 0 means the token is gated off.
    """

    def __init__(
        self,
        rsi_period: int = 10,
        threshold: float = 60.0,
        btc_rsi_threshold: float = 50.0,
        btc_symbol: str = "BTC",
        hysteresis: float = 2.0,
        top_n: int = 3,
        max_single_weight: float = 0.5,
    ):
        self.rsi_period = rsi_period
        self.threshold = threshold
        self.btc_rsi_threshold = btc_rsi_threshold
        self.btc_symbol = btc_symbol
        self.hysteresis = hysteresis
        self.top_n = top_n
        self.max_single_weight = max_single_weight

        # Internal state
        self._rsi_cache: Dict[str, pd.Series] = {}
        self._prev_held: List[str] = []

    def precompute_rsi(self, all_data: Dict[str, pd.DataFrame]):
        """Pre-compute daily RSI series for all tokens.

        Expects all_data to already be daily candles (resampled at load time).
        Uses _compute_daily_rsi (TA-Lib) when available, falls back to
        built-in _compute_rsi_series otherwise.
        """
        if _RESAMPLE_DAILY_AVAILABLE:
            ohlcv = {sym: df for sym, df in all_data.items() if len(df) >= self.rsi_period + 5}
            close_df = pd.DataFrame(
                {sym: df["close"] for sym, df in ohlcv.items()}
            ).sort_index()
            rsi_df = _compute_daily_rsi(close_df, self.rsi_period)
            for sym in rsi_df.columns:
                self._rsi_cache[sym] = rsi_df[sym]
        else:
            for sym, df in all_data.items():
                if len(df) >= self.rsi_period + 5:
                    self._rsi_cache[sym] = _compute_rsi_series(df["close"], self.rsi_period)

    def get_weights(self, date) -> Dict[str, float]:
        """
        Compute RSI momentum weights for a given date.

        Returns dict mapping symbol -> weight (0.0 = gated off).
        Also returns metadata for logging.
        """
        # Gather RSI values for this date
        rsi_values: Dict[str, float] = {}
        for sym, rsi_series in self._rsi_cache.items():
            mask = rsi_series.index.date <= date
            valid = rsi_series[mask]
            if not valid.empty:
                val = float(valid.iloc[-1])
                if not np.isnan(val):
                    rsi_values[sym] = val

        if not rsi_values:
            self._prev_held = []
            return {}

        # BTC RSI floor check
        btc_rsi = rsi_values.get(self.btc_symbol, 50.0)
        if btc_rsi < self.btc_rsi_threshold:
            self._prev_held = []
            return {"_btc_rsi": btc_rsi, "_risk_off": True}

        # Filter eligible tokens (RSI > threshold)
        eligible = {s: r for s, r in rsi_values.items() if r > self.threshold}
        eligible_sorted = sorted(eligible.items(), key=lambda x: x[1], reverse=True)

        if not eligible_sorted:
            self._prev_held = []
            return {"_btc_rsi": btc_rsi}

        # Hysteresis: retain previous holdings above threshold
        new_held: List[str] = []
        for asset in self._prev_held:
            if asset in eligible and len(new_held) < self.top_n:
                new_held.append(asset)

        # Add new candidates that beat weakest held by hysteresis
        weakest_rsi = min((rsi_values[a] for a in new_held), default=0.0)
        for asset, rsi_val in eligible_sorted:
            if asset not in new_held and len(new_held) < self.top_n:
                if rsi_val > weakest_rsi + self.hysteresis or len(new_held) == 0:
                    new_held.append(asset)

        if not new_held:
            self._prev_held = []
            return {"_btc_rsi": btc_rsi}

        # Diversification: equal weight capped at max_single_weight
        n = len(new_held)
        w = min(1.0 / n, self.max_single_weight)
        weights = {asset: w for asset in new_held}
        weights["_btc_rsi"] = btc_rsi

        self._prev_held = new_held
        return weights

    def get_rsi(self, symbol: str, date) -> Optional[float]:
        """Return the RSI value for a symbol at a given date."""
        rsi_series = self._rsi_cache.get(symbol)
        if rsi_series is None:
            return None
        mask = rsi_series.index.date <= date
        valid = rsi_series[mask]
        if valid.empty:
            return None
        val = float(valid.iloc[-1])
        return val if not np.isnan(val) else None


# ─────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────

class BacktesterRORI:

    def __init__(
        self,
        data_dir: str,
        tokens: List[str],
        min_rr: float,
        gate: RSIMomentumGate,
    ):
        self.data_dir = data_dir
        self.tokens = tokens
        self.min_rr = min_rr
        self.gate = gate

        self.open_trades: List[dict] = []
        self.closed_trades: List[dict] = []

        # Load all OHLCV data upfront
        raw_data: Dict[str, pd.DataFrame] = {}
        for sym in tokens:
            df = load_from_cache(sym, data_dir)
            if df is not None and len(df) >= 60:
                raw_data[sym] = df

        # Resample to daily candles (no-op if already daily)
        if _RESAMPLE_DAILY_AVAILABLE:
            self.all_data = _resample_to_daily(raw_data)
        else:
            self.all_data = raw_data

        print(f"  Loaded {len(self.all_data)} tokens with sufficient data (daily)", file=sys.stderr)

        # Pre-compute RSI for the gate
        self.gate.precompute_rsi(self.all_data)

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
          - SL = S1; if |entry - S1| < ATR -> skip to S2
          - TP = R1; if |R1 - entry| < ATR -> skip to R2
          - Returns (tp, sl, rr) or None if no valid setup found.
        """
        s_levels = [s["key_level"] for s in supports if s["key_level"] < entry]
        r_levels = [r["key_level"] for r in resistances if r["key_level"] > entry]

        if not s_levels or not r_levels:
            return None

        sl = s_levels[0]
        if atr > 0 and abs(entry - sl) < atr:
            if len(s_levels) > 1:
                sl = s_levels[1]
            else:
                return None

        tp = r_levels[0]
        if atr > 0 and abs(tp - entry) < atr:
            if len(r_levels) > 1:
                tp = r_levels[1]
            else:
                return None

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

    def _run_model_for_date(self, date, gate_weights: Dict[str, float]) -> List[dict]:
        """
        Score all tokens using data available up to `date`.
        Apply ATR proximity skip and R:R filter.
        Tag each signal with rsi_gate True/False (informational only).
        Returns ALL qualifying signals (no top-N cap).
        """
        # Determine which tokens the gate considers risk-on
        risk_on_symbols: Set[str] = set()
        for sym, w in gate_weights.items():
            if not sym.startswith("_") and w > 0:
                risk_on_symbols.add(sym)

        btc_rsi = gate_weights.get("_btc_rsi")
        is_risk_off = gate_weights.get("_risk_off", False)

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

                # ── Composite score for ranking ──
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

                # ── Gate metadata (informational) ──
                gate_weight = gate_weights.get(symbol, 0.0)
                token_rsi = self.gate.get_rsi(symbol, date)
                rsi_gate = (not is_risk_off) and (symbol in risk_on_symbols)

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
                    # Regime
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
                    # RSI Momentum gate (informational)
                    "rsi_gate":            rsi_gate,
                    "rsi_momentum_weight": round(gate_weight, 4) if not isinstance(gate_weight, bool) else 0.0,
                    "rsi_momentum_rsi":    round(token_rsi, 4) if token_rsi is not None else None,
                    "btc_rsi":             round(btc_rsi, 4) if btc_rsi is not None else None,
                })

            except Exception:
                continue

        signals.sort(key=lambda s: s["composite"], reverse=True)
        return signals

    # ── Trade Lifecycle ───────────────────────────────────────

    def _open_trade(self, sig: dict, date) -> dict:
        """Create and register a new trade record."""
        trade = {
            "symbol":      sig["symbol"],
            "entry_date":  str(date),
            "entry_price": sig["entry_price"],
            "tp":          sig["tp"],
            "sl":          sig["sl"],
            "rr":          sig["rr"],
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
            # RSI Momentum gate (informational)
            "rsi_gate":            sig["rsi_gate"],
            "rsi_momentum_weight": sig["rsi_momentum_weight"],
            "rsi_momentum_rsi":    sig["rsi_momentum_rsi"],
            "btc_rsi":             sig["btc_rsi"],
            # S/R hit tracking
            "S1_hit": False, "S1_days": None,
            "S2_hit": False, "S2_days": None,
            "S3_hit": False, "S3_days": None,
            "R1_hit": False, "R1_days": None,
            "R2_hit": False, "R2_days": None,
            "R3_hit": False, "R3_days": None,
            # Rebound flags
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
          2. Check timeout -> SL -> TP (in that order)
          3. Close if any condition met
        """
        still_open = []
        date_str   = str(date)

        for trade in self.open_trades:
            entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d").date()
            hold_days  = (date - entry_date).days
            candle     = self._get_candle(trade["symbol"], date)

            if candle:
                self._track_sr_hits(trade, candle, hold_days)

            if hold_days >= MAX_HOLD_DAYS:
                exit_price = candle["close"] if candle else trade["entry_price"]
                self._close_trade(trade, exit_price, date_str, hold_days, "TIMEOUT")
                continue

            if not candle:
                still_open.append(trade)
                continue

            if candle["low"] <= trade["sl"]:
                self._close_trade(trade, trade["sl"], date_str, hold_days, "SL")
                continue

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
        risk_off_days = 0
        total_signals = 0

        print(f"\n{'=' * 70}", file=sys.stderr)
        print(f"  BACKTEST (Data Gathering): {start_date} -> {end_date}", file=sys.stderr)
        print(f"  Tokens: {len(self.all_data)} | Min R:R: {self.min_rr}", file=sys.stderr)
        print(f"  RSI gate (info only): RSI({self.gate.rsi_period}) > {self.gate.threshold} | "
              f"BTC floor: {self.gate.btc_rsi_threshold}", file=sys.stderr)
        print(f"{'=' * 70}\n", file=sys.stderr)

        while current <= end_date:
            day_count += 1

            # Step 1: Check exits
            self._check_exits(current)

            # Step 2: Compute RSI momentum gate for today
            gate_weights = self.gate.get_weights(current)

            is_risk_off = gate_weights.get("_risk_off", False)
            risk_on_count = sum(1 for k, v in gate_weights.items()
                                if not k.startswith("_") and v > 0)

            if is_risk_off:
                risk_off_days += 1

            # Step 3: Generate signals (only risk-on tokens pass through)
            signals      = self._run_model_for_date(current, gate_weights)
            total_signals += len(signals)
            held_symbols = {t["symbol"] for t in self.open_trades}

            entered = 0
            for sig in signals:
                if sig["symbol"] not in held_symbols:
                    self._open_trade(sig, current)
                    held_symbols.add(sig["symbol"])
                    entered += 1

            if day_count % 7 == 0 or current == end_date:
                gate_status = "RISK_OFF" if is_risk_off else f"ON({risk_on_count})"
                print(
                    f"  {current}  open={len(self.open_trades):>3}  "
                    f"closed={len(self.closed_trades):>4}  "
                    f"signals={len(signals)}  entered={entered}  "
                    f"gate={gate_status}",
                    file=sys.stderr,
                )

            current += timedelta(days=1)

        # Close remaining open positions
        for trade in self.open_trades:
            candle     = self._get_candle(trade["symbol"], end_date)
            exit_price = candle["close"] if candle else trade["entry_price"]
            entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d").date()
            hold_days  = (end_date - entry_date).days
            self._close_trade(trade, exit_price, str(end_date), hold_days, "END")

        self.open_trades = []

        # Gate stats
        print(f"\n  Gate stats: {risk_off_days}/{day_count} days BTC risk-off "
              f"({risk_off_days/max(day_count,1)*100:.0f}%)", file=sys.stderr)

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

        gate_on  = [t for t in trades if t.get("rsi_gate")]
        gate_off = [t for t in trades if not t.get("rsi_gate")]
        pnls_on  = [t["pnl_pct"] or 0 for t in gate_on]
        pnls_off = [t["pnl_pct"] or 0 for t in gate_off]

        print()
        print("=" * 60)
        print("  TRADE SUMMARY (Data Gathering)")
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
        print(f"  --- RSI Gate breakdown ---")
        print(f"  gate=True:      {len(gate_on)} trades, "
              f"avg P&L {np.mean(pnls_on):+.2f}%" if pnls_on else "  gate=True:      0 trades")
        print(f"  gate=False:     {len(gate_off)} trades, "
              f"avg P&L {np.mean(pnls_off):+.2f}%" if pnls_off else "  gate=False:     0 trades")
        print()

        # Trade journal table
        print(f"  {'SYM':<7} {'ENTRY':>12} {'ENTRY $':>10} {'EXIT':>12} {'EXIT $':>10}"
              f" {'P&L%':>7} {'DAYS':>4} {'REASON':>7} {'R:R':>5} {'GATE':>5} {'RSI_M':>5}")
        print("  " + "-" * 95)
        for t in sorted(trades, key=lambda t: t["entry_date"]):
            rsi_m = t.get("rsi_momentum_rsi")
            rsi_str = f"{rsi_m:5.1f}" if rsi_m is not None else "  N/A"
            gate_str = "  T" if t.get("rsi_gate") else "  F"
            print(
                f"  {t['symbol']:<7} {t['entry_date']:>12} {t['entry_price']:>10.4f}"
                f" {t['exit_date']:>12} {t['exit_price']:>10.4f}"
                f" {(t['pnl_pct'] or 0):>+6.1f}% {t['hold_days']:>4}d"
                f" {t['exit_reason']:>7} {t['rr']:>5.2f}"
                f" {gate_str:>5} {rsi_str}"
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
        print(f"  Exported {len(trades)} trades -> {path}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SR model data gatherer with RSI momentum flag",
        epilog=(
            "Examples:\n"
            "  python3 backtest_roro.py --days 90 --data-dir data49 --tokens 49\n"
            "  python3 backtest_roro.py --days 90 --data-dir data49 --tokens 49 --csv results_roro.csv\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days",       type=int,   default=90,    help="Backtest period in days (default: 90)")
    parser.add_argument("--tokens",     type=int,   default=49,    help="Max tokens to analyze (default: 49)")
    parser.add_argument("--min-rr",     type=float, default=2.0,   help="Minimum R:R to enter a trade (default: 2.0)")
    parser.add_argument("--data-dir",   type=str,   default="data",help="Directory with cached CSV files")
    parser.add_argument("--csv",        type=str,   default=None,  help="Export trade journal to CSV")
    # RSI Momentum gate parameters (informational columns)
    parser.add_argument("--rsi-period",  type=int,   default=10,   help="RSI lookback period (default: 10)")
    parser.add_argument("--rsi-thresh",  type=float, default=60.0, help="RSI threshold for gate=True (default: 60)")
    parser.add_argument("--btc-thresh",  type=float, default=50.0, help="BTC RSI floor for gate=True (default: 50)")
    parser.add_argument("--btc-symbol",  type=str,   default="BTC",help="Symbol used for BTC regime filter (default: BTC)")
    parser.add_argument("--hysteresis",  type=float, default=2.0,  help="RSI gap to trigger asset switch (default: 2.0)")
    parser.add_argument("--gate-top-n",  type=int,   default=3,    help="Top-N for gate logic (default: 3)")
    parser.add_argument("--max-weight",  type=float, default=0.5,  help="Max single asset weight (default: 0.5)")
    args = parser.parse_args()

    # Discover tokens from cached data files
    files = glob.glob(os.path.join(args.data_dir, "*_daily.csv"))
    all_symbols = sorted([os.path.basename(f).replace("_daily.csv", "") for f in files])

    if not all_symbols:
        print(f"No data in {args.data_dir}/. Run fetch_daily.py first.", file=sys.stderr)
        sys.exit(1)

    symbols = all_symbols[:args.tokens]
    print(f"  Backtesting {len(symbols)} tokens over {args.days} days (data gathering)", file=sys.stderr)

    # Determine date range
    sample_df = load_from_cache(symbols[0], args.data_dir)
    if sample_df is None:
        print("Cannot determine date range.", file=sys.stderr)
        sys.exit(1)

    end_date   = sample_df.index[-1].date()
    start_date = end_date - timedelta(days=args.days)

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

    # Build the RSI Momentum gate
    gate = RSIMomentumGate(
        rsi_period=args.rsi_period,
        threshold=args.rsi_thresh,
        btc_rsi_threshold=args.btc_thresh,
        btc_symbol=args.btc_symbol,
        hysteresis=args.hysteresis,
        top_n=args.gate_top_n,
        max_single_weight=args.max_weight,
    )

    bt = BacktesterRORI(args.data_dir, symbols, min_rr=args.min_rr, gate=gate)
    bt.run(start_date, end_date)
    bt.print_summary()

    if args.csv:
        bt.export_csv(args.csv)


if __name__ == "__main__":
    main()
