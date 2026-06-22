#!/usr/bin/env python3
"""
contract_emit.py — SOURCE-LEVEL contract emission for the new sr_build model.
============================================================================
ADDITIVE. This module imports the existing detect -> categorise -> suppress ->
build_profile -> tactical_profile -> dual_snap_merge -> score_levels -> gate chain
(the same chain render_placement.run_pipeline uses) and re-emits each token's
levels under the EXACT field names the frozen TP/SL seam (core/tpsl.py) requires.
Renaming happens here, at the source in sr_build — NOT in a downstream adapter.

It changes nothing: the gate/render output that the charts consume is untouched;
this is a parallel emission only.

STEP 1 contract (renaming + nearest-first ordering + atr/SMAs/price/POC under the
right keys ONLY):
  {
    "price": float,                                   # current price
    "market_structure": {atr14, sma20, sma50, sma100, sma200},  # atr14 lives HERE
    "support":    [ {key_level, zone:[lo,hi], role, strength, scale, status,
                     low_confidence, low_conviction}, ... ],     # nearest-first
    "resistance": [ ... same shape ... ],                        # nearest-first
    "volume_profile": {poc, value_area_low(=val), value_area_high(=vah)},
  }
Mapping: gate level price -> key_level ; gate band -> zone[low,high] ;
         profile vah -> value_area_high ; profile val -> value_area_low.

DELIBERATELY NOT ADDED YET (Step 2 / adapter): tier, touches, confluence,
volume_confirmed, anchor_type, anchor_candle_date, notes, distance_pct.
"""
import sys, os

_HERE = os.path.dirname(os.path.abspath(__file__))
SR = "/Users/xris/GitHub/sr-dashboard"
sys.path.insert(0, SR)
sys.path.insert(0, _HERE)

from core.models import compute_atr
from step1_detect import detect
from step2_categorise import categorise
from step3_suppress import suppress
from step_a_profile import build_profile
from volume_profile import tactical_profile
from step_b_snap import dual_snap_merge
from step_c_score import score_levels
from step_d_gate import gate


def _smas(close):
    """The SMAs the model already uses (rolling means of close); None if too short."""
    out = {}
    for p in (20, 50, 100, 200):
        out[f"sma{p}"] = float(close.rolling(p).mean().iloc[-1]) if len(close) >= p else None
    return out


def _contract_level(L):
    """Gate level dict -> contract level dict. price->key_level, band->zone; natives kept."""
    return {
        "key_level": float(L["price"]),
        "zone": [float(L["band"][0]), float(L["band"][1])],
        "role": L["role"],
        "strength": float(L["strength"]),
        "scale": L["scale"],
        "status": L["status"],
        "regime_live": bool(L["regime_live"]),   # additive: Step-2 adapter carries this through
        "low_confidence": bool(L["low_confidence"]),
        "low_conviction": bool(L["low_conviction"]),
    }


def emit_contract_levels(df, symbol=None):
    """Run the new-model chain on `df` and emit the per-token seam contract (Step 1 shape).

    support/resistance are split by POSITION vs current price (key_level < price ->
    support, >= price -> resistance — the frozen-contract semantics tpsl assumes) and
    sorted NEAREST-FIRST (tpsl's cascade breaks on distance in list order)."""
    price = float(df["close"].iloc[-1])
    atr = compute_atr(df)

    # identical chain to render_placement.run_pipeline (the committed path)
    cat = suppress(categorise(detect(df), df), df)
    structural = [(L.price, L.side) for L in cat if L.state == "live" and L.visible]
    macro = build_profile(df)
    tactical, _ = tactical_profile(df)
    scored = score_levels(
        dual_snap_merge(structural, macro, tactical, df.tail(tactical.lookback), atr, price),
        df, atr,
    )
    _state, leveled = gate(scored, macro, price)

    sup = sorted([L for L in leveled if L["price"] < price], key=lambda L: price - L["price"])
    res = sorted([L for L in leveled if L["price"] >= price], key=lambda L: L["price"] - price)

    ms = {"atr14": float(atr)}
    ms.update(_smas(df["close"]))

    out = {
        "price": price,
        "market_structure": ms,
        "support": [_contract_level(L) for L in sup],
        "resistance": [_contract_level(L) for L in res],
        "volume_profile": {
            "poc": float(macro.poc) if macro.poc is not None else None,
            "value_area_low": float(macro.val) if macro.val is not None else None,    # val -> value_area_low
            "value_area_high": float(macro.vah) if macro.vah is not None else None,   # vah -> value_area_high
        },
    }
    if symbol is not None:
        out["symbol"] = symbol.upper()
    return out


if __name__ == "__main__":
    import json
    from core.fetcher import load_from_cache
    DATA = os.path.join(SR, "data")
    # near-highs (HYPE), mid-range (LINK), clean downtrend (AAVE)
    for sym in ("HYPE", "LINK", "AAVE"):
        df = load_from_cache(sym, DATA)
        if df is None:
            print(f"\n{sym}: no cached data — skip"); continue
        d = emit_contract_levels(df, symbol=sym)
        print("=" * 88)
        print(f"{sym}  (support={len(d['support'])}, resistance={len(d['resistance'])})")
        print(json.dumps(d, indent=2, default=float))
