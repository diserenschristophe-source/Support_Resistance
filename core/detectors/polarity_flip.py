"""
Detector 5: Polarity Flip — Support↔Resistance proven on both sides.
=====================================================================
Detects levels that acted as support then flipped to resistance (or vice versa),
confirmed by body closes with temporal ordering.

A valid flip requires:
  1. Multiple touches as support (body bounces above the level)
  2. A break through the level
  3. Multiple touches as resistance (body rejected below the level)
  (or the reverse: resistance first, then support)

Uses body edges for candidates and touch counting (orthogonal to Market Structure
which uses wicks for swing detection).
"""

import numpy as np
from typing import List

from core.models import SRLevel, compute_atr


class PolarityFlipDetector:

    def __init__(self, tolerance_atr_mult=0.5, min_touches_per_side=2,
                 min_level_distance_pct=0.5):
        self.tol_mult = tolerance_atr_mult
        self.min_tps = min_touches_per_side
        self.min_level_distance_pct = min_level_distance_pct / 100.0

    def detect(self, df):
        cp, n = df["close"].iloc[-1], len(df)
        atr = compute_atr(df)
        tol = atr * self.tol_mult
        o, c = df["open"].values, df["close"].values
        bs = np.abs(c - o)
        avg_bs = np.mean(bs)

        # Step 1: Build candidates from significant body edges
        cands = set()
        for i in range(n):
            if bs[i] < avg_bs * 0.5:
                continue
            cands.add(round(min(o[i], c[i]), 8))
            cands.add(round(max(o[i], c[i]), 8))

        # Step 2: Cluster nearby candidates
        sc = sorted(cands)
        clustered = []
        if sc:
            cl = [sc[0]]
            for v in sc[1:]:
                if abs(v - cl[-1]) < tol:
                    cl.append(v)
                else:
                    clustered.append(np.mean(cl)); cl = [v]
            clustered.append(np.mean(cl))

        # Step 3: For each level, find temporally ordered touches
        levels = []
        for level in clustered:
            # Classify each candle's relationship to the level
            # Skip candles that are "at" the level (body straddles it)
            touches = []  # list of (index, "S" or "R")

            for i in range(n):
                blo = min(o[i], c[i])
                bhi = max(o[i], c[i])

                # Skip if body straddles the level (sitting on it = no clear signal)
                if blo < level - tol * 0.5 and bhi > level + tol * 0.5:
                    continue

                # Support touch: body low near level, candle closed clearly above
                if abs(blo - level) <= tol and c[i] > level + tol * 0.5:
                    touches.append((i, "S"))
                # Resistance touch: body high near level, candle closed clearly below
                elif abs(bhi - level) <= tol and c[i] < level - tol * 0.5:
                    touches.append((i, "R"))

            if len(touches) < self.min_tps * 2:
                continue

            # Step 4: Check temporal ordering — find the best flip sequence
            # Look for S→R or R→S transition with enough touches on each side
            flip = self._find_flip_sequence(touches)
            if not flip:
                continue

            flip_type = flip["type"]      # "S_to_R" or "R_to_S"
            s_count = flip["s_count"]
            r_count = flip["r_count"]
            last_touch_idx = flip["last_touch_idx"]
            flip_idx = flip["flip_idx"]   # where the flip happened

            # Current role: what is it acting as NOW (after the flip)
            if flip_type == "S_to_R":
                # Was support, now resistance
                lt = "resistance"
            else:
                # Was resistance, now support
                lt = "support"

            # Override with price position if the flip is old and price has moved far
            if lt == "resistance" and level < cp * 0.97:
                lt = "support"  # level is well below price, acting as support now
            elif lt == "support" and level > cp * 1.03:
                lt = "resistance"  # level is well above price, acting as resistance now

            tt = s_count + r_count
            rec = 1.0 - (n - 1 - last_touch_idx) / max(n, 1)
            strength = min(1.0,
                0.4 * min(1.0, tt / 10) +
                0.3 * min(1.0, min(s_count, r_count) / 3) +
                0.3 * rec)

            levels.append(SRLevel(
                price=round(float(level), 8), level_type=lt,
                strength=round(strength, 4), method="polarity_flip",
                touches=tt, recency_score=round(rec, 4),
                anchor_type="body", structural_role="flip",
                anchor_candle_idx=flip_idx,
            ))
        return levels

    def _find_flip_sequence(self, touches) -> dict:
        """
        Find the best S→R or R→S flip in the touch sequence.
        Returns None if no valid flip exists.
        """
        if len(touches) < 4:
            return None

        # Try S→R: find a run of S touches followed by a run of R touches
        best = None

        # Walk through and find transition points
        for split in range(self.min_tps, len(touches) - self.min_tps + 1):
            before = touches[:split]
            after = touches[split:]

            # S→R: mostly S before, mostly R after
            s_before = sum(1 for _, t in before if t == "S")
            r_after = sum(1 for _, t in after if t == "R")

            if s_before >= self.min_tps and r_after >= self.min_tps:
                quality = s_before + r_after
                if best is None or quality > best["quality"]:
                    best = {
                        "type": "S_to_R",
                        "s_count": s_before,
                        "r_count": r_after,
                        "quality": quality,
                        "flip_idx": touches[split][0],
                        "last_touch_idx": touches[-1][0],
                    }

            # R→S: mostly R before, mostly S after
            r_before = sum(1 for _, t in before if t == "R")
            s_after = sum(1 for _, t in after if t == "S")

            if r_before >= self.min_tps and s_after >= self.min_tps:
                quality = r_before + s_after
                if best is None or quality > best["quality"]:
                    best = {
                        "type": "R_to_S",
                        "s_count": s_after,
                        "r_count": r_before,
                        "quality": quality,
                        "flip_idx": touches[split][0],
                        "last_touch_idx": touches[-1][0],
                    }

        return best
