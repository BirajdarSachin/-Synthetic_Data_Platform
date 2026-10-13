"""Configuration loading and validation. Contract: ADR-0008.

YAML file -> typed, frozen dataclasses. Invalid configuration raises
ConfigError with a clear message - we never continue with a half-valid config.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from src.core.paths import resolve_path

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ("csv", "json", "parquet")

# ADR-0012: default history span preserves pre-0.3.0 behavior (3 * 365 days).
DEFAULT_HISTORY = "3y"

# ADR-0012 duration units: fixed lengths so config + seed stays deterministic.
_DURATION_UNIT_DAYS = {"y": 365, "m": 30, "d": 1}
_DURATION_TOKEN = re.compile(r"\d+[ymd]")


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class RunConfig:
    seed: int


@dataclass(frozen=True)
class GenerationConfig:
    reference_date: date
    history_days: int


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    records: int


@dataclass(frozen=True)
class OutputConfig:
    directory: Path
    format: str


@dataclass(frozen=True)
class QualityDatasetConfig:
    null_fields: tuple[str, ...]


@dataclass(frozen=True)
class QualityConfig:
    null_rate: float
    datasets: dict[str, QualityDatasetConfig]


@dataclass(frozen=True)
class PipelineConfig:
    config_file: Path
    run: RunConfig
    generation: GenerationConfig
    datasets: dict[str, DatasetConfig]
    output: OutputConfig
    quality: QualityConfig | None = None   # absent quality = clean run (ADR-0021)


def parse_duration_to_days(value: str) -> int:
    """Parse a compact duration string into days (ADR-0012).

    Accepted forms: '30d', '4m', '2y', '1y6m', '2y3m10d'.
    Units: y = 365 days, m = 30 days, d = 1 day. Each unit may appear once.
    Raises ConfigError for anything else.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            "generation.history must be a string like '3y', '6m', '30d' or '1y6m'"
        )
    text = value.strip().lower().replace(" ", "")
    tokens = _DURATION_TOKEN.findall(text)
    if not tokens or "".join(tokens) != text:
        raise ConfigError(
            f"generation.history has invalid format: {value!r}. "
            "Use forms like '3y', '6m', '30d', '1y6m'."
        )
    total_days = 0
    seen_units: set[str] = set()
    for token in tokens:
        number, unit = int(token[:-1]), token[-1]
        if unit in seen_units:
            raise ConfigError(f"generation.history repeats unit {unit!r}: {value!r}")
        seen_units.add(unit)
        total_days += number * _DURATION_UNIT_DAYS[unit]
    if total_days <= 0:
        raise ConfigError(f"generation.history must span at least 1 day: {value!r}")
    return total_days


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
        quality=_parse_quality(raw.get("quality")),
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
        # history is optional; default keeps pre-0.3.0 behavior (ADR-0012).
        history_days = parse_duration_to_days(section.get("history", DEFAULT_HISTORY))
        return GenerationConfig(reference_date=value, history_days=history_days)
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


def _parse_quality(section: object) -> QualityConfig | None:
    """Parse the optional quality section (ADR-0021). Absent = clean runs."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("quality must be a mapping")
    rate = section.get("null_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
            or not 0.0 <= float(rate) <= 1.0:
        raise ConfigError("quality.null_rate must be a number within [0, 1]")
    datasets_section = section.get("datasets")
    if not isinstance(datasets_section, dict) or not datasets_section:
        raise ConfigError("quality.datasets must list at least one dataset")
    datasets: dict[str, QualityDatasetConfig] = {}
    for name, spec in datasets_section.items():
        raw_fields = spec.get("null_fields") if isinstance(spec, dict) else None
        if (not isinstance(raw_fields, list) or not raw_fields
                or not all(isinstance(f, str) and f for f in raw_fields)):
            raise ConfigError(
                f"quality.datasets.{name}.null_fields must be a non-empty "
                "list of column names"
            )
        protected = [f for f in raw_fields if f.endswith("_id")]
        if protected:
            raise ConfigError(
                f"quality.datasets.{name}.null_fields contains identifier "
                f"field(s) {protected} - identifiers are protected in the "
                "null rule; deliberately invalid foreign keys are a separate "
                "impurity rule (ADR-0021)"
            )
        datasets[name] = QualityDatasetConfig(null_fields=tuple(raw_fields))
    return QualityConfig(null_rate=float(rate), datasets=datasets)


def _log_resolved(config: PipelineConfig) -> None:
    """Log every resolved value (ADR-0009: the CONFIG stage contract)."""
    logger.info("[CONFIG] seed=%s", config.run.seed)
    logger.info("[CONFIG] reference_date=%s", config.generation.reference_date.isoformat())
    logger.info("[CONFIG] history_days=%s", config.generation.history_days)
    for dataset in config.datasets.values():
        logger.info("[CONFIG] dataset=%s records=%s", dataset.name, dataset.records)
    logger.info("[CONFIG] output.directory=%s", config.output.directory)
    logger.info("[CONFIG] output.format=%s", config.output.format)
    if config.quality is None:
        logger.info("[CONFIG] quality=clean (no quality section)")
    else:
        logger.info("[CONFIG] quality null_rate=%s", config.quality.null_rate)
        for name, spec in config.quality.datasets.items():
            logger.info("[CONFIG] quality dataset=%s null_fields=%s",
                        name, list(spec.null_fields))
