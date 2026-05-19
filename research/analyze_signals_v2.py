#!/usr/bin/env python3
"""
analyze_signals_v2.py — diagnose where the v2 signal edge lives (or doesn't).

Reads reports/backtest_break_retest_v2/{universe}_signals.csv and slices
win-rate / expectancy by feature buckets:

  - Published vs not
  - Confluence bucket
  - Touches bucket
  - Breakout magnitude (× ATR) bucket
  - Token RSI bucket
  - Planned R:R bucket
  - Per-token
  - Per exit reason

Prints a markdown report; also writes it to
reports/backtest_break_retest_v2/{universe}_diagnostics.md.

Usage:
  python3 research/analyze_signals_v2.py --universe selected17
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
REPORT_DIR = ROOT / "reports" / "backtest_break_retest_v2"


def bucket_stats(df: pd.DataFrame, label: str) -> dict:
    """Stats for one slice of signals."""
    if df.empty:
        return {
            "label": label, "n_signals": 0, "n_filled": 0, "fill_pct": 0.0,
            "wr_filled": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "avg_pnl_filled": 0.0, "expectancy_per_signal": 0.0,
        }
    filled = df[df["filled"] == True]
    nf = len(filled)
    if nf == 0:
        return {
            "label": label, "n_signals": len(df), "n_filled": 0, "fill_pct": 0.0,
            "wr_filled": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "avg_pnl_filled": 0.0, "expectancy_per_signal": 0.0,
        }
    wins = filled[filled["pnl_pct"] > 0]
    losses = filled[filled["pnl_pct"] <= 0]
    return {
        "label": label,
        "n_signals": int(len(df)),
        "n_filled": int(nf),
        "fill_pct": round(100 * nf / len(df), 1),
        "wr_filled": round(100 * len(wins) / nf, 1),
        "avg_win": round(wins["pnl_pct"].mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses["pnl_pct"].mean(), 2) if len(losses) else 0.0,
        "avg_pnl_filled": round(filled["pnl_pct"].mean(), 2),
        "expectancy_per_signal": round(filled["pnl_pct"].mean() * (nf / len(df)), 2),
    }


def render_table(rows: list[dict], title: str) -> str:
    L = [f"### {title}", ""]
    L.append("| Bucket | Signals | Filled | Fill% | WR% | Avg win | Avg loss | Avg pnl (filled) | **Expectancy/signal** |")
    L.append("|--------|---------|--------|-------|-----|---------|----------|------------------|----------------------|")
    for r in rows:
        L.append(f"| {r['label']} | {r['n_signals']} | {r['n_filled']} | "
                 f"{r['fill_pct']:.1f}% | {r['wr_filled']:.1f}% | "
                 f"{r['avg_win']:+.2f}% | {r['avg_loss']:+.2f}% | "
                 f"{r['avg_pnl_filled']:+.2f}% | **{r['expectancy_per_signal']:+.3f}%** |")
    L.append("")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="selected17")
    args = p.parse_args()

    csv = REPORT_DIR / f"{args.universe}_signals.csv"
    if not csv.exists():
        raise SystemExit(f"Missing {csv}. Run backtest_break_retest_v2.py first.")

    df = pd.read_csv(csv)
    df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
    df["filled"] = df["filled"].astype(bool)
    df["published"] = df["published"].astype(bool)

    out = []
    out.append(f"# Signal diagnostics — {args.universe}")
    out.append("")
    out.append(f"Total signals: **{len(df)}**   Published: **{df['published'].sum()}**   "
               f"Filled: **{df['filled'].sum()}**")
    out.append("")

    # 1. Published vs not
    rows = [
        bucket_stats(df[df["published"] == True], "PUBLISHED"),
        bucket_stats(df[(df["published"] == False) & (df["skip_reason"] == "quality")], "DROPPED: quality"),
        bucket_stats(df[(df["published"] == False) & (df["skip_reason"] == "momentum")], "DROPPED: momentum"),
        bucket_stats(df[(df["published"] == False) & (df["skip_reason"] == "per_token_cap")], "DROPPED: per-token cap"),
    ]
    out.append(render_table(rows, "Published vs dropped (would they have performed?)"))

    pub = df[df["published"] == True]

    # 2. Confluence buckets (within published)
    bins = [-0.01, 30, 50, 70, 200]
    labels = ["0-30", "30-50", "50-70", "70+"]
    pub_b = pub.copy()
    pub_b["bucket"] = pd.cut(pub_b["confluence"], bins=bins, labels=labels)
    rows = [bucket_stats(pub_b[pub_b["bucket"] == lab], lab) for lab in labels]
    out.append(render_table(rows, "Confluence bucket (published only)"))

    # 3. Touches buckets
    bins = [-1, 2, 4, 7, 999]
    labels = ["0-2", "3-4", "5-7", "8+"]
    pub_b = pub.copy()
    pub_b["bucket"] = pd.cut(pub_b["touches"], bins=bins, labels=labels)
    rows = [bucket_stats(pub_b[pub_b["bucket"] == lab], lab) for lab in labels]
    out.append(render_table(rows, "Touches bucket (published only)"))

    # 4. Breakout magnitude × ATR
    bins = [0, 0.75, 1.25, 2.0, 100]
    labels = ["0.5-0.75", "0.75-1.25", "1.25-2.0", "2.0+"]
    pub_b = pub.copy()
    pub_b["bucket"] = pd.cut(pub_b["breakout_magnitude_atr"], bins=bins, labels=labels)
    rows = [bucket_stats(pub_b[pub_b["bucket"] == lab], lab) for lab in labels]
    out.append(render_table(rows, "Breakout magnitude (× ATR) — published only"))

    # 5. Token RSI
    bins = [-1, 55, 60, 65, 70, 200]
    labels = ["50-55", "55-60", "60-65", "65-70", "70+"]
    pub_b = pub.copy()
    pub_b["bucket"] = pd.cut(pub_b["token_rsi"], bins=bins, labels=labels)
    rows = [bucket_stats(pub_b[pub_b["bucket"] == lab], lab) for lab in labels]
    out.append(render_table(rows, "Token RSI bucket (published only)"))

    # 6. Planned R:R buckets
    bins = [-100, 1.0, 1.5, 2.0, 3.0, 100]
    labels = ["<1.0", "1.0-1.5", "1.5-2.0", "2.0-3.0", "3.0+"]
    pub_b = pub.copy()
    pub_b["bucket"] = pd.cut(pub_b["raw_rr"], bins=bins, labels=labels)
    rows = [bucket_stats(pub_b[pub_b["bucket"] == lab], lab) for lab in labels]
    out.append(render_table(rows, "Planned R:R at entry (published only)"))

    # 7. Per-token
    rows = []
    for sym in sorted(pub["symbol"].unique()):
        rows.append(bucket_stats(pub[pub["symbol"] == sym], sym))
    rows.sort(key=lambda r: r["expectancy_per_signal"], reverse=True)
    out.append(render_table(rows, "Per-token (published only) — sorted by expectancy"))

    # 8. Exit-reason mix among filled
    filled_pub = pub[pub["filled"] == True]
    if not filled_pub.empty:
        rows = []
        for reason in ["TP", "SL", "TIMEOUT", "OPEN"]:
            sub = filled_pub[filled_pub["exit_reason"] == reason]
            if sub.empty:
                continue
            avg = sub["pnl_pct"].mean()
            pos = (sub["pnl_pct"] > 0).sum()
            rows.append({
                "label": reason, "n_signals": len(sub), "n_filled": len(sub),
                "fill_pct": 100.0, "wr_filled": round(100 * pos / len(sub), 1),
                "avg_win": round(sub[sub["pnl_pct"] > 0]["pnl_pct"].mean(), 2) if pos > 0 else 0.0,
                "avg_loss": round(sub[sub["pnl_pct"] <= 0]["pnl_pct"].mean(), 2) if (len(sub) - pos) > 0 else 0.0,
                "avg_pnl_filled": round(avg, 2),
                "expectancy_per_signal": round(avg, 2),
            })
        out.append(render_table(rows, "Exit reason mix (filled & published)"))

    # 9. Hold-hours buckets — are timeouts wins-that-need-longer-hold?
    if not filled_pub.empty:
        bins = [-1, 24, 72, 120, 168, 999]
        labels = ["0-24h", "24-72h", "72-120h", "120-168h", "168h+"]
        fb = filled_pub.copy()
        fb["bucket"] = pd.cut(fb["hold_hours"], bins=bins, labels=labels)
        rows = [bucket_stats(fb[fb["bucket"] == lab], lab) for lab in labels]
        out.append(render_table(rows, "Hold duration (filled & published)"))

    text = "\n".join(out)
    out_path = REPORT_DIR / f"{args.universe}_diagnostics.md"
    out_path.write_text(text)
    print(text)
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
