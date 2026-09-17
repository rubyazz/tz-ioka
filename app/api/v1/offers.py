"""POST /avia/offers + status polling + offer detail (auth required).

Route order matters: ``/search/{search_id}`` is declared before
``/{offer_id}`` so "search" is never captured as an offer_id.
"""

import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentAgent
from app.cache import get_cache
from app.core.errors import NotFoundError
from app.domain.entities import PassengerQuery, SearchParams
from app.providers import get_provider
from app.repositories.audit import ProviderAuditRecorder
from app.schemas.offers import (
    OfferDetailOut,
    OfferOut,
    SearchCreateIn,
    SearchStartedOut,
    SearchStatusOut,
)
from app.services.search import SearchService

router = APIRouter(prefix="/avia/offers", tags=["offers"])


def _search_service() -> SearchService:
    return SearchService(get_provider(), get_cache(), ProviderAuditRecorder())


@router.post(
    "",
    response_model=SearchStartedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an async flight search",
)
async def start_search(body: SearchCreateIn, agent: CurrentAgent) -> SearchStartedOut:
    """Validate the route and return a search session to poll (202 Accepted)."""
    params = SearchParams(
        origin=body.origin,
        destination=body.destination,
        departure_date=body.departure_date,
        return_date=body.return_date,
        passengers=[PassengerQuery(type=passenger.type.value) for passenger in body.passengers],
        service_class=body.service_class.value,
    )
    snapshot = await _search_service().start_search(agent, params)
    return SearchStartedOut(
        search_id=snapshot.search_id,
        status=snapshot.status,
        expires_at=snapshot.expires_at,
    )


@router.get("/search/{search_id}", response_model=SearchStatusOut, summary="Poll search status")
async def get_search_status(search_id: uuid.UUID, agent: CurrentAgent) -> SearchStatusOut:
    """Current session state; ``items`` stays empty until status is ``done``."""
    snapshot = await _search_service().get_search(agent, search_id)
    return SearchStatusOut(
        search_id=snapshot.search_id,
        status=snapshot.status,
        items=[OfferOut.model_validate(item) for item in snapshot.offers],
        items_found=snapshot.items_found,
        expires_at=snapshot.expires_at,
        error=snapshot.error,
    )


@router.get("/{offer_id}", response_model=OfferDetailOut, summary="Offer details")
async def get_offer_detail(offer_id: str, agent: CurrentAgent) -> OfferDetailOut:
    """Full offer with fare families; unknown or malformed ids are 404."""
    if not offer_id.startswith("of_"):
        raise NotFoundError("Offer not found")
    detail = await _search_service().get_offer_detail(offer_id)
    # model_dump first: nested domain entities are not instances of the
    # nested Out-schemas, and pydantic only coerces dicts here.
    return OfferDetailOut.model_validate(detail.model_dump())
