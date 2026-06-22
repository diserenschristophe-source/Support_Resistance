#!/usr/bin/env python3
"""
STEP A — a profile that is trustworthy by construction.
=======================================================
Window comes from the REGIME ANCHOR (last flip in research/regime.py), never a
bar/day count — its length is whatever the data gives. Inside that window the
profile is built at the finest bars available (daily here) with ATR-sized bins
and CLOSE-WEIGHTED sub-bin distribution, then three dimensionless trust tests
decide if it stands; if not, an enrichment loop widens bins, then blends the
composite, then flags low_confidence. Nothing in the logic is a literal count or
a token name: every gate is an ATR fraction or a percent/ratio, computed fresh.

Resolution ceiling: the store only has DAILY bars, so "go finer in time" is a
no-op here; a short regime keeps its POC trustworthy by coarser bins / composite
blend, and flags low_confidence rather than pretending to be sharp.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

from core.models import compute_atr
from volume_profile import _value_area
from regime_anchor import compute_anchor   # single owner of the regime anchor

# ── gates: all ATR-fractions / dimensionless ratios, no counts ──────────────
BIN_ATR_FRAC      = 0.25    # base bin width as a fraction of ATR
BIN_ATR_FRAC_MAX  = 1.00    # coarsest bin the widen-loop will go to
BIN_WIDEN         = 1.50    # bin-widen multiplier per enrichment step
VALUE_AREA_PCT    = 0.70
NODE_DIST_ATR     = 0.50    # min spacing between nodes, in ATR
SMOOTH_ATR        = 0.375   # gaussian sigma for node detection, in ATR
OCC_BIN_FRAC      = 0.05    # a bin is "occupied" if vol >= this * max-bin
MIN_OCCUPANCY     = 0.30    # >= this fraction of bins occupied = dense enough
POC_STAB_ATR      = 0.50    # POC may not move more than this * ATR under perturbation
VA_VOID_FRAC      = 0.02    # an internal value-area bin must hold >= this * max-bin
BLEND_STEP        = 0.25    # composite-blend weight increment
BOOTSTRAP_DRAWS   = 400     # resample iterations (algorithm resolution, not a data count)
BOOTSTRAP_SEED    = 12345   # fixed -> deterministic per data set (reproducible daily run)
EFF_N_MIN         = 10.0    # EMPIRICAL sufficiency floor for a trustworthy POC.
    # effective independent observations = participation ratio (Sum v)^2 / Sum(v^2) of
    # per-bar volumes (width-invariant; immune to the multimodality that confounds the
    # bootstrap-POC spread). NOT derived from the bootstrap relationship: that derivation
    # was tested across 59 cached tokens and REJECTED -- raw bootstrap-POC spread is
    # confounded by profile width (a small-N window is tightly clumped -> LOW spread, the
    # opposite of what sufficiency needs), corr(spread, effN) ~ +0.2, and the width-
    # normalized form tracks 1/sqrt(effN) at only corr 0.27, so there is no clean effN at
    # which spread crosses an ATR fraction. Justified instead by the effN DISTRIBUTION:
    # thin young-regime windows cluster at effN <= ~6, then an EMPTY GAP (effN ~6-9), then
    # sampled windows at effN >= ~9. The floor sits just above that gap -- conservative,
    # flags borderline 9-10 effN windows as low_confidence, the safe call for the
    # un-eyeballable tail tokens. Flagged empirical; revisit if the distribution shifts.

# ── recency-weighted windowing (REPLACES the regime-anchored slice for the live POC) ──
RECENCY_HL_CONST = 0.5     # THE single load-bearing constant. Decay half-life expressed in
                           # cumulative-relative-volatility units: hl_bars = HL / (ATR/price),
                           # so a volatile token remembers fewer bars and a calm one more.
                           # No literal bar count, no token name. Replaces the whole
                           # regime-periods debt: old regimes fade by WEIGHT, not exclusion.
LABEL_HL_MULT    = 4.0     # the trend-label long EMA uses this many recency half-lives
LABEL_BAND_ATR   = 1.0     # |price - long EMA| within this * ATR = consolidation (balance)
RECENCY_W_FLOOR  = 0.05    # bars whose recency weight falls below this are excluded from the
                           # histogram's PRICE RANGE only (they carry ~0 weight but would
                           # otherwise stretch the bins across the full multi-year range and
                           # gut occupancy). Dimensionless; ~4.3 half-lives of effective span.


@dataclass
class TrustProfile:
    poc: float
    vah: float
    val: float
    hvn: List[float] = field(default_factory=list)
    lvn: List[float] = field(default_factory=list)
    bin_centers: np.ndarray = None
    volume: np.ndarray = None
    # window / anchor
    regime: str = ""            # balance | trend
    anchor_idx: int = 0         # window start (last regime flip)
    window_bars: int = 0
    # trust + enrichment
    occupancy: float = 0.0
    poc_shift_atr: float = 0.0
    va_contiguous: bool = True
    boot_spread_atr: float = 0.0
    eff_n: float = 0.0
    hl_bars: float = 0.0        # recency decay half-life in bars (recency profile only)
    trusted: bool = False
    bin_atr_frac: float = BIN_ATR_FRAC
    blend_weight: float = 0.0
    # flags
    ohlcv_approx: bool = True
    low_confidence: bool = False
    notes: str = ""


# ── regime anchor ───────────────────────────────────────────────────────────
# RETIRED: _regime_labels / _anchor moved to regime_anchor.py, the single owner
# of the regime anchor (debounced 3-way Wyckoff series + onset walk-back).
# build_profile reads onset/regime from regime_anchor.compute_anchor below.
# No second anchor computation lives in this module.


# ── profile construction ────────────────────────────────────────────────────
def _hist_arrays(low, high, close, vol, bin_w):
    p_lo, p_hi = float(low.min()), float(high.max())
    nbins = max(int(np.ceil((p_hi - p_lo) / bin_w)), 1)
    edges = np.linspace(p_lo, p_hi, nbins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    prof = np.zeros(nbins)
    for lo, hi, cl, v in zip(low, high, close, vol):
        idx = np.where((centers >= lo) & (centers <= hi))[0]
        if len(idx) == 0:                            # bar narrower than a bin -> close bin
            prof[min(max(int((cl - p_lo) / bin_w), 0), nbins - 1)] += v
            continue
        w = np.clip(1.0 - np.abs(centers[idx] - cl) / max(hi - lo, bin_w), 1e-6, None)
        prof[idx] += v * (w / w.sum())               # close-weighted, not uniform
    return centers, prof


def _histogram(window, bin_w):
    return _hist_arrays(window["low"].values, window["high"].values,
                        window["close"].values, window["volume"].values, bin_w)


def _bootstrap_poc_spread(window, bin_w, atr):
    """Sample-sufficiency test: resample the window's bars WITH replacement, recompute
    the POC each draw, return the std of the POC distribution in ATR units. A small-N
    window shows a wide spread even when leave-one-out is stable, because resampling
    exposes how little independent information is present."""
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    low, high = window["low"].values, window["high"].values
    close, vol = window["close"].values, window["volume"].values
    nb = len(window)
    if nb < 2:
        return np.inf
    pocs = np.empty(BOOTSTRAP_DRAWS)
    for d in range(BOOTSTRAP_DRAWS):
        j = rng.integers(0, nb, nb)
        c, p = _hist_arrays(low[j], high[j], close[j], vol[j], bin_w)
        pocs[d] = c[int(np.argmax(p))]
    return float(np.std(pocs) / atr)


def _effective_n(window):
    """Sample sufficiency = participation ratio of per-bar volumes, (Sum v)^2 / Sum(v^2).
    The effective number of independent observations behind the profile. Width-invariant
    and immune to the multimodality that confounds bootstrap-POC-position spread, so a
    tiny window (few bars) scores low even when its bars happen to be tightly clumped."""
    v = window["volume"].values.astype(float)
    s2 = float(np.sum(v ** 2))
    return float(v.sum() ** 2 / s2) if s2 > 0 else 0.0


def _poc(centers, prof):
    return float(centers[int(np.argmax(prof))])


def _tests(centers, prof, atr, window, bin_w):
    """occupancy ratio, POC shift under two perturbations, value-area contiguity."""
    mx = prof.max() if prof.max() > 0 else 1.0
    occupancy = float(np.mean(prof >= OCC_BIN_FRAC * mx))

    poc0 = _poc(centers, prof)
    c1, p1 = _histogram(window.iloc[:-1], bin_w)                       # drop most recent bar
    drop_i = int(np.argmax(window["volume"].values))
    c2, p2 = _histogram(window.drop(window.index[drop_i]), bin_w)      # drop highest-volume bar
    poc_shift = max(abs(poc0 - _poc(c1, p1)), abs(poc0 - _poc(c2, p2))) / atr

    poc, vah, val = _value_area(centers, prof, VALUE_AREA_PCT)
    inside = (centers >= val) & (centers <= vah)
    va_contiguous = bool(np.all(prof[inside] >= VA_VOID_FRAC * mx)) if inside.any() else False

    passed = (occupancy >= MIN_OCCUPANCY and poc_shift <= POC_STAB_ATR and va_contiguous)
    return passed, occupancy, poc_shift, va_contiguous, (poc, vah, val)


def _nodes(centers, prof, bin_frac):
    sigma = max(SMOOTH_ATR / bin_frac, 0.5)
    dist = max(int(round(NODE_DIST_ATR / bin_frac)), 1)
    sm = gaussian_filter1d(prof, sigma)
    mx = sm.max() if sm.max() > 0 else 1.0
    hvn_i, _ = find_peaks(sm, prominence=0.10 * mx, distance=dist)
    lvn_i, _ = find_peaks(-sm, prominence=0.10 * mx, distance=dist)
    return sm, [float(centers[i]) for i in hvn_i], [float(centers[i]) for i in lvn_i]


def build_profile_anchored(df, verbose=False):
    """REGIME-ANCHORED profile (the OLD windowing). No longer feeds the live POC; kept
    computable so the regime anchor can be produced on demand for the head-to-head
    comparison against the recency-weighted profile. Returns a TrustProfile.
    """
    atr = compute_atr(df)
    # Regime anchor is owned by regime_anchor.compute_anchor (debounced 3-way
    # Wyckoff + onset walk-back). Map wyckoff -> the balance|trend label this
    # profile records, and use the onset as the window start.
    anchor = compute_anchor(df)
    start = anchor.onset_idx
    regime = "trend" if anchor.wyckoff in ("wr-markup", "wr-markdown") else "balance"
    window = df.iloc[start:]
    notes = []

    # 1) finest bars (daily) + widen-bins enrichment
    chosen, bin_frac = None, BIN_ATR_FRAC
    while True:
        centers, prof = _histogram(window, bin_frac * atr)
        passed, occ, shift, contig, (poc, vah, val) = _tests(centers, prof, atr, window, bin_frac * atr)
        chosen = (centers, prof, occ, shift, contig, poc, vah, val, bin_frac)
        if passed or bin_frac >= BIN_ATR_FRAC_MAX:
            break
        bin_frac = min(bin_frac * BIN_WIDEN, BIN_ATR_FRAC_MAX)
        notes.append(f"widen->{bin_frac:.2f}ATR")

    # 2) composite blend if still untrusted (maturity = tests passing, not time)
    blend_w = 0.0
    if not passed:
        notes.append("blend-composite")
        centers, prof_t = _histogram(window, bin_frac * atr)
        p_lo, p_hi = centers[0], centers[-1]
        comp = df[(df["low"] <= p_hi) & (df["high"] >= p_lo)]      # all history in this price range
        _, prof_c_raw = _histogram(comp, bin_frac * atr)
        # align composite onto the window's bin grid by price
        prof_c = np.interp(centers, np.linspace(p_lo, p_hi, len(prof_c_raw)), prof_c_raw)
        t = prof_t / (prof_t.sum() or 1.0)
        c = prof_c / (prof_c.sum() or 1.0)
        w = 0.0
        while w < 1.0:
            w = min(w + BLEND_STEP, 1.0)
            prof = (1 - w) * t + w * c
            passed, occ, shift, contig, (poc, vah, val) = _tests(centers, prof, atr, window, bin_frac * atr)
            if passed:
                break
        blend_w = w
        chosen = (centers, prof, occ, shift, contig, poc, vah, val, bin_frac)

    centers, prof, occ, shift, contig, poc, vah, val, bin_frac = chosen
    sm, hvn, lvn = _nodes(centers, prof, bin_frac)

    # sample-sufficiency: effective-N gate (primary) + bootstrap spread (diagnostic)
    eff_n = _effective_n(window)
    boot = _bootstrap_poc_spread(window, bin_frac * atr, atr)
    sufficient = eff_n >= EFF_N_MIN
    low_conf = (not passed) or (not sufficient) or anchor.low_confidence
    if not passed:
        notes.append("untrusted-tests")
    if not sufficient:
        notes.append(f"insufficient-sample(effN {eff_n:.1f}<{EFF_N_MIN:.0f})")
    if anchor.low_confidence:
        notes.append("thin-regime")
    if low_conf:
        notes.append("low_confidence")
    notes.append("daily-ceiling")

    if verbose:
        price = float(df["close"].iloc[-1]) if len(df) else float("nan")
        va_width_pct = ((vah - val) / price * 100.0) if price else float("nan")
        print("── STEP A checkpoint ─────────────────────────────────────────")
        print(f"  onset_ts={anchor.onset_ts}  wyckoff={anchor.wyckoff}  "
              f"low_confidence={low_conf}")
        print(f"  window_bars={len(window)}  anchor_idx={start}  "
              f"regime={regime}")
        print(f"  POC={poc:.6g}  VAH={vah:.6g}  VAL={val:.6g}  "
              f"price={price:.6g}")
        print(f"  VA_width={va_width_pct:.2f}% of price")
        print("──────────────────────────────────────────────────────────────")

    return TrustProfile(
        poc=poc, vah=vah, val=val, hvn=hvn, lvn=lvn, bin_centers=centers, volume=sm,
        regime=regime, anchor_idx=int(start), window_bars=int(len(window)),
        occupancy=occ, poc_shift_atr=shift, va_contiguous=contig, boot_spread_atr=boot,
        eff_n=eff_n, trusted=bool(passed and sufficient),
        bin_atr_frac=bin_frac, blend_weight=blend_w,
        ohlcv_approx=True, low_confidence=low_conf, notes=", ".join(notes))


# ── recency-weighted profile (the live windowing) ───────────────────────────
def _recency_weights(n, hl_bars):
    """Exponential recency decay over the FULL history; weight halves every hl_bars.
    Newest bar weight 1.0, fading smoothly toward zero for old bars."""
    age = np.arange(n - 1, -1, -1, dtype=float)        # age[i] = (n-1) - i ; newest = 0
    return np.power(0.5, age / max(hl_bars, 1e-9))


def _recency_effn(wv):
    """effN on the EFFECTIVE (recency-weighted) sample: (Sum W)^2 / Sum(W^2), W_i =
    bar_w_i * vol_i. If a few recent/heavy bars dominate, effN is small -> low_confidence,
    so a weighted profile driven by too little data still flags even with no hard window."""
    s2 = float(np.sum(wv ** 2))
    return float(wv.sum() ** 2 / s2) if s2 > 0 else 0.0


def _regime_label(df, atr, hl_bars):
    """Lightweight trend/balance label for Step D's gate — price vs a long EMA with an
    ATR band, no debounce. Replaces the heavyweight regime label as the gate tiebreaker."""
    price = float(df["close"].iloc[-1])
    ma = float(df["close"].ewm(halflife=max(LABEL_HL_MULT * hl_bars, 1.0)).mean().iloc[-1])
    if abs(price - ma) <= LABEL_BAND_ATR * atr:
        return "balance", "consolidation", ma
    return ("trend", "markup", ma) if price > ma else ("trend", "markdown", ma)


def _recency_profile(df, hl_const, bin_w, atr):
    """Recency-weighted histogram, BOUNDED to the price range that still carries weight.
    Old bars decay to ~0 weight; including them in the bin range only stretches the bins
    across the full multi-year history and guts occupancy. Returns centers, prof,
    weighted-volume(kept), hl_bars."""
    price = float(df["close"].iloc[-1])
    hl_bars = hl_const / (atr / price) if (atr > 0 and price > 0) else float(len(df))
    w = _recency_weights(len(df), hl_bars)
    keep = w >= RECENCY_W_FLOOR
    wv = df["volume"].values[keep].astype(float) * w[keep]
    centers, prof = _hist_arrays(df["low"].values[keep], df["high"].values[keep],
                                 df["close"].values[keep], wv, bin_w)
    return centers, prof, wv, hl_bars


def _recency_tests(df, atr, hl_const, bin_w):
    """Trust battery on the recency-weighted profile: occupancy, POC stability under
    dropping the most-recent bar and the max weighted-contribution bar, VA contiguity."""
    centers, prof, wv, hl_bars = _recency_profile(df, hl_const, bin_w, atr)
    mx = prof.max() if prof.max() > 0 else 1.0
    occupancy = float(np.mean(prof >= OCC_BIN_FRAC * mx))
    poc0 = _poc(centers, prof)
    c1, p1, _, _ = _recency_profile(df.iloc[:-1], hl_const, bin_w, atr)          # drop newest
    w_full = _recency_weights(len(df), hl_bars)                                  # max weighted bar
    drop_i = int(np.argmax(w_full * df["volume"].values.astype(float)))
    c2, p2, _, _ = _recency_profile(df.drop(df.index[drop_i]), hl_const, bin_w, atr)
    poc_shift = max(abs(poc0 - _poc(c1, p1)), abs(poc0 - _poc(c2, p2))) / atr
    poc, vah, val = _value_area(centers, prof, VALUE_AREA_PCT)
    inside = (centers >= val) & (centers <= vah)
    va_contiguous = bool(np.all(prof[inside] >= VA_VOID_FRAC * mx)) if inside.any() else False
    passed = occupancy >= MIN_OCCUPANCY and poc_shift <= POC_STAB_ATR and va_contiguous
    return (passed, occupancy, poc_shift, va_contiguous, centers, prof, wv, hl_bars, (poc, vah, val))


def build_profile(df, verbose=False, hl_const=RECENCY_HL_CONST):
    """STEP A entry point — RECENCY-WEIGHTED profile, the live windowing.

    Built over the full history with an exponential recency decay (single constant
    RECENCY_HL_CONST). No regime-anchored slice, no hard cutoff: old regimes fade by
    weight. The regime module is NOT called here (kept computable via
    build_profile_anchored for comparison). Step D's label comes from the lightweight
    price-vs-long-EMA route (set into .regime), so the gate runs unchanged.
    """
    atr = compute_atr(df)
    price = float(df["close"].iloc[-1])
    bin_frac = BIN_ATR_FRAC
    notes = []
    while True:                                          # widen-bins enrichment if untrusted
        (passed, occ, shift, contig, centers, prof, wv, hl_bars,
         (poc, vah, val)) = _recency_tests(df, atr, hl_const, bin_frac * atr)
        if passed or bin_frac >= BIN_ATR_FRAC_MAX:
            break
        bin_frac = min(bin_frac * BIN_WIDEN, BIN_ATR_FRAC_MAX)
        notes.append(f"widen->{bin_frac:.2f}ATR")

    sm, hvn, lvn = _nodes(centers, prof, bin_frac)
    eff_n = _recency_effn(wv)                            # sufficiency on the weighted sample
    sufficient = eff_n >= EFF_N_MIN
    low_conf = (not passed) or (not sufficient)
    regime, fine_label, _ma = _regime_label(df, atr, hl_bars)

    if not passed:
        notes.append("untrusted-tests")
    if not sufficient:
        notes.append(f"insufficient-sample(effN {eff_n:.1f}<{EFF_N_MIN:.0f})")
    if low_conf:
        notes.append("low_confidence")
    notes += [f"recency-hl={hl_bars:.0f}b", f"label={fine_label}", "ohlcv_approx"]

    if verbose:
        vaw = (vah - val) / price * 100 if price else float("nan")
        print("── STEP A (recency-weighted) checkpoint ──────────────────────")
        print(f"  hl_bars={hl_bars:.1f}  label={fine_label}->{regime}  "
              f"effN={eff_n:.1f}  low_confidence={low_conf}")
        print(f"  POC={poc:.6g} VAH={vah:.6g} VAL={val:.6g} price={price:.6g}  "
              f"VA_width={vaw:.2f}%")
        print("──────────────────────────────────────────────────────────────")

    return TrustProfile(
        poc=poc, vah=vah, val=val, hvn=hvn, lvn=lvn, bin_centers=centers, volume=sm,
        regime=regime, anchor_idx=0, window_bars=len(df),
        occupancy=occ, poc_shift_atr=shift, va_contiguous=contig, boot_spread_atr=0.0,
        eff_n=eff_n, hl_bars=hl_bars, trusted=bool(passed and sufficient),
        bin_atr_frac=bin_frac, blend_weight=0.0,
        ohlcv_approx=True, low_confidence=low_conf, notes=", ".join(notes))
