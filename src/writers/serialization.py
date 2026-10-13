"""Shared record serialization - the single source of truth for cell values.

Every writer converts dataclass field values through these functions, so a
date or a monetary amount looks the same in CSV and JSON
(ADR-0002: Decimal money as 2dp strings; ADR-0003: dates as ISO-8601).
Adding a new value type HERE updates every format at once.
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
    """Convert a dataclass record to a dict of output-ready values."""
    return {
        key: serialize_cell(value)
        for key, value in asdict(record).items()
    }
