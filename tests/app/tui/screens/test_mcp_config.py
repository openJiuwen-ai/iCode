# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the MCP configuration panel."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.panels.markdown_report import NOT_ADVERTISED
from chrys.app.tui.screens.agents.panels.mcp import (
    _CONNECTION_TIMED_OUT,
    _HEADER_NAME_PLACEHOLDER,
    _HEADER_VALUE_PLACEHOLDER,
    _PROMPTS_EXPOSED,
    _TOOLS_EXPOSED,
    MCPConfigPanel,
    MCPConnectionCard,
    _format_mcp_report,
)
from chrys.app.tui.screens.dialogs.connection_test import _MCP_SERVER, ConnectionTestDialog
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import DisplayBlock, Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.util.env_templates import EnvVarResolutionError
from chrys.service.mcp.adapter import (
    MCPAdapter,
    MCPConnectionError,
    MCPToolNameCollisionError,
    MCPToolNameValidationError,
)
from tests.app.tui.screens._agent_config_support import press_and_answer_confirm
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


def test_connection_subject_and_failure_keep_english_and_localize_chinese() -> None:
    assert ConnectionTestDialog()._subject_label == _MCP_SERVER.bind()
    assert format_message(_MCP_SERVER.bind()) == "MCP Server"
    assert format_message(_CONNECTION_TIMED_OUT.bind()) == (
        "Connection test timed out. Please check server and timeout settings."
    )
    chinese = Localizer("zh-Hans")
    assert chinese.render(_MCP_SERVER.bind()) == "MCP 服务器"
    assert chinese.render(_CONNECTION_TIMED_OUT.bind()) == "连接测试超时。请检查服务器和超时设置。"


def test_mcp_report_plurals_keep_english_and_localize_chinese() -> None:
    one_tool = _TOOLS_EXPOSED.bind(count=1, catalog=DisplayBlock("- one"))
    two_tools = _TOOLS_EXPOSED.bind(count=2, catalog=DisplayBlock("- one\n- two"))
    one_prompt = _PROMPTS_EXPOSED.bind(count=1, catalog=DisplayBlock("- greet"))

    assert format_message(one_tool) == "1 tool available to the model.\n\n- one"
    assert format_message(two_tools) == "2 tools available to the model.\n\n- one\n- two"
    assert format_message(one_prompt) == "1 prompt template loaded as a tool.\n\n- greet"

    chinese = Localizer("zh-Hans")
    assert chinese.render(one_tool) == "已向模型提供 1 个工具。\n\n- one"
    assert chinese.render(two_tools) == "已向模型提供 2 个工具。\n\n- one\n- two"
    assert chinese.render(NOT_ADVERTISED.bind()) == "*未声明。*"


def test_mcp_header_placeholders_show_usable_examples() -> None:
    assert format_message(_HEADER_NAME_PLACEHOLDER.bind()) == "e.g. X-Auth-Token"
    assert format_message(_HEADER_VALUE_PLACEHOLDER.bind()) == "e.g. {{AUTH_TOKEN}}"

    chinese = Localizer("zh-Hans")
    assert chinese.render(_HEADER_NAME_PLACEHOLDER.bind()) == "例如 X-Auth-Token"
    assert chinese.render(_HEADER_VALUE_PLACEHOLDER.bind()) == "例如 {{AUTH_TOKEN}}"


class _MCPPanelApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def __init__(self) -> None:
        super().__init__()
        self.notifications: list[tuple[str, dict]] = []

    def compose(self) -> ComposeResult:
        yield Static("placeholder")

    def notify(self, message: str, **kwargs) -> None:  # type: ignore[override]
        self.notifications.append((message, kwargs))


def test_mcp_card_get_config_preserves_seed_before_children_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    seed = MCPServerConfig(
        name="local",
        transport="stdio",
        command="uvx",
        args=["server"],
        encoding="utf-16",
        env={"TOKEN": "abc"},
        resolve_env_templates=False,
        enabled=False,
        tool_name_prefix="local",
        request_timeout=9,
        load_prompts=False,
        expose_instructions=False,
        use_progressive_disclosure=True,
        always_load=["search", "read"],
    )
    card = MCPConnectionCard(seed, index=0)

    def query_one(selector: str, *_args: object, **_kwargs: object) -> object:
        raise NoMatches(f"No nodes match {selector!r}")

    monkeypatch.setattr(card, "query_one", query_one)

    config = card.get_config()

    assert config.name == "local"
    assert config.command == "uvx"
    assert config.args == ["server"]
    assert config.encoding == "utf-16"
    assert config.env == {"TOKEN": "abc"}
    assert config.resolve_env_templates is False
    assert config.enabled is False
    assert config.tool_name_prefix == "local"
    assert config.request_timeout == 9
    assert config.load_prompts is False
    assert config.expose_instructions is False
    assert config.use_progressive_disclosure is True
    assert config.always_load == ["search", "read"]

    http_seed = MCPServerConfig(
        name="remote",
        transport="http",
        url="https://api.example.com/mcp",
        headers={"Authorization": "Bearer {{TOKEN}}"},
        resolve_header_templates=False,
        terminate_on_close=True,
        tool_name_prefix="remote",
    )
    http_card = MCPConnectionCard(http_seed, index=1)
    monkeypatch.setattr(http_card, "query_one", query_one)

    http_config = http_card.get_config()

    assert http_config.url == "https://api.example.com/mcp"
    assert http_config.headers == {"Authorization": "Bearer {{TOKEN}}"}
    assert http_config.resolve_header_templates is False
    assert http_config.terminate_on_close is True
    assert http_config.tool_name_prefix == "remote"


def test_mcp_card_get_config_preserves_seed_when_key_value_rows_are_mounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    empty_container = SimpleNamespace(query=lambda _selector: [])

    http_seed = MCPServerConfig(
        name="remote",
        transport="http",
        url="https://api.example.com/mcp",
        headers={"Authorization": "Bearer {{TOKEN}}"},
    )
    http_card = MCPConnectionCard(http_seed, index=0)

    def http_query_one(selector: str, *_args: object, **_kwargs: object) -> object:
        if selector == "#mcp-headers-0":
            return empty_container
        raise NoMatches(f"No nodes match {selector!r}")

    monkeypatch.setattr(http_card, "query_one", http_query_one)

    assert http_card.get_config().headers == {"Authorization": "Bearer {{TOKEN}}"}

    stdio_seed = MCPServerConfig(
        name="local",
        transport="stdio",
        command="uvx",
        env={"TOKEN": "abc"},
    )
    stdio_card = MCPConnectionCard(stdio_seed, index=1)

    def stdio_query_one(selector: str, *_args: object, **_kwargs: object) -> object:
        if selector == "#mcp-env-1":
            return empty_container
        raise NoMatches(f"No nodes match {selector!r}")

    monkeypatch.setattr(stdio_card, "query_one", stdio_query_one)

    assert stdio_card.get_config().env == {"TOKEN": "abc"}


async def _wait_for_test_dialog(app: App, pilot, *, success: bool) -> ConnectionTestDialog:
    """Poll until the active ConnectionTestDialog has resolved into success/error state."""

    expected_class = "-success" if success else "-error"

    def matching_dialog() -> ConnectionTestDialog | None:
        return next(
            (
                screen
                for screen in app.screen_stack
                if isinstance(screen, ConnectionTestDialog) and screen.has_class(expected_class)
            ),
            None,
        )

    await wait_for(matching_dialog, pilot=pilot, description=f"connection dialog reaches {expected_class}")
    dialog = matching_dialog()
    assert dialog is not None
    return dialog


async def _wait_for_mcp_cards(
    panel: MCPConfigPanel,
    pilot,
    count: int,
    *,
    exact: bool = False,
    laid_out: bool = False,
) -> list[MCPConnectionCard]:
    """Wait for completed card mounts and any geometry the caller needs."""

    def cards_ready() -> bool:
        cards = list(panel.query(MCPConnectionCard))
        count_matches = len(cards) == count if exact else len(cards) >= count
        return count_matches and all(
            card.is_mounted and (not laid_out or card.region.height > 0) for card in cards[:count]
        )

    # Mount completion is owned by the card rebuild. Only geometry checks
    # need the Pilot's layout pump, not a barrier over every widget's queue.
    await wait_for(
        cards_ready,
        pilot=pilot if laid_out else None,
        description=f"{count} MCP cards are mounted with requested geometry",
    )
    return list(panel.query(MCPConnectionCard))


async def _wait_for_mcp_name_inputs(
    panel: MCPConfigPanel,
    pilot,
    count: int,
    *,
    exact: bool = False,
) -> list[MCPConnectionCard]:
    """Poll until each rebuilt card's name input has mounted.

    ``_wait_for_mcp_cards`` waits for the outer card; on Windows CI a single
    ``pilot.pause()`` does not always drain the deferred inner ``#mcp-name-{i}``
    Input mounts after an add/remove rebuild, so query it directly.
    """

    def names_ready() -> bool:
        cards = list(panel.query(MCPConnectionCard))
        count_matches = len(cards) == count if exact else len(cards) >= count
        return count_matches and all(cards[index].query(f"#mcp-name-{index}") for index in range(count))

    await wait_for(names_ready, pilot=pilot, description=f"MCP name inputs for {count} cards are mounted")
    return list(panel.query(MCPConnectionCard))


async def _add_mcp_card(panel: MCPConfigPanel, pilot, *, count: int = 1) -> MCPConnectionCard:
    """Press the Add button and wait until the new MCP card is mounted."""

    panel.query_one("#mcp-add-btn").press()
    cards = await _wait_for_mcp_cards(panel, pilot, count)
    return cards[count - 1]


async def _wait_for_http_transport_fields(
    card: MCPConnectionCard,
    pilot,
    *,
    index: int = 0,
    env_removed: bool = False,
) -> None:
    """Poll until the async transport rebuild has mounted the HTTP fields.

    Includes the headers container's first row key input — without this,
    a saturated CI worker can mount URL/env before the headers row's
    child inputs are wired, making ``.mcp-item-key-input`` queries flake.
    Also requires the URL input and the header row's remove button to
    have a laid-out (non-empty) region: DOM presence alone races the
    arrange pass, and callers assert on widget geometry right after.
    """

    url_selector = f"#mcp-url-{index}"
    env_selector = f"#mcp-env-{index}"
    headers_selector = f"#mcp-headers-{index}"

    def fields_ready() -> bool:
        url_nodes = list(card.query(url_selector))
        has_url = bool(url_nodes) and url_nodes[0].region.width > 0
        has_expected_env_state = not env_removed or not card.query(env_selector)
        headers_nodes = list(card.query(headers_selector))
        has_headers_inputs = False
        if headers_nodes and headers_nodes[0].query(".mcp-item-key-input"):
            remove_buttons = list(headers_nodes[0].query(".mcp-remove-btn"))
            has_headers_inputs = bool(remove_buttons) and remove_buttons[0].region.width > 0
        return has_url and has_expected_env_state and has_headers_inputs

    await wait_for(fields_ready, pilot=pilot, description=f"HTTP transport fields for MCP card {index} are laid out")


async def _wait_for_mcp_item_rows(container, pilot, count: int) -> None:
    """Poll until async MCP header/env row mounts have reached the expected count."""

    await wait_for(
        lambda: len(list(container.query(".mcp-item-row"))) >= count,
        pilot=pilot,
        description=f"Expected at least {count} MCP item rows to be mounted",
    )


async def _wait_for_mcp_item_row_count(container, pilot, count: int) -> None:
    """Poll until MCP header/env rows settle at the exact expected count."""

    await wait_for(
        lambda: len(list(container.query(".mcp-item-row"))) == count,
        pilot=pilot,
        description=f"Expected exactly {count} MCP item rows",
    )


async def _wait_for_mcp_key_value_container(card: MCPConnectionCard, pilot, selector: str):
    """Poll until a rebuilt MCP key/value container and its row inputs are mounted."""

    def inputs_ready() -> bool:
        containers = list(card.query(selector))
        return bool(containers) and bool(containers[0].query(".mcp-item-key-input"))

    await wait_for(inputs_ready, pilot=pilot, description=f"MCP key/value inputs in {selector} are mounted")
    return card.query_one(selector)


def _dialog_message(dialog: ConnectionTestDialog) -> str:
    return str(dialog.query_one("#connection-test-message", Static).content)


def _dialog_status(dialog: ConnectionTestDialog) -> str:
    """Read the connection status, which rides the bottom border (the top one names the server)."""
    return str(dialog.query_one("#connection-test-container").border_subtitle)


@pytest.mark.asyncio
async def test_load_prompts_checkbox_explains_prompt_template_loading() -> None:
    """The prompts checkbox should describe its capability-gated loading behavior."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        checkbox = card.query_one("#mcp-load-prompts-0", Checkbox)

        tooltip = checkbox.tooltip
        assert tooltip is not None
        tooltip_text = str(tooltip)
        assert "loads prompt templates advertised by this MCP server as callable tools" in tooltip_text
        assert "does not advertise prompt support" in tooltip_text
        assert "keep server prompt templates out of the model's tool list" in tooltip_text


def test_mcp_config_panel_uses_workspace_cwd_for_connection_tests() -> None:
    panel = MCPConfigPanel([], workspace_cwd="/workspace/project")

    assert panel.test_adapter._stdio_cwd == "/workspace/project"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_adding_mcp_connections_stacks_cards_without_overlap() -> None:
    """New MCP server cards should stack vertically instead of overlapping."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        await _add_mcp_card(panel, pilot)
        await _add_mcp_card(panel, pilot, count=2)

        # Windows CI can be slow to resolve layout after mount; poll until
        # both cards have been laid out (non-zero height) before asserting.
        cards = await _wait_for_mcp_cards(panel, pilot, 2, laid_out=True)
        assert len(cards) == 2

        first, second = cards
        assert second.region.y >= first.region.y + first.region.height


@pytest.mark.asyncio
async def test_add_mcp_connection_inserts_new_card_before_existing_servers() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([MCPServerConfig(name="existing", transport="stdio", command="python")])
        await app.mount(panel)
        await pilot.pause()

        (existing_card,) = await _wait_for_mcp_cards(panel, pilot, 1)
        existing_card.query_one("#mcp-cmd-0", TextArea).text = "uvx edited-server"
        panel.query_one("#mcp-add-btn", Button).press()

        cards = await _wait_for_mcp_cards(panel, pilot, 2)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 2)
        assert cards[0].query_one("#mcp-name-0", Input).value == "New Server"
        assert cards[1].query_one("#mcp-name-1", Input).value == "existing"
        assert cards[1].query_one("#mcp-cmd-1", TextArea).text == "uvx edited-server"

        configs = panel.get_config()
        assert [config.name for config in configs] == ["New Server", "existing"]
        assert configs[1].command == "uvx"
        assert configs[1].args == ["edited-server"]


@pytest.mark.asyncio
async def test_add_mcp_connection_preserves_invalid_draft_command() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([MCPServerConfig(name="existing", transport="stdio", command="python")])
        await app.mount(panel)
        await pilot.pause()

        (existing_card,) = await _wait_for_mcp_cards(panel, pilot, 1)
        existing_card.query_one("#mcp-cmd-0", TextArea).text = 'python "'
        panel.query_one("#mcp-add-btn", Button).press()

        cards = await _wait_for_mcp_cards(panel, pilot, 2)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 2)
        assert cards[0].query_one("#mcp-name-0", Input).value == "New Server"
        assert cards[1].query_one("#mcp-name-1", Input).value == "existing"
        assert cards[1].query_one("#mcp-cmd-1", TextArea).text == 'python "'
        assert any("No closing quotation" in error for error in cards[1].validate())


@pytest.mark.asyncio
async def test_add_mcp_connection_preserves_partial_env_row_value_without_key() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([MCPServerConfig(name="existing", transport="stdio", command="python")])
        await app.mount(panel)
        await pilot.pause()

        (existing_card,) = await _wait_for_mcp_cards(panel, pilot, 1)
        env_container = await _wait_for_mcp_key_value_container(existing_card, pilot, "#mcp-env-0")
        env_row = env_container.query_one(".mcp-item-row")
        env_row.query_one(".mcp-item-value-input", Input).value = "secret-token"
        assert any("key name is required" in error for error in existing_card.validate())

        panel.query_one("#mcp-add-btn", Button).press()

        await _wait_for_mcp_cards(panel, pilot, 2)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 2)
        existing_card = cards[1]
        env_container = await _wait_for_mcp_key_value_container(existing_card, pilot, "#mcp-env-1")
        env_row = env_container.query_one(".mcp-item-row")
        assert env_row.query_one(".mcp-item-key-input", Input).value == ""
        assert env_row.query_one(".mcp-item-value-input", Input).value == "secret-token"
        assert any("key name is required" in error for error in existing_card.validate())


@pytest.mark.asyncio
async def test_add_mcp_connection_preserves_partial_header_row_value_without_key() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(
                    name="existing",
                    transport="http",
                    url="https://api.example.com/mcp",
                )
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        (existing_card,) = await _wait_for_mcp_cards(panel, pilot, 1)
        headers_container = await _wait_for_mcp_key_value_container(existing_card, pilot, "#mcp-headers-0")
        header_row = headers_container.query_one(".mcp-item-row")
        header_row.query_one(".mcp-item-value-input", Input).value = "Bearer token"
        assert any("key name is required" in error for error in existing_card.validate())

        panel.query_one("#mcp-add-btn", Button).press()

        await _wait_for_mcp_cards(panel, pilot, 2)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 2)
        existing_card = cards[1]
        headers_container = await _wait_for_mcp_key_value_container(existing_card, pilot, "#mcp-headers-1")
        header_row = headers_container.query_one(".mcp-item-row")
        assert header_row.query_one(".mcp-item-key-input", Input).value == ""
        assert header_row.query_one(".mcp-item-value-input", Input).value == "Bearer token"
        assert any("key name is required" in error for error in existing_card.validate())


@pytest.mark.asyncio
async def test_remove_mcp_connection_preserves_edits_in_remaining_servers() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(name="one", transport="stdio", command="python"),
                MCPServerConfig(name="two", transport="stdio", command="python"),
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        cards = await _wait_for_mcp_cards(panel, pilot, 2)
        cards[1].query_one("#mcp-name-1", Input).value = "edited"
        cards[1].query_one("#mcp-desc-1", TextArea).text = "edited description"
        cards[1].query_one("#mcp-cmd-1", TextArea).text = "uvx edited-server"
        await press_and_answer_confirm(pilot, cards[0].query_one("#mcp-delete-btn-0", Button))

        cards = await _wait_for_mcp_cards(panel, pilot, 1, exact=True)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 1, exact=True)
        assert cards[0].query_one("#mcp-name-0", Input).value == "edited"
        assert cards[0].query_one("#mcp-desc-0", TextArea).text == "edited description"
        assert cards[0].query_one("#mcp-cmd-0", TextArea).text == "uvx edited-server"

        configs = panel.get_config()
        assert len(configs) == 1
        assert configs[0].name == "edited"
        assert configs[0].description == "edited description"
        assert configs[0].command == "uvx"
        assert configs[0].args == ["edited-server"]


@pytest.mark.asyncio
async def test_remove_mcp_connection_preserves_invalid_draft_command() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(name="one", transport="stdio", command="python"),
                MCPServerConfig(name="two", transport="stdio", command="python"),
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        cards = await _wait_for_mcp_cards(panel, pilot, 2)
        cards[1].query_one("#mcp-cmd-1", TextArea).text = 'python "'
        await press_and_answer_confirm(pilot, cards[0].query_one("#mcp-delete-btn-0", Button))

        cards = await _wait_for_mcp_cards(panel, pilot, 1, exact=True)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 1, exact=True)
        assert cards[0].query_one("#mcp-name-0", Input).value == "two"
        assert cards[0].query_one("#mcp-cmd-0", TextArea).text == 'python "'
        assert any("No closing quotation" in error for error in cards[0].validate())


@pytest.mark.asyncio
async def test_remove_mcp_connection_preserves_partial_env_row_value_without_key() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(name="one", transport="stdio", command="python"),
                MCPServerConfig(name="two", transport="stdio", command="python"),
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        cards = await _wait_for_mcp_cards(panel, pilot, 2)
        survivor_env = await _wait_for_mcp_key_value_container(cards[1], pilot, "#mcp-env-1")
        survivor_env.query_one(".mcp-item-row").query_one(".mcp-item-value-input", Input).value = "secret-token"
        assert any("key name is required" in error for error in cards[1].validate())

        await press_and_answer_confirm(pilot, cards[0].query_one("#mcp-delete-btn-0", Button))

        await _wait_for_mcp_cards(panel, pilot, 1, exact=True)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 1, exact=True)
        assert cards[0].query_one("#mcp-name-0", Input).value == "two"
        env_container = await _wait_for_mcp_key_value_container(cards[0], pilot, "#mcp-env-0")
        env_row = env_container.query_one(".mcp-item-row")
        assert env_row.query_one(".mcp-item-key-input", Input).value == ""
        assert env_row.query_one(".mcp-item-value-input", Input).value == "secret-token"
        assert any("key name is required" in error for error in cards[0].validate())


@pytest.mark.asyncio
async def test_remove_mcp_connection_preserves_partial_header_row_value_without_key() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(name="one", transport="http", url="https://api.example.com/one"),
                MCPServerConfig(name="two", transport="http", url="https://api.example.com/two"),
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        cards = await _wait_for_mcp_cards(panel, pilot, 2)
        survivor_headers = await _wait_for_mcp_key_value_container(cards[1], pilot, "#mcp-headers-1")
        survivor_headers.query_one(".mcp-item-row").query_one(".mcp-item-value-input", Input).value = "Bearer token"
        assert any("key name is required" in error for error in cards[1].validate())

        await press_and_answer_confirm(pilot, cards[0].query_one("#mcp-delete-btn-0", Button))

        await _wait_for_mcp_cards(panel, pilot, 1, exact=True)
        cards = await _wait_for_mcp_name_inputs(panel, pilot, 1, exact=True)
        assert cards[0].query_one("#mcp-name-0", Input).value == "two"
        headers_container = await _wait_for_mcp_key_value_container(cards[0], pilot, "#mcp-headers-0")
        header_row = headers_container.query_one(".mcp-item-row")
        assert header_row.query_one(".mcp-item-key-input", Input).value == ""
        assert header_row.query_one(".mcp-item-value-input", Input).value == "Bearer token"
        assert any("key name is required" in error for error in cards[0].validate())


@pytest.mark.asyncio
async def test_stdio_command_line_parses_into_command_and_args() -> None:
    """The stdio command input should split a full command line into command and args."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "npx -y example.tar --port 8080"
        card.query_one("#mcp-desc-0", TextArea).text = "Filesystem MCP"
        card.query_one("#mcp-tool-access-0", Select).value = "selected"
        await pilot.pause()
        card.query_one("#mcp-tools-0").value = "echo, add"
        card.query_one("#mcp-prefix-0", Input).value = "filesystem"
        card.query_one("#mcp-timeout-0").value = "45"
        card.query_one("#mcp-enabled-0", Checkbox).value = False
        labels = [str(label.content) for label in card.query(Label)]
        assert "MCP Request Timeout (seconds)" in labels
        assert labels.index("Tool Name Prefix") < labels.index("MCP Request Timeout (seconds)")
        assert card.query_one("#mcp-timeout-0", Input).placeholder == "Leave blank for default (30)"

        transport_section = card.query_one("#mcp-transport-fields-0")
        prompts_section = card.query_one("#mcp-prompts-instructions-section-0")
        access_section = card.query_one("#mcp-tool-access-section-0")
        loading_section = card.query_one("#mcp-model-exposure-fields-0")
        naming_section = card.query_one("#mcp-naming-limits-section-0")
        assert str(transport_section.border_title) == "STDIO Options"
        assert str(prompts_section.border_title) == "Server Instructions & Prompt Templates"
        assert str(access_section.border_title) == "Tool Access"
        assert str(loading_section.border_title) == "Tool Loading"
        assert str(naming_section.border_title) == "Naming & Limits"
        assert prompts_section.styles.border.top[0] == "solid"
        assert access_section.styles.border.top[0] == "solid"
        assert loading_section.styles.border.top[0] == "solid"
        assert naming_section.styles.border.top[0] == "solid"
        assert prompts_section.styles.margin.top == 1
        assert access_section.styles.margin.top == 1
        assert prompts_section.styles.margin.right == 0
        assert access_section.styles.margin.right == 0
        assert loading_section.styles.margin.right == 0
        assert naming_section.styles.margin.right == 0
        assert (
            transport_section.region.right
            == prompts_section.region.right
            == access_section.region.right
            == loading_section.region.right
            == naming_section.region.right
        )
        assert card.query_one("#mcp-cmd-0").parent is transport_section
        assert card.query_one("#mcp-env-0").parent is transport_section
        assert prompts_section.region.y - transport_section.region.bottom == 1
        assert access_section.region.y - prompts_section.region.bottom == 1
        assert card.query_one("#mcp-eadd-row-0").styles.margin.bottom == 0
        assert card.query_one("#mcp-tool-access-0").styles.margin.right == 2
        assert card.query_one("#mcp-loading-strategy-0").styles.margin.right == 2
        assert card.query_one("#mcp-tool-access-0").query_one("SelectCurrent").styles.padding.left == 1
        assert card.query_one("#mcp-loading-strategy-0").query_one("SelectCurrent").styles.padding.left == 1
        assert card.query_one("#mcp-prefix-0", Input).styles.margin.right == 2
        assert card.query_one("#mcp-timeout-0", Input).styles.margin.right == 2
        assert card.query_one("#mcp-prefix-0", Input).styles.padding.left == 1
        assert card.query_one("#mcp-timeout-0", Input).styles.padding.left == 1
        assert access_section.region.y + access_section.region.height <= loading_section.region.y
        assert loading_section.region.y + loading_section.region.height <= naming_section.region.y

        config = card.get_config()
        assert config.transport == "stdio"
        assert config.enabled is False
        assert config.command == "npx"
        assert config.args == ["-y", "example.tar", "--port", "8080"]
        assert config.description == "Filesystem MCP"
        assert config.allowed_tools == ["echo", "add"]
        assert config.tool_name_prefix == "filesystem"
        assert config.request_timeout == 45


def test_a_saved_command_line_shown_in_the_field_reads_back_as_the_same_arguments() -> None:
    args = ["[/bold] literal", 'say "hi"', "", "C:\\Program Files\\data\\", "two\nlines", "--flag"]

    shown = MCPConnectionCard._format_command_line("python server.py", args)

    assert MCPConnectionCard._split_command_line(shown) == ("python server.py", args)


def test_a_command_typed_over_several_lines_splits_at_the_line_breaks() -> None:
    typed = "npx -y\r\n@scope/server\n--port 8080"

    assert MCPConnectionCard._split_command_line(typed) == ("npx", ["-y", "@scope/server", "--port", "8080"])


@pytest.mark.asyncio
async def test_allowed_tools_empty_list_round_trips_through_tui() -> None:
    """An explicit empty allow-list must not be saved back as expose-all."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    initial = MCPServerConfig(name="locked", transport="stdio", command="python", allowed_tools=[])
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([initial])
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]

        assert card.query_one("#mcp-tool-access-0", Select).value == "none"
        assert card.query_one("#mcp-tools-0", Input).value == ""
        assert card.query_one("#mcp-selected-tools-fields-0").display is False
        assert card.query_one("#mcp-model-exposure-fields-0").display is False
        assert card.get_config().allowed_tools == []


@pytest.mark.asyncio
async def test_selected_tool_mode_requires_at_least_one_name() -> None:
    """Selected mode is not an implicit alias for the explicit No tools mode."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-tool-access-0", Select).value = "selected"
        await pilot.pause()

        assert card.query_one("#mcp-tools-0", Input).disabled is False
        assert card.query_one("#mcp-selected-tools-fields-0").display is True
        selected_fields = card.query_one("#mcp-selected-tools-fields-0")
        assert selected_fields.query_one(Label).styles.margin.top == 1
        assert card.query_one("#mcp-tools-0", Input).styles.margin.right == 2
        assert card.get_config().allowed_tools == []
        assert any("select at least one available tool" in error for error in card.validate())


@pytest.mark.asyncio
async def test_all_tool_access_returns_unspecified_allow_list() -> None:
    """All-server-tools maps to None even when the selected-tools input retains stale text."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    initial = MCPServerConfig(name="limited", transport="stdio", command="python", allowed_tools=["echo"])
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([initial])
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]
        card.query_one("#mcp-tool-access-0", Select).value = "all"
        await pilot.pause()

        assert card.query_one("#mcp-tools-0", Input).disabled is True
        assert card.query_one("#mcp-selected-tools-fields-0").display is False
        assert card.get_config().allowed_tools is None


@pytest.mark.asyncio
async def test_loading_strategy_controls_initial_tool_subset() -> None:
    """Full and progressive exposure are exclusive; only progressive persists an initial subset."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    initial = MCPServerConfig(
        name="progressive",
        transport="stdio",
        command="python",
        use_progressive_disclosure=True,
        always_load=["search", "read_file"],
    )
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([initial])
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]
        loading = card.query_one("#mcp-loading-strategy-0", Select)
        always_fields = card.query_one("#mcp-always-load-fields-0")
        always_input = card.query_one("#mcp-always-load-0", Input)

        assert loading.value == "progressive"
        assert loading.tooltip is not None
        assert always_fields.display is True
        assert always_input.disabled is False
        assert always_input.value == "search, read_file"
        assert always_input.tooltip is not None
        initial_label = always_fields.query_one(Label)
        assert initial_label.styles.margin.top == 1
        assert always_input.styles.margin.right == 2
        assert panel.get_config()[0].always_load == ["search", "read_file"]
        exposure_fields = card.query_one("#mcp-model-exposure-fields-0")
        naming_section = card.query_one("#mcp-naming-limits-section-0")
        assert exposure_fields.region.height < 11
        assert naming_section.region.y < exposure_fields.region.y + 14
        rendered_labels = [str(label.content) for label in card.query(Label)]
        assert "All permitted tools are sent to the model on every request." not in rendered_labels
        assert (
            "Only the initial tools are sent first; the model can load other permitted tools during this run."
            not in rendered_labels
        )

        loading.value = "full"
        await pilot.pause()

        assert always_fields.display is False
        assert always_input.disabled is True
        hidden_config = panel.get_config()[0]
        assert hidden_config.use_progressive_disclosure is False
        assert hidden_config.always_load == []

        loading.value = "progressive"
        await pilot.pause()
        assert always_fields.display is True
        assert always_input.disabled is False
        assert always_input.value == "search, read_file"


@pytest.mark.asyncio
async def test_progressive_mcp_read_only_state_and_list_validation() -> None:
    """Read-only cards lock progressive fields and editable lists reject ambiguous entries."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    initial = MCPServerConfig(
        name="progressive",
        transport="stdio",
        command="python",
        use_progressive_disclosure=True,
        always_load=["search"],
    )
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([initial], read_only=True)
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]
        assert card.query_one("#mcp-loading-strategy-0", Select).disabled is True
        assert card.query_one("#mcp-expose-instructions-0", Checkbox).disabled is True
        assert card.query_one("#mcp-load-prompts-0", Checkbox).disabled is True
        always_input = card.query_one("#mcp-always-load-0", Input)
        assert always_input.disabled is True
        assert always_input.value == "search"
        assert panel.get_config()[0] == initial

        editable = MCPConfigPanel([initial])
        await app.mount(editable)
        await pilot.pause()
        editable_card = (await _wait_for_mcp_cards(editable, pilot, 1))[0]
        editable_card.query_one("#mcp-tool-access-0", Select).value = "selected"
        await pilot.pause()
        editable_card.query_one("#mcp-tools-0", Input).value = "search"
        editable_card.query_one("#mcp-always-load-0", Input).value = "search, , search"

        errors = editable_card.validate()
        assert any("empty tool name" in error for error in errors)
        assert any("duplicate tool name 'search'" in error for error in errors)

        editable_card.query_one("#mcp-always-load-0", Input).value = "read_file"
        assert any(
            "must also be included in Available Tool Scope: read_file" in error for error in editable_card.validate()
        )


@pytest.mark.asyncio
async def test_transport_selector_switches_to_http_fields() -> None:
    """Transport Select should switch fields and parse HTTP config."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        transport = card.query_one("#mcp-transport-0", Select)
        transport.value = "http"
        await pilot.pause()
        await _wait_for_http_transport_fields(card, pilot, env_removed=True)

        url_input = card.query_one("#mcp-url-0", Input)
        url_input.value = "https://api.example.com/mcp"
        transport_section = card.query_one("#mcp-transport-fields-0")
        assert str(transport_section.border_title) == "HTTP Options"
        assert transport_section.styles.border.top[0] == "solid"
        assert url_input.parent is transport_section
        headers = card.query_one("#mcp-headers-0")
        assert headers.parent is transport_section
        header_row = headers.query_one(".mcp-item-row")
        header_value = header_row.query_one(".mcp-item-value-input", Input)
        remove_button = header_row.query_one(".mcp-remove-btn", Button)
        assert header_value.styles.margin.right == 0
        assert remove_button.region.right == url_input.region.right
        assert remove_button.styles.content_align == ("right", "middle")
        assert transport_section.region.right == card.query_one("#mcp-tool-access-section-0").region.right
        labels = [str(label.content) for label in card.query(Label)]
        assert "MCP Request Timeout (seconds)" in labels
        assert card.query_one("#mcp-timeout-0", Input).placeholder == "Leave blank for default (30)"

        config = card.get_config()
        assert config.transport == "http"
        assert config.url == "https://api.example.com/mcp"
        assert card._headers_user_modified is True

        assert not list(card.query("#mcp-env-0"))
        assert not card.query_one("#mcp-skip-tls-0", Checkbox).value
        assert not card.query_one("#mcp-bypass-proxy-0", Checkbox).value
        assert config.env == {}
        assert config.verify_ssl is True
        assert config.bypass_proxy is False


@pytest.mark.asyncio
async def test_http_transport_hides_env_section_and_drops_env_config() -> None:
    """HTTP transport no longer exposes or returns Environment Variables."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    initial = MCPServerConfig(
        name="h",
        transport="http",
        url="https://api.example.com/mcp",
        env={"SSL_CERT_FILE": "/tmp/ca.pem"},
    )

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([initial])
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]

        assert list(card.query("#mcp-env-0")) == []
        assert list(card.query("#mcp-eadd-0")) == []

        config = card.get_config()
        assert config.transport == "http"
        assert config.env == {}


@pytest.mark.asyncio
async def test_http_checkboxes_round_trip_saved_tls_and_proxy_choices() -> None:
    """TLS uses an inverse checkbox; proxy bypass uses a direct checkbox."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    initial = MCPServerConfig(
        name="h",
        transport="http",
        url="https://api.example.com/mcp",
        verify_ssl=False,
        bypass_proxy=True,
    )

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([initial])
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]
        proxy = card.query_one("#mcp-bypass-proxy-0", Checkbox)
        assert proxy.value is True and card.get_config().bypass_proxy is True
        checkbox = card.query_one("#mcp-skip-tls-0", Checkbox)
        # Loaded config had verify_ssl=False → checkbox is checked.
        assert checkbox.value is True
        config = card.get_config()
        assert config.verify_ssl is False

        # Uncheck → verify_ssl flips to True (secure default).
        checkbox.value = False
        await pilot.pause()
        config = card.get_config()
        assert config.verify_ssl is True

        assert config.bypass_proxy is True
        proxy.value = False
        await pilot.pause()
        config = card.get_config()
        assert config.bypass_proxy is False and config.verify_ssl is True


@pytest.mark.asyncio
async def test_http_transport_saves_draft_header_rows_without_add() -> None:
    """Filled header rows should be serialized without pressing Add."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-transport-0", Select).value = "http"
        await pilot.pause()
        await _wait_for_http_transport_fields(card, pilot)

        card.query_one("#mcp-url-0", Input).value = "https://api.example.com/mcp"

        headers_container = card.query_one("#mcp-headers-0")
        header_row = headers_container.query_one(".mcp-item-row")
        header_row.query_one(".mcp-item-key-input", Input).value = "X-Team"
        header_row.query_one(".mcp-item-value-input", Input).value = "platform"

        config = card.get_config()
        assert config.headers == {"X-Team": "platform"}
        assert config.env == {}


@pytest.mark.asyncio
async def test_mcp_add_button_appends_another_editable_row() -> None:
    """Add creates a new row while existing filled rows remain active data."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        env_container = card.query_one("#mcp-env-0")
        first_row = env_container.query_one(".mcp-item-row")
        first_row.query_one(".mcp-item-key-input", Input).value = "NO_PROXY"
        first_row.query_one(".mcp-item-value-input", Input).value = "*"

        card.query_one("#mcp-eadd-0").press()
        await _wait_for_mcp_item_rows(env_container, pilot, 2)

        rows = list(env_container.query(".mcp-item-row"))
        assert len(rows) == 2
        rows[1].query_one(".mcp-item-key-input", Input).value = "SSL_CERT_FILE"
        rows[1].query_one(".mcp-item-value-input", Input).value = "/tmp/ca.pem"

        config = card.get_config()
        assert config.env == {"NO_PROXY": "*", "SSL_CERT_FILE": "/tmp/ca.pem"}


@pytest.mark.asyncio
async def test_stdio_validate_rejects_partial_env_rows() -> None:
    """Partially-filled stdio env rows should block Save with clear errors."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        env_container = card.query_one("#mcp-env-0")
        env_row = env_container.query_one(".mcp-item-row")
        env_row.query_one(".mcp-item-key-input", Input).value = "API_KEY"

        errors = card.validate()

    assert "Server 'New Server': Environment Variables row 1: value is required for key 'API_KEY'." in errors


@pytest.mark.asyncio
async def test_mcp_validate_rejects_invalid_tool_name_prefix() -> None:
    """Tool name prefixes should be validated before save or connection test."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"
        card.query_one("#mcp-prefix-0", Input).value = "github.v1"

        errors = card.validate()

    assert any("underscores, and hyphens" in error for error in errors)


@pytest.mark.asyncio
async def test_mcp_validate_rejects_prefix_that_makes_progressive_control_too_long() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"
        card.query_one("#mcp-prefix-0", Input).value = "a" * 50
        card.query_one("#mcp-loading-strategy-0", Select).value = "progressive"
        await pilot.pause()

        errors = card.validate()

    assert any("invalid generated control" in error and "maximum is 64" in error for error in errors)


@pytest.mark.asyncio
async def test_mcp_validate_rejects_partial_header_rows() -> None:
    """Partially-filled HTTP header rows should block Save with clear errors."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-transport-0", Select).value = "http"
        await pilot.pause()
        await _wait_for_http_transport_fields(card, pilot)

        card.query_one("#mcp-url-0", Input).value = "https://api.example.com/mcp"

        headers_container = card.query_one("#mcp-headers-0")
        header_row = headers_container.query_one(".mcp-item-row")
        header_row.query_one(".mcp-item-value-input", Input).value = "platform"

        errors = card.validate()

    assert "Server 'New Server': Headers row 1: key name is required when a value is set." in errors


@pytest.mark.asyncio
async def test_mcp_validate_rejects_duplicate_key_value_rows() -> None:
    """Duplicate keys should block Save instead of silently overwriting."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-transport-0", Select).value = "http"
        await pilot.pause()
        await _wait_for_http_transport_fields(card, pilot)

        card.query_one("#mcp-url-0", Input).value = "https://api.example.com/mcp"

        headers_container = card.query_one("#mcp-headers-0")
        first_row = headers_container.query_one(".mcp-item-row")
        first_row.query_one(".mcp-item-key-input", Input).value = "X-Team"
        first_row.query_one(".mcp-item-value-input", Input).value = "platform"

        card.query_one("#mcp-hadd-0").press()
        await _wait_for_mcp_item_rows(headers_container, pilot, 2)

        rows = list(headers_container.query(".mcp-item-row"))
        rows[1].query_one(".mcp-item-key-input", Input).value = "X-Team"
        rows[1].query_one(".mcp-item-value-input", Input).value = "infra"

        errors = card.validate()

    assert "Server 'New Server': Headers row 2: duplicate key 'X-Team'." in errors


@pytest.mark.asyncio
async def test_mcp_delete_last_key_value_row_recreates_blank_row() -> None:
    """Removing the last row should keep one blank editable row visible."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        env_container = card.query_one("#mcp-env-0")
        env_container.query_one(".mcp-remove-btn", Button).press()
        await pilot.pause()

        rows = list(env_container.query(".mcp-item-row"))

        assert len(rows) == 1
        assert rows[0].query_one(".mcp-item-key-input", Input).value == ""
        assert rows[0].query_one(".mcp-item-value-input", Input).value == ""


@pytest.mark.asyncio
async def test_mcp_seeded_key_value_rows_can_be_deleted_after_mount() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(
                    name="remote",
                    transport="http",
                    url="https://api.example.com/mcp",
                    headers={"A": "1", "B": "2"},
                ),
                MCPServerConfig(
                    name="local",
                    transport="stdio",
                    command="uvx",
                    env={"TOKEN": "abc", "NO_PROXY": "*"},
                ),
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        http_card, stdio_card = await _wait_for_mcp_cards(panel, pilot, 2)
        headers_container = http_card.query_one("#mcp-headers-0")
        env_container = stdio_card.query_one("#mcp-env-1")
        await _wait_for_mcp_item_rows(headers_container, pilot, 2)
        await _wait_for_mcp_item_rows(env_container, pilot, 2)

        next(iter(headers_container.query(".mcp-item-row"))).query_one(".mcp-remove-btn", Button).press()
        next(iter(env_container.query(".mcp-item-row"))).query_one(".mcp-remove-btn", Button).press()
        await _wait_for_mcp_item_row_count(headers_container, pilot, 1)
        await _wait_for_mcp_item_row_count(env_container, pilot, 1)

        assert http_card.get_config().headers == {"B": "2"}
        assert stdio_card.get_config().env == {"NO_PROXY": "*"}


def test_format_mcp_report_renders_markdown_report() -> None:
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="everything", transport="stdio", command="npx", args=["-y", "@mcp/everything"])
    report = MCPTestReport(
        server_name="everything",
        server_title="Everything Server",
        server_version="1.0.0",
        protocol_version="2025-06-18",
        capabilities=("logging", "prompts", "tools", "tools.listChanged"),
        instructions="Use these tools wisely.",
        tools=(("echo", "Echo a message."), ("add", "")),
        prompts=(("greet", "Say hello."),),
        initial_tool_names=None,
    )

    text = _format_mcp_report(config, report)

    assert "**Everything Server** · `everything` · v1.0.0 · MCP `2025-06-18`" in text
    assert "Connected via `stdio`: `npx -y @mcp/everything`" in text
    # Standard capability groups always render, marked advertised or not;
    # nested flags appear as dotted rows.
    assert "| `tools` | ✓ |" in text
    assert "| `tools.listChanged` | ✓ |" in text
    assert "| `resources` | — |" in text
    assert "| `completions` | — |" in text
    assert "> *Use these tools wisely.*" in text
    assert "2 tools available to the model." in text
    assert "- **echo** — Echo a message." in text
    assert "- **add**" in text
    assert "1 prompt template loaded as a tool." in text
    assert "- **greet** — Say hello." in text
    # Progressive disclosure off: no section at all.
    assert "On-demand Loading" not in text


def test_format_mcp_report_states_absences_instead_of_vanishing() -> None:
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="bare", transport="http", url="https://api.example.com/mcp", load_prompts=False)
    report = MCPTestReport(
        server_name=None,
        server_title=None,
        server_version=None,
        protocol_version=None,
        capabilities=("tools",),
        instructions=None,
        tools=(),
        prompts=(),
        initial_tool_names=("mcp_list_tools", "search"),
    )

    text = _format_mcp_report(config, report)

    assert "*The server did not report its identity.*" in text
    assert "Connected via `http`: `https://api.example.com/mcp`" in text
    assert "### Instructions\n\n*Not advertised.*" in text
    assert "*The server supports tools, but none are available (check the available-tool scope).*" in text
    assert "*Prompt loading is disabled for this server.*" in text
    assert "### On-demand Loading" in text
    assert "- `mcp_list_tools`" in text
    assert "- `search`" in text


def test_format_mcp_report_neutralizes_remote_markdown_structure() -> None:
    """Crafted server strings must stay inline: no forged headings, bullets, or rows."""
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="evil", transport="http", url="https://evil.example/mcp")
    report = MCPTestReport(
        server_name="evil`server",
        server_title="Spoof\n\n### Connection Failed",
        server_version="1.0\n# v2",
        protocol_version="x`y",
        capabilities=("tools", "experimental.bad|pipe"),
        instructions="### Fake Section\nline one\n- fake bullet",
        tools=(("hack", "desc\n\n## Forged Heading \x1b[2J\x1b]52;c;evil\x07"),),
        prompts=(),
        initial_tool_names=("mcp`list",),
    )

    text = _format_mcp_report(config, report)

    # Newlines collapse to spaces, so remote text cannot open a block of its own.
    assert "### Connection Failed" not in text.splitlines()
    assert "**Spoof ### Connection Failed**" in text
    assert "v1.0 # v2" in text
    # Backticks are swapped for quotes, so code spans cannot be broken out of.
    assert "`evil'server`" in text
    assert "MCP `x'y`" in text
    # Instructions starting with a block marker stay behind our quote+italic
    # markers — blockquote content is block-parsed, so a bare leading "###"
    # would otherwise render as a real heading.
    assert "> *### Fake Section line one - fake bullet*" in text
    assert "- **hack** — desc ## Forged Heading" in text
    assert "- `mcp'list`" in text
    # Terminal escape/control sequences are stripped, not passed through to
    # the terminal (screen clears, OSC-52 clipboard writes).
    assert "\x1b" not in text
    assert "\x07" not in text
    # Table cells escape pipes so rows keep their column count.
    assert "| `experimental.bad\\|pipe` | ✓ |" in text


def test_format_mcp_report_bounds_identity_and_capability_rows() -> None:
    """Server-controlled identity and capability lists cannot bloat the report."""
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="big", transport="stdio", command="python")
    report = MCPTestReport(
        server_name="big",
        server_title="T" * 500,
        server_version=None,
        protocol_version=None,
        capabilities=("experimental." + "a" * 300, *(f"experimental.cap{i:03d}" for i in range(45))),
        instructions=None,
        tools=(),
        prompts=(),
        initial_tool_names=None,
    )

    text = _format_mcp_report(config, report)

    assert "**" + "T" * 120 + "…**" in text
    assert "T" * 121 not in text
    # Standard groups always render, even when extras alone would fill the
    # 40-row budget; extras only occupy the remaining slots.
    for standard in ("completions", "logging", "prompts", "resources", "tools"):
        assert f"| `{standard}` | — |" in text
    # Each extra name is clamped: 46 extras, 35 shown, one of them huge.
    assert "experimental." + "a" * 107 + "…" in text
    assert "a" * 200 not in text
    assert "*…and 11 more.*" in text
    assert "| Capability | Advertised |" in text
    assert text.count("| `") == 40

    localized = _format_mcp_report(config, report, render_message=Localizer("zh-Hans").render)
    assert "*…另有 11 个。*" in localized
    assert "| 能力 | 已声明 |" in localized


@pytest.mark.asyncio
async def test_test_connect_button_shows_markdown_report_dialog() -> None:
    from chrys.service.mcp.adapter import MCPTestReport

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        report = MCPTestReport(
            server_name="everything",
            server_title="Everything Server",
            server_version="1.0.0",
            protocol_version="2025-06-18",
            capabilities=("tools", "tools.listChanged"),
            instructions="Use the echo tool.",
            tools=(("echo", "Echo a message."),),
            prompts=(),
            initial_tool_names=None,
        )
        panel.test_adapter.test_connection = AsyncMock(return_value=report)  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=True)

        assert _dialog_status(dialog) == "Connection Successful"
        # Success renders the Markdown report in the widened dialog; the plain
        # text body is gone with the rest of the loading chrome.
        assert dialog.has_class("-markdown")
        assert not dialog.query("#connection-test-message")
        # The widget's source fills in asynchronously on mount — poll for it.
        await wait_for(
            lambda: bool(dialog.query_one("#connection-test-markdown", VirtualizedMarkdown).source),
            pilot=pilot,
            description="MCP test report markdown source",
        )
        source = dialog.query_one("#connection-test-markdown", VirtualizedMarkdown).source
        assert "**Everything Server** · `everything` · v1.0.0 · MCP `2025-06-18`" in source
        assert "| `tools` | ✓ |" in source
        assert "- **echo** — Echo a message." in source
        assert not app.notifications


@pytest.mark.asyncio
async def test_test_connect_button_keeps_plain_success_for_reportless_double() -> None:
    """A test double returning ``None`` still gets the plain confirmation."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        panel.test_adapter.test_connection = AsyncMock(return_value=None)  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=True)

        assert _dialog_status(dialog) == "Connection Successful"
        assert "successful" in _dialog_message(dialog).lower()
        assert not app.notifications


@pytest.mark.asyncio
async def test_mcp_card_removal_is_blocked_while_connection_test_runs() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(name="one", transport="stdio", command="python"),
                MCPServerConfig(name="two", transport="stdio", command="python"),
            ]
        )
        await app.mount(panel)
        await pilot.pause()

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_test(_config: MCPServerConfig) -> None:
            started.set()
            await release.wait()

        panel.test_adapter.test_connection = AsyncMock(side_effect=_slow_test)  # type: ignore[method-assign]

        card = (await _wait_for_mcp_cards(panel, pilot, 2))[0]
        card.query_one("#mcp-test-btn-0", Button).press()
        await wait_for(
            started.is_set,
            pilot=pilot,
            description="MCP connection test to start",
        )

        panel._on_remove(MCPConnectionCard.Removed(card))
        await pilot.pause()

        assert len(await _wait_for_mcp_cards(panel, pilot, 2)) == 2
        assert app.notifications
        assert "Wait for MCP connection tests" in app.notifications[-1][0]
        # Refused before asking: no delete confirmation opens over the test dialog.
        assert isinstance(app.screen, ConnectionTestDialog)

        release.set()
        await _wait_for_test_dialog(app, pilot, success=True)


@pytest.mark.asyncio
async def test_mcp_card_add_is_blocked_while_connection_test_runs() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([MCPServerConfig(name="one", transport="stdio", command="python")])
        await app.mount(panel)
        await pilot.pause()

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_test(_config: MCPServerConfig) -> None:
            started.set()
            await release.wait()

        panel.test_adapter.test_connection = AsyncMock(side_effect=_slow_test)  # type: ignore[method-assign]

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]
        card.query_one("#mcp-test-btn-0", Button).press()
        await wait_for(
            started.is_set,
            pilot=pilot,
            description="MCP connection test to start",
        )

        panel.query_one("#mcp-add-btn", Button).press()
        await pilot.pause()

        assert len(await _wait_for_mcp_cards(panel, pilot, 1)) == 1
        assert app.notifications
        assert "Wait for MCP connection tests" in app.notifications[-1][0]

        release.set()
        await _wait_for_test_dialog(app, pilot, success=True)


@pytest.mark.asyncio
async def test_test_connect_button_shows_error_dialog() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        panel.test_adapter.test_connection = AsyncMock(side_effect=RuntimeError("connect failed"))  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        assert _dialog_status(dialog) == "Connection Failed"
        assert "connect failed" in _dialog_message(dialog)
        assert not app.notifications


@pytest.mark.asyncio
async def test_test_connect_button_surfaces_tool_name_collision_guidance() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        reserved = {"explore_agent"}
        panel = MCPConfigPanel([], additional_reserved_tool_names=lambda: reserved)
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"
        collision = MCPToolNameCollisionError(
            "github",
            "stdio",
            conflicting_names={"explore_agent"},
            conflict_with="a reserved iCode tool",
            guidance="Exclude the remote tool from the Permitted Tool Set or configure a Tool Name Prefix.",
        )

        async def fail_with_collision(_config: object) -> None:
            assert "explore_agent" in panel.test_adapter._reserved_tool_names  # type: ignore[attr-defined]
            raise collision

        panel.test_adapter.test_connection = AsyncMock(side_effect=fail_with_collision)  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert _dialog_status(dialog) == "Connection Failed"
        assert message.startswith("MCP tool configuration conflict.")
        assert "has invalid tool configuration" in message
        assert "failed to connect" not in message
        assert "explore_agent" in message
        assert "Permitted Tool Set" in message
        assert "Tool Name Prefix" in message


@pytest.mark.asyncio
async def test_test_connect_button_surfaces_provider_invalid_tool_name() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"
        invalid_name = "x" * 65
        invalid = MCPToolNameValidationError(
            "github",
            "stdio",
            violations={
                f"tool name {invalid_name!r} is 65 characters; the maximum is 64. Remote catalog name: 'remote'."
            },
        )
        panel.test_adapter.test_connection = AsyncMock(side_effect=invalid)  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert message.startswith("MCP tool configuration conflict.")
        assert "has invalid tool configuration" in message
        assert "maximum is 64" in message
        assert "Remote catalog name" in message


@pytest.mark.asyncio
async def test_test_connect_button_surfaces_missing_env_template() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        cause = EnvVarResolutionError(
            "Environment variable 'CHRYS_MCP_TOKEN' is required by MCP server 'github' env['TOKEN'], but it is not set."
        )
        panel.test_adapter.test_connection = AsyncMock(side_effect=MCPConnectionError("github", "stdio", cause))  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert "CHRYS_MCP_TOKEN" in message
        assert "MCP server 'github' env['TOKEN']" in message
        assert "not set" in message


@pytest.mark.asyncio
async def test_test_connect_button_strips_tabpane_noise_from_error() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        panel.test_adapter.test_connection = AsyncMock(
            side_effect=RuntimeError("No nodes match '#mcp-url-0' on TabPane(id='mcp')")
        )  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert "No nodes match '#mcp-url-0'" in message
        assert "TabPane(id='mcp')" not in message


@pytest.mark.asyncio
async def test_test_connect_button_shows_timeout_dialog() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        panel.test_adapter.test_connection = AsyncMock(side_effect=TimeoutError("request timed out"))  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert "Connection test timed out." in message


@pytest.mark.asyncio
async def test_test_connect_button_shows_http_connect_failure_dialog() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        panel.test_adapter.test_connection = AsyncMock(side_effect=httpx.ConnectError("connection refused"))  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert "Unable to reach MCP server." in message


async def test_an_unusable_working_directory_leads_the_test_report_with_the_field_to_check(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    adapter = MCPAdapter(stdio_cwd=str(tmp_path))
    config = MCPServerConfig(name="cwd", transport="stdio", command="python", cwd="no-such-folder")
    with pytest.raises(MCPConnectionError) as caught:
        await asyncio.wait_for(adapter.test_connection(config), timeout=ENGINE_TURN_TIMEOUT)

    shown = MCPConfigPanel._format_connection_error(caught.value)

    assert shown.startswith(
        "The server's working directory cannot be used. Please check the Working Directory field.\n\n"
    )
    assert "failed to connect: the server's working directory does not exist" in shown
    assert f"Working directory: {tmp_path / 'no-such-folder'}" in shown
    assert "(missing)" not in shown


@pytest.mark.asyncio
async def test_test_connect_cancelled_error_keeps_panel_and_shows_dialog() -> None:
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        card.query_one("#mcp-cmd-0", TextArea).text = "python -V"

        panel.test_adapter.test_connection = AsyncMock(
            side_effect=asyncio.CancelledError("Cancelled via cancel scope 10d360440")
        )  # type: ignore[method-assign]

        card.query_one("#mcp-test-btn-0").press()
        dialog = await _wait_for_test_dialog(app, pilot, success=False)

        message = _dialog_message(dialog)
        assert "Connection test was cancelled." in message
        assert len(await _wait_for_mcp_cards(panel, pilot, 1)) == 1


@pytest.mark.asyncio
async def test_expose_instructions_checkbox_roundtrip() -> None:
    """The instructions checkbox lives in the server-instructions group and round-trips."""

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([])
        await app.mount(panel)
        await pilot.pause()

        card = await _add_mcp_card(panel, pilot)
        section = card.query_one("#mcp-prompts-instructions-section-0")
        assert str(section.border_title) == "Server Instructions & Prompt Templates"

        checkbox = card.query_one("#mcp-expose-instructions-0", Checkbox)
        assert checkbox.value is True

        tooltip = checkbox.tooltip
        assert tooltip is not None
        assert "usage instructions from the MCP handshake" in str(tooltip)

        # Instructions checkbox is listed before the prompts checkbox.
        prompts_checkbox = card.query_one("#mcp-load-prompts-0", Checkbox)
        await wait_for(
            lambda: (
                checkbox.region.height > 0
                and prompts_checkbox.region.height > 0
                and screen_is_settled(pilot.app, pilot.app.screen)
            ),
            pilot=pilot,
            description="MCP guidance checkboxes have completed layout",
        )
        assert checkbox.region.y < prompts_checkbox.region.y

        # Hint is visible while checked; unchecking collapses it but keeps a
        # one-row gap to the next row via the -hint-collapsed margin.
        hint = card.query_one("#mcp-expose-instructions-hint-0", Label)
        assert hint.display is True
        assert not checkbox.has_class("-hint-collapsed")
        checkbox.value = False
        await wait_for(lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot)
        assert hint.display is False
        assert checkbox.has_class("-hint-collapsed")
        assert prompts_checkbox.region.y - checkbox.region.bottom == 1

        # The instructions toggle leaves the prompts flag untouched.
        config = card.get_config()
        assert config.expose_instructions is False
        assert config.load_prompts is True

        # The prompts checkbox drives its own hint the same way.
        prompts_hint = card.query_one("#mcp-load-prompts-hint-0", Label)
        assert prompts_hint.display is True
        prompts_checkbox.value = False
        await pilot.pause()
        assert prompts_hint.display is False
        assert prompts_checkbox.has_class("-hint-collapsed")

        config = card.get_config()
        assert config.expose_instructions is False
        assert config.load_prompts is False


@pytest.mark.asyncio
async def test_guidance_hints_start_collapsed_for_seeded_false_config() -> None:
    """A card seeded with both guidance flags False mounts unchecked with hints collapsed."""

    from chrys.service.profiles.agents.schema import MCPServerConfig

    seed = MCPServerConfig(
        name="local",
        transport="stdio",
        command="python",
        load_prompts=False,
        expose_instructions=False,
    )
    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([seed])
        await app.mount(panel)
        await pilot.pause()

        card = (await _wait_for_mcp_cards(panel, pilot, 1))[0]
        for checkbox_id, hint_id in (
            ("#mcp-expose-instructions-0", "#mcp-expose-instructions-hint-0"),
            ("#mcp-load-prompts-0", "#mcp-load-prompts-hint-0"),
        ):
            checkbox = card.query_one(checkbox_id, Checkbox)
            assert checkbox.value is False
            assert checkbox.has_class("-hint-collapsed")
            assert card.query_one(hint_id, Label).display is False

        config = card.get_config()
        assert config.expose_instructions is False
        assert config.load_prompts is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "label", "placeholder"),
    [
        ("en", "Working Directory", "Blank uses the workspace directory"),
        ("zh-Hans", "工作目录", "留空则使用工作区目录"),
    ],
)
@pytest.mark.parametrize("width", [80, 120])
async def test_stdio_card_edits_adds_and_clears_the_working_directory(
    locale: str, label: str, placeholder: str, width: int
) -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(width, 40)) as pilot:
        panel = MCPConfigPanel(
            [
                MCPServerConfig(name="set", transport="stdio", command="python", cwd="~/srv"),
                MCPServerConfig(name="unset", transport="stdio", command="python"),
            ],
            card_folds=[False, False],
        )
        await app.mount(panel)
        set_card, unset_card = await _wait_for_mcp_cards(panel, pilot, 2)

        set_input = set_card.query_one("#mcp-cwd-0", Input)
        unset_input = unset_card.query_one("#mcp-cwd-1", Input)
        assert set_input.value == "~/srv"
        assert unset_input.value == "" and unset_input.placeholder == placeholder
        cwd_label = next(widget for widget in unset_card.query(Label) if str(widget.content) == label)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        panel.scroll_to_center(unset_input, animate=False, immediate=True)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert panel.scrollable_content_region.contains_region(cwd_label.region)
        assert panel.scrollable_content_region.contains_region(unset_input.region)
        assert cwd_label.content_size.width >= cell_len(label)
        assert unset_input.content_size.width >= cell_len(placeholder)
        assert cwd_label.region.bottom <= unset_input.region.y

        unset_input.value = "  tools/srv  "
        set_input.value = ""

        configs = panel.get_config()
        assert [config.cwd for config in configs] == ["", "tools/srv"]


@pytest.mark.asyncio
async def test_working_directory_is_disabled_in_read_only_cards_and_rejects_nul() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        read_only = MCPConfigPanel(
            [MCPServerConfig(name="locked", transport="stdio", command="python", cwd="/srv")], read_only=True
        )
        await app.mount(read_only)
        (locked_card,) = await _wait_for_mcp_cards(read_only, pilot, 1)
        assert locked_card.query_one("#mcp-cwd-0", Input).disabled is True
        assert locked_card.query_one("#mcp-cwd-0", Input).value == "/srv"

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel([MCPServerConfig(name="srv", transport="stdio", command="python")])
        await app.mount(panel)
        (card,) = await _wait_for_mcp_cards(panel, pilot, 1)
        card.query_one("#mcp-cwd-0", Input).value = "a\0b"
        assert any("NUL" in error for error in card.validate())


@pytest.mark.asyncio
async def test_http_card_has_no_working_directory_and_never_returns_one() -> None:
    from chrys.service.profiles.agents.schema import MCPServerConfig

    app = _MCPPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MCPConfigPanel(
            [MCPServerConfig(name="remote", transport="http", url="https://example.com/mcp", cwd="/srv")]
        )
        await app.mount(panel)
        (card,) = await _wait_for_mcp_cards(panel, pilot, 1)

        assert not card.query("#mcp-cwd-0")
        assert panel.get_config()[0].cwd == ""
