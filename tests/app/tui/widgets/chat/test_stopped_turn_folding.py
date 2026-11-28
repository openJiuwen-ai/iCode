# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A turn that stops folds as a finished one does, and a continued run carries on in the title it stopped in.

An interruption or an error ends a turn: each of its sections folds behind its own title, its last reply
and the notice stay below. Continue turns the stopped title back into the running row; when the turn
ends again it folds as one title with every step.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from textual.widget import Widget

from chrys.app.tui.widgets.chat import process_controller as process_controller_module
from chrys.app.tui.widgets.chat import replay_mount as replay_mount_module
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    ThinkingMessage,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import _WAITING_ROW_DELAY_SECONDS, ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Content, Message
from tests.app.tui.widgets.chat.replay_fixtures import _turns
from tests.app.tui.widgets.chat.test_live_process_row import (
    _READ_KIND,
    _finish,
    _members,
    _on,
    _painted,
    _read,
    _row,
    _rows,
    _say,
    _settled,
    _shown,
    _summary,
    _think,
)
from tests.support.tui_helpers import (
    ChatPanelApp,
    chat_content_children,
    click_when_settled,
    open_process_for,
)
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from chrys.app.tui.widgets.chat.scroll_controller import ChatGcPauseClaim

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("ending,opened", [("error", False), ("interrupted", False), ("interrupted", True)])
async def test_a_failed_or_interrupted_turn_keeps_its_steps_folded_with_the_notice_below(
    ending: str, opened: bool
) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _say(panel, "a1", "Checking.")
        await _read(panel, "c1", "/tmp/a.py")
        await _settled(pilot)
        assert _row(panel).collapsed
        if opened:
            await click_when_settled(pilot, _row(panel))
            await wait_for(lambda: not _row(panel).collapsed, pilot=pilot, description="the row opens")
        if ending == "error":
            await panel.add_error("failed")
        else:
            await panel.add_interrupted()
        await _settled(pilot)
        assert not _rows(panel)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed, "what was one folded row while running stays one folded row"
        assert _painted(block) == "▸ 1 tool call: 1 read"
        members = _members(panel)
        assert len(members) == 3 and all(member in block.members for member in members)
        assert not any(_shown(members))
        notice = panel.query_one(ErrorMessage if ending == "error" else InterruptedMessage)
        assert notice.display and notice not in block.members
        children = chat_content_children(panel)
        assert children.index(block) < children.index(notice)
        # It opens like a finished turn's process.
        await click_when_settled(pilot, block)
        await wait_for(lambda: all(_shown(members)), pilot=pilot, description="the steps show")


async def test_an_interrupted_reply_stays_outside_the_folded_steps() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _say(panel, "a1", "Checking.")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_answer_delta("a2", "Here is the partial")
        panel.finish_answer_segment("a2", completed=False)
        await panel.add_interrupted()
        await _settled(pilot)
        (block,) = panel.query(ProcessBlock)
        checking, partial = panel.query(AgentMessage)
        assert checking in block.members and not checking.display
        assert partial not in block.members and partial.display
        children = chat_content_children(panel)
        notice = panel.query_one(InterruptedMessage)
        assert children.index(block) < children.index(partial) < children.index(notice)


async def test_a_steered_then_interrupted_turn_folds_each_section_into_its_own_row() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_user_message("no tests", is_injection=True)
        await _think(panel, "r2", "Static review then.")
        await _read(panel, "c2", "/tmp/b.py")
        await _read(panel, "c3", "/tmp/c.py")
        await _settled(pilot)
        assert [row.collapsed for row in _rows(panel)] == [True, True]
        await panel.add_interrupted()
        await _settled(pilot)
        assert _summary(panel) == [
            (0, 1, True, "▸ 1 tool call: 1 read"),
            (1, 2, True, "▸ 2 tool calls: 2 reads"),
        ]
        assert not any(_shown(_members(panel)))
        assert panel.query_one(InterruptedMessage).display


async def test_a_reopened_interrupted_turn_folds_as_it_did_when_stopped() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_stopped_history(1))
        await panel.wait_replay_complete()
        await _settled(pilot)
        assert _summary(panel) == [(0, 1, True, "▸ 1 tool call: 1 read")]
        assert not any(_shown(_members(panel)))


async def test_continuing_after_only_thinking_starts_a_separate_running_row() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await panel.add_interrupted()
        await _settled(pilot)
        thinking = panel.query_one(ThinkingMessage)
        assert thinking.display and not panel.query(ProcessBlock)

        panel.capture_settled_status()
        panel.agent_running = True
        await panel.clear_settled_status()
        await pilot.pause(_WAITING_ROW_DELAY_SECONDS * 2)
        await _settled(pilot)
        assert panel.query_one(WaitingRow).display

        await _read(panel, "next", "/tmp/next.py", done=False)
        await _settled(pilot)
        row = _row(panel)
        assert thinking.display and thinking not in row.members
        assert chat_content_children(panel).index(thinking) < chat_content_children(panel).index(row)


async def test_a_new_message_after_a_failure_leaves_the_failed_turn_folded() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_error("failed")
        earlier = _members(panel)
        await panel.add_user_message("try again")
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed and all(member in block.members for member in earlier)
        assert not any(_shown(earlier))
        row = _row(panel)
        assert row.collapsed
        children = chat_content_children(panel)
        assert children.index(row) > children.index(panel.query(UserMessage).last())


@pytest.mark.parametrize("opened", [False, True], ids=["folded", "open"])
async def test_continuing_runs_the_stopped_title_at_once_with_no_waiting_row_below_it(opened: bool) -> None:
    """Between Continue and its first step the stopped title is the running row, as between a running turn's steps."""
    async with ChatPanelApp(thinking_expanded=_on).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for call in ("c1", "c2"):
            await _read(panel, call, f"/tmp/{call}.py")
        # The last call is still running when the user stops the turn.
        await _read(panel, "c3", "/tmp/c3.py", done=False)
        await panel.add_interrupted()
        await _settled(pilot)
        (stopped,) = panel.query(ProcessBlock)
        place = chat_content_children(panel).index(stopped)
        if opened:
            open_process_for(panel.query(ToolGroup).first())
            await _settled(pilot)
            assert all(group.display for group in panel.query(ToolGroup))
        title = _painted(stopped)
        assert "interrupted" in title

        # Continue: the run starts again and the model thinks, out of sight, before its next step.
        panel.capture_settled_status()
        panel.agent_running = True
        await panel.clear_settled_status()
        await pilot.pause(_WAITING_ROW_DELAY_SECONDS * 2)
        await _settled(pilot)
        running = _row(panel)
        assert not panel.query(ProcessBlock) and not panel.query(InterruptedMessage)
        assert chat_content_children(panel).index(running) == place and running.collapsed is not opened
        assert all(group.display is opened for group in panel.query(ToolGroup))
        assert not panel.query_one(WaitingRow).display, "no row of its own below the title while the turn waits"
        assert _painted(running) == title, "the stopped call does not read as running again"
        assert all(group in running.members for group in panel.query(ToolGroup))
        if not opened:
            assert not running.gap_above and running.region.height == 2

        # An automatic retry drops this attempt's only new step.
        await panel.add_thinking_delta("abandoned", "reasoning", "Trying a different approach.")
        await panel.abandon_thinking(("abandoned",))
        await _settled(pilot)
        assert _painted(running) == title
        assert not await wait_until(
            lambda: panel.query_one(WaitingRow).display, timeout=_WAITING_ROW_DELAY_SECONDS * 2, pilot=pilot
        )

        await _read(panel, "c4", "/tmp/c4.py")
        await _settled(pilot)
        assert _rows(panel) == [running], "the next step goes on in that row"
        assert running.collapsed is not opened and all(group.display is opened for group in panel.query(ToolGroup))
        assert _painted(running) != title, "the row shows the run's new step"
        if not opened:
            assert not panel.query_one(WaitingRow).display
        await _finish(panel)
        await _settled(pilot)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed and block.tool_count == 4
        assert not _rows(panel)
        assert all(group in block.members and not group.display for group in panel.query(ToolGroup))


@pytest.mark.parametrize("drain_history", [False, True], ids=["cold-archive", "drained-archive"])
async def test_continuing_a_restored_stopped_turn_keeps_its_own_fold_state(drain_history: bool) -> None:
    history = _turns(96)
    history.append(
        {"role": "system", "contents": [], "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN}}
    )
    history.extend(_stopped_history(3))
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(history)
        await _settled(pilot)
        oldest = next(widget for widget in panel.pending_transcript_widgets() if widget.id == "turn-1")
        assert panel.query_one(ProcessBlock).tool_count == 3
        assert panel.toggle_fold_all() is False
        if drain_history:
            await panel.wait_replay_complete()
            assert panel.toggle_fold_all() is True

        panel.capture_settled_status()
        panel.agent_running = True
        await panel.clear_settled_status()
        assert _row(panel).tally.total == 3
        await _read(panel, "next", "/tmp/next.py")
        await _finish(panel)
        await _settled(pilot)

        completed = panel.query_one(ProcessBlock)
        assert completed.tool_count == 4 and completed.collapsed
        assert not panel.query(InterruptedMessage)
        assert panel._replay_mount.windowed and oldest.is_attached is drain_history


@pytest.mark.parametrize("note", ["sent-with-continue", "sent-before-the-first-step"])
async def test_continuing_with_a_note_keeps_the_stopped_title_above_it(note: str) -> None:
    """A note starts a new section; the stopped title above it still reads as it stopped."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await panel.add_interrupted()
        await _settled(pilot)
        title = _painted(panel.query_one(ProcessBlock))

        panel.capture_settled_status()
        if note == "sent-with-continue":
            # As the main screen does: the note shows as it is sent, then the run starts again.
            await panel.add_user_message("also this", is_injection=True)
        panel.agent_running = True
        await panel.clear_settled_status()
        if note == "sent-before-the-first-step":
            # A message sent while the continued run has not added a step yet.
            await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c3", "/tmp/c.py")
        await _settled(pilot)
        note = panel.query(UserMessage).last()
        above = [entry for entry in chat_content_children(panel) if entry.region.y < note.region.y and entry.display]
        assert _painted(above[-1]) == title, "the stopped call does not read as the section's latest step"
        assert _painted(_rows(panel)[-1]) != title


@pytest.mark.parametrize("opened", [False, True], ids=["folded", "open"])
async def test_continuing_keeps_no_waiting_row_when_old_steps_are_withdrawn_or_abandoned(opened: bool) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "thought", "Look first.")
        await _say(panel, "step", "Checking.")
        await _read(panel, "c1", "/tmp/a.py", done=False)
        await panel.add_interrupted()
        if opened:
            open_process_for(panel.query_one(ToolGroup))
        panel.capture_settled_status()
        panel.agent_running = True
        await panel.clear_settled_status()
        running = _row(panel)

        # The retry withdraws text and abandons thinking before any new step arrives.
        for remove, segment in ((panel.withdraw_answer, "step"), (panel.abandon_thinking, "thought")):
            await remove((segment,))
            await _settled(pilot)
            assert _rows(panel) == [running] and running.collapsed is not opened
            assert not await wait_until(
                lambda: panel.query_one(WaitingRow).display,
                timeout=_WAITING_ROW_DELAY_SECONDS * 2,
                pilot=pilot,
            )
        assert not panel.query(AgentMessage) and not panel.query(ThinkingMessage)
        assert panel.query_one(ToolGroup) in running.members

        await _read(panel, "next", "/tmp/next.py")
        await _finish(panel)
        assert panel.query_one(ProcessBlock).tool_count == 2


async def test_continuing_after_a_withdrawn_step_counts_the_new_steps_under_the_same_title() -> None:
    """Continue drops the stopped pass's replies, one of them a folded step; the new steps join the same title."""
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _say(panel, "seg-1", "First part.")
        await panel.add_thinking_delta("think", "reasoning", "Hmm.")
        panel.finish_thinking_segment("think")
        await _say(panel, "seg-2", "Second part.")
        await panel.add_interrupted()
        await _settled(pilot)
        (stopped,) = panel.query(ProcessBlock)
        assert stopped.tool_count == 1

        panel.hide_trailing_status_action()
        await panel.withdraw_answer(("seg-1", "seg-2"))
        panel.agent_running = True
        await _read(panel, "c2", "/tmp/b.py")
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)
        (block,) = panel.query(ProcessBlock)
        assert block.tool_count == 2
        assert all(group in block.members and not group.display for group in panel.query(ToolGroup))


async def test_retiring_a_thinking_only_turn_leaves_no_empty_title() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for attempt in ("first", "second"):
            # Stopped while still thinking, then continued.
            await panel.add_thinking_delta(attempt, "reasoning", "Only thinking.")
            panel.finish_thinking_segment(attempt)
            await panel.add_interrupted()
            if attempt == "first":
                panel.hide_trailing_status_action()
                panel.agent_running = True
        await _settled(pilot)
        assert panel.query(ProcessBlock)

        await panel.add_user_message("New question")
        await panel.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=())
        await _settled(pilot)
        assert not panel.query(ProcessBlock)
        assert not panel.query(ThinkingMessage)


async def test_withdrawing_a_step_keeps_the_other_steps_and_the_previous_title() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Earlier question")
        await _read(panel, "earlier", "/tmp/earlier.py")
        await _finish(panel)
        earlier = panel.query_one(ProcessBlock)
        open_process_for(panel.query_one(ToolGroup))

        await panel.add_user_message("Current question")
        await _read(panel, "first", "/tmp/first.py")
        await _say(panel, "withdrawn", "A step to withdraw.")
        await _read(panel, "second", "/tmp/second.py")
        await panel.add_interrupted()
        _, stopped = panel.query(ProcessBlock)
        step = next(message for message in panel.query(AgentMessage) if "withdrawn" in message.segment_ids)
        assert step in stopped.members

        await panel.withdraw_answer(("withdrawn",))
        assert list(panel.query(ProcessBlock)) == [earlier, stopped]
        assert not earlier.collapsed and stopped.collapsed
        assert not step.is_attached and step not in stopped.members
        assert stopped.tool_count == 2


async def test_continuing_a_reopened_stopped_turn_shows_its_earlier_notice_while_it_runs() -> None:
    history = _stopped_history(2, interrupted_by="error")
    history.insert(3, history[-1].copy())  # Stop once between the two calls, then again after the second.
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(history)
        await panel.wait_replay_complete()
        earlier = panel.query(InterruptedMessage).first()
        assert panel.query_one(ProcessBlock).collapsed and not earlier.display

        panel.hide_trailing_status_action()
        panel.agent_running = True
        await _read(panel, "c3", "/tmp/c.py")
        await _settled(pilot)
        row = _row(panel)
        assert not panel.query(ProcessBlock)
        assert earlier.display, "a notice is no step of a running row; it shows, as it would while the turn runs"
        assert not any(group.display for group in panel.query(ToolGroup))

        await _finish(panel)
        await _settled(pilot)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed and block.tool_count == 3 and not earlier.display
        assert row not in panel.children


async def test_continuing_straight_to_the_reply_folds_the_stopped_title_the_user_opened() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_interrupted()
        await _settled(pilot)
        before = panel.query_one(ToolGroup)
        open_process_for(before)
        await _settled(pilot)
        assert before.display

        # Continue starts: the stop notice goes as the run starts again, before any output.
        panel.capture_settled_status()
        panel.agent_running = True
        await panel.clear_settled_status()
        await _finish(panel, "Here it is.")
        await _settled(pilot)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed and before in block.members and not before.display
        assert [message.text for message in panel.query(AgentMessage) if message.display] == ["Here it is."]


async def test_continuing_with_a_note_to_a_structured_completion_shows_every_step() -> None:
    """A structured completion shows its turn's steps one by one, the steps from before the stop included."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_interrupted()
        await _settled(pilot)
        assert panel.query(ProcessBlock)

        panel.hide_trailing_status_action()
        panel.agent_running = True
        await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c2", "/tmp/b.py")
        await panel.add_agent_message("", structured_output_completed=True)
        await _settled(pilot)
        assert not panel.query(ProcessBlock)
        assert all(group.display for group in panel.query(ToolGroup))


async def test_a_rejected_prompt_after_a_structured_completion_leaves_its_steps_shown() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_agent_message("", structured_output_completed=True)
        await _settled(pilot)
        await panel.add_error("Blocked by a hook")
        await _settled(pilot)
        assert not panel.query(ProcessBlock)
        assert panel.query_one(ToolGroup).display


async def test_a_rejected_prompt_after_a_successful_turn_leaves_its_title_open() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _finish(panel)
        await _settled(pilot)
        block = panel.query_one(ProcessBlock)
        group = panel.query_one(ToolGroup)
        open_process_for(group)
        await _settled(pilot)
        assert not block.collapsed and group.display

        await panel.add_error("Blocked by a hook")
        await _settled(pilot)
        assert panel.query_one(ProcessBlock) is block
        assert not block.collapsed and group.display


def _stopped_history(calls: int, *, interrupted_by: str = "user") -> list[dict[str, Any]]:
    history = [Message("user", [Content.from_text("prompt")]).to_dict()]
    for index in range(calls):
        history += [
            Message(
                "assistant",
                [
                    Content.from_text(f"Step {index}."),
                    Content.from_function_call(
                        call_id=f"c{index}", name="read_file", arguments="{}", additional_properties=_READ_KIND
                    ),
                ],
            ).to_dict(),
            Message("tool", [Content.from_function_result(call_id=f"c{index}", result="ok")]).to_dict(),
        ]
    history.append(
        Message(
            "assistant",
            [Content.from_text("Stopped")],
            additional_properties={
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                "_interrupted_by": interrupted_by,
            },
        ).to_dict()
    )
    return history


@pytest.mark.parametrize("is_status", [False, True], ids=["earlier-reply", "earlier-status"])
async def test_reopening_a_resumed_process_shows_its_nonfoldable_message_body(is_status: bool) -> None:
    history = _stopped_history(1)
    metadata = (
        {
            HistoryMarkerKind.KEY: HistoryMarkerKind.AWAITING_SUB_AGENTS,
            HistoryMarkerKind.STATUS_CODE_KEY: HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS,
        }
        if is_status
        else {}
    )
    history.insert(
        1, Message("assistant", [Content.from_text("Earlier text.")], additional_properties=metadata).to_dict()
    )
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(history)
        await panel.wait_replay_complete()
        message = next(entry for entry in panel.query(AgentMessage) if not entry.foldable)
        assert message.is_status is is_status
        assert message in panel.query_one(ProcessBlock).members

        panel.capture_settled_status()
        panel.agent_running = True
        await panel.clear_settled_status()
        row = _row(panel)
        body = message.query_one(VirtualizedMarkdown)
        assert message in row.members and not message.display
        for opened in (True, False, True):
            await click_when_settled(pilot, row)
            await _settled(pilot)
            assert row.collapsed is not opened
            assert message.display is opened
            if opened:
                assert body.display


@asynccontextmanager
async def _hold_replay_before_prompt(
    panel: ChatPanel, monkeypatch: pytest.MonkeyPatch, *, calls: int
) -> AsyncIterator[None]:
    """Mount the tail now and the pending history when the test leaves this block."""
    release = asyncio.Event()
    mount = panel.mount_replay_batch

    async def gated(batch: list[Widget], *, before: Widget | None) -> None:
        if before is not None:
            await release.wait()
            # Continue can replace the title this batch was about to mount above.
            before = panel.transcript_entry_after(batch[-1])
        await mount(batch, before=before)

    monkeypatch.setattr(panel, "mount_replay_batch", gated)
    await panel.replay_history(_stopped_history(calls))
    try:
        assert panel.replay_in_progress and not panel.query(UserMessage)
        async with asyncio.timeout(DEFAULT_WAIT_TIMEOUT):
            yield
    finally:
        release.set()
        await panel.wait_replay_complete()


async def test_continuing_clears_the_notice_and_takes_over_the_title_during_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        # At 63 calls the tail includes the stopped title, but its prompt is still pending.
        async with _hold_replay_before_prompt(panel, monkeypatch, calls=63):
            stopped = panel.query_one(ProcessBlock)
            assert stopped.is_attached and stopped.tool_count == 63
            assert panel.query_one(InterruptedMessage).is_attached
            panel.capture_settled_status()
            panel.agent_running = True
            await panel.clear_settled_status()
            assert panel.replay_in_progress and not panel.query(InterruptedMessage)
            assert not panel.query(ProcessBlock)
            row = _row(panel)
            assert row.collapsed and row.tally.total == 63

            await _read(panel, "next", "/tmp/next.py")
            assert not panel.query(UserMessage)
            assert _rows(panel) == [row] and row.tally.total == 64
            assert all(group in row.members and not group.display for group in panel.query(ToolGroup))

        assert panel.query_one(UserMessage).text == "prompt"
        await _finish(panel)
        (block,) = panel.query(ProcessBlock)
        assert not _rows(panel) and block.collapsed and block.tool_count == 64
        assert all(group in block.members and not group.display for group in panel.query(ToolGroup))


async def test_live_row_keeps_its_place_below_the_pending_prompt_and_restore_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_error_notice("Restore warning")
        warning = panel.query_one(ErrorMessage)
        release = asyncio.Event()
        prepend = panel._replay_mount._prepend

        async def hold_before_prepend(gc_pause: ChatGcPauseClaim) -> None:
            await release.wait()
            await prepend(gc_pause)

        monkeypatch.setattr(panel._replay_mount, "_prepend", hold_before_prepend)
        try:
            await panel.replay_history(_stopped_history(63))
            assert panel.replay_in_progress and not panel.query(UserMessage)
            panel.capture_settled_status()
            panel.agent_running = True
            await panel.clear_settled_status()
            row = _row(panel)
            assert row.tally.total == 63
        finally:
            release.set()
            await panel.wait_replay_complete()

        assert panel.transcript_content_children()[:3] == [warning, panel.query_one(UserMessage), row]


@pytest.mark.parametrize("title_mounts_first", [False, True], ids=["tool-first", "title-first"])
@pytest.mark.parametrize("calls", [63, 180], ids=["title-in-tail", "title-pending"])
async def test_second_continue_adopts_steps_left_by_the_first_before_replay_loaded_the_title(
    monkeypatch: pytest.MonkeyPatch, title_mounts_first: bool, calls: int
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)

        async def resume() -> None:
            panel.capture_settled_status()
            panel.agent_running = True
            await panel.clear_settled_status()

        async with _hold_replay_before_prompt(panel, monkeypatch, calls=calls):
            await resume()
            await _read(panel, "first", "/tmp/first.py")
            first = _row(panel).latest
            await panel.add_interrupted()
            panel.agent_running = False
            await resume()
            if not title_mounts_first:
                await _read(panel, "second", "/tmp/second.py")
                await panel.add_answer_delta("latest", "New reply in progress.")
            assert not panel.query(UserMessage)

        assert panel.query_one(UserMessage).text == "prompt"
        if title_mounts_first:
            await _read(panel, "second", "/tmp/second.py")
        row = _row(panel)
        assert panel.transcript_content_children()[0:2] == [panel.query_one(UserMessage), row]
        assert not panel.query(ProcessBlock)
        assert isinstance(first, ToolGroup) and first in row.members and not first.display
        assert row.tally.total == calls + 2 and all(group in row.members for group in panel.query(ToolGroup))
        if not title_mounts_first:
            preview = next(message for message in panel.query(AgentMessage) if message.is_provisional)
            assert preview.display and preview not in row.members


async def test_the_replayed_title_joins_an_answer_that_started_before_it_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        async with _hold_replay_before_prompt(panel, monkeypatch, calls=180):
            panel.capture_settled_status()
            panel.agent_running = True
            await panel.clear_settled_status()
            await panel.add_answer_delta("latest", "New reply in progress.")
            assert not _rows(panel)
            preview = next(message for message in panel.query(AgentMessage) if message.is_provisional)

        row = _row(panel)
        assert row.tally.total == 180 and preview in row.guide_members() and preview.display


@pytest.mark.parametrize("boundary", ["injection", "compaction"])
async def test_a_replayed_title_joins_the_row_sealed_before_it_mounted(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        async with _hold_replay_before_prompt(panel, monkeypatch, calls=180):
            panel.capture_settled_status()
            panel.agent_running = True
            await panel.clear_settled_status()
            await _read(panel, "first", "/tmp/first.py")
            first = _row(panel)
            if boundary == "injection":
                await panel.add_user_message("Also check docs", is_injection=True)
                divider = panel.query_one(UserMessage)
            else:
                await panel.add_compaction_start("compact")
                panel.complete_compaction("compact", outcome="ok", duration_ms=1000)
                divider = panel.query_one(CompactionCard)
            await _read(panel, "second", "/tmp/second.py")

        settled, active = _rows(panel)
        assert settled is first and settled.settled and not active.settled
        assert (settled.tally.total, active.tally.total) == (181, 1)
        assert not panel.query(ProcessBlock)
        entries = panel.current_turn_children()
        assert entries.index(settled) < entries.index(divider) < entries.index(active)
        assert all(group in settled.members or group in active.members for group in panel.query(ToolGroup))


@pytest.mark.parametrize("open_process", [False, True], ids=["default", "fold-command"])
async def test_interrupt_before_the_prompt_mounts_folds_the_continued_turn(
    monkeypatch: pytest.MonkeyPatch, open_process: bool
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        async with _hold_replay_before_prompt(panel, monkeypatch, calls=180):
            panel.hide_trailing_status_action()
            panel.agent_running = True
            await _read(panel, "next", "/tmp/next.py")
            await panel.add_interrupted()
            panel.agent_running = False
            assert panel.replay_in_progress and not panel.query(UserMessage) and not _rows(panel)
            if open_process:
                assert panel.toggle_fold_all() is False

        assert panel.query_one(UserMessage).text == "prompt"
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed is not open_process and block.tool_count == 181
        groups = list(panel.query(ToolGroup))
        assert len(groups) == 181 and all(group in block.members and group.display is open_process for group in groups)
        notice = panel.query_one(InterruptedMessage)
        assert notice.display and notice not in block.members


async def test_continue_waits_for_the_replayed_stop_title_before_taking_it_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 4
    # Keep the prompt and original process title in the prepend without
    # mounting a large tool history to reach the held title.
    monkeypatch.setattr(replay_mount_module, "REPLAY_FOREGROUND_ENTRY_LIMIT", 4)
    mounting_title = asyncio.Event()
    release_title = asyncio.Event()
    continuing = asyncio.Event()

    class HeldProcessBlock(ProcessBlock):
        async def on_mount(self) -> None:
            if self.tool_count == calls + 1:
                mounting_title.set()
                await release_title.wait()

    monkeypatch.setattr(process_controller_module, "ProcessBlock", HeldProcessBlock)
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)

        async def stop_before_prompt() -> None:
            async with _hold_replay_before_prompt(panel, monkeypatch, calls=calls):
                assert not panel.query(ProcessBlock)
                panel.capture_settled_status()
                panel.agent_running = True
                await panel.clear_settled_status()
                await _read(panel, "first", "/tmp/first.py")
                await panel.add_interrupted()
                panel.agent_running = False

        async def continue_again() -> None:
            panel.capture_settled_status()
            panel.agent_running = True
            continuing.set()
            await panel.clear_settled_status()
            await _read(panel, "second", "/tmp/second.py")
            await _finish(panel)
            panel.agent_running = False

        async with asyncio.TaskGroup() as tasks:
            try:
                first = tasks.create_task(stop_before_prompt())
                await wait_for(
                    lambda: mounting_title.is_set() or first.done(), description="the replayed title starts mounting"
                )
                if first.done():
                    await first
                assert mounting_title.is_set()
                notice = panel.query_one(InterruptedMessage)
                second = tasks.create_task(continue_again())
                await wait_for(continuing.is_set, description="the second Continue starts")
                assert not second.done() and notice.is_attached and not notice._pruning
            finally:
                release_title.set()

        (block,) = panel.query(ProcessBlock)
        assert block.tool_count == calls + 2 and block.collapsed
        groups = list(panel.query(ToolGroup))
        assert len(groups) == calls + 2 and all(group in block.members and not group.display for group in groups)


async def test_continuing_keeps_a_step_the_user_opened_under_the_folded_title_open() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_interrupted()
        await _settled(pilot)
        before = panel.query_one(ToolGroup)
        open_process_for(before)
        before.collapsed = False
        (stopped,) = panel.query(ProcessBlock)
        stopped.collapsed = True
        await _settled(pilot)

        panel.hide_trailing_status_action()
        panel.agent_running = True
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        row = _row(panel)
        assert row.collapsed and not before.display
        assert not before.collapsed


@pytest.mark.parametrize("ending", ["error", "interrupted"])
async def test_a_new_stop_notice_replacing_the_old_one_leaves_an_opened_title_open(ending: str) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_interrupted()
        await _settled(pilot)
        (stopped,) = panel.query(ProcessBlock)
        open_process_for(panel.query_one(ToolGroup))
        await _settled(pilot)
        if ending == "error":
            await panel.add_error("failed")
        else:
            await panel.add_interrupted()
        await _settled(pilot)
        assert panel.query_one(ProcessBlock) is stopped and not stopped.collapsed


@pytest.mark.parametrize("loaded", [False, True], ids=["prompt-loads-after-continue", "prompt-loads-before-continue"])
async def test_empty_reply_after_another_continue_drops_the_old_stop(
    monkeypatch: pytest.MonkeyPatch, loaded: bool
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)

        async def resume() -> None:
            panel.capture_settled_status()
            panel.agent_running = True
            await panel.clear_settled_status()

        async with _hold_replay_before_prompt(panel, monkeypatch, calls=180):
            await resume()
            await _read(panel, "next", "/tmp/next.py")
            await panel.add_interrupted()
            panel.agent_running = False
            if not loaded:
                await resume()

        if loaded:
            assert panel.query_one(ProcessBlock).tool_count == 181
            await resume()
        await panel.add_agent_message("")
        panel.agent_running = False
        assert not panel.query(ProcessBlock)
        assert all(group.display for group in panel.query(ToolGroup))
