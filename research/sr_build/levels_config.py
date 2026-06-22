#!/usr/bin/env python3
"""
Display configuration for the volume-profile levels module.
===========================================================
These are DELIBERATE RENDERING knobs — not logic thresholds. They never touch
scoring, ranking, or the trusted/low_confidence decision; they only decide how
many of the already-ranked levels get drawn. A chart can show only so many lines
before it becomes the very clutter this module exists to kill, so a fixed display
cap is defensible where an ATR-derived logic gate would be required. Tune freely.
"""

# Max DRAWN lines per side (support/resistance) per scale (macro/tactical).
# Everything beyond the cap stays dormant — stored and queryable, just not drawn.
MAX_LINES_PER_SIDE_PER_SCALE = 3
