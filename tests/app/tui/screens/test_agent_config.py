# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the agent configuration screen: composition, dirty tracking, staged profiles, read-only mode."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Input,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

from chrys.app.tui.screens.agents import config as config_mod
from chrys.app.tui.screens.agents.config import AgentsConfigScreen
from chrys.app.tui.screens.agents.panels.basic import BasicConfigPanel
from chrys.app.tui.screens.agents.panels.compaction import CompactionConfigPanel
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    MCPServerConfig,
    MemoryConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from tests.app.tui.screens._agent_config_support import (
    _DEFAULT_WAIT_TIMEOUT,
    _activate_agent_config_tab,
    _draft_for_original,
    _registry,
    _sidebar_text,
    _wait_for_hydrated,
    _wait_for_selectors,
    make_profile,
    open_agent_config,
    press_and_answer_confirm,
    registry_with,
)
from tests.support.waiting import wait_for

# The polling helpers in _agent_config_support share one 45s ceiling, below
# the repository's 60s default per-test timeout. This module retains a 120s
# override for expensive screen mounting before the first wait; once polling
# starts, the helper deadline still produces a clean AssertionError before the
# thread timeout can kill the xdist worker and surface only "worker gwN crashed".
pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_chrys_config_dir")]


def test_agent_config_canonicalizes_mcp_always_load_names_without_dropping_progressive_mode() -> None:
    profile = AgentProfile(
        name="Progressive",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(
                    name="remote",
                    transport="http",
                    url="https://api.example.com/mcp",
                    use_progressive_disclosure=True,
                    always_load=[" search ", "", "read_file "],
                )
            ]
        ),
    )

    AgentsConfigScreen._canonicalize_profile_for_ui(profile)

    assert profile.tools.mcp[0].use_progressive_disclosure is True
    assert profile.tools.mcp[0].always_load == ["search", "read_file"]


async def test_agent_config_opens_a_padded_mcp_working_directory_without_marking_it_modified() -> None:
    registry = registry_with(
        AgentProfile(
            name="Local",
            display_name="Local",
            tools=ToolsConfig(
                mcp=[
                    MCPServerConfig(name="spaces", transport="stdio", command="python", cwd=" srv "),
                    MCPServerConfig(name="newline", transport="stdio", command="python", cwd="tools/srv\n"),
                ]
            ),
        )
    )

    async with open_agent_config(registry, current_profile="Local", initial_tab="mcp") as (screen, _pilot):
        panel = screen.query_one(MCPConfigPanel)
        await wait_for(lambda: len(panel.query("MCPConnectionCard")) == 2, timeout=_DEFAULT_WAIT_TIMEOUT)
        draft = _draft_for_original(screen, "Local")

        assert [config.cwd for config in panel.get_config()] == ["srv", "tools/srv"]
        assert draft.dirty is False
        assert screen._build_profile_from_mounted_panels(draft) == draft.original_profile


def test_agent_config_clears_initial_subset_for_full_or_empty_loading_policy() -> None:
    full = MCPServerConfig(
        name="full",
        transport="http",
        url="https://api.example.com/full",
        always_load=["stale"],
    )
    empty = MCPServerConfig(
        name="empty",
        transport="http",
        url="https://api.example.com/empty",
        allowed_tools=[],
        use_progressive_disclosure=True,
        always_load=["stale"],
    )
    profile = AgentProfile(name="Policy", tools=ToolsConfig(mcp=[full, empty]))

    AgentsConfigScreen._canonicalize_profile_for_ui(profile)

    assert full.use_progressive_disclosure is False
    assert full.always_load == []
    assert empty.use_progressive_disclosure is False
    assert empty.always_load == []


def test_agent_config_exposes_live_sub_agent_tool_names_to_mcp_diagnostics() -> None:
    registry = registry_with(
        AgentProfile(
            name="Parent",
            sub_agents=SubAgentsConfig(
                agents=[
                    SubAgentRef(profile="Explore", tool_name="explore_agent"),
                    SubAgentRef(profile="General"),
                ]
            ),
        ),
    )
    screen = AgentsConfigScreen(registry, current_profile="Parent")
    screen._initialize_drafts()
    screen._selected_draft_key = screen._existing_draft_key("Parent")

    assert screen._selected_sub_agent_tool_names() == {"explore_agent", "General"}

    draft = screen._drafts[screen._selected_draft_key]
    draft.profile.sub_agents.agents[0].tool_name = "research_agent"
    assert screen._selected_sub_agent_tool_names() == {"research_agent", "General"}


async def test_mounted_mcp_panel_receives_selected_draft_sub_agent_names() -> None:
    registry = registry_with(
        AgentProfile(
            name="Parent",
            display_name="Parent Agent",
            tools=ToolsConfig(mcp=[]),
            sub_agents=SubAgentsConfig(
                agents=[
                    SubAgentRef(profile="Explore", tool_name="explore_agent"),
                    SubAgentRef(profile="General"),
                ]
            ),
        ),
        AgentProfile(name="Explore", sub_agent_only=True),
        AgentProfile(name="General", sub_agent_only=True),
    )

    async with open_agent_config(registry, current_profile="Parent Agent", initial_tab="mcp") as (screen, _pilot):
        panel = screen.query_one(MCPConfigPanel)
        assert {"explore_agent", "General"} <= panel._current_reserved_tool_names()


@pytest.mark.parametrize("initial_tab", ["basic", "compaction", "instructions"])
async def test_agent_config_builtin_profiles_round_trip_cleanly_through_panels(initial_tab: str) -> None:
    registry = _registry()
    profiles = registry.list_profiles(include_sub_agent_only=True)

    async with open_agent_config(registry, current_profile="Code", initial_tab=initial_tab) as (screen, pilot):
        for profile in profiles:
            draft = _draft_for_original(screen, profile.name)
            screen._load_profile(draft.key)
            await pilot.pause()
            await _wait_for_hydrated(screen, pilot)

            rebuilt = screen._build_profile_from_mounted_panels(draft)
            expected = copy.deepcopy(profile)
            screen._canonicalize_profile_for_ui(expected)

            assert rebuilt == expected, profile.name


async def test_agent_config_save_button_and_modified_marker_follow_pending_changes() -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        save = screen.query_one("#ac-save", Button)
        original_display = screen.query_one("#bc-display-name", Input).value
        selected_key = screen._selected_draft_key
        await wait_for(
            lambda: save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save disabled after initial hydration",
        )
        assert "(modified)" not in _sidebar_text(screen, selected_key)

        screen.query_one("#bc-display-name", Input).focus()
        screen.query_one("#bc-display-name", Input).value = "Code Agent Edited"
        await wait_for(
            lambda: not save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after first edit",
        )
        assert screen._drafts[selected_key].dirty is True
        assert "(modified)" in _sidebar_text(screen, selected_key)

        screen.query_one("#bc-display-name", Input).value = original_display
        await wait_for(
            lambda: save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="save disabled after revert"
        )
        assert screen._drafts[selected_key].dirty is False
        assert "(modified)" not in _sidebar_text(screen, selected_key)

        screen.query_one("#bc-display-name", Input).value = "Code Agent Edited"
        await wait_for(
            lambda: not save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after re-edit",
        )
        assert screen._drafts[selected_key].dirty is True
        assert "(modified)" in _sidebar_text(screen, selected_key)

        save.press()
        await wait_for(
            lambda: save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="save disabled after save"
        )

        saved = registry.get("Code")
        assert saved is not None
        assert saved.display_name == "Code Agent Edited"
        assert "(modified)" not in _sidebar_text(screen, screen._selected_draft_key)


async def test_agent_config_invalid_mcp_text_change_still_marks_dirty() -> None:
    registry = registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with editable MCP config",
            tools=ToolsConfig(mcp=[MCPServerConfig(name="local", transport="stdio", command="python", args=["-V"])]),
        ),
    )

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        save = screen.query_one("#ac-save", Button)
        assert save.disabled is True

        command = screen.query_one("#mcp-cmd-0", TextArea)
        command.focus()
        command.text = '"'
        await wait_for(
            lambda: not save.disabled and screen._drafts[screen._selected_draft_key].dirty,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after invalid MCP command edit",
        )

        assert save.disabled is False
        assert screen._drafts[screen._selected_draft_key].dirty is True
        assert "(modified)" in _sidebar_text(screen, screen._selected_draft_key)

        command.text = "python -V"
        await wait_for(
            lambda: save.disabled and not screen._drafts[screen._selected_draft_key].dirty,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save disabled after MCP command revert",
        )

        assert save.disabled is True
        assert screen._drafts[screen._selected_draft_key].dirty is False
        assert "(modified)" not in _sidebar_text(screen, screen._selected_draft_key)


@pytest.mark.parametrize(
    ("tab", "profile_fields", "extra_profiles", "cards_selector", "delete_selector", "card_count", "asks_first"),
    [
        pytest.param(
            "mcp",
            {
                "tools": ToolsConfig(
                    mcp=[MCPServerConfig(name="local", transport="stdio", command="python", args=["-V"])]
                )
            },
            (),
            "#mcp-cards",
            "#mcp-delete-btn-0",
            1,
            True,
            id="mcp_server",
        ),
        pytest.param(
            "sub-agents",
            {"sub_agents": SubAgentsConfig(agents=[SubAgentRef(profile="Child")])},
            (make_profile("Child", display_name="Child Agent", description="Child agent", sub_agent_only=True),),
            "#sa-cards",
            "#sa-delete-btn-0",
            1,
            False,
            id="sub_agent",
        ),
        pytest.param(
            "memory",
            {"memory": MemoryConfig(files=["docs/a.md", "docs/b.md"])},
            (),
            "#mem-files",
            "#mem-delete-btn-0",
            2,
            False,
            id="memory_file",
        ),
        pytest.param(
            "skills",
            {"skills": SkillsConfig(paths=["skills/a", "skills/b"])},
            (),
            "#sk-dirs",
            "#sk-delete-btn-0",
            2,
            False,
            id="skill_path",
        ),
    ],
)
async def test_agent_config_delete_card_marks_draft_dirty(
    tab: str,
    profile_fields: dict[str, object],
    extra_profiles: tuple[AgentProfile, ...],
    cards_selector: str,
    delete_selector: str,
    card_count: int,
    asks_first: bool,
) -> None:
    """Removing a card on any list-shaped tab marks the draft dirty and enables Save."""
    registry = registry_with(
        make_profile(
            "card-agent",
            display_name="Card Agent",
            description="Agent with removable cards",
            **profile_fields,
        ),
        *extra_profiles,
    )

    async with open_agent_config(registry, current_profile="Card Agent", initial_tab=tab) as (screen, pilot):
        save = screen.query_one("#ac-save", Button)
        # Poll rather than assert instantaneously: on slow Windows CI a settle
        # event can momentarily set dirty just after hydration returns; the
        # clean baseline self-corrects once the queued events drain.
        await wait_for(
            lambda: save.disabled and not screen._drafts[screen._selected_draft_key].dirty,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description=f"clean baseline after {tab} hydration",
        )

        cards = screen.query_one(cards_selector)
        assert len(list(cards.query(".agent-config-card"))) == card_count

        delete_button = cards.query_one(delete_selector, Button)
        if asks_first:
            await press_and_answer_confirm(pilot, delete_button)
        else:
            delete_button.press()
        await wait_for(
            lambda: (
                len(list(cards.query(".agent-config-card"))) == card_count - 1
                and not save.disabled
                and screen._drafts[screen._selected_draft_key].dirty
            ),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description=f"save enabled after {tab} card delete",
        )


@pytest.mark.parametrize("row_kind", ["env", "headers"])
async def test_agent_config_delete_mcp_key_value_row_marks_draft_dirty(row_kind: str) -> None:
    if row_kind == "env":
        server_config = MCPServerConfig(
            name="local",
            transport="stdio",
            command="python",
            env={"TOKEN": "abc", "NO_PROXY": "*"},
        )
        container_selector = "#mcp-env-0"
    else:
        server_config = MCPServerConfig(
            name="remote",
            transport="http",
            url="https://api.example.test/mcp",
            headers={"Authorization": "Bearer token", "X-Team": "platform"},
        )
        container_selector = "#mcp-headers-0"

    registry = registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with removable MCP key-value rows",
            tools=ToolsConfig(mcp=[server_config]),
        ),
    )

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        save = screen.query_one("#ac-save", Button)
        assert save.disabled is True

        container = screen.query_one(container_selector)
        remove_buttons = list(container.query(".mcp-remove-btn"))
        assert len(remove_buttons) == 2

        remove_buttons[0].press()
        await wait_for(
            lambda: (
                len(list(container.query(".mcp-item-row"))) == 1
                and not save.disabled
                and screen._drafts[screen._selected_draft_key].dirty
            ),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description=f"save enabled after MCP {row_kind} row delete",
        )


async def test_agent_config_transient_panels_not_ready_does_not_force_dirty() -> None:
    """A forced re-evaluation that hits ``_AgentConfigPanelsNotReady`` must not mark dirty.

    Regression: a field-change event firing in the post-hydration settle window drove
    ``_mark_selected_dirty(force=True)`` into the build-failure branch, which force-set
    a sticky spurious dirty and wrongly enabled Save (intermittent CI failures on the
    dirty-tracking tests). A transient "panels not ready" read must leave the flag alone.

    Uses a plain profile and drives the path directly so the assertion does not race
    panel hydration timing.
    """
    registry = registry_with(
        make_profile(
            "Solo",
            display_name="Solo Agent",
            description="Agent without dynamic panels",
        ),
    )

    async with open_agent_config(registry, current_profile="Solo Agent", initial_tab="basic") as (screen, _pilot):
        draft = screen._drafts[screen._selected_draft_key]
        screen._set_draft_dirty(draft, False)  # baseline clean, independent of hydration timing

        # Simulate a forced field-change re-evaluation while the panels are
        # transiently unreadable, as they can be during the settle window.
        def _raise_not_ready(_draft: object) -> object:
            raise config_mod._AgentConfigPanelsNotReady("panels still mounting")

        original_build = screen._build_profile_from_mounted_panels
        screen._build_profile_from_mounted_panels = _raise_not_ready  # type: ignore[method-assign]
        try:
            screen._mark_selected_dirty(force=True)
        finally:
            screen._build_profile_from_mounted_panels = original_build  # type: ignore[method-assign]

        assert draft.dirty is False
        assert screen.query_one("#ac-save", Button).disabled is True


async def test_agent_config_lazy_tab_mount_uses_hydration_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        mounted_while_hydrating: list[bool] = []
        original_mount_tools = screen._mount_tools_tab

        def mount_tools_with_probe(profile) -> None:
            mounted_while_hydrating.append(screen._hydrating)
            original_mount_tools(profile)

        monkeypatch.setattr(screen, "_mount_tools_tab", mount_tools_with_probe)

        screen.query_one("#ac-tabs", TabbedContent).active = "tools"
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        assert mounted_while_hydrating == [True]
        assert screen.query_one("#ac-save", Button).disabled is True


async def test_agent_config_stale_hydration_completion_does_not_clear_validation_only_dirty() -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code") as (screen, _pilot):
        save = screen.query_one("#ac-save", Button)
        draft = screen._drafts[screen._selected_draft_key]
        assert draft.original_profile is not None
        assert draft.profile == draft.original_profile
        screen._set_draft_dirty(draft, True)
        assert save.disabled is False

        screen._hydrating = True
        screen._hydration_preserve_dirty = True
        screen._hydrating_generation = 2
        screen._complete_hydration(1)

        assert screen._hydrating is True
        assert screen._hydration_preserve_dirty is True
        assert draft.dirty is True
        assert save.disabled is False

        screen._complete_hydration(2)

        assert screen._hydrating is False
        assert screen._hydration_preserve_dirty is False
        assert draft.dirty is True
        assert save.disabled is False


async def test_agent_config_switch_error_includes_actual_validation_errors() -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen.query_one("#bc-display-name", Input).focus()
        screen.query_one("#bc-display-name", Input).value = ""
        await pilot.pause()

        with pytest.raises(RuntimeError) as exc_info:
            screen._sync_selected_draft_from_panels()

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Display name is required."
    assert lines[-1] == "Fix validation errors before switching agents or applying structural changes."


async def test_empty_compaction_supplement_dirty_tracking_round_trips_on_revert() -> None:
    registry = registry_with(
        make_profile(
            "empty-supplement",
            display_name="Empty Supplement",
            description="Compaction supplement test profile",
        ),
    )

    async with open_agent_config(registry, current_profile="empty-supplement", initial_tab="compaction") as (
        screen,
        pilot,
    ):
        save = screen.query_one("#ac-save", Button)
        template = screen.query_one("#cc-last-words-template", TextArea)
        selected_key = screen._selected_draft_key
        await wait_for(
            lambda: save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="empty supplement starts clean",
        )
        assert template.text == ""

        template.text = "Preserve exact benchmark results."
        await wait_for(
            lambda: not save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="supplement edit marks dirty",
        )
        assert screen._drafts[selected_key].dirty is True

        template.text = ""
        await wait_for(
            lambda: save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="empty supplement revert clears dirty",
        )
        assert screen._drafts[selected_key].dirty is False
        assert screen.query_one(CompactionConfigPanel).get_config().last_words_template == ""


async def test_agent_config_new_profile_is_staged_and_close_discards(tmp_path: Path) -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Explore") as (screen, pilot):
        screen.query_one("#ac-new", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        new_profile = screen._drafts[screen._selected_draft_key].profile

        assert registry.get(new_profile.name) is None
        assert not (tmp_path / "agents" / f"{new_profile.name}.yaml").exists()

        screen.action_cancel()
        await pilot.pause()

    assert registry.get("Explore") is not None
    assert registry.get(new_profile.name) is None
    assert not (tmp_path / "agents" / f"{new_profile.name}.yaml").exists()


async def test_agent_config_staged_new_agent_can_be_selected_as_subagent_before_save() -> None:
    registry = registry_with(
        make_profile(
            "Parent",
            display_name="Parent Agent",
            description="Parent profile",
        ),
    )

    async with open_agent_config(registry, current_profile="Parent Agent") as (screen, pilot):
        parent_key = screen._selected_draft_key
        screen.query_one("#ac-new", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        new_profile = screen._drafts[screen._selected_draft_key].profile

        screen._sync_selected_draft_from_panels()
        screen._load_profile(parent_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        await _activate_agent_config_tab(screen, pilot, "sub-agents")
        screen.query_one("#sa-add-btn", Button).press()
        await pilot.pause()
        select = screen.query_one("#sa-profile-0", Select)
        option_values = [value for _prompt, value in select._options]
        assert new_profile.name in option_values

        select.value = new_profile.name
        await pilot.pause()
        screen.query_one("#ac-save", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

    saved_parent = registry.get("Parent")
    saved_new = registry.get(new_profile.name)
    assert saved_parent is not None
    assert saved_new is not None
    assert [ref.profile for ref in saved_parent.sub_agents.agents] == [new_profile.name]


async def test_agent_config_save_ignores_invalid_untouched_draft() -> None:
    registry = registry_with(
        make_profile(
            "Good",
            display_name="Good Agent",
            description="Valid profile",
        ),
        make_profile(
            "stale",
            display_name="",
            description="Legacy invalid profile",
        ),
    )

    async with open_agent_config(registry, current_profile="Good Agent") as (screen, pilot):
        screen.query_one("#bc-display-name", Input).focus()
        screen.query_one("#bc-display-name", Input).value = "Better Agent"
        await pilot.pause()
        # Wait for Input.Changed → _mark_selected_dirty to flip the draft dirty,
        # otherwise the disabled Save button swallows press() and the save becomes
        # a silent no-op on slower Windows CI under xdist load.
        save = screen.query_one("#ac-save", Button)
        await wait_for(
            lambda: not save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after display-name edit",
        )
        save.press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

    saved = registry.get("Good")
    stale = registry.get("stale")
    assert saved is not None
    assert saved.display_name == "Better Agent"
    assert stale is not None
    assert stale.display_name == ""


async def test_agent_config_read_only_hides_mutations_and_disables_controls(tmp_path: Path) -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code", read_only=True) as (screen, pilot):
        assert screen.query_one("#bc-display-name", Input).disabled is True
        assert screen.query_one("#bc-sub-agent-only", Checkbox).disabled is True
        assert screen.query_one("#bc-description", TextArea).read_only is True
        assert screen.query_one("#ac-cancel", Button).display is True
        assert screen.query_one("#ac-cancel", Button).disabled is False
        notice = screen.query_one("#ac-read-only-notice", Static)
        assert notice.display is True
        assert notice.render().plain == "• Agent is running. This page is read-only."
        assert screen.query_one("#ac-buttons-spacer", Static).display is True
        footer = screen.query_one("#ac-footer", Vertical)
        close = screen.query_one("#ac-cancel", Button)
        assert notice.region.y == footer.region.y + footer.region.height - 1
        assert notice.region.y > close.region.y
        assert notice.region.x == footer.region.x + 1
        assert notice.region.width == footer.region.width - 2
        await screen.query_one("#basic").mount(Button("Future", id="future-panel-action"))
        screen._apply_read_only_state()
        future_button = screen.query_one("#future-panel-action", Button)
        assert future_button.display is True
        assert future_button.disabled is True
        for button_id in ("ac-new", "ac-clone", "ac-reset", "ac-delete", "ac-save"):
            button = screen.query_one(f"#{button_id}", Button)
            assert button.display is False
            assert button.disabled is True

        draft_count = len(screen._drafts)
        screen.query_one("#ac-clone", Button).press()
        screen.query_one("#ac-save", Button).press()
        await pilot.pause()

        assert len(screen._drafts) == draft_count
        assert screen._cancel_result() == ""
        assert not (tmp_path / "agents" / "Code.yaml").exists()


async def test_agent_config_read_only_applies_to_lazy_tabs(tmp_path: Path) -> None:
    from chrys.app.tui.screens.agents.panels.memory import MemoryFileCard, MemoryFolderCard

    registry = _registry()
    registry.register(
        AgentProfile(
            name="memory-agent",
            display_name="Memory Agent",
            description="Agent with memory paths",
            sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Explore")]),
            tools=ToolsConfig(mcp=[MCPServerConfig(name="local", transport="stdio", command="python", args=["-V"])]),
            skills=SkillsConfig(paths=[str(tmp_path / "skills")]),
            memory=MemoryConfig(files=[str(tmp_path / "notes.md")], folders=[str(tmp_path / "docs")]),
        )
    )

    async with open_agent_config(registry, current_profile="Memory Agent", read_only=True, hydrated=False) as (
        screen,
        pilot,
    ):
        assert screen.query_one("#ac-tabs ContentSwitcher", ContentSwitcher).disabled is False

        screen.query_one("#ac-tabs", TabbedContent).active = "tools"
        await pilot.pause()
        assert screen.query_one("#tc-cat-filesystem-read", Checkbox).disabled is True

        screen.query_one("#ac-tabs", TabbedContent).active = "sub-agents"
        await pilot.pause()
        await _wait_for_selectors(screen, pilot, "#sa-profile-0", "#sa-delete-btn-0")
        sub_agent_select = screen.query_one("#sa-profile-0", Select)
        sub_agent_delete = screen.query_one("#sa-delete-btn-0", Button)
        assert sub_agent_select.disabled is True
        assert sub_agent_select.is_disabled is True
        assert sub_agent_delete.display is False
        assert sub_agent_delete.disabled is True

        screen.query_one("#ac-tabs", TabbedContent).active = "mcp"
        await pilot.pause()
        await _wait_for_selectors(screen, pilot, "#mcp-test-btn-0")
        mcp_test = screen.query_one("#mcp-test-btn-0", Button)
        assert mcp_test.display is False
        assert mcp_test.disabled is True
        assert screen.query_one("#mcp-name-0", Input).disabled is True
        assert screen.query_one("#mcp-add-btn", Button).display is False

        screen.query_one("#ac-tabs", TabbedContent).active = "skills"
        await pilot.pause()
        await _wait_for_selectors(screen, pilot, "#sk-add-btn", "#sk-ext-py")
        assert screen.query_one("#sk-add-btn", Button).display is False
        assert screen.query_one("#sk-ext-py", Button).display is True
        assert screen.query_one("#sk-ext-py", Button).disabled is True
        await _wait_for_selectors(screen, pilot, "#sk-path-0", "#sk-browse-0", "#sk-delete-btn-0")
        assert screen.query_one("#sk-path-0", Input).disabled is True
        assert screen.query_one("#sk-browse-0", Button).display is False
        assert screen.query_one("#sk-browse-0", Button).disabled is True
        assert screen.query_one("#sk-delete-btn-0", Button).display is False
        assert screen.query_one("#sk-delete-btn-0", Button).disabled is True

        screen.query_one("#ac-tabs", TabbedContent).active = "memory"
        await pilot.pause()
        await _wait_for_selectors(
            screen,
            pilot,
            "#mem-file-path-0",
            "#mem-file-browse-0",
            "#mem-delete-btn-0",
            "#mem-folder-path-0",
        )
        memory_file_card = screen.query_one(MemoryFileCard)
        memory_folder_card = screen.query_one(MemoryFolderCard)
        memory_file_delete = memory_file_card.query_one(".config-card-delete-btn", Button)
        memory_folder_delete = memory_folder_card.query_one(".config-card-delete-btn", Button)
        assert screen.query_one("#mem-add-file", Button).display is False
        assert screen.query_one("#mem-add-folder", Button).display is False
        assert screen.query_one("#mem-file-path-0", Input).disabled is True
        assert screen.query_one("#mem-file-browse-0", Button).display is False
        assert screen.query_one("#mem-file-browse-0", Button).disabled is True
        assert memory_file_delete.display is False
        assert memory_file_delete.disabled is True
        assert screen.query_one("#mem-folder-path-0", Input).disabled is True
        assert memory_folder_delete.display is False
        assert memory_folder_delete.disabled is True


def test_agent_config_missing_child_widgets_are_treated_as_mount_pending(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mounted panel can be queryable before its compose children are."""
    registry = _registry()
    profile = copy.deepcopy(registry.get("Code"))
    assert profile is not None
    screen = AgentsConfigScreen(registry, current_profile="Code")
    draft = config_mod._AgentDraft(
        key="profile:Code",
        original_name="Code",
        profile=profile,
        original_profile=copy.deepcopy(profile),
        is_builtin=True,
    )
    screen._mounted_tabs.add("basic")

    def raise_no_matches(*_args: object, **_kwargs: object) -> object:
        raise NoMatches("No nodes match '#bc-model-use-active'")

    monkeypatch.setattr(screen, "query_one", raise_no_matches)

    with (
        caplog.at_level(logging.DEBUG, logger="chrys.app.tui.screens.agents.config"),
        pytest.raises(config_mod._AgentConfigPanelsNotReady),
    ):
        screen._build_profile_from_mounted_panels(draft)

    assert "Failed to read" not in caplog.text


@pytest.mark.parametrize("initial_profile", ["", "Deleted"], ids=["empty", "stale"])
async def test_agent_config_unknown_initial_profile_loads_first_visible_profile(initial_profile: str) -> None:
    """Startup failures leave the main screen with no (or a deleted) active profile label."""
    registry = _registry()
    first = registry.list_profiles(include_sub_agent_only=True)[0]

    async with open_agent_config(
        registry,
        current_profile=initial_profile,
        initial_profile=initial_profile,
        hydrated=False,
    ) as (screen, _pilot):
        assert screen._selected_profile_name == first.name
        assert len(list(screen.query(BasicConfigPanel))) == 1
