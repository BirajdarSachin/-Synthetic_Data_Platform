"""Unit tests for DQ checks (Milestone 7a, ADR-025).

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
