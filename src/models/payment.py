"""Payment entity - money collected for a completed order.

Business rules (ADR-0016): at most one payment per order; payments reference
completed orders only; payment_date >= order_date; amount = sum of the
order's item totals (quantity * unit_price). Foreign key: order_id -> order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Payment:
    """One payment record."""

    payment_id: int
    order_id: int
    payment_date: date
    amount: Decimal
    payment_method: str
