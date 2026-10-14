"""Entry point - wires the stages together. No business logic lives here.

Pipeline: config -> seed -> generate -> impurity engine (optional) -> write
          (snapshot or windowed, ADR-0024) -> manifest.
Generation order follows FK dependencies (ADR-0014); context (ADR-0015) is
always extracted from CLEAN records - corruption is local to written files.
Windowed mode (ADR-0024): fact entities are bucketed by business date into
event_date= partitions; dimension entities (supplier, product) stay
snapshots. Temporal integrity across windows is inherited from ADR-0016.
Failures are logged to the run log and reported via exit codes:
  0 = success, 1 = failure, 130 = interrupted (Ctrl+C).
"""

import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

from src.core.config import ConfigError, load_config
from src.core.logging_setup import configure_logging, get_run_id
from src.core.paths import resolve_path
from src.core.seed_manager import SeedManager
from src.core.windows import compute_window_keys, window_key_for
from src.generators.customer import CustomerGenerator
from src.generators.employee import EmployeeGenerator
from src.generators.order import OrderGenerator
from src.generators.order_item import OrderItemGenerator
from src.generators.payment import PaymentGenerator
from src.generators.product import ProductGenerator
from src.generators.review import ReviewGenerator
from src.generators.supplier import SupplierGenerator
from src.metadata.manifest import (
    build_dataset_info,
    build_file_info,
    build_run_header,
    write_manifest,
)
from src.models.customer import SCHEMA_VERSION as CUSTOMER_SCHEMA_VERSION
from src.models.employee import SCHEMA_VERSION as EMPLOYEE_SCHEMA_VERSION
from src.models.order import SCHEMA_VERSION as ORDER_SCHEMA_VERSION
from src.models.order_item import SCHEMA_VERSION as ORDER_ITEM_SCHEMA_VERSION
from src.models.payment import SCHEMA_VERSION as PAYMENT_SCHEMA_VERSION
from src.models.product import SCHEMA_VERSION as PRODUCT_SCHEMA_VERSION
from src.models.review import SCHEMA_VERSION as REVIEW_SCHEMA_VERSION
from src.models.supplier import SCHEMA_VERSION as SUPPLIER_SCHEMA_VERSION
from src.quality.engine import ImpurityEngine
from src.writers import get_writer

logger = logging.getLogger(__name__)

KNOWN_DATASETS = {
    "customer", "supplier", "product", "employee", "order", "order_item",
    "payment", "review",
}

# ADR-0014/0016: a child entity requires its parents in the same config.
COMPOSITION_RULES = {
    "product": ["supplier"],
    "order": ["customer", "employee"],
    "order_item": ["order", "product"],
    "payment": ["order", "order_item"],
    "review": ["order", "order_item"],
}

# ADR-0024: fact entities are windowed by their business date; dimension
# entities (supplier, product) remain snapshots. order_item is bucketed via
# its order's date (handled separately - it has no date of its own).
WINDOW_DATE_FIELDS = {
    "customer": "registration_date",
    "employee": "hire_date",
    "order": "order_date",
    "payment": "payment_date",
    "review": "review_date",
}

# ADR-0021: corrupted runs must never overwrite the clean baseline.
_CLEAN_BASELINE = resolve_path(Path("data/generated"))


def _snapshot_file_path(config, entity: str) -> Path:
    """Snapshot layout (ADR-0019):
    {entity}/reference_date={date}/{entity}_{date}.{format}"""
    ref = config.generation.reference_date
    partition = f"reference_date={ref.isoformat()}"
    file_name = f"{entity}_{ref.isoformat()}.{config.output.format}"
    return config.output.directory / entity / partition / file_name


def _windowed_file_path(config, entity: str, window_key: str) -> Path:
    """Windowed layout (ADR-0024):
    {entity}/event_date={window_key}/{entity}_{window_key}.{format}"""
    partition = f"event_date={window_key}"
    file_name = f"{entity}_{window_key}.{config.output.format}"
    return config.output.directory / entity / partition / file_name


def _validate_composition(config) -> None:
    unknown = set(config.datasets) - KNOWN_DATASETS
    if unknown:
        raise ConfigError(f"Unknown dataset(s) in config: {sorted(unknown)}. "
                          f"Known: {sorted(KNOWN_DATASETS)}")
    for child, parents in COMPOSITION_RULES.items():
        if child in config.datasets:
            missing = [p for p in parents if p not in config.datasets]
            if missing:
                raise ConfigError(
                    f"{child} requires {missing} in datasets "
                    "(referential integrity, ADR-0014)"
                )
    if ("order_item" in config.datasets and "order" in config.datasets
            and config.datasets["order_item"].records
            < config.datasets["order"].records):
        raise ConfigError(
            "order_item.records must be >= order.records "
            "(every order needs at least one item, ADR-0016)"
        )


def _validate_quality_output(config) -> None:
    """Quality-run guards (structural checks live in config.py)."""
    if config.quality is None:
        return
    unknown = set(config.quality.datasets) - KNOWN_DATASETS
    if unknown:
        raise ConfigError(
            f"quality.datasets contains unknown dataset(s): {sorted(unknown)}"
        )
    if config.output.directory == _CLEAN_BASELINE:
        raise ConfigError(
            "quality is active but output.directory is the clean baseline "
            "(data/generated). Set output.directory: data/corrupted so "
            "corrupted runs never overwrite clean datasets (ADR-0021)"
        )
    for name, spec in config.quality.datasets.items():
        for field_name, parent in spec.invalid_fk_fields.items():
            if parent not in config.datasets:
                raise ConfigError(
                    f"quality.datasets.{name}.invalid_fk_fields"
                    f"[{field_name!r}] references parent {parent!r} which is "
                    "not in datasets - the parent record count is required "
                    "to compute out-of-range ids (ADR-0022)"
                )


def _write_records(config, writer, engine, entity: str, records: list,
                   window_keys: list[str] | None = None,
                   key_of=None) -> tuple[str, list[dict]]:
    """Write one entity's clean records as snapshot or windowed files.

    Returns (layout, files). In windowed mode, records are bucketed by
    their business-date window key (clean values - corruption never moves
    a record between windows, ADR-0024) and the engine applies per window
    stream with a per-window seed scope. mode="latest" keeps only the
    requested window; mode="full" treats an out-of-range date as a bug.
    """
    window_cfg = config.generation.window
    if window_cfg is None or key_of is None:
        path = _snapshot_file_path(config, entity)
        stream = engine.apply(entity, iter(records)) if engine else iter(records)
        count = writer.write(path, stream)
        return "snapshot", [build_file_info(path, config.output.directory, count)]

    buckets: dict[str, list] = {}
    for record in records:
        key = key_of(record)
        if key not in set(window_keys):
            if window_cfg.mode == "full":
                raise ValueError(
                    f"{entity} maps to window {key!r} outside the configured "
                    "window range - generation bug")
            continue  # mode="latest": keep only the requested window
        buckets.setdefault(key, []).append(record)

    files = []
    for key in sorted(buckets):
        path = _windowed_file_path(config, entity, key)
        stream = (engine.apply(entity, iter(buckets[key]), scope=key)
                  if engine else iter(buckets[key]))
        count = writer.write(path, stream)
        files.append(build_file_info(path, config.output.directory, count))
    return "windowed", files


def main() -> None:
    """Run the full pipeline. Raises on any failure - run() handles logging."""
    start = time.perf_counter()
    configure_logging()

    config = load_config(resolve_path(Path("config/pipeline.yaml")))
    seeds = SeedManager(config.run.seed)
    writer = get_writer(config.output.format)
    dataset_counts = {name: cfg.records for name, cfg in config.datasets.items()}
    engine = (ImpurityEngine(seeds, config.quality, dataset_counts)
              if config.quality else None)
    logger.info("[WRITER] output_dir=%s format=%s writer=%s",
                config.output.directory, config.output.format, type(writer).__name__)
    _validate_composition(config)
    _validate_quality_output(config)

    window_cfg = config.generation.window
    windowed = window_cfg is not None
    gran = window_cfg.granularity if windowed else None
    window_keys = (compute_window_keys(
        config.generation.reference_date, config.generation.history_days,
        gran, window_cfg.mode) if windowed else None)

    datasets: dict[str, dict] = {}
    facts: dict = {}
    order_date_by_id: dict[int, object] | None = None

    if "customer" in config.datasets:
        seed = seeds.child_seed(CustomerGenerator.ENTITY)
        gen = CustomerGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            history_days=config.generation.history_days,
        )
        customers = list(gen.generate(config.datasets["customer"].records))
        key_of = (lambda r: window_key_for(r.registration_date, gran)
                  if windowed else None)
        layout, files = _write_records(
            config, writer, engine, "customer", customers,
            window_keys, key_of)
        datasets["customer"] = build_dataset_info(
            entity="customer", schema_version=CUSTOMER_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)
        facts["customers"] = [(c.customer_id, c.registration_date) for c in customers]

    if "supplier" in config.datasets:
        seed = seeds.child_seed(SupplierGenerator.ENTITY)
        gen = SupplierGenerator(seed=seed)
        suppliers = list(gen.generate(config.datasets["supplier"].records))
        layout, files = _write_records(
            config, writer, engine, "supplier", suppliers)  # always snapshot
        datasets["supplier"] = build_dataset_info(
            entity="supplier", schema_version=SUPPLIER_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)

    if "product" in config.datasets:
        seed = seeds.child_seed(ProductGenerator.ENTITY)
        gen = ProductGenerator(
            seed=seed,
            supplier_count=config.datasets["supplier"].records,
        )
        products = list(gen.generate(config.datasets["product"].records))
        layout, files = _write_records(
            config, writer, engine, "product", products)  # always snapshot
        datasets["product"] = build_dataset_info(
            entity="product", schema_version=PRODUCT_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)
        facts["products"] = [(p.product_id, p.unit_price) for p in products]

    if "employee" in config.datasets:
        seed = seeds.child_seed(EmployeeGenerator.ENTITY)
        gen = EmployeeGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            history_days=config.generation.history_days,
        )
        employees = list(gen.generate(config.datasets["employee"].records))
        key_of = (lambda r: window_key_for(r.hire_date, gran)
                  if windowed else None)
        layout, files = _write_records(
            config, writer, engine, "employee", employees, window_keys, key_of)
        datasets["employee"] = build_dataset_info(
            entity="employee", schema_version=EMPLOYEE_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)

    if "order" in config.datasets:
        seed = seeds.child_seed(OrderGenerator.ENTITY)
        gen = OrderGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            customers=facts["customers"],
            employee_count=config.datasets["employee"].records,
        )
        orders = list(gen.generate(config.datasets["order"].records))
        key_of = (lambda r: window_key_for(r.order_date, gran)
                  if windowed else None)
        layout, files = _write_records(
            config, writer, engine, "order", orders, window_keys, key_of)
        datasets["order"] = build_dataset_info(
            entity="order", schema_version=ORDER_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)
        facts["completed_orders"] = [
            (o.order_id, o.order_date) for o in orders if o.status == "completed"
        ]
        if windowed and "order_item" in config.datasets:
            order_date_by_id = {o.order_id: o.order_date for o in orders}

    if "order_item" in config.datasets:
        seed = seeds.child_seed(OrderItemGenerator.ENTITY)
        gen = OrderItemGenerator(
            seed=seed,
            order_count=config.datasets["order"].records,
            products=facts["products"],
        )
        items = list(gen.generate(config.datasets["order_item"].records))
        key_of = None
        if windowed:
            key_of = (lambda it: window_key_for(
                order_date_by_id[it.order_id], gran))
        layout, files = _write_records(
            config, writer, engine, "order_item", items, window_keys, key_of)
        datasets["order_item"] = build_dataset_info(
            entity="order_item", schema_version=ORDER_ITEM_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)
        totals: dict[int, Decimal] = {}
        for item in items:
            totals[item.order_id] = (
                totals.get(item.order_id, Decimal("0.00"))
                + item.quantity * item.unit_price
            )
        facts["totals"] = totals
        if "review" in config.datasets:
            completed_customer_by_order = {
                order.order_id: (order.customer_id, order.order_date)
                for order in orders
                if order.status == "completed"
            }
            facts["purchases"] = [
                (completed_customer_by_order[item.order_id][0],
                 item.product_id,
                 completed_customer_by_order[item.order_id][1])
                for item in items
                if item.order_id in completed_customer_by_order
            ]

    if "review" in config.datasets:
        seed = seeds.child_seed(ReviewGenerator.ENTITY)
        gen = ReviewGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            purchases=facts["purchases"],
        )
        reviews = list(gen.generate(config.datasets["review"].records))
        key_of = (lambda r: window_key_for(r.review_date, gran)
                  if windowed else None)
        layout, files = _write_records(
            config, writer, engine, "review", reviews, window_keys, key_of)
        datasets["review"] = build_dataset_info(
            entity="review", schema_version=REVIEW_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)

    if "payment" in config.datasets:
        seed = seeds.child_seed(PaymentGenerator.ENTITY)
        gen = PaymentGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            completed_orders=facts["completed_orders"],
            totals=facts["totals"],
        )
        payments = list(gen.generate(config.datasets["payment"].records))
        key_of = (lambda r: window_key_for(r.payment_date, gran)
                  if windowed else None)
        layout, files = _write_records(
            config, writer, engine, "payment", payments, window_keys, key_of)
        datasets["payment"] = build_dataset_info(
            entity="payment", schema_version=PAYMENT_SCHEMA_VERSION,
            layout=layout, files=files, child_seed=seed)

    manifest = build_run_header(
        run_id=get_run_id(), config=config, duration_s=time.perf_counter() - start
    )
    manifest["datasets"] = datasets
    if config.quality is not None:
        manifest["quality"] = {
            "null_rate": config.quality.null_rate,
            "duplicate_rate": config.quality.duplicate_rate,
            "invalid_fk_rate": config.quality.invalid_fk_rate,
            "out_of_range_rate": config.quality.out_of_range_rate,
            "drift_rate": config.quality.drift_rate,
            "datasets": {
                name: {
                    "null_fields": list(spec.null_fields),
                    "invalid_fk_fields": dict(spec.invalid_fk_fields),
                    "out_of_range_fields": list(spec.out_of_range_fields),
                    "drop_fields": list(spec.drop_fields),
                    "extra_fields": list(spec.extra_fields),
                }
                for name, spec in config.quality.datasets.items()
            },
        }
    manifest_path = config.output.directory / "_manifests" / f"{get_run_id()}.json"
    write_manifest(manifest_path, manifest)

    total_records = sum(d["records"] for d in datasets.values())
    logger.info(
        "[SUMMARY] datasets=%s records=%s duration_s=%.2f status=success",
        len(datasets), total_records, time.perf_counter() - start,
    )


def _fake_config_for_guard_test(quality, directory: Path):
    """Test hook: build a minimal config for guard tests only."""
    from datetime import date
    from src.core.config import (
        DatasetConfig,
        GenerationConfig,
        OutputConfig,
        PipelineConfig,
        RunConfig,
    )
    return PipelineConfig(
        config_file=Path("pipeline.yaml"),
        run=RunConfig(seed=1),
        generation=GenerationConfig(reference_date=date(2026, 1, 15), history_days=365),
        datasets={"customer": DatasetConfig(name="customer", records=1)},
        output=OutputConfig(directory=directory, format="csv"),
        quality=quality,
    )


def run() -> int:
    """Entry wrapper: converts crashes into logged failures and exit codes."""
    try:
        main()
        return 0
    except KeyboardInterrupt:
        logger.warning("[SUMMARY] status=interrupted (Ctrl+C)")
        return 130
    except Exception as exc:
        logger.exception("[SUMMARY] status=failed error=%s: %s",
                         type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(run())
