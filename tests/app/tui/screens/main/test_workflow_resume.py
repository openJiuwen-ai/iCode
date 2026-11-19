# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat resume excludes newer workflow sessions from its latest-session lookup."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.kernel import Message
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for
from tests.support.workflow_history import record_workflow_run

from ._workflow_support import WorkflowEngine, save_workflow_session

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_resume_chat_ignores_a_newer_workflow_session(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        store = main._services.state_store
        assert store is not None
        chat_id, workflow_id = (str(uuid4()) for _ in range(2))
        run_id = uuid4().hex
        await store.save_session(chat_id, {"messages": [Message("user", ["Chat history"])]})
        await record_workflow_run(
            store.session_dir(workflow_id) / "workflows" / run_id,
            session_id=workflow_id,
            title="New workflow",
            outcome="completed",
        )
        await save_workflow_session(store, workflow_id, tmp_path)
        assert await store.load_latest_session_id() == workflow_id
        restored: list[str] = []

        async def restore(event: events.SessionRestore) -> None:
            restored.append(event.session_id)
            await bus.publish(events.SessionReady(session_id=event.session_id, primary_cwd=str(tmp_path)))
            await bus.publish(events.SessionRestored(session_id=event.session_id, primary_cwd=str(tmp_path)))

        await bus.subscribe(events.SessionRestore, restore)
        try:
            assert main._suggestions.dispatch_slash_command("/resume")
            await wait_for(lambda: restored == [chat_id] and not main._state.run.agent_loading, pilot=pilot)
            assert main.query_one(ChatPanel).session_id == chat_id
            assert not main._workflow.workflow_mode
        finally:
            await bus.unsubscribe(events.SessionRestore, restore)
