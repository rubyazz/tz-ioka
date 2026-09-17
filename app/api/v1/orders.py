"""Orders endpoints: booking, issuing, status history, PDF ticket download."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from app.api.deps import CurrentAgent, SessionDep, SettingsDep
from app.core.errors import AuthorizationError, ConflictError, NotFoundError
from app.domain.enums import OrderStatus
from app.models import Order
from app.repositories.order import OrderRepository
from app.schemas.orders import BookingCreateIn, IssueOut, OrderOut, OrderStatusOut
from app.services.booking import BookingService
from app.services.issuing import IssuingService
from app.services.ticket import TicketService

router = APIRouter(prefix="/avia/orders", tags=["orders"])

IdempotencyKeyHeader = Annotated[
    Annotated[
        str | None,
        Field(max_length=128, description="Client-generated key for safe retries"),
    ],
    Header(alias="Idempotency-Key"),
]


def _owned_order(order: Order | None, agent_id: uuid.UUID) -> Order:
    """Shared 404 / ownership guard for single-order read paths."""
    if order is None:
        raise NotFoundError("Order not found")
    if order.agent_id != agent_id:
        raise AuthorizationError("Order belongs to another agent")
    return order


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrderOut)
async def create_order(
    body: BookingCreateIn,
    agent: CurrentAgent,
    session: SessionDep,
    settings: SettingsDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> OrderOut | JSONResponse:
    """Book the given offer: price it per passenger and persist as BOOKED.

    With an ``Idempotency-Key`` header, retries replay the original 201 body
    plus an ``Idempotency-Replayed: true`` header instead of double-booking.
    """
    order, replay = await BookingService(session, settings).create_order(
        agent, body, idempotency_key
    )
    if replay is not None:
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=replay,
            headers={"Idempotency-Replayed": "true"},
        )
    if order is None:  # pragma: no cover — service returns order unless replaying
        raise ConflictError("Booking service returned no order")
    return OrderOut.from_order(order)


@router.post("/{order_id}/issue", response_model=IssueOut)
async def issue_order(
    order_id: uuid.UUID,
    agent: CurrentAgent,
    session: SessionDep,
    settings: SettingsDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> IssueOut | JSONResponse:
    """Issue the order: atomic balance debit, ticket number, PDF hand-off."""
    issue_out, replay = await IssuingService(session, settings).issue(
        agent, order_id, idempotency_key
    )
    if replay is not None:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=replay,
            headers={"Idempotency-Replayed": "true"},
        )
    if issue_out is None:  # pragma: no cover — service returns result unless replaying
        raise ConflictError("Issuing service returned no result")
    return issue_out


@router.get("/{order_id}", response_model=OrderStatusOut)
async def get_order(
    order_id: uuid.UUID,
    agent: CurrentAgent,
    session: SessionDep,
) -> OrderStatusOut:
    """Full order view including the ordered status history."""
    order = _owned_order(await OrderRepository(session).get_by_id(order_id), agent.id)
    return OrderStatusOut.from_order(order)


@router.get("/{order_id}/ticket", response_model=None)
async def get_ticket(
    order_id: uuid.UUID,
    agent: CurrentAgent,
    session: SessionDep,
) -> FileResponse | JSONResponse:
    """Download the generated PDF ticket, or trigger generation (202) if absent."""
    order = _owned_order(await OrderRepository(session).get_by_id(order_id), agent.id)
    if order.status != OrderStatus.ISSUED:
        raise ConflictError(
            code="TICKET_NOT_READY",
            message="Ticket is issued only after order is ISSUED",
        )
    tickets = TicketService(session)
    path = await tickets.get_ticket_path(order)
    if path is not None:
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"ticket_{order.ticket_number or order.id}.pdf",
        )
    await tickets.request_generation(order)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "generating", "detail": "PDF is being generated, retry shortly"},
        headers={"Retry-After": "2"},
    )
