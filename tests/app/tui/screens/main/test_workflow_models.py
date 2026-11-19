# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow model selection, lifecycle locks, and historical model display."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.widgets import Select, Static

from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.workflow_session import WorkflowModelSelection
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.store import RunHeader, RunSpec, WorkflowRunStore
from tests.orchestration.workflows._hosting import make_profile, make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, dismiss_workflow_notice, open_workflow, switch_mode, workflow_selection

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

MODEL_A = WorkflowModelSelection("model-a", "Workflow [A]", "model-a-id")
MODEL_B = WorkflowModelSelection("model-b", "Workflow B", "model-b-id")


def registries() -> tuple[AgentProfileRegistry, ModelProfileRegistry]:
    agents, models = AgentProfileRegistry(), ModelProfileRegistry()
    agents.register(make_profile())
    for model in (MODEL_A, MODEL_B):
        models.register(ModelProfile(id=model.profile_id, name=model.name, provider="mock", model_id=model.model_id))
    return agents, models


def project_workflow(tmp_path: Path) -> Path:
    project = make_project(tmp_path)
    write_workflow(
        project,
        "review",
        b"from chrys.workflows import WorkflowBuilder\n"
        b"wf = WorkflowBuilder('review')\n"
        b"a = wf.agent('reviewer', profile='Headless')\n"
        b"wf.start(a)\nwf.output(a)\nworkflow = wf.build()\n",
    )
    return project


async def test_workflow_remembers_its_model_across_mode_switches_and_new_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    bus = EventBus()
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=Settings(model_profile=MODEL_A.profile_id),
        engine=WorkflowEngine(),
        event_bus=bus,
        model_registry=models,
    )
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        await open_workflow(main, pilot, "review")
        workflow = main._workflow
        assert workflow.model == MODEL_A
        await workflow.run_control.choose_model(MODEL_B.profile_id)
        assert main._services.active_model_profile_id == MODEL_A.profile_id
        await switch_mode(main, pilot)
        # A subsequent Chat runtime update must not replace the Workflow choice.
        main._set_active_model_profile_id(MODEL_A.profile_id)
        await switch_mode(main, pilot)
        assert workflow.model == MODEL_B
        assert await workflow.session_view.new_session()
        assert workflow.model == MODEL_B
        graph = main._workflow_panel.query_one(WorkflowGraph)
        await wait_for(
            lambda: graph.diagram is not None and MODEL_B.model_id in "\n".join(graph.diagram.rows), pilot=pilot
        )
        await open_workflow(main, pilot, "review")
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert workflow.run_control.start("review")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert requests[0].target.model == MODEL_B
            await bus.publish(events.WorkflowRunRejected(request_id=requests[0].request_id, error="test"))


@pytest.mark.parametrize("chat_selector", ["", "deleted-model"])
async def test_unset_workflow_model_waits_for_a_usable_chat_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_selector: str
) -> None:
    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=Settings(model_profile=chat_selector),
        engine=WorkflowEngine(),
        model_registry=models,
    )
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        await open_workflow(main, pilot, "review")
        workflow = main._workflow
        assert workflow.model is None
        assert not main.query_one(StatusBar).display
        await switch_mode(main, pilot)
        main._set_active_model_profile_id(MODEL_A.profile_id)
        await switch_mode(main, pilot)
        assert workflow.model == MODEL_A
        assert workflow.browser.draft.model == MODEL_A
        graph = main._workflow_panel.query_one(WorkflowGraph)
        assert graph.diagram is not None and MODEL_A.model_id in "\n".join(graph.diagram.rows)
        await switch_mode(main, pilot)
        main._set_active_model_profile_id(MODEL_B.profile_id)
        await switch_mode(main, pilot)
        assert workflow.model == MODEL_A


async def test_model_picker_is_workflow_local_and_locked_through_run_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus, model_registry=models)
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        status = main.query_one(StatusBar)
        status.set_model(ModelIndicatorState("Chat only", "", "select", "chat", True))
        await open_workflow(main, pilot, "review")
        workflow = main._workflow
        assert not status.display
        initial_model = workflow.model
        workflow.run_control.collect_input()
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        app.screen.query_one(Select).value = MODEL_A.profile_id
        await click_when_settled(pilot, "#workflow-input-cancel")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert workflow.model == initial_model
        async with (
            capture_event_sequence(bus, events.SettingsReload) as reloads,
            capture_event_sequence(bus, events.WorkflowRunRequest) as requests,
        ):
            workflow.run_control.collect_input()
            await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
            app.screen.query_one(Select).value = MODEL_A.profile_id
            await click_when_settled(pilot, "#workflow-input-start")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert requests[0].target.model == MODEL_A
            await bus.publish(events.WorkflowRunRejected(request_id=requests[0].request_id, error="test rejection"))
            await dismiss_workflow_notice(main, pilot, "test rejection")
        assert not reloads
        assert workflow.model == MODEL_A
        graph = main._workflow_panel.query_one(WorkflowGraph)
        assert graph.diagram is not None and MODEL_A.model_id in "\n".join(graph.diagram.rows)
        # Chat updates remain cached independently of the Workflow model.
        status.set_model(ModelIndicatorState("Chat changed", "", "select", "chat-next", True))
        assert workflow.model == MODEL_A
        await switch_mode(main, pilot)
        assert str(status.query_one("#model-tag", Static).content) == "Chat changed"
        await switch_mode(main, pilot)
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert workflow.run_control.start("review")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert requests[0].target.model == MODEL_A
            await workflow.run_control.choose_model("model-b")
            assert workflow.model == MODEL_A
            workflow.run_control.collect_input()
            assert not isinstance(app.screen, WorkflowInputDialog)
            await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
            selection = replace(workflow_selection(main), model=MODEL_A)
            await bus.publish(
                events.WorkflowRunAccepted(request_id=requests[0].request_id, run_id="run", selection=selection)
            )
            await bus.publish(events.WorkflowRunStarted(run_id="run", model=MODEL_A))
            await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="completed"))
            await workflow.run_control.choose_model("model-b")
            assert workflow.model == MODEL_A
            assert not workflow.run_control.can_change_model()
            await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
            await wait_for(workflow.run_control.can_change_model, pilot=pilot)


async def test_model_change_ack_and_picker_target_are_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus, model_registry=models)
    entered, release = asyncio.Event(), asyncio.Event()
    task: asyncio.Task | None = None
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        await open_workflow(main, pilot, "review")
        workflow = main._workflow
        await workflow.run_control.choose_model("model-a")
        # Callback belongs to the target visible when the picker opened.
        workflow.run_control.collect_input()
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        workflow.browser.draft = replace(workflow.browser.draft, model=None)
        app.screen.query_one(Select).value = MODEL_B.profile_id
        await click_when_settled(pilot, "#workflow-input-start")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert workflow.model is None
        workflow.session_view.selection = replace(workflow_selection(main), model=MODEL_A)

        async def save(event: events.WorkflowModelChangeRequest) -> None:
            entered.set()
            await release.wait()
            await bus.publish(
                events.WorkflowModelChangeResult(
                    session_id=event.session_id,
                    request_id=event.request_id,
                    selection=replace(workflow.session_view.selection, model=MODEL_B),
                )
            )

        await bus.subscribe(events.WorkflowModelChangeRequest, save)
        try:
            task = asyncio.create_task(workflow.run_control.choose_model("model-b"))
            await wait_for(lambda: entered.is_set() or task.done(), pilot=pilot)
            if task.done():
                await task
            assert entered.is_set()
            assert workflow.model == MODEL_A and workflow.awaiting_engine
            assert not workflow.run_control.start("too soon")
            release.set()
            await task
            assert workflow.model == MODEL_B and not workflow.awaiting_engine
            assert await workflow.session_view.new_session()
            assert workflow.model == MODEL_B
        finally:
            release.set()
            if task is not None:
                await task


@pytest.mark.parametrize("save_ok", [True, False])
async def test_start_waits_for_session_model_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, save_ok: bool
) -> None:
    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus, model_registry=models)
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        await open_workflow(main, pilot, "review")
        workflow = main._workflow
        selection = replace(workflow_selection(main), model=MODEL_A)
        workflow.session_view.selection = selection
        changes = []

        async def checkpoint(event: events.WorkflowModelChangeRequest) -> None:
            changes.append(event)
            await bus.publish(
                events.WorkflowModelChangeResult(
                    session_id=event.session_id,
                    request_id=event.request_id,
                    selection=replace(selection, model=MODEL_B) if save_ok else None,
                    error="" if save_ok else "model save failed",
                )
            )

        await bus.subscribe(events.WorkflowModelChangeRequest, checkpoint)
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            await click_when_settled(pilot, "#workflow-start")
            await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
            assert app.screen.query_one(Select).value == MODEL_A.profile_id
            app.screen.query_one(Select).value = MODEL_B.profile_id
            await click_when_settled(pilot, "#workflow-input-start")
            await wait_for(lambda: bool(changes), pilot=pilot)
            if save_ok:
                await wait_for(lambda: bool(requests), pilot=pilot)
                assert requests[0].target.model == MODEL_B
                assert workflow.model == MODEL_B
            else:
                await dismiss_workflow_notice(main, pilot, "model save failed")
                assert not requests and workflow.model == MODEL_A


@pytest.mark.parametrize("saved_model", [MODEL_B, None])
async def test_resume_and_run_tabs_show_saved_models_even_after_profile_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved_model: WorkflowModelSelection | None,
) -> None:
    project = project_workflow(tmp_path)
    monkeypatch.chdir(project)
    agents, models = registries()
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=Settings(model_profile=MODEL_A.profile_id),
        engine=WorkflowEngine(),
        model_registry=models,
    )
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        preview = await open_workflow(main, pilot, "review")
        selection = replace(workflow_selection(main, "abcdef1234567890"), model=saved_model)
        store = main._services.state_store
        assert store is not None
        ids = [new_analytics_id(), new_analytics_id()]
        for index, (run_id, model) in enumerate(zip(ids, (MODEL_A, saved_model), strict=True), 1):
            archive = WorkflowRunStore.open(
                spec=RunSpec(manifest=preview.manifest, environment={}, resolved_nodes=()),
                input_text="",
                run_dir=run_dir(store.session_dir(selection.session_id), run_id),
                source=preview.source.source,
                header=RunHeader(
                    run_id=run_id,
                    session_id=selection.session_id,
                    workflow_id=preview.source.workflow_id,
                    source_kind=preview.source.source_kind,
                    canonical_path=preview.source.canonical_path,
                    title=preview.title,
                    input_excerpt="",
                    entry_digest=preview.load.entry_digest,
                    manifest_digest=preview.load.manifest_digest,
                    schema_version=preview.manifest["schema_version"],
                    spec_digest=preview.spec_digest,
                    model=model,
                    started_at=f"2026-09-18T00:00:0{index}+00:00",
                ),
            )
            await archive.close()
        await store.save_workflow_session(
            selection.session_id,
            WorkflowSessionState(
                selection.identity,
                selection.workspace,
                run_count=2,
                latest_run_id=ids[-1],
                model=saved_model,
            ),
        )
        models.remove("model-b")
        await main._workflow.session_view.restore_session(selection.session_id)
        await switch_mode(main, pilot)
        await switch_mode(main, pilot)
        assert main._workflow.model == saved_model
        await main._workflow.session_view.select_run(ids[0])
        # An archived run is shown once the dialog that covered its read has closed.
        await wait_for(lambda: main._workflow_panel.run_id == ids[0] and app.screen is main, pilot=pilot)
        run = main._workflow.session_view.view_run()
        assert run is not None and run.started.model == MODEL_A
        main._workflow.run_control.collect_input()
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        assert app.screen.query_one(Select).value == ("" if saved_model is not None else Select.NULL)
        await click_when_settled(pilot, "#workflow-input-cancel")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await main._workflow.session_view.select_run(ids[1])
        await wait_for(main._workflow.run_control.can_change_model, pilot=pilot)
        assert main._workflow.model == saved_model
        await main._workflow.session_view.select_run(ids[0])
        assert await main._workflow.session_view.new_session()
        # Browsing an older Run cannot replace the current session's default.
        assert main._workflow.model == (saved_model or MODEL_A)


async def test_start_refreshes_changed_profile_and_chat_picker_cannot_change_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus, model_registry=models)
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        await open_workflow(main, pilot, "review")
        workflow = main._workflow
        await workflow.run_control.choose_model("model-a")
        profile = models.get("model-a")
        assert profile is not None
        models.register(replace(profile, model_id="new-model-id"))
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            workflow.run_control.collect_input()
            await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
            # The saved snapshot is preserved until the current profile is explicitly selected.
            assert app.screen.query_one(Select).value == ""
            app.screen.query_one(Select).value = "model-a"
            await click_when_settled(pilot, "#workflow-input-start")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert requests[0].target.model == replace(MODEL_A, model_id="new-model-id")
            await bus.publish(events.WorkflowRunRejected(request_id=requests[0].request_id, error="test"))
            await dismiss_workflow_notice(main, pilot, "test")
        saved = workflow.model
        await main._switch_model_profile("model-b").wait()
        assert workflow.model == saved


async def test_enter_restarts_model_resolution_cancelled_by_leaving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.orchestration.workflows import settings as workflow_settings

    monkeypatch.chdir(project_workflow(tmp_path))
    agents, models = registries()
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=Settings(model_profile=MODEL_A.profile_id),
        engine=WorkflowEngine(),
        model_registry=models,
    )
    async with app.run_test(size=(140, 44)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        await open_workflow(main, pilot, "review")
        started, cancelled = asyncio.Event(), asyncio.Event()
        resolve = workflow_settings.preview_bindings

        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(workflow_settings, "preview_bindings", create_autospec(resolve, side_effect=blocked))
        assert await main._workflow.run_control.choose_model(MODEL_B.profile_id)
        await wait_for(started.is_set, pilot=pilot)
        await switch_mode(main, pilot)
        await wait_for(cancelled.is_set, pilot=pilot)
        monkeypatch.setattr(workflow_settings, "preview_bindings", resolve)
        await switch_mode(main, pilot)
        await wait_for(
            lambda: (
                bool(main._workflow_panel._preview_models)
                and main._workflow_panel._preview_models[0]["model_id"] == MODEL_B.model_id
            ),
            pilot=pilot,
        )
