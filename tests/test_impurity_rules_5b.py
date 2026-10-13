"""Milestone 5b tests: duplicate rule, invalid FK rule, rule chaining."""

from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

import main as entry
from src.core.config import (
    ConfigError,
    DatasetConfig,
    GenerationConfig,
    OutputConfig,
    PipelineConfig,
    QualityConfig,
    QualityDatasetConfig,
    RunConfig,
    load_config,
)
from src.core.seed_manager import SeedManager
from src.generators.customer import CustomerGenerator
from src.generators.order import OrderGenerator
from src.quality.duplicate_rule import DuplicateRule
from src.quality.engine import ImpurityEngine
from src.quality.invalid_fk_rule import InvalidForeignKeyRule

REF = date(2026, 1, 15)
N_CUSTOMERS = 30


def _customers(count: int = 100, seed: int = 42) -> list:
    return list(CustomerGenerator(
        seed=seed, reference_date=REF, history_days=365).generate(count))


def _orders(count: int = 50, seed: int = 7) -> list:
    customers = _customers(N_CUSTOMERS, seed=1)
    return list(OrderGenerator(
        seed=seed, reference_date=REF,
        customers=[(c.customer_id, c.registration_date) for c in customers],
        employee_count=5).generate(count))

@dataclass(frozen=True)
class _R:
    """Minimal immutable record: frozen dataclass gives __eq__ AND __hash__."""
    value: int


def _records(values: list[int]) -> list[_R]:
    """Tiny stand-in records for pure stream tests."""
    return [_R(v) for v in values]

# --- duplicate rule ----------------------------------------------------------

def test_duplicate_rate_one_emits_every_record_twice():
    records = _records(list(range(1, 21)))
    out = list(DuplicateRule("t", seed=5, rate=1.0).apply(iter(records)))
    assert len(out) == 2 * len(records)
    counts = Counter(out)
    assert all(count == 2 for count in counts.values())


def test_duplicates_are_exact_copies_of_the_original():
    records = _records(list(range(1, 26)))
    out = list(DuplicateRule("t", seed=5, rate=1.0).apply(iter(records)))
    assert Counter(out) == Counter(records + records)  # multiset equality


def test_duplicate_delay_semantics():
    """A duplicate appears after its delay elapsed: 0-based index of the first
    duplicate of record #1 equals its drawn delay, which is within
    [1, max_delay] - regardless of which value the seed draws."""
    records = _records(list(range(1, 11)))
    out = list(DuplicateRule("t", seed=5, rate=1.0, max_delay=3)
               .apply(iter(records)))
    first_dup_index = out.index(records[0], 1)  # first re-occurrence of #1
    assert 1 <= first_dup_index <= 3


def test_duplicate_rate_zero_is_passthrough():
    records = _records(list(range(1, 11)))
    out = list(DuplicateRule("t", seed=5, rate=0.0).apply(iter(records)))
    assert out == records


def test_duplicate_rule_is_deterministic():
    records = _records(list(range(1, 51)))
    a = list(DuplicateRule("t", seed=9, rate=0.3).apply(iter(records)))
    b = list(DuplicateRule("t", seed=9, rate=0.3).apply(iter(records)))
    assert a == b


def test_duplicate_rate_out_of_range_rejected():
    with pytest.raises(ValueError, match="duplicate_rate"):
        DuplicateRule("t", seed=1, rate=1.5)


# --- invalid FK rule ---------------------------------------------------------

def test_invalid_fk_rate_one_makes_every_reference_out_of_range():
    orders = _orders()
    rule = InvalidForeignKeyRule(
        "order", seed=3, fk_specs={"customer_id": N_CUSTOMERS}, rate=1.0)
    out = list(rule.apply(iter(orders)))
    assert all(o.customer_id > N_CUSTOMERS for o in out)


def test_invalid_fk_only_touches_configured_field():
    orders = _orders()
    rule = InvalidForeignKeyRule(
        "order", seed=3, fk_specs={"sales_rep_employee_id": 5}, rate=1.0)
    out = list(rule.apply(iter(orders)))
    assert all(o.sales_rep_employee_id > 5 for o in out)
    assert all(1 <= o.customer_id <= N_CUSTOMERS for o in out)  # untouched


def test_invalid_fk_is_deterministic():
    orders = _orders()
    first = list(InvalidForeignKeyRule(
        "order", seed=3, fk_specs={"customer_id": N_CUSTOMERS},
        rate=0.5).apply(iter(orders)))
    second = list(InvalidForeignKeyRule(
        "order", seed=3, fk_specs={"customer_id": N_CUSTOMERS},
        rate=0.5).apply(iter(orders)))
    assert first == second


def test_invalid_fk_unknown_field_fails_loudly():
    orders = _orders(count=1)
    rule = InvalidForeignKeyRule(
        "order", seed=3, fk_specs={"nope_id": N_CUSTOMERS}, rate=1.0)
    with pytest.raises(ValueError, match="nope_id"):
        list(rule.apply(iter(orders)))


def test_invalid_fk_rate_out_of_range_rejected():
    with pytest.raises(ValueError, match="invalid_fk_rate"):
        InvalidForeignKeyRule("order", seed=1, fk_specs={"customer_id": 5},
                              rate=-0.1)


# --- config validation (the protection symmetry) ------------------------------

def test_invalid_fk_rejects_non_identifier_fields(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.0\n  invalid_fk_rate: 0.1\n"
        "  datasets:\n    customer:\n"
        "      invalid_fk_fields:\n        phone: customer\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="_id"):
        load_config(config_file)


def test_invalid_fk_parent_must_exist_in_datasets():
    config = PipelineConfig(
        config_file=Path("pipeline.yaml"),
        run=RunConfig(seed=1),
        generation=GenerationConfig(reference_date=REF, history_days=365),
        datasets={"order": DatasetConfig(name="order", records=10)},
        output=OutputConfig(directory=Path("data/corrupted"), format="csv"),
        quality=QualityConfig(
            null_rate=0.0, invalid_fk_rate=0.1,
            datasets={"order": QualityDatasetConfig(
                invalid_fk_fields={"customer_id": "customer"})},
        ),
    )
    with pytest.raises(ConfigError, match="parent"):
        entry._validate_quality_output(config)


def test_backward_compat_null_only_config_still_parses(tmp_path: Path):
    """A 5a-shaped quality section (no new keys) still parses - new rates
    default to 0.0 (the optional-section default lesson, applied)."""
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.1\n"
        "  datasets:\n    customer:\n      null_fields: [phone]\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.quality.duplicate_rate == 0.0
    assert config.quality.invalid_fk_rate == 0.0


# --- the full chain -----------------------------------------------------------

def test_chain_null_invalid_fk_duplicate_together():
    """All three rules at rate 1.0: every emitted record is corrupted AND
    duplicated as an EXACT copy of the corrupted record (ADR-0022 order)."""
    orders = _orders()
    engine = ImpurityEngine(
        SeedManager(12345),
        QualityConfig(
            null_rate=1.0, duplicate_rate=1.0, invalid_fk_rate=1.0,
            datasets={"order": QualityDatasetConfig(
                null_fields=("status",),
                invalid_fk_fields={"customer_id": "customer"})},
        ),
        dataset_counts={"order": len(orders), "customer": N_CUSTOMERS},
    )
    out = list(engine.apply("order", iter(orders)))
    assert len(out) == 2 * len(orders)
    counts = Counter(out)
    assert all(count == 2 for count in counts.values())
    for record in out:
        assert record.status is None                    # null rule fired
        assert record.customer_id > N_CUSTOMERS         # invalid-FK rule fired


def test_engine_requires_counts_for_invalid_fk():
    engine = ImpurityEngine(
        SeedManager(1),
        QualityConfig(
            null_rate=0.0, invalid_fk_rate=1.0,
            datasets={"order": QualityDatasetConfig(
                invalid_fk_fields={"customer_id": "customer"})},
        ),
    )
    with pytest.raises(ValueError, match="dataset_counts"):
        list(engine.apply("order", iter(_orders(count=2))))
