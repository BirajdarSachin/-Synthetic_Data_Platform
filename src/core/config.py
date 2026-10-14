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

DEFAULT_HISTORY = "3y"

_DURATION_UNIT_DAYS = {"y": 365, "m": 30, "d": 1}
_DURATION_TOKEN = re.compile(r"\d+[ymd]")

WINDOW_GRANULARITIES = ("daily", "monthly")
WINDOW_MODES = ("full", "latest")


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class RunConfig:
    seed: int


@dataclass(frozen=True)
class WindowConfig:
    """Windowed generation (ADR-0024). Absent = single snapshot file."""

    granularity: str  # "daily" | "monthly"
    mode: str         # "full" | "latest"


@dataclass(frozen=True)
class GenerationConfig:
    reference_date: date
    history_days: int
    window: WindowConfig | None = None


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
    """Per-dataset impurity targets (ADR-0021/0022/0023)."""

    null_fields: tuple[str, ...] = ()
    invalid_fk_fields: dict[str, str] = field(default_factory=dict)
    out_of_range_fields: tuple[str, ...] = ()
    drop_fields: tuple[str, ...] = ()
    extra_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityConfig:
    """Impurity configuration. Absent section = clean runs (ADR-0021)."""

    null_rate: float
    datasets: dict[str, QualityDatasetConfig]
    duplicate_rate: float = 0.0
    invalid_fk_rate: float = 0.0
    out_of_range_rate: float = 0.0
    drift_rate: float = 0.0


@dataclass(frozen=True)
class PipelineConfig:
    config_file: Path
    run: RunConfig
    generation: GenerationConfig
    datasets: dict[str, DatasetConfig]
    output: OutputConfig
    quality: QualityConfig | None = None


def parse_duration_to_days(value: str) -> int:
    """Parse a compact duration string into days (ADR-0012)."""
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
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ConfigError("run.seed must be a non-negative integer")
    return RunConfig(seed=seed)


def _parse_window(section: object) -> WindowConfig | None:
    """Parse the optional generation.window section (ADR-0024)."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("generation.window must be a mapping")
    granularity = section.get("granularity")
    if granularity not in WINDOW_GRANULARITIES:
        raise ConfigError(
            f"generation.window.granularity must be one of "
            f"{WINDOW_GRANULARITIES}, got {granularity!r}"
        )
    mode = section.get("mode", "full")
    if mode not in WINDOW_MODES:
        raise ConfigError(
            f"generation.window.mode must be one of {WINDOW_MODES}, "
            f"got {mode!r}"
        )
    return WindowConfig(granularity=granularity, mode=mode)


def _parse_generation(section: object) -> GenerationConfig:
    if not isinstance(section, dict) or "reference_date" not in section:
        raise ConfigError("Missing required section/key: generation.reference_date")
    value = section["reference_date"]
    if isinstance(value, date):
        history_days = parse_duration_to_days(section.get("history", DEFAULT_HISTORY))
        return GenerationConfig(
            reference_date=value,
            history_days=history_days,
            window=_parse_window(section.get("window")),
        )
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
    """Parse and validate a quality rate within [0, 1]."""
    value = section.get(key, default)
    if value is None:
        raise ConfigError(f"Missing required section/key: quality.{key}")
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not 0.0 <= float(value) <= 1.0):
        raise ConfigError(f"quality.{key} must be a number within [0, 1]")
    return float(value)


def _parse_string_list(section: dict, dataset: str, key: str) -> list[str]:
    raw = section.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(f, str) and f for f in raw):
        raise ConfigError(
            f"quality.datasets.{dataset}.{key} must be a list of column names"
        )
    return raw


def _reject_identifiers(dataset: str, key: str, fields: list[str]) -> None:
    ids = [f for f in fields if f.endswith("_id")]
    if ids:
        raise ConfigError(
            f"quality.datasets.{dataset}.{key} contains identifier field(s) "
            f"{ids} - identifier corruption is exclusively the invalid_fk "
            "rule's job (ADR-0021/0023)"
        )


def _parse_quality(section: object) -> QualityConfig | None:
    """Parse the optional quality section (ADR-0021/0022/0023)."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("quality must be a mapping")
    null_rate = _parse_rate(section, "null_rate")
    duplicate_rate = _parse_rate(section, "duplicate_rate", default=0.0)
    invalid_fk_rate = _parse_rate(section, "invalid_fk_rate", default=0.0)
    out_of_range_rate = _parse_rate(section, "out_of_range_rate", default=0.0)
    drift_rate = _parse_rate(section, "drift_rate", default=0.0)
    datasets_section = section.get("datasets")
    if not isinstance(datasets_section, dict) or not datasets_section:
        raise ConfigError("quality.datasets must list at least one dataset")
    datasets: dict[str, QualityDatasetConfig] = {}
    for name, spec in datasets_section.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"quality.datasets.{name} must be a mapping")
        raw_null = _parse_string_list(spec, name, "null_fields")
        _reject_identifiers(name, "null_fields", raw_null)

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
                f"identifier field(s) (*_id), got {non_ids}"
            )
        bad_parents = [p for p in raw_fk.values()
                       if not isinstance(p, str) or not p]
        if bad_parents:
            raise ConfigError(
                f"quality.datasets.{name}.invalid_fk_fields values must be "
                "non-empty parent dataset names"
            )

        raw_oor = _parse_string_list(spec, name, "out_of_range_fields")
        _reject_identifiers(name, "out_of_range_fields", raw_oor)
        overlap = [f for f in raw_oor if f in raw_null]
        if overlap:
            raise ConfigError(
                f"quality.datasets.{name}: field(s) {overlap} appear in both "
                "null_fields and out_of_range_fields - a nulled value cannot "
                "be negated; choose one corruption per field (ADR-0023)"
            )

        raw_drop = _parse_string_list(spec, name, "drop_fields")
        _reject_identifiers(name, "drop_fields", raw_drop)
        raw_extra = _parse_string_list(spec, name, "extra_fields")
        both = [f for f in raw_extra if f in raw_drop]
        if both:
            raise ConfigError(
                f"quality.datasets.{name}: column(s) {both} appear in both "
                "drop_fields and extra_fields (ADR-0023)"
            )

        datasets[name] = QualityDatasetConfig(
            null_fields=tuple(raw_null),
            invalid_fk_fields=dict(raw_fk),
            out_of_range_fields=tuple(raw_oor),
            drop_fields=tuple(raw_drop),
            extra_fields=tuple(raw_extra),
        )
    return QualityConfig(
        null_rate=null_rate,
        datasets=datasets,
        duplicate_rate=duplicate_rate,
        invalid_fk_rate=invalid_fk_rate,
        out_of_range_rate=out_of_range_rate,
        drift_rate=drift_rate,
    )


def _log_resolved(config: PipelineConfig) -> None:
    """Log every resolved value (ADR-0009: the CONFIG stage contract)."""
    logger.info("[CONFIG] seed=%s", config.run.seed)
    logger.info("[CONFIG] reference_date=%s", config.generation.reference_date.isoformat())
    logger.info("[CONFIG] history_days=%s", config.generation.history_days)
    window = config.generation.window
    if window is None:
        logger.info("[CONFIG] window=none (single snapshot per entity)")
    else:
        logger.info("[CONFIG] window granularity=%s mode=%s",
                    window.granularity, window.mode)
    for dataset in config.datasets.values():
        logger.info("[CONFIG] dataset=%s records=%s", dataset.name, dataset.records)
    logger.info("[CONFIG] output.directory=%s", config.output.directory)
    logger.info("[CONFIG] output.format=%s", config.output.format)
    if config.quality is None:
        logger.info("[CONFIG] quality=clean (no quality section)")
    else:
        logger.info(
            "[CONFIG] quality null_rate=%s duplicate_rate=%s invalid_fk_rate=%s "
            "out_of_range_rate=%s drift_rate=%s",
            config.quality.null_rate, config.quality.duplicate_rate,
            config.quality.invalid_fk_rate, config.quality.out_of_range_rate,
            config.quality.drift_rate,
        )
        for name, spec in config.quality.datasets.items():
            logger.info(
                "[CONFIG] quality dataset=%s null=%s invalid_fk=%s "
                "out_of_range=%s drop=%s extra=%s",
                name, list(spec.null_fields), dict(spec.invalid_fk_fields),
                list(spec.out_of_range_fields), list(spec.drop_fields),
                list(spec.extra_fields),
            )
