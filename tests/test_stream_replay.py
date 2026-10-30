"""Unit tests for the replay fold and journal parsing (Milestone 6c, ADR-030).

Pure layer -- no filesystem. Pins the strict fold contract:
  * state == fold(events) for INSERT/UPDATE/DELETE sequences
  * resurrection is legal (DELETE frees the key)
  * every contract violation fails fast (unknown keys, live INSERT,
    non-null DELETE tombstone, column-set breaks, duplicate positions)
  * ordering is (window, seq), stable under input shuffling
  * header (column order) comes from the first full row image
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.core.stream.replay import (
    JournalEvent,
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
    StreamError,
    fold_events,
    order_events,
    parse_journal_event,
)

SRC = "journal.jsonl:1"


def ev(op: str, pk: str, window: str, seq: int, after=None, source: str = SRC) -> JournalEvent:
    return JournalEvent(window=window, seq=seq, op=op, pk=pk, after=after, source=source)


def wire(op: str, pk: str, window: str, seq: int, **fields) -> JournalEvent:
    """Build through the wire parser -- the format tests share the fold tests' path."""
    obj = {"seq": seq, "op": op, "entity": "order", "pk": pk, "window": window,
           "prev_window": None, "changed": [], "before": None, "after": None}
    obj.update(fields)
    return parse_journal_event(obj, SRC, "order")


# -- the fold contract --------------------------------------------------------

def test_initial_load_fold_all_inserts():
    replay = fold_events([
        ev(OP_INSERT, "o2", "2026-01-15", 2, {"order_id": "o2", "status": "new"}),
        ev(OP_INSERT, "o1", "2026-01-15", 1, {"order_id": "o1", "status": "new"}),
    ])
    assert set(replay.state) == {"o1", "o2"}
    assert replay.state["o1"] == {"order_id": "o1", "status": "new"}
    assert replay.counts == {"INSERT": 2, "UPDATE": 0, "DELETE": 0}


def test_delta_fold_insert_update_delete():
    replay = fold_events([
        ev(OP_INSERT, "a", "W1", 1, {"k": "a", "v": "1"}),
        ev(OP_INSERT, "b", "W1", 2, {"k": "b", "v": "1"}),
        ev(OP_INSERT, "c", "W1", 3, {"k": "c", "v": "1"}),
        ev(OP_INSERT, "d", "W2", 1, {"k": "d", "v": "1"}),
        ev(OP_UPDATE, "a", "W2", 2, {"k": "a", "v": "2"}),
        ev(OP_DELETE, "b", "W2", 3, None),
    ])
    assert set(replay.state) == {"a", "c", "d"}
    assert replay.state["a"]["v"] == "2"
    assert replay.counts == {"INSERT": 4, "UPDATE": 1, "DELETE": 1}


def test_resurrection_across_windows_is_legal():
    replay = fold_events([
        ev(OP_INSERT, "a", "W1", 1, {"k": "a", "v": "1"}),
        ev(OP_DELETE, "a", "W2", 1, None),
        ev(OP_INSERT, "a", "W3", 1, {"k": "a", "v": "2"}),
    ])
    assert replay.state["a"] == {"k": "a", "v": "2"}
    assert replay.counts == {"INSERT": 2, "UPDATE": 0, "DELETE": 1}


def test_delete_then_update_in_same_window_still_fails():
    with pytest.raises(StreamError, match="unknown pk"):
        fold_events([
            ev(OP_INSERT, "a", "W1", 1, {"k": "a"}),
            ev(OP_DELETE, "a", "W2", 1, None),
            ev(OP_UPDATE, "a", "W2", 2, {"k": "a"}),
        ])


# -- strict contract violations ------------------------------------------------

def test_insert_for_live_pk_fails():
    with pytest.raises(StreamError, match="INSERT for live pk 'a'"):
        fold_events([
            ev(OP_INSERT, "a", "W1", 1, {"k": "a"}),
            ev(OP_INSERT, "a", "W2", 1, {"k": "a"}),
        ])


def test_update_for_unknown_pk_fails():
    with pytest.raises(StreamError, match="UPDATE for unknown pk"):
        fold_events([ev(OP_UPDATE, "ghost", "W1", 1, {"k": "ghost"})])


def test_delete_for_unknown_pk_fails():
    with pytest.raises(StreamError, match="DELETE for unknown pk"):
        fold_events([ev(OP_DELETE, "ghost", "W1", 1, None)])


def test_delete_must_carry_null_after():
    with pytest.raises(StreamError, match="DELETE must carry after=null"):
        fold_events([
            ev(OP_INSERT, "a", "W1", 1, {"k": "a"}),
            ev(OP_DELETE, "a", "W2", 1, {"k": "a"}),
        ])


def test_insert_and_update_require_full_after_image():
    with pytest.raises(StreamError, match="full row image"):
        fold_events([ev(OP_INSERT, "a", "W1", 1, None)])
    with pytest.raises(StreamError, match="full row image"):
        fold_events([ev(OP_UPDATE, "a", "W1", 1, None)])


def test_after_images_hold_string_cells_only():
    obj = {"seq": 1, "op": "INSERT", "entity": "order", "pk": "a",
           "window": "W1", "changed": [], "before": None,
           "after": {"k": 1}}  # int cell -- reader boundary violated
    with pytest.raises(StreamError, match="column: str"):
        parse_journal_event(obj, SRC, "order")


def test_entity_mismatch_fails():
    obj = {"seq": 1, "op": "INSERT", "entity": "customer", "pk": "a",
           "window": "W1", "changed": [], "before": None, "after": {"k": "a"}}
    with pytest.raises(StreamError, match="does not match journal entity"):
        parse_journal_event(obj, SRC, "order")


# -- column order / column set --------------------------------------------------

def test_header_order_from_first_image_and_set_conflict_fails():
    with pytest.raises(StreamError, match="breaks the column set"):
        fold_events([
            ev(OP_INSERT, "a", "W1", 1, {"k": "a", "v": "1"}),
            ev(OP_INSERT, "b", "W1", 2, {"k": "b"}),  # missing column v
        ])
    replay = fold_events([
        ev(OP_INSERT, "a", "W1", 1, {"k": "a", "v": "1"}),
        ev(OP_INSERT, "b", "W1", 2, {"v": "1", "k": "b"}),  # same set, other order
    ])
    assert replay.header == ["k", "v"]
    assert replay.state["b"] == {"v": "1", "k": "b"}


# -- ordering --------------------------------------------------------------------

def test_ordering_is_window_then_seq_regardless_of_input_order():
    events = [
        ev(OP_INSERT, "a", "W1", 2, {"k": "a", "v": "1"}),
        ev(OP_INSERT, "b", "W1", 1, {"k": "b", "v": "1"}),
        ev(OP_UPDATE, "a", "W2", 1, {"k": "a", "v": "2"}),
        ev(OP_DELETE, "b", "W3", 1, None),
    ]
    for events_shuffled in (list(reversed(events)), [events[2], events[0], events[3], events[1]]):
        replay = fold_events(events_shuffled)
        assert set(replay.state) == {"a"}
        assert replay.state["a"]["v"] == "2"


def test_duplicate_window_seq_position_fails():
    with pytest.raises(StreamError, match="duplicate journal position"):
        order_events([
            ev(OP_INSERT, "a", "W1", 1, {"k": "a"}, source="one.jsonl:1"),
            ev(OP_INSERT, "b", "W1", 1, {"k": "b"}, source="two.jsonl:1"),
        ])


# -- wire parser rejects malformed lines ------------------------------------------

@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"op": "UPSERT"}, "op must be one of"),
        ({"pk": ""}, "pk must be a non-empty string"),
        ({"seq": 0}, "seq must be a positive integer"),
        ({"seq": True}, "seq must be a positive integer"),  # bool is not int here
        ({"window": ""}, "window must be a non-empty string"),
    ],
)
def test_wire_parser_fail_fasts(mutation, message):
    obj = {"seq": 1, "op": "INSERT", "entity": "order", "pk": "a",
           "window": "W1", "changed": [], "before": None, "after": {"k": "a"}}
    obj.update(mutation)
    with pytest.raises(StreamError, match=message):
        parse_journal_event(obj, SRC, "order")


def test_wire_round_trip_matches_cdc_event_shape():
    event = wire(OP_UPDATE, "o-1", "2026-01-16", 3, after={"order_id": "o-1", "status": "paid"})
    assert event.pk == "o-1" and event.window == "2026-01-16" and event.seq == 3
    assert event.after == {"order_id": "o-1", "status": "paid"}
