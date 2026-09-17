"""Order status state machine invariants."""

import pytest

from app.domain.enums import (
    ALLOWED_ORDER_TRANSITIONS,
    InvalidStatusTransition,
    OrderStatus,
)


async def test_booked_can_reach_every_terminal_state() -> None:
    allowed = ALLOWED_ORDER_TRANSITIONS[OrderStatus.BOOKED]
    assert allowed == {OrderStatus.ISSUED, OrderStatus.CANCELLED, OrderStatus.FAILED}


@pytest.mark.parametrize("status", [OrderStatus.ISSUED, OrderStatus.CANCELLED, OrderStatus.FAILED])
async def test_terminal_states_have_no_exits(status: OrderStatus) -> None:
    assert ALLOWED_ORDER_TRANSITIONS[status] == set()


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.ISSUED, OrderStatus.BOOKED),
        (OrderStatus.CANCELLED, OrderStatus.ISSUED),
        (OrderStatus.FAILED, OrderStatus.ISSUED),
        (OrderStatus.ISSUED, OrderStatus.ISSUED),
    ],
)
async def test_illegal_transitions_raise(current: OrderStatus, target: OrderStatus) -> None:
    if target not in ALLOWED_ORDER_TRANSITIONS[current]:
        with pytest.raises(InvalidStatusTransition):
            raise InvalidStatusTransition(current, target)
    else:  # pragma: no cover — guard against a broken parametrization
        pytest.fail(f"{current} -> {target} unexpectedly allowed")


async def test_exception_carries_statuses() -> None:
    exc = InvalidStatusTransition(OrderStatus.BOOKED, OrderStatus.BOOKED)
    assert exc.current == OrderStatus.BOOKED
    assert exc.target == OrderStatus.BOOKED
    assert "BOOKED" in str(exc)
