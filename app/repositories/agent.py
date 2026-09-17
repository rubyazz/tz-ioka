"""Agent account repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, agent_id: uuid.UUID) -> Agent | None:
        return await self.session.get(Agent, agent_id)

    async def get_by_username(self, username: str) -> Agent | None:
        stmt = select(Agent).where(Agent.username == username)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, agent_id: uuid.UUID) -> Agent | None:
        """Lock the agent row (SELECT ... FOR UPDATE) for the duration of the
        current transaction — the basis of atomic balance debits.

        ``populate_existing`` refreshes attributes even if the row is already
        in the identity map (e.g. loaded by the auth dependency), so callers
        always see the locked-read balance."""
        stmt = (
            select(Agent)
            .where(Agent.id == agent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def save(self, agent: Agent) -> Agent:
        self.session.add(agent)
        await self.session.flush()
        return agent
