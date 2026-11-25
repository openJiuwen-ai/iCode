# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An interactive restore takes a real session lock from another idle TUI process."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from textual.widgets import Button

from chrys.app.tui.screens.dialogs import SessionInUseDialog
from chrys.app.tui.session_takeover import TakeoverListener, query_owner
from chrys.app.tui.widgets.sidebar.buddy import BuddyPanel
from chrys.foundation.platform.process import kill_windows_process_tree
from chrys.kernel import Message
from chrys.service.profiles.models.serializer import save_profile
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore, session_active_owner_path
from chrys.service.state.takeover import TakeoverOwner, credential_path
from tests.app.tui.screens.test_prompt_shortcut_send import ALPHA, MAIN_MODEL, mock_model, running_session
from tests.support.symlinks import symlink_or_skip
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from tests.support.engines import AgentEngineFactory


async def test_idle_offer_rechecks_activity_and_lock_generation() -> None:
    current: TakeoverOwner | None = None
    busy = False
    exits = []
    listener = TakeoverListener(lambda: current, lambda: not busy, lambda: exits.append(True))
    port = await listener.start()
    try:
        first = TakeoverOwner(port, os.urandom(32))
        current = first
        assert await query_owner(first, "status")
        busy = True
        assert not await query_owner(first, "take")
        busy = False
        current = TakeoverOwner(port, os.urandom(32))
        assert not await query_owner(first, "take")
        current = first
        assert not exits
        assert await query_owner(first, "take")
        assert exits == [True]
    finally:
        listener.close()


async def test_dialog_offers_only_idle_owner_and_tracks_replacement(tmp_path: Path) -> None:
    from tests.support.tui_app_harness import make_chrys_app

    store = JsonFileStateStore(tmp_path / "sessions")
    holder = ActiveSessionGuard(store)
    busy = True
    exits = []
    listener = TakeoverListener(lambda: holder.takeover_owner, lambda: not busy, lambda: exits.append(True))
    holder.takeover_port = await listener.start()
    target = str(uuid4())
    path = store.active_owner_path(target)
    try:
        assert await asyncio.to_thread(holder.ensure, target)
        message, generation = holder.conflict_details(target)
        assert generation
        record = json.loads(path.read_bytes())
        assert "key" not in record and "takeover_secret" not in record
        assert TakeoverOwner.read(tmp_path / "copy.active.json", target, generation) is None
        dialog = SessionInUseDialog(message, store.sessions_root, target, generation, locale_controller=None)
        app = make_chrys_app(tmp_path / "local")
        async with app.run_test(size=(60, 20)) as pilot:
            await app.push_screen(dialog)
            take, ok = dialog.query_one("#confirm-yes", Button), dialog.query_one("#confirm-no", Button)
            await wait_for(lambda: ok.has_focus, pilot=pilot)
            await pilot.press("left")
            assert ok.has_focus and not take.display
            busy = False
            await wait_for(lambda: take.display, pilot=pilot)
            assert ok.has_focus
            busy = True
            await dialog.take_over()
            assert not exits and holder.owns(target)
            await wait_for(lambda: not take.display, pilot=pilot)
            await asyncio.to_thread(holder.release)
            assert not credential_path(path, target, generation).exists()
            # A crash can leave the old advertisement even though the OS released its lock.
            path.write_text(json.dumps(record))
            await wait_for(lambda: take.display, pilot=pilot)
            assert await asyncio.to_thread(holder.ensure, target)
            await wait_for(lambda: app.screen is not dialog, pilot=pilot)
            assert dialog.owner_changed and not exits
    finally:
        listener.close()
        holder.release()


async def test_link_retarget_after_idle_probe_does_not_close_original_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, alias = (JsonFileStateStore(tmp_path / name) for name in ("first", "second", "alias"))
    session_id = str(uuid4())
    for store in first, second:
        await store.save_session(session_id, {"messages": []})
    link = alias.session_dir(session_id)
    symlink_or_skip(link, first.session_dir(session_id), target_is_directory=True)
    calls = []

    async def query(peer: TakeoverOwner, action: str) -> bool:
        calls.append((peer, action))
        return False

    monkeypatch.setattr("chrys.app.tui.screens.dialogs.session_in_use.query_owner", query)
    dialog = SessionInUseDialog("busy", alias.sessions_root, session_id, "generation", locale_controller=None)
    peer = TakeoverOwner(1234, os.urandom(32))
    dialog._peer = peer
    await dialog.take_over()
    assert calls == [(peer, "take")]
    link.unlink()
    symlink_or_skip(link, second.session_dir(session_id), target_is_directory=True)
    await dialog.take_over()
    assert calls == [(peer, "take")]


@pytest.mark.parametrize("worker_owner", ["main", "buddy"])
async def test_queued_ui_work_blocks_owner_before_the_worker_runs(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_owner: str
) -> None:
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await wait_for(session.app._takeover_idle, pilot=session.pilot)
        release = asyncio.Event()
        if worker_owner == "buddy":
            session.main.query_one(BuddyPanel).run_worker(release.wait, group="buddy-count")
        else:
            session.main._start_worker(release.wait)
        try:
            # Same event-loop turn as scheduling a submit: the worker cannot have started yet.
            assert not session.app._takeover_idle()
        finally:
            release.set()
        await wait_for(session.app._takeover_idle, pilot=session.pilot)


def _run_owner(root: Path, session_id: str) -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.orchestration.engine.assembly import assemble_agent_engine
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from tests.app.tui.screens.test_prompt_shortcut_send import (
        MAIN_MODEL,
        Builds,
        ScriptedModel,
        builder_module,
        make_agent,
        mock_models,
    )
    from tests.support.tui_app_harness import make_chrys_app

    bus = EventBus()
    registry = AgentProfileRegistry()
    registry.save(make_agent(ALPHA))
    save_profile(mock_model(MAIN_MODEL, "Main"))
    models = mock_models()
    settings = Settings(model_profile=MAIN_MODEL)
    store = JsonFileStateStore(root)
    engine = assemble_agent_engine(
        bus,
        settings=settings,
        agent_registry=registry,
        model_registry=models,
        state_store=store,
        surface=SessionSurface.TUI,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(builder_module, "create_client", Builds(ScriptedModel()).create_client)
        app = make_chrys_app(
            root,
            settings_handle=engine.settings_handle,
            settings=engine.settings_handle.settings,
            engine=engine,
            event_bus=bus,
            state_store=store,
            agent_registry=registry,
            model_registry=models,
            profile_name=ALPHA,
            startup_session_id=session_id,
        )
        app.enable_session_takeover()
        # A Windows venv launcher may run the interpreter in a separate process.
        print(os.getpid(), flush=True)  # noqa: T201 - subprocess handshake
        app.run(headless=True, size=(120, 40))
        if app.return_code:
            raise SystemExit(app.return_code)


async def test_takeover_restores_chat_from_other_process(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        store = session.main._services.state_store
        assert store is not None
        target = str(uuid4())
        await asyncio.to_thread(save_profile, mock_model(MAIN_MODEL, "Main"))
        await store.save_session(
            target,
            {"messages": [Message("user", ["remember this conversation"]), Message("assistant", ["Remembered."])]},
            agent_profile=ALPHA,
            model_profile_id=MAIN_MODEL,
            model_id="mock",
            model_provider="mock",
            primary_cwd=str(tmp_path),
        )
        home = tmp_path / "owner-home"
        home.mkdir()
        repo = Path(__file__).resolve().parents[5]
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.app.tui.screens.main.test_session_takeover",
            str(store.sessions_root),
            target,
            cwd=tmp_path,
            env={
                **os.environ,
                "HOME": str(home),
                "USERPROFILE": str(home),
                "APPDATA": str(home),
                "PYTHONPATH": str(repo),
            },
            stderr=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            owner_path = session_active_owner_path(store.sessions_root, target)
            await wait_for(lambda: child.returncode is not None or owner_path.is_file(), timeout=ENGINE_TURN_TIMEOUT)
            if child.returncode is not None:
                assert child.stderr is not None
                pytest.fail((await child.stderr.read()).decode())
            record = json.loads(owner_path.read_bytes())
            assert child.stdout is not None
            owner_pid = int(await asyncio.wait_for(child.stdout.readline(), ENGINE_TURN_TIMEOUT))
            assert record["pid"] == owner_pid
            assert record["parent_name"]

            async def open_dialog() -> SessionInUseDialog:
                session.chat.insert(f"/sessions {target}")
                await session.pilot.press("enter")
                await wait_for(
                    lambda: (
                        isinstance(session.app.screen, SessionInUseDialog)
                        and session.app.screen.is_mounted
                        and session.app.screen.query_one("#confirm-yes", Button).display
                    ),
                    pilot=session.pilot,
                    timeout=ENGINE_TURN_TIMEOUT,
                )
                dialog = session.app.screen
                assert isinstance(dialog, SessionInUseDialog)
                assert str(owner_pid) in str(dialog._message)
                assert dialog.query_one("#confirm-no", Button).has_focus
                return dialog

            await open_dialog()
            await session.pilot.press("enter")
            await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot)
            assert child.returncode is None and session.engine.session_id != target
            await open_dialog()
            await click_when_settled(session.pilot, "#confirm-yes")
            await wait_for(
                lambda: session.main.chat_session_id == target and session.app.screen is session.main,
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            assert await asyncio.wait_for(child.wait(), ENGINE_TURN_TIMEOUT) == 0
            assert session.engine.session.guard.owns(target)
            await wait_for(lambda: not session.bar.agent_loading, pilot=session.pilot)
            session.bar.focus_input()
            session.chat.insert("continue here")
            await session.pilot.press("enter")
            await wait_for(
                lambda: bool(session.model.call_history),
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            history = [m.text for m in session.model.call_history[-1][0] if m.role == "user"]
            assert any("remember this conversation" in text for text in history), history
            assert any("continue here" in text for text in history), history
        finally:
            if child.returncode is None and (os.name != "nt" or not await kill_windows_process_tree(child.pid)):
                child.kill()
            await child.wait()


if __name__ == "__main__":
    _run_owner(Path(sys.argv[1]), sys.argv[2])
