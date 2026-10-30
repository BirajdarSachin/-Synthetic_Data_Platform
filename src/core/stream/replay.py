"""Replay fold over CDC journals (Milestone 6c, ADR-030).

PURE layer: no filesystem access, no config. Parses one journal line into a
JournalEvent, orders events by (window, seq), and folds them into keyed
state with STRICT contract checks:

    INSERT  -> key must NOT be live      (a live key was never deleted)
    UPDATE  -> key must be live          (nothing to patch otherwise)
    DELETE  -> key must be live, after must be null
    INSERT/UPDATE -> after must be a full {column: str} row image

The fold is the defining property of an event stream (ADR-030 decision 2):
state == fold(events), so any consumer can rebuild any window's snapshot
from the journal chain alone. Resurrection is legal -- a DELETE removes the
key from state, so a later INSERT is a fresh birth, not a duplicate.

The column ORDER of the replayed rows is taken from the first full row
image seen (JSON objects preserve insertion order); every later image must
carry the SAME column SET -- order differences are tolerated, set
differences fail fast.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

OP_INSERT = "INSERT"
OP_UPDATE = "UPDATE"
OP_DELETE = "DELETE"
OP_NAMES = (OP_INSERT, OP_UPDATE, OP_DELETE)


class StreamError(Exception):
    """Base class for every stream failure (config, input data, internal)."""


class StreamConfigError(StreamError):
    """Config-shaped failure (bad yaml block, missing key, bad value)."""


@dataclass(frozen=True)
class JournalEvent:
    """One parsed change event from a CDC journal line.

    `source` is the "<file>:<line>" label used in every diagnostic, so a
    broken journal line is clickable back to its byte position.
    """

    window: str
    seq: int
    op: str
    pk: str
    after: Mapping[str, str] | None
    source: str


def parse_journal_event(obj: Any, source: str, entity: str) -> JournalEvent:
    """Validate one decoded JSON object against the ADR-029 wire contract."""
    if not isinstance(obj, dict):
        raise StreamError(f"{source}: journal line must be a JSON object")
    entity_field = obj.get("entity")
    if entity_field is not None and entity_field != entity:
        raise StreamError(
            f"{source}: event entity {entity_field!r} does not match "
            f"journal entity {entity!r} (misplaced file?)"
        )
    op = obj.get("op")
    if op not in OP_NAMES:
        raise StreamError(
            f"{source}: op must be one of {', '.join(OP_NAMES)}; got {op!r}"
        )
    pk = obj.get("pk")
    if not isinstance(pk, str) or not pk:
        raise StreamError(f"{source}: pk must be a non-empty string")
    window = obj.get("window")
    if not isinstance(window, str) or not window:
        raise StreamError(f"{source}: window must be a non-empty string")
    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise StreamError(f"{source}: seq must be a positive integer; got {seq!r}")
    after = obj.get("after")
    if op == OP_DELETE:
        if after is not None:
            raise StreamError(f"{source}: DELETE must carry after=null")
    else:
        if not isinstance(after, dict):
            raise StreamError(
                f"{source}: {op} must carry a full row image in 'after'"
            )
        for key, value in after.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise StreamError(
                    f"{source}: after images hold {{column: str}} cells "
                    f"(reader-boundary normalization, ADR-029); bad cell {key!r}"
                )
    return JournalEvent(
        window=window, seq=seq, op=op, pk=pk, after=after, source=source
    )


def order_events(events: Iterable[JournalEvent]) -> list[JournalEvent]:
    """Sort by (window, seq) and reject duplicate positions.

    Window keys are ISO dates or months, so lexicographic order IS
    chronological order (same convention as the window engine). Duplicate
    (window, seq) means two producers wrote one position -- unrecoverable,
    fail fast naming both sources.
    """
    ordered = sorted(events, key=lambda e: (e.window, e.seq))
    seen: dict[tuple[str, int], JournalEvent] = {}
    for event in ordered:
        position = (event.window, event.seq)
        if position in seen:
            raise StreamError(
                f"duplicate journal position (window={event.window!r}, "
                f"seq={event.seq}): {seen[position].source} and {event.source}"
            )
        seen[position] = event
    return ordered


class StreamReplay:
    """Ordered fold of journal events into {pk: {column: str}} state."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, str]] = {}
        self.header: list[str] | None = None
        self.header_set: frozenset[str] | None = None
        self.counts = {OP_INSERT: 0, OP_UPDATE: 0, OP_DELETE: 0}

    def _register_image(self, event: JournalEvent) -> dict[str, str]:
        if event.after is None:
            raise StreamError(
                f"{event.source}: {event.op} must carry a full row image in 'after'"
            )
        image = dict(event.after)
        if self.header is None:
            self.header = list(image.keys())
            self.header_set = frozenset(self.header)
        elif set(image.keys()) != self.header_set:
            expected = sorted(self.header_set - set(image.keys()))
            unexpected = sorted(set(image.keys()) - self.header_set)
            raise StreamError(
                f"{event.source}: row image for pk {event.pk!r} breaks the "
                f"column set established by the first full image "
                f"(missing: {expected}, unexpected: {unexpected})"
            )
        return image

    def apply(self, event: JournalEvent) -> None:
        if event.op == OP_INSERT:
            image = self._register_image(event)
            if event.pk in self.state:
                raise StreamError(
                    f"{event.source}: INSERT for live pk {event.pk!r} "
                    f"(the journal never deleted it -- broken chain)"
                )
            self.state[event.pk] = image
        elif event.op == OP_UPDATE:
            image = self._register_image(event)
            if event.pk not in self.state:
                raise StreamError(
                    f"{event.source}: UPDATE for unknown pk {event.pk!r} "
                    f"(no prior INSERT -- broken chain)"
                )
            self.state[event.pk] = image
        else:  # DELETE
            if event.after is not None:
                raise StreamError(
                    f"{event.source}: DELETE must carry after=null"
                )
            if event.pk not in self.state:
                raise StreamError(
                    f"{event.source}: DELETE for unknown pk {event.pk!r} "
                    f"(no prior INSERT -- broken chain)"
                )
            del self.state[event.pk]
        self.counts[event.op] += 1

    def apply_all(self, events: Iterable[JournalEvent]) -> None:
        for event in events:
            self.apply(event)


def fold_events(events: Iterable[JournalEvent]) -> StreamReplay:
    """Order and fold; the module's whole contract in one call."""
    replay = StreamReplay()
    replay.apply_all(order_events(events))
    return replay
