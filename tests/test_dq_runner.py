"""Integration tests for the DQ runner and config (Milestones 7a + 7b, ADR-025/026).

Builds tiny synthetic output trees (customers + orders, snapshot layout) in
tmp_path and runs DqRunner end-to-end in both modes:
  clean   tree  -> ok=True
  corrupt tree  -> ok=False in clean mode, ok=True in corrupt mode
Mirrors docs/impurity_guide.md section 11 (the acceptance drill) at unit scale.
7b additions: duplicate_rows / row_count / pk_union wiring and per-entity
check gates.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.core.dq.checks import DqConfigError
from src.core.dq.runner import DqConfig, DqRunner, write_report

DATE = "2026-10-24"
DATE2 = "2026-10-25"


def write_window(root: Path, entity: str, date: str, rows_data: list[dict]) -> None:
    """Lay out one snapshot window: {entity}/reference_date={date}/{entity}_{date}.csv"""
    folder = root / entity / f"reference_date={date}"
    folder.mkdir(parents=True, exist_ok=True)
    header = list(rows_data[0].keys())
    lines = [",".join(header)]
    lines += [",".join(str(row[c]) for c in header) for row in rows_data]
    (folder / f"{entity}_{date}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_tree(root: Path, orders_rows: list[dict], customers_rows: list[dict] | None = None):
    """Lay out a snapshot tree: {entity}/reference_date={date}/{entity}_{date}.csv"""
    if customers_rows is None:
        customers_rows = [{"customer_id": str(i), "name": f"c{i}", "registration_date": DATE}
                          for i in range(1, 4)]
    for entity, rows_data in (("customers", customers_rows), ("orders", orders_rows)):
        write_window(root, entity, DATE, rows_data)
    return root


def base_config() -> dict:
    return {
        "dq": {
            "entities": {
                "customers": {
                    "primary_key": "customer_id",
                    "required": ["customer_id", "name"],
                },
                "orders": {
                    "primary_key": "order_id",
                    "required": ["order_id", "customer_id", "total_amount"],
                    "foreign_keys": {"customer_id": "customers"},
                    "ranges": {"quantity": {"min": 1, "max": 100, "type": "int"}},
                },
            }
        }
    }


def quiet_runner(tree: Path, config: DqConfig, mode: str) -> DqRunner:
    return DqRunner(tree, config, mode, log=lambda tag, msg: None)


def clean_orders() -> list[dict]:
    return [
        {"order_id": str(i), "customer_id": "1", "order_date": DATE,
         "total_amount": f"{10 * i}.50", "quantity": str(i)}
        for i in range(1, 6)
    ]


# ------------------------------------------------------------------ config


class TestDqConfig:
    def test_loads_valid_config(self):
        config = DqConfig.load(base_config())
        assert set(config.entities) == {"customers", "orders"}
        assert all(config.enabled.values())  # absent toggles default to enabled (7 names)

    def test_missing_dq_block_is_config_error(self):
        with pytest.raises(DqConfigError, match="'dq:'"):
            DqConfig.load({})

    def test_no_entities_is_config_error(self):
        with pytest.raises(DqConfigError, match="entities"):
            DqConfig.load({"dq": {"entities": {}}})

    def test_missing_primary_key_is_config_error(self):
        raw = {"dq": {"entities": {"orders": {"required": ["a"]}}}}
        with pytest.raises(DqConfigError, match="primary_key"):
            DqConfig.load(raw)

    def test_unknown_check_toggle_is_config_error(self):
        raw = base_config()
        raw["dq"]["checks"] = {"not_a_check": {"enabled": True}}
        with pytest.raises(DqConfigError, match="unknown check"):
            DqConfig.load(raw)

    def test_all_disabled_is_config_error(self):
        raw = base_config()
        raw["dq"]["checks"] = {
            name: {"enabled": False}
            for name in ("not_null", "fk_integrity", "value_in_range", "pk_unique",
                         "duplicate_rows", "row_count", "pk_union")
        }
        with pytest.raises(DqConfigError, match="all checks are disabled"):
            DqConfig.load(raw)

    def test_row_count_spec_parsed(self):
        raw = base_config()
        raw["dq"]["entities"]["orders"]["row_count"] = {"expected": 5}
        config = DqConfig.load(raw)
        assert config.entities["orders"].row_count is not None
        assert config.entities["orders"].row_count.expected == 5

    def test_row_count_absent_is_none(self):
        config = DqConfig.load(base_config())
        assert config.entities["orders"].row_count is None

    def test_pk_union_flag_parsed(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["pk_union"] = True
        config = DqConfig.load(raw)
        assert config.entities["customers"].pk_union is True

    def test_pk_union_non_bool_is_config_error(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["pk_union"] = "yes"
        with pytest.raises(DqConfigError, match="pk_union must be true or false"):
            DqConfig.load(raw)

    def test_whitelist_parsed(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = ["pk_unique", "duplicate_rows"]
        config = DqConfig.load(raw)
        assert config.entities["customers"].checks_whitelist == ("pk_unique", "duplicate_rows")

    def test_whitelist_unknown_name_is_config_error(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = ["nope"]
        with pytest.raises(DqConfigError, match="unknown check name.*whitelist"):
            DqConfig.load(raw)

    def test_empty_whitelist_is_config_error(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = []
        with pytest.raises(DqConfigError, match="whitelist is empty"):
            DqConfig.load(raw)

    def test_whitelist_must_be_list(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = "pk_unique"
        with pytest.raises(DqConfigError, match="must be a list"):
            DqConfig.load(raw)

    def test_yaml_roundtrip(self):
        config = DqConfig.load(yaml.safe_load(yaml.safe_dump(base_config())))
        assert config.entities["orders"].primary_key == "order_id"


# ------------------------------------------------------------------ runner


class TestRunnerCleanTree:
    def test_clean_tree_passes_in_clean_mode(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True
        assert report.violations_total == 0
        assert report.files_checked == 2
        assert report.rows_checked == 8  # 3 customers + 5 orders

    def test_clean_tree_fails_corrupt_expectation(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "corrupt").run()
        assert report.ok is False  # no dirt found, but dirt was required


class TestRunnerCorruptTree:
    def corrupt_orders(self) -> list[dict]:
        rows_data = clean_orders()
        rows_data[1]["customer_id"] = "999"         # invalid_fk: reserved band > parent max
        rows_data[2]["quantity"] = "-3"             # out_of_range
        rows_data.append(dict(rows_data[3]))        # duplicate: exact copy incl. PK
        return rows_data

    def test_corrupt_tree_caught_in_clean_mode(self, tmp_path):
        tree = make_tree(tmp_path, self.corrupt_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is False
        fired = {r.check for r in report.results if not r.passed}
        # 7b: the exact-copy duplicate now fires BOTH pk_unique and
        # duplicate_rows (Law 4: duplicates are exact copies).
        assert fired == {"fk_integrity", "value_in_range", "pk_unique", "duplicate_rows"}

    def test_corrupt_tree_passes_in_corrupt_mode(self, tmp_path):
        tree = make_tree(tmp_path, self.corrupt_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "corrupt").run()
        assert report.ok is True

    def test_each_rule_caught_individually(self, tmp_path):
        """One corruption at a time -- each impurity rule has its own catcher(s)."""
        config = DqConfig.load(base_config())
        cases = [
            ("invalid_fk", lambda r: r.update(customer_id="999"), {"fk_integrity"}),
            ("out_of_range", lambda r: r.update(quantity="0"), {"value_in_range"}),
            # 7b: the duplicate is an exact copy -> two catchers fire (Law 4)
            ("duplicate", lambda r: None, {"pk_unique", "duplicate_rows"}),
        ]
        for name, mutate, expected_checks in cases:
            rows_data = clean_orders()
            if name == "duplicate":
                rows_data.append(dict(rows_data[0]))
            else:
                mutate(rows_data[0])
            tree = make_tree(tmp_path / name, rows_data)
            report = quiet_runner(tree, config, "clean").run()
            fired = {r.check for r in report.results if not r.passed}
            assert fired == expected_checks, f"{name} should fire {expected_checks} only"

    def test_duplicate_only_dirt_fires_both_catchers_once_each(self, tmp_path):
        """1 exact copy -> pk_unique 1 + duplicate_rows 1, nothing else."""
        rows_data = clean_orders()
        rows_data.append(dict(rows_data[0]))
        tree = make_tree(tmp_path, rows_data)
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        counts = {(r.check, r.file): r.violation_count for r in report.results if r.violation_count}
        assert counts == {("pk_unique", "orders/reference_date=2026-10-24/orders_2026-10-24.csv"): 1,
                          ("duplicate_rows", "orders/reference_date=2026-10-24/orders_2026-10-24.csv"): 1}


class TestDiscovery:
    """File discovery must be layout-tolerant AND self-diagnosing when empty."""

    def test_empty_tree_diagnostic_shows_contents(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tmp_path, config, "clean").run()
        msg = str(excinfo.value)
        assert "no CSV files found for entity 'customers'" in msg
        assert "(tree exists but is EMPTY)" in msg
        assert "generate the data first" in msg

    def test_wrong_entity_name_diagnostic_lists_actual_dirs(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["ghosts"] = {"primary_key": "ghost_id"}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tree, config, "clean").run()
        msg = str(excinfo.value)
        assert "no CSV files found for entity 'ghosts'" in msg
        assert "customers/" in msg and "orders/" in msg  # the actual tree contents

    def test_partition_first_layout_found_by_stem(self, tmp_path):
        """No entity dirs at all: {partition}/{entity}_{date}.csv must be found."""
        folder = tmp_path / f"reference_date={DATE}"
        folder.mkdir()
        customers = [{"customer_id": "1", "name": "c1", "registration_date": DATE}]
        header = ",".join(customers[0])
        (folder / f"customers_{DATE}.csv").write_text(
            header + "\n" + ",".join(customers[0].values()) + "\n", encoding="utf-8"
        )
        header_o = ",".join(clean_orders()[0])
        rows_o = "\n".join(",".join(r.values()) for r in clean_orders())
        (folder / f"orders_{DATE}.csv").write_text(
            header_o + "\n" + rows_o + "\n", encoding="utf-8"
        )
        config = DqConfig.load(base_config())
        report = quiet_runner(tmp_path, config, "clean").run()
        assert report.ok is True
        assert report.files_checked == 2

    def test_prefix_collision_guarded(self, tmp_path):
        """Entity 'orders' must NOT sweep in order_items_*.csv (digit guard)."""
        folder = tmp_path / f"reference_date={DATE}"
        folder.mkdir()
        (folder / f"order_items_{DATE}.csv").write_text(
            "order_item_id,order_id\n1,1\n", encoding="utf-8"
        )
        raw = {"dq": {"entities": {"orders": {"primary_key": "order_id",
                                             "required": ["order_id"]}}}}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="no CSV files found for entity 'orders'"):
            quiet_runner(tmp_path, config, "clean").run()

    def test_entity_dir_takes_precedence_over_stray_stem(self, tmp_path):
        """If {tree}/{entity}/ exists and has CSVs, strays elsewhere are ignored."""
        tree = make_tree(tmp_path, clean_orders())
        archive = tree / "archive"
        archive.mkdir()
        (archive / "customers_2020-01-01.csv").write_text(
            "customer_id,name\n9,old\n", encoding="utf-8"
        )
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True
        assert report.files_checked == 2  # archive strays not swept in


class TestHeaderValidation:
    """Every configured column must exist in the real CSV header (fail-fast).

    A wrong parent pk must raise at load_parent_keys time -- silently loading
    an empty key set would flag every FK row as a violation instead.
    """

    def test_wrong_pk_prints_actual_header(self, tmp_path):
        # raises inside load_parent_keys: orders' FK points at customers,
        # whose (wrong) pk is validated before any key is loaded
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["customers"]["primary_key"] = "cust_id"
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError) as excinfo:
            quiet_runner(tree, config, "clean").run()
        msg = str(excinfo.value)
        assert "customers" in msg
        assert "actual header" in msg
        assert "primary_key='cust_id'" in msg   # the wrong config column...
        assert "customer_id" in msg             # ...and the real header printed

    def test_wrong_required_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["required"] = ["order_id", "amount"]
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="required='amount'"):
            quiet_runner(tree, config, "clean").run()

    def test_wrong_fk_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["foreign_keys"] = {"cust_id": "customers"}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="foreign_keys='cust_id'"):
            quiet_runner(tree, config, "clean").run()

    def test_wrong_range_column_caught(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["ranges"] = {"qty": {"min": 1, "max": 5, "type": "int"}}
        config = DqConfig.load(raw)
        with pytest.raises(DqConfigError, match="ranges='qty'"):
            quiet_runner(tree, config, "clean").run()


# ========================================================== 7b: new checks ===


class TestRowCountWiring:
    def test_expected_fires_on_corrupt_tree(self, tmp_path):
        """duplicate adds a row -> row_count fires IN ADDITION to pk_unique."""
        rows_data = clean_orders()
        rows_data.append(dict(rows_data[0]))
        tree = make_tree(tmp_path, rows_data)
        raw = base_config()
        raw["dq"]["entities"]["orders"]["row_count"] = {"expected": 5}
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        fired = {r.check for r in report.results if not r.passed}
        assert {"pk_unique", "duplicate_rows", "row_count"} <= fired

    def test_expected_passes_on_clean_tree(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["orders"]["row_count"] = {"expected": 5}
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        assert report.ok is True

    def test_row_count_disabled_globally(self, tmp_path):
        rows_data = clean_orders()
        rows_data.append(dict(rows_data[0]))
        tree = make_tree(tmp_path, rows_data)
        raw = base_config()
        raw["dq"]["entities"]["orders"]["row_count"] = {"expected": 5}
        raw["dq"]["checks"] = {"row_count": {"enabled": False}}
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        assert "row_count" not in {r.check for r in report.results}

    def test_bounds_form_fires_on_growth(self, tmp_path):
        """max bound catches duplicate growth without knowing exact counts."""
        rows_data = clean_orders()
        rows_data.append(dict(rows_data[0]))
        tree = make_tree(tmp_path, rows_data)
        raw = base_config()
        raw["dq"]["entities"]["orders"]["row_count"] = {"max": 5}
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        fired = {r.check for r in report.results if not r.passed}
        assert "row_count" in fired

    def test_expected_plus_min_is_config_error(self):
        raw = base_config()
        raw["dq"]["entities"]["orders"]["row_count"] = {"expected": 5, "min": 1}
        with pytest.raises(DqConfigError, match="use 'expected' alone"):
            DqConfig.load(raw)


class TestPkUnionWiring:
    def two_window_tree(self, tmp_path: Path) -> Path:
        tree = make_tree(tmp_path, clean_orders())  # window 1 (customers 1..3)
        window2 = [{"customer_id": "1", "name": "c1", "registration_date": DATE2},  # overlap!
                   {"customer_id": "4", "name": "c4", "registration_date": DATE2}]
        write_window(tree, "customers", DATE2, window2)
        return tree

    def test_fires_across_windows(self, tmp_path):
        tree = self.two_window_tree(tmp_path)
        raw = base_config()
        raw["dq"]["entities"]["customers"]["pk_union"] = True
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        union = [r for r in report.results if r.check == "pk_union"]
        assert len(union) == 1
        assert union[0].file == "(cross-file union)"
        assert union[0].violation_count == 1  # customer 1 in both windows
        assert "reference_date=2026-10-24" in union[0].samples[0].reason
        assert "reference_date=2026-10-25" in union[0].samples[0].reason

    def test_skipped_single_window(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())  # one window only
        raw = base_config()
        raw["dq"]["entities"]["customers"]["pk_union"] = True
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        assert [r for r in report.results if r.check == "pk_union"] == []

    def test_off_by_default(self, tmp_path):
        """No opt-in -> overlapping windows stay silent (SCD is legitimate)."""
        tree = self.two_window_tree(tmp_path)
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        assert [r for r in report.results if r.check == "pk_union"] == []
        assert report.ok is True

    def test_global_toggle_off(self, tmp_path):
        tree = self.two_window_tree(tmp_path)
        raw = base_config()
        raw["dq"]["entities"]["customers"]["pk_union"] = True
        raw["dq"]["checks"] = {"pk_union": {"enabled": False}}
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        assert [r for r in report.results if r.check == "pk_union"] == []

    def test_report_rows_not_inflated_by_union(self, tmp_path):
        """pk_union is entity-level: file/row totals must stay per-file exact."""
        tree = self.two_window_tree(tmp_path)  # 3+2 customers, 5 orders
        raw = base_config()
        raw["dq"]["entities"]["customers"]["pk_union"] = True
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        assert report.files_checked == 3
        assert report.rows_checked == 10


class TestPerEntityGates:
    def test_whitelist_limits_entity_checks(self, tmp_path):
        """customers whitelisted to duplicate_rows -> no not_null for it."""
        rows_data = clean_orders()
        rows_data[0]["total_amount"] = ""  # orders dirt (not_null would fire)
        tree = make_tree(tmp_path, rows_data)
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = ["duplicate_rows"]
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        customer_checks = {r.check for r in report.results if r.entity == "customers"}
        assert customer_checks == {"duplicate_rows"}
        # orders unaffected: not_null still fired there
        order_checks = {r.check for r in report.results if r.entity == "orders"}
        assert "not_null" in order_checks

    def test_whitelist_can_silence_a_clean_entity(self, tmp_path):
        tree = make_tree(tmp_path, clean_orders())
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = ["pk_unique"]
        config = DqConfig.load(raw)
        report = quiet_runner(tree, config, "clean").run()
        customer_results = [r for r in report.results if r.entity == "customers"]
        assert {r.check for r in customer_results} == {"pk_unique"}

    def test_unknown_whitelist_name_is_config_error(self):
        raw = base_config()
        raw["dq"]["entities"]["customers"]["checks"] = ["ghost_check"]
        with pytest.raises(DqConfigError, match="known checks"):
            DqConfig.load(raw)


class TestRunnerEdgeCases:
    def test_missing_entity_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="no CSV files found for entity 'customers'"):
            quiet_runner(tmp_path, config, "clean").run()

    def test_missing_tree_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="output tree not found"):
            quiet_runner(tmp_path / "nope", config, "clean")

    def test_bad_mode_is_config_error(self, tmp_path):
        config = DqConfig.load(base_config())
        with pytest.raises(DqConfigError, match="mode"):
            quiet_runner(tmp_path, config, "paranoid")

    def test_fk_parent_without_dq_entry_is_config_error(self, tmp_path):
        raw = base_config()
        raw["dq"]["entities"]["orders"]["foreign_keys"] = {"customer_id": "ghosts"}
        config = DqConfig.load(raw)
        tree = make_tree(tmp_path, clean_orders())
        with pytest.raises(DqConfigError, match="no dq.entities entry"):
            quiet_runner(tree, config, "clean").run()

    def test_report_written_atomically_and_parseable(self, tmp_path):
        tree = make_tree(tmp_path / "tree", clean_orders())
        config = DqConfig.load(base_config())
        report = quiet_runner(tree, config, "clean").run()
        target = tmp_path / "out" / "report.json"
        write_report(report, target, log=lambda tag, msg: None)
        assert not target.with_name("report.json.tmp").exists()  # tmp replaced away
        loaded = __import__("json").loads(target.read_text(encoding="utf-8"))
        assert loaded["ok"] is True
        assert loaded["report_version"] == 1
        assert loaded["results"][0]["file"].startswith("customers/")
