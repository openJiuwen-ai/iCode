# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact, validated save dialog for user themes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalGroup
from textual.theme import Theme
from textual.widgets import Button, Label

from chrys.app.tui.i18n import LocaleController, render_str, widget_localizer
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import ThemeFileRevision, ThemeStoreError, UserThemeStore
from chrys.app.tui.widgets.dialog_buttons import DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets.input import EnhancedInput as Input
from chrys.foundation.i18n import MessageRef

from . import messages as M
from .palette import _DismissableModal


class SaveThemeDialog(_DismissableModal):
    def __init__(
        self,
        theme: Theme,
        store: UserThemeStore,
        revision: ThemeFileRevision | None,
        *,
        preview: Callable[[Theme], str | None],
        saved: Callable[[Theme, ThemeFileRevision], None],
        restore: Callable[[], object],
        locale_controller: LocaleController,
        preview_error: Callable[[], str],
    ) -> None:
        super().__init__(locale_controller=locale_controller)
        self._theme = copy_theme(theme)
        self._store = store
        self._revision = revision
        self._preview = preview
        self._saved = saved
        self._restore = restore
        self._preview_error = preview_error
        self._error_message: MessageRef | str = ""
        self._error_target: tuple[str, bool] | None = None
        self._retryable_error = False
        self._saving = False
        self._last_input: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="save-theme-container") as dialog:
            dialog.border_title = M.text(self, M.SAVE_TITLE)
            yield Label(Text(M.text(self, M.NAME)), id="save-theme-name-label")
            yield Input(self._theme.name, id="theme-name")
            yield Label(Text(M.text(self, M.SAVE_HINT)), id="save-theme-hint")
            yield Label(Text(M.text(self, M.READ_ONLY_HINT)), id="save-theme-error")
            yield DialogButtonRow(
                DialogButtonSpec(Text(M.text(self, M.SAVE)), "save-theme-confirm", variant="success"),
                DialogButtonSpec(Text(M.text(self, M.CANCEL)), "save-theme-cancel", variant="warning"),
            )

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self.query_one("#save-theme-confirm" if self._revision is not None else "#theme-name").focus()

    def refresh_localization(self) -> None:
        self.query_one("#save-theme-container").border_title = Text(M.text(self, M.SAVE_TITLE))
        for name, definition in (("name-label", M.NAME), ("hint", M.SAVE_HINT)):
            self.query_one(f"#save-theme-{name}", Label).update(Text(M.text(self, definition)))
        self.query_one("#save-theme-cancel", Button).label = Text(M.text(self, M.CANCEL))
        self.query_one("#save-theme-confirm", Button).label = Text(M.text(self, M.SAVING if self._saving else M.SAVE))
        self._render_error()

    def _error(self, message: MessageRef | str, *, retryable: bool = False) -> None:
        self._error_message = message
        self._error_target = self._name_error_key() if message else None
        self._retryable_error = retryable
        self._render_error()

    def _name_error_key(self) -> tuple[str, bool]:
        name = self.query_one(Input).value
        return name.casefold(), self._revision is not None and name == self._theme.name

    def _active_error(self) -> MessageRef | str:
        # Reuse the last rejected name's error without inspecting files or CSS.
        # Case-only name attempts share an error, except the exact original name:
        # its revision allows overwriting the existing user theme.
        return self._error_message if self._name_error_key() == self._error_target else ""

    def _render_error(self) -> None:
        message = self._active_error()
        text = (
            render_str(widget_localizer(self), message)
            if isinstance(message, MessageRef)
            else ((self._preview_error() or message) if message else M.text(self, M.READ_ONLY_HINT))
        )
        label = self.query_one("#save-theme-error", Label)
        content = label.content
        if not isinstance(content, Text) or content.plain != text:
            label.update(Text(text))
        label.set_class(bool(message), "--error")
        self.query_one("#save-theme-confirm", Button).disabled = (
            bool(message) and not self._retryable_error
        ) or self._saving

    def _validate_name(self) -> Theme | None:
        name = self.query_one(Input).value
        try:
            self._store.validate_name(name, self._revision if name == self._theme.name else None)
        except ThemeStoreError as error:
            self._error(error.display, retryable=error.retryable)
            return None
        return copy_theme(self._theme, name=name)

    def _validate(self) -> Theme | None:
        candidate = self._validate_name()
        if candidate is None:
            return None
        error = self._preview(candidate)
        self._error(error or "")
        return None if error else candidate

    @on(Input.Changed, "#theme-name")
    def _name_changed(self, event: Input.Changed) -> None:
        event.stop()
        if (
            self._saving
            or self._dismiss_requested
            or event.value != event.input.value
            or event.value == self._last_input
        ):
            return
        self._last_input = event.value
        self._render_error()

    @on(Button.Pressed, "#save-theme-confirm")
    @on(Input.Submitted, "#theme-name")
    async def _save(self, event: Button.Pressed | Input.Submitted) -> None:
        event.stop()
        if (
            self._saving
            or self._dismiss_requested
            or self.app.screen is not self
            or (self._active_error() and not self._retryable_error)
            or (candidate := self._validate()) is None
        ):
            return
        self._saving = True
        self.query_one(Input).disabled = True
        button = self.query_one("#save-theme-confirm", Button)
        button.disabled = True
        button.label = Text(M.text(self, M.SAVING))
        try:
            previous = self._revision if candidate.name == self._theme.name else None
            revision = await asyncio.to_thread(self._store.save, candidate, previous)
        except ThemeStoreError as error:
            self._error(error.display, retryable=error.retryable)
        else:
            self._saved(candidate, revision)
            self.notify(
                M.text(self, M.SAVED, name=candidate.name),
                title=M.text(self, M.TITLE),
                severity="information",
                timeout=3,
                markup=False,
            )
            self._saving = False
            super().action_close()
            return
        finally:
            self._saving = False
        self.query_one(Input).disabled = False
        button.label = Text(M.text(self, M.SAVE))
        self._error(self._error_message, retryable=self._retryable_error)

    @on(Button.Pressed, "#save-theme-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()

    def action_close(self) -> None:
        if not self._saving:
            super().action_close()

    def on_unmount(self) -> None:
        if self.app.is_running:
            self._restore()
