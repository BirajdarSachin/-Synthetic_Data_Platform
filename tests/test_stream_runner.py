"""Integration tests for the stream runner (Milestone 6c, ADR-030).

Builds tiny synthetic journal + snapshot trees in tmp_path (the post-6b
shape: data/cdc journals, data/generated snapshots) and runs StreamRunner
end-to-end:
  * two-window replay verified cell-level + export written
  * tampered snapshot -> ok=False with sample mismatches
  * missing snapshot -> match=null (unverified), still ok
  * chain validation: gaps, missing initial, junk filenames
  * guards (tree containment), idempotent re-runs, --mode and
    --up-to-window semantics, empty delta journals
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.stream.replay import StreamError
from src.core.stream.runner import (
    StreamConfig,
    StreamEntityConfig,
    StreamRunner,
)

W1 = "2026-01-14"
W2 = "2026-01-15"
W3 = "2026-01-16"


def wire_event(op, pk, window, seq, prev_window=None, changed=None, before=None, after=None):
    return {"seq": seq, "op": op, "entity": "order", "pk": pk, "window": window,
            "prev_window": prev_window, "changed": changed or [], "before": before,
            "after": after}


def write_journal(root: Path, entity: str, name: str, events: list[dict]) -> Path:
    folder = root / entity / f"reference_date={name.split('__')[-1].removesuffix('.jsonl')}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return path


def write_snapshot(root: Path, entity: str, window: str, rows: list[dict]) -> Path:
    folder = root / entity / f"reference_date={window}"
    folder.mkdir(parents=True, exist_ok=True)
    header = list(rows[0].keys()) if rows else ["order_id"]
    lines = [",".join(header)]
    lines += [",".join(str(r[c]) for c in header) for r in rows]
    path = folder / f"{entity}_{window}.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_config(tmp_path: Path, entities: dict[str, str] | None = None, **opts) -> StreamConfig:
    return StreamConfig(
        entities={
            name: StreamEntityConfig(name, pk)
            for name, pk in (entities or {"order": "order_id"}).items()
        },
        journal_root=tmp_path / "cdc",
        snapshot_root=tmp_path / "generated",
        output_root=tmp_path / "streams",
        verify_replay=opts.get("verify_replay", True),
        byte_compare=opts.get("byte_compare", False),
    )


def quiet_log(tag: str, message: str) -> None:
    return None


def initial_chain() -> list[dict]:
    return [
        wire_event("INSERT", "a", W1, 1, after={"order_id": "a", "status": "new"}),
        wire_event("INSERT", "b", W1, 2, after={"order_id": "b", "status": "new"}),
        wire_event("INSERT", "c", W1, 3, after={"order_id": "c", "status": "new"}),
    ]


def delta_chain() -> list[dict]:
    return [
        wire_event("UPDATE", "a", W2, 1, prev_window=W1, changed=["status"],
                   before={"order_id": "a", "status": "new"},
                   after={"order_id": "a", "status": "paid"}),
        wire_event("DELETE", "b", W2, 2, prev_window=W1,
                   before={"order_id": "b", "status": "new"}, after=None),
        wire_event("INSERT", "d", W2, 3, prev_window=W1,
                   after={"order_id": "d", "status": "new"}),
    ]


def seed_two_windows(tmp_path: Path) -> None:
    write_journal(tmp_path / "cdc", "order", f"initial__{W1}.jsonl", initial_chain())
    write_journal(tmp_path / "cdc", "order", f"{W1}__{W2}.jsonl", delta_chain())
    write_snapshot(tmp_path / "generated", "order", W1, [
        {"order_id": "a", "status": "new"},
        {"order_id": "b", "status": "new"},
        {"order_id": "c", "status": "new"},
    ])
    write_snapshot(tmp_path / "generated", "order", W2, [
        {"order_id": "a", "status": "paid"},
        {"order_id": "c", "status": "new"},
        {"order_id": "d", "status": "new"},
    ])


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- the happy path ------------------------------------------------------------

def test_full_pipeline_replay_verified_and_export_written(tmp_path):
    seed_two_windows(tmp_path)
    result = StreamRunner(make_config(tmp_path), quiet_log).run()
    assert result.ok is True and result.events_total == 6
    order = result.per_entity["order"]
    assert order["chain"] == [W1, W2]
    assert order["events"] == {"INSERT": 4, "UPDATE": 1, "DELETE": 1, "total": 6}
    checks = order["replay_checks"]
    assert [c["window"] for c in checks] == [W1, W2]
    assert all(c["match"] is True for c in checks)
    assert [c["rows_replayed"] for c in checks] == [3, 3]

    part = read_lines(tmp_path / "streams" / "order" / "part-00000.jsonl")
    assert [r["offset"] for r in part] == [1, 2, 3, 4, 5, 6]
    assert [(r["key"], r["op"]) for r in part] == [
        ("a", "INSERT"), ("b", "INSERT"), ("c", "INSERT"),
        ("a", "UPDATE"), ("b", "DELETE"), ("d", "INSERT"),
    ]
    assert part[4]["value"] is None  # tombstone
    assert part[3]["value"] == {"order_id": "a", "status": "paid"}
    assert part[0]["window"] == W1 and part[3]["window"] == W2

    offsets = json.loads((tmp_path / "streams" / "order" / "offsets.json").read_text())
    assert offsets == {
        "entity": "order", "events": 6, "last_offset": 6,
        "windows": {W1: {"first": 1, "last": 3}, W2: {"first": 4, "last": 6}},
    }
    report = json.loads((tmp_path / "streams" / "report.json").read_text())
    assert report["ok"] is True and report["report_version"] == 1


def test_idempotent_rerun_is_byte_identical(tmp_path):
    seed_two_windows(tmp_path)
    runner = StreamRunner(make_config(tmp_path), quiet_log)
    runner.run()
    part_1 = (tmp_path / "streams" / "order" / "part-00000.jsonl").read_bytes()
    offsets_1 = (tmp_path / "streams" / "order" / "offsets.json").read_bytes()
    runner.run()
    assert (tmp_path / "streams" / "order" / "part-00000.jsonl").read_bytes() == part_1
    assert (tmp_path / "streams" / "order" / "offsets.json").read_bytes() == offsets_1


def test_up_to_window_truncates_chain_and_export(tmp_path):
    seed_two_windows(tmp_path)
    write_journal(tmp_path / "cdc", "order", f"{W2}__{W3}.jsonl", [
        wire_event("UPDATE", "a", W3, 1, prev_window=W2, changed=["status"],
                   before={"order_id": "a", "status": "paid"},
                   after={"order_id": "a", "status": "shipped"}),
        wire_event("DELETE", "c", W3, 2, prev_window=W2,
                   before={"order_id": "c", "status": "new"}, after=None),
        wire_event("DELETE", "d", W3, 3, prev_window=W2,
                   before={"order_id": "d", "status": "new"}, after=None),
    ])
    write_snapshot(tmp_path / "generated", "order", W3, [{"order_id": "a", "status": "shipped"}])
    result = StreamRunner(make_config(tmp_path), quiet_log).run(up_to_window=W2)
    order = result.per_entity["order"]
    assert order["chain"] == [W1, W2]
    assert [c["window"] for c in order["replay_checks"]] == [W1, W2]
    part = read_lines(tmp_path / "streams" / "order" / "part-00000.jsonl")
    assert all(r["window"] in (W1, W2) for r in part) and len(part) == 6
    assert result.ok is True


def test_empty_delta_journal_replays_unchanged_state(tmp_path):
    seed_two_windows(tmp_path)
    write_snapshot(tmp_path / "generated", "order", W3, [
        {"order_id": "a", "status": "paid"},
        {"order_id": "c", "status": "new"},
        {"order_id": "d", "status": "new"},
    ])
    write_journal(tmp_path / "cdc", "order", f"{W2}__{W3}.jsonl", [])
    result = StreamRunner(make_config(tmp_path), quiet_log).run()
    order = result.per_entity["order"]
    assert order["chain"] == [W1, W2, W3]
    assert order["events"]["total"] == 6  # W3 contributed zero events
    assert all(c["match"] is True for c in order["replay_checks"])
    assert result.ok is True


# -- verification failures -------------------------------------------------------

def test_tampered_snapshot_fails_with_samples(tmp_path):
    seed_two_windows(tmp_path)
    write_snapshot(tmp_path / "generated", "order", W2, [
        {"order_id": "a", "status": "TAMPERED"},
        {"order_id": "z", "status": "ghost"},
    ])
    result = StreamRunner(make_config(tmp_path), quiet_log).run()
    assert result.ok is False
    checks = {c["window"]: c for c in result.per_entity["order"]["replay_checks"]}
    assert checks[W1]["match"] is True
    assert checks[W2]["match"] is False
    assert checks[W2]["rows_replayed"] == 3 and checks[W2]["rows_snapshot"] == 2
    assert any("differ" in s for s in checks[W2]["mismatch_samples"])
    assert any("not in snapshot" in s or "not in replay" in s
               for s in checks[W2]["mismatch_samples"])


def test_missing_snapshot_records_unverified_but_ok(tmp_path):
    seed_two_windows(tmp_path)
    (tmp_path / "generated" / "order" / f"reference_date={W2}").mkdir(parents=True, exist_ok=True)
    (tmp_path / "generated" / "order" / f"reference_date={W2}" / f"order_{W2}.csv").unlink(missing_ok=True)
    result = StreamRunner(make_config(tmp_path), quiet_log).run()
    assert result.ok is True
    checks = result.per_entity["order"]["replay_checks"]
    assert checks[0]["match"] is True
    assert checks[1]["match"] is None and checks[1]["rows_snapshot"] is None


def test_duplicate_pk_in_snapshot_fails(tmp_path):
    seed_two_windows(tmp_path)
    folder = tmp_path / "generated" / "order" / f"reference_date={W2}"
    (folder / f"order_{W2}.csv").write_text(
        "order_id,status\na,paid\na,paid\n", encoding="utf-8"
    )
    with pytest.raises(StreamError, match="duplicate pk 'a'"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_flat_snapshot_fallback_found(tmp_path):
    seed_two_windows(tmp_path)
    flat_dir = tmp_path / "generated" / "order"
    for window in (W1, W2):
        shutil.rmtree(flat_dir / f"reference_date={window}")
    (flat_dir / f"order_{W1}.csv").write_text(
        "order_id,status\na,new\nb,new\nc,new\n", encoding="utf-8"
    )
    (flat_dir / f"order_{W2}.csv").write_text(
        "order_id,status\na,paid\nc,new\nd,new\n", encoding="utf-8"
    )
    result = StreamRunner(make_config(tmp_path), quiet_log).run()
    assert result.ok is True


# -- chain validation -------------------------------------------------------------

def test_chain_gap_fails_with_guidance(tmp_path):
    write_journal(tmp_path / "cdc", "order", f"initial__{W1}.jsonl", initial_chain())
    write_journal(tmp_path / "cdc", "order", f"{W2}__{W3}.jsonl", [
        wire_event("DELETE", "a", W3, 1, prev_window=W2,
                   before={"order_id": "a", "status": "new"}, after=None),
    ])
    with pytest.raises(StreamError, match="chain gap.*windows present"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_missing_initial_journal_fails(tmp_path):
    write_journal(tmp_path / "cdc", "order", f"{W1}__{W2}.jsonl", delta_chain())
    with pytest.raises(StreamError, match="no initial journal.*initial load"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_two_initial_journals_fail(tmp_path):
    write_journal(tmp_path / "cdc", "order", f"initial__{W1}.jsonl", initial_chain())
    write_journal(tmp_path / "cdc", "order", f"initial__{W2}.jsonl", initial_chain())
    with pytest.raises(StreamError, match="2 initial journals"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_unclassifiable_filename_fails(tmp_path):
    write_journal(tmp_path / "cdc", "order", f"initial__{W1}.jsonl", initial_chain())
    junk = tmp_path / "cdc" / "order" / f"reference_date={W1}" / "junk.jsonl"
    junk.write_text("", encoding="utf-8")
    with pytest.raises(StreamError, match="unclassifiable journal file junk.jsonl"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_malformed_partition_directory_fails(tmp_path):
    write_journal(tmp_path / "cdc", "order", f"initial__{W1}.jsonl", initial_chain())
    weird = tmp_path / "cdc" / "order" / "reference_date=junk"
    weird.mkdir()
    with pytest.raises(StreamError, match="malformed partition directory"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_partition_name_mismatch_fails(tmp_path):
    folder = tmp_path / "cdc" / "order" / f"reference_date={W3}"
    folder.mkdir(parents=True)
    (folder / f"initial__{W1}.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(StreamError, match="sits in partition"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


def test_entity_without_journals_is_skipped(tmp_path):
    seed_two_windows(tmp_path)
    result = StreamRunner(
        make_config(tmp_path, {"order": "order_id", "ghost": "ghost_id"}), quiet_log
    ).run()
    assert result.per_entity["ghost"]["status"] == "skipped"
    assert result.per_entity["order"]["status"] == "ok"
    assert result.ok is True


def test_missing_journal_root_fails(tmp_path):
    with pytest.raises(StreamError, match="journal root .* not found"):
        StreamRunner(make_config(tmp_path), quiet_log).run()


# -- guards, modes ------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["inside", "contains"])
def test_tree_containment_guard_both_directions(tmp_path, layout):
    seed_two_windows(tmp_path)
    config = make_config(tmp_path)
    if layout == "inside":
        config = StreamConfig(
            entities=config.entities,
            journal_root=config.journal_root,
            snapshot_root=config.snapshot_root,
            output_root=config.journal_root / "streams",
            verify_replay=True, byte_compare=False,
        )
    else:
        config = StreamConfig(
            entities=config.entities,
            journal_root=config.journal_root,
            snapshot_root=config.snapshot_root,
            output_root=tmp_path,
            verify_replay=True, byte_compare=False,
        )
    with pytest.raises(StreamError, match="overlaps"):
        StreamRunner(config, quiet_log).run()


def test_mode_replay_writes_no_stream_files(tmp_path):
    seed_two_windows(tmp_path)
    result = StreamRunner(make_config(tmp_path), quiet_log).run(mode="replay")
    assert result.ok is True
    assert not (tmp_path / "streams" / "order").exists()
    report = json.loads((tmp_path / "streams" / "report.json").read_text())
    assert report["mode"] == "replay"


def test_mode_export_skips_verification(tmp_path):
    seed_two_windows(tmp_path)
    write_snapshot(tmp_path / "generated", "order", W2, [
        {"order_id": "a", "status": "TAMPERED"},
    ])
    result = StreamRunner(make_config(tmp_path), quiet_log).run(mode="export")
    assert result.ok is True  # fold ran (contract checks), verification skipped
    assert (tmp_path / "streams" / "order" / "part-00000.jsonl").is_file()


def test_verify_replay_option_off_disables_verification(tmp_path):
    seed_two_windows(tmp_path)
    write_snapshot(tmp_path / "generated", "order", W2, [
        {"order_id": "a", "status": "TAMPERED"},
    ])
    result = StreamRunner(make_config(tmp_path, verify_replay=False), quiet_log).run()
    assert result.ok is True
    assert all(c["match"] is None for c in result.per_entity["order"]["replay_checks"])


def test_unknown_entity_filter_fails(tmp_path):
    with pytest.raises(StreamError, match="unknown entity"):
        StreamRunner(make_config(tmp_path), quiet_log).run(entity_filter=["nope"])
