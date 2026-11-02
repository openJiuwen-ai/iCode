# ruff: noqa: T201
# (stderr notes are the observable-degradation channel, DEVELOPMENT.md §9)
"""``aixcoding.*`` logger factory: rotating file sinks, panel bubble opt-in.

Design: aixcoding/docs/batches/diagnostics/design.md (signed off 2026-10-25);
detail channel: aixcoding/docs/batches/detail-logging/design.md and ADR-0017
(signed off 2026-11-02).

Layout: the main ``_Marker`` sink attaches to the top-level ``aixcoding``
logger — child loggers (``aixcoding.telemetry``…) propagate to it by standard
logging rules. The detail ``_DetailMarker`` sink attaches to the
non-propagating ``aixcoding.detail`` subtree (ADR-0017 revised the original
"top-level logger only" wording): its records land in the dedicated
``aixcoding.detail.log`` volume and nowhere else — never the F6 panel, the
upstream debug log, or the main volume. ``propagate`` on the top-level logger
remains the F6-panel switch: ``panel=true`` lets records bubble further to the
root logger where the upstream ``_TuiLogHandler`` picks them up (its whitelist
derives from ``MODULE_ORDER`` in ``app/tui/screens/logs/screen.py``);
``panel=false`` keeps records off the UI while the file sink stays active.
Anyone importing this module gets the feature.

Both sinks follow the six invariants (ADR-0017): each is ensured on every
setup call, one failing never blocks the other, a missing sink is retried on
the next call, neither is attached twice, a failed detail sink leaves the
subtree isolated (records dropped, stderr note, never leaked), and the detail
subtree is isolated (``propagate=False``) before its level is lowered.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DiagnosticsConfig

ROOT_LOGGER_NAME = "aixcoding"
LOG_DIR_NAME = "aixcoding-logs"
LOG_FILE_NAME = "aixcoding.log"
DETAIL_LOGGER_NAME = "aixcoding.detail"
DETAIL_FILE_NAME = "aixcoding.detail.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3
FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LEVEL_NAME = "DETAIL"

#: Log level of the file-only detail channel (``aixcoding.detail`` subtree,
#: ADR-0017): below DEBUG so its records can never reach the F6 panel handler.
#: ``auth/session.py`` mirrors this value locally — kernel cross-subpackage
#: imports are forbidden (test_layering); a test pins both definitions.
DETAIL_LEVEL = 5


class _Marker(RotatingFileHandler):
    """Type tag so repeated setup calls can find and reuse our handler."""


class _DetailMarker(RotatingFileHandler):
    """Type tag for the detail-volume sink (semantics + test identity)."""


def get_logger(child: str = "") -> logging.Logger:
    """Named logger inside the aixcoding subtree (e.g. ``get_logger("telemetry")``)."""
    name = ROOT_LOGGER_NAME if not child else f"{ROOT_LOGGER_NAME}.{child}"
    return logging.getLogger(name)


def _attach_sink(
    logger: logging.Logger,
    config_dir: Path,
    marker_cls: type[RotatingFileHandler],
    file_name: str,
) -> Path | None:
    """Attach (or reuse) one volume's rotating file handler; None on failure."""
    for handler in logger.handlers:
        if isinstance(handler, marker_cls):  # idempotent: reuse the existing sink
            return Path(handler.baseFilename)
    log_dir = config_dir / LOG_DIR_NAME
    log_path = log_dir / file_name
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = marker_cls(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter(FMT))
        logger.addHandler(handler)
        return log_path
    except OSError as exc:
        # Degraded but observable (§9): sink unavailable → stderr note, no crash.
        label = "detail file" if marker_cls is _DetailMarker else "file"
        print(f"aixcoding.logging: {label} sink unavailable at {log_path} ({exc}); logs off", file=sys.stderr)
        return None


def setup_logging(cfg: DiagnosticsConfig, config_dir: Path | None = None) -> Path | None:
    """Idempotently configure the aixcoding logging subtree (main + detail).

    Returns the main log file path, or None when the main sink could not be
    created. The detail volume's outcome is independent (six invariants,
    ADR-0017): each sink is ensured on every call, one failing never blocks
    the other, and a failed detail sink still leaves the subtree isolated
    (records dropped with a stderr note, never leaked to the panel or the
    main channel). Safe to call repeatedly (e.g. tests, re-entry): existing
    sinks are reused.
    """
    from .config import resolve_config_dir

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(cfg.logging.level)
    logger.propagate = cfg.logging.panel

    detail = logging.getLogger(DETAIL_LOGGER_NAME)
    # Order matters (invariant 6): isolate the subtree before lowering its
    # level, so no record can slip out through the initialization gap. This
    # runs even when the detail sink ends up unavailable — a dropped record
    # (invariant 5) beats a leaked one.
    detail.propagate = False
    detail.setLevel(DETAIL_LEVEL)
    logging.addLevelName(DETAIL_LEVEL, _LEVEL_NAME)

    resolved = resolve_config_dir(config_dir)
    if resolved is None:
        print("aixcoding.logging: no config dir resolvable; file sink skipped", file=sys.stderr)
        return None
    main_path = _attach_sink(logger, resolved, _Marker, LOG_FILE_NAME)
    _attach_sink(detail, resolved, _DetailMarker, DETAIL_FILE_NAME)
    return main_path


def ensure_file_sink(config_dir: Path | None = None) -> Path | None:
    """Fail-safe composition of load_config + setup_logging; never raises.

    The startup wiring target (app/aixcoding/startup.py): both steps are
    individually fail-safe already; this adds containment for unexpected
    failures the same way (stderr note, §9) so the sink can never break CLI
    startup. Idempotent through setup_logging's sink reuse.
    """
    try:
        from .config import load_config

        return setup_logging(load_config(config_dir), config_dir)
    except Exception as exc:
        # Degraded but observable (§9): a startup hook must never raise.
        print(f"aixcoding.logging: file sink setup failed ({exc}); logs off", file=sys.stderr)
        return None
