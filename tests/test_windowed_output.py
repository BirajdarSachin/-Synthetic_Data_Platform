"""Milestone 6a tests: window keys, bucketing, temporal integrity, manifest v3."""

import csv
import hashlib
from datetime import date
from pathlib import Path

import pytest

import main as entry
from src.core.config import (
    ConfigError,
    GenerationConfig,
    OutputConfig,
    PipelineConfig,
    QualityConfig,
    QualityDatasetConfig,
    RunConfig,
    WindowConfig,
    load_config,
)
from src.core.seed_manager import SeedManager
from src.core.windows import compute_window_keys, window_key_for
from src.generators.customer import CustomerGenerator
from src.generators.order import OrderGenerator
from src.generators.order_item import OrderItemGenerator
from src.generators.product import ProductGenerator
from src.generators.supplier import SupplierGenerator
from src.metadata.manifest import (
    MANIFEST_VERSION,
    build_dataset_info,
    build_file_info,
    build_run_header,
)
from src.quality.engine import ImpurityEngine
from src.writers import get_writer

REF = date(2026, 1, 15)
DAILY_FULL = WindowConfig(granularity="daily", mode="full")
MONTHLY_FULL = WindowConfig(granularity="monthly", mode="full")


def _config(tmp_path: Path, window: WindowConfig | None = None,
            history_days: int = 30) -> PipelineConfig:
    return PipelineConfig(
        config_file=Path("pipeline.yaml"),
        run=RunConfig(seed=12345),
        generation=GenerationConfig(
            reference_date=REF, history_days=history_days, window=window),
        datasets={},
        output=OutputConfig(directory=tmp_path, format="csv"),
    )


def _customers(count: int = 60, seed: int = 42, history_days: int = 30) -> list:
    return list(CustomerGenerator(
        seed=seed, reference_date=REF,
        history_days=history_days).generate(count))


def _write(config, entity, records, key_of=None, seed: int = 1, corrupt=None):
    writer = get_writer(config.output.format)
    window_cfg = config.generation.window
    keys = None
    if window_cfg is not None:
        keys = compute_window_keys(
            config.generation.reference_date,
            config.generation.history_days,
            window_cfg.granularity, window_cfg.mode)
    return entry._write_records(
        config, writer, corrupt, entity, records,
        window_keys=keys, key_of=key_of)


def _read_csv_column(path: Path, column: str) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [row[column] for row in csv.DictReader(handle)]


# --- window key computation ---------------------------------------------------

def test_daily_full_keys():
    keys = compute_window_keys(REF, 10, "daily", "full")
    assert len(keys) == 11
    assert keys[0] == "2026-01-05"
    assert keys[-1] == "2026-01-15"


def test_monthly_full_keys():
    keys = compute_window_keys(REF, 45, "monthly", "full")
    assert keys == ["2025-12", "2026-01"]


def test_latest_mode_single_key():
    assert compute_window_keys(REF, 365, "daily", "latest") == ["2026-01-15"]
    assert compute_window_keys(REF, 365, "monthly", "latest") == ["2026-01"]


def test_window_key_derivation():
    assert window_key_for(date(2025, 12, 1), "daily") == "2025-12-01"
    assert window_key_for(date(2025, 12, 1), "monthly") == "2025-12"


def test_invalid_granularity_and_mode_rejected():
    with pytest.raises(ValueError, match="granularity"):
        compute_window_keys(REF, 10, "weekly", "full")
    with pytest.raises(ValueError, match="mode"):
        compute_window_keys(REF, 10, "daily", "sometimes")


def test_config_rejects_unknown_granularity(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "  window:\n    granularity: weekly\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/generated\n  format: csv\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="granularity"):
        load_config(config_file)


def test_absent_window_section_parses_to_none(tmp_path: Path):
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(
        "run:\n  seed: 1\n"
        "generation:\n  reference_date: 2026-01-15\n"
        "datasets:\n  customer:\n    records: 5\n"
        "output:\n  directory: data/generated\n  format: csv\n",
        encoding="utf-8",
    )
    assert load_config(config_file).generation.window is None


# --- windowed writing ----------------------------------------------------------

def test_windowed_paths_use_event_date_partition(tmp_path: Path):
    config = _config(tmp_path, window=DAILY_FULL)
    layout, files = _write(config, "customer", _customers(60),
                           key_of=lambda r: window_key_for(
                               r.registration_date, "daily"))
    assert layout == "windowed"
    assert files
    for info in files:
        assert info["path"].startswith("customer/event_date=")
        assert info["path"].endswith(".csv")


def test_bucketing_is_complete_and_pks_globally_sequential(tmp_path: Path):
    config = _config(tmp_path, window=DAILY_FULL)
    customers = _customers(60)
    layout, files = _write(config, "customer", customers,
                           key_of=lambda r: window_key_for(
                               r.registration_date, "daily"))
    assert sum(f["records"] for f in files) == len(customers)
    ids: set[int] = set()
    for info in files:
        path = tmp_path / info["path"]
        ids.update(int(v) for v in _read_csv_column(path, "customer_id"))
    assert ids == set(range(1, 61))  # ADR-0001 union guarantee (ADR-0024)


def test_snapshot_entities_stay_snapshot_in_windowed_mode(tmp_path: Path):
    config = _config(tmp_path, window=DAILY_FULL)
    suppliers = list(SupplierGenerator(seed=7).generate(10))
    layout, files = _write(config, "supplier", suppliers, key_of=None)
    assert layout == "snapshot"
    assert len(files) == 1
    assert "reference_date=2026-01-15" in files[0]["path"]


def test_latest_mode_keeps_only_reference_window(tmp_path: Path):
    config = _config(tmp_path,
                     window=WindowConfig(granularity="daily", mode="latest"),
                     history_days=1)
    layout, files = _write(config, "customer", _customers(50, history_days=1),
                           key_of=lambda r: window_key_for(
                               r.registration_date, "daily"))
    assert all(info["path"].startswith("customer/event_date=2026-01-15/")
               for info in files)
    assert sum(f["records"] for f in files) <= 50


def test_monthly_end_to_end(tmp_path: Path):
    config = _config(tmp_path, window=MONTHLY_FULL, history_days=45)
    layout, files = _write(config, "customer", _customers(40, history_days=45),
                           key_of=lambda r: window_key_for(
                               r.registration_date, "monthly"))
    assert layout == "windowed"
    assert sum(f["records"] for f in files) == 40
    assert all("event_date=2025-12" in f["path"]
               or "event_date=2026-01" in f["path"] for f in files)


def test_windowed_output_is_deterministic(tmp_path: Path):
    digests_first = digests_second = None
    runs = []
    for _ in range(2):
        config = _config(tmp_path / f"run{len(runs)}", window=DAILY_FULL)
        layout, files = _write(config, "customer", _customers(60),
                               key_of=lambda r: window_key_for(
                                   r.registration_date, "daily"))
        runs.append({info["path"]: hashlib.sha256(
            (config.output.directory / info["path"]).read_bytes()).hexdigest()
            for info in files})
    assert runs[0] == runs[1]


# --- temporal integrity across windows -----------------------------------------

def test_order_window_never_precedes_customer_registration_window(tmp_path: Path):
    """THE cross-window rule: it holds BECAUSE ADR-0016 already guarantees
    order_date >= registration_date - windowing inherits it for free."""
    config = _config(tmp_path, window=DAILY_FULL)
    customers = _customers(30, seed=1)
    orders = list(OrderGenerator(
        seed=7, reference_date=REF,
        customers=[(c.customer_id, c.registration_date) for c in customers],
        employee_count=3).generate(50))
    _write(config, "customer", customers,
           key_of=lambda r: window_key_for(r.registration_date, "daily"))
    _, order_files = _write(config, "order", orders,
                            key_of=lambda r: window_key_for(
                                r.order_date, "daily"))
    reg_window = {c.customer_id: window_key_for(c.registration_date, "daily")
                  for c in customers}
    for info in order_files:
        order_window = info["path"].split("event_date=")[1].split("/")[0]
        path = tmp_path / info["path"]
        for value in _read_csv_column(path, "customer_id"):
            assert reg_window[int(value)] <= order_window


def test_order_item_buckets_follow_order_window(tmp_path: Path):
    config = _config(tmp_path, window=DAILY_FULL)
    customers = _customers(30, seed=1)
    orders = list(OrderGenerator(
        seed=7, reference_date=REF,
        customers=[(c.customer_id, c.registration_date) for c in customers],
        employee_count=3).generate(50))
    products = list(ProductGenerator(seed=5, supplier_count=5).generate(20))
    items = list(OrderItemGenerator(
        seed=9, order_count=50,
        products=[(p.product_id, p.unit_price) for p in products]).generate(120))
    order_date_by_id = {o.order_id: o.order_date for o in orders}
    _, item_files = _write(
        config, "order_item", items,
        key_of=lambda it: window_key_for(
            order_date_by_id[it.order_id], "daily"))
    for info in item_files:
        item_window = info["path"].split("event_date=")[1].split("/")[0]
        path = tmp_path / info["path"]
        for value in _read_csv_column(path, "order_id"):
            assert window_key_for(order_date_by_id[int(value)], "daily") == item_window


# --- per-window impurity seed scopes --------------------------------------------

def test_engine_scope_changes_corruption_pattern():
    customers = _customers(60)
    engine = ImpurityEngine(
        SeedManager(12345),
        QualityConfig(null_rate=0.5,
                      datasets={"customer": QualityDatasetConfig(
                          null_fields=("phone",))}))
    first = list(engine.apply("customer", iter(customers), scope="2026-01-01"))
    second = list(engine.apply("customer", iter(customers), scope="2026-01-02"))
    again = list(engine.apply("customer", iter(customers), scope="2026-01-01"))
    nulled = lambda records: [r.customer_id for r in records if r.phone is None]
    assert nulled(first) == nulled(again)          # deterministic per scope
    assert nulled(first) != nulled(second)         # independent per window


# --- manifest v3 -----------------------------------------------------------------

def test_manifest_v3_files_array(tmp_path: Path):
    out_dir = tmp_path / "generated"
    file_path = (out_dir / "customer" / "event_date=2026-01-15"
                 / "customer_2026-01-15.csv")
    file_path.parent.mkdir(parents=True)
    file_path.write_text("x", encoding="utf-8")
    info = build_dataset_info(
        entity="customer", schema_version="1.0", layout="windowed",
        files=[build_file_info(file_path, out_dir, records=7)], child_seed=1)
    assert info["layout"] == "windowed"
    assert info["records"] == 7
    assert info["files"][0]["path"] == (
        "customer/event_date=2026-01-15/customer_2026-01-15.csv")
    assert info["files"][0]["file_sha256"]
    assert "path" not in info  # v3: paths live in files[]


def test_manifest_header_carries_v3_and_window(tmp_path: Path):
    from src.core.config import DatasetConfig
    config = PipelineConfig(
        config_file=Path("pipeline.yaml"),
        run=RunConfig(seed=1),
        generation=GenerationConfig(reference_date=REF, history_days=365,
                                    window=MONTHLY_FULL),
        datasets={"customer": DatasetConfig(name="customer", records=1)},
        output=OutputConfig(directory=tmp_path, format="csv"),
    )
    header = build_run_header(run_id="run_test", config=config, duration_s=0.5)
    assert header["manifest_version"] == MANIFEST_VERSION == 3
    assert header["window"] == {"granularity": "monthly", "mode": "full"}
