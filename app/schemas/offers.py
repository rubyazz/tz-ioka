"""Request/response schemas for the offers API.

Money is always serialized as a string (``Decimal -> str`` via
``field_serializer``) so amounts can never lose precision as floats.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.domain.enums import PassengerType, ServiceClass


class PassengerTypeIn(BaseModel):
    type: PassengerType = PassengerType.ADULT


class SearchCreateIn(BaseModel):
    """Body of POST /avia/offers."""

    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    departure_date: date
    return_date: date | None = None
    passengers: list[PassengerTypeIn] = Field(
        default_factory=lambda: [PassengerTypeIn()], min_length=1
    )
    service_class: ServiceClass = ServiceClass.ECONOMY

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def _uppercase_code(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class BaggageAllowanceOut(BaseModel):
    kg: int | None = None
    pieces: int | None = None


class BaggageOut(BaseModel):
    cabin: BaggageAllowanceOut | None = None
    checked: BaggageAllowanceOut | None = None


class SegmentOut(BaseModel):
    origin: str
    destination: str
    flight_number: str
    carrier_code: str
    carrier_name: str | None = None
    departure: datetime
    arrival: datetime
    duration_minutes: int
    aircraft: str | None = None


class OfferOut(BaseModel):
    """Compact offer as returned inside search status polling."""

    model_config = ConfigDict(from_attributes=True)

    offer_id: str
    origin: str
    destination: str
    validating_carrier: str
    price: Decimal
    currency: str = "USD"
    service_class: str = "ECONOMY"
    refundable: bool = False
    seats_left: int = 1
    segments: list[SegmentOut] = Field(default_factory=list)
    baggage: BaggageOut | None = None

    @field_serializer("price")
    def _price_as_string(self, value: Decimal) -> str:
        return format(value, "f")


class SearchStartedOut(BaseModel):
    search_id: UUID
    status: str
    expires_at: datetime


class SearchStatusOut(BaseModel):
    search_id: UUID
    status: str
    items: list[OfferOut] = Field(default_factory=list)
    items_found: int = 0
    expires_at: datetime | None = None
    error: str | None = None


class FareFamilyOut(BaseModel):
    name: str
    service_class: str = "ECONOMY"
    price: Decimal | None = None
    refundable: bool = False
    exchangeable: bool = False
    baggage: BaggageOut | None = None
    seats_left: int | None = None
    fare_rules: list[str] = Field(default_factory=list)

    @field_serializer("price")
    def _price_as_string(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")


class OfferDetailOut(OfferOut):
    """Full offer: fare families, rules and expiry (GET /offers/{offer_id})."""

    fare_families: list[FareFamilyOut] = Field(default_factory=list)
    fare_rules: list[str] = Field(default_factory=list)
    offer_expires_at: datetime | None = None
