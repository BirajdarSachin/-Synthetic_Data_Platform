"""Customer entity - the data contract for the customer dataset.

SCHEMA_VERSION rules (ADR-0007):
  * additive column        -> minor bump (1.0 -> 1.1)
  * removal / type change  -> major bump, requires approval
Field order below IS the CSV column order (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Customer:
    """One customer record. Frozen: records are values, never mutated."""

    customer_id: int
    first_name: str
    last_name: str
    email: str
    phone: str
    city: str
    country: str
    registration_date: date
