#!/usr/bin/env python3
"""
walkforward_regime_overlay.py — Test mt_regime ≠ D as a hard gate on top of
the full optimization's stable winners.

Reads each (universe, window) results CSV from walkforward_full/, extracts the
top-K (combo, thresholds, timeout, tb, top_n) picks by IS-Pareto fold
frequency (≥ MIN_FOLDS folds), then re-simulates each on every fold's OOS
window — once without regime gate (baseline) and once with `regime ≠ D` as a
mandatory pre-entry check.

The regime is computed from each token's own SMA(40) + 20-bar slope, matching
core.filters.compute_regime_sma40. With confirm_bars=1 the confirmation step
is a no-op (immediate switching), which is fully vectorisable.

IMPORTANT: averages here are across **all folds** of the (universe, window)
job, not only the IS-Pareto-winning folds. That makes the numbers strictly
comparable between the two variants and removes the cherry-picking bias of
the summary.md table.

Output:
  research/walkforward_full/regime_overlay.md
  research/walkforward_full/regime_overlay.csv

Usage:
  python3 research/walkforward_regime_overlay.py
  python3 research/walkforward_regime_overlay.py --top-k 10 --min-folds 3
"""

import argparse
import csv
import os
import pickle
import sys
import time
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _PARENT_DIR)

from research.walkforward_optimize_full import (   # noqa: E402
    UNIVERSES, WINDOWS, MIN_HISTORY, DEFAULTS,
    TokenData, Trade,
    load_token_dfs, precompute_token,
    per_symbol_signature,
    build_folds, fmt_combo, fmt_thresholds,
    metrics,
)


CACHE_DIR = "research/.cache/walkforward_full"
RESULTS_DIR = "research/walkforward_full"


# ─── Vectorised regime (SMA40, slope 20, no confirmation) ─────

def vec_regime_sma40(close: pd.Series) -> np.ndarray:
    """Vectorised core.filters.compute_regime_sma40 with confirm_bars=1.

    With confirm_bars=1, _apply_confirmation reduces to a no-op (the regime
    switches immediately on every raw signal change), so we can compute the
    raw regime per bar with no Python loop.
    """
    n = len(close)
    sma = close.rolling(40).mean()
    sma_shifted = sma.shift(20)
    sma_valid = ~np.isnan(sma_shifted.values)
    price_above = (close > sma).values
    sma_rising = (sma > sma_shifted).values

    regime = np.full(n, "T", dtype="<U1")
    regime[price_above & sma_rising & sma_valid] = "U"
    regime[(~price_above) & (~sma_rising) & sma_valid] = "D"
    return regime


def precompute_regime_for_token(df_local: pd.DataFrame,
                                 master: pd.DatetimeIndex) -> np.ndarray:
    """Compute regime per local bar, align to master index.
    Bars without data → 'T' (won't be queried since has_data filters them)."""
    regime_local = vec_regime_sma40(df_local["close"])
    n_master = len(master)
    aligned = np.full(n_master, "T", dtype="<U1")
    master_pos = {ts: i for i, ts in enumerate(master)}
    for li, ts in enumerate(df_local.index):
        mi = master_pos[ts]
        aligned[mi] = regime_local[li]
    return aligned


# ─── Filter check (with optional regime gate) ─────────────────

def filter_passes_regime(
    combo: FrozenSet[str],
    thresholds: Dict[str, float],
    tdata: TokenData,
    mi: int,
    btc: TokenData,
    regime_array: Optional[np.ndarray],
) -> bool:
    if regime_array is not None and regime_array[mi] == "D":
        return False

    if "btc_rsi_floor" in combo:
        v = btc.rsi[mi]
        thr = thresholds.get("btc_rsi_floor", DEFAULTS["btc_rsi_floor"])
        if np.isnan(v) or v < thr:
            return False
    if "token_rsi_momentum" in combo:
        v = tdata.rsi[mi]
        thr = thresholds.get("token_rsi_momentum", DEFAULTS["token_rsi_momentum"])
        if np.isnan(v) or not (v > thr):
            return False
    if "rsi_cap" in combo:
        v = tdata.rsi[mi]
        thr = thresholds.get("rsi_cap", DEFAULTS["rsi_cap"])
        if np.isnan(v) or not (v <= thr):
            return False
    if "token_di_bullish" in combo:
        dp = tdata.di_plus[mi]
        dm = tdata.di_minus[mi]
        if np.isnan(dp) or np.isnan(dm) or not (dp > dm):
            return False
    if "relative_volume" in combo:
        v = tdata.rvol[mi]
        thr = thresholds.get("relative_volume", DEFAULTS["relative_volume"])
        if np.isnan(v) or v < thr:
            return False
    return True


# ─── Multi-position simulator (regime-aware) ─────────────────

def simulate_regime(
    window_start: int,
    window_end: int,
    combo: FrozenSet[str],
    thresholds: Dict[str, float],
    timeout: int,
    tiebreaker: str,
    top_n: int,
    tokens: Dict[str, TokenData],
    btc: TokenData,
    tpsl_cache: Dict[str, Dict[int, Tuple]],
    regime_arrays: Optional[Dict[str, np.ndarray]],
) -> Tuple[List[Trade], float]:
    """Same simulator as walkforward_optimize_full.simulate, but consults
    regime_arrays at signal time. Pass regime_arrays=None for the baseline."""
    trades: List[Trade] = []
    open_trades: List[Trade] = []
    pending_entries: List[Tuple[str, float, float, float]] = []
    exited_today: Set[str] = set()
    cash = 1.0

    for mi in range(window_start, window_end):
        if pending_entries:
            for sym, tp, sl, alloc in pending_entries:
                if any(t.symbol == sym for t in open_trades):
                    cash += alloc
                    continue
                tdata = tokens[sym]
                entry_price = tdata.open[mi]
                if np.isnan(entry_price):
                    cash += alloc
                    continue
                open_trades.append(Trade(
                    symbol=sym,
                    entry_mi=mi,
                    entry_price=float(entry_price),
                    take_profit=float(tp),
                    stop_loss=float(sl),
                    allocation=alloc,
                ))
            pending_entries = []

        exited_today.clear()

        if open_trades:
            still_open: List[Trade] = []
            for t in open_trades:
                tdata = tokens[t.symbol]
                hi = tdata.high[mi]
                lo = tdata.low[mi]
                cl = tdata.close[mi]
                if np.isnan(cl):
                    still_open.append(t)
                    continue
                t.hold_days += 1
                tp_hit = hi >= t.take_profit
                sl_hit = lo <= t.stop_loss
                exit_reason = ""
                exit_price = 0.0
                if tp_hit and sl_hit:
                    exit_reason, exit_price = "SL", t.stop_loss
                elif tp_hit:
                    exit_reason, exit_price = "TP", t.take_profit
                elif sl_hit:
                    exit_reason, exit_price = "SL", t.stop_loss
                elif t.hold_days >= timeout:
                    exit_reason, exit_price = "TIMEOUT", float(cl)
                if exit_reason:
                    t.exit_mi = mi
                    t.exit_price = exit_price
                    t.exit_reason = exit_reason
                    t.pnl_pct = (exit_price - t.entry_price) / t.entry_price * 100.0
                    cash += t.allocation * (1.0 + t.pnl_pct / 100.0)
                    trades.append(t)
                    exited_today.add(t.symbol)
                else:
                    still_open.append(t)
            open_trades = still_open

        if mi >= window_end - 1:
            continue
        slots = top_n - len(open_trades)
        if slots <= 0:
            continue
        open_syms = {t.symbol for t in open_trades}

        candidates: List[Tuple[str, float, float, float, float]] = []
        for sym, tdata in tokens.items():
            if sym in exited_today or sym in open_syms:
                continue
            if not tdata.has_data[mi]:
                continue
            regime_arr = regime_arrays.get(sym) if regime_arrays else None
            if not filter_passes_regime(combo, thresholds, tdata, mi, btc, regime_arr):
                continue
            cache = tpsl_cache.get(sym, {})
            if mi not in cache:
                continue
            tp, sl, _ = cache[mi]
            if tp is None or sl is None:
                continue
            cur = tdata.close[mi]
            if np.isnan(cur) or not (tp > cur > sl):
                continue
            rsi_val = tdata.rsi[mi]
            if np.isnan(rsi_val):
                continue
            gain = tp - cur
            loss = cur - sl
            rr_val = (gain / loss) if loss > 0 else 0.0
            candidates.append((sym, rsi_val, rr_val, tp, sl))

        if not candidates:
            continue

        if tiebreaker == "rsi":
            candidates.sort(key=lambda c: c[1], reverse=True)
        else:
            candidates.sort(key=lambda c: c[2], reverse=True)

        selected = candidates[:slots]
        if not selected:
            continue

        per_alloc = cash / len(selected)
        for sym, _, _, tp, sl in selected:
            pending_entries.append((sym, tp, sl, per_alloc))
            cash -= per_alloc

    if open_trades:
        last_mi = window_end - 1
        for t in open_trades:
            tdata = tokens[t.symbol]
            cl = tdata.close[last_mi]
            if np.isnan(cl):
                local_close = tdata.df_local["close"]
                cl = float(local_close.iloc[-1]) if len(local_close) else t.entry_price
            t.exit_mi = last_mi
            t.exit_price = float(cl)
            t.exit_reason = "OPEN"
            t.pnl_pct = (float(cl) - t.entry_price) / t.entry_price * 100.0
            cash += t.allocation * (1.0 + t.pnl_pct / 100.0)
            trades.append(t)
        open_trades = []

    return trades, cash


# ─── Pick extraction from CSV ─────────────────────────────────

THRESHOLD_KEY_TO_FILTER = {
    "btc": "btc_rsi_floor",
    "rsi": "token_rsi_momentum",
    "cap": "rsi_cap",
    "rvol": "relative_volume",
}

COMBO_TOKEN_TO_FILTER = {
    "btc_rsi": "btc_rsi_floor",
    "rsi_mom": "token_rsi_momentum",
    "rsi_cap": "rsi_cap",
    "di+": "token_di_bullish",
    "rvol": "relative_volume",
}


def parse_thresholds_str(s: str) -> Dict[str, float]:
    if not isinstance(s, str) or not s:
        return {}
    out: Dict[str, float] = {}
    for kv in s.split(";"):
        kv = kv.strip()
        if not kv:
            continue
        k, v = kv.split("=", 1)
        k = k.strip()
        if k in THRESHOLD_KEY_TO_FILTER:
            out[THRESHOLD_KEY_TO_FILTER[k]] = float(v)
    return out


def parse_combo_str(s: str) -> FrozenSet[str]:
    """Parse e.g. 'rvol+di++rsi_mom' → {relative_volume, token_di_bullish, token_rsi_momentum}.
    The 'di+' token contains a literal '+', so we can't naively split on '+'.
    Strategy: greedy longest-prefix match against COMBO_TOKEN_TO_FILTER keys."""
    if not isinstance(s, str) or s == "(none)" or not s:
        return frozenset()
    keys_by_len = sorted(COMBO_TOKEN_TO_FILTER.keys(), key=len, reverse=True)
    out = set()
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "+":
            i += 1
            continue
        matched = False
        for k in keys_by_len:
            if s.startswith(k, i):
                out.add(COMBO_TOKEN_TO_FILTER[k])
                i += len(k)
                matched = True
                break
        if not matched:
            raise ValueError(f"unparsable combo string {s!r} at position {i}")
    return frozenset(out)


def find_top_picks_from_csv(csv_path: str, min_folds: int, top_k: int):
    """Read results CSV, compute IS-Pareto frequency per (combo, thresholds,
    timeout, tb, top_n), return top-k picks with frequency ≥ min_folds.
    """
    df = pd.read_csv(csv_path, usecols=[
        "fold_id", "combo", "thresholds", "timeout", "tiebreaker", "top_n",
        "is_pnl_pct", "is_n_trades", "is_win_rate", "is_hit_rate",
    ])
    df["thresholds"] = df["thresholds"].fillna("")

    pareto_counts: Dict[Tuple, int] = {}
    for fold_id in df["fold_id"].unique():
        fold = df[df["fold_id"] == fold_id]
        fold = fold[fold["is_n_trades"] > 0]
        if fold.empty:
            continue

        pts = fold[["is_pnl_pct", "is_win_rate", "is_hit_rate"]].values
        n = len(pts)
        is_pareto = np.ones(n, dtype=bool)
        for i in range(n):
            if not is_pareto[i]:
                continue
            ge = pts >= pts[i]
            gt = pts > pts[i]
            dominates = np.all(ge, axis=1) & np.any(gt, axis=1)
            dominates[i] = False
            if dominates.any():
                is_pareto[i] = False

        pareto_rows = fold[is_pareto]
        for _, r in pareto_rows.iterrows():
            key = (
                r["combo"],
                r["thresholds"],
                int(r["timeout"]),
                r["tiebreaker"],
                int(r["top_n"]),
            )
            pareto_counts[key] = pareto_counts.get(key, 0) + 1

    items = [(k, c) for k, c in pareto_counts.items() if c >= min_folds]
    items.sort(key=lambda x: -x[1])
    items = items[:top_k]

    expanded = []
    for (combo_str, thr_str, to, tb, tn), freq in items:
        combo = parse_combo_str(combo_str)
        thresholds = parse_thresholds_str(thr_str)
        expanded.append((combo, thresholds, to, tb, tn, freq))
    return expanded


# ─── Per-symbol cache loader ──────────────────────────────────

def load_per_symbol_cache(symbols: List[str],
                          raw_dfs: Dict[str, pd.DataFrame]) -> Dict[str, Dict[int, Tuple]]:
    out = {}
    for sym in symbols:
        if sym not in raw_dfs:
            continue
        path = os.path.join(CACHE_DIR, f"{sym}.pkl")
        if not os.path.exists(path):
            print(f"[cache] missing {sym}.pkl — skip", file=sys.stderr)
            continue
        with open(path, "rb") as f:
            payload = pickle.load(f)
        if payload.get("signature") != per_symbol_signature(raw_dfs[sym]):
            print(f"[cache] signature mismatch for {sym} — skip", file=sys.stderr)
            continue
        out[sym] = payload["tpsl"]
    return out


def build_master_tpsl_cache(tokens: Dict[str, TokenData],
                             local_cache: Dict[str, Dict[int, Tuple]]) -> Dict[str, Dict[int, Tuple]]:
    out: Dict[str, Dict[int, Tuple]] = {}
    for sym, tdata in tokens.items():
        if sym not in local_cache:
            continue
        lc = local_cache[sym]
        mi_cache: Dict[int, Tuple] = {}
        for mi, li in tdata.master_to_local.items():
            if li in lc:
                mi_cache[mi] = lc[li]
        out[sym] = mi_cache
    return out


# ─── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Regime overlay on walkforward winners")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--results-dir", default=RESULTS_DIR)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-folds", type=int, default=3)
    parser.add_argument("--out-md", default=os.path.join(RESULTS_DIR, "regime_overlay.md"))
    parser.add_argument("--out-csv", default=os.path.join(RESULTS_DIR, "regime_overlay.csv"))
    args = parser.parse_args()

    t0 = time.time()
    print("\n" + "=" * 72, file=sys.stderr)
    print("  REGIME OVERLAY (mt_regime ≠ D bolted on top of stable winners)", file=sys.stderr)
    print(f"  Top-{args.top_k} picks per (universe,window) with ≥{args.min_folds} folds",
          file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    all_symbols = sorted({s for u in UNIVERSES.values() for s in u})
    print(f"\n[1/3] Loading {len(all_symbols)} tokens & TPSL caches", file=sys.stderr)
    raw_dfs = load_token_dfs(all_symbols, args.data_dir)
    local_cache = load_per_symbol_cache(all_symbols, raw_dfs)
    print(f"  loaded {len(raw_dfs)} dfs, {len(local_cache)} TPSL caches", file=sys.stderr)

    print(f"\n[2/3] Per-universe overlay simulation", file=sys.stderr)
    output_rows: List[Dict] = []
    md_sections: List[Tuple[str, str, List[Dict]]] = []

    for universe_name, symbols in UNIVERSES.items():
        u_dfs = {s: raw_dfs[s] for s in symbols if s in raw_dfs}
        if not u_dfs:
            continue
        master_set = set()
        for df in u_dfs.values():
            master_set.update(df.index)
        master = pd.DatetimeIndex(sorted(master_set))

        tokens: Dict[str, TokenData] = {}
        for sym, df in u_dfs.items():
            tokens[sym] = precompute_token(sym, df, master)
        btc = tokens["BTC"]

        regime_arrays: Dict[str, np.ndarray] = {}
        for sym, df in u_dfs.items():
            regime_arrays[sym] = precompute_regime_for_token(df, master)
        # Quick regime distribution stats per universe
        all_regime = np.concatenate(list(regime_arrays.values()))
        n_total = len(all_regime)
        n_d = int((all_regime == "D").sum())
        n_u = int((all_regime == "U").sum())
        n_t = int((all_regime == "T").sum())
        print(f"\n  {universe_name}: {len(u_dfs)} tokens, "
              f"regime distribution U={n_u/n_total:.0%} T={n_t/n_total:.0%} D={n_d/n_total:.0%}",
              file=sys.stderr)

        tpsl_master = build_master_tpsl_cache(tokens, local_cache)

        for window_name, (is_days, oos_days) in WINDOWS.items():
            csv_path = os.path.join(args.results_dir, f"{universe_name}_{window_name}", "results.csv")
            if not os.path.exists(csv_path):
                print(f"  [skip] {csv_path} (missing)", file=sys.stderr)
                continue

            print(f"  ── {universe_name}/{window_name} ──", file=sys.stderr)
            t_picks = time.time()
            picks = find_top_picks_from_csv(csv_path, args.min_folds, args.top_k)
            print(f"    pareto compute: {time.time()-t_picks:.1f}s — "
                  f"{len(picks)} picks with ≥{args.min_folds} folds", file=sys.stderr)
            if not picks:
                md_sections.append((universe_name, window_name, []))
                continue

            folds = build_folds(len(master), is_days, oos_days, oos_days)

            section_rows: List[Dict] = []
            t_sim = time.time()
            for combo, thresholds, timeout, tb, top_n, freq in picks:
                base_pnls, base_n, base_tp, base_sl, base_to = [], 0, 0, 0, 0
                reg_pnls, reg_n, reg_tp, reg_sl, reg_to = [], 0, 0, 0, 0
                for fid, is_start, is_end, oos_start, oos_end in folds:
                    bt, bc = simulate_regime(
                        oos_start, oos_end, combo, thresholds, timeout, tb, top_n,
                        tokens, btc, tpsl_master, regime_arrays=None,
                    )
                    bm = metrics(bt, bc)
                    base_pnls.append(bm[0])
                    base_n += bm[1]; base_tp += bm[2]; base_sl += bm[3]; base_to += bm[4]

                    rt, rc = simulate_regime(
                        oos_start, oos_end, combo, thresholds, timeout, tb, top_n,
                        tokens, btc, tpsl_master, regime_arrays=regime_arrays,
                    )
                    rm = metrics(rt, rc)
                    reg_pnls.append(rm[0])
                    reg_n += rm[1]; reg_tp += rm[2]; reg_sl += rm[3]; reg_to += rm[4]

                base_avg_pnl = float(np.mean(base_pnls))
                base_wr = (base_tp / (base_tp + base_sl) * 100.0) if (base_tp + base_sl) > 0 else 0.0
                base_hr = (base_tp / base_n * 100.0) if base_n > 0 else 0.0

                reg_avg_pnl = float(np.mean(reg_pnls))
                reg_wr = (reg_tp / (reg_tp + reg_sl) * 100.0) if (reg_tp + reg_sl) > 0 else 0.0
                reg_hr = (reg_tp / reg_n * 100.0) if reg_n > 0 else 0.0

                row = {
                    "universe": universe_name,
                    "window": window_name,
                    "combo": fmt_combo(combo),
                    "thresholds": fmt_thresholds(thresholds),
                    "timeout": timeout,
                    "tb": tb,
                    "top_n": top_n,
                    "is_pareto_folds": freq,
                    "base_avg_oos_pnl": round(base_avg_pnl, 2),
                    "base_total_n": base_n,
                    "base_total_tp": base_tp,
                    "base_total_sl": base_sl,
                    "base_total_to": base_to,
                    "base_win_rate": round(base_wr, 1),
                    "base_hit_rate": round(base_hr, 1),
                    "reg_avg_oos_pnl": round(reg_avg_pnl, 2),
                    "reg_total_n": reg_n,
                    "reg_total_tp": reg_tp,
                    "reg_total_sl": reg_sl,
                    "reg_total_to": reg_to,
                    "reg_win_rate": round(reg_wr, 1),
                    "reg_hit_rate": round(reg_hr, 1),
                    "delta_pnl": round(reg_avg_pnl - base_avg_pnl, 2),
                    "delta_n": reg_n - base_n,
                    "delta_wr": round(reg_wr - base_wr, 1),
                    "delta_hr": round(reg_hr - base_hr, 1),
                }
                section_rows.append(row)
                output_rows.append(row)
            print(f"    sim: {time.time()-t_sim:.1f}s ({len(picks)} picks × {len(folds)} folds × 2)",
                  file=sys.stderr)

            md_sections.append((universe_name, window_name, section_rows))

    print(f"\n[3/3] Writing outputs", file=sys.stderr)
    if output_rows:
        with open(args.out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(output_rows[0].keys()))
            w.writeheader()
            for r in output_rows:
                w.writerow(r)
        print(f"  → {args.out_csv}", file=sys.stderr)

    with open(args.out_md, "w") as f:
        f.write("# Regime Overlay — `mt_regime ≠ D` on top of walkforward winners\n\n")
        f.write(f"For each (universe, window) job, took the top **{args.top_k}** picks ")
        f.write(f"by IS-Pareto frequency (≥ {args.min_folds} folds), then re-simulated ")
        f.write("each on **every fold's OOS window** — once **without** regime gate ")
        f.write("(baseline) and once **with** regime gate (block trades when token's "
                "SMA40 regime is `D`).\n\n")
        f.write("Notes on metrics:\n\n")
        f.write("- `base_*`: OOS metrics with no regime filter\n")
        f.write("- `reg_*`: same picks, but trades only enter when token regime ≠ D\n")
        f.write("- `Δ` columns: regime-gated minus baseline (positive = regime helped)\n")
        f.write("- Avg PnL is **mean across all OOS folds** (zero-trade folds counted as 0%)\n")
        f.write("- Win rate / hit rate use **summed counts across folds**, not per-fold averages\n")
        f.write("- These averages differ from `summary.md` because they cover **every fold**, ")
        f.write("not just the IS-Pareto-winning ones — they're the honest cross-fold OOS measure.\n\n")

        for universe_name, window_name, rows in md_sections:
            f.write(f"## {universe_name} / {window_name}\n\n")
            if not rows:
                f.write("_(no picks met the threshold)_\n\n")
                continue
            f.write("| Combo | Thresholds | TO | TB | top_n | Folds | "
                    "Base PnL% | Base N | Base Win% | Base Hit% | "
                    "Reg PnL% | Reg N | Reg Win% | Reg Hit% | "
                    "ΔPnL% | ΔN | ΔWin% | ΔHit% |\n")
            f.write("|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
            for r in rows:
                f.write(
                    f"| `{r['combo']}` | {r['thresholds'] or '—'} | {r['timeout']} | {r['tb']} | "
                    f"{r['top_n']} | {r['is_pareto_folds']} | "
                    f"{r['base_avg_oos_pnl']:+.2f} | {r['base_total_n']} | "
                    f"{r['base_win_rate']:.1f} | {r['base_hit_rate']:.1f} | "
                    f"{r['reg_avg_oos_pnl']:+.2f} | {r['reg_total_n']} | "
                    f"{r['reg_win_rate']:.1f} | {r['reg_hit_rate']:.1f} | "
                    f"{r['delta_pnl']:+.2f} | {r['delta_n']:+d} | "
                    f"{r['delta_wr']:+.1f} | {r['delta_hr']:+.1f} |\n"
                )
            f.write("\n")
    print(f"  → {args.out_md}", file=sys.stderr)

    print(f"\nDone in {time.time()-t0:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
