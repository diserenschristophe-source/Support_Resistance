#!/usr/bin/env python3
"""
chart.py — Candlestick charts with S/R zones
"""

import argparse, sys, os, json
from datetime import datetime
import pandas as pd

try:
    import mplfinance as mpf
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
except ImportError:
    print("pip3 install matplotlib mplfinance", file=sys.stderr); sys.exit(1)

from core.fetcher import load_from_cache
from core.sr_analysis import ProfessionalSRAnalysis
from core.models import SRZone, fmt_price


def generate_chart(symbol: str, output: str = None,
                   df: pd.DataFrame = None, result: dict = None,
                   json_path: str = None, data_dir: str = "data"):
    """Generate S/R chart. Accepts optional pre-computed df and result to avoid double fetch."""
    symbol = symbol.upper()

    # Clear any prior matplotlib state
    plt.close("all")

    # ── Load OHLCV data ──
    if df is None:
        df = load_from_cache(symbol, data_dir)
        if df is None or len(df) == 0:
            raise FileNotFoundError(
                f"No cache for {symbol} at {data_dir} — run main.py fetch first"
            )
        print(f"[{symbol}] Loaded {len(df)} candles from cache", file=sys.stderr)
    elif result is not None:
        print(f"[{symbol}] Using pre-fetched data ({len(df)} candles, last: {df.index[-1]})", file=sys.stderr)

    # ── Load S/R levels from JSON if no result provided ──
    if result is None and json_path:
        _, result = load_from_rank_json(json_path, symbol)
        if result:
            print(f"[{symbol}] Using S/R levels from {json_path}", file=sys.stderr)

    # Try default JSON paths if not specified or not found
    if result is None:
        for default_path in ["output/rank_output.json", "output/daily_output.json"]:
            if os.path.exists(default_path):
                _, result = load_from_rank_json(default_path, symbol)
                if result:
                    print(f"[{symbol}] Using S/R levels from {default_path}", file=sys.stderr)
                    break

    # Fallback: run live analysis
    if result is None:
        print(f"[{symbol}] No JSON found — running live analysis", file=sys.stderr)
        analysis = ProfessionalSRAnalysis(df)
        result = analysis.analyze()

    ms = result["market_structure"]
    vp = result["volume_profile"]
    sup = result["support_zones"]
    res = result["resistance_zones"]
    price = ms["current_price"]

    chart_df = df.copy()
    if not isinstance(chart_df.index, pd.DatetimeIndex):
        chart_df.index = pd.to_datetime(chart_df.index)
    chart_df = chart_df.tail(365)

    # ── Smart candle trimming: reduce window when S/R is compressed ──
    all_levels = [z.key_level for z in sup + res] + [price]
    if all_levels and len(chart_df) > 100:
        sr_min = min(all_levels)
        sr_max = max(all_levels)
        sr_range = sr_max - sr_min
        candle_range = chart_df["high"].max() - chart_df["low"].min()
        if candle_range > 0 and sr_range < candle_range * 0.25:
            # Find how many recent candles fit within the S/R price range
            padding = sr_range * 0.4
            y_low = max(0, sr_min - padding)
            y_high = sr_max + padding
            # Walk backwards: keep candles whose low is below y_high
            n = len(chart_df)
            for i in range(n):
                if chart_df.iloc[n - 1 - i]["high"] > y_high:
                    break
            visible = max(i, 100)
            chart_df = chart_df.tail(visible)

    chart_df.index.name = "Date"

    # ── Horizontal lines + zones ──────────────────────────────
    hlines_prices, hlines_colors, hlines_widths, hlines_styles = [], [], [], []
    fill_zones = []

    for z in res:
        hlines_prices.append(z.key_level)
        hlines_colors.append("#D32F2F" if z.tier == "Major" else "#E57373")
        hlines_widths.append(1.2 if z.tier == "Major" else 0.6)
        hlines_styles.append("solid" if z.tier == "Major" else "dashed")
        fill_zones.append(("R", z.price_low, z.price_high, z.key_level, z.tier, z.volume_confirmed))

    for z in sup:
        hlines_prices.append(z.key_level)
        hlines_colors.append("#2E7D32" if z.tier == "Major" else "#81C784")
        hlines_widths.append(1.2 if z.tier == "Major" else 0.6)
        hlines_styles.append("solid" if z.tier == "Major" else "dashed")
        fill_zones.append(("S", z.price_low, z.price_high, z.key_level, z.tier, z.volume_confirmed))

    # ── SMA overlays ──────────────────────────────────────────
    sma_plots = []
    for period, color in [(20, "#1976D2"), (50, "#F57C00"), (100, "#7B1FA2")]:
        if len(df) >= period:
            sma = df["close"].rolling(period).mean().tail(len(chart_df))
            sma_plots.append(mpf.make_addplot(sma, color=color, width=0.7, linestyle="dashed"))

    # ── White style ───────────────────────────────────────────
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

    # ── Plot ──────────────────────────────────────────────────
    fig, axes = mpf.plot(
        chart_df, type="candle", style=style, volume=True,
        addplot=sma_plots if sma_plots else None,
        hlines=dict(hlines=hlines_prices, colors=hlines_colors,
                    linewidths=hlines_widths, linestyle=hlines_styles) if hlines_prices else None,
        figsize=(16, 8), returnfig=True, tight_layout=True,
        panel_ratios=(5, 1),
    )
    ax = axes[0]

    # Shaded zones
    for ztype, low, high, key, tier, vol in fill_zones:
        alpha = 0.12 if tier == "Major" else 0.06
        color = "#D32F2F" if ztype == "R" else "#2E7D32"
        ax.axhspan(low, high, alpha=alpha, color=color, zorder=0)

    # ── Right-side labels — ordered top to bottom, no overlap ──
    # Build label list in strict order:
    #   R3 (furthest) → R2 → R1 (nearest) → price → S1 (nearest) → S2 → S3 (furthest)
    y_min, y_max = ax.get_ylim()
    min_gap = (y_max - y_min) * 0.035

    labels = []

    # Resistances: furthest first (high to low)
    for z in sorted(res, key=lambda x: x.key_level, reverse=True):
        if y_min <= z.key_level <= y_max:
            tier_tag = "[Major]" if z.tier == "Major" else "[Minor]"
            vol_tag = " [V]" if z.volume_confirmed else ""
            anchor_tag = f" [{z.anchor_type}]" if z.anchor_type else ""
            labels.append({
                "target_y": z.key_level,
                "text": f"R {fmt_price(z.key_level)} {tier_tag}{vol_tag}{anchor_tag}",
                "color": "#D32F2F",
                "bold": z.tier == "Major",
            })

    # Supports: nearest first (high to low)
    for z in sorted(sup, key=lambda x: x.key_level, reverse=True):
        if y_min <= z.key_level <= y_max:
            tier_tag = "[Major]" if z.tier == "Major" else "[Minor]"
            vol_tag = " [V]" if z.volume_confirmed else ""
            anchor_tag = f" [{z.anchor_type}]" if z.anchor_type else ""
            labels.append({
                "target_y": z.key_level,
                "text": f"S {fmt_price(z.key_level)} {tier_tag}{vol_tag}{anchor_tag}",
                "color": "#2E7D32",
                "bold": z.tier == "Major",
            })

    # Resolve positions top-down: each label must be below the previous
    # by at least min_gap, nudged down if needed. Labels that would fall
    # below y_min are clamped to y_min (prevents overflow into volume panel).
    bottom_limit = y_min + min_gap * 0.5
    placed = []
    placed_labels = []
    for lbl in labels:
        y = lbl["target_y"]
        if placed:
            max_allowed = placed[-1] - min_gap
            if y > max_allowed:
                y = max_allowed
        y = min(y, y_max - min_gap * 0.5)
        y = max(y, bottom_limit)
        placed.append(y)
        placed_labels.append(lbl)

    for i, lbl in enumerate(placed_labels):
        ax.annotate(lbl["text"], xy=(1.005, placed[i]),
                   xycoords=("axes fraction", "data"),
                   fontsize=7, color=lbl["color"],
                   fontweight="bold" if lbl["bold"] else "normal",
                   va="center", ha="left")

    # (POC line removed — all context is in the text report)

    # ── Title — clean: just symbol + price ──────────────────
    title = f"{symbol}/USDT | {fmt_price(price)}"
    ax.set_title(title, fontsize=10, color="#222", fontweight="bold", pad=12, loc="left")

    # ── Legend ─────────────────────────────────────────────────
    legend_elements = [
        Line2D([0], [0], color="#2E7D32", lw=1.2, label="Support Major"),
        Line2D([0], [0], color="#81C784", lw=0.6, ls="--", label="Support Minor"),
        Line2D([0], [0], color="#D32F2F", lw=1.2, label="Resistance Major"),
        Line2D([0], [0], color="#E57373", lw=0.6, ls="--", label="Resistance Minor"),
        Line2D([0], [0], color="#1976D2", lw=0.7, ls="--", label="SMA 20"),
        Line2D([0], [0], color="#F57C00", lw=0.7, ls="--", label="SMA 50"),
        Line2D([0], [0], color="#7B1FA2", lw=0.7, ls="--", label="SMA 100"),
    ]
    ax.legend(handles=legend_elements, loc="upper left", fontsize=6.5, ncol=2,
             facecolor="white", edgecolor="#CCC", framealpha=0.9, handlelength=1.5)

    # ── Date/time — prominent, top right ────────────────────────
    now = datetime.now()
    ax.text(0.99, 1.02, now.strftime("%d.%m.%y  %H:%M"),
            transform=ax.transAxes, fontsize=9, color="#555",
            va="bottom", ha="right", fontweight="bold")

    # ── SMA + POC + VA info box (top right) ───────────────────
    info_lines = []
    for period, color, label in [(20, "#1976D2", "SMA 20"), (50, "#F57C00", "SMA 50"), (100, "#7B1FA2", "SMA 100")]:
        if len(df) >= period:
            val = float(df["close"].rolling(period).mean().iloc[-1])
            rel = "▼" if price < val else "▲"
            info_lines.append(f"{label}: {fmt_price(val)} {rel}")
    if vp["poc"]:
        info_lines.append(f"POC: {fmt_price(vp['poc'])}")
    if vp.get("val") and vp.get("vah"):
        info_lines.append(f"VA: {fmt_price(vp['val'])}–{fmt_price(vp['vah'])}")

    if info_lines:
        info_text = "\n".join(info_lines)
        ax.text(0.99, 0.98, info_text, transform=ax.transAxes, fontsize=6.5, color="#555",
               va="top", ha="right", family="monospace",
               bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                        edgecolor="#CCC", alpha=0.85))

    fig.subplots_adjust(right=0.85)

    # Save to output/ directory with the filename the dashboard API expects
    if output:
        outfile = output
    else:
        outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        os.makedirs(outdir, exist_ok=True)
        date_tag = datetime.now().strftime("%d.%m.%y")
        outfile = os.path.join(outdir, f"{symbol}_chart{date_tag}.png")

    fig.savefig(outfile, dpi=150, bbox_inches="tight", facecolor="white")
    print(f"[{symbol}] Saved: {outfile} (price=${price:,.0f}, data through {df.index[-1].strftime('%Y-%m-%d')})", file=sys.stderr)
    plt.close(fig)
    return outfile


def load_from_rank_json(json_path: str, symbol: str):
    """Load pre-computed analysis from rank_output.json and reconstruct SRZone objects."""
    if not os.path.exists(json_path):
        return None, None

    with open(json_path) as f:
        data = json.load(f)

    # Find token in ranking, analyses, or disqualified
    token = None
    for source in [data.get("analyses", []), data.get("ranking", []), data.get("disqualified", [])]:
        for t in source:
            if t.get("symbol", "").upper() == symbol.upper():
                token = t
                break
        if token:
            break
    if not token:
        return None, None

    ms = token.get("market_structure", {})
    if "current_price" not in ms and "price" in token:
        ms["current_price"] = token["price"]
    vp = token.get("volume_profile", {})
    supports_raw = token.get("support", [])
    resistances_raw = token.get("resistance", [])

    atr = ms.get("atr14", 0)
    hw = atr * 0.2

    def to_zone(z_dict, zone_type):
        key = z_dict.get("key_level", 0)
        zone = z_dict.get("zone", [key - hw, key + hw])
        return SRZone(
            price_low=zone[0], price_high=zone[1],
            mid_price=key, key_level=key,
            zone_type=zone_type,
            tier=z_dict.get("tier", "Minor"),
            confluence_score=z_dict.get("confluence", 1),
            touches=z_dict.get("touches", 0),
            volume_confirmed=z_dict.get("volume_confirmed", False),
            label="", action="",
            anchor_type=z_dict.get("anchor_type", ""),
            structural_role=z_dict.get("structural_role", ""),
        )

    sup = [to_zone(z, "support") for z in supports_raw]
    res = [to_zone(z, "resistance") for z in resistances_raw]

    result = {
        "market_structure": ms,
        "volume_profile": vp or {"poc": None, "vah": None, "val": None},
        "support_zones": sup,
        "resistance_zones": res,
    }
    return token, result


def main():
    parser = argparse.ArgumentParser(description="Generate S/R charts")
    parser.add_argument("symbols", nargs="+", help="BTC ETH SOL etc.")
    parser.add_argument("--output", "-o", type=str, default=None)
    parser.add_argument("--json", type=str, default=None,
                        help="Path to rank_output.json or daily_output.json (single source of truth)")
    parser.add_argument("--data-dir", type=str, default="data",
                        help="Directory with cached OHLCV CSVs")
    args = parser.parse_args()
    for symbol in args.symbols:
        try:
            out = args.output if args.output and len(args.symbols) == 1 else None
            generate_chart(symbol, output=out,
                          json_path=args.json, data_dir=args.data_dir)
        except Exception as e:
            print(f"[{symbol}] Error: {e}", file=sys.stderr)
            import traceback; traceback.print_exc()

if __name__ == "__main__":
    main()
