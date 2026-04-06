"""
SRDetector — Ensemble orchestrator for the 5 detection methods.
================================================================
Runs all detectors, merges levels with strength-weighted pricing
and multi-method bonus.
"""

import pandas as pd
from typing import List, Optional

from core.models import SRLevel
from core import config
from .market_structure import MarketStructureDetector
from .volume_profile import VolumeProfileDetector
from .touch_count import TouchCountDetector
from .nison_body import NisonBodyDetector
from .polarity_flip import PolarityFlipDetector


class SRDetector:
    """Runs all 5 methods, merges, scores."""

    def __init__(self, df, detector_config=None):
        self.df = self._validate_df(df)
        self.config = detector_config or {}
        self.market_structure = MarketStructureDetector(**self.config.get("market_structure", {}))
        self.volume = VolumeProfileDetector(**self.config.get("volume", {}))
        self.touch = TouchCountDetector(**self.config.get("touch", {}))
        self.nison = NisonBodyDetector(**self.config.get("nison", {}))
        self.polarity = PolarityFlipDetector(**self.config.get("polarity", {}))

    @staticmethod
    def _validate_df(df):
        df = df.copy()
        df.columns = [c.lower().strip() for c in df.columns]
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing required columns: {missing}")
        if "volume" not in df.columns:
            df["volume"] = 1.0
        return df

    def detect_market_structure(self): return self.market_structure.detect(self.df)
    def detect_volume_profile(self): return self.volume.detect(self.df)
    def detect_touch_count(self): return self.touch.detect(self.df)
    def detect_nison_body(self): return self.nison.detect(self.df)
    def detect_polarity_flip(self): return self.polarity.detect(self.df)

    def detect_all(self, methods=None, max_levels=20, min_strength=0.1):
        all_levels = []
        method_map = {
            "structure": self.detect_market_structure,
            "volume": self.detect_volume_profile,
            "touch": self.detect_touch_count,
            "nison": self.detect_nison_body,
            "polarity": self.detect_polarity_flip,
        }
        for name in (methods or list(method_map.keys())):
            if name in method_map:
                try:
                    all_levels.extend(method_map[name]())
                except Exception as e:
                    print(f"[SRDetector] Warning: {name} failed: {e}")

        merged = self._merge_levels(all_levels)
        merged = [l for l in merged if l.strength >= min_strength]
        merged.sort(key=lambda l: l.strength, reverse=True)
        return merged[:max_levels]

    def _merge_levels(self, levels):
        if not levels:
            return []
        sl = sorted(levels, key=lambda l: l.price)
        merged, used = [], [False] * len(sl)
        for i, lv in enumerate(sl):
            if used[i]:
                continue
            group = [lv]; used[i] = True
            for j in range(i + 1, len(sl)):
                if used[j]:
                    continue
                if abs(sl[j].price - lv.price) / lv.price <= config.MERGE_DISTANCE_PCT / 100.0:
                    group.append(sl[j]); used[j] = True
                else:
                    break
            merged.append(self._merge_group(group))
        return merged

    def _get_method_key(self, method_str):
        if "market_structure" in method_str: return "market_structure"
        if "volume" in method_str: return "volume_profile"
        if "touch" in method_str: return "touch_count"
        if "nison" in method_str: return "nison_body"
        if "polarity" in method_str: return "polarity_flip"
        return ""

    def _merge_group(self, group):
        """Merge a cluster of nearby levels using strength-weighted pricing.

        Price: strength-weighted average across all levels, body levels get
        a 1.5x weight bonus so the price leans toward body edges when close.

        Metadata (anchor_type, structural_role): from the best body level.
        """
        BODY_WEIGHT_BONUS = config.BODY_WEIGHT_BONUS

        cp = self.df["close"].iloc[-1]
        methods_present = set()
        for l in group:
            k = self._get_method_key(l.method)
            if k:
                methods_present.add(k)

        # ── Price: strength-weighted average, body bonus ──
        weighted_sum = 0.0
        weight_total = 0.0
        for l in group:
            w = l.strength
            if l.anchor_type == "body":
                w *= BODY_WEIGHT_BONUS
            weighted_sum += l.price * w
            weight_total += w

        fp = weighted_sum / max(weight_total, 1e-12)

        # ── Metadata: still body-first ──
        body_lvls = [l for l in group if l.anchor_type == "body"]
        wick_lvls = [l for l in group if l.anchor_type == "wick"]

        if body_lvls:
            best = max(body_lvls, key=lambda l: l.strength)
            at, ai, sr = "body", best.anchor_candle_idx, best.structural_role
        elif wick_lvls:
            best = max(wick_lvls, key=lambda l: l.strength)
            at, ai, sr = "wick", best.anchor_candle_idx, best.structural_role
        else:
            at, ai, sr = "blended", -1, ""

        lt = "support" if fp < cp else "resistance"

        # ── Ensemble score ──
        es = 0.0
        for l in group:
            k = self._get_method_key(l.method)
            w = config.METHOD_WEIGHTS.get(k, 0.20)
            es += l.strength * w

        # Multi-method bonus
        es = min(1.0, es + min(config.MULTI_METHOD_BONUS_CAP,
                               (len(methods_present) - 1) * config.MULTI_METHOD_BONUS_PER))

        # Polarity flip bonus
        if "polarity_flip" in methods_present:
            es = min(1.0, es + config.POLARITY_FLIP_BONUS)

        # Best structural role
        roles = [l.structural_role for l in group if l.structural_role]
        br = ""
        for p in ["CHOCH", "flip", "BOS", "HL", "LH", "HH", "LL"]:
            for r in roles:
                if p in r:
                    br = r; break
            if br:
                break

        return SRLevel(
            price=round(fp, 8), level_type=lt, strength=round(es, 4),
            method="+".join(sorted(methods_present)),
            touches=sum(l.touches for l in group),
            volume_weight=max((l.volume_weight for l in group), default=0),
            recency_score=max((l.recency_score for l in group), default=0),
            mtf_confluence=0, anchor_type=at, anchor_candle_idx=ai,
            structural_role=br,
        )

    def nearest_support(self, levels, n=3):
        cp = self.df["close"].iloc[-1]
        s = [l for l in levels if l.price < cp]
        s.sort(key=lambda l: cp - l.price)
        return s[:n]

    def nearest_resistance(self, levels, n=3):
        cp = self.df["close"].iloc[-1]
        r = [l for l in levels if l.price > cp]
        r.sort(key=lambda l: l.price - cp)
        return r[:n]
