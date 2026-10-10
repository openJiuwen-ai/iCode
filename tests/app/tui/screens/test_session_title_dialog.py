# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SessionTitleDialog — submit, clear, cancel, and the session identity row."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs import session_title as session_title_dialog
from chrys.app.tui.screens.dialogs.session_title import SessionTitleDialog
from chrys.app.tui.widgets import EnhancedInput
from chrys.foundation.config.settings import Settings
from tests.support.tui_helpers import click_when_settled, resize_when_settled, rich_plain
from tests.support.waiting import wait_for, wait_until_quiet

pytestmark = pytest.mark.asyncio

_SESSION_ID = "e9796bf5-2747-4a1b-9c3d-5e6f7a8b9c0d"


class _DialogApp(App):
    pass


async def test_enter_submits_stripped_custom_title() -> None:
    app = _DialogApp()
    async with app.run_test() as pilot:
        results: list[str | None] = []
        dialog = SessionTitleDialog(custom_title="", auto_title="Auto title")
        app.push_screen(dialog, results.append)
        await pilot.pause()

        field = dialog.query_one("#session-title-input", EnhancedInput)
        assert field.has_focus
        assert field.placeholder == "Auto title"
        field.value = "  My custom title  "
        await pilot.press("enter")
        await pilot.pause()

        assert results == ["My custom title"]


async def test_empty_submit_clears_custom_title() -> None:
    app = _DialogApp()
    async with app.run_test() as pilot:
        results: list[str | None] = []
        dialog = SessionTitleDialog(custom_title="Pinned", auto_title="Auto")
        app.push_screen(dialog, results.append)
        await pilot.pause()

        field = dialog.query_one("#session-title-input", EnhancedInput)
        assert field.value == "Pinned"
        field.value = ""
        await pilot.press("enter")
        await pilot.pause()

        assert results == [""]


async def test_escape_cancels_without_result() -> None:
    app = _DialogApp()
    async with app.run_test() as pilot:
        results: list[str | None] = []
        dialog = SessionTitleDialog(custom_title="Pinned", auto_title="")
        app.push_screen(dialog, results.append)
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert results == [None]


class _Recorder:
    """Stand-ins for the dialog's clipboard, file-manager and notification boundaries."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, can_open: bool = True) -> None:
        self.copied: list[str] = []
        self.opened: list[Path] = []
        self.notices: list[tuple[str, str]] = []
        self.open_error: OSError | None = None
        monkeypatch.setattr(session_title_dialog, "can_open_in_file_manager", lambda: can_open)
        monkeypatch.setattr(session_title_dialog, "copy_text_to_clipboards", self._copy)
        monkeypatch.setattr(session_title_dialog, "open_in_file_manager", self._open)
        monkeypatch.setattr(SessionTitleDialog, "notify", self._notify)

    def _copy(self, _app: object, text: str) -> bool:
        self.copied.append(text)
        return True

    def _open(self, folder: Path) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(folder)

    def _notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        assert markup is False
        self.notices.append((message, severity))


async def _open_dialog(app: App, dialog: SessionTitleDialog) -> None:
    app.push_screen(dialog)
    await wait_for(
        lambda: app.screen is dialog and dialog.is_mounted,
        description="session title dialog mounted",
    )


async def test_identity_row_copies_the_short_session_id_and_keeps_the_dialog_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(monkeypatch)
    app = _DialogApp()
    async with app.run_test() as pilot:
        dialog = SessionTitleDialog(session_id=_SESSION_ID, session_folder=tmp_path, custom_title="Pinned")
        await _open_dialog(app, dialog)

        assert rich_plain(dialog.query_one("#session-title-id", Static).render()) == "e9796bf52747"
        await click_when_settled(pilot, "#session-title-copy-id")
        await wait_for(lambda: recorder.copied, pilot=pilot, description="session id copied")

        assert recorder.copied == ["e9796bf52747"]
        assert recorder.notices == [("Session ID copied", "information")]
        assert app.screen is dialog
        assert dialog.query_one("#session-title-input", EnhancedInput).has_focus


async def test_identity_row_opens_the_session_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(monkeypatch)
    app = _DialogApp()
    async with app.run_test() as pilot:
        dialog = SessionTitleDialog(session_id=_SESSION_ID, session_folder=tmp_path)
        await _open_dialog(app, dialog)

        await click_when_settled(pilot, "#session-title-open-folder")
        await wait_for(lambda: recorder.opened, pilot=pilot, description="session folder opened")

        assert recorder.opened == [tmp_path]
        assert recorder.notices == []
        assert app.screen is dialog


async def test_open_folder_reports_a_missing_folder_and_an_opener_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(monkeypatch)
    app = _DialogApp()
    async with app.run_test() as pilot:
        missing = tmp_path / "not-saved-yet"
        dialog = SessionTitleDialog(session_id=_SESSION_ID, session_folder=missing)
        await _open_dialog(app, dialog)

        await click_when_settled(pilot, "#session-title-open-folder")
        await wait_for(lambda: recorder.notices, pilot=pilot, description="missing-folder notice")
        assert recorder.opened == []
        assert recorder.notices[0][1] == "warning"
        assert "first saved" in recorder.notices[0][0]

        missing.mkdir()
        recorder.open_error = FileNotFoundError("xdg-open")
        await click_when_settled(pilot, "#session-title-open-folder")
        await wait_for(lambda: len(recorder.notices) == 2, pilot=pilot, description="opener-failure notice")
        assert recorder.notices[1] == ("Could not open the session folder: xdg-open", "error")
        assert app.screen is dialog


@pytest.mark.parametrize(
    ("can_open", "has_folder"),
    [(False, True), (True, False)],
    ids=["no-file-manager", "no-session-folder"],
)
async def test_open_folder_link_needs_a_folder_and_a_file_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, can_open: bool, has_folder: bool
) -> None:
    _Recorder(monkeypatch, can_open=can_open)
    app = _DialogApp()
    async with app.run_test():
        dialog = SessionTitleDialog(session_id=_SESSION_ID, session_folder=tmp_path if has_folder else None)
        await _open_dialog(app, dialog)

        assert dialog.query("#session-title-copy-id")
        assert not dialog.query("#session-title-open-folder")


async def test_identity_row_needs_a_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _Recorder(monkeypatch)
    app = _DialogApp()
    async with app.run_test():
        dialog = SessionTitleDialog(custom_title="Pinned")
        await _open_dialog(app, dialog)

        assert not dialog.query("#session-title-identity")
        assert dialog.query_one("#session-title-input", EnhancedInput).has_focus


def _identity_fits(dialog: SessionTitleDialog) -> bool:
    """Every identity-row widget lies whole inside each container up to the dialog's content box,
    so no group clips it."""
    inner = dialog.query_one("#session-title-inner")
    for widget in (
        dialog.query_one("#session-title-id-label"),
        dialog.query_one("#session-title-id"),
        *dialog.query(".session-title-link"),
    ):
        if not widget.region.width:
            return False
        node = widget.parent
        while node is not None and node is not inner:
            if not isinstance(node, Widget) or not node.region.contains_region(widget.region):
                return False
            node = node.parent
        if not inner.content_region.contains_region(widget.region):
            return False
    return True


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_identity_links_move_below_the_id_when_one_line_is_too_narrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    recorder = _Recorder(monkeypatch)
    app = _DialogApp()
    async with app.run_test(size=(44, 24)) as pilot:
        dialog = SessionTitleDialog(
            session_id=_SESSION_ID,
            session_folder=tmp_path,
            locale_controller=LocaleController(Settings(locale=locale)),
        )
        await _open_dialog(app, dialog)
        row = dialog.query_one("#session-title-identity")
        identity = dialog.query_one("#session-title-id")
        copy_link = dialog.query_one("#session-title-copy-id")

        await wait_for(
            lambda: row.has_class("-stacked") and copy_link.region.y == identity.region.y + 1,
            pilot=pilot,
            description="identity links stacked under the id",
        )
        assert _identity_fits(dialog)

        await resize_when_settled(pilot, 100, 24)
        await wait_for(
            lambda: not row.has_class("-stacked") and copy_link.region.y == identity.region.y,
            pilot=pilot,
            description="identity links back beside the id",
        )
        assert _identity_fits(dialog)

        await resize_when_settled(pilot, 44, 24)
        await wait_for(
            lambda: row.has_class("-stacked") and copy_link.region.y == identity.region.y + 1,
            pilot=pilot,
            description="identity links stacked again",
        )
        await click_when_settled(pilot, copy_link)
        await wait_for(lambda: recorder.copied, pilot=pilot, description="copy from the stacked link")
        assert recorder.copied == ["e9796bf52747"]


# The design's minimum gap between the id and the links when they share a line.
_IDENTITY_LINKS_GAP = 3


def _identity_spans(dialog: SessionTitleDialog) -> tuple[int, int]:
    """The laid-out widths of the labelled id and of the links."""
    label = dialog.query_one("#session-title-id-label")
    identity = dialog.query_one("#session-title-id")
    links = list(dialog.query(".session-title-link"))
    return identity.region.right - label.region.x, links[-1].region.right - links[0].region.x


def _terminal_width_for_row(row_width: int) -> int:
    """The terminal width whose dialog (90% of it, less 2 border and 4 padding columns) leaves *row_width*."""
    return math.ceil((row_width + 6) * 10 / 9)


async def _assert_row_settles(
    pilot: Pilot, dialog: SessionTitleDialog, measurements: list[int], *, row_width: int, stacked: bool
) -> None:
    row = dialog.query_one("#session-title-identity")
    identity = dialog.query_one("#session-title-id")
    copy_link = dialog.query_one("#session-title-copy-id")
    await resize_when_settled(pilot, _terminal_width_for_row(row_width), 24)
    await wait_for(
        lambda: (
            row.size.width == row_width
            and row.has_class("-stacked") is stacked
            and copy_link.region.y == identity.region.y + (1 if stacked else 0)
        ),
        pilot=pilot,
        description=f"identity row {row_width} cells wide, stacked={stacked}",
    )
    # A row whose measurement depends on its own layout keeps flipping, and
    # re-measures on every resize that causes.
    await wait_until_quiet(lambda: len(measurements), pilot=pilot, description="identity row re-measuring")
    assert (row.size.width, row.has_class("-stacked")) == (row_width, stacked)
    assert _identity_fits(dialog)


@pytest.mark.parametrize(
    ("locale", "has_folder"),
    [("en", True), ("zh-Hans", True), ("en", False)],
    ids=["en", "zh-Hans", "en-without-folder-link"],
)
async def test_identity_row_settles_unclipped_at_its_breakpoint_and_its_narrowest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, locale: str, has_folder: bool
) -> None:
    _Recorder(monkeypatch)
    measurements: list[int] = []
    measure = session_title_dialog._IdentityRow._single_line_width

    def counted_measure(row: session_title_dialog._IdentityRow) -> int:
        measurements.append(width := measure(row))
        return width

    monkeypatch.setattr(session_title_dialog._IdentityRow, "_single_line_width", counted_measure)
    app = _DialogApp()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = SessionTitleDialog(
            session_id=_SESSION_ID,
            session_folder=tmp_path if has_folder else None,
            locale_controller=LocaleController(Settings(locale=locale)),
        )
        await _open_dialog(app, dialog)
        assert bool(dialog.query("#session-title-open-folder")) is has_folder
        await wait_for(lambda: _identity_fits(dialog), pilot=pilot, description="identity row laid out")
        id_span, links_span = _identity_spans(dialog)
        one_line = id_span + _IDENTITY_LINKS_GAP + links_span

        await _assert_row_settles(pilot, dialog, measurements, row_width=one_line - 1, stacked=True)
        await _assert_row_settles(pilot, dialog, measurements, row_width=one_line, stacked=False)
        await _assert_row_settles(pilot, dialog, measurements, row_width=one_line - 1, stacked=True)
        # The narrowest row that still holds each line whole.
        await _assert_row_settles(pilot, dialog, measurements, row_width=max(id_span, links_span), stacked=True)


async def test_dragging_across_the_identity_row_selects_the_id_but_not_the_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Recorder(monkeypatch)
    app = _DialogApp()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = SessionTitleDialog(session_id=_SESSION_ID, session_folder=tmp_path)
        await _open_dialog(app, dialog)
        label = dialog.query_one("#session-title-id-label")
        open_link = dialog.query_one("#session-title-open-folder")
        await wait_for(lambda: open_link.region.width > 0, pilot=pilot, description="identity row laid out")

        await pilot.mouse_down(label, offset=(0, 0))
        await pilot.hover(open_link, offset=(open_link.region.width - 1, 0))
        await pilot.mouse_up(open_link, offset=(open_link.region.width - 1, 0))
        await wait_for(dialog.get_selected_text, pilot=pilot, description="drag selection")

        selected = dialog.get_selected_text() or ""
        assert "e9796bf52747" in selected
        assert "Copy ID" not in selected
        assert "Open folder" not in selected
