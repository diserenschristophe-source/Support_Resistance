#!/usr/bin/env python3
"""
contract_emit.py — SOURCE-LEVEL contract emission for the sr_build model.
=========================================================================
Emits each token's S/R levels under the EXACT field names the frozen TP/SL seam
(core/tpsl.py) + the rank_output.json contract require.

LEVELS = STRUCTURAL BASE ONLY: suppress(categorise(detect(df))), selected exactly
like render_clean.nearest_levels (nearest-first, up to 3 per side, sep-deduped, RAW
pivot prices). The volume re-anchoring / scoring / gating stages
(dual_snap_merge -> score_levels -> step_d gate) are INTENTIONALLY OUT of the level
path — their imports are retained for a later GATE reintroduction, but they no longer
move or tag any level. key_level is the raw structural pivot, never POC-snapped.

VOLUME PROFILE is still computed (build_profile) and reported for DISPLAY context only
(poc / value_area_low / value_area_high — the chart's dotted POC line). It anchors
NOTHING now that dual_snap_merge is gone.

Contract shape (consumed by contract_adapter -> rank_output.json):
  {
    "price": float,                                   # current price
    "market_structure": {atr14, sma20, sma50, sma100, sma200},  # atr14 lives HERE
    "support":    [ {key_level, zone:[lo,hi]}, ... ],            # nearest-first, structural
    "resistance": [ ... same shape ... ],                        # nearest-first, structural
    "volume_profile": {poc, value_area_low(=val), value_area_high(=vah)},  # display only
  }
zone = ATR band [pivot - 0.2*ATR, pivot + 0.2*ATR] (the structural pivot has no
intrinsic band once the gate is out; matches chart.py's own fallback width).

The gate-only fields (role/strength/scale/status/regime_live/low_confidence/
low_conviction) are ABSENT here; contract_adapter (.get) tombstones them null until
the gate is reintroduced.
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
from step_a_profile import build_profile           # USED: volume_profile (display POC/VA)
# Retained for a later GATE reintroduction — currently OUT of the live level path
# (levels are structural-only). Kept imported so reintroduction is a body-only change.
from volume_profile import tactical_profile        # noqa: F401  (gate path, not live)
from step_b_snap import dual_snap_merge            # noqa: F401  (gate path, not live)
from step_c_score import score_levels              # noqa: F401  (gate path, not live)
from step_d_gate import gate                       # noqa: F401  (gate path, not live)


def _smas(close):
    """The SMAs the model already uses (rolling means of close); None if too short."""
    out = {}
    for p in (20, 50, 100, 200):
        out[f"sma{p}"] = float(close.rolling(p).mean().iloc[-1]) if len(close) >= p else None
    return out


def _structural_levels(df, price, atr):
    """render_clean.nearest_levels, replicated inline (do NOT import render_clean — it
    renders 45 charts at import time). Nearest UNBROKEN structural pivots, up to 3 per
    side, each >= sep from those already chosen. Reads ONLY L.price + L.state — no
    volume, no snap, no score, no gate. Split: < price -> support, >= price -> resistance."""
    cat = suppress(categorise(detect(df), df), df)
    pool = [L for L in cat if L.state != "broken"]
    sep = max(0.6 * atr, 0.01 * price)

    def pick(levels):
        chosen = []
        for L in levels:
            if all(abs(L.price - c.price) >= sep for c in chosen):
                chosen.append(L)
            if len(chosen) == 3:
                break
        return chosen

    sup = pick(sorted([L for L in pool if L.price <  price], key=lambda L: price - L.price))
    res = pick(sorted([L for L in pool if L.price >= price], key=lambda L: L.price - price))
    return sup, res


def _contract_level(L, atr):
    """Structural pivot Level -> contract level dict. Only the two keys contract_adapter
    hard-reads: key_level (= raw pivot price, NOT POC-snapped) and zone (ATR band around
    the pivot). The gate fields (role/strength/scale/status/regime_live/low_*) are absent;
    contract_adapter (.get) tombstones them null until the gate is reintroduced."""
    return {
        "key_level": float(L.price),
        "zone": [float(L.price - 0.2 * atr), float(L.price + 0.2 * atr)],
    }


def emit_contract_levels(df, symbol=None):
    """Emit the per-token seam contract — STRUCTURAL-ONLY levels + display-only volume profile.

    LEVELS: the structural base (suppress(categorise(detect(df)))) selected exactly like
    render_clean.nearest_levels. dual_snap_merge / score_levels / gate are OUT of the path
    (imports retained for later reintroduction); key_level is the raw pivot, never snapped.

    VOLUME PROFILE: still computed via build_profile and reported for DISPLAY context
    (poc / value_area_low / value_area_high — the chart's dotted POC line). Anchors nothing.

    support/resistance split by POSITION vs price (key_level < price -> support, >= ->
    resistance) and sorted NEAREST-FIRST (tpsl's cascade breaks on distance in list order)."""
    price = float(df["close"].iloc[-1])
    atr = compute_atr(df)

    sup, res = _structural_levels(df, price, atr)   # structural base only — no snap/score/gate

    macro = build_profile(df)                        # DISPLAY context only (poc/val/vah); anchors nothing

    ms = {"atr14": float(atr)}
    ms.update(_smas(df["close"]))

    out = {
        "price": price,
        "market_structure": ms,
        "support": [_contract_level(L, atr) for L in sup],
        "resistance": [_contract_level(L, atr) for L in res],
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
