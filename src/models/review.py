"""Review data contract (ADR-0014/0015/0016).

Reviews reference products purchased by customers in completed orders, with at
most one review per customer/product pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Review:
    """One review of a purchased product by its customer."""

    review_id: int
    customer_id: int
    product_id: int
    rating: int
    review_date: date
    review_text: str
    
