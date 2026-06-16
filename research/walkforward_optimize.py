#!/usr/bin/env python3
# =============================================================================
# ⚠️  RESEARCH-GRADE — NOT DECISION-GRADE.  (validity audit 2026-06-16)
#   • V1 ENGINE: scores signals via `core.sr_analysis` (line 59) — the OLD
#     detector. Live trades on V2 `core.sr_analysis2`, so the signals tested
#     here are NOT the signals that trade.
#   • UNCOSTED: PnL = raw (exit-entry)/entry, compounded, with NO fee/slippage/
#     funding (lines 445, 518-519) — systematically overstates edge.
#   • Fixed 17-token universe (lines 65-68) → survivorship bias.
#   This optimizer produced earlier ta17 filter-combo picks; treat them as
#   HYPOTHESES, not results. Reproduce on the BLESSED, fully-costed engine
#   before any config/deploy decision:
#     trading-system/research/run_portfolio_backtest.py
#     (4.5bps + 5bps slippage + funding; see research/BACKTEST_ENGINES.md)
# =============================================================================
"""
walkforward_optimize.py — Walk-forward Pareto optimization of filter combos.
============================================================================

Spec (per user, 2026-04-09):
  - Universe: 17 tokens
      BTC ETH XRP SOL ADA LINK SUI AAVE AVAX TAO HYPE DOGE BNB HBAR DOT NEAR UNI
  - Strategy: full-capital compounding, ONE position at a time
  - Entry: top-1 by tiebreaker (rsi or rr) among tokens passing the filter combo
           (we run BOTH tiebreakers separately)
  - Exit: TP / SL / Timeout
  - TP/SL: core.tpsl.compute_tp_sl (S/R cascade with ATR fallback)
  - Filters (5, no hard gates): btc_rsi_floor, token_rsi_momentum, rsi_cap,
                                token_di_bullish, relative_volume
  - Search: every on/off subset → 2^5 = 32 combos
  - Timeout sweep: {3, 5, 7, 10, 14, 21} days
  - Walk-forward: 30d IS / 30d OOS / step 30d (sliding, non-overlapping OOS)
  - Score: Pareto frontier on (PnL%, Win%, Hit%) — selected on IS, reported on OOS
  - Definitions:
        win_rate = TP / (TP + SL)         (closed only)
        hit_rate = TP / total_trades      (TP hit ratio incl. timeouts)

Outputs:
  research/walkforward_results.csv   — every (fold, combo, timeout, tb) row
  research/walkforward_summary.md    — Pareto per fold + frequency aggregation
  research/walkforward_trades.csv    — per-trade ledger for the most-frequent
                                       Pareto winners

Caching:
  research/.tpsl_cache.pkl           — analyze_token + tpsl per (sym, day),
                                       reused across runs. Use --rebuild-cache
                                       to force a recompute (e.g. after a
                                       core/sr_analysis.py change).

Usage:
  python3 research/walkforward_optimize.py
  python3 research/walkforward_optimize.py --limit-folds 3      # quick test
  python3 research/walkforward_optimize.py --rebuild-cache      # force recompute
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


# ─── Configuration ────────────────────────────────────────────

UNIVERSE = [
    "BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE", "AVAX",
    "TAO", "HYPE", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI",
]

FILTER_NAMES = (
    "btc_rsi_floor",       # BTC RSI(10) >= 50
    "token_rsi_momentum",  # Token RSI(10) > 60
    "rsi_cap",             # Token RSI(10) <= 80
    "token_di_bullish",    # DI+ > DI-
    "relative_volume",     # RVOL(20) >= 1.5
)

# Filter parameters (dashboard defaults — not tuned)
BTC_RSI_PERIOD = 10
BTC_RSI_THRESHOLD = 50.0
TOK_RSI_PERIOD = 10
TOK_RSI_MOMENTUM_THRESHOLD = 60.0
RSI_CAP_THRESHOLD = 80.0
ADX_PERIOD = 14
RVOL_PERIOD = 20
RVOL_THRESHOLD = 1.5

TIMEOUTS = [3, 5, 7, 10, 14, 21]
TIEBREAKERS = ["rsi", "rr"]

IS_DAYS = 30
OOS_DAYS = 30
STEP_DAYS = 30
MIN_HISTORY = 200       # analyze_token needs ~180d (longest detector window)


# ─── Data structures ──────────────────────────────────────────

@dataclass
class TokenData:
    """Per-token data aligned to the master date index."""
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
    has_data: np.ndarray              # bool, True where the token has a real bar
    df_local: pd.DataFrame            # untouched original df
    master_to_local: Dict[int, int]   # master_idx → local_idx (only for real bars)


@dataclass
class Trade:
    symbol: str
    entry_mi: int
    entry_price: float
    take_profit: float
    stop_loss: float
    exit_mi: int = -1
    exit_price: float = 0.0
    exit_reason: str = ""             # TP / SL / TIMEOUT / OPEN
    pnl_pct: float = 0.0
    hold_days: int = 0


@dataclass
class FoldRecord:
    fold_id: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int
    combo: FrozenSet[str]
    timeout: int
    tiebreaker: str
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

def load_universe(data_dir: str) -> Tuple[Dict[str, pd.DataFrame], pd.DatetimeIndex]:
    raw: Dict[str, pd.DataFrame] = {}
    for sym in UNIVERSE:
        df = load_from_cache(sym, data_dir)
        if df is None or len(df) == 0:
            print(f"[load] WARN: no cache for {sym}", file=sys.stderr)
            continue
        raw[sym] = df
    if "BTC" not in raw:
        raise RuntimeError("BTC required for btc_rsi_floor — not found in cache")
    master_set = set()
    for df in raw.values():
        master_set.update(df.index)
    master = pd.DatetimeIndex(sorted(master_set))
    print(f"[load] {len(raw)}/{len(UNIVERSE)} tokens loaded. "
          f"Master index {master[0].date()} → {master[-1].date()} ({len(master)} days)",
          file=sys.stderr)
    return raw, master


# ─── Vectorized indicators ────────────────────────────────────

def vec_rsi(close: pd.Series, period: int) -> np.ndarray:
    """Matches core.filters.compute_rsi (SMA of gains/losses, returns 100 if loss==0)."""
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
    """Matches core.filters.compute_adx_di — Wilder smoothing, full per-bar arrays."""
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


def precompute_token(sym: str, df_local: pd.DataFrame, master: pd.DatetimeIndex) -> TokenData:
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


# ─── TPSL precompute (slow phase, disk-cached) ────────────────

def precompute_tpsl_for_token(token: TokenData) -> Dict[int, Tuple[Optional[float], Optional[float], Optional[float]]]:
    """For each master day where the token has ≥ MIN_HISTORY local bars, compute (tp, sl, rr)."""
    out: Dict[int, Tuple[Optional[float], Optional[float], Optional[float]]] = {}
    df = token.df_local
    for mi, li in token.master_to_local.items():
        if li < MIN_HISTORY:
            continue
        history = df.iloc[: li + 1]
        try:
            analysis = analyze_token(token.sym, history)
            result = compute_tp_sl(analysis)
            if result is None:
                out[mi] = (None, None, None)
                continue
            tp = result.get("take_profit")
            sl = result.get("stop_loss")
            rr = result.get("raw_rr")
            out[mi] = (tp, sl, rr)
        except Exception:
            out[mi] = (None, None, None)
    return out


def cache_signature(tokens: Dict[str, TokenData], master: pd.DatetimeIndex) -> Tuple:
    """Used to detect if cache on disk is still valid for the current data."""
    parts = []
    for sym in sorted(tokens.keys()):
        tdata = tokens[sym]
        parts.append((sym, len(tdata.df_local),
                      str(tdata.df_local.index[0]),
                      str(tdata.df_local.index[-1])))
    return (str(master[0]), str(master[-1]), len(master), tuple(parts))


def load_or_build_tpsl_cache(tokens: Dict[str, TokenData], master: pd.DatetimeIndex,
                              cache_path: str, rebuild: bool) -> Dict[str, Dict[int, Tuple]]:
    sig = cache_signature(tokens, master)
    if not rebuild and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                payload = pickle.load(f)
            if payload.get("signature") == sig:
                cache = payload["tpsl"]
                print(f"[cache] loaded {cache_path} ({sum(len(v) for v in cache.values())} entries)",
                      file=sys.stderr)
                return cache
            print("[cache] signature mismatch — rebuilding", file=sys.stderr)
        except Exception as e:
            print(f"[cache] load failed ({e}) — rebuilding", file=sys.stderr)

    print("[cache] precomputing analyze_token + compute_tp_sl per (token, day)...", file=sys.stderr)
    print("        (this is the slow phase — single-threaded)", file=sys.stderr)
    cache: Dict[str, Dict[int, Tuple]] = {}
    t0 = time.time()
    for i, (sym, tdata) in enumerate(tokens.items(), 1):
        ts = time.time()
        cache[sym] = precompute_tpsl_for_token(tdata)
        n_total = len(cache[sym])
        n_valid = sum(1 for v in cache[sym].values() if v[0] is not None and v[1] is not None)
        print(f"  [{i:2d}/{len(tokens)}] {sym:<6} {n_total:4d} analyses, "
              f"{n_valid:4d} valid TP/SL ({time.time()-ts:.1f}s)", file=sys.stderr)
    print(f"  total: {time.time()-t0:.1f}s", file=sys.stderr)

    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump({"signature": sig, "tpsl": cache}, f)
    print(f"[cache] saved → {cache_path}", file=sys.stderr)
    return cache


# ─── Filter checks ────────────────────────────────────────────

def filter_passes(combo: FrozenSet[str], tdata: TokenData, mi: int, btc: TokenData) -> bool:
    """Empty combo always passes. Indicator NaN counts as fail (not enough history)."""
    if "btc_rsi_floor" in combo:
        v = btc.rsi[mi]
        if np.isnan(v) or v < BTC_RSI_THRESHOLD:
            return False
    if "token_rsi_momentum" in combo:
        v = tdata.rsi[mi]
        if np.isnan(v) or not (v > TOK_RSI_MOMENTUM_THRESHOLD):
            return False
    if "rsi_cap" in combo:
        v = tdata.rsi[mi]
        if np.isnan(v) or not (v <= RSI_CAP_THRESHOLD):
            return False
    if "token_di_bullish" in combo:
        dp = tdata.di_plus[mi]
        dm = tdata.di_minus[mi]
        if np.isnan(dp) or np.isnan(dm) or not (dp > dm):
            return False
    if "relative_volume" in combo:
        v = tdata.rvol[mi]
        if np.isnan(v) or v < RVOL_THRESHOLD:
            return False
    return True


# ─── Simulator ────────────────────────────────────────────────

def simulate(
    window_start: int,
    window_end: int,                 # exclusive
    combo: FrozenSet[str],
    timeout: int,
    tiebreaker: str,
    tokens: Dict[str, TokenData],
    btc: TokenData,
    tpsl_cache: Dict[str, Dict[int, Tuple]],
) -> List[Trade]:
    """Walk window, manage one open position, return executed trades."""
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    pending_entry: Optional[Tuple[str, float, float]] = None
    exited_today: Set[str] = set()

    for mi in range(window_start, window_end):
        # ── Execute pending entry from yesterday ──
        if pending_entry is not None and open_trade is None:
            sym, tp, sl = pending_entry
            tdata = tokens[sym]
            entry_price = tdata.open[mi]
            if not np.isnan(entry_price):
                open_trade = Trade(
                    symbol=sym,
                    entry_mi=mi,
                    entry_price=float(entry_price),
                    take_profit=float(tp),
                    stop_loss=float(sl),
                )
        pending_entry = None
        exited_today.clear()

        # ── Check open trade for exit ──
        if open_trade is not None:
            t = open_trade
            tdata = tokens[t.symbol]
            hi = tdata.high[mi]
            lo = tdata.low[mi]
            cl = tdata.close[mi]
            if not np.isnan(cl):
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
                    trades.append(t)
                    exited_today.add(t.symbol)
                    open_trade = None

        # ── Generate signal for tomorrow ──
        if open_trade is not None:
            continue
        if mi >= window_end - 1:
            continue

        candidates: List[Tuple[str, float, float, float, float]] = []
        for sym, tdata in tokens.items():
            if sym in exited_today:
                continue
            if not tdata.has_data[mi]:
                continue
            if not filter_passes(combo, tdata, mi, btc):
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

        sym, _, _, tp, sl = candidates[0]
        pending_entry = (sym, tp, sl)

    # ── Force-close any leftover open trade at window end ──
    if open_trade is not None:
        last_mi = window_end - 1
        tdata = tokens[open_trade.symbol]
        cl = tdata.close[last_mi]
        if not np.isnan(cl):
            open_trade.exit_mi = last_mi
            open_trade.exit_price = float(cl)
            open_trade.exit_reason = "OPEN"
            open_trade.pnl_pct = (float(cl) - open_trade.entry_price) / open_trade.entry_price * 100.0
            trades.append(open_trade)

    return trades


# ─── Metrics + Pareto ─────────────────────────────────────────

def metrics(trades: List[Trade]) -> Tuple[float, int, int, int, int, float, float]:
    n = len(trades)
    if n == 0:
        return 0.0, 0, 0, 0, 0, 0.0, 0.0
    n_tp = sum(1 for t in trades if t.exit_reason == "TP")
    n_sl = sum(1 for t in trades if t.exit_reason == "SL")
    n_to = sum(1 for t in trades if t.exit_reason in ("TIMEOUT", "OPEN"))
    cap = 1.0
    for t in trades:
        cap *= (1.0 + t.pnl_pct / 100.0)
    pnl_pct = (cap - 1.0) * 100.0
    win_rate = (n_tp / (n_tp + n_sl) * 100.0) if (n_tp + n_sl) > 0 else 0.0
    hit_rate = n_tp / n * 100.0
    return pnl_pct, n, n_tp, n_sl, n_to, win_rate, hit_rate


def pareto_frontier(records: List[FoldRecord], on_oos: bool) -> List[FoldRecord]:
    """Non-dominated set on (PnL%, Win%, Hit%). Higher is better on all 3 axes.
    Records with zero trades are excluded so the frontier is meaningful."""
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


# ─── Output formatting ────────────────────────────────────────

def fmt_combo(combo: FrozenSet[str]) -> str:
    if not combo:
        return "(none)"
    short = {
        "btc_rsi_floor": "btc_rsi",
        "token_rsi_momentum": "rsi>60",
        "rsi_cap": "rsi<=80",
        "token_di_bullish": "di+",
        "relative_volume": "rvol",
    }
    return "+".join(short.get(f, f) for f in sorted(combo))


# ─── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Walk-forward Pareto optimization of filter combos")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--results-csv", default="research/walkforward_results.csv")
    parser.add_argument("--summary-md", default="research/walkforward_summary.md")
    parser.add_argument("--trades-csv", default="research/walkforward_trades.csv")
    parser.add_argument("--cache-path", default="research/.tpsl_cache.pkl")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="Force re-compute of analyze_token cache")
    parser.add_argument("--limit-folds", type=int, default=0,
                        help="Limit number of folds (debug). 0 = all.")
    args = parser.parse_args()

    t0 = time.time()
    print("\n" + "=" * 72, file=sys.stderr)
    print("  WALK-FORWARD PARETO OPTIMIZATION", file=sys.stderr)
    print(f"  Universe: {len(UNIVERSE)} tokens", file=sys.stderr)
    print(f"  Filters:  {len(FILTER_NAMES)} → {2**len(FILTER_NAMES)} combos", file=sys.stderr)
    print(f"  Timeouts: {TIMEOUTS}", file=sys.stderr)
    print(f"  Tiebrk:   {TIEBREAKERS}", file=sys.stderr)
    print(f"  Window:   IS={IS_DAYS}d / OOS={OOS_DAYS}d / step={STEP_DAYS}d", file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    # 1. Load
    raw, master = load_universe(args.data_dir)

    # 2. Per-token vectorized indicators
    print("\n[1/4] Pre-computing indicators...", file=sys.stderr)
    tokens: Dict[str, TokenData] = {}
    for sym, df in raw.items():
        tokens[sym] = precompute_token(sym, df, master)
        n_valid = int(tokens[sym].has_data.sum())
        print(f"  {sym:<6} {n_valid:4d} bars", file=sys.stderr)
    btc = tokens["BTC"]

    # 3. analyze_token + compute_tp_sl precompute (cached on disk)
    print("\n[2/4] TPSL precompute...", file=sys.stderr)
    tpsl_cache = load_or_build_tpsl_cache(tokens, master, args.cache_path, args.rebuild_cache)

    # 4. Build folds
    n_master = len(master)
    folds: List[Tuple[int, int, int, int, int]] = []
    fid = 0
    is_start = MIN_HISTORY
    while is_start + IS_DAYS + OOS_DAYS <= n_master:
        folds.append((fid, is_start, is_start + IS_DAYS,
                       is_start + IS_DAYS, is_start + IS_DAYS + OOS_DAYS))
        fid += 1
        is_start += STEP_DAYS
    if args.limit_folds > 0:
        folds = folds[: args.limit_folds]
    print(f"\n[3/4] Built {len(folds)} folds", file=sys.stderr)
    if folds:
        f0 = folds[0]
        fL = folds[-1]
        print(f"  fold 0:   IS {master[f0[1]].date()}→{master[f0[2]-1].date()}   "
              f"OOS {master[f0[3]].date()}→{master[f0[4]-1].date()}", file=sys.stderr)
        print(f"  fold {len(folds)-1}:  IS {master[fL[1]].date()}→{master[fL[2]-1].date()}   "
              f"OOS {master[fL[3]].date()}→{master[fL[4]-1].date()}", file=sys.stderr)

    # 5. Simulate every (combo, timeout, tiebreaker) per fold
    combos = []
    for k in range(0, len(FILTER_NAMES) + 1):
        for c in itertools.combinations(FILTER_NAMES, k):
            combos.append(frozenset(c))
    n_sims_per_fold = len(combos) * len(TIMEOUTS) * len(TIEBREAKERS) * 2
    print(f"\n[4/4] Simulating: {len(combos)} combos × {len(TIMEOUTS)} TO × "
          f"{len(TIEBREAKERS)} tb × {len(folds)} folds × 2 (IS+OOS) "
          f"= {n_sims_per_fold * len(folds)} sims", file=sys.stderr)

    records: List[FoldRecord] = []
    t_sim = time.time()
    for fid, is_start, is_end, oos_start, oos_end in folds:
        for combo in combos:
            for timeout in TIMEOUTS:
                for tb in TIEBREAKERS:
                    is_trades = simulate(is_start, is_end, combo, timeout, tb,
                                         tokens, btc, tpsl_cache)
                    oos_trades = simulate(oos_start, oos_end, combo, timeout, tb,
                                          tokens, btc, tpsl_cache)
                    is_m = metrics(is_trades)
                    oos_m = metrics(oos_trades)
                    records.append(FoldRecord(
                        fold_id=fid,
                        is_start=is_start, is_end=is_end,
                        oos_start=oos_start, oos_end=oos_end,
                        combo=combo, timeout=timeout, tiebreaker=tb,
                        is_pnl_pct=is_m[0], is_n_trades=is_m[1],
                        is_n_tp=is_m[2], is_n_sl=is_m[3], is_n_to=is_m[4],
                        is_win_rate=is_m[5], is_hit_rate=is_m[6],
                        oos_pnl_pct=oos_m[0], oos_n_trades=oos_m[1],
                        oos_n_tp=oos_m[2], oos_n_sl=oos_m[3], oos_n_to=oos_m[4],
                        oos_win_rate=oos_m[5], oos_hit_rate=oos_m[6],
                    ))
        print(f"  fold {fid+1}/{len(folds)} done ({time.time()-t_sim:.1f}s)", file=sys.stderr)
    print(f"  Sim total: {time.time()-t_sim:.1f}s", file=sys.stderr)

    # 6. Write per-row CSV
    os.makedirs(os.path.dirname(args.results_csv) or ".", exist_ok=True)
    with open(args.results_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "fold_id", "is_start", "is_end", "oos_start", "oos_end",
            "combo", "n_filters", "timeout", "tiebreaker",
            "is_pnl_pct", "is_n_trades", "is_n_tp", "is_n_sl", "is_n_to",
            "is_win_rate", "is_hit_rate",
            "oos_pnl_pct", "oos_n_trades", "oos_n_tp", "oos_n_sl", "oos_n_to",
            "oos_win_rate", "oos_hit_rate",
        ])
        for r in records:
            w.writerow([
                r.fold_id,
                str(master[r.is_start].date()),
                str(master[r.is_end - 1].date()),
                str(master[r.oos_start].date()),
                str(master[r.oos_end - 1].date()),
                fmt_combo(r.combo), len(r.combo), r.timeout, r.tiebreaker,
                round(r.is_pnl_pct, 3), r.is_n_trades, r.is_n_tp, r.is_n_sl, r.is_n_to,
                round(r.is_win_rate, 2), round(r.is_hit_rate, 2),
                round(r.oos_pnl_pct, 3), r.oos_n_trades, r.oos_n_tp, r.oos_n_sl, r.oos_n_to,
                round(r.oos_win_rate, 2), round(r.oos_hit_rate, 2),
            ])
    print(f"\n[output] {args.results_csv} ({len(records)} rows)", file=sys.stderr)

    # 7. Pareto per fold + frequency aggregation
    pareto_per_fold: Dict[int, List[FoldRecord]] = {}
    pareto_freq: Dict[Tuple[FrozenSet[str], int, str], List[FoldRecord]] = {}
    for fid in sorted({r.fold_id for r in records}):
        fold_recs = [r for r in records if r.fold_id == fid]
        pareto_is = pareto_frontier(fold_recs, on_oos=False)  # IS-selected
        pareto_per_fold[fid] = pareto_is
        for r in pareto_is:
            key = (r.combo, r.timeout, r.tiebreaker)
            pareto_freq.setdefault(key, []).append(r)

    # 8. Summary markdown
    with open(args.summary_md, "w") as f:
        f.write("# Walk-Forward Pareto Optimization Summary\n\n")
        f.write(f"- **Universe ({len(UNIVERSE)}):** {', '.join(UNIVERSE)}\n")
        f.write(f"- **Filters ({len(FILTER_NAMES)}):** {', '.join(FILTER_NAMES)}\n")
        f.write(f"- **Combos:** {len(combos)}  ·  **Timeouts:** {TIMEOUTS}  "
                f"·  **Tiebreakers:** {TIEBREAKERS}\n")
        f.write(f"- **Walk-forward:** IS={IS_DAYS}d / OOS={OOS_DAYS}d / step={STEP_DAYS}d "
                f"·  **Folds:** {len(folds)}\n")
        f.write(f"- **Definitions:** `win_rate = TP/(TP+SL)`, "
                f"`hit_rate = TP/total`, "
                f"`pnl = compounding (1+pnl) over trades, single-position`\n\n")

        f.write("## Frequency on IS Pareto frontier (across folds)\n\n")
        f.write("How often each `(combo, timeout, tiebreaker)` was selected by the IS Pareto. "
                "OOS columns are means across the folds where this point made the IS frontier.\n\n")
        rows = []
        for key, recs in pareto_freq.items():
            combo, to, tb = key
            n_folds = len(recs)
            avg_oos_pnl = float(np.mean([r.oos_pnl_pct for r in recs]))
            avg_oos_wr = float(np.mean([r.oos_win_rate for r in recs]))
            avg_oos_hr = float(np.mean([r.oos_hit_rate for r in recs]))
            avg_oos_n = float(np.mean([r.oos_n_trades for r in recs]))
            rows.append((n_folds, fmt_combo(combo), to, tb,
                         avg_oos_pnl, avg_oos_wr, avg_oos_hr, avg_oos_n))
        rows.sort(key=lambda x: (-x[0], -x[4]))

        f.write("| Folds | Combo | Timeout | Tiebreaker | Avg OOS PnL% | Avg OOS Win% | "
                "Avg OOS Hit% | Avg trades |\n")
        f.write("|---:|---|---:|---|---:|---:|---:|---:|\n")
        for cnt, combo_str, to, tb, p, wr, hr, nt in rows:
            f.write(f"| {cnt} | `{combo_str}` | {to} | {tb} | "
                    f"{p:+.2f} | {wr:.1f} | {hr:.1f} | {nt:.1f} |\n")

        f.write("\n## Pareto frontier per fold (IS-selected, OOS-reported)\n\n")
        for fid in sorted(pareto_per_fold.keys()):
            f0 = folds[fid]
            f.write(f"### Fold {fid}: IS {master[f0[1]].date()}→{master[f0[2]-1].date()}  "
                    f"·  OOS {master[f0[3]].date()}→{master[f0[4]-1].date()}\n\n")
            ps = pareto_per_fold[fid]
            if not ps:
                f.write("_(no Pareto picks — all combos produced zero trades)_\n\n")
                continue
            f.write("| Combo | TO | TB | IS PnL% | IS Win% | IS Hit% | IS N | "
                    "OOS PnL% | OOS Win% | OOS Hit% | OOS N |\n")
            f.write("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
            for r in sorted(ps, key=lambda r: -r.is_pnl_pct):
                f.write(f"| `{fmt_combo(r.combo)}` | {r.timeout} | {r.tiebreaker} | "
                        f"{r.is_pnl_pct:+.2f} | {r.is_win_rate:.1f} | {r.is_hit_rate:.1f} | "
                        f"{r.is_n_trades} | "
                        f"{r.oos_pnl_pct:+.2f} | {r.oos_win_rate:.1f} | {r.oos_hit_rate:.1f} | "
                        f"{r.oos_n_trades} |\n")
            f.write("\n")
    print(f"[output] {args.summary_md}", file=sys.stderr)

    # 9. Per-trade ledger for the most-frequent Pareto winners (top 5 keys)
    top_keys = sorted(pareto_freq.items(), key=lambda kv: -len(kv[1]))[:5]
    with open(args.trades_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["combo", "timeout", "tiebreaker", "fold_id", "window",
                    "symbol", "entry_date", "entry_price",
                    "tp", "sl", "exit_date", "exit_price", "exit_reason",
                    "pnl_pct", "hold_days"])
        for (combo, to, tb), recs in top_keys:
            for r in recs:
                # Re-simulate this record to get the trade ledger (cheap)
                for window_name, ws, we in [("IS", r.is_start, r.is_end),
                                            ("OOS", r.oos_start, r.oos_end)]:
                    trades = simulate(ws, we, combo, to, tb, tokens, btc, tpsl_cache)
                    for t in trades:
                        w.writerow([
                            fmt_combo(combo), to, tb, r.fold_id, window_name,
                            t.symbol,
                            str(master[t.entry_mi].date()), round(t.entry_price, 6),
                            round(t.take_profit, 6), round(t.stop_loss, 6),
                            str(master[t.exit_mi].date()) if t.exit_mi >= 0 else "",
                            round(t.exit_price, 6), t.exit_reason,
                            round(t.pnl_pct, 3), t.hold_days,
                        ])
    print(f"[output] {args.trades_csv}", file=sys.stderr)

    print(f"\nDone in {time.time()-t0:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
