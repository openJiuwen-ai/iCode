# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The prompt-shortcut card shown above the input while an expanded shortcut's draft does more than send its text.

One labelled row for the agent and one for the model the frontmatter asks
for, each as ``current → target`` (or, for a name that matched nothing, a ⚠
saying the current one is used instead), and one saying how many
``!`command``` placeholders in the input run when it is sent: whenever a
send will run commands, the card says so. The commands themselves are not
listed: they show in the draft. A dim line says the switch lasts for the
session. The card offers no choice: sending accepts it, and its
"✕ Don't switch" button drops the agent and model switch; the commands still
run when the draft is sent.
Like :class:`SuggestionList` it is a screen overlay anchored at the input,
and like it, it never lays the screen out, which costs as much as the
transcript is long: its rows are pinned to what it says, and a change of rows
(the button and note going when no switch is left, a ⚠ line) remaps only the card.
The ✕ button keeps its cells while hidden, so the rows wrap at one width.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Static

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.util.visibility import set_rules_without_layout, set_widget_visibility_without_layout
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar

if TYPE_CHECKING:
    from rich.style import Style
    from textual.app import ComposeResult

_AGENT = msg("tui.prompt_card.agent", fallback="Agent")
_MODEL = msg("tui.prompt_card.model", fallback="Model")
_AGENT_NOT_FOUND = msg(
    "tui.prompt_card.agent_not_found",
    fallback="Configured agent {name} not found; using the current agent {current} instead",
)
_MODEL_NOT_FOUND = msg(
    "tui.prompt_card.model_not_found",
    fallback="Configured model {name} not found; using the current model {current} instead",
)
_NOTE = msg("tui.prompt_card.note", fallback="Applies when sent and stays on for this session")
_DONT_SWITCH = msg("tui.prompt_card.dont_switch", fallback="Don't switch")
_COMMANDS = msg("tui.prompt_card.commands", fallback="Commands")
_COMMAND_COUNT = msg(
    "tui.prompt_card.command_count",
    fallback="{count} command in the input runs when sent",
    plural_fallback="{count} commands in the input run when sent",
)
_REMOVE = msg("tui.prompt_card.remove", fallback="Don't switch the agent and model")


@dataclass(frozen=True, slots=True)
class CardChoice:
    """The agent or model a shortcut asks for: what a switch takes, or None when its name matched nothing."""

    value: str | None
    label: str
    """Display name of the target, or the frontmatter name when it matched nothing."""
    current: str
    """Display name of the session's own setting."""


@dataclass(frozen=True, slots=True)
class CardState:
    """Everything the card shows; rebuilt by its owner on every change."""

    name: str
    agent: CardChoice | None = None
    model: CardChoice | None = None
    commands: int = 0
    """The ``!`command``` placeholders a send runs."""

    @property
    def visible(self) -> bool:
        """Whether the card has anything to say: a switch, a ⚠ or commands to run."""
        return self.agent is not None or self.model is not None or self.commands > 0


class PromptCard(Widget):
    """Overlay card for an expanded prompt shortcut."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"prompt-card--label", "prompt-card--warning"}

    DEFAULT_CSS = """
    PromptCard {
        overlay: screen;
        offset-y: -100%;
        width: 100%;
        height: auto;
        visibility: hidden;
        border: round $tui-border-accent $border-opacity;
        background: $surface;
        padding: 0 1;
    }
    PromptCard > .prompt-card--label {
        color: $text-muted;
    }
    PromptCard > .prompt-card--warning {
        color: $warning;
    }
    PromptCard > #prompt-card-row {
        height: auto;
    }
    PromptCard #prompt-card-body {
        width: 1fr;
    }
    PromptCard #prompt-card-remove {
        width: auto;
        min-width: 0;
        height: 1;
        border: none;
        padding: 0 1;
        margin: 0 0 0 2;
        background: $foreground 10%;
        color: $text;
    }
    PromptCard #prompt-card-remove:hover,
    PromptCard #prompt-card-remove:focus {
        background: $accent 30%;
        text-style: none;
    }
    PromptCard #prompt-card-note {
        color: $text-muted;
    }
    """

    class Removed(Message):
        """The user dropped the agent and model switch; the draft and its commands stay."""

    def __init__(self) -> None:
        super().__init__(id="prompt-card")
        self._state: CardState | None = None

    @property
    def state(self) -> CardState | None:
        return self._state

    def compose(self) -> ComposeResult:
        with Horizontal(id="prompt-card-row"):
            yield Static("", id="prompt-card-body")
            yield Button(self._remove_label(), id="prompt-card-remove", compact=True)
        yield Static(self._note_text(), id="prompt-card-note")

    def show_state(self, state: CardState) -> None:
        """Show *state*, or update the card in place when it is already showing."""
        self._state = state
        self._paint(state)
        set_widget_visibility_without_layout(self, True)

    def refresh_localization(self) -> None:
        """Repaint the card in the current language."""
        self.query_one("#prompt-card-remove", Button).label = self._remove_label()
        self.query_one("#prompt-card-note", Static).update(self._note_text())
        if self._state is not None:
            self._paint(self._state)

    def _remove_label(self) -> Text:
        return Text(f"✕ {self._render_message(_DONT_SWITCH.bind())}")

    def hide_card(self) -> None:
        if self._state is None:
            return
        self._state = None
        set_widget_visibility_without_layout(self, False)

    def on_resize(self, _event: events.Resize) -> None:
        # A new width may wrap the rows differently.
        if self._state is not None:
            self._paint(self._state)

    def _paint(self, state: CardState) -> None:
        remove = self.query_one("#prompt-card-remove", Button)
        remove.tooltip = self._render_message(_REMOVE.bind())
        switching = any(choice is not None and choice.value is not None for choice in (state.agent, state.model))
        # Offered only while it would change something: a ⚠ alone switches nothing.
        actions = switching
        body = self.query_one("#prompt-card-body", Static)
        text = self._body(state)
        body.update(text, layout=False)
        rows = self._body_rows(body, text)
        note_rows = 1 if actions else 0
        # The rows pinned to what the card says, and the button shown by inheriting the card's
        # visibility (so it hides with the card), remapped once; while the card is hidden, the
        # flip that shows it remaps it. Every box on the body's path is pinned, its row (body and
        # one-row button) too: no layout runs here, so an auto height there would keep a stale
        # value and clip the body (a card shrunk while hidden lost its last row).
        set_rules_without_layout(
            visibility=[(remove, None if actions else False)],
            heights=[
                (body, rows),
                (self.query_one("#prompt-card-row", Horizontal), rows),
                (self.query_one("#prompt-card-note", Static), note_rows),
                (self, rows + note_rows + self.styles.gutter.height),
            ],
            remap=self.visible,
        )

    @staticmethod
    def _body_rows(body: Static, text: Text) -> int:
        """The rows *text* takes in *body*, measured as a layout would (the update cleared the cached height).

        Before the first layout there is no width to wrap at: one row per line.
        """
        width = body.size.width
        rows = body.get_content_height(body.size, body.size, width) if width else len(text.plain.splitlines())
        return max(rows, 1)

    def _note_text(self) -> Text:
        return Text(self._render_message(_NOTE.bind()))

    def _body(self, state: CardState) -> Text:
        """One row per labelled item, values aligned after the widest label."""
        warning = self.get_component_rich_style("prompt-card--warning", partial=False)
        rows: list[tuple[str, Text]] = []
        for label, not_found, choice in (
            (_AGENT, _AGENT_NOT_FOUND, state.agent),
            (_MODEL, _MODEL_NOT_FOUND, state.model),
        ):
            if choice is not None:
                rows.append((self._render_message(label.bind()), self._choice_value(choice, not_found, warning)))
        if state.commands:
            count = _COMMAND_COUNT.bind(count=state.commands)
            rows.append((self._render_message(_COMMANDS.bind()), Text(self._render_message(count))))
        width = max((cell_len(label) for label, _value in rows), default=0)
        label_style = self.get_component_rich_style("prompt-card--label", partial=False)
        body = Text()
        for index, (label, value) in enumerate(rows):
            if index:
                body.append("\n")
            body.append(label + " " * (width - cell_len(label) + 2), style=label_style)
            body.append_text(value)
        return body

    def _choice_value(self, choice: CardChoice, not_found: MessageDef, warning: Style) -> Text:
        if choice.value is not None:
            return Text(f"{sanitize_legacy_scalar(choice.current)} → {sanitize_legacy_scalar(choice.label)}")
        # The ⚠ alone is coloured: ``Text(..., style=)`` would colour the whole line.
        value = Text()
        value.append("⚠ ", style=warning)
        line = self._render_message(not_found.bind(name=choice.label, current=choice.current))
        value.append(sanitize_legacy_scalar(line))
        return value

    @on(Button.Pressed, "#prompt-card-remove")
    def _on_remove(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Removed())

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)


__all__ = ["CardChoice", "CardState", "PromptCard"]
