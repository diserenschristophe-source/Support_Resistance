"""
Detector 1b: Market Structure Plus — Body + Wick swing analysis with invalidation.
====================================================================================
Extends MarketStructureDetector with three literature-backed improvements:

  1. Invalidation pass (body-close break counting):
     - Each candle body closing beyond a level by > 0.25 ATR degrades strength.
     - Breaks are volume-weighted: a break on 2x avg volume counts as 2 breaks.
     - 1 break → -30%, 2 → -60%, 3+ → removed entirely.
     - 2 consecutive body closes beyond → removed immediately.
     - Wick-only piercings do NOT count (they are rejections).

  2. Polarity conversion on break:
     - Levels with break_count == 2 flip type (resistance → support, vice versa)
       and are emitted as weak "flipped_*" levels (base strength 0.30).
     - Complements (not duplicates) polarity_flip detector: this fires on
       body-close breaks alone; polarity_flip requires confirmed touches on
       both sides.  Ensemble merge handles overlap naturally.

  3. Exponential recency decay:
     - Replaces linear decay with exp(-bars_ago / halflife).
     - Configurable halflife (default 30 bars).  Older levels fade fast,
       matching observed behaviour in crypto markets.
"""

import numpy as np
import pandas as pd
from typing import List

from core.models import SRLevel, compute_atr
from core import config


class MarketStructureDetector:

    def __init__(self, swing_window: int = 5, min_swing_atr: float = None,
                 break_atr_mult: float = None, max_breaks: float = None,
                 consec_kill: int = None, recency_halflife: int = None):
        self.swing_window = max(swing_window, 3)
        self.min_swing_atr = min_swing_atr if min_swing_atr is not None else config.MS_MIN_SWING_ATR
        self.break_atr_mult = break_atr_mult if break_atr_mult is not None else config.MS_BREAK_ATR_MULT
        self.max_breaks = max_breaks if max_breaks is not None else config.MS_MAX_BREAKS
        self.consec_kill = consec_kill if consec_kill is not None else config.MS_CONSEC_KILL
        self.recency_halflife = recency_halflife if recency_halflife is not None else config.MS_RECENCY_HALFLIFE

    # ── helpers ──────────────────────────────────────────────────

    def _body_low(self, opens, closes, i):
        return min(opens[i], closes[i])

    def _body_high(self, opens, closes, i):
        return max(opens[i], closes[i])

    # ── swing detection (unchanged) ─────────────────────────────

    def _find_body_swings(self, df: pd.DataFrame) -> List[dict]:
        """Find swing highs/lows on candle BODIES — the core structure."""
        opens = df["open"].values
        closes = df["close"].values
        n = len(df)
        half = self.swing_window // 2
        swings = []
        dates = df.index if hasattr(df.index, 'strftime') else range(n)

        for i in range(half, n - half):
            bh = self._body_high(opens, closes, i)
            bl = self._body_low(opens, closes, i)

            is_swing_high = True
            for j in range(1, half + 1):
                if bh <= self._body_high(opens, closes, i - j) or \
                   bh <= self._body_high(opens, closes, i + j):
                    is_swing_high = False
                    break

            is_swing_low = True
            for j in range(1, half + 1):
                if bl >= self._body_low(opens, closes, i - j) or \
                   bl >= self._body_low(opens, closes, i + j):
                    is_swing_low = False
                    break

            if is_swing_high:
                swings.append({
                    "type": "high", "price": float(bh), "idx": i,
                    "date": str(dates[i]) if hasattr(dates[i], 'strftime') else "",
                    "source": "body",
                })
            if is_swing_low:
                swings.append({
                    "type": "low", "price": float(bl), "idx": i,
                    "date": str(dates[i]) if hasattr(dates[i], 'strftime') else "",
                    "source": "body",
                })
        return swings

    def _find_wick_swings(self, df: pd.DataFrame, body_swings: List[dict], atr: float) -> List[dict]:
        """Find additional wick rejections that body swings missed."""
        highs = df["high"].values
        lows = df["low"].values
        n = len(df)
        half = self.swing_window // 2
        swings = []
        dates = df.index if hasattr(df.index, 'strftime') else range(n)
        min_distance = atr * self.min_swing_atr

        body_prices = [s["price"] for s in body_swings]

        for i in range(half, n - half):
            is_swing_high = True
            for j in range(1, half + 1):
                if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                    is_swing_high = False
                    break

            if is_swing_high:
                if any(abs(highs[i] - bp) < min_distance for bp in body_prices):
                    continue
                swings.append({
                    "type": "high", "price": float(highs[i]), "idx": i,
                    "date": str(dates[i]) if hasattr(dates[i], 'strftime') else "",
                    "source": "wick",
                })

            is_swing_low = True
            for j in range(1, half + 1):
                if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                    is_swing_low = False
                    break

            if is_swing_low:
                if any(abs(lows[i] - bp) < min_distance for bp in body_prices):
                    continue
                swings.append({
                    "type": "low", "price": float(lows[i]), "idx": i,
                    "date": str(dates[i]) if hasattr(dates[i], 'strftime') else "",
                    "source": "wick",
                })

        return self._filter_by_atr(swings, atr)

    def _filter_by_atr(self, swings: List[dict], atr: float) -> List[dict]:
        """Remove wick swings where the move is less than min_swing_atr * ATR."""
        if self.min_swing_atr <= 0 or atr <= 0 or len(swings) < 2:
            return swings

        min_distance = atr * self.min_swing_atr
        filtered = []
        last_high = None
        last_low = None

        for s in swings:
            if s["type"] == "high":
                if last_low is not None and abs(s["price"] - last_low) < min_distance:
                    continue
                if last_high is not None and abs(s["price"] - last_high) < min_distance:
                    if s["price"] > last_high:
                        filtered = [f for f in filtered if not (f["type"] == "high" and f["price"] == last_high)]
                        filtered.append(s)
                        last_high = s["price"]
                    continue
                filtered.append(s)
                last_high = s["price"]
            else:
                if last_high is not None and abs(last_high - s["price"]) < min_distance:
                    continue
                if last_low is not None and abs(s["price"] - last_low) < min_distance:
                    if s["price"] < last_low:
                        filtered = [f for f in filtered if not (f["type"] == "low" and f["price"] == last_low)]
                        filtered.append(s)
                        last_low = s["price"]
                    continue
                filtered.append(s)
                last_low = s["price"]

        return filtered

    # ── structure classification (unchanged) ────────────────────

    def _classify_structure(self, swings: List[dict]) -> List[dict]:
        if len(swings) < 4:
            return swings

        highs = [s for s in swings if s["type"] == "high"]
        lows = [s for s in swings if s["type"] == "low"]

        for i in range(1, len(highs)):
            highs[i]["role"] = "HH" if highs[i]["price"] > highs[i-1]["price"] else "LH"
        if highs:
            highs[0]["role"] = "HH"

        for i in range(1, len(lows)):
            lows[i]["role"] = "HL" if lows[i]["price"] > lows[i-1]["price"] else "LL"
        if lows:
            lows[0]["role"] = "HL"

        all_points = highs + lows
        all_points.sort(key=lambda s: s["idx"])

        trend = "neutral"
        for i in range(len(all_points)):
            role = all_points[i].get("role", "")
            if role in ("HH", "HL"):
                if trend == "down":
                    all_points[i]["structural"] = "CHOCH"
                    trend = "up"
                else:
                    all_points[i]["structural"] = "BOS"
                    if role == "HH":
                        trend = "up"
            elif role in ("LH", "LL"):
                if trend == "up":
                    all_points[i]["structural"] = "CHOCH"
                    trend = "down"
                else:
                    all_points[i]["structural"] = "BOS"
                    if role == "LL":
                        trend = "down"
        return all_points

    # ── NEW: invalidation pass ──────────────────────────────────

    def _invalidate_broken_levels(self, swings: List[dict],
                                  df: pd.DataFrame, atr: float) -> List[dict]:
        """Remove, penalise, or flip swings that subsequent candle bodies broke.

        For each swing:
          - Count body closes beyond the level by > break_atr_mult * ATR.
          - Each break is volume-weighted: rel_vol = bar_vol / avg_vol (capped 2x).
            A break on 2x volume counts as 2; on 0.3x volume counts as 0.3.
          - Wick-only piercings are ignored (they are rejections).
          - consec_kill consecutive body closes → dead (removed).
          - break_count >= max_breaks → dead, BUT if break_count reached 2
            the level is also emitted as a flipped polarity level.

        Returns (surviving, flipped) — original levels that survived, plus
        any new polarity-converted levels.
        """
        closes = df["close"].values
        volumes = df["volume"].values if "volume" in df.columns else None
        n = len(df)
        threshold = atr * self.break_atr_mult

        # Pre-compute average volume for relative weighting
        avg_vol = float(np.mean(volumes)) if volumes is not None else 1.0
        avg_vol = max(avg_vol, 1e-12)  # avoid div-by-zero

        surviving = []
        flipped = []

        for s in swings:
            start = s["idx"] + 1
            if start >= n:
                s["break_count"] = 0.0
                surviving.append(s)
                continue

            break_count = 0.0
            max_consec = 0
            cur_consec = 0

            for k in range(start, n):
                body_close = closes[k]
                if s["type"] == "high":
                    broken = body_close > s["price"] + threshold
                else:
                    broken = body_close < s["price"] - threshold

                if broken:
                    # Volume-weighted: high-volume breaks count more
                    if volumes is not None:
                        rel_vol = min(volumes[k] / avg_vol, config.MS_MAX_VOLUME_MULT)
                    else:
                        rel_vol = 1.0
                    break_count += rel_vol
                    cur_consec += 1
                    max_consec = max(max_consec, cur_consec)
                else:
                    cur_consec = 0

            # Consecutive-close kill → dead, no flip
            if max_consec >= self.consec_kill:
                # Still emit a flipped level if it accumulated enough breaks
                if break_count >= config.MS_FLIP_THRESHOLD:
                    flipped.append(self._make_flipped(s))
                continue

            # Max-break kill
            if break_count >= self.max_breaks:
                flipped.append(self._make_flipped(s))
                continue

            s["break_count"] = break_count

            # Levels with enough weighted breaks: keep original AND emit flip
            if break_count >= config.MS_FLIP_THRESHOLD:
                flipped.append(self._make_flipped(s))

            surviving.append(s)

        return surviving, flipped

    @staticmethod
    def _make_flipped(s: dict) -> dict:
        """Create a polarity-converted copy of a broken swing.

        Broken resistance → weak support (flipped_resistance).
        Broken support    → weak resistance (flipped_support).
        """
        flipped = dict(s)
        if s["type"] == "high":
            flipped["type"] = "low"            # was resistance, now support
            flipped["flipped_role"] = "flipped_resistance"
        else:
            flipped["type"] = "high"           # was support, now resistance
            flipped["flipped_role"] = "flipped_support"
        flipped["break_count"] = 0.0           # reset — it's a new level now
        return flipped

    @staticmethod
    def _break_penalty(break_count: float) -> float:
        """Strength multiplier based on volume-weighted break count.

        Continuous interpolation:
          0   → 1.0
          1.0 → 0.7
          2.0 → 0.4
          3.0 → 0.1  (nearly dead — should be filtered, but safety floor)
        """
        if break_count <= 0:
            return 1.0
        if break_count >= config.MS_MAX_BREAKS:
            return config.MS_MIN_PENALTY_FLOOR
        return max(config.MS_MIN_PENALTY_FLOOR,
                   1.0 - config.MS_BREAK_PENALTY_SLOPE * break_count)

    # ── detect (with invalidation + polarity flip + exp decay) ──

    def _recency(self, bars_ago: int) -> float:
        """Exponential recency decay: exp(-bars_ago / halflife).

        halflife=30 → 7 bars ago: 0.79, 30: 0.37, 90: 0.05.
        """
        return float(np.exp(-bars_ago / max(self.recency_halflife, 1)))

    def detect(self, df: pd.DataFrame) -> List[SRLevel]:
        current_price = df["close"].iloc[-1]
        n = len(df)
        atr = compute_atr(df)

        # Step 1: Core structure from bodies
        body_swings = self._find_body_swings(df)

        # Step 2: Additional wick rejections (ATR-filtered, deduplicated)
        wick_swings = self._find_wick_swings(df, body_swings, atr)

        # Step 3: Merge and classify
        all_swings = body_swings + wick_swings
        all_swings.sort(key=lambda s: s["idx"])
        classified = self._classify_structure(all_swings)

        # Step 4: Invalidation pass — remove/penalise/flip broken levels
        surviving, flipped = self._invalidate_broken_levels(classified, df, atr)

        levels = []

        # ── Surviving original levels ──
        for s in surviving:
            role = s.get("role", "")
            structural = s.get("structural", "")
            source = s.get("source", "body")
            break_count = s.get("break_count", 0.0)

            if structural == "CHOCH":
                base_strength = config.MS_STRENGTH_CHOCH
            elif role in ("HL", "LH"):
                base_strength = config.MS_STRENGTH_HL_LH
            else:
                base_strength = config.MS_STRENGTH_DEFAULT

            if source == "wick":
                base_strength *= config.MS_WICK_MODIFIER

            bars_ago = n - 1 - s["idx"]
            recency = self._recency(bars_ago)
            strength = base_strength * config.MS_WEIGHT_BASE + recency * config.MS_WEIGHT_RECENCY

            # Apply break penalty
            strength *= self._break_penalty(break_count)

            if s["type"] == "low":
                level_type = "support" if s["price"] < current_price else "resistance"
            else:
                level_type = "resistance" if s["price"] > current_price else "support"

            levels.append(SRLevel(
                price=s["price"],
                level_type=level_type,
                strength=round(min(1.0, strength), 4),
                method="market_structure",
                touches=1 + int(break_count),
                recency_score=round(recency, 4),
                anchor_type=source,
                anchor_candle_idx=s["idx"],
                structural_role=f"{role}_{structural}" if structural else role,
            ))

        # ── Flipped polarity levels (broken R→S or broken S→R) ──
        # Low base strength (0.30) + distinct structural_role so ensemble
        # treats them as early weak signals.  If polarity_flip detector
        # independently confirms the same price, ensemble merge gives
        # a confluence bonus — correct behaviour.
        for s in flipped:
            flipped_role = s.get("flipped_role", "flipped")
            source = s.get("source", "body")

            base_strength = config.MS_STRENGTH_FLIPPED
            if source == "wick":
                base_strength *= config.MS_WICK_MODIFIER

            bars_ago = n - 1 - s["idx"]
            recency = self._recency(bars_ago)
            strength = base_strength * config.MS_WEIGHT_BASE + recency * config.MS_WEIGHT_RECENCY

            if s["type"] == "low":
                level_type = "support" if s["price"] < current_price else "resistance"
            else:
                level_type = "resistance" if s["price"] > current_price else "support"

            levels.append(SRLevel(
                price=s["price"],
                level_type=level_type,
                strength=round(min(1.0, strength), 4),
                method="market_structure",
                touches=1,
                recency_score=round(recency, 4),
                anchor_type=source,
                anchor_candle_idx=s["idx"],
                structural_role=flipped_role,
            ))

        return levels
