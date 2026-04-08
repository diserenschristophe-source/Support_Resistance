"""
regime.py — ADX/RSI Regime Detection from Daily Candles
========================================================

Adapted from the LSR Trading System's regime detection model.
Computes DMI (DI+/DI-/ADX) and RSI from daily OHLCV data to classify
each token into a market regime and provide a momentum score.

Regimes: BULL, BEAR, RANGE, TRANSITION, NEUTRAL, UNDEFINED

Usage:
    from regime import compute_regime
    result = compute_regime(df_daily)  # DataFrame with daily OHLCV
    # result = {'regime': 'BULL', 'confidence': 0.72, 'adx': 34.5, ...}
"""

import numpy as np
import pandas as pd
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Thresholds (from LSR optimized parameters)
# ─────────────────────────────────────────────────────────────

ADX_TREND = 28.92     # ADX above this = trending
ADX_RANGE = 19.02     # ADX below this = ranging
DI_DIFF   = 5.47      # Min |DI+ - DI-| for directional confirmation
BULL_RSI  = 56.40     # RSI above this confirms bullish
BEAR_RSI  = 48.08     # RSI below this confirms bearish

DMI_PERIOD = 14
RSI_PERIOD = 8
ADX_SLOPE_LOOKBACK = 3


# ─────────────────────────────────────────────────────────────
# Wilder Smoothing
# ─────────────────────────────────────────────────────────────

def _wilder_smooth(values, period):
    """
    Wilder's smoothing: smoothed[i] = (smoothed[i-1] * (period-1) + value[i]) / period
    First value is simple average of the first `period` values.
    """
    result = [None] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def _last_valid(lst):
    """Return the last non-None value in a list."""
    for v in reversed(lst):
        if v is not None:
            return v
    return 0


# ─────────────────────────────────────────────────────────────
# Indicator Computation
# ─────────────────────────────────────────────────────────────

def compute_indicators(df):
    """
    Compute ADX, DI+, DI-, RSI from a daily OHLCV DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns: open, high, low, close (and optionally volume).
        Index should be DatetimeIndex.

    Returns
    -------
    dict with: adx, plus_di, minus_di, adx_slope, di_spread_slope, rsi
    or dict with 'error' key if insufficient data.
    """
    df = df.copy()
    df.columns = [c.lower().strip() for c in df.columns]

    if len(df) < DMI_PERIOD + 10:
        return {"error": f"Not enough candles: {len(df)}"}

    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    closes = df["close"].values.astype(float)
    n = len(closes)

    # ── True Range ──────────────────────────────────────────
    tr_list = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    # ── Directional Movement (DM+ / DM-) ───────────────────
    dm_plus, dm_minus = [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        dm_plus.append(max(up, 0) if up > down else 0)
        dm_minus.append(max(down, 0) if down > up else 0)

    # ── Smoothed DM and ATR for DI calculation ──────────────
    sm_dm_plus = _wilder_smooth(dm_plus, DMI_PERIOD)
    sm_dm_minus = _wilder_smooth(dm_minus, DMI_PERIOD)
    sm_atr = _wilder_smooth(tr_list, DMI_PERIOD)

    # ── DI+, DI-, and DX ───────────────────────────────────
    di_plus_list, di_minus_list, dx_list = [], [], []
    for i in range(len(sm_dm_plus)):
        if sm_atr[i] and sm_atr[i] > 0:
            dip = 100 * sm_dm_plus[i] / sm_atr[i]
            dim = 100 * sm_dm_minus[i] / sm_atr[i]
        else:
            dip = dim = 0
        di_plus_list.append(dip)
        di_minus_list.append(dim)
        dsum = dip + dim
        dx_list.append(100 * abs(dip - dim) / dsum if dsum > 0 else 0)

    # ── ADX (Wilder-smoothed DX) ────────────────────────────
    adx_list = _wilder_smooth(dx_list, DMI_PERIOD)

    # ── RSI ─────────────────────────────────────────────────
    gains, losses = [], []
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    rsi_list = [None] * len(gains)
    if len(gains) >= RSI_PERIOD:
        avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
        avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
        for i in range(RSI_PERIOD, len(gains)):
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi_list[i] = 100 - (100 / (1 + rs))

    # ── ADX Slope ───────────────────────────────────────────
    valid_adx = [x for x in adx_list if x is not None]
    adx_slope = (valid_adx[-1] - valid_adx[-1 - ADX_SLOPE_LOOKBACK]
                 if len(valid_adx) >= ADX_SLOPE_LOOKBACK + 1 else 0)

    # ── DI Spread Slope ─────────────────────────────────────
    di_spread_list = [dip - dim for dip, dim in zip(di_plus_list, di_minus_list)]
    di_spread_slope = (di_spread_list[-1] - di_spread_list[-1 - ADX_SLOPE_LOOKBACK]
                       if len(di_spread_list) >= ADX_SLOPE_LOOKBACK + 1 else 0)

    return {
        "adx": round(_last_valid(adx_list), 4),
        "plus_di": round(_last_valid(di_plus_list), 4),
        "minus_di": round(_last_valid(di_minus_list), 4),
        "adx_slope": round(adx_slope, 4),
        "di_spread_slope": round(di_spread_slope, 4),
        "rsi": round(_last_valid(rsi_list), 4),
    }


# ─────────────────────────────────────────────────────────────
# Regime Classification
# ─────────────────────────────────────────────────────────────

def classify_regime(indicators):
    """
    Classify market regime from ADX/RSI indicators.

    Decision tree (from LSR optimized thresholds):
        ADX >= 28.92 AND |DI spread| >= 5.47:
            DI+ > DI- AND RSI > 56.4  → BULL
            DI- > DI+ AND RSI < 48.1  → BEAR
            else                       → NEUTRAL
        ADX >= 28.92 AND |DI spread| < 5.47 → NEUTRAL
        19.02 < ADX < 28.92                 → TRANSITION
        ADX <= 19.02                         → RANGE
        else                                 → UNDEFINED

    Returns dict with: regime, confidence, and all indicator values.
    """
    if "error" in indicators:
        return {"regime": "UNDEFINED", "confidence": 0.0, **indicators}

    adx = indicators["adx"]
    plus_di = indicators["plus_di"]
    minus_di = indicators["minus_di"]
    adx_slope = indicators["adx_slope"]
    rsi = indicators["rsi"]

    signed_di_diff = plus_di - minus_di
    abs_di_diff = abs(signed_di_diff)

    # ── Regime Classification ───────────────────────────────
    if adx >= ADX_TREND and abs_di_diff >= DI_DIFF:
        if signed_di_diff > 0 and rsi > BULL_RSI:
            regime = "BULL"
        elif signed_di_diff < 0 and rsi < BEAR_RSI:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
    elif adx >= ADX_TREND and abs_di_diff < DI_DIFF:
        regime = "NEUTRAL"
    elif ADX_RANGE < adx < ADX_TREND:
        regime = "TRANSITION"
    elif adx <= ADX_RANGE:
        regime = "RANGE"
    else:
        regime = "UNDEFINED"

    # ── Confidence Scoring ──────────────────────────────────
    if regime in ("BULL", "BEAR"):
        adx_conf = min(adx / ADX_TREND, 1.5) / 1.5
        di_conf = min(abs_di_diff / DI_DIFF, 1.5) / 1.5
        base_confidence = (adx_conf + di_conf) / 2
    elif regime == "NEUTRAL":
        base_confidence = min(adx / ADX_TREND, 1.0) * 0.5
    elif regime == "TRANSITION":
        range_span = max(ADX_TREND - ADX_RANGE, 1)
        position = (adx - ADX_RANGE) / range_span
        base_confidence = 0.4 + (position * 0.2)
    elif regime == "RANGE":
        base_confidence = max(1.0 - (adx / max(ADX_RANGE, 1)), 0.1)
    else:
        base_confidence = 0.1

    # ADX slope adjusts confidence
    slope_adj = max(min(adx_slope * 0.05, 0.15), -0.15)
    if regime in ("BULL", "BEAR"):
        confidence = base_confidence + slope_adj
    elif regime == "RANGE":
        confidence = base_confidence - slope_adj
    else:
        confidence = base_confidence

    confidence = min(max(confidence, 0.0), 1.0)

    # ── Direction Indicators ────────────────────────────────
    adx_direction = "up" if adx_slope > 0.5 else "down" if adx_slope < -0.5 else "flat"
    di_direction = "up" if indicators["di_spread_slope"] > 0.5 else \
                   "down" if indicators["di_spread_slope"] < -0.5 else "flat"

    return {
        "regime": regime,
        "confidence": round(confidence, 3),
        "adx": round(adx, 2),
        "adx_slope": round(adx_slope, 3),
        "adx_direction": adx_direction,
        "plus_di": round(plus_di, 2),
        "minus_di": round(minus_di, 2),
        "di_diff": round(signed_di_diff, 2),
        "di_direction": di_direction,
        "rsi": round(rsi, 2),
    }


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def compute_regime(df):
    """
    Full pipeline: compute indicators + classify regime from daily OHLCV.

    Parameters
    ----------
    df : pd.DataFrame with columns: open, high, low, close

    Returns
    -------
    dict with regime, confidence, adx, rsi, di_diff, etc.
    """
    indicators = compute_indicators(df)
    return classify_regime(indicators)


# ─────────────────────────────────────────────────────────────
# CLI test
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from core.fetcher import fetch_data

    symbols = sys.argv[1:] or ["BTC"]
    for sym in symbols:
        try:
            df = fetch_data(sym, days=180)
            result = compute_regime(df)
            print(f"{sym:>6}  regime={result['regime']:<12} conf={result['confidence']:.3f}"
                  f"  ADX={result['adx']:>5.1f} ({result['adx_direction']})"
                  f"  DI={result['di_diff']:>+6.1f} ({result['di_direction']})"
                  f"  RSI={result['rsi']:>5.1f}")
        except Exception as e:
            print(f"{sym:>6}  ERROR: {e}", file=sys.stderr)
