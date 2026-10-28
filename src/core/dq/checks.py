"""DQ check framework (Milestone 7a, ADR-025).

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
