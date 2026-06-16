#!/usr/bin/env python3
# =============================================================================
# ⚠️  RESEARCH-GRADE — NOT DECISION-GRADE.  (validity audit 2026-06-16)
#   • V1 ENGINE: `core.sr_analysis` (line 56) — the OLD detector. Live trades
#     on V2 `core.sr_analysis2`, so the signals tested here are NOT live's.
#   • PARTIAL COSTS: charges 3bps/side fee (line 66) but NO slippage or funding
#     — lighter than live; use for relative comparison only, not absolute edge.
#   • Fixed universes (lines 72-84) → survivorship bias.
#   Numbers here are HYPOTHESES. Reproduce on the BLESSED, fully-costed engine
#   before any decision: trading-system/research/run_portfolio_backtest.py
#   (see research/BACKTEST_ENGINES.md).
# =============================================================================
"""
cascade_compare.py — Re-run the sr_backtest_report.md backtest with asymmetric
cascade thresholds and compare against the 1.0/1.0 baseline.

What this does
==============
Reproduces the methodology from sr-dashboard/sr_backtest_report.md:
  - 1-year window: 2025-04-02 -> 2026-04-02 (~365 daily bars)
  - Single-slot portfolio (1 token at a time, full capital)
  - Entry: signal at daily close, fill at next-day open
  - TP/SL: from S/R model. Same-day TP+SL -> SL (conservative)
  - Timeout: 7 days at close
  - Fee: 3 bps per entry/exit
  - Min history: 60 days before first trade

For each universe + filter combo, runs TWO cascade variants:
  - baseline:   tp_cascade_atr=1.0, sl_cascade_atr=1.0  (current production)
  - asymmetric: tp_cascade_atr=1.0, sl_cascade_atr=0.75 (proposed UI cascade)

Universes / filters / tiebreakers (from sr_backtest_report.md "Results"):
  - Top 3   (BTC, ETH, SOL)               filter=di            select=selRR
  - Top 20  (17 tokens)                   filter=btcrsi+rsicap select=selRSI
  - HL44    (44 HL-tradable, 43 in data)  filter=btcrsi+rsi+rvol+rsicap select=selRSI

Per-symbol S/R analysis is cached at /tmp/cascade_bt_cache/{SYM}.pkl so the
slow part (analyze_token per day) only happens on first run.

Usage
=====
    cd /Users/xris/GitHub/Support_Resistance
    python3 research/cascade_compare.py

    # Quick smoke run on Top 3 only:
    python3 research/cascade_compare.py --only top3

    # Force-rebuild the per-symbol cache:
    python3 research/cascade_compare.py --rebuild-cache
"""

import argparse
import os
import pickle
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_PARENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _PARENT_DIR)

from core.fetcher import load_from_cache             # noqa: E402
from core.sr_analysis import analyze_token           # noqa: E402
from core.tpsl import _compute_tp_sl_impl            # noqa: E402


# ─── Config ───────────────────────────────────────────────────

PERIOD_START = "2025-04-02"
PERIOD_END   = "2026-04-02"
MIN_HISTORY  = 60
TIMEOUT_DAYS = 7
FEE_PER_SIDE = 0.0003          # 3 bps
RSI_PERIOD   = 10
RVOL_PERIOD  = 20

CACHE_DIR    = "/tmp/cascade_bt_cache"

UNIVERSE_TOP3 = ["BTC", "ETH", "SOL"]
UNIVERSE_TOP20 = [
    "BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE", "AVAX",
    "TAO", "HYPE", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI",
]
# HL44 from CLAUDE.md; k-prefix HL contracts mapped to underlying (kSHIB->SHIB etc.)
UNIVERSE_HL44 = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "AVAX", "SUI",
    "DOT", "HBAR", "NEAR", "UNI", "LTC", "TAO", "HYPE", "AAVE", "TRUMP", "ONDO",
    "PAXG", "TON", "ICP", "ATOM", "RENDER", "FET", "ALGO", "WLD", "INJ", "SEI",
    "STX", "APT", "FIL", "JUP", "TRX", "OP", "ENA", "XLM", "ARB", "POL",
    "SHIB", "PEPE", "BONK", "FLOKI",
]


# ─── Filters ──────────────────────────────────────────────────

def vec_rsi(close: pd.Series, period: int = RSI_PERIOD) -> np.ndarray:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(loss != 0, 100.0)
    return rsi.values


def vec_rvol(volume: pd.Series, period: int = RVOL_PERIOD) -> np.ndarray:
    return (volume / volume.rolling(period).mean()).values


def vec_di(df: pd.DataFrame, period: int = 14) -> Tuple[np.ndarray, np.ndarray]:
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)
    if n < period + 1:
        z = np.zeros(n)
        return z, z

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        h_diff = high[i] - high[i - 1]
        l_diff = low[i - 1] - low[i]
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
        plus_dm[i]  = h_diff if (h_diff > l_diff and h_diff > 0) else 0.0
        minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0.0

    atr = np.zeros(n)
    sm_plus = np.zeros(n)
    sm_minus = np.zeros(n)
    atr[period]      = np.mean(tr[1:period + 1])
    sm_plus[period]  = np.mean(plus_dm[1:period + 1])
    sm_minus[period] = np.mean(minus_dm[1:period + 1])
    for i in range(period + 1, n):
        atr[i]      = (atr[i - 1] * (period - 1) + tr[i]) / period
        sm_plus[i]  = (sm_plus[i - 1] * (period - 1) + plus_dm[i]) / period
        sm_minus[i] = (sm_minus[i - 1] * (period - 1) + minus_dm[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus  = np.where(atr > 0, 100.0 * sm_plus  / atr, 0.0)
        di_minus = np.where(atr > 0, 100.0 * sm_minus / atr, 0.0)
    return di_plus, di_minus


# ─── Data layer ───────────────────────────────────────────────

@dataclass
class TokenData:
    sym: str
    df: pd.DataFrame
    rsi: np.ndarray
    rvol: np.ndarray
    di_plus: np.ndarray
    di_minus: np.ndarray
    sma40: np.ndarray
    # Per-day cache entry: li -> dict with price, atr, supports, resistances
    sr_by_li: Dict[int, dict]


def mt_not_downtrend_pass(close: float, sma40_now: float, sma40_prev: float) -> bool:
    """Hard gate per sr_backtest_report.md: block when price < SMA(40) AND SMA(40) is falling."""
    if np.isnan(sma40_now) or np.isnan(sma40_prev):
        return True   # no enough history → don't block
    is_below = close < sma40_now
    is_falling = sma40_now < sma40_prev
    return not (is_below and is_falling)


def load_tokens(symbols: List[str], data_dir: str) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_from_cache(sym, data_dir)
        if df is None or len(df) == 0:
            print(f"[load] WARN no cache for {sym}", file=sys.stderr)
            continue
        out[sym] = df
    return out


def precompute_sr_for_token(sym: str, df: pd.DataFrame,
                            start_li: int, end_li: int,
                            cache_dir: str, rebuild: bool) -> Dict[int, dict]:
    """Cache per-LOCAL-index analysis (price, atr, supports, resistances).
    Re-uses previous cache when (n, first, last, start, end) match."""
    path = os.path.join(cache_dir, f"{sym}.pkl")
    sig = (len(df), str(df.index[0]), str(df.index[-1]), start_li, end_li)
    if not rebuild and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            if payload.get("sig") == sig:
                return payload["sr"]
        except Exception:
            pass

    sr_by_li: Dict[int, dict] = {}
    t0 = time.time()
    for li in range(start_li, end_li + 1):
        history = df.iloc[: li + 1]
        if len(history) < MIN_HISTORY:
            continue
        try:
            analysis = analyze_token(sym, history)
        except Exception:
            continue
        ms = analysis.get("market_structure", {})
        sr_by_li[li] = {
            "price":    analysis.get("price"),
            "atr":      ms.get("atr14", 0),
            "supports":    analysis.get("support", []),
            "resistances": analysis.get("resistance", []),
        }
    dt = time.time() - t0
    print(f"[cache] {sym}: {len(sr_by_li)} days computed in {dt:.1f}s", file=sys.stderr)

    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump({"sig": sig, "sr": sr_by_li}, f)
    return sr_by_li


def build_token_data(sym: str, df: pd.DataFrame,
                     start_li: int, end_li: int,
                     cache_dir: str, rebuild: bool) -> TokenData:
    rsi = vec_rsi(df["close"], RSI_PERIOD)
    rvol = vec_rvol(df["volume"], RVOL_PERIOD)
    dip, dim = vec_di(df, 14)
    sma40 = df["close"].rolling(40).mean().values
    sr_by_li = precompute_sr_for_token(sym, df, start_li, end_li, cache_dir, rebuild)
    return TokenData(sym=sym, df=df, rsi=rsi, rvol=rvol,
                     di_plus=dip, di_minus=dim, sma40=sma40, sr_by_li=sr_by_li)


# ─── Cascade application ──────────────────────────────────────

def tpsl_from_sr(entry: dict,
                 tp_cascade: float, sl_cascade: float,
                 tp_min: float = 0.5, sl_min: float = 0.5) -> Tuple[Optional[float], Optional[float]]:
    """Apply cascade thresholds to cached supports/resistances. Returns (tp, sl) or (None, None)."""
    if entry is None:
        return None, None
    synth = {
        "symbol":  "X",
        "price":   entry["price"],
        "support":    entry["supports"],
        "resistance": entry["resistances"],
        "market_structure": {"atr14": entry["atr"]},
    }
    res = _compute_tp_sl_impl(
        synth,
        tp_cascade_atr=tp_cascade, sl_cascade_atr=sl_cascade,
        tp_min_atr=tp_min, sl_min_atr=sl_min,
    )
    if res is None:
        return None, None
    return res.get("take_profit"), res.get("stop_loss")


# ─── Filters ──────────────────────────────────────────────────

def filter_pass(combo: List[str], btc: TokenData, tdata: TokenData, li: int,
                btc_li: int) -> bool:
    """li is local index in `tdata`, btc_li is local in `btc`. Both align to same date."""
    if "btcrsi" in combo:
        v = btc.rsi[btc_li]
        if np.isnan(v) or v < 50.0:
            return False
    if "rsi" in combo:
        v = tdata.rsi[li]
        if np.isnan(v) or not (v > 60.0):
            return False
    if "rsicap" in combo:
        v = tdata.rsi[li]
        if np.isnan(v) or not (v <= 80.0):
            return False
    if "di" in combo:
        dp, dm = tdata.di_plus[li], tdata.di_minus[li]
        if np.isnan(dp) or np.isnan(dm) or not (dp > dm):
            return False
    if "rvol" in combo:
        v = tdata.rvol[li]
        if np.isnan(v) or v < 1.5:
            return False
    return True


# ─── Simulator ────────────────────────────────────────────────

@dataclass
class Trade:
    sym: str
    entry_date: str
    entry_price: float
    tp: float
    sl: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct_gross: float = 0.0
    pnl_pct_net: float = 0.0


def run_single_slot(
    period_dates: pd.DatetimeIndex,
    tokens: Dict[str, TokenData],
    btc: TokenData,
    combo: List[str],
    tiebreaker: str,                 # 'rsi' or 'rr'
    cascade: Tuple[float, float],    # (tp_cascade, sl_cascade)
) -> Tuple[List[Trade], float, int, int, int]:
    """Single-slot portfolio. Returns (trades, final_equity, n_qual_signals, n_trades_taken, n_skipped_in_position)."""
    cash = 1.0
    open_t: Optional[Trade] = None
    pending: Optional[Tuple[str, float, float]] = None   # (sym, tp, sl)
    trades: List[Trade] = []
    n_qual_signals = 0
    n_skipped_in_position = 0

    tp_c, sl_c = cascade

    # Pre-compute per-token date->local-li lookup
    sym_date_to_li: Dict[str, Dict[pd.Timestamp, int]] = {}
    for sym, td in tokens.items():
        sym_date_to_li[sym] = {ts: i for i, ts in enumerate(td.df.index)}
    btc_date_to_li = {ts: i for i, ts in enumerate(btc.df.index)}

    # Walk daily
    for d_idx, ts in enumerate(period_dates):
        # ── Fill pending entry at this day's open ──
        if pending is not None:
            sym, tp, sl = pending
            td = tokens[sym]
            li = sym_date_to_li[sym].get(ts)
            if li is not None:
                entry_open = float(td.df["open"].iloc[li])
                if not np.isnan(entry_open) and entry_open > 0:
                    open_t = Trade(sym=sym, entry_date=str(ts.date()),
                                   entry_price=entry_open, tp=tp, sl=sl)
            pending = None

        # ── Manage open position (TP / SL / TIMEOUT) ──
        if open_t is not None:
            td = tokens[open_t.sym]
            li = sym_date_to_li[open_t.sym].get(ts)
            if li is not None:
                hi = float(td.df["high"].iloc[li])
                lo = float(td.df["low"].iloc[li])
                cl = float(td.df["close"].iloc[li])
                tp_hit = hi >= open_t.tp
                sl_hit = lo <= open_t.sl
                hold = (ts - pd.Timestamp(open_t.entry_date, tz="UTC")).days

                exit_reason, exit_price = "", 0.0
                if tp_hit and sl_hit:
                    exit_reason, exit_price = "SL", open_t.sl
                elif tp_hit:
                    exit_reason, exit_price = "TP", open_t.tp
                elif sl_hit:
                    exit_reason, exit_price = "SL", open_t.sl
                elif hold >= TIMEOUT_DAYS:
                    exit_reason, exit_price = "TIMEOUT", cl

                if exit_reason:
                    open_t.exit_date = str(ts.date())
                    open_t.exit_price = exit_price
                    open_t.exit_reason = exit_reason
                    gross = (exit_price - open_t.entry_price) / open_t.entry_price
                    open_t.pnl_pct_gross = gross * 100.0
                    net = (1.0 - FEE_PER_SIDE) * (1.0 + gross) * (1.0 - FEE_PER_SIDE) - 1.0
                    open_t.pnl_pct_net = net * 100.0
                    cash *= (1.0 + net)
                    trades.append(open_t)
                    open_t = None

        # ── Generate signals (close-of-day) ──
        candidates: List[Tuple[str, float, float, float, float]] = []
        btc_li = btc_date_to_li.get(ts)
        if btc_li is None:
            continue

        for sym, td in tokens.items():
            li = sym_date_to_li[sym].get(ts)
            if li is None:
                continue
            sr_entry = td.sr_by_li.get(li)
            if sr_entry is None:
                continue
            # Hard gate: mt_not_downtrend (always-on per sr_backtest_report.md)
            close_now = float(td.df["close"].iloc[li])
            sma_now = td.sma40[li]
            sma_prev = td.sma40[li - 1] if li > 0 else np.nan
            if not mt_not_downtrend_pass(close_now, sma_now, sma_prev):
                continue
            if not filter_pass(combo, btc, td, li, btc_li):
                continue
            tp, sl = tpsl_from_sr(sr_entry, tp_c, sl_c)
            if tp is None or sl is None:
                continue
            cur = sr_entry["price"]
            if not (tp > cur > sl):
                continue
            rsi_val = td.rsi[li]
            if np.isnan(rsi_val):
                continue
            rr = (tp - cur) / (cur - sl) if (cur - sl) > 0 else 0.0
            candidates.append((sym, rsi_val, rr, tp, sl))

        n_qual_signals += len(candidates)

        if open_t is not None or pending is not None:
            if candidates:
                n_skipped_in_position += 1
            continue
        if not candidates:
            continue

        if tiebreaker == "rsi":
            candidates.sort(key=lambda c: c[1], reverse=True)
        else:
            candidates.sort(key=lambda c: c[2], reverse=True)
        pick = candidates[0]
        pending = (pick[0], pick[3], pick[4])

    # ── Force-close at end ──
    if open_t is not None:
        last_ts = period_dates[-1]
        td = tokens[open_t.sym]
        li = sym_date_to_li[open_t.sym].get(last_ts)
        if li is None:
            li = len(td.df) - 1
        cl = float(td.df["close"].iloc[li])
        open_t.exit_date = str(last_ts.date())
        open_t.exit_price = cl
        open_t.exit_reason = "OPEN"
        gross = (cl - open_t.entry_price) / open_t.entry_price
        open_t.pnl_pct_gross = gross * 100.0
        net = (1.0 - FEE_PER_SIDE) * (1.0 + gross) * (1.0 - FEE_PER_SIDE) - 1.0
        open_t.pnl_pct_net = net * 100.0
        cash *= (1.0 + net)
        trades.append(open_t)

    n_taken = len(trades)
    return trades, cash, n_qual_signals, n_taken, n_skipped_in_position


# ─── Reporting ────────────────────────────────────────────────

def stats(trades: List[Trade], final_cash: float) -> Dict[str, float]:
    n = len(trades)
    if n == 0:
        return {"n": 0, "pnl_pct": 0.0, "tp_rate": 0.0, "sl_rate": 0.0,
                "to_rate": 0.0, "max_dd": 0.0, "avg_pnl": 0.0}
    n_tp = sum(1 for t in trades if t.exit_reason == "TP")
    n_sl = sum(1 for t in trades if t.exit_reason == "SL")
    n_to = sum(1 for t in trades if t.exit_reason in ("TIMEOUT", "OPEN"))
    pnl_pct = (final_cash - 1.0) * 100.0

    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for t in trades:
        eq *= (1.0 + t.pnl_pct_net / 100.0)
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak * 100.0
        if dd < max_dd:
            max_dd = dd
    avg_pnl = np.mean([t.pnl_pct_net for t in trades])
    return {
        "n": n,
        "pnl_pct": pnl_pct,
        "tp_rate": n_tp / n * 100.0,
        "sl_rate": n_sl / n * 100.0,
        "to_rate": n_to / n * 100.0,
        "max_dd": max_dd,
        "avg_pnl": avg_pnl,
    }


def print_block(title: str, base: dict, asym: dict,
                base_qual: int, asym_qual: int):
    print()
    print(f"=== {title}")
    hdr = f"{'cascade':<14}{'trades':>8}{'pnl%':>10}{'maxDD%':>10}{'TP%':>8}{'SL%':>8}{'TO%':>8}{'avgPnL%':>10}{'qual_sigs':>11}"
    print(hdr)
    print("-" * len(hdr))
    for label, s, q in [("baseline 1.0/1.0", base, base_qual),
                         ("asym    0.75/1.0", asym, asym_qual)]:
        print(f"{label:<14}{s['n']:>8}{s['pnl_pct']:>+10.1f}{s['max_dd']:>+10.1f}"
              f"{s['tp_rate']:>8.1f}{s['sl_rate']:>8.1f}{s['to_rate']:>8.1f}"
              f"{s['avg_pnl']:>+10.2f}{q:>11}")
    delta_pnl = asym["pnl_pct"] - base["pnl_pct"]
    delta_dd  = asym["max_dd"] - base["max_dd"]
    print(f"  Δ asym − base:  pnl {delta_pnl:+.1f}pp   maxDD {delta_dd:+.1f}pp"
          f"   trades {asym['n']-base['n']:+d}")


# ─── Main ─────────────────────────────────────────────────────

UNIVERSES = {
    "top3":  ("Top 3 (BTC, ETH, SOL)",        UNIVERSE_TOP3,  ["di"],                       "rr"),
    "top20": ("Top 20 (17 tokens)",            UNIVERSE_TOP20, ["btcrsi", "rsicap"],         "rsi"),
    "hl44":  ("HL44 (44 tokens)",              UNIVERSE_HL44,  ["btcrsi", "rsi", "rvol", "rsicap"], "rsi"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="Force re-run S/R analysis even if cache is fresh.")
    parser.add_argument("--only", choices=["top3", "top20", "hl44"], default=None,
                        help="Run only one universe.")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    # Load BTC first (needed for filters)
    print(f"\n=== cascade comparison: 1.0/1.0 vs 0.75/1.0", file=sys.stderr)
    print(f"=== period: {PERIOD_START} -> {PERIOD_END}", file=sys.stderr)
    print(f"=== fee: {FEE_PER_SIDE*1e4:.1f} bps/side  timeout: {TIMEOUT_DAYS}d  min hist: {MIN_HISTORY}", file=sys.stderr)

    all_syms = sorted(set(UNIVERSE_TOP3 + UNIVERSE_TOP20 + UNIVERSE_HL44))
    if args.only:
        _, syms_in_uni, _, _ = UNIVERSES[args.only]
        all_syms = sorted(set(syms_in_uni + ["BTC"]))

    raw = load_tokens(all_syms, args.data_dir)
    if "BTC" not in raw:
        print("ERROR: BTC data missing", file=sys.stderr); sys.exit(1)

    # Period bounds, derived from BTC index
    period_start_ts = pd.Timestamp(PERIOD_START, tz="UTC")
    period_end_ts   = pd.Timestamp(PERIOD_END,   tz="UTC")
    btc_df = raw["BTC"]
    period_dates = btc_df.index[(btc_df.index >= period_start_ts) & (btc_df.index <= period_end_ts)]
    print(f"=== {len(period_dates)} bars in period\n", file=sys.stderr)

    # Build TokenData (with sr cache) for every needed token
    td_all: Dict[str, TokenData] = {}
    for sym, df in raw.items():
        # Determine local indices to compute analysis for: the in-period window
        in_period_idx = df.index[(df.index >= period_start_ts) & (df.index <= period_end_ts)]
        if len(in_period_idx) == 0:
            continue
        first_li = df.index.get_loc(in_period_idx[0])
        last_li  = df.index.get_loc(in_period_idx[-1])
        td_all[sym] = build_token_data(sym, df, first_li, last_li, CACHE_DIR, args.rebuild_cache)

    btc = td_all["BTC"]

    # Run each universe with both cascade variants
    universes_to_run = [args.only] if args.only else ["top3", "top20", "hl44"]
    for uni_key in universes_to_run:
        title, syms, combo, tiebreaker = UNIVERSES[uni_key]
        tokens = {s: td_all[s] for s in syms if s in td_all}
        if "BTC" not in tokens:
            tokens["BTC"] = btc
        print(f"[run] {title}: {len(tokens)} tokens, filter={'+'.join(combo)}, sel={tiebreaker}", file=sys.stderr)

        base_trades, base_cash, base_qual, _, _ = run_single_slot(
            period_dates, tokens, btc, combo, tiebreaker, (1.0, 1.0))
        asym_trades, asym_cash, asym_qual, _, _ = run_single_slot(
            period_dates, tokens, btc, combo, tiebreaker, (1.0, 0.75))

        print_block(
            f"{title} | filter={'+'.join(combo)} | sel={tiebreaker}",
            stats(base_trades, base_cash),
            stats(asym_trades, asym_cash),
            base_qual, asym_qual,
        )

    print()


if __name__ == "__main__":
    main()
