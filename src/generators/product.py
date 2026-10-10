"""Product generator - yields Product records referencing real suppliers.

Referential integrity pattern (ADR-0014): a child generator receives the
parent id range (supplier_count) and picks FK values with its own seeded
rng from 1..supplier_count. This works BECAUSE primary keys are sequential
integers (ADR-0001) - supplier ids are exactly 1..supplier_count.
"""

import logging
import random
import time
from datetime import date
from decimal import Decimal
from typing import Iterator

from faker import Faker

from src.models.product import Product

logger = logging.getLogger(__name__)

# Business vocabulary lives in the generator (generation policy),
# not in the model (pure structure).
_CATEGORIES = (
    "Electronics", "Furniture", "Office Supplies",
    "Apparel", "Food & Beverage", "Sports", "Toys",
)
_MIN_PRICE = 5.0
_MAX_PRICE = 2000.0


class ProductGenerator:
    """Generates product records deterministically, FK-valid by construction."""

    ENTITY = "product"

    def __init__(self, seed: int, supplier_count: int) -> None:
        if supplier_count < 1:
            raise ValueError(
                f"supplier_count must be >= 1 to generate products, got {supplier_count}"
            )
        self._supplier_count = supplier_count
        self._rng = random.Random(seed)
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Product]:
        """Yield `count` products; every supplier_id is a real supplier id."""
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        for product_id in range(1, count + 1):
            yield Product(
                product_id=product_id,
                product_name=(
                    f"{self._faker.color_name()} {self._faker.word().title()}"
                ),
                category=self._rng.choice(_CATEGORIES),
                unit_price=self._random_price(),
                supplier_id=self._rng.randint(1, self._supplier_count),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )

    def _random_price(self) -> Decimal:
        """Deterministic price in [5.00, 2000.00], always exactly 2 decimals."""
        raw = round(self._rng.uniform(_MIN_PRICE, _MAX_PRICE), 2)
        return Decimal(str(raw)).quantize(Decimal("0.01"))
