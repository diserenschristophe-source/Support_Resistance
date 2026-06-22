#!/usr/bin/env python3
"""
STEP 3 — SUPPRESS close levels: show only the most recent.
==========================================================
Within super_atr * ATR (CURRENT ATR), the OLDER level is HIDDEN and only the
MOST RECENT level is shown. Same side only. Prices are NOT moved (this is a
display filter, not a merge — merge is the last step, done later).

A level is hidden iff a MORE RECENT, same-side, non-broken level exists within
super_atr * ATR of it. Sets level.visible.
"""
from core.models import compute_atr


def suppress(levels, df, super_atr=1.0):
    atr_now = compute_atr(df)
    others = [M for M in levels if M.state != "broken"]
    for L in levels:
        L.visible = not any(
            M is not L and M.side == L.side and M.anchor_idx > L.anchor_idx
            and abs(M.price - L.price) <= super_atr * atr_now
            for M in others
        )
    return levels
