"""Customer generator - yields Customer records one at a time (ADR-0005).

Uses a seeded faker instance so output is fully deterministic for a given
child seed (ADR-0006). Note: values can change when the faker library version
changes; the run manifest records that version.
"""

import logging
import random
import time
from datetime import date, timedelta
from typing import Iterator

from faker import Faker

from src.models.customer import Customer

logger = logging.getLogger(__name__)

_REGISTRATION_HISTORY_DAYS = 3 * 365  # registrations spread over the last 3 years


class CustomerGenerator:
    """Generates customer records deterministically."""

    ENTITY = "customer"

    def __init__(self, seed: int, reference_date: date) -> None:
        self._reference_date = reference_date
        # Two independent deterministic streams from one child seed:
        # faker has its own internal RNG, so we seed it explicitly too.
        self._rng = random.Random(seed)
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Customer]:
        """Yield `count` customer records with sequential ids starting at 1.

        Note: this is a generator function - the body runs lazily. The
        'started' log fires when the first record is pulled, 'completed'
        when the stream is fully consumed. write_csv consumes the full
        stream, so both fire during a normal run.
        """
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        earliest = self._reference_date - timedelta(days=_REGISTRATION_HISTORY_DAYS)
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
