"""Mock provider contract: determinism, fare families, route sanity."""

from datetime import date

from app.domain.entities import SearchParams
from app.providers.mock import MockAviaProvider


def _params(**overrides) -> SearchParams:
    base = {
        "origin": "TAS",
        "destination": "IST",
        "departure_date": date(2026, 10, 1),
    }
    base.update(overrides)
    return SearchParams(**base)


async def test_search_is_deterministic() -> None:
    provider = MockAviaProvider(latency_ms_range=(0, 0))
    first = await provider.search_offers(_params())
    second = await provider.search_offers(_params())
    assert [o.offer_id for o in first] == [o.offer_id for o in second]
    assert [str(o.price) for o in first] == [str(o.price) for o in second]
    assert first  # sanity: non-empty


async def test_different_params_differ() -> None:
    provider = MockAviaProvider(latency_ms_range=(0, 0))
    a = await provider.search_offers(_params())
    b = await provider.search_offers(_params(departure_date=date(2026, 10, 2)))
    assert {o.offer_id for o in a} != {o.offer_id for o in b}


async def test_offers_have_sane_routes_and_money() -> None:
    provider = MockAviaProvider(latency_ms_range=(0, 0))
    offers = await provider.search_offers(_params())
    assert offers
    for offer in offers:
        assert offer.segments
        assert offer.segments[0].origin == "TAS"
        assert offer.segments[-1].destination == "IST"
        for seg in offer.segments:
            assert seg.origin != seg.destination  # no TAS->TAS legs
            assert seg.arrival > seg.departure
        assert offer.price > 0
        assert offer.currency == "USD"
        assert offer.seats_left >= 1


async def test_offer_detail_rebuildable_from_id() -> None:
    provider = MockAviaProvider(latency_ms_range=(0, 0))
    offers = await provider.search_offers(_params())
    target = offers[0]

    detail = await provider.get_offer_detail(target.offer_id)
    assert detail is not None
    assert detail.offer_id == target.offer_id
    assert str(detail.price) == str(target.price)
    assert len(detail.fare_families) == 3
    assert {ff.name for ff in detail.fare_families} == {
        "ECONOMY LITE",
        "ECONOMY STANDARD",
        "ECONOMY FLEX",
    }
    assert detail.offer_expires_at is not None


async def test_unknown_offer_ids_return_none() -> None:
    provider = MockAviaProvider(latency_ms_range=(0, 0))
    assert await provider.get_offer_detail("of_00000000000099") is None
    assert await provider.get_offer_detail("not-an-offer") is None


async def test_locations_exact_code_ranks_first() -> None:
    provider = MockAviaProvider(latency_ms_range=(0, 0))
    by_code = await provider.search_locations("TAS", 5)
    assert by_code[0].code == "TAS"
    by_city = await provider.search_locations("tashkent", 5)
    assert any(loc.code == "TAS" for loc in by_city)
    assert await provider.search_locations("zz9", 5) == []
