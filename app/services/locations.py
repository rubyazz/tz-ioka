"""Location lookup service: provider call + audit + TTL cache."""

import time

from app.core.config import get_settings
from app.core.errors import ValidationAppError
from app.domain.entities import Location, ProviderCallRecord
from app.domain.ports import AviaContentProvider, Cache
from app.repositories.audit import ProviderAuditRecorder


class LocationsService:
    """IATA dictionary search with a per-(query, limit) read-through cache."""

    def __init__(
        self,
        provider: AviaContentProvider,
        cache: Cache,
        audit: ProviderAuditRecorder | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._audit = audit or ProviderAuditRecorder()

    async def search(self, query: str, limit: int = 10) -> list[Location]:
        """Return locations matching ``query`` (min 2 chars, limit clamped 1..50)."""
        cleaned = query.strip()
        if len(cleaned) < 2:
            raise ValidationAppError("Query must be at least 2 characters")
        limit = max(1, min(limit, 50))
        settings = get_settings()
        cache_key = f"locations:{cleaned.lower()}:{limit}"

        cached = await self._cache.get_json(cache_key)
        if cached is not None:
            return [Location.model_validate(item) for item in cached]

        started = time.perf_counter()
        error: str | None = None
        locations: list[Location] = []
        try:
            locations = await self._provider.search_locations(cleaned, limit)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            await self._audit.record(
                ProviderCallRecord(
                    provider=self._provider.name,
                    operation="locations",
                    request={"query": cleaned, "limit": limit},
                    response={"count": len(locations)},
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=error is None,
                    error=error,
                )
            )
        await self._cache.set_json(
            cache_key,
            [location.model_dump(mode="json") for location in locations],
            settings.locations_cache_ttl_seconds,
        )
        return locations
