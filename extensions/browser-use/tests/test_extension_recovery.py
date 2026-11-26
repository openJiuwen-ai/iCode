# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discovery, status and recovery tools: no implicit launch, no cross-session claim (W03)."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from chrys.extensions import ToolContext, ToolError, TurnContext

import icode_browser_use.extension as ext
from icode_browser_use import registry
from icode_browser_use.journal import Journal


class FakeOwnedSession:
    """An owned session double: real fields, no Chrome and no CDP connection."""

    def __init__(self, cfg, *, journal_owner: str = "", journal_session: str = "") -> None:
        self.cfg = cfg
        self.journal_owner = journal_owner
        self.journal_session = journal_session
        self.journal = Journal(None)
        self.state = "idle"
        self.started_once = False
        self.page_epoch = 11
        self.login_pending = False
        self.snapshot = SimpleNamespace(url="", title="")
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        self.state = "closed"

    def mark_disconnected(self) -> None:
        self.state = "failed"

    def pages(self) -> list[object]:
        return []


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch):
    for name in tuple(os.environ):
        if name.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(name)
    importlib.reload(ext)
    ext._browsers.clear()
    yield
    ext._browsers.clear()


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    for name, value in env.items():
        monkeypatch.setenv(f"ICODE_BROWSER_{name}", value)


def ctx(tmp_path: Path, *, session: str = "s1", agent: str = "A") -> ToolContext:
    return ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        agent_name=agent,
        session_id=session,
    )


def records_for(context: ToolContext) -> list[registry.ManifestRecord]:
    records, problem = registry.load(registry.manifest_path(context))
    assert problem is None
    return records


async def open_one(context: ToolContext) -> tuple[str, registry.ManifestRecord]:
    text = await ext.browser_open(context)
    browser_id = text.removeprefix("browser_id: ")
    return browser_id, next(item for item in records_for(context) if item.browser_id == browser_id)


def test_open_records_identity_without_starting(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        assert record.schema == registry.SCHEMA_VERSION
        assert record.backend == "owned"
        assert record.slot == 0
        assert record.session_id == "s1"
        assert record.agent_name == "A"
        assert record.resume_id.startswith("s0-")
        assert record.origin == "(not navigated yet)"
        assert ext._browsers[browser_id].session.started_once is False

    asyncio.run(scenario())


def test_list_shows_live_browsers_without_launching(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        text = await ext.browser_list(context)
        assert "open=1" in text
        assert f"browser_id={browser_id}" in text
        assert "state=allocated" in text
        assert "backend=owned" in text
        assert f"resume_id={record.resume_id}" in text
        assert ext._browsers[browser_id].session.started_once is False

    asyncio.run(scenario())


def test_list_finds_a_record_after_the_registry_is_lost(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        _, record = await open_one(context)
        ext._browsers.clear()
        text = await ext.browser_list(context)
        assert "open=0" in text
        assert "recoverable=1" in text
        assert f"resume_id={record.resume_id}" in text
        assert "state=recorded" in text
        assert "browser_resume" in text

    asyncio.run(scenario())


def test_list_never_exposes_another_session_record(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)

    async def scenario() -> None:
        _, record = await open_one(ctx(tmp_path, session="s1"))
        text = await ext.browser_list(ctx(tmp_path, session="s2"))
        assert "recoverable=0" in text
        assert "unclaimable=1" in text
        assert record.resume_id not in text
        assert "example.com" not in text

    asyncio.run(scenario())


def test_status_reports_allocated_without_starting(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, _ = await open_one(context)
        text = await ext.browser_status(context, browser_id)
        assert "state=allocated" in text
        assert "browser_navigate" in text
        assert "page_epoch" not in text
        assert ext._browsers[browser_id].session.started_once is False

    asyncio.run(scenario())


def test_status_reports_ready_login_wait_and_redacted_origin(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, _ = await open_one(context)
        session = ext._browsers[browser_id].session
        session.state = "ready"
        session.started_once = True
        session.login_pending = True
        session.snapshot.url = "https://example.com/report?token=SENTINEL"
        text = await ext.browser_status(context, browser_id)
        assert "state=ready" in text
        assert "login_pending=yes" in text
        assert "origin=https://example.com" in text
        assert "page_epoch=11" in text
        assert "SENTINEL" not in text
        assert "sign in" in text

    asyncio.run(scenario())


def test_status_maps_a_dead_connection_to_disconnected(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, _ = await open_one(context)
        ext._browsers[browser_id].session.mark_disconnected()
        text = await ext.browser_status(context, browser_id)
        assert "state=disconnected" in text
        assert "new page" in text
        assert ext._browsers[browser_id].session.started_once is False

    asyncio.run(scenario())


def test_status_unknown_handle_points_to_list_and_open(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)

    async def scenario() -> None:
        with pytest.raises(ToolError) as info:
            await ext.browser_status(ctx(tmp_path), "missing")
        assert info.value.code == "unknown_browser"
        assert "browser_list" in str(info.value)
        assert "browser_open" in str(info.value)

    asyncio.run(scenario())


def test_resume_is_idempotent_for_a_live_generation(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        text = await ext.browser_resume(context, record.resume_id)
        assert text.startswith(f"browser_id: {browser_id}")
        assert "already live" in text
        assert len(ext._browsers) == 1

    asyncio.run(scenario())


def test_resume_reopens_the_same_slot_as_a_new_page(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        ext._browsers.clear()
        resume_id = record.resume_id

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("resume must not act on the page")

        monkeypatch.setattr(ext, "_run", forbidden)
        text = await ext.browser_resume(context, resume_id)
        resumed_id = text.splitlines()[0].removeprefix("browser_id: ")
        assert resumed_id and resumed_id != browser_id
        assert "new page" in text
        assert "not restored" in text
        session = ext._browsers[resumed_id].session
        assert str(session.cfg.state_dir).endswith("slot-0")
        assert session.started_once is False

    asyncio.run(scenario())


def test_resume_rejects_unknown_and_foreign_resume_ids(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)

    async def scenario() -> None:
        context = ctx(tmp_path)
        _, record = await open_one(context)
        with pytest.raises(ToolError) as unknown:
            await ext.browser_resume(context, "s0-missing")
        assert unknown.value.code == "unknown_resume"
        assert "browser_list" in str(unknown.value)

        ext._browsers.clear()
        with pytest.raises(ToolError) as foreign:
            await ext.browser_resume(ctx(tmp_path, session="s2"), record.resume_id)
        assert foreign.value.code == "browser_resume_denied"
        assert "session" in str(foreign.value)
        assert not ext._browsers

    asyncio.run(scenario())


def test_resume_refuses_a_slot_that_is_already_live(tmp_path, monkeypatch) -> None:
    import dataclasses

    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        # A stale generation for the same slot, e.g. written by an older build.
        stale = dataclasses.replace(record, resume_id="s0-stale", generation="stale")
        path = registry.manifest_path(context)
        registry.save(path, [*records_for(context), stale])
        assert ext._browsers[browser_id].slot == 0
        with pytest.raises(ToolError) as info:
            await ext.browser_resume(context, "s0-stale")
        assert info.value.code == "browser_resume_conflict"
        assert "close" in str(info.value)

    asyncio.run(scenario())


def test_resume_refuses_when_the_backend_configuration_changed(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        _, record = await open_one(context)
        ext._browsers.clear()
        monkeypatch.setenv("ICODE_BROWSER_BRIDGE", "1")
        with pytest.raises(ToolError) as info:
            await ext.browser_resume(context, record.resume_id)
        assert info.value.code == "browser_resume_backend_mismatch"
        assert "owned" in str(info.value)
        assert "bridge" in str(info.value)

    asyncio.run(scenario())


def test_close_drops_the_record(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, _ = await open_one(context)
        await ext.browser_close(context, browser_id)
        assert records_for(context) == []
        assert "recoverable=0" in await ext.browser_list(context)

    asyncio.run(scenario())


def test_context_hints_recovery_without_leaking_ids(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        _, record = await open_one(context)
        ext._browsers.clear()
        turn = TurnContext.for_testing(
            tmp_path,
            prompt="hi",
            data_dir=tmp_path / "data",
            session_data_dir=tmp_path / "session",
            agent_name="A",
            session_id="s1",
        )
        text = await ext.browser_context(turn)
        assert text is not None
        assert "browser_list" in text
        assert record.resume_id not in text
        assert "example.com" not in text

    asyncio.run(scenario())


def test_manifest_write_failure_does_not_break_open(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "save", broken)
    text = asyncio.run(ext.browser_open(ctx(tmp_path)))
    assert text.startswith("browser_id: ")


def test_list_reports_an_unreadable_manifest(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    context = ctx(tmp_path)
    path = registry.manifest_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    text = asyncio.run(ext.browser_list(context))
    assert "manifest=unreadable" in text
    assert "open=0" in text


def test_records_survive_a_json_round_trip(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        _, record = await open_one(context)
        raw = json.loads(registry.manifest_path(context).read_text(encoding="utf-8"))
        assert raw == {"schema": registry.SCHEMA_VERSION, "records": [record.to_dict()]}

    asyncio.run(scenario())


def test_stale_workflow_close_preserves_profile_dir_after_newer_generation_resumed_and_idled(
    tmp_path, monkeypatch
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    wf_ctx = ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        agent_name="A",
        session_id="s-wf",
        invocation_type="workflow_node",
        caller_name="wf",
        invocation_id="inv-1",
    )

    async def scenario() -> None:
        opened = await ext.browser_open(wf_ctx)
        bid_a = opened.split("browser_id: ", 1)[1].strip()
        browser_a = ext._browsers[bid_a]
        state_dir = browser_a.ephemeral_state_dir
        assert state_dir is not None
        state_dir.mkdir(parents=True, exist_ok=True)
        cookie_marker = state_dir / "Cookies.txt"
        cookie_marker.write_text("from-a", encoding="utf-8")
        browser_a.session.owns_state_dir = True  # type: ignore[attr-defined]
        assert browser_a.record is not None
        resume_id_a = browser_a.record.resume_id

        # Simulate Host B in another process resuming the same workflow slot while Host A is idle.
        saved_a = ext._browsers.pop(bid_a)
        resumed = await ext.browser_resume(wf_ctx, resume_id_a)
        bid_b = resumed.splitlines()[0].split("browser_id: ", 1)[1].strip()
        browser_b = ext._browsers[bid_b]
        assert browser_b.record is not None and browser_b.record.resume_id != resume_id_a
        cookie_marker.write_text("from-b-cookie", encoding="utf-8")

        # Now B is idle (no instance lock held), and stale Host A closes.
        ext._browsers.pop(bid_b)
        ext._browsers[bid_a] = saved_a
        await ext.browser_close(wf_ctx, bid_a)

        assert state_dir.is_dir(), "closing stale handle A must not delete B's profile dir"
        assert cookie_marker.read_text(encoding="utf-8") == "from-b-cookie"
        remaining = records_for(wf_ctx)
        assert [r.resume_id for r in remaining] == [browser_b.record.resume_id]

    asyncio.run(scenario())


def test_unstarted_subagent_handle_is_reclaimed_after_idle_timeout(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0.05")
    sub_ctx = ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        agent_name="Browser",
        session_id="s-sub",
        invocation_type="sub_agent",
        caller_name="browser_agent",
        invocation_id="sub-inv-9",
    )

    async def scenario() -> None:
        opened = await ext.browser_open(sub_ctx)
        bid = opened.split("browser_id: ", 1)[1].strip()
        assert bid in ext._browsers
        assert len(records_for(sub_ctx)) == 1
        for _ in range(50):
            if bid not in ext._browsers and records_for(sub_ctx) == []:
                break
            await asyncio.sleep(0.05)
        assert bid not in ext._browsers
        assert records_for(sub_ctx) == []

    asyncio.run(scenario())
