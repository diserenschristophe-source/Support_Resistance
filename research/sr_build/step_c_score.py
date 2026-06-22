#!/usr/bin/env python3
"""
STEP C — score by agreement.
============================
Each merged level scores by how many independent methods land on it within
0.5 ATR: structure, POC, VAH, VAL, LVN edge, 200D MA, round number. (200W MA and
liquidations stay removed — the data does not exist.) Sum the per-method weights,
apply an agreement bonus, normalize to 0..1 against the full method set so that
fewer confirmations is genuinely a lower score. Two honesty rules:

  * Variable method set: a method that is absent for a token (e.g. no 200D MA
    history) simply does not contribute; nothing is special-cased, nothing breaks.
  * low_confidence propagation: on a flagged Step A profile the VOLUME methods
    (POC/VAH/VAL/LVN) are down-weighted, so the same level scores lower on a
    fragile profile than on a clean one.

No price/bar/bin/token literals; every gate is an ATR fraction or a ratio.
"""
import math

MATCH_ATR          = 0.5     # a method "lands" on a level within this many ATR
ROUND_TOL_ATR      = 0.25    # proximity to a round number
AGREEMENT_GAIN     = 0.15    # bonus per extra agreeing method
LOWCONF_VOL_FACTOR = 0.5     # volume-method weight multiplier on a low_confidence profile

W = {"structure": 0.30, "poc": 0.30, "vah": 0.15, "val": 0.15,
     "lvn": 0.20, "ma": 0.15, "round": 0.10}
VOLUME_METHODS = {"poc", "vah", "val", "lvn"}
_MAX = sum(W.values()) * (1 + AGREEMENT_GAIN * (len(W) - 1))   # all methods + full agreement


def ma_200d(df):
    """200-DAY MA, or None when history is too short (top-50 tail tokens)."""
    return float(df["close"].rolling(200).mean().iloc[-1]) if len(df) >= 200 else None


def _round_levels(price):
    """Nice round numbers at the level's own order of magnitude — scale-aware, so
    it works for a $0.08 coin and a $60k coin with no token-specific constant."""
    if price <= 0:
        return []
    oom = 10 ** math.floor(math.log10(price))
    out = []
    for step in (oom, oom / 2):
        k = round(price / step)
        out += [(k - 1) * step, k * step, (k + 1) * step]
    return out


def score_levels(merged, df, atr, ma=None):
    """Score each merged level. low_confidence is read PER LEVEL (lv.low_conf), so a
    level on a fragile profile is down-weighted whatever scale it came from."""
    if ma is None:
        ma = ma_200d(df)
    scored = []
    for lv in merged:
        p = lv.price
        methods = set(lv.methods)                       # structure/poc/vah/val/lvn from the merge
        if ma is not None and abs(p - ma) <= MATCH_ATR * atr:
            methods.add("ma")
        if any(abs(p - r) <= ROUND_TOL_ATR * atr for r in _round_levels(p)):
            methods.add("round")

        s = 0.0
        for m in methods:
            w = W.get(m, 0.0)
            if lv.low_conf and m in VOLUME_METHODS:
                w *= LOWCONF_VOL_FACTOR
            s += w
        s *= (1 + AGREEMENT_GAIN * (max(len(methods), 1) - 1))
        scored.append({
            "price": round(p, 8),
            "side": lv.side,
            "scale": lv.scale,
            "band": [round(lv.lo, 8), round(lv.hi, 8)],
            "methods": sorted(methods),
            "strength": round(min(s / _MAX, 1.0), 3),
            "low_confidence": lv.low_conf,
        })
    scored.sort(key=lambda x: -x["strength"])
    return scored
