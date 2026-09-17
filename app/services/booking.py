"""Order creation: offer validation, per-pax pricing, idempotent persistence."""

import asyncio
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    NotFoundError,
    OfferNotAvailableError,
    ProviderTimeoutError,
)
from app.core.logging import get_logger
from app.domain.entities import OfferDetail
from app.domain.enums import OrderStatus, PassengerType
from app.models import Agent, IdempotencyKey, Order, OrderPassenger, OrderStatusHistory
from app.providers import get_provider
from app.repositories.idempotency import IdempotencyRepository, canonical_request_hash
from app.repositories.order import OrderRepository
from app.schemas.orders import BookingCreateIn, OrderOut

log = get_logger(__name__)

_CENT = Decimal("0.01")
_PAX_MULTIPLIERS: dict[PassengerType, Decimal] = {
    PassengerType.ADULT: Decimal("1.00"),
    PassengerType.CHILD: Decimal("0.75"),
    PassengerType.INFANT: Decimal("0.10"),
}


def pax_fare(base_price: Decimal, pax_type: PassengerType) -> Decimal:
    """Per-passenger fare: base price times the pax-type multiplier, 2-dp."""
    return (base_price * _PAX_MULTIPLIERS[pax_type]).quantize(_CENT, rounding=ROUND_HALF_UP)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class BookingService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def create_order(
        self,
        agent: Agent,
        payload: BookingCreateIn,
        idempotency_key: str | None,
    ) -> tuple[Order | None, dict | None]:
        """Create a BOOKED order for ``agent``.

        Returns ``(order, None)`` on a fresh creation, or ``(None, body)`` when
        the request replays a previously COMPLETED idempotency key — ``body``
        being the stored OrderOut JSON of the original 201 response.
        """
        idem_repo = IdempotencyRepository(self._session)
        idem: IdempotencyKey | None = None
        if idempotency_key:
            idem, created = await idem_repo.claim(
                agent.id, "create_order", idempotency_key, canonical_request_hash(payload)
            )
            if not created:
                return None, idem.response_body or {}

        try:
            order = await self._persist(agent, payload)
        except Exception:
            # Release locks and mark the key retryable before propagating.
            # Capture the id BEFORE rollback: rollback expires loaded
            # instances and reading idem.id afterwards would refresh them
            # synchronously.
            if idem is not None:
                idem_id = idem.id
                await self._session.rollback()
                await idem_repo.fail(idem_id)
            raise

        body = OrderOut.from_order(order).model_dump(mode="json")
        if idem is not None:
            await idem_repo.complete(idem.id, 201, body)
        log.info(
            "order_created",
            order_id=str(order.id),
            agent_id=str(agent.id),
            total=f"{order.total_amount:.2f}",
            currency=order.currency,
        )
        return order, None

    async def _persist(self, agent: Agent, payload: BookingCreateIn) -> Order:
        offer = await self._load_offer(payload.offer_id)

        fares = [pax_fare(offer.price, pax.type) for pax in payload.passengers]
        total = sum(fares, Decimal("0.00")).quantize(_CENT, rounding=ROUND_HALF_UP)

        order = Order(
            agent_id=agent.id,
            offer_id=payload.offer_id,
            search_id=None,
            status=OrderStatus.BOOKED,
            total_amount=total,
            currency=offer.currency,
            offer_snapshot=offer.model_dump(mode="json"),
            contact=payload.contact.model_dump(mode="json"),
        )
        order.passengers = [
            OrderPassenger(
                pax_type=pax.type,
                first_name=pax.first_name,
                last_name=pax.last_name,
                date_of_birth=pax.date_of_birth,
                gender=pax.gender,
                citizenship=pax.citizenship,
                doc_type=pax.doc_type,
                doc_number=pax.doc_number,
                fare_amount=fare,
            )
            for pax, fare in zip(payload.passengers, fares, strict=True)
        ]
        order.history = [
            OrderStatusHistory(
                from_status=None,
                to_status=OrderStatus.BOOKED,
                actor=agent.username,
            )
        ]
        await OrderRepository(self._session).create(order)
        await self._session.commit()
        return order

    async def _load_offer(self, offer_id: str) -> OfferDetail:
        try:
            offer = await asyncio.wait_for(
                get_provider().get_offer_detail(offer_id),
                timeout=self._settings.provider_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ProviderTimeoutError(
                f"Provider timed out while fetching offer {offer_id}"
            ) from exc
        if offer is None:
            raise NotFoundError("Offer not found")
        expires_at = offer.offer_expires_at
        if expires_at is not None and _as_utc(expires_at) <= datetime.now(UTC):
            raise OfferNotAvailableError(f"Offer {offer_id} has expired")
        return offer
