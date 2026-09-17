"""GET /avia/locations — IATA dictionary lookup (auth required)."""

from fastapi import APIRouter, Query

from app.api.deps import CurrentAgent
from app.cache import get_cache
from app.providers import get_provider
from app.schemas.locations import LocationOut
from app.services.locations import LocationsService

router = APIRouter(prefix="/avia/locations", tags=["locations"])


@router.get("", response_model=list[LocationOut], summary="Search IATA locations")
async def search_locations(
    agent: CurrentAgent,
    query: str = Query(
        min_length=2,
        max_length=64,
        examples=["TAS", "istan"],
        description="Free-text query: code, city, airport or country name",
    ),
    limit: int = Query(10, ge=1, le=50),
) -> list[LocationOut]:
    """Return airports matching the query; exact IATA codes rank first."""
    service = LocationsService(get_provider(), get_cache())
    found = await service.search(query, limit)
    return [LocationOut.model_validate(location) for location in found]
