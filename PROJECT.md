# Support_Resistance

Standalone research lab for support/resistance detection on crypto assets.
Shares its `core/` engine verbatim with the production
[sr-dashboard](../sr-dashboard) repo, so any experiment validated here can be
moved into the dashboard with no API surgery — and any change in `core/` can
be diffed across both repos to confirm parity.

## Why this repo exists

`sr-dashboard` is the production pipeline (fetch → analyze → rank → filters →
charts → report → web UI). This repo is the research playground that uses the
**same** `core/` so we can iterate on detectors, filters, and backtests
without touching the production cron, web server, or LLM stack.

## Structure

```
Support_Resistance/
  core/                         # IDENTICAL to sr-dashboard/core/
    config.py
    models.py                   # SRLevel, SRZone, compute_atr
    sr_analysis.py              # ProfessionalSRAnalysis + analyze_token()
    fetcher.py                  # multi-source OHLCV (Binance / GeckoTerminal / MEXC / Hyperliquid / CoinGecko)
    filters.py                  # entry filters + AVAILABLE_FILTERS + FilterChain
    tpsl.py                     # compute_tp_sl + output_json + NumpySafeEncoder
    utils.py                    # atomic_json_write
    detectors/                  # 5-method ensemble
      ensemble.py
      market_structure.py       # 40% weight
      touch_count.py            # 25% weight
      nison_body.py             # 15% weight
      volume_profile.py         # 10% weight
      polarity_flip.py          # 10% weight

  research/                     # research & experimentation scripts
    compare.py                  # validate detector levels vs Grok / Perplexity / manual reference
    diagnose_detectors.py       # per-detector chart overlay (debugging)
    backtest_model.py           # signal log recorder → backtest_results.csv
    backtest_hourly.py
    backtest_rsi_sr.py
    roro_features.py            # 7 risk-on/off market features
    regime.py
    rsi_filter.py
    scan_regimes.py             # multi-token regime scanner
    weekly_structure.py
    fetch_hourly.py
    generate_backtest_report.py
    generate_comparison_pdf.py
    md_to_pdf.py
    AUDIT_REPORT.md
    PRESET_VALIDATION_REPORT.md

  data/                         # cached OHLCV CSVs (gitignored, shared via Support_Resistance/data)
  reference_levels.txt          # manual S/R ground-truth used by research/compare.py
  FILTER_CATALOG.md             # walk-forward filter validation reference
  PROJECT.md                    # this file
  requirements.txt
```

## Keeping core/ in sync with sr-dashboard

```bash
# verify core/ is identical
diff -rq core/ ../sr-dashboard/core/

# pull sr-dashboard's core/ on top (after committing local work first)
rsync -a --exclude='__pycache__' --delete ../sr-dashboard/core/ core/
```

A change accepted in either repo's `core/` should be propagated to the other
in the same PR. Treat divergence as a bug.

## Comparing outputs against sr-dashboard

Both repos can call the same `analyze_token()` → `compute_tp_sl()` →
`output_json()` chain. To diff:

```bash
# in sr-dashboard
python3 main.py --tokens BTC ETH SOL --skip-charts --skip-llm

# in Support_Resistance — write a tiny harness that calls the same
# functions and dumps to output/rank_output.json, then:
diff <(jq -S . ../sr-dashboard/output/rank_output.json) \
     <(jq -S . output/rank_output.json)
```

## Stale research scripts (known broken, patch on revival)

These scripts were already drifted out of sync with `core/` before this repo
was reorganised. They fail at import because they reference symbols that no
longer exist (`compute_token_score`, `MTF_REGIME_CONFIG`, `FALLBACK_TOP_50`).
None of them block anything else — they're parked for resurrection if the
underlying experiment becomes interesting again.

- `research/backtest.py`
- `research/backtest_mtf.py`
- `research/backtest_portfolio.py`
- `research/backtest_roro.py`
- `research/mtf_structure.py`

To revive: rename `compute_token_score` → `compute_tp_sl` and adapt the call
site to the current 1-arg signature; rebuild whatever `MTF_REGIME_CONFIG` /
`FALLBACK_TOP_50` constants are needed locally.

## Quick start

```bash
pip install -r requirements.txt

# Validate detector levels against manual reference
python3 research/compare.py

# Visualise per-detector levels on a price chart
python3 research/diagnose_detectors.py BTC

# Scan multi-token regimes
python3 research/scan_regimes.py

# Record signal log for filter analysis
python3 research/backtest_model.py --all --days 1000
```

## Dependencies

```
numpy, pandas, scipy, requests        # core engine
matplotlib, mplfinance                # research/diagnose_detectors.py + research/mtf_structure.py
```

No FastAPI, no LLM, no chart pipeline — those live in sr-dashboard.

## Data

`data/` holds cached daily OHLCV CSVs (109 tokens at the time of writing).
Fetcher logic lives in `core/fetcher.py` and is shared with sr-dashboard, so
both repos can populate the same cache layout. To repopulate from scratch
without sr-dashboard:

```python
from core.fetcher import fetch_and_cache
fetch_and_cache("BTC", days=730, data_dir="data")
```
