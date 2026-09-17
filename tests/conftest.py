"""Shared fixtures: test DB (real commits), app, client.

Design: tests run against a real PostgreSQL test database with REAL commits
(no savepoint tricks) so row locking and transaction semantics are exercised
exactly as in production. Every test gets a fresh schema and fresh engines —
pytest-asyncio gives each test its own event loop, and pooled connections
must never cross loops. RABBITMQ_URL points to an unreachable host, which
makes ``publish_ticket_generation`` return False and the issuing flow fall
back to synchronous PDF generation — ticket endpoints become deterministic.
"""

import os

# Test environment must be pinned BEFORE any app import.
# DATABASE_URL / REDIS_URL honor externally provided test values (CI runs
# services on localhost); everything else is forced so that a compose-defined
# environment can not leak the real broker or production-ish settings in.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://ioka:ioka@postgres:5432/ioka_travel_test"
)
os.environ.setdefault("REDIS_URL", "redis://redis:6379/1")
os.environ["APP_ENV"] = "test"
os.environ["MOCK_LATENCY_MIN_MS"] = "0"
os.environ["MOCK_LATENCY_MAX_MS"] = "0"
os.environ["SEED_DEMO_DATA"] = "false"
# Unreachable broker on purpose: publish_ticket_generation returns False and
# issuing falls back to synchronous PDF generation (deterministic tickets).
os.environ["RABBITMQ_URL"] = "amqp://unreachable.invalid:5672/%2F"

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 — register models on Base.metadata
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.main import create_app
from app.models import Agent

_engine: AsyncEngine | None = None


async def get_test_engine() -> AsyncEngine:
    """Per-test engine; schema is dropped and recreated on every build."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    return _engine


async def _reset_shared_state() -> None:
    """Dispose everything that holds a pooled connection bound to this loop.

    The app module keeps lru-cached singletons (engine/factory in
    app.db.session, redis client in app.cache) — without this reset the next
    test (new event loop) would reuse dead connections.
    """
    global _engine
    from app.cache import get_cache
    from app.db import session as db_session_module

    get_cache.cache_clear()
    await db_session_module.dispose_engine()
    if _engine is not None:
        await _engine.dispose()
        _engine = None

    client = aioredis.from_url(os.environ["REDIS_URL"])
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture(autouse=True)
async def isolated_environment() -> AsyncIterator[None]:
    """Fresh schema, fresh engines, flushed Redis around every test."""
    yield
    await _reset_shared_state()


@pytest.fixture
async def factory() -> async_sessionmaker[AsyncSession]:
    engine = await get_test_engine()
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
async def agent(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Agent]:
    """A fresh active agent with balance 1000.00 USD and password 'pass1234'."""
    async with factory() as session:
        instance = Agent(
            username=f"agent-{uuid.uuid4().hex[:10]}",
            email=f"{uuid.uuid4().hex[:10]}@test.uz",
            password_hash=hash_password("pass1234"),
            company_name="Test Agency",
            balance=Decimal("1000.00"),
        )
        session.add(instance)
        await session.commit()
        yield instance


@pytest.fixture
def auth_headers(agent: Agent) -> dict[str, str]:
    token, _ = create_access_token(agent.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def settings():
    from app.core.config import get_settings

    return get_settings()


@pytest.fixture
async def client(agent: Agent, auth_headers: dict[str, str]) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the real app (ASGI, no network)."""
    application = create_app()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as ac:
        yield ac
