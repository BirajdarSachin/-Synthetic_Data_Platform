#!/usr/bin/env python3
"""bootstrap_milestone7a.py -- DQ test harness (Milestone 7a, ADR-025).

House-style bootstrap:
  NEW_FILES   -> create-if-missing; NEVER overwrite an existing non-empty file;
                 empty crash artifacts are healed ([HEAL]); --dry-run supported
  APPEND_FILES-> docs/decisions.md ADR-025, append-only, idempotent: skipped
                 when the ADR-025 marker is already present; the file is never
                 truncated (existing content preserved byte-for-byte)
  UPDATED_FILES -> v3 fix for 3 files the first 7a delivery already shipped:
                 src/core/dq/runner.py (self-diagnosing file discovery +
                 CSV-header validation of configured columns),
                 tests/test_dq_runner.py (discovery + header tests), and
                 configs/dq_base.yaml (real entity names + drill paths).
                 Byte-identical files are left untouched; review with git diff

What it delivers (Phase 7a -- DQ harness as an output-tree consumer):
  src/core/dq/checks.py     fixed 7a suite: not_null, fk_integrity,
                            value_in_range, pk_unique (mirrors the impurity
                            chain; catches every corruption condition)
  src/core/dq/runner.py     config loading, file discovery, parent key sets,
                            report building (atomic JSON), modes clean/corrupt
  dq_main.py                entry point: --tree --config --mode --report,
                            exit codes 0/1/130, [DQ] log tag (ADR-025)
  configs/dq_base.yaml      DQ config (field names follow Milestone-3/4 schema)
  tests/test_dq_checks.py   unit tests (checks)
  tests/test_dq_runner.py   integration tests (runner + config)
  docs/decisions.md         ADR-025 appended

After running this bootstrap:  python -m pytest tests/test_dq_checks.py
tests/test_dq_runner.py -q  then the two dq_main.py commands printed in the
SUMMARY section.

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
        "src/__init__.py",
        r'''"""src package marker (created only if missing)."""
''',
        "src package marker (created only if missing)",
    ),
    (
        "src/core/__init__.py",
        r'''"""src.core package marker (created only if missing)."""
''',
        "src.core package marker (created only if missing)",
    ),
    (
        "src/core/dq/__init__.py",
        r'''"""Data Quality (DQ) harness — Milestone 7a.

Consumer-side checks over generated output trees (data/generated, data/quality).
See docs/decisions.md ADR-025: the harness reads files + config only; it never
imports generator, writer, or impurity internals.
"""
''',
        "DQ package marker",
    ),
    (
        "src/core/dq/checks.py",
        r'''"""DQ check framework (Milestone 7a, ADR-025).

Design rules:
  * A Check consumes rows of ONE entity file and returns a frozen CheckResult.
  * Checks never mutate rows, never write files, and never read other files --
    cross-entity context (parent key sets) is injected by the runner.
  * Rows are plain mappings (csv.DictReader dicts): all values are strings.
  * CSV null == empty string. Value-based checks SKIP empty cells; whether an
    empty cell is legal is exclusively the not_null check's business. This
    mirrors the impurity engine's Law 2 (null wins first; None is sticky).
  * Each check keeps a bounded sample of violations (default 5) plus the full
    count, so reports stay small on 10M-row trees.

The 7a suite mirrors the impurity chain:
  not_null -> fk_integrity -> value_in_range -> pk_unique
  (duplicates and cross-window PK union arrive in 7b.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Mapping

Row = Mapping[str, str]

DEFAULT_MAX_SAMPLES = 5


@dataclass(frozen=True)
class Violation:
    """One concrete bad row, reported with enough context to find it again."""

    row_number: int  # 1-based data row number within the file (header excluded)
    column: str
    value: str
    reason: str


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check over one file. Immutable, report-ready."""

    check: str
    entity: str
    file: str  # path relative to the tree root, POSIX style
    rows_checked: int
    violation_count: int
    samples: tuple[Violation, ...] = ()

    @property
    def passed(self) -> bool:
        return self.violation_count == 0


class DqConfigError(ValueError):
    """Raised for any DQ config problem the runner can detect at startup."""


def _cell(row: Row, column: str) -> str:
    """DictReader can yield None for short rows; normalise to empty string."""
    value = row.get(column)
    return "" if value is None else value


class Check:
    """Base class for all DQ checks.

    Two extension shapes are supported:
      * row-wise: implement check_row(); the shared run() streams rows for you;
      * stream-wise: override run() when state spans rows (e.g. pk_unique's
        seen-set). A check implementing neither raises NotImplementedError on
        first use -- loud, by design.
    """

    name: str = "check"

    def __init__(self, entity: str, max_samples: int = DEFAULT_MAX_SAMPLES) -> None:
        self.entity = entity
        self.max_samples = max_samples

    def check_row(self, row_number: int, row: Row) -> Violation | None:
        """Return a Violation for a bad row, or None if the row is fine."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement check_row() or override run()"
        )

    def run(self, file_label: str, rows: Iterator[Row]) -> CheckResult:
        violation_count = 0
        samples: list[Violation] = []
        rows_checked = 0
        for row_number, row in enumerate(rows, start=1):
            rows_checked += 1
            violation = self.check_row(row_number, row)
            if violation is not None:
                violation_count += 1
                if len(samples) < self.max_samples:
                    samples.append(violation)
        return CheckResult(
            check=self.name,
            entity=self.entity,
            file=file_label,
            rows_checked=rows_checked,
            violation_count=violation_count,
            samples=tuple(samples),
        )


class NotNullCheck(Check):
    """Fails rows where a required column is empty (CSV null)."""

    name = "not_null"

    def __init__(self, entity: str, required: tuple[str, ...]) -> None:
        super().__init__(entity)
        if not required:
            raise DqConfigError(f"{entity}: not_null check needs at least one required column")
        self.required = tuple(required)

    def check_row(self, row_number: int, row: Row) -> Violation | None:
        for column in self.required:
            if _cell(row, column) == "":
                return Violation(
                    row_number=row_number,
                    column=column,
                    value="",
                    reason="required column is empty",
                )
        return None


class FkIntegrityCheck(Check):
    """Fails rows whose FK value does not resolve to a parent key.

    Parent key sets are loaded by the runner from the SAME output tree, so the
    check sees the tree exactly as a downstream consumer would. On a quality
    tree this is the check that catches the impurity engine's invalid_fk rule:
    corrupted ids live in the reserved band past the parent max and never
    resolve.
    """

    name = "fk_integrity"

    def __init__(self, entity: str, fks: Mapping[str, str], parent_keys: Mapping[str, frozenset[str]]) -> None:
        super().__init__(entity)
        if not fks:
            raise DqConfigError(f"{entity}: fk_integrity check needs at least one foreign key")
        missing_parents = sorted({parent for parent in fks.values() if parent not in parent_keys})
        if missing_parents:
            raise DqConfigError(
                f"{entity}: parent key sets not loaded for: {', '.join(missing_parents)}"
            )
        self.fks = dict(fks)
        self.parent_keys = dict(parent_keys)

    def check_row(self, row_number: int, row: Row) -> Violation | None:
        for column, parent in self.fks.items():
            value = _cell(row, column)
            if value == "":
                continue  # empty FK is not_null's business, not ours
            if value not in self.parent_keys[parent]:
                return Violation(
                    row_number=row_number,
                    column=column,
                    value=value,
                    reason=f"no {parent} row with this key",
                )
        return None


@dataclass(frozen=True)
class RangeSpec:
    """Declared domain of a numeric column: inclusive bounds, int or float."""

    column: str
    minimum: float
    maximum: float
    is_integer: bool

    @staticmethod
    def parse(column: str, raw: Mapping[str, object]) -> "RangeSpec":
        unknown = sorted(set(raw) - {"min", "max", "type"})
        if unknown:
            raise DqConfigError(f"range for {column}: unknown keys {unknown}")
        try:
            minimum = float(raw["min"])  # type: ignore[arg-type]
            maximum = float(raw["max"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise DqConfigError(f"range for {column}: needs numeric 'min' and 'max'") from exc
        if minimum > maximum:
            raise DqConfigError(f"range for {column}: min > max")
        dtype = str(raw.get("type", "float")).lower()
        if dtype not in {"int", "float"}:
            raise DqConfigError(f"range for {column}: type must be int or float")
        return RangeSpec(
            column=column,
            minimum=minimum,
            maximum=maximum,
            is_integer=(dtype == "int"),
        )


class ValueInRangeCheck(Check):
    """Fails rows whose numeric value falls outside its declared domain.

    Empty cells are skipped (not_null's job). Unparseable values are
    violations in their own right: a DQ harness must not silently ignore
    data it cannot interpret.
    """

    name = "value_in_range"

    def __init__(self, entity: str, ranges: Mapping[str, Mapping[str, object]]) -> None:
        super().__init__(entity)
        if not ranges:
            raise DqConfigError(f"{entity}: value_in_range check needs at least one range")
        self.ranges = {column: RangeSpec.parse(column, raw) for column, raw in ranges.items()}

    def check_row(self, row_number: int, row: Row) -> Violation | None:
        for column, spec in self.ranges.items():
            raw_value = _cell(row, column)
            if raw_value == "":
                continue
            try:
                value: float = float(raw_value)
            except ValueError:
                return Violation(
                    row_number=row_number,
                    column=column,
                    value=raw_value,
                    reason="not a number",
                )
            if spec.is_integer and "." in raw_value:
                return Violation(
                    row_number=row_number,
                    column=column,
                    value=raw_value,
                    reason="expected an integer value",
                )
            if not (spec.minimum <= value <= spec.maximum):
                return Violation(
                    row_number=row_number,
                    column=column,
                    value=raw_value,
                    reason=f"outside declared range [{spec.minimum}, {spec.maximum}]",
                )
        return None


class PkUniqueCheck(Check):
    """Fails rows whose primary key repeats within the same file.

    This is the check that catches the impurity engine's duplicate rule:
    duplicates are exact copies of the final corrupted record, so they repeat
    the PK (and the whole row) inside one file. On a clean tree the PK union
    across files is exactly 1..N, so this check passes there by construction.
    """

    name = "pk_unique"

    def __init__(self, entity: str, primary_key: str) -> None:
        super().__init__(entity)
        if not primary_key:
            raise DqConfigError(f"{entity}: pk_unique check needs a primary_key")
        self.primary_key = primary_key

    def run(self, file_label: str, rows: Iterator[Row]) -> CheckResult:
        seen: set[str] = set()
        violation_count = 0
        samples: list[Violation] = []
        rows_checked = 0
        for row_number, row in enumerate(rows, start=1):
            rows_checked += 1
            value = _cell(row, self.primary_key)
            if value == "":
                violation = Violation(
                    row_number=row_number,
                    column=self.primary_key,
                    value="",
                    reason="primary key is empty",
                )
            elif value in seen:
                violation = Violation(
                    row_number=row_number,
                    column=self.primary_key,
                    value=value,
                    reason=f"primary key repeats in {file_label}",
                )
            else:
                seen.add(value)
                continue
            violation_count += 1
            if len(samples) < self.max_samples:
                samples.append(violation)
        return CheckResult(
            check=self.name,
            entity=self.entity,
            file=file_label,
            rows_checked=rows_checked,
            violation_count=violation_count,
            samples=tuple(samples),
        )
''',
        "DQ check framework (7a suite)",
    ),
    (
        "src/core/dq/runner.py",
        r'''"""DQ runner (Milestone 7a, ADR-025).

Wires config -> checks -> report over one output tree.

Pipeline position (consumer side):

    data/generated/  or  data/quality/
            │
            ▼
    DqRunner (--config configs/dq_base.yaml)
            │   glob entity files, load parent key sets,
            │   stream rows through the fixed 7a check suite
            ▼
    DqReport -> dq_report.json + [DQ] log lines + exit code

Modes (the acceptance drill from docs/impurity_guide.md §11, automated):
  clean   -- ok iff ZERO violations anywhere (validate the baseline)
  corrupt -- ok iff AT LEAST ONE check fired (prove the dirt is detectable)

7a deliberately does NOT touch the manifest, Parquet, row_count checks,
full-row duplicate detection, or cross-window PK union -- those are 7b/7c.
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from src.core.dq.checks import (
    Check,
    CheckResult,
    DqConfigError,
    FkIntegrityCheck,
    NotNullCheck,
    PkUniqueCheck,
    ValueInRangeCheck,
)

REPORT_VERSION = 1
CHECK_NAMES = ("not_null", "fk_integrity", "value_in_range", "pk_unique")


@dataclass(frozen=True)
class EntityDqSpec:
    """What we know about one entity, from config only."""

    entity: str
    primary_key: str
    required: tuple[str, ...]
    foreign_keys: Mapping[str, str]  # column -> parent entity
    ranges: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class DqConfig:
    """Parsed configs/dq_base.yaml."""

    entities: Mapping[str, EntityDqSpec]
    enabled: Mapping[str, bool]  # check name -> on/off (absent = enabled)

    @staticmethod
    def load(raw: Mapping[str, object]) -> "DqConfig":
        dq = raw.get("dq")
        if not isinstance(dq, Mapping):
            raise DqConfigError("config needs a top-level 'dq:' block")
        raw_entities = dq.get("entities")
        if not isinstance(raw_entities, Mapping) or not raw_entities:
            raise DqConfigError("dq.entities must list at least one entity")

        entities: dict[str, EntityDqSpec] = {}
        for entity, spec_raw in raw_entities.items():
            if not isinstance(spec_raw, Mapping):
                raise DqConfigError(f"{entity}: entity spec must be a mapping")
            primary_key = str(spec_raw.get("primary_key", "")).strip()
            if not primary_key:
                raise DqConfigError(f"{entity}: primary_key is required")
            required = tuple(str(c) for c in spec_raw.get("required", ()))
            fks_raw = spec_raw.get("foreign_keys", {})
            if not isinstance(fks_raw, Mapping):
                raise DqConfigError(f"{entity}: foreign_keys must be a mapping")
            foreign_keys = {str(c): str(p) for c, p in fks_raw.items()}
            ranges_raw = spec_raw.get("ranges", {})
            if not isinstance(ranges_raw, Mapping):
                raise DqConfigError(f"{entity}: ranges must be a mapping")
            ranges = {str(c): dict(r) for c, r in ranges_raw.items()}  # type: ignore[arg-type]
            entities[str(entity)] = EntityDqSpec(
                entity=str(entity),
                primary_key=primary_key,
                required=required,
                foreign_keys=foreign_keys,
                ranges=ranges,
            )

        raw_checks = dq.get("checks", {})
        if not isinstance(raw_checks, Mapping):
            raise DqConfigError("dq.checks must be a mapping of check toggles")
        unknown = sorted(set(raw_checks) - set(CHECK_NAMES))
        if unknown:
            raise DqConfigError(f"unknown check name(s) in dq.checks: {unknown}")
        enabled = {
            name: bool((raw_checks.get(name) or {}).get("enabled", True))
            for name in CHECK_NAMES
        }
        if not any(enabled.values()):
            raise DqConfigError("all checks are disabled -- nothing to do")
        return DqConfig(entities=entities, enabled=enabled)

    def build_checks(
        self, spec: EntityDqSpec, parent_keys: Mapping[str, frozenset[str]]
    ) -> list[Check]:
        """Instantiate the enabled checks for one entity (fixed 7a order)."""
        checks: list[Check] = []
        if self.enabled["not_null"] and spec.required:
            checks.append(NotNullCheck(spec.entity, spec.required))
        if self.enabled["fk_integrity"] and spec.foreign_keys:
            checks.append(FkIntegrityCheck(spec.entity, spec.foreign_keys, parent_keys))
        if self.enabled["value_in_range"] and spec.ranges:
            checks.append(ValueInRangeCheck(spec.entity, spec.ranges))
        if self.enabled["pk_unique"]:
            checks.append(PkUniqueCheck(spec.entity, spec.primary_key))
        return checks


@dataclass(frozen=True)
class DqReport:
    """Aggregate outcome for one tree. JSON-ready via to_dict()."""

    tree: str
    mode: str
    ok: bool
    files_checked: int
    rows_checked: int
    violations_total: int
    results: tuple[CheckResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": REPORT_VERSION,
            "tree": self.tree,
            "mode": self.mode,
            "ok": self.ok,
            "files_checked": self.files_checked,
            "rows_checked": self.rows_checked,
            "violations_total": self.violations_total,
            "results": [asdict(r) for r in self.results],
        }


class DqRunner:
    def __init__(
        self,
        tree: Path,
        config: DqConfig,
        mode: str,
        log: Callable[[str, str], None],
    ) -> None:
        if mode not in {"clean", "corrupt"}:
            raise DqConfigError(f"mode must be 'clean' or 'corrupt', got {mode!r}")
        if not tree.is_dir():
            raise DqConfigError(
                f"output tree not found: {tree} "
                f"(generate it first -- a clean run populates data/generated, "
                f"a quality run populates data/quality)"
            )
        self.tree = tree
        self.config = config
        self.mode = mode
        self.log = log

    # -- discovery ---------------------------------------------------------

    def entity_files(self, entity: str) -> list[Path]:
        """All CSV files of one entity in the tree, any layout, sorted.

        Primary: the conventional {tree}/{entity}/ directory (both platform
        layouts nest under the entity dir -- snapshot reference_date=... and
        windowed event_date=...).
        Fallback (partition-first layouts): any CSV anywhere under the tree
        whose stem is exactly '{entity}' or '{entity}_<date-ish>' (writer
        convention '{entity}_{date}.csv'). The leading-digit guard after the
        underscore keeps 'orders' from sweeping in 'order_items_2026-01-01.csv'.
        """
        base = self.tree / entity
        files = sorted(base.rglob("*.csv")) if base.is_dir() else []
        if not files:
            stem_pattern = re.compile(re.escape(entity) + r"_[0-9][0-9\-_.]*")
            files = sorted(
                p for p in self.tree.rglob("*.csv")
                if p.stem == entity or stem_pattern.fullmatch(p.stem)
            )
        if not files:
            raise DqConfigError(self._no_files_diagnostic(entity))
        return files

    def _no_files_diagnostic(self, entity: str) -> str:
        """Fail-fast that shows the operator WHAT the tree actually contains."""
        LIMIT = 8
        top = sorted(
            p.name + ("/" if p.is_dir() else "") for p in self.tree.iterdir()
        )
        if top:
            top_repr = ", ".join(top[:LIMIT]) + (f" ... (+{len(top) - LIMIT} more)" if len(top) > LIMIT else "")
        else:
            top_repr = "(tree exists but is EMPTY)"
        csvs = sorted(
            p.relative_to(self.tree).as_posix() for p in self.tree.rglob("*.csv")
        )
        if csvs:
            csv_repr = ", ".join(csvs[:LIMIT]) + (f" ... (+{len(csvs) - LIMIT} more)" if len(csvs) > LIMIT else "")
        else:
            csv_repr = "(no .csv files anywhere under the tree)"
        return (
            f"no CSV files found for entity '{entity}' under {self.tree}; "
            f"tree top-level: {top_repr}; csv files: {csv_repr}; "
            "-> if the tree is empty or the entity is missing, generate the data "
            "first (a clean run populates data/generated, a quality run populates "
            "data/quality); if your entity is named differently, fix dq.entities "
            "in the DQ config"
        )

    def _file_label(self, path: Path) -> str:
        return path.relative_to(self.tree).as_posix()

    # -- context -----------------------------------------------------------

    def load_parent_keys(self, parents: Iterable[str]) -> dict[str, frozenset[str]]:
        """Load each parent's PK value set once, from the same tree."""
        keys: dict[str, frozenset[str]] = {}
        for parent in parents:
            spec = self.config.entities.get(parent)
            if spec is None:
                raise DqConfigError(
                    f"foreign key points at {parent!r}, which has no dq.entities entry"
                )
            files = self.entity_files(parent)
            self._validate_header(parent, spec, files[0])
            values: set[str] = set()
            for path in files:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        value = row.get(spec.primary_key)
                        if value:
                            values.add(value)
            keys[parent] = frozenset(values)
            self.log("DQ", f"loaded {len(values)} parent keys for {parent}")
        return keys

    # -- header validation --------------------------------------------------

    def _validate_header(self, entity: str, spec: EntityDqSpec, path: Path) -> None:
        """Fail-fast: every configured column must exist in the real CSV header.

        A wrong column name would otherwise surface as confusing all-row
        violations (or, worse, as silently EMPTY parent key sets in
        load_parent_keys). One header read per entity at startup makes any
        config/schema mismatch a guided one-line fix.
        """
        with path.open("r", encoding="utf-8", newline="") as handle:
            first_line = handle.readline()
        header = next(csv.reader([first_line]), [])
        if header and header[0].startswith("\ufeff"):
            header[0] = header[0].lstrip("\ufeff")
        missing: list[str] = []
        if spec.primary_key not in header:
            missing.append(f"primary_key={spec.primary_key!r}")
        for col in spec.required:
            if col not in header:
                missing.append(f"required={col!r}")
        for col in spec.foreign_keys:
            if col not in header:
                missing.append(f"foreign_keys={col!r}")
        for col in spec.ranges:
            if col not in header:
                missing.append(f"ranges={col!r}")
        if missing:
            raise DqConfigError(
                f"{entity}: configured columns not in CSV header of "
                f"{self._file_label(path)}; actual header: {header}; "
                f"missing: {', '.join(missing)}; "
                "-> fix dq.entities in the DQ config to match the real column names"
            )

    # -- execution ---------------------------------------------------------

    def run(self) -> DqReport:
        results: list[CheckResult] = []
        parents_needed: set[str] = {
            parent
            for spec in self.config.entities.values()
            for parent in spec.foreign_keys.values()
        }
        parent_keys = self.load_parent_keys(sorted(parents_needed))

        for entity, spec in self.config.entities.items():
            checks = self.config.build_checks(spec, parent_keys)
            files = self.entity_files(entity)
            self._validate_header(entity, spec, files[0])
            self.log("DQ", f"{entity}: {len(files)} file(s), checks={[c.name for c in checks]}")
            for path in files:
                results.extend(self._check_file(path, entity, checks))

        violations_total = sum(r.violation_count for r in results)
        # Every check over the same file reports the same rows_checked; count
        # each FILE once, not once per check.
        rows_checked = sum({r.file: r.rows_checked for r in results}.values())
        ok = self._evaluate(results)
        report = DqReport(
            tree=self.tree.as_posix(),
            mode=self.mode,
            ok=ok,
            files_checked=len({r.file for r in results}),
            rows_checked=rows_checked,
            violations_total=violations_total,
            results=tuple(results),
        )
        self.log(
            "DQ",
            f"summary: files={report.files_checked} rows={rows_checked} "
            f"violations={violations_total} mode={self.mode} ok={ok}",
        )
        return report

    def _check_file(self, path: Path, entity: str, checks: list[Check]) -> list[CheckResult]:
        spec = self.config.entities[entity]
        label = self._file_label(path)
        outcomes: list[CheckResult] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader: Iterator[Mapping[str, str]] = csv.DictReader(handle)
            # Rows are materialised once and shared across this file's checks;
            # checks are read-only over them, keeping one pass over the file.
            rows = [dict(r) for r in reader]
        for check in checks:
            result = check.run(label, iter(rows))
            outcomes.append(result)
            self._log_result(result, spec.primary_key)
        return outcomes

    def _log_result(self, result: CheckResult, primary_key: str) -> None:
        sample = ""
        if result.samples:
            first = result.samples[0]
            sample = f" e.g. row {first.row_number} {first.column}={first.value!r} ({first.reason})"
        self.log(
            "DQ",
            f"{result.file} [{result.check}] rows={result.rows_checked} "
            f"violations={result.violation_count}{sample}",
        )

    def _evaluate(self, results: list[CheckResult]) -> bool:
        if self.mode == "clean":
            return all(r.passed for r in results)
        # corrupt: we REQUIRE the dirt to be detected by at least one check
        return any(not r.passed for r in results)


def write_report(report: DqReport, path: Path, log: Callable[[str, str], None]) -> None:
    """Atomic JSON report write (same .tmp + os.replace discipline as writers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    log("DQ", f"report written: {path.as_posix()}")
''',
        "DQ runner + config + report",
    ),
    (
        "dq_main.py",
        r'''"""dq_main.py -- DQ harness entry point (Milestone 7a, ADR-025).

Validates ONE output tree (data/generated or data/quality) against a DQ config.

Usage (from the project root, venv Style B):

    .venv\\Scripts\\python.exe dq_main.py --tree data\\generated --mode clean
    .venv\\Scripts\\python.exe dq_main.py --tree data\\quality   --mode corrupt

Exit codes (house contract):
    0    ok      -- clean: zero violations | corrupt: at least one check fired
    1    failure -- config error, missing tree/files, or expectation not met
    130  interrupted (Ctrl+C)

Log contract (ADR-0009): same tags as every run, plus [DQ] (new in 7a,
documented in ADR-025). Report defaults to data/dq/report.json (atomic write).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.core.dq.checks import DqConfigError
from src.core.dq.runner import DqConfig, DqRunner, write_report


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DQ checks over one output tree (Milestone 7a)."
    )
    parser.add_argument("--tree", required=True, help="output tree to validate, e.g. data/generated or data/quality")
    parser.add_argument("--config", default="configs/dq_base.yaml", help="DQ config (default: configs/dq_base.yaml)")
    parser.add_argument(
        "--mode",
        choices=("clean", "corrupt"),
        default="clean",
        help="clean: all checks must pass | corrupt: at least one check must fire",
    )
    parser.add_argument("--report", default="data/dq/report.json", help="where to write the JSON report")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log("STARTUP", f"cwd={Path.cwd()}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    log("CONFIG", f"tree={Path(args.tree).as_posix()} config={args.config} mode={args.mode}")

    config_path = Path(args.config)
    if not config_path.is_file():
        log("CONFIG", f"DQ config not found: {config_path}")
        return 1
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = DqConfig.load(raw)
    log(
        "CONFIG",
        "checks enabled: " + ", ".join(name for name, on in config.enabled.items() if on),
    )
    log("CONFIG", "entities: " + ", ".join(config.entities))

    report = DqRunner(Path(args.tree), config, args.mode, log).run()
    write_report(report, Path(args.report), log)

    log("SUMMARY", f"ok={report.ok} violations={report.violations_total} rows={report.rows_checked}")
    if report.ok:
        log("SUMMARY", "result as expected (exit 0)")
        return 0
    log("SUMMARY", f"result NOT as expected for mode={args.mode} (exit 1)")
    return 1


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C)")
        return 130
    except DqConfigError as exc:
        log("CONFIG", f"invalid DQ configuration: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
''',
        "DQ entry point (root)",
    ),
    (
        "configs/dq_base.yaml",
        r'''# =============================================================================
# dq_base.yaml -- DQ check configuration (Milestone 7a, v3 -- ADR-025)
#
# v3: entity names aligned with THIS repository's real schema (singular names:
# order, order_item, employee, review, payment, customer; the generation log
# reported datasets=8 -- add any entity still missing below, same shape).
#
# The harness now VALIDATES every configured column against each entity's
# real CSV header at startup and fail-fasts printing the actual header, so a
# wrong column guess is a guided one-line fix, never a silent misfire.
#
# Drills on this machine (quality root is data/corrupted per your [WRITER] log):
#   clean    .venv\Scripts\python.exe dq_main.py --tree data\generated  --mode clean
#   corrupt  .venv\Scripts\python.exe dq_main.py --tree data\corrupted --mode corrupt
#
# The harness is a consumer of output trees: it reads CSV files and this
# config only. Checks default to enabled=true when absent from dq.checks.
# =============================================================================

dq:
  checks:
    not_null:
      enabled: true          # required columns must be populated
    fk_integrity:
      enabled: true          # FK values must resolve within the same tree
    value_in_range:
      enabled: true          # numeric columns must sit inside declared ranges
    pk_unique:
      enabled: true          # primary keys must not repeat within a file

  entities:
    # --- customer: guessed from order's invalid_fk rule firing; the startup
    # header check confirms or corrects the pk name on first run.
    customer:
      primary_key: customer_id
      required: [customer_id]

    order:
      primary_key: order_id
      required: [order_id]
      foreign_keys:
        customer_id: customer   # TODO verify column + parent against the real header

    order_item:
      primary_key: order_item_id
      required: [order_item_id]
      # out_of_range fired on this entity (65 rows) -- wire the real column and
      # business range once confirmed from the header, e.g.:
      # ranges:
      #   quantity: {min: 1, max: 100, type: int}

    employee:
      primary_key: employee_id
      required: [employee_id]

    review:
      primary_key: review_id
      required: [review_id]

    payment:
      primary_key: payment_id
      required: [payment_id]

    # --- datasets=8 in your log: add the remaining entities here, same shape.
    # Null rule fired on order (57 rows): once you know WHICH column was
    # nulled, add it to that entity's required list so not_null catches it.
''',
        "DQ check configuration",
    ),
    (
        "tests/test_dq_checks.py",
        r'''"""Unit tests for DQ checks (Milestone 7a, ADR-025).

Each check is tested in isolation over synthetic rows -- no output trees, no
generator internals. The sys.path shim keeps these tests self-contained
regardless of pytest/ini configuration.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.dq.checks import (
    DqConfigError,
    FkIntegrityCheck,
    NotNullCheck,
    PkUniqueCheck,
    RangeSpec,
    ValueInRangeCheck,
)


def rows(*rows_data):
    return iter([dict(r) for r in rows_data])


# ---------------------------------------------------------------- not_null


class TestNotNullCheck:
    def test_passes_when_required_present(self):
        check = NotNullCheck("customers", ("customer_id", "name"))
        result = check.run("f.csv", rows({"customer_id": "1", "name": "Acme"}))
        assert result.passed and result.rows_checked == 1

    def test_fails_empty_required_column(self):
        check = NotNullCheck("customers", ("customer_id", "name"))
        result = check.run("f.csv", rows({"customer_id": "1", "name": ""}))
        assert not result.passed
        assert result.samples[0].column == "name"
        assert result.samples[0].reason == "required column is empty"

    def test_missing_key_treated_as_empty(self):
        check = NotNullCheck("orders", ("total_amount",))
        result = check.run("f.csv", rows({"order_id": "1"}))
        assert not result.passed

    def test_counts_all_violations_but_samples_bounded(self):
        check = NotNullCheck("orders", ("coupon_code",))
        data = rows(*[{"order_id": str(i), "coupon_code": ""} for i in range(50)])
        result = check.run("f.csv", data)
        assert result.violation_count == 50
        assert len(result.samples) == 5  # default max_samples

    def test_empty_required_list_is_config_error(self):
        with pytest.raises(DqConfigError):
            NotNullCheck("orders", ())


# ------------------------------------------------------------ fk_integrity


class TestFkIntegrityCheck:
    def make_check(self):
        parents = {"customers": frozenset({"1", "2", "3"})}
        return FkIntegrityCheck("orders", {"customer_id": "customers"}, parents)

    def test_passes_when_fk_resolves(self):
        result = self.make_check().run("f.csv", rows({"customer_id": "2"}))
        assert result.passed

    def test_fails_on_unknown_fk(self):
        result = self.make_check().run("f.csv", rows({"customer_id": "999"}))
        assert not result.passed
        assert result.samples[0].value == "999"
        assert "customers" in result.samples[0].reason

    def test_empty_fk_skipped_not_null_boundary(self):
        result = self.make_check().run("f.csv", rows({"customer_id": ""}))
        assert result.passed  # emptiness is not_null's business (Law 2)

    def test_missing_parent_keys_is_config_error(self):
        with pytest.raises(DqConfigError, match="parent key sets"):
            FkIntegrityCheck("orders", {"customer_id": "customers"}, {})

    def test_no_fks_is_config_error(self):
        with pytest.raises(DqConfigError):
            FkIntegrityCheck("orders", {}, {"customers": frozenset()})


# ----------------------------------------------------------- value_in_range


class TestValueInRange:
    def test_passes_inside_bounds(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        result = check.run("f.csv", rows({"quantity": "3"}))
        assert result.passed

    def test_fails_below_minimum(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        result = check.run("f.csv", rows({"quantity": "-3"}))
        assert not result.passed
        assert "outside declared range" in result.samples[0].reason

    def test_fails_above_maximum(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        result = check.run("f.csv", rows({"quantity": "101"}))
        assert not result.passed

    def test_bounds_inclusive(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        assert check.run("f.csv", rows({"quantity": "1"}, {"quantity": "100"})).passed

    def test_empty_cell_skipped(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        assert check.run("f.csv", rows({"quantity": ""})).passed

    def test_unparseable_value_is_violation(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        result = check.run("f.csv", rows({"quantity": "abc"}))
        assert result.samples[0].reason == "not a number"

    def test_float_rejected_for_int_column(self):
        check = ValueInRangeCheck("orders", {"quantity": {"min": 1, "max": 100, "type": "int"}})
        result = check.run("f.csv", rows({"quantity": "2.5"}))
        assert result.samples[0].reason == "expected an integer value"

    def test_min_greater_than_max_is_config_error(self):
        with pytest.raises(DqConfigError, match="min > max"):
            ValueInRangeCheck("orders", {"quantity": {"min": 10, "max": 1}})

    def test_unknown_range_key_is_config_error(self):
        with pytest.raises(DqConfigError, match="unknown keys"):
            RangeSpec.parse("q", {"min": 1, "max": 2, "typo": 3})


# ---------------------------------------------------------------- pk_unique


class TestPkUniqueCheck:
    def test_passes_on_distinct_keys(self):
        check = PkUniqueCheck("orders", "order_id")
        result = check.run("f.csv", rows({"order_id": "1"}, {"order_id": "2"}))
        assert result.passed

    def test_fails_on_repeated_key(self):
        check = PkUniqueCheck("orders", "order_id")
        result = check.run("f.csv", rows({"order_id": "1"}, {"order_id": "1"}))
        assert not result.passed
        assert result.samples[0].row_number == 2

    def test_empty_pk_is_violation(self):
        check = PkUniqueCheck("orders", "order_id")
        result = check.run("f.csv", rows({"order_id": ""}))
        assert result.samples[0].reason == "primary key is empty"

    def test_missing_pk_column_is_violation(self):
        check = PkUniqueCheck("orders", "order_id")
        result = check.run("f.csv", rows({"total_amount": "9.9"}))
        assert not result.passed


# ------------------------------------------------------------- RangeSpec


class TestRangeSpecParsing:
    def test_defaults_to_float(self):
        spec = RangeSpec.parse("total_amount", {"min": 0, "max": 10})
        assert spec.is_integer is False

    def test_non_numeric_bounds_is_config_error(self):
        with pytest.raises(DqConfigError):
            RangeSpec.parse("q", {"min": "a", "max": 2})

    def test_bad_type_is_config_error(self):
        with pytest.raises(DqConfigError):
            RangeSpec.parse("q", {"min": 1, "max": 2, "type": "decimal"})
''',
        "unit tests: checks",
    ),
    (
        "tests/test_dq_runner.py",
        r'''"""Integration tests for the DQ runner and config (Milestone 7a, ADR-025).

Builds tiny synthetic output trees (customers + orders, snapshot layout) in
tmp_path and runs DqRunner end-to-end in both modes:
  clean   tree  -> ok=True
  corrupt tree  -> ok=False in clean mode, ok=True in corrupt mode
Mirrors docs/impurity_guide.md section 11 (the acceptance drill) at unit scale.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.core.dq.checks import DqConfigError
from src.core.dq.runner import DqConfig, DqRunner, write_report

DATE = "2026-10-24"


def make_tree(root: Path, orders_rows: list[dict], customers_rows: list[dict] | None = None):
    """Lay out a snapshot tree: {entity}/reference_date={date}/{entity}_{date}.csv"""
    if customers_rows is None:
        customers_rows = [{"customer_id": str(i), "name": f"c{i}", "registration_date": DATE}
                          for i in range(1, 4)]
    for entity, rows_data in (("customers", customers_rows), ("orders", orders_rows)):
        folder = root / entity / f"reference_date={DATE}"
        folder.mkdir(parents=True, exist_ok=True)
        header = list(rows_data[0].keys())
        lines = [",".join(header)]
        lines += [",".join(str(row[c]) for c in header) for row in rows_data]
        (folder / f"{entity}_{DATE}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def base_config() -> dict:
    return {
        "dq": {
            "entities": {
                "customers": {
                    "primary_key": "customer_id",
                    "required": ["customer_id", "name"],
                },
                "orders": {
                    "primary_key": "order_id",
                    "required": ["order_id", "customer_id", "total_amount"],
                    "foreign_keys": {"customer_id": "customers"},
                    "ranges": {"quantity": {"min": 1, "max": 100, "type": "int"}},
                },
            }
        }
    }


def quiet_runner(tree: Path, config: DqConfig, mode: str) -> DqRunner:
    return DqRunner(tree, config, mode, log=lambda tag, msg: None)


def clean_orders() -> list[dict]:
    return [
        {"order_id": str(i), "customer_id": "1", "order_date": DATE,
         "total_amount": f"{10 * i}.50", "quantity": str(i)}
        for i in range(1, 6)
    ]


# ------------------------------------------------------------------ config


class TestDqConfig:
    def test_loads_valid_config(self):
        config = DqConfig.load(base_config())
        assert set(config.entities) == {"customers", "orders"}
        assert all(config.enabled.values())  # absent toggles default to enabled

    def test_missing_dq_block_is_config_error(self):
        with pytest.raises(DqConfigError, match="'dq:'"):
            DqConfig.load({})

    def test_no_entities_is_config_error(self):
        with pytest.raises(DqConfigError, match="entities"):
            DqConfig.load({"dq": {"entities": {}}})

    def test_missing_primary_key_is_config_error(self):
        raw = {"dq": {"entities": {"orders": {"required": ["a"]}}}}
        with pytest.raises(DqConfigError, match="primary_key"):
            DqConfig.load(raw)

    def test_unknown_check_toggle_is_config_error(self):
        raw = base_config()
        raw["dq"]["checks"] = {"not_a_check": {"enabled": True}}
        with pytest.raises(DqConfigError, match="unknown check"):
            DqConfig.load(raw)

    def test_all_disabled_is_config_error(self):
        raw = base_config()
        raw["dq"]["checks"] = {
            name: {"enabled": False}
            for name in ("not_null", "fk_integrity", "value_in_range", "pk_unique")
        }
        with pytest.raises(DqConfigError, match="all checks are disabled"):
            DqConfig.load(raw)

    def test_yaml_roundtrip(self):
        config = DqConfig.load(yaml.safe_load(yaml.safe_dump(base_config())))
        assert config.entities["orders"].primary_key == "order_id"


# ------------------------------------------------------------------ runner


class TestRunnerCleanTree:
    def test_clean_tree_passes_in_clean_mode(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True
        assert report.violations_total == 0
        assert report.files_checked == 2
        assert report.rows_checked == 8  # 3 customers + 5 orders

    def test_clean_tree_fails_corrupt_expectation(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "corrupt").run()
        assert report.ok is False  # no dirt found, but dirt was required


class TestRunnerCorruptTree:
    def corrupt_orders(self) -> list[dict]:
        rows_data = clean_orders()
        rows_data[1]["customer_id"] = "999"         # invalid_fk: reserved band > parent max
        rows_data[2]["quantity"] = "-3"             # out_of_range
        rows_data.append(dict(rows_data[3]))        # duplicate: exact copy incl. PK
        return rows_data

    def test_corrupt_tree_caught_in_clean_mode(self, tmp_path):
        tree = make_tree(tmp_path, self.corrupt_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is False
        fired = {r.check for r in report.results if not r.passed}
        assert fired == {"fk_integrity", "value_in_range", "pk_unique"}

    def test_corrupt_tree_passes_in_corrupt_mode(self, tmp_path):
        tree = make_tree(tmp_path, self.corrupt_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "corrupt").run()
        assert report.ok is True

    def test_each_rule_caught_individually(self, tmp_path):
        """One corruption at a time -- each impurity rule has its own catcher."""
        config = DqConfig.load(base_config())
        cases = [
            ("invalid_fk", lambda r: r.update(customer_id="999"), "fk_integrity"),
            ("out_of_range", lambda r: r.update(quantity="0"), "value_in_range"),
            ("duplicate", lambda r: None, "pk_unique"),  # handled below
        ]
        for name, mutate, expected_check in cases:
            rows_data = clean_orders()
            if name == "duplicate":
                rows_data.append(dict(rows_data[0]))
            else:
                mutate(rows_data[0])
            tree = make_tree(tmp_path / name, rows_data)
            report = quiet_runner(tree, config, "clean").run()
            fired = {r.check for r in report.results if not r.passed}
            assert fired == {expected_check}, f"{name} should fire {expected_check} only"


class TestDiscovery:
    """File discovery must be layout-tolerant AND self-diagnosing when empty."""

    def test_empty_tree_diagnostic_shows_contents(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tmp_path, config, "clean").run()
        msg = str(excinfo.value)
        assert "no CSV files found for entity 'customers'" in msg
        assert "(tree exists but is EMPTY)" in msg
        assert "generate the data first" in msg

    def test_wrong_entity_name_diagnostic_lists_actual_dirs(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["ghosts"] = {"primary_key": "ghost_id"}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tree, config, "clean").run()
        msg = str(excinfo.value)
        assert "no CSV files found for entity 'ghosts'" in msg
        assert "customers/" in msg and "orders/" in msg  # the actual tree contents

    def test_partition_first_layout_found_by_stem(self, tmp_path):
        """No entity dirs at all: {partition}/{entity}_{date}.csv must be found."""
        folder = tmp_path / f"reference_date={DATE}"
        folder.mkdir()
        customers = [{"customer_id": "1", "name": "c1", "registration_date": DATE}]
        header = ",".join(customers[0])
        (folder / f"customers_{DATE}.csv").write_text(
            header + "\n" + ",".join(customers[0].values()) + "\n", encoding="utf-8"
        )
        header_o = ",".join(clean_orders()[0])
        rows_o = "\n".join(",".join(r.values()) for r in clean_orders())
        (folder / f"orders_{DATE}.csv").write_text(
            header_o + "\n" + rows_o + "\n", encoding="utf-8"
        )
        config = DqConfig.load(base_config())
        report = quiet_runner(tmp_path, config, "clean").run()
        assert report.ok is True
        assert report.files_checked == 2

    def test_prefix_collision_guarded(self, tmp_path):
        """Entity 'orders' must NOT sweep in order_items_*.csv (digit guard)."""
        folder = tmp_path / f"reference_date={DATE}"
        folder.mkdir()
        (folder / f"order_items_{DATE}.csv").write_text(
            "order_item_id,order_id\n1,1\n", encoding="utf-8"
        )
        raw = {"dq": {"entities": {"orders": {"primary_key": "order_id",
                                             "required": ["order_id"]}}}}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="no CSV files found for entity 'orders'"):
            quiet_runner(tmp_path, config, "clean").run()

    def test_entity_dir_takes_precedence_over_stray_stem(self, tmp_path):
        """If {tree}/{entity}/ exists and has CSVs, strays elsewhere are ignored."""
        tree = make_tree(tmp_path, clean_orders())
        archive = tree / "archive"
        archive.mkdir()
        (archive / "customers_2020-01-01.csv").write_text(
            "customer_id,name\n9,old\n", encoding="utf-8"
        )
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True
        assert report.files_checked == 2  # archive strays not swept in


class TestHeaderValidation:
    """Every configured column must exist in the real CSV header (fail-fast).

    A wrong parent pk must raise at load_parent_keys time -- silently loading
    an empty key set would flag every FK row as a violation instead.
    """

    def test_wrong_pk_prints_actual_header(self, tmp_path):
        # raises inside load_parent_keys: orders' FK points at customers,
        # whose (wrong) pk is validated before any key is loaded
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["customers"]["primary_key"] = "cust_id"
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tree, config, "clean").run()
        msg = str(excinfo.value)
        assert "customers" in msg
        assert "actual header" in msg
        assert "primary_key='cust_id'" in msg   # the wrong config column...
        assert "customer_id" in msg             # ...and the real header printed

    def test_wrong_required_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["required"] = ["order_id", "amount"]
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="required='amount'"):
            quiet_runner(tree, config, "clean").run()

    def test_wrong_fk_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["foreign_keys"] = {"cust_id": "customers"}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="foreign_keys='cust_id'"):
            quiet_runner(tree, config, "clean").run()

    def test_wrong_range_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["ranges"] = {"qty": {"min": 1, "max": 5, "type": "int"}}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="ranges='qty'"):
            quiet_runner(tree, config, "clean").run()


class TestRunnerEdgeCases:
    def test_missing_entity_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="no CSV files found for entity 'customers'"):
            quiet_runner(tmp_path, config, "clean").run()

    def test_missing_tree_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="output tree not found"):
            quiet_runner(tmp_path / "nope", config, "clean")

    def test_bad_mode_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="mode"):
            quiet_runner(tmp_path, config, "paranoid")

    def test_fk_parent_without_dq_entry_is_config_error(self, tmp_path):
        raw = base_config()
        raw["dq"]["entities"]["orders"]["foreign_keys"] = {"customer_id": "ghosts"}
        config = DqConfig.load(raw)
        tree = make_tree(tmp_path, clean_orders())
        with pytest.raises(DqConfigError, match="no dq.entities entry"):
            quiet_runner(tree, config, "clean").run()

    def test_report_written_atomically_and_parseable(self, tmp_path):
        tree = make_tree(tmp_path / "tree", clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        target = tmp_path / "out" / "report.json"
        write_report(report, target, log=lambda tag, msg: None)
        assert not target.with_name("report.json.tmp").exists()  # tmp replaced away
        loaded = __import__("json").loads(target.read_text(encoding="utf-8"))
        assert loaded["ok"] is True
        assert loaded["report_version"] == 1
        assert loaded["results"][0]["file"].startswith("customers/")
''',
        "integration tests: runner + config",
    ),
]


UPDATED_FILES: list[tuple[str, str, str]] = [
    (
        "src/core/dq/runner.py",
        r'''"""DQ runner (Milestone 7a, ADR-025).

Wires config -> checks -> report over one output tree.

Pipeline position (consumer side):

    data/generated/  or  data/quality/
            │
            ▼
    DqRunner (--config configs/dq_base.yaml)
            │   glob entity files, load parent key sets,
            │   stream rows through the fixed 7a check suite
            ▼
    DqReport -> dq_report.json + [DQ] log lines + exit code

Modes (the acceptance drill from docs/impurity_guide.md §11, automated):
  clean   -- ok iff ZERO violations anywhere (validate the baseline)
  corrupt -- ok iff AT LEAST ONE check fired (prove the dirt is detectable)

7a deliberately does NOT touch the manifest, Parquet, row_count checks,
full-row duplicate detection, or cross-window PK union -- those are 7b/7c.
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from src.core.dq.checks import (
    Check,
    CheckResult,
    DqConfigError,
    FkIntegrityCheck,
    NotNullCheck,
    PkUniqueCheck,
    ValueInRangeCheck,
)

REPORT_VERSION = 1
CHECK_NAMES = ("not_null", "fk_integrity", "value_in_range", "pk_unique")


@dataclass(frozen=True)
class EntityDqSpec:
    """What we know about one entity, from config only."""

    entity: str
    primary_key: str
    required: tuple[str, ...]
    foreign_keys: Mapping[str, str]  # column -> parent entity
    ranges: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class DqConfig:
    """Parsed configs/dq_base.yaml."""

    entities: Mapping[str, EntityDqSpec]
    enabled: Mapping[str, bool]  # check name -> on/off (absent = enabled)

    @staticmethod
    def load(raw: Mapping[str, object]) -> "DqConfig":
        dq = raw.get("dq")
        if not isinstance(dq, Mapping):
            raise DqConfigError("config needs a top-level 'dq:' block")
        raw_entities = dq.get("entities")
        if not isinstance(raw_entities, Mapping) or not raw_entities:
            raise DqConfigError("dq.entities must list at least one entity")

        entities: dict[str, EntityDqSpec] = {}
        for entity, spec_raw in raw_entities.items():
            if not isinstance(spec_raw, Mapping):
                raise DqConfigError(f"{entity}: entity spec must be a mapping")
            primary_key = str(spec_raw.get("primary_key", "")).strip()
            if not primary_key:
                raise DqConfigError(f"{entity}: primary_key is required")
            required = tuple(str(c) for c in spec_raw.get("required", ()))
            fks_raw = spec_raw.get("foreign_keys", {})
            if not isinstance(fks_raw, Mapping):
                raise DqConfigError(f"{entity}: foreign_keys must be a mapping")
            foreign_keys = {str(c): str(p) for c, p in fks_raw.items()}
            ranges_raw = spec_raw.get("ranges", {})
            if not isinstance(ranges_raw, Mapping):
                raise DqConfigError(f"{entity}: ranges must be a mapping")
            ranges = {str(c): dict(r) for c, r in ranges_raw.items()}  # type: ignore[arg-type]
            entities[str(entity)] = EntityDqSpec(
                entity=str(entity),
                primary_key=primary_key,
                required=required,
                foreign_keys=foreign_keys,
                ranges=ranges,
            )

        raw_checks = dq.get("checks", {})
        if not isinstance(raw_checks, Mapping):
            raise DqConfigError("dq.checks must be a mapping of check toggles")
        unknown = sorted(set(raw_checks) - set(CHECK_NAMES))
        if unknown:
            raise DqConfigError(f"unknown check name(s) in dq.checks: {unknown}")
        enabled = {
            name: bool((raw_checks.get(name) or {}).get("enabled", True))
            for name in CHECK_NAMES
        }
        if not any(enabled.values()):
            raise DqConfigError("all checks are disabled -- nothing to do")
        return DqConfig(entities=entities, enabled=enabled)

    def build_checks(
        self, spec: EntityDqSpec, parent_keys: Mapping[str, frozenset[str]]
    ) -> list[Check]:
        """Instantiate the enabled checks for one entity (fixed 7a order)."""
        checks: list[Check] = []
        if self.enabled["not_null"] and spec.required:
            checks.append(NotNullCheck(spec.entity, spec.required))
        if self.enabled["fk_integrity"] and spec.foreign_keys:
            checks.append(FkIntegrityCheck(spec.entity, spec.foreign_keys, parent_keys))
        if self.enabled["value_in_range"] and spec.ranges:
            checks.append(ValueInRangeCheck(spec.entity, spec.ranges))
        if self.enabled["pk_unique"]:
            checks.append(PkUniqueCheck(spec.entity, spec.primary_key))
        return checks


@dataclass(frozen=True)
class DqReport:
    """Aggregate outcome for one tree. JSON-ready via to_dict()."""

    tree: str
    mode: str
    ok: bool
    files_checked: int
    rows_checked: int
    violations_total: int
    results: tuple[CheckResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": REPORT_VERSION,
            "tree": self.tree,
            "mode": self.mode,
            "ok": self.ok,
            "files_checked": self.files_checked,
            "rows_checked": self.rows_checked,
            "violations_total": self.violations_total,
            "results": [asdict(r) for r in self.results],
        }


class DqRunner:
    def __init__(
        self,
        tree: Path,
        config: DqConfig,
        mode: str,
        log: Callable[[str, str], None],
    ) -> None:
        if mode not in {"clean", "corrupt"}:
            raise DqConfigError(f"mode must be 'clean' or 'corrupt', got {mode!r}")
        if not tree.is_dir():
            raise DqConfigError(
                f"output tree not found: {tree} "
                f"(generate it first -- a clean run populates data/generated, "
                f"a quality run populates data/quality)"
            )
        self.tree = tree
        self.config = config
        self.mode = mode
        self.log = log

    # -- discovery ---------------------------------------------------------

    def entity_files(self, entity: str) -> list[Path]:
        """All CSV files of one entity in the tree, any layout, sorted.

        Primary: the conventional {tree}/{entity}/ directory (both platform
        layouts nest under the entity dir -- snapshot reference_date=... and
        windowed event_date=...).
        Fallback (partition-first layouts): any CSV anywhere under the tree
        whose stem is exactly '{entity}' or '{entity}_<date-ish>' (writer
        convention '{entity}_{date}.csv'). The leading-digit guard after the
        underscore keeps 'orders' from sweeping in 'order_items_2026-01-01.csv'.
        """
        base = self.tree / entity
        files = sorted(base.rglob("*.csv")) if base.is_dir() else []
        if not files:
            stem_pattern = re.compile(re.escape(entity) + r"_[0-9][0-9\-_.]*")
            files = sorted(
                p for p in self.tree.rglob("*.csv")
                if p.stem == entity or stem_pattern.fullmatch(p.stem)
            )
        if not files:
            raise DqConfigError(self._no_files_diagnostic(entity))
        return files

    def _no_files_diagnostic(self, entity: str) -> str:
        """Fail-fast that shows the operator WHAT the tree actually contains."""
        LIMIT = 8
        top = sorted(
            p.name + ("/" if p.is_dir() else "") for p in self.tree.iterdir()
        )
        if top:
            top_repr = ", ".join(top[:LIMIT]) + (f" ... (+{len(top) - LIMIT} more)" if len(top) > LIMIT else "")
        else:
            top_repr = "(tree exists but is EMPTY)"
        csvs = sorted(
            p.relative_to(self.tree).as_posix() for p in self.tree.rglob("*.csv")
        )
        if csvs:
            csv_repr = ", ".join(csvs[:LIMIT]) + (f" ... (+{len(csvs) - LIMIT} more)" if len(csvs) > LIMIT else "")
        else:
            csv_repr = "(no .csv files anywhere under the tree)"
        return (
            f"no CSV files found for entity '{entity}' under {self.tree}; "
            f"tree top-level: {top_repr}; csv files: {csv_repr}; "
            "-> if the tree is empty or the entity is missing, generate the data "
            "first (a clean run populates data/generated, a quality run populates "
            "data/quality); if your entity is named differently, fix dq.entities "
            "in the DQ config"
        )

    def _file_label(self, path: Path) -> str:
        return path.relative_to(self.tree).as_posix()

    # -- context -----------------------------------------------------------

    def load_parent_keys(self, parents: Iterable[str]) -> dict[str, frozenset[str]]:
        """Load each parent's PK value set once, from the same tree."""
        keys: dict[str, frozenset[str]] = {}
        for parent in parents:
            spec = self.config.entities.get(parent)
            if spec is None:
                raise DqConfigError(
                    f"foreign key points at {parent!r}, which has no dq.entities entry"
                )
            files = self.entity_files(parent)
            self._validate_header(parent, spec, files[0])
            values: set[str] = set()
            for path in files:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        value = row.get(spec.primary_key)
                        if value:
                            values.add(value)
            keys[parent] = frozenset(values)
            self.log("DQ", f"loaded {len(values)} parent keys for {parent}")
        return keys

    # -- header validation --------------------------------------------------

    def _validate_header(self, entity: str, spec: EntityDqSpec, path: Path) -> None:
        """Fail-fast: every configured column must exist in the real CSV header.

        A wrong column name would otherwise surface as confusing all-row
        violations (or, worse, as silently EMPTY parent key sets in
        load_parent_keys). One header read per entity at startup makes any
        config/schema mismatch a guided one-line fix.
        """
        with path.open("r", encoding="utf-8", newline="") as handle:
            first_line = handle.readline()
        header = next(csv.reader([first_line]), [])
        if header and header[0].startswith("\ufeff"):
            header[0] = header[0].lstrip("\ufeff")
        missing: list[str] = []
        if spec.primary_key not in header:
            missing.append(f"primary_key={spec.primary_key!r}")
        for col in spec.required:
            if col not in header:
                missing.append(f"required={col!r}")
        for col in spec.foreign_keys:
            if col not in header:
                missing.append(f"foreign_keys={col!r}")
        for col in spec.ranges:
            if col not in header:
                missing.append(f"ranges={col!r}")
        if missing:
            raise DqConfigError(
                f"{entity}: configured columns not in CSV header of "
                f"{self._file_label(path)}; actual header: {header}; "
                f"missing: {', '.join(missing)}; "
                "-> fix dq.entities in the DQ config to match the real column names"
            )

    # -- execution ---------------------------------------------------------

    def run(self) -> DqReport:
        results: list[CheckResult] = []
        parents_needed: set[str] = {
            parent
            for spec in self.config.entities.values()
            for parent in spec.foreign_keys.values()
        }
        parent_keys = self.load_parent_keys(sorted(parents_needed))

        for entity, spec in self.config.entities.items():
            checks = self.config.build_checks(spec, parent_keys)
            files = self.entity_files(entity)
            self._validate_header(entity, spec, files[0])
            self.log("DQ", f"{entity}: {len(files)} file(s), checks={[c.name for c in checks]}")
            for path in files:
                results.extend(self._check_file(path, entity, checks))

        violations_total = sum(r.violation_count for r in results)
        # Every check over the same file reports the same rows_checked; count
        # each FILE once, not once per check.
        rows_checked = sum({r.file: r.rows_checked for r in results}.values())
        ok = self._evaluate(results)
        report = DqReport(
            tree=self.tree.as_posix(),
            mode=self.mode,
            ok=ok,
            files_checked=len({r.file for r in results}),
            rows_checked=rows_checked,
            violations_total=violations_total,
            results=tuple(results),
        )
        self.log(
            "DQ",
            f"summary: files={report.files_checked} rows={rows_checked} "
            f"violations={violations_total} mode={self.mode} ok={ok}",
        )
        return report

    def _check_file(self, path: Path, entity: str, checks: list[Check]) -> list[CheckResult]:
        spec = self.config.entities[entity]
        label = self._file_label(path)
        outcomes: list[CheckResult] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader: Iterator[Mapping[str, str]] = csv.DictReader(handle)
            # Rows are materialised once and shared across this file's checks;
            # checks are read-only over them, keeping one pass over the file.
            rows = [dict(r) for r in reader]
        for check in checks:
            result = check.run(label, iter(rows))
            outcomes.append(result)
            self._log_result(result, spec.primary_key)
        return outcomes

    def _log_result(self, result: CheckResult, primary_key: str) -> None:
        sample = ""
        if result.samples:
            first = result.samples[0]
            sample = f" e.g. row {first.row_number} {first.column}={first.value!r} ({first.reason})"
        self.log(
            "DQ",
            f"{result.file} [{result.check}] rows={result.rows_checked} "
            f"violations={result.violation_count}{sample}",
        )

    def _evaluate(self, results: list[CheckResult]) -> bool:
        if self.mode == "clean":
            return all(r.passed for r in results)
        # corrupt: we REQUIRE the dirt to be detected by at least one check
        return any(not r.passed for r in results)


def write_report(report: DqReport, path: Path, log: Callable[[str, str], None]) -> None:
    """Atomic JSON report write (same .tmp + os.replace discipline as writers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    log("DQ", f"report written: {path.as_posix()}")
''',
        "DQ runner + config + report (v3: header validation + discovery diagnostics)",
    ),
    (
        "tests/test_dq_runner.py",
        r'''"""Integration tests for the DQ runner and config (Milestone 7a, ADR-025).

Builds tiny synthetic output trees (customers + orders, snapshot layout) in
tmp_path and runs DqRunner end-to-end in both modes:
  clean   tree  -> ok=True
  corrupt tree  -> ok=False in clean mode, ok=True in corrupt mode
Mirrors docs/impurity_guide.md section 11 (the acceptance drill) at unit scale.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.core.dq.checks import DqConfigError
from src.core.dq.runner import DqConfig, DqRunner, write_report

DATE = "2026-10-24"


def make_tree(root: Path, orders_rows: list[dict], customers_rows: list[dict] | None = None):
    """Lay out a snapshot tree: {entity}/reference_date={date}/{entity}_{date}.csv"""
    if customers_rows is None:
        customers_rows = [{"customer_id": str(i), "name": f"c{i}", "registration_date": DATE}
                          for i in range(1, 4)]
    for entity, rows_data in (("customers", customers_rows), ("orders", orders_rows)):
        folder = root / entity / f"reference_date={DATE}"
        folder.mkdir(parents=True, exist_ok=True)
        header = list(rows_data[0].keys())
        lines = [",".join(header)]
        lines += [",".join(str(row[c]) for c in header) for row in rows_data]
        (folder / f"{entity}_{DATE}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def base_config() -> dict:
    return {
        "dq": {
            "entities": {
                "customers": {
                    "primary_key": "customer_id",
                    "required": ["customer_id", "name"],
                },
                "orders": {
                    "primary_key": "order_id",
                    "required": ["order_id", "customer_id", "total_amount"],
                    "foreign_keys": {"customer_id": "customers"},
                    "ranges": {"quantity": {"min": 1, "max": 100, "type": "int"}},
                },
            }
        }
    }


def quiet_runner(tree: Path, config: DqConfig, mode: str) -> DqRunner:
    return DqRunner(tree, config, mode, log=lambda tag, msg: None)


def clean_orders() -> list[dict]:
    return [
        {"order_id": str(i), "customer_id": "1", "order_date": DATE,
         "total_amount": f"{10 * i}.50", "quantity": str(i)}
        for i in range(1, 6)
    ]


# ------------------------------------------------------------------ config


class TestDqConfig:
    def test_loads_valid_config(self):
        config = DqConfig.load(base_config())
        assert set(config.entities) == {"customers", "orders"}
        assert all(config.enabled.values())  # absent toggles default to enabled

    def test_missing_dq_block_is_config_error(self):
        with pytest.raises(DqConfigError, match="'dq:'"):
            DqConfig.load({})

    def test_no_entities_is_config_error(self):
        with pytest.raises(DqConfigError, match="entities"):
            DqConfig.load({"dq": {"entities": {}}})

    def test_missing_primary_key_is_config_error(self):
        raw = {"dq": {"entities": {"orders": {"required": ["a"]}}}}
        with pytest.raises(DqConfigError, match="primary_key"):
            DqConfig.load(raw)

    def test_unknown_check_toggle_is_config_error(self):
        raw = base_config()
        raw["dq"]["checks"] = {"not_a_check": {"enabled": True}}
        with pytest.raises(DqConfigError, match="unknown check"):
            DqConfig.load(raw)

    def test_all_disabled_is_config_error(self):
        raw = base_config()
        raw["dq"]["checks"] = {
            name: {"enabled": False}
            for name in ("not_null", "fk_integrity", "value_in_range", "pk_unique")
        }
        with pytest.raises(DqConfigError, match="all checks are disabled"):
            DqConfig.load(raw)

    def test_yaml_roundtrip(self):
        config = DqConfig.load(yaml.safe_load(yaml.safe_dump(base_config())))
        assert config.entities["orders"].primary_key == "order_id"


# ------------------------------------------------------------------ runner


class TestRunnerCleanTree:
    def test_clean_tree_passes_in_clean_mode(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True
        assert report.violations_total == 0
        assert report.files_checked == 2
        assert report.rows_checked == 8  # 3 customers + 5 orders

    def test_clean_tree_fails_corrupt_expectation(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "corrupt").run()
        assert report.ok is False  # no dirt found, but dirt was required


class TestRunnerCorruptTree:
    def corrupt_orders(self) -> list[dict]:
        rows_data = clean_orders()
        rows_data[1]["customer_id"] = "999"         # invalid_fk: reserved band > parent max
        rows_data[2]["quantity"] = "-3"             # out_of_range
        rows_data.append(dict(rows_data[3]))        # duplicate: exact copy incl. PK
        return rows_data

    def test_corrupt_tree_caught_in_clean_mode(self, tmp_path):
        tree = make_tree(tmp_path, self.corrupt_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is False
        fired = {r.check for r in report.results if not r.passed}
        assert fired == {"fk_integrity", "value_in_range", "pk_unique"}

    def test_corrupt_tree_passes_in_corrupt_mode(self, tmp_path):
        tree = make_tree(tmp_path, self.corrupt_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "corrupt").run()
        assert report.ok is True

    def test_each_rule_caught_individually(self, tmp_path):
        """One corruption at a time -- each impurity rule has its own catcher."""
        config = DqConfig.load(base_config())
        cases = [
            ("invalid_fk", lambda r: r.update(customer_id="999"), "fk_integrity"),
            ("out_of_range", lambda r: r.update(quantity="0"), "value_in_range"),
            ("duplicate", lambda r: None, "pk_unique"),  # handled below
        ]
        for name, mutate, expected_check in cases:
            rows_data = clean_orders()
            if name == "duplicate":
                rows_data.append(dict(rows_data[0]))
            else:
                mutate(rows_data[0])
            tree = make_tree(tmp_path / name, rows_data)
            report = quiet_runner(tree, config, "clean").run()
            fired = {r.check for r in report.results if not r.passed}
            assert fired == {expected_check}, f"{name} should fire {expected_check} only"


class TestDiscovery:
    """File discovery must be layout-tolerant AND self-diagnosing when empty."""

    def test_empty_tree_diagnostic_shows_contents(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tmp_path, config, "clean").run()
        msg = str(excinfo.value)
        assert "no CSV files found for entity 'customers'" in msg
        assert "(tree exists but is EMPTY)" in msg
        assert "generate the data first" in msg

    def test_wrong_entity_name_diagnostic_lists_actual_dirs(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["ghosts"] = {"primary_key": "ghost_id"}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tree, config, "clean").run()
        msg = str(excinfo.value)
        assert "no CSV files found for entity 'ghosts'" in msg
        assert "customers/" in msg and "orders/" in msg  # the actual tree contents

    def test_partition_first_layout_found_by_stem(self, tmp_path):
        """No entity dirs at all: {partition}/{entity}_{date}.csv must be found."""
        folder = tmp_path / f"reference_date={DATE}"
        folder.mkdir()
        customers = [{"customer_id": "1", "name": "c1", "registration_date": DATE}]
        header = ",".join(customers[0])
        (folder / f"customers_{DATE}.csv").write_text(
            header + "\n" + ",".join(customers[0].values()) + "\n", encoding="utf-8"
        )
        header_o = ",".join(clean_orders()[0])
        rows_o = "\n".join(",".join(r.values()) for r in clean_orders())
        (folder / f"orders_{DATE}.csv").write_text(
            header_o + "\n" + rows_o + "\n", encoding="utf-8"
        )
        config = DqConfig.load(base_config())
        report = quiet_runner(tmp_path, config, "clean").run()
        assert report.ok is True
        assert report.files_checked == 2

    def test_prefix_collision_guarded(self, tmp_path):
        """Entity 'orders' must NOT sweep in order_items_*.csv (digit guard)."""
        folder = tmp_path / f"reference_date={DATE}"
        folder.mkdir()
        (folder / f"order_items_{DATE}.csv").write_text(
            "order_item_id,order_id\n1,1\n", encoding="utf-8"
        )
        raw = {"dq": {"entities": {"orders": {"primary_key": "order_id",
                                             "required": ["order_id"]}}}}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="no CSV files found for entity 'orders'"):
            quiet_runner(tmp_path, config, "clean").run()

    def test_entity_dir_takes_precedence_over_stray_stem(self, tmp_path):
        """If {tree}/{entity}/ exists and has CSVs, strays elsewhere are ignored."""
        tree = make_tree(tmp_path, clean_orders())
        archive = tree / "archive"
        archive.mkdir()
        (archive / "customers_2020-01-01.csv").write_text(
            "customer_id,name\n9,old\n", encoding="utf-8"
        )
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True
        assert report.files_checked == 2  # archive strays not swept in


class TestHeaderValidation:
    """Every configured column must exist in the real CSV header (fail-fast).

    A wrong parent pk must raise at load_parent_keys time -- silently loading
    an empty key set would flag every FK row as a violation instead.
    """

    def test_wrong_pk_prints_actual_header(self, tmp_path):
        # raises inside load_parent_keys: orders' FK points at customers,
        # whose (wrong) pk is validated before any key is loaded
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["customers"]["primary_key"] = "cust_id"
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tree, config, "clean").run()
        msg = str(excinfo.value)
        assert "customers" in msg
        assert "actual header" in msg
        assert "primary_key='cust_id'" in msg   # the wrong config column...
        assert "customer_id" in msg             # ...and the real header printed

    def test_wrong_required_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["required"] = ["order_id", "amount"]
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="required='amount'"):
            quiet_runner(tree, config, "clean").run()

    def test_wrong_fk_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["foreign_keys"] = {"cust_id": "customers"}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="foreign_keys='cust_id'"):
            quiet_runner(tree, config, "clean").run()

    def test_wrong_range_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["ranges"] = {"qty": {"min": 1, "max": 5, "type": "int"}}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="ranges='qty'"):
            quiet_runner(tree, config, "clean").run()


class TestRunnerEdgeCases:
    def test_missing_entity_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="no CSV files found for entity 'customers'"):
            quiet_runner(tmp_path, config, "clean").run()

    def test_missing_tree_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="output tree not found"):
            quiet_runner(tmp_path / "nope", config, "clean")

    def test_bad_mode_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="mode"):
            quiet_runner(tmp_path, config, "paranoid")

    def test_fk_parent_without_dq_entry_is_config_error(self, tmp_path):
        raw = base_config()
        raw["dq"]["entities"]["orders"]["foreign_keys"] = {"customer_id": "ghosts"}
        config = DqConfig.load(raw)
        tree = make_tree(tmp_path, clean_orders())
        with pytest.raises(DqConfigError, match="no dq.entities entry"):
            quiet_runner(tree, config, "clean").run()

    def test_report_written_atomically_and_parseable(self, tmp_path):
        tree = make_tree(tmp_path / "tree", clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        target = tmp_path / "out" / "report.json"
        write_report(report, target, log=lambda tag, msg: None)
        assert not target.with_name("report.json.tmp").exists()  # tmp replaced away
        loaded = __import__("json").loads(target.read_text(encoding="utf-8"))
        assert loaded["ok"] is True
        assert loaded["report_version"] == 1
        assert loaded["results"][0]["file"].startswith("customers/")
''',
        "integration tests: runner + config (v3: discovery + header tests)",
    ),
    (
        "configs/dq_base.yaml",
        r'''# =============================================================================
# dq_base.yaml -- DQ check configuration (Milestone 7a, v3 -- ADR-025)
#
# v3: entity names aligned with THIS repository's real schema (singular names:
# order, order_item, employee, review, payment, customer; the generation log
# reported datasets=8 -- add any entity still missing below, same shape).
#
# The harness now VALIDATES every configured column against each entity's
# real CSV header at startup and fail-fasts printing the actual header, so a
# wrong column guess is a guided one-line fix, never a silent misfire.
#
# Drills on this machine (quality root is data/corrupted per your [WRITER] log):
#   clean    .venv\Scripts\python.exe dq_main.py --tree data\generated  --mode clean
#   corrupt  .venv\Scripts\python.exe dq_main.py --tree data\corrupted --mode corrupt
#
# The harness is a consumer of output trees: it reads CSV files and this
# config only. Checks default to enabled=true when absent from dq.checks.
# =============================================================================

dq:
  checks:
    not_null:
      enabled: true          # required columns must be populated
    fk_integrity:
      enabled: true          # FK values must resolve within the same tree
    value_in_range:
      enabled: true          # numeric columns must sit inside declared ranges
    pk_unique:
      enabled: true          # primary keys must not repeat within a file

  entities:
    # --- customer: guessed from order's invalid_fk rule firing; the startup
    # header check confirms or corrects the pk name on first run.
    customer:
      primary_key: customer_id
      required: [customer_id]

    order:
      primary_key: order_id
      required: [order_id]
      foreign_keys:
        customer_id: customer   # TODO verify column + parent against the real header

    order_item:
      primary_key: order_item_id
      required: [order_item_id]
      # out_of_range fired on this entity (65 rows) -- wire the real column and
      # business range once confirmed from the header, e.g.:
      # ranges:
      #   quantity: {min: 1, max: 100, type: int}

    employee:
      primary_key: employee_id
      required: [employee_id]

    review:
      primary_key: review_id
      required: [review_id]

    payment:
      primary_key: payment_id
      required: [payment_id]

    # --- datasets=8 in your log: add the remaining entities here, same shape.
    # Null rule fired on order (57 rows): once you know WHICH column was
    # nulled, add it to that entity's required list so not_null catches it.
''',
        "DQ check configuration (v3: real entity names + drill paths)",
    ),
]

APPEND_FILES: list[tuple[str, str, str, str]] = [
    (
        "docs/decisions.md",
        "ADR-025",
        r'''# ADR text for docs/decisions.md (appended by bootstrap_milestone7a.py)

---

## ADR-025 — DQ harness is an output-tree consumer with a fixed 4-check suite

**Status:** Accepted (Milestone 7a)

**Context.** The platform now produces two output trees: the clean baseline
(`data/generated`) and corrupted copies (`data/quality`, impurity engine,
guides in `docs/impurity_guide.md`). Downstream Data Engineering projects need
a repeatable way to prove two things: the baseline honors the platform's
structural guarantees, and the corrupted tree's dirt is *detectable*. That is
the acceptance drill from the operator guide (§11), currently run by hand.

**Decision.**
1. The DQ harness is a **consumer of output files only**. It reads CSV files
   and a DQ config (`configs/dq_base.yaml`); it never imports generator,
   writer, or impurity internals. It is shaped exactly like a downstream ETL
   test suite, so it doubles as a template for future consumers.
2. The 7a suite is fixed and mirrors the impurity chain:
   `not_null -> fk_integrity -> value_in_range -> pk_unique`
   (one check per corruption condition; duplicates and cross-window PK union
   are deferred to 7b; Parquet support to 7b as well).
3. **Two expectation modes** at the entry point `dq_main.py`:
   `--mode clean` (ok iff zero violations) and `--mode corrupt` (ok iff at
   least one check fires). Exit contract unchanged: 0 ok, 1 failure, 130 Ctrl+C.
4. **New log tag `[DQ]`** joins the ADR-0009 tag set. Same `run()`-wrapper
   structure, per-run console logging, atomic JSON report
   (`data/dq/report.json` by default, `.tmp` + `os.replace`).
5. Config keys default to *enabled* when a check toggle is absent; this is a
   documented default (like `quality.output.root`), not hidden behaviour.
   Unknown check names, unknown range keys, `min > max`, all-checks-disabled,
   missing `primary_key`, missing tree/entity directories, and FK parents
   without a `dq.entities` entry are all startup failures (exit 1).
6. CSV null == empty string. Value-based checks skip empty cells; emptiness
   is exclusively `not_null`'s concern — mirroring the impurity engine's Law 2
   so the two sides of the platform reason about nulls identically.

**Consequences.**
- No coupling: DQ can evolve (and be copied into downstream projects) without
  touching the generator core. Tests are pure-stdlib fixtures.
- The check suite is a *contract*: adding a check type or changing semantics
  changes reports and exit behaviour — future additions follow this ADR as a
  new numbered ADR (instance vs new pattern test applies).
- `[DQ]` lines are additive to the ADR-0009 log contract; existing tags and
  exit codes are unchanged, so no §26 protocol is triggered.
- Corrupted trees are *expected* to fail `--mode clean`; failing there is the
  success signal. Tooling that consumes this harness must pass the mode
  explicitly rather than assume one.''',
        "ADR-025 (append-only, idempotent by marker)",
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
        log("CHECK", "note     configs/ absent -- no problem, it will be created with configs/dq_base.yaml")
    if not (root / ".git").exists():
        log("CHECK", "warn     .git not found -- not fatal, but you lose rollback safety")
    hits: list[str] = []
    if (root / "src").exists():
        for pattern in ("*quality*", "*impurity*"):
            hits.extend(str(p.relative_to(root)) for p in (root / "src").rglob(pattern) if p.is_file())
        hits = sorted(set(hits))
    if hits:
        log("CHECK", f"ok       impurity engine module(s): {', '.join(hits[:2])}")
    else:
        log("CHECK", "note     impurity engine module not found by name under src/ -- not fatal; "
                     "7a is standalone, and the corrupt-tree drill works with your engine whatever it is named locally")
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


def deliver_update(root: Path, rel: str, content: str, label: str, dry: bool) -> str:
    """Logged, atomic overwrite for harness files this bootstrap owns.

    Used ONLY for files 7a itself shipped, when a fix must reach machines that
    already ran the first delivery. Missing or empty target -> created (same
    path as deliver_new). Byte-identical content -> no-op. Otherwise the old
    sha256 is logged before the new bytes land; git is the review/rollback
    path (preconditions warn when .git is absent).
    """
    target = root / rel
    payload = content.encode("utf-8")
    if not target.exists() or target.stat().st_size == 0:
        return deliver_new(root, rel, content, label, dry)
    if target.read_bytes() == payload:
        log("SKIP", f"{rel} already up-to-date (sha256={sha256_of(payload)[:16]}...)")
        return "up-to-date"
    old_digest = sha256_of(target.read_bytes())
    digest = sha256_of(payload)
    log("CONFIG", f"{label}: {target}")
    if dry:
        log("DRY-RUN", f"would update {rel} ({target.stat().st_size} -> {len(payload)} bytes)")
        return "updated (dry-run)"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    log("UPDATE", f"updated {rel}: sha256 {old_digest[:16]}... -> {digest[:16]}... ({len(payload)} bytes)")
    return "updated"


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
    parser = argparse.ArgumentParser(description="Bootstrap Milestone 7a (DQ test harness).")
    parser.add_argument("--dry-run", action="store_true", help="show actions without writing")
    args = parser.parse_args()

    log("STARTUP", "bootstrap_milestone7a.py -- DQ test harness (ADR-025)")
    root = Path.cwd()
    if not check_preconditions(root):
        log("SUMMARY", "preconditions failed -- run this from the project root")
        return 1

    results: list[tuple[str, str]] = []
    try:
        for rel, content, label in NEW_FILES:
            results.append((rel, deliver_new(root, rel, content, label, args.dry_run)))
        for rel, content, label in UPDATED_FILES:
            results.append((rel, deliver_update(root, rel, content, label, args.dry_run)))
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
        log("SUMMARY", "  1. .venv\\Scripts\\python.exe -m pytest tests/test_dq_checks.py tests/test_dq_runner.py -q")
        log("SUMMARY", "  2. .venv\\Scripts\\python.exe dq_main.py --tree data\\generated --mode clean")
        log("SUMMARY", "  3. .venv\\Scripts\\python.exe dq_main.py --tree data\\quality --mode corrupt")
        log("SUMMARY", "     (v2 note: a 'no CSV files found for entity ...' error now prints what the")
        log("SUMMARY", "      tree actually contains -- generate the data first or fix dq.entities)")
        log("SUMMARY", "     (if you keep configs elsewhere, move configs/dq_base.yaml and pass --config <path>)")
        log("SUMMARY", "  4. git add -A && git commit -m \"feat(dq): milestone 7a -- DQ harness as output-tree consumer (ADR-025)\"")
    log("SUMMARY", f"done ({'dry-run' if args.dry_run else 'applied'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
