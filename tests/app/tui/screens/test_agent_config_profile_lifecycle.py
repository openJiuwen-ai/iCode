# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for agent profile lifecycle on the configuration screen: reset/shadow files, delete/promotion, clone."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from textual.containers import Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Select,
)

from chrys.app.tui.screens.agents.config import AgentsConfigScreen
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    MCPServerConfig,
    MemoryConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
)
from tests.app.tui.screens._agent_config_support import (
    _DEFAULT_WAIT_TIMEOUT,
    _activate_agent_config_tab,
    _agent_config_clone_save_debug,
    _draft_for_original,
    _draft_for_profile,
    _file_mode,
    _registry,
    _wait_for_active_screen,
    _wait_for_confirm_button,
    _wait_for_hydrated,
    _wait_for_input_enabled,
    _wait_for_panel_display_name,
    _wait_for_selected_profile_name,
    _wait_for_selectors,
    make_profile,
    open_agent_config,
    registry_with,
)
from tests.support.waiting import wait_for

# The polling helpers in _agent_config_support share one 45s ceiling, below
# the repository's 60s default per-test timeout. This module retains a 120s
# override for expensive screen mounting before the first wait; once polling
# starts, the helper deadline still produces a clean AssertionError before the
# thread timeout can kill the xdist worker and surface only "worker gwN crashed".
pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_chrys_config_dir")]


async def test_agent_config_unsaved_edit_survives_agent_switch_and_close_discards(tmp_path: Path) -> None:
    registry = _registry()
    original = registry.get("Code")
    assert original is not None

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        code_key = screen._selected_draft_key
        explore_key = _draft_for_profile(screen, "Explore").key
        screen.query_one("#bc-display-name", Input).focus()
        screen.query_one("#bc-display-name", Input).value = "Unsaved Code Label"
        await pilot.pause()

        screen._sync_selected_draft_from_panels()
        screen._load_profile(explore_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        screen._sync_selected_draft_from_panels()
        screen._load_profile(code_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        assert screen.query_one("#bc-display-name", Input).value == "Unsaved Code Label"
        screen.action_cancel()
        await pilot.pause()

    current = registry.get("Code")
    assert current is not None
    assert current.display_name == original.display_name
    assert not (tmp_path / "agents" / "Code.yaml").exists()


async def test_agent_config_reset_preserves_unsaved_memory_and_close_discards(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = _registry()
    template = registry.get_builtin_template("Code")
    assert template is not None
    customized = copy.deepcopy(template)
    customized.instructions = "custom instructions"
    customized.skills = SkillsConfig(paths=["custom-skills"])
    customized.tools.mcp = [MCPServerConfig(name="private", transport="http", url="https://example.test")]
    customized.memory = MemoryConfig(files=["saved.md"])
    canonical_customized = copy.deepcopy(customized)
    AgentsConfigScreen._canonicalize_profile_for_ui(canonical_customized)
    save_profile(customized)
    registry.register(customized)
    original_yaml = (tmp_path / "agents" / "Code.yaml").read_bytes()

    async with open_agent_config(registry, current_profile="Code", initial_tab="memory") as (screen, pilot):
        assert screen.query_one("#ac-reset", Button).display is True
        assert screen.query_one("#ac-delete", Button).display is False
        assert not screen._drafts[screen._selected_draft_key].dirty
        memory_input = screen.query_one("#mem-file-path-0", Input)
        memory_input.focus()
        await wait_for(lambda: memory_input.has_focus, pilot=pilot, description="memory path editor focused")
        memory_input.value = "unsaved.md"
        # Let the field-change event reach the draft before Reset replaces the
        # panel; this test exercises preserving an established unsaved edit.
        await wait_for(
            lambda: screen._drafts[screen._selected_draft_key].dirty,
            pilot=pilot,
            description="unsaved memory edit recognized before resetting the profile",
        )
        screen.query_one("#ac-reset", Button).press()
        confirm_button = await _wait_for_confirm_button(pilot.app, pilot)
        assert confirm_button.variant == "primary"  # same look as New/Clone, not a destructive action
        confirm_button.press()
        await _wait_for_active_screen(pilot.app, pilot, screen)
        await _wait_for_hydrated(screen, pilot)

        draft = screen._drafts[screen._selected_draft_key]
        assert draft.profile.instructions == template.instructions
        assert draft.profile.skills == canonical_customized.skills
        assert draft.profile.tools.mcp == canonical_customized.tools.mcp
        assert draft.profile.memory.files == ["unsaved.md"]
        assert draft.reset_to_builtin is True
        assert draft.dirty is True
        assert screen.query_one("#ac-save", Button).disabled is False
        screen.action_cancel()
        await pilot.pause()

    assert registry.get("Code") == customized
    assert (tmp_path / "agents" / "Code.yaml").read_bytes() == original_yaml


async def test_agent_config_reset_to_exact_template_deletes_shadow_on_save(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = _registry()
    template = registry.get_builtin_template("Code")
    assert template is not None
    canonical_template = copy.deepcopy(template)
    AgentsConfigScreen._canonicalize_profile_for_ui(canonical_template)
    customized = copy.deepcopy(template)
    customized.instructions = "custom instructions"
    save_profile(customized)
    registry.register(customized)

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen._do_reset(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        screen.query_one("#ac-save", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

    assert registry.get("Code") == canonical_template
    assert not (tmp_path / "agents" / "Code.yaml").exists()


async def test_agent_config_reset_removes_shadow_loaded_from_noncanonical_filename(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    user_dir = tmp_path / "agents"
    template = _registry().get_builtin_template("Code")
    assert template is not None
    customized = copy.deepcopy(template)
    customized.instructions = "custom instructions"
    save_profile(customized)
    (user_dir / "Code.yaml").rename(user_dir / "my-code.yaml")
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    assert registry.get("Code").instructions == "custom instructions"

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen._do_reset(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        screen.query_one("#ac-save", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

    assert registry.get("Code").instructions == template.instructions
    assert sorted(p.name for p in user_dir.glob("*.y*ml")) == []


async def test_agent_config_reset_with_only_preserved_changes_is_noop(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = _registry()
    template = registry.get_builtin_template("Code")
    assert template is not None
    customized = copy.deepcopy(template)
    customized.skills = SkillsConfig(paths=["custom-skills"])
    canonical_customized = copy.deepcopy(customized)
    AgentsConfigScreen._canonicalize_profile_for_ui(canonical_customized)
    save_profile(customized)
    registry.register(customized)
    shadow_path = tmp_path / "agents" / "Code.yaml"
    original_yaml = shadow_path.read_bytes()

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen._do_reset(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        draft = screen._drafts[screen._selected_draft_key]
        assert draft.profile == canonical_customized
        assert draft.reset_to_builtin is False
        assert draft.dirty is False
        assert screen.query_one("#ac-save", Button).disabled is True

    assert registry.get("Code") == customized
    assert shadow_path.read_bytes() == original_yaml


async def test_agent_config_reset_with_preserved_settings_keeps_shadow(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = _registry()
    template = registry.get_builtin_template("Code")
    assert template is not None
    customized = copy.deepcopy(template)
    customized.instructions = "custom instructions"
    customized.skills = SkillsConfig(paths=["custom-skills"])
    canonical_customized = copy.deepcopy(customized)
    AgentsConfigScreen._canonicalize_profile_for_ui(canonical_customized)
    save_profile(customized)
    registry.register(customized)

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen._do_reset(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        screen._apply_all()
        await _wait_for_hydrated(screen, pilot)

    saved = load_profile_from_yaml(tmp_path / "agents" / "Code.yaml")
    assert saved.instructions == template.instructions
    assert saved.skills == canonical_customized.skills


async def test_agent_config_reset_shadow_delete_rolls_back_when_later_save_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = _registry()
    template = registry.get_builtin_template("Code")
    assert template is not None
    customized = copy.deepcopy(template)
    customized.instructions = "custom instructions"
    other = make_profile(
        "Other",
        display_name="Other",
        description="Another agent",
    )
    save_profile(customized)
    save_profile(other)
    registry.register(customized)
    registry.register(other)
    code_path = tmp_path / "agents" / "Code.yaml"
    other_path = tmp_path / "agents" / "Other.yaml"
    original_code = code_path.read_bytes()
    original_other = other_path.read_bytes()

    original_save_profile = save_profile

    def fail_on_other(profile, *args, **kwargs):
        if profile.name == "Other":
            raise OSError("simulated save failure")
        return original_save_profile(profile, *args, **kwargs)

    monkeypatch.setattr("chrys.service.profiles.agents.serializer.save_profile", fail_on_other)
    deleted: list[str] = []
    monkeypatch.setattr("chrys.service.profiles.agents.serializer.delete_profile", deleted.append)
    original_code_mode = _file_mode(code_path)

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen._do_reset(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        other_draft = _draft_for_original(screen, "Other")
        other_draft.profile.display_name = "Other edited"
        other_draft.dirty = True

        screen.query_one("#ac-save", Button).press()
        await pilot.pause()

        reset_draft = _draft_for_original(screen, "Code")
        assert reset_draft.reset_to_builtin is True
        assert reset_draft.dirty is True

    restored_code = registry.get("Code")
    restored_other = registry.get("Other")
    assert restored_code is not None
    assert restored_code.instructions == "custom instructions"
    assert restored_other is not None
    assert restored_other.display_name == "Other"
    assert code_path.read_bytes() == original_code
    assert other_path.read_bytes() == original_other
    # Shadow deletes run only after every save succeeded, so a failed save
    # never has to be undone by recreating the shadow.
    assert deleted == []
    assert _file_mode(code_path) == original_code_mode


async def test_agent_config_reset_rollback_recreates_shadow_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shadow removed by an exact-template reset comes back 0600 when a later step fails."""
    from chrys.service.profiles.agents.serializer import delete_profile, save_profile

    registry = _registry()
    shadows: dict[str, bytes] = {}
    for name in ("Code", "Explore"):
        template = registry.get_builtin_template(name)
        assert template is not None
        customized = copy.deepcopy(template)
        customized.instructions = f"custom {name}"
        save_profile(customized)
        registry.register(customized)
        shadows[name] = (tmp_path / "agents" / f"{name}.yaml").read_bytes()
    code_path = tmp_path / "agents" / "Code.yaml"
    original_code_mode = _file_mode(code_path)

    original_delete_profile = delete_profile
    deleted: list[str] = []

    def fail_on_explore(name, *args, **kwargs):
        deleted.append(name)
        if name == "Explore":
            raise OSError("simulated delete failure")
        return original_delete_profile(name, *args, **kwargs)

    monkeypatch.setattr("chrys.service.profiles.agents.serializer.delete_profile", fail_on_explore)

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen._do_reset(_draft_for_original(screen, "Code").key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        screen._do_reset(_draft_for_original(screen, "Explore").key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        screen.query_one("#ac-save", Button).press()
        await pilot.pause()

    # The Code shadow really was removed and then recreated by the rollback.
    assert deleted == ["Code", "Explore"]
    for name, original in shadows.items():
        assert (tmp_path / "agents" / f"{name}.yaml").read_bytes() == original
        assert registry.get(name).instructions == f"custom {name}"
    assert _file_mode(code_path) == original_code_mode


async def test_agent_config_delete_custom_profile_is_immediate_and_leaves_save_disabled(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = AgentProfileRegistry()
    profile = make_profile(
        "custom",
        display_name="Custom",
        description="Custom profile",
    )
    fallback = make_profile(
        "fallback",
        display_name="Fallback",
        description="Fallback profile",
    )
    save_profile(profile)
    save_profile(fallback)
    registry.register(profile)
    registry.register(fallback)

    async with open_agent_config(registry, current_profile="Custom") as (screen, pilot):
        screen._do_delete(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        assert registry.get("custom") is None
        assert not (tmp_path / "agents" / "custom.yaml").exists()
        assert screen.query_one("#ac-save", Button).disabled is True
        assert screen._cancel_result() == "switched"


async def test_agent_config_delete_active_then_save_promoted_edit_still_switches(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.serializer import save_profile

    registry = AgentProfileRegistry()
    active = make_profile(
        "active",
        display_name="Active",
        description="Active profile",
    )
    fallback = make_profile(
        "fallback",
        display_name="Fallback",
        description="Fallback profile",
    )
    save_profile(active)
    save_profile(fallback)
    registry.register(active)
    registry.register(fallback)
    saved_callbacks: list[tuple[str | None, str | None]] = []

    async with open_agent_config(
        registry,
        current_profile="Active",
        active_profile_name="active",
        on_saved=lambda display, name: saved_callbacks.append((display, name)),
    ) as (screen, pilot):
        screen._do_delete(screen._selected_draft_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        screen.query_one("#bc-display-name", Input).focus()
        screen.query_one("#bc-display-name", Input).value = "Fallback Edited"
        await pilot.pause()
        await _wait_for_panel_display_name(screen, pilot, "Fallback Edited")
        await wait_for(
            lambda: not screen.query_one("#ac-save", Button).disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after promoted-profile edit",
        )
        screen.query_one("#ac-save", Button).press()
        await wait_for(
            lambda: registry.get("fallback") is not None and registry.get("fallback").display_name == "Fallback Edited",
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="promoted profile edit persisted",
        )
        await _wait_for_hydrated(screen, pilot)

        assert screen._cancel_result() == "switched"

    assert saved_callbacks[0] == (None, "fallback")
    assert registry.get("active") is None
    updated = registry.get("fallback")
    assert updated is not None
    assert updated.display_name == "Fallback Edited"
    assert not (tmp_path / "agents" / "active.yaml").exists()


async def test_agent_config_saving_active_profile_as_acp_promotes_replacement_main(tmp_path: Path) -> None:
    """Converting the session-active profile to External ACP must hand main
    duty to another profile: the engine refuses an ACP main on reload, and
    the next startup would refuse the persisted config outright."""
    from chrys.service.profiles.agents.serializer import save_profile

    registry = AgentProfileRegistry()
    active = make_profile(
        "active",
        display_name="Active",
        description="Active profile",
    )
    fallback = make_profile(
        "fallback",
        display_name="Fallback",
        description="Fallback profile",
    )
    save_profile(active)
    save_profile(fallback)
    registry.register(active)
    registry.register(fallback)
    saved_callbacks: list[tuple[str | None, str | None]] = []

    async with open_agent_config(
        registry,
        current_profile="Active",
        initial_profile="active",
        active_profile_name="active",
        on_saved=lambda display, name: saved_callbacks.append((display, name)),
    ) as (screen, pilot):
        screen.query_one("#bc-agent-type", Select).value = "acp"
        await wait_for(
            lambda: (
                screen._drafts.get(screen._selected_draft_key) is not None
                and screen._drafts[screen._selected_draft_key].profile.acp is not None
                and not screen._hydrating
            ),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="active profile converted to ACP",
        )
        await _activate_agent_config_tab(screen, pilot, "acp")
        command = screen.query_one("#acp-command", Input)
        command.focus()
        command.value = "external-agent"
        await wait_for(
            lambda: not screen.query_one("#ac-save", Button).disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after ACP conversion",
        )
        screen.query_one("#ac-save", Button).press()
        await wait_for(
            lambda: registry.get("active") is not None and registry.get("active").acp is not None,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="ACP conversion persisted",
        )
        await _wait_for_hydrated(screen, pilot)

        assert screen._cancel_result() == "switched"
        assert screen._active_profile_name == "fallback"
        assert screen._current_profile == "Fallback"

    # The queued switch must target the promoted main profile so the caller
    # publishes AgentProfileSwitch instead of reloading into the ACP main.
    assert (None, "fallback") in saved_callbacks
    persisted = registry.get("active")
    assert persisted is not None
    assert persisted.acp is not None
    assert persisted.sub_agent_only is True


async def test_agent_config_delete_guard_includes_sub_agent_references() -> None:
    registry = registry_with(
        make_profile(
            "target",
            display_name="Target",
            description="Referenced profile",
        ),
        make_profile(
            "fallback",
            display_name="Fallback",
            description="Fallback profile",
        ),
        make_profile(
            "parent",
            display_name="Parent",
            description="Profile that still uses the target",
            sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="target")]),
        ),
    )
    async with open_agent_config(registry, current_profile="Target") as (screen, _pilot):
        draft = screen._drafts[screen._selected_draft_key]
        referencing = screen._referencing_profiles_for_delete(draft, {"target"})

    assert referencing == ["Parent"]


async def test_agent_config_generated_names_skip_existing_profiles() -> None:
    registry = registry_with(
        make_profile(
            "Base",
            display_name="Base Agent",
            description="Visible source profile",
        ),
        make_profile(
            "new-agent",
            display_name="Existing New Name",
            description="Existing profile using the default new-agent name",
        ),
        make_profile(
            "Base-copy",
            display_name="Existing Clone Name",
            description="Existing profile using the default clone name",
        ),
    )
    async with open_agent_config(registry, current_profile="Base Agent") as (screen, pilot):
        screen.query_one("#ac-new", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        new_profile = screen._drafts[screen._selected_draft_key].profile

        base_key = _draft_for_profile(screen, "Base").key
        screen._sync_selected_draft_from_panels()
        screen._load_profile(base_key)
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

        screen.query_one("#ac-clone", Button).press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
        clone_profile = screen._drafts[screen._selected_draft_key].profile

    assert new_profile.name == "new-agent-2"
    assert clone_profile.name == "Base-copy-2"


async def test_agent_config_clone_builtin_saves_editable_custom_copy(
    tmp_path: Path,
) -> None:
    registry = _registry()
    original = registry.get("Code")
    assert original is not None

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen.query_one("#ac-clone", Button).press()
        await pilot.pause()
        await _activate_agent_config_tab(screen, pilot, "tools")
        await _wait_for_selectors(
            screen,
            pilot,
            "#bc-name",
            "#bc-model-use-active",
            "#tc-cat-filesystem-read",
            "#tc-cat-todo",
        )
        # The input mounts in its builtin (disabled) state then transitions
        # to enabled for the custom clone — wait for that transition.
        await _wait_for_input_enabled(screen, pilot, "#bc-name")
        await _wait_for_hydrated(screen, pilot)

        copied = screen._drafts[screen._selected_draft_key].profile
        assert screen.query_one("#tc-cat-todo", Checkbox).value is True
        save = screen.query_one("#ac-save", Button)
        await wait_for(
            lambda: not save.disabled,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="save enabled after builtin clone",
        )
        # Exercise the synchronous save transaction directly.  The button
        # handler intentionally swallows validation/save errors into UI
        # notifications, which turns rare CI timing races into opaque waits.
        # This test is about clone persistence, so failures should surface as
        # direct exceptions from the save path.
        screen._apply_all()
        saved = registry.get(copied.name)
        assert saved is not None, _agent_config_clone_save_debug(screen, registry, copied.name)
        await _wait_for_hydrated(screen, pilot)
        await _activate_agent_config_tab(screen, pilot, "basic")
        name_input = screen.query_one("#bc-name", Input)

    assert copied.name == "Code-copy"
    assert copied.id
    assert copied.id != original.id
    assert copied.display_name == f"{original.display_name or original.name} Copy"
    assert copied.description == original.description
    assert copied.instructions == original.instructions
    assert set(copied.tools.builtins) == set(original.tools.builtins)
    assert copied.extensions == original.extensions
    assert copied.tools.mcp == original.tools.mcp
    assert copied.tools.shell_filter == original.tools.shell_filter
    assert copied.skills.paths == original.skills.paths
    assert copied.skills.inline == original.skills.inline
    assert copied.skills.script_timeout == original.skills.script_timeout
    assert set(copied.skills.script_extensions) == set(original.skills.script_extensions)
    assert copied.skills.auto_load_user_agents_skills == original.skills.auto_load_user_agents_skills
    assert copied.skills.auto_load_cwd_agents_skills == original.skills.auto_load_cwd_agents_skills
    assert copied.approval == original.approval
    assert copied.sub_agents.max_total_concurrency == original.sub_agents.max_total_concurrency
    assert [ref.profile for ref in copied.sub_agents.agents] == [ref.profile for ref in original.sub_agents.agents]
    assert [ref.tool_name for ref in copied.sub_agents.agents] == [ref.tool_name for ref in original.sub_agents.agents]
    assert [ref.max_concurrency for ref in copied.sub_agents.agents] == [
        ref.max_concurrency for ref in original.sub_agents.agents
    ]
    assert copied.model == original.model
    assert saved is not None
    assert set(saved.tools.builtins) == set(original.tools.builtins)
    assert "todo" in saved.tools.builtins
    assert [ref.profile for ref in saved.sub_agents.agents] == [ref.profile for ref in original.sub_agents.agents]
    assert [ref.tool_name for ref in saved.sub_agents.agents] == [ref.tool_name for ref in original.sub_agents.agents]
    assert not registry.is_builtin(copied.name)
    assert name_input.disabled is False
    assert (tmp_path / "agents" / f"{copied.name}.yaml").is_file()


async def test_agent_config_save_during_clone_hydration_uses_staged_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen.query_one("#ac-clone", Button).press()
        await pilot.pause()
        copied = screen._drafts[screen._selected_draft_key].profile

        def fail_if_panel_validation_runs() -> list[str]:
            if screen._hydrating:
                raise AssertionError("hydrating save should not read transient panel state")
            return []

        monkeypatch.setattr(screen, "_validate_all", fail_if_panel_validation_runs)
        screen._hydrating = True
        screen.query_one("#ac-save", Button).press()
        await pilot.pause()

    saved = registry.get(copied.name)
    assert saved is not None
    assert saved.name == copied.name


@pytest.mark.parametrize("source", ["builtin", "custom"])
async def test_agent_config_clone_increments_existing_clone_suffix(source: str, tmp_path: Path) -> None:
    if source == "builtin":
        # Clones Explore (no sub-agents) rather than Code: repeated cloning of a
        # sub-agent-bearing profile mounts a Textual Select per ref, which trips an
        # upstream Mount-time race (SelectOverlay queried before compose) on some
        # platforms. The sub-agent-bearing clone path is exercised separately by
        # test_agent_config_clone_builtin_saves_editable_custom_copy.
        registry = _registry()
        source_name = "Explore"
        current_profile = "Explore"
        clone_names = ("Explore-copy", "Explore-copy-2", "Explore-copy-3")
        expected_display = "Explore Agent Copy 3"
    else:
        registry = registry_with(
            make_profile(
                "custom-copy-2",
                id="custom-copy-id",
                display_name="Custom copy 2",
                description="Custom profile",
            )
        )
        source_name = "custom-copy-2"
        current_profile = "Custom copy 2"
        clone_names = ("custom-copy-3",)
        expected_display = "Custom Copy 3"
    original = registry.get(source_name)
    assert original is not None

    async with open_agent_config(registry, current_profile=current_profile) as (screen, pilot):
        for expected in clone_names:
            screen.query_one("#ac-clone", Button).press()
            await _wait_for_selected_profile_name(screen, pilot, expected)

        selected = screen._drafts[screen._selected_draft_key].profile

    assert selected.name == clone_names[-1]
    assert selected.display_name == expected_display
    assert selected.id
    assert selected.id != original.id
    # A clone is staged only: nothing reaches the registry or disk until Save.
    for name in clone_names:
        assert registry.get(name) is None
        assert not (tmp_path / "agents" / f"{name}.yaml").exists()


async def test_agent_config_footer_buttons_stay_inside_modal_after_clone() -> None:
    registry = _registry()

    async with open_agent_config(registry, current_profile="Code", size=(90, 40), hydrated=False) as (screen, pilot):
        screen.query_one("#ac-clone", Button).press()
        await pilot.pause()

        container = screen.query_one("#ac-container", Vertical)
        container_left = container.region.x
        container_right = container.region.x + container.region.width
        buttons = [
            screen.query_one(f"#{button_id}", Button)
            for button_id in ("ac-new", "ac-clone", "ac-reset", "ac-delete", "ac-save", "ac-cancel")
        ]

        visible_buttons = [button for button in buttons if button.display]
        assert all(container_left <= button.region.x for button in visible_buttons)
        assert all(button.region.x + button.region.width <= container_right for button in visible_buttons)
