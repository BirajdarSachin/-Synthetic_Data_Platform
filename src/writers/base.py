"""Writer interface - the contract every output format implements (ADR-0017).

write() is a template method: the base class owns the file lifecycle
(empty-stream rejection, overwrite logging, locked-file error message,
count/bytes logging) so every format behaves identically; subclasses
implement only the format-specific record writing.

Generators never know about writers; main.py selects a Writer via the
factory in src/writers/__init__.py.
"""

import logging
from abc import ABC, abstractmethod
from itertools import chain
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

logger = logging.getLogger(__name__)


class Writer(ABC):
    """Writes dataclass records to one file in one specific format.

    Contract (ADR-0017):
      * write() consumes the record stream lazily - memory stays flat (ADR-0005).
      * Returns the number of records written.
      * Raises ValueError on an empty stream - never write a silent file.
      * Raises PermissionError with an actionable message when the target
        file is locked by another program.
      * Cell serialization rules come from serialization.py - identical
        across all formats (ADR-0002, ADR-0003).
    """

    def write(self, file_path: Path, records: Iterable[Any]) -> int:
        """Template method: validate, open, delegate, log. Returns the count."""
        iterator = iter(records)
        try:
            first = next(iterator)
        except StopIteration:
            raise ValueError(f"No records to write to {file_path}") from None

        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists():
            # ADR-0013: overwrite is intentional (rebuild-in-place) - never silent.
            logger.info("[WRITER] overwriting existing file=%s", file_path)

        try:
            handle = self._open_file(file_path)
        except PermissionError as exc:
            raise PermissionError(
                f"Output file is locked and cannot be written: {file_path}. "
                "It is most likely open in another program (Excel, LibreOffice, "
                "Power BI). Close that program and run again."
            ) from exc

        with handle:
            count = self._write_records(handle, chain([first], iterator))

        logger.info("[WRITER] file=%s records=%s bytes=%s",
                    file_path, count, file_path.stat().st_size)
        return count

    def _open_file(self, file_path: Path) -> TextIO:
        """Hook: how the format opens its text handle. CSV overrides this."""
        return file_path.open("w", encoding="utf-8", newline="\n")

    @abstractmethod
    def _write_records(self, handle: TextIO, records: Iterator[Any]) -> int:
        """Format-specific: write every record (including the first) to the
        open handle. Returns the record count."""
