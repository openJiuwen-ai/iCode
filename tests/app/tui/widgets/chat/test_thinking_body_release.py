# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle and layout of lazily mounted Markdown bodies in thinking and intermediate text."""

from __future__ import annotations

import asyncio
import threading

import pytest
from textual.screen import Screen

from chrys.app.tui.support import gc_freeze
from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcAbsorbRequested, GcReclaimRequested
from chrys.app.tui.widgets.chat import messages as messages_module
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage, _AgentHeader
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import GcMessageChatPanelApp, click_when_settled, open_process_for
from tests.support.waiting import wait_for


@pytest.mark.parametrize("frozen", [False, True], ids=["young", "frozen"])
async def test_fold_releases_body_with_epoch_aware_reclaim(monkeypatch: pytest.MonkeyPatch, frozen: bool) -> None:
    app = GcMessageChatPanelApp(thinking_expanded=lambda: True)
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r1", "reasoning", "# Evidence\n\nLook at the file.")
        panel.finish_thinking_segment("r1")
        message = panel.query_one(ThinkingMessage)
        open_process_for(message)
        message.collapsed = False
        await wait_for(
            lambda: (
                bool(message.query(VirtualizedMarkdown))
                and message.query_one(VirtualizedMarkdown).shown_source == message.text
            ),
            pilot=pilot,
            description="thinking parse ready",
        )
        body = message.query_one(VirtualizedMarkdown)
        epoch = gc_freeze.current_gc_freeze_epoch()
        if frozen:
            monkeypatch.setattr(gc_freeze, "_freeze_epoch", epoch + 1)
        message.collapsed = True
        await wait_for(
            lambda: any(isinstance(event, GcReclaimRequested) for event in app.gc_messages),
            pilot=pilot,
            description="thinking reclaim request",
        )
        reclaim = next(event for event in app.gc_messages if isinstance(event, GcReclaimRequested))
        assert reclaim.prompt is False
        assert not message.query(VirtualizedMarkdown)
        assert not body.is_attached
        if frozen:
            assert reclaim.removed is None
        else:
            assert reclaim.removed is not None
            assert reclaim.removed.built_at_epoch == epoch
            assert body in [node() for node in reclaim.removed.nodes]
        message.collapsed = False
        await wait_for(lambda: bool(message.query(VirtualizedMarkdown)), pilot=pilot, description="thinking rebuilt")
        assert message.query_one(VirtualizedMarkdown) is not body
        await wait_for(
            lambda: message.query_one(VirtualizedMarkdown).shown_source == message.text,
            pilot=pilot,
            description="rebuilt thinking text ready",
        )


async def test_the_turn_ending_folds_its_thinking_and_fast_toggles_keep_one_body() -> None:
    async with GcMessageChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r", "reasoning", "Reasoning.")
        panel.finish_thinking_segment("r")
        await panel.add_agent_message("Done.")
        message = panel.query_one(ThinkingMessage)
        # The turn went on past it: it folded.
        assert message.collapsed
        message.collapsed = False
        message.collapsed = True
        message.collapsed = False
        message.collapsed = True
        message.collapsed = False
        await wait_for(
            lambda: len(message.query(VirtualizedMarkdown)) == 1,
            pilot=pilot,
            description="one final body after rapid toggles",
        )
        assert not message.collapsed


async def test_live_intermediate_text_only_builds_its_body_when_opened() -> None:
    app = GcMessageChatPanelApp(thinking_expanded=lambda: True)
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_answer_delta("a1", "Let me look at the file.")
        panel.finish_answer_segment("a1", completed=True)
        message = panel.query_one(AgentMessage)
        assert message.is_provisional and not message.query(VirtualizedMarkdown)

        await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "x.py"})
        assert (message.foldable, message.collapsed) == (True, True)
        await panel.add_tool_result("c1", "read_file", "ok", 3)
        await panel.add_answer_delta("a2", "More text.")
        assert not message.query(VirtualizedMarkdown)

        open_process_for(message)
        message.collapsed = False
        await wait_for(
            lambda: any(
                body.display and body.shown_source == message.text for body in message.query(VirtualizedMarkdown)
            ),
            pilot=pilot,
            description="opening the intermediate row builds its complete Markdown body",
        )


async def _mount_streamed_markdown(panel: ChatPanel, text: str) -> AgentMessage:
    """Set up an already-streaming Markdown body, as used by nested transcripts."""
    message = AgentMessage(text, is_final=False, segment_ids=("a1",))
    await panel.mount_transcript_widget(message)
    panel._current_agent_msg = message
    return message


@pytest.mark.parametrize("parse", ["installs", "fails"])
async def test_text_opened_as_its_folded_body_parses_its_final_text_waits_for_that_parse_not_a_second_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, parse: str
) -> None:
    """Handed the final text as it streamed, the body parses it while the tool call after it folds it: opened then, it
    shows that parse's blocks, which the GC absorb goes to, or the text it had if that parse fails, never the same text
    parsed again."""
    # The body is handed each part of the text as it streams.
    monkeypatch.setattr(messages_module, "_STREAMED_BODY_INTERVAL_SECONDS", 0.0)
    final = "Let me look.\n\nAt the file."
    parsed: list[str] = []
    release = threading.Event()
    build_blocks = VirtualizedMarkdown._build_blocks

    def held_parse(body: VirtualizedMarkdown, markdown: str) -> list[object]:
        parsed.append(markdown)
        if markdown == final:
            release.wait(10.0)
            if parse == "fails":
                raise ValueError(markdown)
        return build_blocks(body, markdown)

    app = GcMessageChatPanelApp()
    async with app.run_test(size=(80, 30)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        panel.agent_running = True
        message = await _mount_streamed_markdown(panel, "Let me look.")
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(lambda: body.shown_source == message.text, pilot=pilot, description="the streamed text shows")
        monkeypatch.setattr(VirtualizedMarkdown, "_build_blocks", held_parse)
        try:
            await panel.add_answer_delta("a1", "\n\nAt the file.")
            await wait_for(lambda: final in parsed, pilot=pilot, description="the body parses the final text")
            panel.finish_answer_segment("a1", completed=True)
            await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "x.py"})
            assert (message.collapsed, body.display, body.source) == (True, False, final)
            assert body.shown_source != final
            absorbs_before = len([event for event in app.gc_messages if isinstance(event, GcAbsorbRequested)])

            message.collapsed = False
            # Not through the pilot: its pauses would wait for the parse it holds.
            await wait_for(lambda: message._absorb_after_parse, description="the opening waits for the body's parse")
            assert parsed.count(final) == 1
        finally:
            release.set()
        shown = final if parse == "installs" else "Let me look."
        await wait_for(
            lambda: body.display and body.shown_source == shown and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="opened, it shows its body",
        )
        absorbs = [event for event in app.gc_messages if isinstance(event, GcAbsorbRequested)][absorbs_before:]
        assert parsed.count(final) == 1
        if parse == "installs":
            assert [event.reason for event in absorbs] == [GcAbsorbReason.STABLE_CONTENT_MOUNTED]
        else:
            # It shows the text it had, which is no final text to absorb.
            assert absorbs == []
            assert "Parsing an opened message's text failed" in caplog.text

            # Folded and opened again, it parses the text anew, as it did before it waited for the parse it had; this
            # time the parse installs it.
            parse = "installs"
            message.collapsed = True
            await wait_for(lambda: not body.display, pilot=pilot, description="folded again, it hides its body")
            message.collapsed = False
            await wait_for(
                lambda: body.display and body.shown_source == final and screen_is_settled(app, panel.screen),
                pilot=pilot,
                description="opened again, it shows the text parsed anew",
            )
            assert parsed.count(final) == 2


async def _text_folded_before_its_body_shows_its_final_text(
    panel: ChatPanel, pilot: object, monkeypatch: pytest.MonkeyPatch, *, shown: str = "First part.\n\nSecond part."
) -> tuple[AgentMessage, VirtualizedMarkdown]:
    """Text between tool calls whose body shows only what streamed of it as *shown*: the tool call after it folded
    it before its last parts reached the body."""
    # The body catches up with the stream no sooner than an hour after it last did.
    monkeypatch.setattr(messages_module, "_STREAMED_BODY_INTERVAL_SECONDS", 3600.0)
    await panel.add_user_message("prompt")
    panel.agent_running = True
    first, rest = shown.split("\n\n", 1)
    message = await _mount_streamed_markdown(panel, first)
    body = message.query_one(VirtualizedMarkdown)
    # The body is built with the text it starts from, and handed the next parts at once.
    await panel.add_answer_delta("a1", f"\n\n{rest}")
    await wait_for(lambda: body.shown_source == shown, pilot=pilot, description="the streamed text shows")
    await panel.add_answer_delta("a1", "\n\nThird part.\n\nFourth part.")
    panel.finish_answer_segment("a1", completed=True)
    await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "x.py"})
    open_process_for(message)
    assert (message.collapsed, body.display, body.shown_source) == (True, False, shown)
    return message, body


async def test_text_between_tool_calls_opened_shows_no_body_until_it_shows_the_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GcMessageChatPanelApp()
    async with app.run_test(size=(80, 30)) as pilot:
        panel = app.query_one(ChatPanel)
        message, body = await _text_folded_before_its_body_shows_its_final_text(panel, pilot, monkeypatch)
        painted: list[str | None] = []
        paint = Screen._compositor_refresh

        def record(screen: Screen) -> None:
            painted.append(body.shown_source if body in screen._compositor.visible_widgets else None)
            paint(screen)

        def absorbs() -> list[GcAbsorbReason]:
            return [message.reason for message in app.gc_messages if isinstance(message, GcAbsorbRequested)]

        absorbs_before = len(absorbs())
        monkeypatch.setattr(Screen, "_compositor_refresh", record)
        await click_when_settled(pilot, message.query_one(_AgentHeader))
        await wait_for(
            lambda: painted[-1:] == [message.text], pilot=pilot, description="opened, the body shows the final text"
        )
        # Shown at once, the body would paint the text it had for a frame, and the final text's rows only after it.
        assert set(painted) == {None, message.text}
        # The kept body's parse of it is stable content, as a body's built on opening is.
        await wait_for(
            lambda: absorbs()[absorbs_before:],
            pilot=pilot,
            description="the opened body's parse is handed to the GC freeze",
        )
        assert absorbs()[absorbs_before:] == [GcAbsorbReason.STABLE_CONTENT_MOUNTED]


async def test_text_between_tool_calls_whose_final_text_fails_to_parse_shows_the_text_it_had_once_opened(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with GcMessageChatPanelApp().run_test(size=(80, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        message, body = await _text_folded_before_its_body_shows_its_final_text(panel, pilot, monkeypatch)

        def failing_parse(markdown: str) -> list[object]:
            raise ValueError(markdown)

        monkeypatch.setattr(body, "_build_blocks", failing_parse)
        message.collapsed = False
        await wait_for(lambda: body.display, pilot=pilot, description="opened, the body shows")
        assert (message.collapsed, body.shown_source) == (False, "First part.\n\nSecond part.")
        assert "Parsing an opened message's text failed" in caplog.text


async def test_thinking_opened_as_it_streams_shows_its_final_text_once_that_parses_not_the_text_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its final text comes as the opening parse runs: the body shows once that text parses, not the opened one."""
    opened = "First part."
    final = "First part. And then\n\nanother paragraph\n\nand a third one."
    gates = {opened: threading.Event(), final: threading.Event()}
    build_blocks = VirtualizedMarkdown._build_blocks

    def gated_parse(body: VirtualizedMarkdown, markdown: str) -> list[object]:
        if gate := gates.get(markdown):
            gate.wait(10.0)
        return build_blocks(body, markdown)

    monkeypatch.setattr(VirtualizedMarkdown, "_build_blocks", gated_parse)
    async with GcMessageChatPanelApp(thinking_expanded=lambda: False).run_test(size=(80, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        panel.agent_running = True
        await panel.add_thinking_delta("r1", "reasoning", opened)
        message = panel.query_one(ThinkingMessage)
        open_process_for(message)
        await wait_for(lambda: screen_is_settled(pilot.app, panel.screen), pilot=pilot, description="it streams")
        assert message.collapsed
        painted: list[str | None] = []
        paint = Screen._compositor_refresh

        def record(screen: Screen) -> None:
            body = message._md_widget
            painted.append(body.shown_source if body in screen._compositor.visible_widgets else None)
            paint(screen)

        monkeypatch.setattr(Screen, "_compositor_refresh", record)
        message.collapsed = False
        await wait_for(lambda: message._md_widget is not None, pilot=pilot, description="opened, it builds a body")
        body = message._md_widget
        await panel.add_thinking_delta("r1", "reasoning", final.removeprefix(opened))
        panel.finish_thinking_segment("r1")
        gates[opened].set()
        await wait_for(lambda: body.shown_source == opened, pilot=pilot, description="the opened text parses")
        await wait_for(lambda: screen_is_settled(pilot.app, panel.screen), pilot=pilot, description="and lays out")
        # Shown now, it would take the opened text's rows, and the final text's once that parses.
        assert not body.display
        gates[final].set()
        await wait_for(lambda: painted[-1:] == [final], pilot=pilot, description="the body shows the final text")
        assert set(painted) == {None, final}


@pytest.mark.parametrize("entry", ["text between tool calls", "thinking"])
async def test_an_entry_opened_while_its_text_parses_clears_before_the_parse_ends(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Opening waits for the body's parse beside the message, not on its pump: the messages it handles go on."""
    async with GcMessageChatPanelApp(thinking_expanded=lambda: False).run_test(size=(80, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        message: AgentMessage | ThinkingMessage
        if entry == "thinking":
            await panel.add_user_message("prompt")
            await panel.add_thinking_delta("r1", "reasoning", "Reasoning.")
            panel.finish_thinking_segment("r1")
            message = panel.query_one(ThinkingMessage)
        else:
            message, _body = await _text_folded_before_its_body_shows_its_final_text(panel, pilot, monkeypatch)
        assert message.collapsed
        started = threading.Event()
        release = threading.Event()
        build_blocks = VirtualizedMarkdown._build_blocks

        def held_parse(body: VirtualizedMarkdown, markdown: str) -> list[object]:
            if markdown == message.text:
                started.set()
                release.wait(10.0)
            return build_blocks(body, markdown)

        monkeypatch.setattr(VirtualizedMarkdown, "_build_blocks", held_parse)
        message.collapsed = False
        # Not through the pilot: its pauses wait for the very messages a blocked pump would hold up.
        await wait_for(started.is_set, description="the opened body's parse started")
        clearing = asyncio.create_task(panel.clear())
        try:
            await wait_for(clearing.done, description="the transcript cleared as the parse runs")
        finally:
            release.set()
            await clearing
        assert not message.is_attached


@pytest.mark.parametrize("turn", ["running", "ended"])
async def test_text_between_tool_calls_opened_by_hand_moves_the_view_once_as_its_body_shows(
    monkeypatch: pytest.MonkeyPatch, turn: str
) -> None:
    """Until its body shows the final text, the opened text keeps the gap under it: the view would move up as it went,
    and down again as the body showed."""
    async with GcMessageChatPanelApp().run_test(size=(80, 24)) as pilot:
        app = pilot.app
        panel = app.query_one(ChatPanel)
        for index in range(6):
            await panel.add_user_message(f"message {index}")
            await panel.add_agent_message("\n".join(f"line {line}" for line in range(8)))
        shown = "\n\n".join(f"Part {index}." for index in range(6))
        message, body = await _text_folded_before_its_body_shows_its_final_text(panel, pilot, monkeypatch, shown=shown)
        if turn == "ended":
            await panel.add_tool_result("c1", "read_file", "ok", 3)
            panel.agent_running = False
        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the view follows the bottom",
        )
        header = message.query_one(_AgentHeader)
        before = (header.region.y, panel.virtual_size.height)
        release = threading.Event()
        build_blocks = body._build_blocks

        def held_parse(markdown: str) -> list[object]:
            release.wait(10.0)
            return build_blocks(markdown)

        monkeypatch.setattr(body, "_build_blocks", held_parse)
        painted: list[tuple[int | None, int, str | None]] = []
        paint = Screen._compositor_refresh

        def record(screen: Screen) -> None:
            visible = screen._compositor.visible_widgets
            row = visible.get(header)
            shows = body.shown_source if body in visible else None
            painted.append((None if row is None else row[0].y, panel.virtual_size.height, shows))
            paint(screen)

        monkeypatch.setattr(Screen, "_compositor_refresh", record)
        try:
            await click_when_settled(pilot, header)
            if turn == "running":
                # What the turn lands while the text parses lays it out.
                await panel.add_tool_result("c1", "read_file", "ok", 3)
            else:
                # Whatever else in the transcript lays it out while the text parses.
                panel.mounted_agent_messages()[0].refresh(layout=True)
            await wait_for(
                lambda: not message.collapsed and screen_is_settled(app, panel.screen),
                pilot=pilot,
                description="a layout painted as the text parses",
            )
            assert painted and not body.display
        finally:
            release.set()
        await wait_for(
            lambda: painted[-1][2] == message.text and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the body shows the final text",
        )
        assert {(row, height) for row, height, shows in painted if shows is None} == {before}, painted
        assert {shows for _row, _height, shows in painted if shows is not None} == {message.text}, painted
