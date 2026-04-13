"""
SRDetector2 — Ensemble with snap_price preservation.
=====================================================
Extends SRDetector: identical detection and clustering, but each
merged level also carries snap_price / snap_candle_idx — the actual
candle price for chart display.
"""

from core.models import SRLevel
from core import config
from .ensemble import SRDetector


class SRDetector2(SRDetector):
    """Same as SRDetector but preserves candle identity through merge."""

    def _merge_group(self, group):
        """Merge cluster — same as parent, plus snap_price from extreme candle."""
        merged = super()._merge_group(group)

        # Determine support vs resistance from blended price
        cp = self.df["close"].iloc[-1]
        is_support = merged.price < cp

        # Pick the extreme candle in the cluster:
        # support → lowest price, resistance → highest price
        if is_support:
            extreme = min(group, key=lambda l: l.price)
        else:
            extreme = max(group, key=lambda l: l.price)

        merged.snap_price = extreme.price
        merged.snap_candle_idx = extreme.anchor_candle_idx
        return merged
