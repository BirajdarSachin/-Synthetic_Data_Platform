"""Tests for configuration loading and the duration parser (ADR-0012)."""

from datetime import date

import pytest

from src.core.config import ConfigError, load_config, parse_duration_to_days

MINIMAL_YAML = """
run:
  seed: 42
generation:
  reference_date: 2026-01-15
datasets:
  customer:
    records: 10
output:
  directory: data/generated
  format: csv
"""

YAML_WITH_HISTORY = MINIMAL_YAML.replace(
    "  reference_date: 2026-01-15",
    '  reference_date: 2026-01-15\n  history: "18m"',
)


class TestParseDurationToDays:
    def test_years(self):
        assert parse_duration_to_days("3y") == 1095

    def test_ten_years(self):
        assert parse_duration_to_days("10y") == 3650

    def test_months(self):
        assert parse_duration_to_days("4m") == 120

    def test_days(self):
        assert parse_duration_to_days("30d") == 30

    def test_combined(self):
        assert parse_duration_to_days("1y6m") == 545

    def test_all_units(self):
        assert parse_duration_to_days("2y3m10d") == 830

    def test_case_and_spaces_tolerated(self):
        assert parse_duration_to_days(" 2Y ") == 730

    @pytest.mark.parametrize(
        "bad",
        ["abc", "3x", "", "y", "0d", "0y", "1y2y", "3y 4", 12, None],
    )
    def test_invalid_values_raise(self, bad):
        with pytest.raises(ConfigError):
            parse_duration_to_days(bad)


class TestLoadConfigHistory:
    def test_history_key_is_parsed(self, tmp_path):
        config_file = tmp_path / "pipeline.yaml"
        config_file.write_text(YAML_WITH_HISTORY, encoding="utf-8")
        config = load_config(config_file)
        assert config.generation.history_days == 540  # 18m -> 18*30

    def test_history_defaults_when_absent(self, tmp_path):
        config_file = tmp_path / "pipeline.yaml"
        config_file.write_text(MINIMAL_YAML, encoding="utf-8")
        config = load_config(config_file)
        # Default '3y' = 1095 days = pre-0.3.0 hardcoded behavior (ADR-0012).
        assert config.generation.history_days == 1095
