"""Avia content provider factory.

CONTRACT — owned by the SEARCH agent. Dispatches on settings.provider.
`get_provider()` must return a cached instance implementing
app.domain.ports.AviaContentProvider.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.domain.ports import AviaContentProvider


@lru_cache(maxsize=1)
def get_provider() -> AviaContentProvider:
    """Return the configured avia content provider (one instance per process)."""
    settings = get_settings()
    if settings.provider == "mock":
        from app.providers.mock import MockAviaProvider

        return MockAviaProvider(
            latency_ms_range=(settings.mock_latency_min_ms, settings.mock_latency_max_ms)
        )
    if settings.provider == "http":
        raise ValueError("provider 'http' is not implemented yet; set PROVIDER=mock")
    raise ValueError(f"Unknown avia content provider: {settings.provider!r}")
