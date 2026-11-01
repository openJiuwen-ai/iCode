# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process folding through real completion events on a populated MainScreen."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationContentDelta, InvocationContentFinished, InvocationMessage
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled, rich_plain
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

# Enough turns to fill several screens. Every turn lays the whole transcript out again, so each one costs more than
# the one before it.
_TURNS = 24


async def test_completed_turns_fold_without_global_restyle_or_recompose(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        for turn in range(_TURNS):
            await panel.add_user_message(f"Question {turn}")
            main._set_agent_running(True)
            origin = InvocationOrigin("turn", "s1", f"turn-{turn}", None)
            await bus.publish(
                InvocationContentDelta(
                    origin=origin, channel="answer", segment_id=f"middle-{turn}", text="Checking the evidence."
                ),
                raise_handler_errors=True,
            )
            await bus.publish(
                InvocationContentFinished(origin=origin, channel="answer", segment_id=f"middle-{turn}"),
                raise_handler_errors=True,
            )
            await panel.add_tool_start(f"c-{turn}", "read_file", "filesystem.read")
            await panel.add_tool_result(f"c-{turn}", "read_file", "ok")
            await bus.publish(
                InvocationContentDelta(
                    origin=origin, channel="answer", segment_id=f"final-{turn}", text=f"Answer {turn}."
                ),
                raise_handler_errors=True,
            )
            await bus.publish(
                InvocationContentFinished(origin=origin, channel="answer", segment_id=f"final-{turn}"),
                raise_handler_errors=True,
            )
            await bus.publish(InvocationMessage(origin=origin, text=f"Answer {turn}."), raise_handler_errors=True)
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="completed turns settled")
        blocks = list(panel.query(ProcessBlock))
        assert len(blocks) == _TURNS
        assert all(block.collapsed for block in blocks)
        assert {rich_plain(block.render()) for block in blocks} == {"▸ 1 tool call: 1 read"}
        assert all(block not in main.focus_chain for block in blocks)
        input_bar = main.query_one(InputBar)
        input_bar.focus_input()
        composer = input_bar.query_one("#chat-input")
        await wait_for(
            lambda: composer.has_focus and screen_is_settled(app, main), pilot=pilot, description="composer focused"
        )
        block = blocks[-1]
        with (
            patch.object(main, "update_node_styles", wraps=main.update_node_styles) as styles,
            patch.object(main, "recompose", wraps=main.recompose) as recomposes,
            patch.object(main, "_refresh_layout", wraps=main._refresh_layout) as layouts,
        ):
            await click_when_settled(pilot, block)
            assert not block.collapsed
            assert composer.has_focus
            await click_when_settled(pilot, block)
            assert block.collapsed
            assert composer.has_focus
            await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="process toggle settled")
            assert not styles.called
            assert not recomposes.called
            assert layouts.call_count < 12
            before = layouts.call_count
            block.try_finalize_fold()
            await pilot.pause()
            assert layouts.call_count == before
        await pilot.press("x")
        assert input_bar.value == "x"
        input_bar.value = ""
        await pilot.press("/", "f", "o", "l", "d", "enter")
        await wait_for(
            lambda: all(not item.collapsed for item in blocks), pilot=pilot, description="keyboard opens activity"
        )
        assert composer.has_focus
        assert all(member.display for item in blocks for member in item.members)
        await pilot.press("/", "f", "o", "l", "d", "enter")
        await wait_for(
            lambda: all(item.collapsed for item in blocks), pilot=pilot, description="keyboard closes activity"
        )
        assert composer.has_focus
