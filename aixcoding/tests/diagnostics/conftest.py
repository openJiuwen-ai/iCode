# (tests — asserts are pytest idiom, Chinese prose is the data)
"""Shared fixtures for diagnostics tests: logging state isolation."""

from __future__ import annotations

import logging

import pytest

AIX = "aixcoding"
DETAIL = "aixcoding.detail"


@pytest.fixture
def aix_logger_state():
    """Isolate the global ``aixcoding`` and ``aixcoding.detail`` loggers.

    The detail subtree (ADR-0017) carries its own level/propagate state and
    the ``_DetailMarker`` sink; both must be saved and restored so tests that
    call ``setup_logging`` cannot leak isolation or sinks into each other.
    Handlers a test attached (and that are not part of the saved state) are
    closed on teardown — file sinks hold streams open (delay=True) and would
    keep temp files locked on Windows otherwise (review S2).
    """
    logger = logging.getLogger(AIX)
    detail = logging.getLogger(DETAIL)
    saved = (logger.handlers[:], logger.propagate, logger.level)
    saved_detail = (detail.handlers[:], detail.propagate, detail.level)
    logger.handlers.clear()
    detail.handlers.clear()
    yield logger

    def _restore(target: logging.Logger, target_saved: tuple) -> None:
        for handler in target.handlers[:]:
            if handler not in target_saved[0]:
                handler.close()  # close only what this test attached
        target.handlers[:] = target_saved[0]
        target.propagate, target.level = target_saved[1], target_saved[2]

    _restore(logger, saved)
    _restore(detail, saved_detail)
