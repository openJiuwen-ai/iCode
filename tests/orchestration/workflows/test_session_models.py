# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow defaults are session-owned, checkpointed and frozen in each Run."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import (
    SetApprovalMode,
    WorkflowModelChangeRequest,
    WorkflowModelChangeResult,
    WorkflowRunAccepted,
    WorkflowRunStarted,
)
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import ModelConfig
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.outcomes import RunOutcome
from chrys.service.workflows.store import read_run_header
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.event_capture import capture_event_sequence
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow


async def test_model_defaults_are_isolated_and_preserved_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), *[MockChatClient(responses=[MockResponse(text="done")]) for _ in range(6)]],
    )
    project = make_workdir(tmp_path)
    write_workflow(
        project,
        "review",
        b"from chrys.workflows import WorkflowBuilder\n"
        b"wf = WorkflowBuilder('review')\n"
        b"a = wf.agent('default', profile='Headless')\n"
        b"b = wf.agent('bound', profile='Bound')\n"
        b"c = wf.agent('explicit', profile='Headless', model='explicit-model')\n"
        b"wf.start(a)\nwf.chain(a, b, c)\nwf.output(c)\nworkflow = wf.build()\n",
    )
    host = make_host(
        tmp_path,
        project=project,
        profiles=[
            make_profile(),
            replace(make_profile("Bound"), model=ModelConfig(profile_id="bound-model")),
        ],
    )
    registry = host.engine.model_registry
    assert registry is not None
    for identity in ("second-model", "bound-model", "explicit-model"):
        registry.register(ModelProfile(id=identity, name=identity, provider="mock", model_id=identity))
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "review")
        first, first_events = await run(host, "review")
        session_id = host.workflow_session_id
        original_target = host.workflow_target("review")
        chat_settings = host.engine.settings
        async with capture_event_sequence(host.event_bus, WorkflowModelChangeResult) as replies:
            await host.event_bus.publish(
                WorkflowModelChangeRequest(
                    session_id=session_id,
                    profile_id="second-model",
                    request_id="change",
                ),
                raise_handler_errors=True,
            )
        assert len(replies) == 1 and not replies[0].error
        assert replies[0].selection is not None and replies[0].selection.model is not None
        assert replies[0].selection.model.profile_id == "second-model"
        assert host.engine.settings == chat_settings
        saved = (await store.load_workflow_session(session_id)).encode()
        assert saved is not None and saved["model"]["profile_id"] == "second-model"
        # An already prepared target cannot silently adopt a newer session setting.
        with pytest.raises(WorkflowRunRejectedError, match="model changed") as rejected:
            _ = [event async for event in host.iter_workflow_events(original_target)]
        assert rejected.value.event.error == "session_changed"
        await host.load_workflow_session(session_id)
        second, second_events = await run(host, "review")
        for events, expected in ((first_events, "mock-profile"), (second_events, "second-model")):
            started = of_type(events, WorkflowRunStarted)[0]
            assert started.model is not None and started.model.profile_id == expected
            assert {node["node_id"]: node["model_profile_id"] for node in started.resolved_nodes} == {
                "default": expected,
                "bound": "bound-model",
                "explicit": "explicit-model",
            }
        assert host.workflow_session_dir is not None
        for result, expected in ((first, "mock-profile"), (second, "second-model")):
            header = read_run_header(run_dir(host.workflow_session_dir, result.run_id))
            assert header["model"]["profile_id"] == expected
        registry.remove("second-model")
        await host.load_workflow_session(session_id)
        selected = host.workflow_target("review").model
        assert selected is not None and selected.name == "second-model"
        with pytest.raises(WorkflowRunRejectedError, match="no usable model"):
            await run(host, "review")
    finally:
        await host.shutdown()


async def test_running_workflow_rejects_model_change(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "check", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "check")

        async def accepted(event: WorkflowRunAccepted) -> None:
            await host.event_bus.publish(
                WorkflowModelChangeRequest(
                    session_id=event.selection.session_id,
                    profile_id="mock-profile",
                    request_id="busy",
                ),
                raise_handler_errors=True,
            )

        await host.event_bus.subscribe(WorkflowRunAccepted, accepted)
        async with capture_event_sequence(host.event_bus, WorkflowModelChangeResult) as replies:
            await run(host, "check")
        assert len(replies) == 1 and replies[0].selection is None and "active" in replies[0].error
    finally:
        await host.shutdown()


@pytest.mark.parametrize("fail_save", [False, True])
async def test_model_checkpoint_gates_start_and_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_save: bool,
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "check", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    registry = host.engine.model_registry
    assert registry is not None
    registry.register(ModelProfile(id="second", name="second", provider="mock", model_id="second"))
    entered, release = asyncio.Event(), asyncio.Event()
    real_save = JsonFileStateStore.save_workflow_session

    async def save(store, session_id, state, *, title=""):
        if state.model is not None and state.model.profile_id == "second":
            entered.set()
            await release.wait()
            if fail_save:
                raise OSError("disk full")
        return await real_save(store, session_id, state, title=title)

    update: asyncio.Task | None = None
    try:
        await confirm(host, "check")
        await run(host, "check")
        monkeypatch.setattr(JsonFileStateStore, "save_workflow_session", create_autospec(real_save, side_effect=save))
        async with capture_event_sequence(host.event_bus, WorkflowModelChangeResult) as replies:
            update = asyncio.create_task(
                host.event_bus.publish(
                    WorkflowModelChangeRequest(
                        session_id=host.workflow_session_id,
                        profile_id="second",
                        request_id="save",
                    ),
                    raise_handler_errors=True,
                )
            )
            await wait_for(lambda: entered.is_set() or update.done())
            if update.done():
                await update
            assert entered.is_set() and not replies
            with pytest.raises(WorkflowRunRejectedError, match="model is being updated"):
                await run(host, "check")
            release.set()
            await update
        assert len(replies) == 1
        assert bool(replies[0].error) is fail_save
        await host.load_workflow_session(host.workflow_session_id)
        model = host.workflow_target("check").model
        assert model is not None and model.profile_id == ("mock-profile" if fail_save else "second")
    finally:
        release.set()
        if update is not None:
            await update
        await host.shutdown()


async def test_unselected_bad_default_does_not_block_python_workflow(tmp_path: Path) -> None:
    from chrys.foundation.config.settings_store import load_settings
    from chrys.foundation.config.spec import Source

    project = make_workdir(tmp_path)
    write_workflow(project, "check", python_workflow("def check(text):\n    return text\n", "check"))
    loaded = load_settings(env={}).overlay(Source.SESSION, model_profile="removed-default")
    host = make_host(tmp_path, project=project, loaded_settings=loaded)
    try:
        await confirm(host, "check")
        _, events = await run(host, "check")
        assert of_type(events, WorkflowRunStarted)[0].model is None
    finally:
        await host.shutdown()


@pytest.mark.parametrize("binding", ["python", "agent_profile", "node"])
async def test_removed_unused_default_does_not_block_saved_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), *[MockChatClient(responses=[MockResponse(text="done")]) for _ in range(2)]],
    )
    project = make_workdir(tmp_path)
    profile = make_profile()
    if binding == "python":
        source = python_workflow("def check(text):\n    return text\n", "check")
    else:
        if binding == "agent_profile":
            profile = replace(profile, model=ModelConfig(profile_id="bound-model"))
        override = ", model='bound-model'" if binding == "node" else ""
        source = (
            "from chrys.workflows import WorkflowBuilder\n"
            "wf = WorkflowBuilder('check')\n"
            f"node = wf.agent('check', profile='Headless'{override})\n"
            "wf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
        ).encode()
    write_workflow(project, "check", source)
    host = make_host(tmp_path, project=project, profiles=[profile])
    registry = host.engine.model_registry
    assert registry is not None
    registry.register(ModelProfile(id="bound-model", name="Bound", provider="mock", model_id="bound"))
    try:
        await confirm(host, "check")
        first, _ = await run(host, "check")
        session_id = host.workflow_session_id
        selected = host.workflow_target("check").model
        assert selected is not None and selected.profile_id == "mock-profile"
        registry.remove("mock-profile")
        await host.load_workflow_session(session_id)
        second, events = await run(host, "check")
        assert first.outcome == second.outcome == RunOutcome.COMPLETED
        assert host.workflow_session_id == session_id
        started = of_type(events, WorkflowRunStarted)[0]
        assert started.model == selected
        assert [node["model_profile_id"] for node in started.resolved_nodes] == (
            [] if binding == "python" else ["bound-model"]
        )
    finally:
        await host.shutdown()


async def test_cancelled_model_save_holds_guard_until_checkpoint_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.orchestration.workflows.session import WorkflowSessionOwner
    from tests.support.waiting import wait_until

    project = make_workdir(tmp_path)
    write_workflow(project, "check", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    registry = host.engine.model_registry
    assert registry is not None
    registry.register(ModelProfile(id="second", name="second", provider="mock", model_id="second"))
    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_save, real_close = JsonFileStateStore.save_workflow_session, WorkflowSessionOwner.close

    async def save(store, session_id, state, *, title=""):
        if state.model is not None and state.model.profile_id == "second":
            entered.set()
            await release.wait()
        return await real_save(store, session_id, state, title=title)

    async def close(owner):
        closing.set()
        await real_close(owner)

    update: asyncio.Task | None = None
    try:
        await confirm(host, "check")
        await run(host, "check")
        monkeypatch.setattr(JsonFileStateStore, "save_workflow_session", create_autospec(real_save, side_effect=save))
        monkeypatch.setattr(WorkflowSessionOwner, "close", create_autospec(real_close, side_effect=close))
        update = asyncio.create_task(
            host.event_bus.publish(
                WorkflowModelChangeRequest(
                    session_id=host.workflow_session_id,
                    profile_id="second",
                    request_id="cancel-save",
                ),
                raise_handler_errors=True,
            )
        )
        await wait_for(lambda: entered.is_set() or update.done())
        if update.done():
            await update
        assert entered.is_set()
        update.cancel()
        assert not await wait_until(closing.is_set, timeout=0.1)
        with pytest.raises(WorkflowRunRejectedError, match="model is being updated"):
            await run(host, "check")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await update
        assert closing.is_set()
        await host.load_workflow_session(host.workflow_session_id)
        model = host.workflow_target("check").model
        assert model is not None and model.profile_id == "second"
    finally:
        release.set()
        if update is not None and not update.done():
            await asyncio.gather(update, return_exceptions=True)
        await host.shutdown()


async def test_restored_session_without_model_pins_the_resolved_default(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "echo", python_workflow("def echo(value):\n    return value\n", "echo"))
    host = make_host(tmp_path, project=project)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "echo")
        await run(host, "echo")
        session_id = host.workflow_session_id
        saved = (await store.load_workflow_session(session_id)).encode()
        assert saved is not None
        state = WorkflowSessionState.decode(saved)
        state.model = None
        await store.save_workflow_session(session_id, state)
        await host.load_workflow_session(session_id)
        assert host.workflow_target("echo").model is None

        _, events = await run(host, "echo")
        started = of_type(events, WorkflowRunStarted)[0]
        assert started.model is not None and started.model.profile_id == "mock-profile"
        saved = (await store.load_workflow_session(session_id)).encode()
        assert saved is not None
        assert WorkflowSessionState.decode(saved).model == started.model
        assert host.workflow_target("echo").model == started.model
    finally:
        await host.shutdown()


@pytest.mark.parametrize("binding", ["agent_profile", "node"])
async def test_a_judge_without_a_model_of_its_own_runs_on_its_nodes_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    """A node that brings its model keeps running when the run has none; so must its judge."""
    outside = tmp_path / "outside.txt"
    outside.write_text("reference", encoding="utf-8")
    read_outside = MockResponse(tool_calls=[("read_file", "call_1", {"path": str(outside)})])
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), MockChatClient(responses=[read_outside, MockResponse(text="done")])],
        builtin_tools=True,
    )
    evaluate = create_autospec(ApprovalJudge.evaluate, return_value=JudgeVerdict(approved=True, reason="in scope"))
    monkeypatch.setattr(ApprovalJudge, "evaluate", evaluate)
    project = make_workdir(tmp_path)
    profile = make_profile(builtins=["filesystem.read"])
    if binding == "agent_profile":
        profile = replace(profile, model=ModelConfig(profile_id="bound-model"))
    override = ", model='bound-model'" if binding == "node" else ""
    source = (
        "from chrys.workflows import WorkflowBuilder\n"
        "wf = WorkflowBuilder('check')\n"
        f"node = wf.agent('check', profile='Headless'{override})\n"
        "wf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
    ).encode()
    write_workflow(project, "check", source)
    host = make_host(tmp_path, project=project, profiles=[profile])
    registry = host.engine.model_registry
    assert registry is not None
    bound = ModelProfile(id="bound-model", name="Bound", provider="mock", model_id="bound")
    registry.register(bound)

    async def judge_this_run(event: WorkflowRunAccepted) -> None:
        await host.event_bus.publish(SetApprovalMode(mode="auto", persist=False))

    await host.event_bus.subscribe(WorkflowRunAccepted, judge_this_run)
    try:
        await confirm(host, "check")
        await host.start()
        result, _ = await run(host, "check", input_text="Read the reference.")
        assert result.outcome == RunOutcome.COMPLETED
        assert [call.args[0].profile for call in evaluate.await_args_list] == [bound]
    finally:
        await host.shutdown()


@pytest.mark.parametrize("auto_review", ["direct", "formal"])
async def test_workflow_judge_uses_run_strategy_and_shares_matching_profiles(tmp_path, auto_review):
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.workspace import Workspace
    from chrys.orchestration.workflows.session import WorkflowSessionOwner
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.session.persistence import SessionPersistence

    registry = ModelProfileRegistry()
    main = ModelProfile(id="main", name="Main", model_id="reasoning-main")
    node = replace(main, id="node", name="Node", model_id="reasoning-node")
    judge_profile = replace(main, id="judge", name="Judge", model_id="judge-test")
    for profile in (main, node, judge_profile):
        registry.register(profile)
    bus = EventBus()
    owner = WorkflowSessionOwner(
        bus=bus,
        persistence=SessionPersistence(JsonFileStateStore(tmp_path / "sessions"), bus),
        session_id="workflow-test",
        workspace=Workspace.from_cwd(str(tmp_path)),
    )
    settings = Settings(
        model_profile=main.id, approval_judge_model_profile=judge_profile.id, approval_auto_review=auto_review
    )
    owner._prepare_agents(settings, registry)
    try:
        first, second = owner.judge_for(None), owner.judge_for(node)
        assert first.profile == second.profile == judge_profile
        assert owner.judge_for(node) is second
        assert first is second
        assert first._auto_review == auto_review
    finally:
        await owner.close()
