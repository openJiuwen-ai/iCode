# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace picker using the agent picker's rows and model picker's separators."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

from rich.cells import cell_len
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events, on
from textual.containers import VerticalGroup
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from chrys.app.tui import workspace_text as labels
from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.paths import middle_cut_path
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.util.workspace_order import WorkspaceOrder, read_workspace_order
from chrys.app.tui.widgets.fitted_prompt import FittedPrompt
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.abandonable import run_abandonable
from chrys.service.workspaces.paths import missing_folders

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.css.styles import RulesMap

    from chrys.foundation.models.workspace import Workspace
    from chrys.service.workspaces.registry import WorkspaceRegistry, WorkspaceRegistryError


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspacePick:
    action: Literal["select", "create", "edit", "delete"]
    workspace_id: str | None = None
    workspace: Workspace | None = None


class _WorkspaceOptions(OptionList):
    """Treat the painted actions as part of a row, with their own click targets."""

    def __init__(self, row_action: Callable[[Literal["edit", "delete"], str], None]) -> None:
        super().__init__()
        self._row_action = row_action
        self._tooltip_action: str | None = None

    def on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        action = meta.get("workspace_action")
        workspace_id = meta.get("workspace_id")
        if event.button == 1 and action in ("edit", "delete") and isinstance(workspace_id, str):
            event.prevent_default()
            event.stop()
            self._row_action(action, workspace_id)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        action = event.style.meta.get("workspace_action")
        hint = {"edit": labels.EDIT, "delete": labels.DELETE}.get(action)
        if hint is None:
            if self._tooltip_action is not None:
                self._tooltip_action = None
                self.tooltip = None
            return
        if action == self._tooltip_action:
            return
        moving_between_actions = self._tooltip_action is not None
        self._tooltip_action = action
        self.tooltip = Text(render_str(widget_localizer(self), hint.bind()))
        if moving_between_actions:
            # Textual hides a visible tooltip on motion within a widget without rearming its timer.
            self.screen._handle_tooltip_timer(self)

    def on_leave(self, _event: events.Leave) -> None:
        self._tooltip_action = None
        self.tooltip = None


def _highlight_in_view(options: OptionList) -> bool:
    """Whether any line of the highlighted row is painted; reads Textual's private line map."""
    index = options.highlighted
    if index is None or (top := options._index_to_line.get(index)) is None:
        return False
    first = options.scroll_offset.y
    return top < first + options.scrollable_content_region.height and top + options._heights[index] > first


class _WorkspacePrompt(FittedPrompt):
    """Measure the two fitted lines without rendering every row during layout."""

    def __init__(self, build: Callable[[int], Text], minimum_width: int) -> None:
        super().__init__(build)
        self._minimum_width = minimum_width

    def get_height(self, rules: RulesMap, width: int) -> int:
        return 2 if width >= self._minimum_width else super().get_height(rules, width)


class WorkspacesScreen(BaseDialog[WorkspacePick | None]):
    """Pick a workspace or an action on one of its definitions."""

    BINDINGS: ClassVar[list] = [localized_binding("escape", "dismiss", CLOSE_BINDING)]
    DEFAULT_CSS = """
    WorkspacesScreen { align: center middle; }
    WorkspacesScreen > #container {
        width: 60; max-width: 90%; max-height: 90%; height: auto;
        background: $surface; border: round $tui-border-primary $border-opacity;
        border-title-align: left; border-title-color: $primary;
        padding: 0; overflow-x: hidden;
    }
    WorkspacesScreen > #container > OptionList {
        height: auto; max-height: 100%; border: none; padding: 0 0 0 1; scrollbar-size: 1 1;
    }
    """

    def __init__(
        self,
        registry: WorkspaceRegistry,
        cwd: str,
        current: Workspace | None,
        *,
        highlight: str | None = None,
        order: WorkspaceOrder | None = None,
        on_read: Callable[[WorkspaceOrder], None] | None = None,
    ) -> None:
        self._order = order
        self._shown: WorkspaceOrder | None = None
        self._on_read = on_read
        self._registry = registry
        self._cwd = cwd
        self._current_id = current.id if current is not None else "unspecified"
        self._workspaces: dict[str, Workspace] = {}
        self._primary_missing: set[str] = set()
        self._error: WorkspaceRegistryError | None = None
        # The row to highlight once listed; only the user's own move changes it, not a fallback while it is missing.
        self._wanted_id = highlight or self._current_id
        self._highlighted_id = self._wanted_id
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="container") as container:
            container.border_title = Text(render_str(localizer, labels.WORKSPACES.bind()))
            yield _WorkspaceOptions(self._row_action)

    def _options(self) -> list[Option | None]:
        localizer = widget_localizer(self)
        rows: list[Option | None] = []
        error = self._error
        if error is not None:
            rows.extend((Option(Text(render_str(localizer, error.display_message)), id="error", disabled=True), None))
        else:
            for workspace in self._workspaces.values():
                rows.extend((Option(self._fitted_workspace_prompt(workspace), id=workspace.id), None))
        unspecified = self._current_id == "unspecified"
        row = Text(style=self._current_style("unspecified"))
        row.append(("◦ " if unspecified else "") + render_str(localizer, labels.UNSPECIFIED.bind()), style="bold")
        row.append("\n" + render_str(localizer, labels.NO_WORKSPACE.bind()))
        rows.append(Option(row, id="unspecified"))
        if error is None:
            rows.extend((None, Option(Text(render_str(localizer, labels.NEW.bind()), style="bold"), id="create")))
        return rows

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self.query_one(OptionList).focus()
        cached = self._order
        # The main screen's last read for this chat paints the first frame; the fresh read below replaces it.
        if cached is not None and (cached.cwd, cached.current_id) == (self._cwd, self._current_key):
            self._show(cached)
        self.call_after_refresh(self._start_loading)

    @property
    def _current_key(self) -> str | None:
        return None if self._current_id == "unspecified" else self._current_id

    def _start_loading(self) -> None:
        if self._composed_live and not self.dismiss_requested:
            self.run_worker(self._load_rows(), exit_on_error=False)

    async def _load_rows(self) -> None:
        order = await run_abandonable(
            "icode-workspace-order",
            read_workspace_order,
            self._registry,
            self._cwd,
            self._current_key,
            self._order,
        )
        if not self._composed_live or self.dismiss_requested:
            return
        if self._on_read is not None:
            self._on_read(order)
        if order is not self._shown:
            self._show(order)
        workspaces = order.workspaces
        if not workspaces:
            return
        try:
            missing = await run_abandonable(
                "icode-workspace-folders", missing_folders, tuple(workspace.primary_cwd for workspace in workspaces)
            )
        except Exception:
            logger.warning("Could not probe workspace primary folders", exc_info=True)
            return
        # Each opening owns its own worker and rows, including when returning from an edit.
        if not self._composed_live or self.dismiss_requested:
            return
        self._primary_missing = missing
        options = self.query_one(OptionList)
        # A longer note can add a line to rows above the highlight; keep it in view only if the user had not scrolled away.
        in_view = _highlight_in_view(options)
        for workspace in workspaces:
            if workspace.primary_cwd in missing:
                options.replace_option_prompt(workspace.id, self._fitted_workspace_prompt(workspace))
        if missing and in_view:
            self.call_after_refresh(self._scroll_to_highlight)

    def _show(self, order: WorkspaceOrder) -> None:
        self._shown = order
        self._workspaces = {workspace.id: workspace for workspace in order.workspaces}
        self._error = order.snapshot.error
        options = self.query_one(OptionList)
        rows = self._options()
        selectable = {row.id for row in rows if row is not None and not row.disabled}
        self._highlighted_id = next(
            (item for item in (self._wanted_id, self._current_id) if item in selectable), "unspecified"
        )
        options.set_options(rows)
        options.highlighted = options.get_option_index(self._highlighted_id)
        self.call_after_refresh(self._scroll_to_highlight)

    def _fitted_workspace_prompt(self, workspace: Workspace) -> FittedPrompt:
        localizer = widget_localizer(self)
        current = workspace.id == self._current_id
        name = ("◦ " if current else "") + sanitize_legacy_scalar(surrogate_safe_text(workspace.name))
        primary = sanitize_legacy_scalar(surrogate_safe_text(workspace.primary_cwd))
        edit = render_str(localizer, labels.EDIT.bind())
        delete = render_str(localizer, labels.DELETE.bind())
        action_width = cell_len(edit) + cell_len(delete) + 4
        actions = Text.assemble(
            "  ",
            (edit, RichStyle.from_meta({"workspace_action": "edit", "workspace_id": workspace.id})),
            "  ",
            (delete, RichStyle.from_meta({"workspace_action": "delete", "workspace_id": workspace.id})),
        )
        count = len(workspace.working_dirs)
        missing = workspace.primary_cwd in self._primary_missing
        if missing:
            suffix = render_str(localizer, labels.PRIMARY_MISSING.bind())
        else:
            suffix = render_str(localizer, labels.REFERENCE_FOLDER_COUNT.bind(count=count)) if count else ""
        suffix_width = cell_len(suffix) + 3 if suffix else 0

        def prompt(width: int) -> Text:
            current_style = self._current_style(workspace.id)
            title = Text(name, style=current_style)
            title.truncate(max(0, width - action_width), overflow="ellipsis", pad=True)
            row = Text()
            row.append_text(title)
            row.stylize("bold")
            row.append_text(actions)
            detail_width = max(1, width - suffix_width)
            detail = Text(middle_cut_path(primary, detail_width), style=current_style)
            detail.truncate(detail_width, overflow="ellipsis", pad=bool(suffix))
            row.append("\n")
            row.append_text(detail)
            if suffix:
                row.append(" · ", style=current_style)
                warning = (
                    rich_style_from_textual_color(self.app.theme_variables["warning"]) if missing else current_style
                )
                row.append(suffix, style=warning)
            return row

        return _WorkspacePrompt(prompt, max(action_width + 1, suffix_width + 1))

    def _current_style(self, workspace_id: str) -> RichStyle:
        if workspace_id == self._current_id and workspace_id != self._highlighted_id:
            return rich_style_from_textual_color(self.app.theme_variables["text-muted"])
        return RichStyle()

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not self._composed_live or self.dismiss_requested:
            return
        highlighted = event.option.id
        if highlighted is None:
            return
        if highlighted != self._highlighted_id:
            self._wanted_id = highlighted
        current_id = self._current_id
        was_current = self._highlighted_id == current_id
        self._highlighted_id = highlighted
        if was_current == (highlighted == current_id):
            return
        if current_id != "unspecified" and current_id not in self._workspaces:
            return
        options = self.query_one(OptionList)
        if current_id in self._workspaces:
            options.replace_option_prompt(current_id, self._fitted_workspace_prompt(self._workspaces[current_id]))
        else:
            prompt = options.get_option(current_id).prompt
            if not isinstance(prompt, Text):
                return
            prompt = prompt.copy()
            prompt.style = self._current_style(current_id)
            options.replace_option_prompt(current_id, prompt)

    @on(OptionList.OptionSelected)
    def _select(self, event: OptionList.OptionSelected) -> None:
        if not self._composed_live or self.dismiss_requested or self.app.screen is not self:
            return
        option_id = event.option.id
        if not option_id or option_id == "error":
            return
        if option_id == "create":
            self.dismiss(WorkspacePick("create"))
        else:
            self.dismiss(
                WorkspacePick(
                    "select",
                    None if option_id == "unspecified" else option_id,
                    workspace=self._workspaces.get(option_id),
                )
            )

    def _row_action(self, action: Literal["edit", "delete"], workspace_id: str) -> None:
        if not self._composed_live or self.dismiss_requested or self.app.screen is not self:
            return
        self.dismiss(WorkspacePick(action, workspace_id))

    def on_resize(self) -> None:
        self.call_after_refresh(self._scroll_to_highlight)

    def _scroll_to_highlight(self) -> None:
        if self._composed_live and not self.dismiss_requested and self.app.screen is self:
            self.query_one(OptionList).scroll_to_highlight()
