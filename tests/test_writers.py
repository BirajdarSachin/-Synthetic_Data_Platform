"""Milestone 4a tests: writer interface, NDJSON output, shared serialization."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.config import load_config
from src.generators.customer import CustomerGenerator
from src.writers import get_writer
from src.writers.base import Writer
from src.writers.json_writer import JsonWriter
from src.writers.serialization import serialize_cell

REF = date(2026, 1, 15)


def _customers(count: int = 20, seed: int = 42):
    gen = CustomerGenerator(seed=seed, reference_date=REF, history_days=365)
    return list(gen.generate(count))


def test_get_writer_returns_requested_format():
    assert isinstance(get_writer("csv"), Writer)
    assert isinstance(get_writer("json"), JsonWriter)


def test_get_writer_rejects_unknown_format():
    with pytest.raises(ValueError, match="Unsupported output format"):
        get_writer("xml")


def test_serialize_cell_rules():
    assert serialize_cell(date(2026, 1, 15)) == "2026-01-15"
    assert serialize_cell(Decimal("17.5")) == "17.50"
    assert serialize_cell(5) == 5


def test_json_output_is_valid_ndjson(tmp_path: Path):
    customers = _customers()
    path = tmp_path / "customer.json"
    count = get_writer("json").write(path, iter(customers))
    assert count == len(customers)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(customers)
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["customer_id"] == 1
    assert parsed[0]["registration_date"] == customers[0].registration_date.isoformat()


def test_json_output_byte_identical_same_seed(tmp_path: Path):
    digests = []
    for _ in range(2):
        gen = CustomerGenerator(seed=12345, reference_date=REF, history_days=1095)
        path = tmp_path / "customer.json"
        get_writer("json").write(path, gen.generate(50))
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_json_writer_rejects_empty_stream(tmp_path: Path):
    with pytest.raises(ValueError, match="No records"):
        get_writer("json").write(tmp_path / "empty.json", iter([]))


def test_csv_writer_through_interface(tmp_path: Path):
    """Regression: the CSV path behaves identically behind the new interface."""
    gen = CustomerGenerator(seed=1, reference_date=REF, history_days=365)
    path = tmp_path / "customer.csv"
    count = get_writer("csv").write(path, gen.generate(10))
    assert count == 10
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 11  # header + 10 records
    assert lines[0].startswith("customer_id,")


def test_config_accepts_json_format(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/generated\n  format: json\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.output.format == "json"

