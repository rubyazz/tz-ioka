"""Orders, passengers, status history and idempotency keys."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.domain.enums import (
    DocumentType,
    Gender,
    IdempotencyStatus,
    OrderStatus,
    PassengerType,
)


class Order(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_amount > 0", name="positive_total_amount"),
        Index("ix_orders_agent_id_status", "agent_id", "status"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True
    )
    search_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("search_sessions.id"), nullable=True
    )
    offer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[OrderStatus] = mapped_column(
        nullable=False, default=OrderStatus.BOOKED, index=True
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # Immutable snapshots of what was actually sold
    offer_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contact: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    ticket_number: Mapped[str | None] = mapped_column(String(16), unique=True)
    ticket_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    passengers: Mapped[list["OrderPassenger"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderPassenger.created_at",
    )
    history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderPassenger(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "order_passengers"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    pax_type: Mapped[PassengerType] = mapped_column(nullable=False)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str] = mapped_column(String(64), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[Gender | None] = mapped_column(nullable=True)
    citizenship: Mapped[str | None] = mapped_column(String(3), nullable=True)
    doc_type: Mapped[DocumentType] = mapped_column(nullable=False, default=DocumentType.PASSPORT)
    doc_number: Mapped[str] = mapped_column(String(32), nullable=False)
    fare_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    order: Mapped[Order] = relationship(back_populates="passengers")


class OrderStatusHistory(Base, UUIDPrimaryKeyMixin):
    """Audit trail of every status change."""

    __tablename__ = "order_status_history"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[OrderStatus | None] = mapped_column(nullable=True)
    to_status: Mapped[OrderStatus] = mapped_column(nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="history")


class IdempotencyKey(Base, UUIDPrimaryKeyMixin):
    """Stored requests keyed by (agent, scope, key) for replay."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("agent_id", "scope", "key", name="uq_idempotency_agent_scope_key"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)  # create_order | issue
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[IdempotencyStatus] = mapped_column(
        nullable=False, default=IdempotencyStatus.LOCKED
    )
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
