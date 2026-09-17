"""Ports (hexagonal architecture): interfaces the core depends on.

Implementations live in ``app.providers`` (avia content), ``app.repositories``
(persistence), ``app.cache`` (Redis), ``app.messaging`` (RabbitMQ).
Services depend on these protocols only — swapping the mock provider for a
real GDS/API adapter must not touch business logic.
"""

from typing import Protocol, runtime_checkable

from app.domain.entities import Location, Offer, OfferDetail, SearchParams


@runtime_checkable
class AviaContentProvider(Protocol):
    """External source of avia content (IATA locations, offers, fare details)."""

    name: str

    async def search_locations(self, query: str, limit: int) -> list[Location]:
        """Return airports/cities matching a free-text query."""
        ...

    async def search_offers(self, params: SearchParams) -> list[Offer]:
        """Run a (potentially slow) search and return all offers at once.

        Implementations should simulate/perform real network latency —
        the orchestration layer wraps this in a timeout and caches results.
        """
        ...

    async def get_offer_detail(self, offer_id: str) -> OfferDetail | None:
        """Return full offer details incl. fare families, or None if unknown."""
        ...


@runtime_checkable
class Cache(Protocol):
    """Thin async cache port (Redis in production, in-memory dict in tests)."""

    async def get_json(self, key: str) -> dict | list | None: ...
    async def set_json(self, key: str, value: dict | list, ttl_seconds: int) -> None: ...
    async def delete(self, key: str) -> None: ...


@runtime_checkable
class MessageBus(Protocol):
    """Publisher port for background jobs (PDF generation etc.)."""

    async def publish_ticket_generation(self, order_id: str, ticket_number: str) -> bool:
        """Enqueue a PDF generation job. Returns False when the broker is
        unavailable and the caller should fall back to sync generation."""
        ...

    async def close(self) -> None: ...
