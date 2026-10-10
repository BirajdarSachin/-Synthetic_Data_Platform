"""
Entry point of the synthetic data platform.

Milestone 1: proves the skeleton runs and logging works.
No business logic lives here - ever. It only wires stages together.
"""

import logging

from src.core.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Run the platform. (Currently a logging smoke test.)"""
    configure_logging()
    logger.info("[STARTUP] main.py executed - skeleton is runnable")


if __name__ == "__main__":
    main()
