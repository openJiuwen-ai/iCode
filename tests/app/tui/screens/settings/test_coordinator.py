# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SettingsCoordinator: projection, restart tracking and the close-time reload."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.notifications.settings import NotificationSettings
from chrys.app.tui.screens.main import settings_persistence
from chrys.app.tui.screens.main.settings_coordinator import SettingsCoordinator, SettingsCoordinatorCallbacks
from chrys.app.tui.screens.main.settings_persistence import SettingsPersistenceQueue
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.screens.settings import Suggestions
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, PersistResult, SettingsHandle
from chrys.foundation.config.spec import ChoiceProvider, SettingOrigin, Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SettingsReload, SettingsReloaded
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile

_RELOAD_KEY = "session.title.auto"
_RESTART_KEY = "otel.enabled"
_LIVE_KEY = "ui.theme"


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], tuple[str, ...]]] = []

    def persist(
        self, values: Mapping[str, Any], *, remove: Iterable[str] = (), lock_timeout: Any = None
    ) -> PersistResult:
        self.calls.append((dict(values), tuple(remove)))
        return PersistResult(written=dict(values), rejected={})


class _View:
    def __init__(self) -> None:
        self.pushed: list[tuple[object, object | None]] = []
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message: Any, *, title: Any, severity: str = "information", timeout: float | None = 3) -> None:
        self.notifications.append((format_message(message), severity))

    def push_screen(self, screen: object, callback: object | None = None) -> object:
        self.pushed.append((screen, callback))
        return None

    def run_worker(self, awaitable: Any, *, group: str) -> None:
        raise AssertionError("not used")

    def open_runtime_details(self, details: object) -> None:
        raise AssertionError("not used")

    def set_chat_profile(self, profile: str) -> None:
        raise AssertionError("not used")

    def set_status_profile(self, profile: str, *, description: str = "") -> None:
        raise AssertionError("not used")

    def focus_input(self) -> None:
        raise AssertionError("not used")


class _Dialog:
    def __init__(self) -> None:
        self.reprojections = 0

    def reproject(self) -> None:
        self.reprojections += 1


class _NotificationService:
    def __init__(self) -> None:
        self.settings = NotificationSettings()
        self.updated: list[NotificationSettings] = []
        self.tested: list[NotificationSettings] = []

    def update_settings(self, settings: NotificationSettings) -> None:
        self.updated.append(settings)
        self.settings = settings

    async def test(self, settings: NotificationSettings) -> bool:
        self.tested.append(settings)
        return True


class _Harness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Settings | None = None,
        *,
        provenance: Mapping[str, SettingOrigin] | None = None,
        model_registry: ModelProfileRegistry | None = None,
    ) -> None:
        self.recorder = _Recorder()
        monkeypatch.setattr(settings_persistence, "persist", self.recorder.persist)
        self.bus = EventBus()
        self.handle = SettingsHandle(LoadedSettings(settings=settings or Settings(), provenance=dict(provenance or {})))
        self.locale_controller = LocaleController(Settings(locale="zh-Hans"))
        self.view = _View()
        self.themes: list[str] = []
        self.locales: list[str] = []
        self.verify_commands: list[str] = []
        self.tool_groups_expanded: list[bool] = []
        self.defer_while_judging: list[bool] = []
        self.thinking_expanded: list[bool] = []
        self.fold_finished_turns: list[bool] = []
        self.saved_notifications: list[NotificationSettings] = []
        self.notification_service = _NotificationService()
        self.turn_task: asyncio.Task[None] | None = None
        self.turn_running = False
        self.rejected: list[PersistResult] = []
        self.failures: list[Exception] = []
        self.queue = SettingsPersistenceQueue(
            notify_failure=self.failures.append,
            notify_rejected=self.rejected.append,
            logger=logging.getLogger(__name__),
            on_written=self._on_written,
            save_delay_seconds=60,
            flush_lock_timeout_seconds=0.2,
        )
        self.coordinator = SettingsCoordinator(
            services=MainScreenServices(bus=self.bus, model_registry=model_registry),
            settings_handle=self.handle,
            queue=self.queue,
            view=cast(Any, self.view),
            locale_controller=self.locale_controller,
            callbacks=SettingsCoordinatorCallbacks(
                apply_theme=self.themes.append,
                switch_locale=self.locales.append,
                apply_trajectory_verify_commands=self.verify_commands.append,
                apply_tool_groups_expanded=self.tool_groups_expanded.append,
                apply_approval_defer_while_judging=self.defer_while_judging.append,
                apply_thinking_expanded=self.thinking_expanded.append,
                apply_fold_finished_turns=self.fold_finished_turns.append,
                list_themes=lambda: ["textual-dark", "chrys", "chrys-legacy"],
                save_notifications=self.saved_notifications.append,
                notification_service=lambda: self.notification_service,
                turn_lifecycle_task=lambda: self.turn_task,
                turn_in_progress=lambda: self.turn_running,
            ),
        )
        self.dialog = _Dialog()
        self.coordinator.attach_dialog(self.dialog)
        self.reloads = 0

    def _on_written(self, result: PersistResult) -> None:
        # The queue is built before the coordinator; resolve it late.
        self.coordinator.on_written(result)

    async def install_engine(self, *, reject: bool = False, reinstall: Settings | None = None) -> None:
        """A stand-in for the engine: reload inline, then echo ``SettingsReloaded``."""

        async def _on_reload(_event: SettingsReload) -> None:
            self.reloads += 1
            if reject:
                return
            if reinstall is not None:
                self.handle.install(LoadedSettings(settings=reinstall, provenance={}))
            await self.bus.publish(SettingsReloaded())

        async def _on_reloaded(_event: SettingsReloaded) -> None:
            self.coordinator.on_reloaded()

        await self.bus.subscribe(SettingsReload, _on_reload)
        await self.bus.subscribe(SettingsReloaded, _on_reloaded)

    async def close_and_settle(self) -> None:
        self.coordinator.on_dialog_closed()
        task = self.coordinator._finalize_task
        assert task is not None
        await task


def _written(**values: Any) -> PersistResult:
    return PersistResult(written=dict(values), rejected={})


async def test_live_keys_route_to_their_writers_and_never_to_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)

    h.coordinator.apply_live("ui.theme", "chrys-legacy")
    h.coordinator.apply_live("ui.locale", "zh-Hans")
    h.coordinator.apply_live("trajectory.verify_commands", "pytest,ruff")
    h.coordinator.apply_live("ui.chat.tool_groups_expanded", True)

    assert h.themes == ["chrys-legacy"]
    assert h.locales == ["zh-Hans"]
    assert h.verify_commands == ["pytest,ruff"]
    assert h.tool_groups_expanded == [True]
    assert h.recorder.calls == []
    # The locale row is reprojected after the switch: a bundle that failed to
    # load keeps the old locale in force and the row must show that.
    assert h.dialog.reprojections == 1
    with pytest.raises(ValueError, match="no live writer"):
        h.coordinator.apply_live(_RELOAD_KEY, False)
    # A LIVE key the panel does not render has no writer here either.
    with pytest.raises(ValueError, match="no live writer"):
        h.coordinator.apply_live("ui.editor.keymap", "vim")


async def test_the_approval_deferral_choice_applies_at_once_and_saves_without_a_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine()

    h.coordinator.apply_live("ui.approval.defer_while_judging", False)

    assert h.defer_while_judging == [False]
    assert h.recorder.calls == []

    await h.close_and_settle()

    assert h.recorder.calls == [({"ui.approval.defer_while_judging": False}, ())]
    assert h.reloads == 0
    assert h.coordinator.reload_dirty() is False


async def test_the_expand_thinking_choice_applies_at_once_and_saves_without_a_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine()

    h.coordinator.apply_live("ui.chat.show_thinking", True)

    assert h.thinking_expanded == [True]
    assert h.recorder.calls == []

    await h.close_and_settle()

    assert h.recorder.calls == [({"ui.chat.show_thinking": True}, ())]
    assert h.reloads == 0
    assert h.coordinator.reload_dirty() is False


async def test_process_folding_choice_applies_live_and_saves_without_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine()
    h.coordinator.apply_live("ui.chat.fold_finished_turns", False)
    assert h.fold_finished_turns == [False]
    await h.close_and_settle()
    assert h.recorder.calls == [({"ui.chat.fold_finished_turns": False}, ())]
    assert h.reloads == 0
    assert h.coordinator.reload_dirty() is False


async def test_written_values_project_until_the_live_settings_catch_up(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    assert h.coordinator.projected_value(_RELOAD_KEY) is True

    h.coordinator.on_written(_written(**{_RELOAD_KEY: False}))

    assert h.coordinator.projected_value(_RELOAD_KEY) is False
    assert h.coordinator.reload_dirty() is True
    assert h.dialog.reprojections == 1

    # The reload installs the new document: desired is pruned, projection reads live.
    h.handle.install(LoadedSettings(settings=Settings(session_title_auto=False), provenance={}))
    h.coordinator.on_reloaded()
    assert h.coordinator._desired == {}
    assert h.coordinator.projected_value(_RELOAD_KEY) is False


async def test_search_ignore_setting_reloads_on_dialog_close(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "tools.search.respect_gitignore"
    h = _Harness(monkeypatch)
    await h.install_engine(reinstall=Settings(search_respect_gitignore=False))
    h.coordinator.schedule_persist({key: False})
    try:
        await h.close_and_settle()
        assert h.reloads == 1
        assert h.handle.settings.search_respect_gitignore is False
        assert h.coordinator.projected_value(key) is False
    finally:
        await h.queue.flush()


async def test_closed_dialog_is_not_reprojected_when_reload_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.coordinator.on_dialog_closed()
    finalize = h.coordinator._finalize_task
    assert finalize is not None

    h.coordinator.on_reloaded()
    await finalize

    assert h.dialog.reprojections == 0


async def test_greyed_layers_project_the_value_in_force_and_drop_desired(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(
        monkeypatch, Settings(session_title_auto=True), provenance={_RELOAD_KEY: SettingOrigin(layer=Source.ENV)}
    )

    h.coordinator.on_written(_written(**{_RELOAD_KEY: False}))

    assert h.coordinator.projected_value(_RELOAD_KEY) is True
    assert h.coordinator._desired == {}


async def test_restart_keys_are_pending_only_while_they_differ_from_the_process_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch)
    assert h.coordinator.restart_in_force[_RESTART_KEY] is False
    assert h.coordinator.restart_pending_keys() == frozenset()

    h.coordinator.on_written(_written(**{_RESTART_KEY: True}))
    assert h.coordinator.restart_pending_keys() == frozenset({_RESTART_KEY})
    # A RESTART write is not a reload.
    assert h.coordinator.reload_dirty() is False

    # Writing the process value back clears the pending badge again.
    h.coordinator.on_written(_written(**{_RESTART_KEY: False}))
    assert h.coordinator.restart_pending_keys() == frozenset()

    # Even a reload that installs the new value cannot make it "in force":
    # restart_in_force is captured once and never moves.
    h.coordinator.on_written(_written(**{_RESTART_KEY: True}))
    h.handle.install(LoadedSettings(settings=Settings(otel_enabled=True), provenance={}))
    h.coordinator.on_reloaded()
    assert h.coordinator.restart_in_force[_RESTART_KEY] is False
    assert h.coordinator.projected_value(_RESTART_KEY) is True


async def test_external_writes_project_like_our_own_and_live_keys_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)

    h.coordinator.note_external_write("approval.default_mode", "auto")
    h.coordinator.note_external_write(_LIVE_KEY, "chrys-legacy")

    assert h.coordinator.projected_value("approval.default_mode") == "auto"
    assert h.coordinator._desired == {"approval.default_mode": "auto"}
    assert h.dialog.reprojections == 2


async def test_closing_flushes_then_publishes_one_reload_and_acks_it(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine(reinstall=Settings(session_title_auto=False))
    h.coordinator.schedule_persist({_RELOAD_KEY: False})
    assert h.recorder.calls == []

    await h.close_and_settle()

    assert h.recorder.calls == [({_RELOAD_KEY: False}, ())]
    assert h.reloads == 1
    assert h.coordinator.reload_dirty() is False
    assert h.coordinator._desired == {}
    assert h.coordinator._finalize_task is None


async def test_closing_without_reload_edits_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine()
    h.coordinator.schedule_persist({_RESTART_KEY: True})

    await h.close_and_settle()

    assert h.recorder.calls == [({_RESTART_KEY: True}, ())]
    assert h.reloads == 0
    assert h.coordinator.restart_pending_keys() == frozenset({_RESTART_KEY})


async def test_a_rejected_reload_keeps_the_dirty_flag_for_the_next_close(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine(reject=True)
    h.coordinator.schedule_persist({_RELOAD_KEY: False})

    await h.close_and_settle()
    assert h.reloads == 1
    assert h.coordinator.reload_dirty() is True
    assert h.coordinator.projected_value(_RELOAD_KEY) is False

    # A reload the engine did not ack is retried the next time the dialog closes.
    await h.close_and_settle()
    assert h.reloads == 2


async def test_finalize_waits_for_the_running_turn_before_reloading(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine(reinstall=Settings(session_title_auto=False))
    release = asyncio.Event()

    async def _turn() -> None:
        await release.wait()

    h.turn_task = asyncio.create_task(_turn())
    h.turn_running = True
    h.coordinator.schedule_persist({_RELOAD_KEY: False})
    h.coordinator.on_dialog_closed()
    finalize = h.coordinator._finalize_task
    assert finalize is not None
    await asyncio.sleep(0.05)
    # The write already landed, the reload waits for the turn.
    assert h.recorder.calls == [({_RELOAD_KEY: False}, ())]
    assert h.reloads == 0
    assert h.coordinator.turn_in_progress() is True

    release.set()
    await finalize
    assert h.reloads == 1
    assert h.coordinator.reload_dirty() is False


async def test_a_reclose_during_finalize_reloads_again_when_more_edits_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    await h.install_engine()
    release = asyncio.Event()

    async def _turn() -> None:
        await release.wait()

    h.turn_task = asyncio.create_task(_turn())
    h.coordinator.schedule_persist({_RELOAD_KEY: False})
    h.coordinator.on_dialog_closed()
    finalize = h.coordinator._finalize_task
    assert finalize is not None
    await asyncio.sleep(0.05)
    # The dialog was reopened and closed again with a second reload edit while
    # the first close still waited on the turn: still one finalize task.
    h.coordinator.schedule_persist({"project.config_enabled": True})
    h.coordinator.on_dialog_closed()
    assert h.coordinator._finalize_task is finalize

    release.set()
    await finalize
    assert h.recorder.calls == [({_RELOAD_KEY: False}, ()), ({"project.config_enabled": True}, ())]
    # Both edits were flushed before the reload, so one reload covered them.
    assert h.reloads == 1
    assert h.coordinator.reload_dirty() is False


async def test_startup_only_keys_are_written_but_never_trigger_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default agent and approval mode are read at startup: a rebuild would apply nothing."""
    h = _Harness(monkeypatch)
    await h.install_engine()
    h.coordinator.schedule_persist({"agent.default_profile": "reviewer"})
    h.coordinator.schedule_persist({"approval.default_mode": "auto"})

    await h.close_and_settle()

    assert h.recorder.calls == [({"agent.default_profile": "reviewer", "approval.default_mode": "auto"}, ())]
    assert h.reloads == 0
    assert h.coordinator.reload_dirty() is False
    # The saved values still project until a later reload lets ``loaded`` catch up.
    assert h.coordinator.projected_value("agent.default_profile") == "reviewer"
    assert h.coordinator.projected_value("approval.default_mode") == "auto"

    # Together with a real reload key they ride along in that reload.
    h.coordinator.schedule_persist({_RELOAD_KEY: False})
    await h.close_and_settle()
    assert h.reloads == 1


async def test_reopening_during_the_turn_defers_the_reload_to_the_next_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finalize started by an earlier close must not reload under a reopened panel."""
    h = _Harness(monkeypatch)
    await h.install_engine()
    release = asyncio.Event()

    async def _turn() -> None:
        await release.wait()

    h.turn_task = asyncio.create_task(_turn())
    h.coordinator.schedule_persist({_RELOAD_KEY: False})
    h.coordinator.on_dialog_closed()
    finalize = h.coordinator._finalize_task
    assert finalize is not None
    await asyncio.sleep(0.05)
    # Reopened while the turn still runs, and edited again.
    h.coordinator.attach_dialog(h.dialog)
    h.coordinator.schedule_persist({"project.config_enabled": True})

    release.set()
    await finalize
    # The turn ended with the panel open: nothing was applied, the flag stays.
    assert h.reloads == 0
    assert h.coordinator.reload_dirty() is True
    assert h.coordinator._finalize_task is None

    # Closing the reopened panel reloads once, covering both edits.
    await h.close_and_settle()
    assert h.recorder.calls == [({_RELOAD_KEY: False}, ()), ({"project.config_enabled": True}, ())]
    assert h.reloads == 1
    assert h.coordinator.reload_dirty() is False


async def test_reopening_during_the_final_flush_defers_the_reload_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flush before the reload can wait on disk; a reopen during it must still defer."""
    h = _Harness(monkeypatch)
    await h.install_engine()
    real_flush = h.coordinator._flush
    flushes = 0
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def slow_flush() -> None:
        nonlocal flushes
        flushes += 1
        if flushes == 2:
            entered.set()
            await gate.wait()
        await real_flush()

    monkeypatch.setattr(h.coordinator, "_flush", slow_flush)
    h.coordinator.schedule_persist({_RELOAD_KEY: False})
    h.coordinator.on_dialog_closed()
    finalize = h.coordinator._finalize_task
    assert finalize is not None
    await entered.wait()
    # Reopened while the second flush is still waiting.
    h.coordinator.attach_dialog(h.dialog)
    gate.set()
    await finalize

    assert h.reloads == 0
    assert h.coordinator.reload_dirty() is True
    assert h.coordinator._finalize_task is None

    await h.close_and_settle()
    assert h.reloads == 1
    assert h.coordinator.reload_dirty() is False


async def test_confirm_pushes_a_warning_confirm_dialog_and_relays_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    h = _Harness(monkeypatch)
    answers: list[bool] = []

    message = msg(
        "tui.settings.confirm.raw_http_capture",
        fallback=(
            "This writes raw model HTTP requests and responses in plain text to <session>/llm_raw_http.jsonl,"
            " including sensitive data in the requests such as API keys. Continue?"
        ),
    ).bind()
    h.coordinator.confirm(message, answers.append)

    assert len(h.view.pushed) == 1
    dialog, callback = h.view.pushed[0]
    assert isinstance(dialog, ConfirmDialog)
    assert dialog._title == "确认设置"
    assert dialog._message == (
        "这会将模型的原始 HTTP 请求和响应以明文写入 <session>/llm_raw_http.jsonl，包括请求中的 API 密钥等敏感信息。是否继续？"  # noqa: RUF001
    )
    assert dialog._confirm_label == "继续"
    assert dialog._cancel_label == "取消"
    assert callable(callback)
    callback(None)
    callback(True)
    assert answers == [False, True]


async def test_choices_and_notification_ports_come_from_the_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    assert h.coordinator.resolve_choices(ChoiceProvider.THEMES) == [
        ("chrys", "chrys"),
        ("chrys-legacy", "chrys-legacy"),
        ("textual-dark", "textual-dark"),
    ]
    # No registries on the services: empty suggestion lists, never an error.
    assert h.coordinator.resolve_choices(Suggestions.AGENT_PROFILES) == []
    assert h.coordinator.resolve_choices(Suggestions.MODEL_PROFILES) == []

    ports = h.coordinator.notifications()
    edited = NotificationSettings(enabled=False)
    assert ports.current() == NotificationSettings()
    assert ports.save(edited) is True
    assert h.notification_service.updated == [edited]
    assert h.saved_notifications == [edited]
    assert ports.current() == edited
    assert await ports.test(edited) is True
    assert h.notification_service.tested == [edited]

    h.coordinator.notify_error(msg("tui.test.error", fallback="Nope").bind())
    assert h.view.notifications == [("Nope", "error")]


async def test_model_choices_come_from_the_registry_by_profile_id_and_by_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="p1", name="One", model_id="vendor/one"))
    registry.register(ModelProfile(id="p2", name="Two", model_id="vendor/two"))
    registry.register(ModelProfile(id="p3", name="Two again", model_id="vendor/two"))
    registry.register(ModelProfile(id="p4", name="Unfinished"))
    h = _Harness(monkeypatch, model_registry=registry)
    assert h.coordinator.resolve_choices(Suggestions.MODEL_PROFILES) == [
        ("p1", "One"),
        ("p2", "Two"),
        ("p3", "Two again"),
        ("p4", "Unfinished"),
    ]
    # Bare model ids: deduplicated, blank ones skipped, first-seen order.
    assert h.coordinator.resolve_choices(Suggestions.MODEL_IDS) == [
        ("vendor/one", "vendor/one"),
        ("vendor/two", "vendor/two"),
    ]
    assert h.coordinator.resolve_choices(Suggestions.AGENT_PROFILES) == []


async def test_session_storage_port_probes_and_plans_through_the_foundation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    h = _Harness(monkeypatch)
    storage = h.coordinator.session_storage()

    good = storage.probe_root(str(tmp_path / "root"))
    assert good == tmp_path / "root" / "sessions"
    assert good.is_dir()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert storage.probe_root(str(blocker)) is None

    session_dir = tmp_path / "root" / "sessions" / "abc123"
    session_dir.mkdir()
    (session_dir / "session.json").write_text("{}")
    plan = storage.plan_migration(tmp_path / "root" / "sessions", tmp_path / "next" / "sessions")
    assert [item.session_id for item in plan.items] == ["abc123"]
    report = await storage.run_migration(plan)
    assert report.copied == ("abc123",)
    assert (tmp_path / "next" / "sessions" / "abc123").is_dir()


async def test_an_external_reload_prunes_desired_but_keeps_the_dirty_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``SettingsReloaded`` the finalize task did not cause (a model pick, an
    ``/approval`` change) reprojects, but is no evidence our edit was reloaded."""
    h = _Harness(monkeypatch)
    h.coordinator.on_written(_written(**{_RELOAD_KEY: False}))
    assert h.coordinator.reload_dirty() is True

    h.handle.install(LoadedSettings(settings=Settings(session_title_auto=False), provenance={}))
    h.coordinator.on_reloaded()

    assert h.coordinator._desired == {}
    assert h.coordinator.reload_dirty() is True
    assert h.dialog.reprojections == 2
