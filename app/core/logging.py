"""Structured logging via structlog.

- development: pretty console output
- production/test: one JSON line per event

Every log line carries ``request_id`` (from the X-Request-ID middleware) and
``agent_id`` when available, set via ``bind_request_context``.
"""

import logging
import sys
import uuid
from contextvars import ContextVar

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
agent_id_var: ContextVar[str | None] = ContextVar("agent_id", default=None)


def bind_request_context(request_id: str | None = None, agent_id: str | None = None) -> None:
    if request_id is not None:
        request_id_var.set(request_id)
    if agent_id is not None:
        agent_id_var.set(agent_id)


def _inject_context(_, __, event_dict: dict) -> dict:
    rid = request_id_var.get()
    aid = agent_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    if aid:
        event_dict["agent_id"] = aid
    return event_dict


def configure_logging(log_level: str = "INFO", *, json_logs: bool = False) -> None:
    level = logging.getLevelName(log_level.upper())
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
