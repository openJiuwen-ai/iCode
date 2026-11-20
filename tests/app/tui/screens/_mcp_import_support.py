# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host app, fake sources and pane readers for the MCP import dialog's screen tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import App
from textual.widgets import Button

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportItem
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.foundation.config.settings import Settings
from chrys.service.mcp.importers import ImportContext, McpImportCandidate, McpImportResult
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from textual.app import ComposeResult


class PanelApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def __init__(self, panel: MCPConfigPanel) -> None:
        super().__init__()
        self.panel = panel
        self.notifications: list[str] = []

    def compose(self) -> ComposeResult:
        yield self.panel

    def notify(self, message: str, **kwargs: Any) -> None:  # type: ignore[override]
        self.notifications.append(message)


@dataclass
class FakeSource:
    """A source that returns a fixed result, without reading files."""

    source_id: str
    display_name: str
    result: McpImportResult

    def detect(self, ctx: ImportContext) -> bool:
        return True

    def discover(self, ctx: ImportContext) -> McpImportResult:
        return self.result


def stdio(name: str, command: str = "python", *args: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command=command, args=list(args), tool_name_prefix=name)


def candidate(config: MCPServerConfig, **fields: Any) -> McpImportCandidate:
    return McpImportCandidate(config.name, "/src/config.json", config, **fields)


async def open_dialog(app: PanelApp, pilot, sources: list[FakeSource]) -> ImportMcpServersDialog:
    dialog = ImportMcpServersDialog(
        workspace_cwd=None,
        existing=[row.config for row in app.panel._snapshot_rows(preserve_blank_names=True)],
        locale_controller=app.locale_controller,
        sources=sources,
        context=ImportContext(workspace_cwd=None, home=Path.cwd()),
    )
    app.push_screen(dialog, callback=app.panel._import_selected)
    await wait_for(
        lambda: isinstance(app.screen, ImportMcpServersDialog) and app.screen.is_mounted,
        pilot=pilot,
        description="import dialog is open",
    )
    return dialog


async def wait_for_items(dialog: ImportMcpServersDialog, pilot, count: int) -> list[_ImportItem]:
    await wait_for(
        lambda: len(dialog.query(_ImportItem)) == count and screen_is_settled(dialog.app, dialog),
        pilot=pilot,
        description=f"{count} servers are listed and composed",
    )
    return list(dialog.query(_ImportItem))


def notes_text(item: _ImportItem) -> str:
    """The notes a card that can't be imported shows, one line per note."""
    return "\n".join(str(note.render()) for note in item.card.query(".mcp-import-notes Static") if note.display)


def badge_text(item: _ImportItem) -> str:
    badge = item.card.badge
    return str(badge.render()) if badge.display else ""


async def ask_to_import(dialog: ImportMcpServersDialog, pilot) -> ConfirmDialog:
    """Press Import once it is enabled; returns the confirmation listing what would be added."""
    app = dialog.app
    confirm = dialog.query_one("#mcp-import-confirm", Button)
    await wait_for(lambda: not confirm.disabled, pilot=pilot, description="Import is enabled")
    confirm.press()
    await wait_for(
        lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
        pilot=pilot,
        description="import confirmation is open",
    )
    screen = app.screen
    assert isinstance(screen, ConfirmDialog)
    return screen


async def import_ticked(dialog: ImportMcpServersDialog, pilot) -> None:
    """Press Import and confirm it."""
    confirmation = await ask_to_import(dialog, pilot)
    confirmation.query_one("#confirm-yes", Button).press()
