"""FastAPI application factory and entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import bind_request_context, configure_logging, get_logger, new_request_id
from app.db.session import dispose_engine, get_session_factory

log = get_logger("app")


async def seed_demo_data() -> None:
    """Create the demo agent (username/password from settings) if missing."""
    from app.core.security import hash_password
    from app.models import Agent
    from scripts.seed import DEMO_COMPANY

    settings = get_settings()
    if not settings.seed_demo_data:
        return
    async with get_session_factory()() as session:
        existing = (
            await session.execute(
                select(Agent).where(Agent.username == settings.demo_agent_username)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(
            Agent(
                username=settings.demo_agent_username,
                email=f"{settings.demo_agent_username}@demo.ioka.uz",
                password_hash=hash_password(settings.demo_agent_password),
                company_name=DEMO_COMPANY,
                balance=settings.demo_agent_balance,
            )
        )
        await session.commit()
    log.info("demo_agent_seeded", username=settings.demo_agent_username)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_logs=settings.is_production or settings.is_test,
    )
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await seed_demo_data()
    except Exception as exc:  # DB not ready yet — app still boots, /health/ready shows it
        log.error("demo_seed_failed", error=str(exc))
    log.info("api_started", env=settings.app_env, version=settings.app_version)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await dispose_engine()
        log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        description=(
            "B2B avia booking service: agent auth (JWT), IATA locations lookup, "
            "async flight search with polling, offer details, order creation, "
            "atomic balance debit on issuing, PDF tickets."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        bind_request_context(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        log.warning("app_error", code=exc.code, status=exc.status_code, detail=exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.to_payload()},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("unhandled_error")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        )

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health/live", tags=["health"])
    async def live() -> dict:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> JSONResponse:
        checks: dict[str, bool] = {}
        try:
            async with get_session_factory()() as session:
                await session.execute(text("SELECT 1"))
            checks["postgres"] = True
        except Exception:
            checks["postgres"] = False
        try:
            await request.app.state.redis.ping()
            checks["redis"] = True
        except Exception:
            checks["redis"] = False
        ok = all(checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "degraded", "checks": checks},
        )

    return app


app = create_app()
