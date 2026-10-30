"""Topic compaction over the stream log (Milestone 6d, ADR-031).

CONSUMER layer over Milestone 6c's output (ADR-030): reads the append-only,
offset-addressed stream log -- NEVER the CDC journals -- and produces the
compacted, latest-wins-per-key view:

    data/streams/{entity}/part-00000.jsonl      (6c log, read-only)
    data/streams/{entity}/offsets.json          (6c checkpoint, read-only)
    data/generated/{entity}/...                 (snapshots, read-only yardstick)
            |
            v
    CompactRunner (--config configs/compact_base.yaml)
            |   validate log lines against the ADR-030 wire contract
            |   (offset == line number, entity, ops, value images)
            |   cross-check the 6c offsets.json checkpoint
            |   fold: per key, the HIGHEST source offset wins
            |   retained records re-emitted in source offset order,
            |   each line byte-identical to its source line
            |   verify compacted live view == snapshot at the log's
            |   last window (cell-level; missing -> match=null)
            v
    data/streams_compacted/{entity}/part-00000.jsonl
    data/streams_compacted/{entity}/offsets.json
    data/streams_compacted/report.json  (REPORT_VERSION 1, atomic)

Semantics (ADR-031): a compacted record IS a source record -- same envelope,
same offset, byte-identical line; the compacted log is a subsequence of the
source log. Tombstones (DELETE latest) are retained by default;
options.drop_tombstones drops them for a pure live-state table. Reuses
check_containment / snapshot_candidates / read_snapshot from the 6c runner.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from src.core.stream.replay import (
    OP_DELETE,
    OP_NAMES,
    StreamConfigError,
    StreamError,
)
from src.core.stream.runner import (
    check_containment,
    read_snapshot,
    snapshot_candidates,
)

REPORT_VERSION = 1
MISMATCH_SAMPLES = 5

PART_NAME = "part-00000.jsonl"
OFFSETS_NAME = "offsets.json"


@dataclass(frozen=True)
class CompactEntityConfig:
    """What we know about one entity, from config only."""

    entity: str
    primary_key: str


@dataclass(frozen=True)
class CompactConfig:
    """Parsed configs/compact_base.yaml."""

    entities: Mapping[str, CompactEntityConfig]
    stream_root: Path
    snapshot_root: Path
    output_root: Path
    drop_tombstones: bool
    verify_state: bool

    @staticmethod
    def load(raw: Mapping[str, object]) -> "CompactConfig":
        block = raw.get("compaction")
        if not isinstance(block, Mapping):
            raise StreamConfigError("config needs a top-level 'compaction:' block")

        stream_root = str(block.get("stream_root", "data/streams")).strip()
        if not stream_root:
            raise StreamConfigError("compaction.stream_root must be a non-empty path")
        snapshot_root = str(block.get("snapshot_root", "data/generated")).strip()
        if not snapshot_root:
            raise StreamConfigError("compaction.snapshot_root must be a non-empty path")
        output_root = str(block.get("output_root", "data/streams_compacted")).strip()
        if not output_root:
            raise StreamConfigError("compaction.output_root must be a non-empty path")

        options_raw = block.get("options", {})
        if not isinstance(options_raw, Mapping):
            raise StreamConfigError("compaction.options must be a mapping")
        drop_tombstones = bool(options_raw.get("drop_tombstones", False))
        verify_state = bool(options_raw.get("verify_state", True))

        raw_entities = block.get("entities")
        if not isinstance(raw_entities, Mapping) or not raw_entities:
            raise StreamConfigError("compaction.entities must list at least one entity")
        entities: dict[str, CompactEntityConfig] = {}
        for entity, spec_raw in raw_entities.items():
            if not isinstance(spec_raw, Mapping):
                raise StreamConfigError(f"{entity}: entity spec must be a mapping")
            primary_key = str(spec_raw.get("primary_key", "")).strip()
            if not primary_key:
                raise StreamConfigError(f"{entity}: primary_key is required")
            entities[str(entity)] = CompactEntityConfig(
                entity=str(entity), primary_key=primary_key
            )

        return CompactConfig(
            entities=entities,
            stream_root=Path(stream_root),
            snapshot_root=Path(snapshot_root),
            output_root=Path(output_root),
            drop_tombstones=drop_tombstones,
            verify_state=verify_state,
        )


@dataclass(frozen=True)
class StreamRecord:
    """One validated envelope from the 6c stream log.

    `raw` keeps the exact source line (minus the newline) so the compacted
    output can re-emit it byte-identically; `source` is the "<file>:<line>"
    label used in every diagnostic.
    """

    offset: int
    key: str
    op: str
    entity: str
    window: str
    seq: int
    value: Mapping[str, str] | None
    raw: str
    source: str


def parse_log_record(
    obj: object,
    source: str,
    entity: str,
    primary_key: str,
    expected_offset: int,
) -> StreamRecord:
    """Validate one decoded log line against the ADR-030 wire contract."""
    if not isinstance(obj, dict):
        raise StreamError(f"{source}: stream log line must be a JSON object")
    offset = obj.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        raise StreamError(f"{source}: offset must be a positive integer; got {offset!r}")
    if offset != expected_offset:
        raise StreamError(
            f"{source}: offset {offset} breaks the 1..N contiguity contract "
            f"(expected {expected_offset}) -- the log was edited, truncated, "
            f"or reordered; re-run stream_main.py to rebuild it"
        )
    key = obj.get("key")
    if not isinstance(key, str) or not key:
        raise StreamError(f"{source}: key must be a non-empty string")
    op = obj.get("op")
    if op not in OP_NAMES:
        raise StreamError(
            f"{source}: op must be one of {', '.join(OP_NAMES)}; got {op!r}"
        )
    record_entity = obj.get("entity")
    if not isinstance(record_entity, str) or not record_entity:
        raise StreamError(f"{source}: entity must be a non-empty string")
    if record_entity != entity:
        raise StreamError(
            f"{source}: envelope entity {record_entity!r} does not match log "
            f"entity {entity!r} (misplaced file?)"
        )
    window = obj.get("window")
    if not isinstance(window, str) or not window:
        raise StreamError(f"{source}: window must be a non-empty string")
    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise StreamError(f"{source}: seq must be a positive integer; got {seq!r}")
    value = obj.get("value")
    if op == OP_DELETE:
        if value is not None:
            raise StreamError(f"{source}: DELETE tombstone must carry value=null")
    else:
        if not isinstance(value, dict):
            raise StreamError(
                f"{source}: {op} must carry a full row image in 'value'"
            )
        for column, cell in value.items():
            if not isinstance(column, str) or not isinstance(cell, str):
                raise StreamError(
                    f"{source}: value images hold {{column: str}} cells "
                    f"(reader-boundary normalization, ADR-028); bad cell {column!r}"
                )
        if primary_key not in value:
            raise StreamError(
                f"{source}: {op} value image for key {key!r} lacks the "
                f"primary_key column {primary_key!r}"
            )
        if value[primary_key] != key:
            raise StreamError(
                f"{source}: envelope key {key!r} does not match the row image's "
                f"{primary_key}={value[primary_key]!r} -- broken envelope"
            )
    return StreamRecord(
        offset=offset,
        key=key,
        op=op,
        entity=record_entity,
        window=window,
        seq=seq,
        value=dict(value) if value is not None else None,
        raw=_render(obj),
        source=source,
    )


def _render(obj: Mapping[str, object]) -> str:
    """Re-render one envelope exactly the way 6c wrote it."""
    return json.dumps(
        {
            "offset": obj["offset"],
            "key": obj["key"],
            "op": obj["op"],
            "entity": obj["entity"],
            "window": obj["window"],
            "seq": obj["seq"],
            "value": obj["value"],
        }
    )


def read_log(path: Path, entity: str, primary_key: str) -> list[StreamRecord]:
    """Parse one stream log into validated records (strict, line-numbered)."""
    records: list[StreamRecord] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        source = f"{path.as_posix()}:{lineno}"
        if not line.strip():
            raise StreamError(f"{source}: blank line in stream log")
        records.append(
            parse_log_record(
                json.loads(line), source, entity, primary_key, expected_offset=lineno
            )
        )
    return records


def read_checkpoint(path: Path, entity: str, records: list[StreamRecord]) -> None:
    """Cross-check the 6c offsets.json against the log we just read."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StreamError(
            f"{path.as_posix()}: unreadable offsets checkpoint ({exc}) -- "
            f"re-run stream_main.py to rebuild it"
        ) from None
    if not isinstance(payload, dict):
        raise StreamError(f"{path.as_posix()}: offsets checkpoint must be a JSON object")
    last_offset = records[-1].offset if records else 0
    for field, expected in (("events", len(records)), ("last_offset", last_offset)):
        got = payload.get(field)
        if got != expected:
            raise StreamError(
                f"{entity}: offsets checkpoint says {field}={got!r} but the log "
                f"holds {expected} -- log and checkpoint disagree; "
                f"re-run stream_main.py"
            )


def compact_records(
    records: list[StreamRecord], drop_tombstones: bool
) -> list[StreamRecord]:
    """Latest-wins fold: per key the highest source offset survives.

    The retained records come back in ascending source offset order, so the
    compacted log is a subsequence of the source log. With
    drop_tombstones=True keys whose latest record is a DELETE are dropped
    entirely (pure live-state table).
    """
    latest: dict[str, StreamRecord] = {}
    for record in records:  # records arrive in offset order 1..N
        latest[record.key] = record
    retained = sorted(latest.values(), key=lambda r: r.offset)
    if drop_tombstones:
        retained = [r for r in retained if r.op != OP_DELETE]
    return retained


def live_view(retained: list[StreamRecord]) -> dict[str, Mapping[str, str]]:
    """The state table the compacted log represents: non-DELETE latest records."""
    return {r.key: r.value for r in retained if r.op != OP_DELETE}


@dataclass(frozen=True)
class StateCheck:
    """Verification of the compacted live view against one snapshot."""

    window: str
    rows_compacted: int
    rows_snapshot: int | None  # None = snapshot not found -> unverified
    match: bool | None  # None = unverified
    mismatch_samples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "window": self.window,
            "rows_compacted": self.rows_compacted,
            "rows_snapshot": self.rows_snapshot,
            "match": self.match,
            "mismatch_samples": list(self.mismatch_samples),
        }


def _atomic_write(path: Path, data: str, log: Callable[[str, str], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8", newline="")
    os.replace(tmp, path)
    log("WRITE", f"{path} ({len(data.encode('utf-8'))} bytes)")


class CompactRunner:
    """Compacts one entity log at a time, then writes the shared report."""

    def __init__(
        self,
        config: CompactConfig,
        log: Callable[[str, str], None],
    ) -> None:
        self.config = config
        self.log = log

    # -- verification -----------------------------------------------------

    def _state_check(
        self,
        entity_cfg: CompactEntityConfig,
        window: str,
        retained: list[StreamRecord],
    ) -> StateCheck:
        candidates = snapshot_candidates(
            self.config.snapshot_root, entity_cfg.entity, window
        )
        if len(candidates) > 1:
            names = ", ".join(c.as_posix() for c in candidates)
            raise StreamError(
                f"{entity_cfg.entity}: {len(candidates)} snapshot files claim "
                f"window {window}: {names} -- ambiguous, fix the tree"
            )
        if not candidates:
            self.log(
                "COMPACT",
                f"{entity_cfg.entity}: window {window} has no snapshot under "
                f"{self.config.snapshot_root.as_posix()} -- compacted view "
                f"unverified (recorded as match=null)",
            )
            return StateCheck(
                window=window,
                rows_compacted=len(live_view(retained)),
                rows_snapshot=None,
                match=None,
            )
        _, expected = read_snapshot(candidates[0], entity_cfg.primary_key)
        compacted = live_view(retained)
        samples: list[str] = []
        missing = sorted(set(expected) - set(compacted))
        extra = sorted(set(compacted) - set(expected))
        differing = sorted(
            key
            for key in sorted(set(expected) & set(compacted))
            if dict(compacted[key]) != dict(expected[key])
        )
        for label, keys in (("missing", missing), ("extra", extra), ("differing", differing)):
            for key in keys[: max(0, MISMATCH_SAMPLES - len(samples))]:
                samples.append(f"{label}:{key}")
        match = not (missing or extra or differing)
        return StateCheck(
            window=window,
            rows_compacted=len(compacted),
            rows_snapshot=len(expected),
            match=match,
            mismatch_samples=tuple(samples),
        )

    # -- one entity ---------------------------------------------------------

    def _run_entity(self, entity_cfg: CompactEntityConfig) -> dict[str, object]:
        entity = entity_cfg.entity
        entity_dir = self.config.stream_root / entity
        part_path = entity_dir / PART_NAME
        offsets_path = entity_dir / OFFSETS_NAME

        if not entity_dir.is_dir():
            self.log(
                "COMPACT",
                f"{entity}: no stream log under "
                f"{self.config.stream_root.as_posix()} -- skipped "
                f"(run stream_main.py for it first)",
            )
            return {"status": "skipped", "reason": "no stream log for entity"}

        if part_path.is_file() != offsets_path.is_file():
            missing = OFFSETS_NAME if offsets_path.is_file() else PART_NAME
            raise StreamError(
                f"{entity}: stream tree is half-present -- "
                f"{part_path.name} {'exists' if part_path.is_file() else 'is missing'}, "
                f"{offsets_path.name} {'exists' if offsets_path.is_file() else 'is missing'} "
                f"under {entity_dir.as_posix()}; re-run stream_main.py to rebuild it"
            )

        records = read_log(part_path, entity, entity_cfg.primary_key)
        read_checkpoint(offsets_path, entity, records)
        retained = compact_records(records, self.config.drop_tombstones)

        out_part = self.config.output_root / entity / PART_NAME
        payload = "".join(record.raw + "\n" for record in retained)
        _atomic_write(out_part, payload, self.log)

        live = live_view(retained)
        tombstones = sum(1 for r in retained if r.op == OP_DELETE)
        last_window = max((r.window for r in records), default=None)
        checkpoint = json.dumps(
            {
                "entity": entity,
                "records": len(records),
                "retained": len(retained),
                "live_keys": len(live),
                "tombstones": tombstones,
                "source_last_offset": records[-1].offset if records else 0,
                "last_window": last_window,
            }
        )
        _atomic_write(
            self.config.output_root / entity / OFFSETS_NAME,
            checkpoint + "\n",
            self.log,
        )

        if not self.config.verify_state:
            self.log(
                "COMPACT",
                f"{entity}: state verification disabled (options.verify_state=false)",
            )
            state = StateCheck(
                window=last_window or "",
                rows_compacted=len(live),
                rows_snapshot=None,
                match=None,
            )
        elif not records:
            self.log("COMPACT", f"{entity}: empty stream log -- nothing to verify")
            state = StateCheck(
                window=last_window or "",
                rows_compacted=0,
                rows_snapshot=None,
                match=None,
            )
        else:
            assert last_window is not None  # records non-empty
            state = self._state_check(entity_cfg, last_window, retained)

        self.log(
            "COMPACT",
            f"{entity}: records={len(records)} retained={len(retained)} "
            f"live={len(live)} tombstones={tombstones} "
            f"last_window={last_window or 'n/a'} "
            f"state={last_window or 'n/a'}="
            f"{'ok' if state.match else 'MISMATCH' if state.match is False else 'unverified'}",
        )
        summary: dict[str, object] = {
            "status": "ok",
            "records": len(records),
            "retained": len(retained),
            "live_keys": len(live),
            "tombstones": tombstones,
            "source_last_offset": records[-1].offset if records else 0,
            "last_window": last_window,
            "state_check": state.to_dict(),
        }
        if state.mismatch_samples:
            summary["samples"] = list(state.mismatch_samples)
        return summary

    # -- whole run ----------------------------------------------------------

    def run(
        self,
        entity_filter: list[str] | None = None,
        report_path: Path | None = None,
    ) -> dict[str, object]:
        if entity_filter:
            unknown = [
                name for name in entity_filter if name not in self.config.entities
            ]
            if unknown:
                raise StreamConfigError(
                    f"unknown entity/ies: {', '.join(unknown)} -- configured: "
                    f"{', '.join(self.config.entities)}"
                )
        check_containment(
            self.config.output_root,
            (self.config.stream_root, self.config.snapshot_root),
            "compaction output_root",
        )
        if not self.config.stream_root.is_dir():
            raise StreamError(
                f"stream root {self.config.stream_root.as_posix()} not found "
                f"-- run stream_main.py at least once first"
            )

        per_entity: dict[str, dict[str, object]] = {}
        ok = True
        selected = (
            [self.config.entities[name] for name in entity_filter]
            if entity_filter
            else list(self.config.entities.values())
        )
        for entity_cfg in selected:
            summary = self._run_entity(entity_cfg)
            per_entity[entity_cfg.entity] = summary
            check = summary.get("state_check")
            if isinstance(check, dict) and check.get("match") is False:
                ok = False

        report = {
            "report_version": REPORT_VERSION,
            "milestone": "6d",
            "ok": ok,
            "options": {
                "drop_tombstones": self.config.drop_tombstones,
                "verify_state": self.config.verify_state,
            },
            "entities": per_entity,
        }
        target = report_path or (self.config.output_root / "report.json")
        _atomic_write(target, json.dumps(report, indent=2) + "\n", self.log)
        return report
