"""Customer generator - yields Customer records one at a time (ADR-0005).

Uses a seeded faker instance so output is fully deterministic for a given
child seed (ADR-0006). Note: values can change when the faker library version
changes; the run manifest records that version.
"""

import logging
import time
from datetime import date, timedelta
from typing import Iterator

from faker import Faker

from src.models.customer import Customer

logger = logging.getLogger(__name__)


class CustomerGenerator:
    """Generates customer records deterministically."""

    ENTITY = "customer"

    def __init__(self, seed: int, reference_date: date, history_days: int) -> None:
        self._reference_date = reference_date
        self._history_days = history_days
        # faker has its own internal RNG, so we seed it explicitly (ADR-0006).
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Customer]:
        """Yield `count` customer records with sequential ids starting at 1.

        Registration dates fall within the window
        [reference_date - history_days, reference_date] (ADR-0012).

        Note: this is a generator function - the body runs lazily. The
        'started' log fires when the first record is pulled, 'completed'
        when the stream is fully consumed. write_csv consumes the full
        stream, so both fire during a normal run.
        """
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        earliest = self._reference_date - timedelta(days=self._history_days)
        for customer_id in range(1, count + 1):
            yield Customer(
                customer_id=customer_id,
                first_name=self._faker.first_name(),
                last_name=self._faker.last_name(),
                email=self._faker.unique.email(),
                phone=self._faker.phone_number(),
                city=self._faker.city(),
                country=self._faker.country(),
                registration_date=self._faker.date_between(
                    start_date=earliest, end_date=self._reference_date
                ),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )
