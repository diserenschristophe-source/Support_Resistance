# SR Buyback walk-forward validation — 2026-05-14 12:07 UTC

**Window: 60d rolling  (30d overlap)**

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

## Per-window performance (60d windows)

| Window | n_pub | n_fill | fill% | WR% | avg_w/l | exp%/sig | TP/SL/TO/Open |
|--------|-------|--------|-------|-----|---------|----------|---------------|
| 2025-05-21 | 71 | 43 | 60.6% | 39.5% | +7.87/-3.21 | **+0.706%** | 16/26/1/0 |
| 2025-06-20 | 76 | 39 | 51.3% | 46.1% | +8.37/-3.51 | **+1.010%** | 18/21/0/0 |
| 2025-07-20 | 60 | 37 | 61.7% | 32.4% | +9.18/-3.38 | **+0.428%** | 11/25/1/0 |
| 2025-08-19 | 51 | 38 | 74.5% | 26.3% | +7.23/-2.67 | **−0.049%** | 9/28/1/0 |
| 2025-09-18 | 26 | 24 | 92.3% | 16.7% | +5.43/-2.55 | **−1.124%** | 4/20/0/0 |

## Stability summary

- Windows: **5**
- Positive: **3 / 5 (60.0%)**
- Mean expectancy: **+0.194%**   Median: +0.428%   Std: 0.834%
- Min/Max: -1.124% / +1.010%
- Sharpe-like (mean/std × √n): **0.52**
- Mean per-window WR: 32.2%

### Verdict: ★★  MARGINAL  —  edge present but variable