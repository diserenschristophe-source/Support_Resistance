"""
TPSL — Take-Profit / Stop-Loss and S/R quality scoring.
=========================================================
Simple rules:
  TP = nearest resistance (cascade to next if within 1 ATR of price)
  SL = nearest support (cascade to next if within 1 ATR of price)
  RR = (TP - price) / (price - SL)

Quality scores (0-1):
  Support Confidence — how likely the SL level holds
  Resistance Permeability — how likely the TP level breaks
"""

import json
import numpy as np
from typing import List, Optional
from datetime import datetime, timezone


from core.models import fmt_price


class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.bool_,)): return bool(obj)
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super().default(obj)


# ─────────────────────────────────────────────────────────────
# Quality Scores
# ─────────────────────────────────────────────────────────────

def score_support_confidence(support_zones: List[dict]) -> float:
    """How likely is the nearest support to hold? (0.0 - 1.0)"""
    if not support_zones:
        return 0.0
    s = support_zones[0]
    score = 0.0
    score += 0.30 if s["tier"] == "Major" else 0.10
    anchor = (s.get("anchor_type") or "").lower()
    if anchor == "body": score += 0.25
    elif anchor == "wick": score += 0.15
    else: score += 0.05
    if s.get("volume_confirmed"): score += 0.20
    score += min(0.15, (s.get("confluence", 1) / 5) * 0.15)
    score += min(0.10, (s.get("touches", 0) / 20) * 0.10)
    return round(min(1.0, score), 4)


def score_resistance_permeability(resistance_zones: List[dict]) -> float:
    """How likely is the nearest resistance to BREAK? (0.0 - 1.0)
    Higher = weaker resistance = better for a long trade."""
    if not resistance_zones:
        return 0.8
    r = resistance_zones[0]
    score = 0.0
    score += 0.30 if r["tier"] == "Minor" else 0.10
    anchor = (r.get("anchor_type") or "").lower()
    if anchor in ("stat", "wick", ""): score += 0.25
    elif anchor == "body": score += 0.10
    if not r.get("volume_confirmed"): score += 0.20
    score += max(0.0, (1 - r.get("confluence", 1) / 5)) * 0.15
    score += max(0.0, (1 - r.get("touches", 0) / 20)) * 0.10
    return round(min(1.0, score), 4)


# ─────────────────────────────────────────────────────────────
# TP / SL / RR Calculation
# ─────────────────────────────────────────────────────────────

def compute_tp_sl(analysis: dict) -> Optional[dict]:
    """
    Compute TP, SL, and R:R for one token.

    Rules:
      TP = nearest resistance. If within 1 ATR → cascade to next.
      SL = nearest support. If within 1 ATR → cascade to next.
      RR = (TP - price) / (price - SL)
    """
    symbol = analysis.get("symbol", "???")
    price = analysis.get("price")
    if not price or price <= 0:
        return None

    supports = analysis.get("support", [])
    resistances = analysis.get("resistance", [])
    ms = analysis.get("market_structure", {})
    atr = ms.get("atr14", 0)

    # ── Find TP: nearest resistance, cascade if within 1 ATR ──
    tp = None
    for r_zone in resistances:
        r_level = r_zone.get("key_level", 0)
        if r_level > price and (r_level - price) >= atr:
            tp = r_level
            break
    # Fallback: take any resistance above price even if close
    if tp is None:
        for r_zone in resistances:
            r_level = r_zone.get("key_level", 0)
            if r_level > price:
                tp = r_level
                break

    # ── Find SL: nearest support, cascade if within 1 ATR ──
    sl = None
    for s_zone in supports:
        s_level = s_zone.get("key_level", 0)
        if s_level < price and (price - s_level) >= atr:
            sl = s_level
            break
    # Fallback: take any support below price even if close
    if sl is None:
        for s_zone in supports:
            s_level = s_zone.get("key_level", 0)
            if s_level < price:
                sl = s_level
                break

    # ── Can't compute without both ──
    if tp is None or sl is None:
        missing = []
        if tp is None: missing.append("no TP (no resistance above)")
        if sl is None: missing.append("no SL (no support below)")
        return {
            "symbol": symbol, "price": price,
            "take_profit": tp, "stop_loss": sl,
            "potential_gain_pct": 0, "potential_loss_pct": 0,
            "raw_rr": 0,
            "support_confidence": score_support_confidence(supports),
            "resistance_permeability": score_resistance_permeability(resistances),
            "reason": ", ".join(missing),
            "market_structure": ms,
            "support": supports, "resistance": resistances,
            "volume_profile": analysis.get("volume_profile"),
        }

    # ── R:R ──
    potential_gain = tp - price
    potential_loss = price - sl
    if potential_loss <= 0:
        return None

    raw_rr = potential_gain / potential_loss
    gain_pct = (potential_gain / price) * 100
    loss_pct = (potential_loss / price) * 100

    return {
        "symbol": symbol,
        "price": price,
        "take_profit": tp,
        "stop_loss": sl,
        "potential_gain_pct": round(gain_pct, 2),
        "potential_loss_pct": round(loss_pct, 2),
        "potential_gain_abs": round(potential_gain, 2),
        "potential_loss_abs": round(potential_loss, 2),
        "raw_rr": round(raw_rr, 2),
        "support_confidence": score_support_confidence(supports),
        "resistance_permeability": score_resistance_permeability(resistances),
        "market_structure": ms,
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "support": supports,
        "resistance": resistances,
        "volume_profile": analysis.get("volume_profile"),
    }


# ─────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────

def output_json(scored: List[dict], top_n: int = 5) -> str:
    has_tpsl = [s for s in scored if s.get("take_profit") and s.get("stop_loss")]
    has_tpsl.sort(key=lambda s: s.get("raw_rr", 0), reverse=True)

    disqualified = []
    for s in scored:
        if s.get("take_profit") and s.get("stop_loss"):
            continue
        entry = {"symbol": s["symbol"], "raw_rr": s.get("raw_rr", 0)}
        entry["reason"] = s.get("reason", f"R:R {s.get('raw_rr', 0):.1f} (no TP/SL)")
        disqualified.append(entry)

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": {},
        "tokens_analyzed": len(scored),
        "tokens_with_tpsl": len(has_tpsl),
        "ranking": has_tpsl[:top_n],
        "disqualified": disqualified,
        "analyses": sorted(scored, key=lambda s: s.get("raw_rr", 0), reverse=True),
    }
    return json.dumps(output, indent=2, ensure_ascii=False, cls=NumpySafeEncoder)
