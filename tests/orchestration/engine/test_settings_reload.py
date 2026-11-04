# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings reload through the engine: staged loads, env re-reads, restart-scoped keys, and failure reporting."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.config.process_settings import install_process_settings, process_settings
from chrys.foundation.config.runtime_pointer import set_model_pointer
from chrys.foundation.config.settings import (
    DEFAULT_ROLLBACK_SNAPSHOTS_KEEP,
    Settings,
)
from chrys.foundation.config.settings_store import LoadedSettings, load_settings
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    SettingsReload,
    SettingsReloaded,
    Warning,
)
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.state import controls as engine_controls
from chrys.service.profiles.agents.schema import (
    AgentProfile,
)
from chrys.service.profiles.models.resolver import loaded_with_active_model_profile
from chrys.service.profiles.models.schema import ModelProfile
from tests.orchestration.engine._recovery_helpers import (
    _profile,
    _registry,
    stub_engine_start,
)
from tests.support.event_capture import capture_events, collect_events
from tests.support.loaded_agents import install_loaded_agent


async def test_settings_reload_with_no_executor_uses_refreshed_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _profile("Code", "Old Code")
    refreshed = _profile("Code", "Fresh Code")
    replacement_settings = Settings(default_approval_mode="auto")
    engine = assemble_agent_engine(EventBus(), settings=Settings(), agent_registry=_registry(refreshed))
    engine.session.agent_profile = stale
    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=replacement_settings, provenance={}),
    )
    start_calls = stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings is replacement_settings
    assert start_calls == [(refreshed, "settings_reload")]


async def test_settings_reload_disables_baseline_but_preserves_safety_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    profile = _profile()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    engine = assemble_agent_engine(EventBus(), settings=Settings(workspace_change_notice=True))
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace.from_cwd(str(workspace_root))
    tracker = engine_services(engine).workspace_change_tracker
    tracker.retarget_roots(engine.session.workspace)
    tracker.capture_baseline(1)
    tracker.queue_safety_notice("retained files")

    # The reload's real load reads the engine conftest's
    # ``CHRYS_WORKSPACE_CHANGE_NOTICE=0``, which is exactly the disabling
    # re-read this test needs.
    stub_engine_start(monkeypatch, engine, expect_operation="settings_reload")

    await engine._on_settings_reload(SettingsReload())

    assert tracker.baseline is None
    assert tracker.take_pending_notice() == "retained files"
    assert tracker.take_pending_notice() is None


async def test_failed_settings_reload_keeps_workspace_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    profile = _profile()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    engine = assemble_agent_engine(EventBus(), settings=Settings(workspace_change_notice=True))
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace.from_cwd(str(workspace_root))
    tracker = engine_services(engine).workspace_change_tracker
    tracker.retarget_roots(engine.session.workspace)
    baseline = tracker.capture_baseline(1)

    async def failing_start(
        _profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = operation
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(engine.lifecycle, "start", failing_start)

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await engine._on_settings_reload(SettingsReload())

    # The reload rolled back to the enabled settings; the live baseline must survive.
    assert engine.settings.workspace_change_notice is True
    assert tracker.baseline == baseline


async def test_settings_reload_with_missing_registry_entry_reuses_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _profile("Code", "Code Agent")
    replacement_settings = Settings(default_approval_mode="auto")
    engine = assemble_agent_engine(EventBus(), settings=Settings(), agent_registry=_registry())
    engine.session.agent_profile = active
    install_loaded_agent(engine, bindings=object())  # type: ignore[assignment]
    calls: list[tuple[AgentProfile, str]] = []

    async def fake_soft_restart(profile: AgentProfile, **kwargs: Any) -> None:
        calls.append((profile, kwargs["operation"]))
        if kwargs.get("staged_loaded") is not None:
            engine.settings_handle.install(kwargs["staged_loaded"])

    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=replacement_settings, provenance={}),
    )
    monkeypatch.setattr(engine.lifecycle, "reload", fake_soft_restart)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings is replacement_settings
    assert calls == [(active, "settings_reload")]


async def test_settings_reload_derives_the_project_root_from_the_session_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The project trust domain is root-derived, and the pinned model plus the
    launch mode's retry policy travel *into* the load, not after it."""
    profile = _profile()
    root = tmp_path / "workspace"
    root.mkdir()
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(model_profile="pinned-model", frontend_default_max_transient_retries=15),
    )
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace.from_cwd(str(root))
    engine.pin_model_profile()
    load_kwargs: dict[str, Any] = {}

    def fake_load(**kwargs: Any) -> LoadedSettings:
        load_kwargs.update(kwargs)
        return LoadedSettings(settings=Settings(), provenance={})

    monkeypatch.setattr("chrys.orchestration.engine.state.controls.load_settings", fake_load)
    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert load_kwargs["project_root"] == Path(engine.session.workspace.primary_cwd)
    assert load_kwargs["eval_context"].frontend_default_max_transient_retries == 15
    assert load_kwargs["model_profile"] == "pinned-model"


async def test_settings_reload_follows_env_without_per_session_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TUI model switch path: writes CHRYS_MODEL_PROFILE to env, then reloads.
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "env-model")
    engine = assemble_agent_engine(EventBus(), settings=Settings(model_profile="old-model"))
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.model_profile == "env-model"


async def test_settings_reload_preserves_frontend_retry_default_and_rereads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", "7")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(
            max_transient_retries=None,
            frontend_default_max_transient_retries=10,
        ),
    )
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.frontend_default_max_transient_retries == 10
    assert engine.settings.max_transient_retries == 7
    assert engine.settings.effective_max_transient_retries() == 7


async def test_settings_reload_reports_a_value_it_had_to_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload is when a user finds out their edit did not take.

    The reload used to truncate ``LoadedSettings`` to ``.settings``, so the
    same bad value went silent from the second read onwards — exactly when the
    user is looking for feedback.
    """
    monkeypatch.setenv("CHRYS_SESSION_TITLE_AUTO", "nonsense")
    bus = EventBus()
    warnings = await capture_events(bus, Warning)
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert [warning.code for warning in warnings] == ["setting_rejected"]
    assert "CHRYS_SESSION_TITLE_AUTO" in warnings[0].message
    assert warnings[0].session_id == "sid"


async def test_settings_reload_does_not_claim_a_restart_value_took_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reload re-reads every key; the process re-reads none of these six.

    Raw HTTP capture is decided once, at bootstrap, and its consumers hold that
    answer for the process. A reload that wrote the new value into the live
    settings would have the engine — and the panel reading it — report capture
    as on while nothing was capturing.
    """
    monkeypatch.delenv("CHRYS_DEBUG_LLM_RAW_HTTP_LOG", raising=False)
    install_process_settings(load_settings())
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_DEBUG_LLM_RAW_HTTP_LOG", "1")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.raw_http_capture is False
    assert process_settings().raw_http_capture is False


async def test_settings_reload_still_applies_a_reload_scoped_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the freeze: it must not stop a reload from reloading."""
    monkeypatch.delenv("CHRYS_SESSION_TITLE_AUTO", raising=False)
    install_process_settings(load_settings())
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_SESSION_TITLE_AUTO", "0")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.session_title_auto is False


async def test_settings_reload_holds_a_routed_restart_field_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otel has no snapshot slot; the routing itself holds it — and says so.

    Its readers decided at bootstrap whether telemetry exists, so a reload
    writing the new value into the live settings would only change what the
    process *reports*, not what it does. The user still deserves to hear that
    the edit was saved and what it is waiting on.
    """
    monkeypatch.delenv("CHRYS_OTEL", raising=False)
    install_process_settings(load_settings())
    bus = EventBus()
    warnings = await capture_events(bus, Warning)
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_OTEL", "1")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.otel_enabled is False
    assert [warning.code for warning in warnings] == ["settings_restart_required"]
    assert "otel.enabled" in warnings[0].message
    assert warnings[0].session_id == "sid"


async def test_settings_reload_applies_a_dev_mode_change_without_restart_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dev_mode's one consumer reads it during the rebuild a reload performs.

    So the reload genuinely applies it — and must not warn that a restart is
    needed for a value that just took effect.
    """
    monkeypatch.delenv("CHRYS_DEV_MODE", raising=False)
    install_process_settings(load_settings())
    bus = EventBus()
    warnings = await capture_events(bus, Warning)
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_DEV_MODE", "1")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.dev_mode is True
    assert warnings == []


async def test_settings_reload_without_an_agent_reloads_instead_of_echoing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with nothing built must reload for real, not report one it skipped.

    Startup before the first build, a first build that failed, and a host that
    only subscribed all reach this path. Echoing completion left the next build
    reading the configuration the user had just changed.
    """
    monkeypatch.delenv("CHRYS_SESSION_TITLE_AUTO", raising=False)
    install_process_settings(load_settings())
    bus = EventBus()
    reloaded: list[SettingsReloaded] = []
    await bus.subscribe(SettingsReloaded, lambda event: collect_events(reloaded, event))
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = None
    install_loaded_agent(engine, loaded=None)
    start_calls = stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_SESSION_TITLE_AUTO", "0")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.session_title_auto is False
    # Only the rebuild is skipped — there is no runtime to replace.
    assert start_calls == []
    assert len(reloaded) == 1


async def test_settings_reload_loads_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load reads config files and waits on their lock.

    This handler runs inline on the bus, so loading on the event-loop thread
    would stall every other event for as long as the disk takes.
    """
    load_threads: list[int] = []
    real_load = engine_controls.load_settings

    def recording_load(**kwargs: Any) -> LoadedSettings:
        load_threads.append(threading.get_ident())
        return real_load(**kwargs)

    monkeypatch.setattr(engine_controls, "load_settings", recording_load)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert load_threads and threading.get_ident() not in load_threads


async def test_settings_reload_preserves_per_session_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ACP set_session_model path: in-memory override must survive reload, not
    # revert to the global env default.
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "env-model")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(
            model_profile="session-model",
            model_profile_override="session-model",
            model_profile_override_sub_agents=True,
        ),
    )
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)
    engine.session.model_profile_pinned = True

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.model_profile == "session-model"
    assert engine.settings.model_profile_override == "session-model"
    assert engine.settings.model_profile_override_sub_agents is True


async def test_settings_reload_returns_the_model_label_to_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--model`` parks its value in the environment so it survives reload.

    The reload reads it back from there, so without re-attribution the panel
    would claim the user configured ``CHRYS_MODEL_PROFILE`` — on the first
    reload and every one after.
    """
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "cli-model")
    cli_model = ModelProfile(id="cli-model", name="Cli", model_id="gpt-cli")
    loaded = loaded_with_active_model_profile(load_settings(), cli_model, Source.CLI)
    engine = assemble_agent_engine(EventBus(), loaded_settings=loaded)
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    for _ in range(2):
        await engine._on_settings_reload(SettingsReload())

        assert engine.settings.model_profile == "cli-model"
        assert engine.loaded_settings.source_for("model.profile.active").layer is Source.CLI


async def test_settings_reload_does_not_relabel_a_model_the_user_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-attribution only, never re-imposition.

    The model config screen replaces the parked environment value on purpose;
    the reload must let the new value win and credit the environment, not stamp
    the command line's label (or worse, its stale value) back on top.
    """
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "cli-model")
    cli_model = ModelProfile(id="cli-model", name="Cli", model_id="gpt-cli")
    loaded = loaded_with_active_model_profile(load_settings(), cli_model, Source.CLI)
    engine = assemble_agent_engine(EventBus(), loaded_settings=loaded)
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "screen-model")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.model_profile == "screen-model"
    assert engine.loaded_settings.source_for("model.profile.active").layer is Source.ENV


async def test_settings_reload_does_not_relabel_a_runtime_pick_of_the_same_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing the profile the flag already named is still the user choosing it.

    Nothing about the value distinguishes the two, so it is the carrier that
    decides: this one arrives registered as ``PROCESS_RUNTIME`` rather than
    read back out of the parked variable. Relabelling it would credit the flag
    for a live choice — for the rest of the session, since this reload's output
    is the next reload's ``previous``.
    """
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "cli-model")
    cli_model = ModelProfile(id="cli-model", name="Cli", model_id="gpt-cli")
    loaded = loaded_with_active_model_profile(load_settings(), cli_model, Source.CLI)
    engine = assemble_agent_engine(EventBus(), loaded_settings=loaded)
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    set_model_pointer("cli-model", origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))

    for _ in range(2):
        await engine._on_settings_reload(SettingsReload())

        assert engine.settings.model_profile == "cli-model"
        assert engine.loaded_settings.source_for("model.profile.active").layer is Source.PROCESS_RUNTIME


async def test_settings_reload_preserves_pinned_ask_user_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ACP injects ask_user_timeout_seconds via Settings (not env) and pins it;
    # reload must keep it (None = client owns timing) instead of reverting to env.
    monkeypatch.setenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", "600")
    engine = assemble_agent_engine(EventBus(), settings=Settings(ask_user_timeout_seconds=None))
    engine.pin_ask_user_timeout()
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.ask_user_timeout_seconds is None


async def test_settings_reload_unpinned_ask_user_timeout_follows_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TUI/CLI never pin the timeout: a changed CHRYS_ASK_USER_TIMEOUT_SECONDS must
    # take effect on reload instead of being frozen at the live in-memory value.
    monkeypatch.setenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", "42")
    engine = assemble_agent_engine(EventBus(), settings=Settings(ask_user_timeout_seconds=999))
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.ask_user_timeout_seconds == 42


@pytest.mark.parametrize("has_agent", [True, False], ids=["with_agent", "without_agent"])
async def test_settings_reload_publishes_error_when_the_load_fails(
    has_agent: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A load that raises aborts before any rebuild publishes a completion event.
    # The handler must emit a failure event so a caller awaiting the reload
    # resolves instead of hanging, and must restore the previous live settings.
    # A host with nothing built (startup before the first build, a failed first
    # build) takes the same path and must not report a success it skipped.
    bus = EventBus()
    errors: list[Error] = []
    reloaded: list[SettingsReloaded] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    await bus.subscribe(SettingsReloaded, lambda event: collect_events(reloaded, event))
    original = Settings(ask_user_timeout_seconds=123)
    engine = assemble_agent_engine(bus, settings=original)
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile() if has_agent else None
    install_loaded_agent(engine, loaded=None)

    def explode(**_kwargs: object) -> object:
        raise ValueError("settings store unavailable")

    monkeypatch.setattr("chrys.orchestration.engine.state.controls.load_settings", explode)

    with pytest.raises(ValueError):
        await engine._on_settings_reload(SettingsReload())

    assert engine.settings is original
    assert [e.code for e in errors] == ["settings_reload_failed"]
    assert errors[0].message == "settings store unavailable"
    assert errors[0].display_message is None
    assert errors[0].session_id == "sid"
    assert reloaded == []


async def test_settings_reload_survives_one_invalid_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed variable is rejected on its own; the reload still completes.

    ``CHRYS_ROLLBACK_SNAPSHOTS_KEEP=abc`` used to raise out of the loader and
    take the whole reload with it.
    """
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings(ask_user_timeout_seconds=123))
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, loaded=None)

    stub_engine_start(monkeypatch, engine)
    monkeypatch.setenv("CHRYS_ROLLBACK_SNAPSHOTS_KEEP", "abc")

    await engine._on_settings_reload(SettingsReload())

    assert engine.settings.rollback_snapshots_keep == DEFAULT_ROLLBACK_SNAPSHOTS_KEEP
