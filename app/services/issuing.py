"""Atomic order issuing: single-transaction balance debit + ticket hand-off."""

import hashlib
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    InsufficientFundsError,
    NotFoundError,
)
from app.core.logging import get_logger
from app.db.base import utcnow
from app.domain.enums import OrderStatus, TransactionType
from app.models import Agent, BalanceTransaction, IdempotencyKey, Order, OrderStatusHistory
from app.repositories.agent import AgentRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.order import OrderRepository
from app.schemas.orders import IssueOut
from app.services.ticket import TicketService

log = get_logger(__name__)

_CENT = Decimal("0.01")
_TICKET_FORM_CODE = "232"


class IssuingService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def issue(
        self,
        agent: Agent,
        order_id: uuid.UUID,
        idempotency_key: str | None,
    ) -> tuple[IssueOut | None, dict | None]:
        """Issue a BOOKED order: debit balance atomically, assign the ticket.

        Returns ``(issue_out, None)`` on fresh issuing (or idempotent repeat on
        an already-ISSUED order), or ``(None, body)`` when replaying a stored
        COMPLETED idempotency key response.
        """
        idem_repo = IdempotencyRepository(self._session)
        idem: IdempotencyKey | None = None
        if idempotency_key:
            request_hash = hashlib.sha256(f"issue:{order_id}".encode()).hexdigest()
            idem, created = await idem_repo.claim(agent.id, "issue", idempotency_key, request_hash)
            if not created:
                return None, idem.response_body or {}

        # Fixed global lock order — agent row FIRST, then order — prevents
        # deadlocks between concurrent issues of different orders by the same
        # agent (they serialize on the agent row). get_for_update uses
        # populate_existing so the pre-lock instance from the auth dependency
        # is refreshed with post-lock values (no stale-balance race).
        locked_agent = await AgentRepository(self._session).get_for_update(agent.id)
        if locked_agent is None:  # pragma: no cover — token dep already loaded it
            raise AuthenticationError("Agent no longer exists")
        order = await OrderRepository(self._session).get_for_update(order_id)
        if order is None:
            raise NotFoundError("Order not found")
        if order.agent_id != agent.id:
            raise AuthorizationError("Order belongs to another agent")

        if order.status == OrderStatus.ISSUED:
            # First attempt committed but the key never got COMPLETED (e.g.
            # crash between commit and complete): idempotent success, no debit.
            body = self._issue_out(order, locked_agent).model_dump(mode="json")
            if idem is not None:
                await idem_repo.complete(idem.id, 200, body)
            return IssueOut(**body), None

        if order.status != OrderStatus.BOOKED:
            raise ConflictError(
                code="INVALID_ORDER_STATUS",
                message=f"Order is {order.status}, only BOOKED can be issued",
            )
        if order.currency != locked_agent.currency:
            raise ConflictError(
                code="CURRENCY_MISMATCH",
                message=(
                    f"Order currency {order.currency} does not match "
                    f"account currency {locked_agent.currency}"
                ),
            )

        if locked_agent.balance < order.total_amount:
            # Snapshot values BEFORE rollback: rollback expires all loaded
            # instances, and touching their attributes afterwards would fire
            # a synchronous (non-async) refresh.
            order_amount = f"{order.total_amount:.2f}"
            balance = f"{locked_agent.balance:.2f}"
            agent_id_str = str(agent.id)
            idem_id = idem.id if idem is not None else None
            # No writes happened; drop locks and let the client retry later.
            await self._session.rollback()
            if idem_id is not None:
                await idem_repo.fail(idem_id)
            log.warning(
                "issue_rejected_insufficient_funds",
                order_id=str(order_id),
                agent_id=agent_id_str,
                order_amount=order_amount,
                balance=balance,
            )
            raise InsufficientFundsError(details={"order_amount": order_amount, "balance": balance})

        debited = order.total_amount.quantize(_CENT, rounding=ROUND_HALF_UP)
        new_balance = (locked_agent.balance - debited).quantize(_CENT, rounding=ROUND_HALF_UP)
        locked_agent.balance = new_balance
        self._session.add(
            BalanceTransaction(
                agent_id=locked_agent.id,
                order_id=order.id,
                txn_type=TransactionType.DEBIT,
                amount=debited,
                balance_after=new_balance,
                note=f"Issue order {order.id}",
            )
        )
        order.status = OrderStatus.ISSUED
        order.issued_at = utcnow()
        # 13-digit e-ticket: form-code prefix + last 10 digits of the UUID int.
        order.ticket_number = f"{_TICKET_FORM_CODE}{order.id.int % 10**10:010d}"
        self._session.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=OrderStatus.BOOKED,
                to_status=OrderStatus.ISSUED,
                actor=locked_agent.username,
            )
        )
        await self._session.commit()

        log.info(
            "order_issued",
            order_id=str(order.id),
            ticket_number=order.ticket_number,
            debited=f"{debited:.2f}",
            balance_after=f"{new_balance:.2f}",
        )

        issue_out = IssueOut(
            id=order.id,
            status=order.status,
            ticket_number=order.ticket_number,
            debited_amount=f"{debited:.2f}",
            balance_after=f"{new_balance:.2f}",
            currency=order.currency,
            issued_at=order.issued_at,
        )
        if idem is not None:
            await idem_repo.complete(idem.id, 200, issue_out.model_dump(mode="json"))

        await self._kick_ticket_generation(order)
        return issue_out, None

    async def _kick_ticket_generation(self, order: Order) -> None:
        """Best-effort PDF kick AFTER commit — issuing never fails on PDF/broker."""
        try:
            tickets = TicketService(self._session)
            enqueued = await tickets.request_generation(order)
            if not enqueued and self._settings.ticket_fallback_sync:
                await tickets.generate_sync(order)
        except Exception as exc:
            log.warning("ticket_kick_failed", order_id=str(order.id), error=str(exc))

    @staticmethod
    def _issue_out(order: Order, agent: Agent) -> IssueOut:
        """Rebuild the issue response from current state (already-ISSUED path)."""
        return IssueOut(
            id=order.id,
            status=OrderStatus.ISSUED,
            ticket_number=order.ticket_number or "",
            debited_amount=f"{order.total_amount:.2f}",
            balance_after=f"{agent.balance:.2f}",
            currency=order.currency,
            issued_at=order.issued_at or utcnow(),
        )
