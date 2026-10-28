"""DQ runner (Milestone 7a, ADR-025).

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
