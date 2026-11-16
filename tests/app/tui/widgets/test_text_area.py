# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the reusable EnhancedTextArea widget."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.events import Key, MouseDown, Paste
from textual.geometry import Size
from textual.widgets import TextArea
from textual.widgets.text_area import Selection

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from chrys.foundation.config.settings import Settings
from tests.support.waiting import wait_for


class _Harness(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def __init__(self, *, max_paste_tokens: int | None = None, text_area: EnhancedTextArea | None = None) -> None:
        super().__init__()
        self._max_paste_tokens = max_paste_tokens
        self._text_area = text_area
        self.notifications: list[tuple[str, dict]] = []

    def compose(self) -> ComposeResult:
        yield (
            self._text_area
            if self._text_area is not None
            else EnhancedTextArea(id="ta", max_paste_tokens=self._max_paste_tokens)
        )

    def notify(self, message: str, **kwargs) -> None:  # type: ignore[override]
        self.notifications.append((message, kwargs))


@pytest.mark.asyncio
async def test_ctrl_a_selects_all() -> None:
    """Ctrl+A should select the full document, not jump to line start."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.text = "hello\nworld"
        ta.focus()
        await wait_for(lambda: ta.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("ctrl+a")
        await pilot.pause()

        assert ta.selected_text == "hello\nworld"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_shift_enter_replaces_selection_once_and_respects_read_only(read_only: bool) -> None:
    ta = EnhancedTextArea("aOLDz", read_only=read_only, id="ta")
    app = _Harness(text_area=ta)
    async with app.run_test() as pilot:
        ta.focus()
        ta.selection = Selection((0, 1), (0, 4))
        await pilot.press("shift+enter")

        assert ta.text == ("aOLDz" if read_only else "a\nz")
        if not read_only:
            assert ta.cursor_location == (1, 0)
            await pilot.press("ctrl+z")
            assert ta.text == "aOLDz"


@pytest.mark.asyncio
async def test_multi_character_printable_key_inserts_text() -> None:
    """IME commits decoded from Kitty keyboard input may arrive as one key event."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.focus()
        await pilot.pause()

        await ta._on_key(Key("当前", "当前"))
        await pilot.pause()

        assert ta.text == "当前"
        assert ta.cursor_location == (0, 2)


@pytest.mark.asyncio
async def test_right_click_copies_selection_then_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        ta = app.query_one("#ta", EnhancedTextArea)
        ta.text = "copy me"
        ta.selection = Selection((0, 0), (0, 4))
        await pilot.pause()

        await ta._on_mouse_down(
            MouseDown(
                ta,
                x=0,
                y=0,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=ta.region.x,
                screen_y=ta.region.y,
            )
        )

        assert app.clipboard == "copy"
        assert copied == ["copy"]
        assert ta.selected_text == ""


@pytest.mark.asyncio
async def test_mouse_down_dispatch_invokes_text_area_base_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """EnhancedTextArea should let Textual dispatch the base mouse handler once."""

    class _MouseProbe(EnhancedTextArea):
        pass

    base_calls: list[MouseDown] = []

    async def base_mouse_down_spy(_text_area: TextArea, event: MouseDown) -> None:
        base_calls.append(event)

    monkeypatch.setattr(TextArea, "_on_mouse_down", base_mouse_down_spy)
    app = _Harness(text_area=_MouseProbe(id="ta"))
    async with app.run_test():
        ta = app.query_one("#ta", EnhancedTextArea)
        event = MouseDown(
            ta,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=ta.region.x,
            screen_y=ta.region.y,
        )

        await ta._on_message(event)

        assert base_calls == [event]


@pytest.mark.asyncio
async def test_right_click_dispatch_prevents_text_area_base_mouse_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right-click copy should prevent Textual from running the base mouse handler."""

    class _MouseProbe(EnhancedTextArea):
        pass

    base_calls: list[MouseDown] = []

    async def base_mouse_down_spy(_text_area: TextArea, event: MouseDown) -> None:
        base_calls.append(event)

    monkeypatch.setattr(TextArea, "_on_mouse_down", base_mouse_down_spy)
    app = _Harness(text_area=_MouseProbe(id="ta"))
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
        ta = app.query_one("#ta", EnhancedTextArea)
        # Prove this instance's dispatch plan reaches the spy before asserting
        # that right-click handling suppresses it through the same warm plan.
        control = MouseDown(ta, x=0, y=0, delta_x=0, delta_y=0, button=1, shift=False, meta=False, ctrl=False)
        control.stop()
        await ta._on_message(control)
        assert base_calls == [control]
        base_calls.clear()
        ta.text = "copy me"
        ta.selection = Selection((0, 0), (0, 4))
        await pilot.pause()
        event = MouseDown(
            ta,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=3,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=ta.region.x,
            screen_y=ta.region.y,
        )

        await ta._on_message(event)

        assert copied == ["copy"]
        assert ta.selected_text == ""
        assert event._no_default_action is True
        assert base_calls == []


@pytest.mark.asyncio
async def test_check_action_disables_select_keybindings() -> None:
    """F5/F6/F7 selection actions are disabled so they don't conflict with screen bindings."""
    app = _Harness()
    async with app.run_test():
        ta = app.query_one("#ta", EnhancedTextArea)
        assert ta.check_action("select_all", ()) is False
        assert ta.check_action("select_word", ()) is False
        assert ta.check_action("select_line", ()) is False
        # Untouched actions delegate to the base class.
        assert ta.check_action("cursor_up", ()) is not False


@pytest.mark.asyncio
async def test_paste_normalizes_line_endings() -> None:
    """CRLF and bare CR in pasted text are collapsed to LF."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.focus()
        await pilot.pause()

        await ta._on_paste(Paste("a\r\nb\rc"))
        await pilot.pause()

        assert ta.text == "a\nb\nc"


@pytest.mark.asyncio
async def test_paste_skipped_when_read_only() -> None:
    """Read-only widgets must not absorb pasted text."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.read_only = True
        ta.focus()
        await pilot.pause()

        await ta._on_paste(Paste("ignored"))
        await pilot.pause()

        assert ta.text == ""


@pytest.mark.asyncio
async def test_paste_truncates_when_token_cap_set() -> None:
    """When ``max_paste_tokens`` is set, oversized pastes are clipped."""
    app = _Harness(max_paste_tokens=10)
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.focus()
        await pilot.pause()

        long = "word " * 200
        await ta._on_paste(Paste(long))
        await pilot.pause()

        assert ta.text != long
        assert len(ta.text) < len(long)
        assert app.notifications
        message, kwargs = app.notifications[-1]
        assert "Paste truncated" in message
        assert kwargs.get("severity") == "warning"


@pytest.mark.asyncio
async def test_paste_unbounded_when_no_token_cap() -> None:
    """With ``max_paste_tokens=None`` even very large pastes flow through verbatim."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.focus()
        await pilot.pause()

        long = "word " * 1000
        await ta._on_paste(Paste(long))
        await pilot.pause()

        assert ta.text == long.replace("\r\n", "\n").replace("\r", "\n")
        assert not app.notifications


@pytest.mark.asyncio
async def test_action_copy_uses_cross_platform_clipboard() -> None:
    """``action_copy`` must hit both ``app.clipboard`` and the OS clipboard."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.text = "abc"
        ta.focus()
        await pilot.pause()
        ta.select_all()
        await pilot.pause()

        with patch("chrys.app.tui.clipboard.clipboard_copy") as os_copy:
            ta.action_copy()
        assert os_copy.call_args.args == ("abc",)
        assert app.clipboard == "abc"


@pytest.mark.asyncio
async def test_action_copy_defers_key_when_nothing_to_copy() -> None:
    """With no widget or screen selection the copy action must skip, not swallow.

    Stock ``TextArea.action_copy`` raises ``SkipAction`` on an empty selection
    so ctrl+c can reach the app-level quit hint; the override must keep that.
    """
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.focus()
        await pilot.pause()

        with pytest.raises(SkipAction):
            ta.action_copy()


@pytest.mark.asyncio
async def test_action_paste_prefers_fresher_os_clipboard() -> None:
    """Native paste must not let Textual's stale local cache shadow the OS clipboard."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.focus()
        app.copy_to_clipboard("stale-app-text")
        await pilot.pause()

        with patch("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", return_value="from-os"):
            ta.action_paste()
        await pilot.pause()
        assert ta.text == "from-os"


@pytest.mark.asyncio
async def test_get_component_styles_tolerates_unapplied_styles() -> None:
    """Textual #6208: a mid-recompose render must not crash on the gutter class.

    When a recompose fires during active event processing the stylesheet may
    not have populated ``_component_styles`` yet, so a *declared* component
    class (e.g. ``text-area--gutter``) is briefly missing. The base widget
    raises ``KeyError`` there; ``EnhancedTextArea`` must degrade gracefully.
    """
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        await pilot.pause()

        # ``text-area--gutter`` is a class the widget declares.
        assert "text-area--gutter" in type(ta)._get_component_classes()

        # Simulate the race window: styles applied, then cleared before render.
        ta._component_styles.clear()

        # Must not raise for a declared-but-unmaterialized component class.
        styles = ta.get_component_styles("text-area--gutter")
        assert styles is not None

        # A component class the widget never declares is still a real error.
        with pytest.raises(KeyError):
            ta.get_component_styles("not-a-real-component-class")


@pytest.mark.asyncio
async def test_render_line_with_zero_content_width_returns_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Very narrow terminals can give TextArea zero content width."""
    app = _Harness()
    async with app.run_test() as pilot:
        ta = app.query_one("#ta", EnhancedTextArea)
        ta.placeholder = "Type a custom response..."
        await pilot.pause()

        monkeypatch.setattr(EnhancedTextArea, "content_size", property(lambda _self: Size(0, 1)))

        rendered = ta.render_line(0)

        assert rendered.cell_length == 0


def _queued_key_area(kind: str) -> EnhancedTextArea:
    from chrys.app.tui.screens.dialogs.approval.dialog import _ApprovalReasonTextArea
    from chrys.app.tui.screens.diff.comment_bar import DiffCommentInput
    from chrys.app.tui.widgets.ask_user_controls import _AskUserTextArea
    from chrys.app.tui.widgets.editor import EditorMode, MessageEditor

    if kind.startswith("editor-"):
        return MessageEditor(text="", mode=EditorMode(kind.removeprefix("editor-")))
    return {
        "plain": EnhancedTextArea,
        "approval-reason": _ApprovalReasonTextArea,
        "ask-user": _AskUserTextArea,
        "diff-comment": DiffCommentInput,
    }[kind]()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["plain", "approval-reason", "ask-user", "diff-comment", "editor-standard", "editor-emacs", "editor-vim"],
)
@pytest.mark.parametrize(
    ("queued", "expected"),
    [
        ((("backspace", None), ("x", "x")), "abx"),
        ((("left", None), ("backspace", None), ("x", "x")), "axc"),
        ((("ctrl+z", None), ("x", "x")), "x"),
    ],
    ids=["backspace", "move-backspace", "undo"],
)
async def test_keys_typed_at_once_take_effect_in_the_order_typed(kind, queued, expected) -> None:
    """A binding typed ahead of a character runs first in every multi-line input and keymap."""
    area = _queued_key_area(kind)
    app = _Harness(text_area=area)
    async with app.run_test() as pilot:
        area.focus()
        await wait_for(lambda: area.has_focus, pilot=pilot, description="input focused")
        if kind == "editor-vim":
            await pilot.press("i")
        await pilot.press("a", "b", "c")
        for key, character in queued:
            app.post_message(Key(key, character))
        await wait_for(lambda: area.text != "abc", pilot=pilot, description="queued keys handled")
        await pilot.pause()

        assert area.text == expected
