"""cdc_main.py -- CDC change stream entry point (Milestone 6b, ADR-029).

Diffs consecutive windows of the configured entities in one output tree and
writes the CDC change log (JSONL) under data/cdc/.

Usage (from the project root, venv Style B):

    .venv\\Scripts\\python.exe cdc_main.py --window 2026-01-15
    .venv\\Scripts\\python.exe cdc_main.py --window 2026-01-15 --prev-window 2026-01-14

The first run against a tree is the INITIAL LOAD (every row becomes an
INSERT event, prev_window=null). Later runs auto-pair --window with the
latest earlier window unless --prev-window says otherwise.

Exit codes (house contract):
    0    ok      -- change log + report written, ledger identity holds
    1    failure -- config error, missing window/snapshot, or broken layout
    130  interrupted (Ctrl+C)

Log contract (ADR-0009): same tags as every run, plus [CDC] (new in 6b,
documented in ADR-029). Report defaults to data/cdc/report.json (atomic).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.core.cdc.differ import CdcConfigError
from src.core.cdc.events import CdcError
from src.core.cdc.runner import CdcConfig, CdcRunner


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit the CDC change log between two windows (Milestone 6b)."
    )
    parser.add_argument(
        "--window",
        required=True,
        help="partition value of the CURRENT window, e.g. 2026-01-15 (or 2026-01 for monthly)",
    )
    parser.add_argument(
        "--prev-window",
        default=None,
        help="partition value of the PREVIOUS window (default: auto = latest earlier window; none -> initial load)",
    )
    parser.add_argument("--config", default="configs/cdc_base.yaml", help="CDC config (default: configs/cdc_base.yaml)")
    parser.add_argument("--report", default=None, help="report path (default: <output_root>/report.json)")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log("STARTUP", f"cwd={Path.cwd()}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    log(
        "CONFIG",
        f"config={args.config} window={args.window}"
        + (f" prev={args.prev_window} (explicit)" if args.prev_window else " prev=auto"),
    )

    config_path = Path(args.config)
    if not config_path.is_file():
        log("CONFIG", f"CDC config not found: {config_path}")
        return 1
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = CdcConfig.load(raw)
    log("CONFIG", f"source_tree={config.source_tree.as_posix()} output_root={config.output_root.as_posix()}")
    log("CONFIG", "entities: " + ", ".join(config.entities))
    log(
        "CONFIG",
        f"options: before_images={config.before_images} "
        f"allow_schema_drift={config.allow_schema_drift} "
        f"initial_load={config.initial_load}",
    )

    runner = CdcRunner(config, log)
    result = runner.run(
        args.window,
        prev_window=args.prev_window,
        report_path=Path(args.report) if args.report else None,
    )

    for entity, ledger in result.per_entity.items():
        log(
            "SUMMARY",
            f"{entity}: prev={ledger['prev_window'] or 'initial'} "
            f"rows {ledger['rows_prev']}->{ledger['rows_curr']} "
            f"inserts={ledger['inserts']} updates={ledger['updates']} "
            f"deletes={ledger['deletes']} unchanged={ledger['unchanged']}",
        )
    log("SUMMARY", f"ok={result.ok} entities={len(result.per_entity)} events={result.events_total}")
    log("SUMMARY", "result as expected (exit 0)")
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C)")
        return 130
    except CdcError as exc:
        # CdcConfigError (config/semantics) and any internal CdcError both
        # exit 1 with the message as the diagnostic -- one handler, because
        # the operator-facing contract is the same: loud, one-line, guided.
        log("CDC", f"failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
