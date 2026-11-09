# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-visible preview layout, click targets and diff navigation."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.content import Content
from textual.geometry import Region
from textual.widgets import Static, TabbedContent

from chrys.app.tui.screens.diff.screen import DiffPeriodPane, DiffScreen
from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffFocus
from chrys.app.tui.widgets.sidebar.diff_preview import DiffPreview, SidebarChatPanel, preview_rows
from chrys.app.tui.widgets.sidebar.toc import ConversationToc
from chrys.service.mutations.types import MutationOp
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.pilot import Pilot
    from textual.screen import Screen
    from textual.strip import Strip
    from textual.widget import Widget


def entry(name: str) -> DiffFileEntry:
    return DiffFileEntry(
        os.path.normpath(f"/repo/{name}"),
        os.path.normpath(name),
        MutationOp.MODIFY,
        None,
        "before",
        "after",
        False,
        "utf-8",
        True,
    )


class PreviewApp(App):
    def __init__(self, *, original: bool = False) -> None:
        super().__init__()
        self.original = original
        self.opened: list[str | None] = []

    def compose(self) -> ComposeResult:
        if self.original:
            yield ConversationToc()
        else:
            yield SidebarChatPanel()

    def on_diff_preview_opened(self, event: DiffPreview.Opened) -> None:
        self.opened.append(event.display_key)


def drawn_rows(rows: Widget) -> list[Strip]:
    """The file rows as drawn at the size they are laid out at, one strip per row."""
    return rows.render_lines(Region(0, 0, *rows.size))


def drawn_text(rows: Widget) -> str:
    return "\n".join(strip.text for strip in drawn_rows(rows))


def hovered_rows(preview: DiffPreview, rows: Widget) -> list[bool]:
    """For each drawn row, whether it has the hover background."""
    hover = preview.get_component_rich_style("diff-preview--hover").bgcolor
    assert hover is not None
    return [any(segment.style and segment.style.bgcolor == hover for segment in strip) for strip in drawn_rows(rows)]


def dim_text(rows: Widget) -> list[str]:
    """The text of each dim stretch of the rows, as built for the width they are drawn at.

    Read from the content: Textual draws dim text in a fainter color, not with the dim attribute.
    """
    content = rows.render()
    assert isinstance(content, Text)
    return [content.plain[span.start : span.end] for span in content.spans if span.style == "dim"]


async def wait_for_rows(pilot: Pilot, preview: DiffPreview, count: int, *, description: str) -> Widget:
    """Wait for *count* file rows laid out; they are drawn for the width they are laid out at."""
    rows = preview.query_one("#diff-preview-rows")
    await wait_for(
        lambda: preview.display and preview.rows == count and rows.size.height == count,
        pilot=pilot,
        description=description,
    )
    return rows


def record_painted_rows(monkeypatch: pytest.MonkeyPatch, app: App, rows: Widget) -> list[tuple[int, list[str]]]:
    """For each frame the App paints with the file rows on screen: their width and the text of each, as painted.

    A frame is read from the render caches the screen composited it from, right after it was.
    """
    frames: list[tuple[int, list[str]]] = []
    display = app._display

    def record(screen: Screen[object], renderable: RenderableType | None) -> None:
        display(screen, renderable)
        if renderable is None or app._batch_count or screen is not app.screen:
            return
        placed = screen._compositor.visible_widgets.get(rows)
        region = Region() if placed is None else placed[0].intersection(placed[1])
        if region:
            lines = screen._compositor.render_strips()
            frames.append((region.width, [lines[y].crop(region.x, region.right).text for y in region.line_range]))

    monkeypatch.setattr(app, "_display", record)
    return frames


@pytest.mark.parametrize(
    ("height", "expected"),
    [(60, 10), (37, 10), (28, 10), (26, 10), (25, 9), (22, 8), (16, 5), (15, 4), (8, 1), (7, 0), (0, 0)],
)
@pytest.mark.parametrize("total", [0, 1, 9, 10, 11, 20])
def test_preview_height_preserves_the_conversation_directory(height: int, expected: int, total: int) -> None:
    assert preview_rows(total, height) == min(total, expected)


async def test_no_changes_renders_the_same_cells_as_the_original_directory() -> None:
    shots = []
    for original in (True, False):
        async with PreviewApp(original=original).run_test(size=(42, 24)) as pilot:
            toc = pilot.app.query_one(ConversationToc)
            await wait_for(
                lambda toc=toc: toc.size.height == 24, pilot=pilot, description="directory takes all available height"
            )
            shots.append([line.text for line in pilot.app.screen.render_lines(Region(0, 0, 42, 24))])
    assert shots[0] == shots[1]


async def test_rows_clicks_notes_and_resize() -> None:
    async with PreviewApp().run_test(size=(42, 16)) as pilot:
        app = pilot.app
        sidebar_chat = app.query_one(SidebarChatPanel)
        entries = tuple(entry(f"src/very/deep/path/file{i}.py") for i in range(12))
        entries = (
            replace(entries[0], content_omitted="binary", contested=True, inferred=True),
            replace(entries[1], line_count_result=(12, 3)),
            *entries[2:],
        )
        sidebar_chat.set_entries(entries)
        preview = app.query_one(DiffPreview)
        rows = await wait_for_rows(pilot, preview, 5, description="five rows shown")
        assert preview.size.height == 8
        lines = [strip.text for strip in drawn_rows(rows)]
        text = "\n".join(lines)
        assert "(no backup)" in text and "file0.py" in text
        assert not any(marker in text for marker in "?~!")
        assert "+12 -3" in text and "file1.py" in text
        assert "[+]" not in text and "[+-]" not in text and "[-]" not in text
        assert lines[1].endswith("+12 -3")  # A row fills its width, its counts at the right edge.
        assert "file5.py" not in text
        await click_when_settled(pilot, rows, offset=(2, 1))
        assert app.opened == [entries[1].rel_path]
        box = preview.query_one("#diff-preview-box")
        footer = preview.query_one("#diff-preview-footer")
        view_all = preview.query_one("#diff-preview-all", Static)
        assert Content.from_markup(box.border_title).plain == "File Changes (12) ≥+12 -3"
        assert box.region == preview.region
        assert rows.region.y == box.region.y + 1
        assert footer.region.y == rows.region.bottom
        assert footer.size.height == 1
        assert view_all.region.x == rows.region.x
        assert view_all.size.width == view_all.content.cell_len
        assert view_all.region.bottom == box.region.bottom - 1
        await click_when_settled(pilot, box, offset=(2, 0))
        assert app.opened == [entries[1].rel_path]
        await click_when_settled(pilot, footer, offset=(footer.content_size.width - 1, 0))
        assert app.opened == [entries[1].rel_path]
        await click_when_settled(pilot, view_all)
        assert app.opened == [entries[1].rel_path, None]
        await resize_when_settled(pilot, 42, 7)
        await wait_for(lambda: not preview.display, pilot=pilot, description="short window hides preview")
        await resize_when_settled(pilot, 42, 40)
        await wait_for_rows(pilot, preview, 10, description="taller window shows ten file rows")
        assert "file9.py" in drawn_text(rows) and "file10.py" not in drawn_text(rows)
        assert preview.size.height == 13
        sidebar_chat.set_entries(())
        assert not preview.display


async def test_every_painted_frame_shows_the_file_rows_built_for_the_width_it_paints_them_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each row shows its file name, and its counts end at the right edge of the width it is painted at.

    Rows built ahead of drawing took the width last laid out, and the frames before the preview's
    Resize showed them so: each one only its counts when the preview appeared or reappeared (no
    width at all while hidden), and sized for the old width when the sidebar's changed.
    """
    entries = tuple(replace(entry(f"file{i}.py"), line_count_result=(i + 1, i)) for i in range(3))

    def expected(width: int) -> list[str]:
        return [f"file{i}.py".ljust(width - len(f"+{i + 1} -{i}")) + f"+{i + 1} -{i}" for i in range(3)]

    async with PreviewApp().run_test(size=(42, 16)) as pilot:
        app = pilot.app
        sidebar_chat = app.query_one(SidebarChatPanel)
        preview = sidebar_chat.query_one(DiffPreview)
        rows = preview.query_one("#diff-preview-rows")
        frames = record_painted_rows(monkeypatch, app, rows)

        async def painted_at(width: int, description: str) -> None:
            await wait_for(
                lambda: rows.size == (width, 3) and frames[-1:] == [(width, expected(width))],
                pilot=pilot,
                description=description,
            )

        sidebar_chat.set_entries(entries)
        await painted_at(38, "the preview appears")
        await resize_when_settled(pilot, 60, 16)
        await painted_at(56, "the rows widen")
        await resize_when_settled(pilot, 42, 16)
        await painted_at(38, "the rows narrow")
        sidebar_chat.set_entries(())
        await wait_for(lambda: not preview.display, pilot=pilot, description="no changes hide the preview")
        await resize_when_settled(pilot, 50, 16)
        sidebar_chat.set_entries(entries)
        await painted_at(46, "the preview reappears at a width it was not laid out at before")
    assert [frame for frame in frames if frame[1] != expected(frame[0])] == []


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("single", [False, True])
async def test_preview_focus_opens_all_and_selects_the_requested_workspace_or_external_file(external, single) -> None:
    a, target = entry("a.py"), entry("src/deep/z.py")
    if external:
        target = replace(
            target, path=os.path.normpath("/elsewhere/z.py"), rel_path=os.path.normpath("../elsewhere/z.py")
        )
    key = target.path if external else target.rel_path
    periods = {1: [a, target]} if single else {1: [a], 2: [target]}

    class DiffApp(App):
        def on_mount(self) -> None:
            self.push_screen(DiffScreen(periods, all_entries=[a, target], focus=DiffFocus(key)))

    async with DiffApp().run_test(size=(120, 36)) as pilot:
        screen = pilot.app.screen
        await wait_for(
            lambda: isinstance(screen, DiffScreen) and screen._content_ready,
            pilot=pilot,
            description="focused diff ready",
        )
        tabs = screen.query_one(TabbedContent)
        assert tabs.active == ("turn-1" if single else "turn-all")
        pane = tabs.active_pane.query_one(DiffPeriodPane)
        assert pane._active_path == target.path


async def test_missing_target_falls_back_to_the_first_file() -> None:
    class DiffApp(App):
        def on_mount(self) -> None:
            self.push_screen(DiffScreen({1: [entry("a.py")]}, all_entries=[entry("a.py")], focus=DiffFocus("gone.py")))

    async with DiffApp().run_test() as pilot:
        screen = pilot.app.screen
        await wait_for(
            lambda: isinstance(screen, DiffScreen) and screen._content_ready,
            pilot=pilot,
            description="fallback diff ready",
        )
        assert screen.query_one(DiffPeriodPane)._active_path == entry("a.py").path


async def test_preview_hover_shell_border_and_live_chinese_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui import i18n as tui_i18n
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
    from chrys.foundation.config.settings import Settings

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale="en"))

    class SidebarApp(App):
        def compose(self) -> ComposeResult:
            yield SidebarPanel(locale_controller=controller)

    async with SidebarApp().run_test(size=(120, 35)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        sidebar.query_one(SidebarChatPanel).set_entries(
            (replace(entry("one.py"), content_omitted="binary"), entry("two.py"))
        )
        preview = sidebar.query_one(DiffPreview)
        rows = preview.query_one("#diff-preview-rows")
        await wait_for(
            lambda: preview.rows == 2 and rows.size.height == 2, pilot=pilot, description="preview rows laid out"
        )
        await pilot.hover(rows, offset=(5, 1))
        await wait_for(lambda: preview._hovered == 1, pilot=pilot, description="second row hovered")
        assert hovered_rows(preview, rows) == [False, True]
        sidebar.display = False
        await wait_for(lambda: preview._hovered is None, pilot=pilot, description="hidden preview clears hover")
        # Hidden, the rows are not drawn: what they would draw holds no highlight either.
        rendered = rows.render()
        assert isinstance(rendered, Text)
        assert not any(isinstance(span.style, Style) and span.style.bgcolor is not None for span in rendered.spans)
        sidebar.display = True
        await wait_for(
            lambda: preview.display and rows.size.height == 2, pilot=pilot, description="preview visible again"
        )
        assert hovered_rows(preview, rows) == [False, False]
        box = preview.query_one("#diff-preview-box")
        normal_border = box.styles.border_top
        sidebar.add_class("-shell-active")
        await wait_for(lambda: box.styles.border_top != normal_border, pilot=pilot, description="shell border changes")
        sidebar.remove_class("-shell-active")
        await wait_for(
            lambda: box.styles.border_top == normal_border, pilot=pilot, description="normal border restored"
        )
        controller.switch_locale("zh-Hans")
        assert box.border_title == "文件变更（2）"  # noqa: RUF001
        assert preview.query_one("#diff-preview-all", Static).content.plain == "查看全部"
        assert "无备份" in drawn_text(rows)


async def test_focused_leaf_below_the_viewport_is_scrolled_into_view() -> None:
    from textual.widgets import Tree

    entries = [entry(f"src/deep/file{i:03}.py") for i in range(80)]
    target = entries[-1]

    class DiffApp(App):
        def on_mount(self) -> None:
            self.push_screen(DiffScreen({1: entries}, all_entries=entries, focus=DiffFocus(target.rel_path)))

    async with DiffApp().run_test(size=(120, 24)) as pilot:
        screen = pilot.app.screen
        await wait_for(
            lambda: isinstance(screen, DiffScreen) and screen._content_ready,
            pilot=pilot,
            description="focused pane ready",
        )
        tree = screen.query_one(Tree)
        await wait_for(
            lambda: (
                tree.scroll_y > 0
                and tree.scrollable_content_region.height > 0
                and tree.scroll_y <= tree.cursor_line < tree.scroll_y + tree.scrollable_content_region.height
            ),
            pilot=pilot,
            description="focused leaf laid out and scrolled into view",
        )
        assert tree.cursor_node.data.path == target.path
        assert tree.cursor_line >= tree.scroll_y
        assert tree.cursor_line < tree.scroll_y + tree.scrollable_content_region.height


@pytest.mark.parametrize("target", ["#diff-preview-rows", "#diff-preview-all"])
async def test_double_click_opens_one_diff_and_one_escape_returns_to_chat(target: str) -> None:
    rows = [entry("a.py")]

    class NavigationApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.opened: list[str | None] = []

        def compose(self) -> ComposeResult:
            yield SidebarChatPanel()

        def on_diff_preview_opened(self, event: DiffPreview.Opened) -> None:
            self.opened.append(event.display_key)
            self.push_screen(DiffScreen({1: rows}, all_entries=rows, focus=DiffFocus(event.display_key)))

    async with NavigationApp().run_test(size=(120, 36)) as pilot:
        app = pilot.app
        chat = app.screen
        app.query_one(SidebarChatPanel).set_entries(tuple(rows))
        await wait_for(
            lambda: app.query_one(DiffPreview).display and app.query_one("#diff-preview-rows").size.height == 1,
            pilot=pilot,
            description="preview laid out",
        )
        await pilot.click(target, offset=(2, 0), times=2)
        await wait_for(lambda: isinstance(app.screen, DiffScreen), pilot=pilot, description="diff opened")
        await pilot.pause()
        assert len(app.opened) == 1
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await wait_for(lambda: app.screen is chat, pilot=pilot, description="single escape returns to chat")


@pytest.mark.parametrize(("height", "visible_rows"), [(37, 10), (28, 10), (26, 10), (25, 9), (22, 8), (16, 5)])
async def test_eleven_files_show_only_the_rows_that_fit_but_all_contribute_to_the_title(
    height: int, visible_rows: int
) -> None:
    async with PreviewApp().run_test(size=(60, height)) as pilot:
        app = pilot.app
        rows = tuple(replace(entry(f"file{i:02}.py"), line_count_result=(i, i - 1)) for i in range(1, 12))
        app.query_one(SidebarChatPanel).set_entries(rows)
        preview = app.query_one(DiffPreview)
        target = await wait_for_rows(
            pilot, preview, visible_rows, description="only file rows fitting the available height are shown"
        )
        title = Content.from_markup(preview.query_one("#diff-preview-box").border_title or "")
        assert title.plain == "File Changes (11) +66 -55"
        text = drawn_text(target)
        assert len(text.splitlines()) == visible_rows
        for i in range(1, 12):
            assert (f"file{i:02}.py" in text) is (i <= visible_rows)
        assert f"+{visible_rows} -{visible_rows - 1}" in text
        assert "[+-]" not in text
        assert preview.size.height == visible_rows + 3
        assert preview.size.height <= height // 2


@pytest.mark.parametrize("reason", ["large", "binary", "no-backup", "missing-content", "count-failed"])
async def test_finished_counts_include_a_lower_bound_for_each_unavailable_reason(reason: str) -> None:
    unavailable = entry(f"{reason}.py")
    if reason == "large":
        unavailable = replace(unavailable, line_count_result="large")
    elif reason == "binary":
        unavailable = replace(unavailable, is_binary=True)
    elif reason == "no-backup":
        unavailable = replace(unavailable, content_omitted="too_large")
    async with PreviewApp().run_test(size=(60, 30)) as pilot:
        app = pilot.app
        app.query_one(SidebarChatPanel).set_entries(
            (
                unavailable,
                replace(entry("first.py"), line_count_result=(3, 1)),
                replace(entry("second.py"), line_count_result=(2, 4)),
            )
        )
        preview = app.query_one(DiffPreview)
        rows = await wait_for_rows(pilot, preview, 3, description="known and unavailable rows visible")
        title = Content.from_markup(preview.query_one("#diff-preview-box").border_title)
        assert title.plain == "File Changes (3) ≥+5 -5"
        assert "+3 -1" in drawn_text(rows)


async def test_no_countable_files_shows_only_the_file_count() -> None:
    unavailable = (
        replace(entry("large.py"), line_count_result="large"),
        replace(entry("binary.py"), is_binary=True),
        replace(entry("no-backup.py"), content_omitted="too_large"),
        entry("missing.py"),
        entry("failed.py"),
    )
    async with PreviewApp().run_test(size=(60, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarChatPanel)
        sidebar.set_entries(unavailable)
        preview = sidebar.query_one(DiffPreview)
        await wait_for(lambda: preview.rows == 5, pilot=pilot, description="all unavailable rows shown")
        title = Content.from_markup(preview.query_one("#diff-preview-box").border_title)
        assert title.plain == "File Changes (5)"


async def test_a_known_zero_count_still_contributes_a_lower_bound() -> None:
    async with PreviewApp().run_test(size=(60, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarChatPanel)
        sidebar.set_entries((entry("unknown.py"), replace(entry("empty.py"), line_count_result=(0, 0))))
        preview = sidebar.query_one(DiffPreview)
        await wait_for(lambda: preview.rows == 2, pilot=pilot, description="zero and unavailable rows shown")
        title = Content.from_markup(preview.query_one("#diff-preview-box").border_title)
        assert title.plain == "File Changes (2) ≥+0 -0"


@pytest.mark.parametrize("pending_index", [0, 10], ids=["visible-pending", "hidden-pending"])
async def test_pending_counts_hide_totals_even_with_unavailable_files(pending_index: int) -> None:
    entries = [replace(entry(f"file{i}.py"), line_count_result=(2, 1)) for i in range(11)]
    entries[9] = replace(entries[9], line_count_result="large")
    entries[pending_index] = replace(entries[pending_index], line_count_result="pending")
    async with PreviewApp().run_test(size=(60, 39)) as pilot:
        sidebar = pilot.app.query_one(SidebarChatPanel)
        sidebar.set_entries(tuple(entries))
        preview = sidebar.query_one(DiffPreview)
        await wait_for(lambda: preview.rows == 10, pilot=pilot, description="pending and unavailable files shown")
        box = preview.query_one("#diff-preview-box")
        assert box.border_title == "File Changes (11)"
        entries[1] = replace(entries[1], line_count_result=(200, 100))
        sidebar.set_entries(tuple(entries))
        assert box.border_title == "File Changes (11)"


async def test_unchanged_title_is_not_reset_when_only_file_rows_change(monkeypatch: pytest.MonkeyPatch) -> None:
    async with PreviewApp().run_test(size=(60, 30)) as pilot:
        app = pilot.app
        sidebar = app.query_one(SidebarChatPanel)
        entries = (replace(entry("known.py"), line_count_result=(3, 1)), entry("unknown.py"))
        sidebar.set_entries(entries)
        preview = sidebar.query_one(DiffPreview)
        await wait_for(
            lambda: preview.rows == 2 and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="lower-bound title settled",
        )
        box = preview.query_one("#diff-preview-box")
        refresh = create_autospec(box.refresh, side_effect=box.refresh)
        monkeypatch.setattr(box, "refresh", refresh)
        sidebar.set_entries(tuple(replace(row, inferred=True) for row in entries))
        refresh.assert_not_called()
        sidebar.set_entries((replace(entries[0], line_count_result=(4, 1)), entries[1]))
        refresh.assert_called_once()
        assert Content.from_markup(box.border_title).plain == "File Changes (2) ≥+4 -1"


async def test_pending_counts_are_dim_then_replaced_and_totals_wait_for_every_file() -> None:
    async with PreviewApp().run_test(size=(60, 39)) as pilot:
        sidebar = pilot.app.query_one(SidebarChatPanel)
        pending = tuple(replace(entry(f"file{i}.py"), line_count_result="pending") for i in range(11))
        sidebar.set_entries(pending)
        preview = sidebar.query_one(DiffPreview)
        target = await wait_for_rows(pilot, preview, 10, description="pending files shown immediately")
        assert drawn_text(target).count("…") == 10
        assert dim_text(target) == ["…"] * 10
        box = preview.query_one("#diff-preview-box")
        assert box.border_title == "File Changes (11)"

        partial = tuple(replace(row, line_count_result=(2, 1)) for row in pending[:10]) + pending[10:]
        sidebar.set_entries(partial)
        assert "…" not in drawn_text(target)
        assert drawn_text(target).count("+2 -1") == 10
        assert box.border_title == "File Changes (11)"  # The hidden eleventh file is still pending.

        finished = (*partial[:10], replace(partial[10], line_count_result="unavailable"))
        sidebar.set_entries(finished)
        assert Content.from_markup(box.border_title).plain == "File Changes (11) ≥+20 -10"

        complete = (*partial[:10], replace(partial[10], line_count_result=(3, 2)))
        sidebar.set_entries(complete)
        assert Content.from_markup(box.border_title).plain == "File Changes (11) +23 -12"


@pytest.mark.parametrize(("locale", "note"), [("en", " (large file)"), ("zh-Hans", "（大文件）")])  # noqa: RUF001
async def test_large_file_note_and_lower_bound_total_are_localized(
    monkeypatch: pytest.MonkeyPatch, locale: str, note: str
) -> None:
    from chrys.app.tui import i18n as tui_i18n
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.config.settings import Settings

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale=locale))

    class LocalizedApp(App):
        def compose(self) -> ComposeResult:
            yield SidebarChatPanel(locale_controller=controller)

    async with LocalizedApp().run_test(size=(60, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarChatPanel)
        sidebar.set_entries(
            (
                replace(entry("large.py"), line_count_result="large"),
                replace(entry("small.py"), line_count_result=(2, 1)),
            )
        )
        preview = sidebar.query_one(DiffPreview)
        rows = await wait_for_rows(pilot, preview, 2, description="large and small rows shown")
        text = drawn_text(rows)
        assert note in text
        assert "…" not in text
        assert note in dim_text(rows)
        title = Content.from_markup(preview.query_one("#diff-preview-box").border_title)
        prefix = "File Changes (2)" if locale == "en" else "文件变更（2）"  # noqa: RUF001
        assert title.plain == f"{prefix} ≥+2 -1"
        marker = title.plain.index("≥")
        border_content = preview.query_one("#diff-preview-box")._border_title
        assert border_content is not None
        assert border_content.get_style_at_offset(marker) == border_content.get_style_at_offset(marker + 1)
        assert border_content.get_style_at_offset(marker).bold


@pytest.mark.parametrize(
    ("source", "inferred", "contested"),
    [
        ("implicit", False, False),
        ("edit_file", True, False),
        ("edit_file", False, True),
        ("implicit", False, True),
        ("edit_file", True, True),
    ],
    ids=["implicit", "inferred", "contested", "implicit-contested", "inferred-contested"],
)
@pytest.mark.parametrize(("count_result", "tail"), [((12, 3), "+12 -3"), ("pending", "…"), ("unavailable", "")])
async def test_source_flags_leave_preview_text_and_available_path_width_unchanged(
    source: str, inferred: bool, contested: bool, count_result, tail: str
) -> None:
    # This path fits exactly beside the counts in a 42-column sidebar.
    name = f"src/{'x' * 22}.py"
    plain = replace(entry(name), line_count_result=count_result)
    async with PreviewApp().run_test(size=(42, 16)) as pilot:
        sidebar = pilot.app.query_one(SidebarChatPanel)
        sidebar.set_entries((plain,))
        preview = sidebar.query_one(DiffPreview)
        rows = await wait_for_rows(pilot, preview, 1, description="unmarked file has its full available width")
        baseline = drawn_text(rows)
        assert os.path.normpath(name) in baseline  # The platform's separators, as in the full viewer.
        assert baseline.rstrip().endswith(tail)
        sidebar.set_entries((replace(plain, source=source, inferred=inferred, contested=contested),))
        flagged = drawn_text(rows)
        assert flagged == baseline
        assert not any(marker in flagged for marker in "?~!")


@pytest.mark.parametrize(
    ("path", "rel_path", "filename_tail"),
    [
        (
            "/repo/源代码/很长的模块目录/另一层目录/测试预览结果.py",
            "源代码/很长的模块目录/另一层目录/测试预览结果.py",
            "测试预览结果.py",
        ),
        (
            r"C:\outside\deep\nested\long_module_directory\long_preview.py",
            r"..\outside\deep\nested\long_module_directory\long_preview.py",
            "long_preview.py",
        ),
        (
            "/repo/src/component_preview_with_a_name_far_longer_than_the_sidebar_widget.py",
            "src/component_preview_with_a_name_far_longer_than_the_sidebar_widget.py",
            "sidebar_widget.py",
        ),
    ],
    ids=["cjk-directories-and-filename", "backslash-external-path", "long-filename"],
)
async def test_narrow_render_keeps_filename_tail_and_counts_without_source_markers(
    path: str, rel_path: str, filename_tail: str
) -> None:
    target = replace(
        entry(rel_path),
        path=os.path.normpath(path),
        rel_path=os.path.normpath(rel_path),
        line_count_result=(12, 3),
        inferred=True,
        contested=True,
    )
    async with PreviewApp().run_test(size=(42, 24)) as pilot:
        app = pilot.app
        app.query_one(SidebarChatPanel).set_entries((target,))
        preview = app.query_one(DiffPreview)
        rows = await wait_for_rows(pilot, preview, 1, description="narrow file row laid out")
        rendered_lines = drawn_rows(rows)
        assert len(rendered_lines) == 1
        rendered = rendered_lines[0].text.rstrip()
        assert cell_len(rendered) <= rows.content_size.width
        assert "…" in rendered
        assert rendered.endswith("+12 -3")
        assert not any(marker in rendered for marker in "?~!")
        displayed_path, counts, suffix = rendered.partition("+12 -3")
        assert counts == "+12 -3"
        assert suffix == ""
        assert displayed_path.rstrip().endswith(filename_tail)
