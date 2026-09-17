"""RabbitMQ publisher bus — implements ``app.domain.ports.MessageBus``.

No robust (auto-reconnecting) connection is used: the connection and channel
are opened lazily on demand and re-opened whenever they are found closed.
Every failure mode is swallowed — :meth:`RabbitMessageBus.publish_ticket_generation`
never raises, it returns ``False`` so callers can fall back to synchronous
generation (see ``settings.ticket_fallback_sync``).

Topology: the durable ticket queue dead-letters rejected/expired messages to
the durable DLQ through the default exchange (``x-dead-letter-exchange: ""``).
"""

import contextlib
import json
from datetime import UTC, datetime
from functools import lru_cache

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Broker reachability budget — keep the caller's request path snappy.
_CONNECT_TIMEOUT_S = 2.0
_PUBLISH_TIMEOUT_S = 3.0


def ticket_queue_arguments() -> dict[str, str]:
    """Declaration arguments routing rejected messages to the ticket DLQ."""
    settings = get_settings()
    return {
        "x-dead-letter-exchange": "",  # default exchange
        "x-dead-letter-routing-key": settings.ticket_dlq,
    }


async def declare_ticket_topology(channel: AbstractChannel) -> None:
    """Idempotently declare the durable ticket queue and its DLQ.

    Shared by the publisher (bus) and the consumer (PDF worker) so both sides
    declare byte-identical topology (RabbitMQ rejects mismatched re-declares).
    """
    settings = get_settings()
    await channel.declare_queue(settings.ticket_dlq, durable=True)
    await channel.declare_queue(
        settings.ticket_queue,
        durable=True,
        arguments=ticket_queue_arguments(),
    )


class RabbitMessageBus:
    """Lazy, failure-tolerant publisher for ticket generation jobs."""

    def __init__(self, url: str, queue_name: str) -> None:
        self._url = url
        self._queue_name = queue_name
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None

    async def _ensure(self) -> AbstractChannel:
        """Return an open channel, (re)connecting and declaring topology once."""
        if self._connection is None or self._connection.is_closed:
            self._channel = None
            self._connection = await aio_pika.connect(
                self._url,
                timeout=_CONNECT_TIMEOUT_S,
            )
        if self._channel is None or self._channel.is_closed:
            # Publisher confirms make publish() raise when the broker cannot
            # accept/route the message — surfaced to callers as False.
            # aio-pika 10.x confirms by default; 9.x needs the explicit call.
            channel = await self._connection.channel()
            confirm = getattr(channel, "confirm_delivery", None)
            if confirm is not None:
                await confirm()
            await declare_ticket_topology(channel)
            self._channel = channel
        return self._channel

    async def publish_ticket_generation(self, order_id: str, ticket_number: str) -> bool:
        """Enqueue a persistent JSON job. Returns False (never raises) if the
        broker is unavailable so the caller can fall back to sync generation."""
        payload = {
            "order_id": order_id,
            "ticket_number": ticket_number,
            "requested_at": datetime.now(UTC).isoformat(),
        }
        message = aio_pika.Message(
            body=json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        try:
            channel = await self._ensure()
            await channel.default_exchange.publish(
                message,
                routing_key=self._queue_name,
                timeout=_PUBLISH_TIMEOUT_S,
            )
            return True
        except Exception as exc:
            logger.warning(
                "bus_unavailable",
                queue=self._queue_name,
                order_id=order_id,
                error=str(exc),
            )
            # Drop the (possibly half-dead) connection so the next call dials fresh.
            await self._discard_connection()
            return False

    async def close(self) -> None:
        """Close the channel/connection, swallowing any errors."""
        await self._discard_connection()

    async def _discard_connection(self) -> None:
        """Close and forget the current connection/channel (never raises)."""
        connection, self._connection, self._channel = self._connection, None, None
        if connection is not None and not connection.is_closed:
            with contextlib.suppress(Exception):  # close is best-effort
                await connection.close()


@lru_cache(maxsize=1)
def get_bus() -> RabbitMessageBus:
    """Process-wide shared bus instance (lazily connected)."""
    settings = get_settings()
    return RabbitMessageBus(settings.rabbitmq_url, settings.ticket_queue)
