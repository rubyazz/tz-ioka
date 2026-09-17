"""Idempotency key store: atomic claim via INSERT ... ON CONFLICT DO NOTHING.

Semantics per (agent_id, scope, key) — see ``IdempotencyRepository.claim``.
"""

import hashlib
import json
import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, IdempotencyConflictError
from app.domain.enums import IdempotencyStatus
from app.models import IdempotencyKey

_CONSTRAINT = "uq_idempotency_agent_scope_key"


def canonical_request_hash(payload: BaseModel) -> str:
    """sha256 over a stable (sorted, compact) JSON encoding of the request."""
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self,
        agent_id: uuid.UUID,
        scope: str,
        key: str,
        request_hash: str,
    ) -> tuple[IdempotencyKey, bool]:
        """Atomically claim the (agent, scope, key) slot.

        Returns ``(row, created)``:

        - ``created=True``  → caller must process the request and finish with
          ``complete()`` or ``fail()`` (row is LOCKED in the meantime);
        - ``created=False`` → a COMPLETED response exists and must be replayed.

        Raises ``IdempotencyConflictError`` while a concurrent request with the
        same key is still LOCKED, and ``ConflictError`` when a COMPLETED key is
        reused with a different payload. FAILED keys are re-claimed (retry).
        """
        stmt = (
            pg_insert(IdempotencyKey)
            .values(
                agent_id=agent_id,
                scope=scope,
                key=key,
                request_hash=request_hash,
                status=IdempotencyStatus.LOCKED,
            )
            .on_conflict_do_nothing(constraint=_CONSTRAINT)
            .returning(IdempotencyKey.id)
        )
        inserted_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if inserted_id is not None:
            claimed = await self.session.get(IdempotencyKey, inserted_id)
            if claimed is None:  # pragma: no cover — inserted within this tx
                raise ConflictError("Idempotency key row vanished within its transaction")
            return claimed, True

        existing = (
            await self.session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.agent_id == agent_id,
                    IdempotencyKey.scope == scope,
                    IdempotencyKey.key == key,
                )
            )
        ).scalar_one()
        if existing.status == IdempotencyStatus.COMPLETED:
            if existing.request_hash != request_hash:
                raise ConflictError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="Idempotency-Key was already used with a different payload",
                )
            return existing, False
        if existing.status == IdempotencyStatus.LOCKED:
            raise IdempotencyConflictError()
        # FAILED → allow a fresh attempt under the same key
        existing.status = IdempotencyStatus.LOCKED
        existing.request_hash = request_hash
        await self.session.flush()
        return existing, True

    async def complete(self, idem_id: uuid.UUID, status_code: int, body: dict) -> None:
        """Store the final response for a LOCKED key and commit."""
        row = await self.session.get(IdempotencyKey, idem_id)
        if row is None:  # pragma: no cover — claimed earlier in this request
            return
        row.status = IdempotencyStatus.COMPLETED
        row.response_status_code = status_code
        row.response_body = body
        await self.session.commit()

    async def fail(self, idem_id: uuid.UUID) -> None:
        """Mark a LOCKED key FAILED so the client may retry; commits."""
        row = await self.session.get(IdempotencyKey, idem_id)
        if row is None:  # pragma: no cover — claimed earlier in this request
            return
        row.status = IdempotencyStatus.FAILED
        await self.session.commit()
