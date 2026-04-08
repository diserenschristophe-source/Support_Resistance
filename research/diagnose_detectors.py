#!/usr/bin/env python3
"""
diagnose_detectors.py — Run each S/R detector independently and visualize results.
====================================================================================

Usage:
    python3 diagnose_detectors.py BTC
    python3 diagnose_detectors.py BTC --days 90
    python3 diagnose_detectors.py BTC ETH SOL
"""

import argparse
import sys
import os

import numpy as np
import pandas as pd

try:
    import mplfinance as mpf
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
except ImportError:
    print("pip3 install matplotlib mplfinance", file=sys.stderr)
    sys.exit(1)

from core.fetcher import fetch_data, load_from_cache
from core.detectors.ensemble import SRDetector
from core.models import fmt_price, SRLevel


DETECTOR_NAMES = {
    "structure": "Market Structure",
    "volume":    "Volume Profile",
    "touch":     "Touch Count",
    "nison":     "Nison Body",
    "polarity":  "Polarity Flip",
}

DETECTOR_COLORS = {
    "structure": "#1565C0",
    "volume":    "#6A1B9A",
    "touch":     "#E65100",
    "nison":     "#2E7D32",
    "polarity":  "#C62828",
}


def run_all_detectors(df: pd.DataFrame) -> dict:
    """Run each detector independently, return dict of name → levels."""
    det = SRDetector(df)
    return {
        "structure": det.detect_market_structure(),
        "volume":    det.detect_volume_profile(),
        "touch":     det.detect_touch_count(),
        "nison":     det.detect_nison_body(),
        "polarity":  det.detect_polarity_flip(),
    }


def print_detector_table(symbol: str, price: float, results: dict):
    """Print a formatted table of each detector's output."""
    print()
    print("=" * 90)
    print(f"  {symbol}/USDT — DETECTOR DIAGNOSTIC  |  Price: {fmt_price(price)}")
    print("=" * 90)

    for key, name in DETECTOR_NAMES.items():
        levels = results[key]
        supports = sorted([l for l in levels if l.level_type == "support"],
                         key=lambda l: l.price, reverse=True)
        resistances = sorted([l for l in levels if l.level_type == "resistance"],
                            key=lambda l: l.price)

        print(f"\n  [{key.upper()}] {name} — {len(levels)} levels "
              f"({len(supports)}S / {len(resistances)}R)")
        print("  " + "─" * 86)

        if not levels:
            print("    (no levels detected)")
            continue

        print(f"    {'TYPE':<5} {'PRICE':>12} {'DIST%':>7} {'STR':>6} "
              f"{'TOUCHES':>8} {'ANCHOR':>6} {'ROLE':>12} {'RECENCY':>8}")
        print("    " + "─" * 78)

        for l in sorted(levels, key=lambda x: x.price, reverse=True):
            dist = (l.price - price) / price * 100
            dist_str = f"{dist:+.1f}%"
            tag = "S" if l.level_type == "support" else "R"
            role = l.structural_role[:12] if l.structural_role else ""
            print(f"    {tag:<5} {fmt_price(l.price):>12} {dist_str:>7} "
                  f"{l.strength:>6.3f} {l.touches:>8} {l.anchor_type:>6} "
                  f"{role:>12} {l.recency_score:>8.3f}")

    # Overlap analysis
    print(f"\n  {'─' * 88}")
    print(f"  OVERLAP ANALYSIS")
    print(f"  {'─' * 88}")

    all_levels = []
    for key, levels in results.items():
        for l in levels:
            all_levels.append((l.price, key, l))

    all_levels.sort(key=lambda x: x[0])

    # Cluster nearby levels (within 2% of each other)
    clusters = []
    used = [False] * len(all_levels)
    for i in range(len(all_levels)):
        if used[i]:
            continue
        cluster = [(all_levels[i][1], all_levels[i][2])]
        used[i] = True
        for j in range(i + 1, len(all_levels)):
            if used[j]:
                continue
            if abs(all_levels[j][0] - all_levels[i][0]) / all_levels[i][0] < 0.02:
                cluster.append((all_levels[j][1], all_levels[j][2]))
                used[j] = True
        if len(cluster) >= 2:
            clusters.append(cluster)

    if clusters:
        for cluster in clusters:
            methods = [c[0] for c in cluster]
            avg_price = np.mean([c[1].price for c in cluster])
            best = max(cluster, key=lambda c: c[1].strength)
            print(f"    {fmt_price(avg_price):>12}  — found by {len(methods)} detectors: "
                  f"{', '.join(DETECTOR_NAMES[m] for m in methods)}")
    else:
        print("    No overlapping levels found (all detectors produced unique levels)")

    print()


def generate_detector_charts(symbol: str, df: pd.DataFrame, results: dict,
                             output_dir: str = "output"):
    """Generate one chart per detector + one combined overlay chart."""
    os.makedirs(output_dir, exist_ok=True)

    chart_df = df.copy()
    if not isinstance(chart_df.index, pd.DatetimeIndex):
        chart_df.index = pd.to_datetime(chart_df.index)
    chart_df.index.name = "Date"

    price = float(df["close"].iloc[-1])

    mc = mpf.make_marketcolors(
        up="#2E7D32", down="#D32F2F",
        wick={"up": "#2E7D32", "down": "#D32F2F"},
        edge={"up": "#2E7D32", "down": "#D32F2F"},
        volume={"up": "#C8E6C9", "down": "#FFCDD2"},
    )
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=":", gridcolor="#E0E0E0",
        facecolor="white", figcolor="white",
        rc={"font.size": 8, "axes.labelcolor": "#333",
            "xtick.color": "#666", "ytick.color": "#666"},
    )

    # ── Individual detector charts ────────────────────────────
    for key, name in DETECTOR_NAMES.items():
        levels = results[key]
        color = DETECTOR_COLORS[key]
        n = len(chart_df)

        hlines_prices = [l.price for l in levels]
        hlines_colors = []
        hlines_widths = []
        hlines_styles = []

        for l in levels:
            if l.level_type == "support":
                hlines_colors.append("#2E7D32")
            else:
                hlines_colors.append("#D32F2F")
            hlines_widths.append(max(0.4, min(1.5, l.strength * 2)))
            hlines_styles.append("solid" if l.strength >= 0.5 else "dashed")

        # Build anchor candle highlights — markers on the candles that generated each level
        anchor_support_markers = pd.Series(np.nan, index=chart_df.index)
        anchor_resist_markers = pd.Series(np.nan, index=chart_df.index)

        # Compute offset for arrow distance from candle
        price_range = chart_df["high"].max() - chart_df["low"].min()
        arrow_offset = price_range * 0.025  # 2.5% of price range below/above candle

        for l in levels:
            idx = l.anchor_candle_idx
            if idx < 0 or idx >= n:
                continue
            # Place marker offset from the candle wick (below for support, above for resistance)
            if l.level_type == "support":
                anchor_support_markers.iloc[idx] = chart_df["low"].iloc[idx] - arrow_offset
            else:
                anchor_resist_markers.iloc[idx] = chart_df["high"].iloc[idx] + arrow_offset

        addplots = []
        has_anchors = False
        if anchor_support_markers.notna().any():
            addplots.append(mpf.make_addplot(
                anchor_support_markers, type="scatter", markersize=80,
                marker="^", color="#2E7D32", alpha=0.9))
            has_anchors = True
        if anchor_resist_markers.notna().any():
            addplots.append(mpf.make_addplot(
                anchor_resist_markers, type="scatter", markersize=80,
                marker="v", color="#D32F2F", alpha=0.9))
            has_anchors = True

        plt.close("all")
        plot_kwargs = dict(
            type="candle", style=style, volume=True,
            figsize=(16, 8), returnfig=True, tight_layout=True,
            panel_ratios=(5, 1),
        )
        if has_anchors:
            plot_kwargs["addplot"] = addplots
        if hlines_prices:
            plot_kwargs["hlines"] = dict(hlines=hlines_prices, colors=hlines_colors,
                                         linewidths=hlines_widths, linestyle=hlines_styles)
        fig, axes = mpf.plot(chart_df, **plot_kwargs)
        ax = axes[0]

        # Draw lines from anchor candle to the level (visual connection)
        for l in levels:
            idx = l.anchor_candle_idx
            if idx < 0 or idx >= n:
                continue
            lbl_color = "#2E7D32" if l.level_type == "support" else "#D32F2F"
            # Vertical dotted line from candle body to the level price
            body_edge = min(chart_df["open"].iloc[idx], chart_df["close"].iloc[idx]) \
                        if l.level_type == "support" else \
                        max(chart_df["open"].iloc[idx], chart_df["close"].iloc[idx])
            ax.plot([idx, idx], [body_edge, l.price], color=lbl_color,
                    linewidth=0.5, linestyle=":", alpha=0.5)

        # Labels on right side
        y_min, y_max = ax.get_ylim()
        label_positions = []
        min_gap = (y_max - y_min) * 0.03

        def safe_y(target_y):
            adjusted = target_y
            for _ in range(15):
                conflict = False
                for used_y in label_positions:
                    if abs(adjusted - used_y) < min_gap:
                        adjusted = used_y + min_gap if adjusted >= used_y else used_y - min_gap
                        conflict = True
                if not conflict:
                    break
            label_positions.append(adjusted)
            return adjusted

        for l in sorted(levels, key=lambda x: x.price, reverse=True):
            if y_min <= l.price <= y_max:
                tag = "S" if l.level_type == "support" else "R"
                lbl_color = "#2E7D32" if l.level_type == "support" else "#D32F2F"
                anchor_tag = f" [{l.anchor_type}]" if l.anchor_type else ""
                role_tag = f" {l.structural_role}" if l.structural_role else ""
                label = f"{tag} {fmt_price(l.price)} str={l.strength:.2f}{anchor_tag}{role_tag}"
                y = safe_y(l.price)
                ax.annotate(label, xy=(1.005, y), xycoords=("axes fraction", "data"),
                           fontsize=6.5, color=lbl_color, va="center", ha="left")

        # Legend for anchor markers
        from matplotlib.lines import Line2D as L2D
        legend_els = []
        if has_anchors:
            legend_els.append(L2D([0], [0], marker="^", color="#2E7D32", linestyle="None",
                                  markersize=8, label="Support anchor"))
            legend_els.append(L2D([0], [0], marker="v", color="#D32F2F", linestyle="None",
                                  markersize=8, label="Resistance anchor"))
            legend_els.append(L2D([0], [0], color="#999", linestyle=":",
                                  linewidth=0.5, label="Body → level"))
        if legend_els:
            ax.legend(handles=legend_els, loc="upper left", fontsize=6.5,
                     facecolor="white", edgecolor="#CCC", framealpha=0.9)

        title = f"{symbol}/USDT | {fmt_price(price)} | [{key.upper()}] {name} — {len(levels)} levels"
        ax.set_title(title, fontsize=10, color="#222", fontweight="bold", pad=12, loc="left")

        fig.subplots_adjust(right=0.82)
        outfile = os.path.join(output_dir, f"{symbol}_diag_{key}.png")
        fig.savefig(outfile, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"  [{key}] Saved: {outfile} ({len(levels)} levels)", file=sys.stderr)

    # ── Combined overlay chart ────────────────────────────────
    plt.close("all")
    fig, axes = mpf.plot(
        chart_df, type="candle", style=style, volume=True,
        figsize=(18, 9), returnfig=True, tight_layout=True,
        panel_ratios=(5, 1),
    )
    ax = axes[0]

    # Draw all levels color-coded by detector
    for key, levels in results.items():
        color = DETECTOR_COLORS[key]
        for l in levels:
            alpha = max(0.3, min(0.8, l.strength))
            lw = max(0.3, min(1.2, l.strength * 1.5))
            ls = "solid" if l.strength >= 0.5 else "dashed"
            ax.axhline(y=l.price, color=color, linewidth=lw, alpha=alpha, linestyle=ls)

    # Legend
    legend_elements = [
        Line2D([0], [0], color=DETECTOR_COLORS[k], lw=1.2, label=f"{DETECTOR_NAMES[k]}")
        for k in DETECTOR_NAMES
    ]
    ax.legend(handles=legend_elements, loc="upper left", fontsize=7, ncol=1,
             facecolor="white", edgecolor="#CCC", framealpha=0.9)

    total = sum(len(v) for v in results.values())
    ax.set_title(f"{symbol}/USDT | {fmt_price(price)} | ALL DETECTORS — {total} levels",
                fontsize=10, color="#222", fontweight="bold", pad=12, loc="left")

    fig.subplots_adjust(right=0.85)
    outfile = os.path.join(output_dir, f"{symbol}_diag_combined.png")
    fig.savefig(outfile, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  [combined] Saved: {outfile} ({total} total levels)", file=sys.stderr)


def diagnose(symbol: str, days: int = 180, data_dir: str = "data",
             output_dir: str = "output"):
    """Run full diagnostic for one symbol."""
    symbol = symbol.upper()

    # Load data
    df = load_from_cache(symbol, data_dir)
    if df is None or len(df) < 30:
        df = fetch_data(symbol, days)

    price = float(df["close"].iloc[-1])

    # Run detectors
    results = run_all_detectors(df)

    # Print table
    print_detector_table(symbol, price, results)

    # Generate charts
    generate_detector_charts(symbol, df, results, output_dir)


def main():
    parser = argparse.ArgumentParser(description="Diagnose S/R detectors independently")
    parser.add_argument("symbols", nargs="+", help="BTC ETH SOL etc.")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--data-dir", type=str, default="data")
    parser.add_argument("--output-dir", type=str, default="output")
    args = parser.parse_args()

    for symbol in args.symbols:
        try:
            diagnose(symbol, args.days, args.data_dir, args.output_dir)
        except Exception as e:
            print(f"[{symbol}] Error: {e}", file=sys.stderr)
            import traceback; traceback.print_exc()


if __name__ == "__main__":
    main()
