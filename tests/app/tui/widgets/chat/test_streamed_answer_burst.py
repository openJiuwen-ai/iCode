# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A streamed answer burst costs one parse and a bounded number of layouts.

A synchronous burst of answer deltas, such as a buffered answer whose lines
arrive in one loop step, must not cost per delta: each delta queuing a full
markdown parse plus a transcript relayout would make an N-line answer cost
O(N^2) parsing after it had already arrived.
"""

from __future__ import annotations

from types import ModuleType

import pytest
from textual.await_complete import AwaitComplete
from textual.geometry import Size
from textual.timer import Timer, TimerCallback

from chrys.app.tui.widgets.chat import messages as messages_module
from chrys.app.tui.widgets.chat.messages import _STREAMED_BODY_INTERVAL_SECONDS, AgentMessage, MessageCopyButton
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown.blocks import MarkdownBlock
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.kernel import Content, Message
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, WidgetApp
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, wait_for, wait_until, with_wait_deadline

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

_ANSWER_LINES = 200
_SCREEN_LAYOUT_BOUND = 12
"""Screen layouts a burst may cost: one install plus cursor, copy button and scroll settling."""


def _answer(lines: int) -> str:
    parts = ["<think>outline the answer</think># Streaming pipeline report\n", "\n"]
    for index in range(lines - 2):
        if index % 20 == 0:
            parts.append(f"## Section {index // 20}\n")
        elif index % 20 == 10:
            parts.append(f"- item {index} with `inline_code_{index}()` and **bold** text\n")
        else:
            parts.append(f"Line {index} of the answer explains one more step of the pipeline.\n")
    return "".join(parts)


def _transcript(turns: int) -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for index in range(turns):
        history.append(Message("user", [Content.from_text(f"Question {index}\nwith a second line")]).to_dict())
        history.append(
            Message(
                "assistant",
                [Content.from_text(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**.\n\n- a\n- b")],
            ).to_dict()
        )
    return history


async def test_a_streamed_burst_hands_the_body_its_text_once(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(_ANSWER_LINES)
    lines = answer.splitlines(keepends=True)
    message = AgentMessage(lines[0], is_final=False)

    async with WidgetApp(lambda: message).run_test() as pilot:
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(lambda: body.source and body._blocks, pilot=pilot, description="first line parsed")
        await pilot.pause()

        handed: list[str] = []
        real_update = body.update

        def recording_update(markdown: str) -> AwaitComplete:
            handed.append(markdown)
            return real_update(markdown)

        monkeypatch.setattr(body, "update", recording_update)

        for line in lines[1:]:
            message.append_text(line)

        assert handed == []
        # Readers never see a stale answer, even before the body catches up;
        # think tags are answer text, kept as written.
        assert message.text == answer.strip()
        assert message.format_agent_response_copy() == answer.strip()

        await wait_for(lambda: handed, pilot=pilot, description="body synced once per loop turn")
        assert handed == [answer.strip()]

        message.append_text("Done.\n")
        final = message.text
        message.stream_update(final, is_final=True)

        # The final update reaches the body before the cursor and copy button change.
        assert handed == [answer.strip(), final]
        await wait_for(
            lambda: not message.has_class("--streaming") and message.query_one(MessageCopyButton).display,
            pilot=pilot,
            description="streaming ended and copy button shown",
        )
        await wait_for(
            lambda: body.source == final and not body.lock.is_locked,
            pilot=pilot,
            description="final parse installed",
        )


async def test_streamed_text_reaches_the_body_at_most_once_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each body update parses the whole answer and may lay out the transcript, so a steady stream is paced."""
    now = [0.0]
    clock = ModuleType("time")
    clock.monotonic = lambda: now[0]  # type: ignore[attr-defined]
    monkeypatch.setattr(messages_module, "time", clock)
    message = AgentMessage("first", is_final=False)

    async with WidgetApp(lambda: message).run_test() as pilot:
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(lambda: body.source and body._blocks, pilot=pilot, description="first text parsed")

        handed: list[str] = []
        real_update = body.update

        def recording_update(markdown: str) -> AwaitComplete:
            handed.append(markdown)
            return real_update(markdown)

        # The test fires each paced sync itself, so nothing but the timer can deliver it.
        timers: list[tuple[float, TimerCallback]] = []

        def recording_set_timer(
            delay: float, callback: TimerCallback | None = None, *, name: str | None = None, pause: bool = False
        ) -> Timer:
            assert callback is not None
            timers.append((delay, callback))
            return Timer(message, delay, name=name, callback=callback, pause=pause)

        monkeypatch.setattr(body, "update", recording_update)
        monkeypatch.setattr(message, "set_timer", recording_set_timer)

        message.append_text(" second")
        await wait_for(lambda: handed, pilot=pilot, description="first streamed text handed over")
        assert handed == ["first second"]
        assert timers == []

        # Within the interval of that update: the newest text waits for the timer.
        message.append_text(" third")
        message.append_text(" fourth")
        assert not await wait_until(lambda: len(handed) > 1, pilot=pilot, timeout=0.3)
        assert [delay for delay, _ in timers] == [_STREAMED_BODY_INTERVAL_SECONDS]
        timers[0][1]()
        assert handed == ["first second", "first second third fourth"]

        now[0] += 1
        message.append_text(" fifth")
        await wait_for(lambda: len(handed) == 3, pilot=pilot, description="update after the interval handed over")
        assert handed[-1] == "first second third fourth fifth"
        assert len(timers) == 1

        # A final update while a paced sync is pending hands over its text at once; the timer then finds nothing new.
        message.append_text(" sixth")
        final = "first second third fourth fifth sixth seventh"
        message.stream_update(final, is_final=True)
        assert len(timers) == 2
        assert handed[-1] == final
        timers[1][1]()
        await pilot.pause()
        assert handed[-1] == final
        assert len(handed) == 4


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_long_answer_burst_into_a_populated_transcript_parses_once_with_bounded_layouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = _answer(_ANSWER_LINES)
    lines = answer.splitlines(keepends=True)
    final_body = answer.strip()

    async with ChatPanelApp().run_test(size=(120, 36)) as pilot:
        app = pilot.app
        screen = app.screen
        panel = app.query_one(ChatPanel)
        await panel.replay_history(_transcript(24))
        await panel.add_user_message("Explain the streaming pipeline in depth.")
        await wait_for(
            lambda: (
                len(panel.query(AgentMessage)) == 24
                and all(markdown._blocks for markdown in panel.query(VirtualizedMarkdown))
            ),
            pilot=pilot,
            description="populated transcript parsed",
        )

        # The first event mounts the streamed message; its body parses the first line on mount.
        await panel.add_answer_delta("answer", lines[0])
        message = panel.query(AgentMessage).last()
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(lambda: body._blocks, pilot=pilot, description="streamed message mounted")
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot, description="settled before burst")

        parsed: list[str] = []
        real_build = VirtualizedMarkdown._build_blocks

        def recording_build(markdown_widget: VirtualizedMarkdown, markdown: str) -> list[MarkdownBlock]:
            if markdown_widget is body:
                parsed.append(markdown)
            return real_build(markdown_widget, markdown)

        layouts: list[None] = []
        real_refresh_layout = screen._refresh_layout

        def recording_refresh_layout(size: Size | None = None, scroll: bool = False) -> None:
            layouts.append(None)
            real_refresh_layout(size, scroll)

        monkeypatch.setattr(VirtualizedMarkdown, "_build_blocks", recording_build)
        monkeypatch.setattr(screen, "_refresh_layout", recording_refresh_layout)

        # MainScreen awaits each event's handler inline, so the whole burst runs in one loop step.
        for line in lines[1:]:
            await panel.add_answer_delta("answer", line)
        panel.finish_answer_segment("answer", completed=True)
        await panel.add_agent_message(answer)

        await wait_for(
            # The lock is held from the parse of the final text until its blocks are installed.
            lambda: parsed and parsed[-1] == final_body and not body.lock.is_locked,
            pilot=pilot,
            description="final answer installed",
        )
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot, description="settled after burst")

        assert parsed == [final_body]
        assert body.source == final_body
        assert message.text == final_body
        assert len(layouts) <= _SCREEN_LAYOUT_BOUND, f"{len(layouts)} screen layouts for one streamed answer"


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_each_row_a_streamed_answer_adds_lays_the_transcript_out_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new row moves the whole transcript, so its layout costs in proportion to the transcript: one is enough."""
    async with ChatPanelApp().run_test(size=(120, 36)) as pilot:
        app = pilot.app
        screen = app.screen
        panel = app.query_one(ChatPanel)
        await panel.replay_history(_transcript(24))
        await panel.add_user_message("Explain the streaming pipeline in depth.")
        await panel.add_answer_delta("answer", _answer(60))
        message = panel.query(AgentMessage).last()
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: body._blocks and screen_is_settled(app, screen), pilot=pilot, description="settled before growth"
        )

        full_layouts: list[None] = []
        real_refresh_layout = screen._refresh_layout

        def recording_refresh_layout(size: Size | None = None, scroll: bool = False) -> None:
            if not scroll:
                full_layouts.append(None)
            real_refresh_layout(size, scroll)

        monkeypatch.setattr(screen, "_refresh_layout", recording_refresh_layout)

        for row in range(3):
            height = message.size.height
            await panel.add_answer_delta("answer", f"\nRow {row}: a paragraph of its own.\n")
            await wait_for(
                lambda height=height: message.size.height > height and screen_is_settled(app, screen),
                pilot=pilot,
                description=f"row {row} laid out",
            )

        assert len(full_layouts) == 3
