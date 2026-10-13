"""Impurity engine - applies configured corruption rules to record streams.

The engine is constructed once per run from the quality config and sits
between generators and writers in main.py. Per entity it holds an
independent rule with its own child seed, keyed "{entity}:quality:null"
(ADR-0021) - so corruption never consumes a generator's RNG stream and
clean output is bit-for-bit unaffected by the quality section.

Corruption is LOCAL: main.py applies the engine at the write boundary,
after all generation context (ADR-0015) was extracted from clean records.
"""

import logging
from typing import Any, Iterator

from src.core.config import QualityConfig
from src.quality.null_rule import NullInjectionRule
from src.core.seed_manager import SeedManager

logger = logging.getLogger(__name__)


class ImpurityEngine:
    """Applies quality rules to per-entity record streams."""

    def __init__(self, seed_manager: SeedManager, quality: QualityConfig) -> None:
        self._seed_manager = seed_manager
        self._quality = quality

    def apply(self, entity: str, records: Iterator[Any]) -> Iterator[Any]:
        """Return a (possibly corrupted) stream. Passthrough when unconfigured."""
        spec = self._quality.datasets.get(entity)
        if spec is None:
            return records
        seed = self._seed_manager.child_seed(f"{entity}:quality:null")
        rule = NullInjectionRule(
            entity=entity,
            seed=seed,
            null_fields=spec.null_fields,
            rate=self._quality.null_rate,
        )
        return rule.apply(records)
