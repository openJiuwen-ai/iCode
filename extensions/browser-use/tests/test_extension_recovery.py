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

import browser_use.extension as ext
from browser_use import registry
from browser_use.journal import Journal


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


def records_for(context: ToolContext) -> list[dict[str, object]]:
    records, problem = registry.load(registry.manifest_path(context))
    assert problem is None
    return records


async def open_one(context: ToolContext) -> tuple[str, dict[str, object]]:
    text = await ext.browser_open(context)
    browser_id = text.removeprefix("browser_id: ")
    return browser_id, next(item for item in records_for(context) if item["browser_id"] == browser_id)


def test_open_records_identity_without_starting(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        assert record["schema"] == registry.SCHEMA_VERSION
        assert record["backend"] == "owned"
        assert record["slot"] == 0
        assert record["session_id"] == "s1"
        assert record["agent_name"] == "A"
        assert str(record["resume_id"]).startswith("s0-")
        assert record["origin"] == "(not navigated yet)"
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
        assert f"resume_id={record['resume_id']}" in text
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
        assert f"resume_id={record['resume_id']}" in text
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
        assert str(record["resume_id"]) not in text
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
        assert "origin=https://example.com:443" in text
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
        text = await ext.browser_resume(context, str(record["resume_id"]))
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
        resume_id = str(record["resume_id"])

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
            await ext.browser_resume(ctx(tmp_path, session="s2"), str(record["resume_id"]))
        assert foreign.value.code == "browser_resume_denied"
        assert "session" in str(foreign.value)
        assert not ext._browsers

    asyncio.run(scenario())


def test_resume_refuses_a_slot_that_is_already_live(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser_id, record = await open_one(context)
        # A stale generation for the same slot, e.g. written by an older build.
        stale = {**record, "resume_id": "s0-stale", "generation": "stale"}
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
            await ext.browser_resume(context, str(record["resume_id"]))
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
        assert str(record["resume_id"]) not in text
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
        assert raw == {"schema": registry.SCHEMA_VERSION, "records": [record]}

    asyncio.run(scenario())
