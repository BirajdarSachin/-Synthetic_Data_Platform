"""NULL injection rule - nulls one configured field of selected records.

Selection: per-record Bernoulli draw (rng.random() < rate) - streaming-safe
(ADR-0005), deterministic per seed (ADR-0006). A selected record gets exactly
ONE field nulled, chosen by the corruption rng. Records are frozen
(ADR-0005), so corruption emits new records via dataclasses.replace - the
input stream is never mutated.

Identifier fields (*_id) are rejected at config load: nulling identifiers is
schema-breaking, and deliberately invalid FKs are a separate Phase 4 rule.
"""

import logging
import random
from dataclasses import fields, replace
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class NullInjectionRule:
    """Nulls a configured field on a deterministic fraction of records."""

    def __init__(self, entity: str, seed: int,
                 null_fields: tuple[str, ...], rate: float) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"null_rate must be within [0, 1], got {rate}")
        if not null_fields:
            raise ValueError("null_fields must not be empty")
        self._entity = entity
        self._null_fields = tuple(null_fields)
        self._rate = rate
        self._rng = random.Random(seed)
        self._validated = False

    def apply(self, records: Iterator[Any]) -> Iterator[Any]:
        """Yield records; a deterministic fraction carry one nulled field."""
        nulled = total = 0
        for record in records:
            total += 1
            if self._rate > 0.0 and self._rng.random() < self._rate:
                if not self._validated:
                    self._validate_against(record)
                field = self._rng.choice(self._null_fields)
                nulled += 1
                yield replace(record, **{field: None})
            else:
                yield record
        logger.info(
            "[QUALITY] entity=%s rule=null nulled=%s of=%s rate=%s",
            self._entity, nulled, total, self._rate,
        )

    def _validate_against(self, record: Any) -> None:
        """Fail fast on the first record: every configured field must exist."""
        self._validated = True
        known = {f.name for f in fields(record)}
        unknown = [name for name in self._null_fields if name not in known]
        if unknown:
            raise ValueError(
                f"null_fields {unknown} do not exist on {type(record).__name__}"
            )
