"""
Central logging configuration for the platform. Contract: ADR-0009.

Rules enforced by this module:
  * Logging is configured exactly once per process (idempotent by design).
  * Console handler: INFO  - the human-readable run story.
  * File handler:    DEBUG - full detail, one file per run: logs/run_<run_id>.log
  * All modules log via logging.getLogger(__name__) - never print().

Message convention: "[STAGE] message key=value key=value"
"""

import logging
import platform
import sys
from datetime import datetime
from pathlib import Path

from src import __version__

LOG_DIR_NAME = "logs"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Module-level state so configure_logging() is idempotent (see rule 1).
_run_id: str | None = None
_log_file: Path | None = None


def configure_logging(log_dir: Path | None = None) -> Path:
    """Configure console + per-run file logging and write the startup block.

    Args:
        log_dir: Directory for the run log. Defaults to ``<cwd>/logs``.

    Returns:
        Path of the log file written for this run.
    """
    global _run_id, _log_file
    if _log_file is not None:
        # Already configured in this process. Return without adding handlers
        # again - otherwise every message would be emitted twice.
        return _log_file

    _run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    resolved_log_dir = log_dir if log_dir is not None else Path.cwd() / LOG_DIR_NAME
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = resolved_log_dir / f"{_run_id}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(_log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    _log_startup_block()
    return _log_file


def get_run_id() -> str:
    """Return this run's id (log file stem; future manifest correlation key)."""
    if _run_id is None:
        raise RuntimeError("Logging not configured. Call configure_logging() first.")
    return _run_id


def get_log_file() -> Path:
    """Return the log file path for this run."""
    if _log_file is None:
        raise RuntimeError("Logging not configured. Call configure_logging() first.")
    return _log_file


def _log_startup_block() -> None:
    """Log the environment facts that every run should record (ADR-0009)."""
    logger = logging.getLogger(__name__)
    logger.info("[STARTUP] synthetic data platform starting")
    logger.info("[STARTUP] run_id=%s", _run_id)
    logger.info("[STARTUP] version=%s", __version__)
    logger.info("[STARTUP] python=%s on %s", platform.python_version(), platform.system())
    logger.info("[STARTUP] cwd=%s", Path.cwd())
    logger.info("[STARTUP] log_file=%s", _log_file)
