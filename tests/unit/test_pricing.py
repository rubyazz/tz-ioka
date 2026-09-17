"""Per-passenger pricing rules."""

from decimal import Decimal

import pytest

from app.domain.enums import PassengerType
from app.services.booking import pax_fare


@pytest.mark.parametrize(
    ("pax_type", "base", "expected"),
    [
        (PassengerType.ADULT, Decimal("286.10"), Decimal("286.10")),
        (PassengerType.CHILD, Decimal("100.00"), Decimal("75.00")),
        (PassengerType.INFANT, Decimal("100.00"), Decimal("10.00")),
        # banker's-free rounding: HALF_UP on the third decimal
        (PassengerType.CHILD, Decimal("0.34"), Decimal("0.26")),
        (PassengerType.CHILD, Decimal("0.33"), Decimal("0.25")),
        (PassengerType.INFANT, Decimal("0.05"), Decimal("0.01")),
    ],
)
async def test_pax_fare_multipliers(
    pax_type: PassengerType, base: Decimal, expected: Decimal
) -> None:
    assert pax_fare(base, pax_type) == expected


async def test_total_is_sum_of_fares() -> None:
    base = Decimal("300.00")
    fares = [pax_fare(base, t) for t in (PassengerType.ADULT, PassengerType.CHILD)]
    assert sum(fares, Decimal("0.00")) == Decimal("525.00")
