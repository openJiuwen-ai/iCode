# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Step through disk reconciliation, candidate failure, and later model-editor changes in the TUI."""

from __future__ import annotations

import pytest
from textual.widgets import Input

from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.events import types as ev
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.profiles.models.env_bridge import activate_model_profile
from tests.app.tui.screens.test_manual_reload import ask_after_reload, settings_path, slash_reload
from tests.app.tui.screens.test_models_dialog_deletion import _models_on_disk
from tests.app.tui.screens.test_prompt_shortcut_send import (
    MAIN_MODEL,
    make_agent,
    notices,
    profile_tag,
    running_session,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")


async def reload_success(session):
    before = len(session.seen.of(ev.SettingsReloaded))
    session.bar.focus_input()
    await slash_reload(session)
    await wait_for(
        lambda: len(session.seen.of(ev.SettingsReloaded)) > before and session.app.screen is session.main,
        pilot=session.pilot,
        description="manual reload completed",
    )


async def test_renamed_launch_agent_and_new_disk_model_survive_the_next_session(agent_engine, tmp_path, monkeypatch):
    settings = settings_path()
    agents = AgentProfileRegistry()
    profile = make_agent("Writer")
    profile.id = "writer-id"
    source = save_profile(profile)
    agents.load_all()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, registry=agents, startup_agent="Writer", models=models
    ) as session:
        new_text = source.read_text(encoding="utf-8").replace("name: Writer", "name: Editor")
        source.write_text(new_text, encoding="utf-8")
        new_model = models.directory / "new-model.yaml"
        new_model.write_text(
            "id: new-model\nname: New model\nprovider: openai\nmodel_id: new-wire-model", encoding="utf-8"
        )
        settings.write_text("model.profile.active: new-model", encoding="utf-8")
        await reload_success(session)
        assert session.engine.session.agent_profile.name == session.engine.session.new_session_agent == "Editor"
        assert (source.parent / "Editor.yaml").is_file() and not source.exists()
        assert agents.get("Editor").id == "writer-id" and agents.get("Writer") is None
        assert models.get("new-model").model_id == "new-wire-model"
        await ask_after_reload(session, "renamed agent")
        assert session.builds.profiles[-1] == "new-model"
        assert session.engine.lifecycle.new_session_profile().name == "Editor"
        (source.parent / "Editor.yaml").unlink()
        await reload_success(session)
        assert session.engine.session.agent_profile.name == "Code"
        assert agents.get("Editor") is None
        assert any(
            r.definition.key == "controls.reload_agent_fallback"
            for r in session.seen.of(ev.SettingsReloaded)[-1].report
        )


async def test_build_failure_before_normalization_leaves_active_runtime_and_files(agent_engine, tmp_path, monkeypatch):
    settings = settings_path()
    profile = make_agent("Writer")
    profile.id = "writer-id"
    source = save_profile(profile)
    agents = AgentProfileRegistry()
    agents.load_all()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, registry=agents, startup_agent="Writer", models=models
    ) as session:
        old_runtime = session.engine.current.require_loaded()
        renamed = source.read_text(encoding="utf-8").replace("name: Writer", "name: Editor")
        source.write_text(renamed, encoding="utf-8")
        settings.write_text(f"model.profile.active: {MAIN_MODEL}", encoding="utf-8")
        session.builds.failing.add(MAIN_MODEL)
        await slash_reload(session)
        await wait_for(
            lambda: isinstance(session.app.screen, AgentLoadDialog) and session.app.screen.has_class("-error"),
            pilot=session.pilot,
            description="build failed",
        )
        await click_when_settled(session.pilot, "#agent-load-ok")
        await wait_for(
            lambda: session.app.screen is session.main, pilot=session.pilot, description="failure acknowledged"
        )
        assert profile_tag(session.main) == "Writer"
        assert session.engine.current.require_loaded() is old_runtime
        assert agents.get("Editor") is None and agents.get("Writer") is not None
        assert source.read_text(encoding="utf-8") == renamed
        assert not (source.parent / "Editor.yaml").exists()
        await ask_after_reload(session, "keep writing")


async def test_later_model_editor_save_rebuilds_children_from_the_shared_registry(agent_engine, tmp_path, monkeypatch):
    from chrys.orchestration.sub_agents import tools as child_build
    from chrys.service.llm.mock import MockChatClient, MockResponse
    from chrys.service.profiles.agents.schema import SubAgentRef

    settings_path()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    parent = make_agent("Parent")
    parent.sub_agents.agents = [SubAgentRef(profile="Child", tool_name="delegate")]
    child = make_agent("Child", model=MAIN_MODEL)
    child.sub_agent_only = True
    import gc
    import weakref

    clients = []
    model_views = []
    reload_base = models.reload_base

    def track_candidate():
        candidate = reload_base()
        model_views.append(weakref.ref(candidate))
        return candidate

    monkeypatch.setattr(models, "reload_base", track_candidate)

    async def create(profile, **kwargs):
        client = MockChatClient(responses=[MockResponse(text=profile.model_id)])
        clients.append((profile.model_id, client))
        return client

    monkeypatch.setattr(child_build, "create_client", create)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, agents=[parent, child], startup_agent="Parent", models=models
    ) as session:
        path = models.directory / f"{MAIN_MODEL}.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("gpt-test", "after-disk"), encoding="utf-8")
        await reload_success(session)

        async def invoke():
            child_tools = session.engine.current.require_loaded().sub_agent_tools
            return await child_tools.get_tools()[0].func(prompt="What model answered?")

        assert await invoke() == "after-disk"
        gc.collect()
        assert len(model_views) == 1 and model_views[0]() is None
        await session.pilot.press("f4")
        await wait_for(
            lambda: isinstance(session.app.screen, ModelConfigScreen), pilot=session.pilot, description="models page"
        )
        dialog = session.app.screen
        await wait_for(
            lambda: dialog.query_one("#mc-model", Input).value == "after-disk",
            pilot=session.pilot,
            description="updated default loaded",
        )
        dialog.query_one("#mc-model", Input).value = "after-editor"
        await click_when_settled(session.pilot, "#mc-save")
        await wait_for(
            lambda: models.get(MAIN_MODEL).model_id == "after-editor", pilot=session.pilot, description="editor saved"
        )
        before = len(session.seen.of(ev.SettingsReloaded))
        await click_when_settled(session.pilot, "#mc-cancel")
        await wait_for(
            lambda: len(session.seen.of(ev.SettingsReloaded)) > before,
            pilot=session.pilot,
            description="models page closed and applied",
        )
        assert await invoke() == "after-editor"
        assert clients[-1][1].call_count == 1


async def test_deleted_session_and_default_models_fall_back_without_losing_the_last_working_runtime(
    agent_engine, tmp_path, monkeypatch
):
    from tests.app.tui.screens.test_models_dialog_deletion import SPARE_MODEL, _session_model
    from tests.app.tui.screens.test_prompt_shortcut_send import FAST_MODEL

    settings_path()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch, models=models) as session:
        await _session_model(session, FAST_MODEL)
        for profile in models.list_profiles():
            if profile.id != SPARE_MODEL:
                (models.directory / f"{profile.id}.yaml").unlink()
        await reload_success(session)
        report = [line.definition.key for line in session.seen.of(ev.SettingsReloaded)[-1].report]
        assert "controls.reload_session_model_removed" in report and "controls.reload_model_fallback" in report
        assert models.list_ids() == [SPARE_MODEL]
        await ask_after_reload(session, "use the available model")
        assert session.builds.profiles[-1] == SPARE_MODEL
        live = session.engine.current.require_loaded()
        (models.directory / f"{SPARE_MODEL}.yaml").unlink()
        await slash_reload(session)
        await wait_for(
            lambda: any(e.code == "manual_settings_reload_failed" for e in session.seen.of(ev.Error)),
            pilot=session.pilot,
            description="last selectable model removed",
        )
        assert session.engine.current.require_loaded() is live
        assert models.list_ids() == [SPARE_MODEL]
        await ask_after_reload(session, "continue on the previous model")


@pytest.mark.parametrize("reuse_old_name", [False, True])
async def test_named_default_keeps_its_model_id_through_rename_and_bad_yaml_type(
    agent_engine, tmp_path, monkeypatch, reuse_old_name
):
    from chrys.foundation.config.runtime_pointer import get_model_pointer
    from chrys.foundation.config.settings import Settings
    from tests.app.tui.screens.test_prompt_shortcut_send import FAST_MODEL

    settings_path()
    models = _models_on_disk()
    activate_model_profile("Main")
    settings = Settings(model_profile="Main", workspace_change_notice=False)
    async with running_session(agent_engine, tmp_path, monkeypatch, models=models, settings=settings) as session:
        path = models.directory / f"{MAIN_MODEL}.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("name: Main", "name: Renamed"), encoding="utf-8")
        if reuse_old_name:
            other = models.directory / f"{FAST_MODEL}.yaml"
            other.write_text(other.read_text(encoding="utf-8").replace("name: Fast", "name: Main"), encoding="utf-8")
        await reload_success(session)
        assert get_model_pointer()[0] == MAIN_MODEL
        await session.bus.publish(ev.SettingsReload(), raise_handler_errors=True)
        await ask_after_reload(session, "same model after rename")
        assert session.builds.profiles[-1] == MAIN_MODEL
        broken = f"id: {MAIN_MODEL}\nname: Renamed\nmodel_id: [mistyped]"
        path.write_text(broken, encoding="utf-8")
        await reload_success(session)
        assert path.read_text(encoding="utf-8") == broken
        report = session.seen.of(ev.SettingsReloaded)[-1].report
        assert any(line.definition.key == "profiles.reload_kept" for line in report)
        assert models.get(MAIN_MODEL).model_id == "gpt-test"
        await wait_for(
            lambda: any(str(path) in message and "model_id" in message for _, message in notices(session.app)),
            pilot=session.pilot,
            description="invalid model field shown in the reload toast",
        )
        await ask_after_reload(session, "same model after malformed edit")
        assert session.builds.profiles[-1] == MAIN_MODEL


@pytest.mark.parametrize("edited_id", ["", "replacement-id"])
async def test_changed_agent_id_stays_with_the_same_source(agent_engine, tmp_path, monkeypatch, edited_id):
    settings_path()
    agents = AgentProfileRegistry()
    profile = make_agent("Writer")
    profile.id = "writer-id"
    source = save_profile(profile)
    agents.load_all()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch, registry=agents, startup_agent="Writer") as session:
        source.write_text(
            source.read_text(encoding="utf-8").replace("id: writer-id", f"id: {edited_id}"), encoding="utf-8"
        )
        await reload_success(session)
        assert session.engine.session.agent_profile.name == "Writer"
        assert session.engine.session.agent_profile.id == agents.get("Writer").id
        assert all(
            line.definition.key != "controls.reload_agent_fallback"
            for line in session.seen.of(ev.SettingsReloaded)[-1].report
        )
        # The second edit changes both identity fields under the original filename.
        saved = agents.get("Writer").id
        source.write_text(
            source.read_text(encoding="utf-8")
            .replace("name: Writer", "name: Editor")
            .replace(f"id: {saved}", "id: final-id"),
            encoding="utf-8",
        )
        await reload_success(session)
        assert (
            session.engine.session.agent_profile.name == session.engine.lifecycle.new_session_profile().name == "Editor"
        )
        await ask_after_reload(session, "continue editing")


@pytest.mark.parametrize("remove_child", ["delete", "rename"])
async def test_reload_reports_missing_child_and_model_bindings(agent_engine, tmp_path, monkeypatch, remove_child):
    from chrys.foundation.i18n.formatting import format_message
    from chrys.orchestration.sub_agents import tools as child_build
    from chrys.service.llm.mock import MockChatClient, MockResponse
    from chrys.service.profiles.agents.schema import SubAgentRef
    from tests.app.tui.screens.test_prompt_shortcut_send import FAST_MODEL

    settings_path()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    parent = make_agent("Writer", model=FAST_MODEL)
    parent.sub_agents.agents = [SubAgentRef(profile="Child", tool_name="delegate")]
    child = make_agent("Child", model=FAST_MODEL)
    child.sub_agent_only = True
    child_file = save_profile(child)
    save_profile(parent)
    registry = AgentProfileRegistry()
    registry.load_all()

    async def create(profile, **kwargs):
        return MockChatClient(responses=[MockResponse(text=profile.id)])

    monkeypatch.setattr(child_build, "create_client", create)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, registry=registry, startup_agent="Writer", models=models
    ) as session:
        (models.directory / f"{FAST_MODEL}.yaml").unlink()
        await reload_success(session)
        lines = [
            format_message(line)
            for line in session.seen.of(ev.SettingsReloaded)[-1].report
            if line.definition.key == "controls.reload_missing_bound_model"
        ]
        assert len(lines) == 2
        assert any("Writer" in line for line in lines) and any("Child" in line for line in lines)
        assert all(FAST_MODEL in line and MAIN_MODEL in line for line in lines)
        (tool,) = session.engine.current.require_loaded().sub_agent_tools.get_tools()
        assert await tool.func(prompt="Which model?") == MAIN_MODEL
        if remove_child == "delete":
            child_file.unlink()
        else:
            child_file.write_text(
                child_file.read_text(encoding="utf-8").replace("name: Child", "name: Renamed"), encoding="utf-8"
            )
        await reload_success(session)
        assert "delegate" not in session.engine.current.manifest.tool_names
        (lost,) = [
            format_message(line)
            for line in session.seen.of(ev.SettingsReloaded)[-1].report
            if line.definition.key == "controls.reload_unavailable_child"
        ]
        assert all(name in lost for name in ("Writer", "Child", "delegate"))
        assert registry.get("Writer").sub_agents.agents[0].profile == "Child"
        await wait_for(
            lambda: any(lost in message for _, message in notices(session.app)),
            pilot=session.pilot,
            description="missing delegate identified in the reload toast",
        )


@pytest.mark.parametrize(("name", "add_override"), [("QA", True), ("Code", True), ("QA", False)])
async def test_reload_keeps_builtin_name_across_custom_id_override(
    agent_engine, tmp_path, monkeypatch, name, add_override
):
    settings_path()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    agents = AgentProfileRegistry()
    agents.load_all()
    builtin = agents.get(name)
    override = make_agent(name)
    override.id = "mine"
    if not add_override:
        save_profile(override)
        agents.load_user_profiles(agents.directory)

    initial = agents.get(name)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, registry=agents, startup_agent=initial.display_name or name, models=models
    ) as session:
        assert session.engine.session.agent_profile.id == initial.id
        if add_override:
            save_profile(override)
            expected = override
        else:
            (agents.directory / f"{name}.yaml").unlink()
            expected = builtin
        await reload_success(session)
        assert session.engine.session.agent_profile.id == expected.id
        assert session.engine.session.agent_profile.instructions == expected.instructions
        report = session.seen.of(ev.SettingsReloaded)[-1].report
        assert all(line.definition.key != "controls.reload_agent_fallback" for line in report)
        await session.bus.publish(ev.SessionNew(), raise_handler_errors=True)
        assert session.engine.session.agent_profile.id == expected.id
        assert session.engine.session.agent_profile.name == name


async def test_reload_follows_the_user_file_when_a_builtin_is_revealed_and_rejects_a_reused_name(
    agent_engine, tmp_path, monkeypatch
):
    settings_path()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    agents = AgentProfileRegistry()
    agents.load_all()
    override = make_agent("QA")
    override.id = agents.get("QA").id
    source = save_profile(override)
    agents.load_all()
    async with running_session(
        agent_engine, tmp_path, monkeypatch, registry=agents, startup_agent="QA", models=models
    ) as session:
        source.write_text("name: Writer\nid: authored\ninstructions: user definition", encoding="utf-8")
        await reload_success(session)
        assert agents.get("QA").id == override.id
        assert (
            session.engine.session.agent_profile.id == session.engine.lifecycle.new_session_profile().id == "authored"
        )
        (source.parent / "Writer.yaml").unlink()
        (source.parent / "Different.yaml").write_text("name: Writer\nid: unrelated\n", encoding="utf-8")
        await reload_success(session)
        assert agents.get("Writer").id == "unrelated"
        assert session.engine.session.agent_profile.name == "Code"
        await session.bus.publish(ev.SessionNew(), raise_handler_errors=True)
        assert session.engine.session.agent_profile.name == "Code"
        await ask_after_reload(session, "continue with the fallback")
