"""IdempotencyRepository claim/complete/fail semantics (real PostgreSQL)."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, IdempotencyConflictError
from app.core.security import hash_password
from app.domain.enums import IdempotencyStatus
from app.models import Agent
from app.repositories.idempotency import IdempotencyRepository


async def _make_agent(factory: async_sessionmaker[AsyncSession]) -> Agent:
    async with factory() as session:
        instance = Agent(
            username=f"agent-{uuid.uuid4().hex[:10]}",
            email=f"{uuid.uuid4().hex[:10]}@test.uz",
            password_hash=hash_password("pass1234"),
            balance=Decimal("100.00"),
        )
        session.add(instance)
        await session.commit()
        return instance


async def _claim(repo: IdempotencyRepository, key: str, agent_id: uuid.UUID) -> tuple:
    return await repo.claim(agent_id, "create_order", key, "hash-1")


async def test_first_claim_creates_locked_row(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    async with factory() as session:
        repo = IdempotencyRepository(session)
        row, created = await _claim(repo, "k1", agent.id)
        assert created is True
        assert row.status == IdempotencyStatus.LOCKED


async def test_second_claim_while_locked_conflicts(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    async with factory() as s1:
        await _claim(IdempotencyRepository(s1), "k2", agent.id)
        await s1.commit()
    async with factory() as s2:
        with pytest.raises(IdempotencyConflictError):
            await _claim(IdempotencyRepository(s2), "k2", agent.id)


async def test_completed_same_hash_replays(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    async with factory() as session:
        repo = IdempotencyRepository(session)
        row, _ = await _claim(repo, "k3", agent.id)
        await repo.complete(row.id, 201, {"id": "order-1"})
        row2, created2 = await _claim(repo, "k3", agent.id)
        assert created2 is False
        assert row2.response_body == {"id": "order-1"}
        assert row2.response_status_code == 201


async def test_completed_different_hash_reused_key_rejected(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    async with factory() as session:
        repo = IdempotencyRepository(session)
        row, _ = await _claim(repo, "k4", agent.id)
        await repo.complete(row.id, 201, {})
        with pytest.raises(ConflictError) as err:
            await repo.claim(agent.id, "create_order", "k4", "hash-DIFFERENT")
        assert err.value.code == "IDEMPOTENCY_KEY_REUSED"


async def test_failed_key_is_reclaimable(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    async with factory() as session:
        repo = IdempotencyRepository(session)
        row, _ = await _claim(repo, "k5", agent.id)
        await repo.fail(row.id)
        row2, created = await _claim(repo, "k5", agent.id)
        assert created is True
        assert row2.status == IdempotencyStatus.LOCKED


async def test_scopes_are_independent(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    async with factory() as session:
        repo = IdempotencyRepository(session)
        row, _ = await _claim(repo, "k6", agent.id)
        await repo.complete(row.id, 200, {"scope": "create_order"})
        row2, created = await repo.claim(agent.id, "issue", "k6", "hash-1")
        assert created is True  # different scope — fresh slot
        assert row2.status == IdempotencyStatus.LOCKED


async def test_agents_are_independent(
    factory: async_sessionmaker[AsyncSession], agent: Agent
) -> None:
    other = await _make_agent(factory)
    async with factory() as session:
        repo = IdempotencyRepository(session)
        row, _ = await _claim(repo, "k7", agent.id)
        await repo.complete(row.id, 200, {})
        row2, created = await _claim(repo, "k7", other.id)
        assert created is True  # different agent — fresh slot
        assert row2.status == IdempotencyStatus.LOCKED
