"""Tests for native Parquet schema and value preservation."""

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.generators.customer import CustomerGenerator
from src.generators.product import ProductGenerator
from src.writers import get_writer
from src.writers.base import Writer
from src.writers.parquet_writer import ParquetWriter

REFERENCE_DATE = date(2026, 1, 15)


def test_factory_returns_parquet_writer():
    writer = get_writer("parquet")
    assert isinstance(writer, Writer)
    assert isinstance(writer, ParquetWriter)


def test_parquet_preserves_native_date_schema_and_values(tmp_path: Path):
    customers = list(CustomerGenerator(
        seed=42, reference_date=REFERENCE_DATE, history_days=365
    ).generate(20))
    path = tmp_path / "customer.parquet"

    count = get_writer("parquet").write(path, iter(customers))

    schema = pq.read_schema(path)
    table = pq.read_table(path)
    assert count == len(customers)
    assert schema.field("registration_date").type == pa.date32()
    assert "index" not in schema.names
    assert table.column("registration_date").to_pylist()[0] == customers[0].registration_date
    assert isinstance(table.column("registration_date").to_pylist()[0], date)


def test_parquet_preserves_decimal128_and_exact_money(tmp_path: Path):
    products = list(ProductGenerator(seed=17, supplier_count=5).generate(20))
    path = tmp_path / "product.parquet"

    count = get_writer("parquet").write(path, iter(products))

    schema = pq.read_schema(path)
    values = pq.read_table(path).column("unit_price").to_pylist()
    price_type = schema.field("unit_price").type
    assert count == len(products)
    assert pa.types.is_decimal128(price_type)
    assert price_type.scale == 2
    assert values == [product.unit_price for product in products]
    assert all(isinstance(value, Decimal) for value in values)


def test_parquet_output_byte_identical_same_seed(tmp_path: Path):
    digests = []
    for index in range(2):
        products = ProductGenerator(seed=12345, supplier_count=5).generate(50)
        path = tmp_path / f"product-{index}.parquet"
        get_writer("parquet").write(path, products)
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_parquet_writer_rejects_empty_stream(tmp_path: Path):
    with pytest.raises(ValueError, match="No records"):
        get_writer("parquet").write(tmp_path / "empty.parquet", iter([]))


def test_config_accepts_parquet_format(tmp_path: Path):
    from src.core.config import load_config

    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/generated\n  format: parquet\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.output.format == "parquet"
