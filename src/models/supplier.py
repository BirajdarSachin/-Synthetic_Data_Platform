"""Supplier entity - the data contract for the supplier dataset.

SCHEMA_VERSION rules (ADR-0007): additive column -> minor bump;
removal/type change -> major bump, requires approval.
Field order below IS the CSV column order (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Supplier:
    """One supplier record. Parent of product (product.supplier_id)."""

    supplier_id: int
    supplier_name: str
    contact_name: str
    contact_email: str
    phone: str
    city: str
    country: str
