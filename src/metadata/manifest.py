"""Run manifest - the audit record of one generation run.

Location: ADR-0011 (_manifests/). Schema: manifest_version 2 (ADR-0019) -
dataset entries store the output-relative POSIX path under "path" because
partitioned file names alone no longer locate the file.
"""

import hashlib
import importlib.metadata
import json
import logging
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from src import __version__
from src.core.config import PipelineConfig

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 2


def build_run_header(run_id: str, config: PipelineConfig, duration_s: float) -> dict[str, Any]:
    """Run-level facts. generated_at uses the REAL clock - run metadata only,
    never generated data (ADR-0003)."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "platform_version": __version__,
        "python_version": platform.python_version(),
        "faker_version": importlib.metadata.version("faker"),
        "master_seed": config.run.seed,
        "reference_date": config.generation.reference_date.isoformat(),
        "history_days": config.generation.history_days,
        "config_file": str(config.config_file),
        "output_format": config.output.format,
        "duration_s": round(duration_s, 2),
    }


def build_dataset_info(entity: str, schema_version: str, records: int,
                       file_path: Path, output_dir: Path,
                       child_seed: int) -> dict[str, Any]:
    """Facts about one generated dataset - including a checksum of its file.

    ADR-0019: "path" is relative to the output directory with POSIX
    separators - stable across operating systems and manifest readers.
    """
    return {
        "entity": entity,
        "schema_version": schema_version,
        "records": records,
        "path": file_path.relative_to(output_dir).as_posix(),
        "file_bytes": file_path.stat().st_size,
        "file_sha256": _sha256_of(file_path),
        "child_seed": child_seed,
    }


def write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    """Persist the manifest and log its location (ADR-0009: MANIFEST stage)."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("[MANIFEST] manifest=%s", manifest_path)


def _sha256_of(file_path: Path) -> str:
    """Streamed SHA-256 - memory-safe for large files."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
