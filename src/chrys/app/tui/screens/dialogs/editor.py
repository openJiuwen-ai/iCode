# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Large modal editor for preparing a multi-line chat draft.

It edits the whole draft (labels, prompt fields, owner) by the input box's
rules (``DraftEditor``) and hands the whole draft back.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.containers import HorizontalGroup, VerticalGroup
from textual.widgets import Button, Select, Static

from chrys.app.tui.i18n import render_str, widget_locale_controller, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.draft_text_area import DraftTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.app.tui.widgets.editor import (
    EditorBufferSnapshot,
    EditorIntent,
    EditorKeyStroke,
    EditorMode,
    MessageEditor,
)
from chrys.app.tui.widgets.editor.types import COMMON_EDITOR_INTENTS, EditorStatus
from chrys.foundation.i18n import MessageDef, MessageRef, msg

if TYPE_CHECKING:
    from pathlib import Path

    from textual import events
    from textual.app import ComposeResult

    from chrys.app.tui.widgets.editor.types import EditorLocation
    from chrys.foundation.text.draft_references import DraftReference


_UNSAVED_WARNING = msg(
    "tui.editor.unsaved_warning",
    fallback="Unsaved changes · Esc again discards · Ctrl+O/Ctrl+Enter commits",
)
_INPUT_UNAVAILABLE_WARNING = msg("tui.editor.input_unavailable", fallback="Input unavailable")
_DRAFT_CHANGED_WARNING = msg(
    "tui.editor.draft_changed_warning",
    fallback="Chat draft changed — Commit again to overwrite",
)
_EDITOR_TITLE = msg("tui.editor.title", fallback="Prompt Editor")
_MODE_LABEL = msg("tui.editor.mode_label", fallback="Mode:")
_STATUS_LABEL = msg("tui.editor.status_label", fallback="Status:")
_MODE_STANDARD = msg("tui.editor.mode.standard", fallback="Standard")
_MODE_EMACS = msg("tui.editor.mode.emacs", fallback="Emacs")
_MODE_VIM = msg("tui.editor.mode.vim", fallback="Vim")
_COMMIT = msg("tui.editor.button.commit", fallback="Commit")
_DISCARD = msg("tui.editor.button.discard", fallback="Discard")
_DISCARD_TITLE = msg("tui.editor.discard_title", fallback="Discard prompt?")
_DISCARD_MESSAGE = msg(
    "tui.editor.discard_message",
    fallback="Close the editor without committing this content?",
)
_HINT_COMMIT = msg("tui.editor.hint.commit", fallback=" Commit ")
_HINT_DISCARD = msg("tui.editor.hint.discard", fallback=" Discard ")
_HINT_MODE = msg("tui.editor.hint.mode", fallback=" Mode")
_HINT_SELECT = msg("tui.editor.hint.select", fallback=" Select ")
_HINT_EDIT = msg("tui.editor.hint.edit", fallback=" Edit ")
_HINT_VISUAL = msg("tui.editor.hint.visual", fallback=" Visual ")
_HINT_COMMAND = msg("tui.editor.hint.command", fallback=" Command ")

_EDITOR_MODE_MESSAGES: dict[EditorMode, MessageDef] = {
    EditorMode.STANDARD: _MODE_STANDARD,
    EditorMode.EMACS: _MODE_EMACS,
    EditorMode.VIM: _MODE_VIM,
}
_EDITOR_MODE_SEQUENCE = (EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM)


def _build_editor_hint(mode: EditorMode, render_message: Callable[[MessageRef], str]) -> Text:
    """Build a scannable action guide with reverse-video keycaps."""
    if mode is EditorMode.STANDARD:
        return Text.assemble(
            (" Ctrl+O ", "reverse"),
            (" Ctrl+Enter ", "reverse"),
            render_message(_HINT_COMMIT.bind()),
            (" Esc ", "reverse"),
            render_message(_HINT_DISCARD.bind()),
            (" F3 ", "reverse"),
            render_message(_HINT_MODE.bind()),
        )
    if mode is EditorMode.EMACS:
        return Text.assemble(
            (" Ctrl+O ", "reverse"),
            render_message(_HINT_COMMIT.bind()),
            (" Esc ", "reverse"),
            render_message(_HINT_DISCARD.bind()),
            (" Ctrl+Space ", "reverse"),
            render_message(_HINT_SELECT.bind()),
            (" F3 ", "reverse"),
            render_message(_HINT_MODE.bind()),
        )
    return Text.assemble(
        (" i ", "reverse"),
        render_message(_HINT_EDIT.bind()),
        (" v ", "reverse"),
        render_message(_HINT_VISUAL.bind()),
        (" : ", "reverse"),
        render_message(_HINT_COMMAND.bind()),
        (" Ctrl+O ", "reverse"),
        (" ZZ ", "reverse"),
        render_message(_HINT_COMMIT.bind()),
        (" ZQ ", "reverse"),
        (" :q! ", "reverse"),
        render_message(_HINT_DISCARD.bind()).rstrip(),
    )


@dataclass(frozen=True, slots=True)
class EditorDialogResult:
    """Draft, cursor and keymap state returned on every deliberate dismissal."""

    accepted: bool
    draft: ChatDraft
    cursor_location: EditorLocation
    mode: EditorMode

    @property
    def text(self) -> str:
        """The draft's text."""
        return self.draft.text


class EditorDialog(BaseDialog[EditorDialogResult]):
    """Edit a prompt without mutating the underlying InputBar until Commit."""

    CSS_PATH = "editor.tcss"

    def __init__(
        self,
        snapshot: EditorBufferSnapshot,
        *,
        mode: EditorMode | str = EditorMode.STANDARD,
        can_commit: Callable[[], bool] = lambda: True,
        current_draft_revision: Callable[[], int] | None = None,
        draft: ChatDraft | None = None,
        paste_cwd: str = "",
        clipboard_image_dir: Path | None = None,
        open_reference: Callable[[DraftReference], None] | None = None,
    ) -> None:
        """Edit *draft* (the snapshot's text when none), opened at the snapshot's cursor.

        *paste_cwd* and *clipboard_image_dir* are the input box's, so a paste
        lands as it would there; *open_reference* shows what a clicked label
        stands for, as the input box does.
        """
        self._entry_snapshot = snapshot
        self._entry_draft = draft if draft is not None else ChatDraft(snapshot.text)
        self._paste_cwd = paste_cwd
        self._clipboard_image_dir = clipboard_image_dir
        self._open_reference = open_reference
        self._mode = EditorMode.parse(mode)
        self._can_commit = can_commit
        self._current_draft_revision = current_draft_revision or (lambda: snapshot.draft_revision)
        self._discard_armed = False
        self._overwrite_armed_revision: int | None = None
        self._editor_status: EditorStatus = ""
        self._editor_status_warning = False
        super().__init__(dismiss_on_backdrop=False)

    @property
    def discard_armed(self) -> bool:
        """Whether the next guarded Escape will discard the prompt."""
        return self._discard_armed

    def compose(self) -> ComposeResult:
        render_message = self._render_message
        with VerticalGroup(id="editor-dialog", classes=f"editor-mode-{self._mode.value}") as container:
            container.border_title = Text(render_message(_EDITOR_TITLE.bind()))
            container.border_subtitle = Text(render_message(_EDITOR_MODE_MESSAGES[self._mode].bind()))
            with HorizontalGroup(id="editor-header"):
                yield Static(Text(render_message(_MODE_LABEL.bind())), id="editor-mode-label")
                yield Select[str](
                    [
                        (Text(render_message(_MODE_STANDARD.bind())), EditorMode.STANDARD.value),
                        (Text(render_message(_MODE_EMACS.bind())), EditorMode.EMACS.value),
                        (Text(render_message(_MODE_VIM.bind())), EditorMode.VIM.value),
                    ],
                    value=self._mode.value,
                    allow_blank=False,
                    compact=True,
                    id="editor-mode",
                )
                yield Static(_build_editor_hint(self._mode, render_message), id="editor-hint")
            editor = DraftEditor(
                self._entry_draft,
                paste_cwd=self._paste_cwd,
                clipboard_image_dir=self._clipboard_image_dir,
                cursor_location=self._entry_snapshot.cursor_location,
                mode=self._mode,
                id="message-editor",
                compact=True,
                soft_wrap=True,
                show_line_numbers=False,
            )
            yield editor
            with HorizontalGroup(id="editor-actions"):
                yield Static(Text(render_message(_STATUS_LABEL.bind())), id="editor-status-label")
                yield Static(Text(self._render_status(editor.status_text)), id="editor-status")
                yield Button(Text(render_message(_COMMIT.bind())), id="editor-commit", variant="primary", flat=True)
                yield Button(Text(render_message(_DISCARD.bind())), id="editor-cancel", variant="error", flat=True)

    def on_mount(self) -> None:
        self._sync_narrow_layout(self.app.size.width)
        self.query_one(MessageEditor).focus()

    def on_resize(self, event: events.Resize) -> None:
        """Keep the editor chrome usable as the terminal width changes."""
        self._sync_narrow_layout(event.size.width)

    def on_key(self, event: events.Key) -> None:
        """Handle common commands when focus is on dialog chrome."""
        if self.query_one(MessageEditor).has_focus:
            return
        intent = COMMON_EDITOR_INTENTS.get(event.key)
        if event.key == "escape":
            editor = self.query_one(MessageEditor)
            result = editor.dispatch_keymap_stroke(EditorKeyStroke(event.key, event.character))
            if result.consumed:
                event.prevent_default()
                event.stop()
                if result.intent is not EditorIntent.NONE:
                    self._handle_intent(result.intent)
                self._refresh_status(
                    result.status or editor.status_text,
                    warning=result.warning if result.status else editor.status_warning,
                )
            return
        if intent is None:
            return
        event.prevent_default()
        event.stop()
        self._handle_intent(intent)

    @on(MessageEditor.IntentRequested)
    def _on_editor_intent_requested(self, event: MessageEditor.IntentRequested) -> None:
        event.prevent_default()
        event.stop()
        self._handle_intent(event.intent)

    @on(DraftTextArea.ReferenceClicked)
    def _on_reference_clicked(self, event: DraftTextArea.ReferenceClicked) -> None:
        event.stop()
        if self._open_reference is not None:
            self._open_reference(event.reference)

    @on(MessageEditor.EditActivity)
    def _on_editor_edit_activity(self, event: MessageEditor.EditActivity) -> None:
        event.stop()
        self._editor_status = ""
        self._editor_status_warning = False
        self._clear_discard_armed()
        self._clear_overwrite_armed()
        editor = self.query_one(MessageEditor)
        self._refresh_status(editor.status_text, warning=editor.status_warning)

    @on(MessageEditor.ModeChanged)
    def _on_editor_mode_changed(self, event: MessageEditor.ModeChanged) -> None:
        event.stop()
        self._mode = event.mode
        mode_select = self.query_one("#editor-mode", Select)
        if mode_select.value != event.mode.value:
            mode_select.value = event.mode.value
        self._set_border_subtitle(event.mode)
        self._set_hint(_build_editor_hint(event.mode, self._render_message))
        self._clear_discard_armed()
        editor = self.query_one(MessageEditor)
        self._refresh_status(editor.status_text, warning=editor.status_warning)

    @on(Select.Changed, "#editor-mode")
    def _on_mode_selected(self, event: Select.Changed) -> None:
        event.stop()
        if isinstance(event.value, str):
            self._set_mode(EditorMode.parse(event.value))

    @on(MessageEditor.StatusChanged)
    def _on_editor_status_changed(self, event: MessageEditor.StatusChanged) -> None:
        event.stop()
        editor = self.query_one(MessageEditor)
        self._refresh_status(
            event.status or editor.status_text, warning=event.warning if event.status else editor.status_warning
        )

    @on(Button.Pressed, "#editor-commit")
    def _on_commit_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()
        event.stop()
        self.commit()

    @on(Button.Pressed, "#editor-cancel")
    def _on_cancel_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()
        event.stop()
        self.request_discard()

    def request_discard(self) -> None:
        """Confirm the Discard button whenever the editor contains text."""
        if self.app.screen is not self or self.dismiss_requested:
            return
        if not self.query_one(MessageEditor).text:
            self.cancel_immediately()
            return
        self._clear_discard_armed()

        def decided(discard: bool | None) -> None:
            if not self.is_mounted or self.app.screen is not self or self.dismiss_requested:
                return
            if discard:
                self.cancel_immediately()
            else:
                self.query_one(MessageEditor).focus()

        self.app.push_screen(
            ConfirmDialog(
                title=_DISCARD_TITLE.bind(),
                message=_DISCARD_MESSAGE.bind(),
                confirm_label=_DISCARD.bind(),
                confirm_variant="error",
                locale_controller=widget_locale_controller(self),
            ),
            decided,
        )

    def commit(self) -> None:
        """Accept the editor text when the underlying InputBar is writable."""
        if not self._can_commit():
            warning = self._render_message(_INPUT_UNAVAILABLE_WARNING.bind())
            self._refresh_status(warning, warning=True)
            self.notify(warning, severity="warning", markup=False)
            return
        current_revision = self._current_draft_revision()
        if current_revision != self._entry_snapshot.draft_revision:
            if self._overwrite_armed_revision == current_revision:
                self._dismiss_with_result(accepted=True)
                return
            self._overwrite_armed_revision = current_revision
            self._refresh_status()
            return
        self._dismiss_with_result(accepted=True)

    def request_escape_cancel(self) -> None:
        """Cancel clean text, or require a second Escape for dirty text."""
        if not self.query_one(MessageEditor).editor_is_dirty:
            self.cancel_immediately()
            return
        if self._discard_armed:
            self.cancel_immediately()
            return
        self._discard_armed = True
        self._refresh_status()

    def cancel_immediately(self) -> None:
        """Dismiss without applying the editor text to the InputBar."""
        self._dismiss_with_result(accepted=False)

    def _handle_intent(self, intent: EditorIntent) -> None:
        if intent is EditorIntent.ACCEPT:
            self.commit()
        elif intent is EditorIntent.REQUEST_ESCAPE_CANCEL:
            self.request_escape_cancel()
        elif intent is EditorIntent.CANCEL_IMMEDIATE:
            self.cancel_immediately()
        elif intent is EditorIntent.CYCLE_MODE:
            self._cycle_mode()

    def _dismiss_with_result(self, *, accepted: bool) -> None:
        editor = self.query_one(DraftEditor)
        self.dismiss(
            EditorDialogResult(
                accepted=accepted, draft=editor.chat_draft(), cursor_location=editor.cursor_location, mode=self._mode
            )
        )

    def _clear_discard_armed(self) -> None:
        if not self._discard_armed:
            return
        self._discard_armed = False
        self._refresh_status()

    def _clear_overwrite_armed(self) -> None:
        if self._overwrite_armed_revision is None:
            return
        self._overwrite_armed_revision = None
        self._refresh_status()

    def _refresh_status(self, status: EditorStatus | None = None, *, warning: bool | None = None) -> None:
        """Render the highest-priority guard or live state in the status bar."""
        if status is not None:
            self._editor_status = status
            self._editor_status_warning = bool(warning)
        if self._discard_armed:
            value = self._render_message(_UNSAVED_WARNING.bind())
            is_warning = True
        elif self._overwrite_armed_revision is not None:
            value = self._render_message(_DRAFT_CHANGED_WARNING.bind())
            is_warning = True
        else:
            editor = self.query_one(MessageEditor)
            value = self._editor_status or editor.status_text
            is_warning = self._editor_status_warning if self._editor_status else editor.status_warning
        self._set_status(value, warning=is_warning)

    def _set_hint(self, value: Text) -> None:
        self.query_one("#editor-hint", Static).update(value, layout=False)

    def _set_status(self, value: EditorStatus, *, warning: bool) -> None:
        status = self.query_one("#editor-status", Static)
        status.update(Text(self._render_status(value)), layout=False)
        status.set_class(warning, "editor-status-warning")

    def _render_status(self, value: EditorStatus) -> str:
        return self._render_message(value) if isinstance(value, MessageRef) else value

    def _set_border_subtitle(self, mode: EditorMode) -> None:
        container = self.query_one("#editor-dialog", VerticalGroup)
        container.border_subtitle = Text(self._render_message(_EDITOR_MODE_MESSAGES[mode].bind()))
        container.update_classes(
            {f"editor-mode-{candidate.value}": candidate is mode for candidate in _EDITOR_MODE_SEQUENCE}
        )

    def _cycle_mode(self) -> None:
        current_index = _EDITOR_MODE_SEQUENCE.index(self._mode)
        self._set_mode(_EDITOR_MODE_SEQUENCE[(current_index + 1) % len(_EDITOR_MODE_SEQUENCE)], update_control=True)

    def _set_mode(self, mode: EditorMode, *, update_control: bool = False) -> None:
        if mode is self._mode:
            return
        self._mode = mode
        self.query_one(MessageEditor).set_mode(mode)
        if update_control:
            self.query_one("#editor-mode", Select).value = mode.value
        self._clear_discard_armed()
        self._clear_overwrite_armed()
        self._set_hint(_build_editor_hint(mode, self._render_message))
        editor = self.query_one(MessageEditor)
        self._refresh_status(editor.status_text, warning=editor.status_warning)

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _sync_narrow_layout(self, width: int) -> None:
        self.query_one("#editor-dialog", VerticalGroup).set_class(width < 70, "editor-narrow")
