"""Deterministic mock avia content provider.

Same search parameters always produce the same offers — even across process
restarts — because every random draw comes from ``random.Random`` seeded with
a SHA-256 digest of the parameters. ``offer_id`` embeds the first 12 hex
characters of that digest plus a two-digit index, so ``get_offer_detail`` can
rebuild the whole offer set without in-memory offer state and pick the exact
offer by index (only the search params themselves are remembered, in a small
bounded map, to know which airports/dates an offer_id belongs to).
"""

import asyncio
import random
import re
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256

from app.core.config import get_settings
from app.domain.entities import (
    Baggage,
    BaggageAllowance,
    FareFamily,
    FlightSegment,
    Location,
    Offer,
    OfferDetail,
    SearchParams,
)
from app.domain.enums import ServiceClass
from app.providers.locations_data import BY_CODE, LOCATIONS

_CENT = Decimal("0.01")
_NO_CHECKED_BAGGAGE_BELOW = Decimal("250.00")
_MAX_REMEMBERED_SEEDS = 1024
_OFFER_ID_RE = re.compile(r"of_(?P<seed>[0-9a-f]{12})(?P<idx>\d{2})")

CARRIERS: dict[str, str] = {
    "HY": "Uzbekistan Airways",
    "TK": "Turkish Airlines",
    "KC": "Air Astana",
    "FZ": "flydubai",
    "SU": "Aeroflot",
    "QR": "Qatar Airways",
    "EK": "Emirates",
    "S7": "S7 Airlines",
    "W6": "Wizz Air",
    "PC": "Pegasus Airlines",
}

#: Plausible connection hubs per validating carrier.
HUBS: dict[str, str] = {
    "HY": "TAS",
    "TK": "IST",
    "PC": "SAW",
    "KC": "ALA",
    "SU": "SVO",
    "S7": "DME",
    "FZ": "DXB",
    "EK": "DXB",
    "QR": "DOH",
    "W6": "WAW",
}

AIRCRAFT: tuple[str, ...] = (
    "A320",
    "A321",
    "B737-800",
    "B787-9",
    "A350-900",
    "B777-300ER",
    "SSJ-100",
)

_FARE_RULES: tuple[str, ...] = (
    "Name changes are not permitted after ticketing.",
    "Changes are permitted with the fare difference plus the change penalty.",
    "No-show: the full ticket value is forfeited.",
    "The ticket must be issued within 24 hours after booking.",
    "Partial refunds for unused segments are not permitted.",
    "Fares are guaranteed only at the moment of ticket issuance.",
    "Free cabin baggage must fit the dimensions printed on the fare rules.",
)


def _seed_for(params: SearchParams) -> str:
    """12-hex-char deterministic seed derived from the search parameters."""
    key = "|".join(
        [
            params.origin,
            params.destination,
            params.departure_date.isoformat(),
            params.return_date.isoformat() if params.return_date else "",
            ",".join(sorted(passenger.type for passenger in params.passengers)),
            params.service_class,
        ]
    )
    return sha256(key.encode()).hexdigest()[:12]


class MockAviaProvider:
    """In-memory deterministic provider used for development and tests."""

    name = "mock"

    def __init__(self, latency_ms_range: tuple[int, int] = (1500, 3000)) -> None:
        self._latency_ms_range = latency_ms_range
        # offer seed -> params that produced it (lets get_offer_detail rebuild)
        self._seeds: dict[str, SearchParams] = {}

    async def search_locations(self, query: str, limit: int) -> list[Location]:
        """Case-insensitive substring lookup; exact code matches come first."""
        q = query.strip()
        if not q or limit <= 0:
            return []
        needle = q.lower()
        ordered: list[Location] = []
        if q.isascii() and q.isalpha() and q.isupper() and len(q) <= 3:
            exact = BY_CODE.get(q)
            if exact is not None:
                ordered.append(exact)
            ordered.extend(
                location
                for code, location in BY_CODE.items()
                if code.startswith(q) and location is not exact
            )
        seen = {location.code for location in ordered}
        for location in LOCATIONS:
            if location.code in seen:
                continue
            haystack = f"{location.code} {location.name} {location.city} {location.country}"
            if needle in haystack.lower():
                ordered.append(location)
        return ordered[:limit]

    async def search_offers(self, params: SearchParams) -> list[Offer]:
        """Simulate provider latency, then return the deterministic offer set."""
        params = params.model_copy(
            update={
                "origin": params.origin.strip().upper(),
                "destination": params.destination.strip().upper(),
            }
        )
        seed = _seed_for(params)
        latency_rng = random.Random(f"{seed}:latency")
        await asyncio.sleep(latency_rng.uniform(*self._latency_ms_range) / 1000)
        self._remember_seed(seed, params)
        return self._build_offers(seed, params)

    async def get_offer_detail(self, offer_id: str) -> OfferDetail | None:
        """Rebuild the offer set from the seed embedded in ``offer_id``."""
        match = _OFFER_ID_RE.fullmatch(offer_id)
        if match is None:
            return None
        params = self._seeds.get(match["seed"])
        if params is None:
            return None
        offers = self._build_offers(match["seed"], params)
        index = int(match["idx"])
        if index >= len(offers):
            return None
        return self._enrich(offers[index], match["seed"])

    # --- internal helpers -------------------------------------------------

    def _remember_seed(self, seed: str, params: SearchParams) -> None:
        if len(self._seeds) >= _MAX_REMEMBERED_SEEDS:
            self._seeds.clear()
        self._seeds[seed] = params

    def _build_leg(
        self,
        rng: random.Random,
        carrier: str,
        origin: str,
        destination: str,
        day: date,
    ) -> list[FlightSegment]:
        """One direction of travel: nonstop or a single connection via a hub."""
        hub = HUBS.get(carrier)
        via_hub = hub not in (None, origin, destination) and rng.random() < 0.35
        stops = [origin, str(hub), destination] if via_hub else [origin, destination]
        departure = datetime.combine(day, dtime.min, tzinfo=UTC) + timedelta(
            minutes=rng.randint(0, 780)
        )
        segments: list[FlightSegment] = []
        for position in range(1, len(stops)):
            leg_from, leg_to = stops[position - 1], stops[position]
            duration = rng.randint(60, 360) if via_hub else rng.randint(60, 720)
            arrival = departure + timedelta(minutes=duration)
            segments.append(
                FlightSegment(
                    origin=leg_from,
                    destination=leg_to,
                    flight_number=f"{carrier}{rng.randint(100, 999)}",
                    carrier_code=carrier,
                    carrier_name=CARRIERS[carrier],
                    departure=departure,
                    arrival=arrival,
                    duration_minutes=duration,
                    aircraft=rng.choice(AIRCRAFT),
                )
            )
            if position < len(stops) - 1:  # +90 min layover before the next leg
                departure = arrival + timedelta(minutes=90)
        return segments

    def _build_offers(self, seed: str, params: SearchParams) -> list[Offer]:
        rng = random.Random(seed)
        carrier = rng.choice(tuple(CARRIERS))
        is_business = params.service_class == ServiceClass.BUSINESS.value
        offers: list[Offer] = []
        for _ in range(rng.randint(5, 12)):
            segments = self._build_leg(
                rng, carrier, params.origin, params.destination, params.departure_date
            )
            if params.return_date is not None:
                segments += self._build_leg(
                    rng, carrier, params.destination, params.origin, params.return_date
                )
            price = Decimal(str(round(rng.uniform(120.0, 1800.0), 2)))
            if is_business:
                multiplier = Decimal(str(round(rng.uniform(2.8, 3.5), 2)))
                price = (price * multiplier).quantize(_CENT, rounding=ROUND_HALF_UP)
            checked = (
                BaggageAllowance(kg=0, pieces=0)  # cheaper fares carry no checked bag
                if price < _NO_CHECKED_BAGGAGE_BELOW
                else BaggageAllowance(kg=20, pieces=1)
            )
            offers.append(
                Offer(
                    offer_id="",
                    origin=params.origin,
                    destination=params.destination,
                    validating_carrier=carrier,
                    price=price,
                    currency="USD",
                    service_class=params.service_class,
                    refundable=rng.random() < 0.30,
                    seats_left=rng.randint(1, 9),
                    segments=segments,
                    baggage=Baggage(cabin=BaggageAllowance(kg=8, pieces=1), checked=checked),
                )
            )
        offers.sort(key=lambda offer: offer.price)
        return [
            offer.model_copy(update={"offer_id": f"of_{seed}{index:02d}"})
            for index, offer in enumerate(offers)
        ]

    def _enrich(self, offer: Offer, seed: str) -> OfferDetail:
        service_class = offer.service_class
        fares_rng = random.Random(f"{seed}:fares")
        lite_checked = (
            BaggageAllowance(kg=0, pieces=0)
            if offer.baggage is None or offer.baggage.checked is None
            else offer.baggage.checked
        )
        standard_extra = Decimal(str(round(fares_rng.uniform(45.0, 80.0), 2)))
        flex_extra = Decimal(str(round(fares_rng.uniform(110.0, 180.0), 2)))
        families = [
            FareFamily(
                name=f"{service_class} LITE",
                service_class=service_class,
                price=offer.price,
                refundable=False,
                exchangeable=False,
                baggage=Baggage(cabin=BaggageAllowance(kg=8, pieces=1), checked=lite_checked),
                seats_left=offer.seats_left,
                fare_rules=self._fare_rules(f"{seed}:lite"),
            ),
            FareFamily(
                name=f"{service_class} STANDARD",
                service_class=service_class,
                price=(offer.price + standard_extra).quantize(_CENT, ROUND_HALF_UP),
                refundable=True,
                exchangeable=True,
                baggage=Baggage(
                    cabin=BaggageAllowance(kg=8, pieces=1),
                    checked=BaggageAllowance(kg=20, pieces=1),
                ),
                seats_left=max(1, offer.seats_left - 1),
                fare_rules=self._fare_rules(f"{seed}:standard"),
            ),
            FareFamily(
                name=f"{service_class} FLEX",
                service_class=service_class,
                price=(offer.price + flex_extra).quantize(_CENT, ROUND_HALF_UP),
                refundable=True,
                exchangeable=True,
                baggage=Baggage(
                    cabin=BaggageAllowance(kg=8, pieces=1),
                    checked=BaggageAllowance(kg=23, pieces=2),
                ),
                seats_left=max(1, offer.seats_left - 2),
                fare_rules=self._fare_rules(f"{seed}:flex"),
            ),
        ]
        return OfferDetail(
            **offer.model_dump(),
            fare_families=families,
            fare_rules=families[1].fare_rules,
            offer_expires_at=datetime.now(UTC)
            + timedelta(seconds=get_settings().search_session_ttl_seconds),
        )

    @staticmethod
    def _fare_rules(seed: str) -> list[str]:
        rng = random.Random(seed)
        return rng.sample(list(_FARE_RULES), rng.randint(3, 5))
