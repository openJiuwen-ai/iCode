# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the TUI entry point, terminal restore, and startup-session start-up."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.main.session_handlers import RestoreRequest
from chrys.app.tui.screens.main.state import MainScreenState, RunState
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.startup import RuntimeBootstrap
from tests.support.tui_helpers import main_screen_parts


def test_tui_startup_profile_resolves_preferred_agent() -> None:
    from chrys.app.tui.app import _resolve_startup_profile
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    code = AgentProfile(name="Code")
    qa = AgentProfile(name="QA")
    registry.register(code)
    registry.register(qa)

    assert _resolve_startup_profile(registry, "QA") is qa


def test_tui_startup_profile_accepts_id_and_display_name() -> None:
    from chrys.app.tui.app import _resolve_startup_profile
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    code = AgentProfile(name="Code")
    qa = AgentProfile(name="QA", id="qa-id", display_name="Quality Agent")
    registry.register(code)
    registry.register(qa)

    assert _resolve_startup_profile(registry, "qa-id") is qa
    assert _resolve_startup_profile(registry, "quality agent") is qa


def test_tui_startup_profile_falls_back_to_code_when_preferred_missing() -> None:
    from chrys.app.tui.app import _resolve_startup_profile
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    qa = AgentProfile(name="QA")
    code = AgentProfile(name="Code")
    registry.register(qa)
    registry.register(code)

    assert _resolve_startup_profile(registry, "Missing") is code


def test_tui_startup_profile_treats_sub_agent_only_preference_as_unusable() -> None:
    from chrys.app.tui.app import _resolve_startup_profile
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    code = AgentProfile(name="Code")
    explore = AgentProfile(name="Explore", sub_agent_only=True)
    registry.register(explore)
    registry.register(code)

    assert _resolve_startup_profile(registry, "Explore") is code


def test_terminal_restore_skips_textual_web_driver(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """textual-serve captures stderr, so terminal reset escapes must stay native-only."""
    from chrys.app.tui import app as tui_app

    class _App:
        _return_code = 0

    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")

    tui_app._restore_terminal_after_run(_App())  # type: ignore[arg-type]

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("return_code", [0, 1])
def test_terminal_restore_writes_native_reset(monkeypatch: pytest.MonkeyPatch, capsys, return_code: int) -> None:
    """Native TUI runs keep the terminal reset safety net."""
    from chrys.app.tui import app as tui_app

    class _App:
        _return_code = return_code

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)

    tui_app._restore_terminal_after_run(_App())  # type: ignore[arg-type]

    reset = capsys.readouterr().err

    assert ("\x1b[?1049l" in reset) is (return_code == 0)
    assert "\x1b[?1002l" in reset
    assert "\x1b[?1004l" in reset
    assert "\x1b[>4;0m" in reset
    assert "\x1b[?9001l" in reset
    assert "\x1b[<u" in reset
    assert "\x1b[=0;1u" in reset


def test_main_restores_terminal_when_app_run_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_platform,
) -> None:
    """The terminal reset safety net must run even when Textual exits through an exception."""
    from chrys.app.tui import app as tui_app
    from chrys.app.tui.screens import logs as logs_mod
    from chrys.orchestration import startup as startup_mod

    calls: list[str] = []

    class _App:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._return_code = 1

        def run(self) -> None:
            calls.append("run")
            raise RuntimeError("boom")

    class _Registry:
        def load_all(self) -> None:
            return

    class _Engine:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.settings_handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))

    monkeypatch.setattr(sys, "argv", ["chrys"])
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform(config_dir=tmp_path))
    monkeypatch.setattr(startup_mod, "configure_utf8_stdio", lambda: None)
    monkeypatch.setattr(
        startup_mod,
        "bootstrap_runtime",
        lambda *, dotenv_override, project_root: RuntimeBootstrap(
            loaded=LoadedSettings(settings=Settings(), provenance={})
        ),
    )
    monkeypatch.setattr(logs_mod, "install_log_handler", lambda: None)
    monkeypatch.setattr(tui_app, "configure_debug_logging", lambda _path: object())
    monkeypatch.setattr(tui_app, "stop_debug_logging", lambda _runtime: calls.append("stop-debug"))
    monkeypatch.setattr(tui_app, "_restore_terminal_after_run", lambda _app: calls.append("restore-terminal"))
    monkeypatch.setattr(tui_app, "_reset_terminal_title_after_run", lambda: calls.append("reset-title"))
    monkeypatch.setattr(tui_app, "EventBus", object)
    monkeypatch.setattr(tui_app, "AgentProfileRegistry", _Registry)
    monkeypatch.setattr(tui_app, "ModelProfileRegistry", _Registry)
    monkeypatch.setattr(tui_app, "JsonFileStateStore", object)
    monkeypatch.setattr(tui_app, "assemble_agent_engine", _Engine)

    with pytest.raises(RuntimeError, match="boom"):
        tui_app.main(app_cls=_App)  # type: ignore[arg-type]

    assert calls == ["run", "restore-terminal", "reset-title", "stop-debug"]


def test_main_passes_startup_args_to_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_platform,
) -> None:
    """The TUI parser should apply high-priority startup args before app construction."""
    from chrys.app.tui import app as tui_app
    from chrys.app.tui.screens import logs as logs_mod
    from chrys.orchestration import startup as startup_mod
    from chrys.service.profiles.models.schema import ModelProfile

    calls: list[str] = []
    captured_kwargs: dict[str, object] = {}
    captured_engine_kwargs: dict[str, object] = {}
    engine_handle: list[SettingsHandle] = []
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    class _App:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self._return_code = 0
            captured_kwargs.update(kwargs)

        def run(self) -> None:
            calls.append(f"cwd:{Path.cwd()}")
            calls.append("run")

    class _AgentRegistry:
        def load_all(self) -> None:
            return

    class _ModelRegistry:
        def __init__(self) -> None:
            self.profiles = [ModelProfile(id="model-id", name="Friendly Model")]

        def load_all(self) -> None:
            return

        def get(self, profile_id: str) -> ModelProfile | None:
            for profile in self.profiles:
                if profile.id == profile_id:
                    return profile
            return None

        def list_profiles(self) -> list[ModelProfile]:
            return list(self.profiles)

    class _Engine:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured_engine_kwargs.update(kwargs)
            loaded = kwargs["loaded_settings"]
            assert isinstance(loaded, LoadedSettings)
            engine_handle.append(SettingsHandle(loaded))

        @property
        def settings_handle(self) -> SettingsHandle:
            return engine_handle[0]

    monkeypatch.setattr(
        sys,
        "argv",
        ["chrys", "-s", "session-1", "-a", "Code", "-m", "Friendly Model", "-C", str(workdir)],
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    monkeypatch.delenv("CHRYS_MODEL_PROFILE")
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform(config_dir=tmp_path))
    monkeypatch.setattr(startup_mod, "configure_utf8_stdio", lambda: None)
    bootstrap_roots: list[Path] = []

    def _fake_bootstrap(*, dotenv_override: bool, project_root: Path) -> RuntimeBootstrap:
        _ = dotenv_override
        bootstrap_roots.append(project_root)
        return RuntimeBootstrap(loaded=LoadedSettings(settings=Settings(), provenance={}))

    monkeypatch.setattr(startup_mod, "bootstrap_runtime", _fake_bootstrap)
    monkeypatch.setattr(logs_mod, "install_log_handler", lambda: None)
    monkeypatch.setattr(tui_app, "configure_debug_logging", lambda _path: object())
    monkeypatch.setattr(tui_app, "stop_debug_logging", lambda _runtime: calls.append("stop-debug"))
    monkeypatch.setattr(tui_app, "_restore_terminal_after_run", lambda _app: calls.append("restore-terminal"))
    monkeypatch.setattr(tui_app, "_reset_terminal_title_after_run", lambda: calls.append("reset-title"))
    monkeypatch.setattr(tui_app, "EventBus", object)
    monkeypatch.setattr(tui_app, "AgentProfileRegistry", _AgentRegistry)
    monkeypatch.setattr(tui_app, "ModelProfileRegistry", _ModelRegistry)
    monkeypatch.setattr(tui_app, "JsonFileStateStore", object)
    monkeypatch.setattr(tui_app, "assemble_agent_engine", _Engine)

    original_cwd = Path.cwd()
    try:
        tui_app.main(app_cls=_App)  # type: ignore[arg-type]
    finally:
        os.chdir(original_cwd)

    assert captured_kwargs["startup_session_id"] == "session-1"
    assert captured_kwargs["profile_name"] == "Code"
    assert captured_kwargs["apply_saved_model_on_restore"] is False
    # ``-C`` must chdir before bootstrap: the workdir names the project
    # trust domain the settings load reads from.
    assert bootstrap_roots == [workdir]
    loaded = captured_engine_kwargs["loaded_settings"]
    assert isinstance(loaded, LoadedSettings)
    # The app must read through the engine's own handle. Anything else — even
    # an equal LoadedSettings — is a second holder that drifts the moment the
    # user switches theme, so identity is the assertion.
    assert captured_kwargs["settings_handle"] is engine_handle[0]
    assert engine_handle[0].loaded is loaded
    settings = loaded.settings
    assert settings.model_profile == "model-id"
    assert settings.model_profile_override == ""
    assert settings.model_profile_override_sub_agents is False
    assert os.environ["CHRYS_MODEL_PROFILE"] == "model-id"
    # The successful-turn callback composes buddy persistence with the
    # session-title updater; turn-start cancellation goes to the updater.
    title_updater = captured_kwargs["session_title_updater"]
    assert isinstance(title_updater, tui_app.SessionTitleUpdater)
    buddy_calls: list[str] = []
    monkeypatch.setattr(tui_app, "on_buddy_successful_turn", lambda: buddy_calls.append("buddy"))
    monkeypatch.setattr(title_updater, "on_turn_finished", lambda: buddy_calls.append("title"))
    captured_engine_kwargs["on_successful_turn"]()
    assert buddy_calls == ["buddy", "title"]
    assert captured_engine_kwargs["on_turn_started"].__self__ is title_updater
    assert calls == [f"cwd:{workdir}", "run", "restore-terminal", "reset-title", "stop-debug"]


def _hostile_unrecognized_option_argv(_tmp_path: Path) -> tuple[list[str], str]:
    """argv carrying an unknown option whose value hides an escape and a newline."""
    hostile = "--hostile=escape\x1b\nline"
    return ["chrys", hostile], hostile


def _hostile_missing_workdir_argv(tmp_path: Path) -> tuple[list[str], str]:
    """argv whose ``--workdir`` value is missing and hides an escape and a newline."""
    hostile = f"{tmp_path / 'missing'}\x1b\npayload"
    return ["chrys", "--workdir", hostile], hostile


@pytest.mark.parametrize(
    ("build_argv", "expected_error"),
    [
        pytest.param(
            _hostile_unrecognized_option_argv,
            "error: unrecognized arguments:",
            id="unrecognized-option",
        ),
        pytest.param(
            _hostile_missing_workdir_argv,
            "error: workdir does not exist or is not a directory:",
            id="missing-workdir",
        ),
    ],
)
def test_tui_locale_parser_sanitizes_hostile_argv_while_usage_stays_english(
    build_argv: Callable[[Path], tuple[list[str], str]],
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chrys.app.tui import app as tui_app
    from chrys.orchestration import startup as startup_mod

    argv, hostile = build_argv(tmp_path)
    sanitized = hostile.replace("\x1b", "�").replace("\n", "�")
    monkeypatch.setenv("CHRYS_LOCALE", "zh-Hans")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(startup_mod, "configure_utf8_stdio", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        tui_app.main()

    stderr = capsys.readouterr().err
    error_line = stderr.rstrip("\n").splitlines()[-1]
    assert exc_info.value.code == 2
    assert "usage: chrys" in stderr
    assert expected_error in error_line
    assert sanitized in error_line
    assert "\x1b" not in stderr


async def test_unmount_drains_title_updater_after_engine_shutdown() -> None:
    """Engine shutdown can finalize a completed run whose success callback
    schedules one last title task; draining the updater afterwards is what
    guarantees that task gets cancelled and awaited."""
    calls: list[str] = []

    class _Updater:
        async def shutdown(self) -> None:
            calls.append("updater")

    class _Reporter:
        async def unsubscribe(self) -> None:
            calls.append("program-status")

    class _Engine:
        async def shutdown(self) -> None:
            calls.append("engine")

    class _Freeze:
        def close(self) -> None:
            calls.append("gc-close")

    class _Timer:
        def stop(self) -> None:
            calls.append("timer-stop")

    host = SimpleNamespace(
        _startup_task=None,
        _update_check_task=None,
        _program_status_reporter=_Reporter(),
        _session_title_updater=_Updater(),
        _engine=_Engine(),
        _gc_freeze=_Freeze(),
        _gc_freeze_watchdog=_Timer(),
    )

    await ChrysApp.on_unmount(host)

    assert calls == ["program-status", "gc-close", "timer-stop", "engine", "updater"]
    assert host._gc_freeze_watchdog is None


def test_terminal_title_reset_after_run_uses_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """After Textual exits, the host shell should not keep the last prompt preview."""
    from chrys.app.tui import app as tui_app

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.chdir(tmp_path)

    tui_app._reset_terminal_title_after_run()

    title = str(tmp_path)
    assert capsys.readouterr().err == f"\x1b]0;{title}\x07\x1b]2;{title}\x07"


def test_tui_help_does_not_offer_profile(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path, fake_platform) -> None:
    """TUI profile selection should happen inside the app, not through argv."""
    from chrys.app.tui import app as tui_app

    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform(config_dir=tmp_path))
    monkeypatch.setattr("chrys.app.tui.screens.logs.install_log_handler", lambda: None)
    monkeypatch.setattr(sys, "argv", ["chrys", "--help"])
    for key in ("PYTHONUTF8", "PYTHONIOENCODING"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)

    with pytest.raises(SystemExit) as exc_info:
        tui_app.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr()
    assert "--profile" not in out.out


def _attach_startup_facade(screen: object) -> object:
    """Add MainScreen's startup facade to a lightweight double, over the double's ``_state``."""
    state, _services, _live_diff = main_screen_parts(screen)

    def set_startup_agent_loading(value: bool) -> None:
        state.run.agent_loading = value
        screen._set_agent_loading(value)  # type: ignore[attr-defined]

    def is_startup_agent_loading() -> bool:
        return state.run.agent_loading

    async def restore_startup_session(session_id: str) -> str:
        await screen._sessions.do_session_restore(session_id, allow_while_loading=True)  # type: ignore[attr-defined]
        return "restored"

    async def dismiss_startup_load_dialog_before_restore() -> None:
        return

    def cancel_startup_session_restore() -> None:
        return

    screen.set_startup_agent_loading = set_startup_agent_loading  # type: ignore[attr-defined]
    screen.is_startup_agent_loading = is_startup_agent_loading  # type: ignore[attr-defined]
    screen.restore_startup_session = restore_startup_session  # type: ignore[attr-defined]
    screen.dismiss_startup_load_dialog_before_restore = dismiss_startup_load_dialog_before_restore  # type: ignore[attr-defined]
    screen.cancel_startup_session_restore = cancel_startup_session_restore  # type: ignore[attr-defined]
    return screen


async def test_start_engine_surfaces_early_startup_failure() -> None:
    """Failures before AgentLoadStarted should still be visible to the user."""

    class _Engine:
        async def start(self, _profile: object) -> None:
            raise RuntimeError("startup exploded")

    flashes: list[tuple[str, bool]] = []
    notifications: list[tuple[str, str, str]] = []
    loading_states: list[bool] = []

    class _StatusBar:
        def flash(self, text: MessageRef | str, *, error: bool = False, **_kwargs: object) -> None:
            rendered = text if isinstance(text, str) else format_message(text)
            flashes.append((rendered, error))

    class _Screen:
        _state = MainScreenState(run=RunState(agent_loading=True))

        def _set_agent_loading(self, value: bool) -> None:
            loading_states.append(value)

        def query_one(self, cls: type) -> _StatusBar:
            if cls.__name__ != "StatusBar":
                raise AssertionError(f"unexpected query_one({cls.__name__})")
            return _StatusBar()

        def notify(self, message: str, *, title: str, severity: str, **_kwargs: object) -> None:
            notifications.append((title, severity, message))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._startup_session_id = ""
    app._deferred_settings_warnings = []
    screen = _attach_startup_facade(_Screen())

    await app._start_engine(object(), screen)  # type: ignore[arg-type]

    assert loading_states == [False]
    assert flashes == [("Agent startup failed: startup exploded", True)]
    assert notifications == [("Agent startup failed", "error", "startup exploded")]


async def test_start_engine_restores_startup_session_after_start() -> None:
    calls: list[tuple[str, object]] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id)

    class _Sessions:
        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> None:
            calls.append(("restore", session_id, allow_while_loading))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._deferred_settings_warnings = []
    screen = _attach_startup_facade(
        SimpleNamespace(
            _sessions=_Sessions(),
            _set_agent_loading=lambda value: calls.append(("loading", value)),
        )
    )
    profile = object()

    await app._start_engine(profile, screen)  # type: ignore[arg-type]

    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "session-1"),
        ("restore", "session-1", True),
    ]
    assert app._startup_session_id == ""


async def test_start_engine_restores_canonical_startup_session_id() -> None:
    calls: list[tuple[str, object]] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id="canonical-session-id")

    class _Sessions:
        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> None:
            calls.append(("restore", session_id, allow_while_loading))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "canonicalsess"
    app._deferred_settings_warnings = []
    screen = _attach_startup_facade(
        SimpleNamespace(
            _sessions=_Sessions(),
            _set_agent_loading=lambda value: calls.append(("loading", value)),
        )
    )
    profile = object()

    await app._start_engine(profile, screen)  # type: ignore[arg-type]

    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "canonicalsess"),
        ("restore", "canonical-session-id", True),
    ]
    assert app._startup_session_id == ""


@pytest.mark.parametrize("outcome", ["failed", "declined"])
async def test_start_engine_falls_back_when_restore_emits_no_success(outcome: str) -> None:
    """A failed restore warns before the fresh start; one the user declined starts fresh quietly."""
    calls: list[tuple[str, object]] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id)

    class _Screen:
        loading = True

        def set_startup_agent_loading(self, value: bool) -> None:
            self.loading = value
            calls.append(("loading", value))

        def is_startup_agent_loading(self) -> bool:
            return self.loading

        async def dismiss_startup_load_dialog_before_restore(self) -> None:
            return

        async def restore_startup_session(self, session_id: str) -> str:
            calls.append(("restore", session_id))
            return outcome

        def cancel_startup_session_restore(self) -> None:
            calls.append(("cancel", None))

        def notify(self, message: str, *, title: str, severity: str, **_kwargs: object) -> None:
            calls.append(("warning", message, title, severity))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._deferred_settings_warnings = []
    profile = object()

    await app._start_engine(profile, _Screen())  # type: ignore[arg-type]

    warning: list[tuple[str, object]] = [
        (
            "warning",
            f"Could not restore session {session_short_id('session-1')}; started a new session instead.",
            "Session",
            "warning",
        )
    ]
    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "session-1"),
        ("restore", "session-1"),
        ("cancel", None),
        *(warning if outcome == "failed" else []),
        ("reset_restore", None),
        ("start", profile),
    ]


async def test_main_screen_startup_restore_requires_matching_success_event() -> None:
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.screens.main.state import MainScreenServices
    from chrys.foundation.events.types import SessionRestored

    bus = EventBus()

    class _Sessions:
        def __init__(self, *, publish_success: bool, request: RestoreRequest = RestoreRequest.REQUESTED) -> None:
            self.publish_success = publish_success
            self.request = request

        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> RestoreRequest:
            assert allow_while_loading is True
            if self.publish_success:
                await bus.publish(SessionRestored(session_id=session_id))
            return self.request

    screen = object.__new__(MainScreen)
    screen._services = MainScreenServices(bus=bus)
    screen._sessions = _Sessions(publish_success=False)
    assert await MainScreen.restore_startup_session(screen, "session-1") == "failed"

    screen._sessions = _Sessions(publish_success=False, request=RestoreRequest.SKIPPED)
    assert await MainScreen.restore_startup_session(screen, "session-1") == "failed"

    screen._sessions = _Sessions(publish_success=False, request=RestoreRequest.DECLINED)
    assert await MainScreen.restore_startup_session(screen, "session-1") == "declined"

    screen._sessions = _Sessions(publish_success=True)
    assert await MainScreen.restore_startup_session(screen, "session-1") == "restored"


def test_main_screen_cancel_startup_restore_clears_restoring_state() -> None:
    from chrys.app.tui.screens.main.screen import MainScreen

    calls: list[tuple[str, object]] = []
    screen = object.__new__(MainScreen)
    screen._events = SimpleNamespace(cancel_agent_load=lambda: calls.append(("cancel", None)))
    screen._set_restoring_session = lambda value: calls.append(("restoring", value))

    MainScreen.cancel_startup_session_restore(screen)

    assert calls == [("cancel", None), ("restoring", False)]


async def test_start_engine_dismisses_startup_modal_before_startup_session_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui import app as tui_app
    from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog

    calls: list[tuple[str, object]] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(("sleep", delay))

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id)

    class _Sessions:
        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> None:
            calls.append(("restore", session_id, allow_while_loading))

    monkeypatch.setattr(tui_app.asyncio, "sleep", fake_sleep)
    startup_dialog = AgentLoadDialog()
    monkeypatch.setattr(startup_dialog, "dismiss", lambda result=None: calls.append(("dismiss", result)))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._deferred_settings_warnings = []
    screen = _attach_startup_facade(
        SimpleNamespace(
            _events=SimpleNamespace(_agent_load_dialog=None),
            app=SimpleNamespace(screen_stack=[startup_dialog]),
            _sessions=_Sessions(),
            _set_agent_loading=lambda value: calls.append(("loading", value)),
        )
    )

    async def dismiss_startup_load_dialog_before_restore() -> None:
        startup_dialog.dismiss(None)
        await tui_app.asyncio.sleep(0)

    screen.dismiss_startup_load_dialog_before_restore = dismiss_startup_load_dialog_before_restore  # type: ignore[attr-defined]
    profile = object()

    await app._start_engine(profile, screen)  # type: ignore[arg-type]

    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "session-1"),
        ("dismiss", None),
        ("sleep", 0),
        ("restore", "session-1", True),
    ]


async def test_start_engine_cancels_active_startup_modal_before_startup_session_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui import app as tui_app

    calls: list[tuple[str, object]] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(("sleep", delay))

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id)

    class _Sessions:
        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> None:
            calls.append(("restore", session_id, allow_while_loading))

    monkeypatch.setattr(tui_app.asyncio, "sleep", fake_sleep)

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._deferred_settings_warnings = []
    screen = _attach_startup_facade(
        SimpleNamespace(
            _events=SimpleNamespace(
                _agent_load_dialog=object(),
                cancel_agent_load=lambda: calls.append(("cancel", None)),
            ),
            app=SimpleNamespace(screen_stack=[]),
            _sessions=_Sessions(),
            _set_agent_loading=lambda value: calls.append(("loading", value)),
        )
    )

    async def dismiss_startup_load_dialog_before_restore() -> None:
        screen._events.cancel_agent_load()
        await tui_app.asyncio.sleep(0)

    screen.dismiss_startup_load_dialog_before_restore = dismiss_startup_load_dialog_before_restore  # type: ignore[attr-defined]
    profile = object()

    await app._start_engine(profile, screen)  # type: ignore[arg-type]

    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "session-1"),
        ("cancel", None),
        ("sleep", 0),
        ("restore", "session-1", True),
    ]


async def test_start_engine_missing_startup_session_warns_and_continues() -> None:
    calls: list[tuple[str, object]] = []
    flashes: list[tuple[str, bool]] = []
    notifications: list[tuple[str, str, str]] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object | None:
            calls.append(("meta", session_id))
            return None

    class _StatusBar:
        def flash(self, message: MessageRef | str, *, error: bool = False) -> None:
            flashes.append((format_message(message) if isinstance(message, MessageRef) else message, error))

    class _Screen:
        def _set_agent_loading(self, value: bool) -> None:
            calls.append(("loading", value))

        def query_one(self, cls: type) -> _StatusBar:
            if cls.__name__ != "StatusBar":
                raise AssertionError(cls)
            return _StatusBar()

        def notify(self, message: str, *, title: str, severity: str, **_kwargs: object) -> None:
            notifications.append((title, severity, message))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._state_store = _Store()
    app._startup_session_id = "missing-session"
    app._deferred_settings_warnings = []
    profile = object()

    await app._start_engine(profile, _attach_startup_facade(_Screen()))  # type: ignore[arg-type]

    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "missing-session"),
        ("loading", False),
        ("reset_restore", None),
        ("start", profile),
    ]
    warning = f"Session {session_short_id('missing-session')} was not found."
    assert flashes == [(warning, True)]
    assert notifications == [("Session", "warning", warning)]
    assert app._startup_session_id == ""


async def test_start_engine_startup_session_restore_failure_warns_and_continues() -> None:
    calls: list[tuple[str, object]] = []
    flashes: list[tuple[str, bool]] = []
    notifications: list[tuple[str, str, str]] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id)

    class _Sessions:
        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> None:
            calls.append(("restore", session_id, allow_while_loading))
            raise RuntimeError("restore exploded")

    class _StatusBar:
        def flash(self, message: MessageRef | str, *, error: bool = False) -> None:
            flashes.append((format_message(message) if isinstance(message, MessageRef) else message, error))

    class _Screen:
        _sessions = _Sessions()

        def _set_agent_loading(self, value: bool) -> None:
            calls.append(("loading", value))

        def query_one(self, cls: type) -> _StatusBar:
            if cls.__name__ != "StatusBar":
                raise AssertionError(cls)
            return _StatusBar()

        def notify(self, message: str, *, title: str, severity: str, **_kwargs: object) -> None:
            notifications.append((title, severity, message))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._deferred_settings_warnings = []
    profile = object()

    await app._start_engine(profile, _attach_startup_facade(_Screen()))  # type: ignore[arg-type]

    assert calls == [
        ("prepare", profile),
        ("loading", True),
        ("meta", "session-1"),
        ("restore", "session-1", True),
        ("loading", False),
        ("reset_restore", None),
        ("start", profile),
    ]
    warning = f"Could not restore session {session_short_id('session-1')}: restore exploded"
    assert flashes == [(warning, True)]
    assert notifications == [("Session", "warning", warning)]
    assert app._startup_session_id == ""


async def test_start_engine_drops_deferred_settings_warnings_after_a_successful_restore() -> None:
    """The restore republishes its own settings load's warnings; the bootstrap ones are stale."""
    calls: list[str] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append("prepare")

        async def start(self, profile: object) -> None:
            calls.append("start")

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            return SimpleNamespace(session_id=session_id)

    class _Sessions:
        async def do_session_restore(self, session_id: str, *, allow_while_loading: bool = False) -> None:
            calls.append("restore")

    bus = EventBus()
    published: list[Warning] = []

    async def _record(event: Warning) -> None:
        published.append(event)

    await bus.subscribe(Warning, _record)

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._bus = bus
    app._deferred_settings_warnings = [Warning(code="settings_layer_warning", message="From the launch cwd.")]
    screen = _attach_startup_facade(
        SimpleNamespace(
            _sessions=_Sessions(),
            _set_agent_loading=lambda value: None,
        )
    )

    await app._start_engine(object(), screen)  # type: ignore[arg-type]

    assert calls == ["prepare", "restore"]
    assert published == []
    assert app._deferred_settings_warnings == []


async def test_start_engine_flushes_deferred_settings_warnings_when_falling_back() -> None:
    """A failed restore leaves the bootstrap settings in force, so their warnings are due."""
    calls: list[object] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append("prepare")

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append("reset_restore")

        async def start(self, profile: object) -> None:
            calls.append("start")

    class _Store:
        async def load_session_meta(self, session_id: str) -> object:
            return SimpleNamespace(session_id=session_id)

    class _StatusBar:
        def flash(self, message: MessageRef | str, *, error: bool = False) -> None:
            return

    class _Screen:
        loading = True

        def set_startup_agent_loading(self, value: bool) -> None:
            self.loading = value

        def is_startup_agent_loading(self) -> bool:
            return self.loading

        async def dismiss_startup_load_dialog_before_restore(self) -> None:
            return

        async def restore_startup_session(self, session_id: str) -> str:
            return "failed"

        def cancel_startup_session_restore(self) -> None:
            return

        def query_one(self, cls: type) -> _StatusBar:
            return _StatusBar()

        def notify(self, message: str, *, title: str, severity: str, **_kwargs: object) -> None:
            return

    bus = EventBus()

    async def _record(event: Warning) -> None:
        calls.append(("flushed", event.code))

    await bus.subscribe(Warning, _record)

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._bus = bus
    app._deferred_settings_warnings = [Warning(code="settings_layer_warning", message="From the launch cwd.")]

    await app._start_engine(object(), _Screen())  # type: ignore[arg-type]

    assert calls == ["prepare", "reset_restore", ("flushed", "settings_layer_warning"), "start"]
    assert app._deferred_settings_warnings == []
