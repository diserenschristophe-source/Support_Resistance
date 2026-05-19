#!/usr/bin/env python3
"""
fetch.py — Fetch + cache daily OHLCV for a list of tokens.

Thin wrapper around core.fetcher.fetch_and_cache. Mirrors the data-fetch
step of sr-dashboard's main.py so the two repos can stay in sync on a
shared data/ layout.

Usage:
    python3 fetch.py                       # all default tokens
    python3 fetch.py BTC ETH SOL           # specific tokens
    python3 fetch.py --days 730 --force    # full re-download, 2y history
"""

import argparse
import os
import sys
import time

from core import config
from core.fetcher import fetch_and_cache


# Same 51-token list used by chart.py launch config — keeps fetch and chart
# in lockstep so every charted token has guaranteed cache coverage.
DEFAULT_TOKENS = [
    "BTC", "ETH", "SOL", "BNB", "DOGE", "LINK", "NEAR", "SUI", "TAO",
    "ADA", "AVAX", "HBAR", "LTC", "PAXG", "TON", "TRX", "XLM", "XRP",
    "HYPE", "SHIB", "MNT", "UNI", "DOT", "SKY", "ASTER", "AAVE", "PEPE",
    "ONDO", "ICP", "POL", "KAS", "RENDER", "WLD", "QNT", "ATOM", "FIL",
    "ARB", "FET", "APT", "TRUMP", "ALGO", "INJ", "ENA", "VET", "BONK",
    "SEI", "STX", "JUP", "FLOKI", "OP", "BORG",
]


def main():
    parser = argparse.ArgumentParser(description="Fetch + cache daily OHLCV")
    parser.add_argument("tokens", nargs="*", help="Tokens (default: 51-token universe)")
    parser.add_argument("--days", type=int, default=getattr(config, "DEFAULT_DAYS", 365),
                        help="Days of history to request")
    parser.add_argument("--data-dir", type=str, default="data", help="Cache directory")
    parser.add_argument("--force", action="store_true", help="Force full re-download")
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)

    symbols = [t.upper() for t in args.tokens] if args.tokens else DEFAULT_TOKENS
    total = len(symbols)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  FETCH — {total} tokens, {args.days}d, dir={args.data_dir}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    counts = {"skip": 0, "append": 0, "full": 0, "failed": 0}
    failed = []

    for i, symbol in enumerate(symbols):
        result = fetch_and_cache(symbol, args.days, args.data_dir, force=args.force)
        counts[result] = counts.get(result, 0) + 1
        marker = "✓" if result != "failed" else "✗"
        print(f"  [{i+1:3d}/{total}] {marker} {symbol:<8} {result}", file=sys.stderr)
        if result == "failed":
            failed.append(symbol)
        if result in ("append", "full") and i < total - 1:
            time.sleep(getattr(config, "API_RATE_LIMIT_DELAY", 0.25))

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  {counts['skip']} fresh  |  {counts['append']} appended  |  "
          f"{counts['full']} full  |  {counts['failed']} failed", file=sys.stderr)
    if failed:
        print(f"  Failed: {', '.join(failed)}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
