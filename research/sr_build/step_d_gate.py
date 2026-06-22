#!/usr/bin/env python3
"""
STEP D — regime gate, roles, and suppression.
=============================================
Turns the dual-scale scored levels (B+C) into live, role-assigned levels.

STATE (three inputs, price-vs-value is the decider, label + modality are context):
  * price-vs-value : in-value (VAL<=price<=VAH) / below-value / above-value.
  * label          : balance (wr-transition) | trend, from Step A.
  * modality       : unimodal / bimodal / multimodal, from the Step A HVN count.
Two load-bearing rules from the checkpoints:
  1. The label alone never sets the state — price-vs-value overrides what the label
     implies (HYPE: far POC but price inside value reads in-value).
  2. A balance label does not guarantee a fadeable range — fade needs balance AND a
     value area that is actually tight (BNB/HYPE wide-VA balance get NO fade; they
     fall back to price-vs-value and are marked low-conviction).

ROLES (exactly one per level): magnet (POC) / fade (VAH-VAL, only in-value+tight) /
breakout (LVN, only out-of-value) / target (next shelf in the direction of travel,
broken edges, low-conviction references, naked POCs).

SUPPRESSION: top few per side PER SCALE above a fixed score floor; the rest are
dormant (stored, role+strength kept, not drawn). low_confidence levels keep the
lower strength they already carry from B+C and are flagged, never ranked as clean.

Every threshold is an ATR fraction / ratio; the draw caps are fixed display
constants. No literal price/bar/bin count or token name in the logic.
"""
from collections import defaultdict

from levels_config import MAX_LINES_PER_SIDE_PER_SCALE   # display knob (not a logic gate)

VA_TIGHT_PCT = 0.10    # value area is "tight" (fadeable) below this % of price
SCORE_FLOOR  = 0.10    # a level must clear this normalized strength ratio to be drawn


def compute_state(tp, price):
    """price-vs-value + label + modality -> one state. tp is the Step A macro profile."""
    if price < tp.val:
        pvv = "below-value"
    elif price > tp.vah:
        pvv = "above-value"
    else:
        pvv = "in-value"
    n_hvn = len(tp.hvn)
    modality = "unimodal" if n_hvn <= 1 else ("bimodal" if n_hvn == 2 else "multimodal")
    va_width_pct = (tp.vah - tp.val) / price if price else 0.0
    tight = va_width_pct <= VA_TIGHT_PCT
    balance = (tp.regime == "balance")

    if pvv == "below-value":
        state = "below-value"
    elif pvv == "above-value":
        state = "above-value"
    else:                                            # in-value
        state = "in-value-tight" if (balance and tight) else "in-value-weak"
    low_conviction = state == "in-value-weak"     # weak STATE only; thin-profile is low_confidence
    return {"price_vs_value": pvv, "label": tp.regime, "modality": modality,
            "va_width_pct": va_width_pct, "tight": tight, "state": state,
            "low_conviction": low_conviction}


def _vtype(methods):
    for t in ("poc", "vah", "val", "lvn"):
        if t in methods:
            return t
    return "shelf"


def _role_live(level, state, price):
    """Return (role, regime_live) for one level under the state."""
    vt = _vtype(level["methods"])
    if vt == "poc":
        return "magnet", True                                     # fair-value magnet, all states
    if vt == "lvn":
        return "breakout", state in ("below-value", "above-value")   # trapdoor, only out-of-value
    if vt in ("vah", "val"):
        if state == "in-value-tight":
            return "fade", True                                   # fade the edge back to POC
        if state == "below-value" and vt == "val":
            return "target", True                                 # lost support -> resistance retest
        if state == "above-value" and vt == "vah":
            return "target", True                                 # broken resistance -> support retest
        return "fade", False                                      # stale / wide-VA edge -> dormant
    # structural shelf
    if state == "below-value" and level["price"] <= price:
        return "target", True                                     # next support down (continuation)
    if state == "above-value" and level["price"] >= price:
        return "target", True                                     # next resistance up (continuation)
    if state == "in-value-weak":
        return "target", True                                     # low-conviction reference
    return "target", False


def gate(scored, tp, price):
    """Assign role + live/dormant to every scored level. Returns (state, levels)."""
    st = compute_state(tp, price)
    out = []
    for L in scored:
        role, regime_live = _role_live(L, st["state"], price)
        d = dict(L)
        d["role"] = role
        d["regime_live"] = regime_live
        d["low_confidence"] = bool(L.get("low_confidence"))   # thin profile (from B+C)
        d["low_conviction"] = bool(st["low_conviction"])      # weak state (wide-VA / trend-in-value)
        out.append(d)

    # suppression: top-N per (side, scale) above the floor, among regime-live levels
    groups = defaultdict(list)
    for i, d in enumerate(out):
        if d["regime_live"] and d["strength"] >= SCORE_FLOOR:
            groups[(d["side"], d["scale"])].append((d["strength"], i))
    drawn = set()
    for lst in groups.values():
        for _, i in sorted(lst, key=lambda x: x[0], reverse=True)[:MAX_LINES_PER_SIDE_PER_SCALE]:
            drawn.add(i)
    for i, d in enumerate(out):
        d["status"] = "live" if i in drawn else "dormant"
    return st, out
