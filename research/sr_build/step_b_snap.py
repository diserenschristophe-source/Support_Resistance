#!/usr/bin/env python3
"""
STEP B — snap structure to volume, then merge (dual-scale).
===========================================================
Two confluence scales, merged WITHIN scale only (never across):
  * macro    = Step A regime-anchored profile  -> macro value shelves (far in a trend)
  * tactical = the tight near-price profile     -> near-price value edges

Each structural pivot (steps 1-3, live+visible) is assigned to the scale whose
nearest value feature is closest, then snapped to that scale's nearest volume peak
(POC/HVN) within +/-1 ATR. Each scale's own value features (POC/VAH/VAL/LVN) seed
candidates too. Candidates within 0.5 ATR collapse into one (union of methods,
span as band). A near-price pivot can now merge with a near-price value edge, and
a macro shelf with a wide-window edge — they no longer miss each other for being
on different scales.

All gates are ATR fractions; nothing in the logic is a price/bar/bin/token literal.
"""
from dataclasses import dataclass, field
from typing import List, Set

from step_a_profile import _effective_n, EFF_N_MIN

SNAP_ATR  = 1.0     # radius to snap a structural pivot to a volume peak
MERGE_ATR = 0.5     # two candidates within this many ATR collapse into one
# merged price comes from the highest-priority member. Volume sharpens structure to a
# price (spec 8.6): a volume feature sets the line, structure confirms it -> volume first.
_PRIORITY = ["poc", "vah", "val", "lvn", "structure"]


@dataclass
class Candidate:
    price: float
    side: str
    lo: float
    hi: float
    methods: Set[str] = field(default_factory=set)
    scale: str = ""
    low_conf: bool = False

    def anchor(self):
        return min(self.methods, key=lambda m: _PRIORITY.index(m) if m in _PRIORITY else 99)


def _features(prof):
    return [prof.poc, prof.vah, prof.val] + list(prof.lvn)


def _nearest(points, price, tol):
    near = [p for p in points if abs(p - price) <= tol]
    return min(near, key=lambda p: abs(p - price)) if near else None


def _feat_dist(price, prof):
    return min(abs(price - f) for f in _features(prof))


def snap_and_merge(structural, prof, atr, price_now, scale="", low_conf=False):
    """One scale: snap its pivots to its volume peaks, seed its value features,
    merge within 0.5 ATR. Tags every candidate with scale + low_conf."""
    peaks = [prof.poc] + list(prof.hvn)
    side_of = lambda p: "support" if p <= price_now else "resistance"

    cands = []
    for pr, side in structural:
        snapped = _nearest(peaks, pr, SNAP_ATR * atr)
        p = snapped if snapped is not None else pr
        cands.append(Candidate(p, side, p, p, {"structure"}, scale, low_conf))
    for p, m in ((prof.poc, "poc"), (prof.vah, "vah"), (prof.val, "val")):
        cands.append(Candidate(p, side_of(p), p, p, {m}, scale, low_conf))
    for p in prof.lvn:
        cands.append(Candidate(p, side_of(p), p, p, {"lvn"}, scale, low_conf))

    cands.sort(key=lambda c: c.price)
    merged: List[Candidate] = []
    for c in cands:
        if merged and (c.price - merged[-1].price) <= MERGE_ATR * atr:
            m = merged[-1]
            take = _PRIORITY.index(c.anchor()) < _PRIORITY.index(m.anchor())  # before union
            m.methods |= c.methods
            m.lo, m.hi = min(m.lo, c.price), max(m.hi, c.price)
            if take:
                m.price, m.side = c.price, c.side
        else:
            merged.append(c)
    return merged


def assign_scales(structural, macro, tactical, atr):
    """Each pivot -> the scale whose nearest value feature is closest."""
    mac, tac = [], []
    for pr, side in structural:
        (tac if _feat_dist(pr, tactical) <= _feat_dist(pr, macro) else mac).append((pr, side))
    return mac, tac


def dual_snap_merge(structural, macro, tactical, tac_window, atr, price_now):
    """Run both scales and concatenate (no cross-scale merge). macro low_confidence
    comes from Step A; tactical low_confidence is the same effective-N gate applied
    to the tactical window (consistent trust measure across scales)."""
    mac_low = bool(getattr(macro, "low_confidence", False))
    tac_low = _effective_n(tac_window) < EFF_N_MIN
    mac_piv, tac_piv = assign_scales(structural, macro, tactical, atr)
    return (snap_and_merge(mac_piv, macro, atr, price_now, "macro", mac_low)
            + snap_and_merge(tac_piv, tactical, atr, price_now, "tactical", tac_low))
