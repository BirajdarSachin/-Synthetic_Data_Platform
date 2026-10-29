"""CDC runner (Milestone 6b, ADR-029).

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
