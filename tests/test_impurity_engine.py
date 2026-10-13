"""Milestone 5a tests: quality config, NULL injection, format behavior."""

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.core.config import (
    ConfigError,
    QualityConfig,
    QualityDatasetConfig,
    load_config,
    parse_duration_to_days,
)
from src.generators.customer import CustomerGenerator
from src.generators.product import ProductGenerator
from src.quality.engine import ImpurityEngine
from src.quality.null_rule import NullInjectionRule
from src.core.seed_manager import SeedManager
from src.writers import get_writer

import main as entry

REF = date(2026, 1, 15)
QUALITY = QualityConfig(
    null_rate=1.0,
    datasets={"customer": QualityDatasetConfig(null_fields=("phone", "city"))},
)


def _customers(count: int = 200, seed: int = 42) -> list:
    return list(CustomerGenerator(
        seed=seed, reference_date=REF, history_days=365).generate(count))


def _engine(quality) -> ImpurityEngine:
    return ImpurityEngine(SeedManager(12345), quality)


# --- clean regression (backward compatibility) ------------------------------

def test_missing_quality_section_parses_to_none(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/generated\n  format: csv\n",
        encoding="utf-8",
    )
    assert load_config(config_file).quality is None


def test_engine_without_entity_config_is_pure_passthrough():
    customers = _customers()
    engine = _engine(QUALITY)
    out = list(engine.apply("product", iter(customers)))  # no product spec
    assert out == customers


def test_rate_zero_corrupts_nothing():
    customers = _customers()
    quality = QualityConfig(
        null_rate=0.0,
        datasets={"customer": QualityDatasetConfig(null_fields=("phone",))},
    )
    out = list(_engine(quality).apply("customer", iter(customers)))
    assert out == customers


# --- determinism and rate control --------------------------------------------

def test_same_seed_produces_identical_corruption():
    customers = _customers()
    first = [asdict(r) for r in _engine(QUALITY).apply("customer", iter(customers))]
    second = [asdict(r) for r in _engine(QUALITY).apply("customer", iter(customers))]
    assert first == second


def test_rate_one_corrupts_every_record_exactly_one_field():
    customers = _customers()
    out = list(_engine(QUALITY).apply("customer", iter(customers)))
    assert len(out) == len(customers)
    for original, corrupted in zip(customers, out):
        nones = [f for f in ("phone", "city") if getattr(corrupted, f) is None]
        assert len(nones) == 1


def test_fixed_seed_corrupted_count_is_stable():
    """Statistical rate (0.01) is still deterministic per seed: run twice."""
    customers = _customers(count=1000)
    quality = QualityConfig(
        null_rate=0.01,
        datasets={"customer": QualityDatasetConfig(null_fields=("phone", "city"))},
    )
    a = list(_engine(quality).apply("customer", iter(customers)))
    b = list(_engine(quality).apply("customer", iter(customers)))
    nulled = sum(1 for r in a if r.phone is None or r.city is None)
    assert 1 <= nulled <= 100  # sane band for rate 0.01 on 1000 records
    assert sum(1 for r in b if r.phone is None or r.city is None) == nulled


# --- targeting and immutability ----------------------------------------------

def test_only_configured_fields_ever_nulled_and_rest_is_identical():
    customers = _customers()
    out = list(_engine(QUALITY).apply("customer", iter(customers)))
    for original, corrupted in zip(customers, out):
        for name, _ in asdict(original).items():
            if name in ("phone", "city"):
                continue
            assert getattr(corrupted, name) == getattr(original, name)


def test_engine_does_not_mutate_input_records():
    customers = _customers()
    snapshot = [asdict(r) for r in customers]
    list(_engine(QUALITY).apply("customer", iter(customers)))
    assert [asdict(r) for r in customers] == snapshot


# --- config validation --------------------------------------------------------

def test_identifier_fields_are_rejected(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 0.1\n"
        "  datasets:\n    customer:\n      null_fields: [customer_id]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="identifier"):
        load_config(config_file)


def test_null_rate_out_of_range_rejected(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/corrupted\n  format: csv\n"
        "quality:\n  null_rate: 1.5\n"
        "  datasets:\n    customer:\n      null_fields: [phone]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="null_rate"):
        load_config(config_file)


def test_unknown_field_fails_loudly_on_first_record():
    customers = _customers(count=1)
    quality = QualityConfig(
        null_rate=1.0,
        datasets={"customer": QualityDatasetConfig(null_fields=("nope",))},
    )
    with pytest.raises(ValueError, match="nope"):
        list(_engine(quality).apply("customer", iter(customers)))


def test_guard_blocks_quality_writes_to_clean_baseline(tmp_path: Path, monkeypatch):
    """ADR-0021: quality active + the clean baseline directory -> ConfigError.

    The guard compares resolved absolute paths, so the test points the
    baseline constant at tmp_path via monkeypatch (auto-restored)."""
    monkeypatch.setattr(entry, "_CLEAN_BASELINE", tmp_path)
    config = entry._fake_config_for_guard_test(quality=QUALITY, directory=tmp_path)
    with pytest.raises(ConfigError, match="clean baseline"):
        entry._validate_quality_output(config)


def test_guard_allows_quality_writes_outside_baseline(tmp_path: Path, monkeypatch):
    """The complementary half: a non-baseline directory must NOT raise."""
    monkeypatch.setattr(entry, "_CLEAN_BASELINE", tmp_path / "generated")
    config = entry._fake_config_for_guard_test(
        quality=QUALITY, directory=tmp_path / "corrupted")
    entry._validate_quality_output(config)  # must not raise


# --- format behavior ----------------------------------------------------------

def test_csv_renders_null_as_empty_cell(tmp_path: Path):
    customers = _customers(count=5)
    path = tmp_path / "customer.csv"
    get_writer("csv").write(path, _engine(QUALITY).apply("customer", iter(customers)))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6  # header + 5
    for line in lines[1:]:
        cells = line.split(",")
        assert cells[4] == "" or cells[5] == ""  # phone or city empty


def test_json_renders_null_as_json_null(tmp_path: Path):
    customers = _customers(count=5)
    path = tmp_path / "customer.json"
    get_writer("json").write(path, _engine(QUALITY).apply("customer", iter(customers)))
    parsed = [json.loads(line) for line in
              path.read_text(encoding="utf-8").splitlines()]
    assert all("phone" in r or "city" in r for r in parsed)
    assert any(r["phone"] is None or r["city"] is None for r in parsed)


def test_parquet_null_keeps_pinned_schema(tmp_path: Path):
    """THE spike as a test: None in a pinned decimal column stays null,
    and the schema contract (ADR-0018) is unchanged."""
    products = list(ProductGenerator(seed=9, supplier_count=5).generate(20))
    quality = QualityConfig(
        null_rate=1.0,
        datasets={"product": QualityDatasetConfig(null_fields=("unit_price",))},
    )
    path = tmp_path / "product.parquet"
    get_writer("parquet").write(
        path, _engine(quality).apply("product", iter(products)))
    schema = pq.read_schema(path)
    assert schema.field("unit_price").type == pa_decimal128()
    column = pq.read_table(path).column("unit_price").to_pylist()
    assert all(v is None for v in column)


def pa_decimal128():
    import pyarrow as pa
    return pa.decimal128(12, 2)
