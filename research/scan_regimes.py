#!/usr/bin/env python3
"""Quick diagnostic: what regimes existed across the backtest period?"""

import os, sys, glob
from datetime import datetime, timezone, timedelta
import pandas as pd

# ── Path setup: add parent dir (core modules) to sys.path ────
_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)

from core.fetcher import load_from_cache
from research.regime import compute_regime

data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
days_back = int(sys.argv[2]) if len(sys.argv) > 2 else 60

# Load all tokens
pattern = os.path.join(data_dir, "*_daily.csv")
files = glob.glob(pattern)
symbols = [os.path.basename(f).replace("_daily.csv", "") for f in sorted(files)]

# Determine date range
sample = load_from_cache(symbols[0], data_dir)
end_date = sample.index[-1].date()
start_date = end_date - timedelta(days=days_back)

print(f"Scanning {len(symbols)} tokens from {start_date} to {end_date}\n")

# Check weekly snapshots
check_dates = []
current = start_date
while current <= end_date:
    check_dates.append(current)
    current += timedelta(days=7)

# Header
print(f"{'SYM':<8}", end="")
for d in check_dates:
    print(f" {d.strftime('%m/%d'):>6}", end="")
print(f"  {'BULL':>4} {'TRANS':>5} {'BEAR':>4} {'RANGE':>5}")
print("─" * (8 + 7 * len(check_dates) + 25))

bull_tokens = {}  # sym -> list of dates where BULL

for sym in symbols[:30]:  # top 30
    df_full = load_from_cache(sym, data_dir)
    if df_full is None or len(df_full) < 90:
        continue

    regimes = []
    bull_count = 0
    trans_count = 0
    bear_count = 0
    range_count = 0

    for d in check_dates:
        mask = df_full.index.date <= d
        df_slice = df_full[mask]
        if len(df_slice) < 60:
            regimes.append("?")
            continue

        r = compute_regime(df_slice)
        regime = r.get("regime", "?")
        regimes.append(regime)

        if regime == "BULL":
            bull_count += 1
            if sym not in bull_tokens:
                bull_tokens[sym] = []
            bull_tokens[sym].append(d)
        elif "TRANSITION" in regime:
            trans_count += 1
        elif regime == "BEAR":
            bear_count += 1
        else:
            range_count += 1

    # Print row
    print(f"{sym:<8}", end="")
    for r in regimes:
        tag = r[:5] if len(r) > 5 else r
        if r == "BULL":
            tag = "BULL"
        elif "TRANSITION" in r:
            tag = "TRANS"
        elif r == "BEAR":
            tag = "BEAR"
        elif r == "RANGE":
            tag = "RANGE"
        else:
            tag = r[:5]
        print(f" {tag:>6}", end="")
    print(f"  {bull_count:>4} {trans_count:>5} {bear_count:>4} {range_count:>5}")

print()
if bull_tokens:
    print(f"BULL regime tokens found:")
    for sym, dates in bull_tokens.items():
        date_strs = [d.strftime('%m/%d') for d in dates]
        print(f"  {sym}: {', '.join(date_strs)}")
else:
    print("NO tokens were in BULL regime during this period.")
