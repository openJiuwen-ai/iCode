# (tests — asserts are pytest idiom, Chinese prose is the data)
"""Detail channel tests: the file-only ``aixcoding.detail`` subtree (ADR-0017).

Design: aixcoding/docs/batches/detail-logging/design.md (signed off
2026-11-02). Records dispatched through the real logger tree must land in
the dedicated ``aixcoding.detail.log`` volume and nowhere else — never the
F6 panel (real ``_TuiLogHandler`` on the root logger), never a root handler
simulating the unlevelled upstream debug queue, never the main volume. Also
covers the six invariants: dual-sink idempotence and the four partial-failure
states (second external review, 2026-11-02).

``handler.emit`` is never called directly here: the level check lives in
``Logger.callHandlers``, so direct emission cannot validate the isolation.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from chrys.app.tui.screens.logs.screen import _TuiLogHandler
from chrys.kernel.aixcoding.auth.session import _DETAIL_LEVEL
from chrys.kernel.aixcoding.diagnostics.config import DiagnosticsConfig, LoggingConfig
from chrys.kernel.aixcoding.diagnostics.logging_support import (
    DETAIL_LEVEL,
    _DetailMarker,
    _Marker,
    setup_logging,
)


def make_cfg(level: int = logging.INFO, panel: bool = False) -> DiagnosticsConfig:
    return DiagnosticsConfig(logging=LoggingConfig(level=level, panel=panel), source=None)


def test_00_detail_level_constants_agree() -> None:
    # session.py mirrors the value locally (kernel cross-subpackage imports
    # are forbidden); this pin keeps the two definitions from drifting.
    assert _DETAIL_LEVEL == DETAIL_LEVEL
    assert DETAIL_LEVEL == 5  # literal anchor: ADR-0017 pins the value (review N3)


def _volume(config_dir: Path, name: str) -> Path:
    return config_dir / "aixcoding-logs" / name


def _flush(logger_name: str) -> None:
    for handler in logging.getLogger(logger_name).handlers:
        handler.flush()


@pytest.fixture
def root_watch() -> Iterator[tuple[list[logging.LogRecord], _TuiLogHandler]]:
    """Root-level observers: a level-0 spy (simulates the unlevelled upstream
    debug queue handler) plus a real ``_TuiLogHandler`` (the F6 panel path).
    Everything is restored afterwards."""
    records: list[logging.LogRecord] = []

    class _Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    spy = _Spy()
    # NOTSET mirrors the unlevelled upstream debug-queue handler: anything
    # that bubbles to the root would be captured — that is the tripwire.
    spy.setLevel(logging.NOTSET)
    tui = _TuiLogHandler()
    tui.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    saved = root.handlers[:]
    root.addHandler(spy)
    root.addHandler(tui)
    yield records, tui
    root.handlers[:] = saved


def _detail_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.name.startswith("aixcoding.detail")]


def test_01_detail_record_isolated(tmp_path: Path, aix_logger_state, root_watch) -> None:
    spy, tui = root_watch
    setup_logging(make_cfg(panel=True), tmp_path)
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "detail line")

    _flush("aixcoding.detail")
    detail_text = _volume(tmp_path, "aixcoding.detail.log").read_text(encoding="utf-8")
    assert "aixcoding.detail.auth" in detail_text
    assert "detail line" in detail_text
    # Nowhere else: root spy (upstream debug queue), panel buffer, main volume.
    assert _detail_records(spy) == []
    assert tui.snapshot("aixcoding", 0) == ([], 0)
    _flush("aixcoding")
    # delay=True: the main volume only materializes once a main-channel record
    # exists; absence is the cleanest form of "not there".
    main_volume = _volume(tmp_path, "aixcoding.log")
    assert not main_volume.exists() or "detail line" not in main_volume.read_text(encoding="utf-8")


def test_02_detail_level_name(tmp_path: Path, aix_logger_state) -> None:
    setup_logging(make_cfg(), tmp_path)
    assert logging.getLevelName(DETAIL_LEVEL) == "DETAIL"
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "named")
    _flush("aixcoding.detail")
    line = _volume(tmp_path, "aixcoding.detail.log").read_text(encoding="utf-8")
    assert " DETAIL aixcoding.detail.auth: named" in line


def test_03_no_volume_crosstalk(tmp_path: Path, aix_logger_state, root_watch) -> None:
    spy, _ = root_watch
    setup_logging(make_cfg(panel=True), tmp_path)
    logging.getLogger("aixcoding.auth").info("plain info")
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "secret detail")

    _flush("aixcoding")
    _flush("aixcoding.detail")
    main_text = _volume(tmp_path, "aixcoding.log").read_text(encoding="utf-8")
    detail_text = _volume(tmp_path, "aixcoding.detail.log").read_text(encoding="utf-8")
    assert "plain info" in main_text
    assert "plain info" not in detail_text
    assert "secret detail" in detail_text
    assert "secret detail" not in main_text
    # The normal channel still reaches root (panel=true); the detail one never.
    assert any(r.name == "aixcoding.auth" for r in spy)
    assert _detail_records(spy) == []


def test_04_normal_info_panel_states(tmp_path: Path, aix_logger_state, root_watch) -> None:
    spy, tui = root_watch
    setup_logging(make_cfg(panel=True), tmp_path)
    logging.getLogger("aixcoding.auth").info("visible info")
    _flush("aixcoding")
    assert "visible info" in _volume(tmp_path, "aixcoding.log").read_text(encoding="utf-8")
    assert tui.snapshot("aixcoding", 0)[1] == 1
    assert any(r.getMessage() == "visible info" for r in spy)

    setup_logging(make_cfg(panel=False), tmp_path)
    logging.getLogger("aixcoding.auth").info("hidden info")
    _flush("aixcoding")
    assert "hidden info" in _volume(tmp_path, "aixcoding.log").read_text(encoding="utf-8")
    assert tui.snapshot("aixcoding", 0)[1] == 1  # unchanged: nothing new captured


def test_05_setup_idempotent(tmp_path: Path, aix_logger_state) -> None:
    first = setup_logging(make_cfg(), tmp_path)
    second = setup_logging(make_cfg(), tmp_path)
    assert first == second
    markers = [h for h in logging.getLogger("aixcoding").handlers if isinstance(h, _Marker)]
    detail_markers = [h for h in logging.getLogger("aixcoding.detail").handlers if isinstance(h, _DetailMarker)]
    assert len(markers) == 1
    assert len(detail_markers) == 1
    detail = logging.getLogger("aixcoding.detail")
    assert detail.propagate is False
    assert detail.level == DETAIL_LEVEL


class _Boom:
    """Sink constructor that always fails (partial-failure injection)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise OSError("boom")


def test_06_partial_failure_main_ok_detail_fails(
    tmp_path: Path, aix_logger_state, root_watch, capsys, monkeypatch
) -> None:
    import chrys.kernel.aixcoding.diagnostics.logging_support as support

    spy, _ = root_watch
    monkeypatch.setattr(support, "_DetailMarker", _Boom)
    main_path = setup_logging(make_cfg(panel=True), tmp_path)
    assert main_path is not None

    # Main channel unaffected; detail records are dropped, never leaked.
    logging.getLogger("aixcoding.auth").info("main still works")
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "dropped detail")
    _flush("aixcoding")
    assert "main still works" in main_path.read_text(encoding="utf-8")
    assert _detail_records(spy) == []
    # Invariant 5: isolation state held even though the sink failed.
    detail = logging.getLogger("aixcoding.detail")
    assert detail.propagate is False
    assert detail.level == DETAIL_LEVEL
    assert "sink unavailable" in capsys.readouterr().err


def test_07_partial_failure_main_fails_detail_ok(
    tmp_path: Path, aix_logger_state, root_watch, capsys, monkeypatch
) -> None:
    import chrys.kernel.aixcoding.diagnostics.logging_support as support

    monkeypatch.setattr(support, "_Marker", _Boom)
    main_path = setup_logging(make_cfg(panel=True), tmp_path)
    assert main_path is None
    assert "file sink unavailable" in capsys.readouterr().err

    # The detail volume works independently of the failed main sink.
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "detail survives")
    _flush("aixcoding.detail")
    assert "detail survives" in _volume(tmp_path, "aixcoding.detail.log").read_text(encoding="utf-8")


def test_08_partial_failure_retry_rebuilds_detail(tmp_path: Path, aix_logger_state, root_watch, monkeypatch) -> None:
    import chrys.kernel.aixcoding.diagnostics.logging_support as support

    spy, _ = root_watch
    monkeypatch.setattr(support, "_DetailMarker", _Boom)
    setup_logging(make_cfg(), tmp_path)
    assert not _volume(tmp_path, "aixcoding.detail.log").exists()

    monkeypatch.undo()  # the next setup must retry and rebuild the missing sink
    setup_logging(make_cfg(), tmp_path)
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "rebuilt")
    _flush("aixcoding.detail")
    assert "rebuilt" in _volume(tmp_path, "aixcoding.detail.log").read_text(encoding="utf-8")
    assert _detail_records(spy) == []


def test_09_unprovisioned_subtree_stays_silent(aix_logger_state, root_watch) -> None:
    spy, _ = root_watch
    # No setup_logging: reset the subtree explicitly to the unprovisioned state
    # (level NOTSET, default propagate), then pin the tree's INFO threshold — a
    # DETAIL (level 5) record is simply not created (fail-safe silence).
    detail = logging.getLogger("aixcoding.detail")
    detail.setLevel(logging.NOTSET)
    detail.propagate = True
    logging.getLogger("aixcoding").setLevel(logging.INFO)
    logging.getLogger("aixcoding.detail.auth").log(DETAIL_LEVEL, "never created")
    assert _detail_records(spy) == []


def test_10_level_name_registration_idempotent(tmp_path: Path, aix_logger_state) -> None:
    setup_logging(make_cfg(), tmp_path)
    setup_logging(make_cfg(), tmp_path)
    assert logging.getLevelName(DETAIL_LEVEL) == "DETAIL"
