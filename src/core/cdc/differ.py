"""CDC differ (Milestone 6b, ADR-029 decision 1).

Pure primary-key diff between two per-window snapshots of one entity:

    key in current only   -> INSERT  (after image, before null)
    key in previous only  -> DELETE  (before image, after null)
    key in both, rows differ -> UPDATE (both images + sorted changed columns)
    key in both, rows equal  -> no event (counted as unchanged)

The differ is IO-free: it receives rows as {column: str} dicts (already
normalized by the runner: every cell str, missing cells "") and returns a
CdcDiff. Determinism contract: events are ordered by ascending primary key
and `seq` is assigned 1..N in that order, so the same two windows always
produce byte-identical output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.core.cdc.events import (
    CdcError,
    CdcEvent,
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
)

Row = Mapping[str, str]


class CdcConfigError(CdcError):
    """Config or snapshot-semantics problem (invalid config, duplicate or
    empty primary key, header drift). Fail-fast, message is the diagnostic."""


@dataclass(frozen=True)
class CdcDiff:
    """Outcome of one window pair. The ledger identity
    inserts - deletes == rows_curr - rows_prev is a mathematical property
    of the partition-by-key below; the runner re-asserts it as a contract
    (ADR-029 decision 6) so a future refactor cannot break it silently."""

    entity: str
    window: str
    prev_window: str | None
    events: tuple[CdcEvent, ...]
    inserts: int
    updates: int
    deletes: int
    unchanged: int
    rows_prev: int
    rows_curr: int

    @property
    def events_total(self) -> int:
        return self.inserts + self.updates + self.deletes


def _index_snapshot(
    rows: list[dict[str, str]], entity: str, primary_key: str, side: str
) -> dict[str, tuple[int, dict[str, str]]]:
    """Map pk -> (row_number, row) for one window, enforcing snapshot validity.

    Row numbers are 1-based data-row numbers (header excluded), matching the
    DQ harness convention. An empty primary key is a data error, not a key:
    fail fast naming the row.
    """
    indexed: dict[str, tuple[int, dict[str, str]]] = {}
    for number, row in enumerate(rows, start=1):
        pk = row.get(primary_key, "")
        if pk == "":
            raise CdcConfigError(
                f"{entity}: empty primary key {primary_key!r} in the {side} "
                f"snapshot at data row {number} -- CDC needs one non-empty "
                f"key per row; fix the source data or the cdc.entities "
                f"primary_key"
            )
        if pk in indexed:
            raise CdcConfigError(
                f"{entity}: duplicate primary key {pk!r} in the {side} "
                f"snapshot (data rows {indexed[pk][0]} and {number}) -- CDC "
                f"compares per-window SNAPSHOTS, one row per key; "
                f"re-generate the window or fix cdc.entities primary_key"
            )
        indexed[pk] = (number, row)
    return indexed


def _changed_columns(
    before: Row, after: Row, columns: tuple[str, ...]
) -> tuple[str, ...]:
    """Sorted tuple of columns whose value differs between the two images.

    `columns` (config whitelist) restricts WHICH columns are compared; an
    empty tuple means every column of the union of both rows. Columns only
    present on one side compare as "" on the other (ADR-029 decision 4).
    """
    if columns:
        names = columns
    else:
        names = tuple(sorted(set(before) | set(after)))
    return tuple(
        c for c in names if before.get(c, "") != after.get(c, "")
    )


def diff_windows(
    prev_rows: list[dict[str, str]] | None,
    curr_rows: list[dict[str, str]],
    *,
    entity: str,
    primary_key: str,
    columns: tuple[str, ...] = (),
    prev_window: str | None = None,
    window: str,
) -> CdcDiff:
    """Diff one entity's previous snapshot (None = initial load) against the
    current one and return the event ledger.

    `columns` restricts change DETECTION only -- before/after images always
    carry the full rows, so consumers can replay state regardless of the
    whitelist (ADR-029 decision 2).
    """
    curr_index = _index_snapshot(curr_rows, entity, primary_key, "current")
    prev_index: dict[str, tuple[int, dict[str, str]]] = (
        {} if prev_rows is None
        else _index_snapshot(prev_rows, entity, primary_key, "previous")
    )

    inserts = updates = deletes = unchanged = 0
    raw: list[tuple[str, str, tuple[str, ...], dict[str, str] | None, dict[str, str] | None]] = []

    for pk in sorted(set(prev_index) | set(curr_index)):
        prev_entry = prev_index.get(pk)
        curr_entry = curr_index.get(pk)
        if prev_entry is None:
            inserts += 1
            raw.append((pk, OP_INSERT, (), None, curr_entry[1]))
        elif curr_entry is None:
            deletes += 1
            raw.append((pk, OP_DELETE, (), prev_entry[1], None))
        else:
            before_row, after_row = prev_entry[1], curr_entry[1]
            changed = _changed_columns(before_row, after_row, columns)
            if changed:
                updates += 1
                raw.append((pk, OP_UPDATE, changed, before_row, after_row))
            else:
                unchanged += 1

    events = tuple(
        CdcEvent(
            seq=position,
            op=op,
            entity=entity,
            pk=pk,
            window=window,
            prev_window=prev_window,
            changed=changed,
            before=before,
            after=after,
        )
        for position, (pk, op, changed, before, after) in enumerate(raw, start=1)
    )
    return CdcDiff(
        entity=entity,
        window=window,
        prev_window=prev_window,
        events=events,
        inserts=inserts,
        updates=updates,
        deletes=deletes,
        unchanged=unchanged,
        rows_prev=len(prev_index),
        rows_curr=len(curr_index),
    )
