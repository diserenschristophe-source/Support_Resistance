"""
Detector 4: Nison Body — Large-candle body edges as S/R.
=========================================================
Steve Nison methodology: large candle body edges mark institutional levels.

Bullish large candle → open (body bottom) is support
Bearish large candle → open (body top) is resistance

This detector stays body-only by design — it's measuring institutional
footprints, not price rejection (that's Market Structure's job).
"""

import numpy as np
from typing import List

from core.models import SRLevel, compute_atr, compute_atr_series


class NisonBodyDetector:

    def __init__(self, atr_multiplier=1.5, atr_period=14,
                 min_cluster_size=1, recency_halflife=40):
        self.atr_mult = atr_multiplier
        self.atr_period = atr_period
        self.min_cs = min_cluster_size
        self.rec_hl = recency_halflife

    def detect(self, df):
        o, c, cp, n = df["open"].values, df["close"].values, df["close"].iloc[-1], len(df)
        atr_series = compute_atr_series(df, self.atr_period)
        atr = compute_atr(df)
        bs = np.abs(c - o)
        sup, res, sm, rm = [], [], [], []

        for i in range(n):
            if bs[i] <= atr_series[i] * self.atr_mult:
                continue
            if c[i] > o[i]:
                # Bullish large candle: open (body bottom) is support
                sup.append(o[i]); sm.append((i, bs[i]))
            elif c[i] < o[i]:
                # Bearish large candle: open (body top) is resistance
                res.append(o[i]); rm.append((i, bs[i]))

        levels = []
        for pts, meta, lt in [(sup, sm, "support"), (res, rm, "resistance")]:
            if not pts:
                continue
            for cl in self._cluster(pts, meta, cp, n, atr):
                levels.append(SRLevel(
                    price=cl["price"], level_type=lt, strength=cl["strength"],
                    method="nison_body", touches=cl["count"],
                    recency_score=cl["recency"], anchor_type="body",
                    anchor_candle_idx=cl["best_idx"],
                ))
        return levels

    def _cluster(self, prices, meta, cp, nb, atr):
        """Cluster nearby body edges using 1x ATR as merge distance.
        Keep the price of the largest candle in each cluster (no averaging)."""
        if not prices:
            return []
        merge_dist = atr  # 1x ATR merge distance
        si = np.argsort(prices)
        used = [False] * len(prices)
        clusters = []

        for i in si:
            if used[i]:
                continue
            gp, gm = [prices[i]], [meta[i]]
            used[i] = True
            for j in si:
                if not used[j] and abs(prices[j] - prices[i]) <= merge_dist:
                    gp.append(prices[j]); gm.append(meta[j]); used[j] = True
            if len(gp) < self.min_cs:
                continue

            # Keep the price of the largest body candle (strongest footprint)
            bsz = np.array([m[1] for m in gm])
            largest_idx = int(np.argmax(bsz))
            anchor_price = gp[largest_idx]
            anchor_bar = int(gm[largest_idx][0])

            idx = np.array([m[0] for m in gm])
            rec = float(np.exp(-np.log(2) * (nb - 1 - idx) / self.rec_hl).mean())
            cnt = len(gp)
            bn = min(1.0, float(bsz.mean()) / (cp * 0.03))
            st = 0.4 * min(1.0, cnt / 5) + 0.3 * bn + 0.3 * rec

            clusters.append({"price": round(anchor_price, 8), "count": cnt,
                             "recency": round(rec, 4), "strength": round(st, 4),
                             "best_idx": anchor_bar})
        return clusters
