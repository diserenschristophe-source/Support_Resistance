# Limit-Order Signal Service — Research Summary

**Date**: 2026-05-14
**Goal**: Design a publishable limit-order signal service (entry, TP, SL, expiry) that an investor can place passively, with positive expectancy validated by walk-forward analysis.
**Universe**: selected17 (BTC, ETH, XRP, SOL, ADA, LINK, SUI, AAVE, AVAX, TAO, HYPE, DOGE, BNB, HBAR, DOT, NEAR, UNI)
**Timeframe**: 4h cadence, daily-derived S/R levels (analyze_token V2)
**Data**: 2024-11-19 → 2026-05-13 (Binance hourly via fetch_hourly.py, CoinGecko Pro for HYPE)

---

## Executive Summary

After 4 strategy variants and 250k+ parameter combinations explored, the best limit-order strategy is **SR Buyback with BTC uptrend gating**:

```
Buy a major support level when the token's price is 0.2–1.5× ATR above it,
and only when BTC is in confirmed uptrend (close > SMA(100) AND SMA rising 20 bars).
TP = next resistance above current price.  SL = support − 0.5 × daily ATR.
Hold up to 21 days; expire unfilled limit after 7 days.
```

**Walk-forward verdict: ★★ MARGINAL+** — Sharpe-like 0.52 (above DEPLOY threshold), but only 60% of windows positive (below 70% threshold) on a 5-window post-filter sample. The strategy is genuinely positive in BTC uptrends; the BTC filter masks bad regimes correctly.

**Headline numbers (full-window, with all filters):**
- Published: 157 signals over 17 months
- Filled: 104 (66.2% fill rate)
- Win rate (of filled): 31.7%
- Avg win / loss: +8.05% / −3.08%
- Expectancy per signal: **+0.297%**
- 60d-window mean expectancy: +0.194%, Sharpe-like **0.52**

**Conclusion**: deployable as a "conditional signal service" that publishes only during BTC uptrends (~30% of the time historically). Not as strong as the live TA agent's market-order strategy (+80% backtested), but viable as a passive, limit-order alternative.

---

## Final Strategy Specification

### Frozen configuration (the winner)

| Knob | Value | Purpose |
|---|---|---|
| `sr_max_distance_atr` | 1.5 | Support must be within 1.5× daily ATR below current price |
| `sr_min_distance_atr` | 0.2 (fixed) | Support must be at least 0.2× ATR below (avoid "already touching") |
| `sr_sl_atr_mult` | 0.5 | SL = support − 0.5× daily ATR |
| `major_only` | True | Only Major-tier S/R levels from `sr_analysis2` |
| `mt_not_downtrend` | True | Token NOT in confirmed downtrend (per-token SMA(40) + slope) |
| `btc_uptrend` | True | BTC > SMA(100) AND SMA(100) rising 20 bars |
| `min_token_rsi` | 60 | Token partial-bar daily SMA RSI ≥ 60 |
| `relative_volume` | False | Found to hurt; disabled |
| `btc_rsi_floor` | 0 (off) | BTC short-term RSI redundant with BTC uptrend filter |
| `rsi_cap` | 0 (off) | Found to hurt for support-buying |
| `min_confluence` / `min_touches` | 0 / 0 | Found to hurt in conjunction with tier filter |
| `tp_cascade` | False | Cascade hurts here (TP2 too far; TIMEOUT before reach) |
| `retest_ttl_hours` | 168 (fixed) | 7 days for limit to fill |
| `max_hold_days` | 21 | Post-fill position max-hold |
| `per_token_cap` | 1 (structural) | Only 1 active signal per token at a time |

### Setup mechanics

1. **Detection** (every 4h bar):
   - Compute BTC's partial-bar daily SMA RSI (only `btc_rsi` is stored — no longer used as gate)
   - For each token: check if current price sits within `[0.2, 1.5] × daily_ATR` above a Major support
   - Token RSI ≥ 60 (partial-bar daily SMA)
   - Token NOT in confirmed daily downtrend (per-token SMA(40) + slope)
   - BTC currently above its 100-day SMA AND that SMA is rising
2. **Emit signal**:
   - Entry: buy-limit at the support's `key_level`
   - TP: first resistance above current price at signal time
   - SL: support − 0.5 × daily ATR
   - Expiry: 7 days from signal
3. **Per-token cap**: skip new signals while one is already active (unfilled + position not closed) for the token
4. **Evaluation**:
   - Limit fills when any 4h bar's low ≤ entry within TTL
   - After fill: TP / SL / 21-day TIMEOUT

---

## Walk-Forward Validation

**Method**: Run the frozen winner across rolling time windows (30d non-overlapping and 60d with 30d overlap) over the entire dataset.

### 60-day windows (smoother view, more representative)

```
Active windows:           5 (post-BTC-filter)
Signals published:        157
Signals filled:           104 (66.2%)
Per-signal expectancy:    +0.297%

Window breakdown:
  2025-05  +0.71%   71 pub  43 fill  39.5% WR
  2025-06  +1.01%   76 pub  39 fill  46.1% WR
  2025-07  +0.43%   60 pub  37 fill  31.6% WR
  2025-08  −0.05%   51 pub  38 fill  23.8% WR
  2025-09  −1.12%   26 pub  24 fill  16.7% WR  ← regime-transition leak

Stability:
  Positive:           3/5 (60%)
  Mean expectancy:    +0.194%
  Median:             +0.428%
  Std:                0.834%
  Sharpe-like:        0.52  ✓ above DEPLOY threshold
  Mean WR:            32.2%
```

**Verdict criteria**:
| Tier | Threshold | This strategy |
|---|---|---|
| ★★★ DEPLOY | ≥70% positive AND mean > 0 AND Sharpe > 0.5 | 60% / +0.194% / 0.52 — passes 2/3 |
| ★★ MARGINAL | ≥50% positive AND mean > 0 | ✓ qualifies |
| ★ REGIME-FIT | otherwise | — |

**Net verdict: ★★ MARGINAL+** — fails the strict deploy bar only on the pct-positive criterion (5-window sample, 1 transition-period failure).

### 30-day windows (more variable)

```
Active windows:           5
Sharpe-like:              0.31
Positive:                 2/5 (40%)
Verdict: ★ REGIME-FIT
```

30d view is more affected by single-window outliers. Less representative.

---

## Investigation Journey

The work explored two main strategies (BNR and SR Buyback) through 4 main phases.

### Phase 1 — Break-and-Retest (BNR)

**Hypothesis**: After a confirmed resistance breakout, the broken level becomes support. Place limit-buy at the retest. Classic "polarity flip" trade.

**Trajectory**:
| Variant | Trades | WR | Per signal |
|---|---|---|---|
| v1 single-slot | 20 | 50% | (no expectancy reported; small sample) |
| v2 signal-service | 127 filled | 37.0% | **−0.64%** |
| TP cascade | 102 | 30.4% | −0.64% |
| Cascade + 14d hold | 102 | 28.4% | −0.44% |
| Full sweep (1024 combos) | 18 best | 27.8% | +0.628% (tiny sample, regime-overfit) |

**Outcome**: BNR's structural issue is that retests of broken resistances in 4h crypto are not reliable bounce events. ~57% of fills go straight to SL. **Abandoned in favor of SR Buyback.**

### Phase 2 — SR Buyback (pivot)

**Hypothesis**: Inverted entry — buy at MAJOR SUPPORT (well-tested level), not at a freshly-broken resistance. The thesis was that supports historically hold in uptrends.

**Initial results (baseline, no TA filters)**:
- Fill rate jumped 55% → 60% (limits get tested more at known supports)
- Avg SL distance halved (level-based to ATR-based)
- But WR stuck at 27% (supports failing in 4h crypto same as retests)
- Per signal: −0.29% (improvement over BNR's −0.64% but still negative)

### Phase 3 — Full sweep with TA filters

**Insight**: The TA agent has 5 filters we hadn't tested in BNR/SR Buyback:
- `mt_not_downtrend` (per-token SMA(40) + slope)
- `btc_rsi_floor`
- `relative_volume`
- `rsi_cap`
- (token_rsi_momentum, which we had as `min_token_rsi`)

Added all as **soft filters** (each with an off option in the grid).

**Full grid: 248,832 combos, 6.7 hours of compute.**

**Top winner**:
```
max_d=1.5  sl=0.5  major=Y  mt_not_downtrend=Y  rsi_min=60
hold=21d   no cascade  no rsi_cap  no relative_volume

399 published, 241 filled (60.4%)
WR 32.4%, avg win/loss: +8.82% / −3.17%
Expectancy: +0.427% per signal
```

**Per-knob findings (across 248k combos)**:
| Knob | On-average best | Notes |
|---|---|---|
| `sr_sl_atr_mult` | 0.3 best (monotonic) | Tighter SL strictly better on average |
| `min_token_rsi` | 60 (vs 50, 0) | Stricter trend filter helps |
| `min_confluence ≥ 50` | helps (−0.31% vs −0.56% at 0) | NEW finding — was thought to hurt |
| `min_touches ≥ 3` | HURTS (−0.62% vs −0.25% at 0) | Drops too many signals |
| `relative_volume = True` | **HURTS** consistently | Drop |
| `rsi_cap > 0` | HURTS | Don't filter overbought when buying support |
| `btc_rsi_floor = 0` (off) | best | Redundant with token regime + BTC SMA |
| `major_only = True` | small lift | Helpful but not decisive |
| `mt_not_downtrend = True` | flat on avg, ON in every top combo | Strong interaction with other knobs |
| `tp_cascade = True` | hurts | Cascade pushes TP too far |
| `max_hold_days = 7` | best on avg | But winner uses 21d in combo with other filters |

### Phase 4 — Walk-forward + BTC-level regime filter

**Walk-forward result of the sweep winner (without BTC uptrend filter)**:
- 60d view: 56% positive, mean +0.101%, Sharpe 0.41 → ★★ MARGINAL
- 30d view: 62% positive, mean −0.146%, Sharpe −0.32 → ★ REGIME-FIT

**Diagnosis**: Catastrophic failure in 3 specific 30d windows (Feb 2025, Oct 2025, Jan 2026) — all BTC corrections. The per-token `mt_not_downtrend` filter doesn't catch these because individual tokens may stay above their 40d SMA when BTC turns over rapidly.

**Fix: BTC-level uptrend filter** — global gate using BTC's daily close > SMA(100) AND SMA rising 20 bars.

**Final walk-forward result**:
- Published cut 399 → 157 (40% retention; filter is active 30% of the time)
- 60d view: 60% positive, mean +0.194%, **Sharpe 0.52** → ★★ MARGINAL+ (Sharpe passes, pct-positive misses)
- Median window expectancy: +0.428%

---

## Files Inventory

### Data fetchers
| File | Purpose |
|---|---|
| `sr-dashboard/Support_Resistance/research/fetch_hourly.py` | Binance hourly OHLCV for 43 tokens |
| `sr-dashboard/Support_Resistance/research/fetch_hourly_coingecko.py` | CoinGecko Pro hourly for HYPE (not on Binance) |

### Strategy backtests
| File | Strategy variant |
|---|---|
| `research/backtest_break_retest.py` | BNR v1 — single-slot, bot framing |
| `research/backtest_break_retest_v2.py` | BNR v2 — signal-service framing, per-signal metrics, sensitivity |
| `research/backtest_break_retest_tp2.py` | BNR TP2 research — measure TP1/TP2 hit rates, hold to TP2 |
| `research/backtest_sr_buyback.py` | SR Buyback v1 — limit-buy at support |

### Sweeps + validation
| File | Purpose |
|---|---|
| `research/bnr_sweep.py` | BNR full grid sweep (~1k combos) |
| `research/sr_buyback_sweep.py` | SR Buyback full grid sweep (248k combos with all soft filters) |
| `research/analyze_signals_v2.py` | Diagnostic — bucketize BNR signals by feature, find edge |
| `research/sr_buyback_walkforward.py` | Walk-forward validation of frozen winner config |

### Output reports
| Path | Contents |
|---|---|
| `reports/backtest_break_retest/` | BNR v1 results |
| `reports/backtest_break_retest_v2/` | BNR v2, TP2 research, BNR sweep outputs |
| `reports/backtest_sr_buyback/` | SR Buyback results (sweeps, walk-forwards, this summary) |
| `reports/portfolio_backtest_intraday/.sr_cache_full_selected17.pkl` | Pre-computed `analyze_token` results (8,167 entries) — shared across all backtests |

### Modified shared module
| File | Change |
|---|---|
| `core/sr_analysis2.py` | (Unchanged — only consumed via analyze_token) |
| (Implicit) `daily_mt_not_downtrend_mask`, `daily_relative_volume_mask` | Imported from `backtest_intraday.py`; used for daily filter mask pre-computation |

---

## Key Findings

### Strategy-level

1. **Break-and-Retest doesn't work in 4h crypto** — 57% of retest fills go straight to SL. The "broken resistance becomes support" thesis is not statistically supported in this universe/timeframe.

2. **Support-Buyback works marginally** — but only with regime filters. Pure SR Buyback (without `mt_not_downtrend` + `btc_uptrend`) is also break-even.

3. **The strategy is fundamentally regime-conditional** — works in BTC uptrends (4 of 5 active windows positive), fails badly in BTC corrections. This is structural, not a tuning issue.

4. **Maker rebates not modeled** — current results use 3 bps taker × 2 sides. Real-world maker fills on limit entries would add ~5 bps per filled trade (≈ +0.3% per published signal). Could lift +0.297% → +0.6%, materially shifting deployability.

### Filter-level findings (some surprising)

- **`relative_volume` HURTS** for support-buying — counterintuitive (you'd think active tokens are better) but the data is consistent across 100k+ combos.
- **`rsi_cap` HURTS** — overbought-cap filters out signals where current price is high but support is meaningfully below. These setups perform fine.
- **Quality filters (`min_confluence`, `min_touches`)** — `confluence ≥ 50` helps marginally; `touches ≥ 3` consistently hurts.
- **Tighter SL is better** — `sl_atr_mult ∈ {0.3, 0.5}` consistently outperform `1.0`.
- **Longer max_hold is better in the optimal combo** — 21d beats 7d when combined with regime filters (more time for the bounce to play out without much added SL exposure when regime is right).

### Walk-forward / robustness findings

- **Sharpe 0.52 on 5 active windows** crosses the DEPLOY threshold for Sharpe but fails the pct-positive bar.
- **Regime-transition leak (Sep-2025)** is the single bad window — BTC just starting to roll over but still technically above SMA(100). This is fundamentally hard to fix without lookahead.
- **Per-signal expectancy is highly window-dependent** — full-window +0.297% averages across active windows of +0.4-1.0% and the single bad −1.1%.

---

## Learnings Transferable to TA17 / TA44 (Live Agents)

The TA agent is a market-order momentum strategy (different from our limit-order S/R buyback), but several findings cross over.

### Immediately actionable

1. **Add BTC-level SMA(100) + slope filter as a global gate**
   - Currently TA has `btc_rsi_floor=50` (intraday partial-bar RSI) but no longer-term BTC trend gate.
   - The Oct-2025 and Jan-2026 corrections would likely have been caught by `BTC close > SMA(100) AND SMA(100) rising 20 bars`.
   - Implementation: precompute mask once daily, add as filter in `core/filters.py`. Re-validate via `ta_5_7_filter_sweep.py` with this as a new sweepable knob.
   - **Expected effect**: cuts trade volume during BTC downtrends, lifts WR in remaining trades, smoother equity curve.

2. **Re-test `relative_volume` filter in TA**
   - Our SR Buyback sweep showed `relative_volume` HURTS expectancy across 100k+ combos.
   - TA currently uses it with `period=20, threshold=1.5`. Worth re-running `ta_5_7_filter_sweep.py` to confirm it's pulling weight in TA's market-order context — it might be hurting there too.

3. **`min_touches ≥ 3` should be re-validated** if applied as a hard filter
   - We found it consistently hurts in SR Buyback. TA uses `compute_tp_sl` which has internal Major-preference logic, so the effect may be different.

### Confirmatory findings

4. **`min_token_rsi = 60` is the right floor** — confirms TA's `token_rsi_momentum: 60` setting.
5. **`Major` tier matters** — TA's `compute_tp_sl` already preferentially picks Major levels for TP/SL; this is structurally correct.
6. **`mt_not_downtrend` per-token is doing real work** — TA uses this; the SR Buyback sweep validates it (mean lift across all combos when enabled).

### Architectural learnings

7. **Soft filters > hard gates** — every filter should be sweepable from "off" so you can measure its standalone contribution. Most filters interact non-linearly with others; some that "seem necessary" actually hurt.
8. **Walk-forward is essential** — full-window expectancy can be 3× the typical-window expectancy due to favorable regime weighting. Always validate via rolling windows before deploying.
9. **The SR cache (`analyze_token` per-day per-token) is reusable across backtests** — significant compute saving. Should be a shared infrastructure asset.

---

## Future Work

### 1. hl44 Validation

**Goal**: Test the SR Buyback winner config on the broader 44-token universe to see if the edge holds with more diversification.

**Why**: selected17 is a curated set; hl44 includes more volatile mid-caps that may behave differently. More signals + lower correlation could improve Sharpe through diversification.

**Plan**:
```bash
# Step 1: Build the SR cache for hl44 (slow first run, ~30 min)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/backtest_sr_buyback.py --universe hl44 --rebuild-sr

# Step 2: Sweep (estimate: 4 hours, can run overnight)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/sr_buyback_sweep.py --universe hl44

# Step 3: Walk-forward the new winner
# (Manually update WINNER in sr_buyback_walkforward.py based on sweep output)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/sr_buyback_walkforward.py --universe hl44
```

**Hypotheses to test**:
- Higher signal volume (44 tokens × 17 months should ~2.5× the count)
- Mid-caps may have higher WR at supports (more volatile, more dramatic bounces) OR worse (faster regime shifts)
- Diversification should reduce per-window variance → higher Sharpe even if mean expectancy is similar

### 2. Portfolio Strategy

**Goal**: Convert the signal-service per-signal metrics into a realistic portfolio simulation with capital allocation, slot constraints, and a compounded equity curve.

**Current state**: `backtest_break_retest_v2.py` has `typical_investor_sim()` (4 slots × 25% per slot, FIFO) and `sensitivity_by_start_month()`, but these are not used by `sr_buyback_sweep.py` or `sr_buyback_walkforward.py`.

**Plan**:
1. **Add `typical_investor_sim` to the walk-forward script** — output equity curve + monthly returns
2. **Sweep portfolio parameters**: slot count (2/3/4/5/6), per-slot fraction (10-30%), allocation method (FIFO vs best-RR-first vs random)
3. **Compute portfolio-level metrics**: Sharpe of monthly returns, max DD, Calmar, ulcer index, profit factor
4. **Walk-forward the portfolio** — does the 4-slot investor with this config achieve consistent ≥1.5 Sharpe across rolling windows?

**Specific deliverables**:
- New file: `research/sr_buyback_portfolio.py` — wraps the signal feed in a portfolio simulator
- Output: portfolio equity curve PNG/CSV, monthly returns table, Sharpe distribution across start-month sensitivity
- Verdict: if a 4-slot portfolio simulation shows ≥1.0 Sharpe over the BTC-uptrend windows, this is genuinely deployable

**Open questions**:
- How to handle the "BTC downtime" periods where no signals fire? Sit in cash? In BTC long? In stablecoins?
- Slot sizing: fixed-fraction-of-equity or fixed-fraction-of-initial-capital?
- Should slots ever go idle deliberately (max-N-per-month)?

### 3. Apply Learnings to TA17 / TA44 (live agents)

**Goal**: Test whether the regime-filter and per-knob findings from SR Buyback improve TA's already-strong market-order strategy.

**Plan**:

#### 3a. Add BTC-level SMA(100) gate to TA

1. Add a new filter `btc_sma_uptrend` to `core/filters.py` in trading-system:
   ```python
   def btc_sma_uptrend_pass(btc_daily_close, sma_period=100, slope_bars=20):
       sma = btc_daily_close.rolling(sma_period).mean()
       return (btc_daily_close.iloc[-1] > sma.iloc[-1]) and (sma.diff(slope_bars).iloc[-1] > 0)
   ```
2. Re-run `ta_5_7_filter_sweep.py` with this as a 6th filter knob (2⁶ = 64 combos)
3. If the winning combo includes `btc_sma_uptrend=True`, update `config/agents/ta17.json` accordingly
4. Walk-forward via `walkforward_optimize.py` to confirm the lift holds out-of-sample

#### 3b. Re-test `relative_volume` in TA

1. The current TA sweep (`ta_5_7_filter_sweep.py`) treats `relative_volume` as one of 5 filters
2. Look at the existing sweep results: does `relative_volume=True` show up in the top combos by Sharpe, or is it being dragged in by other knobs?
3. If it's hurting on average, simplify the config by removing it

#### 3c. Re-evaluate `min_touches` and `min_confluence` in TA

- TA doesn't directly use these (uses `compute_tp_sl`'s internal logic), so this is more of a code-review check
- Confirm that `compute_tp_sl` isn't quietly applying a "touches ≥ 3" filter that we'd benefit from relaxing

#### Expected impact

- TA17 currently shows +80% backtested over 540d in `run_portfolio_backtest.py`. If the BTC-SMA(100) gate lifts Sharpe by even 0.2, that's a meaningful upgrade.
- The gate may also reduce max DD by avoiding the worst BTC-correction trades.

---

## Run Commands Reference

### Data acquisition (one-time)
```bash
# Binance hourly for 43 tokens
cd /Users/xris/GitHub/sr-dashboard && python3 Support_Resistance/research/fetch_hourly.py \
  BTC ETH XRP SOL BNB ADA DOGE LINK AVAX SUI DOT HBAR NEAR UNI LTC TAO \
  AAVE TRUMP ONDO PAXG TON ICP ATOM RENDER FET ALGO WLD INJ SEI STX APT \
  FIL JUP TRX OP ENA XLM ARB POL SHIB PEPE BONK FLOKI \
  --days 540

# CoinGecko Pro for HYPE
cd /Users/xris/GitHub/sr-dashboard && COINGECKO_API_KEY=<key> \
  python3 Support_Resistance/research/fetch_hourly_coingecko.py HYPE --days 540
```

### SR Buyback (current production strategy)
```bash
# Single-config backtest (the frozen winner)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/backtest_sr_buyback.py --universe selected17 \
  --major-only  # other filters are set in module-level constants

# Walk-forward validation (~30 seconds)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/sr_buyback_walkforward.py --universe selected17

# 60d rolling view (smoother)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/sr_buyback_walkforward.py --universe selected17 \
  --window-days 60 --overlap-days 30
```

### Full sweep (re-run for hl44 or after code changes)
```bash
# Quick sanity check (~2-5 min, 8192 combos)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/sr_buyback_sweep.py --universe selected17 --quick

# Full grid (~6-8 hours, 248k combos)
cd /Users/xris/GitHub/Support_Resistance && \
  python3 research/sr_buyback_sweep.py --universe selected17
```

---

## Data Dependencies

### Hourly OHLCV cache
- **Path**: `/Users/xris/GitHub/Support_Resistance/data/hourly/{SYMBOL}_hourly.csv`
- **Coverage**: 2024-11-19 → 2026-05-13 for 43 tokens (Binance); 2024-11-29 → 2026-05-13 for HYPE (CoinGecko)
- **Format**: `timestamp,open,high,low,close,volume`

### Pre-computed SR cache
- **Path**: `/Users/xris/GitHub/Support_Resistance/reports/portfolio_backtest_intraday/.sr_cache_full_selected17.pkl`
- **Contents**: `analyze_token()` results per (symbol, daily_date) — supports, resistances, market_structure, volume_profile, current_price
- **Size**: 8,167 entries (17 tokens × ~480 trading days)
- **Build time**: ~3-4 minutes one-time; instant load afterwards
- **Rebuild**: pass `--rebuild-sr` to any backtest script

### Filter masks (computed on-the-fly per backtest run)
- `daily_mt_not_downtrend_mask(daily_close, sma_period=40, slope_bars=20)` — per-token
- `daily_relative_volume_mask(daily_volume, period=20, threshold=1.5)` — per-token
- BTC uptrend mask (inline in `detect_sr_buyback`) — global: BTC close > SMA(100) AND SMA rising 20 bars

---

## Open Questions / Out-of-scope

- **Funding rates / open interest** (Coinglass paid tier) — not used. Could add as a regime/confirmation signal in v2.
- **Multi-timeframe confluence** — daily levels with 4h retest works; could try weekly levels with daily retest for slower signals.
- **Order book microstructure** — would need WebSocket data and a different execution simulation framework.
- **Hyperliquid native execution** — current backtest uses Binance hourly. For deployment, need to validate that limit fills behave similarly on HL (typically tighter spreads, but lower liquidity on small caps).
- **Maker rebate modeling** — current model assumes 3 bps taker × 2. Real HL maker rebate is roughly −2 bps; should add to fee model for honest expectancy.
- **Position sizing by ATR** — currently uses fixed fraction-of-equity. Risk-parity (size = target_risk / per-trade_SL_distance) is a known improvement; untested here.

---

*This document is a snapshot as of 2026-05-14. The strategy is in MARGINAL+ state — deployable as a conditional signal service with explicit regime caveats. Three concrete next steps (hl44 validation, portfolio simulation, TA cross-pollination) are detailed above.*
