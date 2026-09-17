"""Orders API schemas: booking input, order views, issuing result.

Every money field is serialized as a plain string (fixed 2 decimal places)
so no Decimal can ever leak into JSON as a float.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.domain.enums import DocumentType, Gender, OrderStatus, PassengerType
from app.models import Order, OrderPassenger


def money_str(value: Decimal | None) -> str | None:
    """Format a Decimal as a fixed 2-dp string (None-safe)."""
    return None if value is None else f"{value:.2f}"


# --- Requests ---


class PassengerIn(BaseModel):
    """One passenger submitted at booking time."""

    model_config = ConfigDict(str_strip_whitespace=True)

    type: PassengerType = PassengerType.ADULT
    first_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z]+(?:[ -][A-Za-z]+)*$")
    last_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z]+(?:[ -][A-Za-z]+)*$")
    date_of_birth: date
    gender: Gender | None = None
    citizenship: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}$")
    doc_type: DocumentType = DocumentType.PASSPORT
    doc_number: str = Field(min_length=4, max_length=32)

    @field_validator("date_of_birth")
    @classmethod
    def _dob_not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("date_of_birth cannot be in the future")
        return value


class ContactIn(BaseModel):
    email: EmailStr
    phone: str | None = None


class BookingCreateIn(BaseModel):
    """POST /avia/orders body."""

    offer_id: str = Field(min_length=1, max_length=64)
    passengers: list[PassengerIn] = Field(min_length=1, max_length=9)
    contact: ContactIn


# --- Responses ---


class PassengerOut(BaseModel):
    id: uuid.UUID
    type: PassengerType
    first_name: str
    last_name: str
    date_of_birth: date
    gender: Gender | None = None
    citizenship: str | None = None
    doc_type: DocumentType
    doc_number: str
    fare_amount: str | None = None

    @classmethod
    def from_passenger(cls, pax: OrderPassenger) -> "PassengerOut":
        return cls(
            id=pax.id,
            type=pax.pax_type,
            first_name=pax.first_name,
            last_name=pax.last_name,
            date_of_birth=pax.date_of_birth,
            gender=pax.gender,
            citizenship=pax.citizenship,
            doc_type=pax.doc_type,
            doc_number=pax.doc_number,
            fare_amount=money_str(pax.fare_amount),
        )


class RouteOut(BaseModel):
    """Condensed itinerary reconstructed from the stored offer snapshot."""

    origin: str
    destination: str
    validating_carrier: str
    departure: datetime
    arrival: datetime


class OrderOut(BaseModel):
    id: uuid.UUID
    status: OrderStatus
    offer_id: str
    total_amount: str
    currency: str
    passengers: list[PassengerOut]
    route: RouteOut | None = None  # None only for snapshots without segments
    ticket_number: str | None = None
    created_at: datetime
    issued_at: datetime | None = None

    @classmethod
    def from_order(cls, order: Order) -> "OrderOut":
        snapshot = order.offer_snapshot or {}
        segments = snapshot.get("segments") or []
        route: RouteOut | None = None
        if segments:
            first, last = segments[0], segments[-1]
            route = RouteOut(
                origin=first["origin"],
                destination=last["destination"],
                validating_carrier=snapshot.get("validating_carrier", ""),
                departure=first["departure"],
                arrival=last["arrival"],
            )
        return cls(
            id=order.id,
            status=order.status,
            offer_id=order.offer_id,
            total_amount=money_str(order.total_amount) or "0.00",
            currency=order.currency,
            passengers=[PassengerOut.from_passenger(pax) for pax in order.passengers],
            route=route,
            ticket_number=order.ticket_number,
            created_at=order.created_at,
            issued_at=order.issued_at,
        )


class HistoryEntryOut(BaseModel):
    from_status: OrderStatus | None = None
    to_status: OrderStatus
    reason: str | None = None
    actor: str
    created_at: datetime


class OrderStatusOut(OrderOut):
    """OrderOut plus the full (chronologically ordered) status history."""

    history: list[HistoryEntryOut]

    @classmethod
    def from_order(cls, order: Order) -> "OrderStatusOut":
        base = OrderOut.from_order(order)
        entries = [
            HistoryEntryOut(
                from_status=item.from_status,
                to_status=item.to_status,
                reason=item.reason,
                actor=item.actor,
                created_at=item.created_at,
            )
            for item in sorted(order.history, key=lambda h: h.created_at)
        ]
        return cls(**base.model_dump(), history=entries)


class IssueOut(BaseModel):
    """POST /avia/orders/{id}/issue response."""

    id: uuid.UUID
    status: OrderStatus
    ticket_number: str
    debited_amount: str
    balance_after: str
    currency: str
    issued_at: datetime
