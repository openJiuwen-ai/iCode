# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Open a saved session by ID from the chat input."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from chrys.app.tui.screens.sessions import SessionsScreen
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import Message
from tests.app.tui.screens.test_prompt_shortcut_send import ALPHA, Session, running_session, type_text
from tests.support.toasts import shown_toasts
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_history import record_workflow_run

from ._workflow_support import save_workflow_session, workflow_notice_text

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.state.store import SessionMeta, StateStore
    from chrys.service.state.workflow import WorkflowSessionState
    from tests.support.engines import AgentEngineFactory


async def enter_command(session: Session, command: str) -> None:
    session.chat.insert(command)
    await session.pilot.press("enter")


def delete_after_resolution(store: StateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = store.resolve_startup_session

    async def delete_after_lookup(value: str, *, require_inactive: bool = False) -> SessionMeta | None:
        meta = await lookup(value, require_inactive=require_inactive)
        assert meta is not None
        monkeypatch.setattr(store, "resolve_startup_session", lookup)
        await store.delete_session(meta.session_id)
        return meta

    monkeypatch.setattr(store, "resolve_startup_session", delete_after_lookup)


@pytest.mark.parametrize("form", ["short", "uuid", "hex"])
async def test_sessions_restores_the_saved_chat(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        store = session.main._services.state_store
        assert store is not None
        target = str(uuid4())
        await store.save_session(
            target,
            {"messages": [Message("user", ["A saved question"])]},
            agent_profile=ALPHA,
            primary_cwd=str(tmp_path),
        )
        selector = {"short": session_short_id(target), "uuid": target, "hex": UUID(target).hex}[form].upper()
        if form == "short":
            await type_text(session.pilot, f"/sessions {selector}")
            await session.pilot.press("enter")
        else:
            await enter_command(session, f"/sessions {selector}")
        await wait_for(
            lambda: session.main.chat_session_id == target and session.app.screen is session.main,
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert session.engine.session_id == target


@pytest.mark.parametrize("deleted", [False, True], ids=["wrong-uuid-with-same-prefix", "deleted-after-lookup"])
async def test_missing_session_warns_once_and_preserves_chat(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deleted: bool
) -> None:
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        store = session.main._services.state_store
        assert store is not None
        original = session.main.chat_session_id
        target = str(uuid4())
        await store.save_session(target, {"messages": []}, primary_cwd=str(tmp_path))
        selector = target[:-1] + ("0" if target[-1] != "0" else "1")
        if deleted:
            selector = target
            delete_after_resolution(store, monkeypatch)
        await enter_command(session, f"/sessions {selector}")
        expected = f"Session {selector} was not found."
        await wait_for(
            lambda: (
                any(str(n.message) == expected for n in shown_toasts(session.app))
                and session.app.screen is session.main
            ),
            pilot=session.pilot,
        )
        assert [n.severity for n in shown_toasts(session.app) if str(n.message) == expected] == ["warning"]
        assert session.main.chat_session_id == original
        assert not session.bar.retry_mode and not session.bar.agent_loading
        assert session.main._state.session.load_owner is None
        await enter_command(session, "/sessions  ")
        await wait_for(
            lambda: isinstance(session.app.screen, SessionsScreen) and session.app.screen.is_mounted,
            pilot=session.pilot,
        )


@pytest.mark.parametrize("removal", ["none", "after-lookup", "before-history", "run-only"])
async def test_sessions_opens_workflow_history_from_chat(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removal: str
) -> None:
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        store = session.main._services.state_store
        assert store is not None
        original = session.main.chat_session_id
        target, run_id = str(uuid4()), uuid4().hex
        await record_workflow_run(
            store.session_dir(target) / "workflows" / run_id,
            session_id=target,
            title="Saved workflow",
            outcome="completed",
        )
        await save_workflow_session(store, target, tmp_path)
        if removal == "run-only":
            run_dir = store.session_dir(target) / "workflows" / run_id
            run_dir.rename(tmp_path / "removed-run")
        elif removal == "after-lookup":
            delete_after_resolution(store, monkeypatch)
        elif removal == "before-history":
            load = store.load_workflow_session

            async def delete_after_read(session_id: str) -> WorkflowSessionState | None:
                state = await load(session_id)
                assert state is not None
                await store.delete_session(session_id)
                return state

            monkeypatch.setattr(store, "load_workflow_session", delete_after_read)
        await enter_command(session, f"/sessions {session_short_id(target)}")
        if removal == "run-only":
            await wait_for(lambda: "No record available." in workflow_notice_text(session.main), pilot=session.pilot)
            assert session.main._workflow.workflow_mode
            assert not any(target in str(n.message) for n in shown_toasts(session.app))
        elif removal != "none":
            await wait_for(
                lambda: (
                    any(n.severity == "warning" and target in str(n.message) for n in shown_toasts(session.app))
                    and session.app.screen is session.main
                ),
                pilot=session.pilot,
            )
            assert not session.main._workflow.workflow_mode
        else:
            await wait_for(
                lambda: session.main._workflow_panel.run_id == run_id and session.app.screen is session.main,
                pilot=session.pilot,
            )
            assert session.main._workflow.session_id == target
        assert session.main.chat_session_id == original
        assert not session.bar.agent_loading
