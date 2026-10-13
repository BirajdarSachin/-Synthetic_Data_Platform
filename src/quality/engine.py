"""Impurity engine - applies the configured rule chain to record streams.

Fixed chain per entity (ADR-0022):
  1. null injection   (one configured non-id field -> None)
  2. invalid FK       (one *_id field -> out-of-range id)
  3. duplication      (exact copy re-emitted after a delay)
Order matters: duplication runs LAST so duplicates are exact copies of the
already-corrupted record. Each rule has its own child seed
"{entity}:quality:{rule}" (ADR-0021) - independent, reproducible streams.
"""

import logging
from typing import Any, Iterator

from src.core.config import QualityConfig
from src.core.seed_manager import SeedManager
from src.quality.duplicate_rule import DuplicateRule
from src.quality.invalid_fk_rule import InvalidForeignKeyRule
from src.quality.null_rule import NullInjectionRule

logger = logging.getLogger(__name__)


class ImpurityEngine:
    """Applies the configured rule chain to per-entity record streams."""

    def __init__(self, seed_manager: SeedManager, quality: QualityConfig,
                 dataset_counts: dict[str, int] | None = None) -> None:
        self._seed_manager = seed_manager
        self._quality = quality
        self._dataset_counts = dataset_counts

    def apply(self, entity: str, records: Iterator[Any]) -> Iterator[Any]:
        """Return a (possibly corrupted) stream. Passthrough when unconfigured."""
        spec = self._quality.datasets.get(entity)
        if spec is None:
            return records
        stream = records
        if spec.null_fields and self._quality.null_rate > 0.0:
            stream = NullInjectionRule(
                entity=entity,
                seed=self._seed_manager.child_seed(f"{entity}:quality:null"),
                null_fields=spec.null_fields,
                rate=self._quality.null_rate,
            ).apply(stream)
        if spec.invalid_fk_fields and self._quality.invalid_fk_rate > 0.0:
            stream = InvalidForeignKeyRule(
                entity=entity,
                seed=self._seed_manager.child_seed(
                    f"{entity}:quality:invalid_fk"),
                fk_specs=self._parent_counts(entity, spec.invalid_fk_fields),
                rate=self._quality.invalid_fk_rate,
            ).apply(stream)
        if self._quality.duplicate_rate > 0.0:
            stream = DuplicateRule(
                entity=entity,
                seed=self._seed_manager.child_seed(
                    f"{entity}:quality:duplicate"),
                rate=self._quality.duplicate_rate,
            ).apply(stream)
        return stream

    def _parent_counts(self, entity: str,
                       fk_fields: dict[str, str]) -> dict[str, int]:
        """Resolve field -> parent record count (valid ids are 1..count)."""
        if not self._dataset_counts:
            raise ValueError(
                "dataset_counts required for invalid_fk rules - pass dataset "
                "record counts to ImpurityEngine (ADR-0022)"
            )
        missing = [p for p in fk_fields.values()
                   if p not in self._dataset_counts]
        if missing:
            raise ValueError(
                f"invalid_fk parent dataset(s) {missing} have no record count "
                f"(entity={entity}) - required to compute out-of-range ids"
            )
        return {field: self._dataset_counts[parent]
                for field, parent in fk_fields.items()}
