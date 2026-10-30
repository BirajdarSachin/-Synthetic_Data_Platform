"""Tests for topic compaction (Milestone 6d, ADR-031).

Unit tests cover the wire parser (envelope contract, contiguity, pk
coupling) and the latest-wins fold. Integration tests run CompactRunner
over synthetic 6c-shaped stream trees in tmp_path: two-window pipeline,
state verification (ok / null / mismatch), checkpoint and contiguity
audits, half-present trees, guards, idempotent re-runs, report shape.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.stream.compact import (
    CompactConfig,
    CompactEntityConfig,
    CompactRunner,
    compact_records,
    parse_log_record,
    read_log,
)
from src.core.stream.replay import StreamConfigError, StreamError

W1 = "2026-01-14"
W2 = "2026-01-15"


def env(offset, key, op, window, seq, value):
    """One 6c envelope, fields in the exact order 6c writes them."""
    return {
        "offset": offset,
        "key": key,
        "op": op,
        "entity": "order",
        "window": window,
        "seq": seq,
        "value": value,
    }


def write_log(root: Path, entity: str, envelopes: list[dict]) -> Path:
    """Write a 6c-shaped part-00000.jsonl + offsets.json for `entity`."""
    entity_dir = root / entity
    entity_dir.mkdir(parents=True, exist_ok=True)
    part = entity_dir / "part-00000.jsonl"
    lines = [json.dumps(e) for e in envelopes]
    part.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    windows: dict[str, dict[str, int]] = {}
    for e in envelopes:
        span = windows.setdefault(e["window"], {"first": e["offset"], "last": e["offset"]})
        span["last"] = max(span["last"], e["offset"])
    offsets = {
        "entity": entity,
        "events": len(envelopes),
        "last_offset": envelopes[-1]["offset"] if envelopes else 0,
        "windows": windows,
    }
    (entity_dir / "offsets.json").write_text(
        json.dumps(offsets) + "\n", encoding="utf-8"
    )
    return part


def write_snapshot(root: Path, entity: str, window: str, rows: list[dict]) -> Path:
    folder = root / entity / f"reference_date={window}"
    folder.mkdir(parents=True, exist_ok=True)
    header = list(rows[0].keys()) if rows else ["order_id"]
    lines = [",".join(header)]
    lines += [",".join(str(r[c]) for c in header) for r in rows]
    path = folder / f"{entity}_{window}.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_config(tmp_path: Path, entities: dict[str, str] | None = None, **opts) -> CompactConfig:
    return CompactConfig(
        entities={
            name: CompactEntityConfig(name, pk)
            for name, pk in (entities or {"order": "order_id"}).items()
        },
        stream_root=tmp_path / "streams",
        snapshot_root=tmp_path / "generated",
        output_root=tmp_path / "compacted",
        drop_tombstones=opts.get("drop_tombstones", False),
        verify_state=opts.get("verify_state", True),
    )


def quiet_log(tag: str, message: str) -> None:
    return None


def sample_envelopes() -> list[dict]:
    """Insert a,b,c @W1; update a, delete b, insert d @W2 (offsets 1..6)."""
    return [
        env(1, "a", "INSERT", W1, 1, {"order_id": "a", "status": "new"}),
        env(2, "b", "INSERT", W1, 2, {"order_id": "b", "status": "new"}),
        env(3, "c", "INSERT", W1, 3, {"order_id": "c", "status": "new"}),
        env(4, "a", "UPDATE", W2, 1, {"order_id": "a", "status": "paid"}),
        env(5, "b", "DELETE", W2, 2, None),
        env(6, "d", "INSERT", W2, 3, {"order_id": "d", "status": "new"}),
    ]


def source_line(e: dict) -> str:
    return json.dumps(e)


# -- parser units -----------------------------------------------------------------


def test_parse_accepts_valid_insert_and_delete():
    rec = parse_log_record(env(1, "a", "INSERT", W1, 1, {"order_id": "a"}), "f:1", "order", "order_id", 1)
    assert (rec.offset, rec.key, rec.op, rec.window, rec.seq) == (1, "a", "INSERT", W1, 1)
    assert rec.value == {"order_id": "a"}
    tomb = parse_log_record(env(2, "b", "DELETE", W1, 2, None), "f:2", "order", "order_id", 2)
    assert tomb.op == "DELETE" and tomb.value is None


def test_parse_rejects_non_object():
    with pytest.raises(StreamError, match="must be a JSON object"):
        parse_log_record([1, 2], "f:1", "order", "order_id", 1)


def test_parse_rejects_bad_op():
    with pytest.raises(StreamError, match="op must be one of"):
        parse_log_record(env(1, "a", "UPSERT", W1, 1, None), "f:1", "order", "order_id", 1)


def test_parse_rejects_offset_breaking_contiguity():
    with pytest.raises(StreamError, match="contiguity"):
        parse_log_record(env(7, "a", "INSERT", W1, 1, {"order_id": "a"}), "f:3", "order", "order_id", 3)


def test_parse_rejects_bad_offset_types():
    for bad in (0, True, "1", None):
        with pytest.raises(StreamError, match="offset must be a positive integer"):
            parse_log_record(env(1, "a", "INSERT", W1, 1, {"order_id": "a"}) | {"offset": bad}, "f:1", "order", "order_id", 1)


def test_parse_rejects_entity_mismatch():
    with pytest.raises(StreamError, match="misplaced file"):
        parse_log_record(env(1, "a", "INSERT", W1, 1, {"order_id": "a"}), "f:1", "customer", "order_id", 1)


def test_parse_rejects_bad_key_window_seq():
    base = env(1, "a", "INSERT", W1, 1, {"order_id": "a"})
    with pytest.raises(StreamError, match="key must be a non-empty string"):
        parse_log_record(base | {"key": ""}, "f:1", "order", "order_id", 1)
    with pytest.raises(StreamError, match="window must be a non-empty string"):
        parse_log_record(base | {"window": 7}, "f:1", "order", "order_id", 1)
    with pytest.raises(StreamError, match="seq must be a positive integer"):
        parse_log_record(base | {"seq": 0}, "f:1", "order", "order_id", 1)


def test_parse_rejects_delete_with_value():
    with pytest.raises(StreamError, match="value=null"):
        parse_log_record(env(1, "b", "DELETE", W1, 2, {"order_id": "b"}), "f:1", "order", "order_id", 1)


def test_parse_rejects_non_string_cells():
    with pytest.raises(StreamError, match="cells"):
        parse_log_record(env(1, "a", "INSERT", W1, 1, {"order_id": "a", "amount": 5}), "f:1", "order", "order_id", 1)


def test_parse_rejects_value_missing_pk_column():
    with pytest.raises(StreamError, match="lacks the primary_key column"):
        parse_log_record(env(1, "a", "INSERT", W1, 1, {"status": "new"}), "f:1", "order", "order_id", 1)


def test_parse_rejects_pk_value_mismatch():
    with pytest.raises(StreamError, match="does not match the row image"):
        parse_log_record(env(1, "a", "INSERT", W1, 1, {"order_id": "zz"}), "f:1", "order", "order_id", 1)


def test_read_log_rejects_blank_line(tmp_path):
    part = tmp_path / "part-00000.jsonl"
    part.write_text(source_line(env(1, "a", "INSERT", W1, 1, {"order_id": "a"})) + "\n\n", encoding="utf-8")
    with pytest.raises(StreamError, match="blank line"):
        read_log(part, "order", "order_id")


# -- fold units --------------------------------------------------------------------


def test_compact_latest_wins_and_offset_order():
    retained = compact_records([StreamRecordShim(e) for e in sample_envelopes()], drop_tombstones=False)
    assert [r.offset for r in retained] == [3, 4, 5, 6]
    assert [r.key for r in retained] == ["c", "a", "b", "d"]
    assert retained[1].value == {"order_id": "a", "status": "paid"}
    assert retained[2].op == "DELETE" and retained[2].value is None


def test_compact_drop_tombstones():
    retained = compact_records([StreamRecordShim(e) for e in sample_envelopes()], drop_tombstones=True)
    assert [r.offset for r in retained] == [3, 4, 6]
    assert all(r.op != "DELETE" for r in retained)


def test_compact_empty_log():
    assert compact_records([], drop_tombstones=False) == []


def test_retained_raw_is_byte_identical_to_source():
    envelopes = sample_envelopes()
    records = [StreamRecordShim(e) for e in envelopes]
    retained = compact_records(records, drop_tombstones=False)
    by_offset = {e["offset"]: source_line(e) for e in envelopes}
    for r in retained:
        assert r.raw == by_offset[r.offset]


class StreamRecordShim:
    """Builds a real StreamRecord from an envelope dict (test helper)."""

    def __new__(cls, e: dict):
        return parse_log_record(e, "f:{0}".format(e["offset"]), "order", "order_id", e["offset"])


# -- integration ---------------------------------------------------------------------


def seed_two_windows(tmp_path: Path) -> None:
    write_log(tmp_path / "streams", "order", sample_envelopes())
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


def test_two_window_pipeline_verify_ok_and_outputs_written(tmp_path):
    seed_two_windows(tmp_path)
    report = CompactRunner(make_config(tmp_path), quiet_log).run()
    entity = report["entities"]["order"]
    assert report["ok"] is True and entity["status"] == "ok"
    assert (entity["records"], entity["retained"], entity["live_keys"], entity["tombstones"]) == (6, 4, 3, 1)
    assert entity["state_check"]["match"] is True
    assert entity["state_check"]["rows_snapshot"] == 3
    part = tmp_path / "compacted" / "order" / "part-00000.jsonl"
    records = read_lines(part)
    assert [r["offset"] for r in records] == [3, 4, 5, 6]
    assert records[2]["value"] is None  # tombstone retained
    assert (tmp_path / "compacted" / "report.json").is_file()


def test_state_check_null_when_snapshot_missing(tmp_path):
    write_log(tmp_path / "streams", "order", sample_envelopes())
    report = CompactRunner(make_config(tmp_path), quiet_log).run()
    check = report["entities"]["order"]["state_check"]
    assert report["ok"] is True and check["match"] is None and check["rows_snapshot"] is None


def test_state_check_mismatch_samples_and_ok_false(tmp_path):
    seed_two_windows(tmp_path)
    write_snapshot(tmp_path / "generated", "order", W2, [
        {"order_id": "a", "status": "TAMPERED"},
    ])
    report = CompactRunner(make_config(tmp_path), quiet_log).run()
    entity = report["entities"]["order"]
    assert report["ok"] is False
    assert entity["state_check"]["match"] is False
    assert any("differing:a" in s for s in entity["samples"])
    assert any("extra:" in s or "missing:" in s for s in entity["samples"])


def test_checkpoint_mismatch_fails(tmp_path):
    seed_two_windows(tmp_path)
    offsets_path = tmp_path / "streams" / "order" / "offsets.json"
    payload = json.loads(offsets_path.read_text(encoding="utf-8"))
    payload["events"] = 5
    offsets_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(StreamError, match="re-run stream_main.py"):
        CompactRunner(make_config(tmp_path), quiet_log).run()


def test_non_contiguous_log_fails(tmp_path):
    seed_two_windows(tmp_path)
    part = tmp_path / "streams" / "order" / "part-00000.jsonl"
    lines = part.read_text(encoding="utf-8").splitlines()
    del lines[2]  # drop offset 3; later lines keep their offsets
    part.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    with pytest.raises(StreamError, match="contiguity"):
        CompactRunner(make_config(tmp_path), quiet_log).run()


def test_half_present_tree_fails(tmp_path):
    seed_two_windows(tmp_path)
    (tmp_path / "streams" / "order" / "offsets.json").unlink()
    with pytest.raises(StreamError, match="half-present"):
        CompactRunner(make_config(tmp_path), quiet_log).run()
    # and the reverse: offsets.json without the part file
    (tmp_path / "streams" / "order" / "offsets.json").write_text("{}", encoding="utf-8")
    (tmp_path / "streams" / "order" / "part-00000.jsonl").unlink()
    with pytest.raises(StreamError, match="half-present"):
        CompactRunner(make_config(tmp_path), quiet_log).run()


def test_entity_without_stream_dir_skipped(tmp_path):
    seed_two_windows(tmp_path)
    config = make_config(tmp_path, {"order": "order_id", "ghost": "ghost_id"})
    report = CompactRunner(config, quiet_log).run()
    assert report["ok"] is True
    assert report["entities"]["ghost"]["status"] == "skipped"


def test_missing_stream_root_fails(tmp_path):
    with pytest.raises(StreamError, match="run stream_main.py"):
        CompactRunner(make_config(tmp_path), quiet_log).run()


def test_containment_guard_rejects_overlap(tmp_path):
    seed_two_windows(tmp_path)
    config = CompactConfig(
        entities={"order": CompactEntityConfig("order", "order_id")},
        stream_root=tmp_path / "streams",
        snapshot_root=tmp_path / "generated",
        output_root=tmp_path / "streams" / "inside",
        drop_tombstones=False,
        verify_state=True,
    )
    with pytest.raises(StreamConfigError, match="overlaps"):
        CompactRunner(config, quiet_log).run()


def test_idempotent_rerun_byte_identical(tmp_path):
    seed_two_windows(tmp_path)
    runner = CompactRunner(make_config(tmp_path), quiet_log)
    runner.run()
    artifacts = [
        tmp_path / "compacted" / "order" / "part-00000.jsonl",
        tmp_path / "compacted" / "order" / "offsets.json",
        tmp_path / "compacted" / "report.json",
    ]
    before = [p.read_bytes() for p in artifacts]
    runner.run()
    assert [p.read_bytes() for p in artifacts] == before


def test_report_shape_echoes_options(tmp_path):
    seed_two_windows(tmp_path)
    report = CompactRunner(make_config(tmp_path, drop_tombstones=True), quiet_log).run()
    assert report["report_version"] == 1
    assert report["milestone"] == "6d"
    assert report["options"] == {"drop_tombstones": True, "verify_state": True}
    assert set(report["entities"]) == {"order"}


def test_unknown_entity_filter_fails(tmp_path):
    seed_two_windows(tmp_path)
    with pytest.raises(StreamConfigError, match="unknown entity"):
        CompactRunner(make_config(tmp_path), quiet_log).run(entity_filter=["ghost"])


def test_ambiguous_snapshot_fails(tmp_path):
    seed_two_windows(tmp_path)
    # a second snapshot file claiming the same entity@window: the flat
    # fallback location alongside the partitioned one
    flat = tmp_path / "generated" / "order" / f"order_{W2}.csv"
    flat.write_text("order_id,status\na,paid\n", encoding="utf-8")
    with pytest.raises(StreamError, match="ambiguous"):
        CompactRunner(make_config(tmp_path), quiet_log).run()


def test_verify_state_disabled_records_null(tmp_path):
    seed_two_windows(tmp_path)
    write_snapshot(tmp_path / "generated", "order", W2, [
        {"order_id": "a", "status": "TAMPERED"},
    ])
    report = CompactRunner(make_config(tmp_path, verify_state=False), quiet_log).run()
    check = report["entities"]["order"]["state_check"]
    assert report["ok"] is True and check["match"] is None
