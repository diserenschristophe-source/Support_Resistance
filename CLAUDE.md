# Support_Resistance

Standalone research lab for support/resistance detection on crypto assets.
Shares its `core/` engine verbatim with the production
[sr-dashboard](../sr-dashboard) repo, so any experiment validated here can be
moved into the dashboard with no API surgery — and any change in `core/` can
be diffed across both repos to confirm parity.

## Critical rule — do NOT modify core/

Claude must NEVER modify any file inside `core/` unless the user explicitly
asks for it. No bug fixes, no refactors, no import cleanups, no type
annotations, no "while I'm here" improvements. The code is production-tested
and working.

If a research script doesn't work with the current core/ API, the fix goes
in the research script — not in core/.

If a core/ change is approved, it must be complete and tested before being
pushed. A half-finished edit that gets pulled by sr-dashboard breaks production.

## core/ ownership and architecture

This repo (Support_Resistance) is the **single source of truth** for `core/`.
sr-dashboard does not have its own copy — it references this repo's `core/`
via a git submodule.

**Direction of changes is one-way:** Support_Resistance → sr-dashboard. Never
the reverse.

When a core/ change is approved:
1. Make the change in Support_Resistance/core/
2. Validate with research scripts
3. Push Support_Resistance to GitHub
4. In sr-dashboard: `git submodule update --remote && git add Support_Resistance && git commit`
5. Push sr-dashboard — server pulls with `--recurse-submodules`

## Why this repo exists

`sr-dashboard` is the production pipeline (fetch → analyze → rank → filters →
charts → report → web UI). This repo is the research playground that uses the
**same** `core/` so we can iterate on detectors, filters, and backtests
without touching the production cron, web server, or LLM stack.

## Stale research scripts (known broken, patch on revival)

`research/mtf_structure.py` references `MTF_REGIME_CONFIG`, which no longer
exists in `core/filters.py`. It fails at import but blocks nothing else. To
revive: rebuild whatever `MTF_REGIME_CONFIG` shape it needs locally, or wire
it to the current `compute_mt_regime` / `detect_regime_series` API.

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
