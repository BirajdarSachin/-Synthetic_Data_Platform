"""Invalid foreign-key rule - points a FK at an id that cannot exist.

The invalid value is always OUT OF RANGE: parent PKs are sequential 1..N
(ADR-0001), so any id > parent record count is guaranteed invalid - no
lookup of actual parent ids is needed, at any scale.

The inverse of the null rule's protection applies: ONLY identifier fields
(*_id) may be corrupted here, and the field -> parent mapping is EXPLICIT in
config (e.g. sales_rep_employee_id -> employee) because naming convention
alone cannot derive every parent (ADR-0022).
"""

import logging
import random
from dataclasses import fields, replace
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class InvalidForeignKeyRule:
    """Corrupts a FK field on a deterministic fraction of records."""

    def __init__(self, entity: str, seed: int,
                 fk_specs: dict[str, int], rate: float) -> None:
        """fk_specs maps field name -> parent record count (valid: 1..count)."""
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"invalid_fk_rate must be within [0, 1], got {rate}")
        if not fk_specs:
            raise ValueError("fk_specs must not be empty")
        self._entity = entity
        self._fk_specs = dict(fk_specs)
        self._rate = rate
        self._rng = random.Random(seed)
        self._validated = False

    def apply(self, records: Iterator[Any]) -> Iterator[Any]:
        """Yield records; a deterministic fraction carry an out-of-range FK."""
        corrupted = total = 0
        for record in records:
            total += 1
            if self._rate > 0.0 and self._rng.random() < self._rate:
                if not self._validated:
                    self._validate_against(record)
                field = self._rng.choice(list(self._fk_specs))
                max_valid = self._fk_specs[field]
                invalid_id = max_valid + self._rng.randint(1, 1000)
                corrupted += 1
                yield replace(record, **{field: invalid_id})
            else:
                yield record
        logger.info(
            "[QUALITY] entity=%s rule=invalid_fk corrupted=%s of=%s rate=%s",
            self._entity, corrupted, total, self._rate,
        )

    def _validate_against(self, record: Any) -> None:
        """Fail fast on the first record: every configured field must exist."""
        self._validated = True
        known = {f.name for f in fields(record)}
        unknown = [name for name in self._fk_specs if name not in known]
        if unknown:
            raise ValueError(
                f"invalid_fk_fields {unknown} do not exist on "
                f"{type(record).__name__}"
            )
