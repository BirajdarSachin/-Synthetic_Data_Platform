"""Deterministic seed derivation. Contract: ADR-0006.

One master seed from configuration. Each entity gets its own child seed,
derived with SHA-256 so it is stable across processes, machines and Python
versions. Generators only ever use random.Random instances created here -
never the global random module.
"""

import hashlib
import logging
import random

logger = logging.getLogger(__name__)


class SeedManager:
    """Derives stable child seeds from one master seed."""

    def __init__(self, master_seed: int) -> None:
        self._master_seed = master_seed
        logger.info("[SEED] master_seed=%s", master_seed)

    def child_seed(self, entity: str) -> int:
        """Return a stable 64-bit child seed for the given entity."""
        digest = hashlib.sha256(
            f"{self._master_seed}:{entity}".encode("utf-8")
        ).hexdigest()
        child = int(digest[:16], 16)
        logger.info("[SEED] child_seed entity=%s seed=%s", entity, child)
        return child

    def random_for(self, entity: str) -> random.Random:
        """Return a fresh Random instance dedicated to the given entity."""
        return random.Random(self.child_seed(entity))
