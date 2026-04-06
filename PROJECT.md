# Support_Resistance

Research library for automated support/resistance detection on crypto assets using a 5-method ensemble detector with multi-timeframe analysis and walk-forward validated entry filters.

## Quick Start

```bash
pip install -r requirements.txt
python backtest_model.py --tokens BTC ETH SOL --days 365
```

## What It Does

Detects tradeable support and resistance zones by running 5 independent detection methods across 3 timeframes (20d/60d/180d), merging results with strength-weighted pricing, and validating entry signals through walk-forward backtested filters. Produces ranked trade setups with TP/SL levels and risk-reward scoring.

## Architecture

```
Support_Resistance/
  core/                         -- production modules (S/R engine)
    config.py                   -- central configuration (all tuneable params)
    models.py                   -- shared dataclasses (SRLevel, SRZone, compute_atr)
    sr_analysis.py              -- main orchestrator (multi-window ensemble + zone conversion)
    fetcher.py                  -- multi-source OHLCV fetcher (Binance, GeckoTerminal, CoinGecko)
    filters.py                  -- 8 entry filters (btc_rsi, momentum, trend, regime, volume)
    tpsl.py                     -- take-profit / stop-loss calculation + R:R scoring
    detectors/                  -- 5 independent S/R detection methods
      ensemble.py               -- orchestrator (runs all 5, merges with strength weighting)
      market_structure.py       -- body/wick swing detection + break invalidation (40% weight)
      touch_count.py            -- weighted touch frequency counting (25% weight)
      nison_body.py             -- large candle body edges / institutional footprints (15% weight)
      volume_profile.py         -- VPVR-style high-volume node detection (10% weight)
      polarity_flip.py          -- support <-> resistance role reversal detection (10% weight)
  research/                     -- 40+ backtesting and validation scripts
    backtest.py                 -- trade-by-trade CSV recorder
    backtest_roro.py            -- RORO feature validation (walk-forward)
    backtest_portfolio.py       -- full portfolio simulations
    walkforward_mtf.py          -- walk-forward testing harness (6m IS / 6m OOS folds)
    optimize_mt_params.py       -- parameter grid search
    regime.py                   -- market regime classification
    FILTER_CATALOG.md           -- complete filter reference + walk-forward results
    BACKTEST_REPORT.md          -- latest performance summary
    PRESET_VALIDATION_REPORT.md -- configuration validation log
  backtest_model.py             -- top-level: simple daily backtest runner
  rank_tokens.py                -- top-level: RSI momentum token picker
  diagnose_detectors.py         -- top-level: individual detector visualisation
  compare.py                    -- top-level: validate against external sources (Grok/Perplexity)
  roro_features.py              -- top-level: 7 risk-on/risk-off market features
  data/                         -- cached OHLCV for 80+ tokens (gitignored)
  data49/                       -- 49-token validated universe subset (gitignored)
```

## Data Flow

```
Token List (top 49 by market cap, configurable)
    |
    v
Fetcher (Binance -> GeckoTerminal -> CoinGecko fallback, CSV cache)
    |
    v
S/R Analysis (3 timeframes x 5 detectors -> ensemble merge -> zone conversion)
    |
    v
TP/SL Computation (nearest R above, nearest S below, ATR cascade, R:R scoring)
    |
    v
Entry Filters (8 independent gates: RSI, ADX, DI, regime, volume, Bollinger)
    |
    v
Backtest / Ranking Output
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

| Filter | Rule | Purpose |
|---|---|---|
| `btc_rsi_floor` | BTC RSI(10) >= 50 | Market-wide risk gate |
| `token_rsi_momentum` | Token RSI(10) > 60 | Momentum confirmation |
| `token_adx_trend` | ADX(14) > 20 | Trend must exist |
| `token_di_bullish` | DI+ > DI- | Bullish directional pressure |
| `mt_regime_gate` | SMA(40) regime != DOWN | Block downtrends |
| `bollinger_pctb` | BB %B < 0.80 | Block overbought |
| `relative_volume` | RVOL(20) >= 1.5 | Volume confirmation |
| `rsi_cap` | RSI(10) < 80 | Overbought protection |

All filters validated via 6-month in-sample / 6-month out-of-sample walk-forward folds.

## Configuration

All tuneable parameters are in `core/config.py`:

- **Multi-timeframe windows:** 20d (weight 1.5), 60d (weight 1.0), 180d (weight 0.7)
- **Detector configs:** per-window settings for each detection method
- **Zone thresholds:** merge distance, ATR multipliers, tier classification rules
- **TP/SL rules:** min R:R = 1.2, ATR cascade for nearby levels
- **Token universe:** 80+ tokens with Binance/GeckoTerminal/CoinGecko mappings
- **Exclusion list:** 60+ stablecoins, wrapped assets, exchange tokens

## Key Design Patterns

- **ATR-normalised thresholds** — all distances and tolerances expressed as ATR multiples.
- **Exponential recency decay** — `exp(-ln(2) * bars_ago / halflife)`, configurable per window.
- **Body-first anchor snapping** — zones snap to large candle bodies (institutional levels) over wicks.
- **Strength-weighted pricing** — merged level price pulled toward stronger constituents.
- **Walk-forward validation** — all research uses 6m IS / 6m OOS folds, no lookahead.

## Output

- **backtest_model.py:** trade-by-trade results with P&L, hold time, exit reason.
- **rank_tokens.py:** JSON with passing tokens ranked by RSI.
- **diagnose_detectors.py:** per-detector visualisation on price chart.
- **research/ scripts:** CSV results, comparison tables, performance reports.

## Dependencies

```
numpy>=1.24
pandas>=2.0
scipy>=1.11
requests>=2.31
TA-Lib>=0.4
```

Optional: `mplfinance`, `matplotlib` (for visualisation/research).

## Known Gaps vs CODING_STANDARDS.md

- Some top-level scripts (`backtest_model.py`, `rank_tokens.py`, `compare.py`) could move to `scripts/`.
- `roro_features.py` exists at both root and in `research/` — duplication.
- Research scripts are functional but less polished than `core/` modules.
- No `.env.template` — no secrets currently needed, but would be needed if LLM comparison features are used.
- Mixed `print()` / `logging` across research scripts.
