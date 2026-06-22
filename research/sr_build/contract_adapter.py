#!/usr/bin/env python3
"""
contract_adapter.py — STEP 2 adapter: emit dict -> frozen rank_output per-token schema.
=======================================================================================
Pure transform (no I/O). Input is one token's dict from
contract_emit.emit_contract_levels (Step 1): price, market_structure{atr14, sma20/50/
100/200}, volume_profile{poc, value_area_low, value_area_high}, and support[]/
resistance[] nearest-first with key_level/zone + native fields (role/strength/scale/
status/regime_live/low_confidence/low_conviction).

Output matches the live rank_output.json per-token level/zone schema EXACTLY so the
frozen contract holds: real values where the new model has them, null-but-present
TOMBSTONES where it does not (never fabricated), and the new native fields carried
alongside (they gate NOTHING in the trade path — tpsl reads only key_level + order).

LIVE/DORMANT POLICY (decided): support/resistance stay nearest-first and include ALL
levels regardless of `status`. `status`/`regime_live`/etc. are recorded, not filtered.
"""


def _distance_pct(key_level, price):
    # live rounds distance_pct to 1 decimal (core/sr_analysis2.analyze_token:640/654)
    return round(abs(key_level - price) / price * 100, 1) if price else None


def _volume_confirmed(role):
    """Rule: a level is volume_confirmed when its role derives from a VOLUME-PROFILE
    feature — magnet (POC), fade (value-area edge VAH/VAL), or breakout (LVN). A
    `target` role (structural shelf, or a directional reference) is NOT volume-confirmed.

    The Step-1 emit dict drops the raw `methods`, so this approximates the live
    "methods include poc/vah/val" rule via `role` (which the gate sets 1:1 from the
    volume vtype). KNOWN UNDERCOUNT: a value edge that flipped to a `target` (a broken
    VAH/VAL) reads False here even though it is volume-derived. Exact parity would need
    `methods` carried through emit — deferred (would touch the Step-1 file)."""
    return role in ("magnet", "fade", "breakout")


def _adapt_level(L, price):
    return {
        # ── REQUIRED, real values ──
        "key_level": L["key_level"],                       # load-bearing for tpsl
        "zone": L["zone"],                                 # [low, high]
        "distance_pct": _distance_pct(L["key_level"], price),
        "volume_confirmed": _volume_confirmed(L.get("role")),
        # ── TOMBSTONES (null-but-present; NOT fabricated) ──
        "tier": None,
        "confluence": None,
        "touches": None,
        "anchor_type": None,
        "anchor_candle_date": None,
        "notes": "",
        # ── NEW native fields (additive; gate NOTHING in the trade path) ──
        "role": L.get("role"),
        "strength": L.get("strength"),
        "scale": L.get("scale"),
        "status": L.get("status"),
        "regime_live": L.get("regime_live"),
        "low_confidence": L.get("low_confidence"),
        "low_conviction": L.get("low_conviction"),
    }


def contract_adapter(emit: dict) -> dict:
    """emit (Step 1) -> contract-shaped per-token dict (levels + tombstones + natives +
    nearest_* + second_resistance). Nearest-first order is preserved from emit."""
    price = emit["price"]
    sup = [_adapt_level(L, price) for L in emit.get("support", [])]
    res = [_adapt_level(L, price) for L in emit.get("resistance", [])]

    em = emit.get("market_structure", {}) or {}
    # market_structure: keep the real atr14 + SMAs the model computes; the LABEL fields
    # (trend/structure/bias) the new model does not produce yet -> tombstones (TODO:
    # later map from build_profile.regime or a shared market-structure classifier).
    market_structure = {
        "trend": None, "structure": None, "bias": None,        # tombstones
        "atr14": em.get("atr14"),
        "sma20": em.get("sma20"), "sma50": em.get("sma50"),
        "sma100": em.get("sma100"), "sma200": em.get("sma200"),
    }

    return {
        "symbol": emit.get("symbol"),
        "price": price,
        "market_structure": market_structure,
        "support": sup,
        "resistance": res,
        "nearest_support": sup[0] if sup else None,
        "nearest_resistance": res[0] if res else None,
        # second_resistance = 2nd-nearest resistance above price.
        # CONFIRMED equivalent to live main.py:322-331 (sorts resistances above price
        # ascending, takes index [1]); `res` is nearest-first and all entries are above
        # price, so res[1] is exactly that level. TODO: re-verify if main's selection
        # ever diverges (e.g. mixed-side res list) once the main trace is signed off.
        "second_resistance": res[1] if len(res) > 1 else None,
        "volume_profile": emit.get("volume_profile"),
    }
