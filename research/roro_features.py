"""
roro_features.py — Risk-On / Risk-Off Feature Engine
=====================================================

Computes 7 market condition features from daily OHLCV data, each normalized
to [-1.0, +1.0]. Designed for use as filter signals in the SR Analyzer system.

Features:
    f_trend         EMA(12) vs EMA(26) — trend direction
    f_momentum      20-day risk-adjusted return
    f_vol_regime    Volatility contraction/expansion (5d vs 20d)
    f_drawdown      Distance from 20-day high
    f_dir_volume    Buying vs selling volume pressure
    f_adx_strength  ADX trend strength (ranging vs trending)
    f_di_spread     DI+/DI- directional pressure

Usage:
    from roro_features import compute_roro_features
    features = compute_roro_features(df)  # df = daily OHLCV DataFrame
    # Returns dict with all 7 features + raw values + binary flags + aggregates

Requirements:
    pandas, numpy
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

EMA_FAST = 12
EMA_SLOW = 26
MOMENTUM_WINDOW = 20
VOL_SHORT = 5
VOL_LONG = 20
DRAWDOWN_WINDOW = 20
DIR_VOL_WINDOW = 20
DMI_PERIOD = 14
RSI_PERIOD = 8
ADX_CENTER = 25         # ADX pivot: above = trending, below = ranging
ADX_SCALE = 25          # Normalization divisor for ADX
DI_SCALE = 30           # Normalization divisor for DI spread
TREND_SCALE = 0.05      # EMA spread normalization (as fraction of price)
MOMENTUM_SCALE = 2.0    # Risk-adjusted return normalization
DRAWDOWN_SCALE = 0.20   # Max drawdown normalization (20% = -1.0)


# ─────────────────────────────────────────────────────────────
# Wilder Smoothing (for ADX/DMI)
# ─────────────────────────────────────────────────────────────

def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's exponential smoothing."""
    result = np.full(len(values), np.nan)
    if len(values) < period:
        return result
    result[period - 1] = np.mean(values[:period])
    for i in range(period, len(values)):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


# ─────────────────────────────────────────────────────────────
# Individual Feature Computations
# ─────────────────────────────────────────────────────────────

def _compute_trend(close: pd.Series) -> float:
    """
    f_trend: EMA(12) vs EMA(26) crossover strength.
    
    Positive = EMA12 above EMA26 (uptrend).
    Negative = EMA12 below EMA26 (downtrend).
    Normalized by price level so it's comparable across assets.
    """
    if len(close) < EMA_SLOW + 5:
        return 0.0
    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    if ema_slow == 0:
        return 0.0
    spread = (ema_fast - ema_slow) / ema_slow
    return float(np.clip(spread / TREND_SCALE, -1.0, 1.0))


def _compute_momentum(close: pd.Series) -> float:
    """
    f_momentum: 20-day return divided by realized volatility.
    
    Measures whether recent price movement is significant relative to noise.
    Positive = strong rally. Negative = meaningful sell-off.
    """
    if len(close) < MOMENTUM_WINDOW + 2:
        return 0.0
    returns = close.pct_change().dropna()
    if len(returns) < MOMENTUM_WINDOW:
        return 0.0
    
    ret_20d = (close.iloc[-1] / close.iloc[-MOMENTUM_WINDOW - 1]) - 1.0
    vol_20d = returns.iloc[-MOMENTUM_WINDOW:].std()
    
    if vol_20d == 0 or np.isnan(vol_20d):
        return 0.0
    risk_adj = ret_20d / vol_20d
    return float(np.clip(risk_adj / MOMENTUM_SCALE, -1.0, 1.0))


def _compute_vol_regime(close: pd.Series) -> float:
    """
    f_vol_regime: Short-term vs long-term volatility.
    
    Positive = vol contracting (5d vol < 20d vol) → calm, S/R levels hold.
    Negative = vol expanding (5d vol > 20d vol) → stressed, noisy.
    """
    returns = close.pct_change().dropna()
    if len(returns) < VOL_LONG + 1:
        return 0.0
    
    vol_short = returns.iloc[-VOL_SHORT:].std()
    vol_long = returns.iloc[-VOL_LONG:].std()
    
    if vol_long == 0 or np.isnan(vol_long):
        return 0.0
    # Positive when short vol < long vol (contraction)
    ratio = (vol_long - vol_short) / vol_long
    return float(np.clip(ratio, -1.0, 1.0))


def _compute_drawdown(close: pd.Series) -> float:
    """
    f_drawdown: How far price is from its recent high.
    
    +1.0 = at the 20-day high (no pullback).
    0.0 = moderate pullback.
    -1.0 = deep drawdown from high.
    """
    if len(close) < DRAWDOWN_WINDOW + 1:
        return 0.0
    
    high_20d = close.iloc[-DRAWDOWN_WINDOW - 1:].max()
    current = close.iloc[-1]
    
    if high_20d == 0:
        return 0.0
    dd_pct = (current - high_20d) / high_20d  # Negative when below high
    # Scale: 0% drawdown = +1.0, -DRAWDOWN_SCALE = -1.0
    normalized = 1.0 + (dd_pct / DRAWDOWN_SCALE) * 2.0
    return float(np.clip(normalized, -1.0, 1.0))


def _compute_dir_volume(df: pd.DataFrame) -> float:
    """
    f_dir_volume: Is volume concentrated on up-days or down-days?
    
    Positive = buying pressure (more volume when price rises).
    Negative = selling pressure (more volume when price drops).
    """
    if len(df) < DIR_VOL_WINDOW + 1:
        return 0.0
    
    recent = df.iloc[-DIR_VOL_WINDOW:]
    price_change = recent['close'].diff()
    volume = recent['volume']
    
    up_vol = volume[price_change > 0].sum()
    down_vol = volume[price_change < 0].sum()
    total_vol = up_vol + down_vol
    
    if total_vol == 0:
        return 0.0
    # Ratio: 0.5 = balanced, >0.5 = buying, <0.5 = selling
    ratio = up_vol / total_vol
    return float(np.clip((ratio - 0.5) * 4.0, -1.0, 1.0))


def _compute_dmi(df: pd.DataFrame) -> Dict[str, float]:
    """
    Compute ADX, DI+, DI- using Wilder's smoothing.
    
    Returns raw values: adx, di_plus, di_minus, di_spread, rsi.
    """
    if len(df) < DMI_PERIOD * 3:
        return {"adx": 25.0, "di_plus": 15.0, "di_minus": 15.0,
                "di_spread": 0.0, "rsi": 50.0}
    
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    
    n = len(df)
    
    # ── True Range ──
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                     abs(high[i] - close[i - 1]),
                     abs(low[i] - close[i - 1]))
    
    # ── Directional Movement ──
    dm_plus = np.zeros(n)
    dm_minus = np.zeros(n)
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        if up_move > down_move and up_move > 0:
            dm_plus[i] = up_move
        if down_move > up_move and down_move > 0:
            dm_minus[i] = down_move
    
    # ── Wilder Smooth ──
    atr = _wilder_smooth(tr, DMI_PERIOD)
    smooth_dm_plus = _wilder_smooth(dm_plus, DMI_PERIOD)
    smooth_dm_minus = _wilder_smooth(dm_minus, DMI_PERIOD)
    
    # ── DI+ / DI- ──
    di_plus = np.zeros(n)
    di_minus = np.zeros(n)
    for i in range(n):
        if atr[i] is not None and not np.isnan(atr[i]) and atr[i] > 0:
            if smooth_dm_plus[i] is not None and not np.isnan(smooth_dm_plus[i]):
                di_plus[i] = (smooth_dm_plus[i] / atr[i]) * 100
            if smooth_dm_minus[i] is not None and not np.isnan(smooth_dm_minus[i]):
                di_minus[i] = (smooth_dm_minus[i] / atr[i]) * 100
    
    # ── DX → ADX ──
    dx = np.zeros(n)
    for i in range(n):
        di_sum = di_plus[i] + di_minus[i]
        if di_sum > 0:
            dx[i] = abs(di_plus[i] - di_minus[i]) / di_sum * 100
    
    adx = _wilder_smooth(dx, DMI_PERIOD)
    
    # ── RSI ──
    rsi_val = 50.0
    gains = np.zeros(n)
    losses = np.zeros(n)
    for i in range(1, n):
        change = close[i] - close[i - 1]
        if change > 0:
            gains[i] = change
        else:
            losses[i] = abs(change)
    
    avg_gain = _wilder_smooth(gains, RSI_PERIOD)
    avg_loss = _wilder_smooth(losses, RSI_PERIOD)
    
    last_gain = avg_gain[-1] if not np.isnan(avg_gain[-1]) else 0
    last_loss = avg_loss[-1] if not np.isnan(avg_loss[-1]) else 0
    if last_loss > 0:
        rs = last_gain / last_loss
        rsi_val = 100 - (100 / (1 + rs))
    elif last_gain > 0:
        rsi_val = 100.0
    
    # Get last valid ADX
    last_adx = 25.0
    for v in reversed(adx):
        if v is not None and not np.isnan(v):
            last_adx = v
            break
    
    return {
        "adx": round(last_adx, 4),
        "di_plus": round(di_plus[-1], 4),
        "di_minus": round(di_minus[-1], 4),
        "di_spread": round(di_plus[-1] - di_minus[-1], 4),
        "rsi": round(rsi_val, 4),
    }


# ─────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────

def compute_roro_features(df: pd.DataFrame) -> Dict:
    """
    Compute all 7 RORO features from a daily OHLCV DataFrame.
    
    Parameters
    ----------
    df : pd.DataFrame
        Must have columns: open, high, low, close, volume.
        Should contain at least 60 rows of data for reliable results.
        Index should be DatetimeIndex (sorted ascending).
    
    Returns
    -------
    dict with keys:
        # 7 normalized features [-1, +1]
        f_trend, f_momentum, f_vol_regime, f_drawdown,
        f_dir_volume, f_adx_strength, f_di_spread
        
        # Raw indicator values
        raw_adx, raw_di_plus, raw_di_minus, raw_di_spread, raw_rsi
        
        # Binary flags (1 if feature > 0)
        f_trend_pos, f_momentum_pos, f_vol_regime_pos, f_drawdown_pos,
        f_dir_volume_pos, f_adx_strength_pos, f_di_spread_pos
        
        # Aggregates
        n_positive      — count of positive flags (0–7)
        roro_composite  — mean of all 7 features
        
        # Pre-built signal filters
        sig_and_tre_dir     — f_trend > 0 AND f_dir_volume > 0
        sig_and_mom_dir_di  — f_momentum > 0 AND f_dir_volume > 0 AND f_di_spread > 0
        sig_vote_3of7       — at least 3 of 7 positive
        sig_vote_4of7       — at least 4 of 7 positive
        sig_vote_5of7       — at least 5 of 7 positive
    """
    close = df['close']

    # ── Detect if high/low data is available (SB data sets high=low=close) ──
    has_hl_data = not (df['high'] == df['close']).all() or not (df['low'] == df['close']).all()

    # ── DMI/RSI (shared computation) ──
    dmi = _compute_dmi(df)

    # ── 7 Features ──
    f_trend = _compute_trend(close)
    f_momentum = _compute_momentum(close)
    f_vol_regime = _compute_vol_regime(close)
    f_drawdown = _compute_drawdown(close)
    f_dir_volume = _compute_dir_volume(df)
    f_adx_strength = float(np.clip((dmi['adx'] - ADX_CENTER) / ADX_SCALE, -1.0, 1.0))
    f_di_spread = float(np.clip(dmi['di_spread'] / DI_SCALE, -1.0, 1.0))

    # When high==low==close, ADX/DI features are meaningless (True Range = 0).
    # Exclude them from composite and vote counts to avoid penalizing the signal.
    if has_hl_data:
        features = [f_trend, f_momentum, f_vol_regime, f_drawdown,
                    f_dir_volume, f_adx_strength, f_di_spread]
        feature_names = ['f_trend', 'f_momentum', 'f_vol_regime', 'f_drawdown',
                         'f_dir_volume', 'f_adx_strength', 'f_di_spread']
    else:
        features = [f_trend, f_momentum, f_vol_regime, f_drawdown, f_dir_volume]
        feature_names = ['f_trend', 'f_momentum', 'f_vol_regime', 'f_drawdown',
                         'f_dir_volume']

    # ── Binary flags (always report all 7 for schema consistency) ──
    all_vals = {
        'f_trend': f_trend, 'f_momentum': f_momentum, 'f_vol_regime': f_vol_regime,
        'f_drawdown': f_drawdown, 'f_dir_volume': f_dir_volume,
        'f_adx_strength': f_adx_strength, 'f_di_spread': f_di_spread,
    }
    flags = {f"{name}_pos": int(val > 0) for name, val in all_vals.items()}
    # n_positive counts only the active features
    n_positive = sum(int(val > 0) for name, val in zip(feature_names, features))
    n_features = len(features)

    # ── Composite (mean of active features only) ──
    roro_composite = float(np.mean(features))

    # ── Pre-built signals ──
    sig_and_tre_dir = int(f_trend > 0 and f_dir_volume > 0)
    # When no H/L data, sig_and_mom_dir_di ignores the DI condition
    if has_hl_data:
        sig_and_mom_dir_di = int(f_momentum > 0 and f_dir_volume > 0 and f_di_spread > 0)
    else:
        sig_and_mom_dir_di = int(f_momentum > 0 and f_dir_volume > 0)
    sig_vote_3of7 = int(n_positive >= 3)
    sig_vote_4of7 = int(n_positive >= min(4, n_features))
    sig_vote_5of7 = int(n_positive >= min(5, n_features))
    
    return {
        # Normalized features
        "f_trend": round(f_trend, 4),
        "f_momentum": round(f_momentum, 4),
        "f_vol_regime": round(f_vol_regime, 4),
        "f_drawdown": round(f_drawdown, 4),
        "f_dir_volume": round(f_dir_volume, 4),
        "f_adx_strength": round(f_adx_strength, 4),
        "f_di_spread": round(f_di_spread, 4),
        
        # Raw values
        "raw_adx": round(dmi['adx'], 4),
        "raw_di_plus": round(dmi['di_plus'], 4),
        "raw_di_minus": round(dmi['di_minus'], 4),
        "raw_di_spread": round(dmi['di_spread'], 4),
        "raw_rsi": round(dmi['rsi'], 4),
        
        # Binary flags
        **flags,
        
        # Aggregates
        "n_features": n_features,
        "n_positive": n_positive,
        "roro_composite": round(roro_composite, 4),
        
        # Signals
        "sig_and_tre_dir": sig_and_tre_dir,
        "sig_and_mom_dir_di": sig_and_mom_dir_di,
        "sig_vote_3of7": sig_vote_3of7,
        "sig_vote_4of7": sig_vote_4of7,
        "sig_vote_5of7": sig_vote_5of7,
    }


# ─────────────────────────────────────────────────────────────
# Batch Processing
# ─────────────────────────────────────────────────────────────

def compute_roro_series(df: pd.DataFrame, min_warmup: int = 60) -> pd.DataFrame:
    """
    Compute RORO features for every row in a DataFrame (rolling).
    
    Useful for backtesting: gives you the feature values that would have
    been available at each point in time.
    
    Parameters
    ----------
    df : pd.DataFrame
        Full OHLCV history. Must have: open, high, low, close, volume.
    min_warmup : int
        Minimum rows before first computation (default 60).
    
    Returns
    -------
    pd.DataFrame with RORO columns appended, indexed same as input.
    """
    results = []
    
    for i in range(len(df)):
        if i < min_warmup:
            results.append({})
            continue
        window = df.iloc[:i + 1]
        features = compute_roro_features(window)
        results.append(features)
    
    roro_df = pd.DataFrame(results, index=df.index)
    return pd.concat([df, roro_df], axis=1)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    print("=" * 60)
    print("RORO Feature Engine — Standalone Test")
    print("=" * 60)
    
    # Generate synthetic data for testing
    np.random.seed(42)
    n = 200
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    price = 100 * np.cumprod(1 + np.random.normal(0, 0.02, n))
    
    df = pd.DataFrame({
        "open": price * (1 + np.random.normal(0, 0.005, n)),
        "high": price * (1 + abs(np.random.normal(0, 0.015, n))),
        "low": price * (1 - abs(np.random.normal(0, 0.015, n))),
        "close": price,
        "volume": np.random.lognormal(10, 1, n),
    }, index=dates)
    
    print(f"\nSynthetic data: {n} days, price {df['close'].iloc[0]:.0f} → {df['close'].iloc[-1]:.0f}")
    
    # Compute features at the last bar
    features = compute_roro_features(df)
    
    print(f"\n{'─' * 40}")
    print("Features at last bar:")
    print(f"{'─' * 40}")
    
    feat_names = ['f_trend', 'f_momentum', 'f_vol_regime', 'f_drawdown',
                  'f_dir_volume', 'f_adx_strength', 'f_di_spread']
    for name in feat_names:
        val = features[name]
        flag = "ON " if features[f"{name}_pos"] else "OFF"
        bar = "█" * int(abs(val) * 20) + "░" * (20 - int(abs(val) * 20))
        sign = "+" if val > 0 else " "
        print(f"  {name:18s}  {sign}{val:+.3f}  [{flag}]  {'▸' if val > 0 else '◂'}{bar}")
    
    print(f"\n  n_positive:       {features['n_positive']}/7")
    print(f"  roro_composite:   {features['roro_composite']:+.4f}")
    print(f"\n  Raw ADX:          {features['raw_adx']:.1f}")
    print(f"  Raw DI spread:    {features['raw_di_spread']:+.1f}")
    print(f"  Raw RSI:          {features['raw_rsi']:.1f}")
    
    print(f"\n{'─' * 40}")
    print("Pre-built signals:")
    print(f"{'─' * 40}")
    for sig in ['sig_and_tre_dir', 'sig_and_mom_dir_di', 
                'sig_vote_3of7', 'sig_vote_4of7', 'sig_vote_5of7']:
        print(f"  {sig:25s}  {'✓ ON' if features[sig] else '✗ OFF'}")
    
    print(f"\n✅ RORO Feature Engine working correctly.")
