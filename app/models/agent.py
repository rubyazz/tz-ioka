"""Agent accounts and their balance ledger."""

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.domain.enums import TransactionType


class Agent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agents"

    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    company_name: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), server_default="0", nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    transactions: Mapped[list["BalanceTransaction"]] = relationship(
        back_populates="agent",
        cascade="save-update, merge",
        passive_deletes=True,
    )


class BalanceTransaction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Immutable ledger entry. The agent balance is only ever changed together
    with an insert of the corresponding row here (see issuing service)."""

    __tablename__ = "balance_transactions"
    __table_args__ = (
        # One DEBIT per order — DB-level guarantee of idempotent issuing
        UniqueConstraint("order_id", "txn_type", name="uq_balance_txns_order_type"),
        CheckConstraint("amount > 0", name="positive_amount"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    txn_type: Mapped[TransactionType] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    note: Mapped[str] = mapped_column(String(255), default="")

    agent: Mapped[Agent] = relationship(back_populates="transactions")
