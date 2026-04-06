"""
Modular entry filters for the S/R analysis engine.
====================================================
Each filter is an independent function that returns (passed: bool, reason: str).
Filters can be composed via FilterChain to gate trade entries.

Usage:
    from core.filters import FilterChain, btc_rsi_floor, token_rsi_momentum

    chain = FilterChain([btc_rsi_floor, token_rsi_momentum])
    passed, reasons = chain.check(df, btc_df=btc_df)

All filters accept a DataFrame with OHLCV data and return:
    (True, "")       — filter passed
    (False, "reason") — filter failed with explanation
"""

import numpy as np
import pandas as pd
from typing import List, Tuple, Callable, Optional


# ── Indicator helpers ────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = 10) -> float:
    """Compute RSI on the last `period` bars."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def compute_adx_di(df: pd.DataFrame, period: int = 14) -> dict:
    """Compute ADX, DI+, DI- on the last `period` bars.

    Returns dict with keys: adx, di_plus, di_minus.
    """
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)

    if n < period + 1:
        return {"adx": 0.0, "di_plus": 0.0, "di_minus": 0.0}

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)

    for i in range(1, n):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
        plus_dm[i] = h_diff if h_diff > l_diff and h_diff > 0 else 0
        minus_dm[i] = l_diff if l_diff > h_diff and l_diff > 0 else 0

    # Smoothed averages (Wilder's method)
    atr = np.zeros(n)
    smooth_plus = np.zeros(n)
    smooth_minus = np.zeros(n)

    atr[period] = np.mean(tr[1:period + 1])
    smooth_plus[period] = np.mean(plus_dm[1:period + 1])
    smooth_minus[period] = np.mean(minus_dm[1:period + 1])

    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
        smooth_plus[i] = (smooth_plus[i - 1] * (period - 1) + plus_dm[i]) / period
        smooth_minus[i] = (smooth_minus[i - 1] * (period - 1) + minus_dm[i]) / period

    # DI+ and DI-
    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = np.where(atr > 0, 100 * smooth_plus / atr, 0)
        di_minus = np.where(atr > 0, 100 * smooth_minus / atr, 0)
        di_sum = di_plus + di_minus
        dx = np.where(di_sum > 0, 100 * np.abs(di_plus - di_minus) / di_sum, 0)

    adx = np.zeros(n)
    if n > 2 * period:
        adx[2 * period] = np.mean(dx[period + 1:2 * period + 1])
        for i in range(2 * period + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return {
        "adx": float(adx[-1]),
        "di_plus": float(di_plus[-1]),
        "di_minus": float(di_minus[-1]),
    }


# ── Individual filters ──────────────────────────────────────

def btc_rsi_floor(df: pd.DataFrame, btc_df: Optional[pd.DataFrame] = None,
                  period: int = 10, threshold: float = 50.0,
                  **kwargs) -> Tuple[bool, str]:
    """BTC RSI(10) >= threshold — market floor.

    Uses btc_df if provided, otherwise assumes df IS BTC.
    """
    src = btc_df if btc_df is not None else df
    rsi = compute_rsi(src["close"], period)
    if rsi >= threshold:
        return True, ""
    return False, f"BTC RSI({period})={rsi:.1f} < {threshold}"


def token_rsi_momentum(df: pd.DataFrame, period: int = 10,
                       threshold: float = 60.0,
                       **kwargs) -> Tuple[bool, str]:
    """Token RSI(10) > threshold — momentum confirmation."""
    rsi = compute_rsi(df["close"], period)
    if rsi > threshold:
        return True, ""
    return False, f"RSI({period})={rsi:.1f} <= {threshold}"


def rsi_cap(df: pd.DataFrame, period: int = 10,
            threshold: float = 80.0,
            **kwargs) -> Tuple[bool, str]:
    """Token RSI(10) <= threshold — avoid exhaustion / overbought entries."""
    rsi = compute_rsi(df["close"], period)
    if rsi <= threshold:
        return True, ""
    return False, f"RSI({period})={rsi:.1f} > {threshold} (overbought)"


def token_adx_trend(df: pd.DataFrame, period: int = 14,
                    threshold: float = 20.0,
                    **kwargs) -> Tuple[bool, str]:
    """Token ADX(14) > threshold — trend exists."""
    indicators = compute_adx_di(df, period)
    adx = indicators["adx"]
    if adx > threshold:
        return True, ""
    return False, f"ADX({period})={adx:.1f} <= {threshold}"


def token_di_bullish(df: pd.DataFrame, period: int = 14,
                     **kwargs) -> Tuple[bool, str]:
    """Token DI+ > DI- — trend is bullish."""
    indicators = compute_adx_di(df, period)
    di_plus = indicators["di_plus"]
    di_minus = indicators["di_minus"]
    if di_plus > di_minus:
        return True, ""
    return False, f"DI+={di_plus:.1f} <= DI-={di_minus:.1f} (bearish)"


def min_risk_reward(df: pd.DataFrame, raw_rr: float = 0.0,
                    threshold: float = 1.2,
                    **kwargs) -> Tuple[bool, str]:
    """R:R ratio >= threshold — minimum risk/reward to qualify.

    raw_rr must be passed as a kwarg (computed by TPSL module).
    """
    if raw_rr >= threshold:
        return True, ""
    return False, f"R:R={raw_rr:.2f} < {threshold}"


# ── MT SMA Regime Gate ──────────────────────────────────────
# Single-timeframe regime filter using SMA40 + slope over 20 bars.
# Gate OFF when regime is D (downtrend: price < SMA AND SMA falling).
#
# Walk-forward validated on 5y data (BTC, ETH, SOL), 12m IS / 6m OOS:
#   - SMA40 / slope 20 / confirm 1 beat all MTF combos
#   - Cuts max drawdown ~15% while preserving returns
#   - Strongest edge on ETH (+24% vs +5% no gate)
#
# Regime states:
#   U (Up):    price > SMA40 AND SMA40 rising over 20 bars
#   D (Down):  price < SMA40 AND SMA40 falling over 20 bars
#   T (Trans): price and slope disagree

MT_REGIME_CONFIG = {"sma": 40, "slope_bars": 20, "confirm": 1}


def _raw_regime(close: pd.Series, sma: pd.Series, slope_bars: int) -> pd.Series:
    """Raw per-bar regime signal (no confirmation)."""
    sma_shifted = sma.shift(slope_bars)
    price_above = close > sma
    sma_rising = sma > sma_shifted

    regime = pd.Series("T", index=close.index)
    regime[price_above & sma_rising] = "U"
    regime[~price_above & ~sma_rising] = "D"
    return regime


def _apply_confirmation(raw: pd.Series, confirm_bars: int) -> pd.Series:
    """Debounce: regime switch locks in only after N consecutive bars."""
    confirmed = raw.copy()
    current_regime = "T"
    pending_regime = None
    pending_count = 0

    for i in range(len(raw)):
        signal = raw.iloc[i]

        if signal == current_regime:
            pending_regime = None
            pending_count = 0
            confirmed.iloc[i] = current_regime
        elif signal == pending_regime:
            pending_count += 1
            if pending_count >= confirm_bars:
                current_regime = pending_regime
                pending_regime = None
                pending_count = 0
            confirmed.iloc[i] = current_regime
        else:
            pending_regime = signal
            pending_count = 1
            if confirm_bars <= 1:
                current_regime = signal
            confirmed.iloc[i] = current_regime

    return confirmed


def detect_regime(close: pd.Series, sma_period: int, slope_bars: int,
                  confirm_bars: int = 1) -> str:
    """Detect current regime with confirmation filter."""
    n = len(close)
    if n < sma_period + slope_bars:
        return "T"
    sma = close.rolling(sma_period).mean()
    raw = _raw_regime(close, sma, slope_bars)
    confirmed = _apply_confirmation(raw, confirm_bars)
    return confirmed.iloc[-1]


def detect_regime_series(close: pd.Series, sma_period: int, slope_bars: int,
                         confirm_bars: int = 1) -> pd.Series:
    """Detect confirmed regime for every bar (for charting)."""
    sma = close.rolling(sma_period).mean()
    raw = _raw_regime(close, sma, slope_bars)
    return _apply_confirmation(raw, confirm_bars)


def compute_mt_regime(close: pd.Series) -> Tuple[str, str]:
    """Compute the MT regime from a close price series.

    Returns (regime_state, description).
    """
    cfg = MT_REGIME_CONFIG
    state = detect_regime(close, cfg["sma"], cfg["slope_bars"], cfg["confirm"])
    return state, f"SMA{cfg['sma']} regime: {state}"


def mt_regime_gate(df: pd.DataFrame, **kwargs) -> Tuple[bool, str]:
    """MT SMA Regime Gate — blocks entries when regime is D (downtrend).

    Uses SMA40 with slope measured over 20 bars, no confirmation delay.
    Gate ON when regime is U (uptrend) or T (transition).
    Gate OFF when regime is D (price < SMA40 AND SMA40 falling).
    """
    state, desc = compute_mt_regime(df["close"])
    if state != "D":
        return True, ""
    return False, f"MT gate OFF ({desc})"


# ── Bollinger %B Overbought Filter ──────────────────────────
# Blocks entries when price is in the top 20% of its Bollinger Band.
#
# Walk-forward validated on 49 tokens, 3 OOS folds:
#   - W/L ratio improved from 0.73 to 1.21
#   - Only filter that turned the strategy profitable (+8.7% vs -25%)
#   - Avg loss shrank from -8.1% to -6.8%
#   - MaxDD dropped from 47.6% to 31.4%

def bollinger_pctb(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0,
                   threshold: float = 0.80, **kwargs) -> Tuple[bool, str]:
    """Bollinger %B < threshold — blocks overbought entries.

    %B = (price - lower_band) / (upper_band - lower_band)
    %B > 0.80 means price is in top 20% of Bollinger range → likely to revert.
    """
    close = df["close"]
    if len(close) < period:
        return True, ""  # not enough data, pass
    sma = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]
    if std == 0:
        return True, ""
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    band_width = upper - lower
    if band_width == 0:
        return True, ""
    pctb = float((close.iloc[-1] - lower) / band_width)
    if pctb < threshold:
        return True, ""
    return False, f"BB %B={pctb:.2f} >= {threshold} (overbought)"


# ── Relative Volume Filter ─────────────────────────────────
# Confirms that volume backs the price move.
#
# Walk-forward validated on 49 tokens:
#   - RVOL 1.5-2.0x is the sweet spot for breakout follow-through
#   - Below 1.5x: insufficient conviction behind the move

def relative_volume(df: pd.DataFrame, period: int = 20,
                    threshold: float = 1.5, **kwargs) -> Tuple[bool, str]:
    """Relative volume >= threshold — volume confirms the move.

    RVOL = current volume / SMA(volume, period).
    """
    vol = df["volume"]
    if len(vol) < period + 1:
        return True, ""  # not enough data, pass
    avg_vol = vol.iloc[-(period+1):-1].mean()
    if avg_vol == 0:
        return True, ""
    rvol = float(vol.iloc[-1] / avg_vol)
    if rvol >= threshold:
        return True, ""
    return False, f"RVOL={rvol:.2f} < {threshold}"


# ── Filter registry ─────────────────────────────────────────

AVAILABLE_FILTERS = {
    "btc_rsi_floor": btc_rsi_floor,
    "token_rsi_momentum": token_rsi_momentum,
    "rsi_cap": rsi_cap,
    "token_adx_trend": token_adx_trend,
    "token_di_bullish": token_di_bullish,
    "min_risk_reward": min_risk_reward,
    "mt_regime_gate": mt_regime_gate,
    "bollinger_pctb": bollinger_pctb,
    "relative_volume": relative_volume,
}


# ── FilterChain ─────────────────────────────────────────────

class FilterChain:
    """Compose multiple filters into a chain. All must pass.

    Usage:
        chain = FilterChain(["btc_rsi_floor", "token_adx_trend"])
        passed, reasons = chain.check(df, btc_df=btc_df)

        # Or with function references:
        chain = FilterChain([btc_rsi_floor, token_rsi_momentum])
    """

    def __init__(self, filters: List = None):
        self.filters = []
        for f in (filters or []):
            if isinstance(f, str):
                if f not in AVAILABLE_FILTERS:
                    raise ValueError(f"Unknown filter: {f}. "
                                     f"Available: {list(AVAILABLE_FILTERS.keys())}")
                self.filters.append(AVAILABLE_FILTERS[f])
            elif callable(f):
                self.filters.append(f)
            else:
                raise ValueError(f"Filter must be a string name or callable, got {type(f)}")

    def check(self, df: pd.DataFrame, **kwargs) -> Tuple[bool, List[str]]:
        """Run all filters. Returns (all_passed, list_of_failure_reasons)."""
        failures = []
        for f in self.filters:
            passed, reason = f(df, **kwargs)
            if not passed:
                failures.append(reason)
        return len(failures) == 0, failures

    def check_verbose(self, df: pd.DataFrame, **kwargs) -> dict:
        """Run all filters with full detail."""
        results = {}
        for f in self.filters:
            name = f.__name__
            passed, reason = f(df, **kwargs)
            results[name] = {"passed": passed, "reason": reason}
        all_passed = all(r["passed"] for r in results.values())
        return {"passed": all_passed, "filters": results}
