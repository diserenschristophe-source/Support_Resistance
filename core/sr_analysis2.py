"""
SR Analysis V2 — Multi-Timeframe Orchestrator with candle alignment.
=====================================================================
Runs the 5-method ensemble (via SRDetector2) across N lookback windows,
converts raw levels into tradeable zones with snap_price candle alignment,
and produces the final analysis output.

Key V2 differences from V1:
  - snap_price preserves the actual candle price through the pipeline
  - ATR-based anchor-stable dedup (prevents chain drift)
  - Seiden-inspired scoring: freshness > touches, confluence bonus
  - Minimum spacing enforcement in final selection
  - TA-friendly display rounding (ta_round) applied last

Pipeline:
  1. Run 5-method ensemble on N windows (20d/60d/180d)
  2. ATR-based dedup across windows (anchor-stable)
  3. Inject POC guarantee
  4. Convert to zones (snap_price → key_level, no body/round snapping)
  5. Merge nearby (0.5 ATR), classify tiers, rank
  6. SMA injection, backfill, Fibonacci enrichment
  7. Cap with spacing enforcement (0.75 ATR min between levels)
  8. Boundary guards, cross-side dedup
  9. TA-friendly display rounding on key_level
"""

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from scipy.signal import argrelextrema
from typing import List, Optional, Dict, Any

from core.models import SRLevel, SRZone, compute_atr
from core import config
from core.detectors.ensemble2 import SRDetector2 as SRDetector


def smart_round(val, price=None):
    """Round to appropriate precision based on price magnitude."""
    ref = abs(price) if price else abs(val)
    if ref == 0: return 0
    if ref >= 1000: return round(val)
    if ref >= 10: return round(val, 1)
    if ref >= 1: return round(val, 2)
    if ref >= 0.01: return round(val, 4)
    if ref >= 0.0001: return round(val, 6)
    if ref >= 0.000001: return round(val, 8)
    return round(val, 10)


def ta_round(val, price=None):
    """Round to TA-friendly display precision.

    Analysts quote psychologically meaningful numbers — $71,000 not $70,983.
    Applied only to key_level for chart display, never to internal calculations.
    """
    ref = abs(price) if price else abs(val)
    if ref == 0: return 0
    if ref >= 50000: return round(val / 100) * 100      # BTC: nearest $100
    if ref >= 10000: return round(val / 50) * 50         # nearest $50
    if ref >= 1000:  return round(val / 5) * 5           # ETH: nearest $5
    if ref >= 100:   return round(val)                   # nearest $1
    if ref >= 10:    return round(val * 2) / 2           # SOL: nearest $0.50
    if ref >= 1:     return round(val, 1)                # nearest $0.10
    if ref >= 0.01:  return round(val, 3)                # nearest $0.001
    if ref >= 0.0001: return round(val, 5)
    if ref >= 0.000001: return round(val, 7)
    return round(val, 9)


class ProfessionalSRAnalysis2:

    def __init__(self, df: pd.DataFrame, sr_config: Optional[dict] = None,
                 symbol: str = "", data_dir: str = "data"):
        self.df = df.copy()
        self.df.columns = [c.lower().strip() for c in self.df.columns]
        self.current_price = float(self.df["close"].iloc[-1])
        self.config = sr_config or {}
        self.atr = compute_atr(self.df)
        self.symbol = symbol.upper()
        self.data_dir = data_dir

    # ── Market Structure ──────────────────────────────────────

    def get_market_structure(self):
        close = self.df["close"]
        price = self.current_price
        sma = {}
        for p in [20, 50, 100, 200]:
            sma[p] = float(close.rolling(p).mean().iloc[-1]) if len(close) >= p else None

        if sma[50] and sma[200]:
            if sma[50] > sma[200] and price > sma[50]: trend = "Bullish"
            elif sma[50] < sma[200] and price < sma[50]: trend = "Bearish"
            elif price > sma[50]: trend = "Recovering"
            else: trend = "Weakening"
        elif sma[50]:
            trend = "Bullish" if price > sma[50] else "Bearish"
        else:
            trend = "Bullish (ST)" if close.iloc[-1] > close.iloc[-len(close)//3] else "Bearish (ST)"

        opens = self.df["open"].values
        closes = self.df["close"].values
        body_highs = np.maximum(opens, closes)
        body_lows = np.minimum(opens, closes)

        hi_idx = argrelextrema(body_highs, np.greater, order=10)[0]
        lo_idx = argrelextrema(body_lows, np.less, order=10)[0]
        structure = "Undefined"
        bias = 0.0
        if len(hi_idx) >= 2 and len(lo_idx) >= 2:
            hh = body_highs[hi_idx[-1]] > body_highs[hi_idx[-2]]
            hl = body_lows[lo_idx[-1]] > body_lows[lo_idx[-2]]
            if hh and hl: structure = "HH + HL (Bullish)"; bias = 1.0
            elif not hh and not hl: structure = "LH + LL (Bearish)"; bias = -1.0
            elif hh: structure = "HH + LL (Transition)"; bias = 0.0
            else: structure = "LH + HL (Compression)"; bias = 0.0

        return {"trend": trend, "structure": structure, "bias": bias,
                "current_price": price, "atr14": self.atr,
                "sma20": sma[20], "sma50": sma[50], "sma100": sma[100], "sma200": sma[200]}

    # ── Volume Profile Summary ────────────────────────────────

    def get_volume_profile_summary(self):
        detector = SRDetector(self.df, self.config)
        vp = detector.detect_volume_profile()
        out = {"poc": None, "vah": None, "val": None}
        for l in vp:
            if "POC" in l.method: out["poc"] = l.price
            elif "VAH" in l.method: out["vah"] = l.price
            elif "VAL" in l.method: out["val"] = l.price
        return out

    # ── Volume Confirmation ───────────────────────────────────

    def _check_volume_at_level(self, level_price: float) -> bool:
        band = self.atr * 0.5
        volumes = self.df["volume"].values
        highs = self.df["high"].values
        lows = self.df["low"].values
        touched = ((lows <= level_price + band) & (highs >= level_price - band))
        if touched.sum() < 2:
            return False
        return volumes[touched].mean() > volumes.mean() * 1.2

    # ── Level → Zone ──────────────────────────────────────────

    def _level_to_zone(self, level: SRLevel, structure_bias: float = 0.0) -> SRZone:
        hw = self.atr * config.ZONE_ATR_MULT
        price = level.price  # blended (for mid_price / zone boundaries)

        # key_level from snap_price (actual candle), fallback to price
        key = level.snap_price if level.snap_price > 0 else price
        anchor_type = "Body" if level.anchor_type == "body" else "Wick"

        # Date from snap_candle_idx (absolute)
        anchor_date = ""
        if 0 <= level.snap_candle_idx < len(self.df):
            anchor_date = str(self.df.index[level.snap_candle_idx])[:10]

        level_type = level.level_type
        if level_type == "support" and key > self.current_price:
            key = price
            if price > self.current_price:
                level_type = "resistance"
        elif level_type == "resistance" and key < self.current_price:
            key = price
            if price < self.current_price:
                level_type = "support"

        n_methods = len(level.method.split("+"))
        vol_confirmed = self._check_volume_at_level(price)

        has_polarity_flip = "flip" in (level.structural_role or "")
        has_choch = "CHOCH" in (level.structural_role or "")

        tier = "Major" if (n_methods >= 2 or level.strength >= 0.5
                          or has_polarity_flip or has_choch) else "Minor"

        if structure_bias < -0.5 and level_type == "resistance" and tier == "Minor":
            if n_methods >= 2 or level.touches >= 10:
                tier = "Major"
        elif structure_bias > 0.5 and level_type == "support" and tier == "Minor":
            if n_methods >= 2 or level.touches >= 10:
                tier = "Major"

        structural_tag = f" [{level.structural_role}]" if level.structural_role else ""
        if level_type == "support":
            act = f"Support at ${key:,.0f}{structural_tag}. Stop below ${price - hw:,.0f}"
        else:
            act = f"Resistance at ${key:,.0f}{structural_tag}. Breakout above ${price + hw:,.0f}"

        return SRZone(
            price_low=smart_round(price - hw, self.current_price),
            price_high=smart_round(price + hw, self.current_price),
            mid_price=smart_round(price, self.current_price), key_level=key,
            zone_type=level_type, tier=tier,
            confluence_score=n_methods, touches=level.touches,
            volume_confirmed=vol_confirmed,
            label=f"{'S' if level_type=='support' else 'R'}: ${price:,.0f}",
            action=act, anchor_type=anchor_type, anchor_candle_date=anchor_date,
            structural_role=level.structural_role or "",
        )

    def _merge_nearby(self, zones):
        """Merge zones within 0.5x ATR using consecutive grouping.

        Groups sorted zones where each consecutive pair is within 0.5 ATR.
        For each cluster, the highest-scored zone (by _zone_score) provides
        both key_level and metadata.  Touches are summed across the cluster.
        """
        if len(zones) < 2:
            return zones

        merge_dist = self.atr * 0.5
        s = sorted(zones, key=lambda z: z.key_level)

        # Group consecutive zones within 0.5 ATR of each other.
        clusters = []
        current_group = [s[0]]
        for i in range(1, len(s)):
            if abs(s[i].key_level - current_group[-1].key_level) <= merge_dist:
                current_group.append(s[i])
            else:
                clusters.append(current_group)
                current_group = [s[i]]
        clusters.append(current_group)

        # Step 2: Resolve each cluster
        out = []
        for group in clusters:
            if len(group) == 1:
                out.append(group[0])
                continue

            # Highest-scored zone provides both mid_price and key_level
            best = max(group, key=lambda z: self._zone_score(z))

            hw = self.atr * config.ZONE_ATR_MULT
            mid = best.mid_price

            merged = SRZone(
                price_low=smart_round(mid - hw, mid),
                price_high=smart_round(mid + hw, mid),
                mid_price=smart_round(mid, mid),
                key_level=best.key_level,
                zone_type=best.zone_type,
                tier=best.tier,
                confluence_score=max(z.confluence_score for z in group),
                touches=sum(z.touches for z in group),
                volume_confirmed=any(z.volume_confirmed for z in group),
                label=best.label,
                action=best.action,
                notes=best.notes,
                anchor_type=best.anchor_type,
                anchor_candle_date=best.anchor_candle_date,
                structural_role=best.structural_role,
            )

            out.append(merged)

        out.sort(key=lambda z: z.mid_price)
        return out

    def _zone_score(self, z):
        """Structure-first scoring with recency. Used for merge and ranking."""
        role = z.structural_role or ""
        if "CHOCH" in role:       structure = 30
        elif "flip" in role:      structure = 25
        elif "BOS" in role:       structure = 10
        else:                     structure = 0

        confluence = (z.confluence_score - 1) * 5   # 0/5/10 for 1/2/3 methods
        base = (structure
                + confluence
                + min(z.touches, 5)
                + (5 if z.volume_confirmed else 0))

        # Recency: exponential decay, halflife 180 days
        recency = 1.0
        if z.anchor_candle_date:
            try:
                age_days = (pd.Timestamp.now(tz="UTC")
                            - pd.Timestamp(z.anchor_candle_date, tz="UTC")).days
                recency = max(0.3, float(np.exp(-age_days / 180)))
            except Exception:
                pass

        return base * recency

    def _level_score(self, l):
        """Structure-first scoring with recency for SRLevel (pre-zone conversion)."""
        role = l.structural_role or ""
        if "CHOCH" in role:       structure = 30
        elif "flip" in role:      structure = 25
        elif "BOS" in role:       structure = 10
        else:                     structure = 0
        n_methods = len(l.method.split("+"))
        confluence = (n_methods - 1) * 5   # 0/5/10 for 1/2/3 methods
        base = structure + min(l.touches, 5) + l.strength * 10 + confluence

        # Recency from snap_candle_idx (absolute), halflife 180 bars
        recency = 1.0
        if l.snap_candle_idx >= 0 and len(self.df) > 0:
            bars_ago = len(self.df) - 1 - l.snap_candle_idx
            recency = max(0.3, float(np.exp(-bars_ago / 180)))
        return base * recency

    def _drop_sandwiched_minors(self, zones):
        """Remove Minor zones that are sandwiched between two Majors within 1x ATR."""
        if len(zones) < 3:
            return zones
        majors = [z for z in zones if z.tier == "Major"]
        if len(majors) < 2:
            return zones

        to_remove = set()
        sorted_zones = sorted(zones, key=lambda z: z.mid_price)
        for i, z in enumerate(sorted_zones):
            if z.tier != "Minor":
                continue
            # Check if there's a Major below and above within 1x ATR
            has_major_below = any(
                m.mid_price < z.mid_price and abs(z.mid_price - m.mid_price) <= self.atr
                for m in majors
            )
            has_major_above = any(
                m.mid_price > z.mid_price and abs(m.mid_price - z.mid_price) <= self.atr
                for m in majors
            )
            if has_major_below and has_major_above:
                to_remove.add(id(z))

        if to_remove:
            return [z for z in zones if id(z) not in to_remove]
        return zones

    @staticmethod
    def _pick_targets(zones, current_price):
        if not zones: return None, None
        s = sorted(zones, key=lambda z: z.mid_price)
        majors = [z for z in s if z.tier == "Major"]
        nearest = s[0]
        if majors:
            fm = majors[0]
            gap = abs(fm.mid_price - current_price) / current_price
            if nearest.tier == "Minor" and fm != nearest and gap > 0.15:
                return nearest, fm
            return fm, (majors[1] if len(majors) > 1 else None)
        return nearest, (s[1] if len(s) > 1 else None)

    def _enrich_notes(self, zone, fib_levels, structure):
        notes = []
        if zone.notes: notes.append(zone.notes)
        if zone.volume_confirmed: notes.append("Volume confirmed")
        if zone.structural_role:
            if "flip" in zone.structural_role:
                notes.append("Polarity flip")
            elif "CHOCH" in zone.structural_role:
                notes.append(f"Structure: {zone.structural_role}")

        for label, fp in fib_levels.items():
            if abs(zone.mid_price - fp) / fp < 0.03:
                notes.append(f"Fib {label}"); break
        for name, val in [("SMA 20", structure["sma20"]), ("SMA 50", structure["sma50"]),
                          ("SMA 100", structure["sma100"]), ("SMA 200", structure["sma200"])]:
            if val and abs(zone.mid_price - val) / val < 0.025:
                notes.append(f"{name} (${val:,.0f})"); break
        zone.notes = " | ".join(notes)

    # ── Multi-Window Ensemble ─────────────────────────────────

    def _run_window(self, days: int, weight: float) -> List[SRLevel]:
        if len(self.df) < days:
            days = len(self.df)
        window_df = self.df.iloc[-days:]
        if len(window_df) < 20:
            return []

        cfg = config.get_detector_config(days)

        try:
            detector = SRDetector(window_df, cfg)
            levels = detector.detect_all(max_levels=15, min_strength=config.MIN_STRENGTH)
        except Exception:
            return []

        window_offset = len(self.df) - days
        for l in levels:
            l.strength = l.strength * weight
            # Convert window-relative snap_candle_idx to absolute
            if l.snap_candle_idx >= 0:
                l.snap_candle_idx += window_offset
        return levels

    # ── Main Analysis ─────────────────────────────────────────

    def analyze(self):
        target = config.MAX_ZONES_PER_SIDE
        structure = self.get_market_structure()
        vp = self.get_volume_profile_summary()
        bias = structure["bias"]

        # Step 1: Run ensemble on N windows (over-provisioned cap)
        overprovision_cap = target * 3
        all_levels = []
        for w in config.WINDOWS:
            if len(self.df) >= w["days"]:
                all_levels.extend(self._run_window(w["days"], w["weight"]))

        # Step 2: Deduplicate across windows (ATR-based, anchor-stable)
        # Keep the most structurally significant level when windows overlap.
        # Anchor price stays fixed to prevent chain drift.
        dedup_dist = self.atr * 0.4
        all_levels.sort(key=lambda l: l.price)
        deduped = []       # (level, anchor_price) pairs
        for l in all_levels:
            merged = False
            for i, (existing, anchor) in enumerate(deduped):
                if existing.price > 0 and existing.level_type == l.level_type and abs(l.price - anchor) < dedup_dist:
                    if self._level_score(l) > self._level_score(existing):
                        deduped[i] = (l, anchor)   # data updates, anchor stays
                    merged = True
                    break
            if not merged:
                deduped.append((l, l.price))

        raw_levels = [level for level, _ in deduped]

        # Step 3: POC guarantee
        if vp["poc"] and not any(abs(l.price - vp["poc"]) / max(l.price, 1e-12) < 0.02
                                  for l in raw_levels):
            t = "support" if vp["poc"] < self.current_price else "resistance"
            raw_levels.append(SRLevel(price=vp["poc"], level_type=t, strength=0.45,
                                      method="volume_profile", touches=0,
                                      volume_weight=0.9, recency_score=0.5))

        # Step 4: Convert to zones
        max_d = self.current_price * config.MAX_DISTANCE_PCT / 100
        zones = [self._level_to_zone(l, bias) for l in raw_levels
                 if abs(l.price - self.current_price) <= max_d]

        # Step 5: Split, merge, rank
        sup = self._merge_nearby([z for z in zones if z.zone_type == "support"])
        res = self._merge_nearby([z for z in zones if z.zone_type == "resistance"])

        def rank_key(z):
            """Score-first for overprovision cut."""
            return self._zone_score(z)

        def final_rank(z):
            """Proximity-dominant ranking for final cap (80/20)."""
            score = self._zone_score(z) / 80  # normalize to ~0-1
            proximity = 1.0 / (1 + abs(z.mid_price - self.current_price) / self.current_price)
            return proximity * 0.80 + score * 0.20

        sup = self._drop_sandwiched_minors(sup)
        res = self._drop_sandwiched_minors(res)

        sup.sort(key=rank_key, reverse=True)
        res.sort(key=rank_key, reverse=True)
        sup = sup[:overprovision_cap]
        res = res[:overprovision_cap]

        # Step 6: Post-ranking guarantees — POC and SMAs
        if vp["poc"] and vp["poc"] < self.current_price:
            if not any(abs(z.mid_price - vp["poc"]) / vp["poc"] < 0.03 for z in sup):
                pz = self._level_to_zone(SRLevel(price=vp["poc"], level_type="support",
                     strength=0.40, method="volume_profile", touches=0, volume_weight=0.9), bias)
                pz.notes = "Volume POC"
                if len(sup) >= overprovision_cap: sup[-1] = pz
                else: sup.append(pz)

        for ma_p in [50, 100, 200]:
            if len(self.df) < ma_p: continue
            mv = float(self.df["close"].rolling(ma_p).mean().iloc[-1])
            dp = abs(mv - self.current_price) / self.current_price
            if dp > 0.20 or dp < 0.01: continue
            mt = "support" if mv < self.current_price else "resistance"
            tgt = sup if mt == "support" else res
            if any(abs(z.mid_price - mv) / mv < 0.03 for z in tgt): continue
            mz = self._level_to_zone(SRLevel(price=mv, level_type=mt, strength=0.35,
                 method="sma", touches=0, volume_weight=0.0, recency_score=0.5), bias)
            mz.notes = f"SMA {ma_p} (${mv:,.0f})"
            tgt.append(mz)

        # Re-merge after SMA/POC injections
        sup = self._merge_nearby(sup)
        res = self._merge_nearby(res)

        # Boundary guard
        sup = [z for z in sup if z.key_level < self.current_price]
        res = [z for z in res if z.key_level > self.current_price]

        # Backfill: if merge reduced count below target, pull from raw ensemble
        if len(res) < target or len(sup) < target:
            backfill = self._backfill_levels(bias, sup, res)
            for z in backfill:
                if z.zone_type == "resistance" and z.key_level > self.current_price:
                    res.append(z)
                elif z.zone_type == "support" and z.key_level < self.current_price:
                    sup.append(z)
            sup = self._merge_nearby(sup)
            res = self._merge_nearby(res)
            sup = [z for z in sup if z.key_level < self.current_price]
            res = [z for z in res if z.key_level > self.current_price]

        # Step 7: Fibonacci
        sh, sl = float(self.df["high"].max()), float(self.df["low"].min())
        fr = sh - sl
        fibs = {"0.236": sl+.236*fr, "0.382": sl+.382*fr, "0.500": sl+.5*fr,
                "0.618": sl+.618*fr, "0.786": sl+.786*fr}

        # Step 8: Enrich notes (including backfilled zones)
        for z in sup + res:
            if not z.notes:
                self._enrich_notes(z, fibs, structure)

        # Cap to target: nearest level guaranteed, rest by score with spacing.
        def _cap(zones, n):
            min_spacing = self.atr * config.MIN_LEVEL_SPACING_ATR
            zones_by_prox = sorted(zones, key=lambda z: abs(z.mid_price - self.current_price))
            if not zones_by_prox:
                return zones
            result = [zones_by_prox[0]]  # nearest always included
            remaining = [z for z in zones if z is not result[0]]
            remaining.sort(key=final_rank, reverse=True)
            for z in remaining:
                if len(result) >= n:
                    break
                if all(abs(z.mid_price - r.mid_price) >= min_spacing for r in result):
                    result.append(z)
            return result

        sup = _cap(sup, target)
        res = _cap(res, target)

        # Step 9: Final boundary guard
        final_sup = [z for z in sup if z.key_level < self.current_price]
        final_res = [z for z in res if z.key_level > self.current_price]
        for z in sup:
            if z.key_level >= self.current_price and z not in final_res:
                z.zone_type = "resistance"
                final_res.append(z)
        for z in res:
            if z.key_level <= self.current_price and z not in final_sup:
                z.zone_type = "support"
                final_sup.append(z)

        # Step 10: Cross-side dedup
        merge_pct = config.MERGE_THRESHOLD_PCT
        to_remove_sup = set()
        to_remove_res = set()
        for i, sz in enumerate(final_sup):
            for j, rz in enumerate(final_res):
                if abs(sz.mid_price - rz.mid_price) / max(sz.mid_price, 1e-12) < merge_pct:
                    s_score = self._zone_score(sz)
                    r_score = self._zone_score(rz)
                    if s_score >= r_score:
                        to_remove_res.add(j)
                    else:
                        to_remove_sup.add(i)
        if to_remove_sup:
            final_sup = [z for i, z in enumerate(final_sup) if i not in to_remove_sup]
        if to_remove_res:
            final_res = [z for i, z in enumerate(final_res) if i not in to_remove_res]

        # Step 11: TA-friendly display rounding (after all calculations)
        for z in final_sup + final_res:
            z.key_level = ta_round(z.key_level, self.current_price)

        return {"market_structure": structure, "volume_profile": vp,
                "support_zones": final_sup, "resistance_zones": final_res,
                "fibonacci": fibs,
                "summary": self._build_summary(structure, vp, final_sup, final_res)}

    def _backfill_levels(self, bias, existing_sup, existing_res):
        """Run V2 ensemble on full data and convert levels not already covered."""
        det = SRDetector(self.df)
        raw = det.detect_all(max_levels=30, min_strength=0.05)

        existing_prices = [z.mid_price for z in existing_sup + existing_res]
        atr = compute_atr(self.df)

        new_zones = []
        for l in sorted(raw, key=lambda x: abs(x.price - self.current_price)):
            if any(abs(l.price - ep) < atr for ep in existing_prices):
                continue
            if l.strength < 0.10 and l.touches < 3:
                continue
            if abs(l.price - self.current_price) / self.current_price > 0.50:
                continue
            zone = self._level_to_zone(l, bias)
            new_zones.append(zone)
            existing_prices.append(zone.mid_price)

        return new_zones

    def _build_summary(self, structure, vp, sup, res):
        p = self.current_price
        lines = [f"Price: ${p:,.0f} | Trend: {structure['trend']} | Structure: {structure['structure']}"]
        if vp["poc"]: lines.append(f"POC: ${vp['poc']:,.0f} | VA: ${vp['val']:,.0f}-${vp['vah']:,.0f}")
        ns = sorted(sup, key=lambda z: z.mid_price, reverse=True)[0] if sup else None
        nr = sorted(res, key=lambda z: z.mid_price)[0] if res else None
        if ns: lines.append(f"Nearest S: ${ns.mid_price:,.0f} ({(p-ns.mid_price)/p*100:.1f}%)")
        if nr: lines.append(f"Nearest R: ${nr.mid_price:,.0f} ({(nr.mid_price-p)/p*100:.1f}%)")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Convenience: analyze_token (JSON-ready output)
# ─────────────────────────────────────────────────────────────

def analyze_token(symbol: str, df: pd.DataFrame, data_dir: str = "data") -> Dict[str, Any]:
    """Run V2 S/R analysis and return structured dict."""
    analysis = ProfessionalSRAnalysis2(df, symbol=symbol, data_dir=data_dir)
    result = analysis.analyze()

    ms = result["market_structure"]
    vp = result["volume_profile"]
    supports = result["support_zones"]
    resistances = result["resistance_zones"]

    sr = lambda val: smart_round(val, ms["current_price"])

    r_list = []
    for z in sorted(resistances, key=lambda x: x.mid_price):
        dist_pct = round((z.mid_price - ms["current_price"]) / ms["current_price"] * 100, 1)
        r_list.append({
            "zone": [sr(z.price_low), sr(z.price_high)],
            "key_level": sr(z.key_level), "tier": z.tier,
            "distance_pct": dist_pct, "confluence": z.confluence_score,
            "touches": z.touches,
            "volume_confirmed": bool(getattr(z, 'volume_confirmed', False)),
            "anchor_type": z.anchor_type if z.anchor_type else None,
            "anchor_candle_date": z.anchor_candle_date if z.anchor_candle_date else None,
            "notes": z.notes if z.notes else None,
        })

    s_list = []
    for z in sorted(supports, key=lambda x: x.mid_price, reverse=True):
        dist_pct = round((ms["current_price"] - z.mid_price) / ms["current_price"] * 100, 1)
        s_list.append({
            "zone": [sr(z.price_low), sr(z.price_high)],
            "key_level": sr(z.key_level), "tier": z.tier,
            "distance_pct": dist_pct, "confluence": z.confluence_score,
            "touches": z.touches,
            "volume_confirmed": bool(getattr(z, 'volume_confirmed', False)),
            "anchor_type": z.anchor_type if z.anchor_type else None,
            "anchor_candle_date": z.anchor_candle_date if z.anchor_candle_date else None,
            "notes": z.notes if z.notes else None,
        })

    # Scenarios
    sorted_r = sorted(resistances, key=lambda z: z.mid_price)
    sorted_s = sorted(supports, key=lambda z: z.mid_price, reverse=True)

    nearest_s = sorted_s[0] if sorted_s else None
    second_s = sorted_s[1] if len(sorted_s) > 1 else None
    third_s = sorted_s[2] if len(sorted_s) > 2 else None

    r_target, r_extended = ProfessionalSRAnalysis2._pick_targets(sorted_r, ms["current_price"])
    min_dist = ms["current_price"] * 0.02

    hold_s = nearest_s
    if nearest_s and abs(ms["current_price"] - nearest_s.key_level) < min_dist and second_s:
        hold_s = second_s

    tp_r = r_target
    if r_target and abs(r_target.key_level - ms["current_price"]) < min_dist:
        tp_r = r_extended
        r_extended = None

    bullish = {}
    if hold_s: bullish["hold_above"] = sr(hold_s.key_level)
    if tp_r: bullish["next_target"] = sr(tp_r.key_level)
    if r_extended: bullish["extended_target"] = sr(r_extended.key_level)
    inv_s = second_s if second_s and second_s != hold_s else third_s
    if inv_s: bullish["invalidation"] = sr(inv_s.key_level)
    elif hold_s: bullish["invalidation"] = sr(hold_s.price_low)

    bearish = {}
    if hold_s: bearish["fails_at"] = sr(hold_s.key_level)
    retest_s = second_s if second_s and second_s != hold_s else third_s
    if retest_s: bearish["retests"] = sr(retest_s.key_level)
    if third_s and third_s != retest_s: bearish["reopens"] = sr(third_s.key_level)
    elif retest_s: bearish["reopens"] = sr(retest_s.price_low)

    triggers = {}
    if hold_s: triggers["buy_the_dip"] = sr(hold_s.key_level)
    stop_s = second_s if second_s and second_s != hold_s else third_s
    if stop_s: triggers["stop_loss"] = sr(stop_s.price_low)
    elif hold_s: triggers["stop_loss"] = sr(hold_s.price_low)

    first_r_above = None
    for z in sorted_r:
        if z.key_level > ms["current_price"] * 1.02:
            first_r_above = z
            break
    if first_r_above: triggers["take_profit"] = sr(first_r_above.key_level)
    elif tp_r: triggers["take_profit"] = sr(tp_r.key_level)

    return {
        "symbol": symbol.upper(),
        "price": sr(ms["current_price"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data_points": len(df),
        "market_structure": {
            "trend": ms["trend"], "structure": ms["structure"],
            "bias": float(ms.get("bias", 0)), "atr14": round(ms.get("atr14", 0), 8),
            "sma20": ms.get("sma20"), "sma50": ms.get("sma50"),
            "sma100": ms.get("sma100"), "sma200": ms.get("sma200"),
        },
        "resistance": r_list, "support": s_list,
        "scenarios": {"bullish": bullish, "bearish": bearish},
        "triggers": triggers,
        "volume_profile": {
            "poc": sr(vp["poc"]) if vp["poc"] else None,
            "value_area_low": sr(vp["val"]) if vp["val"] else None,
            "value_area_high": sr(vp["vah"]) if vp["vah"] else None,
        },
    }
