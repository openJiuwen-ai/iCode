# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for tool cards: ClickAffordance dispatch, ToolCall lifecycle and header zones, copy/view actions, and ToolGroup add/complete."""

from __future__ import annotations

from dataclasses import replace
from types import ModuleType

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.events import Click
from textual.geometry import Offset
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui import clipboard
from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.util.spinner import SPINNER_FRAMES
from chrys.app.tui.widgets.chat import tool_renderers
from chrys.app.tui.widgets.chat.fold_rows import FoldRow
from chrys.app.tui.widgets.chat.messages import (
    MessageCopyButton,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel, _ScrollToBottomButton
from chrys.app.tui.widgets.chat.tool_call import (
    BaseToolCard,
    ToolCall,
    ToolCardHeader,
    ToolCopyButton,
    ToolGroup,
    ToolGroupTitle,
    ToolViewButton,
)
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.kernel import Content
from tests.support.tui_helpers import (
    ChatPanelApp,
    LocalizedApp,
    LocalizedWidgetApp,
    WidgetApp,
    click_copy_button,
    click_widget,
    png_bytes,
    rich_plain,
)


async def test_click_affordance_buttons_prevent_widget_default_action() -> None:
    seen: list[str] = []

    class ButtonClickApp(App):
        def compose(self) -> ComposeResult:
            yield ToolViewButton()
            yield ToolCopyButton()
            yield ToolGroupTitle(FoldRow("tools"))
            yield MessageCopyButton(tooltip="copy")
            yield _ScrollToBottomButton()

        def on_tool_view_button_clicked(self, _event: object) -> None:
            seen.append("tool-view")

        def on_tool_copy_button_clicked(self, _event: object) -> None:
            seen.append("tool-copy")

        def on_tool_group_title_clicked(self, _event: object) -> None:
            seen.append("tool-group-title")

        def on_message_copy_button_clicked(self, _event: object) -> None:
            seen.append("message-copy")

        def on_scroll_to_bottom_requested(self, _event: object) -> None:
            seen.append("scroll-bottom")

    async with ButtonClickApp().run_test() as pilot:
        await pilot.pause()

        widgets = [
            ("tool-view", pilot.app.query_one(ToolViewButton)),
            ("tool-copy", pilot.app.query_one(ToolCopyButton)),
            ("tool-group-title", pilot.app.query_one(ToolGroupTitle)),
            ("message-copy", pilot.app.query_one(MessageCopyButton)),
            ("scroll-bottom", pilot.app.query_one(_ScrollToBottomButton)),
        ]
        for _name, widget in widgets:
            event = click_widget(widget)
            assert event._no_default_action is True
            assert event._stop_propagation is True

        await pilot.pause()

    assert set(seen) == {"tool-view", "tool-copy", "tool-group-title", "message-copy", "scroll-bottom"}
    assert len(seen) == 5


async def test_click_affordance_real_dispatch_skips_widget_default_action(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    base_clicks: list[Widget] = []

    class ProbeAffordance(ClickAffordance):
        class Clicked(Message):
            """Posted when the probe affordance is clicked."""

        CLICK_MESSAGE = Clicked

        def __init__(self) -> None:
            super().__init__("probe", id="probe-affordance")

    class BaseClickProbe(Static):
        pass

    async def record_base_click(self: Widget, _event: Click) -> None:
        base_clicks.append(self)

    monkeypatch.setattr(Widget, "_on_click", record_base_click)

    class DispatchApp(App):
        def compose(self) -> ComposeResult:
            yield ProbeAffordance()
            yield BaseClickProbe("base control", id="base-control")

        def on_probe_affordance_clicked(self, _event: ProbeAffordance.Clicked) -> None:
            seen.append("probe")

    async with DispatchApp().run_test() as pilot:
        control = pilot.app.query_one(BaseClickProbe)
        event = Click(control, 0, 0, 0, 0, 1, False, False, False)
        event.stop()
        await control._on_message(event)
        assert base_clicks == [control]
        base_clicks.clear()
        await pilot.click("#probe-affordance")
        await pilot.pause()

    assert seen == ["probe"]
    assert base_clicks == []


async def test_tool_call_lifecycle() -> None:
    async with LocalizedWidgetApp(
        lambda: ToolCall("c1", "read_file", 'path="/foo"', args={"path": "/foo"})
    ).run_test() as pilot:
        tc = pilot.app.query_one(ToolCall)
        header = tc.query_one(ToolCardHeader)
        assert tc.status == "running"
        assert header.actions_visible is False
        # The spinner timer may already have ticked.
        body = rich_plain(tc.query_one("#tc-body", Static).render())
        assert body.startswith(tuple(f"{frame} " for frame in SPINNER_FRAMES))

        tc.set_complete("file contents", duration_ms=89)
        await pilot.pause()
        assert tc.status == "complete"
        assert tc.duration_ms == 89
        assert header.actions_visible is True

        # Label should contain tool name and duration
        label = tc.query_one("#tc-label", ToolCardHeader).content
        assert "read_file" in label.plain
        assert "89ms" in label.plain


def test_every_tool_card_spins_the_shared_braille_frames() -> None:
    tool_renderers._ensure_loaded()
    cards: set[type[BaseToolCard]] = set()
    pending: list[type[BaseToolCard]] = [BaseToolCard]
    while pending:
        for card in pending.pop().__subclasses__():
            if card not in cards:
                cards.add(card)
                pending.append(card)

    assert len(cards) > 10
    assert {card.__name__ for card in cards if card._SPINNERS != SPINNER_FRAMES} == set()


async def test_tool_card_header_zones_disabled_when_narrower_than_actions_cell() -> None:
    """A header too narrow for the 13-cell actions cell must map no zones.

    Without the clamp, ``offset.x - (width - _ACTIONS_WIDTH)`` shifts clicks
    *into* the zone ranges (e.g. x=0 in a 10-wide header lands in "view"),
    so ordinary label clicks in narrow panes would fire actions.
    """

    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            tc = ToolCall("c1", "read_file", 'path="/foo"', args={"path": "/foo"})
            tc.styles.width = 10
            yield tc

    async with ToolApp().run_test() as pilot:
        tc = pilot.app.query_one(ToolCall)
        tc.set_complete("file contents", duration_ms=89)
        await pilot.pause()
        header = tc.query_one(ToolCardHeader)
        assert header.actions_visible is True
        width = header.content_size.width
        assert 0 < width < ToolCardHeader._ACTIONS_WIDTH
        assert all(header._zone_at(Offset(x, 0)) is None for x in range(width))


async def test_tool_card_header_renders_plain_string_labels_without_markup() -> None:
    """A str label quoting model output like ``[/]`` must not raise MarkupError.

    All renderers pass Rich ``Text``, but the header's contract is that plain
    strings render literally too — both at construction and via ``update()``.
    """

    async with WidgetApp(lambda: ToolCardHeader("stray close [/] and [bold] unclosed")).run_test(size=(60, 4)) as pilot:
        header = pilot.app.query_one(ToolCardHeader)
        assert header.render_line(0).text.rstrip() == "stray close [/] and [bold] unclosed"

        header.update("updated [/][red] later")
        await pilot.pause()
        assert header.render_line(0).text.rstrip() == "updated [/][red] later"


@pytest.mark.parametrize(("duration_ms", "expected"), [(0, "(0ms)"), (987, "(987ms)")])
async def test_tool_card_header_appends_replay_timing_omitted_by_renderer(duration_ms: int, expected: str) -> None:
    async with WidgetApp(lambda: ToolCardHeader(Text("• hosted_tool", style="bold"))).run_test() as pilot:
        header = pilot.app.query_one(ToolCardHeader)
        header.append_replay_timing(
            timestamp="- 9:02 AM",
            duration_ms=duration_ms,
            show_duration=True,
        )
        await pilot.pause()

        assert header._label_renderable().plain == f"• hosted_tool {expected} - 9:02 AM"


async def test_tool_call_renders_result_images_without_placeholder_text() -> None:
    async with LocalizedWidgetApp(
        lambda: ToolCall("c1", "view_image", args={"path": "/tmp/pixel.png"})
    ).run_test() as pilot:
        tc = pilot.app.query_one(ToolCall)
        image = Content.from_data(
            png_bytes((255, 0, 0), size=(4, 4)),
            "image/png",
            additional_properties={"width": 4, "height": 4, "media_type": "image/png"},
        )

        tc.set_complete("Image: segmentation mask\n[image/png image]", duration_ms=12, image_contents=[image])
        await pilot.pause()

        output = tc.query_one("#tc-body", Static)
        assert output.render().plain == "Image: segmentation mask"
        assert "data:image" not in output.render().plain
        panel = tc.query_one("#tc-panel")
        assert str(panel.border_subtitle) == "Resolution: 4x4 · Type: image/png"
        assert len(list(tc.query("#tc-images"))) == 1


async def test_tool_copy_button_copies_full_input_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async with LocalizedWidgetApp(
        lambda: ToolCall("c1", "read_file", args={"path": "/foo", "limit": 3})
    ).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tc = pilot.app.query_one(ToolCall)
        full_output = "file contents\nwith a ``` fence\nand more text"
        tc.set_complete(full_output, duration_ms=89)
        await pilot.pause()

        click_copy_button(tc.query_one(ToolCardHeader))
        await pilot.pause()

        await wait_for_clipboard(pilot.app)
        assert copied
        assert len(copied) == 1
        payload = copied[-1]
        assert pilot.app.clipboard == payload
        assert "# read_file" in payload
        assert "- **Status:** `completed`" in payload
        assert "- **Duration:** `89ms`" in payload
        assert "- **Call ID:** `c1`" in payload
        assert '"path": "/foo"' in payload
        assert '"limit": 3' in payload
        assert full_output in payload
        assert "~~~text" in payload


async def test_tool_copy_button_sanitizes_control_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawPayloadToolCall(ToolCall):
        def _tool_copy_input(self) -> tuple[str, str]:
            return "text", "query\tvalue\nnul:\x00 cr:\rend"

        def _tool_copy_sections(self) -> list[tuple[str, str, str]]:
            return [("Output", "text", "color:\x1b[31mred\x1b[0m csi:\x9b31m\nnext\tcell\x7f")]

    async with LocalizedWidgetApp(lambda: RawPayloadToolCall("c1", "raw_payload")).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tc = pilot.app.query_one(RawPayloadToolCall)
        tc.set_complete("ignored", duration_ms=1)
        await pilot.pause()

        click_copy_button(tc.query_one(ToolCardHeader))
        await pilot.pause()

        await wait_for_clipboard(pilot.app)
        assert len(copied) == 1
        payload = copied[-1]
        assert "query\tvalue\nnul:" in payload
        assert "\nnext\tcell" in payload
        assert "\\x00" in payload
        assert "\\x0d" in payload
        assert "\\x1b[31mred\\x1b[0m" in payload
        assert "\\x9b31m" in payload
        assert "\\x7f" in payload
        assert "\x00" not in payload
        assert "\r" not in payload
        assert "\x1b" not in payload
        assert "\x9b" not in payload
        assert "\x7f" not in payload


@pytest.fixture(params=["macos", "windows"])
def ssh_clipboard_platform(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    os_name: str = request.param
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "client 123 server 22")
    platform = clipboard.platform_helpers
    shadow = ModuleType(platform.__name__)
    shadow.__dict__.update(platform.__dict__)
    info = replace(platform.get_platform(), os_name=os_name)
    shadow.get_platform = lambda: info
    monkeypatch.setattr(clipboard, "platform_helpers", shadow)
    return os_name


async def test_tool_copy_large_payload_skips_terminal_clipboard(
    monkeypatch: pytest.MonkeyPatch, ssh_clipboard_platform: str
) -> None:
    async with LocalizedWidgetApp(lambda: ToolCall("c1", "zsh", args={"command": "yes"})).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tc = pilot.app.query_one(ToolCall)
        tc.set_complete("x" * (70 * 1024))
        await pilot.pause()

        click_copy_button(tc.query_one(ToolCardHeader))
        await pilot.pause()

        await wait_for_clipboard(pilot.app)
        assert pilot.app.clipboard == ""
        if ssh_clipboard_platform == "windows":
            assert copied == []
        else:
            assert copied == [tc.format_tool_execution_copy()]


async def test_tool_copy_small_payload_reports_terminal_unavailable(
    monkeypatch: pytest.MonkeyPatch, ssh_clipboard_platform: str
) -> None:
    class ToolApp(LocalizedApp):
        def __init__(self) -> None:
            super().__init__()
            self.notifications: list[str] = []

        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "read_file", args={"path": "/foo"})

        def copy_to_clipboard(self, _text: str) -> None:
            raise RuntimeError("terminal clipboard unavailable")

        def notify(
            self,
            message: str,
            *,
            title: str = "",
            severity: str = "information",
            timeout: float | None = None,
            markup: bool = True,
        ) -> None:
            self.notifications.append(message)

    async with ToolApp().run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tc = pilot.app.query_one(ToolCall)
        tc.set_complete("file contents")
        await pilot.pause()

        click_copy_button(tc.query_one(ToolCardHeader))
        await pilot.pause()

        await wait_for_clipboard(pilot.app)
        assert pilot.app.clipboard == ""
        if ssh_clipboard_platform == "windows":
            assert copied == []
            assert pilot.app.notifications == ["Could not copy to the system clipboard. Please try again."]
        else:
            assert copied == [tc.format_tool_execution_copy()]
            assert "terminal clipboard unavailable" in pilot.app.notifications[-1]


async def test_file_tool_copy_includes_snapshot_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        cp = pilot.app.query_one(ChatPanel)
        await cp.add_tool_start("c1", "write_file", "filesystem.write", args={"path": "README.md"})
        await pilot.pause()
        header = pilot.app.query_one(ToolCardHeader)
        assert header.actions_visible is False

        await cp.add_tool_result(
            "c1",
            "write_file",
            "Successfully wrote README.md",
            20,
            file_snapshot=("old\n", "new\n"),
        )
        await pilot.pause()
        assert header.actions_visible is True

        click_copy_button(header)
        await pilot.pause()

        await wait_for_clipboard(pilot.app)
        assert len(copied) == 1
        payload = copied[-1]
        assert "## Diff" in payload
        assert "~~~diff" in payload
        assert "--- before/README.md" in payload
        assert "+++ after/README.md" in payload
        assert "-old" in payload
        assert "+new" in payload


async def test_tool_group_add_and_complete() -> None:
    async with WidgetApp(ToolGroup).run_test() as pilot:
        tg = pilot.app.query_one(ToolGroup)
        await tg.add_tool("c1", "read_file", 'path="/a"')
        await tg.add_tool("c2", "grep", 'pattern="foo"')
        assert not tg.all_complete

        tg.complete_tool("c1", "result1", 100)
        tg.complete_tool("c2", "result2", 200)
        assert tg.all_complete


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "args", "result"),
    [
        ("zsh", "shell", {"command": "echo hi"}, "hi\n[exit_code: 0]"),
        ("read_file", "filesystem.read", {"path": "/tmp/a.txt"}, "File: /tmp/a.txt\n1: hello"),
        ("grep", "search", {"pattern": "hello"}, "Found 1 match in /tmp/a.txt\n/tmp/a.txt:1:hello"),
    ],
)
async def test_specialized_tool_copy_button_visibility(
    tool_name: str,
    tool_kind: str,
    args: dict[str, str],
    result: str,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_tool_start("c1", tool_name, tool_kind, args=args)
        await pilot.pause()

        header = pilot.app.query_one(ToolCardHeader)
        assert header.actions_visible is False

        await cp.add_tool_result("c1", tool_name, result, 25)
        await pilot.pause()

        assert header.actions_visible is True
