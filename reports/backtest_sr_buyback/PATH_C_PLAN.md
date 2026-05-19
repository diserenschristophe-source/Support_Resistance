# Path C — TA Cross-Pollination Plan

**Date**: 2026-05-14
**Goal**: Apply the BTC-level uptrend filter learning from SR Buyback to the live TA agents (ta17, ta) and measure whether it improves their risk-adjusted returns.

**Hypothesis**: The same `BTC > SMA(100) AND SMA(100) rising 20 bars` regime gate that lifted SR Buyback's walk-forward Sharpe from 0.41 to 0.52 (and portfolio Sharpe to 2.4+) will also reduce TA's max DD without meaningfully cutting CAGR. Reasoning: TA already does well in BTC uptrends; the gate would skip the worst correction-period trades.

---

## What we found across the codebase

### Existing filter infrastructure

**`trading-system/filters.py`** — pure-function filter library, each returns a per-token DataFrame:
- `btc_rsi_floor(close, ...)` → DataFrame (same value for all tokens at each date)
- `token_rsi_momentum`, `rsi_cap`, `mt_not_downtrend`, `relative_volume`, etc.
- Registered in `ALL_FILTERS` list + computed by `compute_all()`

**`mt_regime_gate(close, sma_period=40, slope_bars=20)` already exists** at line 90 — it's exactly the formula we want (`close > SMA AND SMA rising`), just **per-token** instead of BTC-global. We need a BTC-specific variant.

**`trading-system/agents/ta/strategy.py`** — wraps each filter as a `passes_*` predicate, applies inside `scan_and_score`:
- `passes_mt_not_downtrend(close, sma_period, slope_bars)` — bool
- `passes_btc_rsi_floor(btc_close, period, threshold)` — bool
- Filters are opt-in via `filters_cfg` dict (keys absent = filter disabled)

**`trading-system/research/ta_5_7_filter_sweep.py`** — sweeps `OPTIONAL_FILTERS` tuple: 2⁵ = 32 combos currently. Adding one filter → 2⁶ = 64.

**`Support_Resistance/research/walkforward_optimize.py`** — uses its own `FILTER_NAMES` tuple. Currently 5 filters → 2⁵ = 32 combos × timeouts × tiebreakers × folds.

### Existing config

**`ta17.json`** uses 3 filters: `mt_not_downtrend`, `btc_rsi_floor`, `rsi_cap`.
**`ta.json`** uses 5 filters: above + `token_rsi_momentum`, `relative_volume`.

---

## The new filter

```python
def btc_sma_uptrend(close: pd.DataFrame,
                   btc_col: str = "BTC/USDT",
                   sma_period: int = 100,
                   slope_bars: int = 20) -> pd.DataFrame:
    """Global BTC regime gate: True iff BTC close > BTC SMA(100)
    AND SMA(100) rising over last 20 bars.

    Returns a DataFrame where every row has the same value across all
    token columns (BTC-derived gate applies universally). Same shape as
    other filter masks for easy AND-composition in compute_all().
    """
    btc_close = close[btc_col]
    sma = btc_close.rolling(sma_period).mean()
    above = btc_close > sma
    rising = sma.diff(slope_bars) > 0
    mask = above & rising
    # Broadcast to all columns
    return pd.DataFrame({c: mask for c in close.columns}, index=close.index)
```

**Parameters**:
- `sma_period=100` (vs `mt_not_downtrend`'s 40 — longer-term regime, not just "not in downtrend")
- `slope_bars=20` (same as `mt_not_downtrend` — 20-day slope check)

---

## File-by-file change list

### 1. `trading-system/filters.py`

**Additions** (no edits to existing logic):
- Add `btc_sma_uptrend` function (above).
- Add `"btc_sma_uptrend"` to `ALL_FILTERS` and `compute_all()`.

**Size**: ~15 lines.

### 2. `trading-system/agents/ta/strategy.py`

**Additions**:
- New `passes_btc_sma_uptrend(btc_close, sma_period, slope_bars) -> bool` (mirrors `passes_btc_rsi_floor`).
- In `scan_and_score`, between BTC RSI floor and the per-token filter loop, apply the new gate when `"btc_sma_uptrend" in filters_cfg`.

**Size**: ~20 lines.

### 3. `trading-system/research/ta_5_7_filter_sweep.py`

**Edit**:
- Add `"btc_sma_uptrend"` to `OPTIONAL_FILTERS` tuple. Doubles combo count to 64; sweep runs ~30 min on selected17.

**Size**: 1 line change + small report update.

### 4. `Support_Resistance/research/walkforward_optimize.py`

**Edit**:
- Add `"btc_sma_uptrend"` to `FILTER_NAMES`. Doubles `2^N` combos.
- Update the per-combo simulation to apply the new mask.

**Size**: ~5 lines.

### 5. `trading-system/config/agents/ta17.json` and `ta.json`

**Only update AFTER validation passes.** Add to `filters` block:
```json
"btc_sma_uptrend": { "sma_period": 100, "slope_bars": 20 }
```

**Size**: 1 line per config.

---

## Validation plan

### Phase A — Sanity test (5 min)

1. Add filter function only (file 1 above).
2. Write a quick test script in `trading-system/research/test_btc_sma_uptrend.py`:
   - Compute mask on the existing OHLCV cache
   - Print: % of dates True, monthly breakdown
   - Visual check: should show False during BTC correction periods (Feb 2025, Oct 2025, Jan 2026)

### Phase B — Sweep (30 min)

1. Add to ta_5_7_filter_sweep (file 3).
2. Run on selected17:
   ```bash
   cd /Users/xris/GitHub/trading-system && \
     python3 research/ta_5_7_filter_sweep.py --universe selected17
   ```
3. Inspect `all_combos_ranked.csv`:
   - Does the top combo include `btc_sma_uptrend`?
   - If YES → strong signal that BTC-level gating helps
   - If NO → BTC RSI floor is already capturing the regime info; new filter is redundant

### Phase C — Walk-forward (30 min)

1. Add to walkforward_optimize (file 4).
2. Run:
   ```bash
   cd /Users/xris/GitHub/Support_Resistance && \
     python3 research/walkforward_optimize.py
   ```
3. Check Pareto frontier:
   - Does adding `btc_sma_uptrend` to the winning combo improve OOS Sharpe?
   - Does it reduce OOS max DD?

### Phase D — A/B comparison (~10 min)

Run `ta_sweep_full.py` twice (or once with the new filter, compare to baseline saved earlier):
- Current production config (mt_not_downtrend + btc_rsi_floor + rsi_cap) — known +117.9% / -22.5% DD
- + btc_sma_uptrend enabled

Compare:
- CAGR change
- Max DD change
- Sharpe change
- Trade count change

### Phase E — Production deployment (only if Phases A-D pass)

1. Update `ta17.json` and `ta.json` filters blocks.
2. Push to server.
3. Monitor first 2-4 weeks for unexpected behavior.

---

## Expected outcomes

### Best case
- Sharpe lifts 0.3-0.5 due to skipping correction-period trades
- Max DD shrinks from −22.5% to ~−15%
- CAGR roughly flat or slightly down (filter blocks some trades that would have won)
- Lower trade count (~30-40% fewer)

### Likely case
- Modest Sharpe improvement (0.1-0.2)
- Modest DD reduction (~3-5%)
- Trade count down ~30%
- CAGR slightly down (filter is over-conservative when BTC briefly dips)

### Worst case
- Filter blocks all trades during a few good months (e.g., Q2 2025 recovery)
- CAGR drops materially
- Sharpe net negative because trade count drops below useful sample
- Decision: don't deploy; the filter is duplicating what TA's existing gates already do

---

## Comparison to SR Buyback's filter

| Aspect | SR Buyback btc_uptrend | TA btc_sma_uptrend |
|---|---|---|
| When applied | At signal detection time | At daily scan time |
| Effect on signal count | −60% (399 → 157 in selected17) | Estimated −30-40% (TA already filters more) |
| Walk-forward impact | Sharpe 0.41 → 0.52 ✓ | TBD — that's what we're testing |
| Portfolio impact | Sharpe 0.87 → 2.41 | TBD |
| TA already has similar? | No | Partial — `btc_rsi_floor` is a short-term regime gate, `mt_not_downtrend` is per-token |

**Key uncertainty**: TA's existing `mt_not_downtrend` is per-token (not global BTC), and `btc_rsi_floor` is short-term (RSI, not multi-month trend). The new `btc_sma_uptrend` adds a longer-horizon BTC-specific gate that neither captures. **Should be additive, not redundant.**

---

## Risks and gotchas

1. **Data dependency**: `mt_regime_gate` and `mt_not_downtrend` use 40-day SMA. Adding a 100-day SMA filter means tokens need 100 days of history. Currently OK for the live tokens (all >1 year). But the first ~100 trading days of any new token would have the filter masked.

2. **Filter interaction with `mt_not_downtrend`**: both check BTC trend. If both are ON, redundancy could over-filter. The sweep will reveal this — if `mt_not_downtrend=False, btc_sma_uptrend=True` outperforms `both=True`, drop one.

3. **Cron schedule unchanged**: TA runs daily at 00:05/00:10 UTC. The new filter just adds one more daily check — no scheduling change needed.

4. **Backward compat**: filter is opt-in via config; existing configs continue to work unchanged until we explicitly add the key.

---

## When to start

After the hl44 quick sweep finishes (currently running). Two reasons:

1. The quick sweep's per-knob monotonicity table will show whether `btc_uptrend=True` is the right call on hl44 too — that's evidence for whether the same filter applies broadly or is selected17-specific.
2. The hl44 sweep is producing useful disk I/O. Better to keep the Mac's full attention on it until done.

After hl44 quick result:
1. Read the per-knob table — does `btc_uptrend=True` show meaningful lift on hl44 as well?
2. If YES → strong signal that BTC-level regime gating is universally useful → confidently move to Phase A on TA
3. If NO → BTC-level filter might be selected17-specific; still worth trying on TA (different signal mechanic) but with lower expectation

---

## Sequence summary

```
[Now]                hl44 quick sweep running (~30 min)
[After quick]        Review per-knob: btc_uptrend effect on hl44
[Decision point]     If positive on hl44 → high confidence proceeding to TA
                     If negative → still proceed but watch carefully
[Phase A]            Add filter function + sanity test           (15 min)
[Phase B]            Add to ta_5_7_filter_sweep, run sweep        (45 min)
[Phase C]            Add to walkforward_optimize, run             (45 min)
[Phase D]            A/B against current production              (15 min)
[Decision point]     Deploy or shelve based on metrics
[Phase E]            Production deployment if validated          (configs + push)
```

Total Phase A-D work: **~2 hours active time + sweep durations.**

---

## Open questions for the user

1. **`sma_period` choice**: 100 days is the SR Buyback default. Should we sweep `{60, 100, 150}` to see if a different look-back is better for TA's daily cadence?
2. **Apply to which agents**: just ta17 (curated), or also ta (44-token)? TA17 is the production winner; ta gets less attention.
3. **`mt_not_downtrend` interaction**: if `btc_sma_uptrend` wins decisively, do we keep `mt_not_downtrend` (per-token gate) as a secondary check, or replace it?

Once these are clarified, the actual code changes are mechanical and small (~50 total LOC across 4 files).
