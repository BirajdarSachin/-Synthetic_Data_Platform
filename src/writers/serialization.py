"""Shared record serialization policies for text and typed output formats.

CSV/JSON use text serialization; typed formats such as Parquet preserve
native values so dates and Decimal money retain their logical types.
"""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any


def serialize_cell(value: Any) -> Any:
    """Convert one field value to its output representation."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return value


def serialize_record(record: Any) -> dict[str, Any]:
    """Convert a dataclass record to text-format output values."""
    return {
        key: serialize_cell(value)
        for key, value in asdict(record).items()
    }


def native_record(record: Any) -> dict[str, Any]:
    """Convert a dataclass record to a dict while preserving native field types."""
    return asdict(record)
