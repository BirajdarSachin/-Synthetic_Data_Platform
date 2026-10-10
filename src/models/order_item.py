"""Order item entity - one product line within an order.

Business rules (ADR-0016): every order has >= 1 item; unit_price equals the
referenced product's current unit_price; quantity >= 1.
Foreign keys: order_id -> order, product_id -> product.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class OrderItem:
    """One order line record."""

    order_item_id: int
    order_id: int
    product_id: int
    quantity: int
    unit_price: Decimal
