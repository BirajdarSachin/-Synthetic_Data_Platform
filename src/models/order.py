"""Order entity - a customer purchase, taken by an employee.

Business rules encoded at generation (ADR-0016):
  * order_date >= the ordering customer's registration_date (ADR-0015 context)
  * status in {completed, pending, cancelled}; only completed orders are paid
Foreign keys: customer_id -> customer, sales_rep_employee_id -> employee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = "1.0"

ALLOWED_STATUSES = ("completed", "pending", "cancelled")


@dataclass(frozen=True)
class Order:
    """One order record. Parent of order_item and payment."""

    order_id: int
    customer_id: int
    sales_rep_employee_id: int
    order_date: date
    status: str
