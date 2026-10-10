"""Entry point - wires the stages together. No business logic lives here.

Pipeline: config -> seed -> generate -> write -> manifest.
Generation order follows FK dependencies (ADR-0014):
customer (independent) -> supplier (independent) -> product (needs supplier).
Failures are logged to the run log and reported via exit codes:
  0 = success, 1 = failure, 130 = interrupted (Ctrl+C).
"""

import logging
import sys
import time
from pathlib import Path

from src.core.config import ConfigError, load_config
from src.core.logging_setup import configure_logging, get_run_id
from src.core.paths import resolve_path
from src.core.seed_manager import SeedManager
from src.generators.customer import CustomerGenerator
from src.generators.product import ProductGenerator
from src.generators.supplier import SupplierGenerator
from src.metadata.manifest import (
    build_dataset_info,
    build_run_header,
    write_manifest,
)
from src.models.customer import SCHEMA_VERSION as CUSTOMER_SCHEMA_VERSION
from src.models.product import SCHEMA_VERSION as PRODUCT_SCHEMA_VERSION
from src.models.supplier import SCHEMA_VERSION as SUPPLIER_SCHEMA_VERSION
from src.writers.csv_writer import write_csv

logger = logging.getLogger(__name__)

# Datasets this build of the platform knows how to generate.
KNOWN_DATASETS = {"customer", "supplier", "product"}


def _entity_file_path(config, entity: str) -> Path:
    """File naming contract (ADR-0004): {entity}_{reference_date}.{format}."""
    return config.output.directory / (
        f"{entity}_{config.generation.reference_date}.{config.output.format}"
    )


def main() -> None:
    """Run the full pipeline. Raises on any failure - run() handles logging."""
    start = time.perf_counter()
    configure_logging()

    config = load_config(resolve_path(Path("config/pipeline.yaml")))
    seeds = SeedManager(config.run.seed)
    logger.info("[WRITER] output_dir=%s", config.output.directory)

    unknown = set(config.datasets) - KNOWN_DATASETS
    if unknown:
        raise ConfigError(f"Unknown dataset(s) in config: {sorted(unknown)}. "
                          f"Known: {sorted(KNOWN_DATASETS)}")
    # Dataset composition rule: a child entity requires its parent in config.
    if "product" in config.datasets and "supplier" not in config.datasets:
        raise ConfigError("product requires supplier in datasets "
                          "(referential integrity, ADR-0014)")

    datasets: dict[str, dict] = {}

    if "customer" in config.datasets:
        cfg = config.datasets["customer"]
        child_seed = seeds.child_seed(CustomerGenerator.ENTITY)
        generator = CustomerGenerator(
            seed=child_seed,
            reference_date=config.generation.reference_date,
            history_days=config.generation.history_days,
        )
        file_path = _entity_file_path(config, CustomerGenerator.ENTITY)
        records = write_csv(file_path, generator.generate(cfg.records))
        datasets["customer"] = build_dataset_info(
            entity=CustomerGenerator.ENTITY,
            schema_version=CUSTOMER_SCHEMA_VERSION,
            records=records, file_path=file_path, child_seed=child_seed,
        )

    if "supplier" in config.datasets:
        cfg = config.datasets["supplier"]
        child_seed = seeds.child_seed(SupplierGenerator.ENTITY)
        generator = SupplierGenerator(seed=child_seed)
        file_path = _entity_file_path(config, SupplierGenerator.ENTITY)
        records = write_csv(file_path, generator.generate(cfg.records))
        datasets["supplier"] = build_dataset_info(
            entity=SupplierGenerator.ENTITY,
            schema_version=SUPPLIER_SCHEMA_VERSION,
            records=records, file_path=file_path, child_seed=child_seed,
        )

    if "product" in config.datasets:
        cfg = config.datasets["product"]
        child_seed = seeds.child_seed(ProductGenerator.ENTITY)
        generator = ProductGenerator(
            seed=child_seed,
            supplier_count=config.datasets["supplier"].records,
        )
        file_path = _entity_file_path(config, ProductGenerator.ENTITY)
        records = write_csv(file_path, generator.generate(cfg.records))
        datasets["product"] = build_dataset_info(
            entity=ProductGenerator.ENTITY,
            schema_version=PRODUCT_SCHEMA_VERSION,
            records=records, file_path=file_path, child_seed=child_seed,
        )

    manifest = build_run_header(
        run_id=get_run_id(), config=config, duration_s=time.perf_counter() - start
    )
    manifest["datasets"] = datasets
    manifest_path = (
        config.output.directory / "_manifests" / f"{get_run_id()}.json"
    )
    write_manifest(manifest_path, manifest)

    total_records = sum(d["records"] for d in datasets.values())
    logger.info(
        "[SUMMARY] datasets=%s records=%s duration_s=%.2f status=success",
        len(datasets), total_records, time.perf_counter() - start,
    )


def run() -> int:
    """Entry wrapper: converts crashes into logged failures and exit codes.

    The ONLY place allowed to catch broadly: every failure must be recorded
    in the run log before the process dies. Exit codes: 0 success, 1 failure,
    130 interrupted.
    """
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
