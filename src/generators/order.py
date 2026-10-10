"""Order generator - multi-parent FKs with a temporal business rule.

Receives actual customer (id, registration_date) pairs (ADR-0015 context):
picks a customer first, then an order_date in
[registration_date, reference_date] - so an order can never precede its
customer's registration. Rule satisfied BY CONSTRUCTION (ADR-0016).
Employee FK uses the id-range pattern (ADR-0014).
"""

import logging
import random
import time
from datetime import date
from typing import Iterator

from faker import Faker

from src.models.order import Order

logger = logging.getLogger(__name__)

# Deterministic status distribution: ~70% completed, ~20% pending, ~10% cancelled.
_COMPLETED_BELOW = 0.70
_PENDING_BELOW = 0.90


class OrderGenerator:
    """Generates order records referencing real customers and employees."""

    ENTITY = "order"

    def __init__(self, seed: int, reference_date: date,
                 customers: list[tuple[int, date]], employee_count: int) -> None:
        if not customers:
            raise ValueError("customers context must not be empty")
        if employee_count < 1:
            raise ValueError(f"employee_count must be >= 1, got {employee_count}")
        self._reference_date = reference_date
        self._customers = customers
        self._employee_count = employee_count
        self._rng = random.Random(seed)
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Order]:
        """Yield `count` orders with sequential ids starting at 1."""
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        for order_id in range(1, count + 1):
            customer_id, registration_date = self._customers[
                self._rng.randrange(len(self._customers))
            ]
            yield Order(
                order_id=order_id,
                customer_id=customer_id,
                sales_rep_employee_id=self._rng.randint(1, self._employee_count),
                order_date=self._faker.date_between(
                    start_date=registration_date, end_date=self._reference_date
                ),
                status=self._pick_status(),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )

    def _pick_status(self) -> str:
        roll = self._rng.random()
        if roll < _COMPLETED_BELOW:
            return "completed"
        if roll < _PENDING_BELOW:
            return "pending"
        return "cancelled"
