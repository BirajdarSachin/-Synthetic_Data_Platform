"""Out-of-range rule - negates a numeric field on selected records.

Strategy: value -> -abs(value). Type-preserving (Decimal stays Decimal, int
stays int), deterministic, and realistic (negative quantity/price is the
classic invalid row). Identifier fields are rejected at config load - id
corruption is the invalid_fk rule's job (protection symmetry, ADR-0023).
"""

import logging
import random
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_NUMERIC = (int, float, Decimal)


class OutOfRangeRule:
    """Corrupts a numeric field on a deterministic fraction of records."""

    def __init__(self, entity: str, seed: int,
                 target_fields: tuple[str, ...], rate: float) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"out_of_range_rate must be within [0, 1], got {rate}")
        if not target_fields:
            raise ValueError("out_of_range_fields must not be empty")
        self._entity = entity
        self._target_fields = tuple(target_fields)
        self._rate = rate
        self._rng = random.Random(seed)
        self._validated = False

    def apply(self, records: Iterator[Any]) -> Iterator[Any]:
        """Yield records; a deterministic fraction carry a negated value."""
        corrupted = total = 0
        for record in records:
            total += 1
            if self._rate > 0.0 and self._rng.random() < self._rate:
                if not self._validated:
                    self._validate_against(record)
                field = self._rng.choice(list(self._target_fields))
                value = getattr(record, field)
                corrupted += 1
                yield replace(record, **{field: -abs(value)})
            else:
                yield record
        logger.info(
            "[QUALITY] entity=%s rule=out_of_range corrupted=%s of=%s rate=%s",
            self._entity, corrupted, total, self._rate,
        )

    def _validate_against(self, record: Any) -> None:
        """Fail fast on the first record: fields exist and are numeric."""
        self._validated = True
        known = {f.name for f in fields(record)}
        unknown = [f for f in self._target_fields if f not in known]
        if unknown:
            raise ValueError(
                f"out_of_range_fields {unknown} do not exist on "
                f"{type(record).__name__}"
            )
        non_numeric = [
            f for f in self._target_fields
            if not isinstance(getattr(record, f), _NUMERIC)
            or isinstance(getattr(record, f), bool)
        ]
        if non_numeric:
            raise ValueError(
                f"out_of_range_fields {non_numeric} must be numeric fields "
                f"(int/float/Decimal) on {type(record).__name__} - negation "
                "is only defined for numbers (ADR-0023)"
            )
