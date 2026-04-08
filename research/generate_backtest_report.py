"""Generate Backtest Research Report PDF."""
import os
import sys
from datetime import datetime, timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

DARK = HexColor("#1a1a2e")
ACCENT = HexColor("#0f3460")
GREEN = HexColor("#1b5e20")
GREEN_LIGHT = HexColor("#e8f5e9")
RED = HexColor("#b71c1c")
RED_LIGHT = HexColor("#ffebee")
GREY = HexColor("#6c757d")
LIGHT = HexColor("#e8e8e8")
WHITE = HexColor("#ffffff")
WINNER_BG = HexColor("#e8f5e9")

styles = getSampleStyleSheet()
title_s = ParagraphStyle("T", parent=styles["Title"], fontSize=22, leading=28, textColor=DARK, spaceAfter=6)
subtitle_s = ParagraphStyle("Sub", parent=styles["Normal"], fontSize=10, leading=13, textColor=GREY, spaceAfter=12)
h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, leading=22, textColor=ACCENT, spaceBefore=16, spaceAfter=8)
h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, leading=18, textColor=ACCENT, spaceBefore=12, spaceAfter=6)
h3 = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=11, leading=15, textColor=DARK, spaceBefore=8, spaceAfter=4)
body = ParagraphStyle("B", parent=styles["Normal"], fontSize=9.5, leading=14, textColor=DARK, spaceAfter=6)
body_small = ParagraphStyle("BS", parent=body, fontSize=8.5, leading=12, textColor=GREY)
bullet = ParagraphStyle("BL", parent=body, leftIndent=16, bulletIndent=6, spaceBefore=2, spaceAfter=2)
code_s = ParagraphStyle("C", fontName="Courier", fontSize=8, leading=11, textColor=DARK,
                         leftIndent=10, backColor=HexColor("#f4f4f4"), spaceBefore=4, spaceAfter=4)

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=LIGHT, spaceAfter=8, spaceBefore=8)

def sp(n=6):
    return Spacer(1, n)

def build_pdf():
    OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "backtest_research_report.pdf")
    doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    story = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Title ──
    story.append(Paragraph("S/R Model Backtest Research Report", title_s))
    story.append(Paragraph(f"Generated {now} | Period: Sep 2023 - Mar 2026 (2.5 years) | 16 tokens", subtitle_s))
    story.append(hr())

    # ── 1. Objective ──
    story.append(Paragraph("1. Objective", h1))
    story.append(Paragraph(
        "Evaluate the S/R (Support/Resistance) model as a TP/SL provider for long-only crypto trades. "
        "The model uses a 5-detector ensemble to identify price levels. This report tests different "
        "entry filters to determine which combination produces profitable results over a full market cycle "
        "(bull + bear).", body))

    # ── 2. Methodology ──
    story.append(Paragraph("2. Methodology", h1))

    story.append(Paragraph("2.1 Data", h2))
    story.append(Paragraph("Daily OHLCV candles from Binance, 1,000 days (Jul 2023 - Mar 2026).", body))
    story.append(Paragraph("Universe: BTC, ETH, XRP, SOL, ADA, LINK, SUI, AAVE, AVAX, TAO, DOGE, BNB, HBAR, DOT, NEAR, UNI (16 tokens).", body))
    story.append(Paragraph("Market conditions covered:", body))
    story.append(Paragraph("Bull market: Jul 2023 - Oct 2024 (BTC $30K to $70K)", bullet, bulletText="\u2022"))
    story.append(Paragraph("ATH / consolidation: Oct 2024 - Jan 2025 (BTC $70K to $108K)", bullet, bulletText="\u2022"))
    story.append(Paragraph("Bear market: Jan 2025 - Mar 2026 (BTC $108K to $67K)", bullet, bulletText="\u2022"))

    story.append(Paragraph("2.2 Trade Mechanics", h2))
    story.append(Paragraph("Capital: $1,000 starting. $100 per trade (unlimited concurrent positions).", body))
    story.append(Paragraph("Entry: at the daily close when the signal qualifies.", body))
    story.append(Paragraph("TP (Take Profit): nearest resistance level from the S/R model. If within 1x ATR of current price, cascade to the next resistance.", body))
    story.append(Paragraph("SL (Stop Loss): nearest support level from the S/R model. If within 1x ATR of current price, cascade to the next support.", body))
    story.append(Paragraph("Exit rules:", body))
    story.append(Paragraph("TP hit: close at TP price (limit order)", bullet, bulletText="\u2022"))
    story.append(Paragraph("SL hit: close at SL price (stop order)", bullet, bulletText="\u2022"))
    story.append(Paragraph("Both TP and SL hit same day: assume loss (conservative)", bullet, bulletText="\u2022"))
    story.append(Paragraph("Timeout: 10 days max hold, close at market close", bullet, bulletText="\u2022"))

    story.append(Paragraph("2.3 No Look-Ahead Bias", h2))
    story.append(Paragraph(
        "The S/R analysis is re-run for each day using only data available up to that day. "
        "The model sees df[:day+1] - no future data leaks into the signal or TP/SL calculation.", body))

    # ── 3. Filters Tested ──
    story.append(Paragraph("3. Entry Filters Tested", h1))

    story.append(Paragraph("3.1 No Filter (Baseline)", h2))
    story.append(Paragraph("Enter every day a valid TP and SL exist. No momentum or trend check.", body))

    story.append(Paragraph("3.2 RSI Filter", h2))
    story.append(Paragraph(
        "RSI (Relative Strength Index) measures momentum over a lookback period. "
        "RSI > 50 = price trending up. RSI > 60 = strong upward momentum.", body))
    story.append(Paragraph("Implementation: RSI(10) on daily closes. Enter only when token RSI exceeds threshold.", body))

    story.append(Paragraph("3.3 ADX + DI Filter", h2))
    story.append(Paragraph(
        "ADX (Average Directional Index) measures trend strength (0-100). "
        "DI+ and DI- measure directional pressure. "
        "ADX > 20 = trend exists. DI+ > DI- = bullish direction.", body))
    story.append(Paragraph("Implementation: ADX(14) and DI(14) from Wilder smoothing. Enter only when ADX > threshold AND DI+ > DI-.", body))

    story.append(Paragraph("3.4 Conservative Filter (from Python Backtester)", h2))
    story.append(Paragraph("Two-layer momentum filter:", body))
    story.append(Paragraph("BTC RSI floor: if BTC RSI(10) < 50, skip ALL trades across all tokens. "
                           "Rationale: when Bitcoin momentum is weak, altcoins tend to follow. "
                           "This blocks the entire portfolio during bear phases.", bullet, bulletText="1."))
    story.append(Paragraph("Token RSI threshold: individual token RSI(10) must be > 60. "
                           "Only enter tokens with strong upward momentum.", bullet, bulletText="2."))
    story.append(Paragraph(
        "Note: the original Python Backtester conservative model also includes hysteresis (sticky top-3 selection "
        "with 2-point RSI gap to switch) and top-N diversification. These were NOT implemented in this test. "
        "The results here use the simpler per-token RSI check without ranking or position limits.", body_small))

    story.append(Paragraph("3.5 R:R Filter", h2))
    story.append(Paragraph(
        "Risk/Reward ratio = (TP - entry) / (entry - SL). Only enter when potential gain exceeds potential loss "
        "by a minimum ratio (1.0x, 1.2x, 1.5x tested).", body))

    # ── 4. Results ──
    story.append(PageBreak())
    story.append(Paragraph("4. Results", h1))

    story.append(Paragraph("4.1 Strategy Comparison (Sep 2023 - Mar 2026)", h2))

    # Results table
    tdata = [
        ["Strategy", "Trades", "TP hit", "SL hit", "Timeout", "Win Rate", "Avg P&L", "Total PnL"],
        ["S/R only (no filter)", "3,310", "1,232 (37%)", "1,047 (32%)", "1,031 (31%)", "54.1%", "-0.09%", "-$312"],
        ["RSI>50", "1,842", "924 (50%)", "569 (31%)", "349 (19%)", "61.9%", "+0.06%", "+$113"],
        ["ADX>20 + DI+", "1,204", "677 (56%)", "337 (28%)", "190 (16%)", "66.8%", "-0.03%", "-$39"],
        ["RSI>50 + ADX>20 + DI+", "1,107", "638 (58%)", "311 (28%)", "158 (14%)", "67.2%", "+0.02%", "+$19"],
        ["ADX>20 + DI+ + RR>=1.0", "558", "171 (31%)", "267 (48%)", "120 (22%)", "39.0%", "+0.04%", "+$24"],
        ["Conservative RSI>60 + BTC>50", "1,242", "687 (55%)", "349 (28%)", "206 (17%)", "66.3%", "+0.72%", "+$896"],
        ["Conservative + ADX>20 + DI+", "831", "493 (59%)", "228 (27%)", "110 (13%)", "68.4%", "+0.26%", "+$216"],
    ]

    t = Table(tdata, colWidths=[130, 40, 70, 70, 70, 45, 45, 50])
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.3, LIGHT),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, HexColor("#f8f9fa")]),
        # Highlight winner
        ("BACKGROUND", (0, 7), (-1, 7), WINNER_BG),
        ("FONTNAME", (0, 7), (-1, 7), "Helvetica-Bold"),
    ]
    t.setStyle(TableStyle(ts))
    story.append(t)
    story.append(sp(8))

    # ── 4.2 Key Findings ──
    story.append(Paragraph("4.2 Key Findings", h2))

    story.append(Paragraph("Winner: Conservative RSI>60 + BTC floor>50", h3))
    story.append(Paragraph(
        "+$896 on $1,000 capital (+89.6% return over 2.5 years). 1,242 trades with 66.3% win rate "
        "and +0.72% average P&L per trade. This is the only strategy with both high win rate AND "
        "positive average P&L.", body))

    story.append(Paragraph("The BTC RSI floor is the key innovation", h3))
    story.append(Paragraph(
        "Without the BTC floor, RSI>60 alone would still enter trades during bear markets when "
        "individual token RSI temporarily spikes above 60. The BTC floor blocks ALL trades when "
        "Bitcoin momentum is weak (RSI<50), preventing the entire portfolio from bleeding during "
        "bear phases. This single filter is responsible for most of the performance improvement.", body))

    story.append(Paragraph("ADX reduces performance when combined with Conservative", h3))
    story.append(Paragraph(
        "Adding ADX>20 + DI+ on top of the Conservative filter drops performance from +$896 to +$216. "
        "The ADX filter is too restrictive - it cuts profitable trades that have good RSI momentum "
        "but happen during low-ADX (ranging) periods. Many profitable trades occur in early trend "
        "phases when ADX has not yet risen above 20.", body))

    story.append(Paragraph("R:R filter crashes win rate", h3))
    story.append(Paragraph(
        "Any R:R minimum filter (>=1.0, >=1.2, >=1.5) drops win rate from ~66% to ~30-39%. "
        "This is because high R:R selects trades with close SL and far TP. Close SL gets hit by "
        "normal daily volatility, resulting in many stop-outs. The data shows that winning trades "
        "actually have LOW R:R (0.62 avg) - close TP that gets hit quickly.", body))

    # ── 4.3 Winner Analysis ──
    story.append(Paragraph("4.3 What Separates Winners from Losers", h2))

    wl_data = [
        ["Feature", "Winners (TP hit)", "Losers (SL hit)", "Difference", "Signal"],
        ["DI spread", "16.5", "12.8", "+29%", "Stronger direction wins"],
        ["TP distance", "5.7%", "9.4%", "-39%", "Closer TP hits more often"],
        ["SL distance", "10.8%", "8.7%", "+25%", "Wider SL survives volatility"],
        ["R:R ratio", "0.62", "1.85", "-66%", "Low R:R wins (!)"],
        ["RSI at entry", "69.7", "65.0", "+7%", "Slightly higher momentum"],
        ["Hold duration", "2.7 days", "4.0 days", "-33%", "Winners resolve faster"],
    ]
    t2 = Table(wl_data, colWidths=[70, 80, 80, 55, 140])
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, LIGHT),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, HexColor("#f8f9fa")]),
    ]))
    story.append(t2)
    story.append(sp(6))
    story.append(Paragraph(
        "Counter-intuitive finding: traditional trading theory says high R:R is better. "
        "But in this model, LOW R:R (close TP, wide SL) wins because the TP gets hit quickly "
        "(2.7 days avg) before volatility can trigger the stop. High R:R trades have a far TP "
        "that rarely gets reached within the 10-day timeout.", body))

    # ── 5. Winning Formula ──
    story.append(Paragraph("5. Recommended Strategy", h1))
    story.append(Paragraph("The winning formula has 3 components:", body))
    story.append(Paragraph("BTC RSI(10) >= 50 - Market floor. Do not trade any token when Bitcoin momentum "
                           "is weak. This single rule prevents the portfolio from bleeding during bear markets.",
                           bullet, bulletText="1."))
    story.append(Paragraph("Token RSI(10) > 60 - Only enter tokens with strong upward momentum. "
                           "Ensures you are trading with the trend, not against it.",
                           bullet, bulletText="2."))
    story.append(Paragraph("S/R model for TP/SL - Use the 5-detector ensemble (Market Structure, Volume Profile, "
                           "Touch Count, Nison Body, Polarity Flip) to set exit levels. Nearest resistance for TP, "
                           "nearest support for SL, with 1x ATR cascade if too close. No R:R filter.",
                           bullet, bulletText="3."))

    # ── 6. Limitations ──
    story.append(Paragraph("6. Limitations and Next Steps", h1))
    story.append(Paragraph("No capital constraint: trades are $100 each regardless of available capital. "
                           "In reality, 10-15 concurrent positions would require $1,000-1,500.",
                           bullet, bulletText="\u2022"))
    story.append(Paragraph("Long-only: the model does not short. Performance is naturally worse in bear markets.",
                           bullet, bulletText="\u2022"))
    story.append(Paragraph("Daily candles: colleague's results on hourly candles show 65-76% win rate vs our 66%. "
                           "Hourly data may provide tighter TP/SL and faster exits.",
                           bullet, bulletText="\u2022"))
    story.append(Paragraph("Hysteresis not implemented: the full conservative model from the Python Backtester "
                           "includes top-3 ranking with 2-point RSI hysteresis. This may further improve results.",
                           bullet, bulletText="\u2022"))
    story.append(Paragraph("Slippage and fees not included: real trading incurs exchange fees (~0.1%) and slippage.",
                           bullet, bulletText="\u2022"))
    story.append(sp(8))

    story.append(Paragraph("Next research priorities:", body))
    story.append(Paragraph("Implement hysteresis + top-N diversification from the Python Backtester",
                           bullet, bulletText="1."))
    story.append(Paragraph("Test on hourly candles for comparison with colleague's results",
                           bullet, bulletText="2."))
    story.append(Paragraph("Add capital constraint and position sizing",
                           bullet, bulletText="3."))
    story.append(Paragraph("Build confidence index from winner/loser feature analysis (DI spread, TP distance)",
                           bullet, bulletText="4."))
    story.append(Paragraph("Test MTF (Multi-Timeframe) structure as additional filter",
                           bullet, bulletText="5."))

    # ── Footer ──
    story.append(hr())
    story.append(Paragraph(
        "<i>This report is for research purposes only. Not investment advice. "
        "Past performance does not guarantee future results.</i>", body_small))

    doc.build(story)
    print(f"Generated: {OUT}")


if __name__ == "__main__":
    build_pdf()
