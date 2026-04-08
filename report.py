#!/usr/bin/env python3
"""
report.py — Unified S/R backtest report.
==========================================

Reads `backtest_results.csv` (the raw signal log produced by `backtest_model.py`),
simulates a chosen filter combination over the recorded trades, computes
performance metrics on the resulting equity curve, and outputs a Markdown
report plus a tearsheet PDF.

Inspired by `python_backtester/tearsheet.py` (metrics + chart layout) and built
on the trade-simulation core from the previous `report_final.py`.

Usage
-----
    # Default: simulate the recommended combo for tier "all", R1 target, 7d timeout
    python3 report.py

    # Simulate one specific combo
    python3 report.py --filters btc_rsi di_bull rvol --tier all --target r1 --timeout 7

    # Brute-force search the best combo (1..N filters) for one tier
    python3 report.py --search --tier top_20 --max-filters 4

    # Custom output paths
    python3 report.py --md out.md --pdf out.pdf

Filter names (use these on the CLI)
-----------------------------------
    btc_rsi    BTC RSI(10) >= 50              (col: f_btc_rsi_floor)
    tok_rsi    Token RSI(10) > 60             (col: f_token_rsi_momentum)
    di_bull    DI+ > DI-                      (col: f_di_bullish)
    adx        ADX(14) > 20                   (col: f_adx_trend)
    mt_regime  SMA40 regime != D              (col: f_mt_regime)
    bb_pctb    Bollinger %B < 0.80            (col: f_bollinger)
    rvol       RVOL(20) >= 1.5                (col: f_rvol)
    rsi_cap    RSI(10) <= 80                  (col: f_rsi_cap)
    rr_min     R:R >= 1.2                     (col: f_rr_min)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Filter map  (CLI alias  →  CSV column name)
# ─────────────────────────────────────────────────────────────────────────────

FILTER_MAP = {
    "btc_rsi":   "f_btc_rsi_floor",
    "tok_rsi":   "f_token_rsi_momentum",
    "di_bull":   "f_di_bullish",
    "adx":       "f_adx_trend",
    "mt_regime": "f_mt_regime",
    "bb_pctb":   "f_bollinger",
    "rvol":      "f_rvol",
    "rsi_cap":   "f_rsi_cap",
    "rr_min":    "f_rr_min",
}
ALL_FILTERS = list(FILTER_MAP.keys())

TIER_INCLUSIVE = {  # which tiers each row qualifies for (top_3 ⊂ selected ⊂ top_20 ⊂ all)
    "top_3":    {"top_3"},
    "selected": {"top_3", "selected"},
    "top_20":   {"top_3", "selected", "top_20"},
    "all":      {"top_3", "selected", "top_20", "all"},
}

CLOSE_COLS = {5: "close_d5", 7: "close_d7", 10: "close_d10", 14: "close_d14", 21: "close_d21"}

STARTING_CAPITAL = 10_000.0


# ─────────────────────────────────────────────────────────────────────────────
# Data loading + filtering
# ─────────────────────────────────────────────────────────────────────────────


def load_csv(path: str = "backtest_results.csv") -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["entry_date", "exit_date"])
    df = df.sort_values("entry_date").reset_index(drop=True)
    return df


def slice_tier(df: pd.DataFrame, tier: str) -> pd.DataFrame:
    valid = TIER_INCLUSIVE[tier]
    return df[df["tier"].isin(valid)].copy()


def apply_filters(df: pd.DataFrame, filters: List[str]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for f in filters:
        col = FILTER_MAP.get(f, f)
        if col not in df.columns:
            raise ValueError(f"Unknown filter '{f}' (column {col!r} not found)")
        mask &= df[col].fillna(False).astype(bool)
    return df[mask].copy()


# ─────────────────────────────────────────────────────────────────────────────
# Trade simulation
# ─────────────────────────────────────────────────────────────────────────────


def _trade_pnl_pct(row: pd.Series, target: str, timeout: int) -> Tuple[Optional[float], Optional[int], str]:
    """Resolve a single signal-row into a realised pnl% / hold_days / outcome.

    target  ∈ {"r1", "r2"}
    timeout ∈ {5, 7, 10, 14, 21}  (must match a CLOSE_COLS entry, or 30 = full)
    Outcomes: "TP", "SL", "TIMEOUT", "SKIP"
    """
    e = float(row["entry_price"])
    if e <= 0:
        return None, None, "SKIP"
    sl = float(row["stop_loss"])
    tp = float(row[target])
    if tp <= 0:
        return None, None, "SKIP"

    tp_hit = bool(row[f"{target}_hit"]) and 0 < int(row[f"{target}_days"]) <= timeout
    sl_hit = bool(row["sl_hit"]) and 0 < int(row["sl_days"]) <= timeout

    if tp_hit and sl_hit:
        # Same-window — whichever happened first wins, ties → SL (conservative)
        if int(row[f"{target}_days"]) < int(row["sl_days"]):
            return 100 * (tp - e) / e, int(row[f"{target}_days"]), "TP"
        return 100 * (sl - e) / e, int(row["sl_days"]), "SL"
    if tp_hit:
        return 100 * (tp - e) / e, int(row[f"{target}_days"]), "TP"
    if sl_hit:
        return 100 * (sl - e) / e, int(row["sl_days"]), "SL"

    # Timeout exit at the appropriate close column
    if timeout >= 30:
        c = float(row["exit_price"])
    else:
        col = CLOSE_COLS.get(timeout)
        if col and float(row[col]) > 0:
            c = float(row[col])
        elif int(row["hold_days"]) <= timeout:
            c = float(row["exit_price"])
        else:
            return None, None, "SKIP"
    return 100 * (c - e) / e, min(int(row["hold_days"]), timeout), "TIMEOUT"


@dataclass
class Sim:
    trades: pd.DataFrame      # one row per executed trade
    equity: pd.Series         # daily equity curve (compounded, full capital)
    n_signals: int            # how many CSV rows passed the filter
    filters: List[str]
    tier: str
    target: str
    timeout: int


def simulate(df: pd.DataFrame,
             filters: List[str],
             tier: str = "all",
             target: str = "r1",
             timeout: int = 7,
             single_position: bool = True) -> Sim:
    """Run a filter combo over the signal log → trades + equity curve.

    Single-position compounding:
        At any time only one trade is open. Signals that fire while a trade
        is open are skipped. Equity is compounded as `e *= (1 + pnl/100)`.
    """
    sub = apply_filters(slice_tier(df, tier), filters)
    n_signals = len(sub)
    if n_signals == 0:
        return Sim(trades=pd.DataFrame(), equity=pd.Series(dtype=float),
                   n_signals=0, filters=filters, tier=tier, target=target, timeout=timeout)

    # Walk signals in chronological order, enforce single-position lock
    trades = []
    busy_until: Optional[pd.Timestamp] = None
    for _, row in sub.iterrows():
        ts = row["entry_date"]
        if single_position and busy_until is not None and ts < busy_until:
            continue
        pnl, hold, outcome = _trade_pnl_pct(row, target, timeout)
        if pnl is None:
            continue
        exit_ts = ts + pd.Timedelta(days=int(hold or 0))
        trades.append({
            "entry_date": ts,
            "exit_date": exit_ts,
            "symbol": row["symbol"],
            "tier": row["tier"],
            "raw_rr": row.get("raw_rr", np.nan),
            "pnl_pct": pnl,
            "hold_days": hold,
            "outcome": outcome,
        })
        busy_until = exit_ts

    if not trades:
        return Sim(trades=pd.DataFrame(), equity=pd.Series(dtype=float),
                   n_signals=n_signals, filters=filters, tier=tier, target=target, timeout=timeout)

    tdf = pd.DataFrame(trades)

    # Build daily equity curve, compounding on each trade exit
    equity = STARTING_CAPITAL
    points = [(df["entry_date"].min(), equity)]
    for _, t in tdf.iterrows():
        equity *= 1 + t["pnl_pct"] / 100
        points.append((t["exit_date"], equity))
    equity_curve = (
        pd.Series(dict(points))
        .sort_index()
        .resample("1D").ffill()
    )

    return Sim(trades=tdf, equity=equity_curve, n_signals=n_signals,
               filters=filters, tier=tier, target=target, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics  (style mirrors python_backtester/backtester.py:compute_metrics)
# ─────────────────────────────────────────────────────────────────────────────


def compute_metrics(sim: Sim, risk_free_annual: float = 0.04) -> dict:
    t = sim.trades
    eq = sim.equity
    if len(t) == 0 or len(eq) == 0:
        return {"n_trades": 0, "n_signals": sim.n_signals}

    daily_ret = eq.pct_change().dropna()
    n_days = len(eq)
    n_years = n_days / 365.25 if n_days > 0 else 0

    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    ann_return = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0
    ann_vol = daily_ret.std() * np.sqrt(365.25)

    rf_daily = (1 + risk_free_annual) ** (1 / 365.25) - 1
    excess = daily_ret - rf_daily
    sharpe = (excess.mean() / daily_ret.std() * np.sqrt(365.25)) if daily_ret.std() > 0 else 0
    downside = daily_ret[daily_ret < rf_daily]
    sortino = (excess.mean() / downside.std() * np.sqrt(365.25)) if len(downside) > 1 and downside.std() > 0 else 0

    rolling_max = eq.cummax()
    dd = (eq - rolling_max) / rolling_max
    max_dd = dd.min()
    calmar = ann_return / abs(max_dd) if max_dd != 0 else 0

    wins = t[t["pnl_pct"] > 0]
    losses = t[t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(t)
    tp_count = int((t["outcome"] == "TP").sum())
    hit_rate = tp_count / len(t)
    profit_factor = (
        wins["pnl_pct"].sum() / abs(losses["pnl_pct"].sum())
        if len(losses) > 0 and losses["pnl_pct"].sum() != 0 else float("inf")
    )
    expectancy = t["pnl_pct"].mean()
    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0

    return {
        "n_signals": sim.n_signals,
        "n_trades": len(t),
        "n_tp": tp_count,
        "n_sl": int((t["outcome"] == "SL").sum()),
        "n_timeout": int((t["outcome"] == "TIMEOUT").sum()),
        "win_rate_pct": win_rate * 100,
        "hit_rate_pct": hit_rate * 100,           # TP-only rate
        "expectancy_pct": expectancy,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "avg_hold_days": t["hold_days"].mean(),
        "start_date": eq.index[0].strftime("%Y-%m-%d"),
        "end_date": eq.index[-1].strftime("%Y-%m-%d"),
        "n_days": n_days,
        "starting_capital": STARTING_CAPITAL,
        "final_capital": eq.iloc[-1],
        "total_return_pct": total_return * 100,
        "ann_return_pct": ann_return * 100,
        "ann_vol_pct": ann_vol * 100,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_drawdown_pct": max_dd * 100,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Markdown output
# ─────────────────────────────────────────────────────────────────────────────


def format_md(sim: Sim, m: dict) -> str:
    if m.get("n_trades", 0) == 0:
        return (f"# S/R Backtest Report\n\n"
                f"**Filters:** {' + '.join(sim.filters) or 'NONE'}  |  **Tier:** {sim.tier}  "
                f"|  **Target:** {sim.target.upper()}  |  **Timeout:** {sim.timeout}d\n\n"
                f"No trades. Signals after filtering: {m.get('n_signals', 0)}\n")

    lines = []
    lines.append("# S/R Backtest Report")
    lines.append("")
    lines.append(f"**Filters:** {' + '.join(sim.filters) or 'NONE'}  |  "
                 f"**Tier:** {sim.tier}  |  **Target:** {sim.target.upper()}  |  "
                 f"**Timeout:** {sim.timeout}d")
    lines.append("")
    lines.append(f"**Period:** {m['start_date']} → {m['end_date']}  ({m['n_days']} days)")
    lines.append("")
    lines.append("## Performance")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Starting capital | ${m['starting_capital']:,.0f} |")
    lines.append(f"| Final capital    | ${m['final_capital']:,.2f} |")
    lines.append(f"| Total return     | {m['total_return_pct']:+.2f}% |")
    lines.append(f"| Annualised ret.  | {m['ann_return_pct']:+.2f}% |")
    lines.append(f"| Annualised vol.  | {m['ann_vol_pct']:.2f}% |")
    lines.append(f"| Sharpe           | {m['sharpe']:.2f} |")
    lines.append(f"| Sortino          | {m['sortino']:.2f} |")
    lines.append(f"| Calmar           | {m['calmar']:.2f} |")
    lines.append(f"| Max drawdown     | {m['max_drawdown_pct']:.2f}% |")
    lines.append("")
    lines.append("## Trades")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Signals (pre-lock) | {m['n_signals']} |")
    lines.append(f"| Trades taken       | {m['n_trades']} |")
    lines.append(f"| TP hit             | {m['n_tp']}  ({m['hit_rate_pct']:.1f}%) |")
    lines.append(f"| SL hit             | {m['n_sl']} |")
    lines.append(f"| Timeout            | {m['n_timeout']} |")
    lines.append(f"| Positive PnL rate  | {m['win_rate_pct']:.1f}% |")
    lines.append(f"| Avg win            | {m['avg_win_pct']:+.2f}% |")
    lines.append(f"| Avg loss           | {m['avg_loss_pct']:+.2f}% |")
    lines.append(f"| Expectancy         | {m['expectancy_pct']:+.2f}% per trade |")
    lines.append(f"| Profit factor      | {m['profit_factor']:.2f} |")
    lines.append(f"| Avg hold           | {m['avg_hold_days']:.1f} days |")
    lines.append("")

    # Per-symbol breakdown (top 15 by trade count)
    lines.append("## Per-Symbol")
    lines.append("")
    lines.append("| Symbol | Trades | TP | SL | TO | Win% | Avg PnL% |")
    lines.append("|---|---|---|---|---|---|---|")
    g = sim.trades.groupby("symbol")
    rows = []
    for sym, sg in g:
        rows.append((sym, len(sg),
                     int((sg["outcome"] == "TP").sum()),
                     int((sg["outcome"] == "SL").sum()),
                     int((sg["outcome"] == "TIMEOUT").sum()),
                     (sg["pnl_pct"] > 0).mean() * 100,
                     sg["pnl_pct"].mean()))
    rows.sort(key=lambda r: -r[1])
    for sym, n, tp, sl, to, wr, ap in rows[:15]:
        lines.append(f"| {sym} | {n} | {tp} | {sl} | {to} | {wr:.1f}% | {ap:+.2f}% |")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Tearsheet PDF  (matplotlib only — no reportlab dep)
# ─────────────────────────────────────────────────────────────────────────────


def generate_tearsheet(sim: Sim, m: dict, save_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec

    DARK_BG = "#0E1117"; PANEL = "#1A1F2E"
    TEXT = "#E5E7EB";   DIM = "#9CA3AF"
    ACCENT = "#3B82F6"; GREEN = "#10B981"; RED = "#EF4444"; GRID = "#374151"

    plt.rcParams.update({
        "figure.facecolor": DARK_BG, "axes.facecolor": PANEL,
        "axes.edgecolor": GRID, "axes.labelcolor": TEXT,
        "xtick.color": TEXT, "ytick.color": TEXT,
        "text.color": TEXT, "axes.titlecolor": TEXT,
        "axes.titleweight": "bold", "font.size": 9,
        "savefig.facecolor": DARK_BG,
    })

    fig = plt.figure(figsize=(14, 16))
    title = (f"S/R Backtest — {' + '.join(sim.filters) or 'NO FILTER'}  "
             f"[{sim.tier} / {sim.target.upper()} / {sim.timeout}d]")
    fig.suptitle(title, fontsize=14, color=TEXT, y=0.985)

    gs = gridspec.GridSpec(5, 2, figure=fig,
                           hspace=0.55, wspace=0.25,
                           left=0.07, right=0.97, top=0.95, bottom=0.04)

    eq = sim.equity

    # 1) Equity curve
    ax1 = fig.add_subplot(gs[0, :])
    norm = eq / eq.iloc[0]
    ax1.plot(norm.index, norm.values, color=ACCENT, linewidth=1.5)
    ax1.axhline(1.0, color=GRID, linewidth=0.8, linestyle=":")
    ax1.set_title("Equity (normalised)")
    ax1.grid(True, alpha=0.3)

    # 2) Drawdown
    ax2 = fig.add_subplot(gs[1, :])
    dd = (eq - eq.cummax()) / eq.cummax() * 100
    ax2.fill_between(dd.index, dd.values, 0, color=RED, alpha=0.4)
    ax2.plot(dd.index, dd.values, color=RED, linewidth=0.8)
    ax2.set_title("Drawdown (%)")
    ax2.axhline(0, color=GRID, linewidth=0.8)
    ax2.grid(True, alpha=0.3)

    # 3) Per-trade PnL bars
    ax3 = fig.add_subplot(gs[2, 0])
    pnls = sim.trades["pnl_pct"].values
    colors = [GREEN if p > 0 else RED for p in pnls]
    ax3.bar(range(len(pnls)), pnls, color=colors, width=0.8)
    ax3.set_title("Per-trade PnL %")
    ax3.axhline(0, color=GRID, linewidth=0.8)
    ax3.grid(True, alpha=0.3)

    # 4) Monthly returns heatmap (compounded daily → monthly)
    ax4 = fig.add_subplot(gs[2, 1])
    monthly = eq.resample("ME").last().pct_change().dropna() * 100
    if len(monthly) > 0:
        mdf = monthly.to_frame("ret")
        mdf["year"] = mdf.index.year
        mdf["month"] = mdf.index.month
        pivot = mdf.pivot(index="year", columns="month", values="ret")
        im = ax4.imshow(pivot.values, cmap="RdYlGn", aspect="auto",
                        vmin=-max(abs(pivot.values.min()), abs(pivot.values.max()), 1),
                        vmax= max(abs(pivot.values.min()), abs(pivot.values.max()), 1))
        ax4.set_xticks(range(len(pivot.columns)))
        ax4.set_xticklabels([f"{c:02d}" for c in pivot.columns])
        ax4.set_yticks(range(len(pivot.index)))
        ax4.set_yticklabels(pivot.index)
        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                v = pivot.values[i, j]
                if not np.isnan(v):
                    ax4.text(j, i, f"{v:+.0f}", ha="center", va="center",
                             color="white" if abs(v) > 5 else DIM, fontsize=7)
        plt.colorbar(im, ax=ax4, fraction=0.046)
    ax4.set_title("Monthly returns %")

    # 5) Metrics table
    ax5 = fig.add_subplot(gs[3, 0])
    ax5.axis("off")
    rows = [
        ("Period",          f"{m['start_date']} → {m['end_date']}"),
        ("Days",            f"{m['n_days']}"),
        ("Trades",          f"{m['n_trades']}  ({m['n_signals']} signals)"),
        ("Final capital",   f"${m['final_capital']:,.0f}"),
        ("Total return",    f"{m['total_return_pct']:+.1f}%"),
        ("Ann. return",     f"{m['ann_return_pct']:+.1f}%"),
        ("Ann. vol",        f"{m['ann_vol_pct']:.1f}%"),
        ("Sharpe",          f"{m['sharpe']:.2f}"),
        ("Sortino",         f"{m['sortino']:.2f}"),
        ("Calmar",          f"{m['calmar']:.2f}"),
        ("Max DD",          f"{m['max_drawdown_pct']:.1f}%"),
    ]
    tbl = ax5.table(cellText=rows, loc="center", cellLoc="left", colWidths=[0.5, 0.5])
    tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1, 1.4)
    for cell in tbl.get_celld().values():
        cell.set_facecolor(PANEL); cell.set_edgecolor(GRID); cell.set_text_props(color=TEXT)
    ax5.set_title("Performance metrics", pad=8)

    # 6) Trade-quality table
    ax6 = fig.add_subplot(gs[3, 1])
    ax6.axis("off")
    rows = [
        ("Win rate (TP)",    f"{m['hit_rate_pct']:.1f}%"),
        ("Positive PnL",     f"{m['win_rate_pct']:.1f}%"),
        ("TP / SL / TO",     f"{m['n_tp']} / {m['n_sl']} / {m['n_timeout']}"),
        ("Avg win",          f"{m['avg_win_pct']:+.2f}%"),
        ("Avg loss",         f"{m['avg_loss_pct']:+.2f}%"),
        ("Expectancy",       f"{m['expectancy_pct']:+.2f}%"),
        ("Profit factor",    f"{m['profit_factor']:.2f}"),
        ("Avg hold",         f"{m['avg_hold_days']:.1f} d"),
    ]
    tbl = ax6.table(cellText=rows, loc="center", cellLoc="left", colWidths=[0.5, 0.5])
    tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1, 1.4)
    for cell in tbl.get_celld().values():
        cell.set_facecolor(PANEL); cell.set_edgecolor(GRID); cell.set_text_props(color=TEXT)
    ax6.set_title("Trade quality", pad=8)

    # 7) Top symbols by trade count
    ax7 = fig.add_subplot(gs[4, :])
    ax7.axis("off")
    g = sim.trades.groupby("symbol").agg(
        n=("pnl_pct", "size"),
        tp=("outcome", lambda s: int((s == "TP").sum())),
        sl=("outcome", lambda s: int((s == "SL").sum())),
        to=("outcome", lambda s: int((s == "TIMEOUT").sum())),
        avg=("pnl_pct", "mean"),
    ).sort_values("n", ascending=False).head(12)
    rows = [["Symbol", "N", "TP", "SL", "TO", "Avg %"]]
    for sym, r in g.iterrows():
        rows.append([sym, str(int(r["n"])), str(int(r["tp"])),
                     str(int(r["sl"])), str(int(r["to"])), f"{r['avg']:+.2f}"])
    tbl = ax7.table(cellText=rows[1:], colLabels=rows[0],
                    loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(9); tbl.scale(1, 1.3)
    for cell in tbl.get_celld().values():
        cell.set_facecolor(PANEL); cell.set_edgecolor(GRID); cell.set_text_props(color=TEXT)
    ax7.set_title("Per-symbol breakdown (top 12 by trade count)", pad=8)

    fig.savefig(save_path, dpi=150, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Best-combo search
# ─────────────────────────────────────────────────────────────────────────────


def search_best(df: pd.DataFrame,
                tier: str,
                target: str,
                timeout: int,
                max_filters: int = 4,
                min_trades: int = 20,
                rank_by: str = "total_return_pct") -> List[Tuple[List[str], dict]]:
    """Brute-force every filter combo of size 1..max_filters and rank them."""
    results = []
    for r in range(1, max_filters + 1):
        for combo in combinations(ALL_FILTERS, r):
            sim = simulate(df, list(combo), tier=tier, target=target, timeout=timeout)
            if sim.trades is None or len(sim.trades) < min_trades:
                continue
            m = compute_metrics(sim)
            if m.get("n_trades", 0) < min_trades:
                continue
            results.append((list(combo), m))
    results.sort(key=lambda x: x[1].get(rank_by, -np.inf), reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default="backtest_results.csv", help="Signal log CSV (default: backtest_results.csv)")
    p.add_argument("--filters", nargs="*", default=["btc_rsi", "di_bull", "bb_pctb"],
                   help="Filter aliases to apply (see FILTER_MAP). Default: btc_rsi di_bull bb_pctb")
    p.add_argument("--tier", default="all", choices=list(TIER_INCLUSIVE.keys()))
    p.add_argument("--target", default="r1", choices=["r1", "r2"])
    p.add_argument("--timeout", type=int, default=7)
    p.add_argument("--md", default="sr_backtest_report.md", help="Markdown output path")
    p.add_argument("--pdf", default="sr_backtest_report.pdf", help="Tearsheet PDF output path")
    p.add_argument("--no-pdf", action="store_true", help="Skip PDF generation")
    p.add_argument("--search", action="store_true", help="Brute-force the best combo and report it")
    p.add_argument("--max-filters", type=int, default=4, help="Max combo size for --search")
    p.add_argument("--min-trades", type=int, default=20, help="Min trades to qualify in --search")
    p.add_argument("--rank-by", default="total_return_pct",
                   help="Metric used to rank --search results")
    p.add_argument("--top", type=int, default=10, help="How many --search results to print")
    args = p.parse_args()

    df = load_csv(args.csv)
    print(f"Loaded {len(df)} signals from {args.csv} "
          f"({df['entry_date'].min().date()} → {df['entry_date'].max().date()})", file=sys.stderr)

    if args.search:
        ranked = search_best(df, args.tier, args.target, args.timeout,
                             max_filters=args.max_filters, min_trades=args.min_trades,
                             rank_by=args.rank_by)
        print(f"\n  Top {args.top} combos by {args.rank_by}  "
              f"(tier={args.tier}, target={args.target.upper()}, timeout={args.timeout}d)")
        print(f"  {'#':>3} {'COMBO':<45} {'N':>5} {'WR%':>6} {'PF':>6} {'TR%':>9} {'DD%':>7}")
        print(f"  {'─' * 90}")
        for i, (combo, m) in enumerate(ranked[:args.top], 1):
            print(f"  {i:>3} {' + '.join(combo):<45} "
                  f"{m['n_trades']:>5} {m['win_rate_pct']:>5.1f}% "
                  f"{m['profit_factor']:>5.2f} "
                  f"{m['total_return_pct']:>+8.1f}% "
                  f"{m['max_drawdown_pct']:>6.1f}%")
        if not ranked:
            print("  (no combo met --min-trades threshold)")
            return
        # Render the winner
        winner_combo, _ = ranked[0]
        print(f"\nRendering #1 combo: {' + '.join(winner_combo)}", file=sys.stderr)
        sim = simulate(df, winner_combo, tier=args.tier, target=args.target, timeout=args.timeout)
    else:
        sim = simulate(df, args.filters, tier=args.tier, target=args.target, timeout=args.timeout)

    m = compute_metrics(sim)
    md = format_md(sim, m)
    Path(args.md).write_text(md)
    print(f"\n{md}\n")
    print(f"Markdown written: {args.md}", file=sys.stderr)

    if not args.no_pdf and m.get("n_trades", 0) > 0:
        try:
            generate_tearsheet(sim, m, args.pdf)
            print(f"Tearsheet written: {args.pdf}", file=sys.stderr)
        except Exception as e:
            print(f"PDF generation failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
