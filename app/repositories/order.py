"""Order persistence: create with cascades, eager reads, row locks."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Order


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, order: Order) -> Order:
        """Persist a new order; passengers and history cascade on flush."""
        self.session.add(order)
        await self.session.flush()
        return order

    async def get_by_id(self, order_id: uuid.UUID) -> Order | None:
        """Fetch with passengers and history eagerly loaded (read paths)."""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.passengers), selectinload(Order.history))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, order_id: uuid.UUID) -> Order | None:
        """SELECT ... FOR UPDATE — serializes concurrent issuing of one order."""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .options(selectinload(Order.passengers))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
