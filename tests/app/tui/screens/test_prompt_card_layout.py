# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec test: the prompt-shortcut card shows, changes and hides over a populated transcript without laying it out.

A real app on a real engine, its transcript populated, every card change made the
way the user makes it: expanding a shortcut, editing its commands, stepping into
prompt history and back, and dropping the switch with the card's ✕ button.
Laying the main screen out or restyling it costs as much as the transcript is
long, so none of these may do either (``.plan/prompt-shortcuts-engine.md`` item
24; the pattern is ``test_editor_dialog.py``'s populated overlay test).
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button, Static

from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.messages import UserMessage as UserBubble
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.prompt_card import PromptCard
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events import types as ev
from tests.app.tui.screens.test_prompt_shortcut_send import (
    BETA,
    FAST_MODEL,
    expand_shortcut,
    profile_tag,
    running_session,
    write_shortcut,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot
    from textual.screen import Screen

    from tests.support.engines import AgentEngineFactory

_EARLIER_PROMPT = "an earlier prompt"


@pytest.fixture(autouse=True)
def _no_branch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """No file-system observer behind the transcript: what lays the screen out is the test's alone."""
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


def _body(card: PromptCard) -> str:
    return str(card.query_one("#prompt-card-body", Static).content)


def _shows_actions(card: PromptCard) -> bool:
    """The ✕ button and the dim note: on screen, and taking a row/cells there."""
    button = card.query_one("#prompt-card-remove", Button)
    note = card.query_one("#prompt-card-note", Static)
    return button.visible and note.visible and note.region.height == 1 and button.region.width > 0


async def _settled(pilot: Pilot, screen: Screen[object], predicate: Callable[[], bool], description: str) -> None:
    """*predicate* holds on four pumps running, with nothing left that would still reach the screen's layout."""
    observed = 0

    def holds() -> bool:
        nonlocal observed
        observed = observed + 1 if predicate() and screen_is_settled(pilot.app, screen) else 0
        return observed >= 4

    await wait_for(holds, pilot=pilot, description=description)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_card_changes_over_a_populated_transcript_never_lay_out_or_restyle_the_screen(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzcard", f"---\nagent: {BETA}\nmodel: {FAST_MODEL}\n---\nReview !`echo a`")
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(120, 36)) as session:
        main, pilot, chat = session.main, session.pilot, session.chat
        panel = main.query_one(ChatPanel)
        transcript = []
        for index in range(24):
            transcript.extend(
                (
                    UserBubble(f"Question {index}\nwith a second line"),
                    AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
                )
            )
        await panel.mount(*transcript)
        await wait_for(
            lambda: len(panel.query(UserBubble)) >= 24 and all(md.source for md in panel.query(VirtualizedMarkdown)),
            pilot=pilot,
            description="populated transcript composed",
        )
        session.bar.add_to_history(_EARLIER_PROMPT)

        draft = await expand_shortcut(session, "zzcard")
        assert draft.owner is not None
        card = main.query_one(PromptCard)
        await _settled(
            pilot,
            main,
            lambda: card.visible and card.state is not None and card.state.commands == 1 and _shows_actions(card),
            "card shown with the switch, its command count and the ✕ button",
        )
        assert BETA in _body(card)
        shown_height = card.region.height
        # Anchored on the input: it covers none of what is typed, and every state keeps that bottom edge.
        anchored = card.region.bottom
        assert anchored <= chat.region.y

        layouts: list[None] = []
        restyles: list[bool] = []
        footer_recomposes: list[None] = []
        refresh_layout = main._refresh_layout
        update_node_styles = main.update_node_styles
        footer = main.query_one(ChrysFooter)
        recompose = footer.recompose

        def record_layout(*args: object, **kwargs: object) -> None:
            layouts.append(None)
            refresh_layout(*args, **kwargs)  # type: ignore[arg-type]

        def record_restyle(animate: bool = True) -> None:
            restyles.append(animate)
            update_node_styles(animate)

        async def record_recompose() -> None:
            footer_recomposes.append(None)
            await recompose()

        # Recorded and still done, so a regression shows as a count rather than as a card that never moves.
        monkeypatch.setattr(main, "_refresh_layout", record_layout)
        monkeypatch.setattr(main, "update_node_styles", record_restyle)
        monkeypatch.setattr(footer, "recompose", record_recompose)

        # Changed: one more command repaints the count in place.
        chat.move_cursor(chat.document.end)
        chat.insert(" !`echo b`")
        await _settled(
            pilot,
            main,
            lambda: card.state is not None and card.state.commands == 2 and _shows_actions(card),
            "card shows two commands",
        )
        assert card.region.height == shown_height
        assert card.region.bottom == anchored
        edited = session.bar.draft()

        # Hidden: an entry from prompt history is no shortcut's draft, and nothing of the card stays under the ✕.
        spot = card.query_one("#prompt-card-remove", Button).region.offset
        await pilot.press("up")
        await _settled(
            pilot, main, lambda: chat.text == _EARLIER_PROMPT and not card.visible, "history entry shown, card hidden"
        )
        under, _region = main.get_widget_at(*spot)
        assert card not in under.ancestors_with_self

        # Shown again: stepping back out of history brings the draft back whole, its card with it.
        await pilot.press("down")
        await _settled(
            pilot,
            main,
            lambda: session.bar.draft() == edited and card.visible and _shows_actions(card),
            "draft and card back",
        )
        assert card.region.height == shown_height
        assert card.region.bottom == anchored
        assert footer_recomposes == []

        # The switch dropped with ✕: the commands stay, the button and the note go, and the card shrinks.
        await click_when_settled(pilot, card.query_one("#prompt-card-remove", Button))
        await _settled(
            pilot,
            main,
            lambda: (
                card.state is not None
                and card.state.agent is None
                and card.state.model is None
                and card.state.commands == 2
                and not _shows_actions(card)
            ),
            "card reduced to its commands",
        )
        assert BETA not in _body(card)
        assert card.region.height < shown_height
        assert card.region.bottom == anchored
        assert chat.text == edited.text

        assert layouts == []
        assert restyles == []


def _painted(app: object) -> str:
    """The text the screen shows, as its screenshot draws it."""
    svg = html.unescape(app.export_screenshot()).replace("\xa0", " ")  # type: ignore[attr-defined]
    return " ".join(re.findall(r">([^<]*)<", svg))


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_card_that_shrinks_while_hidden_shows_every_row_again(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shown with its switch, then hidden; the shortcut's agent goes into force meanwhile, so the card comes back
    with fewer rows (no agent row, ✕ or note), set while it was hidden. Each of them is drawn, the last one too."""
    write_shortcut("zzreview", f"---\nagent: {BETA}\nmodel: no-such-model\n---\nReview $1\n\nStats:\n!`echo stat`")
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(172, 48)) as session:
        main, pilot, app, chat = session.main, session.pilot, session.app, session.chat
        card = main.query_one(PromptCard)
        await expand_shortcut(session, "zzreview")
        await _settled(pilot, main, lambda: card.visible and _shows_actions(card), "card shown with its switch")
        assert "1 command in the input" in _painted(app)
        chat.select_all()
        await pilot.press("backspace")
        await _settled(pilot, main, lambda: chat.text == "" and not card.visible, "card hidden")
        await session.bus.publish(ev.AgentProfileSwitch(profile_name=BETA))
        await wait_for(
            lambda: profile_tag(main) == BETA, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="on Beta"
        )
        # The switch's load dialog has closed and the composer has the keys again.
        session.bar.focus_input()
        await _settled(pilot, main, lambda: app.screen is main and chat.has_focus, "the composer ready")

        await expand_shortcut(session, "zzreview")
        await _settled(
            pilot,
            main,
            lambda: card.visible and card.state is not None and card.state.agent is None and not _shows_actions(card),
            "card shown again, the switch in force",
        )

        painted = _painted(app)
        assert "not found" in painted
        assert "1 command in the input" in painted
