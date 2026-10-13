"""CSV writer - Writer implementation for comma-separated output (ADR-0004).

The header comes from the first record's dataclass field order - field order
IS the column order (ADR-0004). Cell values come from serialization.py
(ADR-0002: money as 2dp strings; ADR-0003: dates as ISO-8601).
"""

import csv
import logging
from pathlib import Path
from typing import Any, Iterator, TextIO

from src.writers.base import Writer
from src.writers.serialization import serialize_record

logger = logging.getLogger(__name__)


class CsvWriter(Writer[TextIO]):
    """Writes records as CSV: header + one row per record, UTF-8."""

    def _open_file(self, file_path: Path) -> TextIO:
        # newline="" is required by the csv module to avoid blank lines on Windows.
        return file_path.open("w", encoding="utf-8", newline="")

    def _write_records(self, handle: TextIO, records: Iterator[Any]) -> int:
        writer: csv.DictWriter | None = None
        count = 0
        for record in records:
            row = serialize_record(record)
            if writer is None:
                # First record fixes the header: field order = column order.
                writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
                writer.writeheader()
            writer.writerow(row)
            count += 1
        return count
