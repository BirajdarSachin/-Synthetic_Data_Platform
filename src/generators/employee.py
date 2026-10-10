"""Employee generator - independent entity, hire dates within history window."""

import logging
import time
from datetime import date
from typing import Iterator

from faker import Faker

from src.models.employee import Employee

logger = logging.getLogger(__name__)

_JOB_TITLES = (
    "Sales Representative", "Account Manager", "Sales Manager",
    "Regional Manager", "Inside Sales", "Support Specialist",
)


class EmployeeGenerator:
    """Generates employee records deterministically."""

    ENTITY = "employee"

    def __init__(self, seed: int, reference_date: date, history_days: int) -> None:
        self._reference_date = reference_date
        self._history_days = history_days
        self._rng_random = None  # placeholder removed; rng below
        import random  # local import kept minimal and explicit
        self._rng = random.Random(seed)
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Employee]:
        """Yield `count` employees with sequential ids starting at 1."""
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        earliest = self._reference_date - timedelta_days(self._history_days)
        for employee_id in range(1, count + 1):
            yield Employee(
                employee_id=employee_id,
                first_name=self._faker.first_name(),
                last_name=self._faker.last_name(),
                email=self._faker.unique.email(),
                job_title=self._rng.choice(_JOB_TITLES),
                hire_date=self._faker.date_between(
                    start_date=earliest, end_date=self._reference_date
                ),
                city=self._faker.city(),
                country=self._faker.country(),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )


def timedelta_days(days: int):
    """Small helper to avoid importing timedelta at module top twice."""
    from datetime import timedelta
    return timedelta(days=days)
