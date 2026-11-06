# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A resumed session shows the thinking it saved, folded, where it showed live.

A transcript that shows no thinking (``thinking_expanded`` None) replays as it
would without it; a replayed block, as replayed text between tool calls, builds
its body only once it is opened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from textual.widget import AwaitMount, Widget

from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcAbsorbRequested
from chrys.app.tui.widgets.chat import messages as messages_module
from chrys.app.tui.widgets.chat.fold_rows import text_preview
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage, UserMessage, _AgentHeader
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.history_markers import ANSWER_SEGMENT_KEY, HistoryMarkerKind
from chrys.kernel import Content, Message
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.llm.reasoning_display import REASONING_SUMMARY_KEY
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import ChatPanelApp, GcMessageChatPanelApp, chat_content_children, click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path


def _think(text: str, *, summary: bool = False) -> dict[str, Any]:
    properties = {REASONING_SUMMARY_KEY: True} if summary else None
    return Content.from_text_reasoning(text=text, additional_properties=properties).to_dict()


def _text(text: str, segment: str | None = None) -> dict[str, Any]:
    properties = {ANSWER_SEGMENT_KEY: segment} if segment else None
    return Content.from_text(text, additional_properties=properties).to_dict()


def _call(call_id: str) -> dict[str, Any]:
    return Content.from_function_call(call_id=call_id, name="read_file", arguments="{}").to_dict()


def _result(call_id: str) -> dict[str, Any]:
    return Content.from_function_result(call_id=call_id, result="ok").to_dict()


def _hosted(call_id: str, family: str = "generic") -> list[dict[str, Any]]:
    """A completed hosted call and its result; but for a search, structured output a turn can end with."""
    family = {"hosted_family": family, "hosted_provider": "openai"}
    return [
        Content.from_hosted_tool_call(
            call_id, tool_name="server_task", status="running", provider_phase="start", **family
        ).to_dict(),
        Content.from_hosted_tool_result(
            call_id, tool_name="server_task", result="done", status="completed", provider_phase="terminal", **family
        ).to_dict(),
    ]


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "contents": [_text(text)]}


def _assistant(*contents: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "contents": list(contents)}


def _tool(*contents: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "contents": list(contents)}


def _without_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**message, "contents": [content for content in message["contents"] if content["type"] != "text_reasoning"]}
        for message in messages
    ]


def _shape(panel: ChatPanel) -> list[tuple[str, str]]:
    """Each transcript entry as (kind, text); a tool group gives the number of its calls."""
    shape: list[tuple[str, str]] = []
    for child in chat_content_children(panel):
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


def _shown_bodies(message: Widget) -> list[VirtualizedMarkdown]:
    """The bodies *message* shows. One it builds on opening shows once the parse of its text ends, which the worker
    that waits for it hears after the parse installs."""
    return [body for body in message.query(VirtualizedMarkdown) if body.display]


async def _replayed_shape(messages: list[dict[str, Any]], *, shown: bool = True) -> list[tuple[str, str]]:
    """The replayed transcript's shape; not *shown*, the panel shows no thinking at all."""
    async with ChatPanelApp(thinking_expanded=(lambda: True) if shown else None).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages)
        await panel.wait_replay_complete()
        return _shape(panel)


_A_TURN_WITH_THINKING = [
    _user("prompt"),
    _assistant(_think("Let me look first."), _text("Checking."), _call("read-1")),
    _tool(_result("read-1")),
    _assistant(_think("The file says yes."), _text("Yes.")),
]


async def test_replayed_thinking_shows_folded_where_it_showed_live() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_A_TURN_WITH_THINKING)
        await panel.wait_replay_complete()

        assert _shape(panel) == [
            ("user", "prompt"),
            ("thinking", "Let me look first."),
            ("intermediate", "Checking."),
            ("tools", "1"),
            ("thinking", "The file says yes."),
            ("answer", "Yes."),
        ]
        thinking = list(panel.query(ThinkingMessage))
        assert [message.collapsed for message in thinking] == [True, True]
        assert [str(message.query_one(_AgentHeader).render()) for message in thinking] == [
            "▸ Thought: Let me look first.",
            "▸ Thought: The file says yes.",
        ]
        assert not any(message.query(VirtualizedMarkdown) for message in thinking)


async def test_thinking_and_text_folded_inside_a_hidden_activity_section_read_no_preview_until_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []

    def counting_preview(text: str) -> str:
        reads.append(text)
        return text_preview(text)

    monkeypatch.setattr(messages_module, "text_preview", counting_preview)
    async with ChatPanelApp(thinking_expanded=lambda: True, fold_finished_turns=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_A_TURN_WITH_THINKING)
        await panel.wait_replay_complete()
        (block,) = panel.query(ProcessBlock)
        await wait_for(lambda: block.collapsed and block.region.height == 1, pilot=pilot, description="folded section")
        assert not any(message.display for message in panel.query(ThinkingMessage))
        assert reads == []

        await click_when_settled(pilot, block)
        await wait_for(lambda: len(reads) == 3, pilot=pilot, description="previews read once the rows paint")
        # The text between tool calls folds to its row too.
        assert sorted(reads) == ["Checking.", "Let me look first.", "The file says yes."]


async def test_thinking_a_replay_does_not_show_leaves_the_transcript_as_without_it() -> None:
    shape = await _replayed_shape(_A_TURN_WITH_THINKING, shown=False)

    assert shape == [("user", "prompt"), ("intermediate", "Checking."), ("tools", "1"), ("answer", "Yes.")]
    assert shape == await _replayed_shape(_without_thinking(_A_TURN_WITH_THINKING), shown=False)


@pytest.mark.parametrize(
    ("thought", "shown", "expected"),
    [
        pytest.param("Next, the other file.", True, ["1", "thinking", "1"], id="shown"),
        pytest.param("Next, the other file.", False, ["2"], id="not-shown"),
        pytest.param(" \n", True, ["2"], id="blank"),
    ],
)
async def test_replayed_thinking_before_tools_ends_the_tool_group_above_it(
    thought: str, shown: bool, expected: list[str]
) -> None:
    """Live, thinking with text ends the group its tools would have joined; blank thinking showed nothing."""
    messages = [
        _user("prompt"),
        _assistant(_call("read-1")),
        _tool(_result("read-1")),
        _assistant(_think(thought), _call("read-2")),
        _tool(_result("read-2")),
        _assistant(_text("Done.")),
    ]

    shape = await _replayed_shape(messages, shown=shown)

    assert [text if kind == "tools" else kind for kind, text in shape[1:-1]] == expected


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        pytest.param(
            [_text("First look.", "a1"), _think("Hmm.")],
            [("answer", "First look."), ("thinking", "Hmm.")],
            id="no-answer-after",
        ),
        pytest.param(
            [_text("First look.", "a1"), _think("Hmm."), _text("The answer.", "a2")],
            [("intermediate", "First look."), ("thinking", "Hmm."), ("answer", "The answer.")],
            id="another-answer-after",
        ),
        pytest.param(
            [_text("First look."), _think("Hmm."), _text("The answer.")],
            [("intermediate", "First look."), ("thinking", "Hmm."), ("answer", "The answer.")],
            id="unstamped-answers",
        ),
    ],
)
async def test_replayed_thinking_after_an_answer_leaves_it_the_answer_until_another_answer_follows(
    contents: list[dict[str, Any]], expected: list[tuple[str, str]]
) -> None:
    shape = await _replayed_shape([_user("prompt"), _assistant(*contents)])

    assert shape == [("user", "prompt"), *expected]


async def test_the_final_answer_after_replayed_thinking_keeps_its_timestamp() -> None:
    created_at = "2026-08-19T01:02:04+00:00"
    message = _assistant(_text("First look.", "a1"), _think("Hmm."), _text("The answer.", "a2"))
    message["additional_properties"] = {MESSAGE_CREATED_AT_KEY: created_at}

    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history([_user("prompt"), message])
        await panel.wait_replay_complete()

        first, final = panel.query(AgentMessage)
        assert (first._ts, first._is_intermediate) == ("", True)
        assert final._ts
        assert final._is_intermediate is False


async def test_replayed_thinking_and_its_summary_show_as_blocks_of_their_own() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(
            [
                _user("prompt"),
                _assistant(_think("Step one. "), _think("Step two."), _think("In short.", summary=True), _text("A.")),
            ]
        )
        await panel.wait_replay_complete()

        assert _shape(panel)[1:] == [
            ("thinking", "Step one. Step two."),
            ("thinking", "In short."),
            ("answer", "A."),
        ]
        headers = [str(message.query_one(_AgentHeader).render()) for message in panel.query(ThinkingMessage)]
        assert headers == ["▸ Thought: Step one. Step two.", "▸ Thinking summary: In short."]


async def test_a_tool_result_between_thinking_replays_it_as_two_blocks() -> None:
    """Live, a result closes the thinking segment before it; the thinking after opens another."""
    hosted_call, hosted_result = _hosted("h1")
    message = _assistant(hosted_call, _think("Before result."), hosted_result, _think("After result."), _text("A."))

    shape = await _replayed_shape([_user("prompt"), message])

    assert shape[1:] == [
        ("tools", "1"),
        ("thinking", "Before result."),
        ("thinking", "After result."),
        ("answer", "A."),
    ]


async def test_thinking_with_no_readable_text_replays_nothing() -> None:
    opaque = Content.from_text_reasoning(protected_data="opaque").to_dict()

    shape = await _replayed_shape([_user("prompt"), _assistant(opaque, _text("Done."))])

    assert shape == [("user", "prompt"), ("answer", "Done.")]


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([_assistant(*_hosted("h1"), _think("That worked."))], id="same-message"),
        pytest.param([_assistant(*_hosted("h1")), _assistant(_think("That worked."))], id="next-message"),
    ],
)
@pytest.mark.parametrize("shown", [True, False], ids=["shown", "not-shown"])
async def test_the_check_of_a_structured_result_follows_the_thinking_after_it(
    messages: list[dict[str, Any]], shown: bool
) -> None:
    """Live, the ✓ shows as the turn ends: below the thinking that followed the result."""
    shape = await _replayed_shape([_user("prompt"), *messages], shown=shown)

    thinking = [("thinking", "That worked.")] if shown else []
    assert shape == [("user", "prompt"), ("tools", "1"), *thinking, ("answer", "✓")]


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        pytest.param(True, [("tools", "1"), ("thinking", "Now search."), ("tools", "1")], id="shown"),
        pytest.param(False, [("tools", "2")], id="not-shown"),
    ],
)
async def test_the_check_reads_every_tool_group_shown_thinking_split_the_last_one_into(
    shown: bool, expected: list[tuple[str, str]]
) -> None:
    """A search alone completes nothing; structured output in a group above the thinking still does."""
    messages = [_user("prompt"), _assistant(*_hosted("h1"), _think("Now search."), *_hosted("h2", "search"))]

    shape = await _replayed_shape(messages, shown=shown)

    assert shape == [("user", "prompt"), *expected, ("answer", "✓")]


async def test_replayed_thinking_in_a_compressed_part_shows_as_compressed() -> None:
    fold = {
        "role": "assistant",
        "contents": [_text("[Compressed context: ctx-1]\nSummary: condensed")],
        "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-1"},
    }
    block = CompressedBlock(
        compressed_context_id="ctx-1",
        messages=[
            Message("user", [Content.from_text("old prompt")]),
            Message(
                "assistant",
                [
                    Content.from_text_reasoning(text="Look at a."),
                    Content.from_function_call(call_id="c1", name="read_file", arguments="{}"),
                ],
            ),
            Message("tool", [Content.from_function_result(call_id="c1", result="ok")]),
            Message("assistant", [Content.from_text_reasoning(text="Done thinking."), Content.from_text("Old.")]),
        ],
    )

    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history([fold], compressed_blocks={"ctx-1": block})
        await panel.wait_replay_complete()

        assert _shape(panel)[1:5] == [
            ("thinking", "Look at a."),
            ("tools", "1"),
            ("thinking", "Done thinking."),
            ("answer", "Old."),
        ]
        assert panel.query_one(ToolGroup).has_class("--compressed")
        assert [message.has_class("--compressed") for message in panel.query(ThinkingMessage)] == [True, True]


async def test_a_replayed_block_builds_its_body_when_first_opened_and_hands_it_to_the_gc_freeze() -> None:
    app = GcMessageChatPanelApp(thinking_expanded=lambda: True)
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.replay_history([_user("prompt"), _assistant(_think("# Plan\n\nLook first."), _text("Done."))])
        await panel.wait_replay_complete()
        (thinking,) = panel.query(ThinkingMessage)
        assert not thinking.query(VirtualizedMarkdown)
        absorbs_before = len([message for message in app.gc_messages if isinstance(message, GcAbsorbRequested)])

        await click_when_settled(pilot, thinking.query_one(_AgentHeader))
        # The parse installing hands it to the GC freeze, and the opening shows the body once that parse ended.
        await wait_for(
            lambda: (
                [message for message in app.gc_messages if isinstance(message, GcAbsorbRequested)][absorbs_before:]
                and _shown_bodies(thinking)
            ),
            pilot=pilot,
            description="the opened body's parse is handed to the GC freeze and shows",
        )

        (body,) = thinking.query(VirtualizedMarkdown)
        assert (thinking.collapsed, body.display, body.shown_source) == (False, True, "# Plan\n\nLook first.")
        absorbs = [message for message in app.gc_messages if isinstance(message, GcAbsorbRequested)]
        assert [message.reason for message in absorbs[absorbs_before:]] == [GcAbsorbReason.STABLE_CONTENT_MOUNTED]

        await click_when_settled(pilot, thinking.query_one(_AgentHeader))
        await wait_for(
            lambda: not thinking.query(VirtualizedMarkdown), pilot=pilot, description="folded thinking body released"
        )
        assert thinking.collapsed
        await click_when_settled(pilot, thinking.query_one(_AgentHeader))
        await wait_for(lambda: _shown_bodies(thinking), pilot=pilot, description="thinking body rebuilt and shown")
        assert thinking.query_one(VirtualizedMarkdown) is not body


async def test_replayed_text_between_tool_calls_builds_its_body_only_once_opened() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(
            [
                _user("prompt"),
                _assistant(_text("Let me look."), _call("c1")),
                _tool(_result("c1")),
                _assistant(_text("Done.")),
            ]
        )
        await panel.wait_replay_complete()
        between, answer = panel.query(AgentMessage)
        assert (between.foldable, between.collapsed) == (True, True)
        assert not between.query(VirtualizedMarkdown)
        assert [body.shown_source for body in answer.query(VirtualizedMarkdown)] == ["Done."]

        await click_when_settled(pilot, between.query_one(_AgentHeader))
        await wait_for(
            lambda: [body.shown_source for body in between.query(VirtualizedMarkdown)] == ["Let me look."],
            pilot=pilot,
            description="opened, the text builds its body",
        )
        body = between.query_one(VirtualizedMarkdown)
        await click_when_settled(pilot, between.query_one(_AgentHeader))
        assert between.collapsed
        await click_when_settled(pilot, between.query_one(_AgentHeader))
        # Folded again, it kept the body it built.
        assert (between.collapsed, list(between.query(VirtualizedMarkdown)), body.display) == (False, [body], True)


async def test_fold_all_opens_and_folds_replayed_thinking_with_the_tool_groups() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_A_TURN_WITH_THINKING)
        await panel.wait_replay_complete()
        (group,) = panel.query(ToolGroup)
        thinking = list(panel.query(ThinkingMessage))

        assert panel.toggle_fold_all() is False
        assert [group.collapsed, *(message.collapsed for message in thinking)] == [False, False, False]
        await wait_for(
            lambda: all(_shown_bodies(message) for message in thinking),
            pilot=pilot,
            description="fold-all rebuilds thinking bodies and shows them",
        )
        bodies = [message.query_one(VirtualizedMarkdown) for message in thinking]

        assert panel.toggle_fold_all() is True
        assert [group.collapsed, *(message.collapsed for message in thinking)] == [True, True, True]
        assert [body.display for body in bodies] == [False, False]
        await wait_for(
            lambda: all(not message.query(VirtualizedMarkdown) for message in thinking),
            pilot=pilot,
            description="fold-all releases thinking bodies",
        )


async def test_fold_all_between_a_batch_registering_and_mounting_still_opens_its_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        real_mount = panel.mount
        mounted_when_folded: list[bool] = []

        def mount(*widgets: Widget, **kwargs: Any) -> AwaitMount:
            awaitable = real_mount(*widgets, **kwargs)
            if thinking := list(panel.query(ThinkingMessage)):
                mounted_when_folded.extend(message.is_mounted for message in thinking)
                assert panel.toggle_fold_all() is False
            return awaitable

        with monkeypatch.context() as patch:
            patch.setattr(panel, "mount", mount)
            await panel.replay_history(_A_TURN_WITH_THINKING)
            await panel.wait_replay_complete()
        thinking = list(panel.query(ThinkingMessage))

        def shown_bodies() -> list[str]:
            return [body.shown_source for message in thinking for body in _shown_bodies(message)]

        # Folded as the block was composed, so its body is built once it mounts.
        assert mounted_when_folded == [False, False]
        await wait_for(
            lambda: shown_bodies() == ["Let me look first.", "The file says yes."],
            pilot=pilot,
            description="each opened block shows its body",
        )
        assert [message.collapsed for message in thinking] == [False, False]


async def test_a_resumed_session_shows_its_thinking_by_default(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=EventBus())
    async with app.run_test(size=(120, 36)):
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)

        await panel.replay_history(_A_TURN_WITH_THINKING)
        await panel.wait_replay_complete()

        assert [message.text for message in panel.query(ThinkingMessage)] == [
            "Let me look first.",
            "The file says yes.",
        ]
