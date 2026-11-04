# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The main chat shows the model's thinking as it streams, above what it led to.

It streams open or as one folded row, as the user's choice reads when it
starts, and folds once the turn goes on past it. Thinking is not part of the
answer: an answer above it stays the answer, copying leaves it out, and a
retry that gives up its response takes it away. A transcript that shows no
thinking is as it would be without it.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widget import AwaitMount, Widget

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.util import stream_tokens
from chrys.app.tui.widgets.chat import messages as messages_module
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    MessageCopyButton,
    ThinkingMessage,
    UserMessage,
    _AgentHeader,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.read_file import ReadFileToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from tests.support.tui_helpers import ChatPanelApp, chat_content_children, click_when_settled
from tests.support.waiting import wait_for


def _shown() -> bool:
    return True


async def _think(cp: ChatPanel, segment_id: str, *texts: str, finish: bool = True) -> None:
    for text in texts:
        await cp.add_thinking_delta(segment_id, "reasoning", text)
    if finish:
        cp.finish_thinking_segment(segment_id)


async def _tool(cp: ChatPanel, call_id: str) -> None:
    await cp.add_tool_start(call_id, "read_file", "filesystem.read", f"path=/tmp/{call_id}")
    await cp.add_tool_result(call_id, "read_file", "tool output")


def _shape(cp: ChatPanel) -> list[tuple[str, str]]:
    """Each transcript entry as (kind, text); a tool group gives the number of its calls."""
    shape: list[tuple[str, str]] = []
    for child in chat_content_children(cp):
        if isinstance(child, ThinkingMessage):
            shape.append(("thinking", child.text))
        elif isinstance(child, AgentMessage):
            shape.append(("intermediate" if child._is_intermediate else "answer", child.text))
        elif isinstance(child, UserMessage):
            shape.append(("user", child.text))
        elif isinstance(child, ToolGroup):
            shape.append(("tools", str(len(child._tool_records))))
        else:
            shape.append((type(child).__name__, ""))
    return shape


@pytest.mark.parametrize("expanded", [True, False, None], ids=["expanded", "folded", "not-shown"])
async def test_thinking_streams_above_what_it_led_to_and_folds_once_the_turn_goes_on_past_it(
    expanded: bool | None,
) -> None:
    async with ChatPanelApp(thinking_expanded=None if expanded is None else lambda: expanded).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r1", "Let me ", "look first.", finish=False)
        if expanded is not None:
            streaming = cp.query_one(ThinkingMessage)
            assert streaming.collapsed is not expanded, "it streams as the choice reads"
            if expanded:
                await wait_for(
                    lambda: streaming.query_one(VirtualizedMarkdown).shown_source == "Let me look first.",
                    pilot=pilot,
                    description="the open thinking shows its text as it streams",
                )
            else:
                assert not streaming.query(VirtualizedMarkdown), "folded, it builds no body"
        cp.finish_thinking_segment("r1")
        await cp.add_answer_delta("a1", "Checking.")
        cp.finish_answer_segment("a1", completed=True)
        await _tool(cp, "read-1")
        await _think(cp, "r2", "The file says yes.")
        await cp.add_answer_delta("a2", "Yes.")
        cp.finish_answer_segment("a2", completed=True)
        await cp.add_agent_message("Yes.")
        await pilot.pause()

        without_thinking = [
            ("user", "prompt"),
            ("intermediate", "Checking."),
            ("tools", "1"),
            ("answer", "Yes."),
        ]
        if expanded is None:
            assert _shape(cp) == without_thinking
            return
        assert _shape(cp) == [
            ("user", "prompt"),
            ("thinking", "Let me look first."),
            ("intermediate", "Checking."),
            ("tools", "1"),
            ("thinking", "The file says yes."),
            ("answer", "Yes."),
        ]
        thinking = list(cp.query(ThinkingMessage))
        assert [message._is_final for message in thinking] == [True, True]
        assert [message.collapsed for message in thinking] == [True, True], "each folds once what it led to starts"
        await wait_for(
            lambda: (
                [message.query_one(_AgentHeader).render().plain for message in thinking]
                == ["▸ Thought: Let me look first.", "▸ Thought: The file says yes."]
            ),
            pilot=pilot,
            description="each folded row runs the opening of its text",
        )
        # Copying the agent's replies, or the whole conversation, leaves the thinking out.
        assert cp.get_agent_responses() == [("Agent", "Checking."), ("Agent", "Yes.")]
        assert cp.get_all_messages() == [("You", "prompt"), ("Agent", "Checking."), ("Agent", "Yes.")]


@pytest.mark.parametrize("expanded", [True, False], ids=["expanded", "folded"])
async def test_thinking_ends_the_tool_group_above_it_once_its_text_is_not_blank(expanded: bool) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: expanded).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _tool(cp, "read-1")
        await _think(cp, "r-blank", " ", "\n")
        await _tool(cp, "read-2")
        group = cp.query_one(ToolGroup)
        assert _shape(cp) == [("user", "prompt"), ("tools", "2")], "blank thinking mounts nothing, the group stays open"

        assert not group.collapsed
        await _think(cp, "r1", "Two reads were enough.", finish=False)
        assert cp.query_one(ThinkingMessage).collapsed is not expanded
        assert group.collapsed

        cp.finish_thinking_segment("r1")
        await _tool(cp, "read-3")
        assert _shape(cp) == [
            ("user", "prompt"),
            ("tools", "2"),
            ("thinking", "Two reads were enough."),
            ("tools", "1"),
        ]


async def test_thinking_not_shown_leaves_the_tool_group_as_it_was_for_the_calls_after_it() -> None:
    async with ChatPanelApp(thinking_expanded=None).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _tool(cp, "read-1")
        await pilot.pause()
        group = cp.query_one(ToolGroup)
        collapsed = group.collapsed

        await _think(cp, "r1", "Two reads were enough.")
        await _tool(cp, "read-2")

        assert _shape(cp) == [("user", "prompt"), ("tools", "2")]
        assert group.collapsed is collapsed


@pytest.mark.parametrize("answer_follows", [False, True], ids=["last-answer", "answer-follows"])
async def test_thinking_after_an_answer_leaves_it_the_answer_until_another_answer_follows(
    answer_follows: bool,
) -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_answer_delta("a1", "First.")
        cp.finish_answer_segment("a1", completed=True)
        await _think(cp, "r1", "Maybe more?")
        if answer_follows:
            await cp.add_answer_delta("a2", "Second.")
            cp.finish_answer_segment("a2", completed=True)
        await cp.add_agent_message("Second." if answer_follows else "First.")
        await pilot.pause()

        if answer_follows:
            assert _shape(cp) == [
                ("user", "prompt"),
                ("intermediate", "First."),
                ("thinking", "Maybe more?"),
                ("answer", "Second."),
            ]
        else:
            assert _shape(cp) == [("user", "prompt"), ("answer", "First."), ("thinking", "Maybe more?")]
            await wait_for(
                lambda: cp.query_one(AgentMessage).query_one(MessageCopyButton).display,
                pilot=pilot,
                description="the answer above the thinking offers its copy button",
            )


@pytest.mark.parametrize("ending", ["segment-failed", "error", "interrupted", "retry", "new-prompt"])
async def test_thinking_cut_off_keeps_the_text_it_showed(ending: str) -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r1", "Half a ", "thought", finish=False)
        thinking = cp.query_one(ThinkingMessage)
        assert not thinking._is_final

        if ending == "segment-failed":
            cp.finish_thinking_segment("r1")
        elif ending == "error":
            await cp.add_error("failed")
        elif ending == "interrupted":
            await cp.add_interrupted()
        elif ending == "retry":
            await cp.prepare_retry()
        else:
            await cp.add_user_message("next prompt")

        assert thinking._is_final
        assert thinking.text == "Half a thought"
        # Whatever streams next starts its own message.
        await _think(cp, "r2", "Fresh start.")
        assert [message.text for message in cp.query(ThinkingMessage)] == ["Half a thought", "Fresh start."]


async def test_a_retry_removes_the_thinking_of_the_response_it_gave_up() -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r-kept", "From a landed response.")
        await _tool(cp, "read-1")
        await _think(cp, "r-done", "Given up, though complete.")
        await _think(cp, "r-open", "Given up mid-", finish=False)

        await cp.abandon_thinking(("r-done", "r-open"))
        # The given-up stream's closing event comes after: it finds nothing to close.
        cp.finish_thinking_segment("r-open")
        await _think(cp, "r-retry", "Thinking again.")

        assert _shape(cp) == [
            ("user", "prompt"),
            ("thinking", "From a landed response."),
            ("tools", "1"),
            ("thinking", "Thinking again."),
        ]


async def test_thinking_a_retry_removes_is_not_kept_to_fold_once_the_turn_goes_on() -> None:
    """The retry fails before anything else shows: the given-up thinking, body and all, is gone with its message."""
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r-done", "# Given up\n\n" + "\n\n".join(f"Paragraph {index}." for index in range(50)))

        def parsed() -> bool:
            # Queried each time: a reference held here would keep the message alive.
            message = cp.query_one(ThinkingMessage)
            return message.query_one(VirtualizedMarkdown).shown_source == message.text

        await wait_for(parsed, pilot=pilot, description="the thinking's body parsed")
        removed = weakref.ref(cp.query_one(ThinkingMessage))

        await cp.abandon_thinking(("r-done",))
        assert _shape(cp) == [("user", "prompt")]
        await wait_for(
            lambda: gc.collect() >= 0 and removed() is None,
            pilot=pilot,
            description="the removed thinking is collected",
        )


async def test_an_interrupt_while_a_retry_removes_its_thinking_still_removes_all_of_it() -> None:
    """The removal runs in the engine task an interrupt cancels; the open block must not stay, never final."""
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r-done", "Given up, though complete.")
        await _think(cp, "r-open", "Given up mid-", finish=False)

        removal = asyncio.create_task(cp.abandon_thinking(("r-done", "r-open")))
        # One turn of the loop: the removal of the first block has started, not finished.
        await asyncio.sleep(0)
        assert not removal.done()
        removal.cancel()
        with pytest.raises(asyncio.CancelledError):
            await removal

        assert _shape(cp) == [("user", "prompt")]


async def test_whether_thinking_opens_is_read_as_its_segment_opens() -> None:
    expanded = [False]
    async with ChatPanelApp(thinking_expanded=lambda: expanded[0]).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r1", "Folded ", finish=False)
        expanded[0] = True
        await _think(cp, "r1", "to its end.", finish=False)
        first = cp.query_one(ThinkingMessage)
        assert first.collapsed is True
        await _think(cp, "r2", "Open ", finish=False)
        expanded[0] = False
        await _think(cp, "r2", "to its end.", finish=False)

        assert _shape(cp) == [("user", "prompt"), ("thinking", "Folded to its end."), ("thinking", "Open to its end.")]
        # The second opened as it streams; the first had folded as it streamed.
        assert [message.collapsed for message in cp.query(ThinkingMessage)] == [True, False]


async def test_a_new_prompt_drops_a_failed_prompt_with_the_thinking_it_alone_got() -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("failed prompt")
        await _think(cp, "r1", "Weighing it ", finish=False)
        await _think(cp, "r2", "a summary")
        await cp.add_interrupted()

        await cp.add_user_message("replacement prompt")

        assert _shape(cp) == [("user", "replacement prompt")]
        assert [item.summary for item in cp.toc_items] == ["replacement prompt"]


@pytest.mark.parametrize("status_type", [ErrorMessage, InterruptedMessage])
async def test_a_new_prompt_keeps_a_failed_prompt_whose_turn_got_more_than_thinking(status_type: type) -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("failed prompt")
        await _think(cp, "r1", "First look.")
        await _tool(cp, "read-1")
        await _think(cp, "r2", "Half ", finish=False)
        if status_type is ErrorMessage:
            await cp.add_error("failed")
        else:
            await cp.add_interrupted()

        await cp.add_user_message("replacement prompt")

        assert _shape(cp) == [
            ("user", "failed prompt"),
            ("thinking", "First look."),
            ("tools", "1"),
            ("thinking", "Half"),
            ("user", "replacement prompt"),
        ]


@pytest.mark.parametrize(
    ("locale", "label", "count", "speed", "thought"),
    [
        ("en", "Thinking", "Thinking (100 tokens)", "Thinking (3.2k tokens · 620 tok/s)", "Thought: "),
        ("zh-Hans", "思考中", "思考中（100 token）", "思考中（3.2k token · 620 tok/s）", "思考："),  # noqa: RUF001
    ],
)
async def test_folded_thinking_reports_progress_on_ticks_and_previews_text_only_once_finished(
    locale: str, label: str, count: str, speed: str, thought: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 10.0
    monkeypatch.setattr(messages_module, "time", SimpleNamespace(monotonic=lambda: now))
    app = ChatPanelApp(thinking_expanded=lambda: False)
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r1", "User" * 100, finish=False)
        thinking = cp.query_one(ThinkingMessage)
        header = thinking.query_one(_AgentHeader)
        await wait_for(lambda: thinking._progress_timer is not None, pilot=pilot, description="thinking shown")
        count_row = header.render()
        assert count_row.plain == f"▸ {count}"

        # Mixed-language chunks accumulate without rounding each delta. The folded row stays one line high.
        sync_anchor = Mock(wraps=cp.schedule_anchor_sync)
        monkeypatch.setattr(cp, "schedule_anchor_sync", sync_anchor)
        now += 5.0
        for _ in range(1000):
            await cp.add_thinking_delta("r1", "reasoning", "文" + "a" * 10)
        assert sync_anchor.call_count == 0
        assert header.render().plain == f"▸ {count}"
        await wait_for(lambda: header.render().plain == f"▸ {speed}", pilot=pilot, description="recent thinking speed")
        speed_row = header.render()
        assert not thinking.query(VirtualizedMarkdown)
        now += stream_tokens._IDLE_SECONDS + 0.1
        await wait_for(lambda: " · " not in header.render().plain, pilot=pilot, description="idle speed disappears")
        assert "3.2k" in header.render().plain

        cp.finish_thinking_segment("r1")
        finished = header.render()
        assert finished.plain.startswith(f"▸ {thought}UserUser")
        title_style = finished.get_style_at_offset(app.console, len("▸ "))
        preview_style = finished.get_style_at_offset(app.console, finished.plain.index("User"))
        assert preview_style != title_style
        for row in (count_row, speed_row):
            for offset in range(len("▸ "), len(row)):
                expected = title_style if offset < len(f"▸ {label}") else preview_style
                assert row.get_style_at_offset(app.console, offset) == expected

        await _think(cp, "r2", "N", finish=False)
        next_header = cp.query(ThinkingMessage).last().query_one(_AgentHeader)
        assert next_header.render().plain == f"▸ {label}"
        sync_anchor.assert_called()


async def test_hidden_thinking_counts_without_a_timer_and_catches_up_when_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Showing or refolding the row must catch it up even before any periodic tick.
    monkeypatch.setattr(ThinkingMessage, "_tick_progress", lambda self: None)
    async with ChatPanelApp(thinking_expanded=lambda: False).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.display = False
        await wait_for(
            lambda: cp not in pilot.app.screen._compositor.visible_widgets, pilot=pilot, description="hidden"
        )
        await cp.add_user_message("prompt")
        await _think(cp, "r1", "User", finish=False)
        thinking = cp.query_one(ThinkingMessage)
        header = thinking.query_one(_AgentHeader)
        assert thinking._progress_timer is None

        cp.display = True
        await wait_for(lambda: thinking._progress_timer is not None, pilot=pilot, description="first show")
        assert header.render().plain == "▸ Thinking (1 token)"
        cp.display = False
        await wait_for(lambda: thinking._progress_timer is None, pilot=pilot, description="hide stops the timer")
        await _think(cp, "r1", "User" * 99, finish=False)

        cp.display = True
        await wait_for(lambda: thinking._progress_timer is not None, pilot=pilot, description="shown again")
        assert header.render().plain.startswith("▸ Thinking (100 tokens")
        thinking.collapsed = False
        assert thinking._progress_timer is None
        await _think(cp, "r1", "User", finish=False)
        thinking.collapsed = True
        assert header.render().plain.startswith("▸ Thinking (101 tokens")
        cp.finish_thinking_segment("r1")
        assert thinking._progress_timer is None
        assert header.render().plain.startswith("▸ Thought: UserUser")


async def test_thinking_names_what_it_shows_and_folds_from_its_header_once_complete() -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _think(cp, "r1", "Step one.", finish=False)
        thinking = cp.query_one(ThinkingMessage)
        header = thinking.query_one(_AgentHeader)
        assert header.render().plain == "▾ Thinking"
        # Streaming, it folds and opens from its header too.
        await click_when_settled(pilot, header)
        await wait_for(
            lambda: thinking.collapsed and not thinking.query(VirtualizedMarkdown),
            pilot=pilot,
            description="streaming thinking folded, its body gone",
        )
        assert header.render().plain.split(" (")[0] == "▸ Thinking"
        await click_when_settled(pilot, header)
        await wait_for(
            lambda: not thinking.collapsed and bool(thinking.query(VirtualizedMarkdown)),
            pilot=pilot,
            description="streaming thinking open again",
        )

        cp.finish_thinking_segment("r1")
        await wait_for(lambda: header.render().plain == "▾ Thought", pilot=pilot, description="complete thinking")
        assert not thinking.query(MessageCopyButton)
        # A selection over the header copies its label, as an answer's does.
        assert header.get_selection(None) == ("[Thought]", "\n")  # type: ignore[arg-type]

        await cp.add_thinking_delta("s1", "reasoning_summary", "In short, ")
        summary = cp.query(ThinkingMessage).last()
        assert summary.query_one(_AgentHeader).render().plain == "▾ Thinking summary"
        # What it led to started: the complete thinking folds, and its body goes.
        assert thinking.collapsed is True
        await wait_for(lambda: not thinking.query(VirtualizedMarkdown), pilot=pilot, description="folded body removed")
        # Folded, the opening of its text follows the label.
        assert header.render().plain == "▸ Thought: Step one."

        await click_when_settled(pilot, header)
        await wait_for(
            lambda: not thinking.collapsed and bool(thinking.query(VirtualizedMarkdown)),
            pilot=pilot,
            description="unfolded body rebuilt",
        )
        await click_when_settled(pilot, header)
        await wait_for(lambda: thinking.collapsed, pilot=pilot, description="complete thinking folded again")


async def test_fold_all_folds_complete_thinking_with_the_tool_groups_but_leaves_thinking_that_streams_open() -> None:
    async with ChatPanelApp(thinking_expanded=_shown).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await _tool(cp, "read-1")
        await _think(cp, "r1", "Step one.")
        await cp.add_thinking_delta("s1", "reasoning_summary", "In short, ")
        (group,) = cp.query(ToolGroup)
        thinking, streaming = cp.query(ThinkingMessage)
        # The turn went on past the group and the complete thinking: both folded.
        assert (group.collapsed, thinking.collapsed, streaming.collapsed) == (True, True, False)

        # Nothing that folds is open, so fold-all unfolds; the streaming thinking keeps its state.
        assert cp.toggle_fold_all() is False
        assert (group.collapsed, thinking.collapsed, streaming.collapsed) == (False, False, False)
        await wait_for(lambda: bool(thinking.query(VirtualizedMarkdown)), pilot=pilot, description="body rebuilt")
        await wait_for(lambda: bool(group.query(ReadFileToolCall)), pilot=pilot, description="calls rebuilt")

        assert cp.toggle_fold_all() is True
        assert (group.collapsed, thinking.collapsed, streaming.collapsed) == (True, True, False)
        await wait_for(lambda: not thinking.query(VirtualizedMarkdown), pilot=pilot, description="body removed")

        # Complete, the open summary folds with them.
        cp.finish_thinking_segment("s1")
        assert cp.toggle_fold_all() is True
        assert (group.collapsed, thinking.collapsed, streaming.collapsed) == (True, True, True)


async def test_thinking_opened_as_its_last_text_arrives_shows_that_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: False).run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        cp.agent_running = True
        await cp.add_thinking_delta("r1", "reasoning", "First.")
        thinking = cp.query_one(ThinkingMessage)
        assert (thinking.collapsed, list(thinking.query(VirtualizedMarkdown))) == (True, [])
        mount = thinking.mount

        def mount_as_the_stream_ends(*widgets: Widget) -> AwaitMount:
            mounting = mount(*widgets)
            # The stream ends as the body it built mounts.
            thinking.append_text("\n\nLast.")
            cp.finish_thinking_segment("r1")
            return mounting

        monkeypatch.setattr(thinking, "mount", mount_as_the_stream_ends)
        thinking.collapsed = False
        # It shows once the parse it waits for ends, after that parse installs.
        await wait_for(
            lambda: (
                [body.shown_source for body in thinking.query(VirtualizedMarkdown) if body.display]
                == ["First.\n\nLast."]
            ),
            pilot=pilot,
            description="opened, the body shows the text the stream ended with",
        )
