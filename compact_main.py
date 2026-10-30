"""compact_main.py -- topic compaction entry point (Milestone 6d, ADR-031).

Folds the 6c stream logs (Milestone 6c, ADR-030) into the compacted,
latest-wins-per-key view under data/streams_compacted/ and verifies it
against the snapshot of each log's last window.

Usage (from the project root, venv Style B):

    .venv\\Scripts\\python.exe compact_main.py
    .venv\\Scripts\\python.exe compact_main.py --entity order

Default: every configured entity. Exit codes (house contract): 0 ok,
1 failure (config, stream-log integrity, or state mismatch),
130 interrupted (Ctrl+C).

Log contract (ADR-0009): same tags as every run, plus [COMPACT] (new in 6d,
documented in ADR-031). Report defaults to
data/streams_compacted/report.json.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.core.stream.compact import CompactConfig, CompactRunner
from src.core.stream.replay import StreamError


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compact the 6c stream logs to latest-wins-per-key (Milestone 6d)."
    )
    parser.add_argument(
        "--entity",
        action="append",
        default=None,
        help="run only this entity (repeatable; default: all configured)",
    )
    parser.add_argument(
        "--config", default="configs/compact_base.yaml", help="compaction config"
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
        f"config={args.config}"
        + (f" entities={','.join(args.entity)}" if args.entity else " entities=all"),
    )

    config_path = Path(args.config)
    if not config_path.is_file():
        log("CONFIG", f"compaction config not found: {config_path}")
        return 1
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = CompactConfig.load(raw)
    log(
        "CONFIG",
        f"stream_root={config.stream_root.as_posix()} "
        f"snapshot_root={config.snapshot_root.as_posix()} "
        f"output_root={config.output_root.as_posix()}",
    )
    log("CONFIG", "entities: " + ", ".join(config.entities))
    log(
        "CONFIG",
        f"options: drop_tombstones={config.drop_tombstones} "
        f"verify_state={config.verify_state}",
    )

    runner = CompactRunner(config, log)
    report = runner.run(
        entity_filter=args.entity,
        report_path=Path(args.report) if args.report else None,
    )

    for entity, summary in report["entities"].items():
        if summary["status"] == "skipped":
            log("SUMMARY", f"{entity}: skipped ({summary['reason']})")
            continue
        check = summary["state_check"]
        log(
            "SUMMARY",
            f"{entity}: records={summary['records']} retained={summary['retained']} "
            f"live={summary['live_keys']} tombstones={summary['tombstones']} "
            f"last_window={summary['last_window'] or 'n/a'} "
            f"state=[{check['window'] or 'n/a'}="
            f"{'ok' if check['match'] else 'MISMATCH' if check['match'] is False else 'unverified'}"
            f"]",
        )
    log("SUMMARY", f"ok={report['ok']} entities={len(report['entities'])}")
    if report["ok"]:
        log("SUMMARY", "result as expected (exit 0)")
        return 0
    log(
        "SUMMARY",
        "state mismatch detected -- the compacted view and the snapshot tree "
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
        log("COMPACT", f"failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
