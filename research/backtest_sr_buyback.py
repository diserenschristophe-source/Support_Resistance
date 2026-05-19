#!/usr/bin/env python3
"""
backtest_sr_buyback.py — S/R Buyback signal-service.
=====================================================

Inverted alternative to break-and-retest. Same infrastructure (analyze_token
cache, v2 framework primitives, sensitivity analysis), but flipped entry:

  BNR:           wait for breakout of R, place limit at broken R on retest
  SR Buyback:    wait for price near major S, place limit AT the support level

Thesis: supports that have held multiple times in an uptrend tend to hold
again. Limit-buy at S, TP at next R above current price (not above entry —
this avoids the BNR "tiny TP at next-Minor-above-broken" trap).

Setup:
  - Token must be uptrending (min_token_rsi gate, plus btc_rsi_floor)
  - Closest support within [min_distance_atr, max_distance_atr] × ATR below current
  - Entry: buy-limit at the support's key_level
  - TP: next resistance above CURRENT price at signal time
  - SL: support − sl_atr_mult × daily_ATR (just below the failed support)
  - Limit TTL: 168h (7 days)
  - Max hold (post-fill): 14 days

Reuses from v2 / backtest_intraday:
  - Signal dataclass, daily_atr_series, levels_with_meta
  - load_hourly, aggregate_to_4h/daily, build_sr_cache_full, partial_bar_rsi
  - typical_investor_sim, sensitivity_by_start_month

CLI:
  python3 research/backtest_sr_buyback.py --universe selected17
  python3 research/backtest_sr_buyback.py --universe selected17 --major-only
  python3 research/backtest_sr_buyback.py --universe selected17 --tp-cascade
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
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
    partial_bar_rsi,
    UNIVERSES,
    MIN_DAILY_HISTORY,
    RSI_PERIOD,
    RSI_HISTORY_BARS,
    TRADING_FEE,
)

from backtest_break_retest_v2 import (  # noqa: E402
    Signal,
    daily_atr_series,
    levels_with_meta,
    typical_investor_sim,
    sensitivity_by_start_month,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sr_buyback")


# ─────────────────────────────────────────────────────────────────────────────
# Strategy params
# ─────────────────────────────────────────────────────────────────────────────

# Support proximity (in ATR units below current price)
SR_MIN_DISTANCE_ATR = 0.2        # support at least this far below current
SR_MAX_DISTANCE_ATR = 2.0        # support no further than this below current

# SL: distance below support, in ATR units
SR_SL_ATR_MULT = 0.5

# Regime
BTC_RSI_FLOOR = 50.0

# Filters
MIN_TOKEN_RSI = 50.0             # uptrend filter (token RSI partial-bar)
MIN_CONFLUENCE = 0.0
MIN_TOUCHES = 0
PER_TOKEN_CAP = 1

# Position management
DEFAULT_RETEST_TTL_HOURS = 168   # 7 days for the dip to happen
DEFAULT_MAX_HOLD_DAYS = 14       # 2 weeks for bounce to play out

# Toggles (CLI)
MAJOR_ONLY = False
TP_CASCADE = False
TP_CASCADE_MIN_RR = 2.0

REPORT_DIR = ROOT / "reports" / "backtest_sr_buyback"
SR_CACHE_DIR = ROOT / "reports" / "portfolio_backtest_intraday"


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_support_setups(
    universe_name: str,
    hourly: dict[str, pd.DataFrame],
    sr_cache: dict,
    daily_ohlcv: dict[str, pd.DataFrame],
) -> tuple[list[Signal], dict]:
    """Emit a Signal whenever a token's current price sits within
    [SR_MIN_DISTANCE_ATR, SR_MAX_DISTANCE_ATR] × ATR above a support level.

    Limit at the support key_level. TP = next resistance above current
    price (not above the support, to avoid tiny TPs). SL = support −
    SR_SL_ATR_MULT × daily ATR.
    """
    uconf = UNIVERSES[universe_name]
    available = [t for t in uconf["tokens"] if t in hourly]
    missing = [t for t in uconf["tokens"] if t not in hourly]

    log.info("Detect setups  universe=%s  tokens=%d/%d",
             universe_name, len(available), len(uconf["tokens"]))
    if missing:
        log.info("  missing tokens: %s", missing)

    if "BTC" not in hourly:
        return [], {"error": "BTC hourly missing"}

    bars_4h = aggregate_to_4h(hourly)
    atr_daily = daily_atr_series(daily_ohlcv)
    close_4h = {s: bars_4h[s]["close"] for s in available}

    common_idx = bars_4h[available[0]].index
    for sym in available[1:]:
        common_idx = common_idx.intersection(bars_4h[sym].index)
    common_idx = common_idx.sort_values()

    daily_close_df = pd.DataFrame(
        {s: d["close"] for s, d in daily_ohlcv.items() if s in available}
    ).sort_index()
    daily_close_np = {s: daily_close_df[s].to_numpy(dtype=float) for s in available}
    daily_index = daily_close_df.index

    def last_closed_daily_idx(ts: pd.Timestamp) -> int:
        target = ts.normalize() - pd.Timedelta(days=1)
        return int(daily_index.searchsorted(target, side="right") - 1)

    signals: list[Signal] = []
    btc_rsi_blocked = 0
    cache_miss = 0
    no_atr = 0
    no_valid_support = 0
    no_tp = 0
    tp_sl_sanity = 0
    cascade_skipped = 0
    bars_scanned = 0

    for hi in range(len(common_idx)):
        ts = common_idx[hi]
        if hi < 1:
            continue

        d_idx = last_closed_daily_idx(ts)
        if d_idx < MIN_DAILY_HISTORY:
            continue

        # BTC regime gate
        btc_hist = daily_close_np["BTC"][:d_idx + 1][-RSI_HISTORY_BARS:]
        btc_now = float(close_4h["BTC"].loc[ts])
        btc_rsi = partial_bar_rsi(btc_hist, btc_now, RSI_PERIOD)
        bars_scanned += 1
        if pd.isna(btc_rsi) or btc_rsi < BTC_RSI_FLOOR:
            btc_rsi_blocked += 1
            continue

        d_date = daily_index[d_idx]
        d_date_str = str(d_date)[:10]

        for sym in available:
            sr_entry = sr_cache.get((sym, d_date_str))
            if sr_entry is None:
                cache_miss += 1
                continue
            supports, resistances = levels_with_meta(sr_entry)
            if MAJOR_ONLY:
                supports = [l for l in supports if l.get("tier") == "Major"]
                resistances = [l for l in resistances if l.get("tier") == "Major"]
            if not supports:
                continue

            atr_val = atr_daily.get(sym, pd.Series()).get(d_date, np.nan)
            if pd.isna(atr_val) or atr_val <= 0:
                no_atr += 1
                continue

            current_price = float(close_4h[sym].loc[ts])

            # Find closest support such that:
            #   min_dist × ATR <= (current - support) <= max_dist × ATR
            valid = []
            for s in supports:
                s_price = s["price"]
                gap = current_price - s_price
                if gap <= 0:
                    continue
                gap_atr = gap / atr_val
                if SR_MIN_DISTANCE_ATR <= gap_atr <= SR_MAX_DISTANCE_ATR:
                    valid.append(s)
            if not valid:
                no_valid_support += 1
                continue

            # Closest support (highest price still below current)
            chosen = max(valid, key=lambda s: s["price"])
            entry_price = chosen["price"]

            # TP: first R above CURRENT price (not above support)
            above_now = sorted(
                [r for r in resistances if r["price"] > current_price],
                key=lambda r: r["price"],
            )
            if not above_now:
                no_tp += 1
                continue
            tp = above_now[0]["price"]
            tp2 = above_now[1]["price"] if len(above_now) > 1 else 0.0

            # SL = support − SL_atr_mult × ATR
            sl = entry_price - SR_SL_ATR_MULT * atr_val

            if tp <= entry_price or sl >= entry_price:
                tp_sl_sanity += 1
                continue

            # TP cascade
            tp_source = "tp1"
            if TP_CASCADE:
                sl_dist = max(entry_price - sl, 1e-9)
                rr1 = (tp - entry_price) / sl_dist
                if rr1 < TP_CASCADE_MIN_RR:
                    if tp2 > 0:
                        rr2 = (tp2 - entry_price) / sl_dist
                        if rr2 >= TP_CASCADE_MIN_RR:
                            tp = tp2
                            tp_source = "tp2"
                        else:
                            cascade_skipped += 1
                            continue
                    else:
                        cascade_skipped += 1
                        continue

            # Token RSI at signal time (for momentum/uptrend filter downstream)
            sym_hist = daily_close_np[sym][:d_idx + 1][-RSI_HISTORY_BARS:]
            tok_rsi = partial_bar_rsi(sym_hist, current_price, RSI_PERIOD)

            signals.append(Signal(
                symbol=sym,
                breakout_ts=ts,                   # reusing field — "signal_ts"
                breakout_close=current_price,
                level=float(entry_price),
                tp=float(tp),
                sl=float(sl),
                expires_at=ts + pd.Timedelta(hours=DEFAULT_RETEST_TTL_HOURS),
                confluence=float(chosen["confluence"]),
                touches=int(chosen["touches"]),
                tier=str(chosen.get("tier", "")),
                breakout_magnitude_atr=float((current_price - entry_price) / atr_val),
                token_rsi=float(tok_rsi) if not pd.isna(tok_rsi) else 0.0,
                btc_rsi=float(btc_rsi),
                raw_rr=float((tp - entry_price) / max(entry_price - sl, 1e-9)),
                tp2=float(tp2),
                tp_source=tp_source,
            ))

    funnel = {
        "bars_scanned": int(bars_scanned),
        "btc_regime_blocked": int(btc_rsi_blocked),
        "cache_miss": int(cache_miss),
        "atr_unavailable": int(no_atr),
        "no_valid_support_in_range": int(no_valid_support),
        "no_tp_above_current": int(no_tp),
        "tp_sl_sanity_failed": int(tp_sl_sanity),
        "cascade_skipped": int(cascade_skipped),
        "raw_signals": int(len(signals)),
        "first_bar": str(common_idx[0]) if len(common_idx) else None,
        "last_bar": str(common_idx[-1]) if len(common_idx) else None,
        "tokens_used": available,
        "tokens_missing": missing,
    }
    log.info("Detect done — raw signals: %d", len(signals))
    return signals, funnel


# ─────────────────────────────────────────────────────────────────────────────
# Filtering (parametrized — separate from v2's globals)
# ─────────────────────────────────────────────────────────────────────────────

def filter_signals(
    signals: list[Signal],
) -> tuple[list[Signal], dict]:
    for s in signals:
        s.passed_quality = (s.confluence >= MIN_CONFLUENCE) or (s.touches >= MIN_TOUCHES)
        s.passed_momentum = s.token_rsi >= MIN_TOKEN_RSI

    signals.sort(key=lambda s: s.breakout_ts)
    active_until: dict[str, pd.Timestamp] = {}
    n_quality_drop = 0
    n_momentum_drop = 0
    n_cap_drop = 0
    published: list[Signal] = []

    for s in signals:
        if not s.passed_quality:
            s.skip_reason = "quality"; n_quality_drop += 1; continue
        if not s.passed_momentum:
            s.skip_reason = "momentum"; n_momentum_drop += 1; continue
        last_exp = active_until.get(s.symbol)
        if last_exp is not None and s.breakout_ts < last_exp:
            s.skip_reason = "per_token_cap"; n_cap_drop += 1; continue
        s.published = True
        published.append(s)
        active_until[s.symbol] = s.expires_at

    return published, {
        "raw_signals": int(len(signals)),
        "quality_dropped": int(n_quality_drop),
        "momentum_dropped": int(n_momentum_drop),
        "per_token_cap_dropped": int(n_cap_drop),
        "published": int(len(published)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation (single-TP, parametrized max_hold)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_signal(s: Signal, bars: pd.DataFrame, max_hold_days: int) -> None:
    idx = bars.index
    start_pos = idx.searchsorted(s.breakout_ts)
    if start_pos >= len(idx) - 1:
        return

    # Fill: any 4h bar within TTL where low <= level
    for pos in range(start_pos + 1, len(idx)):
        ts = idx[pos]
        if ts > s.expires_at:
            break
        if float(bars.iloc[pos]["low"]) <= s.level:
            s.filled = True
            s.fill_ts = ts
            break

    if not s.filled:
        return

    fill_pos = idx.searchsorted(s.fill_ts)
    entry_price = s.level
    for pos in range(fill_pos, len(idx)):
        ts = idx[pos]
        bar = bars.iloc[pos]
        high = float(bar["high"]); low = float(bar["low"]); close = float(bar["close"])
        elapsed_days = (ts - s.fill_ts).total_seconds() / 86400

        if pos == fill_pos:
            if elapsed_days >= max_hold_days:
                s.exit_ts, s.exit_price, s.exit_reason = ts, close, "TIMEOUT"
                break
            continue

        if low <= s.sl:
            s.exit_ts, s.exit_price, s.exit_reason = ts, s.sl, "SL"
            break
        if high >= s.tp:
            s.exit_ts, s.exit_price, s.exit_reason = ts, s.tp, "TP"
            break
        if elapsed_days >= max_hold_days:
            s.exit_ts, s.exit_price, s.exit_reason = ts, close, "TIMEOUT"
            break

    if s.exit_ts is None:
        last_ts = idx[-1]
        last_close = float(bars.iloc[-1]["close"])
        s.exit_ts, s.exit_price, s.exit_reason = last_ts, last_close, "OPEN"

    s.pnl_pct = (s.exit_price - entry_price) / entry_price * 100 - TRADING_FEE * 200
    s.hold_hours = int((s.exit_ts - s.fill_ts).total_seconds() / 3600)


def evaluate_all(signals: list[Signal], hourly: dict[str, pd.DataFrame],
                 max_hold_days: int) -> None:
    bars_4h = aggregate_to_4h(hourly)
    for s in signals:
        bars = bars_4h.get(s.symbol)
        if bars is None:
            continue
        evaluate_signal(s, bars, max_hold_days)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def per_signal_metrics(signals: list[Signal]) -> dict:
    n = len(signals)
    if n == 0:
        return {"n_published": 0}
    filled = [s for s in signals if s.filled]
    nf = len(filled)
    tp = [s for s in filled if s.exit_reason == "TP"]
    sl = [s for s in filled if s.exit_reason == "SL"]
    to = [s for s in filled if s.exit_reason == "TIMEOUT"]
    op = [s for s in filled if s.exit_reason == "OPEN"]

    wins = [s for s in filled if s.pnl_pct > 0]
    losses = [s for s in filled if s.pnl_pct <= 0]

    avg_win = float(np.mean([s.pnl_pct for s in wins])) if wins else 0.0
    avg_loss = float(np.mean([s.pnl_pct for s in losses])) if losses else 0.0
    win_rate = 100 * len(wins) / nf if nf else 0.0
    fill_rate = 100 * nf / n if n else 0.0
    avg_pnl_filled = float(np.mean([s.pnl_pct for s in filled])) if filled else 0.0
    expectancy_per_signal = avg_pnl_filled * (nf / n) if n else 0.0
    avg_rr_entry = float(np.mean([s.raw_rr for s in signals])) if signals else 0.0
    avg_rr_filled = float(np.mean([s.raw_rr for s in filled])) if filled else 0.0
    avg_hold = float(np.mean([s.hold_hours for s in filled])) if filled else 0.0

    # Tp_source mix (for cascade-enabled runs)
    n_tp1 = sum(1 for s in signals if s.tp_source == "tp1")
    n_tp2 = sum(1 for s in signals if s.tp_source == "tp2")

    return {
        "n_published": int(n),
        "n_filled": int(nf),
        "n_unfilled_expired": int(n - nf),
        "fill_rate_pct": round(fill_rate, 2),
        "n_tp": int(len(tp)), "n_sl": int(len(sl)),
        "n_timeout": int(len(to)), "n_open": int(len(op)),
        "win_rate_filled_pct": round(win_rate, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_pnl_filled_pct": round(avg_pnl_filled, 2),
        "expectancy_per_signal_pct": round(expectancy_per_signal, 2),
        "avg_rr_entry": round(avg_rr_entry, 2),
        "avg_rr_filled": round(avg_rr_filled, 2),
        "avg_hold_hours": round(avg_hold, 1),
        "n_tp_source_tp1": int(n_tp1),
        "n_tp_source_tp2": int(n_tp2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report + CSV
# ─────────────────────────────────────────────────────────────────────────────

def render_report(
    universe: str,
    funnel_d: dict,
    funnel_f: dict,
    metrics: dict,
    sensitivity: list[dict],
    started: datetime,
    finished: datetime,
    max_hold_days: int,
) -> str:
    L = []
    L.append(f"# S/R Buyback signal-service — {started.strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append("**Strategy**: limit-buy at major support below current price; "
             "expect bounce to next R.")
    L.append("")
    L.append("**Params**")
    L.append("")
    L.append(f"- Support proximity: {SR_MIN_DISTANCE_ATR}-{SR_MAX_DISTANCE_ATR}× daily ATR below current")
    L.append(f"- SL: support − {SR_SL_ATR_MULT}× daily ATR (just below failed support)")
    L.append(f"- TP: first R above current price at signal time")
    L.append(f"- Retest TTL: {DEFAULT_RETEST_TTL_HOURS}h ({DEFAULT_RETEST_TTL_HOURS//24}d)")
    L.append(f"- Max hold (post-fill): {max_hold_days}d")
    L.append(f"- Regime: btc_rsi_floor = {BTC_RSI_FLOOR}")
    L.append(f"- Trend filter: token RSI ≥ {MIN_TOKEN_RSI} (partial-bar daily SMA)")
    L.append(f"- Per-token cap: {PER_TOKEN_CAP} active signal at a time")
    L.append(f"- Major-only levels: **{MAJOR_ONLY}**")
    L.append(f"- TP cascade: **{TP_CASCADE}**  (min RR = {TP_CASCADE_MIN_RR})")
    L.append("")
    L.append(f"- Started:  {started.isoformat(timespec='seconds')}")
    L.append(f"- Finished: {finished.isoformat(timespec='seconds')}")
    L.append(f"- Wall:     {(finished-started).total_seconds()/60:.1f} min")
    L.append("")

    L.append(f"## Universe `{universe}`")
    L.append("")
    L.append(f"- Tokens used: **{len(funnel_d['tokens_used'])}** "
             f"of {len(UNIVERSES[universe]['tokens'])}")
    if funnel_d.get("tokens_missing"):
        L.append(f"- Missing: `{funnel_d['tokens_missing']}`")
    L.append(f"- Bars: {funnel_d['first_bar']} → {funnel_d['last_bar']}")
    L.append("")

    L.append("## Signal funnel")
    L.append("")
    L.append(f"- Bars scanned (after warmup): {funnel_d['bars_scanned']}")
    L.append(f"- BTC regime blocked: {funnel_d['btc_regime_blocked']}")
    L.append(f"- SR cache miss: {funnel_d['cache_miss']}")
    L.append(f"- ATR unavailable: {funnel_d['atr_unavailable']}")
    L.append(f"- No support in proximity window: {funnel_d['no_valid_support_in_range']}")
    L.append(f"- No R above current price: {funnel_d['no_tp_above_current']}")
    L.append(f"- TP/SL sanity failed: {funnel_d['tp_sl_sanity_failed']}")
    if TP_CASCADE:
        L.append(f"- TP cascade skipped (RR<{TP_CASCADE_MIN_RR}): {funnel_d['cascade_skipped']}")
    L.append(f"- **Raw signals:** {funnel_d['raw_signals']}")
    L.append(f"- Quality filter dropped: {funnel_f['quality_dropped']}")
    L.append(f"- Momentum filter dropped: {funnel_f['momentum_dropped']}")
    L.append(f"- Per-token cap dropped: {funnel_f['per_token_cap_dropped']}")
    L.append(f"- **Published signals:** {funnel_f['published']}")
    L.append("")

    L.append("## Per-signal performance")
    L.append("")
    if metrics.get("n_published", 0) == 0:
        L.append("**No published signals.**")
    else:
        m = metrics
        L.append(f"- Published: {m['n_published']}    "
                 f"Filled: {m['n_filled']} ({m['fill_rate_pct']:.1f}%)   "
                 f"Unfilled-expired: {m['n_unfilled_expired']}")
        L.append(f"- Of filled: TP {m['n_tp']}, SL {m['n_sl']}, "
                 f"TIMEOUT {m['n_timeout']}, OPEN {m['n_open']}")
        L.append(f"- Win rate (of filled): **{m['win_rate_filled_pct']:.1f}%**")
        L.append(f"- Avg win / avg loss: {m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%")
        L.append(f"- Avg pnl per filled: {m['avg_pnl_filled_pct']:+.2f}%")
        L.append(f"- **Expectancy per published signal: {m['expectancy_per_signal_pct']:+.2f}%**")
        L.append(f"- Avg R:R at entry (all): {m['avg_rr_entry']}    "
                 f"Filled: {m['avg_rr_filled']}")
        L.append(f"- Avg hold (filled): {m['avg_hold_hours']}h "
                 f"({m['avg_hold_hours']/24:.1f}d)")
        if TP_CASCADE:
            L.append(f"- TP source mix: tp1={m['n_tp_source_tp1']}, tp2={m['n_tp_source_tp2']}")
    L.append("")

    L.append("## Sensitivity by start month (typical-investor sim)")
    L.append("")
    L.append("- Capital $10,000, 4 slots × 25% per slot, FIFO allocation")
    L.append("")
    if not sensitivity:
        L.append("_No data._")
    else:
        L.append("| Start | Eligible | Taken | Skipped (full) | Return % | Final $ |")
        L.append("|-------|----------|-------|---------------|----------|---------|")
        for row in sensitivity:
            L.append(f"| {row['start_ts']} | {row['n_signals_eligible']} | "
                     f"{row['n_taken']} | {row['n_skipped_slots_full']} | "
                     f"**{row['return_pct']:+.1f}%** | ${row['final_equity']:,.0f} |")
    L.append("")
    L.append(f"_Signals CSV: `reports/backtest_sr_buyback/{universe}_signals.csv`_  ")
    L.append(f"_Sensitivity CSV: `reports/backtest_sr_buyback/{universe}_sensitivity.csv`_")
    return "\n".join(L)


def write_signals_csv(signals: list[Signal], path: Path) -> None:
    rows = []
    for s in signals:
        rows.append({
            "symbol": s.symbol,
            "signal_ts": s.breakout_ts,
            "current_price": s.breakout_close,
            "support_level": s.level,
            "tp": s.tp, "tp2": s.tp2, "sl": s.sl, "tp_source": s.tp_source,
            "expires_at": s.expires_at,
            "tier": s.tier, "confluence": s.confluence, "touches": s.touches,
            "distance_to_support_atr": s.breakout_magnitude_atr,  # reusing field
            "token_rsi": s.token_rsi, "btc_rsi": s.btc_rsi,
            "raw_rr": s.raw_rr,
            "passed_quality": s.passed_quality,
            "passed_momentum": s.passed_momentum,
            "published": s.published, "skip_reason": s.skip_reason,
            "filled": s.filled,
            "fill_ts": s.fill_ts,
            "exit_ts": s.exit_ts, "exit_price": s.exit_price,
            "exit_reason": s.exit_reason,
            "pnl_pct": s.pnl_pct, "hold_hours": s.hold_hours,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="S/R buyback signal-service backtest")
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()), default="selected17")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--rebuild-sr", action="store_true")
    parser.add_argument("--major-only", action="store_true")
    parser.add_argument("--tp-cascade", action="store_true")
    parser.add_argument("--min-rr", type=float, default=2.0)
    parser.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD_DAYS)
    args = parser.parse_args()

    global MAJOR_ONLY, TP_CASCADE, TP_CASCADE_MIN_RR
    if args.major_only:
        MAJOR_ONLY = True
        log.info("MAJOR_ONLY: filtering to Major-tier levels only")
    if args.tp_cascade:
        TP_CASCADE = True
        TP_CASCADE_MIN_RR = args.min_rr
        log.info("TP_CASCADE: min_rr=%.2f", TP_CASCADE_MIN_RR)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"sweep_{stamp}.md"

    needed = UNIVERSES[args.universe]["tokens"]
    log.info("Loading hourly for %d tokens...", len(needed))
    hourly = load_hourly(needed)
    log.info("Loaded hourly for %d tokens", len(hourly))

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

    SR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = SR_CACHE_DIR / f".sr_cache_full_{args.universe}.pkl"
    if args.rebuild_sr and cache_path.exists():
        cache_path.unlink()
    u_tokens_present = [t for t in UNIVERSES[args.universe]["tokens"] if t in daily_ohlcv]
    u_daily = {t: daily_ohlcv[t] for t in u_tokens_present}
    sr_cache = build_sr_cache_full(u_daily, cache_path)

    try:
        signals, funnel_d = detect_support_setups(args.universe, hourly, sr_cache, daily_ohlcv)
    except Exception as e:
        log.error("Detect failed: %s", e)
        traceback.print_exc()
        return 1
    if "error" in funnel_d:
        log.error("Detect error: %s", funnel_d["error"])
        return 1

    published, funnel_f = filter_signals(signals)
    evaluate_all(signals, hourly, args.max_hold)
    metrics = per_signal_metrics(published)
    sensitivity = sensitivity_by_start_month(signals)

    sig_csv = REPORT_DIR / f"{args.universe}_signals.csv"
    write_signals_csv(signals, sig_csv)
    log.info("Wrote %d signals to %s", len(signals), sig_csv.name)

    if sensitivity:
        sens_csv = REPORT_DIR / f"{args.universe}_sensitivity.csv"
        pd.DataFrame(sensitivity).to_csv(sens_csv, index=False)
        log.info("Wrote sensitivity to %s", sens_csv.name)

    finished = datetime.now(timezone.utc)
    report = render_report(args.universe, funnel_d, funnel_f, metrics, sensitivity,
                           started, finished, args.max_hold)
    report_path.write_text(report)

    log.info("DONE — report: %s", report_path)
    print("\n" + "=" * 70)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
