#!/usr/bin/env python3
"""
regime_anchor.py — Wyckoff regime anchor for the volume-profile levels build.
=============================================================================
Self-contained ANALYSIS module. Single owner of the regime ANCHOR that Step A
(``step_a_profile.build_profile``) uses to pick its window. It emits a per-bar
three-way Wyckoff series (wr-markup / wr-markdown / wr-transition), a debounced
confirmed state, and the ONSET timestamp of the current regime phase — dated at
the run's FIRST bar, not the bar where the debounce confirms it.

Pipeline (top to bottom):
    raw 6-way ADX/RSI labels (per bar, from research/regime.py math)
      -> fixed 3-way Wyckoff map
      -> debounce (CONFIRM_BARS consecutive bars; seed None, NOT "T")
      -> onset walk-back to the first bar of the current run
      -> latest-bar summary {wyckoff, onset_ts, low_confidence} + full series

Import rules (HARD — this module is on the analysis path, not execution):
  * Reuses the DETECTION primitive (classify_regime, _wilder_smooth, periods)
    from research/regime.py. That file is research code; importing it is fine.
    It is loaded BY PATH via importlib (lazily), exactly as step_a_profile.py
    does, because a `regime/` package at the repo root shadows the name on
    sys.path. Lazy load keeps the pure debounce/onset logic (and its unit test)
    free of regime.py and its deps.
  * Does NOT import core/filters.py or anything on the order/execution path.
    The confirmation/debounce logic is REIMPLEMENTED here in isolation. The one
    required change from the filters.py original is the seed: None, not "T"
    (a foreign U/D/T value here).

Out of scope (do NOT build here): true Wyckoff phase detection (accumulation /
distribution, springs, upthrusts, effort-vs-result) and the levels behavioural
gate (which levels are live, fade-edge vs breakout-line). This module emits the
wyckoff label only; the matrix is a downstream consumer.
"""
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# TRACKED DEBT — fixed literal constants for the 7-token build ONLY.
# These are the LAST literal counts in the anchor path. Before the top-50
# widening they must become volatility-derived. DO NOT do that here: a lookback
# that changes run-to-run can manufacture flips that are artifacts of the
# lookback CHANGING rather than the regime changing — poison for an anchor.
# (The DMI/ADX/RSI lookback periods are also tracked debt; they live in
#  research/regime.py as DMI_PERIOD / RSI_PERIOD / ADX_SLOPE_LOOKBACK and are
#  imported, not redefined here, so there is one source of truth.)
# ─────────────────────────────────────────────────────────────────────────────
CONFIRM_BARS = 3      # TRACKED DEBT: debounce — a switch locks in only after N
                      # consecutive bars of the new state. Must be > 1.
MIN_REGIME_BARS = 10  # TRACKED DEBT: if the current confirmed phase spans fewer
                      # than this many bars the window is too thin for a stable
                      # POC -> low_confidence (do not silently emit a thin result).


# Fixed 6-way (ADX/RSI) -> 3-way (Wyckoff) map. No carry-forward: every label
# resolves deterministically. None (warmup, no label yet) stays None.
_WYCKOFF_FROM_ADXRSI = {
    "BULL": "wr-markup",
    "BEAR": "wr-markdown",
    "RANGE": "wr-transition",
    "TRANSITION": "wr-transition",
    "NEUTRAL": "wr-transition",
    "UNDEFINED": "wr-transition",
}

WR_MARKUP, WR_MARKDOWN, WR_TRANSITION = "wr-markup", "wr-markdown", "wr-transition"


@dataclass
class RegimeAnchor:
    """Latest-bar summary + the full per-bar debounced series."""
    wyckoff: str                  # wr-markup | wr-markdown | wr-transition (latest bar)
    onset_ts: object              # timestamp of the FIRST bar of the current phase
    onset_idx: int                # positional index of onset (for df.iloc slicing)
    low_confidence: bool          # current phase thinner than MIN_REGIME_BARS
    wyckoff_series: pd.Series     # full per-bar debounced Wyckoff series, DatetimeIndexed
    notes: str = ""


# ── detection primitive: research/regime.py, loaded lazily BY PATH ───────────
_REG = None


def _detection():
    """Lazily import research/regime.py BY PATH and cache it.

    Loaded by path (not `from research.regime import …`) because a `regime/`
    package at the repo root shadows the name on sys.path — the same reason
    step_a_profile.py loads it explicitly. Lazy so importing this module (and
    running the debounce/onset unit test) does not require regime.py or its deps.
    """
    global _REG
    if _REG is None:
        import importlib.util
        import os
        rp = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "regime.py")
        spec = importlib.util.spec_from_file_location("_research_regime", rp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _REG = mod
    return _REG


# ── step 1: per-bar raw 6-way labels (seeded from step_a_profile._regime_labels)
def _raw_six_way_labels(df) -> List[Optional[str]]:
    """Per-bar 6-way ADX/RSI labels as a positional list of length n.

    Body is preserved verbatim from step_a_profile._regime_labels so the proven
    handling carries over exactly:
      * the indicator loop (Wilder DMI/ADX/RSI) over research/regime.py math,
      * the off-by-one: indicator lists are length n-1 (loops over range(1, n)),
        so list index j maps to df row j+1 -> labels[j + 1],
      * the warmup Nones (bars with no smoothed ADX yet stay None),
      * the rsi=50 warmup substitution while RSI is still None. PRESERVED as-is
        (not changed); flagged here so a future change is a conscious decision.
    """
    reg = _detection()
    _wilder_smooth = reg._wilder_smooth
    classify_regime = reg.classify_regime
    DMI_PERIOD = reg.DMI_PERIOD
    RSI_PERIOD = reg.RSI_PERIOD
    ADX_SLOPE_LOOKBACK = reg.ADX_SLOPE_LOOKBACK

    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(c)
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    dmp, dmm = [], []
    for i in range(1, n):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        dmp.append(max(up, 0) if up > dn else 0)
        dmm.append(max(dn, 0) if dn > up else 0)
    sdmp, sdmm, satr = (_wilder_smooth(dmp, DMI_PERIOD), _wilder_smooth(dmm, DMI_PERIOD),
                        _wilder_smooth(tr, DMI_PERIOD))
    dip, dim, dx = [], [], []
    for i in range(len(sdmp)):
        if satr[i] and satr[i] > 0:
            p, m = 100 * sdmp[i] / satr[i], 100 * sdmm[i] / satr[i]
        else:
            p = m = 0.0
        dip.append(p); dim.append(m); s = p + m
        dx.append(100 * abs(p - m) / s if s > 0 else 0.0)
    adx = _wilder_smooth(dx, DMI_PERIOD)
    gains = [max(c[i] - c[i - 1], 0) for i in range(1, n)]
    losses = [max(c[i - 1] - c[i], 0) for i in range(1, n)]
    rsi = [None] * len(gains)
    if len(gains) >= RSI_PERIOD:
        ag, al = sum(gains[:RSI_PERIOD]) / RSI_PERIOD, sum(losses[:RSI_PERIOD]) / RSI_PERIOD
        for i in range(RSI_PERIOD, len(gains)):
            ag = (ag * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
            al = (al * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
            rs = ag / al if al > 0 else 100
            rsi[i] = 100 - (100 / (1 + rs))
    labels: List[Optional[str]] = [None] * n
    for j in range(len(adx)):                       # list index j -> df row j+1
        if adx[j] is None:
            continue
        sl = adx[j] - adx[j - ADX_SLOPE_LOOKBACK] if (j >= ADX_SLOPE_LOOKBACK
              and adx[j - ADX_SLOPE_LOOKBACK] is not None) else 0
        ds = ((dip[j] - dim[j]) - (dip[j - ADX_SLOPE_LOOKBACK] - dim[j - ADX_SLOPE_LOOKBACK])
              if j >= ADX_SLOPE_LOOKBACK else 0)
        ind = {"adx": adx[j], "plus_di": dip[j], "minus_di": dim[j], "adx_slope": sl,
               "di_spread_slope": ds, "rsi": rsi[j] if rsi[j] is not None else 50}
        labels[j + 1] = classify_regime(ind)["regime"]
    return labels


def raw_wyckoff_series(df) -> pd.Series:
    """Per-bar 3-way Wyckoff labels, DatetimeIndexed by df.index.

    The addition _regime_labels lacks: carry the timestamp index instead of
    returning a positional list. Warmup bars (no label yet) are None.
    """
    six = _raw_six_way_labels(df)
    three = [(_WYCKOFF_FROM_ADXRSI.get(x) if x is not None else None) for x in six]
    return pd.Series(three, index=df.index, dtype=object)


# ── step 3: debounce (reimplemented from filters.py._apply_confirmation) ─────
def _debounce(raw: pd.Series, confirm_bars: int) -> pd.Series:
    """Confirmed 3-way series. A switch locks in only after `confirm_bars`
    CONSECUTIVE bars of the new state.

    Reimplementation of core.filters._apply_confirmation with ONE required
    change: the initial state is seeded to None (the original seeds "T", a value
    from the foreign U/D/T vocabulary), so warmup bars before the first confirmed
    state emit None — not a foreign label. None raw bars (warmup) hold the current
    state and never start or break a run (warmup Nones are front-only here).

    Behaviour preserved from the original: the confirmed flip is stamped at the
    run's END (lagged by confirm_bars - 1). The onset walk-back corrects this to
    the run's START.
    """
    vals = list(raw.values)
    confirmed: List[Optional[str]] = [None] * len(vals)
    current: Optional[str] = None
    pending: Optional[str] = None
    pending_count = 0
    for i, sig in enumerate(vals):
        if sig is None:                              # warmup / unknown: hold, no run
            confirmed[i] = current
            continue
        if sig == current:
            pending = None
            pending_count = 0
            confirmed[i] = current
        elif sig == pending:
            pending_count += 1
            if pending_count >= confirm_bars:
                current = pending
                pending = None
                pending_count = 0
            confirmed[i] = current
        else:
            pending = sig
            pending_count = 1
            if confirm_bars <= 1:
                current = sig
            confirmed[i] = current
    return pd.Series(confirmed, index=raw.index, dtype=object)


# ── step 4: onset walk-back (run START, not run END) ─────────────────────────
def _last_confirmed_change_idx(confirmed_vals: List[Optional[str]]) -> Optional[int]:
    """Positional index of the most recent bar where the confirmed state changed
    TO its current (latest) value — i.e. the run-END where the debounce fired.
    None if there is no confirmed state."""
    n = len(confirmed_vals)
    if n == 0 or confirmed_vals[-1] is None:
        return None
    current = confirmed_vals[-1]
    for i in range(n - 1, 0, -1):
        if confirmed_vals[i] == current and confirmed_vals[i - 1] != current:
            return i
    # current held from the very first confirmed bar
    for i in range(n):
        if confirmed_vals[i] == current:
            return i
    return None


def _onset_index(raw: pd.Series, confirmed: pd.Series) -> Optional[int]:
    """First bar of the run that produced the current confirmed regime.

    The debounce stamps the flip at the run's END (lagged). We anchor at the
    confirmation point (run-END) and walk back through the RAW labels while they
    still equal the current confirmed state — the first such contiguous bar is
    the onset. Granularity is 3-way: any change among wr-markup/wr-markdown/
    wr-transition is a flip (one rule, no exceptions — incl. a direct
    wr-markup<->wr-markdown reversal). The confirming run is guaranteed contiguous
    (the debounce only counts CONSECUTIVE equal raw bars), so this terminates at
    the true run start. Returns a positional index, or None if no confirmed
    regime exists yet.
    """
    confirmed_vals = list(confirmed.values)
    current = confirmed_vals[-1] if confirmed_vals else None
    if current is None:
        return None
    flip_end = _last_confirmed_change_idx(confirmed_vals)
    if flip_end is None:
        return None
    raw_vals = list(raw.values)
    onset = flip_end
    i = flip_end
    while i >= 0 and raw_vals[i] == current:
        onset = i
        i -= 1
    return onset


# ── public interface ─────────────────────────────────────────────────────────
def compute_anchor(df, confirm_bars: int = CONFIRM_BARS,
                   min_regime_bars: int = MIN_REGIME_BARS) -> RegimeAnchor:
    """Compute the regime anchor for a DatetimeIndexed OHLCV frame.

    Returns BOTH the latest-bar summary (wyckoff / onset_ts / low_confidence) and
    the full per-bar debounced Wyckoff series (wyckoff_series). onset_idx is the
    positional form of onset_ts, for df.iloc slicing by the caller.
    """
    raw = raw_wyckoff_series(df)
    confirmed = _debounce(raw, confirm_bars)
    wy = confirmed.iloc[-1] if len(confirmed) else None

    if wy is None:
        # No confirmed regime yet (series shorter than warmup + confirm_bars):
        # too thin to anchor — fall back to the full frame and flag it.
        idx0 = df.index[0] if len(df) else None
        return RegimeAnchor(
            wyckoff=WR_TRANSITION, onset_ts=idx0, onset_idx=0,
            low_confidence=True, wyckoff_series=confirmed,
            notes="no-confirmed-regime")

    onset_idx = _onset_index(raw, confirmed)
    if onset_idx is None:
        onset_idx = 0
    onset_ts = df.index[onset_idx]
    span = len(df) - onset_idx               # bars in the current phase, inclusive
    low_conf = span < min_regime_bars
    notes = "thin-regime(%d<%d)" % (span, min_regime_bars) if low_conf else ""
    return RegimeAnchor(
        wyckoff=wy, onset_ts=onset_ts, onset_idx=int(onset_idx),
        low_confidence=bool(low_conf), wyckoff_series=confirmed, notes=notes)
