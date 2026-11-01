# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP picker rendering and reuse through the live agent configuration screen."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, OptionList, Static, TextArea

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.mcp_choices import collect_mcp_choices
from chrys.app.tui.screens.agents.mcp_picker import MCPPickerDialog, MCPPickerResult
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, MCPConnectionCard
from chrys.foundation.config.settings import Settings
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import MCPServerConfig, ToolsConfig
from chrys.service.profiles.agents.serializer import save_profile
from tests.app.tui.screens._agent_config_support import make_profile, open_agent_config, registry_with
from tests.support.waiting import wait_for


class _PickerApp(App[None]):
    def __init__(self, *, locale: str = "en") -> None:
        self.locale_controller = LocaleController(Settings(locale=locale))
        self.results: list[MCPPickerResult] = []
        self.server = MCPServerConfig(
            name="[server]\x1b\n", transport="stdio", command="/bin/server", description="[desc]\x1b\n"
        )
        existing = MCPServerConfig(name="0-existing", transport="stdio", command="existing")
        choices = collect_mcp_choices(
            [
                make_profile(
                    "Code\x1b\n",
                    tools=ToolsConfig(mcp=[self.server, existing, replace(self.server, name="invalid", command="")]),
                ),
            ]
        )
        self.picker = MCPPickerDialog(choices, [existing])
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(self.picker, self.results.append)


@pytest.mark.parametrize("transport,field", [("stdio", "env"), ("http", "headers")])
async def test_numeric_yaml_values_can_be_reused_expanded_and_saved(tmp_path: Path, transport: str, field: str) -> None:
    path = tmp_path / "source.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Code",
                "tools": {
                    "mcp": [
                        {
                            "name": "server",
                            "transport": transport,
                            "command": "server",
                            "url": "https://example.com/mcp",
                            field: {"PORT": 8080, "TOKEN": "{{!must-not-run}}"},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    registry = registry_with(
        load_profile_from_yaml(path), make_profile("QA", display_name="Q&A", description="Answers questions")
    )
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="numeric YAML mapping is reusable",
        )
        await pilot.press("enter")
        await wait_for(
            lambda: len(panel.query(MCPConnectionCard)) == 1 and not screen.query_one("#ac-save", Button).disabled,
            pilot=pilot,
            description="numeric YAML card expands",
        )
        card = panel.query_one(MCPConnectionCard)
        assert not card.collapsed
        assert [item.value for item in card.query(".mcp-item-value-input")] == ["8080", "{{!must-not-run}}"]
        screen.query_one("#ac-save", Button).press()
        await wait_for(
            lambda: screen.query_one("#ac-save", Button).disabled,
            pilot=pilot,
            description="numeric mapping saves",
        )
        target = registry.get("QA")
        assert target is not None
        saved = save_profile(target, target_dir=tmp_path / "saved")
        server = load_profile_from_yaml(saved).tools.mcp[0]
        assert (server.env if field == "env" else server.headers) == {"PORT": "8080", "TOKEN": "{{!must-not-run}}"}


async def test_picker_sanitizes_literal_text_and_delivers_only_one_selection() -> None:
    app = _PickerApp()
    async with app.run_test() as pilot:
        await wait_for(
            lambda: app.picker.is_mounted and bool(app.picker.query(OptionList)),
            pilot=pilot,
            description="picker mounted",
        )
        options = app.picker.query_one(OptionList)
        assert [option.id for option in options.options] == ["server:1", "server:0", "action:create"]
        assert options.get_option("server:0").disabled
        assert options.highlighted == 0
        option = options.get_option("server:1")
        text = option.prompt.plain
        assert "[server]" in text and "[desc]" in text
        assert "\x1b" not in text
        assert "STDIO · server" in text
        assert "Sources: Code" in text
        assert "1 invalid configuration hidden" in options.get_option("action:create").prompt.plain
        for _ in range(2):
            options.post_message(OptionList.OptionSelected(options, option, 0))
        await wait_for(lambda: bool(app.results), pilot=pilot, description="selection delivered")
        assert len(app.results) == 1
        assert isinstance(app.results[0], MCPServerConfig)
        assert app.results[0] == app.server


async def test_picker_localizes_and_escape_returns_no_selection() -> None:
    app = _PickerApp(locale="zh-Hans")
    async with app.run_test() as pilot:
        await wait_for(
            lambda: app.picker.is_mounted and bool(app.picker.query(OptionList)),
            pilot=pilot,
            description="picker mounted",
        )
        assert app.picker.query_one("#container").border_title == "添加 MCP 服务器"
        assert app.picker.query_one(OptionList).get_option("action:create").prompt.plain.startswith("+ 新建 MCP 配置…")
        await pilot.press("escape")
        assert app.results == [None]


@pytest.mark.usefixtures("isolated_chrys_config_dir")
async def test_reuse_copies_all_settings_and_saves_without_changing_source() -> None:
    source = MCPServerConfig(
        name="some-mcp-server",
        transport="http",
        url="https://example.com/mcp",
        headers={"Authorization": "{{TOKEN}}"},
        enabled=False,
        allowed_tools=["read"],
        use_progressive_disclosure=True,
        always_load=["read"],
        request_timeout=19,
        max_tool_result_tokens=321,
        load_prompts=False,
        expose_instructions=False,
        resolve_header_templates=False,
        terminate_on_close=False,
        verify_ssl=False,
        bypass_proxy=True,
    )
    registry = registry_with(
        make_profile("Code", tools=ToolsConfig(mcp=[source])),
        make_profile("QA", display_name="Q&A", description="Answers questions"),
    )
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        save = screen.query_one("#ac-save", Button)
        assert save.disabled
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="reuse picker mounted",
        )
        assert save.disabled
        picker = pilot.app.screen
        assert isinstance(picker, MCPPickerDialog)
        assert "Disabled" in picker.query_one(OptionList).get_option("server:0").prompt.plain
        await pilot.press("enter")
        await wait_for(
            lambda: len(panel.query(MCPConnectionCard)) == 1 and not save.disabled,
            pilot=pilot,
            description="copied card dirties target",
        )
        assert panel.get_config() == [source]
        assert not panel.query_one(MCPConnectionCard).collapsed
        screen.query_one("#ac-save", Button).press()
        await wait_for(lambda: save.disabled, pilot=pilot, description="copied configuration saved")
        target = registry.get("QA")
        assert target is not None and target.tools.mcp == [source]
        target.tools.mcp[0].headers["Authorization"] = "changed"
        assert source.headers == {"Authorization": "{{TOKEN}}"}
        code = registry.get("Code")
        assert code is not None and code.tools.mcp == [source]


@pytest.mark.parametrize("close_with_pop", [False, True])
async def test_open_cancel_and_repeated_add_do_not_dirty_or_stack_pickers(
    monkeypatch: pytest.MonkeyPatch, close_with_pop: bool
) -> None:
    source = MCPServerConfig(name="server", transport="stdio", command="uvx")
    registry = registry_with(make_profile("Code", tools=ToolsConfig(mcp=[source])), make_profile("QA"))
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, pilot):
        dirty_states: list[bool] = []
        original = screen._set_draft_dirty

        def record(draft, dirty: bool) -> None:
            dirty_states.append(dirty)
            original(draft, dirty)

        monkeypatch.setattr(screen, "_set_draft_dirty", record)
        panel = screen.query_one(MCPConfigPanel)
        button = panel.query_one("#mcp-add-btn", Button)
        button.press()
        button.press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="single picker mounted",
        )
        assert sum(isinstance(item, MCPPickerDialog) for item in pilot.app.screen_stack) == 1
        if close_with_pop:
            await pilot.app.pop_screen()
        else:
            await pilot.press("escape")
        assert pilot.app.screen is screen
        assert not any(dirty_states)
        assert screen.query_one("#ac-save", Button).disabled
        assert not panel.query(MCPConnectionCard)
        button.press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="picker reopens after cancellation",
        )
        await pilot.press("enter")
        await wait_for(
            lambda: len(panel.query(MCPConnectionCard)) == 1,
            pilot=pilot,
            description="reopened picker adds its selection",
        )


async def test_no_valid_sources_creates_unique_blank_cards_without_picker() -> None:
    invalid = MCPServerConfig(name="incomplete", transport="stdio")
    registry = registry_with(make_profile("Code", tools=ToolsConfig(mcp=[invalid])), make_profile("QA"))
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(lambda: len(panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="blank card added")
        assert pilot.app.screen is screen
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(
            lambda: len(panel.query(MCPConnectionCard)) == 2, pilot=pilot, description="second blank card added"
        )
        assert [config.name for config in panel.get_config()] == ["New Server 2", "New Server"]


async def test_add_ignores_malformed_yaml_timeout_and_copies_valid_sibling(tmp_path: Path) -> None:
    path = tmp_path / "source.yaml"
    path.write_text(
        "name: Code\ntools:\n  mcp:\n    - name: broken\n      transport: stdio\n"
        '      command: server\n      timeout: "30"\n'
        "    - name: valid\n      transport: stdio\n      command: valid-server\n",
        encoding="utf-8",
    )
    registry = registry_with(load_profile_from_yaml(path), make_profile("QA"))
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="picker survives malformed source timeout",
        )
        options = pilot.app.screen.query_one(OptionList)
        assert options.option_count == 2
        assert "1 invalid configuration hidden" in options.get_option("action:create").prompt.plain
        await pilot.press("enter")
        await wait_for(
            lambda: len(panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="valid sibling copied"
        )
        assert panel.get_config()[0].name == "valid"


async def test_live_cards_block_same_name_even_when_invalid_command_keeps_draft_stale() -> None:
    source = MCPServerConfig(name="new-name", transport="stdio", command="uvx")
    target = MCPServerConfig(name="old-name", transport="stdio", command="python")
    registry = registry_with(
        make_profile("Code", tools=ToolsConfig(mcp=[source])), make_profile("QA", tools=ToolsConfig(mcp=[target]))
    )
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        panel.query_one(MCPConnectionCard).set_collapsed(False)
        panel.query_one("#mcp-cmd-0", TextArea).text = 'python "'
        panel.query_one("#mcp-name-0", Input).value = "NEW-NAME"
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="picker reads live cards",
        )
        options = pilot.app.screen.query_one(OptionList)
        assert options.get_option("server:0").disabled
        assert "different settings" in options.get_option("server:0").prompt.plain
        option = options.get_option("action:create")
        options.post_message(OptionList.OptionSelected(options, option, 1))
        await wait_for(
            lambda: len(panel.query(MCPConnectionCard)) == 2, pilot=pilot, description="create action adds blank card"
        )
        assert panel.query_one("#mcp-cmd-1", TextArea).text == 'python "'
        assert panel.query_one("#mcp-name-1", Input).value == "NEW-NAME"


async def test_callback_rechecks_current_list_before_adding() -> None:
    source = MCPServerConfig(name="server", transport="stdio", command="uvx")
    registry = registry_with(make_profile("Code", tools=ToolsConfig(mcp=[source])), make_profile("QA"))
    async with open_agent_config(registry, current_profile="QA", initial_tab="mcp") as (screen, _pilot):
        panel = screen.query_one(MCPConfigPanel)
        await panel._add_selected(source)
        await panel._add_selected(source)
        await panel._add_selected(replace(source, name="alias"))
        assert len(panel.query(MCPConnectionCard)) == 1
        assert panel.get_config() == [source]
        await panel.remove()
        await panel._add_selected(replace(source, name="after-removal", command="other"))
        assert len(panel._rows) == 1


async def test_picker_uses_latest_unsaved_other_agent_draft() -> None:
    source = MCPServerConfig(name="server", transport="stdio", command="uvx", args=["original"])
    registry = registry_with(make_profile("Code", tools=ToolsConfig(mcp=[source])), make_profile("QA"))
    async with open_agent_config(registry, current_profile="Code", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        panel.query_one(MCPConnectionCard).set_collapsed(False)
        panel.query_one("#mcp-cmd-0", TextArea).text = "uvx edited"
        sidebar = screen.query_one("#ac-list", OptionList)
        option = sidebar.get_option("profile:QA")
        sidebar.post_message(OptionList.OptionSelected(sidebar, option, sidebar.get_option_index(option.id)))
        await wait_for(
            lambda: screen._selected_profile_name == "QA" and not screen._hydrating,
            pilot=pilot,
            description="switched to target agent",
        )
        panel = screen.query_one(MCPConfigPanel)
        panel.query_one("#mcp-add-btn", Button).press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, MCPPickerDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="picker of unsaved draft mounted",
        )
        assert "modified" not in pilot.app.screen.query_one(OptionList).get_option("server:0").prompt.plain
        await pilot.press("enter")
        await wait_for(lambda: len(panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="unsaved copy added")
        assert panel.get_config()[0].args == ["edited"]
        assert source.args == ["original"]
