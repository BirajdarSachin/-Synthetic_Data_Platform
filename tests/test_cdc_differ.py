"""Unit tests for the CDC differ (Milestone 6b, ADR-029 decision 1/2).

The differ is pure: rows in, CdcDiff out. These tests pin the event ledger
semantics (INSERT/UPDATE/DELETE/unchanged), the determinism contract
(pk-ordered, seq 1..N), the snapshot-validity fail-fasts (duplicate/empty
primary keys), and the JSONL wire format.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.cdc.differ import CdcConfigError, diff_windows
from src.core.cdc.events import OP_DELETE, OP_INSERT, OP_UPDATE

W1 = "2026-01-14"
W2 = "2026-01-15"


def row(pk: str, **extra) -> dict:
    base = {"order_id": pk, "status": "new", "total": "10"}
    base.update(extra)
    return base


def ops(diff):
    return [e.op for e in diff.events]


def pks(diff):
    return [e.pk for e in diff.events]


# -- ledger semantics -------------------------------------------------------


def test_initial_load_every_row_is_insert():
    diff = diff_windows(None, [row("a"), row("b")], entity="order", primary_key="order_id", window=W2)
    assert diff.inserts == 2 and diff.updates == 0 and diff.deletes == 0
    assert diff.rows_prev == 0 and diff.rows_curr == 2
    assert ops(diff) == [OP_INSERT, OP_INSERT]
    for e in diff.events:
        assert e.before is None and e.after is not None
        assert e.prev_window is None and e.window == W2


def test_pure_inserts_between_windows():
    diff = diff_windows([row("a")], [row("a"), row("b")], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert ops(diff) == [OP_INSERT]
    assert diff.unchanged == 1


def test_pure_deletes_carry_before_image_only():
    diff = diff_windows([row("a"), row("b")], [row("a")], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert ops(diff) == [OP_DELETE]
    assert diff.deletes == 1
    e = diff.events[0]
    assert e.before == row("b") and e.after is None


def test_pure_updates_carry_both_images_and_changed():
    diff = diff_windows(
        [row("a"), row("x", status="old")],
        [row("a"), row("x", status="new")],
        entity="order", primary_key="order_id", prev_window=W1, window=W2,
    )
    assert ops(diff) == [OP_UPDATE]
    e = diff.events[0]
    assert e.changed == ("status",)
    assert e.before["status"] == "old" and e.after["status"] == "new"


def test_mixed_ledger_exact_event_set():
    prev = [row("keep"), row("gone"), row("mut", status="old")]
    curr = [row("keep"), row("born"), row("mut", status="new")]
    diff = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert {e.pk: e.op for e in diff.events} == {
        "born": OP_INSERT, "gone": OP_DELETE, "mut": OP_UPDATE,
    }
    assert diff.unchanged == 1  # 'keep'
    assert (diff.inserts, diff.updates, diff.deletes) == (1, 1, 1)


def test_unchanged_rows_never_emit_events():
    diff = diff_windows([row("a"), row("b")], [row("a"), row("b")], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert diff.events == () and diff.unchanged == 2
    assert diff.inserts == diff.updates == diff.deletes == 0


def test_full_row_image_covers_every_column():
    prev = [{"order_id": "a", "status": "new", "total": "10", "extra": "x"}]
    curr = [{"order_id": "a", "status": "new", "total": "11"}]
    diff = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    e = diff.events[0]
    assert e.changed == ("extra", "total")  # drift-union: extra vanished -> changed
    assert e.before == prev[0] and e.after == curr[0]


# -- determinism --------------------------------------------------------------


def test_events_pk_ordered_and_seq_contiguous():
    curr = [row(k) for k in ("m", "a", "z", "b")]
    diff = diff_windows(None, curr, entity="order", primary_key="order_id", window=W2)
    assert pks(diff) == ["a", "b", "m", "z"]
    assert [e.seq for e in diff.events] == [1, 2, 3, 4]


def test_same_windows_diff_twice_is_identical():
    prev = [row("keep"), row("mut", status="old")]
    curr = [row("keep"), row("mut", status="new"), row("new")]
    one = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    two = diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)
    assert [e.to_jsonl() for e in one.events] == [e.to_jsonl() for e in two.events]


# -- snapshot validity ---------------------------------------------------------


@pytest.mark.parametrize("side", ["prev", "curr"])
def test_duplicate_primary_key_fails_fast(side):
    prev = [row("a"), row("a")] if side == "prev" else [row("a")]
    curr = [row("a")] if side == "prev" else [row("a"), row("a")]
    with pytest.raises(CdcConfigError, match="duplicate primary key 'a'"):
        diff_windows(prev, curr, entity="order", primary_key="order_id", prev_window=W1, window=W2)


def test_empty_primary_key_fails_fast():
    with pytest.raises(CdcConfigError, match="empty primary key"):
        diff_windows(None, [row(""), row("a")], entity="order", primary_key="order_id", window=W2)


# -- config surface (columns whitelist) ------------------------------------------


def test_columns_whitelist_restricts_change_detection():
    prev = [row("a", status="same", total="10")]
    curr = [row("a", status="same", total="99")]  # only 'total' changed
    diff = diff_windows(
        prev, curr, entity="order", primary_key="order_id",
        columns=("status",), prev_window=W1, window=W2,
    )
    assert ops(diff) == []  # total is outside the whitelist -> unchanged
    assert diff.unchanged == 1


def test_columns_whitelist_does_not_trim_images():
    prev = [row("a", status="old")]
    curr = [row("a", status="new")]
    diff = diff_windows(
        prev, curr, entity="order", primary_key="order_id",
        columns=("status",), prev_window=W1, window=W2,
    )
    e = diff.events[0]
    assert e.changed == ("status",)
    assert e.before == prev[0] and e.after == curr[0]  # images stay FULL


# -- wire format -----------------------------------------------------------------


def test_to_jsonl_round_trip_and_field_order():
    diff = diff_windows(
        [row("a", status="old")], [row("a", status="new")],
        entity="order", primary_key="order_id", prev_window=W1, window=W2,
    )
    line = diff.events[0].to_jsonl()
    assert line.endswith("\n")
    parsed = json.loads(line)
    assert list(parsed) == ["seq", "op", "entity", "pk", "window", "prev_window", "changed", "before", "after"]
    assert parsed["op"] == OP_UPDATE and parsed["pk"] == "a"
    assert parsed["prev_window"] == W1 and parsed["window"] == W2


def test_delete_event_json_shape():
    diff = diff_windows([row("b")], [], entity="order", primary_key="order_id", prev_window=W1, window=W2)
    parsed = json.loads(diff.events[0].to_jsonl())
    assert parsed["op"] == OP_DELETE
    assert parsed["before"] == row("b") and parsed["after"] is None
    assert parsed["changed"] == []
