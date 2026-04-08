"""
TPSL — Take-Profit / Stop-Loss and S/R quality scoring.
=========================================================
Simple rules:
  TP = nearest resistance (cascade to next if within 1 ATR of price)
  SL = nearest support (cascade to next if within 1 ATR of price)
  RR = (TP - price) / (price - SL)

"""

import json
import numpy as np
from typing import List, Optional
from datetime import datetime, timezone

from core import config
from core.models import fmt_price


class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.bool_,)): return bool(obj)
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super().default(obj)


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

    # ── Find TP: cascade through resistances (nearest-first) ──
    # Walk resistances sorted nearest-first (ascending). Keep updating tp
    # as we cascade past levels within 1 ATR of price.  Break on the first
    # level that is >= 1 ATR away.  If ALL are within 1 ATR, keep the
    # farthest (last one we saw).
    tp = None
    for r_zone in resistances:
        r_level = r_zone.get("key_level", 0)
        if r_level <= price:
            continue
        tp = r_level                       # always update (cascade)
        if (r_level - price) >= atr:
            break                          # far enough — stop

    # ── Find SL: cascade through supports (nearest-first) ──
    # Supports are sorted descending (nearest-first).  Same logic: keep
    # updating sl, break when distance >= 1 ATR.  If all within ATR, keep
    # the farthest (lowest level).
    sl = None
    for s_zone in supports:
        s_level = s_zone.get("key_level", 0)
        if s_level >= price:
            continue
        sl = s_level                       # always update (cascade)
        if (price - s_level) >= atr:
            break                          # far enough — stop

    # ── ATR distance: best-effort partial setup ──
    # If the cascade exhausted into a level closer than 0.5 ATR, we DO NOT
    # fabricate a synthetic `price ± atr` level (that misleads the trader),
    # and we DO NOT disqualify the entire token (the trader still wants to
    # see the structure). Instead we set the offending side to None so the
    # UI displays "—". The other side, if usable, is preserved.
    if atr > 0:
        if tp is not None and (tp - price) < 0.5 * atr:
            tp = None
        if sl is not None and (price - sl) < 0.5 * atr:
            sl = None

    # ── R:R ── computable only when both sides are usable
    if tp is not None and sl is not None:
        potential_gain = tp - price
        potential_loss = price - sl
        if potential_loss <= 0:
            return None
        raw_rr = round(potential_gain / potential_loss, 2)
        gain_pct = round((potential_gain / price) * 100, 2)
        loss_pct = round((potential_loss / price) * 100, 2)
        gain_abs = round(potential_gain, 2)
        loss_abs = round(potential_loss, 2)
    else:
        raw_rr = None
        gain_pct = None
        loss_pct = None
        gain_abs = None
        loss_abs = None

    # Qualified iff at least one side has a usable cascaded level. A token
    # with both sides None has no tradeable structure at all.
    has_any_side = tp is not None or sl is not None
    qualified = has_any_side
    reason = None
    if not has_any_side:
        reason = "no usable structure (no TP and no SL)"

    return {
        "symbol": symbol,
        "price": price,
        "take_profit": tp,
        "stop_loss": sl,
        "potential_gain_pct": gain_pct,
        "potential_loss_pct": loss_pct,
        "potential_gain_abs": gain_abs,
        "potential_loss_abs": loss_abs,
        "raw_rr": raw_rr,
        # qualified=True when at least one cascade side is tradeable. The
        # frontend renders "—" for any side that is None and excludes
        # partial setups from preset modes (no-filter still shows them).
        "qualified": qualified,
        "reason": reason,
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

def _rr_sort_key(s: dict) -> float:
    """Sort key for raw_rr — None (partial setups) sort below all real values."""
    rr = s.get("raw_rr")
    return rr if rr is not None else -1.0


def output_json(scored: List[dict], top_n: int = 5) -> str:
    qualified = [s for s in scored if s.get("qualified")]
    qualified.sort(key=_rr_sort_key, reverse=True)

    disqualified = []
    for s in scored:
        if s.get("qualified"):
            continue
        entry = {"symbol": s["symbol"], "raw_rr": s.get("raw_rr")}
        entry["reason"] = s.get("reason", "no usable structure")
        disqualified.append(entry)

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tokens_analyzed": len(scored),
        "tokens_qualified": len(qualified),
        "ranking": qualified[:top_n],
        "disqualified": disqualified,
        "analyses": sorted(scored, key=_rr_sort_key, reverse=True),
    }
    return json.dumps(output, indent=2, ensure_ascii=False, cls=NumpySafeEncoder)
