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
from typing import List, Tuple, Optional


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


def token_di_bullish(df: pd.DataFrame, period: int = 14,
                     **kwargs) -> Tuple[bool, str]:
    """Token DI+ > DI- — trend is bullish."""
    indicators = compute_adx_di(df, period)
    di_plus = indicators["di_plus"]
    di_minus = indicators["di_minus"]
    if di_plus > di_minus:
        return True, ""
    return False, f"DI+={di_plus:.1f} <= DI-={di_minus:.1f} (bearish)"


def min_risk_reward(df: pd.DataFrame, raw_rr=None,
                    threshold: float = 1.2,
                    **kwargs) -> Tuple[bool, str]:
    """R:R ratio >= threshold — minimum risk/reward to qualify.

    `raw_rr` is the TPSL-computed R:R, passed as a kwarg by main.py.
    May be None when the cascade returned a partial setup (TP or SL is None).
    Treat None as "not computable" → filter does not pass.
    """
    if raw_rr is None:
        return False, "R:R not computable (partial setup)"
    if raw_rr >= threshold:
        return True, ""
    return False, f"R:R={raw_rr:.2f} < {threshold}"


# ── MTF SMA Regime Gate ─────────────────────────────────────
# Detects regime on 3 timeframes using price position + SMA slope:
#   U (Up):   price > SMA AND SMA rising
#   D (Down): price < SMA AND SMA falling
#   T (Trans): price and slope disagree


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



def compute_regime_sma40(close: pd.Series) -> str:
    """Single-timeframe regime indicator using SMA(40), slope 20, confirm 1.

    Returns "U", "D", or "T".
    """
    return detect_regime(close, sma_period=40, slope_bars=20, confirm_bars=1)


# ── Filter registry ─────────────────────────────────────────

AVAILABLE_FILTERS = {
    "btc_rsi_floor": btc_rsi_floor,
    "token_rsi_momentum": token_rsi_momentum,
    "token_di_bullish": token_di_bullish,
    "min_risk_reward": min_risk_reward,
}


# ── FilterChain ─────────────────────────────────────────────

class FilterChain:
    """Compose multiple filters into a chain. All must pass.

    Usage:
        chain = FilterChain(["btc_rsi_floor", "token_di_bullish"])
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


# ── New filters (v2) ────────────────────────────────────────

def rsi_cap(df: pd.DataFrame, period: int = 10,
            threshold: float = 80.0,
            **kwargs) -> Tuple[bool, str]:
    """Token RSI(10) <= threshold — avoid exhaustion."""
    rsi = compute_rsi(df["close"], period)
    if rsi <= threshold:
        return True, ""
    return False, f"RSI({period})={rsi:.1f} > {threshold} (overbought)"


def relative_volume(df: pd.DataFrame, period: int = 20,
                    threshold: float = 1.5,
                    **kwargs) -> Tuple[bool, str]:
    """RVOL(20) >= threshold — volume confirms the move."""
    vol = df["volume"]
    if len(vol) < period:
        return True, ""
    avg_vol = vol.rolling(period).mean().iloc[-1]
    if avg_vol <= 0:
        return True, ""
    rvol = vol.iloc[-1] / avg_vol
    if rvol >= threshold:
        return True, ""
    return False, f"RVOL={rvol:.2f} < {threshold} (low volume)"


def mt_regime_gate(df: pd.DataFrame, **kwargs) -> Tuple[bool, str]:
    """SMA40 regime gate — block entries in confirmed downtrends.

    Single-timeframe: SMA(40), slope over 20 bars, 1 confirmation bar.
    Gate OFF when regime is D (down). ON when U (up) or T (transition).
    """
    regime = compute_regime_sma40(df["close"])
    if regime != "D":
        return True, ""
    return False, f"Regime=D (SMA40 downtrend)"


# ── Indicator value helpers (for dashboard) ─────────────────

def compute_rvol(volume: pd.Series, period: int = 20) -> float:
    """Compute relative volume."""
    if len(volume) < period:
        return 1.0
    avg_vol = volume.rolling(period).mean().iloc[-1]
    if avg_vol <= 0:
        return 1.0
    return float(volume.iloc[-1] / avg_vol)


# ── Update registry ─────────────────────────────────────────

AVAILABLE_FILTERS.update({
    "rsi_cap": rsi_cap,
    "relative_volume": relative_volume,
    "mt_regime_gate": mt_regime_gate,
})
