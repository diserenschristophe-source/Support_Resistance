# SR Buyback walk-forward validation — 2026-05-14 12:07 UTC

**Window: 30d non-overlapping  **

**Frozen config (winner from sweep)**

- sr_max_distance_atr: 1.5
- sr_sl_atr_mult:      0.5
- major_only:          True
- btc_rsi_floor:       0.0
- mt_not_downtrend:    True
- relative_volume:     False
- btc_uptrend:         True
- rsi_cap:             0.0
- min_token_rsi:       60.0
- min_confluence:      0.0
- min_touches:         0
- tp_cascade:          False
- min_rr:              1.5
- max_hold_days:       21

- Wall: 0.0 min

## Overall (full-window — for reference)

- Published: 157   Filled: 104 (66.2%)
- Win rate: 31.7%   Avg win/loss: +8.05% / -3.08%
- **Expectancy per signal: +0.297%**

## Per-window performance (30d windows)

| Window | n_pub | n_fill | fill% | WR% | avg_w/l | exp%/sig | TP/SL/TO/Open |
|--------|-------|--------|-------|-----|---------|----------|---------------|
| 2025-05-21 | 30 | 27 | 90.0% | 18.5% | +8.55/-3.20 | **−0.923%** | 4/22/1/0 |
| 2025-06-20 | 41 | 16 | 39.0% | 75.0% | +7.58/-3.29 | **+1.898%** | 12/4/0/0 |
| 2025-07-20 | 35 | 23 | 65.7% | 26.1% | +9.93/-3.57 | **−0.030%** | 6/17/0/0 |
| 2025-08-19 | 25 | 14 | 56.0% | 42.9% | +8.42/-2.98 | **+1.069%** | 5/8/1/0 |
| 2025-09-18 | 26 | 24 | 92.3% | 16.7% | +5.43/-2.55 | **−1.124%** | 4/20/0/0 |

## Stability summary

- Windows: **5**
- Positive: **2 / 5 (40.0%)**
- Mean expectancy: **+0.178%**   Median: -0.030%   Std: 1.295%
- Min/Max: -1.124% / +1.898%
- Sharpe-like (mean/std × √n): **0.31**
- Mean per-window WR: 35.8%

### Verdict: ★  REGIME-FIT  —  not robust enough to deploy