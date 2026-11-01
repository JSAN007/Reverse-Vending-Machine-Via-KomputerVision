"""Compact 80 mm shopping-voucher layout, generated from persisted deposits."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import HRFlowable, Paragraph, Spacer, Table, TableStyle

RECEIPT_WIDTH = 80 * mm
MARGIN = 5 * mm
CONTENT_WIDTH = RECEIPT_WIDTH - 2 * MARGIN
MAX_PAGE_HEIGHT = 280 * mm


def rupiah(value):
    return "Rp" + f"{value:,}".replace(",", ".")


def jakarta(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M WIB")


def build_receipt(session):
    def text(value, size=8, bold=False, align=TA_LEFT):
        style = ParagraphStyle("receipt", fontName="Courier-Bold" if bold else "Courier",
            fontSize=size, leading=size * 1.35, alignment=align, textColor=colors.black,
            splitLongWords=True, wordWrap="CJK")
        return Paragraph(escape(str(value)), style)

    def rule():
        return HRFlowable(width="100%", thickness=.5, color=colors.black,
            dash=[2, 2], spaceBefore=0, spaceAfter=0)

    def pair(left, right, bold=False, size=8):
        row = Table([[text(left, size, bold), text(right, size, bold, TA_RIGHT)]],
            colWidths=[CONTENT_WIDTH * .53, CONTENT_WIDTH * .47])
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return row

    # Keep an item's name and its quantity/subtotal together across page boundaries.
    def together(flowables):
        table = Table([[flowable] for flowable in flowables], colWidths=[CONTENT_WIDTH])
        table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return table

    header = [text("ECOVISION", 15, True, TA_CENTER),
        text("VOUCHER BELANJA", 9, True, TA_CENTER),
        text("Program Pengembalian Botol", 7, align=TA_CENTER), Spacer(1, 3 * mm),
        text("No. " + session["id"], 7), text("Terbit: " + jakarta(session["finished_at"]), 7),
        Spacer(1, 2 * mm), rule(), Spacer(1, 2 * mm)]
    grouped = Counter((item["label"], item["amount_rupiah"]) for item in session["deposits"])
    body = []
    for (label, price), quantity in sorted(grouped.items()):
        body.append(together([text(label, 8, True), pair(f"{quantity} x {rupiah(price)}", rupiah(quantity * price)), Spacer(1, 2 * mm)]))
    if not grouped:
        body.append(text("Tidak ada pengembalian botol."))
    body.append(together([rule(), Spacer(1, 2 * mm),
        pair("Total botol", str(session["bottles"])),
        pair("NILAI VOUCHER", rupiah(session["total_rupiah"]), True, 10),
        Spacer(1, 2 * mm), rule(), Spacer(1, 3 * mm),
        text("Terima kasih atas partisipasi Anda.", 7, align=TA_CENTER), Spacer(1, 1 * mm),
        text("Simpan voucher ini sebagai referensi pengembalian botol Anda.", 7, align=TA_CENTER),
        Spacer(1, 2 * mm), text("Penukaran voucher belum tersedia.", 6.5, align=TA_CENTER)]))

    def measured(flowable):
        return (flowable, flowable.wrap(CONTENT_WIDTH, MAX_PAGE_HEIGHT)[1])

    header = [measured(item) for item in header]
    header_height = sum(height for _, height in header)
    budget = MAX_PAGE_HEIGHT - 2 * MARGIN - header_height - 5 * mm
    pages, page, height = [], [], 0
    for flowable in body:
        item = measured(flowable)
        if page and height + item[1] > budget:
            pages.append(page)
            page, height = [], 0
        page.append(item)
        height += item[1]
    pages.append(page)
    output = BytesIO()
    canvas = Canvas(output, pagesize=(RECEIPT_WIDTH, 100 * mm), pageCompression=1)
    canvas.setTitle("EcoVision - Voucher Belanja " + session["id"])
    canvas.setAuthor("EcoVision RVM")
    for index, items in enumerate(pages, 1):
        content = header + items
        page_height = 2 * MARGIN + sum(height for _, height in content) + (5 * mm if len(pages) > 1 else 0)
        canvas.setPageSize((RECEIPT_WIDTH, page_height))
        y = page_height - MARGIN
        for flowable, height in content:
            y -= height
            flowable.drawOn(canvas, MARGIN, y)
        if len(pages) > 1:
            canvas.setFont("Courier", 7)
            canvas.drawCentredString(RECEIPT_WIDTH / 2, MARGIN, f"Halaman {index}/{len(pages)}")
        canvas.showPage()
    canvas.save()
    return output.getvalue()
