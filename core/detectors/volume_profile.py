"""
Detector 2: Volume Profile — VPVR-style analysis.
===================================================
Builds a volume-by-price histogram with recency weighting,
identifies HVN as S/R levels. Also identifies POC and Value Area.

Recent candles contribute more volume than old ones (exponential decay).
"""

import numpy as np
from scipy.signal import argrelextrema
from scipy.ndimage import uniform_filter1d
from typing import List

from core.models import SRLevel


class VolumeProfileDetector:

    def __init__(self, num_bins=200, value_area_pct=0.70, hvn_threshold_percentile=75,
                 recency_halflife=None):
        self.num_bins = num_bins
        self.value_area_pct = value_area_pct
        self.hvn_threshold_percentile = hvn_threshold_percentile
        self.recency_halflife = recency_halflife  # None = auto (n/3)

    def _build_profile(self, df):
        price_low, price_high = df["low"].min(), df["high"].max()
        bin_edges = np.linspace(price_low, price_high, self.num_bins + 1)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        volume_profile = np.zeros(self.num_bins)

        n = len(df)
        halflife = self.recency_halflife or max(n // 3, 10)

        for i, (_, row) in enumerate(df.iterrows()):
            bin_mask = (bin_centers >= row["low"]) & (bin_centers <= row["high"])
            n_bins = np.sum(bin_mask)
            if n_bins > 0:
                vol = row["volume"] if "volume" in row.index else 1.0
                # Recency weight: recent candles contribute more
                recency_weight = np.exp(-np.log(2) * (n - 1 - i) / halflife)
                volume_profile[bin_mask] += vol * recency_weight / n_bins

        return bin_centers, volume_profile

    def _find_hvn(self, bin_centers, volume_profile):
        threshold = np.percentile(volume_profile, self.hvn_threshold_percentile)
        smoothed = uniform_filter1d(volume_profile, size=max(3, self.num_bins // 20))
        peak_indices = argrelextrema(smoothed, np.greater, order=3)[0]
        peak_indices = [i for i in peak_indices if smoothed[i] >= threshold]
        max_vol = smoothed.max() if smoothed.max() > 0 else 1.0
        return [{"price": float(bin_centers[i]), "volume_weight": float(smoothed[i] / max_vol)}
                for i in peak_indices]

    def _find_poc_and_va(self, bin_centers, volume_profile):
        poc_idx = np.argmax(volume_profile)
        total_vol = volume_profile.sum()
        target_vol = total_vol * self.value_area_pct
        low_idx = high_idx = poc_idx
        accumulated = volume_profile[poc_idx]

        while accumulated < target_vol:
            el = volume_profile[low_idx - 1] if low_idx > 0 else 0
            eh = volume_profile[high_idx + 1] if high_idx < len(volume_profile) - 1 else 0
            if el >= eh and low_idx > 0:
                low_idx -= 1
                accumulated += volume_profile[low_idx]
            elif high_idx < len(volume_profile) - 1:
                high_idx += 1
                accumulated += volume_profile[high_idx]
            else:
                break
        return {"poc": float(bin_centers[poc_idx]),
                "vah": float(bin_centers[high_idx]),
                "val": float(bin_centers[low_idx])}

    def detect(self, df):
        bin_centers, vp = self._build_profile(df)
        hvns = self._find_hvn(bin_centers, vp)
        poc_va = self._find_poc_and_va(bin_centers, vp)
        cp = df["close"].iloc[-1]
        levels = []

        for node in hvns:
            levels.append(SRLevel(
                price=node["price"],
                level_type="support" if node["price"] < cp else "resistance",
                strength=node["volume_weight"],
                method="volume_profile",
                volume_weight=node["volume_weight"],
            ))
        for key, price in poc_va.items():
            levels.append(SRLevel(
                price=price,
                level_type="support" if price < cp else "resistance",
                strength=0.9 if key == "poc" else 0.7,
                method=f"volume_profile_{'POC' if key == 'poc' else 'VAH' if key == 'vah' else 'VAL'}",
                volume_weight=0.9 if key == "poc" else 0.7,
            ))
        return levels
