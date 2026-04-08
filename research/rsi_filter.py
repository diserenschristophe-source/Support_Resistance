"""
RSI Momentum Filter — Research module for trade entry filtering.
=================================================================
Computes RSI and provides entry signals.

Variants:
  1. Simple: RSI(period) > threshold → enter
  2. Hysteresis: RSI > enter_threshold to open, RSI < exit_threshold to close signal
  3. Top-N: rank tokens by RSI, only trade top N

Usage:
    from research.rsi_filter import compute_rsi, rsi_qualifies
"""

import numpy as np
import pandas as pd
from typing import Optional


def compute_rsi(df: pd.DataFrame, period: int = 10) -> float:
    """Compute RSI for the last bar. Returns 0-100."""
    close = df["close"].values
    if len(close) < period + 1:
        return 50.0  # neutral if not enough data

    deltas = np.diff(close[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_rsi_series(df: pd.DataFrame, period: int = 10) -> pd.Series:
    """Compute RSI for the full series. Returns Series aligned with df index."""
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # Wilder smoothing
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    # After initial SMA, use exponential smoothing
    for i in range(period, len(close)):
        avg_gain.iloc[i] = (avg_gain.iloc[i-1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i-1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def rsi_qualifies(df: pd.DataFrame, period: int = 10, threshold: float = 50) -> bool:
    """Simple filter: does current RSI exceed threshold?"""
    return compute_rsi(df, period) > threshold


def rsi_hysteresis_state(rsi_value: float, prev_state: bool,
                         enter_threshold: float = 60,
                         exit_threshold: float = 50) -> bool:
    """
    Hysteresis filter: once RSI > enter_threshold → signal ON.
    Signal stays ON until RSI < exit_threshold.
    Prevents rapid toggling near a single threshold.
    """
    if prev_state:
        # Currently in signal — stay in until RSI drops below exit
        return rsi_value >= exit_threshold
    else:
        # Not in signal — need RSI above enter to activate
        return rsi_value > enter_threshold


def rank_tokens_by_rsi(tokens_data: dict, period: int = 10, top_n: int = 5) -> list:
    """
    Rank tokens by RSI strength.
    tokens_data: {symbol: DataFrame}
    Returns: list of (symbol, rsi_value) sorted by RSI descending, top N only.
    """
    ranked = []
    for symbol, df in tokens_data.items():
        rsi = compute_rsi(df, period)
        ranked.append((symbol, rsi))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:top_n]
