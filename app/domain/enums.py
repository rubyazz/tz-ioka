"""Domain enums and the order state machine."""

from enum import StrEnum


class SearchStatus(StrEnum):
    """Lifecycle of an async flight search session."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


class OrderStatus(StrEnum):
    BOOKED = "BOOKED"  # reservation created, not paid/issued yet
    ISSUED = "ISSUED"  # ticket issued, balance debited — final success state
    CANCELLED = "CANCELLED"  # reservation cancelled before issuing
    FAILED = "FAILED"  # issuing failed terminally (provider/technical error)


#: Allowed order status transitions. Anything else raises InvalidStatusTransition.
ALLOWED_ORDER_TRANSITIONS: dict[str, set[str]] = {
    OrderStatus.BOOKED: {OrderStatus.ISSUED, OrderStatus.CANCELLED, OrderStatus.FAILED},
    OrderStatus.ISSUED: set(),  # terminal
    OrderStatus.CANCELLED: set(),  # terminal
    OrderStatus.FAILED: set(),  # terminal
}


class TransactionType(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
    REFUND = "REFUND"


class PassengerType(StrEnum):
    ADULT = "ADULT"
    CHILD = "CHILD"
    INFANT = "INFANT"


class Gender(StrEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"


class DocumentType(StrEnum):
    PASSPORT = "PASSPORT"
    ID_CARD = "ID_CARD"


class ServiceClass(StrEnum):
    ECONOMY = "ECONOMY"
    BUSINESS = "BUSINESS"


class LocationType(StrEnum):
    CITY = "CITY"
    AIRPORT = "AIRPORT"


class IdempotencyStatus(StrEnum):
    LOCKED = "LOCKED"  # first request is processing
    COMPLETED = "COMPLETED"  # response stored, replay it
    FAILED = "FAILED"


class InvalidStatusTransition(Exception):
    """Raised when an order transition violates the state machine."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal order status transition {current!r} -> {target!r}")
