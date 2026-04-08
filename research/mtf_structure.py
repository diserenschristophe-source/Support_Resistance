#!/usr/bin/env python3
"""
mtf_structure.py — Multi-Timeframe SMA Regime Gate
====================================================

Detects regime on three timeframes using price position + SMA slope:
  - U (Up):   price > SMA AND SMA is rising (slope > 0)
  - D (Down): price < SMA AND SMA is falling (slope < 0)
  - T (Trans): price and slope disagree, or SMA is flat

Timeframes:
  - LT (Long-Term):  SMA 100, slope measured over 20 bars
  - MT (Mid-Term):   SMA 50,  slope measured over 10 bars
  - ST (Short-Term): SMA 20,  slope measured over 5 bars

3 timeframes × 3 states = 27 combinations → ON / OFF gate.

Usage:
    python3 mtf_structure.py BTC
    python3 mtf_structure.py BTC ETH SOL
    python3 mtf_structure.py --all
    python3 mtf_structure.py BTC --chart
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── Path setup ───────────────────────────────────────────────
_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _PARENT_DIR)

from core.models import fmt_price
from core.filters import (
    MTF_REGIME_CONFIG as REGIME_CONFIG,
    MTF_GATE_TABLE as GATE_TABLE,
    detect_regime, detect_regime_series, compute_mtf_gate,
)


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class RegimeResult:
    timeframe: str      # "LT" | "MT" | "ST"
    label: str          # "Long-Term" etc.
    state: str          # U | D | T
    sma_value: float    # current SMA value
    sma_period: int     # e.g. 100
    price_vs_sma: str   # "above" | "below"
    slope: str          # "rising" | "falling" | "flat"


@dataclass
class MTFResult:
    symbol: str
    price: float
    gate: bool          # ON (True) or OFF (False)
    lt: RegimeResult
    mt: RegimeResult
    st: RegimeResult
    combo: str          # "U/T/D" etc.
    as_of: str


# ─────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────

def load_daily(symbol: str, data_dir: str) -> Optional[pd.DataFrame]:
    """Load daily OHLCV from cache."""
    path = os.path.join(data_dir, f"{symbol.upper()}_daily.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.columns = [c.lower().strip() for c in df.columns]
    return df


# ─────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────

def analyze_mtf(symbol: str, data_dir: str) -> MTFResult:
    """Full multi-timeframe SMA regime analysis."""
    df = load_daily(symbol, data_dir)

    if df is None or len(df) < 20:
        empty = RegimeResult("", "", "T", 0, 0, "", "")
        return MTFResult(
            symbol=symbol, price=0, gate=False,
            lt=empty, mt=empty, st=empty,
            combo="T/T/T", as_of="",
        )

    close = df["close"]
    price = float(close.iloc[-1])
    as_of = df.index[-1].strftime("%Y-%m-%d")
    n = len(close)

    results = {}
    for tf_key, cfg in REGIME_CONFIG.items():
        sma_period = cfg["sma"]
        slope_bars = cfg["slope_bars"]
        confirm = cfg["confirm"]
        state = detect_regime(close, sma_period, slope_bars, confirm)

        if n >= sma_period + slope_bars:
            sma = close.rolling(sma_period).mean()
            sma_now = float(sma.iloc[-1])
            sma_prev = float(sma.iloc[-1 - slope_bars])
            pos = "above" if price > sma_now else "below"
            slp = "rising" if sma_now > sma_prev else "falling"
        else:
            sma_now = 0
            pos = "n/a"
            slp = "n/a"

        results[tf_key] = RegimeResult(
            timeframe=tf_key,
            label=cfg["label"],
            state=state,
            sma_value=round(sma_now, 2),
            sma_period=sma_period,
            price_vs_sma=pos,
            slope=slp,
        )

    combo_key = (results["LT"].state, results["MT"].state, results["ST"].state)
    gate = GATE_TABLE.get(combo_key, False)
    combo_str = f"{results['LT'].state}/{results['MT'].state}/{results['ST'].state}"

    return MTFResult(
        symbol=symbol, price=price, gate=gate,
        lt=results["LT"], mt=results["MT"], st=results["ST"],
        combo=combo_str, as_of=as_of,
    )


# ─────────────────────────────────────────────────────────────
# Output formatting
# ─────────────────────────────────────────────────────────────

STATE_ICON = {"U": "🟢", "D": "🔴", "T": "🟡"}
STATE_LABEL = {"U": "Up", "D": "Down", "T": "Trans"}


def print_mtf_result(r: MTFResult):
    sep = "=" * 70
    line = "─" * 70
    gate_str = "🟢 ON" if r.gate else "🔴 OFF"

    print(f"\n{sep}")
    print(f"  {r.symbol}  SMA Regime Gate  |  {fmt_price(r.price)}")
    print(f"  As of: {r.as_of}")
    print(sep)

    for reg in (r.lt, r.mt, r.st):
        icon = STATE_ICON.get(reg.state, "?")
        label = STATE_LABEL.get(reg.state, reg.state)
        print(f"  {reg.label:<12}: {icon} {reg.state} ({label:<5})  "
              f"SMA{reg.sma_period}={fmt_price(reg.sma_value)}  "
              f"price {reg.price_vs_sma}  slope {reg.slope}")

    print(line)
    print(f"  Combo:  {r.combo}")
    print(f"  Gate:   {gate_str}")
    print(sep)


# ─────────────────────────────────────────────────────────────
# Chart overlay — LT and MT regime on BTC candlestick
# ─────────────────────────────────────────────────────────────

def generate_regime_chart(symbol: str, data_dir: str, output: str = None):
    """Generate candlestick chart with LT and MT regime background colors."""
    try:
        import mplfinance as mpf
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        print("pip install mplfinance matplotlib", file=sys.stderr)
        return

    df = load_daily(symbol, data_dir)
    if df is None:
        print(f"No data for {symbol}", file=sys.stderr)
        return

    close = df["close"]
    price = float(close.iloc[-1])

    # Compute confirmed regime series for all 3 timeframes
    lt_cfg = REGIME_CONFIG["LT"]
    mt_cfg = REGIME_CONFIG["MT"]
    st_cfg = REGIME_CONFIG["ST"]
    lt_regime = detect_regime_series(close, lt_cfg["sma"], lt_cfg["slope_bars"], lt_cfg["confirm"])
    mt_regime = detect_regime_series(close, mt_cfg["sma"], mt_cfg["slope_bars"], mt_cfg["confirm"])
    st_regime = detect_regime_series(close, st_cfg["sma"], st_cfg["slope_bars"], st_cfg["confirm"])

    # Chart setup — 4 panels: candles, LT strip, MT strip, ST strip
    chart_df = df.copy()
    if not isinstance(chart_df.index, pd.DatetimeIndex):
        chart_df.index = pd.to_datetime(chart_df.index)
    chart_df.index.name = "Date"

    # Encode regimes as numeric for addplot (U=1, T=0, D=-1)
    regime_map = {"U": 1.0, "T": 0.0, "D": -1.0}
    lt_num = lt_regime.map(regime_map).fillna(0).astype(float)
    mt_num = mt_regime.map(regime_map).fillna(0).astype(float)
    st_num = st_regime.map(regime_map).fillna(0).astype(float)

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

    # SMAs as overlays on main panel
    sma_plots = []
    for period, color, lw in [
        (st_cfg["sma"], "#81C784", 0.6),
        (mt_cfg["sma"], "#F57C00", 1.0),
        (lt_cfg["sma"], "#1976D2", 1.2),
    ]:
        if len(df) >= period:
            sma = close.rolling(period).mean()
            sma_plots.append(mpf.make_addplot(sma, color=color, width=lw,
                                              linestyle="dashed", panel=0))

    fig, axes = mpf.plot(
        chart_df, type="candle", style=style, volume=False,
        addplot=sma_plots if sma_plots else None,
        figsize=(18, 10), returnfig=True, tight_layout=True,
    )
    ax = axes[0]

    # Paint 3 separate regime strips at the bottom of the main panel
    REGIME_COLORS = {"U": "#4CAF50", "D": "#E53935", "T": "#FFD54F"}
    n = len(chart_df)
    y_min, y_max = ax.get_ylim()
    total_strip = (y_max - y_min) * 0.08  # 8% of chart height for all 3 strips
    strip_h = total_strip / 3
    gap = strip_h * 0.15

    strips = [
        ("LT", lt_regime, y_min + 2 * (strip_h + gap)),
        ("MT", mt_regime, y_min + 1 * (strip_h + gap)),
        ("ST", st_regime, y_min),
    ]

    for label, regime_series, y_base in strips:
        for i in range(n):
            color = REGIME_COLORS.get(regime_series.iloc[i], "#EEEEEE")
            ax.add_patch(plt.Rectangle(
                (i - 0.5, y_base), 1, strip_h,
                facecolor=color, alpha=0.7, zorder=2, edgecolor="none"))
        # Strip label on the left
        ax.text(-1, y_base + strip_h / 2, label, fontsize=7, fontweight="bold",
                color="#333", va="center", ha="right", zorder=3)

    # Adjust y_min to accommodate strips
    ax.set_ylim(y_min - total_strip * 0.1, y_max)

    # Legend
    legend_elements = [
        mpatches.Patch(facecolor="#4CAF50", alpha=0.7, label="U (Up)"),
        mpatches.Patch(facecolor="#E53935", alpha=0.7, label="D (Down)"),
        mpatches.Patch(facecolor="#FFD54F", alpha=0.7, label="T (Trans)"),
        plt.Line2D([0], [0], color="#81C784", lw=0.6, ls="--",
                   label=f"SMA {st_cfg['sma']} (ST)"),
        plt.Line2D([0], [0], color="#F57C00", lw=1.0, ls="--",
                   label=f"SMA {mt_cfg['sma']} (MT)"),
        plt.Line2D([0], [0], color="#1976D2", lw=1.2, ls="--",
                   label=f"SMA {lt_cfg['sma']} (LT)"),
    ]
    ax.legend(handles=legend_elements, loc="upper left", fontsize=7,
             facecolor="white", edgecolor="#CCC", framealpha=0.9)

    # Title with current regime states
    lt_now = lt_regime.iloc[-1]
    mt_now = mt_regime.iloc[-1]
    st_now = st_regime.iloc[-1]
    combo = f"{lt_now}/{mt_now}/{st_now}"
    gate = GATE_TABLE.get((lt_now, mt_now, st_now), False)
    gate_str = "ON" if gate else "OFF"
    ax.set_title(
        f"{symbol}/USDT | {fmt_price(price)} | "
        f"LT:{lt_now} MT:{mt_now} ST:{st_now} | Gate: {gate_str}",
        fontsize=10, color="#222", fontweight="bold", pad=12, loc="left")

    fig.subplots_adjust(right=0.95)

    if output is None:
        os.makedirs("output", exist_ok=True)
        output = f"output/{symbol}_regime.png"

    fig.savefig(output, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[{symbol}] Regime chart saved: {output}", file=sys.stderr)
    return output


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Multi-timeframe SMA regime gate",
    )
    parser.add_argument("tokens", nargs="*", help="Token symbols")
    parser.add_argument("--all", action="store_true",
                        help="Analyze all tokens with daily cache")
    parser.add_argument("--data-dir", default="data",
                        help="Daily CSV cache directory")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON")
    parser.add_argument("--chart", action="store_true",
                        help="Generate regime overlay chart")
    args = parser.parse_args()

    if args.all:
        data_path = Path(args.data_dir)
        if not data_path.exists():
            print(f"Error: '{args.data_dir}' not found.", file=sys.stderr)
            sys.exit(1)
        tokens = sorted([
            f.stem.replace("_daily", "").upper()
            for f in data_path.glob("*_daily.csv")
        ])
    elif args.tokens:
        tokens = [t.upper() for t in args.tokens]
    else:
        parser.print_help()
        sys.exit(0)

    results = [analyze_mtf(symbol, args.data_dir) for symbol in tokens]

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for r in results:
            print_mtf_result(r)

        if len(results) > 1:
            sep = "=" * 70
            print(f"\n{sep}")
            print(f"  SUMMARY  ({len(results)} tokens)")
            print(f"{'─' * 70}")
            print(f"  {'Token':<8} {'Gate':<6} {'Combo':<8} {'LT':<12} {'MT':<12} {'ST':<12}")
            print(f"{'─' * 70}")
            for r in results:
                g = "🟢 ON " if r.gate else "🔴 OFF"
                print(f"  {r.symbol:<8} {g}  {r.combo:<8}")
            print(sep)

    if args.chart:
        for symbol in tokens:
            generate_regime_chart(symbol, args.data_dir)


if __name__ == "__main__":
    main()
