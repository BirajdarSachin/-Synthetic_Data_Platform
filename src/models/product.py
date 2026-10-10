"""Product entity - the data contract for the product dataset.

unit_price is decimal.Decimal (ADR-0002): float money is forbidden.
supplier_id is a foreign key into the supplier dataset (Milestone 3a).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Product:
    """One product record. Child of supplier (FK: supplier_id)."""

    product_id: int
    product_name: str
    category: str
    unit_price: Decimal
    supplier_id: int
