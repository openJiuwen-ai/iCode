# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interactive handoff through a real Textual app and native engine lifecycle."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

from chrys.app.cli.interactive_launch import InteractiveLaunch, LaunchManifest
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.state.store import JsonFileStateStore
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from textual._path import CSSPathType

    from chrys.app.tui.screens.main import MainScreen
    from tests.support.engines import AgentEngineFactory


def prepare(tmp_path: Path, request_id: str, *, session_id: str = "") -> InteractiveLaunch:
    text = f"Interactive context nonce {request_id}: fetch the business context."
    value = {
        "protocolVersion": 1,
        "requestId": request_id,
        "cwd": str(tmp_path),
        "mode": "resume" if session_id else "new",
        "configuration": {},
        "context": {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()},
        "skills": [],
    }
    if session_id:
        value["sessionId"] = session_id
    path = tmp_path / f"{request_id}.json"
    plant_owner_only_bytes(path, json.dumps(value).encode())
    launch = InteractiveLaunch(LaunchManifest.read(path))
    if not launch.claim():
        raise RuntimeError("Test request already exists")
    return launch


def make_app(
    tmp_path: Path,
    launch: InteractiveLaunch,
    factory: AgentEngineFactory,
    app_type: type[ChrysApp] = ChrysApp,
) -> ChrysApp:
    settings, models = make_mock_settings_and_registry()
    registry = AgentProfileRegistry()
    registry.register(AgentProfile(name="Code", id="code-test"))
    bus = EventBus()
    state_store = JsonFileStateStore(tmp_path / "sessions")
    engine = factory(bus, settings=settings, model_registry=models, agent_registry=registry, state_store=state_store)
    return app_type(
        bus,
        engine,
        settings_handle=engine.settings_handle,
        model_registry=models,
        agent_registry=registry,
        state_store=state_store,
        profile_name="Code",
        startup_session_id=launch.manifest.session_id,
        interactive_launch=launch,
        gc_freeze_enabled=False,
    )


async def test_unsent_interactive_draft_closes_then_resumes_the_same_native_session(
    tmp_path: Path,
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    launch = prepare(tmp_path, "new-one")
    app = make_app(tmp_path, launch, agent_engine)
    async with app.run_test() as pilot:
        await wait_for(
            lambda: launch.receipt["status"] != "unknown",
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="interactive context staged",
        )
        assert launch.receipt["status"] == "confirmed"
        native_id = launch.receipt["sessionId"]
        assert app.screen.query_one(InputBar).snapshot_draft().text == launch.manifest.text
        assert launch.receipt["submitted"] == "unverified"
    # This is a new request with fresh material, not a replay of the staged receipt.
    resume = prepare(tmp_path, "resume-one", session_id=native_id)
    restored = make_app(tmp_path, resume, agent_engine)
    async with restored.run_test() as pilot:
        await wait_for(
            lambda: resume.receipt["status"] != "unknown",
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="same interactive native session restored",
        )
        assert resume.receipt["status"] == "confirmed"
        assert resume.receipt["sessionId"] == native_id
        assert restored.screen.query_one(InputBar).snapshot_draft().text == resume.manifest.text
        assert resume.receipt["materials"] == "composer-staged"
        assert resume.receipt["submitted"] == resume.receipt["adopted"] == "unverified"
        repeated = InteractiveLaunch(resume.manifest)
        assert repeated.claim() is False
        assert repeated.receipt["sessionId"] == native_id


async def test_strict_interactive_restore_of_missing_session_creates_no_replacement(
    tmp_path: Path,
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    launch = prepare(tmp_path, "missing-one", session_id="missing-complete-session-id")
    app = make_app(tmp_path, launch, agent_engine)
    async with app.run_test():
        await wait_for(
            lambda: launch.receipt["status"] == "failed",
            timeout=ENGINE_TURN_TIMEOUT,
            description="strict interactive restore explicitly failed",
        )
        assert app._engine.session_id is None
        assert "sessionId" not in launch.receipt
        assert launch.receipt["failedStage"] == "session-restore"
        assert launch.receipt["materials"] == "not-staged"


async def test_interactive_launch_keeps_an_existing_composer_and_records_the_acquired_native_id(
    tmp_path: Path,
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    launch = prepare(tmp_path, "occupied-composer")

    class OccupiedComposerApp(ChrysApp):
        CSS_PATH: ClassVar[CSSPathType | None] = [
            Path(inspect.getfile(ChrysApp)).parent / "chrys.tcss",
            Path(inspect.getfile(ChrysApp)).parent / "screens/themes/editor.tcss",
        ]
        preserved_draft = ""

        async def _start_interactive_launch(self, profile: AgentProfile, screen: MainScreen) -> None:
            assert screen.query_one(InputBar).restore_draft("My existing unsent draft")
            await super()._start_interactive_launch(profile, screen)
            self.preserved_draft = screen.query_one(InputBar).snapshot_draft().text

    app = make_app(tmp_path, launch, agent_engine, OccupiedComposerApp)
    async with app.run_test():
        await wait_for(
            lambda: launch.receipt["status"] == "failed" and app._startup_task is not None and app._startup_task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="occupied composer refuses launch material",
        )
        assert launch.receipt["failedStage"] == "composer-stage"
        assert launch.receipt["sessionId"]
        assert launch.receipt["materials"] == "not-staged"
        assert app.preserved_draft == "My existing unsent draft"
