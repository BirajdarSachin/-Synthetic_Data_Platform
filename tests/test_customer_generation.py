"""Milestone 2 tests: determinism, schema contract, CSV reproducibility."""

import hashlib
from dataclasses import asdict
from datetime import date
from pathlib import Path

from src.generators.customer import CustomerGenerator
from src.models.customer import Customer
from src.writers.csv_writer import write_csv

# Fixed reference date - never the real clock (ADR-0003).
REFERENCE_DATE = date(2026, 1, 15)


def _generate(seed: int, count: int) -> list[Customer]:
    generator = CustomerGenerator(seed=seed, reference_date=REFERENCE_DATE)
    return list(generator.generate(count))


def test_same_seed_produces_identical_records():
    first = [asdict(r) for r in _generate(seed=12345, count=50)]
    second = [asdict(r) for r in _generate(seed=12345, count=50)]
    assert first == second


def test_different_seed_produces_different_records():
    a = [asdict(r) for r in _generate(seed=1, count=10)]
    b = [asdict(r) for r in _generate(seed=2, count=10)]
    assert a != b


def test_ids_are_sequential_starting_at_one():
    customers = _generate(seed=7, count=100)
    assert [c.customer_id for c in customers] == list(range(1, 101))


def test_emails_are_unique():
    customers = _generate(seed=7, count=100)
    emails = [c.email for c in customers]
    assert len(set(emails)) == len(emails)


def test_registration_date_not_in_future():
    customers = _generate(seed=7, count=100)
    assert all(c.registration_date <= REFERENCE_DATE for c in customers)


def test_csv_output_is_byte_identical_for_same_seed(tmp_path: Path):
    """The strongest reproducibility proof: same seed -> identical bytes."""
    digests = []
    for _ in range(2):
        generator = CustomerGenerator(seed=12345, reference_date=REFERENCE_DATE)
        path = tmp_path / "customer.csv"
        write_csv(path, generator.generate(100))
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]
