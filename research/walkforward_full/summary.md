# Walk-Forward Full Optimization — Summary

- Universes: ['u17', 'u51']
- Windows: ['w14', 'w30', 'w60'] ({'w14': (14, 14), 'w30': (30, 30), 'w60': (60, 60)})
- Filters: ['btc_rsi_floor', 'token_rsi_momentum', 'rsi_cap', 'token_di_bullish', 'relative_volume']
- Threshold grids: {'btc_rsi_floor': [45.0, 50.0, 55.0], 'token_rsi_momentum': [55.0, 60.0, 65.0], 'rsi_cap': [75.0, 80.0, 85.0], 'relative_volume': [1.25, 1.5, 1.75]}
- Timeouts: [3, 5, 7, 10, 14, 21]  ·  Tiebreakers: ['rsi', 'rr']  ·  top_n: [1, 3]
- Definitions: `pnl_pct = portfolio compounding`, `win_rate = TP/(TP+SL)`, `hit_rate = TP/total`

## u17 / w14 (14d / 14d)

- Folds: 56  ·  Records: 688,128

Top 20 picks by IS-Pareto frequency:

| # Folds | Combo | Thresholds | TO | TB | top_n | Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |
|---:|---|---|---:|---|---:|---:|---:|---:|---:|
| 7 | `rvol` | rvol=1.75 | 14 | rsi | 1 | +3.93 | 50.0 | 38.1 | 1.6 |
| 7 | `rvol` | rvol=1.75 | 21 | rsi | 1 | +3.93 | 50.0 | 38.1 | 1.6 |
| 7 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 14 | rsi | 3 | +1.41 | 63.1 | 44.3 | 4.4 |
| 7 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 21 | rsi | 3 | +1.41 | 63.1 | 44.3 | 4.4 |
| 6 | `rvol` | rvol=1.75 | 10 | rsi | 1 | +10.28 | 58.3 | 44.4 | 1.7 |
| 6 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 3 | rr | 3 | +3.67 | 33.3 | 22.2 | 4.8 |
| 6 | `rvol` | rvol=1.75 | 3 | rr | 3 | +2.97 | 33.3 | 22.2 | 5.0 |
| 6 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 14 | rsi | 1 | +2.93 | 50.0 | 33.3 | 1.3 |
| 6 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 21 | rsi | 1 | +2.93 | 50.0 | 33.3 | 1.3 |
| 6 | `rvol` | rvol=1.75 | 14 | rsi | 3 | -0.70 | 62.5 | 36.1 | 4.5 |
| 6 | `rvol` | rvol=1.75 | 21 | rsi | 3 | -0.70 | 62.5 | 36.1 | 4.5 |
| 6 | `rvol+rsi_cap` | rvol=1.75;cap=75.0 | 14 | rsi | 1 | -4.03 | 8.3 | 8.3 | 1.2 |
| 6 | `rvol+rsi_cap` | rvol=1.75;cap=75.0 | 21 | rsi | 1 | -4.03 | 8.3 | 8.3 | 1.2 |
| 5 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=85.0;rsi=65.0 | 3 | rr | 3 | +12.42 | 40.0 | 19.4 | 3.6 |
| 5 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 10 | rsi | 1 | +10.36 | 60.0 | 40.0 | 1.4 |
| 5 | `rvol+rsi_mom` | rvol=1.75;rsi=55.0 | 10 | rsi | 1 | +9.00 | 70.0 | 40.0 | 1.8 |
| 5 | `rvol+rsi_mom` | rvol=1.75;rsi=55.0 | 14 | rsi | 1 | +8.95 | 70.0 | 40.0 | 1.8 |
| 5 | `rvol+rsi_mom` | rvol=1.75;rsi=55.0 | 21 | rsi | 1 | +8.95 | 70.0 | 40.0 | 1.8 |
| 5 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=75.0;rsi=55.0 | 10 | rsi | 1 | +6.77 | 30.0 | 20.0 | 1.0 |
| 5 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=75.0;rsi=55.0 | 14 | rsi | 1 | +6.77 | 30.0 | 20.0 | 1.0 |

## u17 / w30 (30d / 30d)

- Folds: 25  ·  Records: 307,200

Top 20 picks by IS-Pareto frequency:

| # Folds | Combo | Thresholds | TO | TB | top_n | Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |
|---:|---|---|---:|---|---:|---:|---:|---:|---:|
| 3 | `rsi_cap` | cap=85.0 | 14 | rsi | 1 | +6.59 | 55.0 | 44.4 | 6.0 |
| 3 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 21 | rr | 1 | +5.29 | 38.9 | 27.8 | 3.0 |
| 3 | `rvol+rsi_cap` | rvol=1.75;cap=85.0 | 14 | rr | 1 | +4.09 | 38.9 | 27.8 | 3.3 |
| 3 | `rsi_cap` | cap=85.0 | 21 | rsi | 1 | -0.56 | 53.8 | 43.1 | 5.7 |
| 3 | `rvol+rsi_cap` | rvol=1.75;cap=80.0 | 3 | rsi | 1 | -2.62 | 50.0 | 10.3 | 5.3 |
| 3 | `rvol+di+` | rvol=1.75 | 21 | rsi | 1 | -11.97 | 30.6 | 30.6 | 3.0 |
| 2 | `btc_rsi+rsi_cap+rsi_mom` | btc=55.0;cap=75.0;rsi=65.0 | 14 | rsi | 1 | +29.88 | 50.0 | 50.0 | 5.0 |
| 2 | `btc_rsi+rsi_cap+rsi_mom` | btc=55.0;cap=75.0;rsi=65.0 | 21 | rsi | 1 | +29.88 | 50.0 | 50.0 | 5.0 |
| 2 | `btc_rsi+di+` | btc=45.0 | 7 | rsi | 1 | +29.27 | 55.0 | 29.8 | 6.5 |
| 2 | `btc_rsi+di++rsi_mom` | btc=45.0;rsi=55.0 | 14 | rr | 1 | +23.33 | 52.5 | 52.5 | 4.5 |
| 2 | `btc_rsi+di++rsi_mom` | btc=45.0;rsi=55.0 | 21 | rr | 1 | +23.33 | 52.5 | 52.5 | 4.5 |
| 2 | `rvol+rsi_mom` | rvol=1.5;rsi=65.0 | 14 | rr | 1 | +23.28 | 50.0 | 37.5 | 2.5 |
| 2 | `rvol+rsi_mom` | rvol=1.5;rsi=65.0 | 21 | rr | 1 | +23.28 | 50.0 | 37.5 | 2.5 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=45.0;rvol=1.5;rsi=65.0 | 14 | rr | 1 | +23.28 | 50.0 | 37.5 | 2.5 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=45.0;rvol=1.5;rsi=65.0 | 21 | rr | 1 | +23.28 | 50.0 | 37.5 | 2.5 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=50.0;rvol=1.5;rsi=65.0 | 14 | rr | 1 | +23.28 | 50.0 | 37.5 | 2.5 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=50.0;rvol=1.5;rsi=65.0 | 21 | rr | 1 | +23.28 | 50.0 | 37.5 | 2.5 |
| 2 | `rsi_cap+rsi_mom` | cap=85.0;rsi=65.0 | 3 | rsi | 1 | +22.35 | 77.5 | 35.9 | 10.0 |
| 2 | `btc_rsi+rsi_cap+di+` | btc=45.0;cap=85.0 | 7 | rsi | 1 | +19.83 | 58.3 | 25.0 | 6.0 |
| 2 | `btc_rsi+rsi_cap+rsi_mom` | btc=45.0;cap=85.0;rsi=65.0 | 3 | rsi | 1 | +19.24 | 77.5 | 38.6 | 9.5 |

## u17 / w60 (60d / 60d)

- Folds: 12  ·  Records: 147,456

Top 20 picks by IS-Pareto frequency:

| # Folds | Combo | Thresholds | TO | TB | top_n | Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |
|---:|---|---|---:|---|---:|---:|---:|---:|---:|
| 2 | `btc_rsi+di+` | btc=45.0 | 7 | rsi | 1 | +13.78 | 52.5 | 28.3 | 8.0 |
| 2 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=85.0;rsi=55.0 | 21 | rsi | 1 | -11.13 | 50.0 | 41.7 | 5.0 |
| 2 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=85.0;rsi=55.0 | 21 | rr | 1 | -16.49 | 41.7 | 33.3 | 4.5 |
| 2 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=85.0;rsi=55.0 | 7 | rr | 1 | -19.47 | 47.5 | 32.1 | 6.5 |
| 2 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=85.0;rsi=55.0 | 7 | rsi | 1 | -21.00 | 50.0 | 35.7 | 7.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=45.0;rvol=1.75;rsi=65.0 | 5 | rr | 1 | +139.73 | 87.5 | 58.3 | 12.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=60.0 | 5 | rr | 3 | +106.57 | 76.2 | 53.3 | 30.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=55.0 | 5 | rsi | 3 | +98.81 | 75.0 | 46.9 | 32.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=60.0 | 7 | rr | 3 | +98.46 | 78.9 | 55.6 | 27.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=55.0 | 5 | rr | 3 | +96.14 | 75.0 | 51.7 | 29.0 |
| 1 | `btc_rsi+rvol+rsi_mom` | btc=45.0;rvol=1.75;rsi=65.0 | 5 | rr | 1 | +95.94 | 87.5 | 58.3 | 12.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=55.0 | 7 | rr | 3 | +94.68 | 77.8 | 53.8 | 26.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=45.0;rvol=1.75;rsi=65.0 | 21 | rr | 1 | +93.96 | 100.0 | 85.7 | 7.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=45.0;rvol=1.75;rsi=65.0 | 7 | rr | 1 | +87.15 | 85.7 | 60.0 | 10.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=45.0;rvol=1.75;rsi=65.0 | 10 | rr | 1 | +82.33 | 100.0 | 55.6 | 9.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=60.0 | 5 | rsi | 3 | +78.89 | 73.3 | 39.3 | 28.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=55.0 | 7 | rsi | 3 | +77.91 | 76.2 | 53.3 | 30.0 |
| 1 | `btc_rsi+rvol+rsi_cap+di++rsi_mom` | btc=50.0;rvol=1.25;cap=75.0;rsi=60.0 | 21 | rr | 1 | +76.64 | 66.7 | 60.0 | 10.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=60.0 | 7 | rsi | 3 | +76.48 | 81.2 | 52.0 | 25.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.75;rsi=55.0 | 10 | rr | 3 | +74.18 | 80.0 | 52.2 | 23.0 |

## u51 / w14 (14d / 14d)

- Folds: 56  ·  Records: 688,128

Top 20 picks by IS-Pareto frequency:

| # Folds | Combo | Thresholds | TO | TB | top_n | Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |
|---:|---|---|---:|---|---:|---:|---:|---:|---:|
| 5 | `btc_rsi+rsi_cap+rsi_mom` | btc=45.0;cap=85.0;rsi=55.0 | 14 | rsi | 1 | -1.26 | 56.7 | 46.7 | 2.0 |
| 5 | `btc_rsi+rsi_cap+rsi_mom` | btc=45.0;cap=85.0;rsi=55.0 | 21 | rsi | 1 | -1.26 | 56.7 | 46.7 | 2.0 |
| 5 | `btc_rsi+rsi_mom` | btc=45.0;rsi=55.0 | 14 | rsi | 1 | -1.58 | 33.3 | 33.3 | 2.2 |
| 5 | `btc_rsi+rsi_mom` | btc=45.0;rsi=55.0 | 21 | rsi | 1 | -1.58 | 33.3 | 33.3 | 2.2 |
| 5 | `btc_rsi+rsi_cap` | btc=45.0;cap=85.0 | 14 | rsi | 1 | -2.12 | 50.0 | 40.0 | 1.8 |
| 5 | `btc_rsi+rsi_cap` | btc=45.0;cap=85.0 | 21 | rsi | 1 | -2.12 | 50.0 | 40.0 | 1.8 |
| 5 | `btc_rsi` | btc=45.0 | 14 | rsi | 1 | -2.44 | 26.7 | 26.7 | 2.0 |
| 5 | `btc_rsi` | btc=45.0 | 21 | rsi | 1 | -2.44 | 26.7 | 26.7 | 2.0 |
| 5 | `rvol` | rvol=1.25 | 5 | rsi | 1 | -15.00 | 36.7 | 17.4 | 4.2 |
| 4 | `rvol+rsi_cap+rsi_mom` | rvol=1.75;cap=80.0;rsi=55.0 | 5 | rsi | 1 | +12.29 | 54.2 | 42.9 | 3.2 |
| 4 | `btc_rsi+rvol+rsi_cap+rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 10 | rsi | 1 | +10.89 | 25.0 | 18.8 | 2.0 |
| 4 | `btc_rsi+rvol+rsi_cap+di++rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 10 | rsi | 1 | +9.48 | 25.0 | 18.8 | 2.0 |
| 4 | `btc_rsi+rvol+rsi_cap+di++rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 7 | rsi | 1 | +8.80 | 25.0 | 18.8 | 2.0 |
| 4 | `btc_rsi+rvol+rsi_cap+di++rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 14 | rsi | 1 | +8.47 | 25.0 | 18.8 | 1.8 |
| 4 | `btc_rsi+rvol+rsi_cap+di++rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 21 | rsi | 1 | +8.47 | 25.0 | 18.8 | 1.8 |
| 4 | `rvol+rsi_cap+di++rsi_mom` | rvol=1.25;cap=80.0;rsi=60.0 | 7 | rsi | 1 | +7.94 | 25.0 | 18.8 | 2.8 |
| 4 | `btc_rsi+rvol+rsi_cap+di+` | btc=55.0;rvol=1.5;cap=80.0 | 10 | rr | 1 | +7.85 | 25.0 | 25.0 | 1.8 |
| 4 | `btc_rsi+rvol+rsi_cap+rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 14 | rsi | 1 | +7.78 | 25.0 | 18.8 | 1.5 |
| 4 | `btc_rsi+rvol+rsi_cap+rsi_mom` | btc=45.0;rvol=1.25;cap=80.0;rsi=60.0 | 21 | rsi | 1 | +7.78 | 25.0 | 18.8 | 1.5 |
| 4 | `btc_rsi+rvol+rsi_cap+di+` | btc=55.0;rvol=1.5;cap=80.0 | 14 | rr | 1 | +7.77 | 25.0 | 25.0 | 1.5 |

## u51 / w30 (30d / 30d)

- Folds: 25  ·  Records: 307,200

Top 20 picks by IS-Pareto frequency:

| # Folds | Combo | Thresholds | TO | TB | top_n | Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |
|---:|---|---|---:|---|---:|---:|---:|---:|---:|
| 5 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 21 | rsi | 1 | +6.06 | 20.0 | 20.0 | 2.0 |
| 4 | `btc_rsi+rvol+di++rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 21 | rsi | 1 | +13.74 | 25.0 | 25.0 | 1.8 |
| 4 | `btc_rsi+rsi_cap+di++rsi_mom` | btc=45.0;cap=80.0;rsi=65.0 | 21 | rsi | 1 | +12.31 | 66.7 | 37.5 | 2.5 |
| 4 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=60.0 | 21 | rsi | 1 | +9.60 | 25.0 | 25.0 | 2.0 |
| 4 | `btc_rsi+rsi_cap+rsi_mom` | btc=45.0;cap=80.0;rsi=65.0 | 21 | rsi | 1 | +8.06 | 50.0 | 25.0 | 2.0 |
| 4 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 14 | rsi | 1 | -0.72 | 25.0 | 12.5 | 2.2 |
| 3 | `btc_rsi+rvol+di++rsi_mom` | btc=55.0;rvol=1.75;rsi=60.0 | 21 | rsi | 1 | +18.32 | 33.3 | 33.3 | 2.3 |
| 3 | `btc_rsi+rvol+di+` | btc=55.0;rvol=1.75 | 21 | rsi | 1 | +17.35 | 26.7 | 26.7 | 2.3 |
| 3 | `btc_rsi+rsi_cap+di++rsi_mom` | btc=50.0;cap=80.0;rsi=65.0 | 21 | rsi | 1 | +15.10 | 55.6 | 33.3 | 2.7 |
| 3 | `btc_rsi+rsi_cap+di++rsi_mom` | btc=45.0;cap=80.0;rsi=65.0 | 14 | rsi | 1 | +10.57 | 55.6 | 33.3 | 3.0 |
| 3 | `btc_rsi+rsi_cap+di++rsi_mom` | btc=50.0;cap=80.0;rsi=65.0 | 14 | rsi | 1 | +9.36 | 55.6 | 33.3 | 2.7 |
| 3 | `btc_rsi+rvol` | btc=55.0;rvol=1.75 | 21 | rsi | 1 | +8.64 | 26.7 | 26.7 | 3.0 |
| 3 | `btc_rsi+rsi_cap+rsi_mom` | btc=45.0;cap=80.0;rsi=65.0 | 14 | rsi | 1 | +7.27 | 66.7 | 27.8 | 2.7 |
| 3 | `btc_rsi+rvol+di++rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 14 | rsi | 1 | +7.26 | 33.3 | 16.7 | 2.0 |
| 3 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=60.0 | 14 | rsi | 1 | +3.57 | 33.3 | 16.7 | 2.3 |
| 3 | `btc_rsi+rvol+di+` | btc=55.0;rvol=1.75 | 14 | rsi | 1 | +2.22 | 22.2 | 13.3 | 2.3 |
| 3 | `rvol+rsi_mom` | rvol=1.5;rsi=65.0 | 21 | rsi | 1 | -3.94 | 61.1 | 47.2 | 3.7 |
| 3 | `rvol+di++rsi_mom` | rvol=1.5;rsi=65.0 | 21 | rsi | 1 | -3.94 | 61.1 | 47.2 | 3.7 |
| 3 | `btc_rsi+rvol` | btc=55.0;rvol=1.75 | 14 | rsi | 1 | -6.49 | 22.2 | 13.3 | 3.0 |
| 3 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 10 | rsi | 1 | -11.32 | 0.0 | 0.0 | 1.7 |

## u51 / w60 (60d / 60d)

- Folds: 12  ·  Records: 147,456

Top 20 picks by IS-Pareto frequency:

| # Folds | Combo | Thresholds | TO | TB | top_n | Avg OOS PnL% | Avg OOS Win% | Avg OOS Hit% | Avg N |
|---:|---|---|---:|---|---:|---:|---:|---:|---:|
| 2 | `rvol+rsi_cap+di+` | rvol=1.5;cap=80.0 | 3 | rsi | 1 | +13.51 | 58.9 | 25.7 | 17.0 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 10 | rsi | 1 | -1.75 | 58.6 | 41.4 | 8.5 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 21 | rsi | 1 | -6.71 | 41.7 | 33.9 | 5.5 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 14 | rsi | 1 | -10.67 | 46.7 | 31.4 | 6.0 |
| 2 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.75;rsi=55.0 | 7 | rsi | 1 | -11.03 | 62.5 | 31.2 | 8.0 |
| 2 | `btc_rsi+rvol+rsi_cap+rsi_mom` | btc=55.0;rvol=1.75;cap=85.0;rsi=55.0 | 5 | rr | 1 | -12.89 | 37.5 | 16.2 | 6.5 |
| 2 | `btc_rsi+rvol+rsi_cap+rsi_mom` | btc=55.0;rvol=1.75;cap=75.0;rsi=55.0 | 5 | rsi | 1 | -15.49 | 25.0 | 7.1 | 6.0 |
| 1 | `btc_rsi+rvol` | btc=55.0;rvol=1.25 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.25;rsi=55.0 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.25;rsi=60.0 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+rsi_mom` | btc=55.0;rvol=1.25;rsi=65.0 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+di+` | btc=55.0;rvol=1.25 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=55.0;rvol=1.25;rsi=55.0 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=55.0;rvol=1.25;rsi=60.0 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=55.0;rvol=1.25;rsi=65.0 | 21 | rsi | 1 | +67.82 | 100.0 | 80.0 | 5.0 |
| 1 | `rvol+rsi_mom` | rvol=1.25;rsi=65.0 | 21 | rsi | 1 | +67.24 | 100.0 | 71.4 | 7.0 |
| 1 | `rvol+di++rsi_mom` | rvol=1.25;rsi=65.0 | 21 | rsi | 1 | +67.24 | 100.0 | 71.4 | 7.0 |
| 1 | `btc_rsi+rvol+rsi_mom` | btc=45.0;rvol=1.25;rsi=65.0 | 21 | rsi | 1 | +66.33 | 100.0 | 71.4 | 7.0 |
| 1 | `btc_rsi+rvol+di++rsi_mom` | btc=45.0;rvol=1.25;rsi=65.0 | 21 | rsi | 1 | +66.33 | 100.0 | 71.4 | 7.0 |
| 1 | `rsi_cap+di+` | cap=80.0 | 7 | rsi | 1 | +64.26 | 71.4 | 41.7 | 12.0 |

