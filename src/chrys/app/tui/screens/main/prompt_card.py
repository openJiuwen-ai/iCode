# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The prompt-shortcut card for the draft a prompt shortcut expanded into, and where that draft goes.

The draft belongs to its shortcut (its :class:`ShortcutIntent`, kept by the
input bar with the draft's text and fields as one ``ChatDraft``) for as long
as it is edited; an edit that empties it, or a whole new draft, ends that.
Every ``!`command``` in such a draft runs when it is sent, and the card is
shown whenever a send would run commands or switch, so what Enter will do is
always on screen. What the card shows, and what a send switches, are both
worked out from the declaration against the session at that moment: the
card shows only what differs. ✕, or the user picking an agent or model
themselves, drops only the switch: the commands still run.

Sending is the input flow's, like any message's: the engine switches the
session's own agent and model in one step (``SessionOnlySwitch``) and runs
the commands as part of admitting the message. This decides only whether a
draft goes now, and which switch it asks for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from chrys.app.tui.widgets.chrome.prompt_card import CardChoice, CardState
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent
from chrys.foundation.i18n import MessageRef, msg
from chrys.service.prompt_shortcuts.shell import shell_commands

if TYPE_CHECKING:
    from chrys.app.tui.widgets.chrome.input_bar import ChatDraft

logger = logging.getLogger(__name__)

PROMPT_SHORTCUT_TITLE = msg("tui.prompt_card.title", fallback="Prompt shortcut")
_BUSY = msg(
    "tui.prompt_card.busy",
    fallback="Send it when the current task ends, or press “✕ Don't switch” to add it to the task as is.",
)
_BUSY_COMMANDS = msg(
    "tui.prompt_card.busy_commands",
    fallback="This prompt runs commands when sent: send it when the current task ends.",
)
_CONTINUE_IS_A_NOTE = msg(
    "tui.prompt_card.continue_is_a_note",
    fallback="Continue sends a note as typed. Press Enter to send this prompt as a new message.",
)
_MODEL_LOCKED = msg(
    "tui.prompt_card.model_locked",
    fallback="The agent fixes its own model, so the card's model was not applied.",
)

type ShortcutRoute = Literal["plain", "send", "keep"]
"""``plain``: the draft has no shortcut work and is sent as any text is. ``send``: it goes now as a new
message. ``keep``: it stays in the composer (the agent is busy or loading, or Continue was pressed)."""


@dataclass(frozen=True, slots=True)
class Option:
    """A pickable agent or model: what the user sees and what a switch takes."""

    label: str
    value: str
    model_id: str = ""
    provider: str = ""


@dataclass(frozen=True, slots=True)
class PendingSwitch:
    """The session-only switch a send asks the engine for: an agent profile name and/or a model profile id."""

    agent: str = ""
    model_profile: str = ""


@dataclass(frozen=True, slots=True)
class PromptCardCallbacks:
    """Screen-owned reads and effects the card needs."""

    show: Callable[[CardState], None]
    hide: Callable[[], None]
    agents: Callable[[], Sequence[Option]]
    """Agents a chat can switch to."""
    models: Callable[[], Sequence[Option]]
    """Selectable model profiles."""
    current_agent: Callable[[], tuple[str, str]]
    """The session agent's profile name and display name."""
    current_model: Callable[[str], tuple[str, str]]
    """Profile id and display name of the model that runs on *agent* (the current agent when empty) with no
    model switch: an agent's own model, else the session's."""
    agent_running: Callable[[], bool]
    agent_loading: Callable[[], bool]
    """The agent is starting or rebuilding: nothing can switch or send yet."""
    draft: Callable[[], ChatDraft]
    """The composer's draft now (``InputBar.draft``)."""
    draft_owner: Callable[[], ShortcutIntent | None]
    """What the composer's draft belongs to now, read without copying its text (``InputBar.draft_owner``)."""
    set_owner: Callable[[ShortcutIntent | None], None]
    """Change what the composer's draft belongs to, keeping its text."""
    notify: Callable[[MessageRef, MessageRef, str], None]
    """``(message, title, severity)``."""


def match_model(spec: str, options: Sequence[Option]) -> Option | None:
    """Find the profile a frontmatter ``model`` names, or None when nothing or more than one fits.

    In order: a profile id or name; ``provider/model`` with both parts matching
    (opencode's form); a model id. A model id several profiles share, for
    example the same model behind two gateways, names none of them: the
    request would go to whichever came first, so the user is told instead.
    """
    named = [option for option in options if spec in {option.value, option.label}]
    if len(named) == 1:
        return named[0]
    provider, slash, model = spec.partition("/")
    candidates = [option for option in options if slash and option.provider == provider and option.model_id == model]
    if not candidates:
        # A model id may itself hold a slash (``anthropic/claude-…`` behind a router).
        candidates = [option for option in options if option.model_id == spec]
    return candidates[0] if len(candidates) == 1 else None


def shortcut_model_text(spec: str, options: Sequence[Option]) -> str:
    """A shortcut's ``model`` as listed: without its provider only when ``provider/model`` names a configured model.

    Anything else is shown as written, so a profile whose own name holds a
    slash (``team/fast``) is never cut to look like another model.
    """
    found = match_model(spec, options)
    provider, slash, model_id = spec.partition("/")
    if found is not None and slash and found.provider == provider and found.model_id == model_id:
        return model_id
    return spec


class PromptCardController:
    """Owns the card for the draft a prompt shortcut expanded into."""

    def __init__(self, callbacks: PromptCardCallbacks) -> None:
        self._callbacks = callbacks
        self._shown: CardState | None = None
        """What the card shows now, so a repaint that changes nothing paints nothing."""

    @property
    def active(self) -> bool:
        """Whether the composer's draft is a prompt shortcut's (its card shown or, with nothing to say, unseen)."""
        return self._callbacks.draft_owner() is not None

    # -- the draft's card --------------------------------------------------

    def on_draft_changed(self) -> None:
        """The draft's text or owner changed: measure the card again (a new command count, another owner)."""
        self._repaint()

    def drop_switch(self) -> None:
        """Keep the session's agent and model (✕); the draft stays the shortcut's, and its commands still run."""
        owner = self._callbacks.draft_owner()
        if owner is None or owner.switch_dropped:
            return
        self._callbacks.set_owner(replace(owner, switch_dropped=True))
        self._repaint()

    def on_user_pick(self) -> None:
        """The user chose an agent or model themselves: that wins over the draft's switch.

        The one caller is ``RuntimeConfigCallbacks.user_picked``, which says what
        counts as a choice. Every other change (a settings reload, a restore, a
        settings dialog's fallback, a send) only re-measures the card
        (:meth:`on_session_changed`).
        """
        self.drop_switch()

    def on_session_changed(self) -> None:
        """The session's agent or model changed, by anyone: measure the card again, keeping its declaration."""
        self._repaint()

    def _repaint(self) -> None:
        # The draft's card shows what differs from the session now and the
        # commands a send runs; a draft with neither keeps its card unseen.
        # Every keystroke repaints: only a shortcut's draft is worth reading whole.
        owner = self._callbacks.draft_owner()
        state = None if owner is None else self._view(owner, self._callbacks.draft().text)
        shown = state if state is not None and state.visible else None
        if shown == self._shown:
            return
        self._shown = shown
        if shown is None:
            self._callbacks.hide()
        else:
            self._callbacks.show(shown)

    def _view(self, intent: ShortcutIntent, text: str) -> CardState:
        """What the card says for *intent* and its draft *text*, measured against the session now."""
        commands = len(shell_commands(text))
        if intent.switch_dropped:
            return CardState(name=intent.name, commands=commands)
        agent = self._agent_choice(intent.agent)
        model = self._model_choice(intent.model, _target(agent))
        return CardState(name=intent.name, agent=agent, model=model, commands=commands)

    # -- where the draft goes ----------------------------------------------

    def route(self, draft: ChatDraft, *, continue_button: bool) -> ShortcutRoute:
        """Where a submit gesture sends *draft*: as any text, now as a new message, or nowhere yet.

        A shortcut's prompt with work to do (commands, a switch) opens a turn of
        its own: never a note to the task being continued, nor text added to a
        running one.
        """
        owner = draft.owner
        if owner is None:
            return "plain"
        view = self._view(owner, draft.text)
        switching = any(choice is not None and choice.value is not None for choice in (view.agent, view.model))
        if not switching and not view.commands:
            return "plain"
        if self._callbacks.agent_loading():
            # Nothing happens until the agent is ready; the draft stays where it is.
            return "keep"
        if continue_button:
            self._callbacks.notify(_CONTINUE_IS_A_NOTE.bind(), PROMPT_SHORTCUT_TITLE.bind(), "warning")
            return "keep"
        if self._callbacks.agent_running():
            busy = _BUSY_COMMANDS if view.commands else _BUSY
            self._callbacks.notify(busy.bind(), PROMPT_SHORTCUT_TITLE.bind(), "warning")
            return "keep"
        return "send"

    def pending_switch(self, draft: ChatDraft) -> PendingSwitch | None:
        """The switch *draft*'s send asks for, measured against the session now; None when there is none."""
        owner = draft.owner
        if owner is None or owner.switch_dropped:
            return None
        agent = self._agent_choice(owner.agent)
        agent_name = _target(agent)
        # Measured against what runs on the agent the send runs on, which an agent switch may change.
        # Whether that agent runs it at all is the engine's to say (``SessionOnlySwitch.model_left_out``).
        model = self._model_choice(owner.model, agent_name)
        model_id = model.value if model is not None and model.value is not None else ""
        if not agent_name and not model_id:
            return None
        return PendingSwitch(agent=agent_name, model_profile=model_id)

    def model_left_out(self) -> None:
        """Say that the engine left the card's model out: the agent runs a model of its own."""
        self._callbacks.notify(_MODEL_LOCKED.bind(), PROMPT_SHORTCUT_TITLE.bind(), "warning")

    # -- choices -----------------------------------------------------------

    def _agent_choice(self, spec: str) -> CardChoice | None:
        if not spec:
            return None
        options = list(self._callbacks.agents())
        current_name, current = self._callbacks.current_agent()
        # The profile name is the agent's identity; display names may repeat.
        found = next((option for option in options if spec == option.value), None) or next(
            (option for option in options if spec == option.label), None
        )
        if found is not None and found.value == current_name:
            return None
        if found is None:
            return CardChoice(value=None, label=spec, current=current)
        return CardChoice(value=found.value, label=found.label, current=current)

    def _model_choice(self, spec: str, agent: str) -> CardChoice | None:
        """*spec* against what runs on *agent* (the current agent when empty) with no model switch."""
        if not spec:
            return None
        options = list(self._callbacks.models())
        current_id, current_label = self._callbacks.current_model(agent)
        found = match_model(spec, options)
        if found is not None and found.value == current_id:
            return None
        if found is None:
            return CardChoice(value=None, label=spec, current=current_label)
        return CardChoice(value=found.value, label=found.label, current=current_label)


def _target(agent: CardChoice | None) -> str:
    """The agent a switch goes to; "" when the send stays on the current one."""
    return agent.value if agent is not None and agent.value is not None else ""


__all__ = [
    "PROMPT_SHORTCUT_TITLE",
    "Option",
    "PendingSwitch",
    "PromptCardCallbacks",
    "PromptCardController",
    "ShortcutIntent",
    "ShortcutRoute",
    "match_model",
    "shortcut_model_text",
]
