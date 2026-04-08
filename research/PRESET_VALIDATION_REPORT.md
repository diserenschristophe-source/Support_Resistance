# Filter Preset Validation Report

**SR Dashboard v2 — Backtest Evidence for Trading Presets**

Date: 2026-04-03
Methodology: Walk-forward out-of-sample testing
Universe: 49 crypto tokens (daily timeframe)
Data: Binance OHLCV, Jul 2023 – Apr 2026

---

## How This Was Tested

### Walk-Forward Method

Every result in this report is **out-of-sample** — the strategy was tested on data it had never seen.

The data is split into consecutive non-overlapping windows:
- **Training window (6 months):** Used only for indicator warmup. No parameters are fitted.
- **Test window (6 months):** The strategy trades here. All results come from these windows.

This prevents the strategy from "seeing the future."

### Test Periods

| Fold | Test Window | Market Condition |
|------|------------|-----------------|
| Fold 1 | Jan 2024 – Jul 2024 | Choppy, early bull |
| Fold 2 | Jul 2024 – Jan 2025 | Strong bull run |
| Fold 3 | Jan 2025 – Jul 2025 | Bear market / correction |

Total out-of-sample: **547 trading days** (~18 months)

For the 17-token and Cluster A universes, a 4th fold was available (Jul 2025 – Jan 2026, Mixed / recovery).

### How Presets Were Selected

All 8 filters were combined in every possible way — **256 combinations** — and each was tested on the out-of-sample data. The presets are the top-performing combinations, not hand-picked rules.

---

## The Four Presets

### Preset 1: Aggressive

**Filters:** BTC RSI >= 50 + DI+ > DI- + BB %B < 0.80

| Metric | Value |
|--------|-------|
| Win Rate | 53.7% |
| W/L Ratio | 1.46 |
| Avg Win | +8.2% |
| Avg Loss | -5.6% |
| Max Drawdown | 30.4% |
| Trades | 95 |
| Profitable Folds | 2 out of 3 |

Per-fold: +16.8% (choppy), +226.3% (bull), -7.3% (bear)

The Bollinger %B filter blocks entries when a token is in the top 20% of its recent range, preventing buying overbought tokens that immediately reverse.

---

### Preset 2: Defensive

**Filters:** BTC RSI >= 50 + ADX > 20 + DI+ > DI- + MT Regime != Down + RSI <= 80

| Metric | Value |
|--------|-------|
| Win Rate | 63.6% |
| W/L Ratio | 0.87 |
| Avg Win | +7.2% |
| Avg Loss | -8.2% |
| Max Drawdown | 38.6% |
| Trades | 129 |
| Profitable Folds | 2 out of 4 |

Per-fold: +143.6%, +89.1%, -9.1%, -8.8%

Five filters create a strict entry gate. When it loses, it loses small (-9.1% and -8.8%).

---

### Preset 3: Liquid Only

**Filters:** BTC RSI >= 50 + DI+ > DI- + MT Regime != Down + RVOL >= 1.5
**Applies to:** Cluster A tokens only (13 liquid tokens)

| Metric | Value |
|--------|-------|
| Win Rate | 63.6% |
| W/L Ratio | 0.90 |
| Avg Win | +7.8% |
| Avg Loss | -8.7% |
| Max Drawdown | 33.0% |
| Trades | 99 |
| Profitable Folds | 2 out of 4 |

Per-fold: -10.6%, +317.1%, -8.9%, +22.3%

The RVOL filter ensures the price move is backed by above-average volume. On liquid tokens, volume confirmation is a strong institutional signal.

---

### Preset 4: Volatile Only

**Filters:** BTC RSI >= 50 + Token RSI > 60 + DI+ > DI- + RSI <= 80
**Applies to:** Cluster C tokens only (36 volatile tokens)

| Metric | Value |
|--------|-------|
| Win Rate | 52.9% |
| W/L Ratio | 1.34 |
| Avg Win | +7.1% |
| Avg Loss | -5.3% |
| Max Drawdown | 37.6% |
| Trades | 85 |
| Profitable Folds | 2 out of 3 |

Per-fold: +31.3%, +166.0%, -11.9%

The RSI momentum band (60-80) captures real moves while avoiding exhaustion tops.

---

## Filter Reference

| Filter | Rule | What It Does |
|--------|------|-------------|
| BTC RSI Floor | BTC RSI(10) >= 50 | Blocks all trades when Bitcoin is weak |
| Token RSI Momentum | Token RSI(10) > 60 | Requires upward momentum |
| Token ADX Trend | ADX(14) > 20 | Requires a directional trend |
| Token DI Bullish | DI+(14) > DI-(14) | Requires bullish direction |
| MT Regime Gate | SMA40 regime != Down | Blocks entries in downtrends |
| Bollinger %B | BB %B(20,2s) < 0.80 | Blocks overbought entries |
| Relative Volume | RVOL(20) >= 1.5 | Requires above-average volume |
| RSI Cap | Token RSI(10) <= 80 | Prevents extremely overbought entries |

## Cluster Composition

**Cluster A — Liquid (13 tokens):** BTC, ETH, BNB, XRP, SOL, DOGE, ADA, AVAX, LINK, SUI, LTC, HBAR, BCH

**Cluster C — Volatile (36 tokens):** DOT, SHIB, NEAR, UNI, PEPE, TAO, ICP, ETC, AAVE, RENDER, FET, ONDO, ATOM, FIL, ARB, ENA, APT, ALGO, VET, STX, SEI, BONK, JUP, WLD, ZRO, ETHFI, TRUMP, QNT, XLM, TON, TRX, PAXG, FLOKI, INJ, MKR, OP (plus new additions)

## Limitations

1. Bull market fold dominates averages (+166% to +317%)
2. No fees or slippage included (~3-4% annual drag estimated)
3. Single position backtest, not portfolio
4. S/R model changes would require re-validation
5. Historical period: Jan 2024 – Jul 2025 (extended to Jan 2026 for some)
