#!/usr/bin/env python3
"""
STEP 1 — DETECT levels from 2 strategies.
=========================================
  market_structure : swing levels on the WICK, with the edge-case fix —
                     n-bar fractal + (>= left / > right) tie-break so a flat top
                     or equal-high resolves to exactly ONE pivot, then an
                     ATR/ZigZag significance gate (a swing counts only if it
                     reverses >= z_atr * ATR). Level price = wick extreme;
                     role = HH/HL/LH/LL.
  nison_body       : large-candle body-edge levels (existing NisonBodyDetector).

Output: a flat, time-ordered list of Level. No categorisation, no suppression,
no merge.
"""
from dataclasses import dataclass

import numpy as np

from core.models import compute_atr_series
from core.detectors.nison_body import NisonBodyDetector


@dataclass
class Level:
    price: float
    side: str             # 'support' | 'resistance'
    anchor_idx: int       # index of the candle that created the level
    source: str           # 'market_structure' | 'nison_body'
    role: str = ""        # HH/HL/LH/LL (market_structure only)
    state: str = ""       # set by step 2: 'live' | 'dormant' | 'broken'
    visible: bool = True   # set by step 3


def _atr_at(atr_s, i):
    a = atr_s[i] if (0 <= i < len(atr_s) and np.isfinite(atr_s[i]) and atr_s[i] > 0) else atr_s[-1]
    return float(a)


def _fractals(h, l, n):
    """n-bar fractals on the wick. Tie-break: >= on the left, > on the right."""
    N = len(h)
    out = []
    for i in range(n, N - n):
        if all(h[i] >= h[i - j] for j in range(1, n + 1)) and \
           all(h[i] >  h[i + j] for j in range(1, n + 1)):
            out.append((i, float(h[i]), "high"))
        if all(l[i] <= l[i - j] for j in range(1, n + 1)) and \
           all(l[i] <  l[i + j] for j in range(1, n + 1)):
            out.append((i, float(l[i]), "low"))
    out.sort(key=lambda x: x[0])
    return out


def _zigzag(cand, atr_s, z_atr):
    """Keep a swing only if it reverses >= z_atr * ATR from the last confirmed
    pivot. Two same-type pivots in a row keep the more extreme."""
    conf = []
    for idx, price, kind in cand:
        if not conf:
            conf.append({"idx": idx, "price": price, "kind": kind})
            continue
        last = conf[-1]
        if kind == last["kind"]:
            if (kind == "high" and price > last["price"]) or \
               (kind == "low" and price < last["price"]):
                conf[-1] = {"idx": idx, "price": price, "kind": kind}
        elif abs(price - last["price"]) >= z_atr * _atr_at(atr_s, idx):
            conf.append({"idx": idx, "price": price, "kind": kind})
    return conf


def _roles(conf):
    ph = pl = None
    for p in conf:
        if p["kind"] == "high":
            p["role"] = "H" if ph is None else ("HH" if p["price"] > ph else "LH")
            ph = p["price"]
        else:
            p["role"] = "L" if pl is None else ("HL" if p["price"] > pl else "LL")
            pl = p["price"]
    return conf


def _market_structure(df, n, z_atr):
    atr_s = compute_atr_series(df)
    conf = _roles(_zigzag(_fractals(df["high"].values, df["low"].values, n), atr_s, z_atr))
    return [Level(price=p["price"],
                  side="resistance" if p["kind"] == "high" else "support",
                  anchor_idx=p["idx"], source="market_structure", role=p["role"])
            for p in conf]


def _nison_body(df):
    return [Level(price=lv.price, side=lv.level_type,
                  anchor_idx=lv.anchor_candle_idx, source="nison_body")
            for lv in NisonBodyDetector().detect(df)]


def detect(df, n=2, z_atr=1.5):
    levels = _market_structure(df, n, z_atr) + _nison_body(df)
    levels.sort(key=lambda L: L.anchor_idx)
    return levels
