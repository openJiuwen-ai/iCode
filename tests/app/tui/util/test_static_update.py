# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``update_static_in_place`` lays the screen out exactly when the new content changes geometry.

Every case checks the composited frame and the widget's laid-out size, not only whether a layout
ran: skipping a layout the content needed shows up as clipped or stale cells.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.geometry import Size
from textual.widgets import Static

from chrys.app.tui.util.static_update import update_static_in_place
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.asyncio


class _StaticsApp(App[None]):
    CSS = """
    #fill { height: auto; }
    #narrow { width: 12; height: auto; }
    #label { width: auto; height: 1; }
    #shelf { height: auto; }
    #shelf.-hidden { display: none; }
    #shelved { height: auto; }
    """

    def __init__(self, *, shelf_hidden: bool = False) -> None:
        super().__init__()
        self._shelf_hidden = shelf_hidden

    def compose(self) -> ComposeResult:
        yield Static("alpha", id="fill")
        yield Static("short", id="narrow")
        yield Static("Compacting (8s)", id="label")
        with Vertical(id="shelf", classes="-hidden" if self._shelf_hidden else ""):
            yield Static("shelved one", id="shelved")
        yield Static("ROW-BELOW", id="below")


def _frame_text(app: App[None]) -> str:
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


async def _settle(app: App[None]) -> None:
    await wait_for(lambda: screen_is_settled(app, app.screen), description="screen settled")


async def _spy_layout(app: App[None], monkeypatch: pytest.MonkeyPatch):
    await _settle(app)
    screen = app.screen
    layout = create_autospec(screen._refresh_layout, side_effect=screen._refresh_layout)
    monkeypatch.setattr(screen, "_refresh_layout", layout)
    return layout


async def test_same_size_content_repaints_without_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _StaticsApp()
    async with app.run_test(size=(60, 12)):
        fill = app.query_one("#fill", Static)
        label = app.query_one("#label", Static)
        layout = await _spy_layout(app, monkeypatch)

        update_static_in_place(fill, "bravo")
        update_static_in_place(label, "Compacting (9s)")
        await _settle(app)

        layout.assert_not_called()
        frame = _frame_text(app)
        assert "bravo" in frame and "alpha" not in frame
        assert "Compacting (9s)" in frame


async def test_content_that_wraps_to_more_lines_lays_out(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _StaticsApp()
    async with app.run_test(size=(60, 12)):
        narrow = app.query_one("#narrow", Static)
        layout = await _spy_layout(app, monkeypatch)
        assert narrow.outer_size == Size(12, 1)

        update_static_in_place(narrow, "twelve chars twelve chars")
        await _settle(app)

        assert layout.call_count >= 1
        assert narrow.outer_size == Size(12, 2)
        lines = _frame_text(app).splitlines()
        assert [line.rstrip() for line in lines[1:4]] == ["twelve chars", "twelve chars", "Compacting (8s)"]


async def test_auto_width_label_that_widens_lays_out(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _StaticsApp()
    async with app.run_test(size=(60, 12)):
        label = app.query_one("#label", Static)
        update_static_in_place(label, "Compacting (9s)")
        await _settle(app)
        width = label.outer_size.width
        layout = await _spy_layout(app, monkeypatch)

        update_static_in_place(label, "Compacting (10s)")
        await _settle(app)

        assert layout.call_count >= 1
        assert label.outer_size.width == width + 1
        assert "Compacting (10s)" in _frame_text(app)


async def test_never_arranged_widget_under_hidden_ancestor_skips_layout_and_shows_current_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _StaticsApp(shelf_hidden=True)
    async with app.run_test(size=(60, 12)):
        shelved = app.query_one("#shelved", Static)
        layout = await _spy_layout(app, monkeypatch)
        assert shelved.outer_size == Size(0, 0)

        update_static_in_place(shelved, "shelved one\nshelved two\nshelved three")
        await _settle(app)
        layout.assert_not_called()

        app.query_one("#shelf").remove_class("-hidden")
        await _settle(app)

        assert shelved.outer_size.height == 3
        lines = _frame_text(app).splitlines()
        assert [line.rstrip() for line in lines[3:7]] == [
            "shelved one",
            "shelved two",
            "shelved three",
            "ROW-BELOW",
        ]


async def test_widget_hidden_after_layout_that_grows_meanwhile_shows_its_new_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _StaticsApp()
    async with app.run_test(size=(60, 12)):
        shelf = app.query_one("#shelf")
        shelved = app.query_one("#shelved", Static)
        await _settle(app)
        assert shelved.outer_size.height == 1
        shelf.add_class("-hidden")
        layout = await _spy_layout(app, monkeypatch)

        update_static_in_place(shelved, "shelved one\nshelved two")
        await _settle(app)
        layout.assert_not_called()

        shelf.remove_class("-hidden")
        await _settle(app)

        assert shelved.outer_size.height == 2
        lines = _frame_text(app).splitlines()
        assert [line.rstrip() for line in lines[3:6]] == ["shelved one", "shelved two", "ROW-BELOW"]


async def test_plain_display_reveal_remeasures_hidden_content(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _StaticsApp()
    async with app.run_test(size=(60, 12)):
        shelf = app.query_one("#shelf")
        shelved = app.query_one("#shelved", Static)
        await _settle(app)
        assert shelved.outer_size.height == 1
        shelf.display = False
        layout = await _spy_layout(app, monkeypatch)

        update_static_in_place(shelved, "one\ntwo\nthree\nfour")
        await _settle(app)
        layout.assert_not_called()

        shelf.display = True
        await _settle(app)
        assert shelved.outer_size.height == 4
        assert [line.rstrip() for line in _frame_text(app).splitlines()[3:8]] == [
            "one",
            "two",
            "three",
            "four",
            "ROW-BELOW",
        ]
