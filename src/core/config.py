"""Configuration loading and validation. Contract: ADR-0008.

YAML file -> typed, frozen dataclasses. Invalid configuration raises
ConfigError with a clear message - we never continue with a half-valid config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from src.core.paths import resolve_path

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ("csv",)  # json/parquet arrive in Phase 3


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class RunConfig:
    seed: int


@dataclass(frozen=True)
class GenerationConfig:
    reference_date: date


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    records: int


@dataclass(frozen=True)
class OutputConfig:
    directory: Path
    format: str


@dataclass(frozen=True)
class PipelineConfig:
    config_file: Path
    run: RunConfig
    generation: GenerationConfig
    datasets: dict[str, DatasetConfig]
    output: OutputConfig


def load_config(config_file: Path) -> PipelineConfig:
    """Load, validate and log a pipeline configuration file."""
    logger.info("[CONFIG] loading config_file=%s", config_file)
    if not config_file.is_file():
        raise ConfigError(f"Config file not found: {config_file}")

    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_file}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}")

    config = PipelineConfig(
        config_file=config_file,
        run=_parse_run(raw.get("run")),
        generation=_parse_generation(raw.get("generation")),
        datasets=_parse_datasets(raw.get("datasets")),
        output=_parse_output(raw.get("output")),
    )
    _log_resolved(config)
    return config


def _parse_run(section: object) -> RunConfig:
    if not isinstance(section, dict) or "seed" not in section:
        raise ConfigError("Missing required section/key: run.seed")
    seed = section["seed"]
    # bool is a subclass of int in Python - exclude it explicitly (gotcha).
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ConfigError("run.seed must be a non-negative integer")
    return RunConfig(seed=seed)


def _parse_generation(section: object) -> GenerationConfig:
    if not isinstance(section, dict) or "reference_date" not in section:
        raise ConfigError("Missing required section/key: generation.reference_date")
    value = section["reference_date"]
    # yaml.safe_load already parses YYYY-MM-DD into a datetime.date.
    if isinstance(value, date):
        return GenerationConfig(reference_date=value)
    raise ConfigError("generation.reference_date must be a date in YYYY-MM-DD form")


def _parse_datasets(section: object) -> dict[str, DatasetConfig]:
    if not isinstance(section, dict) or not section:
        raise ConfigError("Missing required section: datasets (at least one dataset)")
    datasets: dict[str, DatasetConfig] = {}
    for name, spec in section.items():
        records = spec.get("records") if isinstance(spec, dict) else None
        if not isinstance(records, int) or isinstance(records, bool) or records < 1:
            raise ConfigError(f"datasets.{name}.records must be an integer >= 1")
        datasets[name] = DatasetConfig(name=name, records=records)
    return datasets


def _parse_output(section: object) -> OutputConfig:
    if not isinstance(section, dict):
        raise ConfigError("Missing required section: output")
    directory = section.get("directory")
    fmt = section.get("format")
    if not isinstance(directory, str) or not directory:
        raise ConfigError("output.directory must be a non-empty string")
    if fmt not in SUPPORTED_FORMATS:
        raise ConfigError(f"output.format must be one of {SUPPORTED_FORMATS}, got {fmt!r}")
    return OutputConfig(directory=resolve_path(Path(directory)), format=fmt)


def _log_resolved(config: PipelineConfig) -> None:
    """Log every resolved value (ADR-0009: the CONFIG stage contract)."""
    logger.info("[CONFIG] seed=%s", config.run.seed)
    logger.info("[CONFIG] reference_date=%s", config.generation.reference_date.isoformat())
    for dataset in config.datasets.values():
        logger.info("[CONFIG] dataset=%s records=%s", dataset.name, dataset.records)
    logger.info("[CONFIG] output.directory=%s", config.output.directory)
    logger.info("[CONFIG] output.format=%s", config.output.format)
