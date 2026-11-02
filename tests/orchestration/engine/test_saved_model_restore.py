# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Saved-model reapplication on session restore: the restored session's own model, never the default."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from chrys.foundation.config.settings import (
    Settings,
)
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.config.spec import Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    SessionRestore,
    SettingsReload,
)
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ModelConfig,
)
from chrys.service.profiles.models.resolver import active_model_profile_fields
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine._recovery_helpers import (
    _model_registry,
    _profile,
    _registry,
    _seed_restorable_session,
    _session_meta,
    stub_engine_lifecycle,
)
from tests.support.loaded_agents import install_loaded_agent


@pytest.fixture(autouse=True)
def _pinned_model_profile_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ``CHRYS_MODEL_PROFILE`` back at teardown, whatever the restore under test wrote to it.

    A saved-model restore must not write the process pointer (its environment
    mirror); this keeps a regression from leaking past the test. ``delenv(raising=False)``
    on an absent variable records nothing to undo, so the absent case is pinned by a
    set-then-delete pair instead.
    """
    previous = os.environ.get("CHRYS_MODEL_PROFILE")
    if previous is None:
        monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
        monkeypatch.delenv("CHRYS_MODEL_PROFILE")
    else:
        monkeypatch.setenv("CHRYS_MODEL_PROFILE", previous)


async def test_tui_session_restore_runs_the_saved_model_as_the_sessions_own_without_moving_the_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    dotenv = config_dir / ".env"
    original_dotenv = b'CHRYS_MODEL_PROFILE="global-model"\nUNCHANGED="yes"\n'
    dotenv.write_bytes(original_dotenv)
    platform = replace(get_platform(), config_dir=config_dir, data_dir=config_dir)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)

    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    agent_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path / "sessions")
    await _seed_restorable_session(store, agent_profile=agent_profile.name, model_profile_id=saved_model.id)
    settings = Settings(model_profile="global-model")
    engine = assemble_agent_engine(
        EventBus(),
        settings=settings,
        state_store=store,
        agent_registry=_registry(agent_profile),
        model_registry=_model_registry(saved_model),
    )
    started_model_ids: list[str] = []

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        assert start_profile is agent_profile
        assert operation == "restore"
        # The reapplied model travels as the session's own choice; a successful
        # build commits it with the staged load, so a fake simulating one must.
        handle = engine.settings_handle
        handle.install_prepared(handle.prepare(staged_loaded or handle.base, session=session))
        started_model_ids.append(engine.settings.model_profile)

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me", apply_saved_model=True))
    finally:
        engine.session.guard.release()

    assert started_model_ids == [saved_model.id]
    assert engine.settings.model_profile == saved_model.id
    assert engine.settings.model_profile_override == ""
    assert engine.settings_handle.session_model == saved_model.id
    assert engine.settings_handle.base.settings.model_profile != saved_model.id
    # The default new sessions start on did not move: no pointer write, no dotenv write.
    assert "CHRYS_MODEL_PROFILE" not in os.environ
    assert dotenv.read_bytes() == original_dotenv


def test_saved_model_restore_clears_the_previous_sessions_pin() -> None:
    """The pin outranks the plain selection, so leaving it standing defeats the restore.

    Switching models pins the whole selection — ``model_profile_override``
    included, and the resolver consults that field first. A restore that writes
    only ``model_profile`` claims the saved model while the rebuild goes on
    resolving the previous session's.
    """
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(
            model_profile="pinned-model",
            model_profile_override="pinned-model",
            model_profile_override_sub_agents=True,
        ),
        model_registry=_model_registry(saved_model),
    )

    choice = engine.lifecycle._restored_session_choice(
        _session_meta(model_profile_id=saved_model.id),
        _profile("Code"),
        engine.settings_handle.base,
    )
    staged = engine.settings_handle.prepare(engine.settings_handle.base, session=choice).effective

    assert choice == active_model_profile_fields(saved_model)
    assert staged.settings.model_profile == saved_model.id
    assert staged.settings.model_profile_override == ""
    assert staged.settings.model_profile_override_sub_agents is False
    # Nothing installed: the selection goes live with the build's commit.
    assert engine.settings.model_profile == "pinned-model"


def test_saved_model_restore_moves_settings_and_provenance_together() -> None:
    """The restored model is a session-scoped decision and has to say so.

    A bare ``replace`` on the settings would leave the staged load's two
    halves disagreeing, which the build's commit turns into a live
    inconsistency.
    """
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(model_profile="global-model"),
        model_registry=_model_registry(saved_model),
    )

    choice = engine.lifecycle._restored_session_choice(
        _session_meta(model_profile_id=saved_model.id),
        _profile("Code"),
        engine.settings_handle.base,
    )
    staged = engine.settings_handle.prepare(engine.settings_handle.base, session=choice).effective

    assert staged.settings.model_profile == saved_model.id
    assert staged.source_for("model.profile.active").layer is Source.SESSION
    # The transform stays staged: the live settings have not moved.
    assert engine.settings.model_profile == "global-model"


async def test_a_failed_reload_after_a_saved_model_restore_keeps_the_restored_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload that fails changes nothing: the session keeps the model it runs on.

    Otherwise a reload that fails puts the pre-restore model back into
    ``settings`` while the executor the failure kept running is still bound to
    the saved one — the two disagreeing about which model is in use.
    """
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(model_profile="global-model"),
        model_registry=_model_registry(saved_model),
    )
    engine.session.agent_profile = _profile("Code")
    install_loaded_agent(engine, loaded=None)
    choice = engine.lifecycle._restored_session_choice(
        _session_meta(model_profile_id=saved_model.id),
        _profile("Code"),
        engine.settings_handle.base,
    )
    # The restore build's commit, minimally: the session's own model goes live.
    engine.settings_handle.install_prepared(engine.settings_handle.prepare(engine.settings_handle.base, session=choice))

    async def failing_start(
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = profile, operation
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(engine.lifecycle, "start", failing_start)

    with pytest.raises(RuntimeError):
        await engine._on_settings_reload(SettingsReload())

    assert engine.settings.model_profile == saved_model.id
    assert engine.loaded_settings.settings.model_profile == saved_model.id


@pytest.mark.parametrize("case", ["empty", "missing", "unselectable", "current"])
def test_saved_model_restore_short_circuits_without_side_effects(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    profiles: tuple[ModelProfile, ...]
    settings_model = "current-model"
    saved_model_id = saved_model.id
    if case == "empty":
        profiles = (saved_model,)
        saved_model_id = ""
    elif case == "missing":
        profiles = ()
    elif case == "unselectable":
        profiles = (replace(saved_model, model_id=""),)
    else:
        profiles = (saved_model,)
        settings_model = saved_model.id

    settings = Settings(model_profile=settings_model, model_profile_override="existing-override")
    engine = assemble_agent_engine(EventBus(), settings=settings, model_registry=_model_registry(*profiles))
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "environment-before")

    choice = engine.lifecycle._restored_session_choice(
        _session_meta(model_profile_id=saved_model_id),
        _profile("Code"),
        engine.settings_handle.base,
    )

    assert choice == {}
    assert engine.settings is settings
    assert engine.settings.model_profile_override == "existing-override"
    assert os.environ["CHRYS_MODEL_PROFILE"] == "environment-before"


def test_saved_model_restore_skips_registered_agent_binding_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    dotenv = config_dir / ".env"
    original_dotenv = b'CHRYS_MODEL_PROFILE="global-model"\n'
    dotenv.write_bytes(original_dotenv)
    platform = replace(get_platform(), config_dir=config_dir, data_dir=config_dir)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    bound_model = ModelProfile(id="bound-model", name="Bound", model_id="gpt-bound")
    agent_profile = replace(_profile("Code"), model=ModelConfig(profile_id=bound_model.id))
    settings = Settings(model_profile="global-model", model_profile_override="existing-override")
    engine = assemble_agent_engine(
        EventBus(),
        settings=settings,
        model_registry=_model_registry(saved_model, bound_model),
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "environment-before")

    choice = engine.lifecycle._restored_session_choice(
        _session_meta(model_profile_id=saved_model.id),
        agent_profile,
        engine.settings_handle.base,
    )

    assert choice == {}
    assert engine.settings is settings
    assert engine.settings.model_profile_override == "existing-override"
    assert os.environ["CHRYS_MODEL_PROFILE"] == "environment-before"
    assert dotenv.read_bytes() == original_dotenv


async def test_session_restore_default_gate_does_not_apply_saved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "environment-before")
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    agent_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=agent_profile.name, model_profile_id=saved_model.id)
    settings = Settings(model_profile="current-model", model_profile_override="existing-override")
    engine = assemble_agent_engine(
        EventBus(),
        settings=settings,
        state_store=store,
        agent_registry=_registry(agent_profile),
        model_registry=_model_registry(saved_model),
    )
    started_model_ids: list[str] = []

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        _profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        assert operation == "restore"
        assert not session
        started_model_ids.append(engine.settings.model_profile)

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert started_model_ids == ["current-model"]
    assert engine.settings is settings
    assert engine.settings.model_profile_override == "existing-override"
    assert os.environ["CHRYS_MODEL_PROFILE"] == "environment-before"


async def test_saved_model_restore_changes_nothing_when_start_fails_before_its_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    agent_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=agent_profile.name, model_profile_id=saved_model.id)
    settings = Settings(model_profile="old-model", model_profile_override="existing-override")
    engine = assemble_agent_engine(
        EventBus(),
        settings=settings,
        state_store=store,
        agent_registry=_registry(agent_profile),
        model_registry=_model_registry(saved_model),
    )

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fail_start(
        _profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        assert operation == "restore"
        # The reapplied model arrives staged as the session's own; a build that
        # fails before its commit never installs it, so nothing live has moved.
        assert session == active_model_profile_fields(saved_model)
        assert engine.settings.model_profile == "old-model"
        raise RuntimeError("start failed")

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fail_start)

    try:
        with pytest.raises(RuntimeError, match="start failed"):
            await engine.on_session_restore(SessionRestore(session_id="restore_me", apply_saved_model=True))
    finally:
        engine.session.guard.release()

    assert engine.settings is settings
    assert engine.settings.model_profile_override == "existing-override"
    assert "CHRYS_MODEL_PROFILE" not in os.environ


async def test_saved_model_restore_does_not_roll_back_after_executor_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "environment-before")
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    agent_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=agent_profile.name, model_profile_id=saved_model.id)
    settings = Settings(model_profile="old-model", model_profile_override="existing-override")
    engine = assemble_agent_engine(
        EventBus(),
        settings=settings,
        state_store=store,
        agent_registry=_registry(agent_profile),
        model_registry=_model_registry(saved_model),
    )
    replacement_executor = MagicMock()

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fail_after_install(
        _profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        assert operation == "restore"
        # The build's commit: settings and executor go live together, and the
        # failure lands after it.
        handle = engine.settings_handle
        handle.install_prepared(handle.prepare(staged_loaded or handle.base, session=session))
        install_loaded_agent(engine, bindings=replacement_executor)
        raise RuntimeError("post-install failure")

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fail_after_install)

    try:
        with pytest.raises(RuntimeError, match="post-install failure"):
            await engine.on_session_restore(SessionRestore(session_id="restore_me", apply_saved_model=True))
    finally:
        engine.session.guard.release()

    assert engine.current.loaded.bindings is replacement_executor
    # The commit installs the whole selection, so the stale override went
    # with it — and a post-install failure must not resurrect either field.
    assert engine.settings.model_profile == saved_model.id
    assert engine.settings.model_profile_override == ""
    assert os.environ["CHRYS_MODEL_PROFILE"] == "environment-before"


async def test_saved_model_restore_stays_applied_after_successful_start_and_later_hydrate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "environment-before")
    saved_model = ModelProfile(id="saved-model", name="Saved", model_id="gpt-saved")
    agent_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=agent_profile.name, model_profile_id=saved_model.id)
    settings = Settings(model_profile="old-model", model_profile_override="existing-override")
    engine = assemble_agent_engine(
        EventBus(),
        settings=settings,
        state_store=store,
        agent_registry=_registry(agent_profile),
        model_registry=_model_registry(saved_model),
    )

    def fail_usage_event(*, session_id: str | None = None) -> None:
        _ = session_id
        raise RuntimeError("late hydrate failure")

    stub_engine_lifecycle(monkeypatch, engine, expect_operation="restore")
    monkeypatch.setattr(engine.usage_publisher, "make_usage_event", fail_usage_event)

    try:
        with pytest.raises(RuntimeError, match="late hydrate failure"):
            await engine.on_session_restore(SessionRestore(session_id="restore_me", apply_saved_model=True))
    finally:
        engine.session.guard.release()

    # The commit installs the whole selection, so the stale override went
    # with it — and a failure after a successful start must not roll it back.
    assert engine.settings.model_profile == saved_model.id
    assert engine.settings.model_profile_override == ""
    assert os.environ["CHRYS_MODEL_PROFILE"] == "environment-before"
