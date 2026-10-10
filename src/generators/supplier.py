"""Supplier generator - yields Supplier records one at a time (ADR-0005).

Deterministic for a given child seed (ADR-0006). Suppliers have no dates in
schema v1.0, so no reference_date/history is needed yet; the constructor
grows additively when schema v1.1 adds them.
"""

import logging
import time
from typing import Iterator

from faker import Faker

from src.models.supplier import Supplier

logger = logging.getLogger(__name__)


class SupplierGenerator:
    """Generates supplier records deterministically."""

    ENTITY = "supplier"

    def __init__(self, seed: int) -> None:
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Supplier]:
        """Yield `count` suppliers with sequential ids starting at 1."""
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        for supplier_id in range(1, count + 1):
            yield Supplier(
                supplier_id=supplier_id,
                supplier_name=self._faker.company(),
                contact_name=self._faker.name(),
                contact_email=self._faker.company_email(),
                phone=self._faker.phone_number(),
                city=self._faker.city(),
                country=self._faker.country(),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )
