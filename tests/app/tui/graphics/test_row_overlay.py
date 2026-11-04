# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat images drawn row by row (Sixel, iTerm2 inline images) over the half-blocks of each frame
Textual writes."""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from rich.cells import cell_len
from textual._compositor import CompositorUpdate
from textual.app import App
from textual.containers import Horizontal, VerticalScroll
from textual.drivers._writer_thread import WriterThread
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.widgets import Static

from chrys.app.tui.graphics import manager, payloads
from chrys.app.tui.graphics.detect import GraphicsProtocol
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from tests.support.sixel_decoder import decode_sixel
from tests.support.terminal_images import TerminalImagesApp, image_previews
from tests.support.waiting import wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import RenderableType
    from textual.screen import Screen

_DRAW = re.compile(r"\x1b\[(\d+);(\d+)H(\x1bP0;1;0q.*?\x1b\\|\x1b\]1337;File=.*?\x1b\\)", re.DOTALL)
_TOKEN = re.compile(r"\x1bP.*?\x1b\\|\x1b\].*?\x1b\\|\x1b\[([0-9;?<>=]*)([@-~])|\x1b.|(\n)|([^\x1b\n]+)", re.DOTALL)
_RASTER_WIDTH = re.compile(r'"1;1;(\d+);')
_ITERM_WIDTH = re.compile(r";width=(\d+);")


@pytest.fixture(params=[GraphicsProtocol.SIXEL, GraphicsProtocol.ITERM], ids=["sixel", "iterm"])
def protocol(request: pytest.FixtureRequest) -> GraphicsProtocol:
    return request.param


def _draws(frame: str) -> list[tuple[int, int]]:
    """Each image row a frame draws, as its screen (row, column)."""
    return [(int(row) - 1, int(column) - 1) for row, column, _ in _DRAW.findall(frame)]


def _columns(image: str) -> int:
    """How many cells a row image spans."""
    if image.startswith("\x1bP"):
        (width,) = _RASTER_WIDTH.findall(image)
        return int(width) // 10
    (columns,) = _ITERM_WIDTH.findall(image)
    return int(columns)


def _png_size(image: str) -> tuple[int, int]:
    """The pixel size of an iTerm2 inline image's PNG."""
    data = base64.b64decode(image.rpartition(":")[2].removesuffix("\x1b\\"))
    with Image.open(io.BytesIO(data), formats=("PNG",)) as png:
        return png.size


def _printed_cells(frame: str) -> set[tuple[int, int]]:
    """The cells a frame prints text into, as (row, column); images and other escapes print none."""
    cells: set[tuple[int, int]] = set()
    y = x = 0
    for match in _TOKEN.finditer(frame):
        params, final, newline, text = match.groups()
        if final == "H":
            row, _, column = params.partition(";")
            y, x = int(row or 1) - 1, int(column or 1) - 1
        elif newline:
            y += 1
        elif text:
            for char in text:
                width = cell_len(char)
                cells.update((y, x + offset) for offset in range(width))
                x += width
    return cells


def _on_screen(frames: list[str]) -> set[tuple[int, int]]:
    """Replay ``frames`` as the terminal would: printing into a cell erases the image there.

    A frame's text comes before the image rows appended to it. Fails on a row drawn again while all of
    it is still on screen: that is output for nothing.
    """
    shown: dict[tuple[int, int], int] = {}
    for frame in frames:
        printed = _printed_cells(frame)
        shown = {
            (y, x): columns
            for (y, x), columns in shown.items()
            if not any((y, column) in printed for column in range(x, x + columns))
        }
        for row, column, image in _DRAW.findall(frame):
            draw = (int(row) - 1, int(column) - 1)
            assert draw not in shown, f"{draw} drawn again with nothing printed over it"
            shown[draw] = _columns(image)
    return set(shown)


def _drawn_since(app: TerminalImagesApp, first_frame: int) -> list[tuple[int, int]]:
    return [draw for frame in app.frames[first_frame:] for draw in _draws(frame)]


def _image_rows(view: TerminalImages) -> set[tuple[int, int]]:
    content = view.content_region
    return {(content.y + row, content.x) for row in range(view.content_size.height)}


async def _all_drawn(app: TerminalImagesApp, view: TerminalImages, pilot: object, first_frame: int = 0) -> None:
    def drawn() -> bool:
        rows = _image_rows(view)
        return bool(rows) and rows <= set(_drawn_since(app, first_frame))

    await wait_for(drawn, pilot=pilot, description="every image row was drawn")


async def test_each_row_is_drawn_where_the_half_blocks_are_and_only_when_erased(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        await app.mount(Static("above"))
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await _all_drawn(app, view, pilot)

        assert _on_screen(app.frames) == {(row, 0) for row in range(1, 11)}
        first = next(_DRAW.finditer("".join(app.frames)))[3]
        if protocol is GraphicsProtocol.SIXEL:
            assert decode_sixel(first).raster == (1, 1, 400, 20)
        else:
            assert ";width=40;height=1;" in first
            assert _png_size(first) == (400, 20)


async def test_images_side_by_side_are_each_drawn_at_their_own_column(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((20, 20), (20, 20)))
        await app.mount(view)
        expected = {(row, column) for row in range(10) for column in (0, 22)}
        await wait_for(lambda: expected <= set(_drawn_since(app, 0)), pilot=pilot, description="both images drawn")

        assert [box.x for _slot, box in view.image_cells(view.content_size.width)] == [0, 22]
        assert _on_screen(app.frames) == expected


async def test_a_repaint_of_one_row_draws_that_row_again(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await _all_drawn(app, view, pilot)
        mark = len(app.frames)

        view.refresh(Region(3, 4, 2, 1))
        await wait_for(lambda: _drawn_since(app, mark), pilot=pilot, description="the repainted row was drawn")

        assert _drawn_since(app, mark) == [(4, 0)]


async def test_a_widget_beside_the_image_repainting_draws_nothing_again(protocol: GraphicsProtocol) -> None:
    """Windows Terminal erases an image only in the cells text is printed into."""
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        view.styles.width = 50
        side = Static("tick 0")
        side.styles.height = 10
        await app.mount(Horizontal(view, side))
        await _all_drawn(app, view, pilot)
        mark = len(app.frames)

        for tick in range(1, 4):
            text = f"tick {tick}"
            side.update(text)
            await wait_for(lambda text=text: text in "".join(app.frames[mark:]), pilot=pilot, description="ticked")
        assert not await wait_until(lambda: _drawn_since(app, mark), pilot=pilot, timeout=0.5)
        assert _on_screen(app.frames) == _image_rows(view)


async def test_rows_no_longer_wanted_stop_being_encoded(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    """A tall image takes seconds to encode: unmounting it frees the job for the next image."""
    started, release = threading.Event(), threading.Event()
    answers: list[bool] = []

    name, encode = (
        ("sixel_rows", payloads.sixel_rows)
        if protocol is GraphicsProtocol.SIXEL
        else ("iterm_rows", payloads.iterm_rows)
    )

    def held_rows(
        data_uri: str, *, columns: int, rows: int, pixel_height: int, wanted: Callable[[], bool] | None
    ) -> tuple[str, ...]:
        if not started.is_set():
            started.set()
            release.wait(timeout=10)
            answers.append(wanted())
        return encode(data_uri, columns=columns, rows=rows, pixel_height=pixel_height, wanted=wanted)

    monkeypatch.setattr(manager, name, held_rows)
    app = TerminalImagesApp(protocol)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            tall = TerminalImages(image_previews((40, 200)))
            await app.mount(tall)
            await wait_for(started.is_set, pilot=pilot, description="the tall image is being encoded")
            slot = tall.slots[0]
            await tall.remove()
            release.set()

            after = TerminalImages(image_previews((40, 20)))
            await app.mount(after)
            await _all_drawn(app, after, pilot)
            assert answers == [False]
            assert (slot.rows, slot.failed) == ((), None)
    finally:
        release.set()


async def test_a_moving_image_is_drawn_again_once_it_settles(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        scroll = VerticalScroll(Static("\n".join(["above"] * 4)), view, Static("\n".join(["below"] * 40)))
        await app.mount(scroll)
        await _all_drawn(app, view, pilot)
        mark = len(app.frames)

        scroll.scroll_to(y=2, animate=False, immediate=True)
        await wait_for(lambda: len(app.frames) > mark, pilot=pilot, description="the scroll was painted")
        assert _draws(app.frames[mark]) == []
        await _all_drawn(app, view, pilot, mark)

        assert set(_drawn_since(app, mark)) == {(row, 0) for row in range(2, 12)}
        assert _on_screen(app.frames) == {(row, 0) for row in range(2, 12)}


async def test_rows_under_a_widget_above_them_are_left_as_half_blocks(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        cover = Static("toast")
        cover.styles.layer = "above"
        cover.styles.dock = "top"
        cover.styles.width = 10
        cover.styles.offset = (20, 3)
        await app.mount(view, cover)
        await wait_for(lambda: view.slots[0].rows, pilot=pilot, description="the rows were encoded")
        cover_region = app.screen.find_widget(cover).region
        expected = {(y, x) for y, x in _image_rows(view) if not cover_region.overlaps(Region(x, y, 40, 1))}
        await wait_for(lambda: expected <= set(_drawn_since(app, 0)), pilot=pilot, description="uncovered rows drawn")

        assert len(expected) == 9
        assert set(_drawn_since(app, 0)) == expected


async def test_rows_a_scroll_container_clips_are_not_drawn(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((20, 20)))
        clip = VerticalScroll(view)
        clip.styles.height = 4
        clip.styles.width = 30
        await app.mount(clip)
        in_view = {(row, 0) for row in range(4)}
        await wait_for(lambda: in_view <= set(_drawn_since(app, 0)), pilot=pilot, description="rows in view drawn")
        assert set(_drawn_since(app, 0)) == in_view

        scrolled = len(app.frames)
        clip.scroll_to(y=3, animate=False, immediate=True)
        await wait_for(
            lambda: in_view <= set(_drawn_since(app, scrolled)), pilot=pilot, description="rows scrolled in drawn"
        )
        assert set(_drawn_since(app, scrolled)) == in_view


async def test_a_row_on_the_last_screen_line_is_not_drawn(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 10)) as pilot:
        view = TerminalImages(image_previews((20, 20)))
        await app.mount(view)
        above_last = {(row, 0) for row in range(9)}
        await wait_for(lambda: above_last <= set(_drawn_since(app, 0)), pilot=pilot, description="rows drawn")

        assert view.content_region.bottom == 10
        assert set(_drawn_since(app, 0)) == above_last


async def test_a_frame_draws_at_most_its_budget_and_the_rest_follows(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        # Drawn first: rows owed from before, drawn under the budget and then erased by the repaint,
        # would be drawn twice.
        await _all_drawn(app, view, pilot)
        # Three rows, each with the cursor move before it.
        budget = 3 * len("\x1b[10;1H" + view.slots[0].rows[0])
        monkeypatch.setattr(manager, "FRAME_BUDGET_CHARS", budget)
        mark = len(app.frames)

        view.refresh()
        await _all_drawn(app, view, pilot, mark)

        per_frame = [len(_draws(frame)) for frame in app.frames[mark:] if _draws(frame)]
        assert max(per_frame) == 3
        # The repaint's own text counts: Windows Terminal is written a frame in 8 KB pieces, which
        # Textual's 30-write queue must hold.
        assert all(len(frame) <= budget for frame in app.frames[mark:] if _draws(frame))
        assert _on_screen(app.frames) == _image_rows(view)


async def test_an_image_with_a_row_too_long_for_a_frame_keeps_its_half_blocks(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    monkeypatch.setattr(manager, "MAX_ROW_CHARS", 10)
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await wait_for(lambda: view.slots[0].failed, pilot=pilot, description="the rows were refused")

        assert view.slots[0].rows == ()
        assert _drawn_since(app, 0) == []


async def test_a_moved_image_waits_to_settle_while_other_widgets_repaint(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    """A conversation that streams while it scrolls must not draw the image at each step."""
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        ticker = Static("tick 0")
        scroll = VerticalScroll(Static("\n".join(["above"] * 4)), view, Static("\n".join(["below"] * 40)))
        await app.mount(ticker, scroll)
        await _all_drawn(app, view, pilot)
        overlay = app.graphics.overlay if app.graphics is not None else None
        assert overlay is not None
        await wait_for(lambda: overlay._settle_handle is None, pilot=pilot, description="no overlay frame is due")
        monkeypatch.setattr(manager, "SETTLE_SECONDS", 60)  # still moving for the rest of the test
        mark = len(app.frames)

        scroll.scroll_to(y=2, animate=False, immediate=True)
        await wait_for(lambda: len(app.frames) > mark, pilot=pilot, description="the scroll was painted")
        ticker.update("tick 1")
        await wait_for(lambda: "tick 1" in "".join(app.frames[mark:]), pilot=pilot, description="ticked")

        assert _drawn_since(app, mark) == []


async def test_nothing_is_drawn_while_a_modal_covers_the_screen(protocol: GraphicsProtocol) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await _all_drawn(app, view, pilot)

        await app.push_screen(_Modal())
        await wait_for(lambda: isinstance(app.screen, _Modal) and app.screen.is_mounted, pilot=pilot)
        mark = len(app.frames)
        app.screen.refresh()
        await wait_for(lambda: len(app.frames) > mark, pilot=pilot, description="the modal was painted")
        assert _drawn_since(app, mark) == []

        app.pop_screen()
        await _all_drawn(app, view, pilot, mark)
        assert _on_screen(app.frames) == _image_rows(view)


async def test_a_failing_overlay_still_writes_the_frame(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, protocol: GraphicsProtocol
) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        await app.mount(Static("text"))
        assert app.graphics is not None
        overlay = app.graphics.overlay
        assert overlay is not None

        def fail(_update: object) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(overlay, "frame_suffix", fail)
        mark = len(app.frames)
        with caplog.at_level(logging.ERROR, logger=manager.__name__):
            app.screen.refresh()
            await wait_for(lambda: len(app.frames) > mark, pilot=pilot, description="a frame was written")

        assert "text" in app.frames[-1]
        assert "Could not draw terminal images" in caplog.text


async def test_rows_wait_while_the_terminal_has_not_taken_earlier_output(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    """Drawing behind a backlog would fill Textual's write queue and make the event loop wait."""
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await _all_drawn(app, view, pilot)
        overlay = app.graphics.overlay if app.graphics is not None else None
        assert overlay is not None
        # The frame due after the image first moved into place, which would draw for the retry under test.
        await wait_for(lambda: overlay._settle_handle is None, pilot=pilot, description="no overlay frame is due")
        # Never started: what it is handed stays queued, as behind a terminal that has stopped reading.
        writer = WriterThread(io.StringIO())
        monkeypatch.setattr(app._driver, "_writer_thread", writer, raising=False)
        writer.write("output the terminal has not taken")
        mark = len(app.frames)

        view.refresh()
        await wait_for(lambda: len(app.frames) > mark, pilot=pilot, description="the image was painted over")
        own = app.overlay_frames
        # No rows, nor any frame of the overlay's own, which would add writes to the queue.
        assert not await wait_until(
            lambda: _drawn_since(app, mark) or app.overlay_frames > own, pilot=pilot, timeout=0.3
        )

        writer._queue.get_nowait()
        # Without the pilot, whose pauses paint frames: the overlay draws once the queue is empty.
        await _all_drawn(app, view, None, mark)
        assert _on_screen(app.frames) == _image_rows(view)


async def test_a_backlog_with_no_rows_owed_writes_nothing_more(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    app = TerminalImagesApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        overlay = app.graphics.overlay if app.graphics is not None else None
        assert overlay is not None
        text = Static("text")
        await app.mount(text)
        writer = WriterThread(io.StringIO())  # never started: the test plays the writer
        monkeypatch.setattr(app._driver, "_writer_thread", writer, raising=False)
        writer.write("output the terminal has not taken")
        mark = len(app.frames)
        text.update("more text")
        await wait_for(
            lambda: "more text" in "".join(app.frames[mark:]), pilot=pilot, description="the text was painted"
        )

        # No frame of the overlay's own is due, to try again while the backlog lasts or after it.
        assert (overlay._frame_handle, overlay._settle_handle) == (None, None)


class _SynchronizedApp(TerminalImagesApp):
    """Queues each frame's synchronized-update start on its writer before rendering the frame, as
    Textual's ``App._display`` does, and has the writer take it once the frame is written."""

    def __init__(self, protocol: GraphicsProtocol) -> None:
        super().__init__(protocol)
        self.writer = WriterThread(io.StringIO())  # never started: the test plays the writer

    def _display(self, screen: Screen, renderable: RenderableType | None) -> None:
        framed = renderable if self.graphics is None else self.graphics.frame(renderable)
        if framed is not renderable and isinstance(framed, CompositorUpdate):
            self.writer.write("\x1b[?2026h")
            self.frames.append(framed.render_segments(self.console))
            self.writer._queue.get_nowait()
        App._display(self, screen, framed)


async def test_a_frame_is_not_held_back_by_its_own_synchronized_update_start(
    monkeypatch: pytest.MonkeyPatch, protocol: GraphicsProtocol
) -> None:
    app = _SynchronizedApp(protocol)
    async with app.run_test(size=(80, 24)) as pilot:
        monkeypatch.setattr(app._driver, "_writer_thread", app.writer, raising=False)
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await _all_drawn(app, view, pilot)

        assert _on_screen(app.frames) == _image_rows(view)


class _Modal(ModalScreen[None]):
    def compose(self):
        yield Static("modal")
