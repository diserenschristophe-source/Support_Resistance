# Support_Resistance

Research library for automated support/resistance detection on crypto assets using a 5-method ensemble detector with multi-timeframe analysis and walk-forward validated entry filters.

## Quick Start

```bash
pip install -r requirements.txt

# 1. Generate the raw signal log (one row per qualifying setup, every filter as a column)
python3 backtest_model.py --all --days 1000

# 2. Pick a filter combo and produce the report (Markdown + tearsheet PDF)
python3 report.py --filters btc_rsi di_bull bb_pctb --tier all --target r1 --timeout 7

# 3. (Optional) Brute-force the best combo for a tier
python3 report.py --search --tier top_20 --max-filters 4
```

## What It Does

Detects tradeable support and resistance zones by running 5 independent detection methods across 3 timeframes (20d/60d/180d), merging results with strength-weighted pricing, and validating entry signals through walk-forward backtested filters. Produces ranked trade setups with TP/SL levels and risk-reward scoring, plus a backtest workflow for filter selection.

## Architecture

```
Support_Resistance/
  core/                         -- production engine (S/R detection + filters + TP/SL)
    config.py                   -- central configuration (all tuneable params)
    models.py                   -- shared dataclasses (SRLevel, SRZone, compute_atr)
    sr_analysis.py              -- main orchestrator (multi-window ensemble + zone conversion)
    fetcher.py                  -- multi-source OHLCV fetcher (Binance, GeckoTerminal, CoinGecko)
    filters.py                  -- 8 entry filters + FilterChain
    tpsl.py                     -- take-profit / stop-loss calculation + R:R scoring
    detectors/                  -- 5 independent S/R detection methods
      ensemble.py               -- orchestrator (runs all 5, merges with strength weighting)
      market_structure.py       -- body/wick swing detection + break invalidation (40% weight)
      touch_count.py            -- weighted touch frequency counting (25% weight)
      nison_body.py             -- large candle body edges / institutional footprints (15% weight)
      volume_profile.py         -- VPVR-style high-volume node detection (10% weight)
      polarity_flip.py          -- support <-> resistance role reversal detection (10% weight)

  backtest_model.py             -- signal recorder: writes backtest_results.csv
                                   (one row per qualifying setup, every filter state as a boolean column)
  report.py                     -- unified report generator: reads backtest_results.csv,
                                   simulates a chosen filter combo, computes metrics
                                   (sharpe, sortino, calmar, max DD, profit factor, hit rate),
                                   outputs Markdown + tearsheet PDF
  compare.py                    -- validate detector S/R levels against external sources
                                   (Grok / Perplexity / manual reference_levels.txt)
  diagnose_detectors.py         -- per-detector visualisation on price chart (debugging tool)
  roro_features.py              -- 7 risk-on/risk-off market features

  data/                         -- cached OHLCV CSVs (gitignored)
  data49/                       -- 49-token validated universe snapshot (gitignored)

  FILTER_CATALOG.md             -- canonical filter reference + walk-forward results
  PROJECT.md                    -- this file
  reference_levels.txt          -- manual S/R reference (used by compare.py)
  backtest_results.csv          -- latest signal log from backtest_model.py
  sr_backtest_report.md         -- latest report (Markdown)
  sr_backtest_report.pdf        -- latest report (tearsheet PDF)
```

## Data Flow

```
                fetcher.py
                    │
                    ▼
             daily OHLCV (cached in data/)
                    │
                    ▼
           sr_analysis.py
       (3 windows × 5 detectors → ensemble → zones)
                    │
                    ▼
              tpsl.py
       (nearest R / nearest S → cascade → R:R)
                    │
                    ▼
        backtest_model.py
   (records EVERY qualifying signal × ALL filter states)
                    │
                    ▼
        backtest_results.csv     ←── ground-truth signal log
                    │
                    ▼
              report.py
  (apply filter combo → simulate → metrics → tearsheet)
                    │
                    ▼
   sr_backtest_report.md  +  sr_backtest_report.pdf
```

## 5-Method Ensemble

| Method | Weight | What it detects |
|---|---|---|
| Market Structure | 40% | Body/wick swings, break invalidation, CHOCH/BOS structural roles |
| Touch Count | 25% | Weighted price touches at levels (body > open > wick weighting) |
| Nison Body | 15% | Large candle body edges (institutional footprints, >= 1 ATR) |
| Volume Profile | 10% | VPVR high-volume nodes, Point of Control, Value Area |
| Polarity Flip | 10% | Levels that flipped from support to resistance or vice versa |

Levels from all methods are merged within 0.5% proximity using strength-weighted pricing. Multi-method bonus: +10% per additional confirming method (cap 30%).

## Entry Filters

| Alias (CLI) | Rule | Purpose |
|---|---|---|
| `btc_rsi` | BTC RSI(10) >= 50 | Market-wide risk gate |
| `tok_rsi` | Token RSI(10) > 60 | Momentum confirmation |
| `adx` | ADX(14) > 20 | Trend must exist |
| `di_bull` | DI+ > DI- | Bullish directional pressure |
| `mt_regime` | SMA(40) regime != DOWN | Block downtrends |
| `bb_pctb` | BB %B < 0.80 | Block overbought |
| `rvol` | RVOL(20) >= 1.5 | Volume confirmation |
| `rsi_cap` | RSI(10) <= 80 | Overbought protection |
| `rr_min` | R:R >= 1.2 | Minimum risk/reward |

All filters validated via 6-month in-sample / 6-month out-of-sample walk-forward folds. See [FILTER_CATALOG.md](FILTER_CATALOG.md) for full results, recommended combos per tier, and filter interaction analysis.

## Configuration

All tuneable parameters are in `core/config.py`:

- **Multi-timeframe windows:** 20d (weight 1.5), 60d (weight 1.0), 180d (weight 0.7)
- **Detector configs:** per-window settings for each detection method
- **Zone thresholds:** merge distance, ATR multipliers, tier classification rules
- **TP/SL rules:** ATR cascade for nearby levels
- **Token universe:** four tiers — `top_3`, `selected`, `top_20`, `all` (defined in `core/config.py`)

## Key Design Patterns

- **ATR-normalised thresholds** — all distances and tolerances expressed as ATR multiples.
- **Exponential recency decay** — `exp(-ln(2) * bars_ago / halflife)`, configurable per window.
- **Body-first anchor snapping** — zones snap to large candle bodies (institutional levels) over wicks.
- **Strength-weighted pricing** — merged level price pulled toward stronger constituents.
- **Decoupled signal log → report** — `backtest_model.py` records *all* signals once with every filter state as a boolean column; `report.py` slices that log by any filter combination without re-running the engine. Borrowed from `python_backtester`'s signal/engine separation.
- **Single-position compounding** — `report.py` simulates one trade at a time at 100% capital, compounding equity on each exit.

## Outputs

- **backtest_model.py** → `backtest_results.csv`: one row per qualifying setup, with filter states, TP/SL/timeout outcomes, and look-ahead-free exit prices at days 5/7/10/14/21.
- **report.py** → `sr_backtest_report.md` (table summary) + `sr_backtest_report.pdf` (tearsheet: equity curve, drawdown, monthly heatmap, metrics tables, per-symbol breakdown).
- **diagnose_detectors.py** → matplotlib chart with each detector's levels overlaid on price.
- **compare.py** → console table scoring detectors vs Grok/Perplexity/manual reference.

## Dependencies

```
numpy>=1.24
pandas>=2.0
scipy>=1.11
requests>=2.31
matplotlib>=3.8
```

Optional: `mplfinance` (for `diagnose_detectors.py`).
