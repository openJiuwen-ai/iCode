# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared host/engine/store doubles and manager factories for the ACP session-manager tests."""

from __future__ import annotations

from typing import ClassVar

import pytest
import yaml
from acp import schema as acp_schema

from chrys.app.acp.session_manager import AcpSessionManager
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ProfileSwitched
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    MCPServerConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import SessionMeta, StateStore
from tests.support.platform_fakes import platform_with_config_dir


class _PinnableEngine:
    """Stand-in for the engine the manager pins the ask_user timeout on."""

    def __init__(self) -> None:
        self.timeout_pinned = False
        self.snapshot: ProfileSwitched | None = None
        self.recovered_from_sidecar = False
        self.loaded_settings = LoadedSettings(settings=Settings(), provenance={})

    def pin_ask_user_timeout(self) -> None:
        self.timeout_pinned = True

    def current_profile_snapshot(self) -> ProfileSwitched:
        return self.snapshot if self.snapshot is not None else ProfileSwitched()


class _CloseHost:
    def __init__(self) -> None:
        self.shutdown_called = False
        self.engine = _PinnableEngine()

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _FailingStartHost:
    instances: ClassVar[list[_FailingStartHost]] = []

    def __init__(self, **kwargs) -> None:
        self.session_id = "failed-session"
        self.shutdown_called = False
        self.engine = _PinnableEngine()
        self.event_bus = EventBus()
        _ = kwargs
        self.instances.append(self)

    async def start(self) -> None:
        raise RuntimeError("boom")

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _StartedHost:
    instances: ClassVar[list[_StartedHost]] = []

    def __init__(self, **kwargs) -> None:
        self.session_id = f"started-session-{len(self.instances) + 1}"
        self.shutdown_called = False
        self.engine = _PinnableEngine()
        if "loaded_settings" in kwargs:
            self.engine.loaded_settings = kwargs["loaded_settings"]
        self.event_bus = EventBus()
        self.kwargs = kwargs
        self.instances.append(self)

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _StaticListStore:
    def __init__(self, sessions: list[SessionMeta]) -> None:
        self._sessions = sessions

    async def list_sessions(self, *, kind=None) -> list[SessionMeta]:
        return [meta for meta in self._sessions if kind is None or meta.kind == kind]

    def session_dir(self, _session_id: str):
        raise NotImplementedError

    async def save_session(self, *_args, **_kwargs) -> None:
        raise NotImplementedError

    async def load_session(self, _session_id: str, *, prefer_recovery: bool = False):
        _ = prefer_recovery
        raise NotImplementedError

    async def load_session_raw(self, _session_id: str, *, prefer_recovery: bool = False):
        _ = prefer_recovery
        raise NotImplementedError

    async def load_session_transcript(self, _session_id: str, *, prefer_recovery: bool = False):
        _ = prefer_recovery
        raise NotImplementedError

    async def delete_session(self, _session_id: str, *, allow_active: bool = False) -> None:
        _ = allow_active
        raise NotImplementedError


def _registries() -> tuple[AgentProfileRegistry, ModelProfileRegistry]:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="model", name="Mock"))
    return agent_registry, model_registry


def _manager(process_cwd: str | None, store: StateStore, *, profile_name: str = "Code") -> AcpSessionManager:
    agent_registry, model_registry = _registries()
    return AcpSessionManager(
        loaded_settings=LoadedSettings(settings=Settings(), provenance={}),
        profile_name=profile_name,
        approval_mode=ApprovalMode.MANUAL,
        process_cwd=process_cwd,
        state_store=store,
        agent_registry=agent_registry,
        model_registry=model_registry,
    )


def _stdio_mcp() -> list[acp_schema.McpServerStdio]:
    return [
        acp_schema.McpServerStdio(
            type="stdio",
            name="local",
            command="/usr/bin/python3",
            args=["-m", "local_server"],
            env=[acp_schema.EnvVariable(name="TOKEN", value="literal-{{NOT_A_PLACEHOLDER}}")],
        )
    ]


def _unsupported_sse_mcp() -> list[acp_schema.SseMcpServer]:
    return [
        acp_schema.SseMcpServer(
            type="sse",
            name="events",
            url="https://example.test/sse",
            headers=[],
        )
    ]


def _profile_manager(
    profile_name: str,
    agent_registry: AgentProfileRegistry,
    model_registry: ModelProfileRegistry | None = None,
) -> AcpSessionManager:
    """A manager wired to a caller-supplied profile registry and an empty session store."""
    return AcpSessionManager(
        loaded_settings=LoadedSettings(settings=Settings(), provenance={}),
        profile_name=profile_name,
        approval_mode=ApprovalMode.MANUAL,
        process_cwd=None,
        state_store=_StaticListStore([]),
        agent_registry=agent_registry,
        model_registry=model_registry if model_registry is not None else ModelProfileRegistry(),
    )


def _acp_manager(agent_registry: AgentProfileRegistry) -> AcpSessionManager:
    return _profile_manager("WithAcp", agent_registry)


def _mcp_profile() -> AgentProfile:
    return AgentProfile(
        name="WithMcp",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(
                    name="remote",
                    transport="http",
                    url="https://example.test/mcp",
                    headers={"Authorization": "Bearer real-token", "X-Empty": ""},
                ),
                MCPServerConfig(
                    name="local",
                    transport="stdio",
                    command="run",
                    env={"API_KEY": "real-secret"},
                ),
            ]
        ),
    )


def _acp_profile() -> AgentProfile:
    return AgentProfile(
        name="WithAcp",
        sub_agent_only=True,
        acp=AcpAgentConfig(
            command="remote-agent",
            args=["--token", "real-token", "***"],
            env={"API_KEY": "real-secret", "EMPTY": ""},
            config_options={"channel": "private", "telemetry": False},
        ),
    )


def _redirect_config_dir(monkeypatch: pytest.MonkeyPatch, config_dir) -> None:
    """Point the settings document at a tmp dir and clear ambient mirrors."""
    fake_platform = platform_with_config_dir(config_dir)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    for key in ("CHRYS_DEFAULT_APPROVAL_MODE", "CHRYS_ROLLBACK_SNAPSHOTS_KEEP", "CHRYS_THEME"):
        monkeypatch.delenv(key, raising=False)


def _stored_setting(config_dir, dotted: str) -> object | None:
    settings_path = config_dir / "settings.yaml"
    if not settings_path.exists():
        return None
    node: object = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class _SettingsScopeEngine:
    def __init__(self, loaded: LoadedSettings) -> None:
        self.loaded_settings = loaded


class _SettingsScopeHost:
    def __init__(self, loaded: LoadedSettings) -> None:
        self.engine = _SettingsScopeEngine(loaded)


class _EngineStub:
    def __init__(self, *, is_turn_active: bool) -> None:
        self.is_turn_active = is_turn_active


class _InjectHost:
    def __init__(self, bus, *, is_turn_active: bool) -> None:
        self.session_id = "s1"
        self.event_bus = bus
        self.engine = _EngineStub(is_turn_active=is_turn_active)
