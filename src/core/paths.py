"""Path resolution helpers. Contract: ADR-0010.

All relative paths from configuration are resolved against the project root
(the directory containing src/), so a run behaves identically no matter which
working directory it was started from. The cwd is still logged at startup for
forensics (ADR-0009).
"""

from pathlib import Path


def get_project_root() -> Path:
    """Return the project root directory (the parent of src/)."""
    return Path(__file__).resolve().parents[2]


def resolve_path(path: Path) -> Path:
    """Resolve a possibly-relative path against the project root."""
    return path if path.is_absolute() else get_project_root() / path
