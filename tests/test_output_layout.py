"""Milestone 4c tests: Hive layout, atomic writes, manifest v2 paths."""

from datetime import date
from pathlib import Path
from typing import Any, Iterator

import pytest

import main as entry
from src.core.config import (
    DatasetConfig,
    GenerationConfig,
    OutputConfig,
    PipelineConfig,
    RunConfig,
)
from src.metadata.manifest import build_dataset_info, build_run_header
from src.models.customer import Customer
from src.writers import get_writer
from src.writers.base import Writer

REF = date(2026, 1, 15)


class _FakeConfig:
    """Minimal stand-in: only the attributes the path builder touches."""

    class generation:
        reference_date = REF

    class output:
        directory = Path("data/generated")
        format = "csv"


def _two_customers() -> list[Customer]:
    return [
        Customer(1, "Ada", "Lovelace", "ada@example.com", "555-0100",
                 "London", "United Kingdom", REF),
        Customer(2, "Alan", "Turing", "alan@example.com", "555-0101",
                 "London", "United Kingdom", REF),
    ]


def test_path_builder_produces_hive_layout():
    path = entry._entity_file_path(_FakeConfig(), "customer")
    assert path.as_posix().endswith(
        "customer/reference_date=2026-01-15/customer_2026-01-15.csv"
    )


def test_atomic_success_leaves_no_temp_file(tmp_path: Path):
    target = tmp_path / "customer_2026-01-15.csv"
    count = get_writer("csv").write(target, iter(_two_customers()))
    assert count == 2
    assert target.exists()
    assert not (tmp_path / "customer_2026-01-15.csv.tmp").exists()


def test_failed_write_leaves_no_final_and_no_temp(tmp_path: Path):
    class _BoomWriter(Writer):
        def _open_file(self, file_path: Path):
            return file_path.open("w", encoding="utf-8", newline="")

        def _write_records(self, handle, records: Iterator[Any]) -> int:
            handle.write("partial")
            raise RuntimeError("boom mid-write")

    target = tmp_path / "x.csv"
    with pytest.raises(RuntimeError, match="boom"):
        _BoomWriter().write(target, iter(_two_customers()))
    assert not target.exists()
    assert not (tmp_path / "x.csv.tmp").exists()


def test_locked_target_reports_actionable_error(tmp_path: Path, monkeypatch):
    """With atomic writes a locked FINAL file fails at replace() time - the
    actionable message and temp cleanup must still hold (ADR-0020)."""
    def _locked_replace(src, dst):
        raise PermissionError(13, "Access denied", str(dst))

    monkeypatch.setattr("src.writers.base.os.replace", _locked_replace)
    target = tmp_path / "locked.csv"
    with pytest.raises(PermissionError, match="locked and cannot be written"):
        get_writer("csv").write(target, iter(_two_customers()))
    assert not target.exists()
    assert not (tmp_path / "locked.csv.tmp").exists()


def test_overwrite_still_works_through_replace(tmp_path: Path):
    target = tmp_path / "customer_2026-01-15.csv"
    writer = get_writer("csv")
    writer.write(target, iter(_two_customers()))
    writer.write(target, iter(_two_customers()))  # rebuild-in-place (ADR-0013)
    assert target.exists()
    assert not (tmp_path / "customer_2026-01-15.csv.tmp").exists()


def test_manifest_info_uses_output_relative_path(tmp_path: Path):
    out_dir = tmp_path / "generated"
    file_path = (out_dir / "customer" / "reference_date=2026-01-15"
                 / "customer_2026-01-15.csv")
    file_path.parent.mkdir(parents=True)
    file_path.write_text("x", encoding="utf-8")
    info = build_dataset_info(
        entity="customer", schema_version="1.0", records=1,
        file_path=file_path, output_dir=out_dir, child_seed=1,
    )
    assert info["path"] == (
        "customer/reference_date=2026-01-15/customer_2026-01-15.csv"
    )
    assert "file" not in info


def test_manifest_header_carries_schema_version(tmp_path: Path):
    config = PipelineConfig(
        config_file=Path("pipeline.yaml"),
        run=RunConfig(seed=1),
        generation=GenerationConfig(reference_date=REF, history_days=365),
        datasets={"customer": DatasetConfig(name="customer", records=1)},
        output=OutputConfig(directory=tmp_path, format="csv"),
    )
    header = build_run_header(run_id="run_test", config=config, duration_s=0.5)
    assert header["manifest_version"] == 2
