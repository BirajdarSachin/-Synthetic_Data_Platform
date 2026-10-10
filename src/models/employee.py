"""Employee entity - sales staff who take orders.

SCHEMA_VERSION rules (ADR-0007): additive column -> minor bump;
removal/type change -> major bump, requires approval.
Field order below IS the CSV column order (ADR-0004).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Employee:
    """One employee record. Referenced by order.sales_rep_employee_id."""

    employee_id: int
    first_name: str
    last_name: str
    email: str
    job_title: str
    hire_date: date
    city: str
    country: str
