"""Milestone 3a tests: referential integrity, money format, determinism."""

import hashlib
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.generators.product import ProductGenerator
from src.generators.supplier import SupplierGenerator
from src.models.product import Product
from src.writers.csv_writer import write_csv

SUPPLIER_COUNT = 50
PRODUCT_COUNT = 300


def _suppliers(seed: int = 12345) -> list:
    return list(SupplierGenerator(seed=seed).generate(SUPPLIER_COUNT))


def _products(seed: int = 12345, supplier_count: int = SUPPLIER_COUNT) -> list[Product]:
    gen = ProductGenerator(seed=seed, supplier_count=supplier_count)
    return list(gen.generate(PRODUCT_COUNT))


def test_same_seed_identical_suppliers():
    a = [asdict(s) for s in _suppliers(seed=12345)]
    b = [asdict(s) for s in _suppliers(seed=12345)]
    assert a == b


def test_supplier_ids_sequential_starting_at_one():
    suppliers = _suppliers(seed=7)
    assert [s.supplier_id for s in suppliers] == list(range(1, SUPPLIER_COUNT + 1))


def test_same_seed_identical_products():
    a = [asdict(p) for p in _products(seed=12345)]
    b = [asdict(p) for p in _products(seed=12345)]
    assert a == b


def test_product_ids_sequential_starting_at_one():
    products = _products(seed=7)
    assert [p.product_id for p in products] == list(range(1, PRODUCT_COUNT + 1))


def test_every_product_references_existing_supplier():
    """THE referential integrity contract of Milestone 3a (ADR-0014)."""
    products = _products(seed=7)
    valid_ids = set(range(1, SUPPLIER_COUNT + 1))
    assert all(p.supplier_id in valid_ids for p in products)


def test_products_reference_multiple_suppliers():
    """Fixed seed makes this deterministic - not a statistical flake test."""
    products = _products(seed=7)
    assert len({p.supplier_id for p in products}) > 1


def test_unit_price_positive_and_two_decimals():
    products = _products(seed=7)
    for p in products:
        assert isinstance(p.unit_price, Decimal)
        assert p.unit_price > 0
        assert p.unit_price == p.unit_price.quantize(Decimal("0.01"))


def test_product_generator_rejects_zero_suppliers():
    with pytest.raises(ValueError):
        ProductGenerator(seed=1, supplier_count=0)


def test_product_csv_byte_identical_same_seed(tmp_path: Path):
    digests = []
    for _ in range(2):
        gen = ProductGenerator(seed=12345, supplier_count=SUPPLIER_COUNT)
        path = tmp_path / "product.csv"
        write_csv(path, gen.generate(PRODUCT_COUNT))
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]
