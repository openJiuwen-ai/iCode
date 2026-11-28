# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for deleting model profiles through the ACP session manager."""

from __future__ import annotations

import pytest

from chrys.app.acp import session_manager as session_manager_module
from chrys.app.acp.session_manager import AcpSessionManager
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.acp._session_manager_fakes import _profile_manager


def _manager_with_model() -> tuple[AcpSessionManager, ModelProfileRegistry]:
    """A manager whose model registry already holds one in-memory profile."""
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="model", name="Mock"))
    return _profile_manager("Code", agent_registry, model_registry), model_registry


def test_delete_model_profile_drops_entry_even_when_file_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing file must still drop the stale in-memory entry."""
    manager, model_registry = _manager_with_model()
    assert model_registry.get("model") is not None
    monkeypatch.setattr(session_manager_module, "delete_model_profile", lambda _profile_id: False)

    result = manager.delete_model_profile("model")

    assert result == {"id": "model", "deleted": False}
    assert model_registry.get("model") is None


def test_delete_model_profile_drops_entry_and_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful delete removes both the file (stubbed) and the in-memory entry."""
    manager, model_registry = _manager_with_model()
    monkeypatch.setattr(session_manager_module, "delete_model_profile", lambda _profile_id: True)

    result = manager.delete_model_profile("model")

    assert result == {"id": "model", "deleted": True}
    assert model_registry.get("model") is None
