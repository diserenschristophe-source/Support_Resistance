#!/usr/bin/env python3
"""
compare.py — Compare detector S/R levels against external references.
=======================================================================
Fetches S/R levels from Grok, Perplexity, and a manual reference file,
then scores each detector (and the ensemble) against them.

Usage:
    python3 compare.py BTC
    python3 compare.py BTC ETH --days 180
    python3 compare.py BTC --skip-llm          # manual reference only

Reference sources:
    1. Grok (xAI API) — asks for current S/R levels
    2. Perplexity API — asks for current S/R levels + TradingView consensus
    3. Manual — plain text file: reference_levels.txt
"""

import argparse
import json
import os
import re
import sys
import time
from typing import List, Dict, Optional, Tuple

from core.fetcher import fetch_data, load_from_cache
from core.detectors.ensemble import SRDetector
from core.sr_analysis import ProfessionalSRAnalysis
from core.models import fmt_price


# ─────────────────────────────────────────────────────────────
# Load API keys from .env
# ─────────────────────────────────────────────────────────────

def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#") and line:
                    k, v = line.split("=", 1)
                    if v and k not in os.environ:
                        os.environ[k] = v

_load_env()


# ─────────────────────────────────────────────────────────────
# Price extraction from LLM text
# ─────────────────────────────────────────────────────────────

def extract_prices(text: str, current_price: float = 0) -> List[float]:
    """Extract dollar prices from LLM response text."""
    patterns = [
        r'\$[\s]*([\d,]+(?:\.\d+)?)',           # $65,000 or $65000.50
        r'([\d,]+(?:\.\d+)?)\s*(?:USD|USDT)',   # 65,000 USD
        r'(?:^|\n)\s*-?\s*\$?([\d,]+(?:\.\d+)?)\s*$',  # standalone price on a line
        r'(?:at|around|near|level|price)\s*\$?([\d,]+(?:\.\d+)?)',  # at 65000
    ]
    prices = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            try:
                val = float(match.group(1).replace(",", ""))
                if val > 0:
                    prices.add(val)
            except ValueError:
                continue

    # Filter out obvious noise: prices that are unrealistically small
    # (e.g. $71.0 when BTC is at $67,000 — clearly a misparse)
    if current_price > 0:
        min_plausible = current_price * 0.05   # at least 5% of current price
        max_plausible = current_price * 5.0    # at most 5x current price
        prices = {p for p in prices if min_plausible <= p <= max_plausible}

    return sorted(prices)


def classify_levels(prices: List[float], current_price: float) -> Dict[str, List[float]]:
    """Split prices into support and resistance relative to current price."""
    return {
        "support": sorted([p for p in prices if p < current_price], reverse=True),
        "resistance": sorted([p for p in prices if p > current_price]),
    }


def classify_levels_from_text(text: str, current_price: float) -> Dict[str, List[float]]:
    """Extract prices with context — uses 'support'/'resistance' labels in the text."""
    supports = []
    resistances = []

    # Split by lines and look for context
    lines = text.replace("|", "\n").split("\n")
    context = "unknown"
    for line in lines:
        lower = line.lower().strip()
        if "support" in lower:
            context = "support"
        elif "resistance" in lower:
            context = "resistance"

        line_prices = extract_prices(line, current_price)
        for p in line_prices:
            if context == "support":
                supports.append(p)
            elif context == "resistance":
                resistances.append(p)
            else:
                # Fallback: classify by position relative to price
                if p < current_price:
                    supports.append(p)
                else:
                    resistances.append(p)

    return {
        "support": sorted(set(supports), reverse=True),
        "resistance": sorted(set(resistances)),
    }


# ─────────────────────────────────────────────────────────────
# Source 1: Grok (xAI API)
# ─────────────────────────────────────────────────────────────

def fetch_grok_levels(symbol: str, current_price: float) -> Optional[Dict]:
    """Ask Grok for S/R levels."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("  [Grok] XAI_API_KEY not set — skipping", file=sys.stderr)
        return None

    try:
        import requests
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-3-mini",
                "messages": [
                    {"role": "system", "content": "You are a crypto technical analyst. Give specific price levels only. No explanations needed."},
                    {"role": "user", "content": (
                        f"What are the current key support and resistance price levels for {symbol}/USDT? "
                        f"Current price is ${current_price:,.0f}. "
                        f"List the 3-5 most important support levels and 3-5 most important resistance levels. "
                        f"Format: just the dollar prices, clearly labeled as Support or Resistance."
                    )},
                ],
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        levels = classify_levels_from_text(text, current_price)
        return {"source": "Grok", "raw_text": text, **levels}
    except Exception as e:
        print(f"  [Grok] Error: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────
# Source 2: Perplexity API
# ─────────────────────────────────────────────────────────────

def fetch_perplexity_levels(symbol: str, current_price: float) -> Optional[Dict]:
    """Ask Perplexity for S/R levels (includes web search)."""
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        print("  [Perplexity] PERPLEXITY_API_KEY not set — skipping", file=sys.stderr)
        return None

    try:
        import requests
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "sonar",
                "messages": [
                    {"role": "system", "content": "You are a crypto technical analyst. Give specific price levels with dollar amounts. Be precise."},
                    {"role": "user", "content": (
                        f"What are the current key support and resistance price levels for {symbol}/USDT? "
                        f"Current price is approximately ${current_price:,.0f}. "
                        f"Include what TradingView technical analysis shows if available. "
                        f"List the 3-5 most important support levels and 3-5 most important resistance levels with specific prices."
                    )},
                ],
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        levels = classify_levels_from_text(text, current_price)
        return {"source": "Perplexity", "raw_text": text, **levels}
    except Exception as e:
        print(f"  [Perplexity] Error: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────
# Source 3: Manual reference
# ─────────────────────────────────────────────────────────────

def load_manual_levels(symbol: str, current_price: float) -> Optional[Dict]:
    """Load manual reference levels from reference_levels.txt."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference_levels.txt")
    if not os.path.exists(path):
        print(f"  [Manual] reference_levels.txt not found — skipping", file=sys.stderr)
        return None

    with open(path) as f:
        content = f.read()

    # Find the section for this symbol
    # Format: BTC: 63000 65000 67000 | 73000 78000 85000
    #         (supports)              (resistances)
    # Or freeform text with prices
    symbol = symbol.upper()
    section = None
    for line in content.split("\n"):
        line = line.strip()
        if line.upper().startswith(symbol):
            section = line
            break

    if not section:
        print(f"  [Manual] No entry for {symbol} in reference_levels.txt", file=sys.stderr)
        return None

    # Try structured format: SYMBOL: s1 s2 s3 | r1 r2 r3
    after_colon = section.split(":", 1)[-1].strip() if ":" in section else section
    if "|" in after_colon:
        parts = after_colon.split("|")
        sup_prices = [float(p.strip()) for p in parts[0].split() if p.strip().replace(".", "").isdigit()]
        res_prices = [float(p.strip()) for p in parts[1].split() if p.strip().replace(".", "").isdigit()]
    else:
        # Freeform — extract all prices and classify
        all_prices = extract_prices(after_colon, current_price)
        classified = classify_levels(all_prices, current_price)
        sup_prices = classified["support"]
        res_prices = classified["resistance"]

    return {
        "source": "Manual",
        "raw_text": section,
        "support": sorted(sup_prices, reverse=True),
        "resistance": sorted(res_prices),
    }


# ─────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────

def score_detector_vs_reference(detector_levels: list, ref_supports: list,
                                 ref_resistances: list, current_price: float,
                                 tolerance_pct: float = 3.0) -> Dict:
    """
    Score a detector's output against reference levels.
    A detector level "matches" a reference if within tolerance_pct%.

    Returns: hits, misses, false_positives, precision, recall, f1
    """
    det_supports = [l for l in detector_levels if l.level_type == "support"]
    det_resistances = [l for l in detector_levels if l.level_type == "resistance"]

    def count_matches(det_list, ref_list):
        """Count how many reference levels are matched by detector levels."""
        matched_ref = set()
        matched_det = set()
        for i, ref_p in enumerate(ref_list):
            for j, det_l in enumerate(det_list):
                if abs(det_l.price - ref_p) / max(ref_p, 1) * 100 <= tolerance_pct:
                    matched_ref.add(i)
                    matched_det.add(j)
                    break
        return len(matched_ref), len(matched_det)

    s_hits, s_det_matched = count_matches(det_supports, ref_supports)
    r_hits, r_det_matched = count_matches(det_resistances, ref_resistances)

    total_hits = s_hits + r_hits
    total_ref = len(ref_supports) + len(ref_resistances)
    total_det = len(det_supports) + len(det_resistances)
    total_det_matched = s_det_matched + r_det_matched

    recall = total_hits / total_ref if total_ref > 0 else 0
    precision = total_det_matched / total_det if total_det > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    return {
        "support_hits": s_hits,
        "support_ref": len(ref_supports),
        "resistance_hits": r_hits,
        "resistance_ref": len(ref_resistances),
        "total_hits": total_hits,
        "total_ref": total_ref,
        "detector_levels": total_det,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    }


# ─────────────────────────────────────────────────────────────
# Main comparison
# ─────────────────────────────────────────────────────────────

DETECTOR_NAMES = {
    "structure": "Market Structure",
    "volume":    "Volume Profile",
    "touch":     "Touch Count",
    "nison":     "Nison Body",
    "polarity":  "Polarity Flip",
}


def compare_token(symbol: str, days: int = 180, data_dir: str = "data",
                  skip_llm: bool = False, tolerance_pct: float = 3.0):
    """Run full comparison for one token."""
    symbol = symbol.upper()

    # Load OHLCV
    df = load_from_cache(symbol, data_dir)
    if df is None or len(df) < 30:
        df = fetch_data(symbol, days)

    price = float(df["close"].iloc[-1])

    print(f"\n{'='*90}")
    print(f"  {symbol}/USDT — S/R COMPARISON  |  Price: {fmt_price(price)}  |  Tolerance: {tolerance_pct}%")
    print(f"{'='*90}")

    # ── Fetch reference levels ────────────────────────────────
    print(f"\n  FETCHING REFERENCE LEVELS", file=sys.stderr)
    references = []

    if not skip_llm:
        grok = fetch_grok_levels(symbol, price)
        if grok:
            references.append(grok)
            time.sleep(1)

        perplexity = fetch_perplexity_levels(symbol, price)
        if perplexity:
            references.append(perplexity)

    manual = load_manual_levels(symbol, price)
    if manual:
        references.append(manual)

    if not references:
        print("\n  No reference levels available. Add reference_levels.txt or set API keys.")
        return

    # Print reference levels
    for ref in references:
        print(f"\n  [{ref['source'].upper()}]")
        print(f"  Support:    {', '.join(fmt_price(p) for p in ref['support']) or 'none'}")
        print(f"  Resistance: {', '.join(fmt_price(p) for p in ref['resistance']) or 'none'}")
        if ref.get("raw_text"):
            # Show first 200 chars of raw response
            raw = ref["raw_text"][:300].replace("\n", " | ")
            print(f"  Raw: {raw}...")

    # ── Build consensus from all references ───────────────────
    all_sup = []
    all_res = []
    for ref in references:
        all_sup.extend(ref["support"])
        all_res.extend(ref["resistance"])

    # Cluster consensus levels (within tolerance)
    def cluster_prices(prices, tol_pct):
        if not prices:
            return []
        prices = sorted(prices)
        clusters = [[prices[0]]]
        for p in prices[1:]:
            if abs(p - clusters[-1][-1]) / clusters[-1][-1] * 100 <= tol_pct:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        return [round(sum(c) / len(c), 2) for c in clusters]

    consensus_sup = cluster_prices(all_sup, tolerance_pct)
    consensus_res = cluster_prices(all_res, tolerance_pct)

    print(f"\n  [CONSENSUS] (merged from {len(references)} sources)")
    print(f"  Support:    {', '.join(fmt_price(p) for p in consensus_sup) or 'none'}")
    print(f"  Resistance: {', '.join(fmt_price(p) for p in consensus_res) or 'none'}")

    # ── Run detectors ─────────────────────────────────────────
    det = SRDetector(df)
    detector_results = {
        "structure": det.detect_market_structure(),
        "volume":    det.detect_volume_profile(),
        "touch":     det.detect_touch_count(),
        "nison":     det.detect_nison_body(),
        "polarity":  det.detect_polarity_flip(),
    }

    # Also run ensemble
    ensemble_levels = det.detect_all(max_levels=20)

    # ── Score each detector vs consensus ──────────────────────
    print(f"\n  {'─'*86}")
    print(f"  DETECTOR SCORES vs CONSENSUS  (tolerance: {tolerance_pct}%)")
    print(f"  {'─'*86}")
    print(f"  {'DETECTOR':<20} {'LEVELS':>6} {'S-HIT':>6} {'S-REF':>6} "
          f"{'R-HIT':>6} {'R-REF':>6} {'PREC':>7} {'RECALL':>7} {'F1':>7}")
    print(f"  {'─'*86}")

    all_scores = {}
    for key, name in DETECTOR_NAMES.items():
        levels = detector_results[key]
        sc = score_detector_vs_reference(levels, consensus_sup, consensus_res,
                                         price, tolerance_pct)
        all_scores[key] = sc
        print(f"  {name:<20} {sc['detector_levels']:>6} {sc['support_hits']:>6} "
              f"{sc['support_ref']:>6} {sc['resistance_hits']:>6} {sc['resistance_ref']:>6} "
              f"{sc['precision']:>7.1%} {sc['recall']:>7.1%} {sc['f1']:>7.1%}")

    # Ensemble
    sc = score_detector_vs_reference(ensemble_levels, consensus_sup, consensus_res,
                                     price, tolerance_pct)
    all_scores["ensemble"] = sc
    print(f"  {'─'*86}")
    print(f"  {'ENSEMBLE':<20} {sc['detector_levels']:>6} {sc['support_hits']:>6} "
          f"{sc['support_ref']:>6} {sc['resistance_hits']:>6} {sc['resistance_ref']:>6} "
          f"{sc['precision']:>7.1%} {sc['recall']:>7.1%} {sc['f1']:>7.1%}")

    # ── Score vs individual references ────────────────────────
    for ref in references:
        if not ref["support"] and not ref["resistance"]:
            continue

        print(f"\n  {'─'*86}")
        print(f"  DETECTOR SCORES vs {ref['source'].upper()}")
        print(f"  {'─'*86}")
        print(f"  {'DETECTOR':<20} {'S-HIT':>6}/{ref['source'][:3].upper():>3} "
              f"{'R-HIT':>6}/{ref['source'][:3].upper():>3} {'RECALL':>7} {'F1':>7}")
        print(f"  {'─'*86}")

        for key, name in DETECTOR_NAMES.items():
            levels = detector_results[key]
            sc = score_detector_vs_reference(levels, ref["support"], ref["resistance"],
                                             price, tolerance_pct)
            print(f"  {name:<20} {sc['support_hits']:>6}/{sc['support_ref']:>3} "
                  f"{sc['resistance_hits']:>6}/{sc['resistance_ref']:>3} "
                  f"{sc['recall']:>7.1%} {sc['f1']:>7.1%}")

        sc = score_detector_vs_reference(ensemble_levels, ref["support"], ref["resistance"],
                                         price, tolerance_pct)
        print(f"  {'ENSEMBLE':<20} {sc['support_hits']:>6}/{sc['support_ref']:>3} "
              f"{sc['resistance_hits']:>6}/{sc['resistance_ref']:>3} "
              f"{sc['recall']:>7.1%} {sc['f1']:>7.1%}")

    # ── Missed levels ─────────────────────────────────────────
    print(f"\n  {'─'*86}")
    print(f"  MISSED BY ENSEMBLE (consensus levels not found by any detector)")
    print(f"  {'─'*86}")

    for label, ref_list in [("Support", consensus_sup), ("Resistance", consensus_res)]:
        for ref_p in ref_list:
            found = any(
                abs(l.price - ref_p) / max(ref_p, 1) * 100 <= tolerance_pct
                for l in ensemble_levels
            )
            if not found:
                dist = (ref_p - price) / price * 100
                print(f"    MISSED {label}: {fmt_price(ref_p)} ({dist:+.1f}% from price)")

    print()


def main():
    parser = argparse.ArgumentParser(description="Compare S/R detectors against external references")
    parser.add_argument("symbols", nargs="+", help="BTC ETH SOL etc.")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--data-dir", type=str, default="data")
    parser.add_argument("--skip-llm", action="store_true", help="Skip Grok/Perplexity, use manual only")
    parser.add_argument("--tolerance", type=float, default=3.0, help="Match tolerance in %% (default: 3)")
    args = parser.parse_args()

    for symbol in args.symbols:
        try:
            compare_token(symbol, args.days, args.data_dir,
                         skip_llm=args.skip_llm, tolerance_pct=args.tolerance)
        except Exception as e:
            print(f"[{symbol}] Error: {e}", file=sys.stderr)
            import traceback; traceback.print_exc()


if __name__ == "__main__":
    main()
