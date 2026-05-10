#!/usr/bin/env python3
"""
fetch_hourly_missing.py — Top up the hourly OHLCV store.
=========================================================

For each requested token:
  - if data/hourly/<TOKEN>_hourly.csv does NOT exist: fetch full --days back
  - if it exists: fetch incrementally from the last cached hour to now

Use this before running backtest_intraday.py if hourly coverage is stale.

Usage:
  # Top up all tokens needed by both intraday backtest universes
  python3 research/fetch_hourly_missing.py --all

  # Just selected17
  python3 research/fetch_hourly_missing.py --universe selected17

  # Specific tokens
  python3 research/fetch_hourly_missing.py --tokens HYPE TRUMP ENA RENDER

  # Force a fresh full fetch of N days (overwrites)
  python3 research/fetch_hourly_missing.py --tokens HYPE --days 540 --force
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from core.config import BINANCE_SYMBOL_MAP
except Exception:
    BINANCE_SYMBOL_MAP = {}

DATA_DIR = ROOT / "data" / "hourly"
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_hourly_missing")


SELECTED17 = [
    "BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE", "AVAX",
    "TAO", "HYPE", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI",
]

HL44 = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK",
    "AVAX", "SUI", "DOT", "HBAR", "NEAR", "UNI", "LTC", "TAO",
    "HYPE", "AAVE", "TRUMP", "ONDO", "PAXG", "TON", "ICP", "ATOM",
    "RENDER", "FET", "ALGO", "WLD", "INJ", "SEI", "STX", "APT",
    "FIL", "JUP", "TRX", "OP", "ENA", "XLM", "ARB", "POL",
    "SHIB", "PEPE", "BONK", "FLOKI",
]

UNIVERSES = {"selected17": SELECTED17, "hl44": HL44, "all": sorted(set(SELECTED17 + HL44))}

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def symbol_to_pair(sym: str) -> str:
    s = sym.upper().strip()
    # k-prefixed (HL convention) — Binance still uses base symbol on USDT
    if s.startswith("K") and s[1:] in {"SHIB", "PEPE", "BONK", "FLOKI"}:
        s = s[1:]
    if s in BINANCE_SYMBOL_MAP:
        return BINANCE_SYMBOL_MAP[s]
    return f"{s}USDT"


def fetch_range(pair: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch 1h candles in [start_ms, end_ms), paginated."""
    all_rows = []
    cur = start_ms
    page = 0
    while cur < end_ms:
        page += 1
        params = {"symbol": pair, "interval": "1h",
                  "startTime": cur, "endTime": end_ms, "limit": 1000}
        try:
            r = requests.get(BINANCE_KLINES, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("  page %d failed for %s: %s", page, pair, e)
            return pd.DataFrame()
        if not data:
            break
        all_rows.extend(data)
        last_open = int(data[-1][0])
        cur = last_open + 3_600_000   # next hour
        time.sleep(0.15)              # gentle pacing
        if len(data) < 1000:
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "tb_base", "tb_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c])
    return df.set_index("timestamp").sort_index()


def existing_last_ts(path: Path) -> pd.Timestamp | None:
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "timestamp" not in df.columns or len(df) == 0:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df["timestamp"].max()


def topup_token(sym: str, days: int, force: bool = False) -> int:
    """Returns number of new rows appended (or fetched if file didn't exist)."""
    path = DATA_DIR / f"{sym}_hourly.csv"
    pair = symbol_to_pair(sym)
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)

    if force or not path.exists():
        start_ms = end_ms - days * 86_400_000
        log.info("[%s] full fetch — %d days  (pair=%s)", sym, days, pair)
        df = fetch_range(pair, start_ms, end_ms)
        if df.empty:
            log.error("  [%s] no data fetched", sym)
            return 0
        df.to_csv(path, index_label="timestamp")
        log.info("  [%s] wrote %d rows -> %s", sym, len(df), path.name)
        return len(df)

    last = existing_last_ts(path)
    if last is None:
        log.warning("  [%s] existing CSV unreadable; full fetch", sym)
        return topup_token(sym, days, force=True)

    # Fetch from last + 1h
    start_ms = int(last.timestamp() * 1000) + 3_600_000
    if start_ms >= end_ms:
        log.info("[%s] already up to date  (last=%s)", sym, last)
        return 0
    log.info("[%s] incremental — from %s  (pair=%s)", sym, last, pair)
    new = fetch_range(pair, start_ms, end_ms)
    if new.empty:
        log.info("  [%s] nothing new", sym)
        return 0
    # Append and dedupe
    existing = pd.read_csv(path)
    existing.columns = [c.strip().lower() for c in existing.columns]
    existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
    existing = existing.set_index("timestamp")
    merged = pd.concat([existing, new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    merged.to_csv(path, index_label="timestamp")
    log.info("  [%s] appended %d rows  (total=%d)", sym, len(new), len(merged))
    return len(new)


def main() -> int:
    parser = argparse.ArgumentParser(description="Top up data/hourly/*_hourly.csv from Binance")
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()))
    parser.add_argument("--tokens", nargs="+", help="Specific tokens to fetch")
    parser.add_argument("--all", action="store_true", help="Fetch union of selected17 + hl44")
    parser.add_argument("--days", type=int, default=540,
                        help="Days back for full fetch (default: 540)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch full --days, overwrite existing")
    args = parser.parse_args()

    if args.all:
        toks = UNIVERSES["all"]
    elif args.universe:
        toks = UNIVERSES[args.universe]
    elif args.tokens:
        toks = [t.upper() for t in args.tokens]
    else:
        parser.error("Specify --all, --universe, or --tokens")

    log.info("Fetching/topping up %d tokens: %s", len(toks), toks)
    total = 0
    for tok in toks:
        try:
            total += topup_token(tok, args.days, args.force)
        except Exception as e:
            log.error("[%s] failed: %s", tok, e)
        time.sleep(0.2)
    log.info("=" * 60)
    log.info("DONE — total new rows: %d", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
