# SR Buyback walk-forward validation — 2026-05-14 13:48 UTC

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

- Wall: 0.1 min

## Overall (full-window — for reference)

- Published: 365   Filled: 258 (70.7%)
- Win rate: 28.3%   Avg win/loss: +8.04% / -3.29%
- **Expectancy per signal: -0.056%**

## Per-window performance (60d windows)

| Window | n_pub | n_fill | fill% | WR% | avg_w/l | exp%/sig | TP/SL/TO/Open |
|--------|-------|--------|-------|-----|---------|----------|---------------|
| 2025-05-21 | 165 | 105 | 63.6% | 29.5% | +8.58/-3.63 | **−0.015%** | 29/74/2/0 |
| 2025-06-20 | 182 | 109 | 59.9% | 38.5% | +9.35/-3.49 | **+0.871%** | 41/67/1/0 |
| 2025-07-20 | 144 | 104 | 72.2% | 33.6% | +8.26/-3.35 | **+0.399%** | 34/69/1/0 |
| 2025-08-19 | 115 | 89 | 77.4% | 27.0% | +5.91/-2.67 | **−0.278%** | 23/65/1/0 |
| 2025-09-18 | 56 | 49 | 87.5% | 14.3% | +4.57/-2.56 | **−1.350%** | 7/42/0/0 |

## Stability summary

- Windows: **5**
- Positive: **2 / 5 (40.0%)**
- Mean expectancy: **-0.075%**   Median: -0.015%   Std: 0.835%
- Min/Max: -1.350% / +0.871%
- Sharpe-like (mean/std × √n): **-0.2**
- Mean per-window WR: 28.6%

### Verdict: ★  REGIME-FIT  —  not robust enough to deploy