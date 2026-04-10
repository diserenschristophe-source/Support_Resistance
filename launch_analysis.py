#!/usr/bin/env python3
"""
launch_analysis.py — Run S/R analysis on a single token and print results.
"""

import json
import sys

from core.fetcher import fetch_data
from core.sr_analysis import analyze_token
from core.tpsl import compute_tp_sl, NumpySafeEncoder


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 365

    print(f"Fetching {days}d of data for {symbol}...")
    df = fetch_data(symbol, days=days)
    print(f"Got {len(df)} candles ({str(df.index[0])[:10]} to {str(df.index[-1])[:10]})")

    print(f"\nRunning S/R analysis...")
    analysis = analyze_token(symbol, df)

    print(f"\nComputing TP/SL...")
    result = compute_tp_sl(analysis)

    print(f"\n{'='*60}")
    print(f"  {symbol} — S/R ANALYSIS")
    print(f"{'='*60}")

    # Market structure
    ms = analysis["market_structure"]
    print(f"\n  Price:     ${analysis['price']:,.2f}")
    print(f"  Trend:     {ms['trend']}")
    print(f"  Structure: {ms['structure']}")
    print(f"  ATR(14):   ${ms['atr14']:,.2f}")
    for label, key in [("SMA 20", "sma20"), ("SMA 50", "sma50"),
                       ("SMA 100", "sma100"), ("SMA 200", "sma200")]:
        val = ms.get(key)
        if val:
            print(f"  {label}:   ${val:,.2f}")

    # Volume profile
    vp = analysis["volume_profile"]
    if vp.get("poc"):
        print(f"\n  Volume Profile:")
        print(f"    POC: ${vp['poc']:,.2f}")
        if vp.get("value_area_low"):
            print(f"    VA:  ${vp['value_area_low']:,.2f} - ${vp['value_area_high']:,.2f}")

    # Resistance zones
    print(f"\n  Resistance Zones:")
    for r in analysis.get("resistance", []):
        notes = f"  ({r['notes']})" if r.get("notes") else ""
        print(f"    [{r['tier']}] ${r['key_level']:,.2f}  "
              f"(+{r['distance_pct']:.1f}%, conf={r['confluence']}, "
              f"touches={r['touches']}){notes}")

    # Support zones
    print(f"\n  Support Zones:")
    for s in analysis.get("support", []):
        notes = f"  ({s['notes']})" if s.get("notes") else ""
        print(f"    [{s['tier']}] ${s['key_level']:,.2f}  "
              f"(-{s['distance_pct']:.1f}%, conf={s['confluence']}, "
              f"touches={s['touches']}){notes}")

    # TP/SL
    if result and result.get("take_profit") and result.get("stop_loss"):
        print(f"\n  TP/SL:")
        print(f"    Take Profit: ${result['take_profit']:,.2f}  "
              f"(+{result['potential_gain_pct']:.1f}%)")
        print(f"    Stop Loss:   ${result['stop_loss']:,.2f}  "
              f"(-{result['potential_loss_pct']:.1f}%)")
        print(f"    R:R ratio:   {result['raw_rr']:.2f}")
        print(f"    Support Confidence:       {result['support_confidence']:.2f}")
        print(f"    Resistance Permeability:  {result['resistance_permeability']:.2f}")
    else:
        reason = result.get("reason", "unknown") if result else "no result"
        print(f"\n  TP/SL: incomplete ({reason})")

    # Scenarios
    scenarios = analysis.get("scenarios", {})
    if scenarios.get("bullish"):
        print(f"\n  Bullish Scenario:")
        for k, v in scenarios["bullish"].items():
            print(f"    {k}: ${v:,.2f}")
    if scenarios.get("bearish"):
        print(f"\n  Bearish Scenario:")
        for k, v in scenarios["bearish"].items():
            print(f"    {k}: ${v:,.2f}")

    # Triggers
    triggers = analysis.get("triggers", {})
    if triggers:
        print(f"\n  Triggers:")
        for k, v in triggers.items():
            print(f"    {k}: ${v:,.2f}")

    print(f"\n{'='*60}")

    # Also dump full JSON for programmatic use
    print(f"\nFull JSON output written to {symbol.lower()}_analysis.json")
    with open(f"{symbol.lower()}_analysis.json", "w") as f:
        json.dump({"analysis": analysis, "tpsl": result}, f,
                  indent=2, cls=NumpySafeEncoder)


if __name__ == "__main__":
    main()
