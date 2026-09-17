"""IssuingService: atomic debit, insufficient funds, concurrency, idempotency.

Runs with REAL commits against the test PostgreSQL, separate sessions per
concurrent participant — the row locks do the coordination, exactly as in
production.
"""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import InsufficientFundsError
from app.domain.enums import OrderStatus, TransactionType
from app.models import Agent, BalanceTransaction, Order, OrderPassenger
from app.repositories.agent import AgentRepository
from app.services.issuing import IssuingService

_SNAPSHOT = {
    "offer_id": "of_test00000001",
    "validating_carrier": "HY",
    "price": "300.00",
    "currency": "USD",
    "segments": [
        {
            "origin": "TAS",
            "destination": "IST",
            "flight_number": "HY-281",
            "carrier_code": "HY",
            "departure": "2026-10-01T08:20:00+00:00",
            "arrival": "2026-10-01T11:40:00+00:00",
            "duration_minutes": 200,
        }
    ],
}


async def _make_order(
    factory: async_sessionmaker[AsyncSession],
    agent_id: uuid.UUID,
    total: Decimal = Decimal("300.00"),
) -> uuid.UUID:
    async with factory() as session:
        order = Order(
            agent_id=agent_id,
            offer_id=_SNAPSHOT["offer_id"],
            status=OrderStatus.BOOKED,
            total_amount=total,
            currency="USD",
            offer_snapshot=_SNAPSHOT,
            contact={"email": "t@t.uz"},
        )
        order.passengers = [
            OrderPassenger(
                pax_type="ADULT",
                first_name="IVAN",
                last_name="IVANOV",
                date_of_birth=date(1990, 5, 1),
                doc_type="PASSPORT",
                doc_number="AB123456",
            )
        ]
        session.add(order)
        await session.commit()
        return order.id


async def _balance(factory, agent_id: uuid.UUID) -> Decimal:
    async with factory() as session:
        row = await AgentRepository(session).get_by_id(agent_id)
        assert row is not None
        return row.balance


async def _debits(factory, order_id: uuid.UUID) -> list[BalanceTransaction]:
    async with factory() as session:
        stmt = select(BalanceTransaction).where(
            BalanceTransaction.order_id == order_id,
            BalanceTransaction.txn_type == TransactionType.DEBIT,
        )
        return list((await session.execute(stmt)).scalars())


async def _issue(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    agent_id: uuid.UUID,
    order_id: uuid.UUID,
    key: str | None = None,
):
    async with factory() as session:
        agent = await AgentRepository(session).get_by_id(agent_id)
        result, replay = await IssuingService(session, settings).issue(agent, order_id, key)
        return result, replay


async def test_happy_path_debits_atomically(
    factory: async_sessionmaker[AsyncSession], agent: Agent, settings: Settings
) -> None:
    order_id = await _make_order(factory, agent.id)
    result, replay = await _issue(factory, settings, agent.id, order_id)

    assert replay is None
    assert result is not None and result.status == OrderStatus.ISSUED
    assert result.debited_amount == "300.00"
    assert result.balance_after == "700.00"
    assert result.ticket_number.startswith("232")
    assert len(result.ticket_number) == 13

    txns = await _debits(factory, order_id)
    assert len(txns) == 1
    assert txns[0].amount == Decimal("300.00")
    assert txns[0].balance_after == Decimal("700.00")
    assert await _balance(factory, agent.id) == Decimal("700.00")


async def test_insufficient_funds_leaves_order_booked(
    factory: async_sessionmaker[AsyncSession], agent: Agent, settings: Settings
) -> None:
    order_id = await _make_order(factory, agent.id, total=Decimal("1500.00"))
    with pytest.raises(InsufficientFundsError) as err:
        await _issue(factory, settings, agent.id, order_id)

    assert err.value.details == {"order_amount": "1500.00", "balance": "1000.00"}

    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order.status == OrderStatus.BOOKED
    assert await _debits(factory, order_id) == []
    assert await _balance(factory, agent.id) == Decimal("1000.00")


async def test_reissue_after_success_is_idempotent(
    factory: async_sessionmaker[AsyncSession], agent: Agent, settings: Settings
) -> None:
    order_id = await _make_order(factory, agent.id)
    first, _ = await _issue(factory, settings, agent.id, order_id)
    second, _ = await _issue(factory, settings, agent.id, order_id)

    assert second.ticket_number == first.ticket_number
    assert len(await _debits(factory, order_id)) == 1
    assert await _balance(factory, agent.id) == Decimal("700.00")


async def test_concurrent_issue_single_debit(
    factory: async_sessionmaker[AsyncSession], agent: Agent, settings: Settings
) -> None:
    """Two sessions issue the same order simultaneously: one debit total.

    The agent row is locked first, so the second transaction serializes on it
    and then finds the order already ISSUED (idempotent success).
    """
    order_id = await _make_order(factory, agent.id)

    outcomes = await asyncio.gather(
        _issue(factory, settings, agent.id, order_id),
        _issue(factory, settings, agent.id, order_id),
        return_exceptions=True,
    )

    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome
        result, replay = outcome
        assert replay is None
        assert result.status == OrderStatus.ISSUED

    tickets = {outcome[0].ticket_number for outcome in outcomes if outcome[0] is not None}
    assert len(tickets) == 1
    assert len(await _debits(factory, order_id)) == 1
    assert await _balance(factory, agent.id) == Decimal("700.00")


async def test_issue_with_idempotency_key_replays_stored_response(
    factory: async_sessionmaker[AsyncSession], agent: Agent, settings: Settings
) -> None:
    order_id = await _make_order(factory, agent.id)
    first, replay = await _issue(factory, settings, agent.id, order_id, key="issue-key")
    assert replay is None and first is not None

    second, replay = await _issue(factory, settings, agent.id, order_id, key="issue-key")
    assert second is None
    assert replay["ticket_number"] == first.ticket_number
    assert replay["status"] == "ISSUED"
    assert len(await _debits(factory, order_id)) == 1
