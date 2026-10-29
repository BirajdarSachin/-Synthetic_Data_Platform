"""Integration tests for the CDC runner and config (Milestone 6b, ADR-029).

Builds tiny synthetic windowed output trees (orders + customers, snapshot
layout) in tmp_path and runs CdcRunner end-to-end:
  * initial load   (single window  -> every row is an INSERT)
  * delta runs     (two windows   -> exact INSERT/UPDATE/DELETE ledger)
  * fail-fasts     (missing windows, header drift, duplicate keys, guards)
Pins the atomic outputs too: JSONL bytes, report JSON, idempotent re-runs.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.core.cdc.differ import CdcConfigError
from src.core.cdc.events import OP_DELETE, OP_INSERT, OP_UPDATE
from src.core.cdc.runner import CdcConfig, CdcRunner

W1 = "2026-01-14"
W2 = "2026-01-15"
W3 = "2026-01-16"


def write_window(root: Path, entity: str, date: str, rows: list[dict]) -> None:
    """One snapshot window: {entity}/reference_date={date}/{entity}_{date}.csv"""
    folder = root / entity / f"reference_date={date}"
    folder.mkdir(parents=True, exist_ok=True)
    header = list(rows[0].keys()) if rows else ["order_id"]
    lines = [",".join(header)]
    lines += [",".join(str(r[c]) for c in header) for r in rows]
    (folder / f"{entity}_{date}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_config(tmp_path: Path, entities: dict | None = None, **overrides) -> CdcConfig:
    raw = {
        "cdc": {
            "source_tree": str(tmp_path / "generated"),
            "output_root": str(tmp_path / "cdc"),
            "options": {
                "before_images": True,
                "allow_schema_drift": False,
                "initial_load": "inserts",
            },
            "entities": entities
            if entities is not None
            else {
                "order": {"primary_key": "order_id"},
                "customer": {"primary_key": "customer_id"},
            },
        }
    }
    raw["cdc"].update(overrides)
    return CdcConfig.load(raw)


def make_runner(tmp_path: Path, entities: dict | None = None, **overrides) -> CdcRunner:
    logs: list[tuple[str, str]] = []
    runner = CdcRunner(
        make_config(tmp_path, entities=entities, **overrides),
        lambda t, m: logs.append((t, m)),
    )
    runner.logs = logs  # type: ignore[attr-defined]
    return runner


ORDER_ONLY = {"order": {"primary_key": "order_id"}}


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- config parsing -----------------------------------------------------------


def test_config_load_defaults_and_entities(tmp_path):
    config = make_config(tmp_path)
    assert set(config.entities) == {"order", "customer"}
    assert config.entities["order"].primary_key == "order_id"
    assert config.before_images is True
    assert config.allow_schema_drift is False
    assert config.initial_load == "inserts"


def test_config_missing_cdc_block_fails(tmp_path):
    with pytest.raises(CdcConfigError, match="top-level 'cdc:'"):
        CdcConfig.load({"dq": {}})


def test_config_entity_without_primary_key_fails(tmp_path):
    raw = {"cdc": {"entities": {"order": {}}}}
    with pytest.raises(CdcConfigError, match="order: primary_key is required"):
        CdcConfig.load(raw)


def test_config_bad_initial_load_policy_fails(tmp_path):
    raw = {"cdc": {"options": {"initial_load": "explode"}, "entities": {"order": {"primary_key": "order_id"}}}}
    with pytest.raises(CdcConfigError, match="initial_load"):
        CdcConfig.load(raw)


def test_config_columns_whitelist_parses(tmp_path):
    raw = {"cdc": {"entities": {"order": {"primary_key": "order_id", "columns": ["status"]}}}}
    config = CdcConfig.load(raw)
    assert config.entities["order"].columns == ("status",)


# -- initial load ---------------------------------------------------------------


def test_initial_load_single_window(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "o2", "status": "new"}, {"order_id": "o1", "status": "new"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W2)

    assert result.ok and result.events_total == 2
    ledger = result.per_entity["order"]
    assert ledger["initial_load"] is True and ledger["prev_window"] is None
    assert (ledger["inserts"], ledger["updates"], ledger["deletes"]) == (2, 0, 0)

    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"initial__{W2}.jsonl")
    assert [e["pk"] for e in events] == ["o1", "o2"]  # pk-sorted, not file order
    assert all(e["op"] == OP_INSERT and e["prev_window"] is None for e in events)
    assert [e["seq"] for e in events] == [1, 2]

    report = json.loads((tmp_path / "cdc" / "report.json").read_text(encoding="utf-8"))
    assert report["report_version"] == 1 and report["window"] == W2 and report["ok"] is True
    assert report["entities"]["order"]["ledger_identity_ok"] is True


def test_initial_load_policy_error_refuses(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "o1"}])
    runner = make_runner(
        tmp_path,
        entities=ORDER_ONLY,
        options={"initial_load": "error", "before_images": True, "allow_schema_drift": False},
    )
    with pytest.raises(CdcConfigError, match="initial_load=error"):
        runner.run(W2)


# -- delta runs -------------------------------------------------------------------


def test_delta_run_exact_ledger(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [
        {"order_id": "o-keep", "status": "new", "total": "10"},
        {"order_id": "o-gone", "status": "new", "total": "10"},
        {"order_id": "o-mut", "status": "new", "total": "10"},
    ])
    write_window(tree, "order", W2, [
        {"order_id": "o-keep", "status": "new", "total": "10"},
        {"order_id": "o-mut", "status": "paid", "total": "11"},
        {"order_id": "o-born", "status": "new", "total": "10"},
    ])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W2)  # auto-pair: latest earlier window = W1

    ledger = result.per_entity["order"]
    assert ledger["prev_window"] == W1
    assert (ledger["inserts"], ledger["updates"], ledger["deletes"], ledger["unchanged"]) == (1, 1, 1, 1)
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl")
    by_pk = {e["pk"]: e for e in events}
    assert by_pk["o-born"]["op"] == OP_INSERT
    assert by_pk["o-gone"]["op"] == OP_DELETE and by_pk["o-gone"]["after"] is None
    assert by_pk["o-mut"]["op"] == OP_UPDATE
    assert by_pk["o-mut"]["changed"] == ["status", "total"]
    assert by_pk["o-mut"]["before"]["status"] == "new" and by_pk["o-mut"]["after"]["status"] == "paid"
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_auto_prev_picks_latest_earlier_window(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}])
    write_window(tree, "order", W3, [{"order_id": "a", "status": "shipped"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W3)
    ledger = result.per_entity["order"]
    assert ledger["prev_window"] == W2
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W3}" / f"{W2}__{W3}.jsonl")
    assert events[0]["changed"] == ["status"]


def test_explicit_prev_window_wins(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}])
    write_window(tree, "order", W3, [{"order_id": "a", "status": "shipped"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    result = runner.run(W3, prev_window=W1)
    assert result.per_entity["order"]["prev_window"] == W1


# -- fail-fasts ---------------------------------------------------------------------


def test_missing_current_window_lists_available(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match=W1):
        runner.run(W2)


def test_prev_equal_or_newer_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W2, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="strictly EARLIER"):
        runner.run(W2, prev_window=W2)
    with pytest.raises(CdcConfigError, match="strictly earlier"):
        runner.run(W1, prev_window=W2)


def test_missing_prev_window_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W3, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="previous window"):
        runner.run(W3, prev_window=W2)


def test_header_drift_blocked_by_default(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "new", "bonus_col": "x"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="schema drift"):
        runner.run(W2)


def test_header_drift_allowed_diffs_union(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "new", "bonus_col": "x"}])
    runner = make_runner(
        tmp_path,
        entities=ORDER_ONLY,
        options={"before_images": True, "allow_schema_drift": True, "initial_load": "inserts"},
    )
    result = runner.run(W2)
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl")
    assert events[0]["op"] == OP_UPDATE
    assert events[0]["changed"] == ["bonus_col"]
    assert events[0]["before"] == {"order_id": "a", "status": "new", "bonus_col": ""}
    assert events[0]["after"]["bonus_col"] == "x"


def test_configured_column_not_in_header_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "a"}])
    raw = {
        "cdc": {
            "source_tree": str(tree),
            "output_root": str(tmp_path / "cdc"),
            "entities": {"order": {"primary_key": "order_id", "columns": ["nope"]}},
        }
    }
    runner = CdcRunner(CdcConfig.load(raw), lambda t, m: None)
    with pytest.raises(CdcConfigError, match="nope"):
        runner.run(W2)


# -- guards ---------------------------------------------------------------------------


def test_output_inside_source_tree_refused(tmp_path):
    (tmp_path / "generated").mkdir()
    raw = {
        "cdc": {
            "source_tree": str(tmp_path / "generated"),
            "output_root": str(tmp_path / "generated" / "cdc"),
            "entities": {"order": {"primary_key": "order_id"}},
        }
    }
    with pytest.raises(CdcConfigError, match="inside"):
        CdcRunner(CdcConfig.load(raw), lambda t, m: None)


def test_source_inside_output_refused(tmp_path):
    (tmp_path / "cdc").mkdir()
    raw = {
        "cdc": {
            "source_tree": str(tmp_path / "cdc" / "generated"),
            "output_root": str(tmp_path / "cdc"),
            "entities": {"order": {"primary_key": "order_id"}},
        }
    }
    with pytest.raises(CdcConfigError, match="wrap"):
        CdcRunner(CdcConfig.load(raw), lambda t, m: None)


def test_duplicate_pk_in_window_fails_via_runner(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a"}])
    write_window(tree, "order", W2, [{"order_id": "a"}, {"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="duplicate primary key 'a'"):
        runner.run(W2)


def test_row_longer_than_header_fails(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "a"}])
    target = tree / "order" / f"reference_date={W2}" / f"order_{W2}.csv"
    target.write_text("order_id\na,extra\n", encoding="utf-8")
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    with pytest.raises(CdcConfigError, match="header declares"):
        runner.run(W2)


# -- before_images off + idempotency ---------------------------------------------------


def test_before_images_off_strips_before_keeps_changed(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}])
    runner = make_runner(
        tmp_path,
        entities=ORDER_ONLY,
        options={"before_images": False, "allow_schema_drift": False, "initial_load": "inserts"},
    )
    runner.run(W2)
    events = read_events(tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl")
    assert events[0]["before"] is None
    assert events[0]["changed"] == ["status"] and events[0]["after"]["status"] == "paid"


def test_rerun_is_byte_identical(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W1, [{"order_id": "a", "status": "new"}])
    write_window(tree, "order", W2, [{"order_id": "a", "status": "paid"}, {"order_id": "b", "status": "new"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    runner.run(W2)
    events_path = tmp_path / "cdc" / "order" / f"reference_date={W2}" / f"{W1}__{W2}.jsonl"
    report_path = tmp_path / "cdc" / "report.json"
    first_events, first_report = events_path.read_bytes(), report_path.read_bytes()
    runner.run(W2)
    assert events_path.read_bytes() == first_events
    assert report_path.read_bytes() == first_report


def test_log_lines_carry_cdc_tag(tmp_path):
    tree = tmp_path / "generated"
    write_window(tree, "order", W2, [{"order_id": "a"}])
    runner = make_runner(tmp_path, entities=ORDER_ONLY)
    runner.run(W2)
    tags = {t for t, _ in runner.logs}  # type: ignore[attr-defined]
    assert "CDC" in tags
    assert any("initial load" in m for _, m in runner.logs)  # type: ignore[attr-defined]
