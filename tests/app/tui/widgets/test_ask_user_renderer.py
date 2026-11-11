# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the ask_user tool renderer."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.events import Resize
from textual.geometry import Size
from textual.message import Message
from textual.widgets import Button, Static, TextArea
from textual.widgets.option_list import OptionDoesNotExist

from chrys.app.tui.util.message_gate import messages_disabled
from chrys.app.tui.widgets import AskUserPrompt, AskUserResponseResized, PromptDraft
from chrys.app.tui.widgets.ask_user_controls import (
    _CUSTOM_RESPONSE_PLACEHOLDER,
    ASK_USER_INPUT_MIN_HEIGHT,
    AskUserOptions,
    AskUserResponseFooter,
    _AskUserTextArea,
)
from chrys.app.tui.widgets.chat import tool_renderers
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers import ask_user as ask_user_renderer_module
from chrys.app.tui.widgets.chat.renderers.ask_user import (
    AskUserInlineResized,
    AskUserInlineSubmitted,
    AskUserToolCall,
    _answer_from_result,
)
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolCardHeader, ToolGroup
from chrys.app.tui.widgets.chat.tool_renderers import create_tool_widget
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.ask_user import (
    AskUserAnswer,
    AskUserOption,
    AskUserQuestion,
    format_ask_user_result,
)
from chrys.foundation.tool_kinds import KIND_ASK_USER
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY, TOOL_INTERRUPTED_METADATA_KEY
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import LocalizedApp, rich_plain, rich_segment_lines
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

_RENDERER_MODULES = (
    "chrys.app.tui.widgets.chat.renderers.ask_user",
    "chrys.app.tui.widgets.chat.renderers.execute",
    "chrys.app.tui.widgets.chat.renderers.file_edit",
    "chrys.app.tui.widgets.chat.renderers.read_file",
    "chrys.app.tui.widgets.chat.renderers.search",
    "chrys.app.tui.widgets.chat.renderers.skill",
    "chrys.app.tui.widgets.chat.renderers.sleep",
    "chrys.app.tui.widgets.chat.renderers.sub_agent",
)
_MISSING = object()


def _one_question(options: list[str] | None = None, *, multi_select: bool = False) -> tuple[AskUserQuestion, ...]:
    return (
        AskUserQuestion(
            "Pick?",
            options=tuple(AskUserOption(option) for option in options or [] if option.strip("\u200b\ufeff ")),
            multi_select=multi_select,
        ),
    )


def _maximum_questions() -> tuple[AskUserQuestion, ...]:
    return tuple(
        AskUserQuestion(
            question=f"Question {question_index}: " + "Q" * 3_980,
            header=f"Header{question_index}",
            options=tuple(
                AskUserOption(
                    label=f"Option {option_index}",
                    description="D" * 500,
                )
                for option_index in range(8)
            ),
            multi_select=True,
        )
        for question_index in range(4)
    )


def _question_args(questions: tuple[AskUserQuestion, ...]) -> dict[str, object]:
    return {
        "questions": [
            {
                "question": question.question,
                "header": question.header,
                "options": [{"label": option.label, "description": option.description} for option in question.options],
                "multi_select": question.multi_select,
            }
            for question in questions
        ]
    }


def _ask_user_replay_messages(args: dict[str, object], result: str) -> list[dict[str, object]]:
    return [
        {"role": "user", "contents": [{"type": "text", "text": "ask"}]},
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "ask_user", "call_id": "c1", "arguments": args}],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "c1", "result": result}]},
    ]


def _completed_allocation(tool: AskUserToolCall) -> tuple[int, str, str]:
    return (
        tool._chat_viewport_height,
        str(tool.query_one("#ask-panel").styles.max_height),
        str(tool.styles.max_height),
    )


# Slow geometry waits must exhaust their own deadline (a clean assert), never
# the global per-test pytest-timeout hard cap — its thread method kills the
# whole xdist worker, surfacing as "worker gwN crashed".
pytestmark = pytest.mark.timeout(120)


def test_custom_response_placeholder_keeps_english_and_localizes_chinese() -> None:
    assert format_message(_CUSTOM_RESPONSE_PLACEHOLDER.bind()) == "Type a custom response..."
    assert Localizer("zh-Hans").render(_CUSTOM_RESPONSE_PLACEHOLDER.bind()) == "输入自定义回复..."


async def _wait_for_layout(pilot: object, predicate: Callable[[], bool], *, timeout: float = 30.0) -> None:
    # Bare pilot.pause() pumps drain in ~0ms on loaded CI workers before
    # deferred layout lands; poll against a real deadline instead. Loaded CI
    # runners have blown a 5s deadline on these geometry waits (macOS twice,
    # different tests each time) and later a 15s one (Windows), so the long
    # default applies file-wide.
    assert await wait_until(predicate, timeout=timeout, pilot=pilot)


def test_answer_from_result_strips_only_middleware_prefix() -> None:
    assert _answer_from_result("User response: yes\nfull answer") == "yes\nfull answer"
    assert _answer_from_result("User response:   indented") == "  indented"
    assert _answer_from_result("Error: user did not respond") == "Error: user did not respond"


def test_factory_routes_ask_user_tool_name_to_renderer() -> None:
    widget = create_tool_widget("c1", "ask_user", "", args={"question": "Proceed?"})

    assert isinstance(widget, AskUserToolCall)


def test_factory_routes_ask_user_runtime_kind_to_renderer() -> None:
    widget = create_tool_widget("c1", "custom_question", KIND_ASK_USER, args={"question": "Proceed?"})

    assert isinstance(widget, AskUserToolCall)


@pytest.mark.asyncio
async def test_ask_user_text_area_resize_dispatch_skips_duplicate_base_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-growing ask_user textarea should not let Textual re-run base resize."""

    class _ResizeProbe(_AskUserTextArea):
        pass

    class _BaseResizeProbe(TextArea):
        pass

    base_resize_calls: list[TextArea] = []
    resize_to_content_calls: list[tuple[_AskUserTextArea, bool]] = []

    def base_resize_spy(text_area: TextArea) -> None:
        base_resize_calls.append(text_area)

    def resize_to_content_spy(text_area: _AskUserTextArea, *, rewrap: bool = False) -> int:
        resize_to_content_calls.append((text_area, rewrap))
        return 3

    monkeypatch.setattr(TextArea, "_on_resize", base_resize_spy)
    monkeypatch.setattr(_ResizeProbe, "resize_to_content", resize_to_content_spy)
    # An unsuppressed dispatch must reach the same base spy.
    control = _BaseResizeProbe()
    await control._on_message(Resize(Size(10, 3), Size(10, 3)))
    assert base_resize_calls == [control]
    base_resize_calls.clear()

    text_area = _ResizeProbe()
    event = Resize(Size(10, 3), Size(10, 3))

    await text_area._on_message(event)

    assert resize_to_content_calls == [(text_area, True)]
    assert base_resize_calls == []
    assert event._no_default_action is True


@pytest.mark.asyncio
async def test_ask_user_text_area_ignores_height_only_resize_after_autogrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-height feedback should not become a second edit-time resize pass."""
    resize_to_content_calls: list[tuple[_AskUserTextArea, bool]] = []

    def resize_to_content_spy(text_area: _AskUserTextArea, *, rewrap: bool = False) -> int:
        resize_to_content_calls.append((text_area, rewrap))
        text_area._last_wrapped_width = text_area.wrap_width
        return 3

    monkeypatch.setattr(_AskUserTextArea, "resize_to_content", resize_to_content_spy)
    monkeypatch.setattr(_AskUserTextArea, "wrap_width", property(lambda _self: 8))
    text_area = _AskUserTextArea()

    await text_area._on_message(Resize(Size(10, 3), Size(10, 3)))
    await text_area._on_message(Resize(Size(10, 4), Size(10, 4)))

    assert resize_to_content_calls == [(text_area, True)]


@pytest.mark.asyncio
async def test_ask_user_text_area_rewraps_when_scrollbar_changes_wrap_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scrollbar can narrow TextArea.wrap_width without changing widget width."""
    resize_to_content_calls: list[tuple[_AskUserTextArea, bool]] = []
    wrap_width_state = {"value": 8}

    def resize_to_content_spy(text_area: _AskUserTextArea, *, rewrap: bool = False) -> int:
        resize_to_content_calls.append((text_area, rewrap))
        text_area._last_wrapped_width = text_area.wrap_width
        return 7

    monkeypatch.setattr(_AskUserTextArea, "resize_to_content", resize_to_content_spy)
    monkeypatch.setattr(_AskUserTextArea, "wrap_width", property(lambda _self: wrap_width_state["value"]))
    text_area = _AskUserTextArea()

    await text_area._on_message(Resize(Size(10, 5), Size(10, 5)))
    wrap_width_state["value"] = 7
    await text_area._on_message(Resize(Size(10, 7), Size(10, 7)))

    assert resize_to_content_calls == [(text_area, True), (text_area, True)]


def test_ask_user_tool_defers_stale_scheduled_width_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    """The card panel must be measured after the latest width lands."""
    tool = AskUserToolCall("c1", "ask_user", args={"question": "Pick?"})
    tool._inline_request_id = "req-1"
    callbacks: list[tuple[Callable[..., object], tuple[object, ...]]] = []
    measurements: list[None] = []

    def schedule(callback: Callable[..., object], *args: object, **_kwargs: object) -> bool:
        callbacks.append((callback, args))
        return True

    monkeypatch.setattr(tool, "call_after_refresh", schedule)
    monkeypatch.setattr(tool, "_sync_inline_layout", lambda: measurements.append(None) or False)

    tool.on_resize(Resize(Size(80, 3), Size(80, 3)))
    old_callback, old_args = callbacks.pop(0)
    tool.on_resize(Resize(Size(20, 3), Size(20, 3)))
    old_callback(*old_args)

    assert measurements == []
    assert len(callbacks) == 1

    current_callback, current_args = callbacks.pop(0)
    current_callback(*current_args)

    assert measurements == [None]


@pytest.mark.parametrize("reject_reschedule", [False, True])
def test_inline_layout_recovers_after_refresh_scheduling_is_rejected(
    monkeypatch: pytest.MonkeyPatch, reject_reschedule: bool
) -> None:
    """Both initial scheduling and a stale callback's retry must release their slot."""
    tool = AskUserToolCall("c1", "ask_user", args={"question": "Pick?"})
    tool._inline_request_id = "req-1"
    measurements: list[None] = []
    monkeypatch.setattr(tool, "_sync_inline_layout", lambda: measurements.append(None) or False)
    with patch.object(tool, "call_after_refresh", autospec=True) as schedule:
        schedule.return_value = reject_reschedule
        tool.on_resize(Resize(Size(80, 3), Size(80, 3)))
        if reject_reschedule:
            callback, generation = schedule.call_args.args
            tool.on_resize(Resize(Size(20, 3), Size(20, 3)))
            schedule.return_value = False
            callback(generation)
        assert tool._inline_scheduled_sync_generation is None
        assert measurements == []

        schedule.reset_mock()
        schedule.return_value = True
        tool.on_resize(Resize(Size(40, 3), Size(40, 3)))
        schedule.assert_called_once()
        callback, generation = schedule.call_args.args
        callback(generation)
        assert measurements == [None]
        assert tool._inline_scheduled_sync_generation is None


def test_ask_user_text_area_measurement_self_heals_a_queued_width_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor measurement must rewrap before the TextArea Resize arrives."""
    wrap_width = {"value": 57}
    monkeypatch.setattr(_AskUserTextArea, "wrap_width", property(lambda _self: wrap_width["value"]))
    text_area = _AskUserTextArea(text="x" * 60, soft_wrap=True)
    text_area.resize_to_content(rewrap=True)

    assert [len(section) for section in text_area.wrapped_document.lines[0]] == [57, 3]

    wrap_width["value"] = 20
    text_area.resize_to_content()

    assert [len(section) for section in text_area.wrapped_document.lines[0]] == [20, 20, 20]
    assert text_area._last_wrapped_width == 20


@pytest.mark.asyncio
async def test_ask_user_text_edit_syncs_response_state_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text edits and their resize feedback should share one layout sync path."""

    class FooterApp(App):
        def compose(self) -> ComposeResult:
            yield AskUserResponseFooter("req-1")

    async with FooterApp().run_test() as pilot:
        footer = pilot.app.query_one(AskUserResponseFooter)
        text_area = footer.query_one("#askuser-input", _AskUserTextArea)
        await pilot.pause()
        resize_calls: list[tuple[_AskUserTextArea, bool]] = []

        def resize_to_content_spy(area: _AskUserTextArea, *, rewrap: bool = False) -> int:
            resize_calls.append((area, rewrap))
            # Preserve the synchronization side effects that suppress a late
            # same-width Resize message on slower CI workers.
            area._last_wrapped_width = area.wrap_width
            area._last_reported_height = ASK_USER_INPUT_MIN_HEIGHT
            return ASK_USER_INPUT_MIN_HEIGHT

        monkeypatch.setattr(_AskUserTextArea, "resize_to_content", resize_to_content_spy)
        await wait_until_quiet(
            lambda: len(resize_calls),
            description="ask-user resize call count",
            pilot=pilot,
        )
        resize_calls.clear()
        text_area.insert("x")

        await _wait_for_layout(pilot, lambda: bool(resize_calls))
        await text_area._on_message(Resize(text_area.size, text_area.size))
        await footer._on_message(Resize(Size(max(1, footer.size.width), footer.size.height + 1), footer.size))

        assert not await wait_until(lambda: len(resize_calls) > 1, timeout=0.2, interval=0.01, pilot=pilot)
        assert resize_calls == [(text_area, False)]


@pytest.mark.asyncio
async def test_ask_user_text_edit_does_not_rewrap_the_full_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Changed handler should consume TextArea's incremental wrap result."""

    class FooterApp(App):
        def compose(self) -> ComposeResult:
            yield AskUserResponseFooter("req-1")

    async with FooterApp().run_test() as pilot:
        text_area = pilot.app.query_one("#askuser-input", _AskUserTextArea)
        await pilot.pause()
        rewrap_calls: list[None] = []
        monkeypatch.setattr(text_area, "_rewrap_and_refresh_virtual_size", lambda: rewrap_calls.append(None))

        text_area.insert("x")
        await pilot.pause()

        assert rewrap_calls == []


@pytest.mark.asyncio
async def test_ask_user_text_area_mounts_with_blink_timer_paused() -> None:
    class FooterApp(App):
        def compose(self) -> ComposeResult:
            yield AskUserResponseFooter("req-1")

    async with FooterApp().run_test() as pilot:
        text_area = pilot.app.query_one("#askuser-input", _AskUserTextArea)
        text_area.focus()
        await pilot.pause()

        assert text_area.cursor_blink is False
        assert text_area.blink_timer._active.is_set() is False


def test_ask_user_renderer_uses_base_tool_card_contract() -> None:
    tool = AskUserToolCall("c1", "ask_user", args={"question": "Proceed?"})

    assert isinstance(tool, BaseToolCard)
    assert tool.call_id == "c1"
    assert tool.tool_name == "ask_user"
    assert tool.status == "running"
    assert tool.result_text == ""
    assert tool.duration_ms == 0
    assert tool.args == {"question": "Proceed?"}


def test_lazy_loader_imports_ask_user_renderer_module() -> None:
    parent_module = sys.modules["chrys.app.tui.widgets.chat.renderers"]
    parent_attrs = vars(parent_module)
    saved_parent_attrs = {
        module_name.rsplit(".", 1)[1]: parent_attrs.get(module_name.rsplit(".", 1)[1], _MISSING)
        for module_name in _RENDERER_MODULES
    }
    saved_registry = dict(tool_renderers._REGISTRY)
    saved_kind_registry = dict(tool_renderers._KIND_REGISTRY)
    saved_loaded = tool_renderers._loaded
    saved_modules = {module_name: sys.modules.pop(module_name, _MISSING) for module_name in _RENDERER_MODULES}
    for attr in saved_parent_attrs:
        parent_attrs.pop(attr, None)
    try:
        tool_renderers._REGISTRY.clear()
        tool_renderers._KIND_REGISTRY.clear()
        tool_renderers._loaded = False

        widget = create_tool_widget("c1", "ask_user", "", args={"question": "Proceed?"})

        assert widget.__class__.__name__ == "AskUserToolCall"
    finally:
        for module_name, module in saved_modules.items():
            if module is _MISSING:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = module
        for attr, value in saved_parent_attrs.items():
            if value is _MISSING:
                parent_attrs.pop(attr, None)
            else:
                parent_attrs[attr] = value
        tool_renderers._REGISTRY.clear()
        tool_renderers._REGISTRY.update(saved_registry)
        tool_renderers._KIND_REGISTRY.clear()
        tool_renderers._KIND_REGISTRY.update(saved_kind_registry)
        tool_renderers._loaded = saved_loaded


@pytest.mark.asyncio
async def test_ask_user_renderer_mounts_question_and_full_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall(
                "c1",
                "ask_user",
                args_summary=json.dumps({"question": "**Proceed?**\n\n- yes\n- no"}),
            )

    async with ToolApp().run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tool = pilot.app.query_one(AskUserToolCall)
        card_panel = tool.query_one("#ask-panel")
        assert card_panel.border_title == "Question"
        assert card_panel.border_subtitle is None
        assert tool.query_one("#ask-question", VirtualizedMarkdown).source == "**Proceed?**\n\n- yes\n- no"

        tool.set_complete("User response: first line\nsecond line", duration_ms=1500)
        await pilot.pause()

        assert tool.status == "complete"
        assert rich_plain(tool.query_one("#ask-answer", Static).content) == "└─ first line\n   second line"
        assert tool.query_one(ToolCardHeader).actions_visible is True

        tool.copy_tool_execution()
        await pilot.pause()

        assert len(copied) == 1
        payload = copied[-1]
        assert "~~~markdown" in payload
        assert "**Proceed?**" in payload
        assert "## Answer" in payload
        assert "User response:" not in payload
        assert "first line\nsecond line" in payload


@pytest.mark.asyncio
async def test_native_rich_question_inspection_preserves_non_text_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    args = {
        "questions": [
            {
                "question": "Pick?",
                "header": "Choice",
                "options": [{"label": "A", "description": "First"}],
                "multi_select": True,
            },
            {
                "question": "Why?",
                "header": "Reason",
                "options": [],
                "multiSelect": False,
            },
        ],
        "trace": "keep",
    }
    expected_extra = {
        "trace": "keep",
        "questions": [
            {
                "header": "Choice",
                "options": [{"label": "A", "description": "First"}],
                "multi_select": True,
            },
            {
                "header": "Reason",
                "options": [],
                "multiSelect": False,
            },
        ],
    }

    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args=args)

    async with ToolApp().run_test() as pilot:
        captured: list[dict[str, object]] = []

        def capture_params(params: dict[str, object], **_kwargs: object) -> list[Static]:
            captured.append(params)
            return []

        monkeypatch.setattr(ask_user_renderer_module, "build_params_view", capture_params)
        tool = pilot.app.query_one(AskUserToolCall)
        tool._tool_view_input_widgets()
        language, copied = tool._tool_copy_input()

        assert captured == [expected_extra]
        assert language == "markdown"
        assert copied.startswith("1. Pick?\n2. Why?\n\n```json\n")
        assert json.loads(copied.removeprefix("1. Pick?\n2. Why?\n\n```json\n").removesuffix("\n```")) == expected_extra


@pytest.mark.asyncio
async def test_ask_user_renderer_records_rejected_completion_approval() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Proceed?"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)

        tool.set_complete("Error: rejected", duration_ms=10, approval="user_rejected")
        await pilot.pause()

        assert tool.approval == "user_rejected"
        assert tool.has_class("-rejected")
        assert rich_plain(tool.query_one("#ask-answer", Static).content) == "Error: rejected"


@pytest.mark.asyncio
async def test_ask_user_renderer_uses_structured_failure_metadata() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Proceed?"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)

        tool.set_complete(
            "Error: literal answer text",
            duration_ms=10,
            metadata={TOOL_FAILED_METADATA_KEY: False},
        )
        await pilot.pause()

        assert tool.has_class("-success")
        assert not tool.has_class("-error")


@pytest.mark.asyncio
async def test_ask_user_renderer_sets_error_status_for_failed_results() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Proceed?"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)

        tool.set_complete("Error: user did not respond", duration_ms=10, metadata={TOOL_FAILED_METADATA_KEY: True})
        await pilot.pause()

        assert tool.status == "error"
        assert tool.has_class("-error")


@pytest.mark.asyncio
async def test_ask_user_renderer_moves_the_interrupt_status_into_the_panel_subtitle() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"questions": [{"question": "Pick?"}, {"question": "Why?"}]})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_error("cancelled")
        await pilot.pause()

        panel = tool.query_one("#ask-panel")
        assert tool.has_class("-error")
        assert tool.has_class("-interrupted")
        assert str(panel.border_subtitle) == "Interrupted"
        # The questions stay readable; the body no longer repeats the status.
        assert tool.query_one("#ask-question", VirtualizedMarkdown).display
        assert not tool.query_one("#ask-answer", Static).display
        assert rich_plain(tool.query_one("#ask-answer", Static).content) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("User response: Python", "Python"),
        (
            json.dumps({"responses": [{"question": "Pick?", "answers": ["A"], "note": "why"}]}),
            json.dumps({"responses": [{"question": "Pick?", "answers": ["A"], "note": "why"}]}),
        ),
    ],
)
async def test_ask_user_renderer_shows_the_recorded_result_when_no_question_can_be_parsed(
    result: str, expected: str
) -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            # A degraded record: neither the args nor their summary yield a question.
            yield AskUserToolCall("c1", "ask_user", args_summary="not json", args={})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_complete(result, duration_ms=10)
        await pilot.pause()

        assert tool.has_class("-success")
        answer = tool.query_one("#ask-answer", Static)
        assert answer.display
        assert rich_plain(answer.content) == expected


_KERNEL_INTERRUPTED_RESULT = (
    "Error: Tool execution was interrupted. The operation may have completed; inspect current state before retrying."
)


@pytest.mark.asyncio
async def test_ask_user_renderer_treats_the_kernel_interrupted_result_as_an_interrupt() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"questions": [{"question": "Pick?"}]})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        # Restored sessions hand the card the kernel's model-facing filler
        # result, flagged only by its metadata.
        tool.metadata = {TOOL_INTERRUPTED_METADATA_KEY: True}
        tool.set_error(_KERNEL_INTERRUPTED_RESULT)
        await pilot.pause()

        assert tool.has_class("-interrupted")
        assert str(tool.query_one("#ask-panel").border_subtitle) == "Interrupted"
        assert not tool.query_one("#ask-answer", Static).display
        # The model-facing text stays available to the copy payload.
        assert tool.result_text == _KERNEL_INTERRUPTED_RESULT


@pytest.mark.asyncio
async def test_ask_user_renderer_completed_interrupted_result_shows_the_status_not_the_model_text() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"questions": [{"question": "Pick?"}, {"question": "Why?"}]})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        # Lazily restored records complete through set_complete with the
        # kernel's filler result and the interrupted flag in the metadata.
        tool.set_complete(
            _KERNEL_INTERRUPTED_RESULT,
            duration_ms=59_000,
            metadata={TOOL_FAILED_METADATA_KEY: True, TOOL_INTERRUPTED_METADATA_KEY: True},
        )
        await pilot.pause()

        assert tool.status == "error"
        assert tool.has_class("-error")
        assert tool.has_class("-interrupted")
        assert not tool.has_class("-multi")
        assert str(tool.query_one("#ask-panel").border_subtitle) == "Interrupted"
        assert tool.query_one("#ask-question", VirtualizedMarkdown).display
        assert not tool.query_one("#ask-answer", Static).display
        assert tool.result_text == _KERNEL_INTERRUPTED_RESULT


@pytest.mark.asyncio
async def test_ask_user_replay_of_an_interrupted_call_shows_the_status_not_the_model_text() -> None:
    class PanelApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "ask"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "ask_user",
                    "call_id": "ask-call",
                    "arguments": {"questions": [{"question": "Tea or coffee?"}, {"question": "When?"}]},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "ask-call",
                    "result": _KERNEL_INTERRUPTED_RESULT,
                    "additional_properties": {TOOL_RESULT_METADATA_KEY: {TOOL_INTERRUPTED_METADATA_KEY: True}},
                }
            ],
        },
    ]

    async with PanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({"ask_user": KIND_ASK_USER})
        await panel.replay_history(messages)
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        group.collapsed = False
        await _wait_for_layout(
            pilot, lambda: bool(panel.query(AskUserToolCall)) and panel.query_one(AskUserToolCall).has_class("-done")
        )

        card = panel.query_one(AskUserToolCall)
        assert card.has_class("-error")
        assert card.has_class("-interrupted")
        assert str(card.query_one("#ask-panel").border_subtitle) == "Interrupted"
        assert card.query_one("#ask-question", VirtualizedMarkdown).source == "1. Tea or coffee?\n2. When?"
        assert not card.query_one("#ask-answer", Static).display


@pytest.mark.asyncio
async def test_ask_user_renderer_labels_other_failures_in_the_subtitle_and_keeps_their_text() -> None:
    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Pick?"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_error("prompt handler exploded")
        await pilot.pause()

        assert tool.has_class("-error")
        assert not tool.has_class("-interrupted")
        assert str(tool.query_one("#ask-panel").border_subtitle) == "Errored"
        answer = tool.query_one("#ask-answer", Static)
        assert answer.display
        assert rich_plain(answer.content) == "prompt handler exploded"


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_submit", [False, True])
async def test_ask_user_renderer_inline_prompt_submits_once_and_clears_on_result(
    monkeypatch: pytest.MonkeyPatch, defer_submit: bool
) -> None:
    deferred: list[tuple[AskUserToolCall, AskUserInlineSubmitted]] = []
    post_message = AskUserToolCall.post_message

    def post_with_deferred_submit(tool: AskUserToolCall, message: Message) -> bool:
        if defer_submit and isinstance(message, AskUserInlineSubmitted):
            deferred.append((tool, message))
            return True
        return post_message(tool, message)

    monkeypatch.setattr(AskUserToolCall, "post_message", post_with_deferred_submit)

    class ToolApp(LocalizedApp):
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[tuple[str, str, tuple[AskUserAnswer, ...]]] = []

        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Pick?"})

        def on_ask_user_inline_submitted(self, event: AskUserInlineSubmitted) -> None:
            self.submitted.append((event.call_id, event.request_id, event.answers))
            event.stop()

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)

        assert tool.show_inline_prompt(
            "req-1",
            _one_question(["Python", "Go"]),
            draft=PromptDraft(drafts=("draft",)),
        )
        await pilot.pause()

        assert tool.query_one("#askuser-input", EnhancedTextArea).text == "draft"
        options = tool.query_one("#askuser-q0-options", AskUserOptions)
        options.toggle(options.get_option_at_index(0))
        options.toggle(options.get_option_at_index(0))
        if defer_submit:
            # Hold the final message hop to reproduce a loaded worker returning
            # from pause before the app observes submission. Release it through
            # the real queue; the assertion must await the receiving boundary.
            await wait_for(lambda: bool(deferred), pilot=pilot, description="inline submit queued")
            assert pilot.app.submitted == []
            for sender, message in deferred:
                post_message(sender, message)
        await wait_for(lambda: bool(pilot.app.submitted), pilot=pilot, description="inline submit delivered")

        assert pilot.app.submitted == [("c1", "req-1", (AskUserAnswer(values=("Python",), note="draft"),))]

        tool.set_complete("User response: Python", duration_ms=10)
        await _wait_for_layout(pilot, lambda: len(tool.query("#ask-inline")) == 0)

        with pytest.raises(NoMatches):
            tool.query_one("#ask-inline")
        assert rich_plain(tool.query_one("#ask-answer", Static).content) == "└─ Python"
        assert len(pilot.app.submitted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_layout", [False, True], ids=["settled", "pending-layout"])
async def test_chat_panel_inline_ask_user_text_area_accepts_typing_after_click(
    _inline_pilot_idle: None, pending_layout: bool
) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(["Python"]))
        await pilot.pause()

        text_area = panel.query_one("#askuser-input", EnhancedTextArea)
        options = panel.query_one(AskUserOptions)
        await wait_for(lambda: options.has_focus, pilot=pilot, description="inline prompt initial focus")
        if pending_layout:
            # Focus can be ready before a queued reflow lands. Pilot snapshots
            # click coordinates before its first pause, so reproduce that gap
            # without relying on worker speed or a fixed sleep.
            text_area.styles.margin = (0, 0, 0, 3)
        await pilot.wait_for_scheduled_animations()
        await wait_for(
            lambda: bool(text_area.content_region) and panel.region.contains_region(text_area.region),
            pilot=pilot,
            description="inline input visible geometry before click",
        )
        click_offset = text_area.content_region.offset - text_area.region.offset
        assert await pilot.click(text_area, offset=click_offset)
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="inline input focus after click")
        await pilot.press("x", "y")
        await wait_for(lambda: text_area.text == "xy", pilot=pilot, description="inline input receives typed text")
        assert text_area.text == "xy"


@pytest.mark.asyncio
async def test_ask_user_renderer_inline_input_keeps_its_frame_without_the_modal_stylesheet() -> None:
    """The compact TextArea drops its frame with an !important rule; the inline
    renderer must restore it on its own, not by riding on the ask_user dialog's
    stylesheet having been loaded earlier in the session."""

    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Pick?"})

    async with ToolApp().run_test(size=(100, 30)) as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        assert tool.show_inline_prompt("req-1", _one_question(["Python"]))
        await pilot.pause()

        input_area = tool.query_one("#askuser-input", EnhancedTextArea)
        assert input_area.has_class("-textual-compact")
        assert input_area.styles.border_top[0] == "round"
        assert input_area.styles.border_left[0] == "round"


@pytest.mark.asyncio
async def test_ask_user_renderer_inline_prompt_without_options_keeps_footer_visible() -> None:
    class ToolApp(LocalizedApp):
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[tuple[str, str, tuple[AskUserAnswer, ...]]] = []

        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Pick?"})

        def on_ask_user_inline_submitted(self, event: AskUserInlineSubmitted) -> None:
            self.submitted.append((event.call_id, event.request_id, event.answers))
            event.stop()

    async with ToolApp().run_test(size=(100, 30)) as pilot:
        tool = pilot.app.query_one(AskUserToolCall)

        assert tool.show_inline_prompt("req-1", _one_question())
        await pilot.pause()

        with pytest.raises(NoMatches):
            tool.query_one("#askuser-q0-options")
        inline = tool.query_one("#ask-inline")
        footer = tool.query_one("#askuser-footer")
        assert str(inline.styles.height) == "auto"
        assert str(footer.styles.height) == "auto"
        # ASK_USER_INPUT_MIN_HEIGHT counts the round frame, so measure the outer box.
        assert tool.query_one("#askuser-input", EnhancedTextArea).outer_size.height >= 3

        card_panel = tool.query_one("#ask-panel")
        input_area = tool.query_one("#askuser-input", EnhancedTextArea)
        # The question block plus its top margin, the input at its minimum,
        # the button row, and the panel frame: no rows are reserved for growth.
        await _wait_for_layout(
            pilot,
            lambda: tool._inline_scheduled_sync_generation is None and card_panel.region.height == 3 + 3 + 3 + 2,
        )
        initial_panel_height = card_panel.region.height
        initial_input_height = input_area.outer_size.height
        input_area.insert("Line one\nLine two\nLine three")
        await _wait_for_layout(
            pilot,
            lambda: (
                input_area.outer_size.height >= 5
                and card_panel.region.height
                == initial_panel_height + input_area.outer_size.height - initial_input_height
            ),
        )

        assert input_area.outer_size.height >= 5
        assert card_panel.region.height > initial_panel_height
        tool.query_one("#askuser-submit", Button).press()
        # Button.Pressed → AskUserInlineSubmitted is two message hops; a single
        # pause races on loaded CI workers, so poll the asserted condition.
        await _wait_for_layout(pilot, lambda: bool(pilot.app.submitted))

        assert pilot.app.submitted == [("c1", "req-1", (AskUserAnswer(values=("Line one\nLine two\nLine three",)),))]


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_card_follows_draft_height_without_reserved_rows() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(120, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "What next?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question())
        await pilot.pause()

        input_area = panel.query_one("#askuser-input", EnhancedTextArea)
        card_panel = panel.query_one("#ask-panel")
        tool = panel.query_one(AskUserToolCall)
        # The question block plus its top margin, the input at its minimum,
        # the button row, and the panel frame: nothing is reserved for growth.
        expected_initial_panel_height = 3 + ASK_USER_INPUT_MIN_HEIGHT + 3 + 2
        # show_inline_prompt schedules follow-up layout work. A bare pause can
        # return before it lands on loaded CI workers, so establish the known
        # empty-input geometry before taking a baseline for the growth checks.
        await _wait_for_layout(
            pilot,
            lambda: (
                input_area.outer_size.height == ASK_USER_INPUT_MIN_HEIGHT
                and card_panel.region.height == expected_initial_panel_height
                and tool.region.height > card_panel.region.height
            ),
        )
        initial_input_height = input_area.outer_size.height
        initial_panel_height = card_panel.region.height
        initial_tool_height = tool.region.height

        await wait_for(lambda: input_area.has_focus, pilot=pilot, description="inline prompt initial input focus")
        await pilot.wait_for_scheduled_animations()
        click_offset = input_area.content_region.offset - input_area.region.offset
        assert await pilot.click(input_area, offset=click_offset)
        await wait_for(lambda: input_area.has_focus, pilot=pilot, description="inline input focus after click")
        await pilot.press("enter", "enter")
        await _wait_for_layout(
            pilot,
            lambda: (
                input_area.text == "\n\n"
                and input_area.outer_size.height == initial_input_height + 2
                and card_panel.region.height == initial_panel_height + 2
                and tool.region.height == initial_tool_height + 2
            ),
        )

        assert input_area.text == "\n\n"
        assert str(card_panel.styles.height) == "auto"
        assert str(tool.styles.height) == "auto"

        grown_input_height = input_area.outer_size.height

        # The shrink assertion exercises the layout response to a document
        # edit, not Textual's focus/keyboard routing. Use the public edit API
        # so a concurrent focus change cannot turn this into an input race.
        input_area.delete((0, 0), input_area.document.end, maintain_selection_offset=False)
        await _wait_for_layout(
            pilot,
            lambda: (
                input_area.text == ""
                and input_area.outer_size.height == ASK_USER_INPUT_MIN_HEIGHT
                and card_panel.region.height == expected_initial_panel_height
                and tool.region.height == initial_tool_height
            ),
        )

        assert input_area.text == ""
        assert input_area.outer_size.height < grown_input_height
        assert card_panel.region.height == initial_panel_height
        assert tool.region.height == initial_tool_height


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "after"),
    [pytest.param("", "\n\n", id="grow"), pytest.param("\n\n", "", id="shrink")],
)
async def test_inline_input_follows_an_edit_when_the_card_was_laid_out_between_the_edit_and_the_resize(
    before: str, after: str
) -> None:
    """The input's own refresh lays the card out as soon as an edit lands; the footer sizes the
    input only once the edit's Changed reaches it. On a loaded runner that layout came first and
    recorded the card at the input's old height: its arrangements, and the box models of the boxes
    around the input that are sized to their content. Growing, the input never grew; shrinking, it
    did, but the card kept its old height."""

    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(120, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "What next?"})
        await pilot.pause()
        assert panel.show_ask_user_inline("c1", "req-1", _one_question())
        await pilot.pause()
        input_area = panel.query_one("#askuser-input", _AskUserTextArea)
        footer = panel.query_one(AskUserResponseFooter)
        card_panel = panel.query_one("#ask-panel")
        if before:
            input_area.insert(before)
        rows_before = ASK_USER_INPUT_MIN_HEIGHT + before.count("\n")
        await _wait_for_layout(
            pilot,
            lambda: input_area.outer_size.height == rows_before and screen_is_settled(pilot.app, pilot.app.screen),
        )
        initial_panel_height = card_panel.region.height

        with messages_disabled(TextArea.Changed, input_area):
            input_area.replace(after, (0, 0), input_area.document.end)
        # The layout the edit asked for runs while the input still has its old height.
        await _wait_for_layout(pilot, lambda: screen_is_settled(pilot.app, pilot.app.screen))
        assert input_area.virtual_size.height == after.count("\n") + 1
        assert input_area.outer_size.height == rows_before

        footer.sync_response_state(input_area)
        rows_after = ASK_USER_INPUT_MIN_HEIGHT + after.count("\n")
        await wait_for(
            lambda: (
                input_area.outer_size.height == rows_after
                and card_panel.region.height == initial_panel_height + rows_after - rows_before
            ),
            pilot=pilot,
            description="the input and its card sized to the edited text",
        )


@pytest.mark.asyncio
async def test_inline_cap_survives_context_switches_stale_footer_events_and_height_only_resize() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    questions = _maximum_questions()
    async with PanelApp().run_test(size=(120, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args=_question_args(questions))
        await pilot.pause()
        assert panel.show_ask_user_inline("c1", "req-1", questions)
        await pilot.pause()

        tool = panel.query_one(AskUserToolCall)
        prompt = tool.query_one(AskUserPrompt)
        card_panel = tool.query_one("#ask-panel")
        answer_region = tool.query_one("#askuser-inner")
        input_area = tool.query_one("#askuser-input", EnhancedTextArea)
        # ChatPanel keeps two rows of chrome, so a 32-row screen is a 30-row
        # viewport: the tab strip plus a ten-row content cap, the input at its
        # minimum, the button row and the panel frame.
        await _wait_for_layout(
            pilot,
            lambda: answer_region.region.height == 12 and card_panel.region.height == 12 + 3 + 3 + 2,
        )
        assert str(answer_region.styles.max_height) == "12"
        assert str(answer_region.styles.height) == "auto"
        assert str(card_panel.styles.height) == "auto"
        stale_generation = prompt.generation
        stale_index = prompt.active_index

        # The review pane hides the input; the card shrinks with it.
        prompt._switch(len(questions))
        await _wait_for_layout(
            pilot,
            lambda: answer_region.region.height == 12 and card_panel.region.height == 12 + 3 + 2,
        )
        prompt._switch(2)
        await _wait_for_layout(pilot, lambda: card_panel.region.height == 12 + 3 + 3 + 2)
        input_area.insert("line one\nline two\nline three\nline four")
        await _wait_for_layout(
            pilot,
            lambda: input_area.outer_size.height == 6 and card_panel.region.height == 12 + 6 + 3 + 2,
        )
        assert str(answer_region.styles.max_height) == "12"

        tool._on_ask_user_response_resized(
            AskUserResponseResized(7, generation=stale_generation, question_index=stale_index)
        )
        await pilot.pause()
        assert str(answer_region.styles.max_height) == "12"
        assert card_panel.region.height == 12 + 6 + 3 + 2

        input_area.delete((0, 0), input_area.document.end, maintain_selection_offset=False)
        await _wait_for_layout(pilot, lambda: card_panel.region.height == 12 + 3 + 3 + 2)

        await pilot.resize_terminal(120, 26)
        await _wait_for_layout(
            pilot,
            lambda: answer_region.region.height == 6 and card_panel.region.height == 6 + 3 + 3 + 2,
        )
        assert str(answer_region.styles.max_height) == "6"
        await pilot.resize_terminal(120, 32)
        await _wait_for_layout(
            pilot,
            lambda: answer_region.region.height == 12 and card_panel.region.height == 12 + 3 + 3 + 2,
        )


@pytest.mark.asyncio
async def test_chat_panel_ask_user_inline_resize_message_reaches_relayout_after_registry_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(120, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "What next?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question())
        await pilot.pause()

        tool = panel.query_one(AskUserToolCall)
        await wait_until_quiet(
            lambda: tool._inline_scheduled_sync_generation,
            description="inline ask-user generation",
            pilot=pilot,
        )
        calls: list[tuple[str, bool]] = []
        after_refresh: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        def record_panel_refresh(*, layout: bool = False, **_kwargs: object) -> None:
            calls.append(("panel_refresh", layout))

        def record_anchor_sync() -> None:
            calls.append(("anchor_sync", True))

        def record_after_refresh(callback: object, *args: object, **kwargs: object) -> bool:
            after_refresh.append((callback, args, kwargs))
            return True

        monkeypatch.setattr(panel, "refresh", record_panel_refresh)
        monkeypatch.setattr(panel, "_schedule_anchor_sync", record_anchor_sync)
        monkeypatch.setattr(panel, "call_after_refresh", record_after_refresh)

        tool.post_message(AskUserInlineResized("c1"))
        await pilot.pause()

        # Textual internals may schedule plain repaints (refresh(layout=False))
        # that land inside this instrumented window on slow CI workers. The
        # resize handler's contract (panel.on_inline_prompt_resized) is exactly
        # one relayout followed by one anchor sync — it never issues a plain
        # repaint, so those are filtered rather than counted.
        contract_calls = [call for call in calls if call != ("panel_refresh", False)]
        assert contract_calls == [
            ("panel_refresh", True),
            ("anchor_sync", True),
        ]
        assert after_refresh == []


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_keeps_wrapped_question_inside_the_shared_cap() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(140, 37)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        question = "This is a long ask_user question that should wrap after the terminal narrows. " * 12

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": question})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", (AskUserQuestion(question),))
        await pilot.pause()

        tool = panel.query_one(AskUserToolCall)
        card_panel = panel.query_one("#ask-panel")
        inner = panel.query_one("#askuser-inner")
        footer = panel.query_one("#askuser-footer")
        content_cap, _fixed = tool._allocate_inline_budget(tool._chat_viewport_height, has_tabs=False, live=True)
        await _wait_for_layout(
            pilot,
            lambda: tool._inline_scheduled_sync_generation is None and card_panel.region.height > 12,
        )
        assert str(inner.styles.max_height) == str(content_cap)
        assert inner.region.height < content_cap
        assert inner.virtual_size.height <= inner.region.height
        assert card_panel.region.height == inner.region.height + footer.region.height + 2

        await pilot.resize_terminal(60, 37)
        await _wait_for_layout(pilot, lambda: inner.region.height == content_cap)

        assert str(tool.styles.height) == "auto"
        assert inner.virtual_size.height > inner.region.height
        assert card_panel.region.height == content_cap + footer.region.height + 2
        assert tool.outer_size.height == card_panel.region.height + 1


@pytest.mark.parametrize("rows", [10, 20, 24, 30])
@pytest.mark.asyncio
async def test_multi_question_card_applies_live_then_completed_region_budgets(rows: int) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    questions = _maximum_questions()
    async with PanelApp().run_test(size=(100, rows + 2)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args=_question_args(questions))
        await pilot.pause()
        assert panel.show_ask_user_inline("c1", "req-1", questions)
        await pilot.pause()

        tool = panel.query_one(AskUserToolCall)
        card_panel = tool.query_one("#ask-panel")
        answer_region = tool.query_one("#askuser-inner")
        content, _fixed = tool._allocate_inline_budget(rows, has_tabs=True, live=True)
        # The tab strip plus at least one content row, then the input at its
        # minimum, the button row, the panel frame and the card chrome.
        region = max(content, 1) + 2
        await _wait_for_layout(
            pilot,
            lambda: (
                (
                    answer_region.region.height,
                    card_panel.region.height,
                    tool.outer_size.height,
                )
                == (region, region + 3 + 3 + 2, region + 3 + 3 + 2 + 1)
            ),
        )
        assert str(answer_region.styles.max_height) == str(region)

        answers = tuple(AskUserAnswer(values=(question.options[0].label,)) for question in questions)
        tool.set_complete(format_ask_user_result(questions, answers))
        content, fixed = tool._allocate_inline_budget(rows, has_tabs=False, live=False)
        await _wait_for_layout(
            pilot,
            lambda: (card_panel.region.height, tool.outer_size.height) == (content + 2, fixed + content),
        )


@pytest.mark.asyncio
async def test_inline_allocator_uses_chat_viewport_and_updates_completed_cards_without_terminal_resize() -> None:
    class ChromeApp(App):
        CSS = """
        #fixed-top { height: 4; }
        ChatPanel { height: 1fr; }
        #fixed-bottom { height: 4; }
        """

        def compose(self) -> ComposeResult:
            yield Static("top", id="fixed-top")
            yield ChatPanel()
            yield Static("bottom", id="fixed-bottom")

    questions = _maximum_questions()
    answers = tuple(AskUserAnswer(values=(question.options[0].label,)) for question in questions)
    async with ChromeApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _wait_for_layout(pilot, lambda: panel.size.height == 20)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args=_question_args(questions))
        assert panel.show_ask_user_inline("c1", "req-1", questions)

        tool = panel.query_one(AskUserToolCall)
        await _wait_for_layout(pilot, lambda: len(tool.query("#askuser-inner")) == 1)
        card_panel = tool.query_one("#ask-panel")
        answer_region = tool.query_one("#askuser-inner")
        # A 20-row viewport leaves no content budget: the tab strip plus the
        # one-row floor. The panel itself stays auto-sized.
        await _wait_for_layout(
            pilot,
            lambda: (
                tool._chat_viewport_height == 20
                and str(answer_region.styles.max_height) == "3"
                and answer_region.region.height == 3
                and card_panel.region.height == 3 + 3 + 3 + 2
            ),
        )
        assert str(card_panel.styles.height) == "auto"
        assert tool._chat_viewport_height != pilot.app.screen.size.height

        pilot.app.query_one("#fixed-top", Static).styles.height = 0
        await _wait_for_layout(
            pilot,
            lambda: (
                panel.size.height == 24
                and tool._chat_viewport_height == 24
                and str(answer_region.styles.max_height) == "6"
                and answer_region.region.height == 6
                and card_panel.region.height == 6 + 3 + 3 + 2
            ),
        )
        assert pilot.app.screen.size.height == 30

        tool.set_complete(format_ask_user_result(questions, answers))
        await _wait_for_layout(
            pilot,
            lambda: str(card_panel.styles.max_height) == "18" and str(tool.styles.max_height) == "20",
        )

        pilot.app.query_one("#fixed-top", Static).styles.height = 10
        await _wait_for_layout(
            pilot,
            lambda: (
                panel.size.height == 14
                and tool._chat_viewport_height == 14
                and str(card_panel.styles.max_height) == "8"
                and str(tool.styles.max_height) == "10"
            ),
        )
        assert pilot.app.screen.size.height == 30


@pytest.mark.asyncio
async def test_chat_panel_resize_visits_registered_ask_user_widgets_without_querying_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        await pilot.pause()
        tool = panel.query_one(AskUserToolCall)
        resize_calls: list[tuple[AskUserToolCall, int]] = []

        def record_resize(widget: AskUserToolCall, viewport_height: int) -> None:
            resize_calls.append((widget, viewport_height))

        def reject_transcript_query(_panel: ChatPanel, _selector: object) -> object:
            raise AssertionError("ChatPanel resize queried the transcript")

        with monkeypatch.context() as resize_patch:
            resize_patch.setattr(AskUserToolCall, "handle_chat_viewport_resize", record_resize)
            resize_patch.setattr(ChatPanel, "query", reject_transcript_query)
            panel.on_resize()

        assert resize_calls == [(tool, panel.size.height)]


@pytest.mark.asyncio
async def test_completed_ask_user_rebuild_keeps_fixed_chrome_viewport_allocation() -> None:
    class ChromeApp(App):
        CSS = """
        #fixed-top { height: 4; }
        ChatPanel { height: 1fr; }
        #fixed-bottom { height: 4; }
        """

        def compose(self) -> ComposeResult:
            yield Static("top", id="fixed-top")
            yield ChatPanel()
            yield Static("bottom", id="fixed-bottom")

    questions = _maximum_questions()
    answers = tuple(AskUserAnswer(values=(question.options[0].label,)) for question in questions)
    result = format_ask_user_result(questions, answers)
    async with ChromeApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _wait_for_layout(pilot, lambda: panel.size.height == 20)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args=_question_args(questions))
        await panel.add_tool_result("c1", "ask_user", result)

        group = panel.query_one(ToolGroup)
        original = panel.query_one(AskUserToolCall)
        await _wait_for_layout(pilot, lambda: _completed_allocation(original) == (20, "14", "16"))

        group.collapsed = True
        await _wait_for_layout(
            pilot,
            lambda: not group._content_mounted and group.get_tool("c1") is None,
        )
        group.collapsed = False
        await _wait_for_layout(pilot, lambda: isinstance(group.get_tool("c1"), AskUserToolCall))
        rebuilt = group.get_tool("c1")

        assert isinstance(rebuilt, AskUserToolCall)
        assert rebuilt is not original
        await _wait_for_layout(pilot, lambda: _completed_allocation(rebuilt) == (20, "14", "16"))
        assert pilot.app.screen.size.height == 30


@pytest.mark.asyncio
async def test_replayed_ask_user_receives_first_fixed_chrome_viewport_resize() -> None:
    class ChromeApp(App):
        CSS = """
        #fixed-top { height: 4; }
        ChatPanel { height: 1fr; }
        #fixed-bottom { height: 4; }
        """

        def compose(self) -> ComposeResult:
            yield Static("top", id="fixed-top")
            yield ChatPanel()
            yield Static("bottom", id="fixed-bottom")

    questions = _maximum_questions()
    answers = tuple(AskUserAnswer(values=(question.options[0].label,)) for question in questions)
    result = format_ask_user_result(questions, answers)
    async with ChromeApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        fixed_top = pilot.app.query_one("#fixed-top", Static)
        fixed_top.styles.height = 8
        await _wait_for_layout(pilot, lambda: panel.size.height == 16)

        panel.set_tool_kinds({"ask_user": KIND_ASK_USER})
        await panel.replay_history(_ask_user_replay_messages(_question_args(questions), result))
        group = panel.query_one(ToolGroup)
        group.collapsed = False
        await _wait_for_layout(pilot, lambda: len(panel.query(AskUserToolCall)) == 1)
        replayed = panel.query_one(AskUserToolCall)
        await _wait_for_layout(pilot, lambda: replayed._chat_viewport_height == 16)

        fixed_top.styles.height = 4

        await _wait_for_layout(
            pilot,
            lambda: panel.size.height == 20 and _completed_allocation(replayed) == (20, "14", "16"),
        )
        assert pilot.app.screen.size.height == 30


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_options_leave_room_before_footer() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(120, 37)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        options = [
            "All-purpose assistant",
            "Focused professional tool",
            "Creative brainstorm partner",
            "Casual chat companion",
        ]

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(options))
        await _wait_for_layout(pilot, lambda: len(panel.query("#askuser-q0-options")) > 0)

        options_widget = panel.query_one("#askuser-q0-options", AskUserOptions)
        footer = panel.query_one("#askuser-footer")
        inner = panel.query_one("#askuser-inner")
        card_panel = panel.query_one("#ask-panel")
        await _wait_for_layout(
            pilot,
            lambda: (
                footer.region.y >= inner.region.y + inner.region.height
                and card_panel.region.height >= inner.region.height + footer.region.height + 2
            ),
        )

        # Four one-line options separated by three blank rows.
        assert options_widget.region.height == 7
        assert inner.region.height >= options_widget.region.height + 3
        assert footer.region.y >= inner.region.y + inner.region.height
        assert card_panel.region.height >= inner.region.height + footer.region.height + 2


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_wrapped_options_resize_answer_body() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(140, 37)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        options = [
            "This is a very long option label that wraps after the terminal narrows significantly",
            "Another long option label that should also wrap and force the inline panel to grow",
        ]

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(options))
        await _wait_for_layout(pilot, lambda: len(panel.query("#askuser-q0-options")) > 0)

        options_widget = panel.query_one("#askuser-q0-options", AskUserOptions)
        footer = panel.query_one("#askuser-footer")
        card_panel = panel.query_one("#ask-panel")
        inner = panel.query_one("#askuser-inner")
        await _wait_for_layout(pilot, lambda: options_widget.region.height == 3)

        await pilot.resize_terminal(45, 37)
        # Poll every asserted condition: the container height settles a frame
        # after the options wrap, and loaded CI workers need the long deadline.
        await _wait_for_layout(
            pilot,
            lambda: (
                options_widget.region.height >= 5
                and options_widget.region.height == options_widget.virtual_size.height
                and inner.region.height >= options_widget.region.height + 3
                and card_panel.region.height >= options_widget.region.height + footer.region.height + 4
            ),
        )

        assert options_widget.region.height >= 5
        assert options_widget.region.height == options_widget.virtual_size.height
        assert options_widget.scroll_y == 0
        assert card_panel.region.height >= options_widget.region.height + footer.region.height + 4


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_many_options_do_not_collapse_under_multiline_input() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(120, 37)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        options = ["写代码", "读书", "运动", "看电影/剧", "睡觉", "outdoors / 户外", "\u200b"]

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "你周末通常喜欢做什么?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(options))
        await _wait_for_layout(pilot, lambda: len(panel.query("#askuser-q0-options")) > 0)

        input_area = panel.query_one("#askuser-input", EnhancedTextArea)
        options_widget = panel.query_one("#askuser-q0-options", AskUserOptions)
        input_area.insert("abca\nasdf\nasdf\nasdf")

        footer = panel.query_one("#askuser-footer")
        inner = panel.query_one("#askuser-inner")
        card_panel = panel.query_one("#ask-panel")
        # Six one-line options separated by five blank rows, under the
        # question block with its top margin and the list's top margin; the
        # panel adds the six-row input, the button row and its frame.
        await _wait_for_layout(
            pilot,
            lambda: (
                input_area.outer_size.height == 6
                and options_widget.region.height == 11
                and inner.region.height == 15
                and footer.region.y >= inner.region.y + inner.region.height
                and card_panel.region.height == 15 + 6 + 3 + 2
            ),
        )

        assert options_widget.option_count == 6
        assert options_widget.region.height == 11
        assert footer.region.y >= inner.region.y + inner.region.height
        assert [options_widget.get_option_at_index(index).prompt.plain for index in range(6)] == [
            "写代码",
            "读书",
            "运动",
            "看电影/剧",
            "睡觉",
            "outdoors / 户外",
        ]

        stable_panel_height = card_panel.region.height
        stable_tool_height = panel.query_one(AskUserToolCall).region.height
        assert not await wait_until(
            lambda: (
                card_panel.region.height != stable_panel_height
                or panel.query_one(AskUserToolCall).region.height != stable_tool_height
            ),
            timeout=0.2,
            pilot=pilot,
        )


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_ignores_blank_options() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(120, 35)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        options = ["写一段示例代码", "解释一个概念", "审查你的代码", "   ", "\u200b", "\ufeff"]

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "你希望我接下来展示什么?"})
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(options))
        await _wait_for_layout(pilot, lambda: len(panel.query("#askuser-q0-options")) > 0)

        input_area = panel.query_one("#askuser-input", EnhancedTextArea)
        options_widget = panel.query_one("#askuser-q0-options", AskUserOptions)

        assert [options_widget.get_option_at_index(index).prompt.plain for index in range(3)] == [
            "写一段示例代码",
            "解释一个概念",
            "审查你的代码",
        ]
        assert options_widget.option_count == 3
        with pytest.raises(OptionDoesNotExist):
            options_widget.get_option("askuser-q0-opt-3")

        input_area.insert("1111\n2222\n3333")
        await _wait_for_layout(
            pilot,
            lambda: input_area.outer_size.height >= 5 and options_widget.region.height == 5,
        )

        # Three one-line options separated by two blank rows.
        assert options_widget.region.height == 5


@pytest.mark.asyncio
async def test_chat_panel_inline_ask_user_expands_and_locks_tool_group_until_result() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        await pilot.pause()
        group = panel._tool_groups_by_call_id["c1"]
        group.collapsed = True
        await pilot.pause()

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(["Python"]))
        await pilot.pause()

        assert group.collapsed is False
        group.collapsed = True
        assert group.collapsed is False
        panel.toggle_fold_all()
        assert group.collapsed is False
        group.on_tool_group_title_clicked()
        assert group.collapsed is False

        await panel.add_tool_result("c1", "ask_user", "User response: Python", duration_ms=5)
        await pilot.pause()

        group.on_tool_group_title_clicked()
        assert group.collapsed is True


@pytest.fixture(params=[False, True], ids=["normal-idle", "immediate-idle"])
def _inline_pilot_idle(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise inline interaction and cleanup without Pilot's incidental idle delay."""
    if request.param:

        async def immediate_idle(min_sleep: float = 0.02, max_sleep: float = 1) -> None:
            pass

        monkeypatch.setattr("textual.pilot.wait_for_idle", immediate_idle)


@pytest.mark.asyncio
async def test_request_matched_inline_timeout_unlocks_without_auto_collapsing(_inline_pilot_idle: None) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel(tool_groups_expanded=lambda: False)

    async with PanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        group = panel._tool_groups_by_call_id["c1"]
        assert group.collapsed is True

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(["Python"]))
        await pilot.pause()
        assert group.collapsed is False
        assert group.collapse_locked is True

        assert panel.clear_ask_user_inline("c1", "stale-request") is False
        assert group.collapse_locked is True
        assert panel.clear_ask_user_inline("c1", "req-1") is True
        await wait_for(
            lambda: not panel.query("#ask-inline"),
            pilot=pilot,
            description="timed-out inline prompt removed from the transcript",
        )

        assert group.collapsed is False
        assert group.collapse_locked is False
        with pytest.raises(NoMatches):
            panel.query_one("#ask-inline")


@pytest.mark.asyncio
async def test_failed_inline_handoff_does_not_expand_or_lock_group(monkeypatch: pytest.MonkeyPatch) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel(tool_groups_expanded=lambda: False)

    async with PanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
        group = panel._tool_groups_by_call_id["c1"]
        monkeypatch.setattr(AskUserToolCall, "show_inline_prompt", lambda self, *args, **kwargs: False)

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(["Python"])) is False
        assert group.collapsed is True
        assert group.collapse_locked is False


@pytest.mark.asyncio
async def test_chat_panel_clears_inline_ask_user_prompts_across_tool_groups(_inline_pilot_idle: None) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)

        await panel.add_tool_start("c1", "ask_user", KIND_ASK_USER, args={"question": "First?"})
        await panel.add_agent_message("between tools")
        await panel.add_tool_start("c2", "ask_user", KIND_ASK_USER, args={"question": "Second?"})
        await pilot.pause()

        first_group = panel._tool_groups_by_call_id["c1"]
        second_group = panel._tool_groups_by_call_id["c2"]
        assert first_group is not second_group

        assert panel.show_ask_user_inline("c1", "req-1", _one_question(["Python"]))
        await pilot.pause()
        first_group.on_tool_group_title_clicked()
        assert first_group.collapsed is False

        panel.clear_ask_user_inline_prompts()
        # Clearing unlocks the group synchronously; Textual unmounts the form
        # asynchronously, beyond Pilot's message-queue / CPU-idle heuristic.
        await wait_for(
            lambda: not panel.query("#ask-inline"),
            pilot=pilot,
            description="inline prompts removed from every tool group",
        )

        with pytest.raises(NoMatches):
            panel.query_one("#ask-inline")
        first_group.on_tool_group_title_clicked()
        assert first_group.collapsed is True


@pytest.mark.asyncio
async def test_completed_multi_question_card_hangs_answers_under_questions() -> None:
    args = {
        "questions": [
            {"question": "Pick?", "options": [{"label": "A"}]},
            {"question": "Why?"},
        ]
    }

    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args=args)

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_complete(
            json.dumps(
                {
                    "responses": [
                        {"question": "Pick?", "answers": ["A"], "note": "because"},
                        {"question": "Why?", "answers": [], "unanswered": True},
                    ]
                }
            ),
            duration_ms=10,
        )
        await pilot.pause()

        content = tool.query_one("#ask-answer", Static).content
        assert rich_plain(content) == "1. Pick?\n   └─ A\n      because\n2. Why?\n   └─ (not answered)"
        # Questions keep the foreground colour inside the muted answer block;
        # the hanging answers inherit the block's muted colour.
        question_colour = tool.get_component_rich_style("askuser-answer--question", partial=True).color
        assert question_colour is not None
        coloured = {
            segment.text.strip()
            for line in rich_segment_lines(content)
            for segment in line
            if segment.style is not None and segment.style.color is not None
        }
        assert coloured == {"Pick?", "Why?"}
        assert {
            segment.style.color
            for line in rich_segment_lines(content)
            for segment in line
            if segment.style is not None and segment.text.strip() in coloured
        } == {question_colour}


@pytest.mark.asyncio
async def test_completed_card_displays_controls_as_replacement_characters_and_copies_the_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    esc = "\x1b[2J"
    args = {"questions": [{"question": f"Pick{esc}?\n\tdeep", "options": [{"label": "A"}]}, {"question": "Why?"}]}

    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args=args)

    async with ToolApp().run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_complete(
            json.dumps(
                {
                    "responses": [
                        {"question": "Pick?", "answers": ["A"], "note": f"no{esc}te\tx"},
                        {"question": "Why?", "answers": [f"own{esc}"]},
                    ]
                }
            ),
            duration_ms=10,
        )
        await pilot.pause()

        assert tool.query_one("#ask-question", VirtualizedMarkdown).source == "1. Pick�[2J?\n    deep\n2. Why?"
        shown = rich_plain(tool.query_one("#ask-answer", Static).content)
        assert "\x1b" not in shown and "\t" not in shown
        assert "1. Pick�[2J?\n       deep\n" in shown
        assert "no�[2Jte    x" in shown and "own�[2J" in shown
        tool.copy_tool_execution()
        await pilot.pause()
        # Copy starts from the raw text, not the display copy, and escapes controls its own way.
        assert "1. Pick\\x1b[2J?\n\tdeep\n2. Why?" in copied[-1]


@pytest.mark.asyncio
async def test_completed_card_hangs_wrapped_questions_and_answers_under_their_first_column() -> None:
    long_question = (
        "这个 bug 表现出来是什么。比如某个操作报错、结果不对、崩溃、卡住等。越具体越好、报错信息和现象都要。"
    )
    long_answer = "在终端里输入中文的时候光标位置不对。删除一个字符会少删半个。切换输入法后更明显。"
    long_note = " ".join(["note"] * 12)
    args = {"questions": [{"question": long_question}, {"question": "Why?", "options": [{"label": "A"}]}]}

    class ToolApp(LocalizedApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args=args)

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_complete(
            json.dumps(
                {
                    "responses": [
                        {"question": long_question, "answers": [long_answer]},
                        {"question": "Why?", "answers": ["A"], "note": long_note},
                    ]
                }
            ),
            duration_ms=10,
        )
        await pilot.pause()

        lines = rich_plain(tool.query_one("#ask-answer", Static).content, width=40).split("\n")
        # Every line either carries a gutter (number or glyph) or hangs under
        # the text that gutter introduced; nothing falls back to the margin.
        numbered = [line for line in lines if line[:3] in {"1. ", "2. "}]
        question_lines = [line for line in lines if line[:3] == "   " and line[3] != " " and line[3:5] != "└─"]
        answer_lines = [line for line in lines if line.startswith("   └─ ")]
        hanging_lines = [line for line in lines if line[:6] == "      " and line[6] != " "]
        assert len(numbered) == 2 and len(answer_lines) == 2
        assert len(question_lines) >= 2 and len(hanging_lines) >= 2
        assert len(lines) == len(numbered) + len(question_lines) + len(answer_lines) + len(hanging_lines)
        assert numbered[1] == "2. Why?"
        rebuilt = "".join(line[3:] for line in [numbered[0], *question_lines])
        assert rebuilt.replace(" ", "") == long_question.replace(" ", "")
