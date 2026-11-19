# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A folded thinking message shows the opening of its text on its header row, cut to the row's width."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.errors import NoWidget
from textual.geometry import Offset
from textual.selection import Selection

from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.widgets.chat import messages as messages_module
from chrys.app.tui.widgets.chat.fold_rows import FOLDED_LABEL, text_preview
from chrys.app.tui.widgets.chat.messages import ThinkingMessage, _AgentHeader
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer
from tests.support.tui_helpers import ChatPanelApp, open_process_for, resize_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    import pytest
    from textual.widget import Widget


def test_a_preview_is_the_opening_of_the_text_on_one_line() -> None:
    assert text_preview("First line.\n\n   Second\tline,  spaced.\r\nThird.") == (
        "First line. Second line, spaced. Third."
    )


def test_a_preview_shows_markdown_as_written() -> None:
    """A hint at what the thinking says, not a rendering of it: markers stay."""
    assert text_preview("## Plan\n**Check** the `retry` path\n- one item") == (
        "## Plan **Check** the `retry` path - one item"
    )
    assert text_preview("我需要**先检查**一下调用链。") == "我需要**先检查**一下调用链。"


def test_a_preview_reads_only_the_start_of_a_long_text_and_replaces_control_characters() -> None:
    preview = text_preview("word " * 100_000)
    assert preview.startswith("word word") and len(preview) <= 1024
    assert text_preview("a\x1b[31mb\x07") == "a\ufffd[31mb\ufffd"
    assert text_preview("  \n\t\n") == ""


def test_a_folded_label_ends_with_the_locale_s_colon_before_the_preview() -> None:
    assert Localizer("en").render(FOLDED_LABEL.bind(label="Thought")) == "Thought: "
    assert Localizer("zh-Hans").render(FOLDED_LABEL.bind(label="思考")) == "思考："  # noqa: RUF001


async def test_a_folded_thinking_row_cuts_its_preview_to_the_width_and_a_resize_only_cuts_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def counting_preview(text: str) -> str:
        reads.append(text)
        return text_preview(text)

    monkeypatch.setattr(messages_module, "text_preview", counting_preview)
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(70, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_thinking_delta("r1", "reasoning", "[bold]list[int][/bold] 先检查改动范围和调用链 " * 10)
        (thinking,) = cp.query(ThinkingMessage)
        open_process_for(thinking)
        thinking.collapsed = False
        cp.finish_thinking_segment("r1")
        header = thinking.query_one(_AgentHeader)
        await wait_for(lambda: header.content_size.width > 0, pilot=pilot, description="header laid out")
        assert header.render().plain == "▾ Thought"
        assert reads == []

        refreshes: list[tuple[tuple[object, ...], dict[str, object]]] = []
        repaint = header.refresh

        def recording_refresh(*args: object, **kwargs: object) -> object:
            refreshes.append((args, kwargs))
            return repaint(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(header, "refresh", recording_refresh)
        thinking.collapsed = True
        # The row stays one line high: folding repaints the header without laying out the transcript.
        assert refreshes == [((), {"layout": False})]
        await wait_for(lambda: header.render().plain.startswith("▸"), pilot=pilot, description="folded row")
        wide = header.render().plain
        width = header.content_size.width
        # Text, not markup: the brackets show as written.
        assert wide.startswith("▸ Thought: [bold]list[int][/bold] 先检查")
        assert wide.endswith("…") and width - 1 <= cell_len(wide) <= width

        await resize_when_settled(pilot, 50, 24)
        narrow = header.render().plain
        assert header.content_size.width < width
        assert narrow.endswith("…") and header.content_size.width - 1 <= cell_len(narrow) <= header.content_size.width
        assert len(reads) == 1

        thinking.collapsed = False
        await wait_for(lambda: header.render().plain == "▾ Thought", pilot=pilot, description="open header")
        thinking.collapsed = True
        await wait_for(lambda: header.render().plain.startswith("▸"), pilot=pilot, description="folded again")
        # Folding again reuses the preview of the unchanged text.
        assert len(reads) == 1


async def test_a_selection_over_a_folded_row_copies_the_preview_text_it_covers() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(60, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_thinking_delta(
            "r1", "reasoning", "先检查 Inspect the retry path, then the continuation ticket it reuses."
        )
        (thinking,) = cp.query(ThinkingMessage)
        open_process_for(thinking)
        thinking.collapsed = False
        cp.finish_thinking_segment("r1")
        header = thinking.query_one(_AgentHeader)
        screen = pilot.app.screen

        def copied(selection: Selection) -> str | None:
            screen.selections = {header: selection}
            return screen.get_selected_text()

        # Open, the header copies as a transcript label, as an answer's does.
        assert copied(Selection(None, None)) == "[Thought]"

        thinking.collapsed = True
        await wait_for(lambda: header.render().plain.startswith("▸"), pilot=pilot, description="folded row")
        # Offsets count characters, not cells: the wide characters before them shift no copy.
        start = header.render().plain.index("Inspect")
        assert copied(Selection(Offset(start, 0), Offset(start + len("Inspect the"), 0))) == "Inspect the"
        # From the label, on past the row: the label, then the whole preview, past where the row cuts it.
        assert copied(Selection(Offset(0, 0), None)) == (
            "[Thought] 先检查 Inspect the retry path, then the continuation ticket it reuses."
        )
        assert copied(Selection(Offset(0, 0), Offset(3, 0))) == "[Thought]"


async def test_a_language_switch_reaches_a_folded_row_at_its_next_fold_and_a_selection_copies_what_it_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript text keeps the language it was shown in; a selection, which repaints the row, indexes that row."""
    app = ChatPanelApp(thinking_expanded=lambda: True)
    app.locale_controller = LocaleController(Settings(locale="en"))
    async with app.run_test(size=(60, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_thinking_delta("r1", "reasoning", "Inspect the retry path.")
        (thinking,) = cp.query(ThinkingMessage)
        open_process_for(thinking)
        thinking.collapsed = False
        cp.finish_thinking_segment("r1")
        header = thinking.query_one(_AgentHeader)
        screen = pilot.app.screen
        thinking.collapsed = True
        await wait_for(lambda: header.render().plain.startswith("▸ Thought"), pilot=pilot, description="folded row")
        start = header.render().plain.index("retry")

        result = pilot.app.locale_controller.switch_locale("zh-Hans")
        assert result.status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        renders = 0
        render = header.render

        def counting_render() -> Text:
            nonlocal renders
            renders += 1
            return render()

        monkeypatch.setattr(header, "render", counting_render)
        screen.selections = {header: Selection(Offset(start, 0), Offset(start + len("retry"), 0))}
        await wait_for(lambda: renders > 0, pilot=pilot, description="the selection repainted the row")
        # Repainted for the selection, the row still shows English, and the drag over "retry" copies it.
        assert header.render().plain.startswith("▸ Thought: Inspect")
        assert screen.get_selected_text() == "retry"

        thinking.collapsed = False
        thinking.collapsed = True
        await wait_for(lambda: header.render().plain.startswith("▸ 思考"), pilot=pilot, description="refolded row")
        start = header.render().plain.index("retry")
        screen.selections = {header: Selection(Offset(start, 0), Offset(start + len("retry"), 0))}
        assert screen.get_selected_text() == "retry"


def _mouse(event: type[events.MouseEvent], widget: Widget, x: int) -> events.MouseEvent:
    """A left-button event at column *x* of *widget*'s row, as the terminal driver posts it."""
    screen_x, screen_y = widget.content_region.x + x, widget.content_region.y
    return event(
        widget,
        x=screen_x,
        y=screen_y,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=screen_x,
        screen_y=screen_y,
    )


def _lands_on(widget: Widget, x: int) -> bool:
    """Whether a pointer at column *x* of *widget*'s row reaches it in the current layout."""
    region = widget.content_region
    with contextlib.suppress(NoWidget):
        return region.width > x and widget.app.get_widget_at(region.x + x, region.y)[0] is widget
    return False


async def test_a_drag_inside_a_folded_row_selects_its_text_and_a_click_folds() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(60, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_thinking_delta("r1", "reasoning", "Inspect the retry path.")
        (thinking,) = cp.query(ThinkingMessage)
        open_process_for(thinking)
        thinking.collapsed = False
        cp.finish_thinking_segment("r1")
        header = thinking.query_one(_AgentHeader)
        thinking.collapsed = True
        await wait_for(lambda: header.render().plain.startswith("▸"), pilot=pilot, description="folded row")
        start = header.render().plain.index("retry")
        await wait_for(lambda: _lands_on(header, start + 4), pilot=pilot, description="the row laid out")

        # Down and up on one column is a click: it unfolds.
        pilot.app.post_message(_mouse(events.MouseDown, header, start))
        pilot.app.post_message(_mouse(events.MouseUp, header, start))
        await wait_for(lambda: not thinking.collapsed, pilot=pilot, description="the click unfolded the row")
        thinking.collapsed = True
        await wait_for(lambda: header.render().plain.startswith("▸"), pilot=pilot, description="folded again")
        await wait_for(lambda: _lands_on(header, start + 4), pilot=pilot, description="the row laid out again")

        # Dragged across "retry", the same press and release select it, and the row stays folded.
        for event in (events.MouseDown, events.MouseMove, events.MouseUp):
            pilot.app.post_message(_mouse(event, header, start if event is events.MouseDown else start + 4))
        await wait_for(lambda: pilot.app.screen.selections, pilot=pilot, description="the drag selected text")
        await pilot.pause()
        assert thinking.collapsed is True
        assert pilot.app.screen.get_selected_text() == "retry"
