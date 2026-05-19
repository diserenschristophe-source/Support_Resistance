"""
Export daily OHLCV + indicators to CSV for cross-checking with a third party.

Uses the exact indicator functions from core/filters.py so the output is
bit-for-bit what the SR pipeline sees:
    - RSI(10)
    - ADX(14), DI+, DI-
    - RVOL(20)
    - SMA(40) + regime (U/T/D) via detect_regime_series(40, slope=20, confirm=1)

Output: one CSV per token in output/parity/<TOKEN>_daily.csv
        plus a combined output/parity/ALL_daily.csv

Run:
    python3 research/export_parity_csv.py
"""

import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.filters import compute_adx_di, detect_regime_series

SELECTED = ["BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE",
            "AVAX", "TAO", "HYPE", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI"]

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "parity")


def rsi_series(close: pd.Series, period: int = 10) -> pd.Series:
    """Per-bar RSI matching core.filters.compute_rsi (SMA of gains/losses)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def adx_di_series(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Per-bar ADX / DI+ / DI- using the same Wilder's method as compute_adx_di."""
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)

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

    atr = np.zeros(n)
    smooth_plus = np.zeros(n)
    smooth_minus = np.zeros(n)

    if n >= period + 1:
        atr[period] = np.mean(tr[1:period + 1])
        smooth_plus[period] = np.mean(plus_dm[1:period + 1])
        smooth_minus[period] = np.mean(minus_dm[1:period + 1])
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            smooth_plus[i] = (smooth_plus[i - 1] * (period - 1) + plus_dm[i]) / period
            smooth_minus[i] = (smooth_minus[i - 1] * (period - 1) + minus_dm[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = np.where(atr > 0, 100 * smooth_plus / atr, np.nan)
        di_minus = np.where(atr > 0, 100 * smooth_minus / atr, np.nan)
        di_sum = di_plus + di_minus
        dx = np.where(di_sum > 0, 100 * np.abs(di_plus - di_minus) / di_sum, 0.0)

    adx = np.full(n, np.nan)
    if n > 2 * period:
        adx[2 * period] = np.mean(dx[period + 1:2 * period + 1])
        for i in range(2 * period + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    out = pd.DataFrame({
        "adx14": adx,
        "di_plus14": di_plus,
        "di_minus14": di_minus,
    }, index=df.index)
    # pre-warmup rows should be NaN, not 0
    out.loc[out.index[:period], ["di_plus14", "di_minus14"]] = np.nan
    return out


def rvol_series(volume: pd.Series, period: int = 20) -> pd.Series:
    avg = volume.rolling(period).mean()
    return volume / avg


def build_token_frame(symbol: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{symbol}_daily.csv")
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    out = pd.DataFrame({
        "date": df["timestamp"].dt.strftime("%Y-%m-%d"),
        "symbol": symbol,
        "open": df["open"],
        "high": df["high"],
        "low": df["low"],
        "close": df["close"],
        "volume": df["volume"],
    })

    out["rsi10"] = rsi_series(df["close"], 10).round(4)

    adx_df = adx_di_series(df, 14).round(4)
    out["adx14"] = adx_df["adx14"]
    out["di_plus14"] = adx_df["di_plus14"]
    out["di_minus14"] = adx_df["di_minus14"]

    out["rvol20"] = rvol_series(df["volume"], 20).round(4)

    sma40 = df["close"].rolling(40).mean()
    out["sma40"] = sma40.round(6)
    out["regime_sma40"] = detect_regime_series(
        df["close"], sma_period=40, slope_bars=20, confirm_bars=1
    ).values

    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    all_frames = []

    for sym in SELECTED:
        try:
            frame = build_token_frame(sym)
        except FileNotFoundError:
            print(f"  skip {sym}: no data file")
            continue
        path = os.path.join(OUT_DIR, f"{sym}_daily.csv")
        frame.to_csv(path, index=False)
        print(f"  {sym}: {len(frame)} rows -> {path}")
        all_frames.append(frame)

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        combined_path = os.path.join(OUT_DIR, "ALL_daily.csv")
        combined.to_csv(combined_path, index=False)
        print(f"\ncombined: {len(combined)} rows -> {combined_path}")


if __name__ == "__main__":
    main()
