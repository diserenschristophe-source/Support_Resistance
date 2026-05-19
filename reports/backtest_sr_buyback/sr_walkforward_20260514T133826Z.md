# SR Buyback walk-forward validation — 2026-05-14 13:38 UTC

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

- Wall: 10.5 min

## Overall (full-window — for reference)

- Published: 365   Filled: 258 (70.7%)
- Win rate: 28.3%   Avg win/loss: +8.04% / -3.29%
- **Expectancy per signal: -0.056%**

## Per-window performance (30d windows)

| Window | n_pub | n_fill | fill% | WR% | avg_w/l | exp%/sig | TP/SL/TO/Open |
|--------|-------|--------|-------|-----|---------|----------|---------------|
| 2025-05-21 | 68 | 60 | 88.2% | 11.7% | +7.53/-3.77 | **−2.165%** | 6/53/1/0 |
| 2025-06-20 | 97 | 45 | 46.4% | 53.3% | +8.89/-3.27 | **+1.492%** | 23/21/1/0 |
| 2025-07-20 | 85 | 64 | 75.3% | 28.1% | +9.95/-3.59 | **+0.163%** | 18/46/0/0 |
| 2025-08-19 | 59 | 40 | 67.8% | 42.5% | +6.46/-2.88 | **+0.740%** | 16/23/1/0 |
| 2025-09-18 | 56 | 49 | 87.5% | 14.3% | +4.57/-2.56 | **−1.350%** | 7/42/0/0 |

## Stability summary

- Windows: **5**
- Positive: **3 / 5 (60.0%)**
- Mean expectancy: **-0.224%**   Median: +0.163%   Std: 1.505%
- Min/Max: -2.165% / +1.492%
- Sharpe-like (mean/std × √n): **-0.33**
- Mean per-window WR: 30.0%

### Verdict: ★  REGIME-FIT  —  not robust enough to deploy