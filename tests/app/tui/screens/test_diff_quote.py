# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Review comments follow line numbers, the live draft, and the exact comparison being reviewed."""

from __future__ import annotations

from dataclasses import replace

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Offset, Region
from textual.selection import Selection
from textual.widgets import TabbedContent, TabPane

from chrys.app.tui.screens.diff.comment_bar import DiffCommentBar, DiffCommentInput
from chrys.app.tui.screens.diff.screen import DiffPeriodPane, DiffScreen
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.widgets.chat.quote_popup import QuoteSelectionPopup
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.diff_view import CodeColumn, DiffView
from chrys.app.tui.widgets.diff_view.gutter import GutterColumn
from chrys.app.tui.widgets.diff_view.rows import RowKind, Side
from chrys.service.mutations.types import MutationOp
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for


def _entry(path: str = "/repo/sub/a.py") -> DiffFileEntry:
    return DiffFileEntry(
        path,
        "intentionally/wrong.py",
        MutationOp.MODIFY,
        None,
        "\told\n" + "same\n" * 30,
        "\tnew\n" + "same\n" * 30,
        False,
        "utf-8",
        True,
    )


class _Host(App):
    CSS = "AppHeader { dock: top; height: 1; }"


class _App(_Host):
    def __init__(self, *, enabled: bool = True, cwd: str = "/repo/sub", entries=None) -> None:
        super().__init__()
        self.enabled = enabled
        self.cwd = cwd
        self.entries = entries or {1: [_entry(), _entry("/repo/sub/b.py")], 2: [_entry()]}
        self.comments = []
        self.diff: DiffScreen

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_mount(self) -> None:
        self.open_diff()

    def open_diff(self) -> None:
        self.diff = DiffScreen(
            self.entries,
            cwd=self.cwd,
            all_entries=[_entry("/repo/sub/session.py")],
            draft=self.query_one(InputBar) if self.enabled else None,
            comments=self.comments,
        )
        self.push_screen(self.diff)


async def _ready(app: _App, pilot) -> list[CodeColumn]:
    # A Pilot barrier also waits for unrelated widget queues and can hold this check past its deadline.
    await wait_for(lambda: app.diff._content_ready and bool(app.diff.query(CodeColumn)), description="diff code ready")
    await pilot.pause()
    pane = app.diff.query_one(f"#{app.diff.query_one(TabbedContent).active}", TabPane)
    return list(pane.query(CodeColumn))


def _number_gutter(column: CodeColumn, side: Side | None = None) -> GutterColumn:
    return next(g for g in column.gutters if g.side is (side or column.gutters[0].side))


async def _open(app: _App, pilot, column: CodeColumn, y: int = 0, side: Side | None = None) -> DiffCommentBar:
    gutter = _number_gutter(column, side)
    await click_when_settled(pilot, gutter, offset=(1, y))
    await wait_for(
        lambda: isinstance(app.screen, DiffCommentBar) and app.screen.is_mounted,
        pilot=pilot,
        description="inline comment opens",
    )
    await pilot.pause()
    return app.screen


async def _add(app: _App, pilot, column: CodeColumn, y: int = 0, text: str = "fix") -> None:
    dialog = await _open(app, pilot, column, y)
    dialog.query_one(DiffCommentInput).load_text(text)
    await pilot.press("enter")
    await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="comment added")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("split", "side", "y", "position", "body"),
    [
        (True, Side.BEFORE, 0, "1", "- \told"),
        (True, Side.AFTER, 0, "1", "+ \tnew"),
        (True, Side.BEFORE, 1, "2", "  same"),
        (True, Side.AFTER, 1, "2", "  same"),
        (False, Side.BEFORE, 0, "1", "- \told"),
        (False, Side.AFTER, 1, "1", "+ \tnew"),
        (False, Side.BEFORE, 2, "2", "  same"),
        (False, Side.AFTER, 2, "2", "  same"),
    ],
)
async def test_number_click_quotes_exactly_one_raw_line_without_selection(split, side, y, position, body) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        columns = await _ready(app, pilot)
        if not split:
            await pilot.press("space")
            await wait_for(
                lambda: len(app.diff.query_one("#turn-2", TabPane).query(CodeColumn)) == 1,
                pilot=pilot,
                description="unified view",
            )
            await pilot.pause()
            columns = list(app.diff.query_one("#turn-2", TabPane).query(CodeColumn))
        column = columns[0] if side is Side.BEFORE or not split else columns[1]
        dialog = await _open(app, pilot, column, y, side)
        assert app.diff.get_selected_text() is None
        assert column.current_comment_row == y
        assert (
            dialog._quote.content
            == f"File: a.py ({'before' if body.startswith('-') else 'after'}) | Source: turn 2\n```\n{position} {body}\n```"
        )


@pytest.mark.asyncio
async def test_scrolled_number_uses_rounded_code_position_and_hover_only_changes_that_cell() -> None:
    old = "\n".join(f"old {i}" for i in range(60))
    entry = replace(_entry(), before_text=old, after_text=old.replace("old", "new"))
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 24)) as pilot:
        left, _right = await _ready(app, pilot)
        left.scroll_y = 10
        await wait_for(lambda: round(left.scroll_offset.y) == 10, pilot=pilot, description="scroll settled")
        gutter = left.gutters[0]
        before = gutter.render_line(1)
        await pilot.hover(gutter, offset=(1, 1))
        await wait_for(lambda: gutter._hover_row == 11, pilot=pilot, description="number hovered")
        assert gutter.render_line(1) != before
        dialog = await _open(app, pilot, left, 1)
        assert dialog._quote.content == "File: a.py (before) | Source: turn 1\n```\n12 - old 11\n```"
        assert left.current_comment_row == 11
        await pilot.press("escape")
        assert left.current_comment_row is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["annotation", "filler", "break", "empty-number"])
async def test_non_numbers_never_open_comment(kind) -> None:
    if kind == "break":
        middle = "\n".join(f"same {i}" for i in range(20))
        entry = replace(_entry(), before_text=f"old\n{middle}\nold", after_text=f"new\n{middle}\nnew")
    else:
        entry = replace(_entry(), before_text="old", after_text="new\nextra")
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 30)) as pilot:
        left, right = await _ready(app, pilot)
        if kind == "annotation":
            target, y = left.gutters[-1], 0
        elif kind == "empty-number":
            await pilot.press("space")
            await wait_for(
                lambda: len(app.diff.query_one("#turn-1", TabPane).query(CodeColumn)) == 1,
                pilot=pilot,
                description="unified view",
            )
            await pilot.pause()
            column = app.diff.query_one("#turn-1", TabPane).query_one(CodeColumn)
            target, y = column.gutters[0], 1
        else:
            target = left.gutters[0]
            row_kind = RowKind.BREAK if kind == "break" else RowKind.FILLER
            y = next(i for i, row in enumerate(left.rows) if row.kind is row_kind)
        await click_when_settled(pilot, target, offset=(1, y))
        assert app.screen is app.diff
        assert not app.comments
        # Positive control on an actual line number.
        await _open(app, pilot, right if kind != "empty-number" else column, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["escape", "outside", "enter"])
async def test_bar_position_modal_keys_highlight_and_close(cancel) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        original = left.render_line(0)
        dialog = await _open(app, pilot, left)
        bar = dialog.query_one("#diff-comment-bar")
        assert bar.region.x == left.scrollable_content_region.x
        assert bar.region.width == left.scrollable_content_region.width
        assert bar.region.y == left.region.y + 1
        assert dialog.region.contains_region(bar.region)
        assert left.render_line(0) != original
        view = next(v for v in left.ancestors if isinstance(v, DiffView))
        await pilot.press("space", "ctrl+g")
        assert view.split is True
        assert app.diff._change_list_visible is True
        if cancel == "outside":
            await click_when_settled(pilot, dialog, offset=(0, 0))
        else:
            await pilot.press(cancel)
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="bar dismissed")
        assert left.current_comment_row is None
        assert all(g.current_comment_row is None for g in left.gutters)
        assert len(app.comments) == (1 if cancel == "enter" else 0)
        if cancel != "enter":
            assert left.render_line(0) == original


@pytest.mark.asyncio
async def test_bottom_bar_flips_above_and_resize_reanchors() -> None:
    entry = replace(
        _entry(),
        before_text="\n".join(f"old {i}" for i in range(60)),
        after_text="\n".join(f"new {i}" for i in range(60)),
    )
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 24)) as pilot:
        left, _ = await _ready(app, pilot)
        y = left.scrollable_content_region.height - 1
        dialog = await _open(app, pilot, left, y)
        bar = dialog.query_one("#diff-comment-bar")
        assert bar.region.height == 5
        assert dialog.query_one(DiffCommentInput).content_size.height == 3
        assert bar.region.bottom == left.region.y + y
        await resize_when_settled(pilot, 80, 20)
        area = left.scrollable_content_region
        # Too narrow for "a.py · Before · Line N": the bar reaches past the column to keep its title whole.
        assert area.width < bar.region.width == 6 + len(str(bar.border_title))
        assert str(bar.border_title).startswith("a.py · Before · Line ")
        assert bar.region.x == area.x
        assert dialog.region.contains_region(bar.region)
        assert bar.region.bottom <= area.y + y


@pytest.mark.asyncio
async def test_add_failure_keeps_comment_and_rechecks_on_enter() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        dialog = await _open(app, pilot, left)
        field = dialog.query_one(DiffCommentInput)
        field.load_text("  keep this  ")
        draft = app.query_one(InputBar)
        draft.locked = True
        await pilot.press("enter")
        assert app.screen is dialog and field.text == "  keep this  "
        assert (
            str(dialog.query_one("#diff-comment-bar").border_subtitle)
            == "The message input cannot take a quote right now."
        )
        assert dialog.query_one("#diff-comment-bar").has_class("-error")
        draft.locked = False
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="retry succeeds")
        assert len(app.comments) == 1
        assert draft.value == "[Quote #1]\n  keep this  \n"


@pytest.mark.asyncio
async def test_context_bubble_only_on_right_and_identity_survives_split_toggle_and_edit() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, right = await _ready(app, pilot)
        await _add(app, pilot, left, 1, "original")
        assert not _number_gutter(left).comment_rows
        gutter = _number_gutter(right)
        assert gutter.comment_rows == frozenset({1})
        assert "💬" not in _number_gutter(left).render_line(1).text
        assert "💬" in gutter.render_line(1).text
        assert "💬" not in left.render_line(1).text
        assert "💬" not in right.render_line(1).text
        # Both entrances on the same line edit the same record.
        dialog = await _open(app, pilot, left, 1)
        assert dialog.query_one(DiffCommentInput).text == "original"
        await pilot.press("escape")
        await click_when_settled(pilot, gutter, offset=(gutter.size.width - 1, 1))
        await wait_for(
            lambda: isinstance(app.screen, DiffCommentBar) and app.screen.is_mounted,
            pilot=pilot,
            description="bubble opens editor",
        )
        app.screen.query_one(DiffCommentInput).load_text("edited")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="edit applied")
        assert len(app.comments) == 1
        assert app.query_one(InputBar).value == "[Quote #1]\nedited\n"
        await pilot.press("space")
        await wait_for(
            lambda: len(app.diff.query_one("#turn-2", TabPane).query(CodeColumn)) == 1,
            pilot=pilot,
            description="unified recompose",
        )
        column = app.diff.query_one("#turn-2", TabPane).query_one(CodeColumn)
        assert _number_gutter(column, Side.AFTER).comment_rows == frozenset({2})
        assert not _number_gutter(column, Side.BEFORE).comment_rows
        dialog = await _open(app, pilot, column, 2, Side.AFTER)
        assert dialog.query_one(DiffCommentInput).text == "edited"
        assert app.diff.get_selected_text() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edited", "absent"])
async def test_changed_comment_cannot_be_opened_or_overwritten(change) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _add(app, pilot, left)
        bar = app.query_one(InputBar)
        draft = bar.draft()
        text = draft.text.replace("fix", "user edit") if change == "edited" else "fix\n"
        bar.set_draft(replace(draft, text=text))
        app.diff._refresh_comments()
        assert bool(_number_gutter(left).comment_rows) is (change != "absent")
        if change != "absent":
            await click_when_settled(pilot, left.gutters[0], offset=(1, 0))
            assert app.screen is app.diff
            assert [str(n.message) for n in app._notifications] == [
                "Added to the message input: [Quote #1]",
                "This comment has changed in the message input. Please edit it there.",
            ]
            assert bar.value == text


@pytest.mark.asyncio
async def test_enter_checks_original_paragraph_again_after_opening() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _add(app, pilot, left)
        dialog = await _open(app, pilot, left)
        draft = app.query_one(InputBar)
        draft.set_draft(replace(draft.draft(), text="[Quote #1]\nchanged elsewhere\n"))
        dialog.query_one(DiffCommentInput).load_text("must not overwrite")
        await pilot.press("enter")
        assert app.screen is dialog
        assert draft.value == "[Quote #1]\nchanged elsewhere\n"
        assert str(dialog.query_one("#diff-comment-bar").border_subtitle) == (
            "This comment has changed in the message input. Please edit it there."
        )
        assert dialog.query_one("#diff-comment-bar").has_class("-error")


@pytest.mark.asyncio
async def test_bubble_lifetime_delete_undo_send_undo_and_reopen_diff() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _add(app, pilot, left)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not app.diff, pilot=pilot, description="back to composer")
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        area.focus()
        area.move_cursor((0, 10))
        await pilot.press("backspace")
        assert bar.probe_diff_comment(app.comments[0].handle()) == "absent"
        app.open_diff()
        left, _ = await _ready(app, pilot)
        assert not _number_gutter(left).comment_rows
        await pilot.press("escape")
        area.focus()
        await pilot.press("ctrl+z")
        app.open_diff()
        left, _ = await _ready(app, pilot)
        assert _number_gutter(left).comment_rows == frozenset({0})
        await pilot.press("escape")
        area.focus()
        await pilot.press("enter")
        assert not bar.value
        app.open_diff()
        left, _ = await _ready(app, pilot)
        assert not _number_gutter(left).comment_rows
        await pilot.press("escape")
        area.focus()
        await pilot.press("ctrl+z")
        app.open_diff()
        left, _ = await _ready(app, pilot)
        assert _number_gutter(left).comment_rows == frozenset({0})


@pytest.mark.asyncio
async def test_bubbles_require_same_tab_file_identity_and_raw_text() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _add(app, pilot, left)
        tabs = app.diff.query_one(TabbedContent)
        tabs.active = "turn-1"
        await wait_for(
            lambda: bool(app.diff.query_one("#turn-1", TabPane).query(CodeColumn)),
            pilot=pilot,
            description="other tab ready",
        )
        assert not any(g.comment_rows for g in app.diff.query_one("#turn-1", TabPane).query(GutterColumn))
        pane = app.diff.query_one("#turn-1", TabPane).query_one(DiffPeriodPane)
        await pane._show_file(_entry("/repo/sub/b.py"))
        assert not any(g.comment_rows for g in pane.query(GutterColumn))
        tabs.active = "turn-2"
        await pilot.pause()
        assert _number_gutter(left).comment_rows == frozenset({0})
        await pilot.press("escape")
        app.entries[2] = [replace(_entry(), before_text="different\n" + "same\n" * 30)]
        app.open_diff()
        left, _ = await _ready(app, pilot)
        assert not _number_gutter(left).comment_rows
        await _add(app, pilot, left, text="new record")
        assert len(app.comments) == 2
        assert _number_gutter(left).comment_rows == frozenset({0})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tab", "cwd", "expected"),
    [
        ("turn-all", "/repo/sub", "File: session.py (before) | Source: session"),
        ("turn-2", "/different", "File: /repo/sub/a.py (before) | Source: turn 2"),
    ],
)
async def test_path_is_from_absolute_comparison_relative_to_chat_directory(tab, cwd, expected) -> None:
    app = _App(cwd=cwd)
    async with app.run_test(size=(100, 30)) as pilot:
        await _ready(app, pilot)
        app.diff.query_one(TabbedContent).active = tab
        await wait_for(
            lambda: bool(app.diff.query_one(f"#{tab}", TabPane).query(CodeColumn)),
            pilot=pilot,
            description="source tab ready",
        )
        column = app.diff.query_one(f"#{tab}", TabPane).query_one(CodeColumn)
        dialog = await _open(app, pilot, column)
        assert dialog._quote.content == expected + "\n```\n1 - \told\n```"


@pytest.mark.asyncio
async def test_workflow_diff_has_no_comment_capability_and_drag_remains_copy_only() -> None:
    app = _App(enabled=False)
    async with app.run_test(size=(100, 30)) as pilot:
        left, right = await _ready(app, pilot)
        for column in (left, right):
            await pilot.hover(column, offset=(4, 0))
            await pilot.hover(_number_gutter(column), offset=(1, 0))
            assert "💬" not in column.render_line(0).text
            assert all("💬" not in g.render_line(0).text and g.tooltip is None for g in column.gutters)
        await click_when_settled(pilot, left.gutters[0], offset=(1, 0))
        assert app.screen is app.diff
        assert not left.comments_enabled
        assert not app.diff.query(QuoteSelectionPopup)
        await pilot.mouse_down(left, offset=(1, 0))
        await pilot.hover(left, offset=(4, 1))
        await pilot.mouse_up(left, offset=(4, 1))
        assert app.diff.get_selected_text()
        assert app.screen is app.diff


@pytest.mark.asyncio
async def test_gutter_bubble_leaves_scrolled_code_and_selection_unchanged() -> None:
    entry = replace(_entry(), before_text="old " * 200, after_text="new " * 200)
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        left.scroll_x = 20
        await pilot.pause()
        raw = left.rows[0].code.plain
        gutter = _number_gutter(left)
        crop = Region(0, 0, gutter.size.width, 1)
        original_number = gutter.render_lines(crop)[0].text
        original_code = left.render_line(0)
        await _add(app, pilot, left)
        assert "💬" in gutter.render_line(0).text
        assert gutter.render_lines(crop)[0].text != original_number
        first = left.render_line(0)
        assert first.text == original_code.text
        assert "💬" not in first.text
        assert first.cell_length == left.scrollable_content_region.width
        assert left.render_line(0) == first
        app.diff.selections = {left: Selection.from_offsets(Offset(0, 0), Offset(10, 0))}
        selected = left.render_line(0)
        assert "💬" not in selected.text
        assert "💬" in gutter.render_line(0).text
        assert left.rows[0].code.plain == raw
        assert app.diff.get_selected_text() == raw[:10]


@pytest.mark.asyncio
async def test_rollback_diff_has_no_comment_capability(tmp_path) -> None:
    import asyncio

    from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
    from tests.app.tui.screens.test_rollback_modal import _make_tracker_with_two_changes

    tracker = await asyncio.to_thread(_make_tracker_with_two_changes, tmp_path)
    modal = RollbackModal(tracker, [0, 1], cwd=str(tmp_path), initial_selection=0)
    app = _Host()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(modal)
        await wait_for(
            lambda: modal._content_ready and bool(modal.query(CodeColumn)), pilot=pilot, description="rollback ready"
        )
        assert all(not view.comments_enabled for view in modal.query(DiffView))
        column = modal.query_one(CodeColumn)
        await pilot.hover(column, offset=(1, 0))
        await pilot.hover(_number_gutter(column), offset=(1, 0))
        assert "💬" not in column.render_line(0).text
        assert all("💬" not in g.render_line(0).text and g.tooltip is None for g in column.gutters)
        await click_when_settled(pilot, column.gutters[0], offset=(1, 0))
        assert app.screen is modal


@pytest.mark.asyncio
async def test_approval_preview_and_default_embedded_diff_have_no_comment_capability() -> None:
    from chrys.app.tui.screens.dialogs.approval.bodies.file_edit import ApprovalDiffPreview

    class PreviewApp(_Host):
        CSS = "ApprovalDiffPreview { height: 8; } DiffView { height: 8; }"

        def compose(self) -> ComposeResult:
            yield ApprovalDiffPreview("a.py", "old", "new", title="Planned Diff")
            yield DiffView("a.py", "a.py", "old", "new", id="embedded")

    app = PreviewApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(
            lambda: app.query_one(ApprovalDiffPreview).is_ready, pilot=pilot, description="approval preview ready"
        )
        for view in app.query(DiffView):
            assert not view.comments_enabled
            assert all(not col.comments_enabled for col in view.query(CodeColumn))
        column = app.query_one(ApprovalDiffPreview).query_one(CodeColumn)
        await click_when_settled(pilot, column.gutters[0], offset=(1, 0))
        assert not isinstance(app.screen, DiffCommentBar)


@pytest.mark.asyncio
async def test_real_main_draft_focus_and_transcript_quote_placement_regression(monkeypatch) -> None:
    from textual.widgets import Button
    from textual.widgets.text_area import Selection as DraftSelection

    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.util.git_branch import GitBranchMonitor
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.foundation.events.bus import EventBus
    from tests.support.pilot_barrier import screen_is_settled

    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)
    app = _Host()
    main = MainScreen(EventBus(), engine_provider=None)
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(main)
        await wait_for(lambda: main.is_mounted, pilot=pilot, description="main mounts")
        await main.query_one(ChatPanel).add_agent_message("Review this change before sending your comments.")
        bar = main.query_one(InputBar)
        bar.value = "keep this"
        area = bar.query_one(_ChatTextArea)
        area.selection = DraftSelection((0, 0), (0, 4))
        original_focus = main.query_one("#editor-btn", Button)
        original_focus.focus()
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="main settles")
        main._view_adapter.open_diff_screen(
            {1: [_entry()]},
            cwd="/repo/sub",
            subtitle_parts=(),
            session_id="",
            all_entries=[],
            draft=main._view_adapter.diff_draft(),
        )
        await wait_for(
            lambda: isinstance(app.screen, DiffScreen) and app.screen.is_mounted and app.screen._content_ready,
            pilot=pilot,
            description="chat diff ready",
        )
        diff = app.screen
        column = diff.query_one(CodeColumn)
        await click_when_settled(pilot, column.gutters[0], offset=(1, 0))
        await wait_for(
            lambda: isinstance(app.screen, DiffCommentBar) and app.screen.is_mounted,
            pilot=pilot,
            description="main comment ready",
        )
        app.screen.query_one(DiffCommentInput).load_text("fix")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is diff, pilot=pilot, description="main comment added")
        assert bar.value == "keep this\n[Quote #1]\nfix\n"
        assert main.focused is original_focus
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="returns to main")
        assert main.focused is original_focus
        assert area.cursor_location == (3, 0)
        assert bar.draft().sent_text() == (
            "keep this\n> File: a.py (before) | Source: turn 1\n> ```\n> 1 - \told\n> ```\n\nfix"
        )
        # Restored MainScreen arrangement pins the conversation button on its target cells.
        popup = main.query_one(QuoteSelectionPopup)
        from textual.geometry import Offset

        popup.place_near(Offset(90, 20))
        main.refresh(layout=True)
        await wait_for(
            lambda: popup.region == popup.target_region, pilot=pilot, description="conversation quote placement"
        )
        assert main.region.contains_region(popup.region)


@pytest.mark.asyncio
async def test_comment_overlay_does_not_restyle_or_relayout_populated_main_or_diff(monkeypatch, tmp_path) -> None:
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.util.git_branch import GitBranchMonitor
    from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter
    from tests.support.pilot_barrier import screen_is_settled
    from tests.support.tui_app_harness import make_chrys_app

    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        await panel.mount(
            *(
                widget
                for index in range(24)
                for widget in (UserMessage(f"Review {index}"), AgentMessage(f"Answer {index} with **code**."))
            )
        )
        await wait_for(
            lambda: len(panel.query(UserMessage)) == 24 and screen_is_settled(app, main),
            pilot=pilot,
            description="populated main settles",
        )
        main._view_adapter.open_diff_screen(
            {1: [_entry()]},
            cwd="/repo/sub",
            subtitle_parts=(),
            session_id="",
            all_entries=[],
            draft=main._view_adapter.diff_draft(),
        )
        await wait_for(
            lambda: isinstance(app.screen, DiffScreen) and app.screen.is_mounted and app.screen._content_ready,
            pilot=pilot,
            description="diff mounted",
        )
        diff = app.screen
        await wait_for(lambda: screen_is_settled(app, diff), pilot=pilot, description="diff settles")
        calls = []

        def record_styles(animate=True):
            calls.append("style")

        def record_layout(*args, **kwargs):
            calls.append("layout")

        async def record_footer():
            calls.append("footer")

        with monkeypatch.context() as probe:
            for screen in (main, diff):
                probe.setattr(screen, "update_node_styles", record_styles)
                probe.setattr(screen, "_refresh_layout", record_layout)
                probe.setattr(screen.query_one(ChrysFooter), "recompose", record_footer)
            column = diff.query_one(CodeColumn)
            await click_when_settled(pilot, column.gutters[0], offset=(1, 0))
            await wait_for(
                lambda: isinstance(app.screen, DiffCommentBar) and app.screen.is_mounted,
                pilot=pilot,
                description="overlay mounted",
            )
            await pilot.press("x", "escape")
            await wait_for(lambda: app.screen is diff, pilot=pilot, description="overlay canceled")
            assert not calls, "\n".join(calls)


@pytest.mark.asyncio
async def test_dragging_numbers_or_code_does_not_become_a_comment_click() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        gutter = left.gutters[0]
        await pilot.mouse_down(gutter, offset=(1, 0))
        await pilot.hover(gutter, offset=(1, 1))
        await pilot.mouse_up(gutter, offset=(1, 1))
        assert app.screen is app.diff
        await pilot.mouse_down(left, offset=(1, 0))
        await pilot.hover(left, offset=(4, 1))
        await pilot.mouse_up(left, offset=(4, 1))
        assert app.diff.get_selected_text()
        assert app.screen is app.diff
        assert not app.diff.query(QuoteSelectionPopup)
        await _open(app, pilot, left)
        assert app.diff.get_selected_text() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_short_terminal_retains_both_key_hints_and_comment_on_resize(locale) -> None:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import Localizer

    app = _App()
    app.locale_controller = LocaleController(localizer=Localizer(locale))
    async with app.run_test(size=(60, 17)) as pilot:
        _left, right = await _ready(app, pilot)
        dialog = await _open(app, pilot, right)
        bar = dialog.query_one("#diff-comment-bar")
        field = dialog.query_one(DiffCommentInput)
        field.load_text("保留大小写" if locale == "zh-Hans" else "keep case")
        footer = bar.render_lines(Region(0, 0, bar.size.width, 5))[4].text
        assert ("Enter" in footer or "↵" in footer) and "Esc" in footer
        await resize_when_settled(pilot, 80, 24)
        assert dialog.region.contains_region(bar.region)
        assert field.text == ("保留大小写" if locale == "zh-Hans" else "keep case")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="narrow comment added")
        label = "[引用 #1]" if locale == "zh-Hans" else "[Quote #1]"
        assert app.query_one(InputBar).value == f"{label}\n{field.text}\n"
        assert app.comments[0].handle().content == "File: a.py (after) | Source: turn 2\n```\n1 + \tnew\n```"


@pytest.mark.asyncio
async def test_selection_background_overrides_current_comment_highlight() -> None:
    from chrys.app.tui.widgets import normalize_selection_rich_style

    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _open(app, pilot, left)
        app.diff.selections = {left: Selection.from_offsets(Offset(0, 0), Offset(3, 0))}
        strip = left.render_line(0)
        expected = normalize_selection_rich_style(app.diff.get_component_rich_style("screen--selection")).bgcolor
        first = next(segment for segment in strip if segment.text)
        assert first.style.bgcolor == expected
        assert left.current_comment_row == 0
        await pilot.press("escape")
        assert left.current_comment_row is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "add_tip", "edit_tip"),
    [
        (
            "en",
            "Comment on this line. It goes into the message input; send it from the chat.",
            "Edit this comment",
        ),
        ("zh-Hans", "评论这一行。写好后放进输入框，回到聊天再发送", "修改这条评论"),  # noqa: RUF001
    ],
)
async def test_hovered_number_bubble_opens_comment_and_persists_with_edit_tooltip(locale, add_tip, edit_tip) -> None:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import Localizer

    app = _App()
    app.locale_controller = LocaleController(localizer=Localizer(locale))
    async with app.run_test(size=(100, 30)) as pilot:
        _left, right = await _ready(app, pilot)
        gutter = _number_gutter(right)
        edge = gutter.size.width - 1
        number = gutter.render_line(0).text
        assert number[1:].strip() == "1"
        await pilot.hover(right, offset=(4, 0))
        await wait_for(lambda: "💬" in gutter.render_line(0).text, pilot=pilot, description="number becomes bubble")
        assert "1" not in gutter.render_line(0).text
        assert "💬" not in gutter.render_line(1).text
        assert "💬" not in right.render_line(0).text
        assert right.tooltip is None
        await pilot.hover(gutter, offset=(edge, 0))
        assert str(gutter.tooltip) == add_tip
        await click_when_settled(pilot, gutter, offset=(edge, 0))
        await wait_for(
            lambda: isinstance(app.screen, DiffCommentBar) and app.screen.is_mounted,
            pilot=pilot,
            description="number bubble opens comment",
        )
        assert app.diff.get_selected_text() is None
        assert gutter._hover_row is None
        app.screen.query_one(DiffCommentInput).load_text("fix")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="comment added")
        assert gutter.comment_rows == frozenset({0})
        await pilot.hover(right, offset=(4, 1))
        assert "💬" in gutter.render_line(0).text
        assert "💬" not in right.render_line(0).text
        await pilot.hover(gutter, offset=(edge, 0))
        assert str(gutter.tooltip) == edit_tip
        await click_when_settled(pilot, gutter, offset=(edge, 0))
        await wait_for(
            lambda: isinstance(app.screen, DiffCommentBar) and app.screen.is_mounted,
            pilot=pilot,
            description="persistent number bubble edits comment",
        )
        assert app.screen.query_one(DiffCommentInput).text == "fix"
        assert len(app.comments) == 1


@pytest.mark.asyncio
async def test_code_hover_restores_numbers_on_move_leave_and_scroll() -> None:
    old = "\n".join(f"old {i}" for i in range(60))
    entry = replace(_entry(), before_text=old, after_text=old.replace("old", "new"))
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 24)) as pilot:
        _left, right = await _ready(app, pilot)
        gutter = _number_gutter(right)
        numbers = [gutter.render_line(y).text for y in (2, 3)]
        await pilot.hover(right, offset=(4, 2))
        assert "💬" in gutter.render_line(2).text
        assert gutter.render_line(3).text == numbers[1]
        await pilot.hover(right, offset=(4, 3))
        assert gutter.render_line(2).text == numbers[0]
        assert "💬" in gutter.render_line(3).text
        await pilot.hover(app.diff.query_one(TabbedContent), offset=(1, 0))
        assert [gutter.render_line(y).text for y in (2, 3)] == numbers
        await pilot.hover(right, offset=(4, 3))
        assert "💬" in gutter.render_line(3).text
        right.scroll_y = 2
        await wait_for(lambda: round(right.scroll_offset.y) == 2, pilot=pilot, description="scroll settled")
        assert gutter.render_line(3 - 2).text == numbers[1]
        assert all("💬" not in gutter.render_line(y).text for y in range(right.scrollable_content_region.height))
        await pilot.hover(right, offset=(4, 3))
        assert "💬" in gutter.render_line(3).text
        assert "💬" not in gutter.render_line(1).text


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["code", "number"])
async def test_split_hover_shows_only_pointed_side_and_left_context_uses_right_number(target) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, right = await _ready(app, pilot)
        old, new = _number_gutter(left), _number_gutter(right)
        x = 4 if target == "code" else 1
        await pilot.hover(left if target == "code" else old, offset=(x, 0))
        assert "💬" in old.render_line(0).text
        assert "💬" not in new.render_line(0).text
        await pilot.hover(right if target == "code" else new, offset=(x, 0))
        assert "💬" not in old.render_line(0).text
        assert "💬" in new.render_line(0).text
        await pilot.hover(left if target == "code" else old, offset=(x, 1))
        assert "💬" not in old.render_line(1).text
        assert "💬" in new.render_line(1).text
        assert "💬" not in new.render_line(0).text
        assert all("💬" not in col.render_line(1).text for col in (left, right))


@pytest.mark.parametrize("target", ["code", "old-number", "new-number"])
async def test_unified_hover_uses_old_number_for_removed_and_new_number_for_added_and_context(target) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        await _ready(app, pilot)
        await pilot.press("space")
        pane = app.diff.query_one("#turn-2", TabPane)
        await wait_for(lambda: len(pane.query(CodeColumn)) == 1, pilot=pilot, description="unified view ready")
        await pilot.pause()
        column = pane.query_one(CodeColumn)
        old, new = _number_gutter(column, Side.BEFORE), _number_gutter(column, Side.AFTER)
        widget = {"code": column, "old-number": old, "new-number": new}[target]
        x = 4 if target == "code" else 1
        for y, side in ((0, Side.BEFORE), (1, Side.AFTER), (2, Side.AFTER)):
            await pilot.hover(widget, offset=(x, y))
            assert ("💬" in old.render_line(y).text) is (side is Side.BEFORE)
            assert ("💬" in new.render_line(y).text) is (side is Side.AFTER)
            assert "💬" not in column.render_line(y).text
            assert "💬" not in column.gutters[-1].render_line(y).text


@pytest.mark.parametrize("start_at", ["start", "middle", "text-end", "viewport-end"])
@pytest.mark.parametrize("long_line", [False, True])
async def test_drag_from_any_code_position_including_line_end_copies_code(start_at, long_line) -> None:
    text = "begin " + ("wide " * 30 if long_line else "short")
    entry = replace(_entry(), before_text="old\n" * 4, after_text="first\n" + text + "\nfinish\nlast")
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 30)) as pilot:
        _left, right = await _ready(app, pilot)
        if long_line:
            right.scroll_x = 5
            await wait_for(lambda: right.scroll_offset.x == 5, pilot=pilot, description="horizontal scroll")
        last = right.scrollable_content_region.width - 1
        x = {"start": 0, "middle": 4, "text-end": min(last, len(text) - 1), "viewport-end": last}[start_at]
        await pilot.hover(right, offset=(x, 1))
        assert "💬" in _number_gutter(right).render_line(1).text
        assert "💬" not in right.render_line(1).text
        widget, offset = app.diff.get_widget_and_offset_at(right.region.x + x, right.region.y + 1)
        assert widget is right
        assert offset is not None and offset == Offset(x + int(right.scroll_offset.x), 1)
        await pilot.mouse_down(right, offset=(x, 1))
        await pilot.hover(right, offset=(3, 2))
        await pilot.mouse_up(right, offset=(3, 2))
        assert app.screen is app.diff
        assert not app.comments
        start = min(len(text), offset.x)
        # Textual includes the character under the release pointer.
        end = 4 + int(right.scroll_offset.x)
        assert app.diff.get_selected_text() == text[start:] + "\n" + "finish"[:end]


async def test_horizontal_scrollbar_row_in_gutter_never_opens_a_comment() -> None:
    entry = replace(
        _entry(),
        before_text="\n".join(f"old {i} " + "wide " * 20 for i in range(30)),
        after_text="\n".join(f"new {i} " + "wide " * 20 for i in range(30)),
    )
    app = _App(entries={1: [entry]})
    async with app.run_test(size=(100, 24)) as pilot:
        left, _right = await _ready(app, pilot)
        assert left.show_horizontal_scrollbar is True
        y = left.scrollable_content_region.height
        assert y < left.size.height and y < len(left.rows)
        await click_when_settled(pilot, left.gutters[0], offset=(1, y))
        assert app.screen is app.diff
        assert not app.comments
        await _open(app, pilot, left)


async def test_truncation_toast_and_hash_record_are_created_through_number_click(monkeypatch) -> None:
    import chrys.app.tui.screens.diff.screen as module
    from chrys.app.tui.util.diff_comments import line_hash
    from chrys.app.tui.widgets.diff_view.quote import build_diff_quote
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    raw = "word " * 500
    entry = replace(_entry(), after_text=raw)
    app = _App(entries={1: [entry]})
    original = build_diff_quote

    def bounded(row, lines, path, source):
        return original(row, lines, path, source, max_tokens=80)

    monkeypatch.setattr(module, "build_diff_quote", bounded)
    monkeypatch.setattr(module, "MESSAGE_EDITOR_PASTE_MAX_TOKENS", 80)
    async with app.run_test(size=(100, 30)) as pilot:
        _left, right = await _ready(app, pilot)
        dialog = await _open(app, pilot, right)
        quote = dialog._quote
        assert quote.truncated is True
        assert MixedLanguageTokenizer().count_tokens(quote.content) <= 80
        assert quote.content == "File: a.py (after) | Source: turn 1\n```\n1 + " + "word " * 54 + "word…\n```"
        notices = [n for n in app._notifications if n.title == "Quote truncated"]
        assert [(str(n.message), n.severity) for n in notices] == [
            (f"Quote truncated: {quote.original_tokens:,} tokens exceeds the 80-token limit.", "warning")
        ]
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="truncated quote added")
        record = app.comments[0]
        assert record.raw_hash == line_hash(raw)
        assert len(record.raw_hash) == 64
        assert set(vars(record)) == {"handle", "tab", "path", "line", "raw_hash"}
        assert _number_gutter(right).comment_rows == frozenset({0})


@pytest.mark.parametrize("clear", ["clear", "load_text", "write_draft", "history_eviction"])
async def test_comment_records_expire_when_draft_and_undo_history_release_the_reference(clear: str) -> None:
    import gc

    from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft

    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _add(app, pilot, left)
        reference = app.comments[0].handle
        assert reference() is not None
        await pilot.press("escape")
        area = app.query_one(InputBar).query_one(_ChatTextArea)
        if clear == "clear":
            area.clear()
        elif clear == "load_text":
            area.load_text("")
        elif clear == "write_draft":
            area.write_draft(ChatDraft(""))
        else:
            # Sending keeps the quote alive only through the undo step.
            area.leave_draft()
            assert reference() is not None
            area.history.clear()
        await pilot.pause()
        gc.collect()
        assert reference() is None
        assert len(app.comments) == 1
        app.open_diff()
        left, _ = await _ready(app, pilot)
        assert app.comments == []
        assert not _number_gutter(left).comment_rows


@pytest.mark.parametrize("newline", ["shift+enter", "ctrl+enter", "ctrl+j"])
async def test_multiline_comment_enter_submits_and_bubble_edits_with_one_undo(newline) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        dialog = await _open(app, pilot, left)
        field = dialog.query_one(DiffCommentInput)
        bar = dialog.query_one("#diff-comment-bar")
        assert field.content_size.height == 3
        assert bar.region.height == 5
        await pilot.press("a", newline, "b", newline, "c")
        assert app.screen is dialog
        assert field.text == "a\nb\nc"
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="multiline comment added")
        draft = app.query_one(InputBar)
        assert draft.value == "[Quote #1]\na\nb\nc\n"
        assert draft.diff_comment(app.comments[0].handle()) == "a\nb\nc"
        dialog = await _open(app, pilot, left)
        field = dialog.query_one(DiffCommentInput)
        assert field.text == "a\nb\nc"
        await pilot.press("ctrl+a", "x", newline, "y", "enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="multiline comment edited")
        assert draft.value == "[Quote #1]\nx\ny\n"
        assert draft.diff_comment(app.comments[0].handle()) == "x\ny"
        await pilot.press("escape")
        area = draft.query_one(_ChatTextArea)
        area.focus()
        await pilot.press("ctrl+z")
        assert draft.value == "[Quote #1]\na\nb\nc\n"
        await pilot.press("ctrl+z")
        assert draft.value == ""


async def test_undoing_an_edit_in_the_chat_input_leaves_the_comment_editable_from_the_diff() -> None:
    """After Ctrl+Z restores an earlier comment, its 💬 opens that comment and saves over it."""
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        await _add(app, pilot, left, text="original")
        await _add(app, pilot, left, text="updated")
        draft = app.query_one(InputBar)
        assert draft.value == "[Quote #1]\nupdated\n"
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not app.diff, pilot=pilot, description="diff closed")
        draft.query_one(_ChatTextArea).focus()
        await pilot.press("ctrl+z")
        assert draft.value == "[Quote #1]\noriginal\n"

        app.open_diff()
        left, _ = await _ready(app, pilot)
        dialog = await _open(app, pilot, left)
        field = dialog.query_one(DiffCommentInput)
        assert field.text == "original"
        field.load_text("again")
        await pilot.press("enter")
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="comment edited")

        assert draft.value == "[Quote #1]\nagain\n"


@pytest.mark.parametrize(
    ("keys", "comment"),
    [(("backspace",), "ab"), (("left", "delete"), "ab"), (("ctrl+z",), "")],
    ids=["backspace", "delete", "undo"],
)
async def test_enter_typed_at_once_adds_what_the_keys_before_it_left(keys, comment) -> None:
    """Enter adds the comment the edits typed just before it left, not the text before them."""
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        dialog = await _open(app, pilot, left)
        await pilot.press("a", "b", "c")
        for key in (*keys, "enter"):
            app.post_message(events.Key(key, "\r" if key == "enter" else None))
        await wait_for(lambda: app.screen is app.diff, pilot=pilot, description="comment added")

        assert app.query_one(InputBar).value == "[Quote #1]\n" + (comment + "\n" if comment else "")
        assert dialog.is_attached is False


@pytest.mark.parametrize("switch", ["layout", "tab", "file"])
async def test_a_click_whose_line_a_switch_queued_behind_it_hid_opens_nothing(switch) -> None:
    """A tab, file or layout switch handled before the click leaves no comment bar for a line no longer shown."""
    app = _App(entries={1: [_entry("/repo/sub/one.py")], 2: [_entry("/repo/sub/two.py")]})
    async with app.run_test(size=(100, 30)) as pilot:
        left, _ = await _ready(app, pilot)
        tabs = app.diff.query_one(TabbedContent)
        view = next(ancestor for ancestor in left.ancestors if isinstance(ancestor, DiffView))
        gutter = _number_gutter(left)
        click = {"x": 1, "y": 0, "delta_x": 0, "delta_y": 0, "button": 1, "shift": False, "meta": False, "ctrl": False}
        gutter.on_mouse_down(events.MouseDown(gutter, **click))
        gutter.on_click(events.Click(gutter, **click))
        if switch == "layout":
            view.split = not view.split
        elif switch == "tab":
            tabs.active = "turn-1" if tabs.active != "turn-1" else "turn-2"
        else:
            view.display = False  # What showing another file does to this one.
        for _ in range(3):
            await pilot.pause()

        assert app.screen is app.diff
        assert app.return_value is None


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_all_tab_comment_title_names_the_file_and_line(locale) -> None:
    from textual.widgets import Tab

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import Localizer

    app = _App()
    app.locale_controller = LocaleController(localizer=Localizer(locale))
    async with app.run_test(size=(140, 30)) as pilot:
        await _ready(app, pilot)
        tabs = app.diff.query_one(TabbedContent)
        assert [tab.label.plain for tab in tabs.query(Tab)] == (
            ["T1", "T2", "All"] if locale == "en" else ["第 1 轮", "第 2 轮", "全部"]
        )
        tabs.active = "turn-all"
        pane = app.diff.query_one("#turn-all", TabPane)
        await wait_for(lambda: bool(pane.query(CodeColumn)), pilot=pilot, description="All diff ready")
        dialog = await _open(app, pilot, pane.query_one(CodeColumn))
        title = str(dialog.query_one("#diff-comment-bar").border_title)
        assert title == ("session.py · Before · Line 1" if locale == "en" else "session.py · 修改前 · 第 1 行")
        assert dialog._quote.content == "File: session.py (before) | Source: session\n```\n1 - \told\n```"


_LONG_FILE = "".join(f"line {i}\n" for i in range(1, 10_010))


@pytest.mark.parametrize("width", [60, 80, 100, 120])
@pytest.mark.parametrize(
    ("locale", "path", "kind", "suffix"),
    [
        ("en", "/repo/sub/a.py", RowKind.REMOVED, " · Before · Line 10005"),
        ("zh-Hans", "/repo/sub/a.py", RowKind.ADDED, " · 修改后 · 第 10005 行"),
        ("en", "/repo/sub/services/authentication/session_store.py", RowKind.ADDED, " · After · Line 10005"),
        ("zh-Hans", "/repo/sub/services/authentication/session_store.py", RowKind.CONTEXT, " · 修改后 · 第 10004 行"),
    ],
)
async def test_title_keeps_the_version_and_line_and_elides_only_the_path(width, locale, path, kind, suffix) -> None:
    from rich.cells import cell_len

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import Localizer

    entry = replace(
        _entry(path), before_text=_LONG_FILE, after_text=_LONG_FILE.replace("line 10005\n", "changed 10005\n")
    )
    app = _App(entries={1: [entry]})
    app.locale_controller = LocaleController(localizer=Localizer(locale))
    async with app.run_test(size=(width, 30)) as pilot:
        left, right = await _ready(app, pilot)
        column = left if kind is RowKind.REMOVED else right
        y = next(
            index
            for index, row in enumerate(column.rows)
            if row.kind is kind and (kind is not RowKind.CONTEXT or row.after == 10_004)
        )
        dialog = await _open(app, pilot, column, y)
        bar = dialog.query_one("#diff-comment-bar")
        title = str(bar.border_title)
        name = path.removeprefix("/repo/sub/")
        assert title.endswith(suffix)
        shown = title.removesuffix(suffix)
        assert shown == name or ("…" in shown and shown[0] == name[0] and shown[-1] == name[-1])
        assert cell_len(title) <= bar.outer_size.width - 6
        view = next(ancestor for ancestor in column.ancestors if isinstance(ancestor, DiffView))
        assert view.region.contains_region(bar.region)


@pytest.mark.parametrize("repository", [False, True])
async def test_empty_session_diff_notifies_and_closes_even_with_repository_changes(
    tmp_path, git_repo_factory, repository
) -> None:
    import asyncio

    from chrys.app.tui.util.diff_entries import DiffLoadResult

    if repository:
        await asyncio.to_thread(git_repo_factory, tmp_path)
    await asyncio.to_thread((tmp_path / "new.py").write_text, "new\n", encoding="utf-8")

    async def load_data() -> DiffLoadResult:
        return DiffLoadResult.empty()

    class EmptyApp(_Host):
        def on_mount(self) -> None:
            self.diff = DiffScreen({}, cwd=str(tmp_path), load_data=load_data)
            self.push_screen(self.diff)

    app = EmptyApp()
    async with app.run_test() as pilot:
        await wait_for(lambda: not app.diff.is_attached, pilot=pilot, description="empty diff closes")
        assert not isinstance(app.screen, DiffScreen)
        assert [(str(n.message), n.severity) for n in app._notifications] == [("No file changes to show", "warning")]


async def test_comments_use_the_lines_split_while_the_diff_was_prepared(monkeypatch) -> None:
    import chrys.app.tui.widgets.diff_view.widget as widget_module

    app = _App(entries={1: [_entry()]})
    async with app.run_test(size=(100, 30)) as pilot:
        _left, right = await _ready(app, pilot)

        def split_again(code: str) -> list[str]:
            raise AssertionError("a click splits the whole file again")

        monkeypatch.setattr(widget_module, "compared_lines", split_again)
        await _add(app, pilot, right)
        assert [record.line for record in app.comments] == [("after", 1)]
        dialog = await _open(app, pilot, right)
        assert dialog._record is app.comments[0]


async def test_reopening_a_diff_with_comments_splits_its_lines_off_the_event_loop(monkeypatch) -> None:
    import threading

    import chrys.app.tui.widgets.diff_view.widget as widget_module

    original = widget_module.compared_lines
    on_event_loop: list[bool] = []

    def recorded(code: str) -> list[str]:
        on_event_loop.append(threading.current_thread() is threading.main_thread())
        return original(code)

    monkeypatch.setattr(widget_module, "compared_lines", recorded)
    app = _App(entries={1: [_entry()]})
    async with app.run_test(size=(100, 30)) as pilot:
        _left, right = await _ready(app, pilot)
        await _add(app, pilot, right)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not app.diff, pilot=pilot, description="diff closed")
        on_event_loop.clear()
        app.open_diff()
        _left, right = await _ready(app, pilot)
        assert _number_gutter(right).comment_rows == frozenset({0})
        assert on_event_loop == [False, False]
