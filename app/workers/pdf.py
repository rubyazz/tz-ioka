"""Pure PDF ticket renderer (reportlab platypus).

No database, no broker: takes a fully prepared :class:`TicketPDFData` and
returns PDF bytes. Atomic persistence and bookkeeping are the caller's concern
(see ``app.services.ticket`` and ``app.workers.pdf_worker``).
"""

import contextlib
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel
from reportlab.graphics.barcode import code128
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_MARGIN = 18 * mm
_USABLE_WIDTH = A4[0] - 2 * _MARGIN

_BRAND_DARK = colors.HexColor("#0F2557")
_ACCENT = colors.HexColor("#2563EB")
_GREY = colors.HexColor("#6B7280")
_LINE = colors.HexColor("#D1D5DB")
_ZEBRA = colors.HexColor("#F3F4F6")

_TITLE_STYLE = ParagraphStyle(
    "ticket-title",
    fontName="Helvetica-Bold",
    fontSize=17,
    leading=21,
    alignment=TA_CENTER,
    textColor=_BRAND_DARK,
)
_BRAND_STYLE = ParagraphStyle(
    "ticket-brand",
    fontName="Helvetica",
    fontSize=9,
    leading=12,
    alignment=TA_CENTER,
    textColor=_ACCENT,
)
_SECTION_STYLE = ParagraphStyle(
    "ticket-section",
    fontName="Helvetica-Bold",
    fontSize=10.5,
    leading=13,
    textColor=_BRAND_DARK,
    spaceBefore=12,
    spaceAfter=4,
)
_CELL_STYLE = ParagraphStyle("ticket-cell", fontName="Helvetica", fontSize=7.5, leading=9)
_CELL_EMPTY_STYLE = ParagraphStyle(
    "ticket-cell-empty", fontName="Helvetica-Oblique", fontSize=8, leading=10, textColor=_GREY
)
_NOTE_STYLE = ParagraphStyle(
    "ticket-note", fontName="Helvetica", fontSize=8.5, leading=11, textColor=_GREY
)
_TOTAL_STYLE = ParagraphStyle(
    "ticket-total", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=_BRAND_DARK
)

# Standard PDF fonts are WinAnsi-encoded; map the popular Unicode punctuation
# to ASCII look-alikes so it survives, everything else falls back to "?".
_TRANSLATIONS = str.maketrans(
    {
        "\u2192": "->",  # rightwards arrow
        "\u2014": "-",  # em dash
        "\u2013": "-",  # en dash
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u00a0": " ",  # no-break space
    }
)


class PassengerPDFData(BaseModel):
    """One traveller row of the PASSENGERS table."""

    type: str
    last_name: str
    first_name: str
    date_of_birth: date
    doc_type: str
    doc_number: str


class SegmentPDFData(BaseModel):
    """One itinerary row of the FLIGHTS table (times are UTC)."""

    origin: str
    destination: str
    flight_number: str
    carrier_code: str
    carrier_name: str
    departure: datetime
    arrival: datetime
    duration_minutes: int
    aircraft: str


class TicketPDFData(BaseModel):
    """Everything the renderer needs — fully denormalized, no lookups inside."""

    order_id: str
    ticket_number: str
    pnr: str
    status: str
    created_at: datetime
    issued_at: datetime
    agent_company: str
    contact_email: str
    total_amount: str
    currency: str
    passengers: list[PassengerPDFData]
    segments: list[SegmentPDFData]
    baggage: dict[str, Any] | None = None
    validating_carrier: str | None = None


def _latin(text: object) -> str:
    """Make text safe for the standard PDF fonts (never raises)."""
    raw = "" if text is None else str(text)
    raw = raw.translate(_TRANSLATIONS)
    try:
        raw.encode("cp1252")
    except UnicodeEncodeError:
        raw = raw.encode("cp1252", "replace").decode("cp1252")
    return raw


def _fmt_dt(value: datetime | None, *, with_date: bool = True) -> str:
    if value is None:
        return "-"
    fmt = "%d %b %Y %H:%M" if with_date else "%d %b %H:%M"
    return f"{value.strftime(fmt)} UTC"


def _fmt_duration(minutes: int) -> str:
    if minutes <= 0:
        return "-"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _fmt_bunch(label: str, info: object) -> str | None:
    """Format one baggage bunch, e.g. ``checked: 1 pc / 23 kg``."""
    if not isinstance(info, dict):
        return None
    pieces = info.get("pieces")
    kg = info.get("kg")
    if not pieces and not kg:
        return None
    parts = []
    if pieces:
        parts.append(f"{pieces} pc")
    if kg:
        parts.append(f"{kg} kg")
    return f"{label}: {' / '.join(parts)}"


def _fmt_baggage(baggage: dict[str, Any] | None) -> str:
    if not baggage:
        return "Baggage: not included"
    bunches = [
        _fmt_bunch("cabin", baggage.get("cabin")),
        _fmt_bunch("checked", baggage.get("checked")),
    ]
    present = [part for part in bunches if part]
    return "Baggage - " + "; ".join(present) if present else "Baggage: not included"


def _rows(
    headers: Sequence[str],
    items: Sequence[Sequence[str]],
    weights: Sequence[float],
) -> Table | None:
    """Build a styled data table, or None when there is nothing to show."""
    if not items:
        return None
    total_weight = sum(weights)
    col_widths = [_USABLE_WIDTH * w / total_weight for w in weights]
    data = [[_latin(h) for h in headers], *([_latin(cell) for cell in row] for row in items)]
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _BRAND_DARK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA]),
                ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _empty_note(text: str) -> Paragraph:
    return Paragraph(_latin(text), _CELL_EMPTY_STYLE)


def _info_block(data: TicketPDFData) -> Table:
    """Key-value header block: ticket identity, agent, contact, issue time."""
    rows = [
        ("Order ID", data.order_id),
        ("Ticket number", data.ticket_number or "-"),
        ("PNR", data.pnr),
        ("Status", data.status),
        ("Agent", data.agent_company or "-"),
        ("Contact", data.contact_email or "-"),
        ("Validating carrier", data.validating_carrier or "-"),
        ("Issued at", _fmt_dt(data.issued_at)),
    ]
    table = Table(
        [[_latin(label), _latin(value)] for label, value in rows],
        colWidths=[_USABLE_WIDTH * 0.28, _USABLE_WIDTH * 0.72],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("TEXTCOLOR", (0, 0), (0, -1), _GREY),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _flights_block(data: TicketPDFData) -> Flowable:
    headers = [
        "DATE",
        "FLIGHT",
        "FROM -> TO",
        "CARRIER",
        "DEPARTURE (UTC)",
        "ARRIVAL (UTC)",
        "DURATION",
        "AIRCRAFT",
    ]
    items = [
        [
            _fmt_dt(segment.departure),
            f"{_latin(segment.carrier_code)}-{_latin(segment.flight_number)}",
            f"{_latin(segment.origin)} -> {_latin(segment.destination)}",
            _latin(segment.carrier_name) or "-",
            _fmt_dt(segment.departure, with_date=False),
            _fmt_dt(segment.arrival, with_date=False),
            _fmt_duration(segment.duration_minutes),
            _latin(segment.aircraft) or "-",
        ]
        for segment in data.segments
    ]
    table = _rows(headers, items, weights=[1.15, 0.85, 1.0, 1.5, 1.0, 1.0, 0.7, 1.4])
    return table if table is not None else _empty_note("No flight segments on record.")


def _passengers_block(data: TicketPDFData) -> Flowable:
    headers = ["TYPE", "LAST NAME", "FIRST NAME", "DATE OF BIRTH", "DOCUMENT"]
    items = [
        [
            _latin(passenger.type),
            _latin(passenger.last_name),
            _latin(passenger.first_name),
            passenger.date_of_birth.strftime("%d %b %Y"),
            f"{_latin(passenger.doc_type)} {_latin(passenger.doc_number)}",
        ]
        for passenger in data.passengers
    ]
    table = _rows(headers, items, weights=[0.7, 1.3, 1.3, 1.0, 1.7])
    return table if table is not None else _empty_note("No passengers on record.")


def _draw_barcode(canvas: pdfcanvas.Canvas, ticket_number: str) -> None:
    """Code128 barcode of the ticket number, bottom right. Decorative: on any
    barcode failure the document is still emitted without it."""
    with contextlib.suppress(Exception):
        code = code128.Code128(
            _latin(ticket_number).strip(),
            barHeight=9 * mm,
            barWidth=0.55,
            humanReadable=True,
        )
        if code.width > _USABLE_WIDTH:  # squeeze to fit, however long the number
            code = code128.Code128(
                _latin(ticket_number).strip(),
                barHeight=9 * mm,
                barWidth=0.55 * _USABLE_WIDTH / code.width,
                humanReadable=True,
            )
        code.drawOn(canvas, A4[0] - _MARGIN - code.width, 8 * mm)


def _page_footer(data: TicketPDFData):
    """Footer callback: tz-ioka line bottom-left, barcode bottom right."""

    def draw(canvas: pdfcanvas.Canvas, doc: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setStrokeColor(_LINE)
        canvas.setLineWidth(0.4)
        canvas.line(_MARGIN, 16 * mm, A4[0] - _MARGIN, 16 * mm)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(_GREY)
        generated = datetime.now(UTC).strftime("%d %b %Y %H:%M:%S UTC")
        canvas.drawString(_MARGIN, 11.5 * mm, f"Generated automatically by tz-ioka at {generated}")
        if data.ticket_number:
            _draw_barcode(canvas, data.ticket_number)
        canvas.restoreState()

    return draw


def render_ticket_pdf(data: TicketPDFData) -> bytes:
    """Render the electronic ticket document and return the PDF bytes."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN + 6 * mm,  # keep clear of the footer strip
        title=f"Electronic ticket {data.ticket_number}".strip(),
        author="tz-ioka",
        subject=f"PNR {data.pnr}",
    )
    story: list[Flowable] = [
        Paragraph("ELECTRONIC TICKET / BOARDING PASS", _TITLE_STYLE),
        Spacer(1, 2),
        Paragraph("ioka travel - your journey starts here", _BRAND_STYLE),
        Spacer(1, 10),
        _info_block(data),
        Paragraph("FLIGHTS", _SECTION_STYLE),
        _flights_block(data),
        Paragraph("PASSENGERS", _SECTION_STYLE),
        _passengers_block(data),
        Spacer(1, 12),
        Paragraph(
            f"Total fare: {_latin(data.total_amount)} {_latin(data.currency)}",
            _TOTAL_STYLE,
        ),
        Paragraph(_latin(_fmt_baggage(data.baggage)), _NOTE_STYLE),
    ]
    doc.build(story, onFirstPage=_page_footer(data), onLaterPages=_page_footer(data))
    return buffer.getvalue()


__all__ = [
    "PassengerPDFData",
    "SegmentPDFData",
    "TicketPDFData",
    "render_ticket_pdf",
]
