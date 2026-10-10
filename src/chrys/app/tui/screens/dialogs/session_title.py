# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session title editor — set or clear the session's custom title.

The dialog also names the session it edits and offers copying its id and
opening its folder, since the chat border's session id is what opens it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import events, on
from textual.containers import HorizontalGroup, VerticalGroup
from textual.message import Message
from textual.widgets import Button, Static

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.support.file_manager import can_open_in_file_manager, open_in_file_manager
from chrys.app.tui.widgets import DialogButtonRow, DialogButtonSpec, EnhancedInput
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.session_ids import session_short_id

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController


_HINT = msg(
    "tui.session_title.hint",
    fallback="A custom title for this session. Leave empty for the auto-generated title.",
)
_TITLE = msg("tui.session_title.title", fallback="Session Title")
_PLACEHOLDER = msg("tui.session_title.placeholder", fallback="Session title")
_SAVE = msg("tui.session_title.button.save", fallback="Save")
_CANCEL = msg("tui.session_title.button.cancel", fallback="Cancel")
_SESSION_LABEL = msg("tui.session_title.session_label", fallback="Session")
_COPY_ID = msg("tui.session_title.copy_id", fallback="Copy ID")
_OPEN_FOLDER = msg("tui.session_title.open_folder", fallback="Open folder")
_ID_COPIED = msg("tui.session_title.id_copied", fallback="Session ID copied")
_FOLDER_MISSING = msg(
    "tui.session_title.folder_missing",
    fallback="This session has no folder yet. It is created when the session is first saved.",
)
_OPEN_FAILED = msg("tui.session_title.open_failed", fallback="Could not open the session folder: {error}")


class _IdentityLink(ClickAffordance):
    """A link in the identity row; its label stays out of drag selections over the id."""

    ALLOW_SELECT = False


class _CopyIdLink(_IdentityLink):
    """The identity row's copy-id link."""

    class Clicked(Message):
        """Posted when the link is clicked."""

    CLICK_MESSAGE = Clicked


class _OpenFolderLink(_IdentityLink):
    """The identity row's open-folder link."""

    class Clicked(Message):
        """Posted when the link is clicked."""

    CLICK_MESSAGE = Clicked


class _IdentityRow(HorizontalGroup):
    """The session id with its links beside it, or below it when one line is too narrow.

    A horizontal group never wraps: it clips whatever overflows, so on a
    narrow terminal the links would be cut off or vanish entirely.
    """

    def on_resize(self, _event: events.Resize) -> None:
        self.set_class(self.size.width < self._single_line_width(), "-stacked")

    def _single_line_width(self) -> int:
        # Everything read here is the same stacked or not; a measurement that
        # shrank once stacked would unstack the row on the next Resize.
        id_group = self.query_one("#session-title-id-group", HorizontalGroup)
        links = self.query_one("#session-title-links", HorizontalGroup)
        width = id_group.styles.margin.width + links.styles.margin.width
        for widget in (*id_group.children, *links.children):
            width += widget.get_content_width(self.size, self.screen.size) + widget.styles.margin.width
        return width


class SessionTitleDialog(BaseDialog[str | None]):
    """Edit the current session's custom title.

    Dismisses with the new custom title (an empty string clears it back to
    the automatic title) or ``None`` when cancelled. Copying the id and
    opening the folder leave the dialog open; the open-folder link is shown
    only when there is a folder and a desktop file manager to show it in.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]

    CSS_PATH = "session_title.tcss"

    def __init__(
        self,
        *,
        session_id: str = "",
        session_folder: Path | None = None,
        custom_title: str = "",
        auto_title: str = "",
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._locale_controller = locale_controller
        self._session_id = session_short_id(session_id)
        self._session_folder = session_folder
        self._custom_title = custom_title
        self._auto_title = auto_title
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="session-title-container") as container:
            container.border_title = Text(self._render_message(_TITLE.bind()))
            with VerticalGroup(id="session-title-inner"):
                if self._session_id:
                    yield from self._compose_identity_row()
                yield Static(Text(self._render_message(_HINT.bind())), id="session-title-hint")
                yield EnhancedInput(
                    value=self._custom_title,
                    placeholder=self._auto_title or self._render_message(_PLACEHOLDER.bind()),
                    id="session-title-input",
                )
                yield DialogButtonRow(
                    DialogButtonSpec(
                        Text(self._render_message(_SAVE.bind())),
                        id="session-title-save",
                        variant="primary",
                    ),
                    DialogButtonSpec(
                        Text(self._render_message(_CANCEL.bind())),
                        id="session-title-cancel",
                        variant="warning",
                    ),
                    id="session-title-buttons",
                )

    def _compose_identity_row(self) -> ComposeResult:
        with _IdentityRow(id="session-title-identity"):
            with HorizontalGroup(id="session-title-id-group"):
                yield Static(Text(self._render_message(_SESSION_LABEL.bind())), id="session-title-id-label")
                yield Static(Text(self._session_id), id="session-title-id")
            with HorizontalGroup(id="session-title-links"):
                yield _CopyIdLink(
                    Text(self._render_message(_COPY_ID.bind())),
                    id="session-title-copy-id",
                    classes="session-title-link",
                )
                if self._session_folder is not None and can_open_in_file_manager():
                    yield _OpenFolderLink(
                        Text(self._render_message(_OPEN_FOLDER.bind())),
                        id="session-title-open-folder",
                        classes="session-title-link",
                    )

    def on_mount(self) -> None:
        self.query_one("#session-title-input", EnhancedInput).focus()

    @on(_CopyIdLink.Clicked)
    def _on_copy_id(self, _event: _CopyIdLink.Clicked) -> None:
        copy_text_to_clipboards(self.app, self._session_id)
        self.notify(
            self._render_message(_ID_COPIED.bind()),
            title=self._render_message(COPIED_TITLE.bind()),
            timeout=1,
            markup=False,
        )

    @on(_OpenFolderLink.Clicked)
    def _on_open_folder(self, _event: _OpenFolderLink.Clicked) -> None:
        folder = self._session_folder
        if folder is None:
            return
        if not folder.is_dir():
            self.notify(self._render_message(_FOLDER_MISSING.bind()), severity="warning", markup=False)
            return
        try:
            open_in_file_manager(folder)
        except OSError as error:
            # The OS error text is data, not markup: "[Errno 2] ..." would
            # otherwise be parsed as a content tag.
            self.notify(
                self._render_message(_OPEN_FAILED.bind(error=surrogate_safe_text(str(error)))),
                severity="error",
                markup=False,
            )

    @on(EnhancedInput.Submitted, "#session-title-input")
    def _on_input_submitted(self, event: EnhancedInput.Submitted) -> None:
        event.stop()
        self._submit()

    @on(Button.Pressed, "#session-title-save")
    def _on_save(self, _event: Button.Pressed) -> None:
        self._submit()

    @on(Button.Pressed, "#session-title-cancel")
    def _on_cancel(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        self.dismiss(self.query_one("#session-title-input", EnhancedInput).value.strip())

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)
