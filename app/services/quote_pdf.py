"""Quotation PDF. Uses 'Rs.' because the built-in PDF fonts have no rupee glyph."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.config import settings
from app.models import PaymentTerm, Quote


def _rs(x: float) -> str:
    return f"Rs. {x:,.2f}"


def render_quote_pdf(db, quote: Quote) -> Path:
    out_dir = settings.data_dir / "quotes"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{quote.number}.pdf"
    styles = getSampleStyleSheet()
    term = db.get(PaymentTerm, quote.payment_term_code)

    story = [
        Paragraph(f"<b>{settings.company_name.upper()}</b>", styles["Title"]),
        Paragraph(f"{settings.company_address} &nbsp;|&nbsp; Ph: {settings.company_phone}", styles["Normal"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>QUOTATION {quote.number}</b>", styles["Heading2"]),
        Paragraph(
            f"Date: {quote.created_at:%d-%m-%Y} &nbsp;&nbsp; Valid till: {quote.valid_until:%d-%m-%Y} (rates are revised daily)",
            styles["Normal"],
        ),
        Paragraph(f"To: <b>{quote.party_name}</b>", styles["Normal"]),
        Paragraph(
            f"Delivery: {quote.destination or 'Ex-works Siltara'} &nbsp;&nbsp; Payment: {term.label if term else quote.payment_term_code}",
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
    ]

    rows = [["#", "Product", "Size / Grade", "Qty (MT)", "Rate/MT", "Amount", "GST"]]
    for i, line in enumerate(quote.lines, 1):
        rows.append([
            str(i),
            line.product.name,
            line.variant or "-",
            f"{line.qty_mt:g}",
            _rs(line.rate_per_mt),
            _rs(line.amount),
            f"{line.gst_rate * 100:g}%",
        ])
    rows += [
        ["", "", "", "", "Sub-total", _rs(quote.subtotal), ""],
        ["", "", "", "", "GST", _rs(quote.gst_total), ""],
        ["", "", "", "", "Total", _rs(quote.grand_total), ""],
    ]
    table = Table(rows, colWidths=[8 * mm, 42 * mm, 32 * mm, 18 * mm, 28 * mm, 32 * mm, 14 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b57")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -4), 0.4, colors.grey),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("FONTNAME", (4, -1), (5, -1), "Helvetica-Bold"),
    ]))
    story += [
        table,
        Spacer(1, 6 * mm),
        Paragraph("<b>Terms</b>", styles["Heading4"]),
        Paragraph("1. Rates are delivered (FOR destination) unless stated ex-works, exclusive of GST shown separately.", styles["Normal"]),
        Paragraph("2. Rates are subject to confirmation at the time of order due to daily raw-material movement.", styles["Normal"]),
        Paragraph("3. Mill test certificate supplied with every dispatch. Weighment at our weighbridge is final.", styles["Normal"]),
    ]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm).build(story)
    quote.pdf_path = str(path)
    return path
