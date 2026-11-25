# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Textual app shell."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Checkbox, Input, TextArea

from chrys.app.tui import app as chrys_app
from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.i18n import LocaleSwitchStatus
from chrys.app.tui.notifications import NotificationService
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import DEFAULT_THEME, Settings, persist_approval_mode
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle, load_settings
from chrys.foundation.config.spec import Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SettingsReload, Warning
from chrys.foundation.i18n import Localizer
from chrys.service.approval.policy import ApprovalMode
from chrys.service.state.store import JsonFileStateStore
from tests.support.tui_app_harness import (
    EmptyAgentRegistry,
    SessionGenerationEngine,
    ShutdownOnlyEngine,
    make_chrys_app,
)
from tests.support.waiting import wait_for


def test_tui_module_main_is_callable() -> None:
    """The hidden serve subprocess dispatch relies on the TUI module entrypoint."""
    from chrys.app.tui import app as tui_app

    assert callable(tui_app.main)


@pytest.mark.parametrize(
    ("profile_kwargs", "expected_profile"),
    [
        pytest.param({}, "QA", id="defaults-profile-from-settings"),
        pytest.param({"profile_name": "Code"}, "Code", id="explicit-profile-overrides-settings-default"),
    ],
)
def test_chrys_app_resolves_the_startup_profile_name(
    profile_kwargs: dict[str, str],
    expected_profile: str,
    tmp_path: Path,
) -> None:
    """The default case omits ``profile_name`` outright, so the constructor default stays under test."""
    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(default_agent="QA"),
        state_store=JsonFileStateStore(tmp_path),
        **profile_kwargs,
    )

    assert app._profile_name == expected_profile


@pytest.mark.parametrize("mode", list(ApprovalMode))
async def test_startup_warns_only_in_bypass_mode(tmp_path: Path, mode: ApprovalMode) -> None:
    from chrys.orchestration.engine.assembly import assemble_agent_engine

    assert persist_approval_mode(mode.value) is None
    freeze_process_env()
    loaded = load_settings()
    bus = EventBus()
    engine = assemble_agent_engine(bus, loaded_settings=loaded)
    app = make_chrys_app(tmp_path, settings=engine.settings, engine=engine, event_bus=bus)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        warnings = [notice.message for notice in app._notifications if notice.severity == "warning"]
        assert warnings == (
            ["Approval mode is bypass. Tool calls will run without asking for approval."]
            if mode is ApprovalMode.BYPASS
            else []
        )


async def _approval_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChrysApp:
    from chrys.orchestration.engine.assembly import assemble_agent_engine
    from chrys.service.profiles.agents.registry import AgentProfileRegistry

    freeze_process_env()
    bus = EventBus()
    engine = assemble_agent_engine(bus, loaded_settings=load_settings())
    await engine.prepare()
    registry = AgentProfileRegistry()
    monkeypatch.setattr(registry, "load_all", lambda: None)
    return ChrysApp(
        bus,
        engine,
        settings_handle=engine.settings_handle,
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=registry,
        gc_freeze_enabled=False,
    )


@pytest.mark.parametrize("scenario", ["saved", "permission-denied", "env-pinned", "invalid-env"])
async def test_approval_default_in_settings_follows_the_last_successful_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    from textual.widgets import Select

    from chrys.app.tui.screens.settings import SettingsDialog

    env_pinned = scenario in {"env-pinned", "invalid-env"}
    if env_pinned:
        monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", "invalid" if scenario == "invalid-env" else "auto")
    app = await _approval_app(tmp_path, monkeypatch)

    def deny_write(*_args, **_kwargs):
        raise PermissionError("settings write denied")

    async with app.run_test(notifications=True) as pilot:
        main = app._main_screen
        assert main is not None
        if not env_pinned:
            # F10 can save a new default without changing the active mode or its settings overlay.
            main._settings_coordinator().schedule_persist({"approval.default_mode": "auto"})
            assert await main._settings_persistence().flush()
            assert load_settings().settings.default_approval_mode == "auto"
        if scenario == "permission-denied":
            monkeypatch.setattr("chrys.app.tui.screens.main.settings_persistence.persist", deny_write)

        await main._config_actions.set_approval_mode("bypass")
        main._config_actions.open_settings("safety")
        await pilot.pause()

        assert isinstance(app.screen, SettingsDialog)
        row = next(row for row in app.screen.rows() if row.spec.key == "approval.default_mode")
        assert main.header_approval_mode is ApprovalMode.BYPASS
        expected = {"saved": "bypass", "permission-denied": "auto", "env-pinned": "auto", "invalid-env": "manual"}[
            scenario
        ]
        selected = row.query_one(Select)
        assert selected.value == expected
        assert selected.disabled is env_pinned
        next_launch = load_settings(manual_reload=True)
        assert next_launch.settings.default_approval_mode == expected
        assert next_launch.file_values["default_approval_mode"] == (
            "auto" if scenario == "permission-denied" else "bypass"
        )


@pytest.mark.parametrize("edit_again", [False, True], ids=["panel-then-command", "panel-command-panel"])
async def test_approval_writes_follow_the_order_the_user_chose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit_again: bool
) -> None:
    from threading import Event

    from textual.widgets import Select

    from chrys.app.tui.screens.main import settings_persistence
    from chrys.app.tui.screens.settings import SettingsDialog
    from chrys.foundation.config.yaml_store import LOCK_TIMEOUT_SECONDS
    from tests.support.waiting import DEFAULT_WAIT_TIMEOUT

    assert persist_approval_mode("bypass") is None
    app = await _approval_app(tmp_path, monkeypatch)
    persist = settings_persistence.persist
    committed = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def hold_receipt(values, *, remove, lock_timeout=LOCK_TIMEOUT_SECONDS):
        result = persist(values, remove=remove, lock_timeout=lock_timeout)
        loop.call_soon_threadsafe(committed.set)
        assert release.wait(DEFAULT_WAIT_TIMEOUT)
        return result

    monkeypatch.setattr(settings_persistence, "persist", hold_receipt)
    async with app.run_test() as pilot:
        main = app._main_screen
        assert main is not None
        main._settings_coordinator().schedule_persist({"approval.default_mode": "auto"})
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(main._settings_persistence().flush())
            try:
                await asyncio.wait_for(committed.wait(), DEFAULT_WAIT_TIMEOUT)
                tasks.create_task(main._config_actions.set_approval_mode("manual"), eager_start=True)
                assert app._engine.approval_mode is ApprovalMode.MANUAL
                assert main.header_approval_mode is ApprovalMode.MANUAL
                if edit_again:
                    main._settings_coordinator().schedule_persist({"approval.default_mode": "auto"})
            finally:
                release.set()
        assert await main._settings_persistence().flush()
        main._config_actions.open_settings("safety")
        await pilot.pause()
        assert isinstance(app.screen, SettingsDialog)
        row = next(row for row in app.screen.rows() if row.spec.key == "approval.default_mode")
        expected = "auto" if edit_again else "manual"
        assert main.header_approval_mode is ApprovalMode.MANUAL
        assert row.query_one(Select).value == load_settings().settings.default_approval_mode == expected


@pytest.mark.parametrize(
    ("requested_locale", "expected_message"),
    [
        ("zh-Hans", "Could not load translations for locale zh-Hans; using English."),
        ("system", "Could not load translations for locale system; using English."),
    ],
)
async def test_chrys_app_locale_catalog_failure_follows_startup_warning_order(
    requested_locale: str,
    expected_message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _TestApp(ChrysApp):
        CSS_PATH = None

        def _build_main_screen(self) -> Screen:
            return Screen()

    monkeypatch.setenv("CHRYS_LOCALE", requested_locale)
    if requested_locale == "system":
        monkeypatch.setattr("chrys.foundation.i18n.localizer.normalize_locale", lambda _requested: "zh-Hans")

    catalog_root = tmp_path / "catalogs"
    catalog_path = catalog_root / "zh-Hans" / "LC_MESSAGES" / "chrys.mo"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_bytes(b"not a catalog")
    localizer = Localizer(requested_locale, catalog_root=catalog_root)
    injected_warning = Warning(code="bootstrap_warning", message="Injected startup warning.")
    bus = EventBus()
    published: list[Warning] = []

    async def _record_warning(event: Warning) -> None:
        published.append(event)

    await bus.subscribe(Warning, _record_warning)
    app = _TestApp(
        bus,
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(locale=requested_locale),
        state_store=JsonFileStateStore(tmp_path / "state"),
        agent_registry=EmptyAgentRegistry(),  # type: ignore[arg-type]
        startup_warnings=[injected_warning],
        localizer=localizer,
        gc_freeze_enabled=False,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

    assert [(warning.code, warning.message) for warning in published] == [
        ("bootstrap_warning", "Injected startup warning."),
        ("i18n_catalog_load_failed", expected_message),
    ]


async def test_chrys_app_locale_uses_settings_and_healthy_catalog_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from chrys.orchestration import session_host as session_host_mod

    class _TestApp(ChrysApp):
        CSS_PATH = None

        def _build_main_screen(self) -> Screen:
            return Screen()

    monkeypatch.setenv("CHRYS_LOCALE", "zh-Hans")
    bus = EventBus()
    published: list[Warning] = []

    async def _record_warning(event: Warning) -> None:
        published.append(event)

    await bus.subscribe(Warning, _record_warning)
    app = _TestApp(
        bus,
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(locale="zh-Hans"),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=EmptyAgentRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    assert (
        app.localizer.render(session_host_mod._SESSION_NOT_FOUND.bind(session_id="missing")) == "未找到会话：missing"  # noqa: RUF001
    )

    async with app.run_test() as pilot:
        await pilot.pause()

    assert [warning for warning in published if warning.code == "i18n_catalog_load_failed"] == []


def test_chrys_app_controller_owns_localizer_and_preserves_transcript_seam(tmp_path: Path) -> None:
    settings = Settings(locale="en")
    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=settings,
        state_store=JsonFileStateStore(tmp_path),
    )

    screen = app._build_main_screen()

    assert app.locale_controller.localizer is app.localizer
    assert app.locale_controller.requested_locale == settings.locale
    assert screen._locale_controller is app.locale_controller
    assert screen._localization is app.localizer


async def test_locale_switch_avoids_backend_global_ui_transcript_and_gc_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.widgets.chat.panel import ChatPanel

    class _Engine:
        approval_mode = ApprovalMode.MANUAL

        def __init__(self) -> None:
            self.rebuild_calls = 0

        async def shutdown(self) -> None:
            return

        async def rebuild(self) -> None:
            self.rebuild_calls += 1

    engine = _Engine()
    bus = EventBus()
    settings_reloads: list[SettingsReload] = []

    async def record_settings_reload(event: SettingsReload) -> None:
        settings_reloads.append(event)

    await bus.subscribe(SettingsReload, record_settings_reload)
    app = ChrysApp(
        bus,
        engine,  # type: ignore[arg-type]
        settings=Settings(locale="en"),
        state_store=JsonFileStateStore(tmp_path),
        gc_freeze_enabled=False,
    )
    main_screen = app._build_main_screen()
    app._main_screen = main_screen
    forbidden_calls: list[str] = []
    persisted: list[str] = []

    def forbidden(name: str):
        def record(*_args: object, **_kwargs: object) -> None:
            forbidden_calls.append(name)

        return record

    monkeypatch.setattr(tui_i18n, "persist_locale", persisted.append)
    monkeypatch.setattr(app._gc_freeze, "request_absorb", forbidden("gc_absorb"))
    monkeypatch.setattr(app._gc_freeze, "request_reclaim", forbidden("gc_reclaim"))
    monkeypatch.setattr(type(app.stylesheet), "update", forbidden("stylesheet_update"))
    monkeypatch.setattr(MainScreen, "recompose", forbidden("main_recompose"))
    monkeypatch.setattr(MainScreen, "walk_children", forbidden("main_transcript_walk"))
    monkeypatch.setattr(ChatPanel, "walk_children", forbidden("chat_transcript_walk"))
    monkeypatch.setattr(app, "run_worker", forbidden("revision_worker"))

    results = [app.locale_controller.switch_locale("zh-Hans") for _ in range(4)]
    await asyncio.sleep(0)

    assert [result.status for result in results] == [
        LocaleSwitchStatus.EFFECTIVE_CHANGED,
        LocaleSwitchStatus.IDENTICAL_REQUEST,
        LocaleSwitchStatus.IDENTICAL_REQUEST,
        LocaleSwitchStatus.IDENTICAL_REQUEST,
    ]
    assert app.locale_controller.revision == 1
    assert persisted == ["zh-Hans"]
    assert settings_reloads == []
    assert engine.rebuild_calls == 0
    assert forbidden_calls == []


def test_build_main_screen_plumbs_workspace_mru_max_entries(tmp_path: Path) -> None:
    """The startup-only MRU setting must reach the screen services intact."""

    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(workspace_mru_max_entries=7),
        state_store=JsonFileStateStore(tmp_path),
    )

    screen = app._build_main_screen()

    assert screen._services.workspace_mru_max_entries == 7


def test_chrys_app_accepts_startup_session_id(tmp_path: Path) -> None:
    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        state_store=JsonFileStateStore(tmp_path),
        startup_session_id=" session-1 ",
    )

    assert app._startup_session_id == "session-1"


def test_chrys_app_defers_settings_warnings_only_for_a_startup_restore(tmp_path: Path) -> None:
    """Bootstrap settings warnings describe the launch cwd, not a restored session's root."""

    bootstrap_warning = Warning(code="bootstrap_warning", message="Env grumbled.")
    settings_warning = Warning(code="settings_layer_warning", message="Project layer grumbled.")

    restoring = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        state_store=JsonFileStateStore(tmp_path / "restoring"),
        startup_warnings=[bootstrap_warning],
        settings_warnings=[settings_warning],
        startup_session_id="session-1",
    )
    fresh = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        state_store=JsonFileStateStore(tmp_path / "fresh"),
        startup_warnings=[bootstrap_warning],
        settings_warnings=[settings_warning],
    )

    assert restoring._deferred_settings_warnings == [settings_warning]
    assert settings_warning not in restoring._startup_warnings
    assert fresh._deferred_settings_warnings == []
    assert fresh._startup_warnings[:2] == [bootstrap_warning, settings_warning]


def _app_with_loaded_settings(
    tmp_path: Path,
    loaded: LoadedSettings,
    handle: SettingsHandle | None = None,
) -> ChrysApp:
    return ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings_handle=handle or SettingsHandle(loaded),
        state_store=JsonFileStateStore(tmp_path),
    )


def test_a_live_write_reaches_every_holder_of_the_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """What the shared handle buys over two holders that started equal.

    Freezing ``Settings`` removed the accident that used to carry a live write
    across — the app and the engine held the same instance, so mutating a field
    changed both — and rebinding a private field would have traded a provenance
    bug for a divergence bug. Sharing the cell is what makes the write arrive
    everywhere without anyone having to push it.
    """
    monkeypatch.setattr(chrys_app, "persist_theme", lambda _theme: None)
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    handle = SettingsHandle(LoadedSettings(settings=Settings(theme=DEFAULT_THEME, locale="en"), provenance={}))
    app = _app_with_loaded_settings(tmp_path, handle.loaded, handle=handle)

    app.theme = "chrys-legacy"
    app.locale_controller.switch_locale("zh-Hans")

    # Read through the handle, which is what the engine holds too.
    assert handle.settings.theme == "chrys-legacy"
    assert handle.settings.locale == "zh-Hans"
    # One object, not two that agree: provenance and seals travel with it.
    assert handle.loaded is app._loaded_settings
    assert handle.settings is app._settings


def test_switching_the_theme_moves_its_provenance_with_the_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The app shares its ``Settings`` with the engine, so a live write must
    go through the overlay or the engine keeps reporting the old layer."""
    monkeypatch.setattr(chrys_app, "persist_theme", lambda _theme: None)
    loaded = LoadedSettings(settings=Settings(theme=DEFAULT_THEME), provenance={})
    app = _app_with_loaded_settings(tmp_path, loaded)

    app.theme = "chrys-legacy"

    assert app._settings.theme == "chrys-legacy"
    assert app._loaded_settings.settings is app._settings
    assert app._loaded_settings.source_for("ui.theme").layer is Source.RUNTIME


def test_switching_the_locale_moves_its_provenance_with_the_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    loaded = LoadedSettings(settings=Settings(locale="en"), provenance={})
    app = _app_with_loaded_settings(tmp_path, loaded)

    app.locale_controller.switch_locale("zh-Hans")

    assert app._settings.locale == "zh-Hans"
    assert app.locale_controller.requested_locale == "zh-Hans"
    assert app._loaded_settings.settings is app._settings
    assert app._loaded_settings.source_for("ui.locale").layer is Source.RUNTIME


def test_chrys_app_rejects_two_different_settings_objects(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not two different ones"):
        ChrysApp(
            EventBus(),
            ShutdownOnlyEngine(),  # type: ignore[arg-type]
            settings=Settings(theme="chrys-legacy"),
            settings_handle=SettingsHandle(LoadedSettings(settings=Settings(theme=DEFAULT_THEME), provenance={})),
            state_store=JsonFileStateStore(tmp_path),
        )


@pytest.mark.parametrize(
    ("module_default", "gc_freeze_kwargs"),
    [
        pytest.param(False, {"gc_freeze_enabled": True}, id="argument-overrides-the-module-default"),
        pytest.param(True, {}, id="follows-the-module-default-when-no-argument-is-given"),
    ],
)
def test_chrys_app_gc_freeze_enablement(
    module_default: bool,
    gc_freeze_kwargs: dict[str, bool],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No setting reaches this: production is on, and only code turns it off.

    The second case omits the keyword entirely, so the constructor's own
    default is what defers to the module flag.
    """
    monkeypatch.setattr(chrys_app, "GC_FREEZE_ENABLED", module_default)
    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        **gc_freeze_kwargs,
    )

    assert app._gc_freeze.enabled is True
    assert app._gc_freeze.started is False
    assert app._startup_warnings == []


@pytest.mark.parametrize(
    ("locale", "message", "title"),
    [
        ("en", "Press ctrl+q to quit the app", "Do you want to quit?"),
        ("zh-Hans", "按 ctrl+q 退出应用", "要退出吗？"),  # noqa: RUF001
    ],
)
async def test_inherited_ctrl_c_help_quit_notifies_in_active_locale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    locale: str,
    message: str,
    title: str,
) -> None:
    """Stock App keeps ctrl+c bound to help_quit; its hint must localize.

    Both lower namespaces that bind ctrl+c to a copy action — the focused
    chat text area and the screen — must defer on an empty selection
    (SkipAction) so the key reaches the app-level hint at all. Press from
    the real post-launch focus to exercise the whole chain.
    """
    from chrys.app.tui.widgets.text_area import EnhancedTextArea

    app = make_chrys_app(tmp_path, settings=Settings(locale=locale), engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        await pilot.pause()
        captured: list[tuple[str, str, bool]] = []

        def _notify(
            notice: str,
            *,
            title: str = "",
            severity: str = "information",
            timeout: float | None = None,
            markup: bool = True,
        ) -> None:
            del severity, timeout
            captured.append((notice, title, markup))

        monkeypatch.setattr(app, "notify", _notify)
        assert isinstance(app.focused, EnhancedTextArea)
        await pilot.press("ctrl+c")
        assert captured == [(message, title, False)]


@pytest.mark.asyncio
async def test_insert_clipboard_shortcuts_cover_editor_and_screen_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Editor-owned Insert bindings copy the focused selection, then rendered output."""

    class _Engine(SessionGenerationEngine):
        session_generation = 0

        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    clipboard = {"text": "outside"}
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.clipboard_copy",
        lambda text: clipboard.__setitem__("text", text),
    )
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.platform_helpers.clipboard_paste",
        lambda: clipboard["text"],
    )
    app = ChrysApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    insert_bindings = {
        binding.key: (binding.action, binding.priority, binding.show)
        for binding in ChrysApp.BINDINGS
        if "insert" in binding.key
    }
    assert insert_bindings == {
        "ctrl+insert": ("copy_with_insert", False, False),
        "shift+insert": ("paste_with_insert", False, False),
    }

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._main_screen is not None
        editor = app._main_screen.query_one("#chat-input", TextArea)
        editor.text = "focused selection"
        editor.select_all()
        editor.focus()
        await wait_for(lambda: editor.has_focus, pilot=pilot, description="control focus before interaction")

        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "focused selection"
        assert app.clipboard == "focused selection"

        editor.move_cursor((0, len(editor.text)))
        monkeypatch.setattr(app._main_screen, "get_selected_text", lambda: "rendered selection")
        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "rendered selection"
        assert app.clipboard == "rendered selection"

        editor.text = ""
        clipboard["text"] = "pasted\r\ntext"
        await pilot.press("shift+insert")
        await pilot.pause()

        assert editor.text == "pasted\ntext"


@pytest.mark.asyncio
async def test_insert_clipboard_shortcuts_reach_focused_embedded_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Terminal handles Insert clipboard keys instead of forwarding their ANSI codes."""
    from chrys.app.tui.terminal.widget import Terminal

    class _Engine:
        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    class _TerminalScreen(Screen):
        def compose(self) -> ComposeResult:
            yield Terminal()

    class _TerminalApp(ChrysApp):
        CSS_PATH = None

        def _build_main_screen(self) -> Screen:
            return _TerminalScreen()

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: "shell paste")
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
    app = _TerminalApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        terminal = app._main_screen.query_one(Terminal)
        terminal.emulator.feed("\x1b[?2004h")
        writes: list[str] = []

        async def write_stdin(text: str) -> None:
            writes.append(text)

        terminal.set_write_to_stdin(write_stdin)
        terminal.focus()
        await wait_for(lambda: terminal.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("shift+insert")
        await pilot.pause()

        assert writes == ["\x1b[200~shell paste\x1b[201~"]

        # Unlike Ctrl+C, Ctrl+Insert without a selection never interrupts the shell.
        await pilot.press("ctrl+insert")
        await pilot.pause()

        await wait_for_clipboard(app)
        assert copied == []
        assert writes == ["\x1b[200~shell paste\x1b[201~"]

        monkeypatch.setattr(app._main_screen, "get_selected_text", lambda: "shell selection")
        await pilot.press("ctrl+insert")
        await pilot.pause()

        await wait_for_clipboard(app)
        assert copied == ["shell selection"]
        assert app.clipboard == "shell selection"
        assert writes == ["\x1b[200~shell paste\x1b[201~"]


@pytest.mark.asyncio
async def test_insert_clipboard_shortcuts_fall_back_to_app_actions_for_stock_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Controls without their own Insert bindings reach the app-level copy and paste actions."""

    class _Engine:
        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    class _StockScreen(Screen):
        def compose(self) -> ComposeResult:
            yield Input(value="stock value", id="stock-input", select_on_focus=False)

    class _StockApp(ChrysApp):
        CSS_PATH = None

        def _build_main_screen(self) -> Screen:
            return _StockScreen()

    clipboard = {"text": "outside"}
    screen_selection: dict[str, str | None] = {"text": None}
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.clipboard_copy",
        lambda text: clipboard.__setitem__("text", text),
    )
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.platform_helpers.clipboard_paste",
        lambda: clipboard["text"],
    )
    app = _StockApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        monkeypatch.setattr(app._main_screen, "get_selected_text", lambda: screen_selection["text"])
        stock_input = app._main_screen.query_one("#stock-input", Input)
        stock_input.focus()
        await wait_for(lambda: stock_input.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        # Nothing selected anywhere: the clipboards stay untouched.
        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "outside"
        assert app.clipboard == ""

        stock_input.select_all()
        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "stock value"
        assert app.clipboard == "stock value"

        stock_input.clear()
        screen_selection["text"] = "rendered selection"
        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "rendered selection"
        assert app.clipboard == "rendered selection"

        # Shift+Insert reaches the stock Input through the same Paste event the driver posts.
        clipboard["text"] = "pasted value"
        await pilot.press("shift+insert")
        await pilot.pause()

        assert stock_input.value == "pasted value"


def test_app_routes_diagram_open_request_to_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from chrys.app.tui.screens.dialogs.diagram import DiagramDialog
    from chrys.app.tui.widgets.markdown.diagram import compile_mermaid
    from chrys.app.tui.widgets.markdown.diagram.messages import DiagramOpenRequested

    app = _app_with_loaded_settings(
        tmp_path,
        LoadedSettings(settings=Settings(), provenance={}),
    )
    pushed: list[Screen] = []

    def capture_screen(screen: Screen) -> None:
        pushed.append(screen)

    monkeypatch.setattr(app, "push_screen", capture_screen)
    diagram = compile_mermaid("flowchart LR\nA --> B")

    event = DiagramOpenRequested(diagram)
    app.on_diagram_open_requested(event)

    assert len(pushed) == 1
    assert isinstance(pushed[0], DiagramDialog)
    assert pushed[0].diagram is diagram


def test_notification_focus_handlers_use_chrys_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Chrys notification state must not collide with Textual's internal notification manager."""
    from textual import events

    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(theme="chrys"),
        state_store=JsonFileStateStore(tmp_path),
    )

    assert isinstance(app.notification_service, NotificationService)

    app._on_app_blur(events.AppBlur())
    assert app.notification_service._focus_known is True
    assert app.notification_service._focused is False
    app._on_app_focus(events.AppFocus())
    assert app.notification_service._focused is True

    assert isinstance(app.notification_service, NotificationService)


def test_a_saved_theme_that_no_longer_resolves_is_not_written_back(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Falling back is a decision about what to draw, not about what to save.

    A theme renamed between releases, a typo, a value some other writer
    damaged: persisting the fallback would delete the name the user chose and
    the next start would have nothing left to recover from. The user keeps the
    setting and loses only this session's colours — and a real switch
    afterwards still persists.
    """

    persisted: list[str] = []
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted.append)

    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(theme="retired-theme-name"),
        state_store=JsonFileStateStore(tmp_path),
    )

    assert app.theme == DEFAULT_THEME
    assert persisted == []

    app.theme = "chrys-legacy"

    assert persisted == ["chrys-legacy"]


def test_apply_theme_setting_persists_a_switch_exactly_once(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The Settings panel routes theme picks through one write point: the
    reactive assignment persists, so the setter itself must not."""

    persisted: list[str] = []
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted.append)
    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(theme="chrys"),
        state_store=JsonFileStateStore(tmp_path),
    )

    app.apply_theme_setting("chrys-legacy")
    assert app.theme == "chrys-legacy"
    assert persisted == ["chrys-legacy"]
    assert app.settings_handle.settings.theme == "chrys-legacy"

    # Re-applying the showing-and-saved theme is a no-op.
    app.apply_theme_setting("chrys-legacy")
    assert persisted == ["chrys-legacy"]

    # A name that is not a registered theme is ignored, not persisted.
    app.apply_theme_setting("no-such-theme")
    assert app.theme == "chrys-legacy"
    assert persisted == ["chrys-legacy"]


def test_apply_theme_setting_records_the_showing_fallback_when_the_user_picks_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A saved theme that no longer resolves fell back at startup; picking the
    fallback in the panel is a real choice, and the reactive will not fire
    for an equal value — so the setter persists and overlays it directly."""

    persisted: list[str] = []
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted.append)
    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(theme="retired-theme-name"),
        state_store=JsonFileStateStore(tmp_path),
    )
    assert app.theme == DEFAULT_THEME
    assert persisted == []

    app.apply_theme_setting(DEFAULT_THEME)

    assert persisted == [DEFAULT_THEME]
    assert app.settings_handle.settings.theme == DEFAULT_THEME


async def test_ctrl_q_over_the_settings_dialog_commits_a_focused_edit_before_exiting(tmp_path: Path) -> None:
    """Ctrl+Q is an app-level priority binding, so it fires with a modal on
    top; a value being typed in the Settings panel must still land."""

    from chrys.app.tui.screens.settings import SettingsDialog
    from tests.app.tui.screens.settings.support import StubPorts

    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())
    ports = StubPorts()

    async with app.run_test() as pilot:
        await pilot.pause()
        dialog = SettingsDialog(ports, initial_tab="sessions")
        await app.push_screen(dialog)
        initial_row = next(row for row in dialog.rows() if row.spec.key == "session.title.auto")
        initial = initial_row.query_one(Checkbox)
        await wait_for(lambda: initial.has_focus, pilot=pilot, description="initial sessions control focus")
        row = next(row for row in dialog.rows() if row.spec.key == "rollback.snapshots_keep")
        field = row.query_one(Input)
        field.focus()
        await wait_for(lambda: field.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()
        field.value = "7"
        await pilot.press("ctrl+q")
        await pilot.pause()

    assert app.return_code is not None
    assert ports.persisted == [{"rollback.snapshots_keep": 7}]
    assert ports.closed == 0


def test_editor_keymap_pick_records_a_runtime_override_on_the_shared_handle() -> None:
    """The keymap picker is a write point like theme and locale: the shared
    handle records the choice, and a later reload reapplies it instead of
    un-switching the visible keymap."""
    app = object.__new__(ChrysApp)
    app._settings_handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))

    app._record_editor_keymap_override("vim")

    assert app._settings_handle.settings.editor_keymap == "vim"
    app._settings_handle.install(LoadedSettings(settings=Settings(), provenance={}))
    assert app._settings_handle.settings.editor_keymap == "vim"
