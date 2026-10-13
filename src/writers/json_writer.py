"""JSON writer - NDJSON: one JSON object per line, UTF-8 (ADR-0017).

Why NDJSON instead of one JSON array: line-oriented output streams with flat
memory, is read line by line by Spark / pandas / streaming consumers, and a
truncated file is still parseable up to the last complete line. A single
array forces whole-file parsing - the wrong shape for data pipelines.
"""

import json
import logging
from pathlib import Path
from typing import Any, Iterator, TextIO

from src.writers.base import Writer
from src.writers.serialization import serialize_record

logger = logging.getLogger(__name__)


class JsonWriter(Writer[TextIO]):
    """Writes records as newline-delimited JSON."""

    def _write_records(self, handle: TextIO, records: Iterator[Any]) -> int:
        count = 0
        for record in records:
            # ensure_ascii=False: real UTF-8 characters (fidelity); file is UTF-8.
            handle.write(json.dumps(serialize_record(record), ensure_ascii=False))
            handle.write("\n")
            count += 1
        return count
