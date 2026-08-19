"""Logging setup using only the standard library."""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "reversible"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the ``reversible`` logger with a simple stream handler.

    Messages are printed as-is: tags such as ``[EXEC]`` / ``[LOG ]`` are
    embedded in the message text by the runtime itself.
    """
    logger = get_logger()
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
