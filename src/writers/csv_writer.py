"""CSV writer - turns dataclass records into a CSV file (ADR-0004, ADR-0005).

Serialization rules (the writer is the only place that knows formats):
  * date          -> ISO-8601 string
  * Decimal money -> string with exactly 2 decimal places (ADR-0002)
Overwrite policy ADR-0013: rebuilding an existing dataset overwrites its file,
and says so in the log.
"""

import csv
import logging
from dataclasses import asdict, fields
from datetime import date
from decimal import Decimal
from itertools import chain
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def write_csv(file_path: Path, records: Iterable[Any]) -> int:
    """Write dataclass records to file_path as CSV. Returns the record count.

    Raises ValueError when records is empty - we never write a silent,
    header-only file.
    Raises PermissionError with an actionable message when the target file
    is locked by another program (e.g. Excel holding a previous run's
    output open).
    """
    iterator = iter(records)
    try:
        first = next(iterator)
    except StopIteration:
        raise ValueError(f"No records to write to {file_path}") from None

    fieldnames = [f.name for f in fields(first)]
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if file_path.exists():
        # ADR-0013: overwrite is intentional (rebuild-in-place) - but never silent.
        logger.info("[WRITER] overwriting existing file=%s", file_path)

    count = 0
    try:
        # newline="" is required by the csv module to avoid blank lines on Windows.
        handle = file_path.open("w", encoding="utf-8", newline="")
    except PermissionError as exc:
        raise PermissionError(
            f"Output file is locked and cannot be written: {file_path}. "
            "It is most likely open in another program (Excel, LibreOffice, "
            "Power BI). Close that program and run again."
        ) from exc

    with handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in chain([first], iterator):
            writer.writerow(_to_row(record))
            count += 1

    logger.info("[WRITER] file=%s records=%s bytes=%s",
                file_path, count, file_path.stat().st_size)
    return count


def _to_row(record: Any) -> dict[str, Any]:
    """Convert a dataclass to a CSV-ready dict (dates and decimals to strings)."""
    row = asdict(record)
    return {
        key: _to_cell(value)
        for key, value in row.items()
    }


def _to_cell(value: Any) -> Any:
    """Serialize one cell value according to the writer's format rules."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return value
