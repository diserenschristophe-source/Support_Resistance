"""Generate S/R Comparison PDF — Model vs Internet + SwissBorg."""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.join(_THIS_DIR, '..')
sys.path.insert(0, _PROJECT_ROOT)
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output")
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from core.fetcher import fetch_data
from core.sr_analysis import ProfessionalSRAnalysis
from core.models import fmt_price, compute_atr

# ── Styles ───────────────────────────────────────────────────
DARK   = HexColor("#1a1a2e")
ACCENT = HexColor("#0f3460")
GREEN  = HexColor("#1b5e20")
GREEN_LIGHT = HexColor("#e8f5e9")
RED    = HexColor("#b71c1c")
RED_LIGHT = HexColor("#ffebee")
GREY   = HexColor("#6c757d")
LIGHT  = HexColor("#e8e8e8")
WHITE  = HexColor("#ffffff")
PRICE_BG = HexColor("#fff3e0")
PRICE_COLOR = HexColor("#e65100")
SB_GREEN = HexColor("#01c38d")
SB_BG = HexColor("#e0f7ef")

styles = getSampleStyleSheet()
title_style = ParagraphStyle("T", parent=styles["Title"], fontSize=20, leading=26, textColor=DARK, spaceAfter=4)
subtitle_style = ParagraphStyle("Sub", parent=styles["Normal"], fontSize=10, leading=13, textColor=GREY, spaceAfter=12)
h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=14, leading=18, textColor=ACCENT, spaceBefore=14, spaceAfter=6)
h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11, leading=15, textColor=DARK, spaceBefore=10, spaceAfter=4)
body = ParagraphStyle("B", parent=styles["Normal"], fontSize=9, leading=12, textColor=DARK)
body_small = ParagraphStyle("BS", parent=body, fontSize=8, leading=10, textColor=GREY)
body_intro = ParagraphStyle("BI", parent=body, fontSize=8.5, leading=12, textColor=DARK, spaceAfter=6)

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=LIGHT, spaceAfter=6, spaceBefore=6)

def sp(n=4):
    return Spacer(1, n)

# SwissBorg levels (from weekly newsletter, wick-based)
SWISSBORG_LEVELS = {
    "BTC": {"support": [65700, 60000], "resistance": [75970, 79300, 90570]},
    "ETH": {"support": [1930, 1750], "resistance": [2380, 2740]},
    "SOL": {"support": [76, 64.5], "resistance": [95, 117]},
}


def extract_prices(text, current_price):
    patterns = [
        r'\$[\s]*([\d,]+(?:\.\d+)?)',
        r'([\d,]+(?:\.\d+)?)\s*(?:USD|USDT)',
        r'(?:^|\n)\s*-?\s*\$?([\d,]+(?:\.\d+)?)\s*$',
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
    if current_price > 0:
        min_p = current_price * 0.05
        max_p = current_price * 5.0
        prices = {p for p in prices if min_p <= p <= max_p}
    return sorted(prices)


def classify_from_text(text, current_price):
    supports, resistances = [], []
    lines = text.replace("|", "\n").split("\n")
    context = "unknown"
    for line in lines:
        lower = line.lower().strip()
        if "support" in lower: context = "support"
        elif "resistance" in lower: context = "resistance"
        for p in extract_prices(line, current_price):
            if context == "support": supports.append(p)
            elif context == "resistance": resistances.append(p)
            elif p < current_price: supports.append(p)
            else: resistances.append(p)
    return sorted(set(supports), reverse=True), sorted(set(resistances))


def merge_internet_levels(tagged_prices):
    """Merge internet levels within 0.5% of each other.
    Input: list of (price, source_name) tuples.
    Returns: list of (price, sources_set, count) tuples."""
    if not tagged_prices:
        return []
    sorted_tp = sorted(tagged_prices, key=lambda x: x[0])
    clusters = [[sorted_tp[0]]]
    for tp in sorted_tp[1:]:
        if abs(tp[0] - clusters[-1][-1][0]) / clusters[-1][-1][0] <= 0.005:
            clusters[-1].append(tp)
        else:
            clusters.append([tp])

    result = []
    for cl in clusters:
        median_price = cl[len(cl) // 2][0]
        sources = set(tp[1] for tp in cl)
        count = len(cl)
        result.append((round(median_price, 2), sources, count))
    return result


def build_pdf():
    OUT = os.path.join(OUTPUT_DIR, "sr_comparison_report.pdf")
    doc = SimpleDocTemplate(OUT, pagesize=landscape(A4),
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=12*mm, bottomMargin=12*mm)
    story = []

    with open(os.path.join(OUTPUT_DIR, "internet_levels.json")) as f:
        internet = json.load(f)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    story.append(Paragraph("S/R Analysis — Model vs Market Consensus", title_style))
    story.append(Paragraph(f"Generated {now} — BTC, SOL, ETH", subtitle_style))
    story.append(hr())

    # Body vs Wick intro
    story.append(Paragraph("<b>Body vs Wick — Anchoring Methodology</b>", h2))
    story.append(Paragraph(
        "This model uses a <b>body-first</b> approach for its core structure detection: "
        "candle bodies (open/close) define where the market committed, filtering out wick noise. "
        "Additional <b>wick rejections</b> are layered on top to capture levels where price was "
        "rejected but didn't close — these appear as [Wick] anchored levels.", body_intro))
    story.append(Paragraph(
        "<b>Pros of body-first:</b> cleaner levels, fewer false signals, reflects institutional positioning. "
        "<b>Cons:</b> can miss wick rejections that experienced traders see as key levels. "
        "The SwissBorg analysis (included below) uses <b>wick-based</b> levels from TradingView, "
        "which explains differences between the two approaches. Both are valid — "
        "body levels show where orders sat, wick levels show where price was rejected.", body_intro))
    story.append(hr())

    for sym in ["BTC", "SOL", "ETH"]:
        df = fetch_data(sym, 180)
        price = float(df["close"].iloc[-1])
        atr = compute_atr(df)
        result = ProfessionalSRAnalysis(df).analyze()

        sup_zones = result["support_zones"]
        res_zones = result["resistance_zones"]

        # Extract internet levels
        pplx_text = internet.get(sym, {}).get("perplexity", "")
        grok_text = internet.get(sym, {}).get("grok", "")
        pplx_sup, pplx_res = classify_from_text(pplx_text, price)
        grok_sup, grok_res = classify_from_text(grok_text, price)
        sb = SWISSBORG_LEVELS.get(sym, {"support": [], "resistance": []})

        # Tag each price with its source, then merge
        tagged_res = ([(p, "Perplexity") for p in pplx_res] +
                      [(p, "Grok") for p in grok_res] +
                      [(p, "SwissBorg") for p in sb["resistance"]])
        tagged_sup = ([(p, "Perplexity") for p in pplx_sup] +
                      [(p, "Grok") for p in grok_sup] +
                      [(p, "SwissBorg") for p in sb["support"]])
        merged_res = merge_internet_levels(tagged_res)
        merged_sup = merge_internet_levels(tagged_sup)

        # ── Token header ──────────────────────────────────────
        story.append(Paragraph(f"{sym}/USDT — {fmt_price(price)}", h1))
        story.append(Paragraph(f"ATR(14): {fmt_price(atr)} ({atr/price*100:.1f}%)", body_small))
        story.append(sp(6))

        # ── Build unified table ───────────────────────────────
        rows = []

        # Model resistances (furthest first)
        for z in reversed(res_zones):
            dist = (z.mid_price - price) / price * 100
            rows.append((z.key_level, "R", "Model",
                         f"{z.tier} [{z.anchor_type}]",
                         z.structural_role or "—",
                         str(z.confluence_score), f"+{dist:.1f}%"))

        # Merged internet + SwissBorg resistances
        for p, sources, count in reversed(merged_res):
            if p > price:
                dist = (p - price) / price * 100
                src_label = " + ".join(sorted(sources))
                if count > len(sources):
                    src_label += f" ({count}x)"
                anchor_note = "Wick-based" if "SwissBorg" in sources else "—"
                rows.append((p, "R", src_label, anchor_note, "—", "—", f"+{dist:.1f}%"))

        # Current price
        rows.append((price, "PRICE", "—", "—", "—", "—", "0.0%"))

        # Model supports (nearest first)
        for z in sup_zones:
            dist = (z.mid_price - price) / price * 100
            rows.append((z.key_level, "S", "Model",
                         f"{z.tier} [{z.anchor_type}]",
                         z.structural_role or "—",
                         str(z.confluence_score), f"{dist:.1f}%"))

        # Merged internet + SwissBorg supports
        for p, sources, count in merged_sup:
            if p < price:
                dist = (p - price) / price * 100
                src_label = " + ".join(sorted(sources))
                if count > len(sources):
                    src_label += f" ({count}x)"
                anchor_note = "Wick-based" if "SwissBorg" in sources else "—"
                rows.append((p, "S", src_label, anchor_note, "—", "—", f"{dist:.1f}%"))

        # Sort by price descending
        rows.sort(key=lambda r: r[0], reverse=True)

        # Deduplicate: if Model and Internet/SwissBorg are within 1x ATR, keep both but don't double-list
        # (they're already separate sources, just shown together)

        def fmt_p(p):
            if p >= 1000: return f"${p:,.0f}"
            elif p >= 10: return f"${p:,.1f}"
            elif p >= 1: return f"${p:,.2f}"
            else: return f"${p:,.4f}"

        tdata = [["", "Price Level", "Source", "Tier / Anchor", "Role", "Conf.", "Distance"]]

        for i, (p, typ, source, tier, role, conf, dist) in enumerate(rows):
            if typ == "PRICE":
                tdata.append([">>>", fmt_p(p), "CURRENT PRICE", "", "", "", ""])
            else:
                tdata.append([typ, fmt_p(p), source, tier, role, conf, dist])

        t = Table(tdata, colWidths=[25, 70, 145, 75, 60, 35, 50])

        table_style = [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("GRID", (0, 0), (-1, -1), 0.3, LIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]

        for i, (p, typ, source, *_) in enumerate(rows):
            row_idx = i + 1
            if typ == "R":
                if source == "Model":
                    table_style.append(("BACKGROUND", (0, row_idx), (-1, row_idx), RED_LIGHT))
                    table_style.append(("TEXTCOLOR", (0, row_idx), (0, row_idx), RED))
                    table_style.append(("FONTNAME", (0, row_idx), (-1, row_idx), "Helvetica-Bold"))
                elif "SwissBorg" in source:
                    table_style.append(("BACKGROUND", (0, row_idx), (-1, row_idx), SB_BG))
                    table_style.append(("TEXTCOLOR", (0, row_idx), (0, row_idx), RED))
                else:
                    table_style.append(("TEXTCOLOR", (0, row_idx), (0, row_idx), RED))
            elif typ == "S":
                if source == "Model":
                    table_style.append(("BACKGROUND", (0, row_idx), (-1, row_idx), GREEN_LIGHT))
                    table_style.append(("TEXTCOLOR", (0, row_idx), (0, row_idx), GREEN))
                    table_style.append(("FONTNAME", (0, row_idx), (-1, row_idx), "Helvetica-Bold"))
                elif "SwissBorg" in source:
                    table_style.append(("BACKGROUND", (0, row_idx), (-1, row_idx), SB_BG))
                    table_style.append(("TEXTCOLOR", (0, row_idx), (0, row_idx), GREEN))
                else:
                    table_style.append(("TEXTCOLOR", (0, row_idx), (0, row_idx), GREEN))
            elif typ == "PRICE":
                table_style.append(("BACKGROUND", (0, row_idx), (-1, row_idx), PRICE_BG))
                table_style.append(("TEXTCOLOR", (0, row_idx), (-1, row_idx), PRICE_COLOR))
                table_style.append(("FONTNAME", (0, row_idx), (-1, row_idx), "Helvetica-Bold"))

        t.setStyle(TableStyle(table_style))
        story.append(t)
        story.append(sp(4))
        story.append(Paragraph(
            "<i>Sources: Model = 5-detector ensemble (body-first) | "
            "Internet = Perplexity + Grok merged (Nx = number of sources agreeing) | "
            "*SwissBorg = weekly newsletter (wick-based, TradingView)</i>", body_small))
        story.append(sp(8))

        # Charts
        chart_path = os.path.join(OUTPUT_DIR, f"{sym}_sr_chart.png")
        if os.path.exists(chart_path):
            story.append(Paragraph("S/R Chart", h2))
            story.append(Image(chart_path, width=250*mm, height=120*mm))
            story.append(sp(6))

        diag_struct = os.path.join(OUTPUT_DIR, f"{sym}_diag_structure.png")
        if os.path.exists(diag_struct):
            story.append(Paragraph("Market Structure (with anchor candles)", h2))
            story.append(Image(diag_struct, width=250*mm, height=120*mm))
            story.append(sp(6))

        diag_combined = os.path.join(OUTPUT_DIR, f"{sym}_diag_combined.png")
        if os.path.exists(diag_combined):
            story.append(Paragraph("All Detectors Combined", h2))
            story.append(Image(diag_combined, width=250*mm, height=120*mm))

        if sym != "ETH":
            story.append(PageBreak())

    doc.build(story)
    print(f"Generated: {OUT}")


if __name__ == "__main__":
    build_pdf()
