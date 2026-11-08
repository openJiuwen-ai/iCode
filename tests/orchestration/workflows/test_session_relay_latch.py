# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow sessions latch relay risk: relay nodes tighten later nodes and judge choice."""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.workflow_session import WorkflowIdentity
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.persistence import SessionPersistence
from chrys.service.state.store import JsonFileStateStore
from chrys.service.state.workflow import WorkflowSessionState

_BINDING = WorkflowIdentity("review", "/project/review.py", "project")


def _profile(**overrides: object) -> ModelProfile:
    fields: dict[str, object] = {
        "id": "run-direct",
        "name": "Run Direct",
        "provider": "openai",
        "model_id": "gpt-run",
        "base_url": "https://direct.example.com/v1",
        "api_key": "sk-run",
    }
    fields.update(overrides)
    return ModelProfile(**fields)  # type: ignore[arg-type]


def _registry() -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    registry.register(_profile())
    registry.register(
        _profile(
            id="relay-node",
            name="Relay Node",
            model_id="gpt-relay",
            base_url="https://relay.example.com/v1",
            api_key="sk-relay",
            is_relay=True,
        )
    )
    registry.register(
        _profile(
            id="relay-judge",
            name="Relay Judge",
            model_id="gpt-judge",
            base_url="https://judge-relay.example.com/v1",
            api_key="sk-judge",
            is_relay=True,
        )
    )
    return registry


async def _hooks(*, project_root: str, project_hooks_enabled: bool, session_id: str, request_id: str = "") -> None:
    return None


async def _owner_with_agents(tmp_path, *, settings: Settings | None = None) -> WorkflowSessionOwner:
    bus = EventBus()
    store = JsonFileStateStore(tmp_path / "sessions")
    session_id = str(uuid4())
    await store.save_workflow_session(
        session_id,
        WorkflowSessionState.decode(
            {
                "identity": asdict(_BINDING),
                "workspace": asdict(Workspace.from_cwd(str(tmp_path))),
                "summary": {"run_count": 0, "latest_run_id": ""},
            }
        ),
    )
    owner = WorkflowSessionOwner(
        bus=bus,
        persistence=SessionPersistence(store, bus),
        session_id=session_id,
        workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await owner.open(reconcile=True)
    await owner.prepare(
        identity=_BINDING,
        run_id=uuid4().hex,
        title="Review",
        settings=settings or Settings(mutation_coordination=False),
        model_registry=_registry(),
        has_agents=True,
        hooks=_hooks,
    )
    return owner


async def test_relay_node_model_latches_the_session_for_later_direct_nodes(tmp_path) -> None:
    owner = await _owner_with_agents(tmp_path)
    try:
        registry = _registry()
        relay_node = registry.get("relay-node")
        direct_node = registry.get("run-direct")

        enhanced, reviewed = owner.approval_relay_context(relay_node)
        assert enhanced is True
        assert reviewed == "gpt-relay"
        # The relay node latched the session fact itself.
        assert owner.session.runtime_meta.relay_risk_seen is True

        # A later direct node stays enhanced (latched history risk) without
        # being named as a relay-routed working model.
        assert owner.approval_relay_context(direct_node) == (True, "")
    finally:
        await owner.close()


async def test_latched_history_disqualifies_a_relay_judge_for_direct_nodes(tmp_path) -> None:
    owner = await _owner_with_agents(
        tmp_path, settings=Settings(mutation_coordination=False, approval_judge_model_profile="relay-judge")
    )
    try:
        direct_node = _registry().get("run-direct")

        # Without latched risk the selector resolves as configured.
        assert owner._judge_model(direct_node) is not None
        assert owner._judge_model(direct_node).id == "relay-judge"

        owner.session.runtime_meta.relay_risk_seen = True

        # Latched relay-era content must not be judged by a relay operator.
        assert owner._judge_model(direct_node) is None
    finally:
        await owner.close()


async def test_runtime_latch_discovery_persists_through_runtime_meta(tmp_path) -> None:
    owner = await _owner_with_agents(tmp_path)
    try:
        assert owner.session.runtime_meta.relay_risk_seen is False

        owner.relay_latch.set()

        assert owner.session.runtime_meta.relay_risk_seen is True
    finally:
        await owner.close()
