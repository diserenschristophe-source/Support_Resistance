#!/usr/bin/env python3
"""
walkforward_optimize_full.py — Full walk-forward optimization (Option 1 / A+B+C+D).
====================================================================================

Spec (per user, 2026-04-09):
  A. Threshold sweep on the 4 sweepable filters (3-value coarse grid each)
  B. Multiple walk-forward windows: 14/14, 30/30, 60/60 (sliding, step = OOS)
  C. Top-N variants: {1, 3} positions concurrent, equal-cash split
  D. Multiple universes: 17 user tokens + all-51 standard universe
                         (top-3 dropped — strict subset of 17)

Filters (5, no hard gates):
  - btc_rsi_floor       — sweep {45, 50, 55}
  - token_rsi_momentum  — sweep {55, 60, 65}
  - rsi_cap             — sweep {75, 80, 85}
  - token_di_bullish    — on/off only (no threshold)
  - relative_volume     — sweep {1.25, 1.5, 1.75}

Per (combo, threshold) variants: 512 total.

Strategy semantics:
  - Multi-position: top-N concurrent, equal cash split per entry
  - Compounding: cash on exit returns to pool, next entry sized off current cash
  - Entry: top-N by tiebreaker (rsi or rr) among tokens passing filter combo
  - Exit: TP / SL / Timeout / OPEN (force-close at window end)
  - Same-day TP+SL → SL (conservative)
  - Same-day re-entry per symbol blocked

Definitions:
  - pnl_pct  = (final_cash / starting_cash − 1) × 100         (portfolio return)
  - win_rate = TP_count / (TP_count + SL_count)               (closed only)
  - hit_rate = TP_count / total_trades_count                  (TP hit ratio incl. timeouts)

Outputs (under research/walkforward_full/):
  u17_w14/results.csv ... u51_w60/results.csv      (6 per-job CSVs)
  summary.md                                       (aggregate Pareto)
  trades.csv                                       (top-5 picks per job)

Cache:
  research/.cache/walkforward_full/{SYM}.pkl       (per-symbol, reusable across runs/universes)

Usage:
  python3 research/walkforward_optimize_full.py
  python3 research/walkforward_optimize_full.py --rebuild-cache         # force tpsl recompute
  python3 research/walkforward_optimize_full.py --limit-folds 2 --limit-universes u17 --limit-windows w30  # smoke
"""

import argparse
import csv
import itertools
import os
import pickle
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _PARENT_DIR)

from core.fetcher import load_from_cache         # noqa: E402
from core.sr_analysis import analyze_token       # noqa: E402
from core.tpsl import compute_tp_sl              # noqa: E402


# ─── Universe definitions ─────────────────────────────────────

UNIVERSE_17 = [
    "BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE", "AVAX",
    "TAO",   # = BITTENSOR
    "HYPE",  # = HYPERLIQUID
    "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI",
]

UNIVERSE_51 = [
    "BTC", "ETH", "SOL", "BNB", "DOGE", "LINK", "NEAR", "SUI", "TAO",
    "ADA", "AVAX", "HBAR", "LTC", "PAXG", "TON", "TRX", "XLM", "XRP",
    "HYPE", "SHIB", "MNT", "UNI", "DOT", "SKY", "ASTER", "AAVE", "PEPE",
    "ONDO", "ICP", "POL", "KAS", "RENDER", "WLD", "QNT", "ATOM", "FIL",
    "ARB", "FET", "APT", "TRUMP", "ALGO", "INJ", "ENA", "VET", "BONK",
    "SEI", "STX", "JUP", "FLOKI", "OP", "BORG",
]

UNIVERSES: Dict[str, List[str]] = {
    "u17": UNIVERSE_17,
    "u51": UNIVERSE_51,
}


# ─── Filter & sweep definitions ───────────────────────────────

FILTER_NAMES = (
    "btc_rsi_floor",
    "token_rsi_momentum",
    "rsi_cap",
    "token_di_bullish",
    "relative_volume",
)

# 3-value coarse grids per spec; only the 4 "sweepable" filters have a knob
THRESHOLD_GRIDS: Dict[str, List[float]] = {
    "btc_rsi_floor":      [45.0, 50.0, 55.0],
    "token_rsi_momentum": [55.0, 60.0, 65.0],
    "rsi_cap":            [75.0, 80.0, 85.0],
    "relative_volume":    [1.25, 1.5, 1.75],
}
SWEEPABLE = set(THRESHOLD_GRIDS.keys())

# Defaults used when a filter is OFF (irrelevant) or not yet swept
DEFAULTS = {
    "btc_rsi_floor":      50.0,
    "token_rsi_momentum": 60.0,
    "rsi_cap":            80.0,
    "relative_volume":    1.5,
}

BTC_RSI_PERIOD = 10
TOK_RSI_PERIOD = 10
ADX_PERIOD = 14
RVOL_PERIOD = 20

TIMEOUTS = [3, 5, 7, 10, 14, 21]
TIEBREAKERS = ["rsi", "rr"]
TOP_N_VALUES = [1, 3]
WINDOWS: Dict[str, Tuple[int, int]] = {
    "w14": (14, 14),
    "w30": (30, 30),
    "w60": (60, 60),
}

MIN_HISTORY = 200


# ─── Data structures ──────────────────────────────────────────

@dataclass
class TokenData:
    sym: str
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    rsi: np.ndarray
    di_plus: np.ndarray
    di_minus: np.ndarray
    rvol: np.ndarray
    has_data: np.ndarray              # bool array, True where the token has a real bar
    df_local: pd.DataFrame
    master_to_local: Dict[int, int]


@dataclass
class Trade:
    symbol: str
    entry_mi: int
    entry_price: float
    take_profit: float
    stop_loss: float
    allocation: float                 # cash deployed at entry
    exit_mi: int = -1
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    hold_days: int = 0


@dataclass
class FoldRecord:
    universe: str
    window: str
    fold_id: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int
    combo: FrozenSet[str]
    thresholds: Tuple[Tuple[str, float], ...]   # sorted tuple for hashability
    timeout: int
    tiebreaker: str
    top_n: int
    is_pnl_pct: float
    is_n_trades: int
    is_n_tp: int
    is_n_sl: int
    is_n_to: int
    is_win_rate: float
    is_hit_rate: float
    oos_pnl_pct: float
    oos_n_trades: int
    oos_n_tp: int
    oos_n_sl: int
    oos_n_to: int
    oos_win_rate: float
    oos_hit_rate: float


# ─── Loading ──────────────────────────────────────────────────

def load_token_dfs(symbols: List[str], data_dir: str) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_from_cache(sym, data_dir)
        if df is None or len(df) == 0:
            print(f"[load] WARN: no cache for {sym}", file=sys.stderr)
            continue
        out[sym] = df
    return out


# ─── Vectorized indicators (same logic as core.filters) ───────

def vec_rsi(close: pd.Series, period: int) -> np.ndarray:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(loss != 0, 100.0)
    return rsi.values


def vec_rvol(volume: pd.Series, period: int) -> np.ndarray:
    return (volume / volume.rolling(period).mean()).values


def vec_adx_di(df: pd.DataFrame, period: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)
    if n < period + 1:
        z = np.zeros(n)
        return z, z, z

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
        plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
        minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

    atr = np.zeros(n)
    smooth_plus = np.zeros(n)
    smooth_minus = np.zeros(n)
    atr[period] = np.mean(tr[1:period + 1])
    smooth_plus[period] = np.mean(plus_dm[1:period + 1])
    smooth_minus[period] = np.mean(minus_dm[1:period + 1])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
        smooth_plus[i] = (smooth_plus[i - 1] * (period - 1) + plus_dm[i]) / period
        smooth_minus[i] = (smooth_minus[i - 1] * (period - 1) + minus_dm[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = np.where(atr > 0, 100.0 * smooth_plus / atr, 0.0)
        di_minus = np.where(atr > 0, 100.0 * smooth_minus / atr, 0.0)
        di_sum = di_plus + di_minus
        dx = np.where(di_sum > 0, 100.0 * np.abs(di_plus - di_minus) / di_sum, 0.0)

    adx = np.zeros(n)
    if n > 2 * period:
        adx[2 * period] = np.mean(dx[period + 1:2 * period + 1])
        for i in range(2 * period + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx, di_plus, di_minus


def precompute_token(sym: str, df_local: pd.DataFrame,
                     master: pd.DatetimeIndex) -> TokenData:
    aligned = df_local.reindex(master)
    has = ~aligned["close"].isna().values

    rsi_local = vec_rsi(df_local["close"], TOK_RSI_PERIOD)
    rvol_local = vec_rvol(df_local["volume"], RVOL_PERIOD)
    _, dip_local, dim_local = vec_adx_di(df_local, ADX_PERIOD)

    n_master = len(master)
    rsi = np.full(n_master, np.nan)
    rvol = np.full(n_master, np.nan)
    di_plus = np.full(n_master, np.nan)
    di_minus = np.full(n_master, np.nan)

    master_pos = {ts: i for i, ts in enumerate(master)}
    master_to_local: Dict[int, int] = {}
    for li, ts in enumerate(df_local.index):
        mi = master_pos[ts]
        master_to_local[mi] = li
        rsi[mi] = rsi_local[li]
        rvol[mi] = rvol_local[li]
        di_plus[mi] = dip_local[li]
        di_minus[mi] = dim_local[li]

    return TokenData(
        sym=sym,
        open=aligned["open"].values,
        high=aligned["high"].values,
        low=aligned["low"].values,
        close=aligned["close"].values,
        volume=aligned["volume"].values,
        rsi=rsi,
        di_plus=di_plus,
        di_minus=di_minus,
        rvol=rvol,
        has_data=has,
        df_local=df_local,
        master_to_local=master_to_local,
    )


# ─── TPSL precompute (per-symbol cache) ───────────────────────

def per_symbol_signature(df: pd.DataFrame) -> Tuple:
    return (len(df), str(df.index[0]), str(df.index[-1]))


def precompute_tpsl_for_token_local(df: pd.DataFrame, sym: str) -> Dict[int, Tuple]:
    """Compute (tp, sl, rr) per LOCAL row index for each row with li >= MIN_HISTORY.
    Indexed by LOCAL li so it's independent of any universe's master index."""
    out: Dict[int, Tuple] = {}
    n = len(df)
    for li in range(MIN_HISTORY, n):
        history = df.iloc[: li + 1]
        try:
            analysis = analyze_token(sym, history)
            result = compute_tp_sl(analysis)
            if result is None:
                out[li] = (None, None, None)
                continue
            tp = result.get("take_profit")
            sl = result.get("stop_loss")
            rr = result.get("raw_rr")
            out[li] = (tp, sl, rr)
        except Exception:
            out[li] = (None, None, None)
    return out


def load_or_build_per_symbol_cache(symbol: str, df: pd.DataFrame,
                                   cache_dir: str, rebuild: bool) -> Dict[int, Tuple]:
    path = os.path.join(cache_dir, f"{symbol}.pkl")
    sig = per_symbol_signature(df)
    if not rebuild and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            if payload.get("signature") == sig:
                return payload["tpsl"]
        except Exception:
            pass

    tpsl = precompute_tpsl_for_token_local(df, symbol)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({"signature": sig, "tpsl": tpsl}, f)
    return tpsl


def build_tpsl_cache_for_universe(
    tokens: Dict[str, TokenData],
    raw_dfs: Dict[str, pd.DataFrame],
    per_symbol_cache: Dict[str, Dict[int, Tuple]],
) -> Dict[str, Dict[int, Tuple]]:
    """Translate the per-LOCAL-index cache to per-MASTER-index for the active universe."""
    out: Dict[str, Dict[int, Tuple]] = {}
    for sym, tdata in tokens.items():
        local_cache = per_symbol_cache[sym]
        mi_cache: Dict[int, Tuple] = {}
        for mi, li in tdata.master_to_local.items():
            if li in local_cache:
                mi_cache[mi] = local_cache[li]
        out[sym] = mi_cache
    return out


# ─── Filter check (with thresholds) ───────────────────────────

def filter_passes(combo: FrozenSet[str], thresholds: Dict[str, float],
                  tdata: TokenData, mi: int, btc: TokenData) -> bool:
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


# ─── Multi-position simulator ─────────────────────────────────

def simulate(
    window_start: int,
    window_end: int,                 # exclusive
    combo: FrozenSet[str],
    thresholds: Dict[str, float],
    timeout: int,
    tiebreaker: str,
    top_n: int,
    tokens: Dict[str, TokenData],
    btc: TokenData,
    tpsl_cache: Dict[str, Dict[int, Tuple]],
) -> Tuple[List[Trade], float]:
    """Walk window, manage up to `top_n` concurrent positions, return (trades, final_cash)."""
    trades: List[Trade] = []
    open_trades: List[Trade] = []
    pending_entries: List[Tuple[str, float, float, float]] = []  # (sym, tp, sl, alloc)
    exited_today: Set[str] = set()
    cash = 1.0

    for mi in range(window_start, window_end):
        # ── Execute pending entries from yesterday ──
        if pending_entries:
            for sym, tp, sl, alloc in pending_entries:
                if any(t.symbol == sym for t in open_trades):
                    cash += alloc       # return reserved cash
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

        # ── Check exits on all open trades ──
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
                    exit_reason, exit_price = "SL", t.stop_loss     # conservative
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

        # ── Generate new signals ──
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
            if not filter_passes(combo, thresholds, tdata, mi, btc):
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
        else:  # rr
            candidates.sort(key=lambda c: c[2], reverse=True)

        selected = candidates[:slots]
        if not selected:
            continue

        per_alloc = cash / len(selected)        # split remaining cash equally across new entries
        for sym, _, _, tp, sl in selected:
            pending_entries.append((sym, tp, sl, per_alloc))
            cash -= per_alloc

    # ── Force-close any leftover open trades at window end ──
    if open_trades:
        last_mi = window_end - 1
        for t in open_trades:
            tdata = tokens[t.symbol]
            cl = tdata.close[last_mi]
            if np.isnan(cl):
                # Use last known close from token's local frame
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


# ─── Metrics + Pareto ─────────────────────────────────────────

def metrics(trades: List[Trade], final_cash: float) -> Tuple[float, int, int, int, int, float, float]:
    n = len(trades)
    if n == 0:
        return 0.0, 0, 0, 0, 0, 0.0, 0.0
    n_tp = sum(1 for t in trades if t.exit_reason == "TP")
    n_sl = sum(1 for t in trades if t.exit_reason == "SL")
    n_to = sum(1 for t in trades if t.exit_reason in ("TIMEOUT", "OPEN"))
    pnl_pct = (final_cash - 1.0) * 100.0
    win_rate = (n_tp / (n_tp + n_sl) * 100.0) if (n_tp + n_sl) > 0 else 0.0
    hit_rate = n_tp / n * 100.0
    return pnl_pct, n, n_tp, n_sl, n_to, win_rate, hit_rate


def pareto_frontier(records: List[FoldRecord], on_oos: bool) -> List[FoldRecord]:
    pts = []
    for r in records:
        if on_oos:
            if r.oos_n_trades == 0:
                continue
            pts.append((r, (r.oos_pnl_pct, r.oos_win_rate, r.oos_hit_rate)))
        else:
            if r.is_n_trades == 0:
                continue
            pts.append((r, (r.is_pnl_pct, r.is_win_rate, r.is_hit_rate)))

    pareto: List[FoldRecord] = []
    for r, pr in pts:
        dominated = False
        for q, qr in pts:
            if q is r:
                continue
            if all(qr[i] >= pr[i] for i in range(3)) and any(qr[i] > pr[i] for i in range(3)):
                dominated = True
                break
        if not dominated:
            pareto.append(r)
    return pareto


# ─── Combo / threshold enumeration ────────────────────────────

def enumerate_combo_thresholds() -> List[Tuple[FrozenSet[str], Dict[str, float]]]:
    """All (combo_set, thresholds_dict) variants. ~512 with 3-value grids."""
    out: List[Tuple[FrozenSet[str], Dict[str, float]]] = []
    for k in range(0, len(FILTER_NAMES) + 1):
        for combo_tuple in itertools.combinations(FILTER_NAMES, k):
            combo = frozenset(combo_tuple)
            sweep_filters = [f for f in combo_tuple if f in SWEEPABLE]
            if not sweep_filters:
                out.append((combo, {}))
                continue
            grids = [THRESHOLD_GRIDS[f] for f in sweep_filters]
            for combination in itertools.product(*grids):
                thresholds = dict(zip(sweep_filters, combination))
                out.append((combo, thresholds))
    return out


def fmt_combo(combo: FrozenSet[str]) -> str:
    if not combo:
        return "(none)"
    short = {
        "btc_rsi_floor": "btc_rsi",
        "token_rsi_momentum": "rsi_mom",
        "rsi_cap": "rsi_cap",
        "token_di_bullish": "di+",
        "relative_volume": "rvol",
    }
    return "+".join(short.get(f, f) for f in sorted(combo))


def fmt_thresholds(thresholds: Dict[str, float]) -> str:
    if not thresholds:
        return ""
    short = {
        "btc_rsi_floor": "btc",
        "token_rsi_momentum": "rsi",
        "rsi_cap": "cap",
        "relative_volume": "rvol",
    }
    return ";".join(f"{short[k]}={v}" for k, v in sorted(thresholds.items()))


def thresholds_key(thresholds: Dict[str, float]) -> Tuple[Tuple[str, float], ...]:
    return tuple(sorted(thresholds.items()))


# ─── Per-(universe, window) job ───────────────────────────────

def build_folds(n_master: int, is_days: int, oos_days: int, step_days: int) -> List[Tuple[int, int, int, int, int]]:
    folds = []
    fid = 0
    is_start = MIN_HISTORY
    while is_start + is_days + oos_days <= n_master:
        folds.append((fid, is_start, is_start + is_days,
                       is_start + is_days, is_start + is_days + oos_days))
        fid += 1
        is_start += step_days
    return folds


def run_job(
    universe_name: str,
    window_name: str,
    is_days: int,
    oos_days: int,
    tokens: Dict[str, TokenData],
    btc: TokenData,
    tpsl_cache: Dict[str, Dict[int, Tuple]],
    master: pd.DatetimeIndex,
    combo_thresholds: List[Tuple[FrozenSet[str], Dict[str, float]]],
    output_dir: str,
    limit_folds: int,
) -> Tuple[List[FoldRecord], List[Tuple[FrozenSet[str], Dict[str, float], int, str, int]]]:
    """Run one (universe, window) job. Writes per-job CSV. Returns (records, top_picks)."""
    n_master = len(master)
    folds = build_folds(n_master, is_days, oos_days, oos_days)
    if limit_folds > 0:
        folds = folds[:limit_folds]

    print(f"\n{'─'*68}", file=sys.stderr)
    print(f"  JOB: universe={universe_name}  window={window_name} ({is_days}d/{oos_days}d)", file=sys.stderr)
    print(f"  Folds: {len(folds)}  Combo×threshold variants: {len(combo_thresholds)}", file=sys.stderr)
    if folds:
        f0, fL = folds[0], folds[-1]
        print(f"  fold 0:   IS {master[f0[1]].date()}→{master[f0[2]-1].date()}   "
              f"OOS {master[f0[3]].date()}→{master[f0[4]-1].date()}", file=sys.stderr)
        print(f"  fold {len(folds)-1}:  IS {master[fL[1]].date()}→{master[fL[2]-1].date()}   "
              f"OOS {master[fL[3]].date()}→{master[fL[4]-1].date()}", file=sys.stderr)
    sims_per_fold = len(combo_thresholds) * len(TIMEOUTS) * len(TIEBREAKERS) * len(TOP_N_VALUES) * 2
    print(f"  Sims:  {sims_per_fold * len(folds):,} (per-fold {sims_per_fold:,})", file=sys.stderr)
    print(f"{'─'*68}", file=sys.stderr)

    records: List[FoldRecord] = []
    t_job = time.time()
    for fid, is_start, is_end, oos_start, oos_end in folds:
        t_fold = time.time()
        for combo, thresholds in combo_thresholds:
            for timeout in TIMEOUTS:
                for tb in TIEBREAKERS:
                    for top_n in TOP_N_VALUES:
                        is_trades, is_cash = simulate(is_start, is_end, combo, thresholds,
                                                       timeout, tb, top_n,
                                                       tokens, btc, tpsl_cache)
                        oos_trades, oos_cash = simulate(oos_start, oos_end, combo, thresholds,
                                                         timeout, tb, top_n,
                                                         tokens, btc, tpsl_cache)
                        is_m = metrics(is_trades, is_cash)
                        oos_m = metrics(oos_trades, oos_cash)
                        records.append(FoldRecord(
                            universe=universe_name, window=window_name, fold_id=fid,
                            is_start=is_start, is_end=is_end,
                            oos_start=oos_start, oos_end=oos_end,
                            combo=combo, thresholds=thresholds_key(thresholds),
                            timeout=timeout, tiebreaker=tb, top_n=top_n,
                            is_pnl_pct=is_m[0], is_n_trades=is_m[1],
                            is_n_tp=is_m[2], is_n_sl=is_m[3], is_n_to=is_m[4],
                            is_win_rate=is_m[5], is_hit_rate=is_m[6],
                            oos_pnl_pct=oos_m[0], oos_n_trades=oos_m[1],
                            oos_n_tp=oos_m[2], oos_n_sl=oos_m[3], oos_n_to=oos_m[4],
                            oos_win_rate=oos_m[5], oos_hit_rate=oos_m[6],
                        ))
        print(f"  fold {fid+1:3d}/{len(folds)} done in {time.time()-t_fold:.1f}s "
              f"(job total {time.time()-t_job:.1f}s)", file=sys.stderr)
    print(f"  job complete: {time.time()-t_job:.1f}s, {len(records):,} rows", file=sys.stderr)

    # Write per-job CSV
    job_dir = os.path.join(output_dir, f"{universe_name}_{window_name}")
    os.makedirs(job_dir, exist_ok=True)
    csv_path = os.path.join(job_dir, "results.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "universe", "window", "fold_id",
            "is_start", "is_end", "oos_start", "oos_end",
            "combo", "n_filters", "thresholds", "timeout", "tiebreaker", "top_n",
            "is_pnl_pct", "is_n_trades", "is_n_tp", "is_n_sl", "is_n_to",
            "is_win_rate", "is_hit_rate",
            "oos_pnl_pct", "oos_n_trades", "oos_n_tp", "oos_n_sl", "oos_n_to",
            "oos_win_rate", "oos_hit_rate",
        ])
        for r in records:
            w.writerow([
                r.universe, r.window, r.fold_id,
                str(master[r.is_start].date()), str(master[r.is_end - 1].date()),
                str(master[r.oos_start].date()), str(master[r.oos_end - 1].date()),
                fmt_combo(r.combo), len(r.combo),
                ";".join(f"{k}={v}" for k, v in r.thresholds),
                r.timeout, r.tiebreaker, r.top_n,
                round(r.is_pnl_pct, 3), r.is_n_trades, r.is_n_tp, r.is_n_sl, r.is_n_to,
                round(r.is_win_rate, 2), round(r.is_hit_rate, 2),
                round(r.oos_pnl_pct, 3), r.oos_n_trades, r.oos_n_tp, r.oos_n_sl, r.oos_n_to,
                round(r.oos_win_rate, 2), round(r.oos_hit_rate, 2),
            ])
    print(f"  → {csv_path}", file=sys.stderr)

    # Top picks across the IS Pareto frontier (by frequency)
    pareto_freq: Dict[Tuple[FrozenSet[str], Tuple, int, str, int], int] = {}
    for fid in sorted({r.fold_id for r in records}):
        fold_recs = [r for r in records if r.fold_id == fid]
        for r in pareto_frontier(fold_recs, on_oos=False):
            key = (r.combo, r.thresholds, r.timeout, r.tiebreaker, r.top_n)
            pareto_freq[key] = pareto_freq.get(key, 0) + 1
    top_picks = sorted(pareto_freq.items(), key=lambda kv: -kv[1])[:5]
    return records, [(combo, dict(th), to, tb, tn) for (combo, th, to, tb, tn), _ in top_picks]


# ─── Aggregate summary across all jobs ────────────────────────

def write_summary(
    output_dir: str,
    all_records: Dict[Tuple[str, str], List[FoldRecord]],
    masters: Dict[str, pd.DatetimeIndex],
):
    path = os.path.join(output_dir, "summary.md")
    with open(path, "w") as f:
        f.write("# Walk-Forward Full Optimization — Summary\n\n")
        f.write(f"- Universes: {list(UNIVERSES.keys())}\n")
        f.write(f"- Windows: {list(WINDOWS.keys())} ({WINDOWS})\n")
        f.write(f"- Filters: {list(FILTER_NAMES)}\n")
        f.write(f"- Threshold grids: {THRESHOLD_GRIDS}\n")
        f.write(f"- Timeouts: {TIMEOUTS}  ·  Tiebreakers: {TIEBREAKERS}  ·  top_n: {TOP_N_VALUES}\n")
        f.write(f"- Definitions: `pnl_pct = portfolio compounding`, "
                f"`win_rate = TP/(TP+SL)`, `hit_rate = TP/total`\n\n")

        for (uni, win), records in sorted(all_records.items()):
            master = masters[uni]
            is_days, oos_days = WINDOWS[win]
            f.write(f"## {uni} / {win} ({is_days}d / {oos_days}d)\n\n")
            f.write(f"- Folds: {len({r.fold_id for r in records})}  ·  "
                    f"Records: {len(records):,}\n\n")

            # Frequency on IS Pareto across folds
            pareto_freq: Dict[Tuple, List[FoldRecord]] = {}
            for fid in sorted({r.fold_id for r in records}):
                fold_recs = [r for r in records if r.fold_id == fid]
                for r in pareto_frontier(fold_recs, on_oos=False):
                    key = (r.combo, r.thresholds, r.timeout, r.tiebreaker, r.top_n)
                    pareto_freq.setdefault(key, []).append(r)

            rows = []
            for key, recs in pareto_freq.items():
                combo, th, to, tb, tn = key
                cnt = len(recs)
                avg_oos_pnl = float(np.mean([r.oos_pnl_pct for r in recs]))
                avg_oos_wr = float(np.mean([r.oos_win_rate for r in recs]))
                avg_oos_hr = float(np.mean([r.oos_hit_rate for r in recs]))
                avg_oos_n = float(np.mean([r.oos_n_trades for r in recs]))
                rows.append((cnt, fmt_combo(combo), dict(th), to, tb, tn,
                             avg_oos_pnl, avg_oos_wr, avg_oos_hr, avg_oos_n))
            rows.sort(key=lambda x: (-x[0], -x[6]))

            f.write("Top 20 picks by IS-Pareto frequency:\n\n")
            f.write("| # Folds | Combo | Thresholds | TO | TB | top_n | "
                    "Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |\n")
            f.write("|---:|---|---|---:|---|---:|---:|---:|---:|---:|\n")
            for cnt, combo_str, th, to, tb, tn, p, wr, hr, nt in rows[:20]:
                f.write(f"| {cnt} | `{combo_str}` | {fmt_thresholds(th) or '—'} | "
                        f"{to} | {tb} | {tn} | "
                        f"{p:+.2f} | {wr:.1f} | {hr:.1f} | {nt:.1f} |\n")
            f.write("\n")
    print(f"\n[summary] → {path}", file=sys.stderr)


# ─── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Walk-forward FULL optimization (A+B+C+D)")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output-dir", default="research/walkforward_full")
    parser.add_argument("--cache-dir", default="research/.cache/walkforward_full")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--limit-folds", type=int, default=0)
    parser.add_argument("--limit-universes", nargs="+", default=None,
                        help="Subset of universes to run (e.g. u17). Default: all.")
    parser.add_argument("--limit-windows", nargs="+", default=None,
                        help="Subset of windows to run (e.g. w30). Default: all.")
    args = parser.parse_args()

    t0 = time.time()
    print("\n" + "=" * 72, file=sys.stderr)
    print("  WALK-FORWARD FULL OPTIMIZATION (A + B + C + D)", file=sys.stderr)
    print(f"  Universes: {list(UNIVERSES.keys())}", file=sys.stderr)
    print(f"  Windows:   {list(WINDOWS.keys())}", file=sys.stderr)
    print(f"  Filters:   {len(FILTER_NAMES)}, sweepable {len(SWEEPABLE)}", file=sys.stderr)
    combo_thresholds = enumerate_combo_thresholds()
    print(f"  Combo×threshold variants: {len(combo_thresholds)}", file=sys.stderr)
    print(f"  Timeouts:  {TIMEOUTS}", file=sys.stderr)
    print(f"  Tiebrk:    {TIEBREAKERS}", file=sys.stderr)
    print(f"  Top-N:     {TOP_N_VALUES}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    # Apply subset filters
    universes_to_run = list(UNIVERSES.items())
    if args.limit_universes:
        universes_to_run = [(k, v) for k, v in universes_to_run if k in args.limit_universes]
    windows_to_run = list(WINDOWS.items())
    if args.limit_windows:
        windows_to_run = [(k, v) for k, v in windows_to_run if k in args.limit_windows]

    # ── Phase 1: Load all unique tokens once ──
    all_symbols = sorted({s for _, syms in universes_to_run for s in syms})
    print(f"\n[1/4] Loading {len(all_symbols)} unique tokens...", file=sys.stderr)
    raw_dfs_global = load_token_dfs(all_symbols, args.data_dir)
    if "BTC" not in raw_dfs_global:
        raise RuntimeError("BTC required for btc_rsi_floor — not in data")

    # ── Phase 2: Per-symbol TPSL precompute (the slow phase) ──
    print(f"\n[2/4] Per-symbol TPSL precompute (cached at {args.cache_dir})", file=sys.stderr)
    per_symbol_cache: Dict[str, Dict[int, Tuple]] = {}
    t_pre = time.time()
    n_to_compute = 0
    for sym in all_symbols:
        if sym not in raw_dfs_global:
            continue
        path = os.path.join(args.cache_dir, f"{sym}.pkl")
        if not args.rebuild_cache and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    payload = pickle.load(f)
                if payload.get("signature") == per_symbol_signature(raw_dfs_global[sym]):
                    per_symbol_cache[sym] = payload["tpsl"]
                    n_valid = sum(1 for v in payload["tpsl"].values()
                                  if v[0] is not None and v[1] is not None)
                    print(f"  {sym:<6} (cached) {len(payload['tpsl']):4d} entries, "
                          f"{n_valid:4d} valid TP/SL", file=sys.stderr)
                    continue
            except Exception:
                pass
        n_to_compute += 1
        ts = time.time()
        per_symbol_cache[sym] = load_or_build_per_symbol_cache(
            sym, raw_dfs_global[sym], args.cache_dir, rebuild=True
        )
        n_valid = sum(1 for v in per_symbol_cache[sym].values()
                      if v[0] is not None and v[1] is not None)
        print(f"  {sym:<6} computed  {len(per_symbol_cache[sym]):4d} entries, "
              f"{n_valid:4d} valid TP/SL ({time.time()-ts:.1f}s)", file=sys.stderr)
    print(f"  precompute total: {time.time()-t_pre:.1f}s "
          f"({n_to_compute} computed, {len(per_symbol_cache)-n_to_compute} cached)",
          file=sys.stderr)

    # ── Phase 3: Per-universe pipeline ──
    print(f"\n[3/4] Running {len(universes_to_run)} × {len(windows_to_run)} jobs", file=sys.stderr)
    all_records: Dict[Tuple[str, str], List[FoldRecord]] = {}
    masters: Dict[str, pd.DatetimeIndex] = {}
    universe_top_picks: Dict[Tuple[str, str], List] = {}

    for universe_name, symbols in universes_to_run:
        # Build per-universe master index from raw dfs
        u_dfs = {s: raw_dfs_global[s] for s in symbols if s in raw_dfs_global}
        if not u_dfs:
            print(f"  [skip] {universe_name}: no data", file=sys.stderr)
            continue
        master_set = set()
        for df in u_dfs.values():
            master_set.update(df.index)
        master = pd.DatetimeIndex(sorted(master_set))
        masters[universe_name] = master
        print(f"\n  {universe_name}: {len(u_dfs)}/{len(symbols)} tokens, "
              f"master {master[0].date()}→{master[-1].date()} ({len(master)} days)",
              file=sys.stderr)

        # Per-token aligned indicator pre-compute
        tokens: Dict[str, TokenData] = {}
        for sym, df in u_dfs.items():
            tokens[sym] = precompute_token(sym, df, master)
        btc = tokens["BTC"]

        # Translate per-LOCAL cache → per-MASTER cache for this universe
        tpsl_cache_universe = build_tpsl_cache_for_universe(tokens, u_dfs, per_symbol_cache)

        for window_name, (is_days, oos_days) in windows_to_run:
            records, picks = run_job(
                universe_name, window_name, is_days, oos_days,
                tokens, btc, tpsl_cache_universe, master,
                combo_thresholds, args.output_dir, args.limit_folds,
            )
            all_records[(universe_name, window_name)] = records
            universe_top_picks[(universe_name, window_name)] = picks

    # ── Phase 4: Aggregate summary ──
    print(f"\n[4/4] Writing aggregate summary", file=sys.stderr)
    write_summary(args.output_dir, all_records, masters)

    print(f"\nDone in {time.time()-t0:.1f}s "
          f"({(time.time()-t0)/60:.1f} min)", file=sys.stderr)


if __name__ == "__main__":
    main()
