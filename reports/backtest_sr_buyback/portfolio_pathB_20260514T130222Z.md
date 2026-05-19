# SR Buyback Portfolio Analysis — 2026-05-14 13:02 UTC

- Universe: `selected17`
- Strategy: SR Buyback Phase 4 winner (`max_d=1.5, sl=0.5, major=Y, mtND=Y, rsiMin=60, hold=21d`)
- Filter-level per-token cap: **ON (=1)** — walk-forward conditions / Path B
  Portfolio's `per_token_cap` is therefore redundant beyond 1 (only 1 signal per token published).
- Wall: 0.1 min

## A1. BTC filter A/B comparison

Same portfolio config (single-slot AND 4-slot multi), btc_uptrend ON vs OFF.
Best PnL+DD combo for each is highlighted by Sharpe and Calmar.

| Filter | Mode | n_eligible | n_taken | n_real | Return% | MaxDD% | CAGR% | Sharpe | Calmar | WR% |
|--------|------|------------|---------|--------|---------|--------|-------|--------|--------|-----|
| OFF | single-slot | 399 | 3 | 2 | -9.2% | -9.2% | -7.1% | -0.87 | -0.77 | 0.0% |
| OFF | multi-slot(4×3) | 399 | 9 | 5 | +0.9% | -2.3% | +0.7% | -0.87 | 0.28 | 20.0% |
| ON | single-slot | 157 | 8 | 7 | +4.0% | -5.9% | +10.6% | 1.35 | 1.79 | 28.6% |
| ON | multi-slot(4×3) | 157 | 15 | 11 | +5.6% | -1.2% | +15.0% | 2.41 | 12.92 | 45.5% |

## A2. Slot count × per-token cap sweep (filter=ON)

| Slots | TokCap | per-slot | n_taken | n_real | Return% | MaxDD% | CAGR% | Sharpe | Calmar | WR% |
|-------|--------|----------|---------|--------|---------|--------|-------|--------|--------|-----|
| 1 | 1 | 100% | 8 | 7 | +4.0% | -5.9% | +10.6% | 1.35 | 1.79 | 28.6% |
| 1 | 2 | 100% | 8 | 7 | +4.0% | -5.9% | +10.6% | 1.35 | 1.79 | 28.6% |
| 1 | 3 | 100% | 8 | 7 | +4.0% | -5.9% | +10.6% | 1.35 | 1.79 | 28.6% |
| 2 | 1 | 50% | 9 | 7 | +0.9% | -3.4% | +2.3% | 1.31 | 0.69 | 28.6% |
| 2 | 2 | 50% | 9 | 7 | +2.3% | -3.0% | +6.0% | 1.37 | 2.03 | 28.6% |
| 2 | 3 | 50% | 9 | 7 | +2.3% | -3.0% | +6.0% | 1.37 | 2.03 | 28.6% |
| 3 | 1 | 33% | 13 | 10 | +7.1% | -1.8% | +19.4% | 2.51 | 10.61 | 50.0% |
| 3 | 2 | 33% | 13 | 10 | +8.6% | -1.5% | +23.5% | 2.41 | 15.93 | 50.0% |
| 3 | 3 | 33% | 13 | 10 | +8.6% | -1.5% | +23.5% | 2.41 | 15.93 | 50.0% |
| 4 | 1 | 25% | 15 | 11 | +4.5% | -1.4% | +12.0% | 2.51 | 8.78 | 45.5% |
| 4 | 2 | 25% | 15 | 11 | +5.6% | -1.2% | +15.0% | 2.41 | 12.92 | 45.5% |
| 4 | 3 | 25% | 15 | 11 | +5.6% | -1.2% | +15.0% | 2.41 | 12.92 | 45.5% |
| 5 | 1 | 20% | 19 | 14 | +5.6% | -1.9% | +15.0% | 2.02 | 8.04 | 42.9% |
| 5 | 2 | 20% | 21 | 16 | +8.0% | -1.3% | +21.7% | 2.49 | 16.15 | 50.0% |
| 5 | 3 | 20% | 21 | 16 | +8.0% | -1.3% | +21.7% | 2.49 | 16.15 | 50.0% |

**Best Sharpe**: slots=3 tok=1 → Sharpe 2.51, Return +7.1%, DD -1.8%
**Best Calmar**: slots=5 tok=2 → Calmar 16.15, Return +8.0%, DD -1.3%
**Best Return**: slots=3 tok=2 → Return +8.6%, DD -1.5%, Sharpe 2.41

## A3. Start-date sensitivity (filter=ON)

Investor return depending on when they started. Slots ∈ {1, 4}.

| Start | Slots | TokCap | n_taken | Return% | MaxDD% | Sharpe |
|-------|-------|--------|---------|---------|--------|--------|
| 2025-05-01 | 1 | 1 | 8 | +4.0% | -5.9% | 1.35 |
| 2025-05-01 | 4 | 3 | 15 | +5.6% | -1.2% | 2.41 |
| 2025-06-01 | 1 | 1 | 6 | +10.5% | -4.5% | -1.73 |
| 2025-06-01 | 4 | 3 | 12 | +3.7% | -2.3% | 1.73 |
| 2025-07-01 | 1 | 1 | 2 | +4.5% | 0.0% | 0.00 |
| 2025-07-01 | 4 | 3 | 7 | -0.8% | -1.1% | 0.00 |
| 2025-08-01 | 1 | 1 | 2 | -2.9% | -2.9% | 0.00 |
| 2025-08-01 | 4 | 3 | 5 | -0.7% | -0.7% | 0.00 |
| 2025-09-01 | 1 | 1 | 1 | +0.0% | 0.0% | 0.00 |
| 2025-09-01 | 4 | 3 | 4 | +0.0% | 0.0% | 0.00 |

**Spread**: 5/10 start dates positive  (min return -2.9%, max +10.5%, median +3.7%)
**DD spread**: worst -5.9%, median -1.1%
**Sharpe spread**: min -1.73, max 2.41, median 0.00

## Verdict

Pick the (filter, slots, token_cap) combo with the best DD-adjusted return
(Sharpe + Calmar). If A1 shows ON > OFF on Sharpe, the BTC filter is
doing real work at the portfolio level. If A2's best is at higher slot
counts, capital efficiency rewards diversification.

_CSV exports: `portfolio_filter_ab_<UTC>.csv`, `portfolio_slot_sweep_<UTC>.csv`, `portfolio_start_sens_<UTC>.csv`_
_Equity curves for best configs in `portfolio_equity_<UTC>.csv`._