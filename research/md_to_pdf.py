"""Convert DOCUMENTATION.md to PDF."""
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Paragraph, Preformatted, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

DARK = HexColor("#1a1a2e")
ACCENT = HexColor("#0f3460")
LIGHT = HexColor("#e8e8e8")
CODE_BG = HexColor("#f4f4f4")

styles = getSampleStyleSheet()
title_s = ParagraphStyle("T", parent=styles["Title"], fontSize=22, leading=28, textColor=DARK, spaceAfter=6)
h1_s = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, leading=22, textColor=ACCENT, spaceBefore=20, spaceAfter=8)
h2_s = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, leading=18, textColor=ACCENT, spaceBefore=14, spaceAfter=6)
h3_s = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=11, leading=15, textColor=DARK, spaceBefore=10, spaceAfter=4)
body_s = ParagraphStyle("B", parent=styles["Normal"], fontSize=9, leading=13, textColor=DARK, spaceAfter=4)
code_s = ParagraphStyle("C", fontName="Courier", fontSize=7.5, leading=10, textColor=DARK,
                         leftIndent=10, backColor=CODE_BG, spaceBefore=4, spaceAfter=4)
bullet_s = ParagraphStyle("BL", parent=body_s, leftIndent=16, bulletIndent=6, spaceBefore=1, spaceAfter=1)

TICK3 = "```"
BULLET = "\u2022"


def hr():
    return HRFlowable(width="100%", thickness=0.5, color=LIGHT, spaceAfter=6, spaceBefore=6)


def safe(text):
    """Strip markdown to plain text safe for reportlab."""
    text = text.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text


def main():
    import os
    md_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "DOCUMENTATION.md")
    with open(md_path) as f:
        lines = f.readlines()
    print(f"Read {len(lines)} lines from {md_path}")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "sr_dashboard_documentation.pdf")
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm, topMargin=18*mm, bottomMargin=18*mm,
    )
    story = []
    in_code = False
    code_buf = []

    for line in lines:
        raw = line.rstrip()

        # Code blocks
        if raw.strip().startswith(TICK3):
            if in_code:
                if code_buf:
                    ct = "\n".join(code_buf)
                    ct = ct.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    story.append(Preformatted(ct, code_s))
                    code_buf = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_buf.append(raw)
            continue

        # Headers
        if raw.startswith("# ") and not raw.startswith("## "):
            story.append(Paragraph(safe(raw[2:]), title_s))
            story.append(hr())
            continue
        if raw.startswith("## "):
            story.append(Paragraph(safe(raw[3:]), h1_s))
            continue
        if raw.startswith("### "):
            story.append(Paragraph(safe(raw[4:]), h2_s))
            continue
        if raw.startswith("#### "):
            story.append(Paragraph(safe(raw[5:]), h3_s))
            continue

        # Horizontal rule
        if raw.strip() == "---":
            story.append(hr())
            continue

        # Bullet points
        if raw.strip().startswith("- ") or raw.strip().startswith("* "):
            story.append(Paragraph(safe(raw.strip()[2:]), bullet_s, bulletText=BULLET))
            continue

        # Numbered items
        m = re.match(r"^\s*(\d+)\.\s+(.+)", raw)
        if m:
            story.append(Paragraph(safe(m.group(2)), bullet_s, bulletText=f"{m.group(1)}."))
            continue

        # Table rows — render as plain text
        if raw.startswith("|"):
            cells = [c.strip() for c in raw.split("|")[1:-1]]
            if cells and not all(c.startswith("-") for c in cells):
                story.append(Paragraph(safe("  |  ".join(cells)), body_s))
            continue

        # Empty lines
        if not raw.strip():
            continue

        # Regular paragraph
        text = safe(raw)
        if text:
            story.append(Paragraph(text, body_s))

    print(f"Building PDF with {len(story)} elements...")
    if not story:
        print("ERROR: No content parsed from DOCUMENTATION.md")
        return
    doc.build(story)
    print(f"Generated: output/sr_dashboard_documentation.pdf ({len(story)} elements)")


if __name__ == "__main__":
    main()
