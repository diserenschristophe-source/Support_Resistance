#!/usr/bin/env python3
"""
STEP 2 — CATEGORISE each level: LIVE / DORMANT / BROKEN.
========================================================
  BROKEN  : a CLOSE penetrated the level by >= pen_atr * ATR after its anchor.
  LIVE    : unbroken AND in play — within react_atr * ATR of the current price,
            OR the nearest unbroken level on its side (immediate floor/ceiling).
  DORMANT : unbroken but out of play.

Sets level.state. No suppression, no merge.
"""
import numpy as np

from core.models import compute_atr_series


def _atr_at(atr_s, i):
    a = atr_s[i] if (0 <= i < len(atr_s) and np.isfinite(atr_s[i]) and atr_s[i] > 0) else atr_s[-1]
    return float(a)


def categorise(levels, df, pen_atr=0.75, react_atr=3.0):
    c = df["close"].values
    atr_s = compute_atr_series(df)
    N = len(df)
    price = float(c[-1])
    atr_now = _atr_at(atr_s, N - 1)

    # BROKEN: first close beyond the level by pen_atr*ATR after its anchor
    for L in levels:
        L.state = ""
        for k in range(L.anchor_idx + 1, N):
            pen = pen_atr * _atr_at(atr_s, k)
            if (L.side == "resistance" and c[k] > L.price + pen) or \
               (L.side == "support" and c[k] < L.price - pen):
                L.state = "broken"
                break

    # nearest unbroken level on each side of the current price
    unbroken = [L for L in levels if L.state != "broken"]
    sup = [L for L in unbroken if L.side == "support" and L.price <= price]
    res = [L for L in unbroken if L.side == "resistance" and L.price >= price]
    nearest_sup = max(sup, key=lambda L: L.price) if sup else None
    nearest_res = min(res, key=lambda L: L.price) if res else None

    # LIVE / DORMANT
    for L in unbroken:
        in_band = abs(L.price - price) <= react_atr * atr_now
        L.state = "live" if (in_band or L is nearest_sup or L is nearest_res) else "dormant"
    return levels
