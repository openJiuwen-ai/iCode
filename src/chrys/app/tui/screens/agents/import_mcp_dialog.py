# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dialog that imports MCP servers configured in other applications.

The left pane lists the applications with MCP configuration on this machine;
the right pane lists the highlighted one's servers, each as the read-only card
the MCP tab will show after import, with its warnings and source file.
Each application shows how many of its servers are ticked; Import lists the
ticked servers by application for confirmation, then returns them for the MCP
tab to add as unsaved cards.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.compose import compose
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.agents.import_mcp_view import (
    imported_config,
    names_field,
    problem_text,
    source_error_text,
)
from chrys.app.tui.screens.agents.mcp_choices import (
    MCPConflict,
    MCPConflictKind,
    comparable_mcp_config,
    mcp_choice_conflict,
)
from chrys.app.tui.screens.agents.mcp_picker import conflict_message
from chrys.app.tui.screens.agents.panels.mcp import MCPConnectionCard
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.util.removal import remove_children_shielded
from chrys.app.tui.widgets import Checkbox, DialogButtonRow, DialogButtonSpec, HatchedEmptyState
from chrys.app.tui.widgets.config_card import CollapsibleConfigCard
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.service.mcp.importers import IMPORT_SOURCES, ImportContext, detected_sources
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.app import ComposeResult
    from textual.widget import Widget

    from chrys.app.tui.i18n import LocaleController
    from chrys.service.mcp.importers import McpImportCandidate, McpImportResult, McpImportSource

logger = logging.getLogger(__name__)

_TITLE = msg("tui.mcp.import.title", fallback="Import from Other Apps")
_IMPORT = msg("tui.mcp.import.button.import", fallback="Import")
_CANCEL = msg("tui.mcp.import.button.cancel", fallback="Cancel")
_UNAVAILABLE = msg("tui.mcp.import.status.unavailable", fallback="Can't import")
_DUPLICATE = msg("tui.mcp.import.status.duplicate", fallback="Duplicate")
# Short forms of the "+ Add" picker's reasons, which the badge's tooltip gives in full.
_NAME_IN_USE = msg("tui.mcp.import.status.name_in_use", fallback="Name in use")
_CONNECTION_IN_USE = msg("tui.mcp.import.status.connection_in_use", fallback="Connection in use")
_NOTES = msg("tui.mcp.import.notes", fallback="Import Notes")
_TICKED_CONFLICT = msg(
    "tui.mcp.import.note.conflict",
    fallback="Conflicts with the selected {name} ({agent}); only one of them can be imported.",
)
_SOURCE = msg("tui.mcp.import.source", fallback="Configuration source: {path}")
_DETECTING = msg("tui.mcp.import.detecting", fallback="Looking for applications...")
_LOADING = msg("tui.mcp.import.loading", fallback="Reading MCP servers...")
_DETECT_FAILED = msg(
    "tui.mcp.import.detect_failed",
    fallback="Couldn't search for MCP configurations. Reopen import to try again. If it still fails, see the User Guide (F8) to find {app}'s debug.log.",
)
_READ_FAILED = msg(
    "tui.mcp.import.read_failed",
    fallback="Couldn't load MCP configurations from {agent}. Reopen import to try again. If it still fails, see the User Guide (F8) to find {app}'s debug.log.",
)
_NO_AGENTS = msg(
    "tui.mcp.import.no_agents", fallback="No MCP configuration found for supported applications: {agents}."
)
_AGENT_SEPARATOR = msg("tui.mcp.import.agent_separator", fallback=", ")
_EMPTY_SOURCE = msg("tui.mcp.import.empty_source", fallback="No MCP servers found.")
_TICKED_COUNT = msg("tui.mcp.import.ticked_count", fallback="{agent} ({ticked})")
_CONFIRM_TITLE = msg("tui.mcp.import.confirm.title", fallback="Import MCP Servers")
_CONFIRM_MESSAGE = msg(
    "tui.mcp.import.confirm.message",
    fallback="Import {count} MCP server configuration:",
    plural_fallback="Import {count} MCP server configurations:",
)
_BACK = msg("tui.mcp.import.confirm.back", fallback="Back")

_SHORT_CONFLICT = {MCPConflictKind.NAME: _NAME_IN_USE, MCPConflictKind.CONNECTION: _CONNECTION_IN_USE}

type _State = Literal["importable", "unsupported", "conflict"]
_STATE_ORDER: dict[_State, int] = {"importable": 0, "conflict": 2, "unsupported": 3}


def _agent_conflict(imported: MCPServerConfig, existing: Sequence[MCPServerConfig]) -> MCPConflict | None:
    """How *imported* clashes with the agent's servers, as the "+ Add" picker judges it, but for the description.

    The import writes the description itself, in the language of the moment,
    and the user may edit it afterwards: it is not a server setting, so a
    server differing only there was already added.
    """
    conflict = mcp_choice_conflict(imported, existing)
    if conflict is None or conflict.kind is not MCPConflictKind.NAME:
        return conflict
    target = comparable_mcp_config(imported)
    for config in existing:
        if comparable_mcp_config(replace(config, description=imported.description)) == target:
            return MCPConflict(MCPConflictKind.ADDED, config.name)
    return conflict


def _mark_open(card: CollapsibleConfigCard) -> None:
    """Mark the card's item while the card is open, so its tick follows the title down a line."""
    if card.parent is not None:
        card.parent.set_class(not card.collapsed, "-open")


class _PreviewCard(MCPConnectionCard):
    """The read-only card a server becomes, its description included, with its status in the header."""

    def __init__(self, config: MCPServerConfig, index: int, *, badge: Static, collapsed: bool) -> None:
        self.badge = badge
        super().__init__(config, index, read_only=True, collapsed=collapsed)

    def compose(self) -> ComposeResult:
        # A folded card mounts its header alone: the fields are most of a card's widgets, and
        # a pane of servers is rebuilt on every application switch. They mount on first unfold.
        yield from self.compose_title_row()
        if not self.collapsed:
            yield from self.compose_fields()

    def compose_header_extras(self) -> ComposeResult:
        yield self.badge

    def set_collapsed(self, collapsed: bool, *, scroll_visible: bool = True) -> None:
        if not collapsed and self._body is None and self._title_widget is not None:
            # Composed folded, so without fields; card_body() records them, so this happens once.
            self.mount(*compose(self, self.compose_fields()))
        super().set_collapsed(collapsed, scroll_visible=scroll_visible)
        _mark_open(self)


class _RefusedCard(CollapsibleConfigCard):
    """A server that can't be imported: its name, then why not and where it is configured."""

    _delete_button_prefix = "mcp-import-refused-delete"

    def __init__(
        self, title: str, index: int, *, badge: Static, notes_title: str, notes: list[Widget], collapsed: bool
    ) -> None:
        self._title = title
        self.badge = badge
        self._notes_title = notes_title
        self._notes = notes
        super().__init__(index, collapsed=collapsed, read_only=True)

    def compose(self) -> ComposeResult:
        yield from self.compose_header(self._title, row_class="mcp-header-row", title_class="mcp-title")
        with self.card_body(), Vertical(classes="mcp-import-notes") as notes:
            notes.border_title = self._notes_title
            yield from self._notes

    def compose_header_extras(self) -> ComposeResult:
        yield self.badge

    def set_collapsed(self, collapsed: bool, *, scroll_visible: bool = True) -> None:
        super().set_collapsed(collapsed, scroll_visible=scroll_visible)
        _mark_open(self)


class _ImportItem(Horizontal):
    """One server: its tick, then its card."""

    def __init__(
        self,
        source: McpImportSource,
        index: int,
        *,
        imported: MCPServerConfig | None,
        conflict: MCPConflict | None,
        tick: Checkbox,
        card: _PreviewCard | _RefusedCard,
    ) -> None:
        super().__init__(classes="mcp-import-item" if card.collapsed else "mcp-import-item -open")
        self.source = source
        self.index = index
        # The server as it would be imported, and its conflict with a server the agent already has.
        self.imported = imported
        self.conflict = conflict
        self.tick = tick
        self.card = card

    def compose(self) -> ComposeResult:
        yield self.tick
        yield self.card


class ImportMcpServersDialog(BaseDialog[list[MCPServerConfig] | None]):
    """Pick MCP servers from other applications; dismisses with the ticked servers' configs, or None."""

    BINDINGS: ClassVar[list] = [localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True)]

    CSS_PATH = "import_mcp_dialog.tcss"

    def __init__(
        self,
        *,
        workspace_cwd: str | None,
        existing: Sequence[MCPServerConfig],
        locale_controller: LocaleController | None,
        sources: Sequence[McpImportSource] | None = None,
        context: ImportContext | None = None,
    ) -> None:
        self._locale_controller = locale_controller
        self._existing = list(existing)
        self._import_context = context if context is not None else ImportContext.current(workspace_cwd)
        self._all_sources = tuple(IMPORT_SOURCES if sources is None else sources)
        self._sources: list[McpImportSource] = []
        self._results: dict[str, McpImportResult | None] = {}
        self._load_locks: dict[str, asyncio.Lock] = {}
        self._ticked: dict[str, set[int]] = {}
        # Folds the user changed, so a pane rebuild (application or locale switch) keeps them.
        self._folded: dict[str, dict[int, bool]] = {}
        self._shown_source: str | None = None
        # The sidebar's prompt per application, so a tick redraws only the one that changed.
        self._source_prompts: dict[str, Content] = {}
        self._pane_message: tuple[MessageRef, bool] | None = None
        # The application whose servers the pane shows, or None while it shows a message.
        self._pane_source: str | None = None
        # Each pane change takes a generation; a read finishing after a newer change was asked for is dropped.
        self._generation = 0
        self._pane_lock = asyncio.Lock()
        super().__init__()

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-import-container") as container:
            container.border_title = Text(self._render_message(_TITLE.bind()))
            with Horizontal(id="mcp-import-body"):
                with Vertical(id="mcp-import-sidebar") as sidebar:
                    sidebar.display = False
                    yield OptionList(id="mcp-import-sources")
                with Vertical(id="mcp-import-right"):
                    yield VerticalScroll(id="mcp-import-servers")
                    yield DialogButtonRow(
                        DialogButtonSpec(
                            Text(self._render_message(_IMPORT.bind())), id="mcp-import-confirm", disabled=True
                        ),
                        DialogButtonSpec(
                            Text(self._render_message(_CANCEL.bind())), id="mcp-import-cancel", variant="warning"
                        ),
                        id="mcp-import-buttons",
                    )

    async def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        await self._show_message(_DETECTING.bind(), self._generation)
        self.run_worker(self._detect(), group="mcp-import-detect", exclusive=True, exit_on_error=False)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self.query_one("#mcp-import-container").border_title = Text(self._render_message(_TITLE.bind()))
        self.query_one("#mcp-import-confirm", Button).label = Text(self._render_message(_IMPORT.bind()))
        self.query_one("#mcp-import-cancel", Button).label = Text(self._render_message(_CANCEL.bind()))
        self._sync_source_counts()
        if self._shown_source is not None:
            self._request_source(self._shown_source)
        elif self._pane_message is not None:
            reference, error = self._pane_message
            self.run_worker(
                self._show_message_safely(reference, self._generation, error=error),
                group="mcp-import-pane",
                exit_on_error=False,
            )

    async def _detect(self) -> None:
        # Nothing awaits this worker and it doesn't exit the app on an error: say so, or the pane spins forever.
        try:
            await self._list_sources()
        except Exception:
            logger.exception("Listing applications to import MCP servers from failed")
            await self._show_message_safely(_DETECT_FAILED.bind(app=APP_DISPLAY_NAME), self._generation, error=True)

    async def _list_sources(self) -> None:
        self._sources = await asyncio.to_thread(detected_sources, self._import_context, self._all_sources)
        if not self._sources:
            separator = self._render_message(_AGENT_SEPARATOR.bind())
            agents = separator.join(source.display_name for source in self._all_sources)
            await self._show_message(_NO_AGENTS.bind(agents=agents), self._generation)
            return
        option_list = self.query_one("#mcp-import-sources", OptionList)
        options: list[Option | None] = []
        for position, source in enumerate(self._sources):
            if position:
                options.append(None)
            prompt = self._source_prompt(source)
            self._source_prompts[source.source_id] = prompt
            options.append(Option(prompt, id=source.source_id))
        option_list.add_options(options)
        self.query_one("#mcp-import-sidebar").display = True
        option_list.highlighted = 0
        option_list.focus()

    @on(OptionList.OptionHighlighted, "#mcp-import-sources")
    def _on_source_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None and event.option.id != self._shown_source:
            self._request_source(event.option.id)

    def _request_source(self, source_id: str) -> None:
        # Source and generation change here, not in the worker: a highlight landing before it starts compares to them.
        self._generation += 1
        self._shown_source = source_id
        self.run_worker(self._show_source(source_id, self._generation), group="mcp-import-pane", exit_on_error=False)

    async def _show_source(self, source_id: str, generation: int) -> None:
        source = next((item for item in self._sources if item.source_id == source_id), None)
        if source is None:
            return
        try:
            if source_id not in self._results:
                await self._show_message(_LOADING.bind(), generation)
                # Pane rebuilds share one result, so ticked indices keep naming the same servers.
                async with self._load_locks.setdefault(source_id, asyncio.Lock()):
                    if source_id not in self._results:
                        try:
                            result = await asyncio.to_thread(source.discover, self._import_context)
                        except Exception:
                            logger.exception("Reading MCP servers from %s failed", source.display_name)
                            result = None
                        self._results[source_id] = result
            async with self._pane_lock:
                if generation == self._generation:
                    await self._mount_pane(self._pane_widgets(source, self._results[source_id]), source_id)
                    self._pane_message = None
        except Exception:
            logger.exception("Showing the MCP servers of %s failed", source.display_name)
            await self._show_message_safely(
                _READ_FAILED.bind(agent=source.display_name, app=APP_DISPLAY_NAME), generation, error=True
            )

    async def _show_message(self, reference: MessageRef, generation: int, *, error: bool = False) -> None:
        async with self._pane_lock:
            if generation == self._generation:
                classes = "mcp-import-error" if error else "mcp-import-message"
                await self._mount_pane([Static(Text(self._render_message(reference)), classes=classes)])
                self._pane_message = (reference, error)

    async def _show_message_safely(self, reference: MessageRef, generation: int, *, error: bool = False) -> None:
        try:
            await self._show_message(reference, generation, error=error)
        except Exception:
            logger.exception("Showing a message in the MCP import dialog failed")

    async def _mount_pane(self, widgets: list[Widget], source_id: str | None = None) -> None:
        """Replace the right pane with *source_id*'s servers, or a message when ``None``.

        Callers hold ``_pane_lock`` so two rebuilds never interleave.
        """
        pane = self.query_one("#mcp-import-servers", VerticalScroll)
        for item in pane.query(_ImportItem):
            self._folded.setdefault(item.source.source_id, {})[item.index] = item.card.collapsed
        with self.app.batch_update():
            await remove_children_shielded(pane)
            await pane.mount(*widgets)
            if source_id is None or source_id != self._pane_source:
                # Another application's list starts at its top; rebuilding the same one keeps the place.
                pane.scroll_home(animate=False)
        self._pane_source = source_id

    def _pane_widgets(self, source: McpImportSource, result: McpImportResult | None) -> list[Widget]:
        agent = source.display_name
        if result is None:
            message = _READ_FAILED.bind(agent=agent, app=APP_DISPLAY_NAME)
            return [Static(Text(self._render_message(message)), classes="mcp-import-error")]
        widgets: list[Widget] = [
            Static(Text(self._render_message(source_error_text(error, agent))), classes="mcp-import-error")
            for error in result.source_errors
        ]
        ticked = self._ticked.setdefault(source.source_id, set())
        items = [self._item(source, index, candidate) for index, candidate in enumerate(result.candidates)]
        for item in items:
            if self._state(item) != "importable":
                ticked.discard(item.index)
        for item in items:
            item.tick.value = item.index in ticked
            self._update_status(item)
        ordered: list[_ImportItem] = sorted(items, key=self._order)
        widgets.extend(ordered)
        if not result.candidates and not result.source_errors:
            widgets.append(HatchedEmptyState(self._render_message(_EMPTY_SOURCE.bind()), classes="mcp-import-empty"))
        self._sync_import_button()
        return widgets

    def _imported(self, source: McpImportSource, candidate: McpImportCandidate) -> MCPServerConfig | None:
        """The server as Import adds it: what conflicts are judged on, as the "+ Add" picker judges them."""
        return imported_config(candidate, source.display_name, self._render_message)

    def _item(self, source: McpImportSource, index: int, candidate: McpImportCandidate) -> _ImportItem:
        imported = self._imported(source, candidate)
        badge = Static(classes="mcp-import-badge")
        collapsed = self._folded.get(source.source_id, {}).get(index, True)
        card: _PreviewCard | _RefusedCard
        if imported is None:
            reason = self._note(problem_text(candidate, source.display_name), "mcp-import-problem")
            field = [self._note_text(candidate.problem_field, "mcp-import-problem mcp-import-detail")]
            path = sanitize_legacy_scalar(self._display_path(candidate.source_path))
            where = self._note(_SOURCE.bind(path=path), "mcp-import-source")
            notes: list[Widget] = [reason, *(field if names_field(candidate) else []), where]
            card = _RefusedCard(
                candidate.name,
                index,
                badge=badge,
                notes_title=self._render_message(_NOTES.bind()),
                notes=notes,
                collapsed=collapsed,
            )
        else:
            card = _PreviewCard(imported, index, badge=badge, collapsed=collapsed)
        conflict = None if imported is None else _agent_conflict(imported, self._existing)
        tick = Checkbox("", classes="mcp-import-tick")
        return _ImportItem(source, index, imported=imported, conflict=conflict, tick=tick, card=card)

    def _note(self, reference: MessageRef, classes: str) -> Static:
        return Static(Text(self._render_message(reference)), classes=classes)

    def _note_text(self, text: str, classes: str) -> Static:
        return Static(Text(sanitize_legacy_scalar(text)), classes=classes)

    def _display_path(self, path: str) -> str:
        home = str(self._import_context.home)
        return "~" + path[len(home) :] if path == home or path.startswith(home + os.sep) else path

    def _order(self, item: _ImportItem) -> tuple[int, str, str]:
        """Each group by name: importable, clashing with a ticked server, clashing with the agent's, can't import.

        The order is set when the pane is built, so a tick never moves the cards under the pointer.
        """
        name = self._candidate(item).name
        state = self._state(item)
        rank = 1 if state == "importable" and self._ticked_conflict(item) is not None else _STATE_ORDER[state]
        return rank, name.casefold(), name

    def _state(self, item: _ImportItem) -> _State:
        if item.imported is None:
            return "unsupported"
        return "conflict" if item.conflict is not None else "importable"

    def _update_status(self, item: _ImportItem) -> None:
        state = self._state(item)
        ticked_conflict = self._ticked_conflict(item) if state == "importable" else None
        badge = item.card.badge
        item.tick.disabled = state != "importable" or ticked_conflict is not None
        # A server that can't be ticked shows no tick and a muted name; its badge says why.
        item.set_class(item.tick.disabled, "-unavailable")
        warnings = self._candidate(item).warnings
        detail = ticked_conflict
        if state == "unsupported":
            label, style = self._render_message(_UNAVAILABLE.bind()), "-status"
        elif item.conflict is not None:
            label = self._render_message(conflict_message(item.conflict))
            if short := _SHORT_CONFLICT.get(item.conflict.kind):
                label, detail = self._render_message(short.bind()), label
            style = "-status"
        elif ticked_conflict is not None:
            label, style = self._render_message(_DUPLICATE.bind()), "-status"
        elif warnings:
            label, style = f"⚠ {len(warnings)}", "-warning"
        else:
            label, style = "", ""
        badge.tooltip = None if detail is None else Text(detail)
        badge.update(Text(label))
        badge.set_classes(f"mcp-import-badge {style}".strip())
        badge.display = bool(label)

    def _candidate(self, item: _ImportItem) -> McpImportCandidate:
        result = self._results[item.source.source_id]
        if result is None:
            raise LookupError(f"{item.source.display_name} has no servers listed")
        return result.candidates[item.index]

    def _ticked_conflict(self, item: _ImportItem) -> str | None:
        """Why a server a ticked one from any application conflicts with can't be ticked too.

        A ticked server is never locked, so ticking stays reversible.
        """
        if item.imported is None or item.index in self._ticked.get(item.source.source_id, set()):
            return None
        for source in self._sources:
            result = self._results.get(source.source_id)
            if result is None:
                continue
            for other_index in sorted(self._ticked.get(source.source_id, set())):
                other = result.candidates[other_index]
                other_config = self._imported(source, other)
                if other_config is not None and mcp_choice_conflict(item.imported, [other_config]) is not None:
                    name = sanitize_legacy_scalar(other.name)
                    return self._render_message(_TICKED_CONFLICT.bind(name=name, agent=source.display_name))
        return None

    @on(Checkbox.Changed, ".mcp-import-tick")
    def _on_tick_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        item = next((node for node in event.checkbox.ancestors if isinstance(node, _ImportItem)), None)
        if item is None or self._state(item) != "importable":
            return
        ticked = self._ticked.setdefault(item.source.source_id, set())
        if event.value:
            ticked.add(item.index)
        else:
            ticked.discard(item.index)
        for other in self.query(_ImportItem):
            if other is not item:
                self._update_status(other)
        self._sync_import_button()

    def _sync_import_button(self) -> None:
        self.query_one("#mcp-import-confirm", Button).disabled = not any(self._ticked.values())
        self._sync_source_counts()

    def _source_prompt(self, source: McpImportSource) -> Content:
        count = len(self._ticked.get(source.source_id, ()))
        label = self._render_message(_TICKED_COUNT.bind(agent=source.display_name, ticked=count))
        return Content.styled(label if count else source.display_name, "bold")

    def _sync_source_counts(self) -> None:
        option_list = self.query_one("#mcp-import-sources", OptionList)
        for source in self._sources:
            prompt = self._source_prompt(source)
            if self._source_prompts.get(source.source_id) != prompt:
                self._source_prompts[source.source_id] = prompt
                option_list.replace_option_prompt(source.source_id, prompt)

    def _selected(self) -> list[tuple[McpImportSource, MCPServerConfig]]:
        selected: list[tuple[McpImportSource, MCPServerConfig]] = []
        for source in self._sources:
            result = self._results.get(source.source_id)
            ticked = self._ticked.get(source.source_id, set())
            for index, candidate in enumerate(result.candidates if result else ()):
                config = self._imported(source, candidate) if index in ticked else None
                taken = [*self._existing, *(chosen for _source, chosen in selected)]
                # Ticks lock conflicting servers; this still keeps the first of two ticked at once.
                if config is not None and mcp_choice_conflict(config, taken) is None:
                    selected.append((source, config))
        return selected

    def _confirmation(self, selected: list[tuple[McpImportSource, MCPServerConfig]]) -> Text:
        """The servers Import adds, grouped under their application as the sidebar lists them, one per line."""
        text = Text(self._render_message(_CONFIRM_MESSAGE.bind(count=len(selected))))
        for source in self._sources:
            names = sorted(
                (sanitize_legacy_scalar(config.name) for chosen, config in selected if chosen is source),
                key=lambda name: (name.casefold(), name),
            )
            if names:
                heading = _TICKED_COUNT.bind(agent=source.display_name, ticked=len(names))
                text.append("\n\n")
                text.append(self._render_message(heading), style="bold")
                text.append("".join(f"\n  {name}" for name in names))
        return text

    @on(Button.Pressed, "#mcp-import-confirm")
    def _on_import(self, event: Button.Pressed) -> None:
        event.stop()
        selected = self._selected()
        # A second press queued behind the first would stack a second confirmation.
        if not selected or self is not self.app.screen:
            return
        confirm = ConfirmDialog(
            _CONFIRM_TITLE.bind(),
            self._confirmation(selected),
            _IMPORT.bind(),
            _BACK.bind(),
            locale_controller=self._locale_controller,
            message_align="left",
        )

        def _confirmed(confirmed: bool | None) -> None:
            # Back keeps the ticks: this dialog stays open under the confirmation.
            if confirmed and self.is_attached and self is self.app.screen:
                self.dismiss([config for _source, config in selected])

        self.app.push_screen(confirm, callback=_confirmed)

    @on(Button.Pressed, "#mcp-import-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _default_dismiss_result(self) -> list[MCPServerConfig] | None:  # type: ignore[override]
        return None
