"""Milestone 5c tests: out-of-range rule, schema drift, completed chain."""

from collections import Counter
from dataclasses import FrozenInstanceError, asdict
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import main as entry
from src.core.config import (
    ConfigError,
    QualityConfig,
    QualityDatasetConfig,
    load_config,
)
from src.core.seed_manager import SeedManager
from src.generators.customer import CustomerGenerator
from src.generators.product import ProductGenerator
from src.quality.engine import ImpurityEngine
from src.quality.out_of_range_rule import OutOfRangeRule
from src.quality.schema_drift_rule import EXTRA_MARKER, SchemaDriftRule
from src.writers import get_writer

REF = date(2026, 1, 15)


def _products(count: int = 50, seed: int = 7) -> list:
    return list(ProductGenerator(seed=seed, supplier_count=5).generate(count))


def _customers(count: int = 50, seed: int = 7) -> list:
    return list(CustomerGenerator(
        seed=seed, reference_date=REF, history_days=365).generate(count))


# --- out-of-range rule --------------------------------------------------------

def test_out_of_range_rate_one_negates_every_target():
    products = _products()
    rule = OutOfRangeRule("product", seed=3,
                          target_fields=("unit_price",), rate=1.0)
    out = list(rule.apply(iter(products)))
    assert all(p.unit_price == -o.unit_price for o, p in zip(products, out))


def test_out_of_range_only_touches_configured_field():
    products = _products()
    rule = OutOfRangeRule("product", seed=3,
                          target_fields=("unit_price",), rate=1.0)
    out = list(rule.apply(iter(products)))
    assert all(p.supplier_id == o.supplier_id for o, p in zip(products, out))
    assert all(p.product_name == o.product_name for o, p in zip(products, out))


def test_out_of_range_is_deterministic():
    products = _products()
    first = list(OutOfRangeRule("product", seed=3,
                                target_fields=("unit_price",),
                                rate=0.5).apply(iter(products)))
    second = list(OutOfRangeRule("product", seed=3,
                                 target_fields=("unit_price",),
                                 rate=0.5).apply(iter(products)))
    assert first == second


def test_out_of_range_non_numeric_field_fails_loudly():
    products = _products(count=2)
    rule = OutOfRangeRule("product", seed=3,
                          target_fields=("category",), rate=1.0)
    with pytest.raises(ValueError, match="numeric"):
        list(rule.apply(iter(products)))


def test_out_of_range_unknown_field_fails_loudly():
    products = _products(count=2)
    rule = OutOfRangeRule("product", seed=3,
                          target_fields=("nope",), rate=1.0)
    with pytest.raises(ValueError, match="nope"):
        list(rule.apply(iter(products)))


# --- config validation --------------------------------------------------------

def test_out_of_range_rejects_identifier_fields(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.0\n  out_of_range_rate: 0.1\n"
        "  datasets:\n    customer:\n"
        "      out_of_range_fields: [customer_id]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="_id"):
        load_config(config_file)


def test_null_and_out_of_range_overlap_rejected(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.1\n  out_of_range_rate: 0.1\n"
        "  datasets:\n    customer:\n"
        "      null_fields: [phone]\n"
        "      out_of_range_fields: [phone]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="overlap|both"):
        load_config(config_file)


def test_drop_rejects_identifier_fields(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.0\n  drift_rate: 0.1\n"
        "  datasets:\n    customer:\n"
        "      drop_fields: [customer_id]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="_id"):
        load_config(config_file)


def test_backward_compat_5b_config_still_parses(tmp_path: Path):
    """A 5b-shaped quality section (no 5c keys) still parses; new rates
    default to 0.0 (optional-section default discipline)."""
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.1\n  duplicate_rate: 0.02\n"
        "  datasets:\n    customer:\n"
        "      null_fields: [phone]\n"
        "      invalid_fk_fields:\n        customer_id: customer\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.quality.out_of_range_rate == 0.0
    assert config.quality.drift_rate == 0.0


# --- schema drift rule ---------------------------------------------------------

def test_drop_mode_removes_field_and_keeps_the_rest():
    products = _products()
    rule = SchemaDriftRule("product", seed=3, drop_fields=("category",),
                           extra_fields=(), rate=1.0)
    out = list(rule.apply(iter(products)))
    assert len(out) == len(products)
    first = asdict(out[0])
    assert "category" not in first
    assert "product_id" in first and "unit_price" in first


def test_extra_mode_adds_marker_field_and_keeps_the_rest():
    customers = _customers()
    rule = SchemaDriftRule("customer", seed=3, drop_fields=(),
                           extra_fields=("legacy_code",), rate=1.0)
    out = list(rule.apply(iter(customers)))
    assert all(getattr(r, "legacy_code") == EXTRA_MARKER for r in out)
    assert all(r.customer_id == o.customer_id
               for o, r in zip(customers, out))


def test_drift_rate_zero_is_passthrough():
    products = _products()
    rule = SchemaDriftRule("product", seed=3, drop_fields=("category",),
                           extra_fields=(), rate=0.0)
    out = list(rule.apply(iter(products)))
    assert out == products


def test_drift_is_deterministic():
    products = _products()
    first = list(SchemaDriftRule("product", seed=3,
                                 drop_fields=("category",),
                                 extra_fields=(), rate=0.5).apply(iter(products)))
    second = list(SchemaDriftRule("product", seed=3,
                                  drop_fields=("category",),
                                  extra_fields=(), rate=0.5).apply(iter(products)))
    assert first == second


def test_drifted_records_are_frozen():
    products = _products(count=2)
    rule = SchemaDriftRule("product", seed=3, drop_fields=("category",),
                           extra_fields=(), rate=1.0)
    out = list(rule.apply(iter(products)))
    with pytest.raises(FrozenInstanceError):
        out[0].product_id = 999


def test_parquet_schema_exposes_drift(tmp_path: Path):
    """Drift is detectable by READING the parquet schema: dropped column
    absent, extra column present (ADR-0023)."""
    customers = _customers(5)
    rule = SchemaDriftRule("customer", seed=3, drop_fields=("city",),
                           extra_fields=("legacy_code",), rate=1.0)
    path = tmp_path / "customer.parquet"
    get_writer("parquet").write(path, rule.apply(iter(customers)))
    schema = pq.read_schema(path)
    assert "city" not in schema.names
    assert "legacy_code" in schema.names


# --- the completed chain -------------------------------------------------------

def test_chain_all_rules_with_duplicate_last():
    """null -> out_of_range -> drift -> duplicate at rate 1.0: every emitted
    record is fully corrupted AND duplicated as an exact copy."""
    products = _products()
    engine = ImpurityEngine(
        SeedManager(12345),
        QualityConfig(
            null_rate=1.0, duplicate_rate=1.0, invalid_fk_rate=0.0,
            out_of_range_rate=1.0, drift_rate=1.0,
            datasets={"product": QualityDatasetConfig(
                null_fields=("category",),
                out_of_range_fields=("unit_price",),
                drop_fields=("product_name",))},
        ),
        dataset_counts={"product": len(products), "supplier": 5},
    )
    out = list(engine.apply("product", iter(products)))
    assert len(out) == 2 * len(products)
    counts = Counter(out)
    assert all(count == 2 for count in counts.values())
    for record in out:
        assert record.category is None          # null rule fired
        assert record.unit_price < 0            # out-of-range rule fired
        assert not hasattr(record, "product_name")  # drift rule fired


def test_engine_report_from_main_guard_helpers_exist():
    """main.py still exposes the guard hooks the earlier tests rely on."""
    assert hasattr(entry, "_validate_quality_output")
