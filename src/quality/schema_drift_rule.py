"""Schema drift rule - emits records with a DIFFERENT SHAPE than the model.

Mechanism: on the first corrupted record the rule resolves ONE frozen
dataclass via dataclasses.make_dataclass - the model's fields minus the
dropped ones plus the extra marker fields. The class is cached at MODULE
level, keyed by (record type, dropped fields, extra fields): the drifted
shape of a model is a well-defined type shared across rule instances.
This matters because dataclass __eq__ compares CLASS IDENTITY first -
two make_dataclass calls with identical specs yield distinct, non-equal
classes - and because shared caching prevents unbounded class creation.

Format behavior (ADR-0023):
  CSV   - dropped field -> empty cell (csv restval); extra field -> loud error
  JSON  - dropped key absent / extra key present (renders naturally)
  Parquet - drift is VISIBLE IN THE FILE SCHEMA (pinned from the first
          record's class): a dropped column is absent, an extra column exists.
          Reading the schema IS the drift detector.
"""

import logging
import random
import typing
from dataclasses import asdict, make_dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

EXTRA_MARKER = "__unexpected__"

# Key = (record type, dropped fields, extra fields). One drifted shape = one class.
_drift_class_cache: dict[tuple[type, tuple[str, ...], tuple[str, ...]], type] = {}


def _drift_class_for(record_type: type,
                     drop: tuple[str, ...],
                     extra: tuple[str, ...]) -> type:
    """Return the cached drifted dataclass for this exact shape."""
    key = (record_type, drop, extra)
    cached = _drift_class_cache.get(key)
    if cached is not None:
        return cached
    hints = typing.get_type_hints(record_type)
    spec = [(name, hints[name]) for name in hints if name not in drop]
    spec += [(name, str) for name in extra]
    cls = make_dataclass(f"{record_type.__name__}Drifted", spec, frozen=True)
    _drift_class_cache[key] = cls
    return cls


class SchemaDriftRule:
    """Changes the record shape on a deterministic fraction of records."""

    def __init__(self, entity: str, seed: int,
                 drop_fields: tuple[str, ...],
                 extra_fields: tuple[str, ...], rate: float) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"drift_rate must be within [0, 1], got {rate}")
        if not drop_fields and not extra_fields:
            raise ValueError("schema drift needs drop_fields or extra_fields")
        self._entity = entity
        self._drop = tuple(drop_fields)
        self._extra = tuple(extra_fields)
        self._rate = rate
        self._rng = random.Random(seed)
        self._validated = False

    def apply(self, records: Iterator[Any]) -> Iterator[Any]:
        """Yield records; a deterministic fraction have a drifted shape."""
        drifted = total = 0
        for record in records:
            total += 1
            if self._rate > 0.0 and self._rng.random() < self._rate:
                if not self._validated:
                    self._validate(type(record))
                drifted += 1
                yield self._transform(record)
            else:
                yield record
        logger.info(
            "[QUALITY] entity=%s rule=schema_drift drifted=%s of=%s rate=%s",
            self._entity, drifted, total, self._rate,
        )

    def _validate(self, record_type: type) -> None:
        """Fail fast on the first record: field names must be coherent."""
        self._validated = True
        hints = typing.get_type_hints(record_type)
        unknown = [f for f in self._drop if f not in hints]
        if unknown:
            raise ValueError(
                f"drop_fields {unknown} do not exist on {record_type.__name__}"
            )
        collisions = [f for f in self._extra if f in hints]
        if collisions:
            raise ValueError(
                f"extra_fields {collisions} already exist on "
                f"{record_type.__name__}"
            )

    def _transform(self, record: Any) -> Any:
        values = asdict(record)
        for name in self._drop:
            values.pop(name, None)
        for name in self._extra:
            values[name] = EXTRA_MARKER
        drift_cls = _drift_class_for(type(record), self._drop, self._extra)
        return drift_cls(**values)