"""Audit persistence for external provider calls."""

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.entities import ProviderCallRecord
from app.models import ProviderCallLog

log = get_logger("audit")


class ProviderAuditRecorder:
    """Writes ``ProviderCallRecord`` rows and logs the structured event.

    Opens its own session per record: it is invoked from background tasks and
    cache-side code paths where no request session exists. Database failures
    are logged and swallowed — auditing must never break the main flow.
    """

    async def record(self, record: ProviderCallRecord) -> None:
        """Persist one provider call; never raises."""
        log.info(
            "provider_call",
            provider=record.provider,
            operation=record.operation,
            latency_ms=record.latency_ms,
            success=record.success,
            error=record.error,
        )
        try:
            async with get_session_factory()() as session:
                session.add(
                    ProviderCallLog(
                        provider=record.provider,
                        operation=record.operation,
                        request=record.request,
                        response=record.response,
                        http_status=record.http_status,
                        latency_ms=record.latency_ms,
                        success=record.success,
                        error=record.error,
                    )
                )
                await session.commit()
        except Exception as exc:
            log.warning(
                "provider_audit_failed",
                provider=record.provider,
                operation=record.operation,
                error=str(exc),
            )
