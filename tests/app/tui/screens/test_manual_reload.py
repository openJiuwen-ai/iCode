# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manual settings reloads in the real TUI, through the next model request."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.runtime_pointer import get_model_pointer
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.events import types as ev
from chrys.service.profiles.models.env_bridge import activate_model_profile
from tests.app.tui.screens.test_prompt_shortcut_send import (
    FAST_MODEL,
    MAIN_MODEL,
    Session,
    error_cards,
    notices,
    running_session,
    send_plain,
    turn_finished,
    type_text,
)
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")


async def slash_reload(session: Session) -> None:
    await type_text(session.pilot, "/reload")
    await session.pilot.press("enter")


async def ask_after_reload(session: Session, prompt: str) -> None:
    await wait_for(
        lambda: session.app.screen is session.main and not session.bar.agent_loading,
        pilot=session.pilot,
        description="main screen ready",
    )
    assert not session.bar.retry_mode
    assert not error_cards(session.main)
    session.bar.focus_input()
    await wait_for(lambda: session.chat.has_focus, pilot=session.pilot, description="chat input focused")
    await send_plain(session, prompt)
    await turn_finished(session)
    assert prompt in session.model.last_user_text()
    assert not session.seen.of(ev.UserRetry)


async def reload_report(session: Session) -> dict[str, str]:
    """Run ``/reload`` from a ready screen; return its report lines by message key."""
    from chrys.foundation.i18n.formatting import format_message

    before = len(session.seen.of(ev.SettingsReloaded))
    await wait_for(
        lambda: session.app.screen is session.main and not session.bar.agent_loading,
        pilot=session.pilot,
        description="ready for /reload",
    )
    session.bar.focus_input()
    await wait_for(lambda: session.chat.has_focus, pilot=session.pilot, description="chat focused")
    await slash_reload(session)
    await wait_for(
        lambda: len(session.seen.of(ev.SettingsReloaded)) > before,
        pilot=session.pilot,
        description="manual report",
    )
    return {ref.definition.key: format_message(ref) for ref in session.seen.of(ev.SettingsReloaded)[-1].report}


def settings_path() -> Path:
    freeze_process_env()
    path = user_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def test_broken_settings_and_subsequent_repair_leave_chat_usable(agent_engine, tmp_path, monkeypatch):
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        path.write_text("ui: [broken", encoding="utf-8")
        await slash_reload(session)
        await wait_for(
            lambda: any(str(path) in m for _, m in notices(session.app)),
            pilot=session.pilot,
            description="settings error shown in a toast",
        )
        errors = [n for n in session.app._notifications if str(path) in str(n.message)]
        assert [n.timeout for n in errors] == [8]
        assert path.read_text(encoding="utf-8") == "ui: [broken"
        await ask_after_reload(session, "after broken settings")

        path.write_text(f"model.profile.active: {MAIN_MODEL}\n", encoding="utf-8")
        await slash_reload(session)
        await wait_for(
            lambda: any("Reloaded settings" in m for _, m in notices(session.app)),
            pilot=session.pilot,
            description="reload completed",
        )
        await ask_after_reload(session, "after repaired settings")


async def test_turn_started_during_panel_finalization_survives_reload_denial(agent_engine, tmp_path, monkeypatch):
    from chrys.app.tui.terminal import program_status

    settings_path()
    writes = []
    monkeypatch.setattr(program_status, "app_terminal_title_writer", lambda app: writes.append)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        coordinator = session.main._settings_coordinator()
        finalizing = session.hold()
        coordinator._finalize_task = asyncio.create_task(finalizing.pass_through())
        waiting = asyncio.Event()
        join = coordinator.wait_for_finalize

        async def join_finalize(baseline):
            waiting.set()
            return await join(baseline)

        monkeypatch.setattr(coordinator, "wait_for_finalize", join_finalize)
        await slash_reload(session)
        await wait_for(waiting.is_set, pilot=session.pilot, description="worker joined outstanding panel write")
        assert not session.seen.of(ev.SettingsReload)
        running = session.hold()
        session.model.hold = running
        await send_plain(session, "while settings wait")
        await wait_for(running.entered.is_set, pilot=session.pilot, description="model request in flight")
        assert "state=working" in writes[-1]
        report_count = len(writes)
        finalizing.release()
        await wait_for(
            lambda: any(e.code == "manual_settings_reload_denied" for e in session.seen.of(ev.Error)),
            pilot=session.pilot,
            description="backend refused only the reload",
        )
        await wait_for(
            lambda: any("workflow" in m.lower() for _, m in notices(session.app)),
            pilot=session.pilot,
            description="busy notice displayed",
        )
        assert "state=working" in writes[-1]
        assert not any("state=error" in report for report in writes[report_count:])
        assert session.bar.agent_running and session.engine.is_running
        assert not session.bar.retry_mode and not error_cards(session.main)
        running.release()
        await turn_finished(session)
        await ask_after_reload(session, "after a busy reload")


async def test_workflow_blocks_reload_without_putting_chat_in_retry(agent_engine, tmp_path, monkeypatch):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        lease = session.engine.turns.turn_state.lease
        workflow = lease.begin_workflow("active-run", "request")
        try:
            await slash_reload(session)
            await wait_for(
                lambda: any("workflow" in m.lower() for _, m in notices(session.app)),
                pilot=session.pilot,
                description="workflow busy notice displayed",
            )
            assert lease.workflow is workflow
            assert not session.seen.of(ev.SettingsReload)
            assert not session.bar.retry_mode and not error_cards(session.main)
        finally:
            lease.end_workflow(workflow)
        await ask_after_reload(session, "after workflow")


async def test_manual_model_continues_through_chdir_and_model_dialog(agent_engine, tmp_path, monkeypatch):
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    from tests.app.tui.screens.test_models_dialog_deletion import _models_on_disk

    async with running_session(agent_engine, tmp_path, monkeypatch, models=_models_on_disk()) as session:
        path.write_text(f"model.profile.active: {FAST_MODEL}\n", encoding="utf-8")
        await slash_reload(session)
        await wait_for(lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="model reloaded")
        other = tmp_path / "second-workspace"
        other.mkdir()
        await session.bus.publish(ev.WorkspaceChange(primary_cwd=str(other)), raise_handler_errors=True)
        assert session.engine.settings.model_profile == get_model_pointer()[0] == FAST_MODEL
        await wait_for(
            lambda: session.app.screen is session.main and not session.bar.agent_loading,
            pilot=session.pilot,
            description="workspace changed",
        )
        await session.pilot.press("f4")
        await wait_for(
            lambda: isinstance(session.app.screen, ModelConfigScreen) and session.app.screen.is_mounted,
            pilot=session.pilot,
            description="models page opened",
        )
        assert session.app.screen._global_default_profile_id == FAST_MODEL
        await session.pilot.press("escape")
        await ask_after_reload(session, "after changing workspace")
        assert session.builds.profiles[-1] == FAST_MODEL


async def test_deleted_default_with_unwritable_settings_still_chooses_a_model_and_rebuilds(
    agent_engine, tmp_path, monkeypatch
):
    from tests.app.tui.screens.test_models_dialog_deletion import _delete_in_the_models_dialog, _models_on_disk

    path = settings_path()
    models = _models_on_disk()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch, models=models) as session:
        path.write_text("ui: [broken", encoding="utf-8")
        await _delete_in_the_models_dialog(session, MAIN_MODEL)
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="deletion rebuilt"
        )
        assert models.get(MAIN_MODEL) is None
        active = get_model_pointer()[0]
        assert models.get(active) is not None
        assert session.builds.profiles[-1] == active
        assert path.read_text(encoding="utf-8") == "ui: [broken"
        await ask_after_reload(session, "after deleting default")


async def test_manual_model_commits_only_after_success_and_keeps_the_session_model(agent_engine, tmp_path, monkeypatch):
    from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
    from tests.app.tui.screens.test_prompt_shortcut_send import BROKEN_MODEL

    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        previous = session.engine.current.require_loaded()
        session.builds.failing.add(BROKEN_MODEL)
        path.write_text(f"model.profile.active: {BROKEN_MODEL}\n", encoding="utf-8")
        await slash_reload(session)
        await wait_for(
            lambda: session.seen.of(ev.AgentLoadFailed) and isinstance(session.app.screen, AgentLoadDialog),
            pilot=session.pilot,
            description="candidate build failed",
        )
        assert get_model_pointer()[0] == MAIN_MODEL
        assert session.engine.current.require_loaded() is previous
        await session.pilot.press("escape")
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot, description="failure dismissed")
        await ask_after_reload(session, "after failed model build")
        assert session.engine.current_profile_snapshot().model_profile_id == MAIN_MODEL
        request = ev.SessionOnlySwitch(model_profile=FAST_MODEL)
        await session.bus.publish(request, raise_handler_errors=True)
        assert request.applied
        session.builds.failing.clear()
        await wait_for(
            lambda: session.app.screen is session.main and not session.bar.agent_loading,
            pilot=session.pilot,
            description="session selection finished",
        )
        await session.bus.publish(
            ev.SettingsReload(manual=True, session_id=session.session_id()), raise_handler_errors=True
        )
        assert get_model_pointer()[0] == BROKEN_MODEL
        assert session.engine.current_profile_snapshot().model_profile_id == FAST_MODEL
        await ask_after_reload(session, "after the session kept its model")
        assert session.builds.profiles[-1] == FAST_MODEL


async def test_reload_reports_applied_deferred_and_conflicting_values_once(agent_engine, tmp_path, monkeypatch):
    from chrys.foundation.config.process_settings import install_process_settings
    from chrys.foundation.config.settings_store import load_settings
    from tests.support.event_capture import capture_events

    monkeypatch.delenv("CHRYS_OTEL", raising=False)
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    install_process_settings(load_settings())
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        warnings = await capture_events(session.bus, ev.Warning)
        session.engine.settings_handle.override(chat_fold_finished_turns=True)
        path.write_text(
            f"model.profile.active: {MAIN_MODEL}\nui.theme: nord\nui.chat.fold_finished_turns: true\n"
            "notifications.enabled: false\ntrajectory.verify_commands: mycheck\notel.enabled: true\n"
            "agent.default_profile: QA\napproval.default_mode: auto\n",
            encoding="utf-8",
        )

        other = tmp_path / "summary-workspace"
        other.mkdir()
        await session.bus.publish(ev.WorkspaceChange(primary_cwd=str(other)), raise_handler_errors=True)
        lines = await reload_report(session)
        assert session.app.theme == "nord"
        assert "ui.theme" in lines["controls.reload_applied"]
        assert "ui.theme" not in lines["controls.reload_next_launch"]
        assert "notifications.enabled" in lines["controls.reload_applied"]
        assert "trajectory.verify_commands" in lines["controls.reload_applied"]
        assert "otel.enabled" in lines["controls.settings_restart_required"]
        assert all(warning.code != "settings_restart_required" for warning in warnings)
        assert "controls.reload_shadowed" not in lines
        await wait_for(
            lambda: any("ui.theme" in m for _, m in notices(session.app)),
            pilot=session.pilot,
            description="applied theme shown",
        )
        assert session.app.notification_service.settings.enabled is False
        assert session.main._trajectory_verify_commands == "mycheck"
        await session.bus.publish(ev.SettingsReload(), raise_handler_errors=True)
        pending = (await reload_report(session))["controls.reload_next_launch"]
        assert all(key in pending for key in ("agent.default_profile", "approval.default_mode"))

        path.write_text(
            path.read_text(encoding="utf-8").replace("fold_finished_turns: true", "fold_finished_turns: false"),
            encoding="utf-8",
        )
        assert "ui.chat.fold_finished_turns" in (await reload_report(session))["controls.reload_shadowed"]
        path.write_text("ui.theme: gone-theme\n", encoding="utf-8")
        lines = await reload_report(session)
        assert get_model_pointer()[0] == ""
        assert "gone-theme" in lines["controls.reload_unknown_theme"]
        assert "ui.theme" not in lines.get("controls.reload_applied", "")
        assert session.app.theme == "nord"


async def test_reload_waits_for_the_finished_turn_to_save(agent_engine, tmp_path, monkeypatch):
    from chrys.service.state.store import JsonFileStateStore

    settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        saving = session.hold()
        original = JsonFileStateStore.save_session

        async def save(store, *args, **kwargs):
            await saving.pass_through()
            return await original(store, *args, **kwargs)

        monkeypatch.setattr(JsonFileStateStore, "save_session", save)
        await send_plain(session, "finish before reload")
        await wait_for(
            lambda: saving.entered.is_set() and not session.bar.agent_running and not session.engine.is_running,
            pilot=session.pilot,
            description="turn finished, save pending",
        )
        draining = asyncio.Event()
        state = session.engine.turns.turn_state
        original_drain = state.drain_for_boundary

        async def drain():
            draining.set()
            return await original_drain()

        monkeypatch.setattr(state, "drain_for_boundary", drain)
        await slash_reload(session)
        await wait_for(draining.is_set, pilot=session.pilot, description="rebuild waiting on the owned turn")
        assert not saving.released.is_set()
        assert not session.seen.of(ev.SettingsReloaded)
        assert not session.seen.of(ev.Error)
        saving.release()
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="reload after save"
        )
        await ask_after_reload(session, "after the save")


@pytest.mark.parametrize("launch", ["cli", "exported", "default-only"])
async def test_no_file_edit_preserves_the_model_used_by_the_next_request(agent_engine, tmp_path, monkeypatch, launch):
    from chrys.foundation.config.runtime_pointer import set_model_pointer
    from chrys.foundation.config.spec import SettingOrigin, Source
    from chrys.orchestration.startup import bootstrap_runtime
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    if launch == "exported":
        monkeypatch.setenv("CHRYS_MODEL_PROFILE", MAIN_MODEL)
    set_global_default_profile_id(MAIN_MODEL)
    bootstrap_runtime(apply_patches=False, setup_telemetry=False)
    if launch == "cli":
        set_model_pointer(MAIN_MODEL, origin=SettingOrigin(layer=Source.CLI))
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        if launch == "default-only":
            set_global_default_profile_id(FAST_MODEL)
            expected = MAIN_MODEL
        else:
            await session.main._config_actions.on_model_picked(FAST_MODEL)
            expected = FAST_MODEL
        await wait_for(
            lambda: session.app.screen is session.main and not session.bar.agent_loading,
            pilot=session.pilot,
            description="model page change settled",
        )
        session.bar.focus_input()
        await wait_for(lambda: session.chat.has_focus, pilot=session.pilot, description="main input focused")
        await slash_reload(session)
        await wait_for(
            lambda: any("Reloaded settings" in m for _, m in notices(session.app)),
            pilot=session.pilot,
            description="manual reload completed",
        )
        await ask_after_reload(session, "same choice after reload")
        assert get_model_pointer()[0] == session.builds.profiles[-1] == expected


@pytest.mark.parametrize("during_flush", [False, True], ids=["awaiting-finalizer", "flushing-writes"])
async def test_panel_finalize_failure_stops_only_this_reload(agent_engine, tmp_path, monkeypatch, during_flush):
    settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        coordinator = session.main._settings_coordinator()
        held = session.hold()
        joining = asyncio.Event()
        wait_for_finalize = coordinator.wait_for_finalize

        async def finalize():
            await held.pass_through()
            raise RuntimeError("injected panel failure")

        async def join(baseline):
            joining.set()
            return await wait_for_finalize(baseline)

        flushing = session.hold()
        queue = session.main._settings_persistence()
        flush = queue.flush

        async def await_flush(**kwargs):
            await flushing.pass_through()
            return await flush(**kwargs)

        if during_flush:
            monkeypatch.setattr(queue, "flush", await_flush)

        monkeypatch.setattr(coordinator, "_finalize", finalize)
        monkeypatch.setattr(coordinator, "wait_for_finalize", join)
        coordinator.on_dialog_closed()
        await slash_reload(session)
        await wait_for(
            flushing.entered.is_set if during_flush else joining.is_set,
            pilot=session.pilot,
            description="reload waiting",
        )
        held.release()
        if during_flush:
            await wait_for(
                lambda: coordinator._finalize_task is None, pilot=session.pilot, description="failed finalizer cleared"
            )
            flushing.release()
        await wait_for(
            lambda: any("injected panel failure" in text for _, text in notices(session.app)),
            pilot=session.pilot,
            description="panel failure reported",
        )
        assert not session.seen.of(ev.SettingsReload)
        await ask_after_reload(session, "after panel failure")
        await slash_reload(session)
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="a later reload succeeds"
        )
