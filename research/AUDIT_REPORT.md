# SR Dashboard — Comprehensive Codebase Audit

**Date:** 2026-04-03
**Auditor:** Claude Opus 4.6
**Branch:** main (commit `f31228c`)
**Scope:** Architecture, security, code quality, git hygiene, legacy code, design, UX/UI

---

## 1. Architecture

### Stack Overview

| Layer | Technology | File(s) |
|-------|-----------|---------|
| Pipeline | Python 3 (numpy, pandas, scipy, sklearn) | `main.py`, `core/` |
| API | FastAPI + Uvicorn | `dashboard.py` |
| Frontend | React 18 (CDN) + TailwindCSS (CDN) + Babel in-browser | `dashboard.html` |
| Charts | mplfinance (static PNG) + LightweightCharts (interactive) | `chart.py`, frontend |
| LLM | Anthropic Claude API | `report.py` |
| News | Grok + Perplexity APIs | `news/fetcher.py` |
| Reverse proxy | nginx | `/etc/nginx/sites-enabled/sr-dashboard` |
| Process mgmt | systemd | `/etc/systemd/system/sr-dashboard.service` |
| Scheduling | cron (root) | Daily pipeline at 05:00 UTC, news every 2h |

### Data Flow

```
CoinGecko/Binance -> fetch_and_cache -> data/*.csv
  -> analyze_token -> compute_tp_sl -> compute_indicators -> compute_filter_results
    -> rank_output.json -> generate_analyses (LLM) -> daily_output.json
    -> generate_chart -> output/*_chart.png

dashboard.py reads output/*.json and serves via FastAPI
dashboard.html fetches from /api/* + live prices from Binance/CoinGecko
```

### Infrastructure

- **systemd service** binds uvicorn to `127.0.0.1:8000` (correct -- not exposed directly)
- **nginx** listens on port 80, proxies `/api/` to uvicorn, serves `dashboard.html` at `/`
- **Cron** runs pipeline daily at 05:00 UTC with `flock` (good -- prevents overlap), news collection every 2h, compile at 05:55 weekdays, weekly digest Sundays at 05:50

### Findings

| Severity | Issue | Detail |
|----------|-------|--------|
| HIGH | No HTTPS | nginx listens on port 80 only. HTTP Basic Auth credentials and API key transmitted in plaintext. SSL config exists in `nginx.conf` but no site-level SSL block. |
| MEDIUM | Single server, no failover | All components (pipeline, API, data, output) on one machine. If disk fills or process crashes, entire service is down. systemd `Restart=always` mitigates process crashes. |
| MEDIUM | No log rotation for pipeline logs | `output/pipeline_*.log` files accumulate indefinitely. 40+ log files visible. No logrotate config. |
| MEDIUM | Nested `output/output/` directory | A stale 12MB `output/output/` directory contains duplicate chart PNGs and old pipeline logs, likely from a misconfigured pipeline run. Wastes disk space. |
| LOW | CDN dependency | React, Babel, TailwindCSS, LightweightCharts all loaded from CDNs. If CDN is down, dashboard is blank. |
| LOW | `@app.on_event("startup")` is deprecated | FastAPI recommends `lifespan` context manager instead. |

### Recommendations

1. **CRITICAL: Enable HTTPS** via Let's Encrypt / certbot. Without it, Basic Auth credentials are exposed in transit.
2. Add logrotate config to clean pipeline logs older than 14 days.
3. Delete `output/output/` directory.
4. Consider bundling frontend assets locally for reliability.

---

## 2. Security

### Authentication

- HTTP Basic Auth implemented with `secrets.compare_digest` (timing-safe comparison -- good).
- **`SR_NO_AUTH=1` is set in `.env`** -- authentication is completely disabled in production. Anyone who can reach port 80 can access all API endpoints without credentials.

### API Key Handling

- `.env` contains the Anthropic API key. The key is loaded via `EnvironmentFile` in the systemd service, which is the correct pattern. The `.env` file is gitignored (confirmed in `.gitignore`).
- The `.env` file is owned by root with default permissions (`-rw-r--r--`), meaning any user on the system can read it. Should be `0600`.

### CORS

- `SR_CORS_ORIGINS` defaults to `http://46.62.153.138` (the server IP). This is reasonable for a single-server setup.
- `allow_methods=["*"]` and `allow_headers=["*"]` are overly permissive but acceptable for an internal tool.

### Path Traversal

- **`/api/chart/{symbol}`**: The `symbol` parameter is uppercased, then interpolated into `f"{symbol}_chart.png"` and joined with `OUTPUT_DIR` via `os.path.join`. A request like `/api/chart/../../etc/passwd` would be uppercased to `../../ETC/PASSWD` and `os.path.join` would resolve it, but `os.path.exists` would check whether the traversed path exists. **This is a path traversal vulnerability.** An attacker could read any `.png` file on the filesystem.
- **`/api/token/{symbol}`**: Not vulnerable -- looks up the symbol in a JSON dictionary, never touches the filesystem.
- **`/api/candles/{symbol}`**: Same pattern as `/api/chart/` -- uppercases then does `os.path.join(DATA_DIR, f"{symbol}_daily.csv")`. **Also vulnerable to path traversal** for `.csv` files.

### Rate Limiting

- No rate limiting on any endpoint. An attacker could hammer the API.

### Input Validation

- Symbol parameters are uppercased but never validated against an allowlist. No regex check (e.g., `^[A-Z0-9]{1,10}$`).

### Findings

| Severity | Issue | Detail |
|----------|-------|--------|
| CRITICAL | `SR_NO_AUTH=1` in production | All endpoints publicly accessible without credentials. |
| CRITICAL | No HTTPS | Basic Auth credentials (when enabled) sent in plaintext over HTTP. |
| HIGH | Path traversal in `/api/chart/{symbol}` and `/api/candles/{symbol}` | Attacker can read arbitrary files matching pattern. Fix: validate symbol against `^[A-Z0-9]+$` regex and/or check resolved path starts with expected directory. |
| HIGH | `.env` world-readable | File permissions are `644`. Should be `600` with ownership restricted to `sr-dashboard` user. |
| MEDIUM | No rate limiting | API has no protection against abuse. |
| LOW | CORS `allow_methods=["*"]` | Overly permissive but low risk for internal tool. |

### Recommendations

1. **Immediately** set `SR_NO_AUTH=0` or remove the `SR_NO_AUTH=1` line from `.env`.
2. Add symbol validation regex to all endpoints that accept a symbol parameter:
   ```python
   import re
   if not re.match(r'^[A-Z0-9]{1,10}$', symbol):
       raise HTTPException(400, "Invalid symbol")
   ```
3. Additionally, verify resolved file paths stay within expected directories:
   ```python
   real_path = os.path.realpath(chart_path)
   if not real_path.startswith(os.path.realpath(OUTPUT_DIR)):
       raise HTTPException(400, "Invalid symbol")
   ```
4. Set `.env` permissions: `chmod 600 /opt/sr-dashboard/.env && chown sr-dashboard:sr-dashboard /opt/sr-dashboard/.env`
5. Enable HTTPS with Let's Encrypt.
6. Add `slowapi` or similar rate limiter.

---

## 3. Code Quality

### dashboard.py

| Severity | Issue | Detail |
|----------|-------|--------|
| HIGH | Path traversal (see Security) | Symbol not validated before filesystem access. |
| MEDIUM | `load_daily_output()` called per-request | Reads and parses the full JSON file on every API call. Should cache with TTL. |
| LOW | Unused import: `StaticFiles` | Imported but never used. |
| LOW | Endpoint order inconsistency | `/api/report/presets` defined after `if __name__` block. Works but confusing. |

### main.py

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | Double data load | In step 4, each token loads `load_from_cache` again even though step 3 already loaded the same data. Should pass the DataFrame through. |
| LOW | `--top` default is 10 but cron passes `--top 50` | The default is misleading; anyone running `python3 main.py` manually gets only 10. |

### core/tpsl.py

| Severity | Issue | Detail |
|----------|-------|--------|
| HIGH | Missing functions called | `score_support_confidence()` and `score_resistance_permeability()` are called on line 92-93 but never defined anywhere in the codebase. This will raise `NameError` whenever a token lacks both TP and SL. |
| MEDIUM | `potential_loss_abs` rounded incorrectly | Line 119: `round(potential_loss, 2)` loses precision for sub-cent tokens (e.g., SHIB at $0.000012). |

### core/filters.py

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | `bollinger_pctb` filter and `compute_bollinger_pctb` helper duplicate logic | The filter function (line 306) and the indicator function (line 356) compute the same thing independently. Should share code. |
| LOW | `FilterChain` class defined but never used in production code | Only `AVAILABLE_FILTERS` dict is used by `main.py`. The class is dead code in production. |

### core/fetcher.py

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | `datetime.utcnow()` used in news/fetcher.py | Deprecated in Python 3.12+. Should use `datetime.now(timezone.utc)`. |

### chart.py

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | Imports `argparse, sys, os, json` on one line | Style inconsistency; other files use one-per-line. |
| LOW | `fmt_price_zone` function defined but never called | Dead code. |

### report.py

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | `tokens_with_analysis` count logic fragile | Line 282: `"[" not in a.get("llm_analysis", "[")[:1]` -- checks if first char is `[` to detect failures. Brittle. |

### daily_pipeline.sh

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | `source /opt/sr-dashboard/.env` in shell | Exports all env vars including API key into shell environment. Acceptable for a pipeline script but could leak into child process environments. |

### strategies.json

- Well-structured. All 8 filters referenced exist in `AVAILABLE_FILTERS`.
- Cluster assignments appear complete (13 in A, 38 in C = 51 tokens).

---

## 4. GitHub vs Server Comparison

### Git Status

- Branch: `main`, up to date with `origin/main`.
- **One modified file:** `.claude/settings.local.json` (minor: additional tool permissions). Not important to track.
- **No stash entries.**
- **No untracked files** (good -- `.gitignore` is working).

### .gitignore Review

Currently ignores: `venv/`, `data/`, `data49/`, `output/`, `__pycache__/`, `*.pyc`, `.DS_Store`, `.env`, `credentials.json`, `*.token.json`, `logs/`.

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | `news/` directory not gitignored but contains no tracked files | The `news/__init__.py` and `news/fetcher.py`, `news/formatter.py` ARE tracked (correct). Data files in `data/news_buffer/` are under `data/` which is gitignored (correct). |
| LOW | `.claude/` directory is tracked | Contains `settings.local.json` which is IDE/tool-specific. Should probably be gitignored. |
| LOW | `.vscode/` directory is tracked | Contains `launch.json`. Usually gitignored for personal config. |
| LOW | `data49/` is gitignored but the directory exists | Appears to be a legacy data directory with a different token set. Could be cleaned up from the server. |

### Missing from .gitignore

- `.claude/` -- tool-specific config
- `.vscode/` -- IDE-specific config

---

## 5. Legacy Code

### `core/old/` Directory

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | `core/old/sr_analysis.py` | Exact copy of current `core/sr_analysis.py` header. This is a stale backup of the previous version. Should be deleted. |
| MEDIUM | `core/old/ensemble.py` | Stale backup of `core/detectors/ensemble.py`. Should be deleted. |
| MEDIUM | `core/old/market_structure.py` | Stale backup. Should be deleted. |

### Research Files with Broken Imports

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | `research/backtest_portfolio.py` imports `compute_token_score` from `core.tpsl` | This function does not exist in `core/tpsl.py`. Will crash on import. |
| LOW | `research/backtest_portfolio.py` imports `compute_regime` from `research.regime` | Function exists but uses the old multi-timeframe regime API, not the current `compute_regime_sma40`. |

### Dead Functions

| Severity | Issue | Detail |
|----------|-------|--------|
| HIGH | `score_support_confidence` and `score_resistance_permeability` | Called in `core/tpsl.py:92-93` but never defined. Will crash at runtime for tokens without both TP and SL. |
| LOW | `chart.py:fmt_price_zone()` | Defined but never called. |
| LOW | `FilterChain` class in `core/filters.py` | Not used in production pipeline. Only used conceptually. |
| LOW | `load_rank_output()` in `dashboard.py` | Fallback function rarely used since `daily_output.json` is always generated. |

### Stale Directories

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | `data49/` | Legacy data directory with 50 CSV files. Not referenced by any code. 924K+ of stale data. |
| MEDIUM | `output/output/` | Nested duplicate directory with 12MB of old charts and logs. Created by a pipeline misconfiguration. |

---

## 6. Design

### Data Model (`daily_output.json`)

The output JSON has a clean structure:
```
{
  "generated_at": ISO timestamp,
  "pipeline_timestamp": ISO timestamp,
  "model": { "min_rr": 1.2 },
  "tokens_analyzed": int,
  "tokens_qualified": int,
  "analyses": [
    {
      "symbol", "price", "take_profit", "stop_loss",
      "potential_gain_pct", "potential_loss_pct", "raw_rr",
      "qualified", "market_structure": {...},
      "support": [...zones], "resistance": [...zones],
      "indicators": {...}, "filter_results": {...},
      "cluster": "A"|"C",
      "llm_analysis": "text"
    }
  ]
}
```

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | Dual output files (`rank_output.json` + `daily_output.json`) | `rank_output.json` is written in step 4, then `daily_output.json` overwrites most of the same data in step 6. The rank file is only used as input to the LLM step and as a fallback in the API. Consider consolidating. |
| LOW | `analyses` key used in both rank and daily output with different schemas | In `rank_output.json`, `analyses` contains all scored tokens. In `daily_output.json`, it adds `llm_analysis`. The API handles both but the naming overlap is confusing. |

### API Contract

| Endpoint | Returns | Auth |
|----------|---------|------|
| `GET /api/status` | Pipeline status, staleness check | Yes (bypassed) |
| `GET /api/rank` | All tokens with indicators, filter results, clusters | Yes |
| `GET /api/token/{symbol}` | Full detail for one token including LLM text | Yes |
| `GET /api/chart/{symbol}` | Static PNG chart | Yes |
| `GET /api/candles/{symbol}` | OHLCV JSON from CSV | Yes |
| `GET /api/tokens` | Token list with Binance pairs | Yes |
| `GET /api/strategies` | Presets, clusters, filter catalog | Yes |
| `GET /api/coingecko-map` | CoinGecko ID mapping | Yes |
| `GET /api/news` | News intelligence feed | Yes |
| `GET /api/news/digest` | Claude-generated digest markdown | Yes |
| `GET /api/news/promoted` | Promoted topics | Yes |
| `GET /api/report/presets` | PDF report | Yes |
| `GET /api/report/presets-html` | HTML report | Yes |
| `GET /health` | Health check | No |

The API is read-only (GET only), which is a good security posture.

### Filter System Design

The filter architecture is well-designed:
1. Pipeline pre-computes all 8 indicator values and 8 filter pass/fail results per token.
2. `strategies.json` defines presets as named filter combinations.
3. Frontend applies presets client-side by checking `filter_results[filter_id] === true`.
4. This eliminates runtime computation in the API, making responses fast.

| Severity | Issue | Detail |
|----------|-------|--------|
| LOW | No versioning on `strategies.json` | If presets change, there is no way to know which version generated a given `daily_output.json`. |

### Pipeline Architecture

The pipeline (`main.py`) follows a clean sequential flow:
1. Determine token list (CoinGecko top N or CLI args)
2. Fetch/cache OHLCV data (incremental updates via Binance)
3. Run S/R analysis + TP/SL scoring
4. Compute indicators and filter results
5. Generate charts
6. Run LLM analysis (optional)

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | No pipeline health check / alerting | If the 05:00 UTC pipeline fails silently, stale data is served all day. The `stale` flag in `/api/status` detects this but nothing acts on it. |
| LOW | No atomic write for output files | If the pipeline crashes mid-write, `daily_output.json` could be corrupted. Should write to a temp file and `os.rename()`. |

---

## 7. UX/UI

### General Assessment

The frontend is a well-crafted single-page React app with a dark theme (zinc-950 background). It provides:
- Preset filter cards with backtest stats
- Two-cluster table view (Liquid / Volatile)
- Live prices from Binance (60s refresh)
- Token detail modal with interactive TradingView chart
- Live TP/SL recalculation based on current price
- News intelligence page with tag heatmap
- Search functionality

### Findings

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | In-browser Babel transpilation | Babel standalone is loaded to transpile JSX at runtime. This adds ~500ms+ to initial page load and increases bundle size. Should pre-compile. |
| MEDIUM | No error boundary | If any React component throws, the entire app crashes to a white screen. Wrap `<App>` in an error boundary. |
| MEDIUM | `dangerouslySetInnerHTML` in ReportModal | Line 382: Renders arbitrary HTML from the API without sanitization. If the HTML report is compromised, this is an XSS vector. |
| LOW | No keyboard navigation for modals | Token detail and report modals have no Escape key handler. Users must click the X button. |
| LOW | Table columns hide on mobile (`hidden md:table-cell`) | Support, Resistance, and Near columns disappear on small screens, leaving only Token, Price, 24h, R:R. This is acceptable but the R:R without context is less useful. |
| LOW | Loading state is minimal | Just "Loading Xris S/R Analyser..." text with pulse animation. No skeleton UI. |
| LOW | No empty state for news page | If news data is empty (not error), `news` is truthy but `macroItems` and `cryptoItems` could both be empty arrays, showing an empty page with no message. |
| LOW | Live price green dot always pulses | The green dot at top-right animates even when price feed fails. It should reflect actual connection status. |
| LOW | Search only filters by symbol | Cannot search by any other field (e.g., cluster, regime state). |

### Accessibility

| Severity | Issue | Detail |
|----------|-------|--------|
| MEDIUM | No ARIA labels | Table headers, buttons, and interactive elements lack `aria-label` attributes. Screen readers cannot navigate the interface. |
| LOW | Color-only indicators | Pass/fail status relies on green/red colors with no text/icon alternative for colorblind users. |
| LOW | Very small text sizes | Extensive use of `text-[10px]` and `text-[9px]` classes. May be difficult to read for users with visual impairments. |

### Mobile Responsiveness

- Viewport meta tag is present (good).
- TailwindCSS provides responsive utilities.
- Table layout uses `fixed` with specific widths -- works on desktop but columns are cramped on mobile.
- Preset grid uses `grid-cols-2 md:grid-cols-3` (good responsive behavior).
- Token detail modal uses `max-w-4xl mx-2 md:mx-4` (good margins).

---

## Prioritized Action Items

### CRITICAL (fix immediately)

1. **Enable authentication:** Remove `SR_NO_AUTH=1` from `/opt/sr-dashboard/.env` or set to `0`. All API endpoints are currently publicly accessible.
2. **Fix path traversal vulnerability:** Add symbol validation (`^[A-Z0-9]{1,10}$` regex) and resolved-path checks to `/api/chart/{symbol}` and `/api/candles/{symbol}` endpoints.
3. **Enable HTTPS:** Install certbot, obtain SSL certificate, configure nginx for TLS. Without this, any credentials are transmitted in plaintext.

### HIGH (fix this week)

4. **Fix missing functions in `core/tpsl.py`:** `score_support_confidence` and `score_resistance_permeability` are called but not defined. Add stub implementations or remove the calls. This crashes for tokens without both TP and SL.
5. **Fix `.env` file permissions:** `chmod 600 /opt/sr-dashboard/.env && chown sr-dashboard:sr-dashboard /opt/sr-dashboard/.env`.
6. **Add rate limiting:** Install `slowapi` and apply rate limits to all API endpoints.

### MEDIUM (fix this sprint)

7. **Delete `core/old/` directory:** Contains stale backup files that are never imported.
8. **Delete `output/output/` directory:** 12MB of duplicate/stale data from a misconfigured pipeline run.
9. **Cache `daily_output.json` in memory:** Currently re-read and parsed on every API request. Add a simple TTL cache (e.g., 60 seconds).
10. **Add pipeline failure alerting:** Send notification (email/Telegram) if the daily pipeline fails or if `/api/status` reports stale data.
11. **Pre-compile frontend JSX:** Replace in-browser Babel with a build step (Vite/esbuild). Reduces page load by 500ms+.
12. **Add React error boundary:** Prevent entire app crash on component errors.
13. **Sanitize HTML in ReportModal:** Use DOMPurify before `dangerouslySetInnerHTML`.
14. **Add log rotation:** Create logrotate config for `output/pipeline_*.log` files.
15. **Fix broken research imports:** `backtest_portfolio.py` references `compute_token_score` which no longer exists.
16. **Fix double data loading in `main.py`:** Pass DataFrames from step 3 to step 4 instead of reloading from cache.
17. **Use atomic writes for output JSON:** Write to temp file then `os.rename()`.

### LOW (backlog)

18. Add `.claude/` and `.vscode/` to `.gitignore`.
19. Delete `data49/` directory (stale legacy data).
20. Remove dead code: `fmt_price_zone()` in chart.py, unused `StaticFiles` import.
21. Consolidate `bollinger_pctb` filter and `compute_bollinger_pctb` helper into shared code.
22. Add Escape key handler to modals.
23. Add ARIA labels for accessibility.
24. Replace `datetime.utcnow()` with `datetime.now(timezone.utc)` in `news/fetcher.py`.
25. Add `strategies.json` versioning.
26. Migrate from deprecated `@app.on_event("startup")` to `lifespan` context manager.
27. Add empty state message for news page when no items exist.
28. Make green connection dot reflect actual price feed status.
29. Bundle frontend CDN dependencies locally for offline resilience.
