# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared path-entry card primitive for configuration panels."""

from __future__ import annotations

import contextlib
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from textual import on
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Label

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets import Checkbox, ConfigActionButton
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets.config_card import ConfigCard
from chrys.foundation.i18n import MessageDef, msg

if TYPE_CHECKING:
    from textual.app import ComposeResult

_BROWSE = msg("tui.agent_config.path_entry.browse", fallback="Browse")
WORKSPACE_RELATIVE = msg("tui.agent_config.path_entry.workspace_relative", fallback="Workspace relative")


class PathEntryCard(ConfigCard):
    """Base card for one configurable path row.

    Subclasses supply ID/class prefixes and preview/browse behavior. The
    generated widget IDs stay in the owning panel's namespace so existing
    tests and CSS can remain panel-specific.
    """

    _title_label: MessageDef
    _id_prefix: str = ""
    _style_prefix: str = ""
    _placeholder_relative_posix: MessageDef
    _placeholder_relative_windows: MessageDef
    _placeholder_absolute_macos: MessageDef
    _placeholder_absolute_linux: MessageDef
    _placeholder_absolute_windows: MessageDef
    _supports_file_path_mode: bool = True

    def __init__(
        self,
        path: str,
        index: int,
        *,
        workspace_cwd: str | None = None,
        read_only: bool = False,
    ) -> None:
        self._delete_button_prefix = f"{self._style_prefix}-delete-btn"
        self._path = path
        self._index = index
        self._workspace_cwd = workspace_cwd
        self._is_relative = self._infer_relative(path) if self._supports_file_path_mode else True
        super().__init__(index=index, read_only=read_only)

    @property
    def _path_input_id(self) -> str:
        return f"{self._id_prefix}-path-{self._index}"

    @property
    def _browse_button_id(self) -> str:
        return f"{self._id_prefix}-browse-{self._index}"

    @property
    def _relative_checkbox_id(self) -> str:
        return f"{self._id_prefix}-rel-{self._index}"

    @property
    def _preview_id(self) -> str:
        return f"{self._id_prefix}-preview-{self._index}"

    @staticmethod
    def _infer_relative(path: str) -> bool:
        """Infer checkbox state from the saved path string."""
        from chrys.foundation.platform.paths import is_absolute_path

        p = path.strip()
        if not p:
            return False
        if p.startswith("~"):
            return False
        return not is_absolute_path(p)

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        yield from self.compose_header(
            f"[red]*[/red] {escape(render_str(localizer, self._title_label.bind()))}",
            row_class=f"{self._style_prefix}-header-row",
            title_class=f"{self._style_prefix}-title",
        )

        with Vertical(classes=f"{self._style_prefix}-field"):
            with Horizontal(classes=f"{self._style_prefix}-path-row"):
                path_input = Input(
                    value=self._path,
                    placeholder=self._placeholder(),
                    id=self._path_input_id,
                )
                path_input.disabled = self._read_only
                yield path_input
                if self._supports_file_path_mode:
                    browse = ConfigActionButton(
                        render_str(localizer, _BROWSE.bind()),
                        id=self._browse_button_id,
                        classes=f"{self._style_prefix}-browse-btn",
                    )
                    browse.disabled = self._read_only
                    browse.display = not self._read_only and not self._is_relative
                    yield browse
            if self._supports_file_path_mode:
                relative = Checkbox(
                    render_str(localizer, WORKSPACE_RELATIVE.bind()),
                    value=self._is_relative,
                    id=self._relative_checkbox_id,
                    classes=f"{self._style_prefix}-rel-cb",
                )
                relative.disabled = self._read_only
                yield relative
                yield Label("", id=self._preview_id, classes=f"{self._style_prefix}-preview")

    def on_mount(self) -> None:
        # Removal can begin during compose. Textual then skips child mounting
        # but still dispatches Mount; the discarded card has no preview to update.
        if self._pruning:
            return
        self._apply_read_only_state()
        if self._supports_file_path_mode:
            self._refresh_preview()

    def _apply_read_only_state(self) -> None:
        if not self._read_only:
            return
        for input_widget in self.query(Input):
            input_widget.disabled = True
        for checkbox in self.query(Checkbox):
            checkbox.disabled = True
        for button in self.query(Button):
            button.disabled = True
            button.display = False

    def _placeholder(self) -> str:
        from chrys.foundation.platform import get_platform

        platform_info = get_platform()
        placeholder: MessageDef
        if self._supports_file_path_mode and not self._is_relative:
            if platform_info.is_windows:
                placeholder = self._placeholder_absolute_windows
            elif platform_info.is_macos:
                placeholder = self._placeholder_absolute_macos
            else:
                placeholder = self._placeholder_absolute_linux
        else:
            placeholder = (
                self._placeholder_relative_windows if platform_info.is_windows else self._placeholder_relative_posix
            )
        return render_str(widget_localizer(self), placeholder.bind())

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        if self._read_only:
            return
        btn = event.button
        if self._supports_file_path_mode and btn.id == self._browse_button_id:
            self._open_browse()

    @on(Checkbox.Changed)
    def _on_rel_changed(self, event: Checkbox.Changed) -> None:
        if not self._supports_file_path_mode or event.checkbox.id != self._relative_checkbox_id:
            return
        if self._read_only:
            self._apply_read_only_state()
            return
        self._is_relative = event.value
        browse = self.query_one(f"#{self._browse_button_id}", Button)
        browse.display = not event.value
        path_input = self.query_one(f"#{self._path_input_id}", Input)
        path_input.placeholder = self._placeholder()
        self._refresh_preview()

    @on(Input.Changed)
    def _on_path_changed(self, event: Input.Changed) -> None:
        if self._supports_file_path_mode and event.input.id == self._path_input_id:
            self._refresh_preview()

    @staticmethod
    def _resolve_for_preview(path: Path) -> tuple[Path, bool]:
        try:
            return path.resolve(), False
        except OSError:
            return path, True

    def get_path(self) -> str:
        with contextlib.suppress(NoMatches):
            return self.query_one(f"#{self._path_input_id}", Input).value.strip()
        return self._path.strip()

    def is_relative_checked(self) -> bool:
        if not self._supports_file_path_mode:
            return True
        with contextlib.suppress(NoMatches):
            return bool(self.query_one(f"#{self._relative_checkbox_id}", Checkbox).value)
        return self._is_relative

    @abstractmethod
    def _removed_message(self) -> Message:
        """Build the owning panel's remove message."""

    @abstractmethod
    def _refresh_preview(self) -> None:
        """Refresh the subclass-specific resolved-path preview."""

    @abstractmethod
    def _open_browse(self) -> None:
        """Open the subclass-specific file picker."""
