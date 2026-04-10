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

def _compute_tp_sl_impl(
    analysis: dict,
    tp_cascade_atr: float = 1.0,
    sl_cascade_atr: float = 1.0,
    tp_min_atr: float = 0.5,
    sl_min_atr: float = 0.5,
    flavour: str = "balanced",
) -> Optional[dict]:
    """
    Shared TP/SL engine.

    Parameters control how aggressively the cascade skips near levels:
      tp_cascade_atr – ATR multiplier to cascade past close resistances
      sl_cascade_atr – ATR multiplier to cascade past close supports
      tp_min_atr     – minimum ATR distance to keep a TP (else None)
      sl_min_atr     – minimum ATR distance to keep a SL (else None)
      flavour        – label stored in the result dict
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
    tp = None
    for r_zone in resistances:
        r_level = r_zone.get("key_level", 0)
        if r_level <= price:
            continue
        tp = r_level
        if (r_level - price) >= tp_cascade_atr * atr:
            break

    # ── Find SL: cascade through supports (nearest-first) ──
    sl = None
    for s_zone in supports:
        s_level = s_zone.get("key_level", 0)
        if s_level >= price:
            continue
        sl = s_level
        if (price - s_level) >= sl_cascade_atr * atr:
            break

    # ── ATR distance: discard levels too close ──
    if atr > 0:
        if tp is not None and (tp - price) < tp_min_atr * atr:
            tp = None
        if sl is not None and (price - sl) < sl_min_atr * atr:
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

    has_both = tp is not None and sl is not None and sl < tp
    qualified = has_both
    reason = None
    if not qualified:
        if tp is None and sl is None:
            reason = "no usable structure (no TP and no SL)"
        elif tp is None:
            reason = "no usable TP"
        elif sl is None:
            reason = "no usable SL"
        else:
            reason = "SL >= TP (invalid setup)"

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
        "flavour": flavour,
        "qualified": qualified,
        "reason": reason,
        "market_structure": ms,
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "support": supports,
        "resistance": resistances,
        "volume_profile": analysis.get("volume_profile"),
    }


def compute_tp_sl(analysis: dict) -> Optional[dict]:
    """Balanced TP/SL — cascade past levels within 1 ATR."""
    return _compute_tp_sl_impl(
        analysis,
        tp_cascade_atr=1.0, sl_cascade_atr=1.0,
        tp_min_atr=0.5, sl_min_atr=0.5,
        flavour="balanced",
    )


def compute_tp_sl_conservative(analysis: dict) -> Optional[dict]:
    """
    Conservative TP/SL — prioritises capital preservation.

    TP: accept the nearest viable resistance (0.5 ATR cascade) → smaller,
        more achievable target.
    SL: cascade further through supports (1.5 ATR) → wider stop, more room
        to absorb volatility.
    """
    return _compute_tp_sl_impl(
        analysis,
        tp_cascade_atr=0.5, sl_cascade_atr=1.5,
        tp_min_atr=0.3, sl_min_atr=0.75,
        flavour="conservative",
    )


def compute_tp_sl_aggressive(analysis: dict) -> Optional[dict]:
    """
    Aggressive TP/SL — maximises reward-to-risk.

    TP: skip minor resistances (2 ATR cascade) → targets bigger moves.
    SL: accept the nearest viable support (0.5 ATR cascade) → tight stop,
        cut losses fast.
    """
    return _compute_tp_sl_impl(
        analysis,
        tp_cascade_atr=2.0, sl_cascade_atr=0.5,
        tp_min_atr=0.75, sl_min_atr=0.3,
        flavour="aggressive",
    )


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
