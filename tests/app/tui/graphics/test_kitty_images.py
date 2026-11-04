# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat images through the kitty graphics protocol: sent once, shown by placeholder cells."""

from __future__ import annotations

import re
from dataclasses import replace
from io import StringIO

import pytest
from rich.console import Console
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from chrys.app.tui.graphics import kitty, manager, payloads
from chrys.app.tui.graphics.detect import GraphicsProtocol
from chrys.app.tui.widgets.chat.image_preview import ChatImagePreview
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from tests.support.terminal_images import TerminalImagesApp, image_previews
from tests.support.waiting import wait_for, wait_until

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_HALF_BLOCK = "▀"


def _transmits(app: TerminalImagesApp) -> list[str]:
    return [write for write in app.writes if "a=T" in write]


def _deletes(app: TerminalImagesApp) -> list[str]:
    return re.findall(r"a=d,d=I,i=(\d+)", "".join(app.writes))


def _row(columns: int, row: int = 0) -> str:
    return "".join(kitty.placeholder_cells(row, columns))


async def _shown(app: TerminalImagesApp, view: TerminalImages, pilot: object) -> int:
    await wait_for(lambda: view.slots[0].kitty_id, pilot=pilot, description="the image was sent")
    columns = view.image_cells(view.content_size.width)[0][1].columns
    await wait_for(
        lambda: any(_row(columns) in line for line in app.screen_lines()),
        pilot=pilot,
        description="the screen shows the image's placeholder cells",
    )
    return view.slots[0].kitty_id


async def test_image_is_sent_once_and_shown_by_one_segment_per_placeholder_cell() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        image_id = await _shown(app, view, pilot)

        (transmit,) = _transmits(app)
        assert f"i={image_id},c=40,r=10," in transmit
        strip = view.render_line(9)
        assert [segment.text for segment in strip] == list(kitty.placeholder_cells(9, 40))
        assert {segment.style.color for segment in strip if segment.style} == {kitty.image_id_style(image_id).color}
        view.refresh()
        assert not await wait_until(lambda: len(_transmits(app)) > 1, pilot=pilot, timeout=0.5)


async def test_images_side_by_side_are_each_placed_at_their_own_column() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((20, 20), (20, 20)))
        await app.mount(view)
        await wait_for(
            lambda: all(slot.kitty_id for slot in view.slots), pilot=pilot, description="both images were sent"
        )
        left, right = (slot.kitty_id for slot in view.slots)
        await wait_for(
            lambda: app.placeholder_ids() == {left, right}, pilot=pilot, description="the screen shows both images"
        )

        strip = view.render_line(0)
        columns = [(segment.text, segment.style.color if segment.style else None) for segment in strip]
        cells = list(kitty.placeholder_cells(0, 20))
        assert columns[:20] == [(cell, kitty.image_id_style(left).color) for cell in cells]
        assert columns[20][0] == "  "
        assert columns[21:] == [(cell, kitty.image_id_style(right).color) for cell in cells]


async def test_new_images_free_the_old_and_the_same_images_again_keep_theirs() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        first = await _shown(app, view, pilot)

        view.set_previews(image_previews((30, 20)))
        second = await _shown(app, view, pilot)
        assert second != first
        assert _deletes(app) == [str(first)]

        view.set_previews(image_previews((30, 20)))
        assert not await wait_until(
            lambda: view.slots[0].kitty_id != second or len(_deletes(app)) > 1 or len(_transmits(app)) > 2,
            pilot=pilot,
            timeout=0.5,
        )
        assert app.placeholder_ids() == {second}


async def test_written_frame_keeps_each_cell_whole_in_the_image_color_when_clipped() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        view.styles.width = 60
        clip = Container(view)
        clip.styles.width = 25
        clip.styles.height = 12
        clip.styles.overflow_x = "auto"
        clip.styles.scrollbar_size_horizontal = 0
        await app.mount(clip)
        await wait_for(lambda: view.slots[0].kitty_id, pilot=pilot, description="the image was sent")
        image_id = view.slots[0].kitty_id
        clip.scroll_to(x=7, animate=False, immediate=True)
        await wait_for(
            lambda: "".join(kitty.placeholder_cells(3, 40)[7:32]) in app.screen_lines()[3],
            pilot=pilot,
            description="the scrolled clip shows the image's middle columns",
        )

        console = Console(file=StringIO(), force_terminal=True, color_system="truecolor", width=80)
        frame = app.screen._compositor.render_full_update().render_segments(console)

        visible = _ANSI.sub("", frame)
        cells = kitty.placeholder_cells(3, 40)
        assert "".join(cells[7:32]) in visible
        assert "".join(cells[6:33]) not in visible
        red, green, blue = (image_id >> 16) & 0xFF, (image_id >> 8) & 0xFF, image_id & 0xFF
        assert f"38;2;{red};{green};{blue}" in frame


async def test_screen_under_a_modal_shows_half_blocks_until_it_is_on_top_again() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await _shown(app, view, pilot)
        main = app.screen

        await app.push_screen(_Modal())
        await wait_for(lambda: isinstance(app.screen, _Modal) and app.screen.is_mounted, pilot=pilot)
        await wait_for(
            lambda: _HALF_BLOCK * 40 in main._compositor.render_strips()[0].text,
            pilot=pilot,
            description="the covered screen draws half-blocks",
        )
        assert not any(kitty.PLACEHOLDER in strip.text for strip in main._compositor.render_strips())

        app.pop_screen()
        await _shown(app, view, pilot)
        assert len(_transmits(app)) == 1


@pytest.mark.parametrize("blend", ["tint", "opacity", "text_opacity"])
async def test_styles_that_blend_the_foreground_draw_half_blocks(blend: str) -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        parent = Container(view)
        await app.mount(parent)
        await _shown(app, view, pilot)

        if blend == "tint":
            view.styles.tint = "red 20%"
        elif blend == "opacity":
            parent.styles.opacity = 0.5
        else:
            view.styles.text_opacity = 0.5
        await pilot.pause()

        assert view.render_line(0).text == _HALF_BLOCK * 40


async def test_unmount_deletes_the_image_and_a_new_box_replaces_it() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((60, 20)))
        holder = Container(view)
        holder.styles.height = "auto"
        await app.mount(holder)
        first = await _shown(app, view, pilot)

        holder.styles.width = 30
        second = await _wait_for_new_id(view, first, pilot)
        assert f"i={second},c=30,r=5," in _transmits(app)[-1]
        await wait_for(lambda: str(first) in _deletes(app), pilot=pilot, description="the old image is deleted")

        await view.remove()
        assert _deletes(app)[-1] == str(second)
        assert app.graphics is not None
        assert app.graphics.views == ()


async def test_suspend_frees_images_and_resume_sends_them_again() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        first = await _shown(app, view, pilot)

        app.app_suspend_signal.publish(app)
        assert _deletes(app) == [str(first)]
        assert view.slots[0].kitty_id == 0

        app.app_resume_signal.publish(app)
        await _wait_for_new_id(view, first, pilot)
        assert len(_transmits(app)) == 2


async def test_stopping_sends_the_delete_still_waiting_for_its_frame() -> None:
    """A placement moved to a new box is deleted after the frame; stopping first must still delete it."""
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        first = await _shown(app, view, pilot)
        graphics = app.graphics
        assert graphics is not None
        slot, box = view.image_cells(view.content_size.width)[0]

        assert graphics.kitty_image(view, slot, replace(box, columns=20, rows=5, pixel_height=10)) == 0
        assert _deletes(app) == []
        graphics.stop()
        assert _deletes(app) == [str(first)]
        assert not await wait_until(
            lambda: len(_deletes(app)) > 1 or len(_transmits(app)) > 1, pilot=pilot, timeout=0.5
        )


async def test_stopping_deletes_every_image_in_one_write() -> None:
    """Each write takes one of the 30 places in Textual's write queue, which nothing takes from once the
    terminal's window is closed: a write per image would hold the exit there for good."""
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((20, 10), (20, 10), (20, 10)))
        await app.mount(view)
        await wait_for(lambda: all(slot.kitty_id for slot in view.slots), pilot=pilot, description="images sent")
        ids = sorted(str(slot.kitty_id) for slot in view.slots)
        graphics = app.graphics
        assert graphics is not None
        slot, box = view.image_cells(view.content_size.width)[0]
        # One of them waiting for its frame to be deleted.
        assert graphics.kitty_image(view, slot, replace(box, columns=10, rows=3, pixel_height=5)) == 0
        written = len(app.writes)

        graphics.stop()

        assert len(app.writes) == written + 1
        assert sorted(_deletes(app)) == ids


async def test_an_image_evicted_out_of_view_is_sent_again_when_scrolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The screen must not keep placeholder lines it cached for a deleted id: those cells show nothing."""
    # Each 40x10-cell image at 10x20 pixels a cell costs 400 * 200 * 4 bytes: the budget holds one.
    monkeypatch.setattr(manager, "KITTY_BUDGET_BYTES", 400 * 200 * 4 * 3 // 2)
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        older = TerminalImages(image_previews((40, 20)))
        newer = TerminalImages(image_previews((40, 20)))
        spacer = Static()
        spacer.styles.height = 20
        scroll = VerticalScroll(older, spacer, newer)
        scroll.styles.height = 12
        await app.mount(scroll)
        older_id = await _shown(app, older, pilot)

        scroll.scroll_end(animate=False, immediate=True)
        await wait_for(lambda: newer.slots[0].kitty_id, pilot=pilot, description="the second image was sent")
        assert _deletes(app) == [str(older_id)]
        assert older.slots[0].kitty_id == 0

        scroll.scroll_home(animate=False, immediate=True)
        await wait_for(
            lambda: older.slots[0].kitty_id not in (0, older_id) and app.placeholder_ids() == {older.slots[0].kitty_id},
            pilot=pilot,
            description="the first image is shown by a new id",
        )


async def test_an_image_that_cannot_be_decoded_stays_half_blocks_and_is_not_decoded_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def kitty_png(data_uri: str, *, columns: int, rows: int, cell_pixels: tuple[int, int]) -> bytes:
        attempts.append(data_uri)
        return payloads.kitty_png(data_uri, columns=columns, rows=rows, cell_pixels=cell_pixels)

    monkeypatch.setattr(manager, "kitty_png", kitty_png)
    (preview,) = image_previews((40, 20))
    broken = ChatImagePreview(image=preview.image, media_type="image/png", data_uri="data:image/png;base64,AAAA")
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages([broken])
        await app.mount(view)
        await wait_for(lambda: view.slots[0].failed is not None, pilot=pilot, description="the payload failed")

        assert len(attempts) == 1
        view.refresh()
        assert not await wait_until(lambda: len(attempts) > 1, pilot=pilot, timeout=0.5)
        assert view.render_line(0).text == _HALF_BLOCK * 40
        assert _transmits(app) == []
        assert view.render_line(0).text == _HALF_BLOCK * 40


async def test_close_deletes_every_image_and_stops_sending() -> None:
    app = TerminalImagesApp(GraphicsProtocol.KITTY)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        image_id = await _shown(app, view, pilot)
        assert app.graphics is not None

        await app.graphics.close()

        assert _deletes(app) == [str(image_id)]
        assert manager.graphics_for(app) is None
        view.refresh()
        await wait_for(
            lambda: app.screen_lines()[0].startswith(_HALF_BLOCK * 40), pilot=pilot, description="half-blocks shown"
        )
        assert _transmits(app)[1:] == []


async def _wait_for_new_id(view: TerminalImages, old: int, pilot: object) -> int:
    await wait_for(lambda: view.slots[0].kitty_id not in (0, old), pilot=pilot, description="the image was sent again")
    return view.slots[0].kitty_id


class _Modal(ModalScreen[None]):
    def compose(self):
        yield Static("modal")
