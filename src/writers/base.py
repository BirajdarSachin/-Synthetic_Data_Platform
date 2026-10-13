"""Writer interface - the contract every output format implements (ADR-0017).

write() is a template method: the base class owns the file lifecycle
(empty-stream rejection, overwrite logging, locked-file error message,
count/bytes logging) so every format behaves identically; subclasses
implement only the format-specific record writing.

Atomic writes (ADR-0020): every write targets a temporary file in the SAME
directory as the final file, then os.replace() moves it into place. Readers
never observe a partially written file and a failed write leaves no residue.
Consequence: a locked FINAL file now fails at replace() time, not at open()
time - the PermissionError handler below covers both steps.

Generators never know about writers; main.py selects a Writer via the
factory in src/writers/__init__.py.
"""

import logging
import os
from abc import ABC, abstractmethod
from itertools import chain
from pathlib import Path
from typing import Any, BinaryIO, Generic, Iterable, Iterator, TextIO, TypeVar, cast

logger = logging.getLogger(__name__)

Handle = TypeVar("Handle", TextIO, BinaryIO)

_TEMP_SUFFIX = ".tmp"


def _remove_quietly(path: Path) -> None:
    """Best-effort temp cleanup - never masks the original failure."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("[WRITER] could not remove temp file=%s", path)


class Writer(ABC, Generic[Handle]):
    """Writes dataclass records to one file in one specific format.

    Contract (ADR-0017, ADR-0020):
      * write() consumes the record stream lazily - memory stays flat (ADR-0005).
      * Returns the number of records written.
      * Raises ValueError on an empty stream - never write a silent file.
      * Raises PermissionError with an actionable message when the target
        file is locked by another program (temp open or final replace).
      * Writes are atomic: the final file appears only on full success.
    """

    def write(self, file_path: Path, records: Iterable[Any]) -> int:
        """Template method: validate, write temp, atomically replace, log."""
        iterator = iter(records)
        try:
            first = next(iterator)
        except StopIteration:
            raise ValueError(f"No records to write to {file_path}") from None

        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists():
            # ADR-0013: overwrite is intentional (rebuild-in-place) - never silent.
            logger.info("[WRITER] overwriting existing file=%s", file_path)

        # Temp in the SAME directory: os.replace() is atomic per volume (ADR-0020).
        temp_path = file_path.with_name(file_path.name + _TEMP_SUFFIX)
        try:
            with self._open_file(temp_path) as handle:
                count = self._write_records(handle, chain([first], iterator))
            os.replace(temp_path, file_path)
        except PermissionError as exc:
            _remove_quietly(temp_path)
            raise PermissionError(
                f"Output file is locked and cannot be written: {file_path}. "
                "It is most likely open in another program (Excel, LibreOffice, "
                "Power BI). Close that program and run again."
            ) from exc
        except BaseException:
            # Any other failure (write error, Ctrl+C): no temp residue, re-raise.
            _remove_quietly(temp_path)
            raise

        logger.info("[WRITER] file=%s records=%s bytes=%s",
                    file_path, count, file_path.stat().st_size)
        return count

    def _open_file(self, file_path: Path) -> Handle:
        """Hook: open a format-specific text or binary output handle."""
        # The default opens a text handle; binary formats override this hook.
        return cast(Handle, file_path.open("w", encoding="utf-8", newline="\n"))

    @abstractmethod
    def _write_records(self, handle: Handle, records: Iterator[Any]) -> int:
        """Format-specific: write every record (including the first) to the
        open handle. Returns the record count."""
