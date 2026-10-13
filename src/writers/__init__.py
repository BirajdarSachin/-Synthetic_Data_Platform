"""Writers - serialize records to output formats. Only place that knows formats.

The factory is the single point where an output format string maps to a
Writer implementation (ADR-0017). Adding a format = implement Writer +
one registry entry here. main.py never imports concrete writers.
"""

from typing import Any

from src.writers.base import Writer
from src.writers.csv_writer import CsvWriter
from src.writers.json_writer import JsonWriter
from src.writers.parquet_writer import ParquetWriter

_WRITERS: dict[str, type[Writer[Any]]] = {
    "csv": CsvWriter,
    "json": JsonWriter,
    "parquet": ParquetWriter,
}


def get_writer(format_name: str) -> Writer[Any]:
    """Return a new writer instance for the given format name."""
    try:
        return _WRITERS[format_name]()
    except KeyError:
        raise ValueError(
            f"Unsupported output format: {format_name!r}. "
            f"Supported: {sorted(_WRITERS)}"
        ) from None
