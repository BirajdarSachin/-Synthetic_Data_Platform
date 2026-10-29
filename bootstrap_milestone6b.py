#!/usr/bin/env python3
"""bootstrap_milestone6b.py -- incremental change stream + CDC change log
(Milestone 6b, ADR-029).

House-style bootstrap:
  NEW_FILES    -> create-if-missing, [HEAL] empty crash artifacts, NEVER
                  overwrite. 6b is a pure ADDITION: 8 new files (src/core/
                  cdc/ module, cdc_main.py, configs/cdc_base.yaml, 2 test
                  files). Nothing you already have is modified.
  APPEND_FILES -> docs/decisions.md ADR-029, append-only, idempotent by
                  marker (ADR-024..028 content preserved byte-for-byte).

What it delivers (roadmap Phase 5 -- Milestone 6b):
  src/core/cdc/        diff-based CDC over windowed output trees: reads two
                       reference_date/event_date windows of an entity, diffs
                       by primary key, writes INSERT/UPDATE/DELETE events as
                       JSONL with full before/after images (ADR-029).
  cdc_main.py          CLI: --window W [--prev-window P], auto-pairing,
                       initial-load semantics, [CDC] log tag, exit 0/1/130.
  configs/cdc_base.yaml  customer/order/order_item wired; source_tree
                       data/generated, output_root data/cdc.
  tests/               39 new tests (differ + runner). The 103 DQ tests are
                       untouched; full suite expectation after install: 142.

Hard preconditions: the project skeleton (main.py, src/, docs/, tests/).
CDC is independent of the DQ harness -- no 7x requirement.

Exit codes: 0 = delivered/skipped cleanly, 1 = precondition or write failure,
            130 = interrupted (Ctrl+C).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

NEW_FILES: list[tuple[str, str, str]] = [
    (
        'src/core/cdc/__init__.py',
        r'''"""CDC — incremental change stream + change log (Milestone 6b).

Consumer-side module (ADR-025 precedent): reads windowed output trees and
config only; never imports generator, writer, or impurity internals.
See docs/decisions.md ADR-029 for the diff-based design, the JSONL event
contract, and the guards.
"""
''',
        'CDC package marker (ADR-029)',
    ),
    (
        'src/core/cdc/events.py',
        r'''"""CDC event model (Milestone 6b, ADR-029).

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
''',
        'CDC event model + JSONL wire format',
    ),
    (
        'src/core/cdc/differ.py',
        r'''"""CDC differ (Milestone 6b, ADR-029 decision 1).

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
''',
        'Pure primary-key window differ',
    ),
    (
        'src/core/cdc/runner.py',
        r'''"""CDC runner (Milestone 6b, ADR-029).

Reads TWO windows of each configured entity from one source tree, diffs
them by primary key (src/core/cdc/differ.py), and writes the change log:

    data/generated/  (source tree, read-only)
            |
            v
    CdcRunner (--config configs/cdc_base.yaml --window 2026-01-15)
            |   discover the window file per entity (any "*={window}"
            |   partition dir, or {entity}_{window}.csv flat fallback),
            |   pair with the previous window (explicit --prev-window or
            |   auto = latest earlier window; none -> initial load),
            |   validate headers (drift fail-fast unless allowed),
            |   diff -> INSERT / UPDATE / DELETE events
            v
    data/cdc/{entity}/reference_date={window}/{prev|initial}__{window}.jsonl
    data/cdc/report.json  (REPORT_VERSION 1, atomic)

Guards (ADR-029 decision 5): the output tree must not sit inside the source
tree or vice versa; every write is atomic (.tmp + os.replace); the ledger
identity inserts - deletes == rows_curr - rows_prev is asserted before any
success. Zero imports from generator/writer/impurity internals (ADR-025
consumer precedent).
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from src.core.cdc.differ import (
    CdcConfigError,
    CdcDiff,
    diff_windows,
)
from src.core.cdc.events import CdcError, CdcEvent, OP_UPDATE

REPORT_VERSION = 1
INITIAL_LOAD_POLICIES = ("inserts", "error")


@dataclass(frozen=True)
class CdcEntityConfig:
    """What we know about one entity, from config only."""

    entity: str
    primary_key: str
    columns: tuple[str, ...]  # change-detection whitelist; empty = all


@dataclass(frozen=True)
class CdcConfig:
    """Parsed configs/cdc_base.yaml."""

    entities: Mapping[str, CdcEntityConfig]
    source_tree: Path
    output_root: Path
    before_images: bool
    allow_schema_drift: bool
    initial_load: str  # "inserts" | "error"

    @staticmethod
    def load(raw: Mapping[str, object]) -> "CdcConfig":
        cdc = raw.get("cdc")
        if not isinstance(cdc, Mapping):
            raise CdcConfigError("config needs a top-level 'cdc:' block")

        source_tree = str(cdc.get("source_tree", "data/generated")).strip()
        if not source_tree:
            raise CdcConfigError("cdc.source_tree must be a non-empty path")
        output_root = str(cdc.get("output_root", "data/cdc")).strip()
        if not output_root:
            raise CdcConfigError("cdc.output_root must be a non-empty path")

        options_raw = cdc.get("options", {})
        if not isinstance(options_raw, Mapping):
            raise CdcConfigError("cdc.options must be a mapping")
        before_images = bool(options_raw.get("before_images", True))
        allow_schema_drift = bool(options_raw.get("allow_schema_drift", False))
        initial_load = str(options_raw.get("initial_load", "inserts")).strip()
        if initial_load not in INITIAL_LOAD_POLICIES:
            raise CdcConfigError(
                f"cdc.options.initial_load must be one of "
                f"{', '.join(INITIAL_LOAD_POLICIES)}; got {initial_load!r}"
            )

        raw_entities = cdc.get("entities")
        if not isinstance(raw_entities, Mapping) or not raw_entities:
            raise CdcConfigError("cdc.entities must list at least one entity")
        entities: dict[str, CdcEntityConfig] = {}
        for entity, spec_raw in raw_entities.items():
            if not isinstance(spec_raw, Mapping):
                raise CdcConfigError(f"{entity}: entity spec must be a mapping")
            primary_key = str(spec_raw.get("primary_key", "")).strip()
            if not primary_key:
                raise CdcConfigError(f"{entity}: primary_key is required")
            columns_raw = spec_raw.get("columns", ())
            if isinstance(columns_raw, str) or not isinstance(columns_raw, (list, tuple)):
                raise CdcConfigError(
                    f"{entity}: columns must be a list of column names "
                    f"(change-detection whitelist)"
                )
            columns = tuple(str(c) for c in columns_raw)
            entities[str(entity)] = CdcEntityConfig(
                entity=str(entity),
                primary_key=primary_key,
                columns=columns,
            )

        return CdcConfig(
            entities=entities,
            source_tree=Path(source_tree),
            output_root=Path(output_root),
            before_images=before_images,
            allow_schema_drift=allow_schema_drift,
            initial_load=initial_load,
        )


@dataclass(frozen=True)
class CdcRunResult:
    """Aggregate outcome for one cdc_main invocation."""

    window: str
    ok: bool
    events_total: int
    per_entity: Mapping[str, Mapping[str, object]]


class CdcRunner:
    def __init__(
        self,
        config: CdcConfig,
        log: Callable[[str, str], None],
    ) -> None:
        self.config = config
        self.log = log
        self._check_tree_guard()

    # -- guards --------------------------------------------------------------

    def _check_tree_guard(self) -> None:
        """The cdc tree is DERIVED: it must never live inside the source tree,
        and the source must never live inside it (either direction would make
        a later run read its own output -- the same class of accident the
        impurity engine's quality-write guard prevents)."""
        source = self.config.source_tree.resolve()
        output = self.config.output_root.resolve()
        if source == output or output.is_relative_to(source):
            raise CdcConfigError(
                f"output_root {self.config.output_root.as_posix()} is inside "
                f"source_tree {self.config.source_tree.as_posix()} -- CDC "
                f"output must live in its own tree (e.g. data/cdc)"
            )
        if source.is_relative_to(output):
            raise CdcConfigError(
                f"source_tree {self.config.source_tree.as_posix()} is inside "
                f"output_root {self.config.output_root.as_posix()} -- CDC "
                f"output would wrap the data it reads"
            )

    # -- discovery -----------------------------------------------------------

    def available_windows(self, entity: str) -> list[str]:
        """All window values present for one entity, sorted ascending.

        A window is any partition directory named '*={value}' (covers
        reference_date= and event_date=, daily and monthly keys alike).
        Assumes one key TYPE per entity (ISO dates or month keys), so
        string ordering matches time ordering (ADR-029 decision 3).
        """
        base = self.config.source_tree / entity
        windows: set[str] = set()
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir() and "=" in child.name:
                    value = child.name.split("=", 1)[1]
                    if value:
                        windows.add(value)
        return sorted(windows)

    def window_file(self, entity: str, window: str) -> Path | None:
        """The one CSV file holding this entity's snapshot for this window.

        Primary: any partition dir '{prefix}={window}' under the entity dir.
        Fallback (flat layouts): {entity}/{entity}_{window}.csv.
        Returns None when the window simply does not exist; ambiguous or
        broken layouts fail fast with a diagnostic.
        """
        base = self.config.source_tree / entity
        files: list[Path] = []
        matched_dirs = 0
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name.endswith("=" + window):
                    matched_dirs += 1
                    files.extend(sorted(p for p in child.glob("*.csv") if p.is_file()))
            if matched_dirs and not files:
                raise CdcConfigError(
                    f"{entity}: partition dir for window {window!r} exists but "
                    f"holds no .csv file -- crash artifact? re-generate the "
                    f"window or inspect {base.as_posix()}"
                )
            if not files:
                direct = base / f"{entity}_{window}.csv"
                if direct.is_file():
                    files.append(direct)
        if not files:
            return None
        if len(files) > 1:
            listed = ", ".join(p.name for p in files)
            raise CdcConfigError(
                f"{entity}: {len(files)} csv files match window {window!r} "
                f"({listed}) -- CDC needs exactly one snapshot file per "
                f"entity per window"
            )
        return files[0]

    # -- reading ---------------------------------------------------------------

    def _read_header_and_rows(
        self, path: Path, entity: str
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Read one snapshot file: header list + rows as {column: str}.

        BOM-tolerant (same convention as the DQ harness); ragged rows are
        padded with "" on the right; a row LONGER than the header is a data
        error and fails fast with the row number.
        """
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            if header and header[0].startswith("\ufeff"):
                header[0] = header[0].lstrip("\ufeff")
            rows: list[dict[str, str]] = []
            for number, fields in enumerate(reader, start=1):
                if not fields:  # blank line
                    continue
                if len(fields) > len(header):
                    raise CdcConfigError(
                        f"{entity}: data row {number} of "
                        f"{path.as_posix()} has {len(fields)} fields but the "
                        f"header declares {len(header)} columns -- fix the "
                        f"source file or the window layout"
                    )
                rows.append(
                    {
                        col: (fields[i] if i < len(fields) else "")
                        for i, col in enumerate(header)
                    }
                )
        return header, rows

    def _validate_columns(self, entity: str, spec: CdcEntityConfig, header: list[str], path: Path) -> None:
        """Every configured column must exist in the real header (same
        guided-fail-fast discipline as the DQ harness's header validation)."""
        missing = [c for c in (spec.primary_key, *spec.columns) if c not in header]
        if missing:
            raise CdcConfigError(
                f"{entity}: configured columns not in header of "
                f"{self._label(path)}; actual header: {header}; "
                f"missing: {', '.join(missing)}; "
                f"-> fix cdc.entities in the CDC config to match the real "
                f"column names"
            )

    def _pair_windows(self, entity: str, window: str, prev_window: str | None) -> str | None:
        """Resolve the previous window: explicit wins (validated), otherwise
        the latest earlier window present, otherwise None (initial load)."""
        if prev_window is not None:
            if prev_window == window:
                raise CdcConfigError(
                    f"{entity}: prev_window {prev_window!r} equals window -- "
                    f"prev_window must be strictly EARLIER"
                )
            if prev_window > window:
                raise CdcConfigError(
                    f"{entity}: prev_window {prev_window!r} is NEWER than "
                    f"window {window!r} -- prev_window must be strictly earlier"
                )
            return prev_window
        earlier = [w for w in self.available_windows(entity) if w < window]
        return max(earlier) if earlier else None

    # -- execution -------------------------------------------------------------

    def run(
        self,
        window: str,
        prev_window: str | None = None,
        report_path: Path | None = None,
    ) -> CdcRunResult:
        if not window:
            raise CdcConfigError("--window must be a non-empty partition value")
        self.log("CDC", f"source_tree={self.config.source_tree.as_posix()} window={window}")
        per_entity: dict[str, dict[str, object]] = {}
        events_total = 0

        for entity, spec in self.config.entities.items():
            curr_file = self.window_file(entity, window)
            if curr_file is None:
                available = self.available_windows(entity)
                listed = ", ".join(available) if available else "(none)"
                raise CdcConfigError(
                    f"{entity}: no snapshot for window {window!r} under "
                    f"{self.config.source_tree.as_posix()}; windows present: "
                    f"{listed}; -> generate the window first or fix --window"
                )
            curr_header, curr_rows = self._read_header_and_rows(curr_file, entity)
            self._validate_columns(entity, spec, curr_header, curr_file)

            prev = self._pair_windows(entity, window, prev_window)
            if prev is None and self.config.initial_load == "error":
                raise CdcConfigError(
                    f"{entity}: no earlier window exists for {window!r} and "
                    f"cdc.options.initial_load=error -- an initial load would "
                    f"emit {len(curr_rows)} INSERT events; generate an earlier "
                    f"window or set initial_load: inserts"
                )
            prev_file: Path | None = None
            prev_header: list[str] = []
            prev_rows: list[dict[str, str]] | None = None
            if prev is not None:
                prev_file = self.window_file(entity, prev)
                if prev_file is None:
                    available = self.available_windows(entity)
                    listed = ", ".join(available) if available else "(none)"
                    raise CdcConfigError(
                        f"{entity}: previous window {prev!r} has no snapshot "
                        f"under {self.config.source_tree.as_posix()}; windows "
                        f"present: {listed}"
                    )
                prev_header, prev_rows = self._read_header_and_rows(prev_file, entity)
                self._validate_columns(entity, spec, prev_header, prev_file)

            self._check_header_drift(entity, prev, prev_header, curr_header)
            if prev is not None and set(prev_header) != set(curr_header):
                # Drift allowed (ADR-029 decision 4): pad BOTH sides to the
                # union of columns so before/after images carry identical key
                # sets and cells absent from a window read as "".
                union = list(curr_header) + [
                    c for c in prev_header if c not in set(curr_header)
                ]
                prev_rows = [{c: row.get(c, "") for c in union} for row in prev_rows or []]
                curr_rows = [{c: row.get(c, "") for c in union} for row in curr_rows]
            diff = diff_windows(
                prev_rows,
                curr_rows,
                entity=entity,
                primary_key=spec.primary_key,
                columns=spec.columns,
                prev_window=prev,
                window=window,
            )
            self._assert_ledger_identity(entity, diff)
            events = self._apply_before_images(diff.events)
            events_file = self._write_events(entity, window, prev, events)
            events_total += len(events)

            per_entity[entity] = {
                "prev_window": prev,
                "initial_load": prev is None,
                "file_prev": self._label(prev_file) if prev_file else None,
                "file_curr": self._label(curr_file),
                "rows_prev": diff.rows_prev,
                "rows_curr": diff.rows_curr,
                "inserts": diff.inserts,
                "updates": diff.updates,
                "deletes": diff.deletes,
                "unchanged": diff.unchanged,
                "events_total": len(events),
                "events_file": events_file,
                "ledger_identity_ok": True,
            }
            if prev is None:
                self.log(
                    "CDC",
                    f"{entity}: no earlier window -- initial load "
                    f"(policy={self.config.initial_load}, "
                    f"{diff.rows_curr} INSERT events)",
                )
            else:
                self.log(
                    "CDC",
                    f"{entity}: prev={prev} -> window={window} "
                    f"inserts={diff.inserts} updates={diff.updates} "
                    f"deletes={diff.deletes} unchanged={diff.unchanged}",
                )
            self.log("CDC", f"{entity}: wrote {len(events)} events -> {events_file}")

        result = CdcRunResult(
            window=window,
            ok=True,
            events_total=events_total,
            per_entity=per_entity,
        )
        target = report_path or (self.config.output_root / "report.json")
        self._write_report(result, target)
        return result

    # -- drift -------------------------------------------------------------------

    def _check_header_drift(
        self,
        entity: str,
        prev: str | None,
        prev_header: list[str],
        curr_header: list[str],
    ) -> None:
        if prev is None:
            return
        added = sorted(set(curr_header) - set(prev_header))
        removed = sorted(set(prev_header) - set(curr_header))
        if not added and not removed:
            return
        if not self.config.allow_schema_drift:
            raise CdcConfigError(
                f"{entity}: headers of the two windows differ "
                f"(added: {added or 'none'}; removed: {removed or 'none'}) -- "
                f"CDC compares snapshots, so schema drift between windows is "
                f"a decision, not a surprise; set cdc.options."
                f"allow_schema_drift: true to diff over the union of columns "
                f"(missing cells read as empty)"
            )
        self.log(
            "CDC",
            f"{entity}: schema drift allowed (added: {added or 'none'}; "
            f"removed: {removed or 'none'}) -- diffing over the union",
        )

    # -- events out ----------------------------------------------------------------

    def _apply_before_images(self, events: tuple[CdcEvent, ...]) -> tuple[CdcEvent, ...]:
        if self.config.before_images:
            return events
        return tuple(
            CdcEvent(
                seq=e.seq,
                op=e.op,
                entity=e.entity,
                pk=e.pk,
                window=e.window,
                prev_window=e.prev_window,
                changed=e.changed if e.op == OP_UPDATE else (),
                before=None,
                after=e.after,
            )
            for e in events
        )

    def _events_rel_path(self, entity: str, window: str, prev: str | None) -> Path:
        prev_label = prev if prev is not None else "initial"
        return (
            Path(entity)
            / f"reference_date={window}"
            / f"{prev_label}__{window}.jsonl"
        )

    def _write_events(
        self, entity: str, window: str, prev: str | None, events: tuple[CdcEvent, ...]
    ) -> str:
        target = self.config.output_root / self._events_rel_path(entity, window, prev)
        payload = "".join(e.to_jsonl() for e in events).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
        os.replace(tmp, target)
        return target.relative_to(self.config.output_root).as_posix()

    def _assert_ledger_identity(self, entity: str, diff: CdcDiff) -> None:
        """The contract: INSERTs minus DELETEs must equal the row-count delta.
        Guaranteed by the differ's partition-by-key; asserted here so any
        future refactor fails loudly instead of shipping a wrong stream."""
        if diff.inserts - diff.deletes != diff.rows_curr - diff.rows_prev:
            raise CdcError(
                f"{entity}: ledger identity violated (inserts={diff.inserts} "
                f"deletes={diff.deletes} rows {diff.rows_prev}->{diff.rows_curr}) "
                f"-- internal diff bug, report this run's output"
            )

    # -- report -------------------------------------------------------------------

    def _label(self, path: Path) -> str:
        return path.relative_to(self.config.source_tree).as_posix()

    def _write_report(self, result: CdcRunResult, path: Path) -> None:
        payload = json.dumps(
            {
                "report_version": REPORT_VERSION,
                "source_tree": self.config.source_tree.as_posix(),
                "output_root": self.config.output_root.as_posix(),
                "window": result.window,
                "ok": result.ok,
                "events_total": result.events_total,
                "entities": result.per_entity,
            },
            indent=2,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        self.log("CDC", f"report written: {path.as_posix()}")
''',
        'CDC runner: discovery, guards, outputs',
    ),
    (
        'cdc_main.py',
        r'''"""cdc_main.py -- CDC change stream entry point (Milestone 6b, ADR-029).

Diffs consecutive windows of the configured entities in one output tree and
writes the CDC change log (JSONL) under data/cdc/.

Usage (from the project root, venv Style B):

    .venv\\Scripts\\python.exe cdc_main.py --window 2026-01-15
    .venv\\Scripts\\python.exe cdc_main.py --window 2026-01-15 --prev-window 2026-01-14

The first run against a tree is the INITIAL LOAD (every row becomes an
INSERT event, prev_window=null). Later runs auto-pair --window with the
latest earlier window unless --prev-window says otherwise.

Exit codes (house contract):
    0    ok      -- change log + report written, ledger identity holds
    1    failure -- config error, missing window/snapshot, or broken layout
    130  interrupted (Ctrl+C)

Log contract (ADR-0009): same tags as every run, plus [CDC] (new in 6b,
documented in ADR-029). Report defaults to data/cdc/report.json (atomic).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.core.cdc.differ import CdcConfigError
from src.core.cdc.events import CdcError
from src.core.cdc.runner import CdcConfig, CdcRunner


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit the CDC change log between two windows (Milestone 6b)."
    )
    parser.add_argument(
        "--window",
        required=True,
        help="partition value of the CURRENT window, e.g. 2026-01-15 (or 2026-01 for monthly)",
    )
    parser.add_argument(
        "--prev-window",
        default=None,
        help="partition value of the PREVIOUS window (default: auto = latest earlier window; none -> initial load)",
    )
    parser.add_argument("--config", default="configs/cdc_base.yaml", help="CDC config (default: configs/cdc_base.yaml)")
    parser.add_argument("--report", default=None, help="report path (default: <output_root>/report.json)")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log("STARTUP", f"cwd={Path.cwd()}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    log(
        "CONFIG",
        f"config={args.config} window={args.window}"
        + (f" prev={args.prev_window} (explicit)" if args.prev_window else " prev=auto"),
    )

    config_path = Path(args.config)
    if not config_path.is_file():
        log("CONFIG", f"CDC config not found: {config_path}")
        return 1
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = CdcConfig.load(raw)
    log("CONFIG", f"source_tree={config.source_tree.as_posix()} output_root={config.output_root.as_posix()}")
    log("CONFIG", "entities: " + ", ".join(config.entities))
    log(
        "CONFIG",
        f"options: before_images={config.before_images} "
        f"allow_schema_drift={config.allow_schema_drift} "
        f"initial_load={config.initial_load}",
    )

    runner = CdcRunner(config, log)
    result = runner.run(
        args.window,
        prev_window=args.prev_window,
        report_path=Path(args.report) if args.report else None,
    )

    for entity, ledger in result.per_entity.items():
        log(
            "SUMMARY",
            f"{entity}: prev={ledger['prev_window'] or 'initial'} "
            f"rows {ledger['rows_prev']}->{ledger['rows_curr']} "
            f"inserts={ledger['inserts']} updates={ledger['updates']} "
            f"deletes={ledger['deletes']} unchanged={ledger['unchanged']}",
        )
    log("SUMMARY", f"ok={result.ok} entities={len(result.per_entity)} events={result.events_total}")
    log("SUMMARY", "result as expected (exit 0)")
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C)")
        return 130
    except CdcError as exc:
        # CdcConfigError (config/semantics) and any internal CdcError both
        # exit 1 with the message as the diagnostic -- one handler, because
        # the operator-facing contract is the same: loud, one-line, guided.
        log("CDC", f"failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
''',
        'CDC CLI entry point ([CDC] log tag)',
    ),
    (
        'configs/cdc_base.yaml',
        r'''# =============================================================================
# cdc_base.yaml -- CDC change stream configuration (Milestone 6b, ADR-029)
#
# The CDC module is a CONSUMER of output trees (ADR-025 precedent): it reads
# consecutive windows of each configured entity from source_tree and writes
# the change log under output_root -- INSERT / UPDATE / DELETE per primary
# key, full before/after images, one JSON object per line.
#
#   source_tree : any tree the platform wrote (data/generated; point it at
#                 data/corrupted to stream the impurity engine's changes --
#                 pair with options.allow_schema_drift: true there, because
#                 the drift rule adds columns between windows).
#   output_root : data/cdc (derived tree; the runner REFUSES to write it
#                 inside the source tree or vice versa).
#
#   options:
#     before_images      -- keep the full previous row on UPDATE/DELETE
#                           events (replay safety). false = smaller files.
#     allow_schema_drift -- diff over the UNION of the two windows' columns
#                           instead of fail-fast when headers differ.
#     initial_load       -- "inserts" (default): the first run with no
#                           earlier window emits every row as INSERT
#                           (prev_window=null). "error": refuse instead.
#
#   entities: primary_key required; optional columns = change-DETECTION
#             whitelist (images always carry full rows regardless).
#
# First drills (single window on this machine):
#   .venv\Scripts\python.exe cdc_main.py --window 2026-01-15
#   -> data\cdc\report.json + per-entity JSONL under data\cdc\<entity>\
# With a second window generated:
#   .venv\Scripts\python.exe cdc_main.py --window <newer>
#   (prev auto-detected; or pass --prev-window explicitly)
#
# customer/order/order_item are wired (the three highest-volume entities --
# an initial load emits one event per row, so wire more entities when you
# actually need their streams). [CDC] is the new log tag (ADR-029).
# =============================================================================

cdc:
  source_tree: data/generated
  output_root: data/cdc

  options:
    before_images: true
    allow_schema_drift: false
    initial_load: inserts

  entities:
    customer:
      primary_key: customer_id

    order:
      primary_key: order_id
      # foreign keys are irrelevant to the diff -- CDC works per entity

    order_item:
      primary_key: order_item_id

    # --- wire the remaining entities the same way when needed:
    # employee:  {primary_key: employee_id}
    # review:    {primary_key: review_id}
    # payment:   {primary_key: payment_id}
    #
    # --- change-detection whitelist example (images stay full either way):
    # order:
    #   primary_key: order_id
    #   columns: [status, sales_rep_employee_id]
''',
        'CDC config (customer/order/order_item wired)',
    ),
    (
        'tests/test_cdc_differ.py',
        r'''"""Unit tests for the CDC differ (Milestone 6b, ADR-029 decision 1/2).

The differ is pure: rows in, CdcDiff out. These tests pin the event ledger
semantics (INSERT/UPDATE/DELETE/unchanged), the determinism contract
(pk-ordered, seq 1..N), the snapshot-validity fail-fasts (duplicate/empty
primary keys), and the JSONL wire format.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.cdc.differ import CdcConfigError, diff_windows
from src.core.cdc.events import OP_DELETE, OP_INSERT, OP_UPDATE

W1 = "2026-01-14"
W2 = "2026-01-15"


def row(pk: str, **extra) -> dict:
    base = {"order_id": pk, "status": "new", "total": "10"}
    base.update(extra)
    return base


def ops(diff):
    return [e.op for e in diff.events]


def pks(diff):
    return [e.pk for e in diff.events]


# -- ledger semantics -------------------------------------------------------


def test_initial_load_every_row_is_insert():
    diff = diff_windows(None, [row("a"), row("b")], entity="order", primary_key="order_id", window=W2)
    assert diff.inserts == 2 and diff.updates == 0 and diff.deletes == 0
    assert diff.rows_prev == 0 and diff.rows_curr == 2
    assert ops(diff) == [OP_INSERT, OP_INSERT]
    for e in diff.events:
        assert e.before is None and e.after is not None
        assert e.prev_window is None and e.window == W2


def test_pure_inserts_between_windows():
    diff = diff_windows([row("a")], [row("a"), row("b")], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert ops(diff) == [OP_INSERT]
    assert diff.unchanged == 1


def test_pure_deletes_carry_before_image_only():
    diff = diff_windows([row("a"), row("b")], [row("a")], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert ops(diff) == [OP_DELETE]
    assert diff.deletes == 1
    e = diff.events[0]
    assert e.before == row("b") and e.after is None


def test_pure_updates_carry_both_images_and_changed():
    diff = diff_windows(
        [row("a"), row("x", status="old")],
        [row("a"), row("x", status="new")],
        entity="order", primary_key="order_id", prev_window=W1, window=W2,
    )
    assert ops(diff) == [OP_UPDATE]
    e = diff.events[0]
    assert e.changed == ("status",)
    assert e.before["status"] == "old" and e.after["status"] == "new"


def test_mixed_ledger_exact_event_set():
    prev = [row("keep"), row("gone"), row("mut", status="old")]
    curr = [row("keep"), row("born"), row("mut", status="new")]
    diff = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert {e.pk: e.op for e in diff.events} == {
        "born": OP_INSERT, "gone": OP_DELETE, "mut": OP_UPDATE,
    }
    assert diff.unchanged == 1  # 'keep'
    assert (diff.inserts, diff.updates, diff.deletes) == (1, 1, 1)


def test_unchanged_rows_never_emit_events():
    diff = diff_windows([row("a"), row("b")], [row("a"), row("b")], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert diff.events == () and diff.unchanged == 2
    assert diff.inserts == diff.updates == diff.deletes == 0


def test_full_row_image_covers_every_column():
    prev = [{"order_id": "a", "status": "new", "total": "10", "extra": "x"}]
    curr = [{"order_id": "a", "status": "new", "total": "11"}]
    diff = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    e = diff.events[0]
    assert e.changed == ("extra", "total")  # drift-union: extra vanished -> changed
    assert e.before == prev[0] and e.after == curr[0]


# -- determinism --------------------------------------------------------------


def test_events_pk_ordered_and_seq_contiguous():
    curr = [row(k) for k in ("m", "a", "z", "b")]
    diff = diff_windows(None, curr, entity="order", primary_key="order_id", window=W2)
    assert pks(diff) == ["a", "b", "m", "z"]
    assert [e.seq for e in diff.events] == [1, 2, 3, 4]


def test_same_windows_diff_twice_is_identical():
    prev = [row("keep"), row("mut", status="old")]
    curr = [row("keep"), row("mut", status="new"), row("new")]
    one = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    two = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert [e.to_jsonl() for e in one.events] == [e.to_jsonl() for e in two.events]


# -- snapshot validity ---------------------------------------------------------


@pytest.mark.parametrize("side", ["prev", "curr"])
def test_duplicate_primary_key_fails_fast(side):
    prev = [row("a"), row("a")] if side == "prev" else [row("a")]
    curr = [row("a")] if side == "prev" else [row("a"), row("a")]
    with pytest.raises(CdcConfigError, match="duplicate primary key 'a'"):
        diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)


def test_empty_primary_key_fails_fast():
    with pytest.raises(CdcConfigError, match="empty primary key"):
        diff_windows(None, [row(""), row("a")], entity="order", primary_key="order_id", window=W2)


# -- config surface (columns whitelist) ------------------------------------------


def test_columns_whitelist_restricts_change_detection():
    prev = [row("a", status="same", total="10")]
    curr = [row("a", status="same", total="99")]  # only 'total' changed
    diff = diff_windows(
        prev, curr, entity="order", primary_key="order_id",
        columns=("status",), prev_window=W1, window=W2,
    )
    assert ops(diff) == []  # total is outside the whitelist -> unchanged
    assert diff.unchanged == 1


def test_columns_whitelist_does_not_trim_images():
    prev = [row("a", status="old")]
    curr = [row("a", status="new")]
    diff = diff_windows(
        prev, curr, entity="order", primary_key="order_id",
        columns=("status",), prev_window=W1, window=W2,
    )
    e = diff.events[0]
    assert e.changed == ("status",)
    assert e.before == prev[0] and e.after == curr[0]  # images stay FULL


# -- wire format -----------------------------------------------------------------


def test_to_jsonl_round_trip_and_field_order():
    diff = diff_windows(
        [row("a", status="old")], [row("a", status="new")],
        entity="order", primary_key="order_id", prev_window=W1, window=W2,
    )
    line = diff.events[0].to_jsonl()
    assert line.endswith("\n")
    parsed = json.loads(line)
    assert list(parsed) == ["seq", "op", "entity", "pk", "window", "prev_window", "changed", "before", "after"]
    assert parsed["op"] == OP_UPDATE and parsed["pk"] == "a"
    assert parsed["prev_window"] == W1 and parsed["window"] == W2


def test_delete_event_json_shape():
    diff = diff_windows([row("b")], [], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    parsed = json.loads(diff.events[0].to_jsonl())
    assert parsed["op"] == OP_DELETE
    assert parsed["before"] == row("b") and parsed["after"] is None
    assert parsed["changed"] == []
''',
        'Differ unit tests',
    ),
    (
        'tests/test_cdc_runner.py',
        r'''"""Integration tests for the CDC runner and config (Milestone 6b, ADR-029).

Builds tiny synthetic windowed output trees (orders + customers, snapshot
layout) in tmp_path and runs CdcRunner end-to-end:
  * initial load   (single window  -> every row is an INSERT)
  * delta runs     (two windows   -> exact INSERT/UPDATE/DELETE ledger)
  * fail-fasts     (missing windows, header drift, duplicate keys, guards)
Pins the atomic outputs too: JSONL bytes, report JSON, idempotent re-runs.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.core.cdc.differ import CdcConfigError
from src.core.cdc.events import OP_DELETE, OP_INSERT, OP_UPDATE
from src.core.cdc.runner import CdcConfig, CdcRunner

W1 = "2026-01-14"
W2 = "2026-01-15"
W3 = "2026-01-16"


def write_window(root: Path, entity: str, date: str, rows: list[dict]) -> None:
    """One snapshot window: {entity}/reference_date={date}/{entity}_{date}.csv"""
    folder = root / entity / f"reference_date={date}"
    folder.mkdir(parents=True, exist_ok=True)
    header = list(rows[0].keys()) if rows else ["order_id"]
    lines = [",".join(header)]
    lines += [",".join(str(r[c]) for c in header) for r in rows]
    (folder / f"{entity}_{date}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_config(tmp_path: Path, entities: dict | None = None, **overrides) -> CdcConfig:
    raw = {
        "cdc": {
            "source_tree": str(tmp_path / "generated"),
            "output_root": str(tmp_path / "cdc"),
            "options": {
                "before_images": True,
                "allow_schema_drift": False,
                "initial_load": "inserts",
            },
            "entities": entities
            if entities is not None
            else {
                "order": {"primary_key": "order_id"},
                "customer": {"primary_key": "customer_id"},
            },
        }
    }
    raw["cdc"].update(overrides)
    return CdcConfig.load(raw)


def make_runner(tmp_path: Path, entities: dict | None = None, **overrides) -> CdcRunner:
    logs: list[tuple[str, str]] = []
    runner = CdcRunner(
        make_config(tmp_path, entities=entities, **overrides),
        lambda t, m: logs.append((t, m)),
    )
    runner.logs = logs  # type: ignore[attr-defined]
    return runner


ORDER_ONLY = {"order": {"primary_key": "order_id"}}


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- config parsing -----------------------------------------------------------


def test_config_load_defaults_and_entities(tmp_path):
    config = make_config(tmp_path)
    assert set(config.entities) == {"order", "customer"}
    assert config.entities["order"].primary_key == "order_id"
    assert config.before_images is True
    assert config.allow_schema_drift is False
    assert config.initial_load == "inserts"


def test_config_missing_cdc_block_fails(tmp_path):
    with pytest.raises(CdcConfigError, match="top-level 'cdc:'"):
        CdcConfig.load({"dq": {}})


def test_config_entity_without_primary_key_fails(tmp_path):
    raw = {"cdc": {"entities": {"order": {}}}}
    with pytest.raises(CdcConfigError, match="order: primary_key is required"):
        CdcConfig.load(raw)


def test_config_bad_initial_load_policy_fails(tmp_path):
    raw = {"cdc": {"options": {"initial_load": "explode"}, "entities": {"order": {"primary_key": "order_id"}}}}
    with pytest.raises(CdcConfigError, match="initial_load"):
        CdcConfig.load(raw)


def test_config_columns_whitelist_parses(tmp_path):
    raw = {"cdc": {"entities": {"order": {"primary_key": "order_id", "columns": ["status"]}}}}
    config = CdcConfig.load(raw)
    assert config.entities["order"].columns == ("status",)


# -- initial load ---------------------------------------------------------------


def test_initial_load_single_window(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "o2", "status": "new"}, {"order_id": "o1", "status": "new"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W2)

    assert result.ok and result.events_total == 2
    ledger = result.per_entity["order"]
    assert ledger["initial_load"] is True and ledger["prev_window"] is None
    assert (ledger["inserts"], ledger["updates"], ledger["deletes"]) == (2, 0, 0)

    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"initial__{W2}.jsonl")
    assert [e["pk"] for e in events] == ["o1", "o2"]  # pk-sorted, not file order
    assert all(e["op"] == OP_INSERT and e["prev_window"] is None for e in events)
    assert [e["seq"] for e in events] == [1, 2]

    report = json.loads((tmp_path / "cdc" / "report.json").read_text(encoding="utf-8"))
    assert report["report_version"] == 1 and report["window"] == W2 and report["ok"] is True
    assert report["entities"]["order"]["ledger_identity_ok"] is True


def test_initial_load_policy_error_refuses(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "o1"}])
    runner = make_runner(
        tmp_path,
        entities=ORDER_ONLY,
        options={"initial_load": "error", "before_images": True, "allow_schema_drift": False},
    )
    with pytest.raises(CdcConfigError, match="initial_load=error"):
        runner.run(W2)


# -- delta runs -------------------------------------------------------------------


def test_delta_run_exact_ledger(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [
        {"order_id": "o-keep", "status": "new", "total": "10"},
        {"order_id": "o-gone", "status": "new", "total": "10"},
        {"order_id": "o-mut", "status": "new", "total": "10"},
    ])
    write_window(tree, "order", W2, [
        {"order_id": "o-keep", "status": "new", "total": "10"},
        {"order_id": "o-mut", "status": "paid", "total": "11"},
        {"order_id": "o-born", "status": "new", "total": "10"},
    ])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W2)  # auto-pair: latest earlier window = W1

    ledger = result.per_entity["order"]
    assert ledger["prev_window"] == W1
    assert (ledger["inserts"], ledger["updates"], ledger["deletes"], ledger["unchanged"]) == (1, 1, 1, 1)
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl")
    by_pk = {e["pk"]: e for e in events}
    assert by_pk["o-born"]["op"] == OP_INSERT
    assert by_pk["o-gone"]["op"] == OP_DELETE and by_pk["o-gone"]["after"] is None
    assert by_pk["o-mut"]["op"] == OP_UPDATE
    assert by_pk["o-mut"]["changed"] == ["status", "total"]
    assert by_pk["o-mut"]["before"]["status"] == "new" and by_pk["o-mut"]["after"]["status"] == "paid"
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_auto_prev_picks_latest_earlier_window(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}])
    write_window(tree, "order", W3, [{"order_id": "a", "status": "shipped"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W3)
    ledger = result.per_entity["order"]
    assert ledger["prev_window"] == W2
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W3}" / f"{W2}__{W3}.jsonl")
    assert events[0]["changed"] == ["status"]


def test_explicit_prev_window_wins(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}])
    write_window(tree, "order", W3, [{"order_id": "a", "status": "shipped"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W3, prev_window=W1)
    assert result.per_entity["order"]["prev_window"] == W1


# -- fail-fasts ---------------------------------------------------------------------


def test_missing_current_window_lists_available(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match=W1):
        runner.run(W2)


def test_prev_equal_or_newer_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W2, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="strictly EARLIER"):
        runner.run(W2, prev_window=W2)
    with pytest.raises(CdcConfigError, match="strictly earlier"):
        runner.run(W1, prev_window=W2)


def test_missing_prev_window_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W3, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="previous window"):
        runner.run(W3, prev_window=W2)


def test_header_drift_blocked_by_default(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "new", "bonus_col": "x"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="schema drift"):
        runner.run(W2)


def test_header_drift_allowed_diffs_union(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "new", "bonus_col": "x"}])
    runner = make_runner(
        tmp_path,
        entities=ORDER_ONLY,
        options={"before_images": True, "allow_schema_drift": True, "initial_load": "inserts"},
    )
    result = runner.run(W2)
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl")
    assert events[0]["op"] == OP_UPDATE
    assert events[0]["changed"] == ["bonus_col"]
    assert events[0]["before"] == {"order_id": "a", "status": "new", "bonus_col": ""}
    assert events[0]["after"]["bonus_col"] == "x"


def test_configured_column_not_in_header_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "a"}])
    raw = {
        "cdc": {
            "source_tree": str(tree),
            "output_root": str(tmp_path / "cdc"),
            "entities": {"order": {"primary_key": "order_id", "columns": ["nope"]}},
        }
    }
    runner = CdcRunner(CdcConfig.load(raw), lambda t, m: None)
    with pytest.raises(CdcConfigError, match="nope"):
        runner.run(W2)


# -- guards ---------------------------------------------------------------------------


def test_output_inside_source_tree_refused(tmp_path):
    (tmp_path / "generated").mkdir()
    raw = {
        "cdc": {
            "source_tree": str(tmp_path / "generated"),
            "output_root": str(tmp_path / "generated" / "cdc"),
            "entities": {"order": {"primary_key": "order_id"}},
        }
    }
    with pytest.raises(CdcConfigError, match="inside"):
        CdcRunner(CdcConfig.load(raw), lambda t, m: None)


def test_source_inside_output_refused(tmp_path):
    (tmp_path / "cdc").mkdir()
    raw = {
        "cdc": {
            "source_tree": str(tmp_path / "cdc" / "generated"),
            "output_root": str(tmp_path / "cdc"),
            "entities": {"order": {"primary_key": "order_id"}},
        }
    }
    with pytest.raises(CdcConfigError, match="wrap"):
        CdcRunner(CdcConfig.load(raw), lambda t, m: None)


def test_duplicate_pk_in_window_fails_via_runner(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W2, [{"order_id": "a"}, {"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="duplicate primary key 'a'"):
        runner.run(W2)


def test_row_longer_than_header_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "a"}])
    target = tree / "order" / f"reference_date={W2}" / f"order_{W2}.csv"
    target.write_text("order_id\na,extra\n", encoding="utf-8")
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="header declares"):
        runner.run(W2)


# -- before_images off + idempotency ---------------------------------------------------


def test_before_images_off_strips_before_keeps_changed(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}])
    runner = make_runner(
        tmp_path,
        entities=ORDER_ONLY,
        options={"before_images": False, "allow_schema_drift": False, "initial_load": "inserts"},
    )
    runner.run(W2)
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl")
    assert events[0]["before"] is None
    assert events[0]["changed"] == ["status"] and events[0]["after"]["status"] == "paid"


def test_rerun_is_byte_identical(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}, {"order_id": "b", "status": "new"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    runner.run(W2)
    events_path = tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl"
    report_path = tmp_path / "cdc" / "report.json"
    first_events, first_report = events_path.read_bytes(), report_path.read_bytes()
    runner.run(W2)
    assert events_path.read_bytes() == first_events
    assert report_path.read_bytes() == first_report


def test_log_lines_carry_cdc_tag(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    runner.run(W2)
    tags = {t for t, _ in runner.logs}  # type: ignore[attr-defined]
    assert "CDC" in tags
    assert any("initial load" in m for _, m in runner.logs)  # type: ignore[attr-defined]
''',
        'Runner integration tests',
    ),
]

APPEND_FILES: list[tuple[str, str, str, str]] = [
    (
        "docs/decisions.md",
        'ADR-029',
        r'''---
## ADR-029 — Milestone 6b: incremental change stream + CDC-style change log

**Status:** Accepted (Milestone 6b — new `src/core/cdc/` module, no existing files modified)

**Context.** Phase 5's window engine (ADR-0024) is live: every run writes
per-window partitions (`{tree}/{entity}/reference_date={window}/…`, daily or
monthly keys), fact/dimension split, manifest v3. What the platform does NOT
yet produce is the downstream-facing artifact those windows imply: a change
stream saying WHICH rows appeared, mutated, or vanished between consecutive
windows, in a format a consumer can replay. The roadmap names both halves of
Milestone 6b: an incremental change stream with INSERT/UPDATE/DELETE
semantics, and a CDC-style change log format. The delivery constraint is the
same one ADR-025 faced: the harness-side modules must integrate through
documented, stable contracts (output-tree layouts + config files) because
they are verified and installed without access to the generator's internals.

**Decision.**
1. **CDC is diff-based, over output trees.** `src/core/cdc/` reads TWO
   windows of one entity from the same tree (the same class of reads the DQ
   harness has done since ADR-025) and computes the change stream by
   primary-key diff: key in current window only -> INSERT; key in previous
   window only -> DELETE; key in both with differing row -> UPDATE; key in
   both with identical row -> no event. Zero imports from generator, writer,
   or impurity internals. In-generator event emission (hooks inside the
   pipeline, true transaction ordering) is explicitly OUT OF SCOPE and stays
   a registered future option — it requires touching `main.py`'s core and
   the writers, which this delivery model cannot verify. Diff-based CDC is
   replay-safe, works against every tree the engine has ever written, and
   doubles as the reference implementation a consumer would write.
2. **Change log format: JSONL, full before/after images.** One JSON object
   per line, UTF-8, LF endings:
   `{"seq": 1, "op": "INSERT", "entity": "order", "pk": "o-1",
     "window": "2026-01-15", "prev_window": "2026-01-14",
     "changed": [], "before": null, "after": {...}}`.
   Canonical op names INSERT/UPDATE/DELETE. Full row images (not field
   deltas) keep events self-contained and replayable; `changed` additionally
   carries the sorted list of differing columns for UPDATEs. `before` is
   carried for UPDATE/DELETE and dropped when `options.before_images: false`
   (images always reflect the raw string cells exactly as the files hold
   them — same reader-boundary philosophy as ADR-028 decision 3: every cell
   is a str, missing cells are ""). `seq` is 1-based per entity per run,
   assigned in ascending primary-key order — byte-identical output on
   re-runs, deterministic across platforms.
3. **Window pairing: explicit or auto-detected; first run is the initial
   load.** `cdc_main.py --window W` pairs W with the latest EARLIER window
   present for that entity (auto), unless `--prev-window P` is given
   (explicit; P == W and P > W fail fast). When no earlier window exists,
   the run is an INITIAL LOAD: every current row becomes an INSERT event and
   `prev_window` is null — the standard CDC bootstrap semantics — unless
   `options.initial_load: error` is set, which makes a missing predecessor a
   hard failure for teams that only ever want deltas.
4. **Snapshot validity is enforced.** CDC compares per-window SNAPSHOTS, so
   a primary key may appear at most once per window per entity: a duplicate
   fails fast naming the key and both row numbers. Headers of the two
   compared windows must have the same column set unless
   `options.allow_schema_drift: true`, in which case the diff runs over the
   union of headers and cells absent from a window read as "" (the
   impurity engine's drift rule can add columns, so corrupted trees need
   this switch; clean trees never will).
5. **Output layout, guard, report.** Change logs land in their own derived
   tree, never inside the source: `data/cdc/{entity}/reference_date={window}/
   {prev|initial}__{window}.jsonl` plus one run report `data/cdc/report.json`
   (REPORT_VERSION 1, atomic write, same .tmp + os.replace discipline as
   every writer). The runner refuses any `output_root` that sits inside the
   source tree or vice versa (both directions), mirroring the impurity
   engine's quality-write guard. Per-window filenames carry BOTH windows, so
   re-runs with a different predecessor never silently overwrite a different
   analysis. Manifest v3 for the cdc tree is deferred (derived artifact,
   rebuildable; the report carries sha256-free counts and file labels).
6. **Contract and budget unchanged.** No new dependency (stdlib + PyYAML,
   already in use). Exit codes 0/1/130; new `[CDC]` log tag, additive to
   ADR-0009 exactly as `[DQ]` was in ADR-025 (no §26 trigger). The ledger
   identity `inserts − deletes ≡ rows_curr − rows_prev` is asserted before
   every success — a cheap, permanent reconciliation guard on the diff
   engine itself. The DQ suite (103 tests) is untouched; CDC ships its own
   test files.

**Alternatives considered.**
- In-generator incremental emission now: INSERT/UPDATE/DELETE decided while
  rows are generated. Rejected for 6b — needs core pipeline + writer access,
  cannot be sandbox-verified under the bootstrap delivery model, and couples
  the stream to the generator's internals. Registered as the future upgrade
  path (the JSONL format is chosen so those events can slot in unchanged).
- duckdb/SQL diffing: a new dependency and a paradigm shift for a two-file
  key-diff. Rejected (same reasoning as ADR-028's duckdb alternative).
- Field-delta-only payloads (no images): smaller files, but consumers cannot
  replay state from the log alone and every schema change bites. Rejected.
- CSV change log: quoting churn and no stream semantics; JSONL is the
  NDJSON writer's existing family (ADR-0017) and streams line-by-line.
  Rejected.
- Hard-require a previous window: breaks the first run against an existing
  tree for no informational gain. Rejected (initial-load default, `error`
  available).

**Consequences.**
- The change stream is derivable at any time from any two windows of any
  tree the platform wrote — including `data/corrupted`, where drift handling
  (decision 4) applies.
- Initial loads on big entities produce one event per row (all six entities
  ≈ 23k events ≈ a few MB of JSONL); wire `cdc.entities` selectively if
  that matters, and prefer delta runs once a second window exists.
- A future DQ check can consume the cdc tree as a new surface (e.g. "every
  current row appears as an INSERT or UPDATE after-image") — registered as
  backlog synergy with the 7-track, not part of 6b.
- If the engine later grows true incremental emission, the JSONL schema
  (decision 2) is the contract both producers write; `seq` then becomes the
  merge key instead of a per-run index.
''',
        "ADR-029 (append-only, idempotent by marker)",
    ),
]


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_preconditions(root: Path) -> bool:
    ok = True
    log("STARTUP", f"cwd={root}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    for rel in ("main.py", "src", "docs", "tests"):
        p = root / rel
        if p.exists():
            log("CHECK", f"ok       {rel}")
        else:
            log("CHECK", f"MISSING  {rel} (expected in project root)")
            ok = False
    if (root / "configs").is_dir():
        log("CHECK", "ok       configs")
    else:
        log("CHECK", "note     configs/ absent -- will be created for cdc_base.yaml")
    if (root / "data" / "generated").is_dir():
        log("CHECK", "ok       data/generated (drill target present)")
    else:
        log("CHECK", "note     data/generated absent -- tests run fine, drills need generated data first")
    if not (root / ".git").exists():
        log("CHECK", "warn     .git not found -- not fatal, but you lose rollback safety")
    runner = root / "src" / "core" / "dq" / "runner.py"
    if runner.is_file() and "pk_union" in runner.read_text(encoding="utf-8", errors="replace"):
        log("CHECK", "ok       7b DQ harness present (independent module, no conflict)")
    else:
        log("CHECK", "note     DQ harness not detected -- CDC does not require it")
    cdc_dir = root / "src" / "core" / "cdc"
    if cdc_dir.exists():
        log("CHECK", "note     src/core/cdc already exists -- existing files are never overwritten")
    return ok


def deliver_new(root: Path, rel: str, content: str, label: str, dry: bool) -> str:
    target = root / rel
    payload = content.encode("utf-8")
    digest = sha256_of(payload)
    log("CONFIG", f"{label}: {target}")
    if target.exists() and target.stat().st_size > 0:
        log("SKIP", f"{rel} already exists ({target.stat().st_size} bytes) -- never overwritten")
        return "skip"
    heal = target.exists()
    if heal:
        log("HEAL", f"{rel} exists but is EMPTY (crash artifact) -- healing")
    if dry:
        log("DRY-RUN", f"would {'heal' if heal else 'create'} {rel} ({len(payload)} bytes, sha256={digest[:16]}...)")
        return "healed" if heal else "created (dry-run)"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    log("HEAL" if heal else "WRITE", f"wrote {rel} ({len(payload)} bytes, sha256={digest[:16]}...)")
    return "healed" if heal else "created"


def deliver_append(root: Path, rel: str, marker: str, content: str, label: str, dry: bool) -> str:
    target = root / rel
    log("CONFIG", f"{label}: {target}")
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if marker in existing:
            log("SKIP", f"{rel} already contains {marker} -- append skipped (idempotent)")
            return "skip"
        addition = content if existing.endswith("\n") or not existing else "\n" + content
        addition += "\n"
        if dry:
            log("DRY-RUN", f"would append {len(addition.encode('utf-8'))} bytes to {rel}")
            return "appended (dry-run)"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(addition)
        log("APPEND", f"appended {marker} to {rel} (nothing truncated)")
        return "appended"
    if dry:
        log("DRY-RUN", f"would create {rel} with {marker}")
        return "appended (dry-run)"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content + "\n", encoding="utf-8")
    log("WRITE", f"created {rel} with {marker}")
    return "appended"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Milestone 6b (CDC change stream).")
    parser.add_argument("--dry-run", action="store_true", help="show actions without writing")
    args = parser.parse_args()

    log("STARTUP", "bootstrap_milestone6b.py -- incremental change stream + CDC change log (ADR-029)")
    root = Path.cwd()
    if not check_preconditions(root):
        log("SUMMARY", "preconditions failed -- run this from the project root")
        return 1

    results: list[tuple[str, str]] = []
    try:
        for rel, content, label in NEW_FILES:
            results.append((rel, deliver_new(root, rel, content, label, args.dry_run)))
        for rel, marker, content, label in APPEND_FILES:
            results.append((rel, deliver_append(root, rel, marker, content, label, args.dry_run)))
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C) -- writes are atomic, nothing half-written")
        return 130
    except OSError as exc:
        log("SUMMARY", f"write failure: {exc}")
        return 1

    log("MANIFEST", "actions: " + ("; ".join(f"{r}={s}" for r, s in results) or "none"))
    if not args.dry_run:
        log("SUMMARY", "next steps:")
        log("SUMMARY", "  1. .venv\\Scripts\\python.exe -m pytest tests/test_cdc_differ.py tests/test_cdc_runner.py -q")
        log("SUMMARY", "     (expect 39 passed -- the 103 DQ tests are untouched)")
        log("SUMMARY", "  2. .venv\\Scripts\\python.exe -m pytest -q")
        log("SUMMARY", "     (expect 142 passed = 103 DQ + 39 CDC)")
        log("SUMMARY", "  3. initial-load drill (single window on this machine):")
        log("SUMMARY", "     .venv\\Scripts\\python.exe cdc_main.py --window 2026-01-15")
        log("SUMMARY", "     (expect exit 0; customer 1000 / order 5000 / order_item 12000 INSERTs")
        log("SUMMARY", "      per the accepted clean-drill volumes; report at data\\cdc\\report.json.")
        log("SUMMARY", "      If the window value differs on this machine, the diagnostic lists the")
        log("SUMMARY", "      windows present -- re-run with the listed value.)")
        log("SUMMARY", "  4. once a SECOND window exists (window engine daily/monthly run):")
        log("SUMMARY", "     .venv\\Scripts\\python.exe cdc_main.py --window <newer>")
        log("SUMMARY", "     (prev auto-detected; the ledger shows real INSERT/UPDATE/DELETE deltas)")
        log("SUMMARY", "  5. optional -- stream the impurity engine's changes instead:")
        log("SUMMARY", "     in configs/cdc_base.yaml set source_tree: data/corrupted and")
        log("SUMMARY", "     allow_schema_drift: true, then repeat step 3 (drift rule adds columns).")
        log("SUMMARY", "  6. git add -A && git commit -m \"feat(cdc): milestone 6b -- incremental change stream + CDC change log (ADR-029)\"")
    log("SUMMARY", f"done ({'dry-run' if args.dry_run else 'applied'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
