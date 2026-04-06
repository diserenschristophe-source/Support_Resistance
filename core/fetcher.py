"""
Data Fetcher — Download & cache daily OHLCV data.
===================================================
Fetches from Binance (primary), GeckoTerminal (DEX), CoinGecko (fallback).
Supports caching with incremental updates.

Usage:
    from core.fetcher import fetch_data, load_from_cache, fetch_and_cache
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import pandas as pd
import requests

from core import config


# ─────────────────────────────────────────────────────────────
# API Fetchers
# ─────────────────────────────────────────────────────────────

def fetch_binance(symbol: str, days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV from Binance public API (no key required)."""
    pair = config.BINANCE_SYMBOL_MAP.get(symbol.upper(), f"{symbol.upper()}USDT")
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": pair, "interval": "1d", "limit": min(days, 1000)}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return None

        rows = []
        for candle in data:
            rows.append({
                "timestamp": pd.to_datetime(candle[0], unit="ms", utc=True),
                "open": float(candle[1]), "high": float(candle[2]),
                "low": float(candle[3]), "close": float(candle[4]),
                "volume": float(candle[5]),
            })
        return pd.DataFrame(rows).set_index("timestamp")
    except Exception as e:
        print(f"[Binance] Failed for {pair}: {e}", file=sys.stderr)
        return None


def fetch_geckoterminal(symbol: str, days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV from GeckoTerminal DEX API with pagination."""
    pool_info = config.GECKOTERMINAL_POOL_MAP.get(symbol.upper())
    if not pool_info:
        return None

    network, pool_address = pool_info
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}/ohlcv/day"
    all_candles = []
    before_ts = None
    max_pages = (days // 180) + 2

    try:
        for page in range(max_pages):
            params = {"limit": 1000, "currency": "usd"}
            if before_ts:
                params["before_timestamp"] = before_ts

            for attempt in range(3):
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code in (401, 429):
                    wait = 5 * (attempt + 1)
                    print(f"[GeckoTerminal] Rate limited (page {page+1}), waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                break

            if resp.status_code != 200:
                break

            ohlcv_list = resp.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if not ohlcv_list:
                break

            all_candles.extend(ohlcv_list)
            before_ts = ohlcv_list[-1][0]
            if len(all_candles) >= days:
                break
            time.sleep(6)

        if not all_candles:
            return None

        # Deduplicate
        seen = set()
        unique = []
        for candle in all_candles:
            if candle[0] not in seen:
                seen.add(candle[0])
                unique.append(candle)

        rows = [{
            "timestamp": pd.to_datetime(c[0], unit="s", utc=True),
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]),
        } for c in unique]

        return pd.DataFrame(rows).set_index("timestamp").sort_index()
    except Exception as e:
        print(f"[GeckoTerminal] Failed for {symbol}: {e}", file=sys.stderr)
        return None


def fetch_coingecko(symbol: str, days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch daily OHLC from CoinGecko free API."""
    coin_id = config.COINGECKO_ID_MAP.get(symbol.upper(), symbol.lower())

    valid_days = [365, 180, 90, 30, 14, 7, 1]
    cg_days = 365
    for vd in valid_days:
        if days >= vd:
            cg_days = vd
            break

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(cg_days)}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return None

        rows = [{
            "timestamp": pd.to_datetime(c[0], unit="ms", utc=True),
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]),
        } for c in data]

        df = pd.DataFrame(rows).set_index("timestamp")

        # Fetch volume separately
        try:
            mkt_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
            mkt_resp = requests.get(mkt_url, params={"vs_currency": "usd", "days": str(days)}, timeout=10)
            mkt_data = mkt_resp.json()
            if "total_volumes" in mkt_data:
                vol_df = pd.DataFrame(mkt_data["total_volumes"], columns=["ts", "volume"])
                vol_df["timestamp"] = pd.to_datetime(vol_df["ts"], unit="ms", utc=True)
                vol_df = vol_df.set_index("timestamp").resample("1D").sum()
                df = df.resample("1D").agg({
                    "open": "first", "high": "max", "low": "min", "close": "last"
                }).dropna()
                df = df.join(vol_df["volume"], how="left")
                df["volume"] = df["volume"].fillna(1.0)
        except Exception:
            df["volume"] = 1.0

        return df
    except Exception as e:
        print(f"[CoinGecko] Failed for {coin_id}: {e}", file=sys.stderr)
        return None


def fetch_data(symbol: str, days: int = 180) -> pd.DataFrame:
    """Auto-detect: Binance → GeckoTerminal → CoinGecko."""
    print(f"[{symbol}] Fetching data...", file=sys.stderr)

    df = fetch_binance(symbol, days)
    if df is not None and len(df) >= 30:
        print(f"[{symbol}] Binance OK — {len(df)} daily candles", file=sys.stderr)
        return df

    if symbol.upper() in config.GECKOTERMINAL_POOL_MAP:
        print(f"[{symbol}] Trying GeckoTerminal...", file=sys.stderr)
        df = fetch_geckoterminal(symbol, days)
        if df is not None and len(df) >= 30:
            print(f"[{symbol}] GeckoTerminal OK — {len(df)} candles", file=sys.stderr)
            return df

    print(f"[{symbol}] Trying CoinGecko fallback...", file=sys.stderr)
    df = fetch_coingecko(symbol, days)
    if df is not None and len(df) >= 30:
        print(f"[{symbol}] CoinGecko OK — {len(df)} candles", file=sys.stderr)
        return df

    raise RuntimeError(
        f"Could not fetch data for {symbol} from Binance, GeckoTerminal, or CoinGecko."
    )


def fetch_from_csv(path: str) -> pd.DataFrame:
    """Load OHLCV from a local CSV file."""
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    for col in ["timestamp", "date", "datetime", "time"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
            df = df.set_index(col)
            break
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        raise ValueError(f"CSV must have columns: {required}. Found: {set(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 1.0
    return df


# ─────────────────────────────────────────────────────────────
# Cache Management
# ─────────────────────────────────────────────────────────────

def get_cache_path(symbol: str, data_dir: str) -> str:
    return os.path.join(data_dir, f"{symbol.upper()}_daily.csv")


def get_cache_status(symbol: str, data_dir: str) -> dict:
    """Check cache freshness."""
    path = get_cache_path(symbol, data_dir)
    if not os.path.exists(path):
        return {"exists": False, "last_date": None, "missing_days": 999, "fresh": False}

    try:
        df = pd.read_csv(path)
        df.columns = [c.lower().strip() for c in df.columns]
        if "timestamp" not in df.columns or len(df) == 0:
            return {"exists": True, "last_date": None, "missing_days": 999, "fresh": False}

        last_ts = pd.to_datetime(df["timestamp"].iloc[-1], utc=True)
        last_date = last_ts.date()
        today = datetime.now(timezone.utc).date()
        missing = (today - last_date).days
        fresh = missing <= 1

        return {"exists": True, "last_date": last_date, "missing_days": missing,
                "fresh": fresh, "rows": len(df)}
    except Exception:
        return {"exists": True, "last_date": None, "missing_days": 999, "fresh": False}


def save_to_cache(df: pd.DataFrame, symbol: str, data_dir: str):
    """Save OHLCV DataFrame to CSV cache."""
    path = get_cache_path(symbol, data_dir)
    df_out = df.copy()
    if isinstance(df_out.index, pd.DatetimeIndex):
        df_out.index.name = "timestamp"
        df_out.reset_index(inplace=True)
    elif "timestamp" not in df_out.columns:
        df_out.insert(0, "timestamp", df_out.index)
    df_out.to_csv(path, index=False)


def load_from_cache(symbol: str, data_dir: str) -> Optional[pd.DataFrame]:
    """Load OHLCV from cache."""
    path = get_cache_path(symbol, data_dir)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df.columns = [c.lower().strip() for c in df.columns]
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp")
        if "volume" not in df.columns:
            df["volume"] = 1.0
        return df
    except Exception:
        return None


def _fetch_incremental_binance(symbol: str, start_date, days_needed: int) -> Optional[pd.DataFrame]:
    """Fetch only missing days from Binance."""
    pair = config.BINANCE_SYMBOL_MAP.get(symbol.upper(), f"{symbol.upper()}USDT")
    start_ms = int(datetime.combine(start_date, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp() * 1000)
    params = {"symbol": pair, "interval": "1d", "startTime": start_ms,
              "limit": min(days_needed + 2, 1000)}
    try:
        resp = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return None
        rows = [{
            "timestamp": pd.to_datetime(c[0], unit="ms", utc=True),
            "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]),
        } for c in data]
        return pd.DataFrame(rows).set_index("timestamp")
    except Exception:
        return None


def fetch_and_cache(symbol: str, days: int, data_dir: str, force: bool = False) -> str:
    """
    Smart fetch with incremental caching.
    Returns: "skip", "append", "full", or "failed".
    """
    status = get_cache_status(symbol, data_dir)

    if not force and status["fresh"]:
        return "skip"

    # Incremental append
    if (not force and status["exists"] and status["last_date"] is not None
        and 1 < status["missing_days"] <= config.MAX_INCREMENTAL_DAYS):

        fetch_from = status["last_date"] - timedelta(days=1)
        new_data = _fetch_incremental_binance(symbol, fetch_from, status["missing_days"] + 2)

        if new_data is not None and len(new_data) > 0:
            cached = load_from_cache(symbol, data_dir)
            if cached is not None:
                last_cached_date = cached.index[-1].date()
                cached = cached[cached.index.date < last_cached_date]
                combined = pd.concat([cached, new_data])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                save_to_cache(combined, symbol, data_dir)
                return "append"

    # Full re-download
    df = fetch_binance(symbol, days)
    if df is None or len(df) < 30:
        time.sleep(0.5)
        df = fetch_binance(symbol, days)

    if df is None or len(df) < 30:
        time.sleep(1.5)
        df = fetch_coingecko(symbol, days)

    if df is None or len(df) < 30:
        return "failed"

    save_to_cache(df, symbol, data_dir)
    return "full"


# ─────────────────────────────────────────────────────────────
# Token Discovery
# ─────────────────────────────────────────────────────────────

def _fetch_stablecoin_symbols() -> set:
    """Fetch stablecoin symbols from CoinGecko."""
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                           params={"vs_currency": "usd", "category": "stablecoins",
                                   "per_page": 250, "page": 1}, timeout=10)
        resp.raise_for_status()
        return {coin["symbol"].upper() for coin in resp.json()}
    except Exception:
        return config.EXCLUDE_SYMBOLS


def get_top_tokens(target: int = 50) -> List[str]:
    """Get top tokens by market cap, validated against Binance."""
    print(f"  Fetching top {target} tradeable tokens by market cap...", file=sys.stderr)

    print("  Fetching stablecoin list from CoinGecko...", file=sys.stderr)
    stablecoins = _fetch_stablecoin_symbols()
    print(f"  Found {len(stablecoins)} stablecoins to exclude", file=sys.stderr)
    time.sleep(1.5)

    try:
        resp = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                           params={"vs_currency": "usd", "order": "market_cap_desc",
                                   "per_page": 250, "page": 1, "sparkline": "false"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[CoinGecko] Failed to fetch market cap list: {e}", file=sys.stderr)
        return config.FALLBACK_TOP_50[:target]

    candidates = []
    seen = set()
    for coin in data:
        sym = coin.get("symbol", "").upper()
        cg_id = coin.get("id", "")
        rank = coin.get("market_cap_rank")

        if cg_id in config.COINGECKO_SYMBOL_FIX:
            sym = config.COINGECKO_SYMBOL_FIX[cg_id]
        if not sym or len(sym) <= 1 or "_" in sym or rank is None:
            continue
        if sym in stablecoins or sym in seen:
            continue
        seen.add(sym)
        candidates.append(sym)

    validated = []
    need_check = []
    for sym in candidates:
        if sym in config.BINANCE_SYMBOL_MAP:
            validated.append(sym)
        else:
            need_check.append(sym)
        if len(validated) >= target:
            break

    if len(validated) < target and need_check:
        to_check = min(len(need_check), target - len(validated) + 10)
        for sym in need_check[:to_check]:
            if len(validated) >= target:
                break
            try:
                r = requests.get("https://api.binance.com/api/v3/klines",
                                params={"symbol": f"{sym}USDT", "interval": "1d", "limit": 1},
                                timeout=5)
                if r.status_code == 200:
                    validated.append(sym)
            except Exception:
                pass
            time.sleep(0.1)

    tokens = validated[:target]

    # Always include pinned tokens
    existing = set(tokens)
    for sym in sorted(config.ALWAYS_INCLUDE):
        if sym not in existing:
            tokens.append(sym)
            print(f"  + {sym} (pinned via ALWAYS_INCLUDE)", file=sys.stderr)

    if len(tokens) < target // 2:
        print(f"  Only {len(tokens)} found — using fallback list", file=sys.stderr)
        tokens = config.FALLBACK_TOP_50[:target]

    return tokens
