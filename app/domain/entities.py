"""Domain entities (framework-free DTOs shared between layers).

These are pydantic models on purpose: the whole service is serialization-heavy
(external provider payloads, Redis caches, JSONB snapshots) and pydantic v2
gives validation + fast JSON for free. Domain logic itself never touches
FastAPI/SQLAlchemy — see ``app/domain/ports.py`` for the boundaries.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _Entity(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- Locations ---


class Location(_Entity):
    code: str = Field(min_length=3, max_length=3, description="IATA code")
    name: str
    city: str
    country: str
    country_code: str | None = None
    type: str = "AIRPORT"  # LocationType


# --- Search / offers ---


class PassengerQuery(_Entity):
    """Passenger requested in a search."""

    type: str = "ADULT"  # PassengerType


class SearchParams(_Entity):
    origin: str
    destination: str
    departure_date: date
    return_date: date | None = None
    passengers: list[PassengerQuery] = Field(default_factory=lambda: [PassengerQuery()])
    service_class: str = "ECONOMY"  # ServiceClass


class BaggageAllowance(_Entity):
    kg: int | None = None
    pieces: int | None = None


class Baggage(_Entity):
    cabin: BaggageAllowance | None = None
    checked: BaggageAllowance | None = None


class FlightSegment(_Entity):
    origin: str
    destination: str
    flight_number: str
    carrier_code: str
    carrier_name: str | None = None
    departure: datetime
    arrival: datetime
    duration_minutes: int
    aircraft: str | None = None


class Offer(_Entity):
    """Compact offer returned by search polling."""

    offer_id: str
    origin: str
    destination: str
    validating_carrier: str
    price: Decimal
    currency: str = "USD"
    service_class: str = "ECONOMY"
    refundable: bool = False
    seats_left: int = 1
    segments: list[FlightSegment] = Field(default_factory=list)
    baggage: Baggage | None = None  # top-level (cheapest fare) allowance


class FareFamily(_Entity):
    name: str
    service_class: str = "ECONOMY"
    price: Decimal | None = None  # vs base fare
    refundable: bool = False
    exchangeable: bool = False
    baggage: Baggage | None = None
    seats_left: int | None = None
    fare_rules: list[str] = Field(default_factory=list)


class OfferDetail(Offer):
    """Offer + fare families and detailed baggage info (GET /offers/{id})."""

    fare_families: list[FareFamily] = Field(default_factory=list)
    fare_rules: list[str] = Field(default_factory=list)
    offer_expires_at: datetime | None = None


# --- Orders ---


class ContactInfo(_Entity):
    email: str
    phone: str | None = None


class PassengerInput(_Entity):
    """Passenger data submitted at booking time."""

    type: str = "ADULT"
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)
    date_of_birth: date
    gender: str | None = None
    citizenship: str | None = Field(default=None, max_length=3)
    doc_type: str = "PASSPORT"
    doc_number: str = Field(min_length=4, max_length=32)


# --- Provider call audit ---


class ProviderCallRecord(_Entity):
    provider: str
    operation: str  # locations | search | offer_detail
    request: dict | None = None
    response: dict | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    success: bool = True
    error: str | None = None


# --- Search session snapshot stored in Redis / PG ---


class SearchSessionSnapshot(BaseModel):
    """State of a search session as cached in Redis (mutable in place)."""

    model_config = ConfigDict(frozen=False)

    search_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    status: str  # SearchStatus
    params: SearchParams
    offers: list[dict] = Field(default_factory=list)  # Offer.model_dump()
    items_found: int = 0
    error: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
