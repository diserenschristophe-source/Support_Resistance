"""
Fetch hourly OHLCV data from Binance with pagination.
======================================================
Binance returns max 1,000 candles per request.
For 365 days of hourly data = 8,760 candles = 9 pages.

Usage:
    python3 research/fetch_hourly.py BTC ETH SOL --days 365
    python3 research/fetch_hourly.py --all --days 365
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)
from core.config import BINANCE_SYMBOL_MAP

DATA_DIR = Path(_PARENT_DIR) / "data"
HOURLY_DIR = DATA_DIR / "hourly"


def fetch_hourly_binance(symbol: str, days: int = 365) -> pd.DataFrame:
    """Fetch hourly candles with pagination."""
    pair = BINANCE_SYMBOL_MAP.get(symbol.upper(), f"{symbol.upper()}USDT")
    url = "https://api.binance.com/api/v3/klines"

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - (days * 24 * 3600 * 1000)

    all_rows = []
    current_start = start_ms
    page = 0

    while current_start < end_ms:
        page += 1
        params = {
            "symbol": pair,
            "interval": "1h",
            "startTime": current_start,
            "limit": 1000,
        }

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{symbol}] Page {page} failed: {e}", file=sys.stderr)
            break

        if not data:
            break

        for candle in data:
            all_rows.append({
                "timestamp": pd.to_datetime(candle[0], unit="ms", utc=True),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
            })

        # Move start to after the last candle
        last_ts = data[-1][0]
        current_start = last_ts + 1

        if len(data) < 1000:
            break  # no more data

        time.sleep(0.1)  # rate limit

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows).set_index("timestamp").sort_index()
    # Remove duplicates
    df = df[~df.index.duplicated(keep="last")]
    return df


def save_hourly(df: pd.DataFrame, symbol: str):
    """Save hourly data to CSV."""
    HOURLY_DIR.mkdir(parents=True, exist_ok=True)
    path = HOURLY_DIR / f"{symbol.upper()}_hourly.csv"
    df_out = df.copy()
    df_out.index.name = "timestamp"
    df_out.reset_index(inplace=True)
    df_out.to_csv(path, index=False)
    return path


def load_hourly(symbol: str) -> pd.DataFrame:
    """Load hourly data from CSV."""
    path = HOURLY_DIR / f"{symbol.upper()}_hourly.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
    return df


def main():
    parser = argparse.ArgumentParser(description="Fetch hourly OHLCV data")
    parser.add_argument("symbols", nargs="*", default=[])
    parser.add_argument("--all", action="store_true",
                        help="Fetch all standard tokens")
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    if args.all:
        symbols = ["BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE",
                    "AVAX", "TAO", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI"]
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        print("Usage: python3 fetch_hourly.py BTC ETH SOL --days 365")
        print("       python3 fetch_hourly.py --all --days 365")
        return

    HOURLY_DIR.mkdir(parents=True, exist_ok=True)
    total_candles = args.days * 24
    pages_needed = (total_candles // 1000) + 1

    print(f"Fetching {args.days} days of hourly data ({total_candles} candles, ~{pages_needed} pages per token)")
    print(f"Output: {HOURLY_DIR}/")
    print()

    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym}...", end="", file=sys.stderr, flush=True)
        df = fetch_hourly_binance(sym, args.days)
        if len(df) > 0:
            path = save_hourly(df, sym)
            days_actual = (df.index[-1] - df.index[0]).days
            print(f" {len(df)} candles ({days_actual} days) -> {path.name}", file=sys.stderr)
        else:
            print(f" FAILED", file=sys.stderr)

        if i < len(symbols):
            time.sleep(0.5)

    print(f"\nDone. Data saved to {HOURLY_DIR}/")


if __name__ == "__main__":
    main()
