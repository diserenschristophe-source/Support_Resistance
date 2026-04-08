#!/usr/bin/env python3
"""
weekly_structure.py — Weekly Market Structure Analysis
=======================================================

Resamples existing daily OHLCV cache into calendar weekly candles,
detects swing highs/lows (right-side only, no look-ahead bias),
classifies trend via HH/HL/LH/LL sequence, and detects BOS and CHOCH.

Design principles:
  - No re-downloading: resamples from data/{SYMBOL}_daily.csv
  - Body-first: all pivots and breaks measured on candle body closes
  - Right-side only pivot confirmation: k=2 candles after the pivot
  - No indicators: pure price structure
  - State machine: BULLISH / BEARISH / RANGING / TRANSITIONING
  - Partial (current) week always excluded from pivot detection

Usage:
    python3 weekly_structure.py BTC
    python3 weekly_structure.py BTC ETH SOL BNB
    python3 weekly_structure.py --all
    python3 weekly_structure.py BTC --data-dir /path/to/data
    python3 weekly_structure.py BTC --json        # machine-readable output
    python3 weekly_structure.py BTC --min-weeks 20

Output (default): human-readable terminal summary
Output (--json):  JSON to stdout, one object per token
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

PIVOT_K        = 2      # candles on the right needed to confirm a pivot
N_SWING_KEEP   = 8      # number of confirmed swing points to retain (4H + 4L)
MIN_WEEKS      = 16     # minimum closed weekly candles required for analysis
CHOCH_MIN_BODY = 0.003  # minimum body close beyond broken level (0.3%)
STALE_WEEKS    = 6      # weeks without a new pivot before staleness check kicks in
STALE_BREAK_PCT = 0.10  # price must be >10% beyond stale pivot to inject synthetic


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    kind: str           # 'high' or 'low'
    price: float        # body close price (open or close, whichever is the body edge)
    week_start: str     # ISO date of the Monday of that week
    confirmed_on: str   # ISO date of the Monday when confirmed (week_start + k weeks)
    index: int          # position in the weekly DataFrame

@dataclass
class StructureSignal:
    signal: str         # 'BOS_UP' | 'BOS_DOWN' | 'CHOCH_UP' | 'CHOCH_DOWN' | 'NONE'
    broken_level: float
    break_price: float
    break_margin_pct: float
    strength: int       # 0–3
    week: str           # ISO date of the candle that produced the signal

@dataclass
class WeeklyStructure:
    symbol: str
    state: str                      # BULLISH | BEARISH | RANGING | TRANSITIONING
    prior_state: Optional[str]
    sequence: str                   # e.g. "HH + HL" | "LH + LL" | etc.
    last_swing_high: Optional[float]
    last_swing_high_week: Optional[str]
    last_swing_low: Optional[float]
    last_swing_low_week: Optional[str]
    signal: StructureSignal
    weeks_in_state: int
    closed_weekly_candles: int
    as_of_week: str                 # last fully closed week
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Step 1 — Load and resample
# ─────────────────────────────────────────────────────────────

def load_daily(symbol: str, data_dir: str) -> Optional[pd.DataFrame]:
    """Load daily OHLCV from CSV cache."""
    path = os.path.join(data_dir, f"{symbol.upper()}_daily.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df.columns = [c.lower().strip() for c in df.columns]
        if "timestamp" not in df.columns:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                return None
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df.sort_index(inplace=True)
        df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        return df
    except Exception:
        return None


def resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """
    Resample daily OHLCV to calendar weekly candles (Mon–Sun, label=Monday).

    OHLCV aggregation:
      open   = first daily open of the week
      high   = max of daily highs
      low    = min of daily lows
      close  = last daily close of the week
      volume = sum of daily volumes

    Drops the current (incomplete) week — only fully closed weeks are usable.
    """
    weekly = daily.resample("W-MON", label="left", closed="left").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    })
    weekly.dropna(subset=["open", "close"], inplace=True)

    # Remove the current partial week:
    # A week is complete if its Monday + 7 days <= today (UTC)
    today = datetime.now(timezone.utc)
    weekly = weekly[weekly.index + pd.Timedelta(days=7) <= today]

    return weekly


def resample_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """
    Resample daily OHLCV to calendar monthly candles (label=month start).
    Drops the current (incomplete) month.
    """
    monthly = daily.resample("MS").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    })
    monthly.dropna(subset=["open", "close"], inplace=True)

    # Remove current partial month
    today = datetime.now(timezone.utc)
    first_of_this_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly = monthly[monthly.index < first_of_this_month]

    return monthly


def prepare_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare daily candles for structure analysis.
    Drops today's incomplete candle.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return daily[daily.index < today].copy()


def resample_to_timeframe(daily: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resample daily OHLCV to the specified timeframe.

    timeframe: 'daily' | 'weekly' | 'monthly'
    """
    if timeframe == "daily":
        return prepare_daily(daily)
    elif timeframe == "weekly":
        return resample_weekly(daily)
    elif timeframe == "monthly":
        return resample_monthly(daily)
    else:
        raise ValueError(f"Unknown timeframe: {timeframe}")


# ─────────────────────────────────────────────────────────────
# Step 2 — Body edge helpers
# ─────────────────────────────────────────────────────────────

def body_high(row: pd.Series) -> float:
    """Upper body edge (max of open/close)."""
    return max(row["open"], row["close"])


def body_low(row: pd.Series) -> float:
    """Lower body edge (min of open/close)."""
    return min(row["open"], row["close"])


def body_size(row: pd.Series) -> float:
    return abs(row["close"] - row["open"])


# ─────────────────────────────────────────────────────────────
# Step 3 — Pivot detection (right-side only, no look-ahead)
# ─────────────────────────────────────────────────────────────

def detect_pivots(
    weekly: pd.DataFrame,
    k: int = PIVOT_K,
    use_wicks: bool = False,
    min_swing_pct: float = 0.0,
) -> list[SwingPoint]:
    """
    Detect swing highs and lows on candle prices.

    A candle at index n is a swing HIGH if:
      high(n) > high(n-1) ... high(n-k)   [left side]
      high(n) > high(n+1) ... high(n+k)   [right side — confirmation]

    Crucially: the pivot at n is only KNOWN after candle n+k closes.
    The SwingPoint.confirmed_on reflects this honest lag.

    Parameters
    ----------
    k             : candles on each side required to confirm a pivot
    use_wicks     : if True, use high/low (wicks) instead of body edges.
                    Better for monthly/weekly where wicks capture real extremes.
    min_swing_pct : minimum price move (%) from previous pivot of the same kind
                    to register a new pivot. Filters noise on higher timeframes.
                    E.g. 0.10 = pivot must be 10% beyond previous same-kind pivot.

    The last k candles can never have a confirmed pivot yet.
    """
    pivots: list[SwingPoint] = []
    dates  = weekly.index
    n = len(weekly)

    if use_wicks:
        # Use actual high/low (wicks) for pivot detection
        candle_highs = weekly["high"].values
        candle_lows  = weekly["low"].values
    else:
        # Use body edges (max/min of open, close)
        closes = weekly["close"].values
        opens  = weekly["open"].values
        candle_highs = np.maximum(closes, opens)
        candle_lows  = np.minimum(closes, opens)

    for i in range(k, n - k):
        ch = candle_highs[i]
        cl = candle_lows[i]

        # Check swing high
        left_h  = [candle_highs[i-j] for j in range(1, k+1)]
        right_h = [candle_highs[i+j] for j in range(1, k+1)]

        if ch > max(left_h) and ch > max(right_h):
            pivots.append(SwingPoint(
                kind="high",
                price=ch,
                week_start=dates[i].strftime("%Y-%m-%d"),
                confirmed_on=dates[i + k].strftime("%Y-%m-%d"),
                index=i,
            ))

        # Check swing low
        left_l  = [candle_lows[i-j] for j in range(1, k+1)]
        right_l = [candle_lows[i+j] for j in range(1, k+1)]

        if cl < min(left_l) and cl < min(right_l):
            pivots.append(SwingPoint(
                kind="low",
                price=cl,
                week_start=dates[i].strftime("%Y-%m-%d"),
                confirmed_on=dates[i + k].strftime("%Y-%m-%d"),
                index=i,
            ))

    # Sort by confirmation date
    pivots.sort(key=lambda p: p.confirmed_on)

    # Filter by minimum swing size: a new pivot must move min_swing_pct
    # beyond the previous pivot of the same kind
    if min_swing_pct > 0:
        pivots = _filter_min_swing(pivots, min_swing_pct)

    return pivots


def _filter_min_swing(pivots: list[SwingPoint], min_pct: float) -> list[SwingPoint]:
    """
    Remove pivots that are too close to the previous pivot of the same kind.
    A new high must be at least min_pct above the previous high (or below for lows).
    This filters minor bounces that aren't real structural pivots.
    """
    filtered: list[SwingPoint] = []
    last_high: Optional[SwingPoint] = None
    last_low: Optional[SwingPoint] = None

    for p in pivots:
        if p.kind == "high":
            if last_high is None:
                filtered.append(p)
                last_high = p
            else:
                # New high must differ by min_pct from last high
                change = abs(p.price - last_high.price) / last_high.price
                if change >= min_pct:
                    filtered.append(p)
                    last_high = p
                # If new high is higher, always update (even if filtered out)
                elif p.price > last_high.price:
                    # Replace last high with this higher one
                    filtered = [x for x in filtered if x is not last_high] + [p]
                    last_high = p
        else:  # low
            if last_low is None:
                filtered.append(p)
                last_low = p
            else:
                change = abs(p.price - last_low.price) / last_low.price
                if change >= min_pct:
                    filtered.append(p)
                    last_low = p
                elif p.price < last_low.price:
                    filtered = [x for x in filtered if x is not last_low] + [p]
                    last_low = p

    filtered.sort(key=lambda p: p.confirmed_on)
    return filtered


def get_last_n_pivots(pivots: list[SwingPoint], n: int = N_SWING_KEEP
                      ) -> tuple[list[SwingPoint], list[SwingPoint]]:
    """Return last n/2 confirmed swing highs and last n/2 confirmed swing lows."""
    highs = [p for p in pivots if p.kind == "high"]
    lows  = [p for p in pivots if p.kind == "low"]
    return highs[-(n//2):], lows[-(n//2):]


def fix_stale_pivots(
    weekly: pd.DataFrame,
    highs: list[SwingPoint],
    lows: list[SwingPoint],
    stale_weeks: int = STALE_WEEKS,
    stale_break_pct: float = STALE_BREAK_PCT,
) -> tuple[list[SwingPoint], list[SwingPoint]]:
    """
    Fix stale pivot problem: in a sustained trend with no confirmed pivots,
    the last swing point becomes outdated and misleading.

    If the current price has moved >stale_break_pct beyond the last pivot
    AND the last pivot is older than stale_weeks, inject a synthetic pivot
    at the most recent weekly close to reflect reality.

    Example: ADA dropped from 0.81 (last swing low, Sep 2025) to 0.25
    with no confirmed lower low — the sustained downtrend never produced
    k=2 confirming candles. This function injects a synthetic low at 0.25.
    """
    if len(weekly) < 2:
        return highs, lows

    last_candle = weekly.iloc[-1]
    last_week = weekly.index[-1]
    current_body_low = body_low(last_candle)
    current_body_high = body_high(last_candle)
    last_week_str = last_week.strftime("%Y-%m-%d")

    # ── Check for stale swing LOW ─────────────────────────────
    if lows:
        last_low = lows[-1]
        weeks_since_low = (last_week - pd.Timestamp(last_low.confirmed_on, tz="UTC")).days // 7
        drop_pct = (last_low.price - current_body_low) / last_low.price

        if weeks_since_low >= stale_weeks and drop_pct >= stale_break_pct:
            # Find the actual lowest body-low in the recent candles
            recent = weekly.iloc[-stale_weeks:]
            lowest_idx = None
            lowest_price = float('inf')
            for i in range(len(recent)):
                bl = min(recent.iloc[i]["open"], recent.iloc[i]["close"])
                if bl < lowest_price:
                    lowest_price = bl
                    lowest_idx = i
            if lowest_price < last_low.price:
                syn_week = recent.index[lowest_idx]
                lows = lows + [SwingPoint(
                    kind="low",
                    price=lowest_price,
                    week_start=syn_week.strftime("%Y-%m-%d"),
                    confirmed_on=last_week_str,
                    index=len(weekly) - stale_weeks + lowest_idx,
                )]

    # ── Check for stale swing HIGH ────────────────────────────
    if highs:
        last_high = highs[-1]
        weeks_since_high = (last_week - pd.Timestamp(last_high.confirmed_on, tz="UTC")).days // 7
        rise_pct = (current_body_high - last_high.price) / last_high.price

        if weeks_since_high >= stale_weeks and rise_pct >= stale_break_pct:
            recent = weekly.iloc[-stale_weeks:]
            highest_idx = None
            highest_price = float('-inf')
            for i in range(len(recent)):
                bh = max(recent.iloc[i]["open"], recent.iloc[i]["close"])
                if bh > highest_price:
                    highest_price = bh
                    highest_idx = i
            if highest_price > last_high.price:
                syn_week = recent.index[highest_idx]
                highs = highs + [SwingPoint(
                    kind="high",
                    price=highest_price,
                    week_start=syn_week.strftime("%Y-%m-%d"),
                    confirmed_on=last_week_str,
                    index=len(weekly) - stale_weeks + highest_idx,
                )]

    return highs, lows


# ─────────────────────────────────────────────────────────────
# Step 4 — Trend classification
# ─────────────────────────────────────────────────────────────

def classify_sequence(highs: list[SwingPoint], lows: list[SwingPoint]) -> str:
    """
    Classify the HH/HL/LH/LL sequence from the last 2 swing highs and 2 swing lows.

    Returns one of:
      "HH + HL"  — Bullish
      "LH + LL"  — Bearish
      "HH + LL"  — Expanding (transition)
      "LH + HL"  — Compressing (transition)
      "Incomplete" — Not enough swing points yet
    """
    if len(highs) < 2 or len(lows) < 2:
        return "Incomplete"

    sh1, sh2 = highs[-1], highs[-2]   # sh1 = most recent
    sl1, sl2 = lows[-1],  lows[-2]

    higher_high = sh1.price > sh2.price
    higher_low  = sl1.price > sl2.price

    if higher_high and higher_low:
        return "HH + HL"
    elif not higher_high and not higher_low:
        return "LH + LL"
    elif higher_high and not higher_low:
        return "HH + LL"
    else:
        return "LH + HL"


def sequence_to_trend(sequence: str) -> str:
    """Map sequence label to directional state."""
    return {
        "HH + HL":    "BULLISH",
        "LH + LL":    "BEARISH",
        "HH + LL":    "TRANSITIONING",
        "LH + HL":    "TRANSITIONING",
        "Incomplete": "RANGING",
    }.get(sequence, "RANGING")


# ─────────────────────────────────────────────────────────────
# Step 5 — BOS and CHOCH detection
# ─────────────────────────────────────────────────────────────

def detect_signal(
    weekly: pd.DataFrame,
    highs: list[SwingPoint],
    lows: list[SwingPoint],
    current_state: str,
) -> StructureSignal:
    """
    Check the most recent closed weekly candle for BOS or CHOCH.

    BOS  = break of last swing in the direction of the trend (continuation)
    CHOCH = break of last swing AGAINST the trend direction (reversal warning)

    Break condition: weekly body close (not wick) beyond the pivot level
    by at least CHOCH_MIN_BODY (0.3%).

    Returns a StructureSignal with type, broken level, margin, and strength score.
    """
    if len(weekly) == 0:
        return StructureSignal("NONE", 0, 0, 0, 0, "")

    last = weekly.iloc[-1]
    last_week = weekly.index[-1].strftime("%Y-%m-%d")
    # Use close for break confirmation (conservative: price must close beyond level)
    last_body_high = body_high(last)
    last_body_low  = body_low(last)
    last_atr = _weekly_atr(weekly, period=10)

    # Need at least one pivot on each side to detect signals
    if not highs or not lows:
        return StructureSignal("NONE", 0, 0, 0, 0, last_week)

    sh = highs[-1]  # most recent swing high
    sl = lows[-1]   # most recent swing low

    signal_type   = "NONE"
    broken_level  = 0.0
    break_price   = 0.0
    break_margin  = 0.0

    # ── Check upside break ──────────────────────────────────
    if last_body_high > sh.price * (1 + CHOCH_MIN_BODY):
        broken_level = sh.price
        break_price  = last_body_high
        break_margin = (last_body_high - sh.price) / sh.price

        if current_state in ("BULLISH", "RANGING"):
            signal_type = "BOS_UP"
        else:
            # Breaking a swing high while in BEARISH or TRANSITIONING = CHOCH
            signal_type = "CHOCH_UP"

    # ── Check downside break ─────────────────────────────────
    elif last_body_low < sl.price * (1 - CHOCH_MIN_BODY):
        broken_level = sl.price
        break_price  = last_body_low
        break_margin = (sl.price - last_body_low) / sl.price

        if current_state in ("BEARISH", "RANGING"):
            signal_type = "BOS_DOWN"
        else:
            signal_type = "CHOCH_DOWN"

    if signal_type == "NONE":
        return StructureSignal("NONE", 0, 0, 0, 0, last_week)

    # ── Strength score (0–3) ─────────────────────────────────
    strength = 0

    # 1. Prior trend was established (2+ confirmed swings on each side)
    if len(highs) >= 2 and len(lows) >= 2:
        strength += 1

    # 2. Break margin > 1%
    if break_margin >= 0.01:
        strength += 1

    # 3. Breaking candle body size > 1× weekly ATR
    if last_atr and body_size(last) >= last_atr:
        strength += 1

    return StructureSignal(
        signal=signal_type,
        broken_level=round(broken_level, 6),
        break_price=round(break_price, 6),
        break_margin_pct=round(break_margin * 100, 2),
        strength=strength,
        week=last_week,
    )


def _weekly_atr(weekly: pd.DataFrame, period: int = 10) -> Optional[float]:
    """Simple ATR approximation on weekly candles."""
    if len(weekly) < period + 1:
        return None
    highs  = weekly["high"].values
    lows   = weekly["low"].values
    closes = weekly["close"].values
    trs = []
    for i in range(1, len(weekly)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]),
        )
        trs.append(tr)
    return float(np.mean(trs[-period:]))


# ─────────────────────────────────────────────────────────────
# Step 6 — State machine
# ─────────────────────────────────────────────────────────────

def resolve_state(
    sequence: str,
    signal: StructureSignal,
    prior_state: Optional[str],
    weeks_no_bos: int,
) -> str:
    """
    Determine current state from sequence + signal + prior state.

    Follows the Wyckoff cycle order as the default path:
      BULLISH → TRANSITIONING (distribution) → BEARISH → TRANSITIONING (accumulation) → BULLISH

    Skipping a phase (e.g. BULLISH → BEARISH directly) is only allowed
    when the signal is strong (strength >= 2), indicating a crash or
    V-reversal. Otherwise, the state machine routes through TRANSITIONING.

    Transitions:
      BULLISH   + CHOCH_DOWN              → TRANSITIONING (distribution)
      BULLISH   + CHOCH_DOWN (strength≥2) → BEARISH (crash, skip distribution)
      BEARISH   + CHOCH_UP                → TRANSITIONING (accumulation)
      BEARISH   + CHOCH_UP (strength≥2)   → BULLISH (V-reversal, skip accumulation)
      TRANSITIONING + BOS_UP              → BULLISH
      TRANSITIONING + BOS_DOWN            → BEARISH
      TRANSITIONING (4+ periods no BOS)   → RANGING
      RANGING   + BOS_UP                  → BULLISH
      RANGING   + BOS_DOWN                → BEARISH
    """
    base = sequence_to_trend(sequence)
    sig = signal.signal
    strength = signal.strength

    # ── From BULLISH ──────────────────────────────────────────
    if prior_state == "BULLISH":
        if sig == "CHOCH_DOWN":
            # Strong signal → skip distribution, go straight to BEARISH
            if strength >= 2:
                return "BEARISH"
            return "TRANSITIONING"
        if sig == "BOS_DOWN":
            # BOS against the trend = unusual, treat as distribution starting
            return "TRANSITIONING"
        # Stay BULLISH unless sequence says otherwise
        if base == "BEARISH" and sig in ("BOS_DOWN", "CHOCH_DOWN"):
            return "TRANSITIONING"  # route through transition, don't jump
        return base if base == "BULLISH" else "BULLISH"

    # ── From BEARISH ──────────────────────────────────────────
    if prior_state == "BEARISH":
        if sig == "CHOCH_UP":
            if strength >= 2:
                return "BULLISH"
            return "TRANSITIONING"
        if sig == "BOS_UP":
            return "TRANSITIONING"
        if base == "BULLISH" and sig in ("BOS_UP", "CHOCH_UP"):
            return "TRANSITIONING"
        return base if base == "BEARISH" else "BEARISH"

    # ── From TRANSITIONING ────────────────────────────────────
    if prior_state == "TRANSITIONING":
        if sig == "BOS_UP":
            return "BULLISH"
        if sig == "BOS_DOWN":
            return "BEARISH"
        if weeks_no_bos >= 4:
            return "RANGING"
        return "TRANSITIONING"

    # ── From RANGING ──────────────────────────────────────────
    if prior_state == "RANGING":
        if sig == "BOS_UP" or (base == "BULLISH" and sig == "CHOCH_UP"):
            return "BULLISH"
        if sig == "BOS_DOWN" or (base == "BEARISH" and sig == "CHOCH_DOWN"):
            return "BEARISH"
        if sig in ("CHOCH_UP", "CHOCH_DOWN"):
            return "TRANSITIONING"
        return "RANGING"

    # ── No prior state (first run) — follow sequence directly ─
    return base


# ─────────────────────────────────────────────────────────────
# Step 7 — Main analysis per symbol
# ─────────────────────────────────────────────────────────────

def analyze_symbol(
    symbol: str,
    data_dir: str,
    min_weeks: int = MIN_WEEKS,
    prior_state: Optional[str] = None,
    weeks_no_bos: int = 0,
) -> WeeklyStructure:
    """
    Full weekly structure analysis for one symbol.

    Args:
        symbol:      Token symbol (e.g. "BTC")
        data_dir:    Path to daily CSV cache
        min_weeks:   Minimum closed weekly candles required
        prior_state: Previous known state (for state machine continuity)
        weeks_no_bos: Weeks elapsed since last BOS (for RANGING transition)

    Returns:
        WeeklyStructure dataclass
    """
    # ── Load daily data ──────────────────────────────────────
    daily = load_daily(symbol, data_dir)
    if daily is None:
        return WeeklyStructure(
            symbol=symbol, state="UNKNOWN", prior_state=None,
            sequence="", last_swing_high=None, last_swing_high_week=None,
            last_swing_low=None, last_swing_low_week=None,
            signal=StructureSignal("NONE", 0, 0, 0, 0, ""),
            weeks_in_state=0, closed_weekly_candles=0, as_of_week="",
            error=f"No daily cache found: data/{symbol.upper()}_daily.csv",
        )

    # ── Resample to weekly ───────────────────────────────────
    weekly = resample_weekly(daily)
    n_weeks = len(weekly)

    if n_weeks < min_weeks:
        return WeeklyStructure(
            symbol=symbol, state="INSUFFICIENT_DATA", prior_state=None,
            sequence="", last_swing_high=None, last_swing_high_week=None,
            last_swing_low=None, last_swing_low_week=None,
            signal=StructureSignal("NONE", 0, 0, 0, 0, ""),
            weeks_in_state=0, closed_weekly_candles=n_weeks, as_of_week="",
            error=f"Only {n_weeks} closed weeks — need {min_weeks}",
        )

    as_of_week = weekly.index[-1].strftime("%Y-%m-%d")

    # ── Detect pivots ────────────────────────────────────────
    pivots = detect_pivots(weekly, k=PIVOT_K)
    highs, lows = get_last_n_pivots(pivots)

    # ── Fix stale pivots (sustained trends without confirmation)
    highs, lows = fix_stale_pivots(weekly, highs, lows)

    # ── Classify sequence ────────────────────────────────────
    sequence = classify_sequence(highs, lows)

    # ── Detect BOS / CHOCH on latest candle ─────────────────
    signal = detect_signal(weekly, highs, lows, prior_state or sequence_to_trend(sequence))

    # ── Resolve state ────────────────────────────────────────
    state = resolve_state(sequence, signal, prior_state, weeks_no_bos)

    # ── Compute weeks_in_state (approximate from sequence stability) ─
    # Simple approximation: count consecutive weeks the sequence hasn't changed
    # For a full state machine, this would be tracked externally across runs
    weeks_in_state = _estimate_weeks_in_state(weekly, highs, lows, state)

    return WeeklyStructure(
        symbol=symbol,
        state=state,
        prior_state=prior_state,
        sequence=sequence,
        last_swing_high=round(highs[-1].price, 6) if highs else None,
        last_swing_high_week=highs[-1].week_start if highs else None,
        last_swing_low=round(lows[-1].price, 6) if lows else None,
        last_swing_low_week=lows[-1].week_start if lows else None,
        signal=signal,
        weeks_in_state=weeks_in_state,
        closed_weekly_candles=n_weeks,
        as_of_week=as_of_week,
    )


def _estimate_weeks_in_state(
    weekly: pd.DataFrame,
    highs: list[SwingPoint],
    lows: list[SwingPoint],
    current_state: str,
) -> int:
    """
    Approximate how many weeks the asset has been in its current state.
    Counts backward from the most recent confirmed swing that established the state.
    """
    if not highs or not lows:
        return 0

    # Use the most recently confirmed pivot as the state anchor
    last_pivot_week = max(highs[-1].confirmed_on, lows[-1].confirmed_on)
    try:
        anchor = pd.Timestamp(last_pivot_week, tz="UTC")
        last_week = weekly.index[-1]
        delta = (last_week - anchor).days // 7
        return max(0, delta)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────
# Output formatting
# ─────────────────────────────────────────────────────────────

STATE_EMOJI = {
    "BULLISH":       "🟢",
    "BEARISH":       "🔴",
    "TRANSITIONING": "🟡",
    "RANGING":       "⚪",
    "UNKNOWN":       "❓",
    "INSUFFICIENT_DATA": "⚠️",
}

SIGNAL_LABEL = {
    "BOS_UP":    "BOS ↑  (trend continuation — new high)",
    "BOS_DOWN":  "BOS ↓  (trend continuation — new low)",
    "CHOCH_UP":  "CHOCH ↑  ⚡ potential REVERSAL — watch for follow-through",
    "CHOCH_DOWN":"CHOCH ↓  ⚡ potential REVERSAL — watch for follow-through",
    "NONE":      "None",
}

INTEGRATION_HINT = {
    "BULLISH":       "No constraint — weekly macro supports longs",
    "BEARISH":       "Hard skip — do not enter longs",
    "TRANSITIONING": "Halve position size — structure not confirmed",
    "RANGING":       "Reduce size — no macro direction",
}


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "—"
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def print_result(ws: WeeklyStructure):
    emoji = STATE_EMOJI.get(ws.state, "?")
    sep = "─" * 60

    print(f"\n{sep}")
    print(f"  {ws.symbol}  Weekly Market Structure")
    print(f"  As of week starting: {ws.as_of_week}  ({ws.closed_weekly_candles} closed weeks)")
    print(sep)

    if ws.error:
        print(f"  ⚠️  {ws.error}")
        return

    print(f"  State:     {emoji} {ws.state}")
    print(f"  Sequence:  {ws.sequence}")
    print(f"  In state:  ~{ws.weeks_in_state} weeks")

    print(f"\n  Last swing HIGH : {fmt_price(ws.last_swing_high)}"
          f"  (week {ws.last_swing_high_week})")
    print(f"  Last swing LOW  : {fmt_price(ws.last_swing_low)}"
          f"  (week {ws.last_swing_low_week})")

    sig = ws.signal
    print(f"\n  Signal this week:  {SIGNAL_LABEL.get(sig.signal, sig.signal)}")
    if sig.signal != "NONE":
        print(f"    Broken level : {fmt_price(sig.broken_level)}")
        print(f"    Break price  : {fmt_price(sig.break_price)}"
              f"  (+{sig.break_margin_pct:.2f}% beyond level)")
        strength_bar = "●" * sig.strength + "○" * (3 - sig.strength)
        print(f"    Strength     : {strength_bar} ({sig.strength}/3)")

    hint = INTEGRATION_HINT.get(ws.state, "")
    if hint:
        print(f"\n  SR Analyzer gate:  {hint}")

    print(sep)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Weekly market structure analysis (resampled from daily cache)",
        epilog=(
            "Examples:\n"
            "  python3 weekly_structure.py BTC\n"
            "  python3 weekly_structure.py BTC ETH SOL BNB\n"
            "  python3 weekly_structure.py --all\n"
            "  python3 weekly_structure.py BTC --json\n"
            "  python3 weekly_structure.py BTC --min-weeks 20\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tokens", nargs="*", help="Token symbols to analyze")
    parser.add_argument("--all",       action="store_true",
                        help="Analyze all tokens with a daily cache in data-dir")
    parser.add_argument("--data-dir",  default="data",
                        help="Daily CSV cache directory (default: data/)")
    parser.add_argument("--json",      action="store_true",
                        help="Output JSON instead of human-readable text")
    parser.add_argument("--min-weeks", type=int, default=MIN_WEEKS,
                        help=f"Minimum closed weekly candles required (default: {MIN_WEEKS})")

    args = parser.parse_args()

    # ── Determine token list ──────────────────────────────────
    if args.all:
        data_path = Path(args.data_dir)
        if not data_path.exists():
            print(f"Error: data directory '{args.data_dir}' not found.", file=sys.stderr)
            sys.exit(1)
        tokens = sorted([
            f.stem.replace("_daily", "").upper()
            for f in data_path.glob("*_daily.csv")
        ])
        if not tokens:
            print(f"No *_daily.csv files found in '{args.data_dir}'.", file=sys.stderr)
            sys.exit(1)
    elif args.tokens:
        tokens = [t.upper() for t in args.tokens]
    else:
        parser.print_help()
        sys.exit(0)

    # ── Run analysis ──────────────────────────────────────────
    results = []
    for symbol in tokens:
        ws = analyze_symbol(symbol, args.data_dir, min_weeks=args.min_weeks)
        results.append(ws)

    # ── Output ────────────────────────────────────────────────
    if args.json:
        output = []
        for ws in results:
            d = asdict(ws)
            # Flatten signal into top-level for readability
            d["signal"] = asdict(ws.signal)
            output.append(d)
        print(json.dumps(output, indent=2))
    else:
        for ws in results:
            print_result(ws)
        if len(results) > 1:
            # Summary table
            print(f"\n{'─' * 60}")
            print(f"  SUMMARY  ({len(results)} tokens)")
            print(f"{'─' * 60}")
            for ws in results:
                emoji = STATE_EMOJI.get(ws.state, "?")
                sig_short = ws.signal.signal if ws.signal.signal != "NONE" else ""
                choch_flag = "  ⚡ CHOCH" if "CHOCH" in ws.signal.signal else ""
                print(f"  {ws.symbol:<8}  {emoji} {ws.state:<16}  {ws.sequence:<12}{choch_flag}")
            print(f"{'─' * 60}\n")


if __name__ == "__main__":
    main()
