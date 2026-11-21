# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The committed workspace reaches real main/child builds, tools and continuation identity."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from unittest.mock import MagicMock

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.orchestration.engine.build import builder
from chrys.orchestration.sub_agents import tools as children
from chrys.service.agent_middleware import ResponseBatchCounter
from chrys.service.agent_middleware.control.approval import _is_workspace_git_path
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.agent_middleware.reminders.runtime_env import RuntimeEnvSource
from chrys.service.context.providers.memory import MemoryProvider
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import MemoryConfig, ToolsConfig
from chrys.service.tools import packs
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.components import plan_workspace
from tests.support.extensions import install_file_extension
from tests.support.initialization_build import InitializationBuild
from tests.support.rollback_spec import PROFILE, ask
from tests.support.workspace_engine import capture_async_results, freeze_workspace_settings, select_workspace
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry
from tests.support.workspace_registry import make_workdirs
from tests.support.workspace_registry import workspace_store as workspace_store


async def _build(probe, workspace, project):
    plan = plan_workspace(project or workspace)
    return await builder.build_agent(
        profile=probe.profile,
        settings=Settings(model_profile="mock-model", workspace_change_notice=False),
        workspace=plan.workspace,
        session_id="project-scope",
        bus=probe.bus,
        agent_registry=probe.registry,
        model_registry=probe.models,
        injection=InjectionMiddleware(),
        batch_counter=ResponseBatchCounter(),
        on_intermediate=MagicMock(),
        on_usage=MagicMock(),
        session_dir=probe.root / "session",
    )


@pytest.mark.parametrize("with_extra", [False, True])
async def test_real_main_and_child_share_access_roots_but_only_main_gets_workspace_index(
    tmp_path, monkeypatch, with_extra
):
    outside, extra, primary, related = (tmp_path / name for name in ("outside", "extra", "primary", "related"))
    for path in (outside, extra, primary, related):
        path.mkdir()
        (path / ".git").mkdir()
    (related / "AGENTS.md").write_text("Related instructions must be read explicitly.", encoding="utf-8")
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    selected = registry.create("Selected", str(primary), (str(related), str(tmp_path / "missing")))
    project = registry.snapshot().workspace(selected.id)
    # The second extra is also a workspace folder and retains its explicit label.
    extras = [WorkingDir(str(extra), label="ext"), WorkingDir(str(primary), label="Explicit")]
    workspace = Workspace(str(outside), extras if with_extra else [])
    probe = InitializationBuild(tmp_path, 1, [])
    probe.install(monkeypatch)
    install_file_extension(
        monkeypatch,
        "scope_probe",
        "import json\nfrom chrys.extensions import Extension, ToolContext\nextension = Extension()\n"
        "@extension.tool\nasync def declared_roots(ctx: ToolContext) -> str:\n"
        "    return json.dumps([str(path) for path in ctx.workspace_dirs])\n",
    )
    for profile in (probe.profile, probe.registry.get("Child0")):
        profile.memory = MemoryConfig(files=["AGENTS.md"], folders=[])
        profile.extensions = ["scope_probe"]
        profile.tools = ToolsConfig(builtins=["shell"])
    tools_by_agent, extensions_by_agent, child_approvals = {}, {}, []

    def record_tools(destination, result, build, *args, **kwargs):
        destination[build.agent_name] = result.tools

    capture_async_results(monkeypatch, packs, "assemble_tools", partial(record_tools, tools_by_agent))
    capture_async_results(monkeypatch, packs, "assemble_extensions", partial(record_tools, extensions_by_agent))
    capture_async_results(
        monkeypatch, children, "create_approval", lambda result, *args: child_approvals.append(result)
    )
    result = await _build(probe, workspace, project)
    try:
        expected = [str(path.resolve()) for path in (primary, related)]
        missing = tmp_path / "missing"
        assert result.bindings.approval._workspace_roots == [*expected, str(missing.resolve())]
        assert result.sub_agent_tools is not None
        assert result.sub_agent_tools._workspace_roots == [str(primary), str(related), str(missing)]
        assert result.runtime.cwd == str(primary)
        assert [directory.path for directory in result.runtime.working_dirs] == [str(related), str(missing)]
        # The workspace is runtime state now: its index rides the runtime reminder,
        # never the system prompt.
        assert "# Project" not in result.instructions
        assert str(related / "AGENTS.md") not in result.instructions
        assert "Related instructions must be read explicitly." not in result.instructions
        reminder = RuntimeEnvSource(result.runtime, shell_tool_enabled=False, workspace_section=True).snapshot()
        assert reminder is not None
        assert "  Workspace: Selected" in reminder
        assert f"  - {primary} (primary)" in reminder
        assert f"  - {related}" in reminder
        assert f"  Instructions for this folder: {related / 'AGENTS.md'}" in reminder
        assert "  Read them before working in this folder; they apply only inside it." in reminder
        lines = reminder.splitlines()
        assert lines[lines.index("  Workspace: Selected") :] == [
            "  Workspace: Selected",
            "  Workspace folders:",
            f"  - {primary} (primary)",
            f"  - {related}",
            f"    Instructions for this folder: {related / 'AGENTS.md'}",
            "    Read them before working in this folder; they apply only inside it.",
        ]
        assert f"    - {extra}" not in reminder
        assert f"    - {primary} [Explicit]" not in reminder
        assert f"    - {related}" not in reminder
        assert " [Selected]" not in reminder
        assert sum(line == f"  - {primary} (primary)" for line in lines) == 1
        assert not any(
            isinstance(provider, MemoryProvider)
            for provider in result.sub_agent_tools._context_managers["Child0"].providers
        )
        for name in ("Parent", "Child0"):
            extension = next(tool for tool in extensions_by_agent[name] if tool.name == "declared_roots")
            assert json.loads(await extension.invoke(arguments={}, skip_parsing=True)) == [
                *expected,
                str(missing.resolve()),
            ]
            directory_tools = [tool for tool in tools_by_agent[name] if tool.name == "list_workspace_dirs"]
            assert len(directory_tools) == 1
        await result.sub_agent_tools.get_tools()[0].invoke(arguments={"prompt": "Say hello"}, skip_parsing=True)
        assert len(child_approvals) == 1
        assert child_approvals[0]._workspace_roots == [*expected, str(missing.resolve())]
        for approval in (result.bindings.approval, child_approvals[0]):
            for root in expected:
                assert _is_workspace_git_path(root + "/new.txt", approval._workspace_roots)
            assert not _is_workspace_git_path(str(tmp_path / "elsewhere" / "new.txt"), approval._workspace_roots)
    finally:
        await result.prepared.aclose()


@pytest.mark.parametrize(
    "scenario",
    ["cold_reference", "cold_identity", "missing_fingerprint", "same", "unspecified", "hot_reference", "hot_identity"],
)
async def test_service_session_continuation_follows_the_effective_scope(
    registry, store, agent_engine, monkeypatch, tmp_path, scenario
):
    from chrys.foundation.config.settings_store import LoadedSettings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import SessionRestore, SetApprovalMode
    from chrys.foundation.platform.files import atomic_write_owner_only_text
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile

    primary, first, second = make_workdirs(tmp_path, "primary", "first", "second")
    workspace = registry.create("Workspace", str(primary), [str(first)])
    in_workspace = scenario != "unspecified"
    profiles = AgentProfileRegistry()
    profile = replace(PROFILE, tools=ToolsConfig(builtins=["shell"]))
    profiles.register(profile)
    models = ModelProfileRegistry()
    models.register(
        ModelProfile(
            id="responses",
            name="Responses",
            provider="openai",
            api_style="responses",
            model_id="test",
            chat_options='{"store": true}',
            stream=False,
        )
    )
    loaded = LoadedSettings(settings=Settings(model_profile="responses", workspace_change_notice=False), provenance={})
    freeze_workspace_settings(monkeypatch, loaded)
    client = MockChatClient(
        responses=[
            MockResponse(text="first answer", conversation_id="saved-provider-conversation"),
            *([MockResponse(tool_calls=[("list_workspace_dirs", "list-dirs", {})])] if in_workspace else []),
            MockResponse(text="listed folders", conversation_id="saved-provider-conversation"),
        ]
    )

    async def create_client(*args, **kwargs):
        return client

    monkeypatch.setattr(builder, "create_client", create_client)

    async def start():
        engine = agent_engine(
            EventBus(),
            loaded_settings=loaded,
            agent_registry=profiles,
            model_registry=models,
            state_store=store,
            initial_workspace=Workspace(str(primary), [] if in_workspace else [WorkingDir(str(first))]),
        )
        await engine.start(profile)
        return engine

    original = await start()
    reopened = original
    try:
        if in_workspace:
            await select_workspace(original.event_bus, workspace.id)
        await ask(original, "start the conversation")
        sid = original.session_id
        hot = scenario.startswith("hot_")
        if not hot:
            await original.shutdown()
        meta = await store.load_session_meta(sid)
        assert meta.working_dirs == ([] if in_workspace else [str(first)])
        assert meta.service_session_id == "saved-provider-conversation"

        target = workspace
        if scenario.endswith("reference"):
            target = registry.update(workspace.id, name="Renamed", primary=str(primary), folders=[str(second)])
        elif scenario.endswith("identity"):
            target = registry.create("Replacement", str(primary), [str(first)])
            if not hot:
                registry.set_session_workspace(sid, target.id)
        elif scenario == "missing_fingerprint":
            session_file = store.session_dir(sid) / "session.json"
            checkpoint = json.loads(session_file.read_bytes())
            checkpoint["meta"].pop("service_session_workspace_fingerprint", None)
            atomic_write_owner_only_text(session_file, json.dumps(checkpoint))

        if hot:
            await select_workspace(original.event_bus, target.id)
        else:
            reopened = await start()
            await reopened.event_bus.publish(SessionRestore(session_id=sid), raise_handler_errors=True)
        await reopened.event_bus.publish(SetApprovalMode(mode="bypass", persist=False), raise_handler_errors=True)
        await ask(reopened, "list the working directories" if in_workspace else "continue")

        messages, options = client.call_history[1]
        if scenario in {"same", "unspecified"}:
            assert options["conversation_id"] == "saved-provider-conversation"
        else:
            assert not options.get("conversation_id")
            assert any("start the conversation" in message.text for message in messages)
        if in_workspace:
            assert "list_workspace_dirs" in {tool.name for tool in options["tools"]}
            tool_content = [
                part
                for message in client.call_history[-1][0]
                for part in message.contents
                if part.type == "function_result"
            ]
            expected_ref = second if scenario.endswith("reference") else first
            assert any(str(expected_ref) in part.result for part in tool_content)
            assert (await store.load_session_meta(sid)).working_dirs == []
        else:
            session_file = store.session_dir(sid) / "session.json"
            assert "service_session_workspace_fingerprint" not in json.loads(session_file.read_bytes())["meta"]
    finally:
        await reopened.shutdown()
