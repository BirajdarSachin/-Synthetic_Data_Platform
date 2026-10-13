"""Entry point - wires the stages together. No business logic lives here.

Pipeline: config -> seed -> generate -> write (format from config, ADR-0017)
          -> manifest.
Generation order follows FK dependencies (ADR-0014):
  customer, supplier, employee (independent)
    -> product (needs supplier count)
    -> order (needs customer DATES + employee count, ADR-0015 context)
    -> order_item (needs order count + product prices)
    -> review (completed orders + purchased order items)
    -> payment (needs completed orders + item totals)
Output layout is Hive-style (ADR-0019); writes are atomic (ADR-0020).
main.py selects the Writer via the factory and never imports concrete writers.
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


def _entity_file_path(config, entity: str) -> Path:
    """Hive-style layout (ADR-0019):
    {entity}/reference_date={date}/{entity}_{date}.{format}
    The date stays in the file name so ADR-0004's naming contract holds."""
    ref = config.generation.reference_date
    partition = f"reference_date={ref.isoformat()}"
    file_name = f"{entity}_{ref.isoformat()}.{config.output.format}"
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


def _run_entity(config, writer, generator, schema_version: str,
                child_seed: int) -> tuple[list, dict]:
    """Generate, write and build manifest info for one entity. Returns (records, info)."""
    cfg = config.datasets[generator.ENTITY]
    file_path = _entity_file_path(config, generator.ENTITY)
    records = list(generator.generate(cfg.records))
    count = writer.write(file_path, records)
    info = build_dataset_info(
        entity=generator.ENTITY,
        schema_version=schema_version,
        records=count,
        file_path=file_path,
        output_dir=config.output.directory,
        child_seed=child_seed,
    )
    return records, info


def main() -> None:
    """Run the full pipeline. Raises on any failure - run() handles logging."""
    start = time.perf_counter()
    configure_logging()

    config = load_config(resolve_path(Path("config/pipeline.yaml")))
    seeds = SeedManager(config.run.seed)
    writer = get_writer(config.output.format)
    logger.info("[WRITER] output_dir=%s format=%s writer=%s",
                config.output.directory, config.output.format, type(writer).__name__)
    _validate_composition(config)

    datasets: dict[str, dict] = {}
    facts: dict = {}

    if "customer" in config.datasets:
        seed = seeds.child_seed(CustomerGenerator.ENTITY)
        gen = CustomerGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            history_days=config.generation.history_days,
        )
        customers, info = _run_entity(
            config, writer, gen, CUSTOMER_SCHEMA_VERSION, seed)
        datasets["customer"] = info
        facts["customers"] = [(c.customer_id, c.registration_date) for c in customers]

    if "supplier" in config.datasets:
        seed = seeds.child_seed(SupplierGenerator.ENTITY)
        gen = SupplierGenerator(seed=seed)
        _, info = _run_entity(config, writer, gen, SUPPLIER_SCHEMA_VERSION, seed)
        datasets["supplier"] = info

    if "product" in config.datasets:
        seed = seeds.child_seed(ProductGenerator.ENTITY)
        gen = ProductGenerator(
            seed=seed,
            supplier_count=config.datasets["supplier"].records,
        )
        products, info = _run_entity(
            config, writer, gen, PRODUCT_SCHEMA_VERSION, seed)
        datasets["product"] = info
        facts["products"] = [(p.product_id, p.unit_price) for p in products]

    if "employee" in config.datasets:
        seed = seeds.child_seed(EmployeeGenerator.ENTITY)
        gen = EmployeeGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            history_days=config.generation.history_days,
        )
        _, info = _run_entity(config, writer, gen, EMPLOYEE_SCHEMA_VERSION, seed)
        datasets["employee"] = info

    if "order" in config.datasets:
        seed = seeds.child_seed(OrderGenerator.ENTITY)
        gen = OrderGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            customers=facts["customers"],
            employee_count=config.datasets["employee"].records,
        )
        orders, info = _run_entity(config, writer, gen, ORDER_SCHEMA_VERSION, seed)
        datasets["order"] = info
        facts["completed_orders"] = [
            (o.order_id, o.order_date) for o in orders if o.status == "completed"
        ]

    if "order_item" in config.datasets:
        seed = seeds.child_seed(OrderItemGenerator.ENTITY)
        gen = OrderItemGenerator(
            seed=seed,
            order_count=config.datasets["order"].records,
            products=facts["products"],
        )
        items, info = _run_entity(
            config, writer, gen, ORDER_ITEM_SCHEMA_VERSION, seed)
        datasets["order_item"] = info
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
        _, info = _run_entity(config, writer, gen, REVIEW_SCHEMA_VERSION, seed)
        datasets["review"] = info

    if "payment" in config.datasets:
        seed = seeds.child_seed(PaymentGenerator.ENTITY)
        gen = PaymentGenerator(
            seed=seed,
            reference_date=config.generation.reference_date,
            completed_orders=facts["completed_orders"],
            totals=facts["totals"],
        )
        _, info = _run_entity(config, writer, gen, PAYMENT_SCHEMA_VERSION, seed)
        datasets["payment"] = info

    manifest = build_run_header(
        run_id=get_run_id(), config=config, duration_s=time.perf_counter() - start
    )
    manifest["datasets"] = datasets
    manifest_path = config.output.directory / "_manifests" / f"{get_run_id()}.json"
    write_manifest(manifest_path, manifest)

    total_records = sum(d["records"] for d in datasets.values())
    logger.info(
        "[SUMMARY] datasets=%s records=%s duration_s=%.2f status=success",
        len(datasets), total_records, time.perf_counter() - start,
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
