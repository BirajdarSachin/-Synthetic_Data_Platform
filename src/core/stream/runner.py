"""Stream runner (Milestone 6c, ADR-030).

Turns the CDC journals of one entity into an OPERATIONAL event stream:

    data/cdc/{entity}/reference_date=*/...jsonl   (journals, ADR-029)
    data/generated/{entity}/reference_date=*/...  (snapshots, read-only)
            |
            v
    StreamRunner (--config configs/stream_base.yaml)
            |   discover journal files, classify
            |   initial__{W}.jsonl / {P}__{W}.jsonl,
            |   link them into ONE chain per entity
            |   (gaps / missing initial / unclassifiable -> fail fast),
            |   fold events in (window, seq) order,
            |   verify each folded prefix against its snapshot
            |   (cell-level; byte-compare informational),
            |   export the offset-addressed stream
            v
    data/streams/{entity}/part-00000.jsonl
    data/streams/{entity}/offsets.json
    data/streams/report.json  (REPORT_VERSION 1, atomic)

Guards (ADR-030 decision 5): the output tree must not sit inside the
journal root or the snapshot root, or contain them (both directions,
against both). Every write is atomic (.tmp + os.replace). Offsets are
recomputed from the journal set on every run -- deterministic, no state
file to corrupt. Zero imports from generator/writer/impurity internals.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from src.core.stream.replay import (
    JournalEvent,
    OP_DELETE,
    OP_INSERT,
    OP_NAMES,
    OP_UPDATE,
    StreamConfigError,
    StreamError,
    StreamReplay,
    order_events,
    parse_journal_event,
)

REPORT_VERSION = 1
MODES = ("both", "replay", "export")
MISMATCH_SAMPLES = 5

_INITIAL_RE = re.compile(r"^initial__(\d{4}-\d{2}(?:-\d{2})?)\.jsonl$")
_DELTA_RE = re.compile(
    r"^(\d{4}-\d{2}(?:-\d{2})?)__(\d{4}-\d{2}(?:-\d{2})?)\.jsonl$"
)
_PARTITION_RE = re.compile(r"^[^=]+=(\d{4}-\d{2}(?:-\d{2})?)$")


@dataclass(frozen=True)
class StreamEntityConfig:
    """What we know about one entity, from config only."""

    entity: str
    primary_key: str


@dataclass(frozen=True)
class StreamConfig:
    """Parsed configs/stream_base.yaml."""

    entities: Mapping[str, StreamEntityConfig]
    journal_root: Path
    snapshot_root: Path
    output_root: Path
    verify_replay: bool
    byte_compare: bool

    @staticmethod
    def load(raw: Mapping[str, object]) -> "StreamConfig":
        stream = raw.get("stream")
        if not isinstance(stream, Mapping):
            raise StreamConfigError("config needs a top-level 'stream:' block")

        journal_root = str(stream.get("journal_root", "data/cdc")).strip()
        if not journal_root:
            raise StreamConfigError("stream.journal_root must be a non-empty path")
        snapshot_root = str(stream.get("snapshot_root", "data/generated")).strip()
        if not snapshot_root:
            raise StreamConfigError("stream.snapshot_root must be a non-empty path")
        output_root = str(stream.get("output_root", "data/streams")).strip()
        if not output_root:
            raise StreamConfigError("stream.output_root must be a non-empty path")

        options_raw = stream.get("options", {})
        if not isinstance(options_raw, Mapping):
            raise StreamConfigError("stream.options must be a mapping")
        verify_replay = bool(options_raw.get("verify_replay", True))
        byte_compare = bool(options_raw.get("byte_compare", False))

        raw_entities = stream.get("entities")
        if not isinstance(raw_entities, Mapping) or not raw_entities:
            raise StreamConfigError("stream.entities must list at least one entity")
        entities: dict[str, StreamEntityConfig] = {}
        for entity, spec_raw in raw_entities.items():
            if not isinstance(spec_raw, Mapping):
                raise StreamConfigError(f"{entity}: entity spec must be a mapping")
            primary_key = str(spec_raw.get("primary_key", "")).strip()
            if not primary_key:
                raise StreamConfigError(f"{entity}: primary_key is required")
            entities[str(entity)] = StreamEntityConfig(
                entity=str(entity), primary_key=primary_key
            )

        return StreamConfig(
            entities=entities,
            journal_root=Path(journal_root),
            snapshot_root=Path(snapshot_root),
            output_root=Path(output_root),
            verify_replay=verify_replay,
            byte_compare=byte_compare,
        )


@dataclass(frozen=True)
class JournalFile:
    """One classified journal file on disk."""

    path: Path
    kind: str  # "initial" | "delta"
    prev_window: str | None  # None for initial
    window: str


@dataclass(frozen=True)
class ReplayCheck:
    """Verification of one folded window prefix against its snapshot."""

    window: str
    rows_replayed: int
    rows_snapshot: int | None  # None = snapshot not found -> unverified
    match: bool | None  # None = unverified
    byte_identical: bool | None = None
    mismatch_samples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "window": self.window,
            "rows_replayed": self.rows_replayed,
            "rows_snapshot": self.rows_snapshot,
            "match": self.match,
            "byte_identical": self.byte_identical,
            "mismatch_samples": list(self.mismatch_samples),
        }


@dataclass(frozen=True)
class StreamRunResult:
    """Aggregate outcome for one stream_main invocation."""

    mode: str
    ok: bool
    events_total: int
    per_entity: Mapping[str, Mapping[str, object]]


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_containment(
    output_root: Path, guarded: tuple[Path, ...], label: str
) -> None:
    """Refuse output inside any guarded root, or containing it (both ways)."""
    out = output_root.resolve()
    for root in guarded:
        other = root.resolve()
        if _is_inside(out, other) or _is_inside(other, out):
            raise StreamConfigError(
                f"{label} {output_root.as_posix()} overlaps {root.as_posix()} -- "
                f"the stream tree must be its own derived root "
                f"(it must neither sit inside nor contain the other roots)"
            )


def build_chain(entity_dir: Path, entity: str) -> list[JournalFile] | None:
    """Classify journal files and link them into ONE ordered chain.

    Returns None when the entity has no journal files at all (a legitimate
    state -- the operator never ran cdc_main for this entity). Everything
    else that cannot form a clean chain fails fast with a guided message.
    """
    candidates: list[tuple[Path, str]] = []  # (path, window of its partition)
    for partition in sorted(entity_dir.glob("reference_date=*/")):
        match = _PARTITION_RE.match(partition.name)
        if match is None:
            raise StreamError(
                f"{entity}: malformed partition directory "
                f"{partition.name!r} under {entity_dir.as_posix()} -- "
                f"cdc journals live in reference_date=<window>/"
            )
        for journal in sorted(partition.glob("*.jsonl")):
            candidates.append((journal, match.group(1)))
    for journal in sorted(entity_dir.glob("*.jsonl")):  # flat fallback
        candidates.append((journal, ""))

    if not candidates:
        return None

    files: list[JournalFile] = []
    for path, partition_window in candidates:
        initial = _INITIAL_RE.match(path.name)
        delta = _DELTA_RE.match(path.name)
        if initial is not None:
            window = initial.group(1)
            if partition_window and partition_window != window:
                raise StreamError(
                    f"{entity}: journal {path.name} sits in partition "
                    f"reference_date={partition_window} but is named for "
                    f"window {window} -- move or rename it"
                )
            files.append(JournalFile(path, "initial", None, window))
        elif delta is not None:
            prev_window, window = delta.group(1), delta.group(2)
            if prev_window == window:
                raise StreamError(
                    f"{entity}: journal {path.name} pairs a window with "
                    f"itself ({window}) -- prev must be earlier"
                )
            if partition_window and partition_window != window:
                raise StreamError(
                    f"{entity}: journal {path.name} sits in partition "
                    f"reference_date={partition_window} but is named for "
                    f"window {window} -- move or rename it"
                )
            files.append(JournalFile(path, "delta", prev_window, window))
        else:
            raise StreamError(
                f"{entity}: unclassifiable journal file {path.name} under "
                f"{entity_dir.as_posix()} -- expected initial__<window>.jsonl "
                f"or <prev>__<window>.jsonl"
            )

    initials = [f for f in files if f.kind == "initial"]
    if not initials:
        windows = ", ".join(sorted({f.window for f in files})) or "(none)"
        raise StreamError(
            f"{entity}: no initial journal under {entity_dir.as_posix()} -- "
            f"the chain cannot start; windows present: {windows}; "
            f"-> run cdc_main.py --window <earliest> for the initial load"
        )
    if len(initials) > 1:
        names = ", ".join(sorted(f.path.name for f in initials))
        raise StreamError(
            f"{entity}: {len(initials)} initial journals found ({names}) -- "
            f"exactly one chain head is allowed"
        )

    chain: list[JournalFile] = [initials[0]]
    deltas = sorted((f for f in files if f.kind == "delta"), key=lambda f: f.window)
    seen_windows = {chain[0].window}
    for delta in deltas:
        if delta.window in seen_windows:
            raise StreamError(
                f"{entity}: two journals end at window {delta.window} -- "
                f"the chain would fork"
            )
        expected_prev = chain[-1].window
        if delta.prev_window != expected_prev:
            present = ", ".join(sorted(seen_windows | {delta.window}))
            raise StreamError(
                f"{entity}: chain gap -- journal {delta.path.name} pairs "
                f"{delta.prev_window} -> {delta.window}, but the chain head "
                f"is {expected_prev}; windows present: {present}; "
                f"-> run cdc_main.py --window {delta.window} "
                f"(prev auto-detected) to fill the gap"
            )
        seen_windows.add(delta.window)
        chain.append(delta)
    return chain


def load_journal(path: Path, entity: str) -> list[JournalEvent]:
    """Parse one journal file into validated events (strict, line-numbered)."""
    events: list[JournalEvent] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        source = f"{path.as_posix()}:{lineno}"
        if not line.strip():
            raise StreamError(f"{source}: blank line in journal")
        events.append(parse_journal_event(json.loads(line), source, entity))
    return events


def snapshot_candidates(snapshot_root: Path, entity: str, window: str) -> list[Path]:
    """All CSV files that claim to be {entity} @ {window} (6b-style discovery)."""
    entity_dir = snapshot_root / entity
    if not entity_dir.is_dir():
        return []
    found: list[Path] = []
    for partition in sorted(entity_dir.glob("*={window}".format(window=window))):
        if _PARTITION_RE.match(partition.name) is None:
            continue
        found.extend(sorted(partition.glob(f"{entity}_{window}.csv")))
    flat = entity_dir / f"{entity}_{window}.csv"
    if flat.is_file():
        found.append(flat)
    return found


def read_snapshot(
    path: Path, primary_key: str
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Parse one snapshot CSV into (header, {pk: {column: cell}}).

    Same reader-boundary rules as the whole harness family: every cell is a
    str, short rows are padded with "", long rows fail fast, duplicate pks
    fail fast (6b parity -- CDC compares per-window snapshots too).
    """
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        raise StreamError(f"{path.as_posix()}: empty snapshot file")
    header = rows[0]
    width = len(header)
    if primary_key not in header:
        raise StreamError(
            f"{path.as_posix()}: primary_key column {primary_key!r} "
            f"not in header {header}"
        )
    pk_index = header.index(primary_key)
    state: dict[str, dict[str, str]] = {}
    for lineno, cells in enumerate(rows[1:], start=2):
        if len(cells) > width:
            raise StreamError(
                f"{path.as_posix()}:{lineno}: {len(cells)} fields > "
                f"{width} header columns"
            )
        padded = cells + [""] * (width - len(cells))
        pk = padded[pk_index]
        if pk in state:
            raise StreamError(
                f"{path.as_posix()}:{lineno}: duplicate pk {pk!r} "
                f"(already seen earlier in this file)"
            )
        state[pk] = dict(zip(header, padded))
    return header, state


def render_csv(header: list[str], state: Mapping[str, Mapping[str, str]]) -> str:
    """Render replayed state as CSV (pk-sorted, minimal quoting, LF)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for pk in sorted(state):
        row = state[pk]
        writer.writerow([row.get(column, "") for column in header])
    return buffer.getvalue()


def assign_offsets(events: list[JournalEvent]) -> list[tuple[int, JournalEvent]]:
    """Global 1..N offsets across the whole ordered chain (deterministic)."""
    return [(index + 1, event) for index, event in enumerate(events)]


def export_line(offset: int, entity: str, event: JournalEvent) -> str:
    """Kafka-log-style envelope: key + op + value, offset-addressed.

    {"offset": 7, "key": "o-3", "op": "UPDATE", "entity": "order",
     "window": "2026-01-16", "seq": 3, "value": {...full after image...}}
    DELETE records carry value=null -- the tombstone convention.
    """
    return (
        json.dumps(
            {
                "offset": offset,
                "key": event.pk,
                "op": event.op,
                "entity": entity,
                "window": event.window,
                "seq": event.seq,
                "value": dict(event.after) if event.after is not None else None,
            }
        )
        + "\n"
    )


def _atomic_write(path: Path, data: str, log: Callable[[str, str], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8", newline="")
    os.replace(tmp, path)
    log("WRITE", f"{path} ({len(data.encode('utf-8'))} bytes)")


class StreamRunner:
    def __init__(
        self,
        config: StreamConfig,
        log: Callable[[str, str], None],
    ) -> None:
        self.config = config
        self.log = log

    # -- verification -----------------------------------------------------

    def _verify_prefix(
        self,
        entity_cfg: StreamEntityConfig,
        window: str,
        replay: "StreamReplay",
    ) -> ReplayCheck:
        candidates = snapshot_candidates(self.config.snapshot_root, entity_cfg.entity, window)
        if len(candidates) > 1:
            names = ", ".join(c.as_posix() for c in candidates)
            raise StreamError(
                f"{entity_cfg.entity}: {len(candidates)} snapshot files claim "
                f"window {window}: {names} -- ambiguous, fix the tree"
            )
        if not candidates:
            self.log(
                "STREAM",
                f"{entity_cfg.entity}: window {window} has no snapshot under "
                f"{self.config.snapshot_root.as_posix()} -- replay unverified "
                f"(recorded as match=null)",
            )
            return ReplayCheck(
                window=window,
                rows_replayed=len(replay.state),
                rows_snapshot=None,
                match=None,
                byte_identical=None,
            )
        header, expected = read_snapshot(candidates[0], entity_cfg.primary_key)
        samples: list[str] = []
        missing = sorted(set(expected) - set(replay.state))
        extra = sorted(set(replay.state) - set(expected))
        differing = sorted(
            pk
            for pk in set(expected) & set(replay.state)
            if expected[pk] != replay.state[pk]
        )
        for pk in (missing + extra + differing)[:MISMATCH_SAMPLES]:
            if pk in missing:
                samples.append(f"{pk}: in snapshot, not in replay")
            elif pk in extra:
                samples.append(f"{pk}: in replay, not in snapshot")
            else:
                samples.append(f"{pk}: cell values differ")
        match = not (missing or extra or differing)
        byte_identical: bool | None = None
        if self.config.byte_compare and match:
            rendered = render_csv(header, replay.state)
            byte_identical = rendered.encode("utf-8") == candidates[0].read_bytes()
        return ReplayCheck(
            window=window,
            rows_replayed=len(replay.state),
            rows_snapshot=len(expected),
            match=match,
            byte_identical=byte_identical,
            mismatch_samples=tuple(samples),
        )

    # -- per entity --------------------------------------------------------

    def _run_entity(
        self,
        entity_cfg: StreamEntityConfig,
        mode: str,
        up_to_window: str | None,
    ) -> dict[str, object]:
        entity = entity_cfg.entity
        entity_dir = self.config.journal_root / entity
        if not entity_dir.is_dir():
            self.log(
                "STREAM",
                f"{entity}: no journal directory under "
                f"{self.config.journal_root.as_posix()} -- skipped "
                f"(run cdc_main.py for this entity first)",
            )
            return {"status": "skipped", "reason": "no journal directory"}

        chain = build_chain(entity_dir, entity)
        if chain is None:
            self.log(
                "STREAM",
                f"{entity}: no journal files under "
                f"{entity_dir.as_posix()} -- skipped",
            )
            return {"status": "skipped", "reason": "no journal files"}

        if up_to_window is not None:
            kept: list[JournalFile] = []
            for link in chain:
                if link.kind == "delta" and link.window > up_to_window:
                    break
                if link.kind == "initial" and up_to_window < link.window:
                    raise StreamError(
                        f"{entity}: --up-to-window {up_to_window} precedes the "
                        f"chain head {link.window}"
                    )
                kept.append(link)
            chain = kept
        windows = [link.window for link in chain]
        self.log(
            "STREAM",
            f"{entity}: chain "
            + " -> ".join(
                (f"{link.prev_window or 'initial'}:{link.window}" if link.kind == "delta" else f"initial:{link.window}")
                for link in chain
            ),
        )

        ordered: list[JournalEvent] = []
        for link in chain:
            for event in load_journal(link.path, entity):
                if event.window != link.window:
                    raise StreamError(
                        f"{event.source}: event window {event.window!r} does "
                        f"not match its journal's window {link.window!r} "
                        f"(misplaced event)"
                    )
                ordered.append(event)
        ordered = order_events(ordered)

        replay = StreamReplay()
        replay_checks: list[ReplayCheck] = []
        by_window: dict[str, list[JournalEvent]] = {}
        for event in ordered:
            by_window.setdefault(event.window, []).append(event)
        for link in chain:
            replay.apply_all(by_window.get(link.window, []))
            if self.config.verify_replay and mode in ("both", "replay"):
                replay_checks.append(
                    self._verify_prefix(entity_cfg, link.window, replay)
                )
        for check in replay_checks:
            if check.match is False:
                self.log(
                    "STREAM",
                    f"{entity}: REPLAY MISMATCH at window {check.window} "
                    f"(replayed {check.rows_replayed} vs snapshot "
                    f"{check.rows_snapshot} rows); samples: "
                    + "; ".join(check.mismatch_samples),
                )

        result: dict[str, object] = {
            "status": "ok",
            "chain": windows,
            "events": {
                **{op: replay.counts[op] for op in OP_NAMES},
                "total": len(ordered),
            },
            "replay_checks": [check.to_dict() for check in replay_checks],
        }

        if mode in ("both", "export"):
            offset_events = assign_offsets(ordered)
            part_path = self.config.output_root / entity / "part-00000.jsonl"
            payload = "".join(
                export_line(offset, entity, event) for offset, event in offset_events
            )
            _atomic_write(part_path, payload, self.log)
            window_ranges: dict[str, dict[str, int]] = {}
            for offset, event in offset_events:
                span = window_ranges.setdefault(event.window, {"first": offset, "last": offset})
                span["last"] = offset
            offsets_payload = json.dumps(
                {
                    "entity": entity,
                    "events": len(offset_events),
                    "last_offset": offset_events[-1][0] if offset_events else 0,
                    "windows": window_ranges,
                }
            )
            _atomic_write(
                self.config.output_root / entity / "offsets.json",
                offsets_payload + "\n",
                self.log,
            )
            last = offset_events[-1][0] if offset_events else 0
            self.log(
                "STREAM",
                f"{entity}: exported {len(offset_events)} events, "
                f"offsets 1..{last} -> {part_path.as_posix()}",
            )
            result["export"] = {
                "last_offset": last,
                "windows": window_ranges,
            }
        return result

    # -- whole run ----------------------------------------------------------

    def run(
        self,
        entity_filter: list[str] | None = None,
        mode: str = "both",
        up_to_window: str | None = None,
        report_path: Path | None = None,
    ) -> StreamRunResult:
        if mode not in MODES:
            raise StreamConfigError(
                f"mode must be one of {', '.join(MODES)}; got {mode!r}"
            )
        if entity_filter:
            unknown = [name for name in entity_filter if name not in self.config.entities]
            if unknown:
                raise StreamConfigError(
                    f"unknown entity/ies: {', '.join(unknown)} -- configured: "
                    f"{', '.join(self.config.entities)}"
                )
        check_containment(
            self.config.output_root,
            (self.config.journal_root, self.config.snapshot_root),
            "stream output_root",
        )
        if not self.config.journal_root.is_dir():
            raise StreamError(
                f"journal root {self.config.journal_root.as_posix()} not found "
                f"-- run cdc_main.py at least once (initial load) first"
            )

        per_entity: dict[str, dict[str, object]] = {}
        events_total = 0
        ok = True
        selected = (
            [self.config.entities[name] for name in entity_filter]
            if entity_filter
            else list(self.config.entities.values())
        )
        for entity_cfg in selected:
            summary = self._run_entity(entity_cfg, mode, up_to_window)
            per_entity[entity_cfg.entity] = summary
            if summary["status"] == "ok":
                events = summary["events"]
                events_total += int(events["total"])
                for check in summary["replay_checks"]:
                    if check["match"] is False:
                        ok = False

        report = {
            "report_version": REPORT_VERSION,
            "milestone": "6c",
            "mode": mode,
            "ok": ok,
            "entities": per_entity,
        }
        target = report_path or (self.config.output_root / "report.json")
        _atomic_write(target, json.dumps(report, indent=2) + "\n", self.log)
        return StreamRunResult(
            mode=mode,
            ok=ok,
            events_total=events_total,
            per_entity=per_entity,
        )



