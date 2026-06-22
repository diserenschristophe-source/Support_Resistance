#!/usr/bin/env python3
"""
VOLUME PROFILE (on top of steps 1-3) — POC, VAH/VAL, LVN (+ HVN).
=================================================================
Overlay layer. Does NOT change step1_detect / step2_categorise / step3_suppress;
it adds volume-at-price context computed from the SAME daily OHLCV we already
cache (Binance spot) — no new data source.

Each bar's volume is spread uniformly across the price bins its [low, high]
range spans; summed over the window that gives volume-at-price:
  POC       — bin with the most traded volume (fair-value magnet).
  VAH / VAL — edges of the ~70% value area around the POC.
  HVN       — volume peaks (acceptance shelves).
  LVN       — volume valleys (rejection / fast-move zones).

Window = last `lookback` bars (default 180). Bin width = bin_atr_fraction * ATR
so resolution is comparable across assets.
"""
from dataclasses import dataclass, field
from typing import List

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

from core.models import compute_atr


@dataclass
class Profile:
    poc: float
    vah: float
    val: float
    hvn: List[float] = field(default_factory=list)
    lvn: List[float] = field(default_factory=list)
    bin_centers: np.ndarray = None    # price of each bin
    volume: np.ndarray = None         # smoothed volume-at-price (for the sidebar)
    lookback: int = 0


def _histogram(df, bin_atr_fraction):
    low, high, vol = df["low"].values, df["high"].values, df["volume"].values
    p_lo, p_hi = float(low.min()), float(high.max())
    width = max(bin_atr_fraction * compute_atr(df), (p_hi - p_lo) / 500)
    nbins = max(int((p_hi - p_lo) / width), 20)
    edges = np.linspace(p_lo, p_hi, nbins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    profile = np.zeros(nbins)
    for lo, hi, v in zip(low, high, vol):
        mask = (centers >= lo) & (centers <= hi)
        k = int(mask.sum())
        if k:
            profile[mask] += v / k        # spread the bar's volume across its range
    return centers, profile


def _value_area(centers, profile, value_area_pct):
    poc_i = int(np.argmax(profile))
    target = profile.sum() * value_area_pct
    lo = hi = poc_i
    acc = profile[poc_i]
    while acc < target:
        el = profile[lo - 1] if lo > 0 else -1.0
        eh = profile[hi + 1] if hi < len(profile) - 1 else -1.0
        if el < 0 and eh < 0:
            break
        if el >= eh:
            lo -= 1; acc += profile[lo]
        else:
            hi += 1; acc += profile[hi]
    return float(centers[poc_i]), float(centers[hi]), float(centers[lo])


def _nodes(centers, raw, smoothing_sigma_bins, prominence):
    sm = gaussian_filter1d(raw, smoothing_sigma_bins)
    mx = sm.max() if sm.max() > 0 else 1.0
    hvn_idx, _ = find_peaks(sm, prominence=prominence * mx, distance=3)
    lvn_idx, _ = find_peaks(-sm, prominence=prominence * mx, distance=3)
    return sm, [float(centers[i]) for i in hvn_idx], [float(centers[i]) for i in lvn_idx]


def profile_levels(df, lookback=180, bin_atr_fraction=0.25, value_area_pct=0.70,
                   smoothing_sigma_bins=1.5, prominence=0.10):
    """COMPOSITE profile — fixed long window. Macro shelves only (Rule 1)."""
    sub = df.tail(lookback) if lookback and len(df) > lookback else df
    centers, raw = _histogram(sub, bin_atr_fraction)
    poc, vah, val = _value_area(centers, raw, value_area_pct)
    sm, hvn, lvn = _nodes(centers, raw, smoothing_sigma_bins, prominence)
    return Profile(poc=poc, vah=vah, val=val, hvn=hvn, lvn=lvn,
                   bin_centers=centers, volume=sm, lookback=len(sub))


def tactical_profile(df, va_width_max=0.10, l_min=14, l_max=180,
                     bin_atr_fraction=0.25, value_area_pct=0.70,
                     smoothing_sigma_bins=1.5, prominence=0.10):
    """TACTICAL profile — self-anchored window (Rules 1+2). Nothing hardcoded in
    price terms: the only gate is value-area width as a FRACTION OF PRICE.

    Rule 2 self-check: scan window lengths ending at the last bar; the value area
    of the right window is a range (tight), a window that captures a move is wide.
    Pick the LONGEST window whose VA width <= va_width_max * price (most context
    that is still balance). If none qualifies (pure trend, no balance), fall back
    to the shortest window and flag balanced=False. Because the window ends at the
    last bar it always contains current price, and the gate is a price-percentage,
    so this adapts on its own to any token and any regime each run.

    Returns (Profile, meta) where meta has window_bars, va_width_pct, balanced,
    poc_dist_pct (|POC-price|/price)."""
    price = float(df["close"].iloc[-1])
    n = len(df)
    lo, hi = min(l_min, n), min(l_max, n)
    chosen = None
    for L in range(lo, hi + 1):                      # ascending -> keep the largest qualifying
        centers, raw = _histogram(df.tail(L), bin_atr_fraction)
        poc, vah, val = _value_area(centers, raw, value_area_pct)
        if (vah - val) / price <= va_width_max:
            chosen = (L, centers, raw, poc, vah, val)
    balanced = chosen is not None
    if not balanced:                                 # trend: no balance window found
        L = lo
        centers, raw = _histogram(df.tail(L), bin_atr_fraction)
        poc, vah, val = _value_area(centers, raw, value_area_pct)
        chosen = (L, centers, raw, poc, vah, val)
    L, centers, raw, poc, vah, val = chosen
    sm, hvn, lvn = _nodes(centers, raw, smoothing_sigma_bins, prominence)
    prof = Profile(poc=poc, vah=vah, val=val, hvn=hvn, lvn=lvn,
                   bin_centers=centers, volume=sm, lookback=L)
    meta = {"window_bars": L, "va_width_pct": (vah - val) / price,
            "balanced": balanced, "poc_dist_pct": abs(poc - price) / price}
    return prof, meta
