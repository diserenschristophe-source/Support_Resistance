#!/usr/bin/env python3
"""
backtest_intraday.py — Intraday TA backtest at 1h or 4h cadence.
================================================================

Models the live ta / ta17 agents at higher than daily cadence.

Design choices:
  - OHLCV: hourly from data/hourly/*_hourly.csv (4h cadence aggregates 4 bars).
  - Filters: live config per universe — single combo, not a sweep.
      * mt_not_downtrend, relative_volume   → DAILY (frozen at yesterday's close)
      * btc_rsi_floor, token_rsi_momentum,
        rsi_cap, selection-RSI              → PARTIAL-BAR DAILY:
            RSI input = [9 closed daily closes] + [current hourly close]
  - SR levels: analyze_token on daily history through D-1 close. compute_tp_sl
    is re-run at each scan with current_price overridden, so TP/SL pick the
    nearest level relative to where price is now.
  - Selection: top-RSI single attempt, single slot, no cooldown (matches live).
  - Timeout: 7 calendar days from entry timestamp.

CLI:
  python3 research/backtest_intraday.py --cadence 1h --universe selected17
  python3 research/backtest_intraday.py --cadence 4h --universe selected17
  python3 research/backtest_intraday.py --all                # all 4 cells

Outputs (under reports/portfolio_backtest_intraday/):
  sweep_<UTC>.md
  {universe}_{cadence}_trades.csv
  .sr_cache_full_<universe>.pkl   (re-buildable; full analyze_token results)
"""
from __future__ import annotations

import argparse
import logging
import os
import pickle
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import talib

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.sr_analysis2 import analyze_token  # noqa: E402
from core.tpsl import compute_tp_sl  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest_intraday")

INITIAL_CAPITAL = 100_000.0
TRADING_FEE = 0.0003       # 3 bps each side
MAX_HOLD_DAYS = 7
MIN_DAILY_HISTORY = 60     # bars before first signal
RSI_PERIOD = 10
RSI_HISTORY_BARS = 60      # daily bars used to compute partial-bar RSI

DATA_DIR = ROOT / "data" / "hourly"
REPORT_DIR = ROOT / "reports" / "portfolio_backtest_intraday"


# ─────────────────────────────────────────────────────────────────────────────
# Universe + filter configs (mirror live ta / ta17 configs)
# ─────────────────────────────────────────────────────────────────────────────

UNIVERSES = {
    "selected17": {
        "agent": "ta17",
        "tokens": [
            "BTC", "ETH", "XRP", "SOL", "ADA", "LINK", "SUI", "AAVE", "AVAX",
            "TAO", "HYPE", "DOGE", "BNB", "HBAR", "DOT", "NEAR", "UNI",
        ],
        "filters": {
            "mt_not_downtrend":   {"scope": "daily",   "sma_period": 40, "slope_bars": 20},
            "btc_rsi_floor":      {"scope": "intraday", "period": RSI_PERIOD, "threshold": 50.0},
            "rsi_cap":            {"scope": "intraday", "period": RSI_PERIOD, "threshold": 80.0},
        },
    },
    "hl44": {
        "agent": "ta",
        "tokens": [
            "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK",
            "AVAX", "SUI", "DOT", "HBAR", "NEAR", "UNI", "LTC", "TAO",
            "HYPE", "AAVE", "TRUMP", "ONDO", "PAXG", "TON", "ICP", "ATOM",
            "RENDER", "FET", "ALGO", "WLD", "INJ", "SEI", "STX", "APT",
            "FIL", "JUP", "TRX", "OP", "ENA", "XLM", "ARB", "POL",
            "SHIB", "PEPE", "BONK", "FLOKI",
        ],
        "filters": {
            "mt_not_downtrend":   {"scope": "daily",   "sma_period": 40, "slope_bars": 20},
            "btc_rsi_floor":      {"scope": "intraday", "period": RSI_PERIOD, "threshold": 50.0},
            "token_rsi_momentum": {"scope": "intraday", "period": RSI_PERIOD, "threshold": 60.0},
            "relative_volume":    {"scope": "daily",   "period": 20, "threshold": 1.5},
            "rsi_cap":            {"scope": "intraday", "period": RSI_PERIOD, "threshold": 80.0},
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Trade dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    entry_ts: pd.Timestamp
    entry_price: float
    take_profit: float
    stop_loss: float
    raw_rr: float
    rsi_at_entry: float
    size_usd: float
    exit_ts: pd.Timestamp | None = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    hold_hours: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def load_hourly(tokens: list[str]) -> dict[str, pd.DataFrame]:
    """Load *_hourly.csv for each token; return only those that exist."""
    out: dict[str, pd.DataFrame] = {}
    for tok in tokens:
        p = DATA_DIR / f"{tok}_hourly.csv"
        if not p.exists():
            log.warning("hourly CSV missing: %s", p.name)
            continue
        df = pd.read_csv(p)
        df.columns = [c.strip().lower() for c in df.columns]
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        if "volume" not in df.columns:
            df["volume"] = 1.0
        out[tok] = df
    return out


def aggregate_to_4h(hourly: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """1h -> 4h with proper OHLCV aggregation, aligned to UTC 00/04/08/12/16/20."""
    out = {}
    for sym, df in hourly.items():
        agg = df.resample("4h", origin="start_day", label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna()
        out[sym] = agg
    return out


def aggregate_to_daily(hourly: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """1h -> daily for daily-scope filters and SR analysis."""
    out = {}
    for sym, df in hourly.items():
        agg = df.resample("1D", label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna()
        out[sym] = agg
    return out


# ─────────────────────────────────────────────────────────────────────────────
# SR cache (full analyze_token results, keyed by (sym, daily_date))
# ─────────────────────────────────────────────────────────────────────────────

def build_sr_cache_full(
    daily_ohlcv: dict[str, pd.DataFrame],
    cache_path: Path,
) -> dict[tuple[str, str], dict | None]:
    """One analyze_token() result per (sym, date), where date is the date AT WHICH
    the analysis is computed (i.e., using daily history through that date's close)."""
    if cache_path.exists():
        log.info("Loading SR cache from %s", cache_path)
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    log.info("Building full SR cache (slow on first run)...")
    cache: dict[tuple[str, str], dict | None] = {}
    total = sum(max(0, len(df) - MIN_DAILY_HISTORY) for df in daily_ohlcv.values())
    done = 0
    t0 = time.time()

    for sym, df in daily_ohlcv.items():
        n = len(df)
        for i in range(MIN_DAILY_HISTORY, n):
            date_str = str(df.index[i])[:10]
            history = df.iloc[:i + 1]   # through date i's close
            try:
                analysis = analyze_token(sym, history)
                # Strip down: keep only what compute_tp_sl needs
                cache[(sym, date_str)] = {
                    "symbol": analysis.get("symbol", sym),
                    "price": analysis.get("price"),
                    "support": analysis.get("support", []),
                    "resistance": analysis.get("resistance", []),
                    "market_structure": analysis.get("market_structure", {}),
                    "volume_profile": analysis.get("volume_profile"),
                }
            except Exception as e:
                cache[(sym, date_str)] = None
                if done < 5:
                    log.warning("analyze_token failed for %s @ %s: %s", sym, date_str, e)

            done += 1
            if done % 200 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate / 60 if rate else 0
                log.info("  SR %d/%d (%.0f%%) ETA %.0fm  [%s]",
                         done, total, 100 * done / total, eta, sym)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(cache, f)
    log.info("SR cache saved: %d entries -> %s", len(cache), cache_path)
    return cache


# ─────────────────────────────────────────────────────────────────────────────
# Daily filters (precomputed once, looked up per-day)
# ─────────────────────────────────────────────────────────────────────────────

def daily_mt_not_downtrend_mask(
    daily_close: pd.DataFrame, sma_period: int, slope_bars: int
) -> pd.DataFrame:
    """True where NOT in clear downtrend (matches trading-system filter)."""
    sma = daily_close.rolling(sma_period).mean()
    below = daily_close < sma
    falling = sma.diff(slope_bars) < 0
    downtrend = below & falling
    return ~downtrend


def daily_relative_volume_mask(
    daily_volume: pd.DataFrame, period: int, threshold: float
) -> pd.DataFrame:
    avg = daily_volume.rolling(period).mean().replace(0, np.nan)
    rvol = daily_volume / avg
    return rvol >= threshold


# ─────────────────────────────────────────────────────────────────────────────
# Partial-bar RSI: [closed daily closes] + [current intraday price]
# ─────────────────────────────────────────────────────────────────────────────

def partial_bar_rsi(
    daily_closes_through_yday: np.ndarray,
    current_intraday_price: float,
    period: int = RSI_PERIOD,
) -> float:
    """Compute RSI using closed daily closes plus today's in-progress close.

    Returns NaN if the series is too short.
    """
    if len(daily_closes_through_yday) < period + 1:
        return float("nan")
    series = np.concatenate([daily_closes_through_yday, [current_intraday_price]]).astype(float)
    rsi = talib.RSI(series, period)
    return float(rsi[-1])


# ─────────────────────────────────────────────────────────────────────────────
# Single-cell engine
# ─────────────────────────────────────────────────────────────────────────────

def run_cell(
    universe_name: str,
    cadence: str,
    hourly: dict[str, pd.DataFrame],
    sr_cache: dict[tuple[str, str], dict | None],
    daily_ohlcv: dict[str, pd.DataFrame],
) -> tuple[list[Trade], dict]:
    """Run one (universe, cadence) cell. Returns (trades, run_info)."""
    uconf = UNIVERSES[universe_name]
    fconf = uconf["filters"]
    available_tokens = [t for t in uconf["tokens"] if t in hourly]
    missing = [t for t in uconf["tokens"] if t not in hourly]

    log.info("=" * 70)
    log.info("CELL  universe=%s  cadence=%s", universe_name, cadence)
    log.info("  available: %d/%d tokens; missing: %s",
             len(available_tokens), len(uconf["tokens"]),
             missing if missing else "—")

    if "BTC" not in hourly:
        log.error("BTC hourly missing — cannot run btc_rsi_floor; aborting cell")
        return [], {"error": "BTC hourly missing"}

    if cadence == "4h":
        bars = aggregate_to_4h(hourly)
        cadence_step_seconds = 4 * 3600
    elif cadence == "1h":
        bars = hourly
        cadence_step_seconds = 3600
    else:
        raise ValueError(f"unsupported cadence: {cadence}")

    # Common bar index across available tokens
    common_idx = bars[available_tokens[0]].index
    for sym in available_tokens[1:]:
        common_idx = common_idx.intersection(bars[sym].index)
    common_idx = common_idx.sort_values()
    if len(common_idx) < MIN_DAILY_HISTORY * (24 if cadence == "1h" else 6):
        log.error("Not enough common bars (%d) — aborting", len(common_idx))
        return [], {"error": "not enough common bars"}

    log.info("  common bars: %d  (%s -> %s)",
             len(common_idx),
             str(common_idx[0])[:16], str(common_idx[-1])[:16])

    # Daily series (close, volume) for daily filters and partial-bar RSI history
    daily_close_df = pd.DataFrame(
        {s: d["close"] for s, d in daily_ohlcv.items() if s in available_tokens}
    ).sort_index()
    daily_volume_df = pd.DataFrame(
        {s: d["volume"] for s, d in daily_ohlcv.items() if s in available_tokens}
    ).sort_index()

    # Daily filter masks (precomputed once)
    daily_masks: dict[str, pd.DataFrame] = {}
    if "mt_not_downtrend" in fconf:
        c = fconf["mt_not_downtrend"]
        daily_masks["mt_not_downtrend"] = daily_mt_not_downtrend_mask(
            daily_close_df, c["sma_period"], c["slope_bars"]
        )
    if "relative_volume" in fconf:
        c = fconf["relative_volume"]
        daily_masks["relative_volume"] = daily_relative_volume_mask(
            daily_volume_df, c["period"], c["threshold"]
        )

    # Numpy daily-close series per token for fast partial-bar RSI lookups
    daily_close_np = {s: daily_close_df[s].to_numpy(dtype=float) for s in available_tokens}
    daily_index = daily_close_df.index  # DatetimeIndex of daily bars

    # Helper: find the last daily index strictly BEFORE a given timestamp
    def last_closed_daily_idx(ts: pd.Timestamp) -> int:
        # daily bar at date X represents [X 00:00, X+1 00:00). Closed once we pass X+1 00:00.
        # We treat "yesterday" as the most recent date strictly before ts.date().
        target_date = ts.normalize() - pd.Timedelta(days=1)
        idxs = daily_index.searchsorted(target_date, side="right") - 1
        return int(idxs)

    # ── Trade loop ──────────────────────────────────────────────────────────
    trades: list[Trade] = []
    open_trade: Trade | None = None
    pending: tuple | None = None
    capital = INITIAL_CAPITAL
    skipped_no_btc_rsi = 0
    skipped_daily_filter = 0
    skipped_no_eligible = 0
    skipped_sr = 0
    skipped_sanity = 0

    cadence_seconds = cadence_step_seconds
    n = len(common_idx)
    btc_bars = bars["BTC"]

    for hi in range(n):
        ts: pd.Timestamp = common_idx[hi]

        # 1. Fill pending entry at open(this bar)
        if pending and open_trade is None:
            sym, tp, sl, raw_rr, rsi_val = pending
            entry_price = float(bars[sym]["open"].loc[ts])
            fee = capital * TRADING_FEE
            open_trade = Trade(
                symbol=sym, entry_ts=ts, entry_price=entry_price,
                take_profit=tp, stop_loss=sl, raw_rr=raw_rr,
                rsi_at_entry=rsi_val, size_usd=capital - fee,
            )
            capital = 0.0
        pending = None

        # 2. Check exit on open position (every bar, regardless of cadence)
        if open_trade is not None:
            sym = open_trade.symbol
            bar = bars[sym].loc[ts]
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            elapsed_days = (ts - open_trade.entry_ts).total_seconds() / 86400
            open_trade.hold_hours = int((ts - open_trade.entry_ts).total_seconds() / 3600)

            tp_hit = high >= open_trade.take_profit
            sl_hit = low <= open_trade.stop_loss

            exit_reason = None
            exit_price = None
            if tp_hit and sl_hit:
                exit_reason, exit_price = "SL", open_trade.stop_loss
            elif sl_hit:
                exit_reason, exit_price = "SL", open_trade.stop_loss
            elif tp_hit:
                exit_reason, exit_price = "TP", open_trade.take_profit
            elif elapsed_days >= MAX_HOLD_DAYS:
                exit_reason, exit_price = "TIMEOUT", close

            if exit_reason:
                open_trade.exit_ts = ts
                open_trade.exit_price = exit_price
                open_trade.exit_reason = exit_reason
                open_trade.pnl_pct = (
                    (exit_price - open_trade.entry_price)
                    / open_trade.entry_price * 100
                )
                open_trade.pnl_usd = open_trade.size_usd * open_trade.pnl_pct / 100
                exit_fee = (open_trade.size_usd + open_trade.pnl_usd) * TRADING_FEE
                capital = open_trade.size_usd + open_trade.pnl_usd - exit_fee
                trades.append(open_trade)
                open_trade = None

        # 3. Generate signal — only at cadence boundary
        if open_trade is not None or hi >= n - 1:
            continue
        # 4h cadence: only scan at UTC hours 00/04/08/12/16/20
        if cadence == "4h" and ts.hour % 4 != 0:
            continue

        # Latest closed daily index (strictly before ts.date())
        d_idx = last_closed_daily_idx(ts)
        if d_idx < MIN_DAILY_HISTORY:
            continue
        d_date_str = str(daily_index[d_idx])[:10]

        # ── BTC RSI floor (intraday partial-bar) ──
        if "btc_rsi_floor" in fconf:
            c = fconf["btc_rsi_floor"]
            btc_hist = daily_close_np["BTC"][:d_idx + 1][-RSI_HISTORY_BARS:]
            btc_now = float(btc_bars["close"].loc[ts])
            btc_rsi = partial_bar_rsi(btc_hist, btc_now, c["period"])
            if np.isnan(btc_rsi) or btc_rsi < c["threshold"]:
                skipped_no_btc_rsi += 1
                continue

        # ── Build candidates ──
        # Daily filters: token must pass on day d_idx
        eligible_tokens = []
        for sym in available_tokens:
            if sym not in daily_close_np:
                continue
            ok = True
            for fname, mask_df in daily_masks.items():
                if sym in mask_df.columns:
                    val = mask_df.iloc[d_idx].get(sym, False)
                    if not bool(val):
                        ok = False
                        break
            if ok:
                eligible_tokens.append(sym)

        if not eligible_tokens:
            skipped_daily_filter += 1
            continue

        # Intraday filters: token_rsi_momentum, rsi_cap (per token, partial-bar)
        candidates = []  # (sym, intraday_rsi)
        for sym in eligible_tokens:
            if sym not in bars:
                continue
            sym_hist = daily_close_np[sym][:d_idx + 1][-RSI_HISTORY_BARS:]
            sym_now = float(bars[sym]["close"].loc[ts])
            rsi = partial_bar_rsi(sym_hist, sym_now, RSI_PERIOD)
            if np.isnan(rsi):
                continue
            ok = True
            if "token_rsi_momentum" in fconf:
                if rsi <= fconf["token_rsi_momentum"]["threshold"]:
                    ok = False
            if ok and "rsi_cap" in fconf:
                if rsi > fconf["rsi_cap"]["threshold"]:
                    ok = False
            if ok:
                candidates.append((sym, rsi, sym_now))

        if not candidates:
            skipped_no_eligible += 1
            continue

        # Top-RSI single attempt (matches max_attempts=1 in live)
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_sym, best_rsi, best_price = candidates[0]

        # ── SR / TP-SL reselection at intraday price ──
        sr_entry = sr_cache.get((best_sym, d_date_str))
        if sr_entry is None:
            skipped_sr += 1
            continue

        analysis = dict(sr_entry)            # shallow copy
        analysis["price"] = best_price       # override with current intraday price
        result = compute_tp_sl(analysis)
        if not result or not result.get("qualified"):
            skipped_sr += 1
            continue

        tp = result.get("take_profit")
        sl = result.get("stop_loss")
        raw_rr = result.get("raw_rr") or 0.0
        if tp is None or sl is None or tp <= best_price or sl >= best_price:
            skipped_sanity += 1
            continue

        pending = (best_sym, float(tp), float(sl), float(raw_rr), float(best_rsi))

    # Close remaining open trade at last bar
    if open_trade is not None:
        sym = open_trade.symbol
        last_ts = common_idx[-1]
        last_close = float(bars[sym]["close"].loc[last_ts])
        open_trade.exit_ts = last_ts
        open_trade.exit_price = last_close
        open_trade.exit_reason = "OPEN"
        open_trade.pnl_pct = (
            (last_close - open_trade.entry_price) / open_trade.entry_price * 100
        )
        open_trade.pnl_usd = open_trade.size_usd * open_trade.pnl_pct / 100
        trades.append(open_trade)

    info = {
        "universe": universe_name,
        "cadence": cadence,
        "tokens_used": available_tokens,
        "tokens_missing": missing,
        "common_bars": int(len(common_idx)),
        "first_bar": str(common_idx[0]),
        "last_bar": str(common_idx[-1]),
        "skipped_no_btc_rsi": int(skipped_no_btc_rsi),
        "skipped_daily_filter": int(skipped_daily_filter),
        "skipped_no_eligible": int(skipped_no_eligible),
        "skipped_sr": int(skipped_sr),
        "skipped_sanity": int(skipped_sanity),
    }
    return trades, info


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(trades: list[Trade], initial_capital: float) -> dict:
    if not trades:
        return {"n_trades": 0}

    n = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    n_tp = sum(1 for t in trades if t.exit_reason == "TP")
    n_sl = sum(1 for t in trades if t.exit_reason == "SL")
    n_to = sum(1 for t in trades if t.exit_reason == "TIMEOUT")
    n_open = sum(1 for t in trades if t.exit_reason == "OPEN")

    equity = [initial_capital]
    for t in trades:
        prev = equity[-1]
        equity.append(prev * (1 + t.pnl_pct / 100))
    equity = np.array(equity)
    returns = np.diff(equity) / equity[:-1]

    total_return = (equity[-1] / equity[0] - 1) * 100
    if len(returns) > 1 and returns.std() > 0:
        sharpe_per_trade = returns.mean() / returns.std() * np.sqrt(len(returns))
        downside = returns[returns < 0]
        sortino = returns.mean() / downside.std() * np.sqrt(len(returns)) if len(downside) > 1 else float("nan")
    else:
        sharpe_per_trade = float("nan")
        sortino = float("nan")

    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak * 100
    max_dd = float(dd.min()) if len(dd) else 0.0

    span_days = (trades[-1].exit_ts - trades[0].entry_ts).total_seconds() / 86400 if n >= 1 else 1
    cagr = ((equity[-1] / equity[0]) ** (365 / max(span_days, 1)) - 1) * 100 if span_days > 0 else 0
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")

    win_pcts = [t.pnl_pct for t in wins]
    loss_pcts = [t.pnl_pct for t in losses]
    profit_factor = (
        sum(win_pcts) / abs(sum(loss_pcts)) if loss_pcts and sum(loss_pcts) < 0 else float("nan")
    )

    avg_hold_h = float(np.mean([t.hold_hours for t in trades])) if trades else 0
    avg_rr = float(np.mean([t.raw_rr for t in trades])) if trades else 0

    return {
        "n_trades": n,
        "n_tp": n_tp, "n_sl": n_sl, "n_timeout": n_to, "n_open": n_open,
        "tp_rate": round(100 * n_tp / n, 2),
        "sl_rate": round(100 * n_sl / n, 2),
        "timeout_rate": round(100 * n_to / n, 2),
        "win_rate_pct": round(100 * len(wins) / n, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "sharpe": round(sharpe_per_trade, 2) if not np.isnan(sharpe_per_trade) else None,
        "sortino": round(sortino, 2) if not np.isnan(sortino) else None,
        "calmar": round(calmar, 2) if not np.isnan(calmar) else None,
        "max_dd_pct": round(max_dd, 2),
        "avg_win_pct": round(np.mean(win_pcts), 2) if win_pcts else 0,
        "avg_loss_pct": round(np.mean(loss_pcts), 2) if loss_pcts else 0,
        "profit_factor": round(profit_factor, 2) if not np.isnan(profit_factor) else None,
        "avg_hold_hours": round(avg_hold_h, 1),
        "avg_rr": round(avg_rr, 2),
        "span_days": round(span_days, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def render_report(results: list[dict], started: datetime, finished: datetime) -> str:
    L = []
    L.append(f"# Intraday TA backtest — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append(f"- Started: {started.isoformat(timespec='seconds')}")
    L.append(f"- Finished: {finished.isoformat(timespec='seconds')}")
    L.append(f"- Wall time: {(finished - started).total_seconds() / 60:.1f} min")
    L.append("")
    L.append("Filter scopes:")
    L.append("- `mt_not_downtrend`, `relative_volume`: **daily** (frozen at yesterday's close)")
    L.append("- `btc_rsi_floor`, `token_rsi_momentum`, `rsi_cap`, selection-RSI: **partial-bar daily**")
    L.append("  (RSI input = 9 closed daily closes + current intraday close)")
    L.append("")
    L.append("TP/SL reselection: `compute_tp_sl` re-run at each scan with current intraday price.")
    L.append("")

    for r in results:
        u = r["universe"]
        c = r["cadence"]
        L.append(f"## {u} — cadence {c}  →  live agent `{r.get('agent', '?')}`")
        L.append("")

        info = r.get("info", {})
        if "error" in info:
            L.append(f"**ERROR:** {info['error']}")
            L.append("")
            continue

        L.append(f"- Tokens used: **{len(info.get('tokens_used', []))}** of {len(UNIVERSES[u]['tokens'])}")
        if info.get("tokens_missing"):
            L.append(f"- Missing tokens: `{info['tokens_missing']}`")
        L.append(f"- Bars: {info.get('common_bars', 0)}  ({info.get('first_bar','?')[:16]} → {info.get('last_bar','?')[:16]})")
        L.append("")

        m = r.get("metrics", {})
        if not m or m.get("n_trades", 0) == 0:
            L.append("**No trades** — see skip counters below.")
            L.append("")
        else:
            L.append("**Metrics**")
            L.append("")
            L.append(f"- Trades: {m['n_trades']}  (TP {m['n_tp']}, SL {m['n_sl']}, timeout {m['n_timeout']}, open {m['n_open']})")
            L.append(f"- Total return: **{m['total_return_pct']:+.1f}%**")
            L.append(f"- CAGR: {m['cagr_pct']:+.1f}%")
            L.append(f"- Sharpe: {m.get('sharpe')}")
            L.append(f"- Sortino: {m.get('sortino')}")
            L.append(f"- Calmar: {m.get('calmar')}")
            L.append(f"- Max DD: {m['max_dd_pct']:.1f}%")
            L.append(f"- Win rate: {m['win_rate_pct']:.1f}%  (TP {m['tp_rate']:.1f}%, SL {m['sl_rate']:.1f}%, timeout {m['timeout_rate']:.1f}%)")
            L.append(f"- Avg win / avg loss: {m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%")
            L.append(f"- Profit factor: {m.get('profit_factor')}")
            L.append(f"- Avg hold: {m['avg_hold_hours']} hours  ({m['avg_hold_hours']/24:.1f} days)")
            L.append(f"- Avg R:R at entry: {m['avg_rr']}")
            L.append("")

        L.append("**Skip counters**")
        L.append("")
        L.append(f"- BTC RSI floor blocked: {info.get('skipped_no_btc_rsi', 0)}")
        L.append(f"- Daily filter blocked all tokens: {info.get('skipped_daily_filter', 0)}")
        L.append(f"- No intraday-eligible candidate: {info.get('skipped_no_eligible', 0)}")
        L.append(f"- SR not qualified for top-RSI: {info.get('skipped_sr', 0)}")
        L.append(f"- TP/SL sanity failed: {info.get('skipped_sanity', 0)}")
        L.append("")
        L.append(f"_Trades CSV: `reports/portfolio_backtest_intraday/{u}_{c}_trades.csv`_")
        L.append("")

    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday TA backtest at 1h or 4h cadence")
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()))
    parser.add_argument("--cadence", choices=["1h", "4h"])
    parser.add_argument("--all", action="store_true",
                        help="Run all 4 cells: {selected17, hl44} × {1h, 4h}")
    parser.add_argument("--rebuild-sr", action="store_true",
                        help="Force rebuild of full SR cache")
    args = parser.parse_args()

    if args.all:
        cells = [
            ("selected17", "1h"), ("selected17", "4h"),
            ("hl44", "1h"),       ("hl44", "4h"),
        ]
    else:
        if not args.universe or not args.cadence:
            parser.error("Specify --universe and --cadence, or use --all")
        cells = [(args.universe, args.cadence)]

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"sweep_{stamp}.md"

    log.info("Cells to run: %s", cells)
    log.info("Report: %s", report_path)

    # Load hourly OHLCV once for the union of needed tokens
    needed = sorted({t for u, _ in cells for t in UNIVERSES[u]["tokens"]})
    log.info("Loading hourly for %d tokens...", len(needed))
    hourly = load_hourly(needed)
    log.info("Loaded hourly for %d tokens", len(hourly))

    # Daily series derived from hourly (consistent — same source of truth)
    daily_ohlcv = aggregate_to_daily(hourly)

    # SR cache per universe (built once even if both cadences run)
    sr_caches: dict[str, dict] = {}
    for u in {u for u, _ in cells}:
        cache_path = REPORT_DIR / f".sr_cache_full_{u}.pkl"
        if args.rebuild_sr and cache_path.exists():
            cache_path.unlink()
        # Build SR cache only over tokens present in hourly (no point analysing missing ones)
        u_tokens_present = [t for t in UNIVERSES[u]["tokens"] if t in daily_ohlcv]
        u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
        sr_caches[u] = build_sr_cache_full(u_daily, cache_path)

    # Run cells
    results = []
    for u, c in cells:
        try:
            trades, info = run_cell(u, c, hourly, sr_caches[u], daily_ohlcv)
        except Exception as e:
            log.error("Cell %s/%s failed: %s", u, c, e)
            traceback.print_exc()
            results.append({"universe": u, "cadence": c, "agent": UNIVERSES[u]["agent"],
                            "info": {"error": f"{type(e).__name__}: {e}"}})
            continue

        metrics = compute_metrics(trades, INITIAL_CAPITAL)
        # Save trades CSV
        if trades:
            df = pd.DataFrame([{
                "symbol": t.symbol,
                "entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "take_profit": t.take_profit, "stop_loss": t.stop_loss,
                "raw_rr": t.raw_rr, "rsi_at_entry": t.rsi_at_entry,
                "exit_reason": t.exit_reason, "hold_hours": t.hold_hours,
                "pnl_pct": t.pnl_pct, "pnl_usd": t.pnl_usd,
                "size_usd": t.size_usd,
            } for t in trades])
            csv_path = REPORT_DIR / f"{u}_{c}_trades.csv"
            df.to_csv(csv_path, index=False)
            log.info("Wrote %d trades to %s", len(df), csv_path.name)

        results.append({
            "universe": u, "cadence": c, "agent": UNIVERSES[u]["agent"],
            "info": info, "metrics": metrics,
        })
        log.info("CELL DONE  %s/%s  trades=%d  pnl=%s%%  sharpe=%s",
                 u, c, metrics.get("n_trades", 0),
                 metrics.get("total_return_pct"), metrics.get("sharpe"))

    finished = datetime.now(timezone.utc)
    report = render_report(results, started, finished)
    report_path.write_text(report)

    log.info("=" * 70)
    log.info("DONE — report: %s", report_path)
    log.info("Wall time: %.1f min", (finished - started).total_seconds() / 60)

    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
