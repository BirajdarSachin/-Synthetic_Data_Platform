"""Configuration loading and validation. Contract: ADR-0008.

YAML file -> typed, frozen dataclasses. Invalid configuration raises
ConfigError with a clear message - we never continue with a half-valid config.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
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
    """Per-dataset impurity targets (ADR-0021/0022).

    null_fields:      columns the null rule may set to None (never *_id).
    invalid_fk_fields: mapping FK field -> parent dataset name (only *_id).
    """

    null_fields: tuple[str, ...] = ()
    invalid_fk_fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QualityConfig:
    """Impurity configuration (ADR-0021/0022). Absent section = clean runs."""

    null_rate: float
    datasets: dict[str, QualityDatasetConfig]
    duplicate_rate: float = 0.0
    invalid_fk_rate: float = 0.0


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


def _parse_rate(section: dict, key: str, default: float | None = None) -> float:
    """Parse and validate a quality rate within [0, 1] (ADR-0021/0022)."""
    value = section.get(key, default)
    if value is None:
        raise ConfigError(f"Missing required section/key: quality.{key}")
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not 0.0 <= float(value) <= 1.0):
        raise ConfigError(f"quality.{key} must be a number within [0, 1]")
    return float(value)


def _parse_quality(section: object) -> QualityConfig | None:
    """Parse the optional quality section (ADR-0021/0022). Absent = clean runs."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("quality must be a mapping")
    null_rate = _parse_rate(section, "null_rate")
    duplicate_rate = _parse_rate(section, "duplicate_rate", default=0.0)
    invalid_fk_rate = _parse_rate(section, "invalid_fk_rate", default=0.0)
    datasets_section = section.get("datasets")
    if not isinstance(datasets_section, dict) or not datasets_section:
        raise ConfigError("quality.datasets must list at least one dataset")
    datasets: dict[str, QualityDatasetConfig] = {}
    for name, spec in datasets_section.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"quality.datasets.{name} must be a mapping")
        raw_fields = spec.get("null_fields", [])
        if (not isinstance(raw_fields, list)
                or not all(isinstance(f, str) and f for f in raw_fields)):
            raise ConfigError(
                f"quality.datasets.{name}.null_fields must be a list of column names"
            )
        protected = [f for f in raw_fields if f.endswith("_id")]
        if protected:
            raise ConfigError(
                f"quality.datasets.{name}.null_fields contains identifier "
                f"field(s) {protected} - identifiers are protected in the "
                "null rule; deliberately invalid foreign keys are the "
                "separate invalid_fk rule (ADR-0021/0022)"
            )
        raw_fk = spec.get("invalid_fk_fields", {})
        if not isinstance(raw_fk, dict):
            raise ConfigError(
                f"quality.datasets.{name}.invalid_fk_fields must be a mapping "
                "of FK field -> parent dataset name"
            )
        non_ids = [f for f in raw_fk if not f.endswith("_id")]
        if non_ids:
            raise ConfigError(
                f"quality.datasets.{name}.invalid_fk_fields must contain only "
                f"identifier field(s) (*_id), got {non_ids} - the invalid-FK "
                "rule corrupts foreign keys only (ADR-0022)"
            )
        bad_parents = [p for p in raw_fk.values()
                       if not isinstance(p, str) or not p]
        if bad_parents:
            raise ConfigError(
                f"quality.datasets.{name}.invalid_fk_fields values must be "
                "non-empty parent dataset names"
            )
        datasets[name] = QualityDatasetConfig(
            null_fields=tuple(raw_fields),
            invalid_fk_fields=dict(raw_fk),
        )
    return QualityConfig(
        null_rate=null_rate,
        datasets=datasets,
        duplicate_rate=duplicate_rate,
        invalid_fk_rate=invalid_fk_rate,
    )


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
        logger.info(
            "[CONFIG] quality null_rate=%s duplicate_rate=%s invalid_fk_rate=%s",
            config.quality.null_rate, config.quality.duplicate_rate,
            config.quality.invalid_fk_rate,
        )
        for name, spec in config.quality.datasets.items():
            logger.info("[CONFIG] quality dataset=%s null_fields=%s invalid_fk=%s",
                        name, list(spec.null_fields), dict(spec.invalid_fk_fields))
