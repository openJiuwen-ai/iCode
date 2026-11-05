# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared card chrome for configuration panels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Button, Label
from textual.widgets.collapsible import CollapsibleTitle

from chrys.foundation.i18n.formatting import sanitize_legacy_scalar

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widget import Widget


class CardListChanged(Message):
    """Posted by a panel when a change to its card list lands after the button press that asked for it.

    The config screen re-reads the draft right after an add or delete button
    press; a delete the user must confirm first lands later, so the panel posts
    this once the rebuilt cards are mounted.
    """


class ConfigCard(Vertical):
    """Base card with shared visual chrome and delete-message behavior."""

    DEFAULT_CSS = """
    ConfigCard {
        height: auto;
        background: $foreground 5%;
        border: solid $tui-border-foreground 15%;
        padding: 1 2;
        margin: 0 2 1 0;
    }
    ConfigCard .config-card-title {
        width: 1fr;
        height: 1;
        color: $secondary;
        text-style: bold;
        content-align: left middle;
    }
    ConfigCard Button.config-card-delete-btn.-style-default.-error {
        min-width: 3;
        border: none;
        background: transparent;
        color: $error;
        content-align: center middle;
        text-align: center;
        offset-x: 1;
    }
    """

    _delete_button_prefix: str = ""

    class RemoveRequested(Message):
        """Posted when the user clicks delete."""

        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    def __init__(self, index: int, *, classes: str | None = None, read_only: bool = False) -> None:
        self._index = index
        self._read_only = read_only
        card_classes = "agent-config-card"
        if classes:
            card_classes = f"{classes} {card_classes}"
        super().__init__(classes=card_classes)

    @property
    def _delete_button_id(self) -> str:
        return f"{self._delete_button_prefix}-{self._index}"

    def compose_header(self, title: str, *, row_class: str, title_class: str) -> ComposeResult:
        """Yield a standard card header with the subclass's existing classes."""
        with Horizontal(classes=f"{row_class} config-card-header-row"):
            yield self._make_title(title, classes=f"{title_class} config-card-title")
            delete_button = Button("✕", variant="error", id=self._delete_button_id, classes="config-card-delete-btn")
            if self._read_only:
                delete_button.disabled = True
                delete_button.display = False
            yield delete_button

    def _make_title(self, title: str, *, classes: str) -> Widget:
        """Build the header title widget; ``title`` may carry markup."""
        return Label(title, classes=classes)

    def _removed_message(self) -> Message:
        """Build the remove message for this card."""
        return self.RemoveRequested(self._index)

    @on(Button.Pressed)
    def _on_config_card_button_pressed(self, event: Button.Pressed) -> None:
        if self._read_only:
            return
        if event.button.id != self._delete_button_id:
            return
        self.call_later(self.post_message, self._removed_message())


class CollapsibleConfigCard(ConfigCard):
    """Config card whose body folds away, leaving its border around one header row of title and delete button.

    The header title is Textual's ``CollapsibleTitle`` (▶/▼, click or Enter to
    toggle), but the card owns the fold: Textual's ``Collapsible`` keeps its
    title alone on the first line, while this row also carries the delete button.
    The title is literal text (no markup), so subclasses can show user data in it.
    """

    DEFAULT_CSS = """
    CollapsibleConfigCard.-collapsed {
        padding: 0 2;
    }
    CollapsibleConfigCard .config-card-header-row,
    CollapsibleConfigCard .config-card-body {
        height: auto;
    }
    CollapsibleConfigCard CollapsibleTitle.config-card-title {
        padding: 0 1;
        color: $secondary;
        text-style: bold;
    }
    CollapsibleConfigCard CollapsibleTitle.config-card-title:hover {
        background: $block-hover-background;
        color: $foreground;
    }
    CollapsibleConfigCard CollapsibleTitle.config-card-title:focus {
        background: $block-cursor-background;
        color: $block-cursor-foreground;
        text-style: $block-cursor-text-style;
    }
    """

    def __init__(
        self,
        index: int,
        *,
        collapsed: bool = False,
        classes: str | None = None,
        read_only: bool = False,
    ) -> None:
        self._collapsed = collapsed
        self._title_text = ""
        self._title_widget: CollapsibleTitle | None = None
        self._body: Vertical | None = None
        if collapsed:
            classes = f"{classes} -collapsed" if classes else "-collapsed"
        super().__init__(index, classes=classes, read_only=read_only)

    @property
    def collapsed(self) -> bool:
        """Whether the card body is folded away."""
        return self._collapsed

    def set_collapsed(self, collapsed: bool, *, scroll_visible: bool = True) -> None:
        """Fold or unfold the body; a no-op when the state is unchanged.

        Pass ``scroll_visible=False`` when unfolding several cards at once, and
        scroll the one that matters into view yourself.
        """
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.set_class(collapsed, "-collapsed")
        if self._title_widget is not None:
            self._title_widget.collapsed = collapsed
        if self._body is not None:
            self._body.display = not collapsed
        if scroll_visible and self.is_mounted:
            # As Textual's Collapsible does: keep the toggled card in view.
            self.call_after_refresh(self.scroll_visible, animate=False)

    @property
    def header_title(self) -> str:
        """The header title text, as last composed or set."""
        return self._title_text

    def set_title(self, title: str) -> None:
        """Replace the header title with literal ``title`` text."""
        self._title_text = title
        if self._title_widget is not None:
            self._title_widget.label = self._title_content(title)

    def card_body(self) -> Vertical:
        """Return the container to compose the foldable fields into (``with self.card_body():``)."""
        body = Vertical(classes="config-card-body")
        body.display = not self._collapsed
        self._body = body
        return body

    @staticmethod
    def _title_content(title: str) -> Content:
        return Content.from_text(sanitize_legacy_scalar(title), markup=False)

    def _make_title(self, title: str, *, classes: str) -> Widget:
        self._title_text = title
        title_widget = CollapsibleTitle(
            label=self._title_content(title),
            collapsed_symbol="▶",
            expanded_symbol="▼",
            collapsed=self._collapsed,
        )
        title_widget.add_class(*classes.split())
        self._title_widget = title_widget
        return title_widget

    @on(CollapsibleTitle.Toggle)
    def _on_config_card_title_toggle(self, event: CollapsibleTitle.Toggle) -> None:
        event.stop()
        self.set_collapsed(not self._collapsed)
