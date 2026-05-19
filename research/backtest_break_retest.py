#!/usr/bin/env python3
"""
backtest_break_retest.py — Break-and-retest backtest at 4h cadence.
====================================================================

Strategy (v1):
  1. Daily S/R levels via `analyze_token` (reuses the cache built by
     backtest_intraday: reports/portfolio_backtest_intraday/.sr_cache_full_*.pkl)
  2. On each 4h close, check for a fresh resistance breakout:
       - 4h close_now > R + BREAKOUT_ATR_MULT * daily_ATR(14)
       - 4h close_prev > R (two consecutive 4h closes above)
       - R is taken from the most recent closed daily snapshot
  3. On breakout, place a buy-limit at R (the broken level), TTL 3 days.
       - Fill rule: any 4h bar with low <= R within TTL fills at R
         (maker assumption — conservative)
       - TTL expiry without fill: cancel, miss the trade
  4. Position (after fill):
       - TP = next resistance level above R (sorted resistance list)
       - SL = nearest level below R (highest support, OR
              lower resistance if support is missing)
       - Max hold: 7 calendar days
       - Single concurrent position (v1 limitation)
  5. Filter: btc_rsi_floor=50 (partial-bar daily SMA RSI), same as ta17.

Reuses from backtest_intraday:
  - load_hourly, aggregate_to_4h, aggregate_to_daily
  - build_sr_cache_full (shares the .sr_cache_full_<universe>.pkl)
  - compute_metrics, partial_bar_rsi
  - Trade dataclass, UNIVERSES, constants

CLI:
  python3 research/backtest_break_retest.py
  python3 research/backtest_break_retest.py --universe hl44
  python3 research/backtest_break_retest.py --start 2024-12-01

Outputs (under reports/backtest_break_retest/):
  sweep_<UTC>.md
  {universe}_4h_trades.csv

Note: minimum daily history (60 bars) means the effective trading start is
~60 days after the data start. With hourly data starting 2024-11-19, the
earliest valid signal is around 2025-01-18.
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from backtest_intraday import (  # noqa: E402
    load_hourly,
    aggregate_to_4h,
    aggregate_to_daily,
    build_sr_cache_full,
    compute_metrics,
    partial_bar_rsi,
    Trade,
    UNIVERSES,
    INITIAL_CAPITAL,
    TRADING_FEE,
    MAX_HOLD_DAYS,
    MIN_DAILY_HISTORY,
    RSI_PERIOD,
    RSI_HISTORY_BARS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bnr")

# Strategy params
ATR_PERIOD = 14
BREAKOUT_ATR_MULT = 0.5       # close_now - R >= mult * daily_ATR
RETEST_TTL_HOURS = 72         # 3 days
SL_ATR_MULT = 1.0             # fallback if no level exists below R
BTC_RSI_FLOOR = 50.0
DUP_COOLDOWN_DAYS = MAX_HOLD_DAYS  # don't re-fire on same level within this

REPORT_DIR = ROOT / "reports" / "backtest_break_retest"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PendingLimit:
    symbol: str
    breakout_ts: pd.Timestamp
    level: float          # buy-limit price
    tp: float
    sl: float
    expires_at: pd.Timestamp


def daily_atr_series(daily_ohlcv: dict[str, pd.DataFrame], period: int = ATR_PERIOD) -> dict[str, pd.Series]:
    """Compute SMA-ATR(period) per token, returned as Series indexed by daily date."""
    out: dict[str, pd.Series] = {}
    for sym, df in daily_ohlcv.items():
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1).fillna(close)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        out[sym] = tr.rolling(period).mean()
    return out


def levels_from_cache_entry(entry: dict) -> tuple[list[float], list[float]]:
    """Extract sorted-ASC support and resistance key_level prices."""
    def _extract(lst):
        out = []
        for item in lst or []:
            if isinstance(item, dict):
                v = item.get("key_level")
                if v is None:
                    v = item.get("price") or item.get("level")
                if v is not None:
                    try:
                        out.append(float(v))
                    except (TypeError, ValueError):
                        pass
            elif isinstance(item, (int, float)):
                out.append(float(item))
        return sorted(out)
    return _extract(entry.get("support")), _extract(entry.get("resistance"))


def nearest_above(price: float, levels: list[float]) -> float | None:
    above = [l for l in levels if l > price]
    return min(above) if above else None


def nearest_below(price: float, levels: list[float]) -> float | None:
    below = [l for l in levels if l < price]
    return max(below) if below else None


# ─────────────────────────────────────────────────────────────────────────────
# Backtest cell
# ─────────────────────────────────────────────────────────────────────────────

def run_cell(
    universe_name: str,
    hourly: dict[str, pd.DataFrame],
    sr_cache: dict,
    daily_ohlcv: dict[str, pd.DataFrame],
) -> tuple[list[Trade], dict]:
    uconf = UNIVERSES[universe_name]
    available = [t for t in uconf["tokens"] if t in hourly]
    missing = [t for t in uconf["tokens"] if t not in hourly]

    log.info("=" * 70)
    log.info("BNR cell  universe=%s  cadence=4h", universe_name)
    log.info("  available: %d/%d  missing: %s",
             len(available), len(uconf["tokens"]),
             missing if missing else "—")

    if "BTC" not in hourly:
        return [], {"error": "BTC hourly missing"}

    bars_4h = aggregate_to_4h(hourly)
    atr_daily = daily_atr_series(daily_ohlcv)

    close_4h = {s: bars_4h[s]["close"] for s in available}
    close_prev_4h = {s: close_4h[s].shift(1) for s in available}
    high_4h = {s: bars_4h[s]["high"] for s in available}
    low_4h = {s: bars_4h[s]["low"] for s in available}

    common_idx = bars_4h[available[0]].index
    for sym in available[1:]:
        common_idx = common_idx.intersection(bars_4h[sym].index)
    common_idx = common_idx.sort_values()

    log.info("  common 4h bars: %d  (%s → %s)",
             len(common_idx),
             str(common_idx[0])[:16], str(common_idx[-1])[:16])

    # Daily series for partial-bar BTC RSI
    daily_close_df = pd.DataFrame(
        {s: d["close"] for s, d in daily_ohlcv.items() if s in available}
    ).sort_index()
    daily_close_np = {s: daily_close_df[s].to_numpy(dtype=float) for s in available}
    daily_index = daily_close_df.index

    def last_closed_daily_idx(ts: pd.Timestamp) -> int:
        target = ts.normalize() - pd.Timedelta(days=1)
        return int(daily_index.searchsorted(target, side="right") - 1)

    trades: list[Trade] = []
    open_trade: Trade | None = None
    pending: PendingLimit | None = None
    capital = INITIAL_CAPITAL
    last_breakout: dict[str, tuple[pd.Timestamp, float]] = {}

    skip_btc_rsi = 0
    skip_dup_breakout = 0
    skip_no_sr = 0
    skip_no_atr = 0
    skip_tp_sl_sanity = 0
    n_breakouts = 0
    fills_ok = 0
    fills_missed = 0

    for hi in range(len(common_idx)):
        ts: pd.Timestamp = common_idx[hi]

        # 1. Try to fill pending limit (or expire it)
        if pending is not None and open_trade is None:
            sym = pending.symbol
            if ts >= pending.expires_at:
                pending = None
                fills_missed += 1
            else:
                bar_low = float(low_4h[sym].loc[ts])
                if bar_low <= pending.level:
                    entry_price = pending.level
                    fee = capital * TRADING_FEE
                    open_trade = Trade(
                        symbol=sym, entry_ts=ts, entry_price=entry_price,
                        take_profit=pending.tp, stop_loss=pending.sl,
                        raw_rr=(pending.tp - entry_price)
                               / max(entry_price - pending.sl, 1e-9),
                        rsi_at_entry=0.0,
                        size_usd=capital - fee,
                    )
                    capital = 0.0
                    pending = None
                    fills_ok += 1

        # 2. Check exit on open trade
        if open_trade is not None:
            sym = open_trade.symbol
            high = float(high_4h[sym].loc[ts])
            low = float(low_4h[sym].loc[ts])
            close = float(close_4h[sym].loc[ts])
            elapsed_days = (ts - open_trade.entry_ts).total_seconds() / 86400
            open_trade.hold_hours = int(
                (ts - open_trade.entry_ts).total_seconds() / 3600
            )

            tp_hit = high >= open_trade.take_profit
            sl_hit = low <= open_trade.stop_loss

            exit_reason, exit_price = None, None
            if sl_hit:
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

        # 3. Single-slot: skip signal generation if already engaged
        if open_trade is not None or pending is not None:
            continue
        if hi < 1:
            continue

        d_idx = last_closed_daily_idx(ts)
        if d_idx < MIN_DAILY_HISTORY:
            continue
        d_date = daily_index[d_idx]
        d_date_str = str(d_date)[:10]

        # 4. BTC RSI floor (partial-bar daily SMA RSI)
        btc_hist = daily_close_np["BTC"][:d_idx + 1][-RSI_HISTORY_BARS:]
        btc_now = float(close_4h["BTC"].loc[ts])
        btc_rsi = partial_bar_rsi(btc_hist, btc_now, RSI_PERIOD)
        if np.isnan(btc_rsi) or btc_rsi < BTC_RSI_FLOOR:
            skip_btc_rsi += 1
            continue

        # 5. Scan tokens for fresh breakout
        for sym in available:
            sr_entry = sr_cache.get((sym, d_date_str))
            if sr_entry is None:
                skip_no_sr += 1
                continue
            supports, resistances = levels_from_cache_entry(sr_entry)
            if not resistances:
                continue

            atr_val = atr_daily.get(sym, pd.Series()).get(d_date, np.nan)
            if pd.isna(atr_val) or atr_val <= 0:
                skip_no_atr += 1
                continue

            cn = float(close_4h[sym].loc[ts])
            cp = close_prev_4h[sym].loc[ts]
            if pd.isna(cp):
                continue
            cp = float(cp)

            # Find HIGHEST resistance such that both bars closed above it,
            # and current bar exceeds by ATR magnitude.
            broken: float | None = None
            for r in resistances:
                if cn > r + BREAKOUT_ATR_MULT * atr_val and cp > r:
                    broken = r
                else:
                    break
            if broken is None:
                continue

            # Dedup: skip if we recently fired on the same level for this sym
            prev = last_breakout.get(sym)
            if prev is not None:
                prev_ts, prev_level = prev
                same_level = abs(prev_level - broken) / broken < 0.001
                fresh = (ts - prev_ts).total_seconds() / 86400 < DUP_COOLDOWN_DAYS
                if same_level and fresh:
                    skip_dup_breakout += 1
                    continue

            tp = nearest_above(broken, resistances)
            all_levels = sorted(set(supports + resistances))
            sl = nearest_below(broken, all_levels)
            if sl is None:
                sl = broken - SL_ATR_MULT * atr_val
            if tp is None or sl is None or tp <= broken or sl >= broken:
                skip_tp_sl_sanity += 1
                continue

            pending = PendingLimit(
                symbol=sym,
                breakout_ts=ts,
                level=float(broken),
                tp=float(tp),
                sl=float(sl),
                expires_at=ts + pd.Timedelta(hours=RETEST_TTL_HOURS),
            )
            last_breakout[sym] = (ts, float(broken))
            n_breakouts += 1
            break  # single-slot: one pending at a time

    # Mark-to-market remaining open trade
    if open_trade is not None:
        sym = open_trade.symbol
        last_ts = common_idx[-1]
        last_close = float(close_4h[sym].loc[last_ts])
        open_trade.exit_ts = last_ts
        open_trade.exit_price = last_close
        open_trade.exit_reason = "OPEN"
        open_trade.pnl_pct = (last_close - open_trade.entry_price) / open_trade.entry_price * 100
        open_trade.pnl_usd = open_trade.size_usd * open_trade.pnl_pct / 100
        trades.append(open_trade)

    info = {
        "universe": universe_name,
        "cadence": "4h",
        "tokens_used": available,
        "tokens_missing": missing,
        "common_bars": int(len(common_idx)),
        "first_bar": str(common_idx[0]),
        "last_bar": str(common_idx[-1]),
        "n_breakouts": int(n_breakouts),
        "fills_ok": int(fills_ok),
        "fills_missed_ttl": int(fills_missed),
        "skipped_btc_rsi": int(skip_btc_rsi),
        "skipped_dup_breakout": int(skip_dup_breakout),
        "skipped_no_sr": int(skip_no_sr),
        "skipped_no_atr": int(skip_no_atr),
        "skipped_tp_sl_sanity": int(skip_tp_sl_sanity),
    }
    return trades, info


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def render_report(result: dict, started: datetime, finished: datetime) -> str:
    L = []
    L.append(f"# Break-and-Retest backtest — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append("**Strategy spec (v1)**")
    L.append("")
    L.append("- 4h cadence, daily-derived S/R levels (analyze_token)")
    L.append(f"- Breakout: 4h close > R + {BREAKOUT_ATR_MULT}×ATR(14d), prior 4h close > R")
    L.append(f"- Entry: buy-limit at broken level, TTL {RETEST_TTL_HOURS}h (3d)")
    L.append("- TP: next resistance above broken level")
    L.append("- SL: nearest level below broken (any S or R), fallback R − 1×ATR")
    L.append(f"- Max hold: {MAX_HOLD_DAYS} days   Single concurrent position (v1 limit)")
    L.append(f"- Filter: btc_rsi_floor={BTC_RSI_FLOOR} (partial-bar daily SMA RSI)")
    L.append(f"- Dedup: skip same level within {DUP_COOLDOWN_DAYS} days")
    L.append("")
    L.append(f"- Started:  {started.isoformat(timespec='seconds')}")
    L.append(f"- Finished: {finished.isoformat(timespec='seconds')}")
    L.append(f"- Wall:     {(finished-started).total_seconds()/60:.1f} min")
    L.append("")

    u = result["universe"]
    info = result.get("info", {})
    L.append(f"## Universe {u}")
    L.append("")
    if "error" in info:
        L.append(f"**ERROR:** {info['error']}")
        return "\n".join(L)

    L.append(f"- Tokens used: **{len(info['tokens_used'])}** of {len(UNIVERSES[u]['tokens'])}")
    if info.get("tokens_missing"):
        L.append(f"- Missing: `{info['tokens_missing']}`")
    L.append(f"- 4h bars: {info['common_bars']}  ({info['first_bar'][:16]} → {info['last_bar'][:16]})")
    L.append("")

    nsetups = info["fills_ok"] + info["fills_missed_ttl"]
    fill_rate = 100 * info["fills_ok"] / max(nsetups, 1)
    L.append("**Limit-order fill stats**")
    L.append("")
    L.append(f"- Breakouts detected: {info['n_breakouts']}")
    L.append(f"- Limits placed (= breakouts, single-slot): {nsetups}")
    L.append(f"- Filled: {info['fills_ok']}  ({fill_rate:.1f}%)")
    L.append(f"- Expired (no retest within TTL): {info['fills_missed_ttl']}")
    L.append("")

    m = result.get("metrics") or {}
    if not m or m.get("n_trades", 0) == 0:
        L.append("**No trades** — see skip counters below.")
    else:
        L.append("**Metrics**")
        L.append("")
        L.append(f"- Trades: {m['n_trades']}  (TP {m['n_tp']}, SL {m['n_sl']}, timeout {m['n_timeout']}, open {m['n_open']})")
        L.append(f"- Total return: **{m['total_return_pct']:+.1f}%**")
        L.append(f"- CAGR: {m['cagr_pct']:+.1f}%   Sharpe: {m.get('sharpe')}   Sortino: {m.get('sortino')}   Calmar: {m.get('calmar')}")
        L.append(f"- Max DD: {m['max_dd_pct']:.1f}%   Win rate: {m['win_rate_pct']:.1f}%")
        L.append(f"- Avg win/loss: {m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%   Profit factor: {m.get('profit_factor')}")
        L.append(f"- Avg hold: {m['avg_hold_hours']}h ({m['avg_hold_hours']/24:.1f} d)   Avg R:R at entry: {m['avg_rr']}")
    L.append("")

    L.append("**Skip counters**")
    L.append("")
    L.append(f"- BTC RSI floor blocked: {info['skipped_btc_rsi']}")
    L.append(f"- Duplicate-level cooldown: {info['skipped_dup_breakout']}")
    L.append(f"- SR cache miss: {info['skipped_no_sr']}")
    L.append(f"- ATR unavailable: {info['skipped_no_atr']}")
    L.append(f"- TP/SL sanity failed: {info['skipped_tp_sl_sanity']}")
    L.append("")
    L.append(f"_Trades CSV: `reports/backtest_break_retest/{u}_4h_trades.csv`_")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Break-and-retest backtest (4h)")
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()), default="selected17")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--rebuild-sr", action="store_true",
                        help="Force rebuild of the shared SR cache")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"sweep_{stamp}.md"

    log.info("Universe: %s  Report: %s", args.universe, report_path)

    needed = UNIVERSES[args.universe]["tokens"]
    log.info("Loading hourly for %d tokens...", len(needed))
    hourly = load_hourly(needed)
    log.info("Loaded hourly for %d tokens", len(hourly))

    # Optional date window
    if args.start or args.end:
        start_ts = pd.Timestamp(args.start, tz="UTC") if args.start else None
        end_ts = (pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)) if args.end else None
        for sym in list(hourly.keys()):
            df = hourly[sym]
            if start_ts is not None:
                df = df.loc[df.index >= start_ts]
            if end_ts is not None:
                df = df.loc[df.index < end_ts]
            hourly[sym] = df
        log.info("Date window applied: %s → %s", args.start, args.end)

    daily_ohlcv = aggregate_to_daily(hourly)

    # Reuse the intraday backtest's SR cache (same pickle path).
    SR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    if args.rebuild_sr and cache_path.exists():
        cache_path.unlink()
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    try:
        trades, info = run_cell(args.universe, hourly, sr_cache, daily_ohlcv)
    except Exception as e:
        log.error("Cell failed: %s", e)
        traceback.print_exc()
        return 1

    metrics = compute_metrics(trades, INITIAL_CAPITAL)

    if trades:
        df = pd.DataFrame([{
            "symbol": t.symbol,
            "entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "take_profit": t.take_profit, "stop_loss": t.stop_loss,
            "raw_rr": t.raw_rr,
            "exit_reason": t.exit_reason, "hold_hours": t.hold_hours,
            "pnl_pct": t.pnl_pct, "pnl_usd": t.pnl_usd,
            "size_usd": t.size_usd,
        } for t in trades])
        csv_path = REPORT_DIR / f"{args.universe}_4h_trades.csv"
        df.to_csv(csv_path, index=False)
        log.info("Wrote %d trades to %s", len(df), csv_path.name)

    result = {"universe": args.universe, "info": info, "metrics": metrics}
    finished = datetime.now(timezone.utc)
    report = render_report(result, started, finished)
    report_path.write_text(report)

    log.info("=" * 70)
    log.info("DONE — report: %s", report_path)
    print("\n" + "=" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
