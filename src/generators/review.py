"""Review generator - creates reviews for customer/product purchases."""

import logging
import random
import time
from datetime import date, timedelta
from typing import Iterator

from faker import Faker

from src.models.review import Review

logger = logging.getLogger(__name__)


class ReviewGenerator:
    """Generates at most one review for each purchased customer/product pair."""

    ENTITY = "review"

    def __init__(
        self,
        seed: int,
        reference_date: date,
        purchases: list[tuple[int, int, date]],
    ) -> None:
        """Receive purchases as (customer_id, product_id, purchase_date) tuples."""
        self._reference_date = reference_date
        latest_purchase_by_pair: dict[tuple[int, int], date] = {}
        for customer_id, product_id, purchase_date in purchases:
            if purchase_date > reference_date:
                raise ValueError(
                    f"purchase date {purchase_date} is after reference date "
                    f"{reference_date}"
                )
            pair = (customer_id, product_id)
            latest_purchase_by_pair[pair] = max(
                purchase_date, latest_purchase_by_pair.get(pair, purchase_date)
            )
        self._purchases = [
            (customer_id, product_id, purchase_date)
            for (customer_id, product_id), purchase_date
            in sorted(latest_purchase_by_pair.items())
            if purchase_date < reference_date
        ]
        self._rng = random.Random(seed)
        self._faker = Faker()
        self._faker.seed_instance(seed)

    def generate(self, count: int) -> Iterator[Review]:
        """Yield up to one review per purchased pair, after its latest purchase."""
        if count > len(self._purchases):
            raise ValueError(
                f"review count ({count}) exceeds eligible unique purchases "
                f"({len(self._purchases)})"
            )
        logger.info("[GENERATION] entity=%s started records=%s", self.ENTITY, count)
        start = time.perf_counter()
        selected = self._rng.sample(self._purchases, count)
        for review_id, (customer_id, product_id, purchase_date) in enumerate(
            selected, start=1
        ):
            first_review_date = purchase_date + timedelta(days=1)
            review_date = first_review_date + timedelta(
                days=self._rng.randint(
                    0, (self._reference_date - first_review_date).days
                )
            )
            yield Review(
                review_id=review_id,
                customer_id=customer_id,
                product_id=product_id,
                rating=self._rng.randint(1, 5),
                review_date=review_date,
                review_text=self._faker.text(max_nb_chars=200),
            )
        duration = time.perf_counter() - start
        logger.info(
            "[GENERATION] entity=%s completed records=%s duration_s=%.2f",
            self.ENTITY, count, duration,
        )
