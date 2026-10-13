"""Parquet writer - columnar output with native, pinned column types (ADR-0018).

The Arrow schema is derived from the first record's dataclass annotations,
so the dataclass stays the single schema contract (ADR-0007):
int -> int64, str -> string, date -> date32[day], Decimal -> decimal128(12, 2).
Pinning prevents pyarrow from inferring decimal precision from the values
present, which would change the column type whenever generation
parameters change (schema drift).
"""

import logging
import typing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.writers.base import Writer
from src.writers.serialization import native_record

logger = logging.getLogger(__name__)

# Warehouse money contract: DECIMAL(12, 2) (ADR-0002).
_MONEY_PRECISION = 12
_MONEY_SCALE = 2

_ARROW_TYPE_BY_FIELD: dict[type, pa.DataType] = {
    int: pa.int64(),
    str: pa.string(),
    date: pa.date32(),
    Decimal: pa.decimal128(_MONEY_PRECISION, _MONEY_SCALE),
}


def arrow_schema_for(record_class: type) -> pa.Schema:
    """Derive a pinned Arrow schema from a dataclass's field annotations."""
    hints = typing.get_type_hints(record_class)
    unknown = {t for t in hints.values() if t not in _ARROW_TYPE_BY_FIELD}
    if unknown:
        raise ValueError(
            f"Unsupported field types for Parquet output: {sorted(map(str, unknown))}"
        )
    return pa.schema([
        (name, _ARROW_TYPE_BY_FIELD[field_type])
        for name, field_type in hints.items()
    ])


class ParquetWriter(Writer[BinaryIO]):
    """Writes records as a Parquet table with native, pinned column types."""

    def _open_file(self, file_path: Path) -> BinaryIO:
        return file_path.open("wb")

    def _write_records(self, handle: BinaryIO, records: Iterator[Any]) -> int:
        records = list(records)  # base class guarantees at least one record
        schema = arrow_schema_for(type(records[0]))
        frame = pd.DataFrame([native_record(record) for record in records])
        table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        pq.write_table(table, handle)
        return len(records)