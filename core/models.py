"""
Shared data structures and utilities for the S/R analysis engine.
==================================================================
Dataclasses + shared helpers — no dependencies on other core modules.
Imported by detectors, sr_analysis, tpsl, chart, report.
"""

from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# Shared Utilities
# ─────────────────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range — used by multiple detectors and sr_analysis."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    return float(np.mean(tr[-period:]))


def compute_atr_series(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """ATR as a full array (one value per bar) — used by NisonBodyDetector."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr = np.full_like(tr, np.nan)
    for i in range(period - 1, len(tr)):
        atr[i] = np.mean(tr[max(0, i - period + 1):i + 1])
    atr[:period] = np.nanmean(tr[:period])
    return atr


def fmt_price(p: float) -> str:
    """Smart price formatter: adapts decimals to token price scale."""
    if p >= 1000:
        return f"${p:,.0f}"
    elif p >= 10:
        return f"${p:,.1f}"
    elif p >= 1:
        return f"${p:,.2f}"
    elif p >= 0.01:
        return f"${p:,.4f}"
    elif p >= 0.0001:
        return f"${p:,.6f}"
    else:
        return f"${p:,.8f}"


@dataclass
class SRLevel:
    """A single Support/Resistance level detected by one method."""
    price: float
    level_type: str          # 'support', 'resistance', or 'both'
    strength: float          # 0.0 - 1.0 normalized strength score
    method: str              # which detection method found it
    touches: int = 0
    volume_weight: float = 0.0
    recency_score: float = 0.0
    timeframes: List[str] = field(default_factory=list)
    mtf_confluence: int = 0
    anchor_type: str = ""    # 'body' or 'wick'
    anchor_candle_idx: int = -1
    structural_role: str = ""  # 'CHOCH', 'BOS', 'HL', 'HH', 'LH', 'LL', 'flip'
    snap_price: float = 0.0       # actual candle price for chart display
    snap_candle_idx: int = -1     # absolute index of that candle

    def __repr__(self):
        tf_str = ",".join(self.timeframes) if self.timeframes else "n/a"
        anchor_str = f", anchor={self.anchor_type}" if self.anchor_type else ""
        role_str = f", role={self.structural_role}" if self.structural_role else ""
        return (
            f"SRLevel(price={self.price:.2f}, type={self.level_type}, "
            f"strength={self.strength:.3f}, method={self.method}, "
            f"touches={self.touches}, mtf={self.mtf_confluence}, "
            f"tf=[{tf_str}]{anchor_str}{role_str})"
        )


@dataclass
class SRZone:
    """A merged, tiered tradeable zone built from multiple SRLevels."""
    price_low: float
    price_high: float
    mid_price: float
    key_level: float
    zone_type: str       # 'support' or 'resistance'
    tier: str            # 'Major' or 'Minor'
    confluence_score: int
    touches: int
    volume_confirmed: bool
    label: str
    action: str
    notes: str = ""
    anchor_type: str = ""
    anchor_candle_date: str = ""
    structural_role: str = ""
