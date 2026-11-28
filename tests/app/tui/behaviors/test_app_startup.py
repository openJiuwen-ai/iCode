# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the TUI entry point, terminal restore, and startup-session start-up."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from textual.widgets import Static

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.graphics.probe import TerminalReport
from chrys.app.tui.screens.main.session_handlers import RestoreRequest
from chrys.app.tui.screens.main.state import MainScreenState, RunState
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message

# Bind the runtime's platform getter before the help test patches the platform module.
from chrys.foundation.models import session_env as session_env
from chrys.orchestration.startup import RuntimeBootstrap
from tests.support.platform_fakes import platform_with_config_dir
from tests.support.symlinks import junction_or_skip, symlink_or_skip
from tests.support.toasts import shown_toasts
from tests.support.tui_app_harness import await_engine_startup
from tests.support.tui_helpers import main_screen_parts
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry
from tests.support.workspace_registry import move_profile_to_legacy_path, retarget_workdir_alias, save_chat_session

TARGET_SESSION = "0f1e2d3c-4b5a-4968-8776-655443322110"
MISSING_SESSION = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


@pytest.mark.parametrize("show_startup_loading", [False, True])
async def test_main_screen_paints_before_engine_construction(tmp_path: Path, show_startup_loading: bool) -> None:
    from chrys.app.tui import app as tui_module
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.state.store import JsonFileStateStore
    from tests.support.tui_app_harness import ShutdownOnlyEngine
    from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, MAIN_SCREEN_BUILD_TIMEOUT, wait_for

    started = asyncio.Event()
    frames: list[object] = []

    class PaintProbe(ChrysApp):
        CSS_PATH: ClassVar = [Path(tui_module.__file__).parent / path for path in ChrysApp.CSS_PATH]

        def _display(self, screen, renderable) -> None:
            super()._display(screen, renderable)
            frames.append(screen)

        def _build_main_screen(self):
            if show_startup_loading:
                assert frames
                assert frames[0].id == "_default"
            return super()._build_main_screen()

        async def _start_engine(self, profile, screen) -> None:
            assert screen in frames
            started.set()

    registry = AgentProfileRegistry()
    registry.register(AgentProfile(name="Code"))
    app = PaintProbe(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=registry,
        gc_freeze_enabled=False,
        show_startup_loading=show_startup_loading,
    )
    async with app.run_test():
        task = app._startup_task
        assert task is not None
        await wait_for(
            lambda: started.is_set() or task.done(),
            description="agent construction after the main screen paints",
            # The loading shell builds the main screen inside this wait; without it, that build ran before the app
            # was ready.
            timeout=MAIN_SCREEN_BUILD_TIMEOUT if show_startup_loading else DEFAULT_WAIT_TIMEOUT,
        )
        if task.done():
            await task
        assert started.is_set()
        if show_startup_loading:
            indicator = app._startup_loading_indicator
            assert indicator is not None
            assert not indicator._animating
            timer = indicator._auto_refresh_timer
            assert timer is not None
            assert not timer._active.is_set()


async def test_cancelled_startup_ignores_late_refresh_callback() -> None:
    from tests.support.waiting import wait_for

    callback_ready: asyncio.Future[Callable[[], None]] = asyncio.get_running_loop().create_future()
    starts: list[object] = []

    class RefreshGate:
        is_running = True
        _closing = False
        _exit = False
        _wait_for_startup_refresh = ChrysApp._wait_for_startup_refresh

        def call_after_refresh(self, callback: Callable[[], None]) -> bool:
            callback_ready.set_result(callback)
            return True

        async def _start_engine(self, profile: object, screen: object) -> None:
            starts.append(profile)

    task = asyncio.create_task(
        ChrysApp._start_engine_after_refresh(RefreshGate(), object(), object())  # type: ignore[arg-type]
    )
    try:
        await wait_for(lambda: callback_ready.done() or task.done(), description="startup waiting for paint")
        if task.done():
            await task
        assert not task.done()
        callback = callback_ready.result()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        callback()
        assert starts == []
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_tui_startup_profile_resolves_preferred_agent() -> None:
    from chrys.service.profiles.agents.registry import AgentProfileRegistry, resolve_launch_profile
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    code = AgentProfile(name="Code")
    qa = AgentProfile(name="QA")
    registry.register(code)
    registry.register(qa)

    assert resolve_launch_profile(registry, "QA") is qa


def test_tui_startup_profile_accepts_id_and_display_name() -> None:
    from chrys.service.profiles.agents.registry import AgentProfileRegistry, resolve_launch_profile
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    code = AgentProfile(name="Code")
    qa = AgentProfile(name="QA", id="qa-id", display_name="Quality Agent")
    registry.register(code)
    registry.register(qa)

    assert resolve_launch_profile(registry, "qa-id") is qa
    assert resolve_launch_profile(registry, "quality agent") is qa


def test_tui_startup_profile_falls_back_to_code_when_preferred_missing() -> None:
    from chrys.service.profiles.agents.registry import AgentProfileRegistry, resolve_launch_profile
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    qa = AgentProfile(name="QA")
    code = AgentProfile(name="Code")
    registry.register(qa)
    registry.register(code)

    assert resolve_launch_profile(registry, "Missing") is code


def test_tui_startup_profile_treats_sub_agent_only_preference_as_unusable() -> None:
    from chrys.service.profiles.agents.registry import AgentProfileRegistry, resolve_launch_profile
    from chrys.service.profiles.agents.schema import AgentProfile

    registry = AgentProfileRegistry()
    code = AgentProfile(name="Code")
    explore = AgentProfile(name="Explore", sub_agent_only=True)
    registry.register(explore)
    registry.register(code)

    assert resolve_launch_profile(registry, "Explore") is code


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
        lambda *, workdir: RuntimeBootstrap(loaded=LoadedSettings(settings=Settings(), provenance={})),
    )
    monkeypatch.setattr(logs_mod, "install_log_handler", lambda: None)
    monkeypatch.setattr(tui_app, "configure_debug_logging", lambda _path: object())
    monkeypatch.setattr(tui_app, "stop_debug_logging", lambda _runtime: calls.append("stop-debug"))
    monkeypatch.setattr(tui_app, "_restore_terminal_after_run", lambda _app: calls.append("restore-terminal"))
    monkeypatch.setattr(tui_app, "_reset_terminal_title_after_run", lambda: calls.append("reset-title"))
    monkeypatch.setattr(tui_app, "ask_terminal", lambda _env, *, enabled: None)
    monkeypatch.setattr(tui_app, "EventBus", object)
    monkeypatch.setattr(tui_app, "AgentProfileRegistry", _Registry)
    monkeypatch.setattr(tui_app, "ModelProfileRegistry", _Registry)
    monkeypatch.setattr("chrys.service.state.store.JsonFileStateStore", object)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.assemble_agent_engine", _Engine)

    with pytest.raises(RuntimeError, match="boom"):
        tui_app.main(app_cls=_App)  # type: ignore[arg-type]

    assert calls == ["run", "restore-terminal", "reset-title", "stop-debug"]


@pytest.mark.parametrize("entry", ["workdir", "alias", "junction"])
def test_main_passes_startup_args_to_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_platform,
    entry,
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
    alias = tmp_path / "alias"
    if entry == "alias":
        symlink_or_skip(alias, workdir, target_is_directory=True)
        getcwd = os.getcwd

        def getcwd_with_alias():
            cwd = getcwd()
            return str(alias) if Path(cwd).resolve() == workdir.resolve() else cwd

        monkeypatch.setattr(os, "getcwd", getcwd_with_alias)
    elif entry == "junction":
        junction_or_skip(alias, workdir)

    class _App:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.return_code = 0
            captured_kwargs.update(kwargs)

        def run(self) -> None:
            calls.append(f"cwd:{Path.cwd()}")
            calls.append("run")

    class _AgentRegistry:
        def load_all(self) -> None:
            assert entry == "workdir"

        def load_all_readonly(self) -> None:
            assert entry != "workdir"

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
        [
            "chrys",
            "-s",
            "abc123abc123",
            "-a",
            "Code",
            "-m",
            "Friendly Model",
            *(["-C", str(workdir)] if entry == "workdir" else ["--workspace", "Team"]),
        ],
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    monkeypatch.delenv("CHRYS_MODEL_PROFILE")
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform(config_dir=tmp_path))
    if entry != "workdir":
        from chrys.service.workspaces.registry import WorkspaceRegistry

        workspace = WorkspaceRegistry().create("Team", str(alias))
    monkeypatch.setattr(startup_mod, "configure_utf8_stdio", lambda: None)
    bootstrap_roots: list[Path] = []

    def _fake_bootstrap(*, workdir: Path) -> RuntimeBootstrap:
        bootstrap_roots.append(workdir)
        return RuntimeBootstrap(loaded=LoadedSettings(settings=Settings(), provenance={}))

    monkeypatch.setattr(startup_mod, "bootstrap_runtime", _fake_bootstrap)
    monkeypatch.setattr(logs_mod, "install_log_handler", lambda: None)
    monkeypatch.setattr(tui_app, "configure_debug_logging", lambda _path: object())
    monkeypatch.setattr(tui_app, "stop_debug_logging", lambda _runtime: calls.append("stop-debug"))
    monkeypatch.setattr(tui_app, "_restore_terminal_after_run", lambda _app: calls.append("restore-terminal"))
    monkeypatch.setattr(tui_app, "_reset_terminal_title_after_run", lambda: calls.append("reset-title"))
    terminal_report = TerminalReport("ghostty 1.4.0")
    asked: list[tuple[bool, bool]] = []

    def _ask_terminal(env: Mapping[str, str], *, enabled: bool) -> TerminalReport:
        asked.append((env is os.environ, enabled))
        return terminal_report

    monkeypatch.setattr(tui_app, "ask_terminal", _ask_terminal)
    monkeypatch.setattr(tui_app, "EventBus", object)
    monkeypatch.setattr(tui_app, "AgentProfileRegistry", _AgentRegistry)
    monkeypatch.setattr(tui_app, "ModelProfileRegistry", _ModelRegistry)
    monkeypatch.setattr("chrys.service.state.store.JsonFileStateStore", object)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.assemble_agent_engine", _Engine)

    original_cwd = Path.cwd()
    try:
        tui_app.main(app_cls=_App)  # type: ignore[arg-type]
    finally:
        os.chdir(original_cwd)

    # The terminal named by the process environment is asked, by the loaded setting, and the answer goes
    # to the app.
    assert asked == [(True, Settings().chat_image_graphics)]
    assert captured_kwargs["terminal_report"] is terminal_report
    assert captured_kwargs["startup_session_id"] == "abc123abc123"
    assert captured_kwargs["profile_name"] == "Code"
    assert captured_kwargs["apply_saved_model_on_restore"] is False
    # ``-C`` must chdir before bootstrap: the workdir names the workdir
    # trust domain the settings load reads from.
    assert len(bootstrap_roots) == 1 and bootstrap_roots[0].samefile(workdir)
    if entry != "workdir":
        assert captured_kwargs["startup_workspace_id"] == workspace.id
        snapshot = captured_kwargs["startup_workspace_snapshot"]
        assert snapshot.workspace(workspace.id) == workspace
        assert Path(captured_kwargs["startup_workspace_cwd"]) == bootstrap_roots[0]
        assert workspace.primary_cwd == str(alias)
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
    from chrys.app.features.session_title import SessionTitleUpdater

    assert isinstance(title_updater, SessionTitleUpdater)
    buddy_calls: list[str] = []
    monkeypatch.setattr(tui_app, "on_buddy_successful_turn", lambda: buddy_calls.append("buddy"))
    monkeypatch.setattr(title_updater, "on_turn_finished", lambda: buddy_calls.append("title"))
    captured_engine_kwargs["on_successful_turn"]()
    assert buddy_calls == ["buddy", "title"]
    assert captured_engine_kwargs["on_turn_started"].__self__ is title_updater
    cwd, *events = calls
    assert Path(cwd.removeprefix("cwd:")).samefile(workdir)
    assert events == ["run", "restore-terminal", "reset-title", "stop-debug"]


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
    # Simulate a console script: argparse also inspects the main module spec.
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None)
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


@pytest.mark.parametrize(
    ("clipboard_outcome", "failure_stage", "failure_type"),
    [
        ("success", "", None),
        ("failure", "", None),
        ("cancelled", "", None),
        *[
            ("success", stage, error)
            for stage in ("program-status", "graphics", "engine")
            for error in (RuntimeError, asyncio.CancelledError)
        ],
    ],
)
async def test_unmount_drains_title_updater_after_engine_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    clipboard_outcome: str,
    failure_stage: str,
    failure_type: type[BaseException] | None,
) -> None:
    """Keep hangup protection until a real clipboard queue has drained."""
    from threading import Event

    from chrys.app.tui import clipboard
    from chrys.app.tui.support.clipboard_writer import ClipboardWriter
    from tests.support.waiting import wait_for

    calls: list[str] = []
    entered, release = Event(), Event()

    def record(stage: str) -> None:
        calls.append(stage)
        if stage == failure_stage and failure_type is not None:
            raise failure_type("earlier cleanup failed")

    def write(text: str) -> None:
        entered.set()
        assert release.wait(5)
        calls.append(f"copied:{text}")

    class _Updater:
        async def shutdown(self) -> None:
            calls.append("updater")

    class _Reporter:
        async def close(self) -> None:
            record("program-status")

    class _Engine:
        async def shutdown(self) -> None:
            # Programmatic callbacks during teardown must not add new work.
            assert not clipboard.copy_text_to_clipboards(host, "late engine copy")
            record("engine")

    class _Graphics:
        async def close(self) -> None:
            record("graphics")

    class _Freeze:
        def close(self) -> None:
            calls.append("gc-close")

    class _Timer:
        def stop(self) -> None:
            calls.append("timer-stop")

    class _Host:
        _clipboard = "queued"
        _takeover_listener = None
        _startup_task = None
        _update_check_task = None
        _left_behind_task = None
        _program_status_reporter = _Reporter()
        _terminal_graphics = _Graphics()
        _session_title_updater = _Updater()
        _engine = _Engine()
        _gc_freeze = _Freeze()
        _gc_freeze_watchdog = _Timer()

        @property
        def clipboard(self) -> str:
            return self._clipboard

        def copy_to_clipboard(self, text: str) -> None:
            raise AssertionError("native Windows must not dispatch OSC52")

    async def observed_clipboard_close(app) -> None:
        calls.append("clipboard")
        await clipboard.close_clipboard(app)
        if clipboard_outcome == "failure":
            raise RuntimeError("clipboard cleanup failed")

    def remove_signal_handler(signum: int) -> bool:
        calls.append(f"remove-signal:{signum}")
        return True

    monkeypatch.setattr(clipboard, "_clipboard_frontend", lambda: "native-windows")
    monkeypatch.setattr("chrys.app.tui.app.close_clipboard", observed_clipboard_close)
    # Observe the signal-removal boundary portably, without changing OS handlers.
    monkeypatch.setattr("chrys.app.tui.app.HANGUP_SIGNALS", (1, 15))
    monkeypatch.setattr(asyncio.get_running_loop(), "remove_signal_handler", remove_signal_handler)
    host = _Host()
    writer = ClipboardWriter(write)
    clipboard._native_writers[host] = writer
    shutdown = None
    try:
        assert writer.submit("queued")
        await wait_for(entered.is_set)
        shutdown = asyncio.create_task(ChrysApp.on_unmount(host))
        await wait_for(lambda: writer.closed or shutdown.done())
        if shutdown.done():
            await shutdown
        assert writer.closed
        expected = ["program-status", "graphics", "gc-close", "timer-stop", "engine", "updater", "clipboard"]
        if failure_stage:
            expected = [*expected[: expected.index(failure_stage) + 1], "clipboard"]
        assert calls == expected
        assert not shutdown.done()
        if clipboard_outcome == "cancelled":
            shutdown.cancel()
        release.set()
        expected_error = asyncio.CancelledError if clipboard_outcome == "cancelled" else failure_type
        if expected_error is not None:
            with pytest.raises(expected_error):
                await shutdown
        else:
            await shutdown

        assert calls == [*expected, "copied:queued", "remove-signal:1", "remove-signal:15"]
        assert not clipboard.copy_text_to_clipboards(host, "late copy after unmount")
        assert host.clipboard == "queued"
        if failure_stage not in ("program-status", "graphics"):
            assert host._gc_freeze_watchdog is None
    finally:
        release.set()
        try:
            if shutdown is not None:
                with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                    await shutdown
        finally:
            await writer.wait()
            await writer.close()
            await clipboard.close_clipboard(host)


def test_terminal_title_reset_after_run_uses_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """After Textual exits, the host shell should not keep the last prompt preview."""
    from chrys.app.tui import app as tui_app

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.chdir(tmp_path)

    tui_app._reset_terminal_title_after_run()

    title = str(tmp_path)
    assert capsys.readouterr().err == f"\x1b]0;{title}\x07\x1b]2;{title}\x07"


def test_tui_help_does_not_offer_profile(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path) -> None:
    """TUI profile selection should happen inside the app, not through argv."""
    from chrys.app.tui import app as tui_app

    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform_with_config_dir(tmp_path))
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

    async def restore_startup_session(session_id: str, *, saved_cwd: str) -> str:
        await screen._sessions.do_session_restore(session_id, allow_while_loading=True, saved_cwd=saved_cwd)  # type: ignore[attr-defined]
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
    app._startup_workspace_id = None
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
        async def resolve_startup_session(self, session_id: str, *, require_inactive: bool = False) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id, primary_cwd="/saved")

    class _Sessions:
        async def do_session_restore(
            self, session_id: str, *, allow_while_loading: bool = False, saved_cwd: str | None = None
        ) -> None:
            calls.append(("restore", session_id, allow_while_loading, saved_cwd))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._locale_controller = tui_i18n.LocaleController(Settings(locale="en"))
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._startup_workspace_id = None
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
        ("restore", "session-1", True, "/saved"),
    ]
    assert app._startup_session_id == ""


async def test_start_engine_restores_canonical_startup_session_id() -> None:
    calls: list[tuple[str, object]] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append(("prepare", profile))

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def resolve_startup_session(self, session_id: str, *, require_inactive: bool = False) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id="canonical-session-id", primary_cwd="/saved")

    class _Sessions:
        async def do_session_restore(
            self, session_id: str, *, allow_while_loading: bool = False, saved_cwd: str | None = None
        ) -> None:
            calls.append(("restore", session_id, allow_while_loading, saved_cwd))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "canonicalsess"
    app._startup_workspace_id = None
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
        ("restore", "canonical-session-id", True, "/saved"),
    ]
    assert app._startup_session_id == ""


@pytest.mark.parametrize(
    "outcome",
    ["failed", "declined", "missing", "in_use", "in_use_preflight", "exception", "restore_error", "workspace_error"],
)
async def test_explicit_startup_restore_waits_for_choice_and_retries(outcome: str) -> None:
    from unittest.mock import AsyncMock, Mock

    from chrys.foundation.events.types import Error
    from chrys.service.state.store import StartupSessionInUseError
    from chrys.service.workspaces.paths import MissingWorkspacePrimary

    selector = "abc123abc123"
    meta = SimpleNamespace(session_id=selector + "0" * 20, primary_cwd="/saved")
    app = object.__new__(ChrysApp)
    app._locale_controller = SimpleNamespace(localizer=Localizer("en"))
    app._engine = AsyncMock()
    app._startup_session_id = selector
    app._startup_workspace_id = None
    app._deferred_settings_warnings = []
    app._state_store = AsyncMock()
    initial = {"missing": None, "in_use_preflight": StartupSessionInUseError(selector, meta.session_id)}
    app._state_store.resolve_startup_session.side_effect = [initial.get(outcome, meta), meta]
    screen = Mock()
    screen.dismiss_startup_load_dialog_before_restore = AsyncMock()
    missing_primary = MissingWorkspacePrimary("/gone")
    first = {
        "in_use": Error(code="session_in_use", message="busy", session_id=meta.session_id),
        "exception": RuntimeError("restore exploded"),
        # A failure after the restore committed: ordinary errors carry only text,
        # workspace errors their own display. Either way the session is named.
        "restore_error": Error(code="session_restore_failed", message="build exploded", session_id=meta.session_id),
        "workspace_error": Error(
            code="session_restore_failed",
            message=str(missing_primary),
            display_message=missing_primary.display_message,
            session_id=meta.session_id,
        ),
    }.get(outcome, outcome)
    screen.restore_startup_session = AsyncMock(side_effect=["restored"] if outcome in initial else [first, "restored"])

    async def retry(_screen, problem) -> bool:
        app._engine.start.assert_not_awaited()
        app._engine.reset_after_failed_startup_restore.assert_awaited_once()
        assert selector in format_message(problem)
        if outcome in {"in_use", "in_use_preflight"}:
            assert "open in another window" in format_message(problem)
        if outcome == "restore_error":
            assert format_message(problem) == f"Could not restore session {selector}: build exploded"
        if outcome == "workspace_error":
            assert (
                format_message(problem)
                == f"Could not restore session {selector}: Primary folder does not exist: /gone."
            )
        if outcome in initial:
            screen.restore_startup_session.assert_not_awaited()
        return False

    app._choose_new_startup_session = AsyncMock(side_effect=retry)
    await app._start_engine(object(), screen)
    app._choose_new_startup_session.assert_awaited_once()
    screen.restore_startup_session.assert_awaited_with(meta.session_id, saved_cwd="/saved")
    assert app._state_store.resolve_startup_session.await_count == 2
    app._state_store.resolve_startup_session.assert_awaited_with(selector, require_inactive=True)
    assert app._startup_session_id == ""
    app._engine.start.assert_not_awaited()


async def test_main_screen_startup_restore_requires_matching_success_event() -> None:
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.screens.main.state import MainScreenServices
    from chrys.foundation.events.types import SessionRestored

    bus = EventBus()

    class _Sessions:
        def __init__(self, *, publish_success: bool, request: RestoreRequest = RestoreRequest.REQUESTED) -> None:
            self.publish_success = publish_success
            self.request = request

        async def do_session_restore(
            self, session_id: str, *, allow_while_loading: bool = False, saved_cwd: str | None = None
        ) -> RestoreRequest:
            assert allow_while_loading is True
            if self.publish_success:
                await bus.publish(SessionRestored(session_id=session_id))
            return self.request

    screen = object.__new__(MainScreen)
    screen._services = MainScreenServices(bus=bus)
    screen._sessions = _Sessions(publish_success=False)
    assert await MainScreen.restore_startup_session(screen, "session-1", saved_cwd="/saved") == "failed"

    screen._sessions = _Sessions(publish_success=False, request=RestoreRequest.SKIPPED)
    assert await MainScreen.restore_startup_session(screen, "session-1", saved_cwd="/saved") == "failed"

    screen._sessions = _Sessions(publish_success=False, request=RestoreRequest.DECLINED)
    assert await MainScreen.restore_startup_session(screen, "session-1", saved_cwd="/saved") == "declined"

    screen._sessions = _Sessions(publish_success=True)
    assert await MainScreen.restore_startup_session(screen, "session-1", saved_cwd="/saved") == "restored"


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
        async def resolve_startup_session(self, session_id: str, *, require_inactive: bool = False) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id, primary_cwd="/saved")

    class _Sessions:
        async def do_session_restore(
            self, session_id: str, *, allow_while_loading: bool = False, saved_cwd: str | None = None
        ) -> None:
            calls.append(("restore", session_id, allow_while_loading, saved_cwd))

    monkeypatch.setattr(tui_app.asyncio, "sleep", fake_sleep)
    startup_dialog = AgentLoadDialog()
    monkeypatch.setattr(startup_dialog, "dismiss", lambda result=None: calls.append(("dismiss", result)))

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._startup_workspace_id = None
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
        ("restore", "session-1", True, "/saved"),
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

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append(("reset_restore", None))

        async def start(self, profile: object) -> None:
            calls.append(("start", profile))

    class _Store:
        async def resolve_startup_session(self, session_id: str, *, require_inactive: bool = False) -> object:
            calls.append(("meta", session_id))
            return SimpleNamespace(session_id=session_id, primary_cwd="/saved")

    class _Sessions:
        async def do_session_restore(
            self, session_id: str, *, allow_while_loading: bool = False, saved_cwd: str | None = None
        ) -> None:
            calls.append(("restore", session_id, allow_while_loading, saved_cwd))

    monkeypatch.setattr(tui_app.asyncio, "sleep", fake_sleep)

    app = object.__new__(ChrysApp)
    app._engine = _Engine()
    app._state_store = _Store()
    app._startup_session_id = "session-1"
    app._startup_workspace_id = None
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
        ("restore", "session-1", True, "/saved"),
    ]


async def test_start_engine_drops_deferred_settings_warnings_after_a_successful_restore() -> None:
    """The restore republishes its own settings load's warnings; the bootstrap ones are stale."""
    calls: list[str] = []

    class _Engine:
        async def prepare(self, profile: object) -> None:
            calls.append("prepare")

        async def reset_after_failed_startup_restore(self) -> None:
            calls.append("reset_restore")

        async def start(self, profile: object) -> None:
            calls.append("start")

    class _Store:
        async def resolve_startup_session(self, session_id: str, *, require_inactive: bool = False) -> object:
            return SimpleNamespace(session_id=session_id, primary_cwd="/saved")

    class _Sessions:
        async def do_session_restore(
            self, session_id: str, *, allow_while_loading: bool = False, saved_cwd: str | None = None
        ) -> None:
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
    app._startup_workspace_id = None
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
        async def resolve_startup_session(self, session_id: str, *, require_inactive: bool = False) -> object:
            return SimpleNamespace(session_id=session_id, primary_cwd="/saved")

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

        async def restore_startup_session(self, session_id: str, *, saved_cwd: str) -> str:
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
    app._startup_workspace_id = None
    app._bus = bus
    app._deferred_settings_warnings = [Warning(code="settings_layer_warning", message="From the launch cwd.")]

    from unittest.mock import AsyncMock

    app._choose_new_startup_session = AsyncMock(return_value=True)
    await app._start_engine(object(), _Screen())  # type: ignore[arg-type]

    assert calls == ["prepare", "reset_restore", ("flushed", "settings_layer_warning"), "start"]
    assert app._deferred_settings_warnings == []


@pytest.mark.parametrize(
    ("locale", "outcome", "restart_text"),
    [
        ("en", "failed", "start again"),
        ("en", "declined", "start again"),
        ("zh-Hans", "failed", "重新启动"),
        ("zh-Hans", "declined", "重新启动"),
        ("zh-Hans", "no-success", ""),
        ("zh-Hans", "fresh", ""),
        ("zh-Hans", "fresh-missing", "重新启动"),
    ],
)
async def test_workspace_startup_error_is_localized(tmp_path, locale, outcome, restart_text):
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.state.store import JsonFileStateStore
    from chrys.service.workspaces.paths import MissingWorkspacePrimary
    from chrys.service.workspaces.registry import workspace_not_found

    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("target", {"messages": []}, primary_cwd=str(tmp_path))
    app = object.__new__(ChrysApp)
    app._state_store = store
    app._startup_session_id = "" if outcome.startswith("fresh") else "target"
    app._startup_workspace_id = "private-selector" if outcome == "fresh" else "team"
    app._startup_workspace_snapshot = None
    app._startup_workspace_cwd = ""
    app._locale_controller = tui_i18n.LocaleController(Settings(locale=locale))
    failure = (
        MissingWorkspacePrimary(str(tmp_path / "missing"))
        if outcome == "fresh-missing"
        else workspace_not_found("private-selector")
    )
    app._engine = SimpleNamespace(prepare=AsyncMock(), start=AsyncMock(side_effect=failure))
    app.exit = Mock()
    screen = create_autospec(MainScreen, instance=True)
    if outcome == "declined":
        screen.restore_startup_session.return_value = "declined"
    elif outcome == "no-success":
        screen.restore_startup_session.return_value = "failed"
    else:
        screen.restore_startup_session.side_effect = MissingWorkspacePrimary(str(tmp_path / "missing"))

    profile = object()
    await app._start_engine(profile, screen)

    app.exit.assert_called_once()
    assert app.exit.call_args.kwargs["return_code"] == 1
    message = app.exit.call_args.kwargs["message"]
    if outcome == "fresh":
        app._engine.start.assert_awaited_once_with(
            profile, workspace_id="private-selector", workspace=Workspace.from_cwd(), workspace_snapshot=None
        )
        assert message == tui_i18n.render_str(app.localizer, failure.display_message)
        assert message != str(failure)
    elif outcome == "fresh-missing":
        app._engine.start.assert_awaited_once()
        assert message.splitlines()[0] == tui_i18n.render_str(app.localizer, failure.display_message)
        assert restart_text in message
    elif outcome == "no-success":
        assert "无法恢复会话" in message
        app._engine.start.assert_not_awaited()
    else:
        app._engine.start.assert_not_awaited()
        assert restart_text in message
        assert "--workspace" in message and "-s" in message


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_missing_tui_workspace_primary_exits_before_bootstrap_in_the_ui_locale(
    monkeypatch, tmp_path, capsys, restore, locale
):
    from chrys.app.tui import app as tui_app
    from chrys.foundation.config.user_settings import user_settings_path
    from chrys.service.workspaces.registry import WorkspaceRegistry

    primary = tmp_path / "primary"
    primary.mkdir()
    WorkspaceRegistry().create("Team", str(primary))
    primary.rmdir()
    user_settings_path().write_text(f"ui:\n  locale: {locale}\n", encoding="utf-8")
    argv = ["icode", "--workspace", "Team"]
    if restore:
        argv.extend(["-s", "abc123abc123"])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr("chrys.orchestration.startup.configure_utf8_stdio", lambda: None)
    monkeypatch.setattr(
        "chrys.orchestration.startup.bootstrap_runtime", lambda **_: pytest.fail("missing primary reached bootstrap")
    )

    with pytest.raises(SystemExit) as caught:
        tui_app.main()
    assert caught.value.code == 1
    error, hint = capsys.readouterr().err.splitlines()
    if locale == "en":
        assert error == f"The primary folder of workspace “Team”, {primary}, does not exist."
        assert "primary folder" in hint and "main folder" not in hint
        assert ("start again with --workspace and -s" in hint) == restore
    else:
        assert error == f"工作区“Team”的主文件夹 {primary} 不存在。"
        assert "主文件夹" in hint and "start again" not in hint
        assert ("再用 --workspace 和 -s 重新启动" in hint) == restore
    if not restore:
        assert "-s" not in hint


@pytest.fixture
def workspace_argv(monkeypatch, tmp_path, frozen_earlier_heap):
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    from chrys.app.tui import app as tui_app
    from chrys.service.llm.mock import MockChatClient
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.models.serializer import save_profile
    from chrys.service.state.store import JsonFileStateStore
    from tests.orchestration import _session_host_helpers as helpers

    profile = helpers._make_profile()
    AgentProfileRegistry().save(profile)
    _, models = helpers._make_settings_and_models()
    save_profile(models.get("mock-profile"))
    helpers._patch_runtime(monkeypatch, [MockChatClient() for _ in range(3)], [])
    monkeypatch.setattr("chrys.orchestration.startup.configure_utf8_stdio", lambda: None)
    monkeypatch.setattr(tui_app, "ask_terminal", lambda *args, **kwargs: None)
    monkeypatch.setattr(tui_app, "_restore_terminal_after_run", lambda app: None)
    monkeypatch.setattr(tui_app, "_reset_terminal_title_after_run", lambda: None)
    monkeypatch.chdir(tmp_path)
    messages = []

    def launch(args, examine):
        class HeadlessTui(ChrysApp):
            CSS_PATH = Path(sys.modules[ChrysApp.__module__].__file__).with_name("chrys.tcss")

            def run(self):
                async def drive():
                    async with self.run_test(size=(120, 40)) as pilot:
                        await examine(self, pilot)

                asyncio.run(drive())

            def exit(self, result=None, return_code=0, message=None):
                if message:
                    messages.append(str(message))
                super().exit(result, return_code, message)

        monkeypatch.setattr(sys, "argv", ["icode", "-a", profile.name, "-m", "mock-profile", *args])
        tui_app.main(app_cls=HeadlessTui)

    return JsonFileStateStore(), launch, messages


@pytest.mark.parametrize("entry", ["present", "retarget", "choose-replacement", "cancel"])
def test_tui_argv_restore_ignores_workdir_until_replacement_is_chosen(
    registry, workspace_argv, monkeypatch, tmp_path, entry
):
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
    from chrys.foundation.events.types import SessionRestore
    from chrys.kernel import Message
    from tests.app.tui.behaviors.test_missing_working_dir_flows import _choose_folder, _top_screen
    from tests.support.tui_helpers import click_when_settled
    from tests.support.workspace_registry import make_workdirs, write_launch_settings

    store, launch, _ = workspace_argv
    primary, override, replacement = make_workdirs(tmp_path, "primary", "override", "replacement")
    workspace_primary = primary
    if entry == "retarget":
        workspace_primary = tmp_path / "workspace-primary"
        symlink_or_skip(workspace_primary, primary, target_is_directory=True)
        publish = EventBus.publish

        async def retarget_at_open(bus, event, **kwargs):
            if isinstance(event, SessionRestore) and workspace_primary.is_symlink():
                retarget_workdir_alias(workspace_primary, override)
            return await publish(bus, event, **kwargs)

        monkeypatch.setattr(EventBus, "publish", retarget_at_open)
    workspace = registry.create("Team", str(workspace_primary))
    registry.set_session_workspace(TARGET_SESSION, workspace.id)
    asyncio.run(
        save_chat_session(
            store, TARGET_SESSION, override, profile="Headless", messages=[Message("user", ["Keep this history"])]
        )
    )
    expected = primary if entry in {"present", "retarget"} else replacement
    report = tmp_path / "restore-hook.txt"
    write_launch_settings(expected, report, event="session_restored")
    if entry in {"choose-replacement", "cancel"}:
        primary.rmdir()

    async def examine(app, pilot):
        if entry in {"choose-replacement", "cancel"}:
            dialog = await _top_screen(pilot, ConfirmDialog)
            message = dialog.query_one("#confirm-message", Static).render().plain
            assert message == (
                f"The primary folder {primary} of workspace “Team” no longer exists. "
                f"Restore {primary} to keep the saved session in “Team”.\n\n"
                "• Choose Folder…: select a replacement working directory and open the saved session as Unspecified.\n"
                "• Cancel: keep the current session open."
            )
            assert not report.exists()
            if entry == "cancel":
                from chrys.app.tui.screens.dialogs.confirm import StartupSessionChoiceDialog

                await click_when_settled(pilot, "#confirm-no")
                await _top_screen(pilot, StartupSessionChoiceDialog)
                await click_when_settled(pilot, "#confirm-yes")
            else:
                await _choose_folder(pilot, dialog, tmp_path, replacement)
        await await_engine_startup(app)
        assert registry.snapshot().workspace_id_for_session(TARGET_SESSION) == (
            None if entry == "choose-replacement" else workspace.id
        )
        if entry == "cancel":
            assert app._engine.session_id != TARGET_SESSION
            assert not report.exists()
            assert (await store.load_session(TARGET_SESSION))["messages"][0].text == "Keep this history"
        else:
            assert app._engine.session_id == TARGET_SESSION
            assert app._engine.workspace.primary_cwd == str(expected)
            assert app._engine.settings_handle.settings.warn_threshold_pct == 0.73
            assert any(message.text == "Keep this history" for message in app._engine.history_messages)
            assert Path(report.read_text(encoding="utf-8")).samefile(expected)

    launch(["-s", TARGET_SESSION, "-C", str(override)], examine)


@pytest.mark.parametrize(
    "outcome", ["new", "missing", "move", "refused", "saved-open-failed", "installed-restore-failed"]
)
def test_tui_workspace_argv(registry, workspace_argv, monkeypatch, tmp_path, outcome):
    from chrys.foundation.config.spec import Source
    from chrys.kernel import Message
    from chrys.orchestration.engine.build import builder
    from chrys.orchestration.engine.session_lifecycle import SessionLifecycle
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.workspaces import registry as registry_module
    from tests.orchestration._session_host_helpers import _make_profile
    from tests.support.workspace_registry import make_workdirs, write_launch_settings

    store, launch, messages = workspace_argv
    primary, old, extra = make_workdirs(tmp_path, "primary", "old", "extra")
    report = tmp_path / "workspace-hook.txt"
    write_launch_settings(
        primary, report, event="session_start" if outcome in {"new", "missing"} else "session_restored"
    )
    workspace = registry.create("Team", str(primary))
    previous = registry.create("Previous", str(old))
    before = None
    if outcome not in {"new", "missing"}:
        asyncio.run(
            save_chat_session(
                store,
                TARGET_SESSION,
                old,
                profile="Headless",
                messages=[Message("user", ["previous chat"])],
                workdirs=[old, extra],
            )
        )
        registry.set_session_workspace(TARGET_SESSION, previous.id)
        before = (store.session_dir(TARGET_SESSION) / "session.json").read_bytes()
    if outcome == "refused":
        default_path, original_path, profile_bytes = move_profile_to_legacy_path(
            AgentProfileRegistry(), _make_profile()
        )

        def reject(*args, **kwargs):
            raise OSError("membership disk full")

        monkeypatch.setattr(registry_module, "atomic_write_text", reject)
    if outcome == "saved-open-failed":

        async def fail_build(*args, **kwargs):
            raise RuntimeError("target build failed")

        monkeypatch.setattr(builder, "create_client", fail_build)
    if outcome == "installed-restore-failed":

        def fail_after_install(lifecycle):
            assert lifecycle._session.session_id == TARGET_SESSION
            assert lifecycle._session.workspace.primary_cwd == str(primary)
            assert lifecycle._current.loaded is not None
            raise RuntimeError("installed restore failed")

        monkeypatch.setattr(SessionLifecycle, "_restore_terminal_fsm_from_history", fail_after_install)

    failing = outcome in {"refused", "saved-open-failed", "installed-restore-failed"}

    async def examine(app, pilot):
        if failing:
            from tests.support.waiting import MAIN_SCREEN_BUILD_TIMEOUT, wait_for

            await wait_for(lambda: bool(messages), timeout=MAIN_SCREEN_BUILD_TIMEOUT, description="startup error")
            return
        if outcome == "missing":
            from chrys.app.tui.screens.dialogs.confirm import StartupSessionChoiceDialog
            from tests.app.tui.behaviors.test_missing_working_dir_flows import _top_screen
            from tests.support.tui_helpers import click_when_settled

            dialog = await _top_screen(pilot, StartupSessionChoiceDialog)
            assert "was not found" in str(dialog.query_one("#confirm-message", Static).content)
            await click_when_settled(pilot, "#confirm-yes")
        await await_engine_startup(app)
        engine = app._engine
        assert Path.cwd().samefile(primary)
        assert engine.workspace.primary_cwd == str(primary)
        tag = app._main_screen.query_one("#workspace-tag", Static)
        assert tag.display
        assert tag.render().plain == "Team"
        assert engine.settings_handle.base.source_for("context.warn_threshold_pct").layer == Source.WORKDIR
        assert Path(report.read_text(encoding="utf-8")).samefile(primary)
        assert registry.snapshot().workspace_id_for_session(engine.session_id) == workspace.id
        if outcome in {"new", "missing"}:
            assert engine.session_id != MISSING_SESSION
        else:
            assert engine.session_id == TARGET_SESSION
            from tests.support.waiting import wait_for

            # The move notice is posted after startup settles; wait for it rather than racing it.
            await wait_for(
                lambda: any("Previous" in str(n.message) and "Team" in str(n.message) for n in shown_toasts(app)),
                pilot=pilot,
                description="workspace move notice",
            )

    args = ["--workspace", "Team"]
    if outcome != "new":
        args.extend(("-s", MISSING_SESSION if outcome == "missing" else TARGET_SESSION))
    if failing:
        with pytest.raises(SystemExit) as failed:
            launch(args, examine)
        assert failed.value.code == 1
        assert (store.session_dir(TARGET_SESSION) / "session.json").read_bytes() == before
        assert not report.exists()
        if outcome == "refused":
            assert any("membership disk full" in message for message in messages)
            assert registry.snapshot().workspace_id_for_session(TARGET_SESSION) == previous.id
            assert original_path.read_bytes() == profile_bytes
            assert not default_path.exists()
        else:
            error = "target build failed" if outcome == "saved-open-failed" else "installed restore failed"
            assert any(error in message and "Reopening uses Team" in message for message in messages)
            assert registry.snapshot().workspace_id_for_session(TARGET_SESSION) == workspace.id
    else:
        launch(args, examine)
