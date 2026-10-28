"""dq_main.py -- DQ harness entry point (Milestone 7a, ADR-025).

Validates ONE output tree (data/generated or data/quality) against a DQ config.

Usage (from the project root, venv Style B):

    .venv\\Scripts\\python.exe dq_main.py --tree data\\generated --mode clean
    .venv\\Scripts\\python.exe dq_main.py --tree data\\quality   --mode corrupt

Exit codes (house contract):
    0    ok      -- clean: zero violations | corrupt: at least one check fired
    1    failure -- config error, missing tree/files, or expectation not met
    130  interrupted (Ctrl+C)

Log contract (ADR-0009): same tags as every run, plus [DQ] (new in 7a,
documented in ADR-025). Report defaults to data/dq/report.json (atomic write).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.core.dq.checks import DqConfigError
from src.core.dq.runner import DqConfig, DqRunner, write_report


def log(tag: str, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{tag}] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DQ checks over one output tree (Milestone 7a)."
    )
    parser.add_argument("--tree", required=True, help="output tree to validate, e.g. data/generated or data/quality")
    parser.add_argument("--config", default="configs/dq_base.yaml", help="DQ config (default: configs/dq_base.yaml)")
    parser.add_argument(
        "--mode",
        choices=("clean", "corrupt"),
        default="clean",
        help="clean: all checks must pass | corrupt: at least one check must fire",
    )
    parser.add_argument("--report", default="data/dq/report.json", help="where to write the JSON report")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log("STARTUP", f"cwd={Path.cwd()}")
    log("STARTUP", f"python={sys.version.split()[0]}")
    log("CONFIG", f"tree={Path(args.tree).as_posix()} config={args.config} mode={args.mode}")

    config_path = Path(args.config)
    if not config_path.is_file():
        log("CONFIG", f"DQ config not found: {config_path}")
        return 1
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = DqConfig.load(raw)
    log(
        "CONFIG",
        "checks enabled: " + ", ".join(name for name, on in config.enabled.items() if on),
    )
    log("CONFIG", "entities: " + ", ".join(config.entities))

    report = DqRunner(Path(args.tree), config, args.mode, log).run()
    write_report(report, Path(args.report), log)

    log("SUMMARY", f"ok={report.ok} violations={report.violations_total} rows={report.rows_checked}")
    if report.ok:
        log("SUMMARY", "result as expected (exit 0)")
        return 0
    log("SUMMARY", f"result NOT as expected for mode={args.mode} (exit 1)")
    return 1


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        log("SUMMARY", "interrupted by user (Ctrl+C)")
        return 130
    except DqConfigError as exc:
        log("CONFIG", f"invalid DQ configuration: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
