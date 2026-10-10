"""Payment generator - one payment per completed order, amount = order total.

Receives completed orders as (order_id, order_date) pairs and a totals dict
{order_id: Decimal} computed from order items in main.py (ADR-0015 context).
Orders are sampled WITHOUT replacement -> at most one payment per order.
Raises ValueError when count exceeds available completed orders (ADR-0016).
"""

import logging
import random
import time
from datetime import date
from decimal import Decimal
from typing import Iterator

from faker import Faker

from src.models.payment import Payment

logger = logging.getLogger(__name__)

_PAYMENT_METHODS = ("credit_card", "bank_transfer", "paypal", "cash")
_MAX_PAYMENT_LAG_DAYS = 5


class PaymentGenerator:
    """Generates payments for completed orders with exact order totals."""

    ENTITY = "payment"

    def __init__(self, seed: int, reference_date: date,
                 completed_orders: list[tuple[int, date]],
                 totals: dict[int, Decimal]) -> None:
        if not completed_orders:
            raise ValueError(
                "no completed orders available - payments require completed orders"
            )
        self._reference_date = reference_date
        self._completed_orders = completed_orders
        self._totals = totals
        self._rng = random.Random(seed)
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Payment]:
        """Yield `count` payments; count must not exceed completed orders."""
        if count > len(self._completed_orders):
            raise ValueError(
                f"payment count ({count}) exceeds completed orders "
                f"({len(self._completed_orders)}) - one payment per order (ADR-0016)"
            )
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        picked = self._rng.sample(range(len(self._completed_orders)), count)
        for payment_id, index in enumerate(picked, start=1):
            order_id, order_date = self._completed_orders[index]
            yield Payment(
                payment_id=payment_id,
                order_id=order_id,
                payment_date=self._faker.date_between(
                    start_date=order_date, end_date=self._reference_date
                ),
                amount=self._totals[order_id],
                payment_method=self._rng.choice(_PAYMENT_METHODS),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )
