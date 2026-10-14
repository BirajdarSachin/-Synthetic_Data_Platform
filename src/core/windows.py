"""Window engine - deterministic time windows for partitioned generation.

A window is identified by a stable string KEY derived directly from a
record's business date (window_key_for) - no search, deterministic by
construction (ADR-0024):
  daily   - key "YYYY-MM-DD", one window per day
  monthly - key "YYYY-MM",    one window per calendar month

Design (ADR-0024): entities are generated as ONE deterministic stream and
BUCKETED into windows at the write boundary. This keeps primary keys
globally sequential across all window files for free (ADR-0001: the union
of window files is exactly 1..N) and preserves every ADR-0016 temporal
rule across windows (order_date >= registration_date implies the order's
window is >= the customer's registration window). Per-window independent
GENERATION is deferred to Phase 7.
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

GRANULARITIES = ("daily", "monthly")
MODES = ("full", "latest")


def window_key_for(value: date, granularity: str) -> str:
    """Derive the window key of a business date."""
    if granularity == "daily":
        return value.isoformat()
    if granularity == "monthly":
        return f"{value.year:04d}-{value.month:02d}"
    raise ValueError(f"Unsupported granularity: {granularity!r}")


def compute_window_keys(reference_date: date, history_days: int,
                        granularity: str, mode: str) -> list[str]:
    """Compute the deterministic, chronologically sorted window-key list.

    mode="full": every window from (reference_date - history_days) through
    reference_date. mode="latest": only the window containing
    reference_date (the incremental daily/monthly file shape).
    """
    if granularity not in GRANULARITIES:
        raise ValueError(
            f"granularity must be one of {GRANULARITIES}, got {granularity!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "latest":
        return [window_key_for(reference_date, granularity)]
    start = date.fromordinal(reference_date.toordinal() - history_days)
    if granularity == "daily":
        return [date.fromordinal(start.toordinal() + offset).isoformat()
                for offset in range(history_days + 1)]
    keys: list[str] = []
    current = date(start.year, start.month, 1)
    while current <= reference_date:
        keys.append(window_key_for(current, "monthly"))
        current = _next_month(current)
    return keys


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12),
                1 if value.month == 12 else value.month + 1, 1)
