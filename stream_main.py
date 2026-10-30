"""stream_main.py -- event stream entry point (Milestone 6c, ADR-030).

Folds the CDC journals (Milestone 6b) back into state and verifies the
replay against the platform's snapshot trees, then exports each entity's
events as one append-only, offset-addressed stream under data/streams/.

Usage (from the project root, venv Style B):

    .venv\\Scripts\\python.exe stream_main.py
    .venv\\Scripts\\python.exe stream_main.py --entity order
    .venv\\Scripts\\python.exe stream_main.py --mode replay
    .venv\\Scripts\\python.exe stream_main.py --up-to-window 2026-01-15

Default: mode=both, every configured entity -- verify every folded window
prefix against its snapshot AND write the stream files. Exit codes (house
contract): 0 ok, 1 failure (config, journal chain, or replay mismatch),
130 interrupted (Ctrl+C).

Log contract (ADR-0009): same tags as every run, plus [STREAM] (new in 6c,
documented in ADR-030). Report defaults to data/streams/report.json.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.core.stream.replay import StreamError
from src.core.stream.runner import MODES, StreamConfig, StreamRunner


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay + export the CDC journals as an event stream (Milestone 6c)."
    )
    parser.add_argument(
        "--entity",
        action="append",
        default=None,
        help="run only this entity (repeatable; default: all configured)",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="both",
        help="replay = verify only; export = fold + write stream files, no "
        "verification; both = default (verify + export)",
    )
    parser.add_argument(
        "--up-to-window",
        default=None,
        help="fold/verify only the chain prefix up to this window",
    )
    parser.add_argument(
        "--config", default="configs/stream_base.yaml", help="stream config"
    )
    parser.add_argument(
        "--report",
        default=None,
        help="report path (default: <output_root>/report.json)",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log("STARTUP", f"cwd={Path.cwd()}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    log(
        "CONFIG",
        f"config={args.config} mode={args.mode}"
        + (f" entities={','.join(args.entity)}" if args.entity else " entities=all")
        + (f" up_to_window={args.up_to_window}" if args.up_to_window else ""),
    )

    config_path = Path(args.config)
    if not config_path.is_file():
        log("CONFIG", f"stream config not found: {config_path}")
        return 1
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = StreamConfig.load(raw)
    log(
        "CONFIG",
        f"journal_root={config.journal_root.as_posix()} "
        f"snapshot_root={config.snapshot_root.as_posix()} "
        f"output_root={config.output_root.as_posix()}",
    )
    log("CONFIG", "entities: " + ", ".join(config.entities))
    log(
        "CONFIG",
        f"options: verify_replay={config.verify_replay} byte_compare={config.byte_compare}",
    )

    runner = StreamRunner(config, log)
    result = runner.run(
        entity_filter=args.entity,
        mode=args.mode,
        up_to_window=args.up_to_window,
        report_path=Path(args.report) if args.report else None,
    )

    for entity, summary in result.per_entity.items():
        if summary["status"] == "skipped":
            log("SUMMARY", f"{entity}: skipped ({summary['reason']})")
            continue
        events = summary["events"]
        checks = summary["replay_checks"]
        verified = ", ".join(
            f"{check['window']}={'ok' if check['match'] else 'MISMATCH' if check['match'] is False else 'unverified'}"
            for check in checks
        )
        export = summary.get("export")
        tail = (
            f" export=offsets 1..{export['last_offset']}" if export else " export=none"
        )
        log(
            "SUMMARY",
            f"{entity}: chain {' -> '.join(summary['chain'])} "
            f"events={events['total']} "
            f"({events['INSERT']}I/{events['UPDATE']}U/{events['DELETE']}D) "
            f"replay=[{verified or 'n/a'}]{tail}",
        )
    log("SUMMARY", f"ok={result.ok} entities={len(result.per_entity)} events={result.events_total}")
    if result.ok:
        log("SUMMARY", "result as expected (exit 0)")
        return 0
    log(
        "SUMMARY",
        "replay mismatch detected -- the journal chain and the snapshot tree "
        "disagree; see report.json for the window and sample keys (exit 1)",
    )
    return 1


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C)")
        return 130
    except StreamError as exc:
        log("STREAM", f"failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
