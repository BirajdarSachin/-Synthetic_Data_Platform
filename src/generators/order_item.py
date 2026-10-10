"""Order item generator - guarantees every order has at least one line.

Mechanics (ADR-0016): items 1..order_count belong to orders 1..order_count
(one line each), remaining items pick a random existing order. Hence
order_item.records >= order.records is REQUIRED (validated in main.py).
unit_price comes from the referenced product - parent attributes via
generation context (ADR-0015).
"""

import logging
import random
import time
from decimal import Decimal
from typing import Iterator

from src.models.order_item import OrderItem

logger = logging.getLogger(__name__)

_MIN_QUANTITY = 1
_MAX_QUANTITY = 5


class OrderItemGenerator:
    """Generates order items referencing real orders and real products."""

    ENTITY = "order_item"

    def __init__(self, seed: int, order_count: int,
                 products: list[tuple[int, Decimal]]) -> None:
        if order_count < 1:
            raise ValueError(f"order_count must be >= 1, got {order_count}")
        if not products:
            raise ValueError("products context must not be empty")
        self._order_count = order_count
        self._products = products
        self._rng = random.Random(seed)

    def generate(self, count: int) -> Iterator[OrderItem]:
        """Yield `count` items; count must be >= order_count."""
        if count < self._order_count:
            raise ValueError(
                f"item count ({count}) must be >= order_count ({self._order_count}) "
                "- every order needs at least one item (ADR-0016)"
            )
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        for item_id in range(1, count + 1):
            # First order_count items: exactly one line per order (deterministic).
            if item_id <= self._order_count:
                order_id = item_id
            else:
                order_id = self._rng.randint(1, self._order_count)
            product_id, unit_price = self._products[
                self._rng.randrange(len(self._products))
            ]
            yield OrderItem(
                order_item_id=item_id,
                order_id=order_id,
                product_id=product_id,
                quantity=self._rng.randint(_MIN_QUANTITY, _MAX_QUANTITY),
                unit_price=unit_price,
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )
