"""DQ check framework (Milestones 7a + 7b, ADR-025/026).

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
7b adds three structural checks (ADR-026):
  duplicate_rows -- exact full-row copies within one file (default on;
                    complements pk_unique: key-uniqueness vs record-copy)
  row_count      -- per-file size expectation (expected N, or min/max bounds)
  pk_union       -- entity-level, OPT-IN: a primary key must not appear in
                    more than one file/window (runner drives it, because its
                    state spans files; empty keys are pk_unique's business)
Parquet input is deferred to 7c (needs a reader-dependency decision).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

Row = Mapping[str, str]

DEFAULT_MAX_SAMPLES = 5


@dataclass(frozen=True)
class Violation:
    """One concrete bad row, reported with enough context to find it again.

    row_number semantics: 1-based data row within the file (header excluded);
    0 marks a FILE-LEVEL finding (row_count bounds, pk_union cross-window) --
    there is no single row to point at.
    """

    row_number: int  # 1-based data row number within the file; 0 = file-level
    column: str
    value: str
    reason: str


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check over one file (or one entity, for pk_union)."""

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


def _row_key(row: Row) -> tuple:
    """Order-stable hashable identity of a full row (None -> empty string)."""
    return tuple(
        (str(column), "" if value is None else value)
        for column, value in sorted(row.items(), key=lambda kv: str(kv[0]))
    )


def _row_preview(key: tuple, limit: int = 100) -> str:
    """Compact printable form of a full row for violation samples."""
    text = "|".join(f"{column}={value}" for column, value in key)
    return text if len(text) <= limit else text[:limit] + "..."


class Check:
    """Base class for all DQ checks.

    Two extension shapes are supported:
      * row-wise: implement check_row(); the shared run() streams rows for you;
      * stream-wise: override run() when state spans rows (e.g. pk_unique's
        seen-set, duplicate_rows' first-seen map, row_count's total).
        A check implementing neither raises NotImplementedError on first
        use -- loud, by design.
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

    This is one of the two checks that catch the impurity engine's duplicate
    rule: duplicates are exact copies of the final corrupted record, so they
    repeat the PK (and the whole row) inside one file. On a clean tree the PK
    union across files is exactly 1..N, so this check passes there by
    construction. Cross-FILE key repeats are pk_union's business (opt-in),
    not ours.
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


# =====================================================================
# 7b checks (ADR-026)
# =====================================================================


class DuplicateRowsCheck(Check):
    """Fails rows that are exact copies of an earlier row in the same file.

    The impurity engine's duplicate rule appends exact copies of the final
    corrupted record (Law 4), so on a corrupted tree BOTH pk_unique and this
    check fire -- by design: pk_unique answers "is the key unique?",
    duplicate_rows answers "is the whole record a copy?". The two can
    disagree (a key can be unique while the rest of the record is not), which
    is exactly why a consumer wants both signals.

    Comparison covers ALL columns (order-stable, None normalised to empty
    string). Each extra copy beyond the first is one violation, matching
    pk_unique's counting semantics.
    """

    name = "duplicate_rows"

    def run(self, file_label: str, rows: Iterator[Row]) -> CheckResult:
        first_seen: dict[tuple, int] = {}
        violation_count = 0
        samples: list[Violation] = []
        rows_checked = 0
        for row_number, row in enumerate(rows, start=1):
            rows_checked += 1
            key = _row_key(row)
            first = first_seen.get(key)
            if first is None:
                first_seen[key] = row_number
                continue
            violation_count += 1
            if len(samples) < self.max_samples:
                samples.append(
                    Violation(
                        row_number=row_number,
                        column="(full row)",
                        value=_row_preview(key),
                        reason=f"exact duplicate of row {first}",
                    )
                )
        return CheckResult(
            check=self.name,
            entity=self.entity,
            file=file_label,
            rows_checked=rows_checked,
            violation_count=violation_count,
            samples=tuple(samples),
        )


@dataclass(frozen=True)
class RowCountSpec:
    """Declared size of one entity file: exact 'expected' OR inclusive bounds.

    'expected' is the strict form (snapshot windows are deterministic); 'min'
    and 'max' are the tolerant forms. Mixing 'expected' with bounds is a
    config error -- one expectation, stated once.
    """

    expected: int | None
    minimum: int | None
    maximum: int | None

    @staticmethod
    def parse(entity: str, raw: object) -> "RowCountSpec":
        if not isinstance(raw, Mapping):
            raise DqConfigError(
                f"{entity}: row_count must be a mapping with 'expected' or 'min'/'max'"
            )
        unknown = sorted(set(raw) - {"expected", "min", "max"})
        if unknown:
            raise DqConfigError(f"{entity}: row_count: unknown keys {unknown}")
        expected = raw.get("expected")
        minimum = raw.get("min")
        maximum = raw.get("max")
        if expected is not None and (minimum is not None or maximum is not None):
            raise DqConfigError(
                f"{entity}: row_count: use 'expected' alone or 'min'/'max', not both"
            )
        if expected is None and minimum is None and maximum is None:
            raise DqConfigError(
                f"{entity}: row_count: needs 'expected' or at least one of 'min'/'max'"
            )

        def as_int(name: str, value: object) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise DqConfigError(
                    f"{entity}: row_count.{name} must be an integer, got {value!r}"
                )
            if value < 0:
                raise DqConfigError(f"{entity}: row_count.{name} must be >= 0")
            return value

        expected_v = as_int("expected", expected) if expected is not None else None
        min_v = as_int("min", minimum) if minimum is not None else None
        max_v = as_int("max", maximum) if maximum is not None else None
        if min_v is not None and max_v is not None and min_v > max_v:
            raise DqConfigError(f"{entity}: row_count: min > max")
        return RowCountSpec(expected=expected_v, minimum=min_v, maximum=max_v)

    def violation_reason(self, actual: int) -> str | None:
        """Why 'actual' rows violate this spec, or None if it complies."""
        if self.expected is not None:
            if actual != self.expected:
                return f"expected exactly {self.expected} rows, found {actual}"
            return None
        if self.minimum is not None and actual < self.minimum:
            return f"expected at least {self.minimum} rows, found {actual}"
        if self.maximum is not None and actual > self.maximum:
            return f"expected at most {self.maximum} rows, found {actual}"
        return None


class RowCountCheck(Check):
    """File-level check: the file must hold the configured number of rows.

    Row counts are the fingerprint of the duplicate rule: only duplicates
    change row counts (Laws 1-3), so a clean tree with a wired 'expected'
    proves the writer's row discipline, and a corrupted tree trips this check
    the moment duplicates land.

    A violation is FILE-LEVEL: one violation with row_number=0 (no single row
    is to blame), column='(row count)', value=<actual count>.
    """

    name = "row_count"

    def __init__(self, entity: str, spec: RowCountSpec) -> None:
        super().__init__(entity)
        self.spec = spec

    def run(self, file_label: str, rows: Iterator[Row]) -> CheckResult:
        rows_checked = sum(1 for _ in rows)
        reason = self.spec.violation_reason(rows_checked)
        samples: tuple[Violation, ...] = ()
        violation_count = 0
        if reason is not None:
            violation_count = 1
            samples = (
                Violation(
                    row_number=0,
                    column="(row count)",
                    value=str(rows_checked),
                    reason=reason,
                ),
            )
        return CheckResult(
            check=self.name,
            entity=self.entity,
            file=file_label,
            rows_checked=rows_checked,
            violation_count=violation_count,
            samples=samples,
        )


class PkUnionCheck:
    """Cross-file PK uniqueness for one entity (7b, per-entity opt-in).

    Snapshot windows legitimately repeat keys ACROSS reference_date windows
    (SCD-style history), so this check never runs unless the entity opts in
    with 'pk_union: true'. It complements pk_unique:
      pk_unique = "the key repeats WITHIN one file"   (always on)
      pk_union  = "the key appears in MORE THAN ONE file" (opt-in)
    Empty keys are skipped: emptiness is pk_unique's business (Law 2).

    The runner drives this check because its state spans files; the result is
    entity-level: file='(cross-file union)', one violation per key that shows
    up in 2+ files (samples bounded), rows_checked = size of the key UNION
    (not a row count -- per-file row totals are the runner's accounting).
    """

    name = "pk_union"
    FILE_LABEL = "(cross-file union)"

    def __init__(self, entity: str, primary_key: str, max_samples: int = DEFAULT_MAX_SAMPLES) -> None:
        if not primary_key:
            raise DqConfigError(f"{entity}: pk_union check needs a primary_key")
        self.entity = entity
        self.primary_key = primary_key
        self.max_samples = max_samples

    def run_over_files(self, file_pks: Mapping[str, Iterable[str]]) -> CheckResult:
        first_file: dict[str, str] = {}
        multi: dict[str, list[str]] = {}
        for file_label, pk_values in file_pks.items():
            for value in pk_values:
                if value is None or value == "":
                    continue  # emptiness is pk_unique's business, not ours
                first = first_file.get(value)
                if first is None:
                    first_file[value] = file_label
                elif first != file_label:
                    seen = multi.setdefault(value, [first])
                    if file_label not in seen:
                        seen.append(file_label)
        samples = tuple(
            Violation(
                row_number=0,
                column=self.primary_key,
                value=key,
                reason=f"primary key appears in {len(files)} files: {', '.join(files)}",
            )
            for key, files in sorted(multi.items())[: self.max_samples]
        )
        return CheckResult(
            check=self.name,
            entity=self.entity,
            file=self.FILE_LABEL,
            rows_checked=len(first_file),  # distinct keys in the union
            violation_count=len(multi),
            samples=samples,
        )
