"""Seed demo data for MANUAL API testing (add --force to wipe first).

Creates (idempotently):
  - agent        / agent123      balance 10 000 (main demo agent, seeded on boot)
  - poor-agent   / poor123       balance 50    (issue -> 402 INSUFFICIENT_FUNDS)
  - disabled-agent / disabled123 is_active=False (login -> 401)
  - orders in every meaningful state, with realistic offer snapshots from the
    mock provider, ledger entries and status history

Run:  docker compose exec api python -m scripts.seed_demo [--force]
"""

import asyncio
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, text

from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import get_session_factory
from app.domain.entities import Offer, SearchParams
from app.domain.enums import DocumentType, Gender, OrderStatus, PassengerType, TransactionType
from app.models import Agent, BalanceTransaction, Order, OrderPassenger, OrderStatusHistory
from app.providers.mock import MockAviaProvider

log = get_logger("seed_demo")

_CENT = Decimal("0.01")
_PAX_FARE = {
    PassengerType.ADULT: Decimal("1.00"),
    PassengerType.CHILD: Decimal("0.75"),
    PassengerType.INFANT: Decimal("0.10"),
}


async def _ensure_agent(session, username, password, balance, *, active=True) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.username == username))
    ).scalar_one_or_none()
    if agent is not None:
        return agent
    agent = Agent(
        username=username,
        email=f"{username}@demo.ioka.uz",
        password_hash=hash_password(password),
        company_name=f"{username.upper()} Travel",
        balance=Decimal(balance),
        is_active=active,
    )
    session.add(agent)
    await session.flush()
    return agent


def _passenger(order_id, idx: int, pax_type: PassengerType) -> OrderPassenger:
    return OrderPassenger(
        order_id=order_id,
        pax_type=pax_type,
        first_name="IVAN" if idx % 2 == 0 else "MARIA",
        last_name=f"IVANOV{idx}",
        date_of_birth=date(1985 + idx, 3 + idx, 10),
        gender=Gender.MALE if idx % 2 == 0 else Gender.FEMALE,
        citizenship="UZ",
        doc_type=DocumentType.PASSPORT,
        doc_number=f"AB{1234560 + idx}",
    )


async def _make_order(
    session,
    agent: Agent,
    offer: Offer,
    *,
    status: OrderStatus,
    pax_types: list[PassengerType],
) -> Order:
    """Order with passengers, history and (for ISSUED) ledger + ticket number."""
    fares = [
        (offer.price * _PAX_FARE[t]).quantize(_CENT, rounding=ROUND_HALF_UP) for t in pax_types
    ]
    total = sum(fares, Decimal("0.00"))
    order = Order(
        agent_id=agent.id,
        offer_id=offer.offer_id,
        status=OrderStatus.BOOKED,
        total_amount=total,
        currency=offer.currency,
        offer_snapshot=offer.model_dump(mode="json"),
        contact={"email": f"{agent.username}@demo.ioka.uz", "phone": "+998901234567"},
    )
    session.add(order)
    await session.flush()
    for i, (t, fare) in enumerate(zip(pax_types, fares, strict=True)):
        pax = _passenger(order.id, i, t)
        pax.fare_amount = fare
        session.add(pax)
    session.add(
        OrderStatusHistory(
            order_id=order.id, from_status=None, to_status=OrderStatus.BOOKED, actor=agent.username
        )
    )
    if status == OrderStatus.ISSUED:
        agent.balance = (agent.balance - total).quantize(_CENT)
        order.status = OrderStatus.ISSUED
        order.issued_at = utcnow()
        order.ticket_number = f"232{order.id.int % 10**10:010d}"
        session.add(
            BalanceTransaction(
                agent_id=agent.id,
                order_id=order.id,
                txn_type=TransactionType.DEBIT,
                amount=total,
                balance_after=agent.balance,
                note=f"Issue order {order.id} (demo seed)",
            )
        )
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=OrderStatus.BOOKED,
                to_status=OrderStatus.ISSUED,
                actor=agent.username,
            )
        )
    return order


async def seed(force: bool = False) -> None:
    if force:
        async with get_session_factory()() as session:
            await session.execute(
                text(
                    "TRUNCATE order_status_history, order_passengers, orders, "
                    "balance_transactions, idempotency_keys, search_sessions, "
                    "provider_call_logs RESTART IDENTITY CASCADE"
                )
            )
            await session.execute(text("DELETE FROM agents WHERE username NOT IN ('agent')"))
            await session.execute(
                text("UPDATE agents SET balance = 10000.00 WHERE username = 'agent'")
            )
            await session.commit()
        log.info("seed_force_wiped", tables="orders/ledger/idempotency/search/audit")

    provider = MockAviaProvider(latency_ms_range=(0, 0))

    async def offers(origin: str, dest: str) -> list[Offer]:
        return await provider.search_offers(
            SearchParams(origin=origin, destination=dest, departure_date=date(2026, 12, 15))
        )

    tas_ist = await offers("TAS", "IST")
    tas_dxb = await offers("TAS", "DXB")
    ala_ist = await offers("ALA", "IST")

    factory = get_session_factory()
    async with factory() as session:
        main = await _ensure_agent(session, "agent", "agent123", "10000.00")
        poor = await _ensure_agent(session, "poor-agent", "poor123", "50.00")
        await _ensure_agent(session, "disabled-agent", "disabled123", "1000.00", active=False)

        already = (await session.execute(select(func.count()).select_from(Order))).scalar()
        if already:
            log.info("seed_skipped", orders_already=already)
            return

        # BOOKED for main agent: TAS->IST, ADULT + CHILD
        booked1 = await _make_order(
            session,
            main,
            tas_ist[0],
            status=OrderStatus.BOOKED,
            pax_types=[PassengerType.ADULT, PassengerType.CHILD],
        )
        # ISSUED for main agent: ALA->IST, single ADULT (balance + ledger updated)
        issued1 = await _make_order(
            session,
            main,
            ala_ist[1],
            status=OrderStatus.ISSUED,
            pax_types=[PassengerType.ADULT],
        )
        # BOOKED for poor agent: TAS->DXB (issue -> 402 demo)
        poor1 = await _make_order(
            session,
            poor,
            tas_dxb[0],
            status=OrderStatus.BOOKED,
            pax_types=[PassengerType.ADULT],
        )
        # one more BOOKED for main agent: TAS->DXB, ADULT + INFANT
        booked2 = await _make_order(
            session,
            main,
            tas_dxb[1],
            status=OrderStatus.BOOKED,
            pax_types=[PassengerType.ADULT, PassengerType.INFANT],
        )
        await session.commit()

        def info(tag: str, order: Order) -> str:
            return (
                f"{tag}: id={order.id} status={order.status.value} "
                f"total={order.total_amount} {order.currency} "
                f"{order.offer_snapshot.get('validating_carrier')} "
                f"{order.offer_snapshot.get('origin')}->{order.offer_snapshot.get('destination')}"
            )

        log.info(info("order BOOKED  (issue me)", booked1))
        log.info(info("order ISSUED  (ticket ready)", issued1))
        log.info(info("order POOR    (issue -> 402)", poor1))
        log.info(info("order BOOKED#2", booked2))
        log.info(
            "agent_balances",
            agent=f"{main.balance:.2f}",
            poor=f"{poor.balance:.2f}",
        )


if __name__ == "__main__":
    configure_logging("INFO")
    asyncio.run(seed(force="--force" in sys.argv))
