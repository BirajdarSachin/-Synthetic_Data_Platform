"""CSV writer - turns dataclass records into a CSV file (ADR-0004, ADR-0005).

A shared Writer interface (ABC) will be introduced in Phase 3, when a second
format exists - not before. Single-implementation ABCs are premature.
"""

import csv
import logging
from dataclasses import asdict, fields
from datetime import date
from itertools import chain
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def write_csv(file_path: Path, records: Iterable[Any]) -> int:
    """Write dataclass records to file_path as CSV. Returns the record count.

    Raises ValueError when records is empty - we never write a silent,
    header-only file.
    """
    iterator = iter(records)
    try:
        first = next(iterator)
    except StopIteration:
        raise ValueError(f"No records to write to {file_path}") from None

    fieldnames = [f.name for f in fields(first)]
    file_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    # newline="" is required by the csv module to avoid blank lines on Windows.
    with file_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in chain([first], iterator):
            writer.writerow(_to_row(record))
            count += 1

    logger.info("[WRITER] file=%s records=%s bytes=%s",
                file_path, count, file_path.stat().st_size)
    return count


def _to_row(record: Any) -> dict[str, Any]:
    """Convert a dataclass to a CSV-ready dict (dates -> ISO-8601 strings)."""
    row = asdict(record)
    return {key: (value.isoformat() if isinstance(value, date) else value)
            for key, value in row.items()}
