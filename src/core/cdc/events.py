"""CDC event model (Milestone 6b, ADR-029).

One JSON object per line is the change log contract:

    {"seq": 1, "op": "INSERT", "entity": "order", "pk": "o-1",
     "window": "2026-01-15", "prev_window": "2026-01-14",
     "changed": [], "before": null, "after": {...}}

Field order above is the wire order (json.dumps preserves dict insertion
order). Ops are canonical uppercase INSERT / UPDATE / DELETE. `seq` is
1-based per entity per run, assigned in ascending primary-key order by the
differ, so re-runs are byte-identical and consumers can sort/merge safely.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping


OP_INSERT = "INSERT"
OP_UPDATE = "UPDATE"
OP_DELETE = "DELETE"
OP_NAMES = (OP_INSERT, OP_UPDATE, OP_DELETE)


class CdcError(Exception):
    """Base class for every CDC failure (config, input data, internal)."""


@dataclass(frozen=True)
class CdcEvent:
    """One change event. Row images are plain {column: str} dicts exactly
    as the source files hold them (cells already normalized to str, missing
    cells "") — the same reader-boundary philosophy as ADR-028."""

    seq: int
    op: str
    entity: str
    pk: str
    window: str
    prev_window: str | None
    changed: tuple[str, ...]
    before: Mapping[str, str] | None
    after: Mapping[str, str] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "op": self.op,
            "entity": self.entity,
            "pk": self.pk,
            "window": self.window,
            "prev_window": self.prev_window,
            "changed": list(self.changed),
            "before": dict(self.before) if self.before is not None else None,
            "after": dict(self.after) if self.after is not None else None,
        }

    def to_jsonl(self) -> str:
        """One compact JSON line (with trailing newline) — the wire format."""
        return json.dumps(self.to_dict()) + "\n"
