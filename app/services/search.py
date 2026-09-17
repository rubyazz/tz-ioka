"""Async flight-search orchestration.

Lifecycle: ``start_search`` validates params, persists a PENDING session,
caches its snapshot in Redis and kicks off a background provider call.
``get_search`` serves polls Redis-first and lazily fails sessions whose
worker died past the deadline. Offer details are cached per offer_id.
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthorizationError,
    NotFoundError,
    ProviderTimeoutError,
    ValidationAppError,
)
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.entities import (
    OfferDetail,
    ProviderCallRecord,
    SearchParams,
    SearchSessionSnapshot,
)
from app.domain.enums import SearchStatus
from app.domain.ports import AviaContentProvider, Cache
from app.models import Agent, SearchSession
from app.repositories.audit import ProviderAuditRecorder

log = get_logger("search")

#: Strong references keep fire-and-forget tasks from being garbage-collected.
_background_tasks: set[asyncio.Task[None]] = set()

_LAZY_FAIL_GRACE_SECONDS = 5


class SearchService:
    """Coordinates provider calls, Redis snapshots and search session rows."""

    def __init__(
        self,
        provider: AviaContentProvider,
        cache: Cache,
        audit: ProviderAuditRecorder | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._audit = audit or ProviderAuditRecorder()

    async def start_search(
        self, agent: Agent | None, params: SearchParams
    ) -> SearchSessionSnapshot:
        """Validate params, create the session and launch the background search."""
        params = await self._validated_params(params)
        settings = get_settings()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=settings.search_session_ttl_seconds)

        async with get_session_factory()() as session:
            row = SearchSession(
                agent_id=agent.id if agent is not None else None,
                params=params.model_dump(mode="json"),
                status=SearchStatus.PENDING,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()

        snapshot = SearchSessionSnapshot(
            search_id=row.id,
            agent_id=row.agent_id,
            status=SearchStatus.PENDING.value,
            params=params,
            offers=[],
            items_found=0,
            error=None,
            created_at=now,
            expires_at=expires_at,
        )
        await self._cache.set_json(
            f"search:session:{row.id}",
            snapshot.model_dump(mode="json"),
            settings.search_session_ttl_seconds,
        )
        task = asyncio.create_task(self._run_search(row.id, params, snapshot))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        log.info(
            "search_started",
            search_id=str(row.id),
            origin=params.origin,
            destination=params.destination,
            departure_date=params.departure_date.isoformat(),
        )
        return snapshot

    async def get_search(self, agent: Agent | None, search_id: uuid.UUID) -> SearchSessionSnapshot:
        """Return the current snapshot: Redis first, PostgreSQL on miss."""
        settings = get_settings()
        cached = await self._cache.get_json(f"search:session:{search_id}")
        snapshot = (
            SearchSessionSnapshot.model_validate(cached)
            if cached is not None
            else await self._load_snapshot_from_db(search_id)
        )

        if snapshot.status == SearchStatus.PENDING and self._is_stale(snapshot, settings):
            # The background worker died before finishing — fail lazily.
            snapshot.status = SearchStatus.FAILED.value
            snapshot.error = "deadline_exceeded"
            await self._store_snapshot(snapshot)
            await self._persist_result(
                snapshot.search_id, SearchStatus.FAILED, snapshot.items_found, snapshot.error
            )

        if snapshot.expires_at is not None and datetime.now(UTC) > snapshot.expires_at:
            raise NotFoundError("Search session has expired", code="SEARCH_EXPIRED")
        if snapshot.agent_id is not None and agent is not None and snapshot.agent_id != agent.id:
            raise AuthorizationError("This search session belongs to another agent")
        return snapshot

    async def get_offer_detail(self, offer_id: str) -> OfferDetail:
        """Offer details: Redis cache first, then the provider, then 404."""
        if not offer_id.startswith("of_"):
            raise NotFoundError("Offer not found")
        cache_key = f"offer:{offer_id}"
        cached = await self._cache.get_json(cache_key)
        if cached is not None:
            return OfferDetail.model_validate(cached)

        settings = get_settings()
        started = time.perf_counter()
        error: str | None = None
        detail: OfferDetail | None = None
        try:
            detail = await asyncio.wait_for(
                self._provider.get_offer_detail(offer_id),
                timeout=settings.provider_timeout_seconds,
            )
        except TimeoutError as exc:
            error = "provider_timeout"
            raise ProviderTimeoutError(f"Offer detail timed out for {offer_id}") from exc
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            await self._audit.record(
                ProviderCallRecord(
                    provider=self._provider.name,
                    operation="offer_detail",
                    request={"offer_id": offer_id},
                    response={"found": detail is not None},
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=error is None,
                    error=error,
                )
            )
        if detail is None:
            raise NotFoundError("Offer not found")
        await self._cache.set_json(
            cache_key,
            detail.model_dump(mode="json"),
            settings.search_session_ttl_seconds,
        )
        return detail

    # --- internal helpers -------------------------------------------------

    async def _validated_params(self, params: SearchParams) -> SearchParams:
        """Uppercase codes, then validate routes and dates (422 on failure)."""
        origin = params.origin.strip().upper()
        destination = params.destination.strip().upper()
        if origin == destination:
            raise ValidationAppError("Origin and destination must be different")
        if params.departure_date < datetime.now(UTC).date():
            raise ValidationAppError("departure_date is in the past")
        if params.return_date is not None and params.return_date < params.departure_date:
            raise ValidationAppError("return_date cannot be before departure_date")
        for role, code in (("origin", origin), ("destination", destination)):
            matches = await self._provider.search_locations(code, 1)
            if not matches or matches[0].code != code:
                raise ValidationAppError(f"Unknown {role} airport code: {code}")
        return params.model_copy(update={"origin": origin, "destination": destination})

    @staticmethod
    def _is_stale(snapshot: SearchSessionSnapshot, settings: Settings) -> bool:
        deadline = settings.search_deadline_seconds
        created_at = snapshot.created_at
        if created_at is None:
            return False
        age = datetime.now(UTC) - created_at
        return age > timedelta(seconds=int(deadline) + _LAZY_FAIL_GRACE_SECONDS)

    async def _load_snapshot_from_db(self, search_id: uuid.UUID) -> SearchSessionSnapshot:
        async with get_session_factory()() as session:
            row = await session.get(SearchSession, search_id)
        if row is None:
            raise NotFoundError("Search session not found")
        # NOTE: PG stores status/count only — offers are restored as empty.
        return SearchSessionSnapshot(
            search_id=row.id,
            agent_id=row.agent_id,
            status=row.status,
            params=SearchParams.model_validate(row.params),
            offers=[],
            items_found=row.items_found,
            error=row.error,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )

    async def _store_snapshot(self, snapshot: SearchSessionSnapshot) -> None:
        settings = get_settings()
        ttl = settings.search_session_ttl_seconds
        if snapshot.expires_at is not None:
            remaining = int((snapshot.expires_at - datetime.now(UTC)).total_seconds())
            ttl = max(remaining, 1)
        await self._cache.set_json(
            f"search:session:{snapshot.search_id}",
            snapshot.model_dump(mode="json"),
            ttl,
        )

    async def _persist_result(
        self,
        session_id: uuid.UUID,
        status: SearchStatus,
        items_found: int,
        error: str | None,
    ) -> None:
        try:
            async with get_session_factory()() as session:
                row = await session.get(SearchSession, session_id)
                if row is None:
                    log.warning("search_session_missing", search_id=str(session_id))
                    return
                row.status = status
                row.items_found = items_found
                row.error = error
                await session.commit()
        except Exception:
            log.exception("search_session_persist_failed", search_id=str(session_id))

    async def _run_search(
        self,
        session_id: uuid.UUID,
        params: SearchParams,
        base: SearchSessionSnapshot,
    ) -> None:
        """Background worker: provider call with deadline, then snapshot+row update.

        Never raises into the event loop — every failure path is caught.
        """
        try:
            settings = get_settings()
            started = time.perf_counter()
            status = SearchStatus.DONE
            offers: list[dict] = []
            error: str | None = None
            try:
                found = await asyncio.wait_for(
                    self._provider.search_offers(params),
                    timeout=settings.search_deadline_seconds,
                )
            except TimeoutError:
                status, error = SearchStatus.FAILED, "provider_timeout"
            except Exception as exc:
                status, error = SearchStatus.FAILED, f"provider_error: {exc}"
                log.exception("search_provider_failed", search_id=str(session_id))
            else:
                offers = [offer.model_dump(mode="json") for offer in found]
            latency_ms = int((time.perf_counter() - started) * 1000)

            cached = await self._cache.get_json(f"search:session:{session_id}")
            snapshot = (
                SearchSessionSnapshot.model_validate(cached)
                if cached is not None
                else base.model_copy()
            )
            snapshot.status = status.value
            snapshot.offers = offers
            snapshot.items_found = len(offers)
            snapshot.error = error
            await self._store_snapshot(snapshot)
            await self._persist_result(session_id, status, len(offers), error)
            await self._audit.record(
                ProviderCallRecord(
                    provider=self._provider.name,
                    operation="search",
                    request=params.model_dump(mode="json"),
                    response={"count": len(offers)},
                    latency_ms=latency_ms,
                    success=status == SearchStatus.DONE,
                    error=error,
                )
            )
            log.info(
                "search_finished",
                search_id=str(session_id),
                status=status.value,
                items_found=len(offers),
                latency_ms=latency_ms,
            )
        except Exception:
            log.exception("search_task_crashed", search_id=str(session_id))
