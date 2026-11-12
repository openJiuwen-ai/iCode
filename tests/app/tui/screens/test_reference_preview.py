# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The preview a clicked label opens: a keyboard-closed viewer that reads image paths as the engine does and shows
a Diff quote as a Diff block."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.content import Content
from textual.widgets import Button, Static
from textual.worker import WorkerState

from chrys.app.tui.screens.dialogs.reference_preview import ReferencePreviewDialog
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines
from chrys.foundation.text.draft_references import DraftReference
from tests.support.diff_quote_text import diff_quote_text
from tests.support.tui_helpers import png_bytes, rich_plain
from tests.support.waiting import wait_for


@pytest.mark.parametrize(
    ("mention", "relative_to_home"),
    [("shot.png", False), ("~/shot.png", True)],
    ids=["workspace-relative", "home-relative"],
)
@pytest.mark.asyncio
async def test_a_recalled_image_mention_previews_from_the_workspace_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mention: str, relative_to_home: bool
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home if relative_to_home else workspace).joinpath("shot.png").write_bytes(png_bytes((10, 20, 30)))

    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference("image", 1, mention), cwd=workspace)
        await app.push_screen(dialog)

        await wait_for(
            lambda: bool(dialog.query(TerminalImages)),
            pilot=pilot,
            description="the image was read and shown",
        )


@pytest.mark.parametrize("key", ["escape", "q"])
@pytest.mark.asyncio
async def test_the_preview_has_no_buttons_and_closes_by_key(tmp_path: Path, key: str) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference("quote", 1, "line one\nline two"), cwd=tmp_path)
        await app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")
        assert not dialog.query(Button)

        await pilot.press(key)

        await wait_for(lambda: app.screen is not dialog, pilot=pilot, description="preview closed by its key")


@pytest.mark.asyncio
async def test_a_quote_previews_as_its_plain_text(tmp_path: Path) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference("quote", 1, "first line\nsecond line"), cwd=tmp_path)
        await app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")

        body = dialog.query_one("#reference-preview-text", Static)
        assert rich_plain(body.render()) == "first line\nsecond line"


def _diff_quote(scope: str = "turn 2", version: str = "before", path: str = "src/a.py", code: str = "b = 2") -> str:
    mark = "-" if version == "before" else "+"
    return diff_quote_text(path, version, scope, 2, [(1, " ", "a = 1"), (2, mark, code), (3, " ", "c = 3")])


async def _show(app: App, pilot, content: str, locale: str = "en") -> ReferencePreviewDialog:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import Localizer

    dialog = ReferencePreviewDialog(
        DraftReference("quote", 1, content),
        cwd=Path("."),
        locale_controller=LocaleController(localizer=Localizer(locale)),
    )
    await app.push_screen(dialog)
    await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")
    return dialog


@pytest.mark.parametrize(
    ("locale", "scope", "version", "title"),
    [
        ("en", "turn 2", "before", "src/a.py · Before · Line 2 · Turn 2"),
        ("en", "this session", "after", "src/a.py · After · Line 2 · All turns"),
        (
            "zh-Hans",
            'turn 12, the reply to the user message "修复"',
            "before",
            "src/a.py · 修改前 · 第 2 行 · 第 12 轮",
        ),
        ("zh-Hans", "this session", "after", "src/a.py · 修改后 · 第 2 行 · 全部轮次"),
    ],
)
@pytest.mark.asyncio
async def test_a_diff_quote_previews_as_a_diff_block_with_its_line_marked(locale, scope, version, title) -> None:
    app = App()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = await _show(app, pilot, _diff_quote(scope, version), locale)
        assert rich_plain(dialog.query_one("#reference-preview-title", Static).render()) == title
        assert not dialog.query("#reference-preview-text")
        block = dialog.query_one(UnifiedDiffLines)
        mark = "-" if version == "before" else "+"
        assert [block.render_line(y).text.rstrip() for y in range(3)] == [
            "  ▎1    a = 1",
            f"💬▎2  {mark} b = 2",
            "  ▎3    c = 3",
        ]


@pytest.mark.asyncio
async def test_the_marker_cell_has_the_background_of_its_line() -> None:
    from rich.console import ColorSystem

    app = App()
    async with app.run_test(size=(100, 24)) as pilot:
        app.console._color_system = ColorSystem.TRUECOLOR
        dialog = await _show(app, pilot, _diff_quote())
        block = dialog.query_one(UnifiedDiffLines)
        await pilot.pause()
        marked = list(block.render_line(1))
        assert marked[0].text == "💬"
        assert marked[0].style.bgcolor is not None
        assert marked[0].style.bgcolor == marked[-1].style.bgcolor
        context = list(block.render_line(0))
        assert context[0].style.bgcolor != marked[0].style.bgcolor


@pytest.mark.asyncio
async def test_a_long_line_scrolls_sideways_and_a_long_title_wraps_whole() -> None:
    from textual.containers import HorizontalScroll

    path = "src/" + "deeply/nested/" * 6 + "module.py"
    code = "value = " + "x" * 200 + " # end"
    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _show(app, pilot, _diff_quote(path=path, code=code))
        title = dialog.query_one("#reference-preview-title", Static)
        await wait_for(lambda: title.size.height > 1, pilot=pilot, description="title laid out")
        assert path in rich_plain(title.render()) and "…" not in rich_plain(title.render())
        scroll = dialog.query_one("#reference-preview-diff", HorizontalScroll)
        await wait_for(lambda: scroll.max_scroll_x > 0, pilot=pilot, description="block scrolls sideways")
        assert scroll.horizontal_scrollbar.size.height == 1
        block = dialog.query_one(UnifiedDiffLines)
        assert block.render_line(1).text.rstrip().endswith("# end")
        assert "…" not in block.render_line(1).text


@pytest.mark.asyncio
async def test_lines_are_highlighted_after_the_preview_shows_and_closing_first_is_quiet(monkeypatch) -> None:
    import threading

    import chrys.app.tui.screens.dialogs.reference_preview as module

    release = threading.Event()
    started = threading.Event()
    returned = threading.Event()

    def slow(code: str, path: str) -> list[Content]:
        started.set()
        release.wait(5)
        returned.set()
        return [Content.styled(line, "bold") for line in code.split("\n")]

    monkeypatch.setattr(module, "highlight_snippet", slow)
    app = App()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = await _show(app, pilot, _diff_quote())
        block = dialog.query_one(UnifiedDiffLines)
        await wait_for(started.is_set, pilot=pilot, description="highlighting started")
        assert all(not row.code.spans for row in block.rows)
        release.set()
        await wait_for(
            lambda: all(row.code.spans for row in block.rows), pilot=pilot, description="highlighted lines shown"
        )

        release.clear()
        started.clear()
        returned.clear()
        dialog = await _show(app, pilot, _diff_quote())
        await wait_for(started.is_set, pilot=pilot, description="highlighting started again")
        workers = list(dialog.workers)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not dialog, pilot=pilot, description="preview closed")
        release.set()
        await wait_for(returned.is_set, pilot=pilot, description="highlighting returned after the close")
        await wait_for(lambda: all(worker.is_finished for worker in workers), pilot=pilot)
        assert workers and all(worker.state is not WorkerState.ERROR for worker in workers)


@pytest.mark.asyncio
async def test_a_diff_quote_is_highlighted_by_its_file_type() -> None:
    app = App()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = await _show(app, pilot, _diff_quote(code="def b(): return 2"))
        block = dialog.query_one(UnifiedDiffLines)
        await wait_for(lambda: bool(block.rows[1].code.spans), pilot=pilot, description="python highlighted")


@pytest.mark.parametrize(
    "content",
    [
        "File: a.py (after) | Source: turn 2\n```\n1 + x\n```",
        _diff_quote().replace("  3   c = 3", "  4   c = 3"),
        _diff_quote() + "\n",
        _diff_quote(scope="turn " + "9" * 5000),
    ],
    ids=["previous-form", "inconsistent", "extra-line", "number-too-long"],
)
@pytest.mark.asyncio
async def test_text_that_is_not_a_whole_diff_quote_previews_as_plain_text(content: str) -> None:
    app = App()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = await _show(app, pilot, content)
        # The text it shows, whole: a rendering would cut a line as long as the over-long number.
        assert dialog.query_one("#reference-preview-text", Static).content.plain == content
        assert not dialog.query(UnifiedDiffLines)


@pytest.mark.parametrize("row", [0, 1, 2], ids=["before-it", "marked", "after-it"])
@pytest.mark.asyncio
async def test_a_selection_copies_and_shows_the_characters_dragged_over_on_every_row(row: int) -> None:
    """The comment icon is one character in two cells: the marked row copies what the others copy."""
    lines = [(1, " ", "abcdef"), (2, "-", "abcdef"), (3, " ", "abcdef")]
    app = App()
    async with app.run_test(size=(100, 24)) as pilot:
        dialog = await _show(app, pilot, diff_quote_text("src/a.txt", "before", "turn 2", 2, lines))
        block = dialog.query_one(UnifiedDiffLines)
        code_x = 8  # The gutter's cells, the same on every row: 💬 or two spaces, then the number and the "-".
        assert block.render_line(row).text.replace("💬", "  ")[code_x : code_x + 6] == "abcdef"

        async def drag(start: int, end: int) -> str | None:
            await pilot.mouse_down(block, offset=(start, row))
            await pilot.hover(block, offset=(end, row))
            await pilot.mouse_up(block, offset=(end, row))
            await pilot.pause()
            return app.screen.get_selected_text()

        assert await drag(code_x + 1, code_x + 3) == "bcd"
        # Dragged from the gutter, the selection starts at the code: its first character shows selected.
        assert await drag(0, code_x + 2) == "abc"
        # The selected characters are drawn as one piece of their own.
        assert "abc" in [segment.text for segment in block.render_line(row)]
