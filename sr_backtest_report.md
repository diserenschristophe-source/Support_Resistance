# S/R Model Backtest

**Signal Quality Analysis — Filter Selection Guide**

| | |
|---|---|
| **Period** | July 2023 – April 2026 (~2.7 years) |
| **Tokens** | 45 tokens across 4 inclusive tiers |
| **Total Trades** | 1,173 (after hard gates) |
| **Exit Rules** | SL hit or timeout — NO take-profit exit in backtest |
| **Targets** | R1 (first resistance) and R2 (second resistance) |

> **DISCLAIMER — NOT AN INVESTMENT RECOMMENDATION.** This is a purely retrospective analysis of historical signals. It is **not** a prediction of future performance. Past performance does not guarantee future results. Cryptocurrency markets involve substantial risk of loss.

---

### Key Definitions

| Term | Meaning |
|------|---------|
| **Hit%** | The target price was reached before the stop loss. The trade hit its target. |
| **TO Win%** | The trade didn't reach the target, but timed out in profit anyway. |
| **Win%** | Total winners (Hit% + TO Win%). The rest are losses. |
| **Return/trade** | Average percentage gain or loss per trade, including all outcomes. |
| **Trades/month** | How often this setup triggers. |
| **PF** | Profit Factor — total gains / total losses. Above 1.0 = profitable. |

### Tier Definitions (Inclusive)

- **top_3** — BTC, ETH, SOL (3 tokens)
- **selected** — top_3 + BNB, DOGE, LINK, NEAR, SUI, TAO (9 tokens)
- **top_20** — selected + ADA, AVAX, HBAR, LTC, PAXG, TON, TRX, XLM, XRP (18 tokens)
- **all** — top_20 + 27 additional tokens (45 tokens)

*Each tier includes all tokens from the tiers above it.*

---

## 1. Summary

We tested 1,173 trades across 45 tokens over 2.7 years. The goal: find the best combination of filters, target, and timeout for each tier.

> **Context:** This backtest does NOT exit at the target — it records whether the target was reached. Returns show what would have happened under each strategy. $100 test trade size; percentages are what matter.

### What We Found

The system's profitability comes down to three choices: **which tokens**, **which filter**, and **how long you hold**.

**Large-cap tokens (top_3, selected)** behave differently from broader markets. They trend more reliably — when buying pressure is confirmed (DI+ > DI-), the move tends to sustain for weeks. The best approach is to target **R2** (the second resistance level) and give the trade **up to 21 days** to reach it. The only filter needed is **di_bull**.

**Broader markets (top_20, all)** are noisier. More tokens means more unpredictable moves. The best approach here is to take quick wins: target **R1** (the nearest resistance), set a **7-day timeout**, and use **rsi_cap** (remove overbought entries) as the only filter.

For traders who want **higher hit rates** — meaning the target is actually reached more often — a different set of filters and a **14-day timeout** targeting R1 produces the best results. These quality filters (rvol, rr_min, tok_rsi, btc_rsi) reduce the number of trades but increase the percentage that hit their target.

---

## 2. Recommended Presets

### PnL Mode — Maximize Total Profit

Simple setup: one filter per tier, maximum trade volume.

| Tier | Tokens | Strategy | Target | Filter | Trades | /mth | Avg Days | Return | Hit% | TO Win% | Win% | PF |
|------|--------|----------|--------|--------|--------|------|----------|--------|------|---------|------|----|
| top_3 | 3 | Patient 21d | R2 | di_bull | 48 | 1.3 | 16d | +4.89% | 17% | 44% | 60% | 2.50 |
| selected | 9 | Patient 21d | R2 | di_bull | 148 | 4.0 | 14d | +2.74% | 21% | 24% | 45% | 1.48 |
| top_20 | 18 | Quick 7d | R1 | rsi_cap | 518 | 14.1 | 5d | +1.05% | 31% | 24% | 55% | 1.37 |
| all | 45 | Quick 7d | R1 | rsi_cap | 1,116 | 30.3 | 5d | +1.21% | 31% | 23% | 54% | 1.36 |

*Hit% = target reached. TO Win% = timed out positive. Win% = total winners. PF = profit factor.*

### Quality Mode — Maximize Hit Rate (R1 @ 14 days)

Selective setup: more filters, fewer trades, but the target is reached more often.

| Tier | Tokens | Strategy | Target | Filters | Trades | /mth | Avg Days | Return | Hit% | TO Win% | Win% | PF |
|------|--------|----------|--------|---------|--------|------|----------|--------|------|---------|------|----|
| top_3 | 3 | Quality 14d | R1 | tok_rsi | 36 | 1.0 | 9d | +3.07% | 56% | 17% | 72% | 2.32 |
| selected | 9 | Quality 14d | R1 | tok_rsi + di_bull + rvol | 28 | 0.8 | 7d | +3.18% | 61% | 11% | 71% | 1.88 |
| top_20 | 18 | Quality 14d | R1 | tok_rsi + di_bull + rvol + rsi_cap | 43 | 1.2 | 6d | +2.71% | 56% | 14% | 70% | 1.82 |
| all | 45 | Quality 14d | R1 | btc_rsi + di_bull + rvol + rsi_cap | 142 | 3.9 | 6d | +1.37% | 53% | 6% | 59% | 1.31 |

### Why These Presets?

**PnL mode** uses minimal filtering to keep trade volume high. The edge per trade is smaller, but the sheer number of trades generates the highest total profit. Only two filters matter here:
- **di_bull** (DI+ > DI-) for large caps — confirms the trend has buying pressure
- **rsi_cap** (RSI ≤ 80) for broader markets — removes overbought entries that are likely to reverse

**Quality mode** adds volume confirmation (rvol), momentum (tok_rsi), and other selective filters. This cuts the number of trades dramatically but lifts the hit rate from ~30% to 53–61%. The trade-off: fewer opportunities, higher precision.

**The timeout matters as much as the filter:**
- 7 days for Quick: captures fast R1 hits, closes undecided trades before they turn into losses
- 14 days for Quality: gives more time for R1 to be reached while quality filters prevent stop-loss hits
- 21 days for Patient: lets large-cap R2 targets play out — 44% of top_3 trades time out as winners

---

## 3. How Filters Work

Six filters are recorded at entry. This backtest measures what happens when you use them as gates.

| Filter | Condition | What It Means | Used In |
|--------|-----------|---------------|---------|
| di_bull | DI+ > DI- | Buying pressure > selling pressure | PnL |
| rsi_cap | RSI(10) ≤ 80 | Token is not overbought | PnL |
| rvol | Vol ≥ 1.5x avg | Volume confirms conviction | Quality |
| rr_min | R:R ≥ 1.2 | Reward at least 1.2x the risk | Quality |
| btc_rsi | BTC RSI ≥ 50 | Bitcoin momentum positive | Quality |
| tok_rsi | Token RSI > 60 | Token has momentum | Quality |

**PnL filters** (di_bull, rsi_cap) maximize total profit by keeping trade volume high.

**Quality filters** (rvol, rr_min, btc_rsi, tok_rsi) increase hit rate but reduce trade count.

---

## 4. Baseline Performance (No Filters)

All dollar figures use a $100 test trade size. +$2.78 = +2.78% per trade.

### Average Trade Duration

*Timeout trades excluded — only trades that reached a definitive outcome (SL hit or target hit).*

| Category | Average |
|----------|---------|
| Days to R1 hit (when reached) | 7.3 days |
| Days to R2 hit (when reached) | 11.8 days |
| Days to SL hit | 11.1 days |

**R1 is reached fast.** When R1 hits, it takes 7 days on average. R2 takes 12 days. Losers are stopped out in 11 days.

### Targeting R1

| Tier | Tokens | Trades | Total $ | Exp | Hit% | Win% | Avg Win | Avg Loss | PF | Max DD |
|------|--------|--------|---------|-----|------|------|---------|----------|-----|--------|
| top_3 | 3 | 97 | +$25 | +$0.26 | 52.6% | 54.6% | +$8.66 | -$9.86 | 1.06 | $106 |
| selected | 9 | 278 | +$177 | +$0.64 | 52.9% | 55.0% | +$10.53 | -$11.47 | 1.12 | $190 |
| top_20 | 18 | 547 | +$67 | +$0.12 | 49.0% | 53.0% | +$9.60 | -$10.58 | 1.02 | $433 |
| all | 45 | 1,173 | -$291 | -$0.25 | 47.7% | 51.4% | +$11.36 | -$12.53 | 0.96 | $1,324 |

### Targeting R2

| Tier | Tokens | Trades | Total $ | Exp | Hit% | Win% | Avg Win | Avg Loss | PF | Max DD |
|------|--------|--------|---------|-----|------|------|---------|----------|-----|--------|
| top_3 | 3 | 97 | +$267 | +$2.75 | 23.7% | 42.3% | +$19.21 | -$9.29 | 1.51 | $133 |
| selected | 9 | 278 | +$508 | +$1.83 | 22.7% | 38.5% | +$22.20 | -$10.92 | 1.27 | $417 |
| top_20 | 18 | 547 | +$408 | +$0.75 | 21.4% | 39.1% | +$17.94 | -$10.30 | 1.12 | $771 |
| all | 45 | 1,173 | +$234 | +$0.20 | 21.0% | 36.7% | +$21.44 | -$12.09 | 1.03 | $2,245 |

### Exit Behavior — Where the Money Comes From

| Exit | N | Exp R1 | Win% R1 | Exp R2 | Win% R2 |
|------|---|--------|---------|--------|---------|
| SL Hit | 645 | -$7.26 | 24.3% | -$11.19 | 4.5% |
| Timeout | 528 | +$8.31 | 84.5% | +$14.12 | 75.9% |

**85% of timeout trades are profitable.** The stop loss handles losers. Winners need time.

---

## 5. Individual Filter Impact

### Targeting R1 — Best Direction per Filter

*Sorted by lift (impact on expectancy vs baseline).*

| Filter | N | Total $ | Exp | Hit% | Win% | Lift |
|--------|---|---------|-----|------|------|------|
| rr_min PASS | 401 | +$339 | +$0.84 | 40.6% | 46.1% | +1.09% |
| rvol PASS | 298 | +$218 | +$0.73 | 52.3% | 55.0% | +0.98% |
| di_bull FAIL | 481 | +$48 | +$0.10 | 45.7% | 49.5% | +0.35% |
| tok_rsi FAIL | 696 | +$49 | +$0.07 | 47.7% | 51.9% | +0.32% |
| btc_rsi FAIL | 463 | -$7 | -$0.01 | 46.0% | 51.8% | +0.23% |
| rsi_cap PASS | 1,116 | -$191 | -$0.17 | 47.8% | 51.5% | +0.08% |

### Targeting R2 — Best Direction per Filter

*Sorted by lift (impact on expectancy vs baseline).*

| Filter | N | Total $ | Exp | Hit% | Win% | Lift |
|--------|---|---------|-----|------|------|------|
| rr_min PASS | 401 | +$747 | +$1.86 | 20.2% | 36.2% | +1.66% |
| btc_rsi PASS | 710 | +$916 | +$1.29 | 23.4% | 36.6% | +1.09% |
| di_bull PASS | 692 | +$755 | +$1.09 | 21.5% | 38.2% | +0.89% |
| rvol PASS | 298 | +$321 | +$1.08 | 27.9% | 39.9% | +0.88% |
| rsi_cap PASS | 1,116 | +$258 | +$0.23 | 21.1% | 36.6% | +0.03% |
| tok_rsi FAIL | 696 | +$157 | +$0.23 | 19.8% | 37.6% | +0.03% |

---

## 6. Indicator Sweet Spots

### Token RSI — Sweet Spot: 50–60

| Range | N | R1 Return | R1 Hit% | R1 Win% | R2 Return | R2 Hit% | R2 Win% |
|-------|---|-----------|---------|---------|-----------|---------|---------|
| 0–30 | 202 | -1.14% | 40.6% | 46.0% | -3.48% | 16.3% | 32.7% |
| 30–40 | 149 | +0.14% | 45.6% | 49.7% | -0.18% | 15.4% | 35.6% |
| 40–50 | 151 | -1.35% | 44.4% | 48.3% | -1.39% | 16.6% | 33.1% |
| **50–60** | **194** | **+2.38%** | **59.3%** | **62.4%** | **+5.65%** | **29.4%** | **47.9%** |
| 60–70 | 251 | -0.13% | 49.0% | 52.2% | +1.15% | 26.3% | 37.8% |
| 70–80 | 169 | -1.22% | 46.7% | 49.1% | -1.11% | 18.9% | 30.2% |
| 80–100 | 57 | -1.76% | 45.6% | 49.1% | -0.41% | 17.5% | 38.6% |

> Best bucket is 50–60. Current filter requires RSI > 60 — consider widening to 45–65.

### R:R Ratio — Sweet Spot: 1.2–2.0

| Range | N | R1 Return | R1 Hit% | R1 Win% | R2 Return | R2 Hit% | R2 Win% |
|-------|---|-----------|---------|---------|-----------|---------|---------|
| 0–0.5 | 98 | -1.68% | 61.2% | 64.3% | -5.29% | 15.3% | 34.7% |
| 0.5–0.8 | 281 | -1.15% | 51.6% | 54.1% | -0.44% | 22.4% | 38.8% |
| 0.8–1.0 | 175 | -0.21% | 51.4% | 54.3% | -0.59% | 21.7% | 37.1% |
| 1.0–1.2 | 218 | -0.48% | 46.8% | 49.5% | +1.07% | 22.5% | 35.3% |
| **1.2–1.5** | **165** | **+0.53%** | **44.8%** | **47.9%** | **+0.02%** | **19.4%** | **35.2%** |
| **1.5–2.0** | **138** | **+1.60%** | **41.3%** | **47.8%** | **+3.90%** | **19.6%** | **38.4%** |
| 2.0–3.0 | 81 | +0.39% | 37.0% | 42.0% | +2.69% | 25.9% | 35.8% |
| 3.0+ | 17 | -0.08% | 11.8% | 35.3% | -0.75% | 5.9% | 29.4% |

### MT Regime — Transition Beats Uptrend

| Regime | N | R1 Return | R1 Hit% | R1 Win% | R2 Return | R2 Hit% | R2 Win% |
|--------|---|-----------|---------|---------|-----------|---------|---------|
| T (Transition) | 821 | +0.64% | 50.5% | 53.7% | +0.37% | 27.0% | 38.0% |
| U (Uptrend) | 352 | -2.33% | 41.2% | 46.0% | -0.20% | 6.8% | 33.5% |

**Why?** Uptrends blow past old S/R levels. Transition (ranging) markets are where S/R works best.

---

## 7. Timing, Hold & Survival Analysis

### When Does R1 Get Hit?

*Out of 560 trades where R1 was reached.*

| Days | N | % of R1 Hits | Cumulative |
|------|---|-------------|------------|
| 1–2d | 68 | 12.1% | 12.1% |
| 2–4d | 148 | 26.4% | 38.6% |
| 4–7d | 115 | 20.5% | 59.1% |
| 7–10d | 71 | 12.7% | 71.8% |
| 10–15d | 76 | 13.6% | 85.4% |
| 15–30d | 78 | 13.9% | 99.3% |

### If a Trade Survives Past Day X

The longer a trade stays alive without being stopped, the more likely it wins.

| Survived past... | Trades | R1 Win% | R2 Win% |
|-----------------|--------|---------|---------|
| Day 3 | 1,057 | 57% | 40% |
| Day 5 | 978 | 60% | 44% |
| Day 7 | 917 | 63% | 47% |
| Day 10 | 819 | 69% | 52% |
| Day 14 | 715 | 73% | 58% |
| Day 21 | 604 | 81% | 68% |

> **Key:** After 14 days without a stop-loss hit, 73% of trades end profitable. The stop loss handles losers early. Winners need room to run.

### Quarterly Performance

*Quarters with fewer than 3 trades excluded.*

| Quarter | N | R1 Ret | R1 Hit% | R1 Win% | R2 Ret | R2 Hit% | R2 Win% |
|---------|---|--------|---------|---------|--------|---------|---------|
| 2023Q2 | 3 | +1.69% | 66.7% | 66.7% | +2.45% | 33.3% | 33.3% |
| 2023Q3 | 26 | +1.46% | 50.0% | 57.7% | +2.96% | 19.2% | 46.2% |
| 2023Q4 | 106 | +5.94% | 73.6% | 77.4% | +17.51% | 29.2% | 65.1% |
| 2024Q1 | 115 | +1.13% | 53.9% | 57.4% | +0.86% | 23.5% | 44.3% |
| 2024Q2 | 110 | -4.42% | 30.0% | 34.5% | -6.42% | 8.2% | 21.8% |
| 2024Q3 | 110 | +1.33% | 53.6% | 56.4% | -1.39% | 25.5% | 32.7% |
| 2024Q4 | 168 | +0.64% | 49.4% | 53.0% | +2.36% | 23.2% | 41.1% |
| 2025Q1 | 59 | -7.36% | 28.8% | 30.5% | -10.56% | 10.2% | 18.6% |
| 2025Q2 | 138 | +0.38% | 52.9% | 54.3% | +1.35% | 29.7% | 41.3% |
| 2025Q3 | 149 | +0.49% | 49.0% | 55.7% | +0.84% | 24.2% | 39.6% |
| 2025Q4 | 58 | -3.49% | 36.2% | 37.9% | -5.93% | 12.1% | 24.1% |
| 2026Q1 | 124 | -2.84% | 35.5% | 37.9% | -5.58% | 12.1% | 18.5% |
| 2026Q2 | 5 | +2.06% | 40.0% | 60.0% | +3.37% | 20.0% | 60.0% |

---

## Appendix A: Methodology

### Trade Mechanics

- Each day, for each token: run S/R analysis on data up to that day
- Entry: next-day open price (no look-ahead bias)
- Exit: SL hit (intraday low ≤ stop loss) or timeout — close at market
- NO take-profit exit — R1/R2 measured but trade continues until SL or timeout
- One trade per token at a time. No same-day reopen. Fixed $100, no compounding

### TP/SL Cascade Logic (from core/tpsl.py)

**Take Profit (TP)** = key_level (body) of nearest resistance above price. If the nearest resistance is within 1x ATR(14) of price, skip it and cascade to the next one. If no resistance is >= 1 ATR away, fall back to any resistance above price.

**Stop Loss (SL)** = key_level of nearest support below price. Same cascade rule: if nearest support is within 1x ATR of price, cascade to next deeper support. If no support is >= 1 ATR away, fall back to any support below price.

### Backtest Entry Gates

| # | Gate | Description |
|---|------|-------------|
| 1 | ATR distance | TP and SL must be >= 1x ATR(14) from close |
| 2 | MT Regime | No entries when regime = D (downtrend) |
| 3 | Entry > SL | Skip if next-day open gaps below stop loss |
| 4 | Valid R1 | At least one resistance >= 1 ATR above entry must exist |

### PnL Calculation

- Target hit before SL -> $100 x (target - entry) / entry
- SL hit first -> $100 x (SL - entry) / entry
- Timeout -> $100 x (close at timeout day - entry) / entry

---

## Appendix B: Full Combination Tables

*Top 6 combos per tier and strategy, ranked by composite score (Exp × √N × Win%). N < 30 marked with \*.*

### top_3 (3 tokens) — Quick R1 @ 7d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| btc_rsi + di_bull + rsi_cap | 31 | 0.8 | +3.90% | 36% | 26% | 61% | 5.46 | +$121 |
| btc_rsi + di_bull | 37 | 1.0 | +3.46% | 38% | 24% | 62% | 4.15 | +$128 |
| di_bull | 48 | 1.3 | +2.94% | 40% | 21% | 60% | 2.91 | +$141 |
| di_bull + rsi_cap | 42 | 1.1 | +3.19% | 38% | 21% | 60% | 3.21 | +$134 |
| rr_min + di_bull + rsi_cap \* | 18 | 0.5 | +4.47% | 33% | 28% | 61% | 6.02 | +$80 |
| tok_rsi + di_bull + rsi_cap \* | 24 | 0.7 | +3.56% | 33% | 29% | 62% | 4.41 | +$85 |

### top_3 (3 tokens) — Quality R1 @ 14d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| btc_rsi + di_bull + rsi_cap | 31 | 0.8 | +4.02% | 52% | 23% | 74% | 2.95 | +$125 |
| btc_rsi + di_bull | 37 | 1.0 | +3.68% | 54% | 19% | 73% | 2.79 | +$136 |
| rr_min + di_bull + rsi_cap \* | 18 | 0.5 | +5.30% | 44% | 28% | 72% | 4.89 | +$95 |
| di_bull | 48 | 1.3 | +3.21% | 52% | 19% | 71% | 2.40 | +$154 |
| di_bull + rsi_cap | 42 | 1.1 | +3.39% | 50% | 21% | 71% | 2.46 | +$142 |
| btc_rsi + rr_min + di_bull + rsi_cap \* | 17 | 0.5 | +4.97% | 41% | 29% | 71% | 4.45 | +$85 |

### top_3 (3 tokens) — Patient R2 @ 21d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| btc_rsi + di_bull | 37 | 1.0 | +5.79% | 16% | 43% | 60% | 2.84 | +$214 |
| di_bull | 48 | 1.3 | +4.89% | 17% | 44% | 60% | 2.50 | +$235 |
| tok_rsi + di_bull | 30 | 0.8 | +5.50% | 13% | 43% | 57% | 2.56 | +$165 |
| btc_rsi + tok_rsi + di_bull \* | 29 | 0.8 | +5.64% | 14% | 41% | 55% | 2.55 | +$164 |
| di_bull + rsi_cap | 42 | 1.1 | +4.20% | 17% | 40% | 57% | 2.19 | +$176 |
| btc_rsi + di_bull + rsi_cap | 31 | 0.8 | +5.03% | 16% | 39% | 55% | 2.46 | +$156 |

### selected (9 tokens) — Quick R1 @ 7d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| di_bull + rsi_cap | 134 | 3.6 | +1.74% | 37% | 19% | 56% | 1.63 | +$233 |
| rsi_cap | 264 | 7.2 | +1.21% | 32% | 23% | 55% | 1.39 | +$319 |
| di_bull | 148 | 4.0 | +1.54% | 38% | 18% | 56% | 1.54 | +$228 |
| tok_rsi + di_bull + rsi_cap | 71 | 1.9 | +1.93% | 37% | 21% | 58% | 1.76 | +$137 |
| tok_rsi + rsi_cap | 87 | 2.4 | +1.66% | 36% | 22% | 58% | 1.64 | +$145 |
| rr_min + rvol + rsi_cap \* | 25 | 0.7 | +3.13% | 28% | 28% | 56% | 2.15 | +$78 |

### selected (9 tokens) — Quality R1 @ 14d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| rr_min + rvol + rsi_cap \* | 25 | 0.7 | +5.30% | 48% | 24% | 72% | 2.87 | +$132 |
| rr_min + rvol \* | 26 | 0.7 | +4.92% | 46% | 23% | 69% | 2.69 | +$128 |
| rvol | 79 | 2.1 | +2.48% | 48% | 16% | 65% | 1.64 | +$196 |
| tok_rsi + rr_min + rvol + rsi_cap \* | 11 | 0.3 | +5.89% | 46% | 27% | 73% | 3.81 | +$65 |
| rvol + rsi_cap | 72 | 2.0 | +2.50% | 46% | 18% | 64% | 1.62 | +$180 |
| tok_rsi + di_bull + rsi_cap | 71 | 1.9 | +2.36% | 51% | 11% | 62% | 1.66 | +$168 |

### selected (9 tokens) — Patient R2 @ 21d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| tok_rsi + rr_min + rvol + rsi_cap \* | 11 | 0.3 | +9.29% | 36% | 18% | 54% | 3.80 | +$102 |
| di_bull | 148 | 4.0 | +2.74% | 21% | 24% | 45% | 1.48 | +$405 |
| tok_rsi + rr_min + rvol \* | 12 | 0.3 | +8.14% | 33% | 17% | 50% | 3.38 | +$98 |
| rr_min + rvol + rsi_cap \* | 25 | 0.7 | +5.64% | 32% | 16% | 48% | 2.37 | +$141 |
| di_bull + rsi_cap | 134 | 3.6 | +2.50% | 22% | 22% | 43% | 1.43 | +$335 |
| rr_min + rvol \* | 26 | 0.7 | +5.24% | 31% | 15% | 46% | 2.27 | +$136 |

### top_20 (18 tokens) — Quick R1 @ 7d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| di_bull + rsi_cap | 276 | 7.5 | +1.45% | 35% | 22% | 57% | 1.55 | +$399 |
| rsi_cap | 518 | 14.1 | +1.05% | 31% | 24% | 55% | 1.37 | +$544 |
| di_bull | 304 | 8.2 | +1.31% | 35% | 22% | 57% | 1.49 | +$397 |
| tok_rsi + di_bull + rsi_cap | 141 | 3.8 | +1.50% | 38% | 20% | 57% | 1.61 | +$211 |
| btc_rsi + di_bull + rsi_cap | 197 | 5.3 | +1.17% | 36% | 20% | 56% | 1.43 | +$231 |
| tok_rsi + di_bull | 169 | 4.6 | +1.23% | 37% | 20% | 57% | 1.49 | +$209 |

### top_20 (18 tokens) — Quality R1 @ 14d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| tok_rsi + di_bull | 169 | 4.6 | +1.73% | 50% | 13% | 63% | 1.54 | +$293 |
| tok_rsi + di_bull + rsi_cap | 141 | 3.8 | +1.92% | 50% | 12% | 62% | 1.60 | +$270 |
| tok_rsi + di_bull + rvol | 54 | 1.5 | +2.67% | 56% | 13% | 68% | 1.84 | +$144 |
| btc_rsi + rvol | 80 | 2.2 | +2.29% | 51% | 14% | 65% | 1.71 | +$183 |
| rr_min + rvol + rsi_cap | 49 | 1.3 | +2.98% | 41% | 22% | 63% | 1.92 | +$146 |
| tok_rsi + di_bull + rvol + rsi_cap | 43 | 1.2 | +2.71% | 56% | 14% | 70% | 1.82 | +$116 |

### top_20 (18 tokens) — Patient R2 @ 21d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| rr_min + rvol + rsi_cap | 49 | 1.3 | +3.77% | 29% | 18% | 47% | 1.89 | +$185 |
| tok_rsi + rr_min + rvol + rsi_cap \* | 21 | 0.6 | +5.59% | 33% | 14% | 48% | 2.33 | +$117 |
| tok_rsi + di_bull | 169 | 4.6 | +2.16% | 21% | 21% | 42% | 1.43 | +$364 |
| di_bull | 304 | 8.2 | +1.54% | 20% | 23% | 43% | 1.29 | +$468 |
| rr_min + rvol | 53 | 1.4 | +3.37% | 26% | 19% | 45% | 1.80 | +$179 |
| tok_rsi + di_bull + rsi_cap | 141 | 3.8 | +2.26% | 24% | 16% | 40% | 1.44 | +$318 |

### all (45 tokens) — Quick R1 @ 7d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| rr_min + rvol + rsi_cap | 103 | 2.8 | +3.96% | 30% | 32% | 62% | 2.46 | +$408 |
| rr_min + rsi_cap | 378 | 10.3 | +2.26% | 26% | 30% | 56% | 1.74 | +$855 |
| rr_min | 401 | 10.9 | +2.15% | 25% | 30% | 56% | 1.70 | +$862 |
| rr_min + rvol | 112 | 3.0 | +3.47% | 29% | 31% | 60% | 2.19 | +$389 |
| rsi_cap | 1116 | 30.3 | +1.21% | 31% | 23% | 54% | 1.36 | +$1,352 |
| rvol + rsi_cap | 274 | 7.4 | +2.10% | 38% | 19% | 57% | 1.62 | +$575 |

### all (45 tokens) — Quality R1 @ 14d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| rr_min + rvol + rsi_cap | 103 | 2.8 | +3.20% | 40% | 20% | 60% | 1.89 | +$330 |
| rvol + rsi_cap | 274 | 7.4 | +1.67% | 46% | 14% | 60% | 1.39 | +$456 |
| rr_min + rvol | 112 | 3.0 | +2.57% | 38% | 20% | 57% | 1.65 | +$288 |
| rr_min + rsi_cap | 378 | 10.3 | +1.54% | 35% | 16% | 50% | 1.36 | +$581 |
| rvol | 298 | 8.1 | +1.43% | 46% | 13% | 59% | 1.32 | +$425 |
| rsi_cap | 1116 | 30.3 | +0.78% | 41% | 13% | 54% | 1.17 | +$874 |

### all (45 tokens) — Patient R2 @ 21d

| Combo | N | /mth | Return | Hit% | TO W% | Win% | PF | Total |
|-------|---|------|--------|------|-------|------|-----|-------|
| rr_min + rvol + rsi_cap | 103 | 2.8 | +4.87% | 28% | 20% | 48% | 2.03 | +$502 |
| rr_min + rvol | 112 | 3.0 | +4.17% | 27% | 20% | 46% | 1.83 | +$467 |
| rr_min + di_bull + rvol + rsi_cap | 70 | 1.9 | +5.47% | 33% | 11% | 44% | 2.03 | +$383 |
| rr_min + rsi_cap | 378 | 10.3 | +2.12% | 18% | 22% | 40% | 1.38 | +$800 |
| rr_min + di_bull + rvol | 79 | 2.1 | +4.41% | 30% | 11% | 42% | 1.78 | +$348 |
| tok_rsi + rr_min + rvol + rsi_cap | 51 | 1.4 | +5.27% | 31% | 12% | 43% | 2.01 | +$269 |

---

*Report from 1,173 trades (Jul 2023 – Apr 2026). All retrospective. Past performance ≠ future results.*
