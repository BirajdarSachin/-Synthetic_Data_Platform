"""Unit tests for DQ checks (Milestones 7a + 7b, ADR-025/026).

Each check is tested in isolation over synthetic rows -- no output trees, no
generator internals. The sys.path shim keeps these tests self-contained
regardless of pytest/ini configuration.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.dq.checks import (
    DuplicateRowsCheck,
    DqConfigError,
    FkIntegrityCheck,
    NotNullCheck,
    PkUnionCheck,
    PkUniqueCheck,
    RangeSpec,
    RowCountCheck,
    RowCountSpec,
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


# ============================================================ 7b: duplicate_rows


class TestDuplicateRowsCheck:
    def test_passes_on_distinct_rows(self):
        check = DuplicateRowsCheck("orders")
        result = check.run("f.csv", rows({"order_id": "1", "qty": "2"},
                                         {"order_id": "2", "qty": "3"}))
        assert result.passed and result.rows_checked == 2

    def test_fires_on_exact_copy(self):
        check = DuplicateRowsCheck("orders")
        result = check.run("f.csv", rows({"order_id": "1", "qty": "2"},
                                         {"order_id": "1", "qty": "2"}))
        assert not result.passed
        assert result.violation_count == 1
        sample = result.samples[0]
        assert sample.row_number == 2
        assert sample.column == "(full row)"
        assert sample.reason == "exact duplicate of row 1"

    def test_differing_single_field_is_not_a_duplicate(self):
        check = DuplicateRowsCheck("orders")
        result = check.run("f.csv", rows({"order_id": "1", "qty": "2"},
                                         {"order_id": "1", "qty": "3"}))
        assert result.passed  # pk_unique's business, not ours

    def test_counts_each_extra_copy_samples_bounded(self):
        check = DuplicateRowsCheck("orders")
        data = rows(*[{"order_id": "1", "qty": "2"} for _ in range(9)])
        result = check.run("f.csv", data)
        assert result.violation_count == 8  # every copy beyond the first
        assert len(result.samples) == 5

    def test_none_values_normalised_like_empty_cells(self):
        # DictReader yields None for short rows; the copy with explicit ""
        # is the SAME record after normalisation (CSV null == empty string).
        check = DuplicateRowsCheck("orders")
        result = check.run("f.csv", rows({"order_id": "1", "note": None},
                                         {"order_id": "1", "note": ""}))
        assert not result.passed

    def test_key_order_does_not_affect_identity(self):
        check = DuplicateRowsCheck("orders")
        result = check.run("f.csv", rows({"order_id": "1", "qty": "2"},
                                         {"qty": "2", "order_id": "1"}))
        assert not result.passed


# ================================================================ 7b: row_count


class TestRowCountSpec:
    def test_parses_expected(self):
        spec = RowCountSpec.parse("orders", {"expected": 5})
        assert spec.expected == 5 and spec.minimum is None and spec.maximum is None

    def test_parses_min_max(self):
        spec = RowCountSpec.parse("orders", {"min": 1, "max": 9})
        assert spec.expected is None and spec.minimum == 1 and spec.maximum == 9

    def test_expected_mixed_with_bounds_is_config_error(self):
        with pytest.raises(DqConfigError, match="use 'expected' alone"):
            RowCountSpec.parse("orders", {"expected": 5, "min": 1})

    def test_unknown_key_is_config_error(self):
        with pytest.raises(DqConfigError, match="unknown keys"):
            RowCountSpec.parse("orders", {"expected": 5, "typo": 1})

    def test_empty_mapping_is_config_error(self):
        with pytest.raises(DqConfigError, match="needs 'expected'"):
            RowCountSpec.parse("orders", {})

    def test_non_mapping_is_config_error(self):
        with pytest.raises(DqConfigError, match="row_count must be a mapping"):
            RowCountSpec.parse("orders", 5)

    def test_non_integer_is_config_error(self):
        with pytest.raises(DqConfigError, match="must be an integer"):
            RowCountSpec.parse("orders", {"expected": "5"})

    def test_boolean_is_rejected_as_integer(self):
        with pytest.raises(DqConfigError, match="must be an integer"):
            RowCountSpec.parse("orders", {"expected": True})

    def test_negative_is_config_error(self):
        with pytest.raises(DqConfigError, match=">= 0"):
            RowCountSpec.parse("orders", {"min": -1})

    def test_min_greater_than_max_is_config_error(self):
        with pytest.raises(DqConfigError, match="min > max"):
            RowCountSpec.parse("orders", {"min": 9, "max": 1})


class TestRowCountCheck:
    def test_passes_on_exact_expected(self):
        check = RowCountCheck("orders", RowCountSpec.parse("orders", {"expected": 2}))
        result = check.run("f.csv", rows({"order_id": "1"}, {"order_id": "2"}))
        assert result.passed and result.rows_checked == 2

    def test_fires_one_file_level_violation_on_mismatch(self):
        check = RowCountCheck("orders", RowCountSpec.parse("orders", {"expected": 5}))
        data = rows(*[{"order_id": str(i)} for i in range(6)])
        result = check.run("f.csv", data)
        assert not result.passed
        assert result.violation_count == 1  # file-level, not per row
        sample = result.samples[0]
        assert sample.row_number == 0
        assert sample.column == "(row count)"
        assert sample.value == "6"
        assert "exactly 5" in sample.reason

    def test_min_bound_fires_below(self):
        check = RowCountCheck("orders", RowCountSpec.parse("orders", {"min": 3}))
        result = check.run("f.csv", rows({"order_id": "1"}, {"order_id": "2"}))
        assert "at least 3" in result.samples[0].reason

    def test_max_bound_fires_above(self):
        check = RowCountCheck("orders", RowCountSpec.parse("orders", {"max": 1}))
        result = check.run("f.csv", rows({"order_id": "1"}, {"order_id": "2"}))
        assert "at most 1" in result.samples[0].reason

    def test_bounds_inclusive_pass(self):
        check = RowCountCheck("orders", RowCountSpec.parse("orders", {"min": 1, "max": 2}))
        assert check.run("f.csv", rows({"order_id": "1"}, {"order_id": "2"})).passed

    def test_empty_file_passes_zero_expected(self):
        check = RowCountCheck("orders", RowCountSpec.parse("orders", {"expected": 0}))
        assert check.run("f.csv", rows()).passed


# ================================================================ 7b: pk_union


class TestPkUnionCheck:
    def test_disjoint_files_pass(self):
        check = PkUnionCheck("customers", "customer_id")
        result = check.run_over_files({"a.csv": ["1", "2"], "b.csv": ["3"]})
        assert result.passed
        assert result.rows_checked == 3  # distinct keys in the union
        assert result.file == "(cross-file union)"

    def test_shared_key_fires_listing_files(self):
        check = PkUnionCheck("customers", "customer_id")
        result = check.run_over_files({"a.csv": ["1", "2"], "b.csv": ["2", "3"]})
        assert not result.passed
        assert result.violation_count == 1
        sample = result.samples[0]
        assert sample.value == "2"
        assert sample.row_number == 0
        assert "a.csv, b.csv" in sample.reason

    def test_same_file_repeat_ignored_pk_unique_business(self):
        check = PkUnionCheck("customers", "customer_id")
        result = check.run_over_files({"a.csv": ["1", "1"], "b.csv": ["2"]})
        assert result.passed

    def test_three_windows_dedupe_file_labels(self):
        check = PkUnionCheck("customers", "customer_id")
        result = check.run_over_files({"a.csv": ["1"], "b.csv": ["1"], "c.csv": ["1"]})
        assert result.violation_count == 1
        assert "3 files: a.csv, b.csv, c.csv" in result.samples[0].reason

    def test_empty_keys_skipped(self):
        check = PkUnionCheck("customers", "customer_id")
        result = check.run_over_files({"a.csv": [""], "b.csv": [""]})
        assert result.passed and result.rows_checked == 0

    def test_samples_bounded_and_deterministic(self):
        check = PkUnionCheck("customers", "customer_id")
        data = {f"{i}.csv": [str(k) for k in range(8)] for i in range(3)}
        result = check.run_over_files(data)
        assert result.violation_count == 8
        assert len(result.samples) == 5
        assert [s.value for s in result.samples] == ["0", "1", "2", "3", "4"]

    def test_missing_primary_key_is_config_error(self):
        with pytest.raises(DqConfigError, match="primary_key"):
            PkUnionCheck("customers", "")
