# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow history metadata remains fresh independently of the chat envelope cache."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from chrys.kernel import Message
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore, WorkflowSessionMeta
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.store import RunSpec, WorkflowRunStore
from tests.service.state._store_helpers import browser_page
from tests.support.workflow_history import record_workflow_run, workflow_state


@pytest.mark.parametrize(
    "outcome,reason,status",
    [
        ("cancelled", "", "cancelled"),
        ("completed", "", "completed"),
        ("node_failed", "", "failed"),
        ("orphaned", "", "interrupted"),
        ("cancelled", "shutdown", "interrupted"),
        ("cancelled", "deadline_exceeded", "failed"),
        ("cancelled", "internal_error", "failed"),
    ],
)
async def test_workflow_status_comes_from_log_without_resaving_session(
    tmp_path: Path, outcome: str, reason: str, status: str
) -> None:
    from dataclasses import replace

    from tests.service.workflows.test_store import header

    store = JsonFileStateStore(tmp_path)
    identity = replace(header(), title="Review [literal]")
    session_id, run_id = identity.session_id, identity.run_id
    directory = store.session_dir(session_id) / "workflows" / run_id
    await store.save_workflow_session(
        session_id, WorkflowSessionState.decode(workflow_state(tmp_path, run_count=1, latest_run_id=run_id))
    )
    run_store = WorkflowRunStore.open(directory, header=identity, spec=RunSpec({}, {}), input_text="go", source=b"")
    first = (await store.list_sessions())[0]
    assert first.kind == "workflow" and first.run_count == 1
    assert first.latest_run.title == "Review [literal]" and first.latest_run.status == "interrupted"
    # The browser's snapshot reads the same live status: its search goes by what each row shows.
    assert await _snapshot_status(store) == "interrupted"
    envelope = (directory.parent.parent / "session.json").read_bytes()
    guard = ActiveSessionGuard(store)
    assert guard.ensure(session_id)
    try:
        assert (await store.list_sessions())[0].latest_run.status == "running"
        assert await _snapshot_status(store) == "running"
        await run_store.finish(outcome, {"reason": reason})
        listed = await browser_page(store, "workflow")
        assert listed[0].latest_run.status == status
        assert await _snapshot_status(store) == status
    finally:
        await run_store.close()
        guard.release()
    assert (directory.parent.parent / "session.json").read_bytes() == envelope


async def _snapshot_status(store: JsonFileStateStore) -> str:
    """The latest run's status as the sessions browser's snapshot read it."""
    (entry,) = (await store.open_session_listing(kind="workflow")).entries
    run = entry.meta.latest_run if isinstance(entry.meta, WorkflowSessionMeta) else None
    if run is None:
        raise AssertionError("the snapshot read no run summary")
    return run.status


async def test_session_kind_is_immutable_even_when_empty(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    workflow_id, chat_id = str(uuid4()), str(uuid4())
    await store.save_workflow_session(workflow_id, WorkflowSessionState.decode(workflow_state(tmp_path)))
    await store.save_session(chat_id, {})
    with pytest.raises(ValueError, match="cannot change"):
        await store.save_session(workflow_id, {"messages": [Message("user", ["wrong mode"])]})
    with pytest.raises(ValueError, match="cannot change"):
        await store.save_workflow_session(chat_id, WorkflowSessionState.decode(workflow_state(tmp_path)))
    await store.save_workflow_session(workflow_id, WorkflowSessionState.decode(workflow_state(tmp_path)))
    assert await store.load_latest_session_id(chat_only=True) == chat_id
    workflow = await store.load_session_meta(workflow_id)
    chat = await store.load_session_meta(chat_id)
    assert workflow is not None and workflow.kind == "workflow"
    assert chat is not None and chat.kind == "chat"


async def test_chat_envelopes_predating_workflows_are_chat_sessions(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    session_id = str(uuid4())
    await store.save_session(session_id, {"messages": []})
    path = store.session_dir(session_id) / "session.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    del envelope["meta"]["kind"]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    meta = await store.load_session_meta(session_id)
    assert meta is not None and meta.kind == "chat"


async def test_kind_queries_skip_workflow_headers_and_load_only_latest_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.state import store as store_module

    store = JsonFileStateStore(tmp_path)
    chat_id, workflow_id = str(uuid4()), str(uuid4())
    await store.save_session(chat_id, {"messages": []})
    directory = store.session_dir(workflow_id)
    older, latest = uuid4().hex, uuid4().hex
    for run_id in (older, latest):
        await record_workflow_run(
            directory / "workflows" / run_id, session_id=workflow_id, title="Review", outcome="completed"
        )
    await store.save_workflow_session(
        workflow_id, WorkflowSessionState.decode(workflow_state(tmp_path, run_count=2, latest_run_id=latest))
    )
    reads = []
    original = store_module.read_workflow_meta

    def read(path, *, active):
        reads.append(path.name)
        return original(path, active=active)

    monkeypatch.setattr(store_module, "read_workflow_meta", read)
    for _ in range(2):
        assert [meta.session_id for meta in await store.list_sessions(kind="chat")] == [chat_id]
        assert [meta.session_id for meta in await browser_page(store, "chat")] == [chat_id]
    assert reads == []
    (meta,) = await store.list_sessions(kind="workflow")
    assert meta.kind == "workflow" and meta.run_count == 2
    assert meta.latest_run is not None and meta.latest_run.status == "completed"
    assert reads == [latest]


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "outcome,status", [("completed", "completed"), ("node_failed", "failed"), ("cancelled", "cancelled")]
)
async def test_listing_uses_log_terminal_without_mutating_the_header(
    tmp_path: Path, active: bool, outcome: str, status: str
) -> None:
    from chrys.service.workflows.history import read_workflow_meta

    session_id = str(uuid4())
    directory = tmp_path / "workflows" / uuid4().hex
    await record_workflow_run(directory, session_id=session_id, title="Review", outcome=outcome)
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}

    meta = read_workflow_meta(directory, active=active)

    assert meta.status == status
    assert {path: path.read_bytes() for path in before} == before


async def test_incomplete_header_remains_visible_when_log_is_unreadable(tmp_path: Path) -> None:
    from chrys.service.workflows.history import read_workflow_meta

    directory = tmp_path / "workflows" / uuid4().hex
    await record_workflow_run(directory, session_id=str(uuid4()), title="Review", outcome="")
    events = directory / "events.jsonl"
    events.unlink()
    events.mkdir()

    meta = read_workflow_meta(directory, active=False)

    assert meta.status == "interrupted"


@pytest.mark.parametrize(
    "field,value",
    [
        ("identity", None),
        ("identity", []),
        ("summary", None),
        ("summary", []),
        ("workspace", None),
        ("total_session_tokens", True),
    ],
)
async def test_corrupt_workflow_is_isolated_from_both_session_lists(tmp_path: Path, field: str, value: object) -> None:
    store = JsonFileStateStore(tmp_path)
    chat_id, good_id, broken_id = (str(uuid4()) for _ in range(3))
    await store.save_session(chat_id, {"messages": [Message("user", ["legacy chat"])]})
    for session_id in (good_id, broken_id):
        run_id = uuid4().hex
        state = workflow_state(tmp_path, run_count=1, latest_run_id=run_id)
        await store.save_workflow_session(session_id, WorkflowSessionState.decode(state))
        directory = store.session_dir(session_id) / "workflows" / run_id
        await record_workflow_run(directory, session_id=session_id, title="Review", outcome="completed")
    path = store.session_dir(broken_id) / "session.json"
    envelope = json.loads(path.read_text())
    envelope["state"][field] = value
    path.write_text(json.dumps(envelope))
    assert [meta.session_id for meta in await store.list_sessions(kind="chat")] == [chat_id]
    assert [meta.session_id for meta in await store.list_sessions(kind="workflow")] == [good_id]
    browsed = [*await browser_page(store, "chat"), *await browser_page(store, "workflow")]
    assert {meta.session_id for meta in browsed} == {chat_id, good_id}
    assert await store.load_session_meta(broken_id) is None


async def test_workflow_encoding_does_not_add_chat_history(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    session_id = str(uuid4())
    await store.save_workflow_session(session_id, WorkflowSessionState.decode(workflow_state(tmp_path)))
    state = json.loads((store.session_dir(session_id) / "session.json").read_text())["state"]
    assert not {"messages", "compressed_msgs", "turn_counter", "approval_mode"}.intersection(state)
    restored = (await store.load_workflow_session(session_id)).encode()
    assert restored == state
