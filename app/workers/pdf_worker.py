"""Standalone PDF ticket consumer.

Usage: ``python -m app.workers.pdf_worker``

Consumes ``settings.ticket_queue`` (prefetch 1, manual ack). Delivery rules:

- malformed message / unknown order -> acked and dropped (poison messages);
- transient DB/render failures -> up to 3 in-handler retries (2s apart),
  then ``nack(requeue=False)`` dead-letters the message to
  ``settings.ticket_dlq``;
- ack happens only after the DB commit that records the ticket file.

The worker never touches ``order.status`` — PDF generation is not a status
transition; it only fills ``ticket_file``/``ticket_generated_at``. Rendering is
idempotent: a message for an already-ticketed order simply overwrites the same
file path.
"""

import asyncio
import contextlib
import json
import os
import signal
import uuid
from datetime import UTC, datetime

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.messaging.bus import declare_ticket_topology
from app.services.ticket import (
    build_ticket_data,
    load_ticket_context,
    mark_ticket_generated,
    write_pdf_atomic,
)
from app.workers.pdf import render_ticket_pdf

logger = get_logger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_DELAY_S = 2.0
_RECONNECT_DELAY_S = 5.0
_CONNECTION_POLL_S = 1.0
_CONNECT_TIMEOUT_S = 2.0


async def handle_message(message: AbstractIncomingMessage) -> None:
    """Process one delivery: render, persist atomically, record, then ack."""
    try:
        payload = json.loads(message.body)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as exc:
        logger.error("worker_bad_message", error=str(exc), body_preview=message.body[:200])
        await message.ack()
        return

    order_id_raw = str(payload.get("order_id") or "")
    ticket_number = str(payload.get("ticket_number") or "")
    try:
        order_id = uuid.UUID(order_id_raw)
    except ValueError:
        logger.error("worker_bad_order_id", order_id=order_id_raw)
        await message.ack()
        return

    started = datetime.now(UTC)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            order, passengers, agent_company = await load_ticket_context(order_id)
            if order is None:
                logger.warning("worker_order_missing", order_id=order_id_raw)
                await message.ack()
                return
            data = build_ticket_data(order, passengers, agent_company)
            path = write_pdf_atomic(order.id, render_ticket_pdf(data))
            await mark_ticket_generated(order.id, path)
            logger.info(
                "ticket_generated",
                order_id=str(order.id),
                ticket_number=ticket_number or data.ticket_number,
                latency_ms=_latency_ms(started, payload.get("requested_at")),
            )
            await message.ack()
            return
        except Exception as exc:
            logger.warning(
                "ticket_generation_attempt_failed",
                order_id=order_id_raw,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_S)

    logger.error("ticket_generation_failed_permanently", order_id=order_id_raw)
    await message.nack(requeue=False)  # dead-letters to settings.ticket_dlq


def _latency_ms(started: datetime, requested_at: object) -> int:
    """End-to-end latency from the message timestamp (fallback: handler start)."""
    try:
        reference = datetime.fromisoformat(str(requested_at))
    except (TypeError, ValueError):
        reference = started
    return int((datetime.now(UTC) - reference).total_seconds() * 1000)


async def run() -> None:
    """Connect loop: consume until SIGINT/SIGTERM, reconnecting forever."""
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        connection: aio_pika.Connection | None = None
        try:
            connection = await aio_pika.connect(settings.rabbitmq_url, timeout=_CONNECT_TIMEOUT_S)
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            await declare_ticket_topology(channel)
            queue = await channel.get_queue(settings.ticket_queue)
            await queue.consume(handle_message)  # manual ack (no_ack=False default)
            logger.info("worker_consuming", queue=settings.ticket_queue, pid=os.getpid())

            # Block until shutdown or the broker drops us (poll: plain aio-pika
            # connections do not auto-reconnect, unlike RobustConnection).
            while not stop.is_set() and not connection.is_closed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_CONNECTION_POLL_S)
                except TimeoutError:
                    continue
        except Exception as exc:
            if not stop.is_set():
                logger.warning(
                    "worker_reconnecting",
                    delay_s=_RECONNECT_DELAY_S,
                    error=str(exc),
                )
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.close()
        if stop.is_set():
            break
        # Reconnect delay, interruptible by shutdown signals.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_RECONNECT_DELAY_S)

    await dispose_engine()
    logger.info("worker_stopped")


def main() -> None:
    configure_logging(log_level=get_settings().log_level, json_logs=False)
    logger.info("worker_started", queue=get_settings().ticket_queue, pid=os.getpid())
    asyncio.run(run())


if __name__ == "__main__":
    main()
