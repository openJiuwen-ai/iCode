# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace membership never enters persisted conversation data."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Message
from chrys.service.session.persistence import SessionPersistence
from chrys.service.session.side_questions import SideQuestionSessionSeed
from chrys.service.state.session_catalog import decode_meta, encode_meta
from chrys.service.state.store import JsonFileStateStore

LEGACY = Path(__file__).parents[2] / "support" / "fixtures" / "workspace_legacy"
_QUESTION = SideQuestionEntry("2026-11-01T15:08:47+00:00", "Question", "Answer")
_HISTORY = {"messages": [Message("user", ["A real conversation"])]}
_WORKSPACE_FIELDS = {"workspace_id", "workspace_name"}


async def test_normal_recovery_side_question_and_fork_have_no_workspace_fields(tmp_path):
    store = JsonFileStateStore(tmp_path)
    persistence = SessionPersistence(store, EventBus())
    workspace = Workspace.from_cwd(str(tmp_path))
    metadata = {
        "agent_profile_name": "Code",
        "agent_display_name": "Code",
        "agent_profile_id": "code",
        "workspace": workspace,
    }
    assert await persistence.save_session("session", _HISTORY, **metadata)
    assert await persistence.save_recovery_session_strict("session", _HISTORY, **metadata)
    await store.save_side_question("side", _QUESTION, SideQuestionSessionSeed())
    fork_id = await asyncio.to_thread(store.fork_session, "session")

    for session_id in ("session", "side", fork_id):
        for filename in ("session.json", "session.json.bak", "session.recovery.json"):
            path = store.session_dir(session_id) / filename
            if path.exists():
                envelope = json.loads(path.read_text(encoding="utf-8"))
                assert not _WORKSPACE_FIELDS.intersection(envelope["meta"])
        meta = await store.load_session_meta(session_id)
        assert meta is not None
        record = encode_meta(meta)
        assert not _WORKSPACE_FIELDS.intersection(record)
        assert decode_meta(record) == replace(meta, size_bytes=0)


async def test_empty_history_does_not_rewrite_a_side_question_session(tmp_path):
    store = JsonFileStateStore(tmp_path)
    await store.save_side_question("side", _QUESTION, SideQuestionSessionSeed())
    path = store.session_dir("side") / "session.json"
    before = path.read_bytes(), path.stat().st_mtime_ns
    persistence = SessionPersistence(store, EventBus())
    assert not await persistence.save_session(
        "side",
        {"messages": []},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="code",
        workspace=Workspace.from_cwd(str(tmp_path)),
    )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    ("shape", "dirs"),
    [("missing", []), ("empty", []), ("primary", ["/repo"]), ("extra", ["/repo", "/shared"])],
)
async def test_main_chat_checkpoint_loads_without_rewrite(tmp_path, shape, dirs):
    # 9228ec4a wrote every fixture; for the missing-key case its envelope writer
    # received the older shape with working_dirs omitted.
    original = (LEGACY / f"chat-{shape}.json").read_bytes()
    session_id = f"legacy-{shape}"
    store = JsonFileStateStore(tmp_path)
    path = store.session_dir(session_id) / "session.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    before = path.stat().st_mtime_ns
    state = await store.load_session(session_id)
    meta = await store.load_session_meta(session_id)
    assert [message.text for message in state["messages"]] == ["Keep this conversation"]
    assert (meta.primary_cwd, meta.working_dirs) == ("/repo", dirs)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (original, before)

    await store.save_session(session_id, state, primary_cwd=meta.primary_cwd, working_dirs=meta.working_dirs)
    rewritten = json.loads(path.read_bytes())
    expected = json.loads(original)
    expected["meta"]["working_dirs"] = dirs
    expected["session_checkpoint_id"] = rewritten["session_checkpoint_id"]
    for runtime_stamp in ("app_version", "os_name", "arch"):
        expected["meta"][runtime_stamp] = rewritten["meta"][runtime_stamp]
    assert rewritten == expected
