"""Ticket (PDF) generation facade — real implementation.

Callers (orders API) only ever see the three methods of :class:`TicketService`:

- :meth:`TicketService.request_generation` publishes a job to RabbitMQ and
  returns ``False`` when the broker is unreachable (the caller may then fall
  back to :meth:`TicketService.generate_sync`, per ``settings.ticket_fallback_sync``).
- :meth:`TicketService.get_ticket_path` resolves the stored PDF path.

Rendering itself lives in the pure module ``app.workers.pdf`` and is shared
with the standalone consumer ``app.workers.pdf_worker`` (via the module-level
helpers below).
"""

import base64
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import get_session_factory
from app.messaging.bus import get_bus
from app.models import Agent, Order, OrderPassenger
from app.workers.pdf import (
    PassengerPDFData,
    SegmentPDFData,
    TicketPDFData,
    render_ticket_pdf,
)

logger = get_logger(__name__)


def _pnr(order_id: uuid.UUID) -> str:
    """Stable 6-character PNR derived from the order UUID (base32 of its bytes)."""
    return base64.b32encode(order_id.bytes)[:6].decode("ascii").upper()


def _as_utc(value: Any) -> datetime:
    """Coerce snapshot/ORM values to an aware datetime (epoch as a sentinel).

    Snapshot fields arrive from JSONB, so they may be ISO strings, datetimes
    or missing — this must never crash ticket rendering.
    """
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return datetime(1970, 1, 1, tzinfo=UTC)
    else:
        parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_ticket_data(
    order: Order,
    passengers: Sequence[OrderPassenger],
    agent_company: str,
) -> TicketPDFData:
    """Denormalize an order (+ passengers + agent name) for the renderer."""
    snapshot: dict[str, Any] = order.offer_snapshot or {}
    contact: dict[str, Any] = order.contact or {}
    segments = [
        SegmentPDFData(
            origin=str(seg.get("origin") or ""),
            destination=str(seg.get("destination") or ""),
            flight_number=str(seg.get("flight_number") or ""),
            carrier_code=str(seg.get("carrier_code") or ""),
            carrier_name=str(seg.get("carrier_name") or ""),
            departure=_as_utc(seg.get("departure")),
            arrival=_as_utc(seg.get("arrival")),
            duration_minutes=int(seg.get("duration_minutes") or 0),
            aircraft=str(seg.get("aircraft") or ""),
        )
        for seg in snapshot.get("segments") or []
        if isinstance(seg, dict)
    ]
    baggage = snapshot.get("baggage")
    return TicketPDFData(
        order_id=str(order.id),
        ticket_number=order.ticket_number or "",
        pnr=_pnr(order.id),
        status=str(order.status),
        created_at=_as_utc(order.created_at),
        issued_at=_as_utc(order.issued_at),
        agent_company=str(agent_company or ""),
        contact_email=str(contact.get("email") or ""),
        total_amount=str(order.total_amount),
        currency=str(order.currency or snapshot.get("currency") or "USD"),
        passengers=[
            PassengerPDFData(
                type=str(p.pax_type),
                last_name=p.last_name,
                first_name=p.first_name,
                date_of_birth=p.date_of_birth,
                doc_type=str(p.doc_type),
                doc_number=p.doc_number,
            )
            for p in passengers
        ],
        segments=segments,
        baggage=baggage if isinstance(baggage, dict) else None,
        validating_carrier=str(snapshot.get("validating_carrier") or "") or None,
    )


def write_pdf_atomic(order_id: uuid.UUID, pdf_bytes: bytes) -> Path:
    """Write the PDF to ``<storage_dir>/<order_id>.pdf`` atomically.

    The payload first lands in a ``.tmp`` sibling (same filesystem) which is
    then ``os.replace``d over the final name, so readers never observe a
    half-written ticket and re-generation is idempotent.
    """
    directory = Path(get_settings().ticket_storage_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{order_id}.pdf"
    tmp = directory / f"{order_id}.pdf.tmp"
    with open(tmp, "wb") as fh:
        fh.write(pdf_bytes)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final


async def mark_ticket_generated(order_id: uuid.UUID, path: Path) -> None:
    """Persist ticket_file/ticket_generated_at in one short, own transaction.

    A dedicated session (not the caller's) is used because generation can run
    after the request transaction has already committed.
    """
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        if order is None:
            logger.warning("ticket_order_missing", order_id=str(order_id))
            return
        order.ticket_file = str(path)
        order.ticket_generated_at = utcnow()
        await session.commit()


async def load_ticket_context(
    order_id: uuid.UUID,
) -> tuple[Order | None, list[OrderPassenger], str]:
    """Fresh-session load of the order (+passengers) and the agent company name.

    Returns ``(None, [], "")`` when the order no longer exists. A fresh session
    is used because rendering may happen after the creating request committed.
    """
    async with get_session_factory()() as session:
        result = await session.execute(
            select(Order).options(selectinload(Order.passengers)).where(Order.id == order_id)
        )
        fresh = result.scalar_one_or_none()
        if fresh is None:
            return None, [], ""
        agent = await session.get(Agent, fresh.agent_id)
        return fresh, list(fresh.passengers), agent.company_name if agent else ""


class TicketService:
    """Facade used by the orders API (see the contract in the docstring)."""

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    async def get_ticket_path(self, order: Order) -> Path | None:
        """Return the generated PDF path for an ISSUED order, else None."""
        if not order.ticket_file:
            return None
        path = Path(get_settings().ticket_storage_dir) / f"{order.id}.pdf"
        return path if path.is_file() else None

    async def request_generation(self, order: Order) -> bool:
        """Enqueue background PDF generation via the message bus.

        Returns True if enqueued, False when the broker is unavailable
        (caller may then fall back to generate_sync)."""
        bus = get_bus()  # shared process-wide instance — never closed here
        ok = await bus.publish_ticket_generation(str(order.id), order.ticket_number or "")
        if ok:
            logger.info("ticket_generation_enqueued", order_id=str(order.id))
        else:
            logger.warning("ticket_generation_enqueue_failed", order_id=str(order.id))
        return ok

    async def generate_sync(self, order: Order) -> Path | None:
        """Render and persist the PDF right away (fallback / tests)."""
        try:
            fresh_order, passengers, agent_company = await load_ticket_context(order.id)
            if fresh_order is None:
                logger.error("ticket_sync_failed", order_id=str(order.id), reason="order_missing")
                return None
            data = build_ticket_data(fresh_order, passengers, agent_company)
            path = write_pdf_atomic(fresh_order.id, render_ticket_pdf(data))
            await mark_ticket_generated(fresh_order.id, path)
            logger.info(
                "ticket_generated_sync",
                order_id=str(fresh_order.id),
                ticket_number=data.ticket_number,
                path=str(path),
            )
            return path
        except Exception as exc:
            logger.error("ticket_sync_failed", order_id=str(order.id), exc_info=exc)
            return None


__all__ = [
    "TicketService",
    "build_ticket_data",
    "load_ticket_context",
    "mark_ticket_generated",
    "write_pdf_atomic",
]
