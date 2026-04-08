# Filter Catalog — Complete Reference

> **Note (2026-04-04):** The Cluster A/C system referenced below has been replaced by 4 canonical token tiers (TOP_3, SELECTED, TOP_20, ALL) defined in `core/config.py`. Historical backtest results below were generated with the original 49-token / 2-cluster system.

**Date:** 2026-04-03
**Validated on:** 49 tokens, walk-forward (6m IS / 6m OOS), 3 OOS folds
**OOS Period:** Jan 2024 → Jul 2025 (547 trading days)
**Base setup:** S/R TP/SL exits, 7-day timeout, DI+ flip early exit, single position (highest RSI), full compounding

---

## 1. Available Filters

### 1.1 BTC RSI Floor (`btc_rsi_floor`)

| | |
|---|---|
| **Rule** | BTC RSI(10) >= 50 |
| **Purpose** | Market-wide risk gate — don't trade when BTC is weak |
| **Implementation** | `core/filters.py` → `btc_rsi_floor()` |
| **Parameters** | period=10, threshold=50 |

**Indicator:** Simple moving average RSI on BTC close, 10-period.

**Logic:** When BTC RSI drops below 50, the entire crypto market is likely in risk-off mode. Skip all entries regardless of token strength.

---

### 1.2 Token RSI Momentum (`token_rsi_momentum`)

| | |
|---|---|
| **Rule** | Token RSI(10) > 60 |
| **Purpose** | Momentum confirmation — only enter tokens with upward momentum |
| **Implementation** | `core/filters.py` → `token_rsi_momentum()` |
| **Parameters** | period=10, threshold=60 |

**Logic:** RSI above 60 indicates the token has recent upward momentum. Below 60, the move lacks conviction.

---

### 1.3 Token ADX Trend (`token_adx_trend`)

| | |
|---|---|
| **Rule** | ADX(14) > 20 |
| **Purpose** | Trend must exist — avoid ranging/choppy markets |
| **Implementation** | `core/filters.py` → `token_adx_trend()` |
| **Parameters** | period=14, threshold=20 |

**Indicator:** Wilder-smoothed ADX, 14-period.

**Logic:** ADX below 20 means no trend. Above 20, a directional move is in play. Does not indicate direction, only trend strength.

---

### 1.4 Token DI+ Bullish (`token_di_bullish`)

| | |
|---|---|
| **Rule** | DI+(14) > DI-(14) |
| **Purpose** | Trend direction must be bullish |
| **Implementation** | `core/filters.py` → `token_di_bullish()` |
| **Parameters** | period=14 |

**Indicator:** Wilder-smoothed DI+/DI-, 14-period.

**Logic:** DI+ > DI- means buyers are stronger than sellers. Combined with ADX > 20, confirms a bullish trend.

---

### 1.5 MT Regime Gate (`mt_regime_gate`)

| | |
|---|---|
| **Rule** | SMA40 regime != D (downtrend) |
| **Purpose** | Block entries in confirmed downtrends |
| **Implementation** | `core/filters.py` → `mt_regime_gate()` |
| **Parameters** | sma=40, slope_bars=20, confirm=1 |

**Regime states:**
- **U (Up):** price > SMA40 AND SMA40 rising over 20 bars
- **D (Down):** price < SMA40 AND SMA40 falling over 20 bars
- **T (Transition):** price and slope disagree

**Logic:** Gate is OFF (no trade) when regime is D. ON when U or T.

**History:** Originally a 3-timeframe (LT/MT/ST) gate with 27-combo lookup table. Walk-forward on 5y data showed massive overfitting. Grid search found SMA40/slope20/confirm1 as optimal single-timeframe filter. The longer slope (20 bars) acts as a natural debouncer, making confirmation bars unnecessary.

---

### 1.6 Bollinger %B (`bollinger_pctb`)

| | |
|---|---|
| **Rule** | BB %B(20, 2σ) < 0.80 |
| **Purpose** | Block overbought entries — prevents buying at the top of the range |
| **Implementation** | `core/filters.py` → `bollinger_pctb()` |
| **Parameters** | period=20, std_mult=2.0, threshold=0.80 |

**Indicator:** %B = (price - lower_band) / (upper_band - lower_band), where bands are SMA(20) ± 2σ.

**Interpretation:**
- %B > 1.0: price above upper band (extremely overbought)
- %B > 0.80: price in top 20% of range (overbought)
- %B = 0.50: price at SMA (middle)
- %B < 0.20: price in bottom 20% (oversold)

**Logic:** When %B >= 0.80, the token is near the top of its Bollinger range. Momentum entries at this level often reverse before reaching S/R take-profit levels, resulting in large stop-loss hits.

**Key discovery:** This filter was the breakthrough for the 49-token universe. It directly fixes the win/loss asymmetry (avg loss was 1.6x avg win) by preventing entries on overbought tokens that immediately reverse.

---

### 1.7 Relative Volume (`relative_volume`)

| | |
|---|---|
| **Rule** | RVOL >= 1.5 |
| **Purpose** | Volume must confirm the price move |
| **Implementation** | `core/filters.py` → `relative_volume()` |
| **Parameters** | period=20, threshold=1.5 |

**Indicator:** RVOL = current volume / SMA(volume, 20).

**Interpretation:**
- RVOL < 1.0: below-average volume (no conviction)
- RVOL 1.0-1.5: normal volume
- RVOL 1.5-2.0: elevated volume (sweet spot)
- RVOL > 4.0: extreme volume (possible exhaustion)

**Logic:** A price move without volume support is more likely to fail. Requiring 1.5x average volume ensures the move has institutional participation.

---

### 1.8 Min Risk/Reward (`min_risk_reward`)

| | |
|---|---|
| **Rule** | R:R >= 1.2 |
| **Purpose** | Only enter when potential gain exceeds potential loss |
| **Implementation** | `core/filters.py` → `min_risk_reward()` |
| **Parameters** | threshold=1.2 |

**Logic:** R:R = (TP - entry) / (entry - SL). Requires TP upside to be at least 1.2x the SL downside.

**Warning:** This filter was **counter-productive** on altcoins in walk-forward testing. It destroyed win rate from 55% to 37% because it filtered out trades with tight TP (quick wins) and kept trades with wide TP that rarely hit. **Not recommended for current strategy.**

---

### 1.9 RSI Cap (not a standalone filter in core, used as a parameter)

| | |
|---|---|
| **Rule** | Token RSI(10) <= 80 |
| **Purpose** | Avoid extremely overbought entries |

**Logic:** While RSI > 60 is required for momentum confirmation, RSI > 80 often signals exhaustion rather than momentum. Capping at 80 creates a "momentum band" (60-80) that captures genuine moves without entering at extremes.

---

## 2. Solo Filter Performance (49 tokens, 3 OOS folds)

Each filter tested alone on top of base (no filters), using S/R TP/SL + 7d timeout.

| Filter | Trades | WR% | Avg Win | Avg Loss | W/L | Δ Ret vs base | Δ MaxDD | Verdict |
|--------|--------|-----|---------|----------|-----|-------------|---------|---------|
| **Base (no filter)** | **1238** | **44.8%** | — | — | — | **-36.4%** | **67.6%** | — |
| ADX > 20 | 947 | 45.5% | — | — | — | +8.7% | -8.9% | HELPS |
| DI+ > DI- | 700 | 44.6% | — | — | — | +9.4% | -18.3% | HELPS |
| **RSI > 60** | **499** | **45.3%** | — | — | — | **+20.1%** | **-24.5%** | **HELPS** |
| BTC RSI >= 50 | 836 | 42.9% | — | — | — | -4.6% | -5.6% | MIXED |
| MT gate | 770 | 47.2% | — | — | — | +13.0% | -14.5% | HELPS |
| **R:R >= 1.2** | **786** | **38.1%** | — | — | — | **+19.6%** | **-13.8%** | **HELPS** |

*Source: `research/backtest_filter_impact.py`*

---

## 3. New Filter Solo Performance (49 tokens, on top of 4 momentum + MT gate)

Base here = btc_rsi + token_rsi + adx + di + mt_gate + DI flip exit.

| Filter | Trades | WR% | Avg Win | Avg Loss | W/L | Ret% | MaxDD |
|--------|--------|-----|---------|----------|-----|------|-------|
| **Base (4 mom + MT)** | **97** | **54.6%** | **+5.9%** | **-8.1%** | **0.73** | **-25.0%** | **47.6%** |
| + RSI cap 80 | 92 | 54.3% | +7.5% | -8.7% | 0.86 | -12.5% | 44.7% |
| + RVOL >= 1.5 | 86 | 55.8% | +8.7% | -9.6% | 0.91 | -1.8% | 42.2% |
| **+ BB %B < 0.80** | **74** | **48.6%** | **+8.2%** | **-6.8%** | **1.21** | **+8.7%** | **31.4%** |
| + Weekly trend | 94 | 56.4% | +6.6% | -8.3% | 0.79 | -16.3% | 45.8% |

**BB %B is the only filter that turned the strategy profitable** and pushed W/L above 1.0.

*Source: `research/backtest_new_filters.py`*

---

## 4. Combination Performance (49 tokens, on top of 4 momentum + MT gate)

| Combo | Trades | WR% | W/L | Ret% | MaxDD |
|-------|--------|-----|-----|------|-------|
| BB %B alone | 74 | 48.6% | 1.21 | **+8.7%** | 31.4% |
| **BB + RSI cap 80** | **75** | **53.3%** | **1.04** | **+10.5%** | **33.1%** |
| BB + Weekly | 72 | 48.6% | 1.22 | +6.6% | 34.6% |
| BB + RVOL | 32 | 43.8% | 0.86 | -11.8% | 27.0% |
| BB + RVOL + Weekly | 29 | 44.8% | 0.77 | -11.5% | 24.2% |

**BB + RSI cap 80** is the best combination: highest return, maintains WR above 50%.

*Source: `research/backtest_new_filters.py`*

---

## 5. Exhaustive 256-Combo Search Results

### 5.1 All 49 Tokens (3 OOS folds)

**Top 10:**

| Rank | Filters | #F | Trades | WR% | W/L | Ret% | DD% | Folds |
|------|---------|----|----|-----|-----|------|-----|-------|
| 1 | **btc_rsi + di + bb_pctb** | 3 | 95 | 53.7% | 1.46 | **+78.6%** | 30.4% | +17%, +226%, -7% |
| 2 | btc_rsi + token_rsi + di + bb_pctb | 4 | 91 | 52.7% | 1.48 | +76.6% | 30.7% | +0%, +226%, +3% |
| 3 | btc_rsi + token_rsi + di + mt_gate + bb_pctb | 5 | 88 | 52.3% | 1.45 | +65.4% | 32.0% | +7%, +196%, -7% |
| 4 | btc_rsi + di + mt_gate + bb_pctb | 4 | 94 | 53.2% | 1.39 | +64.8% | 33.6% | +22%, +196%, -24% |
| 5 | btc_rsi + di + bb_pctb + rsi_cap | 4 | 96 | 55.2% | 1.27 | +59.4% | 36.6% | -3%, +168%, +14% |
| 12 | **btc_rsi + adx + mt_gate + bb_pctb + rsi_cap** | 5 | 99 | **56.6%** | 1.04 | +25.4% | 38.4% | **+30%, +35%, +11%** |

**#1** = highest return but volatile. **#12** = most consistent (3/3 folds profitable).

**Filter frequency in top 20:**
- btc_rsi: 100% — **essential**
- bb_pctb: 75% — **critical**
- rsi_cap: 50%
- di: 45%
- mt_gate: 40%
- token_rsi: 40%
- adx: 35%
- rvol: 10% — drop it

*Source: `research/backtest_8filter_combos.py`*

### 5.2 17-Token Universe (4 OOS folds)

| Rank | Filters | Trades | WR% | Ret% | Folds |
|------|---------|--------|-----|------|-------|
| 1 | token_rsi + di + mt_gate + rvol | 126 | 64.3% | +65.2% | -5%, +313%, -24%, -24% |
| 10 | **btc_rsi + adx + di + mt_gate + rsi_cap** | 129 | **63.6%** | +53.7% | **+144%, +89%, -9%, -9%** |

**#10** is the consistent pick: small losses (-9%), large wins (+144%, +89%).

**Filter frequency in top 20:** di 60%, mt_gate 60%, token_rsi 55%, btc_rsi 50%, rsi_cap 50%, rvol 45%, adx 35%, **bb_pctb 0%**

BB %B is useless on the 17-token universe (all high-liquidity tokens).

*Source: `research/backtest_17token_combos.py`*

### 5.3 Cluster A: High-Liquidity (13 tokens, 4 OOS folds)

| Rank | Filters | Trades | WR% | Ret% | Folds |
|------|---------|--------|-----|------|-------|
| 1 | **btc_rsi + di + mt_gate + rvol** | 99 | **63.6%** | **+79.9%** | -11%, +317%, -9%, +22% |
| 6 | btc_rsi + di + mt_gate | 133 | 66.2% | +71.0% | +22%, +227%, -24%, +59% |

**Filter frequency in top 20:** rvol 75%, di 65%, btc_rsi 60%, mt_gate 55%, **adx 0%, bb_pctb 0%**

*Source: `research/backtest_cluster_combos.py --cluster A`*

### 5.4 Cluster C: Small/Volatile (34 tokens, 3 OOS folds)

| Rank | Filters | Trades | WR% | W/L | Ret% | Folds |
|------|---------|--------|-----|-----|------|-------|
| 1 | **btc_rsi + token_rsi + di + rsi_cap** | 85 | 52.9% | 1.34 | **+61.8%** | +31%, +166%, -12% |
| 5 | btc_rsi + token_rsi + di + mt_gate | 92 | 55.4% | 1.20 | +40.6% | **+68%, +44%, +10%** |
| 9 | btc_rsi + adx + di + mt_gate + bb_pctb + rvol + rsi_cap | 30 | 63.3% | 1.20 | +36.2% | +9%, +78%, +22% |

**Filter frequency in top 20:** btc_rsi 100%, di 75%, mt_gate 75%, rvol 70%, token_rsi 65%, bb_pctb 45%, rsi_cap 45%

**#5** is the most consistent (3/3 folds profitable).

*Source: `research/backtest_cluster_combos.py --cluster C`*

---

## 6. Filter Interaction Summary

### What works everywhere
- **btc_rsi >= 50**: universal market gate (100% of top combos on volatile tokens)
- **DI+ > DI-**: directional confirmation (65-75% of top combos across all universes)

### What works on liquid tokens (Cluster A)
- **RVOL >= 1.5**: volume confirmation (75% of top 20)
- **MT gate**: regime filter (55% of top 20)

### What works on volatile tokens (Cluster C)
- **token_rsi > 60**: momentum threshold (65% of top 20)
- **rsi_cap <= 80**: prevents overbought entries (45% of top 20)
- **BB %B < 0.80**: overbought protection (45% of top 20)

### What works on large mixed universes (49 tokens)
- **BB %B < 0.80**: essential (75% of top 20) — the key discovery
- Fixes W/L ratio from 0.73 to 1.46

### What doesn't work
- **ADX > 20**: rarely appears in top combos (0% for liquid, 10% for volatile)
- **R:R >= 1.2**: destroys win rate on altcoins (55% → 37%)
- **RVOL on volatile tokens**: over-filters (kills trades)
- **BB %B on liquid tokens**: filters out valid momentum entries (0% in top 20)
- **More than 5-6 filters**: diminishing then negative returns

### Optimal filter count
| Filters | Avg return (49 tokens) | Best combo |
|---------|----------------------|------------|
| 0 | -17.9% | — |
| 1 | -22.1% | btc_rsi (-8.9%) |
| 2 | -14.3% | btc_rsi + bb_pctb (+24.7%) |
| **3** | -7.6% | **btc_rsi + di + bb_pctb (+78.6%)** |
| 4 | -2.7% | btc_rsi + token_rsi + di + bb_pctb (+76.6%) |
| 5 | -1.1% | btc_rsi + token_rsi + di + mt_gate + bb_pctb (+65.4%) |
| 6 | -2.6% | — |
| 7 | -4.9% | — |
| 8 | -9.5% | all filters (-9.5%) |

**Sweet spot: 3-5 filters.** More is worse.

---

## 7. Recommended Configurations

### For 49-token universe (aggressive)

```
Filters: btc_rsi + di + bb_pctb
WR: 53.7%  |  W/L: 1.46  |  Ret: +78.6%/fold  |  MaxDD: 30.4%
```

### For 49-token universe (consistent)

```
Filters: btc_rsi + adx + mt_gate + bb_pctb + rsi_cap
WR: 56.6%  |  W/L: 1.04  |  Ret: +25.4%/fold  |  MaxDD: 38.4%
3/3 folds profitable
```

### For 17-token curated universe

```
Filters: btc_rsi + adx + di + mt_gate + rsi_cap
WR: 63.6%  |  Ret: +53.7%/fold  |  MaxDD: 38.6%
Folds: +144%, +89%, -9%, -9%
```

### For Cluster A only (13 liquid tokens)

```
Filters: btc_rsi + di + mt_gate + rvol
WR: 63.6%  |  Ret: +79.9%/fold  |  MaxDD: 33.0%
```

### For Cluster C only (34 volatile tokens)

```
Filters: btc_rsi + token_rsi + di + rsi_cap
WR: 52.9%  |  W/L: 1.34  |  Ret: +61.8%/fold  |  MaxDD: 37.6%
```

---

## 8. Exit Rules (same for all configurations)

| Exit | Rule | Notes |
|------|------|-------|
| **Take Profit** | Nearest resistance from S/R model | Cascades past 1 ATR proximity |
| **Stop Loss** | Nearest support from S/R model | Cascades past 1 ATR proximity |
| **Timeout** | Exit at close after 7 days | 7d beats 10d on OOS data |
| **DI+ flip** | Exit at close when DI+ < DI- | Early exit prevents larger losses |

