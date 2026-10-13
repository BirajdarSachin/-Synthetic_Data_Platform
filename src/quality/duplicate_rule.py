"""Duplicate record rule - re-emits a fraction of records as exact copies.

Realistic duplicates arrive LATER, not adjacent to their original (think of
an event delivered twice by an upstream retry). Selected records are
re-emitted after a random delay of 1..max_delay ORIGINAL records, using a
bounded re-emission buffer keyed by position. Memory stays O(max_delay) -
never O(stream) (ADR-0005, ADR-0022).

The engine chains this rule LAST (ADR-0022): duplicates must be exact copies
of the already-corrupted record.
"""

import logging
import random
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class DuplicateRule:
    """Re-emits a deterministic fraction of records after a short delay."""

    def __init__(self, entity: str, seed: int, rate: float,
                 max_delay: int = 10) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"duplicate_rate must be within [0, 1], got {rate}")
        if max_delay < 1:
            raise ValueError(f"max_delay must be >= 1, got {max_delay}")
        self._entity = entity
        self._rate = rate
        self._max_delay = max_delay
        self._rng = random.Random(seed)

    def apply(self, records: Iterator[Any]) -> Iterator[Any]:
        """Yield originals; re-emit a fraction after 1..max_delay originals."""
        emitted = duplicated = 0
        consumed = 0                        # originals consumed so far
        pending: dict[int, list[Any]] = {}  # emit_after -> records to re-emit
        for record in records:
            consumed += 1
            # Flush duplicates whose delay has elapsed (before this original).
            for duplicate in pending.pop(consumed, []):
                yield duplicate
                duplicated += 1
            yield record
            emitted += 1
            if self._rate > 0.0 and self._rng.random() < self._rate:
                delay = self._rng.randint(1, self._max_delay)
                pending.setdefault(consumed + delay, []).append(record)
        # Stream exhausted: flush trailing duplicates in deterministic order.
        for position in sorted(pending):
            for duplicate in pending[position]:
                yield duplicate
                duplicated += 1
        logger.info(
            "[QUALITY] entity=%s rule=duplicate duplicates=%s of=%s rate=%s",
            self._entity, duplicated, emitted, self._rate,
        )
