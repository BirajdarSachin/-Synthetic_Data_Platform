"""Entry point - wires the stages together. No business logic lives here.

Pipeline: config -> seed -> generate -> write -> manifest.
Failures are logged to the run log and reported via process exit codes:
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
from src.metadata.manifest import (
    build_dataset_info,
    build_run_header,
    write_manifest,
)
from src.models.customer import SCHEMA_VERSION as CUSTOMER_SCHEMA_VERSION
from src.writers.csv_writer import write_csv

logger = logging.getLogger(__name__)

# Datasets this build of the platform knows how to generate.
KNOWN_DATASETS = {"customer"}


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

    datasets: dict[str, dict] = {}

    if "customer" in config.datasets:
        dataset_cfg = config.datasets["customer"]
        child_seed = seeds.child_seed(CustomerGenerator.ENTITY)
        generator = CustomerGenerator(
            seed=child_seed,
            reference_date=config.generation.reference_date,
            history_days=config.generation.history_days,
        )
        # File naming contract (ADR-0004): {entity}_{date}.{format}
        file_path = config.output.directory / (
            f"{CustomerGenerator.ENTITY}_{config.generation.reference_date}.csv"
        )
        records = write_csv(file_path, generator.generate(dataset_cfg.records))
        datasets["customer"] = build_dataset_info(
            entity=CustomerGenerator.ENTITY,
            schema_version=CUSTOMER_SCHEMA_VERSION,
            records=records,
            file_path=file_path,
            child_seed=child_seed,
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

    This is the ONLY place allowed to catch broadly: its job is to make sure
    every failure is recorded in the run log before the process dies.
    Nothing is swallowed - the full traceback is logged, then the process
    exits non-zero so scripts and schedulers can detect the failure.

    Exit codes: 0 = success, 1 = failure, 130 = interrupted (Ctrl+C).
    """
    try:
        main()
        return 0
    except KeyboardInterrupt:
        # Ctrl+C during a long run is a user decision, not an error.
        # (Will matter in Phase 7 when runs get long.)
        logger.warning("[SUMMARY] status=interrupted (Ctrl+C)")
        return 130
    except Exception as exc:
        # logger.exception must be called inside an except block:
        # it logs at ERROR level and automatically appends the full traceback,
        # so the complete failure story lands in the run log file.
        logger.exception("[SUMMARY] status=failed error=%s: %s",
                         type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(run())