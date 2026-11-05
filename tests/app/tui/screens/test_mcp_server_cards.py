# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP server cards fold to a three-row card titled by the server name, and ask before deleting a server."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Input, OptionList, Static, TabbedContent, TextArea
from textual.widgets.collapsible import CollapsibleTitle

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.mcp_picker import MCPPickerDialog
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, MCPConnectionCard
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.foundation.config.settings import Settings
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import MCPServerConfig, SubAgentRef, SubAgentsConfig, ToolsConfig
from tests.app.tui.screens._agent_config_support import (
    _DEFAULT_WAIT_TIMEOUT,
    _wait_for_active_screen,
    _wait_for_confirm_button,
    _wait_for_hydrated,
    make_profile,
    open_agent_config,
    press_and_answer_confirm,
    registry_with,
)
from tests.app.tui.screens._agent_config_support import _registry as _builtin_registry
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until

# The polling helpers in _agent_config_support share one 45s ceiling, below
# the repository's 60s default per-test timeout. The agent-config screen tests
# here keep the sibling modules' 120s override for expensive screen mounting
# before the first wait; once polling starts, the helper deadline still produces
# a clean AssertionError before the thread timeout can kill the xdist worker.
pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_chrys_config_dir")]


class _MCPPanelApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def __init__(self, panel: MCPConfigPanel) -> None:
        super().__init__()
        self._panel = panel

    def compose(self) -> ComposeResult:
        yield Static("placeholder")
        yield self._panel


def _servers(*names: str) -> list[MCPServerConfig]:
    return [MCPServerConfig(name=name, transport="stdio", command="python", args=[name]) for name in names]


def _title(card: MCPConnectionCard) -> str:
    return str(card.query_one(CollapsibleTitle).content)


def _folds(panel: MCPConfigPanel) -> list[bool]:
    return [card.collapsed for card in panel.query(MCPConnectionCard)]


def _capture_errors(screen: Screen) -> list[str]:
    """Shadow ``screen.notify`` and record the messages of its error notifications."""
    errors: list[str] = []

    def _notify(
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        if severity == "error":
            errors.append(message)

    screen.notify = _notify  # type: ignore[method-assign]
    return errors


def _mcp_agent_registry(*server_names: str):
    return registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with MCP servers",
            tools=ToolsConfig(mcp=_servers(*server_names)),
        ),
    )


async def _cards_laid_out(panel: MCPConfigPanel, pilot, count: int) -> list[MCPConnectionCard]:
    def ready() -> bool:
        cards = list(panel.query(MCPConnectionCard))
        return len(cards) == count and all(
            card.region.height > 0 and card.query(f"#mcp-name-{card.index}") for card in cards
        )

    await wait_for(ready, pilot=pilot, description=f"{count} MCP cards are mounted and laid out")
    return list(panel.query(MCPConnectionCard))


async def test_saved_servers_open_folded_to_three_rows_titled_by_name() -> None:
    seeded = _servers("github", "[/bold] literal", "filesystem")
    panel = MCPConfigPanel(seeded)
    async with _MCPPanelApp(panel).run_test(size=(120, 40)) as pilot:
        cards = await _cards_laid_out(panel, pilot, 3)

        assert [_title(card) for card in cards] == ["▶ github", "▶ [/bold] literal", "▶ filesystem"]
        # Border, name row, border: the name sits in the middle row, and one blank row separates cards.
        assert [card.region.height for card in cards] == [3, 3, 3]
        assert [card.region.y - cards[0].region.y for card in cards] == [0, 4, 8]
        assert all(card.query_one(CollapsibleTitle).region.y == card.region.y + 1 for card in cards)
        assert all(not card.query_one(".config-card-body").display for card in cards)
        # Folding hides the fields without unmounting them: the saved config reads back unchanged.
        assert panel.get_config() == seeded
        assert panel.validate() == []


async def test_added_server_opens_unfolded_and_rebuilds_keep_each_fold() -> None:
    panel = MCPConfigPanel(_servers("one", "two"))
    async with _MCPPanelApp(panel).run_test(size=(120, 40)) as pilot:
        cards = await _cards_laid_out(panel, pilot, 2)
        await click_when_settled(pilot, cards[1].query_one(CollapsibleTitle))
        await wait_for(lambda: _folds(panel) == [True, False], pilot=pilot, description="second card unfolded")

        panel.query_one("#mcp-add-btn", Button).press()
        cards = await _cards_laid_out(panel, pilot, 3)
        assert _folds(panel) == [False, True, False]
        assert [_title(card) for card in cards] == ["▼ New Server", "▶ one", "▼ two"]
        assert cards[0].query_one("#mcp-name-0", Input).region.height == 1

        await press_and_answer_confirm(pilot, cards[1].query_one("#mcp-delete-btn-1", Button))
        cards = await _cards_laid_out(panel, pilot, 2)
        assert _folds(panel) == [False, False]
        assert [_title(card) for card in cards] == ["▼ New Server", "▼ two"]


async def test_card_title_follows_the_server_name_input() -> None:
    panel = MCPConfigPanel([])
    async with _MCPPanelApp(panel).run_test(size=(120, 40)) as pilot:
        panel.query_one("#mcp-add-btn", Button).press()
        (card,) = await _cards_laid_out(panel, pilot, 1)
        name = card.query_one("#mcp-name-0", Input)

        name.value = "  [red]docs[/red]  "
        await wait_for(lambda: _title(card) == "▼ [red]docs[/red]", pilot=pilot, description="title shows the name")
        name.value = "   "
        await wait_for(lambda: _title(card) == "▼ Server 1", pilot=pilot, description="blank name falls back")


async def test_unnamed_servers_are_told_apart_by_position() -> None:
    panel = MCPConfigPanel(_servers("one", "two"))
    app = _MCPPanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _cards_laid_out(panel, pilot, 2)
        for card in cards:
            card.query_one(f"#mcp-name-{card.index}", Input).value = ""
        await wait_for(
            lambda: [_title(card) for card in cards] == ["▶ Server 1", "▶ Server 2"],
            pilot=pilot,
            description="unnamed cards are numbered",
        )

        dialog = await press_and_answer_confirm(pilot, cards[1].query_one("#mcp-delete-btn-1", Button), confirm=False)

        assert isinstance(dialog, ConfirmDialog)
        assert _dialog_text(dialog) == ("Delete MCP Server", 'Delete MCP server\n"Server 2"?')
        # Save's error names the card by the same label.
        assert cards[1].validate() == ["Server 2: name is required."]


async def test_folding_an_mcp_card_leaves_the_agent_draft_clean() -> None:
    registry = registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with one MCP server",
            tools=ToolsConfig(mcp=_servers("local")),
        ),
    )

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        (card,) = await _cards_laid_out(panel, pilot, 1)
        save = screen.query_one("#ac-save", Button)
        assert card.collapsed is True
        assert save.disabled is True

        await click_when_settled(pilot, card.query_one(CollapsibleTitle))
        await wait_for(
            lambda: card.query_one("#mcp-name-0", Input).region.height == 1,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="card unfolds in the agent config screen",
        )
        assert not await wait_until(lambda: screen._drafts[screen._selected_draft_key].dirty, pilot=pilot, timeout=0.5)
        assert save.disabled is True

        # The title sync must not swallow Input.Changed: renaming still dirties the draft.
        card.query_one("#mcp-name-0", Input).value = "renamed"
        await wait_for(
            lambda: not save.disabled and _title(card) == "▼ renamed",
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="rename retitles the card and enables Save",
        )
        assert screen._drafts[screen._selected_draft_key].dirty is True


def _dialog_text(dialog: ConfirmDialog) -> tuple[str, str]:
    title = dialog.query_one("#confirm-container").border_title
    return str(title), str(dialog.query_one("#confirm-message", Static).content)


async def test_deleting_an_mcp_server_asks_first_and_cancel_keeps_it() -> None:
    seeded = _servers("one", "[b]two[/b]")
    panel = MCPConfigPanel(seeded)
    app = _MCPPanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _cards_laid_out(panel, pilot, 2)

        dialog = await press_and_answer_confirm(pilot, cards[1].query_one("#mcp-delete-btn-1", Button), confirm=False)

        assert isinstance(dialog, ConfirmDialog)
        assert _dialog_text(dialog) == ("Delete MCP Server", 'Delete MCP server\n"[b]two[/b]"?')
        await wait_for(lambda: app.screen is not dialog, pilot=pilot, description="cancel closes the dialog")
        assert not await wait_until(lambda: list(panel.query(MCPConnectionCard)) != cards, pilot=pilot, timeout=0.5)
        assert panel.get_config() == seeded

        await press_and_answer_confirm(pilot, cards[1].query_one("#mcp-delete-btn-1", Button))
        (card,) = await _cards_laid_out(panel, pilot, 1)
        assert _title(card) == "▶ one"
        assert panel.get_config() == seeded[:1]


async def test_confirming_after_the_cards_were_rebuilt_deletes_nothing() -> None:
    seeded = _servers("one", "two")
    panel = MCPConfigPanel(seeded)
    app = _MCPPanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _cards_laid_out(panel, pilot, 2)
        cards[0].query_one("#mcp-delete-btn-0", Button).press()
        confirm = await _wait_for_confirm_button(app, pilot)
        dialog = app.screen

        # The confirmation names a card that a rebuild has since replaced.
        await panel._rebuild_cards()
        rebuilt = await _cards_laid_out(panel, pilot, 2)
        assert not any(card is rebuilt[0] for card in cards)
        confirm.press()

        await wait_for(lambda: app.screen is not dialog, pilot=pilot, description="confirm closes the dialog")
        assert not await wait_until(lambda: list(panel.query(MCPConnectionCard)) != rebuilt, pilot=pilot, timeout=0.5)
        assert panel.get_config() == seeded


async def test_mcp_server_delete_dirties_the_agent_draft_only_once_confirmed() -> None:
    registry = registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with one MCP server",
            tools=ToolsConfig(mcp=_servers("only")),
        ),
    )

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        # One server: deleting it re-mounts no field whose Changed event could dirty the draft instead.
        (card,) = await _cards_laid_out(panel, pilot, 1)
        save = screen.query_one("#ac-save", Button)
        await wait_for(
            lambda: save.disabled and not screen._drafts[screen._selected_draft_key].dirty,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="clean baseline after MCP tab hydration",
        )

        await press_and_answer_confirm(pilot, card.query_one("#mcp-delete-btn-0", Button), confirm=False)
        await wait_for(lambda: pilot.app.screen is screen, pilot=pilot, description="cancel returns to the screen")
        assert not await wait_until(
            lambda: not save.disabled or screen._drafts[screen._selected_draft_key].dirty,
            pilot=pilot,
            timeout=0.5,
        )

        await press_and_answer_confirm(pilot, card.query_one("#mcp-delete-btn-0", Button))
        # The screen marks the draft dirty at once and re-reads its profile
        # only after the next refresh: wait for the profile itself.
        await wait_for(
            lambda: (
                not panel.query(MCPConnectionCard)
                and not save.disabled
                and screen._drafts[screen._selected_draft_key].dirty
                and screen._drafts[screen._selected_draft_key].profile.tools.mcp == []
            ),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="confirmed delete removes the server from the draft and enables Save",
        )


async def test_string_yaml_timeout_reports_error_before_mcp_tab_opens_and_can_be_corrected(tmp_path: Path) -> None:
    path = tmp_path / "source.yaml"
    path.write_text(
        "name: Code\ndisplay_name: Code\ndescription: Coding agent\ninstructions: Help with code\ntools:\n  mcp:\n"
        '    - name: server\n      transport: stdio\n      command: server\n      timeout: "30"\n',
        encoding="utf-8",
    )
    registry = registry_with(load_profile_from_yaml(path))
    async with open_agent_config(registry, current_profile="Code", initial_tab="basic") as (screen, pilot):
        assert not screen.query(MCPConfigPanel)
        errors = _capture_errors(screen)
        save = screen.query_one("#ac-save", Button)
        screen.query_one("#bc-description", TextArea).text = "Edited description"
        await wait_for(lambda: not save.disabled, pilot=pilot, description="description enables Save")
        save.press()
        await wait_for(lambda: bool(errors), pilot=pilot, description="string timeout reports validation error")
        assert "timeout must be a positive integer" in errors[0]
        screen.query_one("#ac-tabs", TabbedContent).active = "mcp"
        await wait_for(
            lambda: bool(screen.query("#mcp-timeout-0")), pilot=pilot, description="timeout field opens for correction"
        )
        # The field parses the quoted YAML value as an integer, so it intentionally differs from the draft.
        await wait_for(lambda: not screen._hydrating, pilot=pilot, description="timeout editor is ready")
        screen.query_one("#mcp-timeout-0", Input).value = "45"
        save.press()
        await wait_for(lambda: save.disabled, pilot=pilot, description="corrected timeout saves")
        assert registry.get("Code").tools.mcp[0].request_timeout == 45


async def test_save_unfolds_the_cards_that_fail_validation() -> None:
    registry = _mcp_agent_registry("good", "bad")

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        good, bad = await _cards_laid_out(panel, pilot, 2)
        save = screen.query_one("#ac-save", Button)
        errors = _capture_errors(screen)

        # Folded, the card hides the field Save is about to reject.
        bad.query_one("#mcp-cmd-1", TextArea).text = ""
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )
        assert _folds(panel) == [True, True]

        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the validation error")
        assert "command" in errors[0].lower()
        assert (good.collapsed, bad.collapsed) == (True, False)
        assert bad.query_one("#mcp-cmd-1", TextArea).region.height > 0
        assert registry.get("mcp-agent").tools.mcp == _servers("good", "bad")


async def test_delete_request_from_a_replaced_card_asks_nothing() -> None:
    panel = MCPConfigPanel(_servers("one", "two"))
    app = _MCPPanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        (_one, two) = await _cards_laid_out(panel, pilot, 2)
        await panel._rebuild_cards()
        rebuilt = await _cards_laid_out(panel, pilot, 2)

        # A delete request queued before the rebuild names the old card, not whatever now sits at its index.
        panel.post_message(MCPConnectionCard.Removed(two))

        assert not await wait_until(lambda: isinstance(app.screen, ConfirmDialog), pilot=pilot, timeout=0.5)
        assert list(panel.query(MCPConnectionCard)) == rebuilt
        assert panel.get_config() == _servers("one", "two")


def _in_view(panel: MCPConfigPanel, card: MCPConnectionCard) -> bool:
    """Whether the card's top row shows in the panel's viewport (screen coordinates)."""
    viewport = panel.scrollable_content_region
    return viewport.y <= card.region.y < viewport.bottom


async def test_save_scrolls_to_the_first_card_that_fails_validation() -> None:
    registry = _mcp_agent_registry("zero", "one", "two", "three")

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        cards = await _cards_laid_out(panel, pilot, 4)
        save = screen.query_one("#ac-save", Button)
        errors = _capture_errors(screen)
        # An unfolded first card pushes the others below the fold.
        cards[0].set_collapsed(False, scroll_visible=False)
        await wait_for(
            lambda: cards[0].region.height > panel.scrollable_content_region.height,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the unfolded first card is taller than the panel",
        )
        for index in (1, 3):
            cards[index].query_one(f"#mcp-cmd-{index}", TextArea).text = ""
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )
        assert panel.scroll_y == 0
        assert not _in_view(panel, cards[1])

        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the validation error")
        assert _folds(panel) == [False, False, True, False]
        # The error lists the first invalid card first, so that is the one brought into view.
        await wait_for(
            lambda: _in_view(panel, cards[1]),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the first invalid card is scrolled into view",
        )
        assert not await wait_until(lambda: not _in_view(panel, cards[1]), pilot=pilot, timeout=0.5)


async def test_save_unfolds_a_card_that_only_the_saved_draft_check_rejects() -> None:
    registry = _mcp_agent_registry("one", "two")

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        one, two = await _cards_laid_out(panel, pilot, 2)
        save = screen.query_one("#ac-save", Button)
        errors = _capture_errors(screen)
        # The panel compares names with lower(), the saved-draft check with casefold(): only the second calls these a clash.
        one.query_one("#mcp-name-0", Input).value = "strasse"
        two.query_one("#mcp-name-1", Input).value = "Straße"
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )
        assert panel.validate() == []

        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the duplicate server name")
        assert "Straße" in errors[0]
        assert _folds(panel) == [True, False]
        assert registry.get("mcp-agent").tools.mcp == _servers("one", "two")


async def test_save_reports_its_error_when_a_card_cannot_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _mcp_agent_registry("one")

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        (card,) = await _cards_laid_out(panel, pilot, 1)
        save = screen.query_one("#ac-save", Button)
        errors = _capture_errors(screen)
        card.query_one("#mcp-cmd-0", TextArea).text = ""
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )

        def _broken_validate(self: MCPConnectionCard) -> list[str]:
            raise RuntimeError("card cannot validate")

        # Save skips a panel that can't validate, so the saved-draft check reports the error, and
        # unfolding the rejected card asks that card to validate again.
        monkeypatch.setattr(MCPConnectionCard, "validate", _broken_validate)
        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the saved-draft error")
        assert "command" in errors[0].lower()
        assert pilot.app.is_running
        assert registry.get("mcp-agent").tools.mcp == _servers("one")


async def test_test_request_from_a_replaced_card_runs_nothing() -> None:
    panel = MCPConfigPanel(_servers("one", "two"))
    app = _MCPPanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        (_one, two) = await _cards_laid_out(panel, pilot, 2)
        await panel._rebuild_cards()
        rebuilt = await _cards_laid_out(panel, pilot, 2)

        # A test request queued before the rebuild names the old card, not whatever now sits at its index.
        panel.post_message(MCPConnectionCard.TestRequested(two))

        assert not await wait_until(lambda: isinstance(app.screen, ConnectionTestDialog), pilot=pilot, timeout=0.5)
        assert panel._testing == set()

        # The same request from the mounted card runs; a blank command fails validation before any connection.
        rebuilt[1].query_one("#mcp-cmd-1", TextArea).text = ""
        panel.post_message(MCPConnectionCard.TestRequested(rebuilt[1]))
        await wait_for(
            lambda: isinstance(app.screen, ConnectionTestDialog),
            pilot=pilot,
            description="the mounted card's test request opens the test dialog",
        )


async def test_deleting_a_focused_card_keeps_the_scroll_position() -> None:
    panel = MCPConfigPanel(_servers("zero", "one", "two", "three"))
    app = _MCPPanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _cards_laid_out(panel, pilot, 4)
        for card in cards:
            card.set_collapsed(False, scroll_visible=False)
        await wait_for(
            lambda: all(card.region.height > panel.scrollable_content_region.height for card in cards),
            pilot=pilot,
            description="every card unfolds taller than the panel",
        )
        panel.scroll_to(y=cards[2].virtual_region.y, animate=False)
        await wait_for(lambda: _in_view(panel, cards[2]), pilot=pilot, description="the third card is at the top")
        before = panel.scroll_y
        delete = cards[2].query_one("#mcp-delete-btn-2", Button)
        app.set_focus(delete, scroll_visible=False)

        await press_and_answer_confirm(pilot, delete)
        await _cards_laid_out(panel, pilot, 3)

        # Textual would focus a neighbour of the removed ✕ and animate the scroll to it.
        await wait_for(lambda: app.focused is panel, pilot=pilot, description="the panel keeps the keyboard")
        assert not await wait_until(lambda: panel.scroll_y != before, pilot=pilot, timeout=0.5)


def _two_agent_registry(mcp_servers: list[MCPServerConfig], *, sub_agents: SubAgentsConfig | None = None):
    return registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with MCP servers",
            tools=ToolsConfig(mcp=mcp_servers),
            sub_agents=sub_agents or SubAgentsConfig(),
        ),
        make_profile("other-agent", display_name="Other Agent", description="Agent without MCP servers"),
    )


def _post_agent_selection(screen, draft_key: str) -> None:
    option_list = screen.query_one("#ac-list", OptionList)
    index = option_list.get_option_index(draft_key)
    option_list.post_message(OptionList.OptionSelected(option_list, option_list.get_option_at_index(index), index))


async def _select_agent(screen, pilot, draft_key: str) -> None:
    _post_agent_selection(screen, draft_key)
    await wait_for(
        lambda: screen._selected_draft_key == draft_key,
        pilot=pilot,
        timeout=_DEFAULT_WAIT_TIMEOUT,
        description=f"agent {draft_key} is selected",
    )
    await _wait_for_hydrated(screen, pilot)


async def test_unsaved_server_stays_unfolded_after_switching_agents() -> None:
    registry = _two_agent_registry(_servers("saved"))

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 1)
        panel.query_one("#mcp-add-btn", Button).press()
        added, _saved = await _cards_laid_out(panel, pilot, 2)
        # Leaving an agent validates its draft, so the added server needs a command.
        added.query_one("#mcp-cmd-0", TextArea).text = "python server.py"
        await wait_for(
            lambda: screen._drafts[screen._selected_draft_key].dirty or not panel.validate(),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the added server is valid",
        )
        mcp_key = screen._selected_draft_key
        other_key = screen._find_visible_draft_key("other-agent")
        assert other_key is not None

        await _select_agent(screen, pilot, other_key)
        await _select_agent(screen, pilot, mcp_key)

        panel = screen._live_panel(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 2)
        assert _folds(panel) == [False, True]
        assert [server.name for server in panel.get_config()] == ["New Server", "saved"]


async def _add_valid_server(screen, pilot) -> MCPConnectionCard:
    panel = screen._live_panel(MCPConfigPanel)
    count = len(panel.card_folds())
    panel.query_one("#mcp-add-btn", Button).press()
    await wait_for(
        lambda: (
            len(panel.query(MCPConnectionCard)) == count + 1
            or (isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted)
        ),
        pilot=pilot,
        description="Add opens the picker or inserts a blank card",
    )
    if isinstance(pilot.app.screen, MCPPickerDialog):
        options = pilot.app.screen.query_one(OptionList)
        option = options.get_option("action:create")
        options.post_message(OptionList.OptionSelected(options, option, options.get_option_index("action:create")))
    added, *_ = await _cards_laid_out(panel, pilot, count + 1)
    added.query_one("#mcp-cmd-0", TextArea).text = "python server.py"
    save = screen.query_one("#ac-save", Button)
    await wait_for(lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled")
    return added


async def _save_and_wait(screen, pilot, saved: Callable[[], bool]) -> None:
    screen.query_one("#ac-save", Button).press()
    await wait_for(saved, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save wrote the registry")
    await _wait_for_hydrated(screen, pilot)


async def test_save_keeps_each_card_folded_or_unfolded() -> None:
    registry = _two_agent_registry(_servers("one", "two"))

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        _one, two = await _cards_laid_out(panel, pilot, 2)
        await click_when_settled(pilot, two.query_one(CollapsibleTitle))
        await _add_valid_server(screen, pilot)
        assert _folds(panel) == [False, True, False]

        await _save_and_wait(screen, pilot, lambda: len(registry.get("mcp-agent").tools.mcp) == 3)

        panel = screen._live_panel(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 3)
        assert [server.name for server in panel.get_config()] == ["New Server", "one", "two"]
        assert _folds(panel) == [False, True, False]

        # An agent left before the Save keeps its folds too.
        mcp_key = screen._selected_draft_key
        other_key = screen._find_visible_draft_key("other-agent")
        assert other_key is not None
        await _select_agent(screen, pilot, other_key)
        await _add_valid_server(screen, pilot)
        await _save_and_wait(screen, pilot, lambda: len(registry.get("other-agent").tools.mcp) == 1)
        await _select_agent(screen, pilot, mcp_key)

        panel = screen._live_panel(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 3)
        assert _folds(panel) == [False, True, False]


# An empty command fails validation; an unclosed quote fails earlier, when the panels are read.
@pytest.mark.parametrize("command", ["", 'python "unclosed'], ids=["empty-command", "unclosed-quote"])
async def test_leaving_an_agent_unfolds_the_card_that_blocks_it(command: str) -> None:
    registry = _two_agent_registry(_servers("good", "bad"))

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        good, bad = await _cards_laid_out(panel, pilot, 2)
        errors = _capture_errors(screen)
        mcp_key = screen._selected_draft_key
        other_key = screen._find_visible_draft_key("other-agent")
        assert other_key is not None
        bad.query_one("#mcp-cmd-1", TextArea).text = command

        _post_agent_selection(screen, other_key)

        await wait_for(lambda: errors, pilot=pilot, description="leaving reports the validation error")
        assert "command" in errors[0].lower()
        assert screen._selected_draft_key == mcp_key
        assert (good.collapsed, bad.collapsed) == (True, False)


async def test_reset_keeps_each_card_folded_or_unfolded() -> None:
    registry = _builtin_registry()
    code = registry.get_builtin_template("Code")
    assert code is not None
    customized = copy.deepcopy(code)
    customized.tools.mcp = _servers("one", "two")
    registry.register(customized)

    async with open_agent_config(registry, current_profile="Code", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        _one, two = await _cards_laid_out(panel, pilot, 2)
        await click_when_settled(pilot, two.query_one(CollapsibleTitle))
        await _add_valid_server(screen, pilot)

        # Reset keeps the MCP servers, so it keeps their cards' folds too.
        await press_and_answer_confirm(pilot, screen.query_one("#ac-reset", Button))
        await _wait_for_active_screen(pilot.app, pilot, screen)
        await _wait_for_hydrated(screen, pilot)

        panel = screen._live_panel(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 3)
        assert [server.name for server in panel.get_config()] == ["New Server", "one", "two"]
        assert _folds(panel) == [False, True, False]


async def test_clone_opens_the_copy_cards_as_the_original_shows_them() -> None:
    registry = _two_agent_registry(_servers("one", "two"))

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        _one, two = await _cards_laid_out(panel, pilot, 2)
        await click_when_settled(pilot, two.query_one(CollapsibleTitle))
        await _add_valid_server(screen, pilot)
        original_key = screen._selected_draft_key

        screen.query_one("#ac-clone", Button).press()
        await wait_for(
            lambda: screen._selected_draft_key != original_key,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the copy is selected",
        )
        await _wait_for_hydrated(screen, pilot)

        panel = screen._live_panel(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 3)
        assert [server.name for server in panel.get_config()] == ["New Server", "one", "two"]
        assert _folds(panel) == [False, True, False]


async def test_save_failing_on_another_agent_unfolds_no_card() -> None:
    # This agent is clean, so the saved-draft check skips it; alone it would reject the second name.
    registry = _two_agent_registry(_servers("strasse", "Straße"))

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 2)
        save = screen.query_one("#ac-save", Button)
        errors = _capture_errors(screen)
        other_key = screen._find_visible_draft_key("other-agent")
        assert other_key is not None
        other = screen._drafts[other_key]
        other.profile.display_name = ""
        other.dirty = True
        screen._update_save_button_state()
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )

        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the other agent's error")
        assert "Straße" not in errors[0]
        assert not await wait_until(lambda: _folds(panel) != [True, True], pilot=pilot, timeout=0.5)


async def test_save_unfolds_a_card_of_the_agent_a_rename_retargeted() -> None:
    # Renaming the sub-agent it calls makes this agent's draft part of the saved-draft check.
    registry = _two_agent_registry(
        _servers("strasse", "Straße"), sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="other-agent")])
    )

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        await _cards_laid_out(panel, pilot, 2)
        save = screen.query_one("#ac-save", Button)
        errors = _capture_errors(screen)
        other_key = screen._find_visible_draft_key("other-agent")
        assert other_key is not None
        other = screen._drafts[other_key]
        other.profile.name = "renamed-agent"
        other.dirty = True
        screen._update_save_button_state()
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )

        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the duplicate server name")
        assert "Straße" in errors[0]
        assert _folds(panel) == [True, False]
        # The failed Save puts the drafts back as they were, this one clean again.
        assert screen._drafts[screen._selected_draft_key].dirty is False
