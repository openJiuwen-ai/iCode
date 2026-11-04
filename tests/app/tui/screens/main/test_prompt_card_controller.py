# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the prompt-shortcut card's controller and its model matching, driven through fake screen callbacks.

The controller shows the card and decides where a shortcut's draft goes and which switch it asks for; sending is
the input flow's and the engine's (``test_prompt_shortcut_send.py`` drives that through a real app).
"""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from chrys.app.tui.screens.main.prompt_card import (
    Option,
    PendingSwitch,
    PromptCardCallbacks,
    PromptCardController,
    match_model,
    shortcut_model_text,
)
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft
from chrys.app.tui.widgets.chrome.prompt_card import CardState
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent
from chrys.foundation.i18n import MessageRef

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")


# -- match_model -------------------------------------------------------------


_OPTIONS = (
    Option(label="Fast", value="fast-profile", model_id="gpt-x", provider="openai"),
    Option(label="Smart", value="smart-profile", model_id="claude-y", provider="anthropic"),
)


@pytest.mark.parametrize("spec", ["fast-profile", "Fast"])
def test_match_model_by_profile_id_or_name(spec: str) -> None:
    assert match_model(spec, _OPTIONS) == _OPTIONS[0]


def test_match_model_provider_slash_model_needs_both_parts() -> None:
    a = Option(label="A", value="pa", model_id="same", provider="a")
    b = Option(label="B", value="pb", model_id="same", provider="b")
    assert match_model("b/same", (a, b)) == b
    assert match_model("a/same", (a, b)) == a
    assert match_model("c/same", (a, b)) is None
    assert match_model("openai/claude-y", _OPTIONS) is None


def test_match_model_bare_model_id_must_be_unique() -> None:
    assert match_model("claude-y", _OPTIONS) == _OPTIONS[1]
    a = Option(label="A", value="pa", model_id="same", provider="a")
    b = Option(label="B", value="pb", model_id="same", provider="b")
    assert match_model("same", (a, b)) is None


def test_match_model_model_id_holding_a_slash() -> None:
    routed = Option(label="Routed", value="routed", model_id="anthropic/claude-x", provider="openrouter")
    assert match_model("anthropic/claude-x", (*_OPTIONS, routed)) == routed


def test_match_model_no_match() -> None:
    assert match_model("nothing-like-it", _OPTIONS) is None
    assert match_model("x", ()) is None


@pytest.mark.parametrize(
    ("spec", "listed"),
    [
        # provider/model naming a configured model: the model alone.
        ("anthropic/claude-y", "claude-y"),
        # A profile's own name or id, slash included, is what the file set: as written.
        ("team/fast", "team/fast"),
        ("fast-profile", "fast-profile"),
        # A model id that holds a slash, behind a router: as written.
        ("anthropic/claude-x", "anthropic/claude-x"),
        # Nothing configured by that name, or more than one: as written.
        ("openai/claude-y", "openai/claude-y"),
        ("b/unknown", "b/unknown"),
    ],
)
def test_a_shortcut_model_drops_its_provider_only_when_that_names_a_configured_model(spec: str, listed: str) -> None:
    options = (
        *_OPTIONS,
        Option(label="Team fast", value="team/fast", model_id="gpt-x", provider="openai"),
        Option(label="Routed", value="routed", model_id="anthropic/claude-x", provider="openrouter"),
    )
    assert shortcut_model_text(spec, options) == listed


# -- controller harness ------------------------------------------------------


class _Screen:
    """Fake screen: the composer's draft, the session's agent and model, and what the controller shows or says."""

    def __init__(self) -> None:
        self.shown: list[CardState] = []
        self.visible: CardState | None = None
        self.agent_list = [Option(label="Code", value="Code"), Option(label="QA", value="QA")]
        self.model_list = [
            Option(label="Model A", value="model-a", model_id="ma", provider="p"),
            Option(label="Model B", value="model-b", model_id="mb", provider="p"),
        ]
        self.agent = ("Code", "Code")
        self.model = ("model-a", "Model A")
        self.running = False
        self.loading = False
        self.target_models: dict[str, tuple[str, str]] = {}
        """What runs on a named agent with no model switch, when not the current model."""
        self.composer = ChatDraft("")
        self.notices: list[tuple[MessageRef, str]] = []

    def callbacks(self) -> PromptCardCallbacks:
        return PromptCardCallbacks(
            show=self._show,
            hide=self._hide,
            agents=lambda: self.agent_list,
            models=lambda: self.model_list,
            current_agent=lambda: self.agent,
            current_model=lambda agent: self.target_models.get(agent, self.model) if agent else self.model,
            agent_running=lambda: self.running,
            agent_loading=lambda: self.loading,
            draft=lambda: self.composer,
            draft_owner=lambda: self.composer.owner,
            set_owner=self._set_owner,
            notify=lambda message, _title, severity: self.notices.append((message, severity)),
        )

    def _show(self, state: CardState) -> None:
        self.shown.append(state)
        self.visible = state

    def _hide(self) -> None:
        self.visible = None

    def _set_owner(self, owner: ShortcutIntent | None) -> None:
        self.composer = replace(self.composer, owner=owner)

    def expand(self, text: str, *, agent: str = "", model: str = "") -> PromptCardController:
        """The composer now holds a shortcut's draft; return a controller that has seen it."""
        self.composer = ChatDraft(text, owner=ShortcutIntent("zz", agent, model, path="/shortcuts/zz.md"))
        controller = PromptCardController(self.callbacks())
        controller.on_draft_changed()
        return controller


def _keys(screen: _Screen) -> list[str]:
    return [message.definition.key for message, _severity in screen.notices]


@pytest.fixture
def screen() -> _Screen:
    return _Screen()


# -- the card -------------------------------------------------------------------


@pytest.mark.parametrize(("agent", "model"), [("QA", ""), ("", "model-b"), ("Nobody", ""), ("", "nothing")])
def test_the_card_shows_what_differs_from_the_session_or_was_not_found(screen: _Screen, agent: str, model: str) -> None:
    screen.expand("Review it", agent=agent, model=model)
    assert screen.visible is not None
    choice = screen.visible.agent if agent else screen.visible.model
    assert choice is not None
    assert (choice.value is None) == (agent == "Nobody" or model == "nothing")


@pytest.mark.parametrize(("agent", "model"), [("Code", ""), ("", "model-a"), ("Code", "model-a"), ("", "")])
def test_what_the_session_already_uses_shows_no_card(screen: _Screen, agent: str, model: str) -> None:
    screen.expand("Review it", agent=agent, model=model)
    assert screen.visible is None


def test_the_same_agent_and_another_model_shows_only_the_model_row(screen: _Screen) -> None:
    screen.expand("Review it", agent="Code", model="model-b")
    assert screen.visible is not None
    assert screen.visible.agent is None
    assert screen.visible.model is not None and screen.visible.model.value == "model-b"


def test_commands_alone_show_their_count(screen: _Screen) -> None:
    screen.expand("Diff: !`git diff` and !`git status`")
    assert screen.visible == CardState(name="zz", commands=2)


def test_an_edit_that_adds_a_command_repaints_the_count(screen: _Screen) -> None:
    controller = screen.expand("Diff: !`git diff`")
    screen.composer = replace(screen.composer, text="Diff: !`git diff` !`git log`")
    controller.on_draft_changed()
    assert screen.visible is not None and screen.visible.commands == 2


def test_dropping_the_switch_keeps_the_commands_and_the_owner(screen: _Screen) -> None:
    controller = screen.expand("Diff: !`git diff`", agent="QA")
    controller.drop_switch()
    assert screen.visible == CardState(name="zz", commands=1)
    assert screen.composer.owner is not None and screen.composer.owner.switch_dropped


def test_dropping_the_only_switch_hides_the_card_and_the_draft_stays_the_shortcuts(screen: _Screen) -> None:
    controller = screen.expand("Review it", agent="QA")
    controller.drop_switch()
    assert screen.visible is None
    assert screen.composer.owner is not None


def test_the_users_own_pick_drops_the_switch_and_a_session_change_only_measures_again(screen: _Screen) -> None:
    controller = screen.expand("Review it", agent="QA")
    screen.agent = ("QA", "QA")
    controller.on_session_changed()
    # The session runs the shortcut's agent now: nothing differs, and the declaration stays.
    assert screen.visible is None
    assert screen.composer.owner is not None and not screen.composer.owner.switch_dropped
    screen.agent = ("Code", "Code")
    controller.on_session_changed()
    assert screen.visible is not None and screen.visible.agent is not None
    controller.on_user_pick()
    assert screen.visible is None
    assert screen.composer.owner is not None and screen.composer.owner.switch_dropped


# -- where the draft goes ---------------------------------------------------------


def test_a_draft_that_is_no_shortcuts_goes_plainly(screen: _Screen) -> None:
    controller = PromptCardController(screen.callbacks())
    assert controller.route(ChatDraft("hi !`ls`"), continue_button=False) == "plain"


def test_a_shortcuts_draft_with_no_work_goes_plainly(screen: _Screen) -> None:
    controller = screen.expand("Review it", agent="Code")
    assert controller.route(screen.composer, continue_button=False) == "plain"
    assert controller.route(screen.composer, continue_button=True) == "plain"


@pytest.mark.parametrize(("text", "agent"), [("Diff: !`git diff`", ""), ("Review it", "QA")])
def test_a_shortcuts_draft_with_work_goes_as_a_new_message(screen: _Screen, text: str, agent: str) -> None:
    controller = screen.expand(text, agent=agent)
    assert controller.route(screen.composer, continue_button=False) == "send"
    assert screen.notices == []


def test_while_the_agent_loads_the_draft_stays_without_a_notice(screen: _Screen) -> None:
    controller = screen.expand("Diff: !`git diff`")
    screen.loading = True
    assert controller.route(screen.composer, continue_button=False) == "keep"
    assert screen.notices == []


@pytest.mark.parametrize(
    ("text", "agent", "notice"),
    [
        ("Diff: !`git diff`", "", "tui.prompt_card.busy_commands"),
        ("Review it", "QA", "tui.prompt_card.busy"),
    ],
)
def test_while_a_turn_runs_the_draft_stays_with_a_notice(screen: _Screen, text: str, agent: str, notice: str) -> None:
    controller = screen.expand(text, agent=agent)
    screen.running = True
    assert controller.route(screen.composer, continue_button=False) == "keep"
    assert _keys(screen) == [notice]


def test_the_continue_button_never_sends_a_draft_with_work(screen: _Screen) -> None:
    controller = screen.expand("Diff: !`git diff`")
    assert controller.route(screen.composer, continue_button=True) == "keep"
    assert _keys(screen) == ["tui.prompt_card.continue_is_a_note"]


# -- the switch a send asks for ---------------------------------------------------


def test_the_switch_names_the_targets_profile_name_and_model_id(screen: _Screen) -> None:
    screen.agent_list = [Option(label="Code", value="Code"), Option(label="Quality", value="QA")]
    controller = screen.expand("Review it", agent="Quality", model="Model B")
    assert controller.pending_switch(screen.composer) == PendingSwitch(agent="QA", model_profile="model-b")


@pytest.mark.parametrize(("agent", "model"), [("Code", "model-a"), ("Nobody", "nothing"), ("", "")])
def test_no_switch_for_what_is_in_use_or_not_found(screen: _Screen, agent: str, model: str) -> None:
    controller = screen.expand("Review it", agent=agent, model=model)
    assert controller.pending_switch(screen.composer) is None


def test_a_dropped_switch_asks_for_nothing(screen: _Screen) -> None:
    controller = screen.expand("Review it", agent="QA", model="model-b")
    controller.drop_switch()
    assert controller.pending_switch(screen.composer) is None


def test_a_model_is_asked_for_and_the_engines_word_that_it_was_left_out_is_said(screen: _Screen) -> None:
    # Whether the agent runs it is the engine's to say: the card asks, then tells what the engine answered.
    controller = screen.expand("Review it", model="model-b")
    assert controller.pending_switch(screen.composer) == PendingSwitch(agent="", model_profile="model-b")
    assert _keys(screen) == []
    controller.model_left_out()
    assert _keys(screen) == ["tui.prompt_card.model_locked"]


def test_a_model_switch_with_an_agent_switch_is_asked_for_whole(screen: _Screen) -> None:
    controller = screen.expand("Review it", agent="QA", model="model-b")
    assert controller.pending_switch(screen.composer) == PendingSwitch(agent="QA", model_profile="model-b")


def test_a_model_that_runs_now_is_asked_for_when_the_target_agent_would_run_another(screen: _Screen) -> None:
    # The current agent runs model-b (its own); QA binds none, so it would run the session's model-a.
    screen.model = ("model-b", "Model B")
    screen.target_models = {"QA": ("model-a", "Model A")}
    controller = screen.expand("Review it", agent="QA", model="model-b")
    assert screen.visible is not None
    assert screen.visible.model is not None
    assert screen.visible.model.current == "Model A"
    assert controller.pending_switch(screen.composer) == PendingSwitch(agent="QA", model_profile="model-b")


def test_a_model_the_target_agent_runs_anyway_needs_no_model_switch(screen: _Screen) -> None:
    # The current agent runs model-a; QA would run model-b, the one named.
    screen.target_models = {"QA": ("model-b", "Model B")}
    controller = screen.expand("Review it", agent="QA", model="model-b")
    assert screen.visible is not None
    assert screen.visible.model is None
    assert controller.pending_switch(screen.composer) == PendingSwitch(agent="QA", model_profile="")
    assert _keys(screen) == []


def test_a_model_running_now_is_measured_against_what_the_target_agent_would_run(screen: _Screen) -> None:
    # The current agent runs model-a, the one named; QA would run model-b (its own): the model is asked for.
    screen.target_models = {"QA": ("model-b", "Model B")}
    controller = screen.expand("Review it", agent="QA", model="model-a")
    assert screen.visible is not None
    assert screen.visible.model is not None
    assert screen.visible.model.current == "Model B"
    assert controller.pending_switch(screen.composer) == PendingSwitch(agent="QA", model_profile="model-a")
