"""
Detector 3: Touch Count — Body + Wick weighted analysis.
=========================================================
Counts touches with dual weighting: body close 1.0, body open 0.7, wick 0.3.
Recency decay halflife = window_length / 3.

A "touch" requires the candle to actually reach the level:
  - Body close/open: within body_tolerance of the level
  - Wick: the high/low must cross or reach the level (not just be nearby)
"""

import numpy as np
from scipy.signal import argrelextrema
from typing import List

from core.models import SRLevel


class TouchCountDetector:

    def __init__(self, window_sizes=None, body_tolerance_pct=0.5,
                 wick_tolerance_pct=1.0, min_weighted_touches=1.5,
                 recency_halflife=None):
        self.window_sizes = window_sizes or [5, 10, 20]
        self.body_tol = body_tolerance_pct / 100.0
        self.wick_tol = wick_tolerance_pct / 100.0
        self.min_wt = min_weighted_touches
        self.halflife = recency_halflife

    def _find_candidates(self, df):
        candidates = set()
        for w in self.window_sizes:
            order = max(w // 2, 2)
            for idx in argrelextrema(df["low"].values, np.less_equal, order=order)[0]:
                candidates.add(float(df["low"].iloc[idx]))
            for idx in argrelextrema(df["high"].values, np.greater_equal, order=order)[0]:
                candidates.add(float(df["high"].iloc[idx]))
        return list(candidates)

    def _cluster(self, candidates):
        """Cluster nearby candidates, keeping the one with actual price (no averaging)."""
        if len(candidates) < 2:
            return candidates
        s = sorted(candidates)
        clusters = [[s[0]]]
        for p in s[1:]:
            if abs(p - clusters[-1][-1]) / clusters[-1][-1] < self.wick_tol:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        # Keep the most common price in each cluster (or median if all unique)
        result = []
        for cl in clusters:
            if len(cl) == 1:
                result.append(cl[0])
            else:
                # Keep the median — closest to actual tested price
                result.append(float(np.median(cl)))
        return result

    def _score(self, df, level):
        """Score a level by counting weighted touches with recency decay.
        Returns (weighted_score, raw_count, recency, best_touch_idx)."""
        o, c, h, l = df["open"].values, df["close"].values, df["high"].values, df["low"].values
        n = len(df)
        hl = self.halflife or max(n // 3, 5)
        bt = level * self.body_tol
        wt = level * self.wick_tol
        ws, rc, ti = 0.0, 0, []
        best_idx, best_weight = -1, 0.0

        for i in range(n):
            rw = np.exp(-np.log(2) * (n - 1 - i) / hl)
            touch_weight = 0.0

            # Body close touch (strongest)
            if abs(c[i] - level) <= bt:
                touch_weight = 1.0
            # Body open touch (medium)
            elif abs(o[i] - level) <= bt:
                touch_weight = 0.7
            # Wick touch: the wick must actually REACH the level
            # For a level below current candle range: low must reach down to the level
            # For a level above current candle range: high must reach up to the level
            elif l[i] <= level + wt and l[i] >= level - wt and l[i] <= level:
                # Wick low reached down to or below the level
                touch_weight = 0.3
            elif h[i] >= level - wt and h[i] <= level + wt and h[i] >= level:
                # Wick high reached up to or above the level
                touch_weight = 0.3

            if touch_weight > 0:
                weighted = touch_weight * rw
                ws += weighted
                rc += 1
                ti.append(i)
                if weighted > best_weight:
                    best_weight = weighted
                    best_idx = i

        rec = float(np.mean(np.exp(-np.log(2) * (n - 1 - np.array(ti)) / hl))) if ti else 0.0
        return ws, rc, rec, best_idx

    def detect(self, df):
        cands = self._cluster(self._find_candidates(df))
        cp = df["close"].iloc[-1]
        scored, mx = [], 0.01

        for p in cands:
            ws, rc, rec, best_idx = self._score(df, p)
            if ws >= self.min_wt:
                scored.append((p, ws, rc, rec, best_idx))
                mx = max(mx, ws)

        levels = []
        for p, ws, rc, rec, best_idx in scored:
            levels.append(SRLevel(
                price=p,
                level_type="support" if p < cp else "resistance",
                strength=round(min(1.0, 0.6 * (ws / mx) + 0.4 * rec), 4),
                method="touch_count",
                touches=rc,
                recency_score=round(rec, 4),
                anchor_type="wick",
                anchor_candle_idx=best_idx,
            ))
        return levels
