# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from chrys.extensions import InvocationType, ToolCall, ToolContext, ToolError, TurnContext

from icode_browser_use import registry
import icode_browser_use.extension as ext
from icode_browser_use.errors import BadRequest, ConnectionClosed, StaleRef
from icode_browser_use.journal import Journal


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.disconnected = False
        self.state = "ready"
        self.snapshot = SimpleNamespace(url="https://example.com/a", title="Example")
        self.cfg = SimpleNamespace(screenshot_keep=20)

    async def aclose(self) -> None:
        self.closed = True
        self.state = "closed"

    def mark_disconnected(self) -> None:
        self.disconnected = True
        self.state = "failed"


@pytest.fixture(autouse=True)
def clear_registry(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(name)
    importlib.reload(ext)
    ext._browsers.clear()
    ext._reclaiming_slots.clear()
    yield
    ext._browsers.clear()
    ext._reclaiming_slots.clear()


def ctx(
    tmp_path,
    *,
    agent: str = "A",
    session: str = "s1",
    with_session_dir: bool = False,
    caller_name: str | None = None,
    invocation_type: InvocationType | None = None,
    invocation_id: str | None = None,
) -> ToolContext:
    return ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=(tmp_path / "session") if with_session_dir else None,
        agent_name=agent,
        session_id=session,
        caller_name=caller_name,
        invocation_type=invocation_type,
        invocation_id=invocation_id,
    )


def legacy_ctx(tmp_path, *, agent: str = "A", session: str = "s1"):
    """A context as older hosts (before PR #348) built it: no caller/invocation fields."""
    base = ctx(tmp_path, agent=agent, session=session)
    return SimpleNamespace(
        cwd=base.cwd,
        workspace_dirs=base.workspace_dirs,
        agent_name=base.agent_name,
        session_id=base.session_id,
        data_dir=base.data_dir,
        session_data_dir=base.session_data_dir,
    )


def add_browser(
    *,
    owner_agent: str = "A",
    owner_session: str = "s1",
    browser_id: str = "b1",
    slot: int = 0,
) -> ext._Browser:
    browser = ext._Browser(
        browser_id,
        FakeSession(),
        owner_session,
        owner_agent,
        slot=slot,
    )
    ext._browsers[browser_id] = browser
    return browser


def test_declares_extension_tools() -> None:
    assert [tool.name for tool in ext.extension.tools] == [
        "browser_open",
        "browser_list",
        "browser_status",
        "browser_resume",
        "browser_navigate",
        "browser_tabs",
        "browser_tab_switch",
        "browser_tab_close",
        "browser_find",
        "browser_snapshot",
        "browser_click",
        "browser_hover",
        "browser_fill",
        "browser_press_key",
        "browser_select_option",
        "browser_wait_for",
        "browser_dialog",
        "browser_downloads",
        "browser_download_cancel",
        "browser_scroll",
        "browser_extract",
        "browser_logs",
        "browser_back",
        "browser_forward",
        "browser_reload",
        "browser_screenshot",
        "browser_close",
        "browser_upload",
        "browser_action_status",
    ]


def test_browser_id_descriptions_point_to_open() -> None:
    from typing import get_type_hints

    for tool in ext.extension.tools:
        hints = get_type_hints(getattr(ext, tool.name), include_extras=True)
        if "browser_id" in hints:
            description = hints["browser_id"].__metadata__[0]
            assert "browser_navigate" not in description
            if "returned by" in description:
                assert description == "Browser id returned by browser_open"


def test_optin_descriptions_use_the_real_names(monkeypatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_EVALUATE", "allow")
    monkeypatch.setenv("ICODE_BROWSER_SAVE_STATE", "allow")
    importlib.reload(ext)
    assert "ICODE_BROWSER_EVALUATE=allow" in ext.browser_evaluate.__doc__
    assert "ICODE_BROWSER_SAVE_STATE=allow" in ext.browser_save_state.__doc__


def test_open_and_navigate_describe_lazy_relative_url_contract() -> None:
    from typing import get_type_hints

    assert "Allocate a browser handle" in ext.browser_open.__doc__
    assert "launch/connect is deferred" in ext.browser_open.__doc__
    description = get_type_hints(ext.browser_navigate, include_extras=True)["url"].__metadata__[0]
    assert "relative path against the current http(s) page" in description.lower()
    assert "Public sites work by default" in description
    assert "ICODE_BROWSER_ALLOWED_DOMAINS" in description
    assert "ICODE_BROWSER_ALLOWED_ORIGINS" in description


def test_open_allocates_without_starting_browser(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ICODE_BROWSER_BROKER", raising=False)
    browser_id = asyncio.run(ext.browser_open(ctx(tmp_path))).removeprefix("browser_id: ")
    assert ext._browsers[browser_id].session.state == "idle"


def test_context_is_silent_without_browsers(tmp_path) -> None:
    turn = TurnContext.for_testing(tmp_path, prompt="hi", agent_name="A", session_id="s1")
    assert asyncio.run(ext.browser_context(turn)) is None


def test_owned_handle_uses_the_session_download_folder(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ICODE_BROWSER_DOWNLOADS", "allow")
    context = ctx(tmp_path, with_session_dir=True)
    browser = ext._allocate(context, ext._config(), 0)
    try:
        assert browser.session.download_directory == tmp_path / "session" / "downloads"
    finally:
        asyncio.run(browser.session.aclose())
        ext._browsers.pop(browser.id, None)


def test_navigate_reuses_an_explicit_browser(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(context: ToolContext) -> ext._Browser:
        return add_browser(owner_agent=context.agent_name, owner_session=context.session_id)

    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_run(browser: ext._Browser, name: str, arguments: dict[str, object]) -> str:
        calls.append((name, dict(arguments)))
        browser.url = "https://example.com/a"
        browser.title = "Example"
        return "snapshot-text"

    monkeypatch.setattr(ext, "_open", fake_open)
    monkeypatch.setattr(ext, "_run", fake_run)

    browser_id = asyncio.run(ext.browser_open(ctx(tmp_path))).removeprefix("browser_id: ")
    first = asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://example.com", browser_id))
    assert first.startswith("browser_id: b1")
    assert "snapshot-text" in first

    second = asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://example.com/next", browser_id="b1"))
    assert second.startswith("browser_id: b1")
    assert [name for name, _ in calls] == ["browser_navigate", "browser_navigate"]


def test_context_lists_open_browser(tmp_path) -> None:
    add_browser()
    ext._browsers["b1"].url = "https://example.com/a"
    ext._browsers["b1"].title = "Example"
    turn = TurnContext.for_testing(tmp_path, prompt="hi", agent_name="A", session_id="s1")
    text = asyncio.run(ext.browser_context(turn))
    assert text is not None
    assert "Open browsers: 1" in text
    assert "b1" not in text
    assert "https://example.com" not in text
    assert "Browser playbook:" in text
    assert "browser_wait_for" in text
    # Byte-stable across calls so the host's cached prompt prefix survives.
    assert asyncio.run(ext.browser_context(turn)) == text


def test_unknown_browser_is_typed(tmp_path) -> None:
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_snapshot(ctx(tmp_path), "missing"))
    assert info.value.code == "unknown_browser"
    assert "browser_open" in str(info.value)
    assert "browser_navigate" not in str(info.value)


def test_other_agents_browser_is_refused(tmp_path) -> None:
    add_browser(owner_agent="B", owner_session="s2")
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_snapshot(ctx(tmp_path), "b1"))
    assert info.value.code == "browser_owner_mismatch"
    assert "browser_open" in str(info.value)
    assert "browser_navigate" not in str(info.value)


def test_same_owner_handles_do_not_cross_pages(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = add_browser(browser_id="b1", slot=0)
    second = add_browser(browser_id="b2", slot=1)
    seen: list[str] = []

    async def fake_run(browser, _name, _arguments):
        seen.append(browser.id)
        return browser.id

    monkeypatch.setattr(ext, "_run", fake_run)
    context = ctx(tmp_path)
    assert asyncio.run(ext.browser_snapshot(context, "b1")) == "b1"
    assert asyncio.run(ext.browser_snapshot(context, "b2")) == "b2"
    assert seen == ["b1", "b2"]
    assert first.id != second.id


def test_close_cancellation_still_finishes_teardown_and_drops_the_record(tmp_path) -> None:
    class SlowSession:
        def __init__(self) -> None:
            self.state = "ready"
            self.closed = False
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def aclose(self) -> None:
            self.started.set()
            await self.release.wait()
            self.state = "closed"
            self.closed = True

    session = SlowSession()
    context = ctx(tmp_path)
    record = registry.new_record(
        browser_id="b1",
        resume_id="s0-abc",
        session_id="s1",
        agent_name="A",
        owner_key=ext._owner_key(context),
        backend="owned",
        generation="abc",
        slot=0,
    )
    manifest = tmp_path / "data" / registry.MANIFEST_NAME
    registry.save(manifest, [record])
    ext._browsers["b1"] = ext._Browser("b1", session, "s1", "A", record=record, manifest=manifest)

    async def scenario() -> None:
        task = asyncio.create_task(ext.browser_close(context, "b1"))
        await session.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "b1" in ext._browsers
        assert not session.closed
        session.release.set()
        # The record drop runs off the loop (Windows icacls), so poll briefly for it.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.closed and "b1" not in ext._browsers and registry.load(manifest)[0] == []:
                break
        assert session.closed
        assert "b1" not in ext._browsers
        assert registry.load(manifest)[0] == []
        assert "recoverable=0" in await ext.browser_list(context)

    asyncio.run(scenario())


def test_sub_agents_sharing_session_and_profile_do_not_share_browsers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    first = ctx(tmp_path, agent="Browser", caller_name="BrowserOne", invocation_type="sub_agent", invocation_id="sub-1")
    second = ctx(
        tmp_path, agent="Browser", caller_name="BrowserTwo", invocation_type="sub_agent", invocation_id="sub-2"
    )
    assert ext._owner_key(first) != ext._owner_key(second)

    mine = ext._open(first)
    theirs = ext._open(second)
    # Each sub-agent gets its own slots, profiles and journals despite sharing a profile name.
    assert mine.slot == theirs.slot == 0

    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_snapshot(second, mine.id))
    assert info.value.code == "browser_owner_mismatch"
    listing = asyncio.run(ext.browser_list(second))
    assert "open=1" in listing
    assert theirs.id in listing
    assert mine.id not in listing
    first_listing = asyncio.run(ext.browser_list(first))
    assert mine.id in first_listing
    assert theirs.id not in first_listing
    resume_id = first_listing.split("resume_id=")[1].split()[0]
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_resume(second, resume_id))
    assert info.value.code == "browser_resume_denied"


def test_concurrent_calls_to_same_sub_agent_are_isolated_by_invocation_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_GUARD", "strict")
    first = ctx(tmp_path, agent="Browser", caller_name="Explore", invocation_type="sub_agent", invocation_id="sub-1")
    second = ctx(tmp_path, agent="Browser", caller_name="Explore", invocation_type="sub_agent", invocation_id="sub-2")
    assert ext._owner_key(first) != ext._owner_key(second)

    mine = ext._open(first)
    # Concurrent call 2 must not see call 1's browser handle, while strict guard protects the whole session.
    assert "open=0" in asyncio.run(ext.browser_list(second))
    call_for_second = ToolCall(name="shell", kind="shell", call_id="c2", arguments={}, context=second)
    with pytest.raises(ToolError) as guard_exc:
        asyncio.run(ext.browser_guard(call_for_second))
    assert guard_exc.value.code == "browser_guard"
    call_for_first = ToolCall(name="shell", kind="shell", call_id="c1", arguments={}, context=first)
    with pytest.raises(ToolError) as guard_err:
        asyncio.run(ext.browser_guard(call_for_first))
    assert guard_err.value.code == "browser_guard"
    with pytest.raises(ToolError) as err:
        asyncio.run(ext.browser_snapshot(second, mine.id))
    assert err.value.code == "browser_owner_mismatch"


def test_workflow_node_isolates_activations_but_preserves_owner_across_retries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    attempt1 = ctx(
        tmp_path, agent="Worker", caller_name="fetch_docs", invocation_type="workflow_node", invocation_id="wf-act-1"
    )
    retry = ctx(
        tmp_path, agent="Worker", caller_name="fetch_docs", invocation_type="workflow_node", invocation_id="wf-act-1"
    )
    next_activation = ctx(
        tmp_path, agent="Worker", caller_name="fetch_docs", invocation_type="workflow_node", invocation_id="wf-act-2"
    )
    assert ext._owner_key(attempt1) == ext._owner_key(retry)
    assert ext._owner_key(attempt1) != ext._owner_key(next_activation)

    opened = ext._open(attempt1)
    assert opened.id in asyncio.run(ext.browser_list(retry))
    assert opened.id not in asyncio.run(ext.browser_list(next_activation))
    with pytest.raises(ToolError) as err:
        asyncio.run(ext.browser_snapshot(next_activation, opened.id))
    assert err.value.code == "browser_owner_mismatch"


def test_side_question_and_distinct_caller_name_are_isolated(tmp_path) -> None:
    main_turn = ctx(tmp_path, agent="Code", caller_name="Code", invocation_type="turn", invocation_id="turn-1")
    side = ctx(tmp_path, agent="Code", caller_name="Code", invocation_type="side_question", invocation_id="sq-1")
    outside_sub = ctx(tmp_path, agent="Code", caller_name="Explore", invocation_type=None, invocation_id=None)
    assert ext._owner_key(main_turn) != ext._owner_key(side)
    assert ext._owner_key(main_turn) != ext._owner_key(outside_sub)
    assert ext._owner_key(side) != ext._owner_key(outside_sub)


def test_the_main_agent_keeps_its_existing_owner_key_across_turns(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Main-agent turns keep the legacy profile/journal key and reuse handles across turns.
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    turn1 = ctx(tmp_path, agent="Code", caller_name="Code", invocation_type="turn", invocation_id="turn-1")
    turn2 = ctx(tmp_path, agent="Code", caller_name="Code", invocation_type="turn", invocation_id="turn-2")
    legacy_key = hashlib.sha256(f"{turn1.session_id}\x00{turn1.agent_name}".encode()).hexdigest()[:16]
    assert ext._owner_key(turn1) == legacy_key
    assert ext._owner_key(turn2) == legacy_key
    assert ext._owner_key(legacy_ctx(tmp_path, agent="Code")) == legacy_key
    assert ext._owner_key(ctx(tmp_path, agent="Code")) == legacy_key

    opened = ext._open(turn1)
    assert opened.id in asyncio.run(ext.browser_list(turn2))


def test_owner_scope_does_not_collide_on_colons(tmp_path) -> None:
    first = ctx(tmp_path, agent="Browser", caller_name="a:b", invocation_type="sub_agent", invocation_id="c")
    second = ctx(tmp_path, agent="Browser", caller_name="a", invocation_type="sub_agent", invocation_id="b:c")
    assert ext._owner_key(first) != ext._owner_key(second)


def test_ephemeral_sub_agent_close_reclaims_owned_state_dir_while_main_agent_keeps_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")

    sub_ctx = ctx(
        tmp_path, agent="Browser", caller_name="Explore", invocation_type="sub_agent", invocation_id="sub-ephemeral-1"
    )
    sub_browser = ext._open(sub_ctx)
    sub_browser.session._owns_state_dir = True
    sub_dir = state_root / "agents" / ext._owner_key(sub_ctx) / f"slot-{sub_browser.slot}"
    (sub_dir / "chrome").mkdir(parents=True, exist_ok=True)
    (sub_dir / "chrome" / "Preferences").write_text("{}", encoding="utf-8")
    asyncio.run(ext.browser_close(sub_ctx, sub_browser.id))
    assert not sub_dir.exists()
    assert not sub_dir.parent.exists()

    main_ctx = ctx(tmp_path, agent="Code", caller_name="Code", invocation_type="turn", invocation_id="turn-1")
    main_browser = ext._open(main_ctx)
    main_browser.session._owns_state_dir = True
    main_dir = state_root / "agents" / ext._owner_key(main_ctx) / f"slot-{main_browser.slot}"
    (main_dir / "chrome").mkdir(parents=True, exist_ok=True)
    (main_dir / "chrome" / "Preferences").write_text("{}", encoding="utf-8")
    asyncio.run(ext.browser_close(main_ctx, main_browser.id))
    assert (main_dir / "chrome" / "Preferences").is_file()

    # Non-owned backends (attach) must never delete shared state_root.
    monkeypatch.setenv("ICODE_BROWSER_CDP_URL", "ws://existing")
    attach_sentinel = state_root / "shared-keep.txt"
    attach_sentinel.write_text("keep", encoding="utf-8")
    attach_ctx = ctx(
        tmp_path, agent="Browser", caller_name="Explore", invocation_type="sub_agent", invocation_id="sub-attach-1"
    )
    attach_browser = ext._open(attach_ctx)
    asyncio.run(ext.browser_close(attach_ctx, attach_browser.id))
    assert attach_sentinel.is_file()


def test_same_owner_concurrent_open_allocates_distinct_slots(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")

    async def scenario() -> None:
        first, second = await asyncio.gather(
            ext.browser_open(ctx(tmp_path, agent="A")),
            ext.browser_open(ctx(tmp_path, agent="A")),
        )
        ids = {str(first).removeprefix("browser_id: "), str(second).removeprefix("browser_id: ")}
        assert len(ids) == 2
        browsers = [ext._browsers[browser_id] for browser_id in ids]
        assert {browser.slot for browser in browsers} == {0, 1}
        for browser in browsers:
            await browser.session.aclose()

    asyncio.run(scenario())


def test_attach_mode_uses_explicit_ids_without_launching(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_CDP_URL", "ws://existing")
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    created: list[object] = []

    class FakeAttachSession:
        def __init__(self, cfg, *, journal_owner: str = "", journal_session: str = "") -> None:
            self.cfg = cfg
            self.journal_owner = journal_owner
            self.journal_session = journal_session
            self.journal = Journal(None)
            self.state = "idle"
            self.snapshot = SimpleNamespace(url="", title="")
            created.append(self)

        async def aclose(self) -> None:
            self.state = "closed"

    monkeypatch.setattr(ext, "BrowserSession", FakeAttachSession)
    context = ctx(tmp_path)
    first = ext._open(context)
    second = ext._open(context)
    assert first.id != second.id
    assert len(created) == 2
    assert all(session.cfg.cdp_url == "ws://existing" for session in created)
    assert created[0].cfg.state_dir != created[1].cfg.state_dir


def test_action_status_routes_through_the_browser_handle(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_browser()
    captured: dict[str, object] = {}

    async def fake_run(browser, name, arguments):
        captured.update(browser=browser.id, name=name, arguments=arguments)
        return "ok"

    monkeypatch.setattr(ext, "_run", fake_run)
    assert asyncio.run(ext.browser_action_status(ctx(tmp_path), "abc", "b1")) == "ok"
    assert captured == {"browser": "b1", "name": "browser_action_status", "arguments": {"action_id": "abc"}}


def test_upload_resolves_a_relative_path_against_ctx_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from icode_browser_use.config import BrowserConfig

    browser = add_browser()
    browser.session.cfg = BrowserConfig(
        allowed_domains=("example.com",),
        state_dir=tmp_path / "state",
        upload_roots=(tmp_path,),
    )
    file = tmp_path / "input.txt"
    file.write_text("x", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_run(_browser, _name, arguments):
        captured.update(arguments)
        return "ok"

    monkeypatch.setattr(ext, "_run", fake_run)
    assert asyncio.run(ext.browser_upload(ctx(tmp_path), "b1", "@e1#1", "input.txt")) == "ok"
    assert captured["path"] == str(file.resolve())


def test_upload_denied_when_upload_roots_unset(tmp_path) -> None:
    from icode_browser_use.config import BrowserConfig

    browser = add_browser()
    browser.session.cfg = BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state")
    file = tmp_path / "secret.env"
    file.write_text("TOKEN=1", encoding="utf-8")
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_upload(ctx(tmp_path), "b1", "@e1#1", "secret.env"))
    assert info.value.code == "browser_upload_denied"
    assert browser.session.cfg.upload_roots == ()


def test_upload_rejects_a_path_outside_the_workspace(tmp_path) -> None:
    add_browser()
    outside = tmp_path.parent / "outside-upload.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        with pytest.raises(ToolError) as info:
            asyncio.run(ext.browser_upload(ctx(tmp_path), "b1", "@e1#1", str(outside)))
        assert info.value.code == "browser_upload_denied"
    finally:
        outside.unlink(missing_ok=True)


def test_stale_ref_maps_to_tool_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_browser()
    context = ctx(tmp_path)

    async def stale(*_args, **_kwargs):
        raise StaleRef("element moved")

    monkeypatch.setattr(ext, "call_tool", stale)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_click(context, "b1", "@e1#1"))
    assert info.value.code == "browser_stale_ref"


def test_optional_high_risk_tools_are_hidden_by_default() -> None:
    names = {tool.name for tool in ext.extension.tools}
    assert "browser_evaluate" not in names
    assert "browser_save_state" not in names


def test_optional_high_risk_tools_register_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    monkeypatch.setenv("ICODE_BROWSER_EVALUATE", "allow")
    monkeypatch.setenv("ICODE_BROWSER_SAVE_STATE", "allow")
    reloaded = importlib.reload(ext)
    try:
        names = {tool.name for tool in reloaded.extension.tools}
        assert {"browser_evaluate", "browser_save_state"} <= names
    finally:
        monkeypatch.delenv("ICODE_BROWSER_EVALUATE", raising=False)
        monkeypatch.delenv("ICODE_BROWSER_SAVE_STATE", raising=False)
        importlib.reload(ext)


@pytest.mark.parametrize("value,enabled", [("allow", True), ("", False)])
def test_optins_without_allowlist(monkeypatch, value, enabled):
    for suffix in ("EVALUATE", "SAVE_STATE"):
        monkeypatch.setenv("ICODE_BROWSER_" + suffix, value)
    try:
        importlib.reload(ext)
        names = {tool.name for tool in ext.extension.tools}
        assert ("browser_evaluate" in names) is enabled
        assert ("browser_save_state" in names) is enabled
    finally:
        for suffix in ("EVALUATE", "SAVE_STATE"):
            monkeypatch.delenv("ICODE_BROWSER_" + suffix)
        importlib.reload(ext)


@pytest.mark.parametrize("value", ["strict", ""])
def test_guard_blocks_shell_only_in_strict_mode(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ICODE_BROWSER_GUARD", value)
    add_browser()
    call = ToolCall(name="run_shell", kind="shell", call_id="c1", arguments={}, context=ctx(tmp_path))
    if value == "strict":
        with pytest.raises(ToolError, match="blocked while ICODE_BROWSER_GUARD=strict"):
            asyncio.run(ext.browser_guard(call))
    else:
        asyncio.run(ext.browser_guard(call))


@pytest.mark.parametrize("roots", ["root", ""])
def test_broker_upload_roots(tmp_path, monkeypatch, roots):
    from icode_browser_use.config import BrowserConfig

    monkeypatch.setenv("ICODE_BROWSER_UPLOAD_ROOTS", roots)
    browser = add_browser()
    browser.session = ext.BrokerSession(BrowserConfig(state_dir=tmp_path), owner="A", session_id="s1")
    upload = tmp_path / "upload.txt"
    upload.write_text("test")

    async def run(*args):
        return "uploaded"

    monkeypatch.setattr(ext, "_run", run)
    if roots:
        assert asyncio.run(ext.browser_upload(ctx(tmp_path), "b1", "file", str(upload))) == "uploaded"
    else:
        with pytest.raises(ToolError, match="upload"):
            asyncio.run(ext.browser_upload(ctx(tmp_path), "b1", "file", str(upload)))


def test_close_releases_and_forgets(tmp_path) -> None:
    browser = add_browser()
    asyncio.run(ext.browser_close(ctx(tmp_path), "b1"))
    assert browser.session.closed is True
    assert "b1" not in ext._browsers
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_close(ctx(tmp_path), "b1"))
    assert info.value.code == "unknown_browser"


def test_strict_guard_blocks_shell_only_when_browser_open(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_GUARD", "strict")
    context = ctx(tmp_path)

    shell = ToolCall(name="run_shell", kind="shell", call_id="c1", arguments={}, context=context)
    asyncio.run(ext.browser_guard(shell))  # no browser: allowed

    add_browser()
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_guard(shell))
    assert info.value.code == "browser_guard"

    read = ToolCall(name="read_file", kind="filesystem.read", call_id="c2", arguments={}, context=context)
    asyncio.run(ext.browser_guard(read))  # reads stay allowed

    write = ToolCall(name="write_file", kind="filesystem.write", call_id="c3", arguments={}, context=context)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_guard(write))
    assert info.value.code == "browser_guard"


def test_tool_signatures_match_core_contracts() -> None:
    by_name = {tool.name: tool for tool in ext.extension.tools}
    assert list(by_name["browser_navigate"].signature.parameters) == ["url", "browser_id"]
    assert list(by_name["browser_fill"].signature.parameters) == [
        "browser_id",
        "target",
        "text",
        "clear",
        "submit",
        "keypress_fallback",
        "expect_condition",
        "expect_target",
        "expect_value",
        "expect_timeout_ms",
        "expect_return",
    ]
    assert list(by_name["browser_press_key"].signature.parameters) == ["browser_id", "key", "target"]
    assert list(by_name["browser_select_option"].signature.parameters) == ["browser_id", "target", "values"]
    assert list(by_name["browser_wait_for"].signature.parameters) == [
        "browser_id",
        "condition",
        "target",
        "value",
        "timeout_ms",
        "return_mode",
        "max_chars",
    ]
    assert list(by_name["browser_scroll"].signature.parameters) == [
        "browser_id",
        "direction",
        "amount",
        "target",
        "to_text",
    ]
    assert list(by_name["browser_extract"].signature.parameters) == [
        "browser_id",
        "query",
        "format",
        "max_chars",
        "offset",
    ]


def test_the_bundled_extension_manifest_matches_the_package_version() -> None:
    """doctor warns when they differ; forgetting to bump the manifest would warn forever."""
    from icode_browser_use import __version__

    manifest_path = Path(__file__).resolve().parents[1] / "src" / "icode_browser_use" / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == __version__


def test_interaction_tools_declare_their_approval() -> None:
    by_name = {tool.name: tool for tool in ext.extension.tools}
    # W08: new write actions need approval; the read-only wait must not.
    assert by_name["browser_press_key"].requires_approval is True
    assert by_name["browser_select_option"].requires_approval is True
    assert by_name["browser_wait_for"].requires_approval is False
    # W12 ④: hover and scroll send no click and no text, but they can still trigger
    # page side effects (hover menus, lazy-load); they are journalled, not approval-gated.
    assert by_name["browser_hover"].requires_approval is False
    assert by_name["browser_scroll"].requires_approval is False
    # W09-2: accepting a dialog can submit or delete, so it is a write action.
    assert by_name["browser_dialog"].requires_approval is True
    # W09-3: listing downloads is read-only; canceling one is a write action.
    assert by_name["browser_downloads"].requires_approval is False
    assert by_name["browser_download_cancel"].requires_approval is True
    # Reading logs is read-only; closing a tab and moving through history/reloading change the page.
    assert by_name["browser_logs"].requires_approval is False
    assert by_name["browser_tab_close"].requires_approval is True
    assert by_name["browser_forward"].requires_approval is True
    assert by_name["browser_reload"].requires_approval is True


def test_closed_browser_is_pruned(tmp_path) -> None:
    browser = add_browser()
    browser.session.state = "closed"

    assert ext._mine(ctx(tmp_path)) == []
    turn = TurnContext.for_testing(tmp_path, prompt="hi", agent_name="A", session_id="s1")
    assert asyncio.run(ext.browser_context(turn)) is None
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_snapshot(ctx(tmp_path), "b1"))
    assert info.value.code == "unknown_browser"


def test_browser_limit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    add_browser(browser_id="b1", slot=0)
    add_browser(browser_id="b2", slot=1)
    with pytest.raises(ToolError) as info:
        ext._open(ctx(tmp_path))
    assert info.value.code == "browser_limit"


def test_profile_slots_are_stable_across_reopen(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    context = ctx(tmp_path)

    first = ext._open(context)
    second = ext._open(context)
    assert {first.slot, second.slot} == {0, 1}
    assert first.session.cfg.state_dir != second.session.cfg.state_dir

    asyncio.run(ext.browser_close(context, first.id))
    again = ext._open(context)
    assert again.slot == 0
    assert again.session.cfg.state_dir == first.session.cfg.state_dir


def test_bridge_uses_the_shared_discovery_directory(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_BRIDGE", "1")
    context = ctx(tmp_path)
    first = ext._open(context)
    second = ext._open(context)
    assert first.session.cfg.bridge is True
    assert first.session.cfg.state_dir == second.session.cfg.state_dir
    assert first.session.cfg.state_dir != tmp_path / "agents"


def test_extension_uses_broker_session_when_enabled(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_BROKER", "1")

    class FakeBrokerSession:
        def __init__(self, cfg, *, owner: str, session_id: str) -> None:
            self.cfg = cfg
            self.owner = owner
            self.session_id = session_id
            self.state = "ready"
            self.snapshot = SimpleNamespace(url="", title="")

        async def call_tool(self, _tool, _arguments):
            return [{"type": "text", "text": "broker"}]

        def mark_disconnected(self) -> None:
            self.state = "failed"

        async def aclose(self) -> None:
            self.state = "closed"

    monkeypatch.setattr(ext, "BrokerSession", FakeBrokerSession)
    browser = ext._open(ctx(tmp_path))
    assert isinstance(browser.session, FakeBrokerSession)
    assert browser.session.owner == ext._owner_key(ctx(tmp_path))


def test_connection_closed_marks_disconnected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = add_browser()

    async def fail(*_args, **_kwargs):
        raise ConnectionClosed("gone")

    monkeypatch.setattr(ext, "call_tool", fail)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_snapshot(ctx(tmp_path), "b1"))
    assert info.value.code == "browser_disconnected"
    assert browser.session.disconnected is True


def test_browser_error_maps_to_tool_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_browser()

    async def fail(*_args, **_kwargs):
        raise BadRequest("bad target")

    monkeypatch.setattr(ext, "call_tool", fail)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_snapshot(ctx(tmp_path), "b1"))
    assert info.value.code == "browser_bad_request"


def test_fill_passes_core_arguments(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_browser()
    captured: dict[str, object] = {}

    async def fake_call(_session, _tool, arguments):
        captured.update(arguments)
        return [{"type": "text", "text": "ok"}]

    monkeypatch.setattr(ext, "call_tool", fake_call)
    text = asyncio.run(ext.browser_fill(ctx(tmp_path), "b1", "target", "hello"))
    assert text == "ok"
    assert captured == {
        "target": "target",
        "text": "hello",
        "clear": True,
        "submit": False,
        "keypress_fallback": False,
    }


def _png(payload: bytes = b"payload", *, width: int = 3, height: int = 2) -> bytes:
    """A minimal PNG header with real IHDR dimensions, so the adapter can read them back."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + payload
    )


def _image_block(data: bytes, mime: str = "image/png") -> list[dict[str, object]]:
    return [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}]


def test_screenshot_writes_a_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_browser()
    payload = _png()

    async def fake_call(_session, _tool, arguments):
        assert arguments == {"full_page": False}
        return _image_block(payload)

    monkeypatch.setattr(ext, "call_tool", fake_call)
    text = asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))
    first = text.splitlines()[0]
    path = first.removeprefix("Screenshot saved to ").split(" (")[0]
    assert path.endswith(".png")
    assert Path(path).read_bytes() == payload
    assert Path(path).parent == tmp_path / "session" / "screenshots"


def test_screenshot_reports_format_pixels_bytes_and_session_relative_path(tmp_path, monkeypatch) -> None:
    add_browser()
    payload = _png(width=640, height=480)

    async def fake_call(_session, _tool, arguments):
        return _image_block(payload, "image/jpeg")

    monkeypatch.setattr(ext, "call_tool", fake_call)
    text = asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))

    lines = text.splitlines()
    assert lines[0].endswith(f"(image/jpeg, 640x480, {len(payload)} bytes)")
    assert lines[1].startswith("Session file: screenshots/screenshot-")
    assert lines[1].endswith(".jpg")
    assert lines[2] == "Use view_image with the absolute path to inspect it."
    assert Path(lines[0].removeprefix("Screenshot saved to ").split(" (")[0]).is_file()


def test_screenshot_names_are_unique_and_writes_are_atomic(tmp_path, monkeypatch) -> None:
    add_browser()

    async def fake_call(_session, _tool, arguments):
        return _image_block(_png())

    monkeypatch.setattr(ext, "call_tool", fake_call)
    # A frozen clock must not let two captures share a name.
    monkeypatch.setattr(ext, "time", SimpleNamespace(strftime=lambda *_: "20260101T000000", gmtime=lambda: None))
    context = ctx(tmp_path, with_session_dir=True)
    first = asyncio.run(ext.browser_screenshot(context, "b1"))
    second = asyncio.run(ext.browser_screenshot(context, "b1"))

    directory = tmp_path / "session" / "screenshots"
    names = sorted(path.name for path in directory.iterdir())
    assert len(names) == 2, names
    assert names[0] != names[1]
    assert all(name.startswith("screenshot-20260101T000000-") and name.endswith(".png") for name in names)
    assert not list(directory.glob("*.tmp")), "the temporary file must be renamed, not left behind"
    for text in (first, second):
        assert Path(text.splitlines()[0].removeprefix("Screenshot saved to ").split(" (")[0]).is_file()


def test_a_failed_write_leaves_neither_a_file_nor_a_temporary(tmp_path, monkeypatch) -> None:
    add_browser()

    async def fake_call(_session, _tool, arguments):
        return _image_block(_png())

    def refuse(_descriptor):
        raise OSError("disk is full")

    monkeypatch.setattr(ext, "call_tool", fake_call)
    monkeypatch.setattr(ext.os, "fsync", refuse)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))
    assert info.value.code == "browser_screenshot_failed"
    directory = tmp_path / "session" / "screenshots"
    assert list(directory.iterdir()) == [], "a failed capture must not leave a file or a temporary behind"


def test_retention_never_removes_the_screenshot_it_just_wrote(tmp_path) -> None:
    directory = tmp_path / "screenshots"
    directory.mkdir()
    # The current file is *older* by name and mtime order ties, so only the
    # caller's "current" marker can keep it alive.
    written = directory / "screenshot-20200101T000000-aaaaaaaa.png"
    written.write_bytes(b"new")
    other = directory / "screenshot-20290101T000000-zzzzzzzz.png"
    other.write_bytes(b"other")
    for path in (written, other):
        os.utime(path, (2_000_000_000, 2_000_000_000))

    ext._prune_screenshots(directory, 1, written)

    assert written.is_file(), "retention deleted the capture it had just reported"
    assert not other.exists()
    assert sorted(path.name for path in directory.iterdir()) == [written.name]


def test_retention_skips_symlinks_and_foreign_extensions(tmp_path) -> None:
    directory = tmp_path / "screenshots"
    directory.mkdir()
    keep = directory / "screenshot-20260101T000000-aaaaaaaa.png"
    keep.write_bytes(b"keep")
    notes = directory / "screenshot-notes.txt"
    notes.write_bytes(b"not an image")
    target = tmp_path / "outside.png"
    target.write_bytes(b"outside")
    link = directory / "screenshot-20250101T000000-bbbbbbbb.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem does not allow creating symlinks")

    ext._prune_screenshots(directory, 1, keep)

    assert keep.is_file() and notes.is_file() and link.is_symlink() and target.is_file()


def test_retention_reclaims_only_old_temporary_files(tmp_path) -> None:
    directory = tmp_path / "screenshots"
    directory.mkdir()
    current = directory / "screenshot-20260101T000000-aaaaaaaa.png"
    current.write_bytes(b"current")
    crashed = directory / ".screenshot-20200101T000000-aaaaaaaa.png.deadbeef.tmp"
    crashed.write_bytes(b"crashed")
    os.utime(crashed, (1_000, 1_000))
    live = directory / ".screenshot-20260101T000000-cccccccc.png.feedface.tmp"
    live.write_bytes(b"live")

    ext._prune_screenshots(directory, 10, current)

    assert not crashed.exists(), "a crashed writer's temporary file is never reclaimed"
    assert live.exists(), "a live writer's temporary file must survive"


def test_a_capture_never_overwrites_an_existing_name(tmp_path, monkeypatch) -> None:
    add_browser()
    directory = tmp_path / "session" / "screenshots"
    directory.mkdir(parents=True)
    existing = directory / "screenshot-20260101T000000-aaaaaaaa.png"
    existing.write_bytes(b"existing")

    async def fake_call(_session, _tool, arguments):
        return _image_block(_png())

    tokens = iter(["aaaaaaaa", "bbbbbbbb", "tmpdead01"])
    monkeypatch.setattr(ext, "call_tool", fake_call)
    monkeypatch.setattr(ext.secrets, "token_hex", lambda _n: next(tokens))
    monkeypatch.setattr(ext.time, "strftime", lambda *_: "20260101T000000")
    monkeypatch.setattr(ext.time, "gmtime", lambda: None)

    text = asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))

    written = Path(text.splitlines()[0].removeprefix("Screenshot saved to ").split(" (")[0])
    assert written.name == "screenshot-20260101T000000-bbbbbbbb.png"
    assert existing.read_bytes() == b"existing", "an existing capture was overwritten"
    assert written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_screenshot_retention_prunes_only_its_own_old_files(tmp_path, monkeypatch) -> None:
    browser = add_browser()
    browser.session.cfg.screenshot_keep = 2
    directory = tmp_path / "session" / "screenshots"
    directory.mkdir(parents=True)
    stale = []
    for index in range(3):
        path = directory / f"screenshot-2020010{index}T000000-aaaaaaaa.png"
        path.write_bytes(b"stale")
        os.utime(path, (1_000 + index, 1_000 + index))
        stale.append(path)
    foreign = directory / "other-tool.png"
    foreign.write_bytes(b"not ours")

    async def fake_call(_session, _tool, arguments):
        return _image_block(_png())

    monkeypatch.setattr(ext, "call_tool", fake_call)
    asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))
    asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))

    ours = [path for path in directory.iterdir() if path.name.startswith("screenshot-")]
    assert len(ours) == 2
    assert not any(path.exists() for path in stale), "older screenshots beyond the keep count are removed"
    assert foreign.read_bytes() == b"not ours", "retention must never touch another writer's files"


def test_screenshot_uses_the_user_folder_without_a_session(tmp_path, monkeypatch) -> None:
    add_browser()

    async def fake_call(_session, _tool, arguments):
        return _image_block(_png())

    monkeypatch.setattr(ext, "call_tool", fake_call)
    text = asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=False), "b1"))

    assert (tmp_path / "data" / "screenshots").is_dir()
    assert text.splitlines()[1].startswith("Session file: screenshots/")


def test_screenshot_forwards_full_page_and_target(tmp_path, monkeypatch) -> None:
    add_browser()
    captured: dict[str, object] = {}

    async def fake_call(_session, _tool, arguments):
        captured.update(arguments)
        return _image_block(_png())

    monkeypatch.setattr(ext, "call_tool", fake_call)
    asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1", full_page=True, target="Send"))

    assert captured == {"full_page": True, "target": "Send"}


def test_screenshot_write_failure_is_a_tool_error(tmp_path, monkeypatch) -> None:
    add_browser()

    async def fake_call(_session, _tool, arguments):
        return _image_block(_png())

    def refuse(*_args, **_kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr(ext, "call_tool", fake_call)
    monkeypatch.setattr(ext, "_write_screenshot", refuse)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_screenshot(ctx(tmp_path, with_session_dir=True), "b1"))
    assert info.value.code == "browser_screenshot_failed"


def test_navigate_uses_browser_open_handle(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[int] = []

    def fake_open(_context: ToolContext) -> ext._Browser:
        opened.append(1)
        return add_browser()

    async def fake_run(_browser, _name, _arguments):
        return "ok"

    monkeypatch.setattr(ext, "_open", fake_open)
    monkeypatch.setattr(ext, "_run", fake_run)

    asyncio.run(ext.browser_open(ctx(tmp_path)))
    asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://example.com/a", "b1"))
    asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://example.com/b", "b1"))
    assert len(opened) == 1


def test_navigate_with_several_browsers_requires_an_id(tmp_path) -> None:
    add_browser(browser_id="b1", slot=0)
    add_browser(browser_id="b2", slot=1)
    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://example.com"))
    assert info.value.code == "browser_id_required"


def test_context_hides_query_tokens(tmp_path) -> None:
    add_browser()
    ext._browsers["b1"].url = "https://example.com/path?token=secret"
    ext._browsers["b1"].title = "Example"
    turn = TurnContext.for_testing(tmp_path, prompt="hi", agent_name="A", session_id="s1")
    text = asyncio.run(ext.browser_context(turn))
    assert text is not None
    assert "secret" not in text
    assert "https://example.com" not in text
    assert "Open browsers: 1" in text
    assert "b1" not in text


def test_playbook_is_byte_stable_and_contains_w13_rules(tmp_path) -> None:
    from icode_browser_use.prompts import BROWSER_PLAYBOOK

    add_browser()
    turn = TurnContext.for_testing(tmp_path, prompt="hi", agent_name="A", session_id="s1")
    first = asyncio.run(ext.browser_context(turn))
    second = asyncio.run(ext.browser_context(turn))
    assert first == second
    assert first is not None
    assert BROWSER_PLAYBOOK in first
    for phrase in (
        "wait_for(editable)",
        "never use page-wide wait_for(stable)",
        "retry at most once",
        "one primary signal",
        "over routine browser_screenshot + view_image",
        "state_dir_locked",
        "UIAutomation",
    ):
        assert phrase in BROWSER_PLAYBOOK


def test_navigate_one_step_keeps_allocated_handle_on_login_required(tmp_path, monkeypatch) -> None:
    from icode_browser_use.errors import LoginRequired

    browser = add_browser(browser_id="b-login", slot=0)
    ext._browsers.clear()

    def fake_open(_context: ToolContext) -> ext._Browser:
        ext._browsers[browser.id] = browser
        return browser

    async def fake_call(_browser, _name, _arguments):
        raise ToolError(LoginRequired("需要登录").render(), code=LoginRequired.code)

    monkeypatch.setattr(ext, "_open", fake_open)
    monkeypatch.setattr(ext, "_call", fake_call)

    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://example.com/login"))

    assert info.value.code == "browser_login_required"
    assert f"browser_id: {browser.id}" in info.value.message
    assert browser.id in ext._browsers
    assert not browser.session.closed


def test_browser_close_reports_blocked_origins_for_browser_blocked(tmp_path, monkeypatch) -> None:
    from icode_browser_use.errors import Blocked

    browser = add_browser(browser_id="b-blocked", slot=0)

    async def fake_call(_browser, _name, _arguments):
        exc = Blocked("目标不在 allowlist 中", blocked_origin="https://denied.example:443")
        raise ToolError(exc.render(), code=exc.code) from exc

    monkeypatch.setattr(ext, "_call", fake_call)

    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://denied.example/secret", browser.id))
    assert info.value.code == "browser_blocked"

    summary = asyncio.run(ext.browser_close(ctx(tmp_path), browser.id))
    assert "blocked_origins: https://denied.example" in summary


def test_browser_close_records_redirect_target_origin_not_source_origin(tmp_path, monkeypatch) -> None:
    from icode_browser_use.errors import Blocked

    browser = add_browser(browser_id="b-redir", slot=0)
    browser.url = "https://allowed.example/start"

    async def fake_call(_browser, _name, _arguments):
        exc = Blocked(
            "net::ERR_BLOCKED_BY_CLIENT (https://blocked-b.example:443)", blocked_origin="https://blocked-b.example:443"
        )
        raise ToolError(exc.render(), code=exc.code) from exc

    monkeypatch.setattr(ext, "_call", fake_call)

    with pytest.raises(ToolError) as info:
        asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://allowed.example/redir", browser.id))
    assert info.value.code == "browser_blocked"

    summary = asyncio.run(ext.browser_close(ctx(tmp_path), browser.id))
    assert "blocked_origins: https://blocked-b.example" in summary
    assert "allowed.example" not in summary.split("blocked_origins:", 1)[1].splitlines()[0]


def test_record_origin_strips_credentials_from_visited_and_blocked_origins(tmp_path, monkeypatch) -> None:
    from icode_browser_use.errors import Blocked

    browser = add_browser(browser_id="b-creds", slot=0)
    browser.session.snapshot = SimpleNamespace(url="https://alice:topsecret@allowed.example/app?k=1", title="App")

    async def fake_call(_browser, _name, _arguments):
        exc = Blocked("目标不在域名白名单内")
        raise ToolError(exc.render(), code=exc.code) from exc

    monkeypatch.setattr(ext, "_call", fake_call)

    with pytest.raises(ToolError):
        asyncio.run(ext.browser_navigate(ctx(tmp_path), "https://bob:hunter2@denied.example/secret", browser.id))

    summary = asyncio.run(ext.browser_close(ctx(tmp_path), browser.id))
    assert "alice" not in summary and "topsecret" not in summary
    assert "bob" not in summary and "hunter2" not in summary
    assert "visited_origins: https://allowed.example" in summary
    assert "blocked_origins: https://denied.example" in summary
    assert ext._origin("https://alice:topsecret@allowed.example/path?t=1") == "https://allowed.example"


def test_navigate_redirect_blocked_reports_target_origin_from_policy() -> None:
    from icode_browser_use.actions import navigate
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.errors import Blocked
    from icode_browser_use.policy import NetworkPolicy

    policy = NetworkPolicy(("allowed.example",))

    class FakeClient:
        async def send(self, method: str, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                await policy._on_request_paused(
                    {
                        "requestId": "req-1",
                        "resourceType": "Document",
                        "request": {"url": "https://user:pw@blocked-b.example/target"},
                    },
                    session_id,
                )
                return {"errorText": "net::ERR_BLOCKED_BY_CLIENT"}
            return {}

    session = SimpleNamespace(
        cfg=BrowserConfig(allowed_domains=("allowed.example",)),
        snapshot=SimpleNamespace(url="https://allowed.example/"),
        session_id="s1",
        nav_seq=0,
        _policy=policy,
        require_client=lambda: FakeClient(),
    )

    with pytest.raises(Blocked) as info:
        asyncio.run(navigate.run(session, {"url": "https://allowed.example/redir"}))
    assert info.value.blocked_origin == "https://blocked-b.example"
    assert "user" not in info.value.message and "pw" not in info.value.message
    assert "https://blocked-b.example" in info.value.message


def test_concurrent_close_and_open_does_not_delete_new_slot_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    sub_ctx = ctx(
        tmp_path,
        session="s-race",
        agent="Worker",
        caller_name="worker_tool",
        invocation_type="sub_agent",
        invocation_id="inv-race",
    )

    entered = threading.Event()
    release = threading.Event()
    real_cleanup = ext._cleanup_ephemeral_dir

    def paused_cleanup(target: Path) -> None:
        entered.set()
        release.wait(timeout=5.0)
        real_cleanup(target)

    monkeypatch.setattr(ext, "_cleanup_ephemeral_dir", paused_cleanup)

    async def scenario() -> tuple[Path, Path]:
        opened = await ext.browser_open(sub_ctx)
        b1_id = opened.removeprefix("browser_id: ").strip()
        b1 = ext._browsers[b1_id]
        b1.session._owns_state_dir = True
        old_dir = b1.session.cfg.state_dir
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "old.txt").write_text("old", encoding="utf-8")

        close_task = asyncio.create_task(ext.browser_close(sub_ctx, b1_id))
        await asyncio.to_thread(entered.wait, 5.0)
        assert entered.is_set()

        opened2 = await ext.browser_open(sub_ctx)
        b2_id = opened2.removeprefix("browser_id: ").strip()
        b2 = ext._browsers[b2_id]
        new_dir = b2.session.cfg.state_dir
        new_dir.mkdir(parents=True, exist_ok=True)
        sentinel = new_dir / "sentinel.txt"
        sentinel.write_text("fresh", encoding="utf-8")

        release.set()
        await close_task
        return old_dir, sentinel

    old_dir, sentinel = asyncio.run(scenario())
    assert sentinel.is_file(), "closing the old browser deleted the new browser's state_dir"
    assert sentinel.read_text(encoding="utf-8") == "fresh"
    assert not (old_dir / "old.txt").exists()


def test_concurrent_close_with_locked_rename_holds_slot_until_cleanup_finishes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    sub_ctx = ctx(
        tmp_path,
        session="s-lock",
        agent="Worker",
        caller_name="worker_tool",
        invocation_type="sub_agent",
        invocation_id="inv-lock",
    )

    entered = threading.Event()
    release = threading.Event()
    real_cleanup = ext._cleanup_ephemeral_dir

    def paused_cleanup(target: Path) -> None:
        entered.set()
        release.wait(timeout=5.0)
        real_cleanup(target)

    def fail_rename(self: Path, target: Path) -> Path:
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(ext, "_cleanup_ephemeral_dir", paused_cleanup)
    monkeypatch.setattr(Path, "rename", fail_rename)

    async def scenario() -> tuple[int, Path]:
        opened = await ext.browser_open(sub_ctx)
        b1_id = opened.removeprefix("browser_id: ").strip()
        b1 = ext._browsers[b1_id]
        b1.session._owns_state_dir = True
        old_dir = b1.session.cfg.state_dir
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "old.txt").write_text("old", encoding="utf-8")

        close_task = asyncio.create_task(ext.browser_close(sub_ctx, b1_id))
        await asyncio.to_thread(entered.wait, 5.0)
        assert entered.is_set()

        opened2 = await ext.browser_open(sub_ctx)
        b2_id = opened2.removeprefix("browser_id: ").strip()
        b2 = ext._browsers[b2_id]
        sentinel = b2.session.cfg.state_dir / "sentinel.txt"
        b2.session.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("safe", encoding="utf-8")

        release.set()
        await close_task
        return b2.slot, sentinel

    slot2, sentinel = asyncio.run(scenario())
    assert slot2 == 1, "when rename fails, slot 0 must stay reserved until cleanup completes"
    assert sentinel.is_file()
    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_save_state_archive_survives_ephemeral_sub_agent_close(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from icode_browser_use.actions import save_state

    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    monkeypatch.setenv("ICODE_BROWSER_SAVE_STATE", "allow")
    sub_ctx = ctx(
        tmp_path,
        session="s-save",
        agent="Worker",
        caller_name="worker_tool",
        invocation_type="sub_agent",
        invocation_id="inv-save",
    )

    class FakeClient:
        async def send(self, method: str, params=None, session_id=None, timeout=None):
            if method == "Runtime.evaluate":
                expr = (params or {}).get("expression", "")
                if "localStorage" in expr:
                    return {"result": {"value": '{"token":"abc"}'}}
                return {"result": {"value": "https://example.com"}}
            if method == "Network.getCookies":
                return {"cookies": [{"name": "sid", "value": "xyz"}]}
            return {}

    async def scenario() -> tuple[Path, Path]:
        opened = await ext.browser_open(sub_ctx)
        browser_id = opened.removeprefix("browser_id: ").strip()
        browser = ext._browsers[browser_id]
        browser.session._owns_state_dir = True
        slot_dir = browser.session.cfg.state_dir
        slot_dir.mkdir(parents=True, exist_ok=True)
        (slot_dir / "profile.dat").write_text("temp", encoding="utf-8")

        monkeypatch.setattr(browser.session, "require_client", lambda: FakeClient())
        blocks = await save_state.run(browser.session, {"name": "deliverable"})
        first_line = blocks[0]["text"].splitlines()[0]
        exported = Path(first_line.split("：", 1)[1].strip())
        assert exported.is_file()

        await ext.browser_close(sub_ctx, browser_id)
        return slot_dir, exported

    slot_dir, exported = asyncio.run(scenario())
    assert not slot_dir.exists(), "ephemeral slot dir should still be reclaimed on close"
    assert exported.is_file(), "explicitly exported state archive must survive browser_close"
    assert "deliverable.json" == exported.name
    assert slot_dir not in exported.parents


def test_idle_timeout_reclaims_abandoned_sub_agent_but_keeps_workflow_node(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0.02")

    sub_ctx = ctx(
        tmp_path,
        session="s-idle",
        agent="Worker",
        caller_name="worker_tool",
        invocation_type="sub_agent",
        invocation_id="inv-abandoned",
        with_session_dir=True,
    )
    wf_ctx = ctx(
        tmp_path,
        session="s-idle",
        agent="Worker",
        caller_name="step_fetch",
        invocation_type="workflow_node",
        invocation_id="inv-wf-retry",
        with_session_dir=True,
    )

    async def scenario() -> tuple[str, Path, str, Path, Path]:
        sub_id = (await ext.browser_open(sub_ctx)).removeprefix("browser_id: ").strip()
        wf_id = (await ext.browser_open(wf_ctx)).removeprefix("browser_id: ").strip()
        sub_browser = ext._browsers[sub_id]
        wf_browser = ext._browsers[wf_id]

        sub_dir = sub_browser.session.cfg.state_dir
        wf_dir = wf_browser.session.cfg.state_dir
        sub_dir.mkdir(parents=True, exist_ok=True)
        wf_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "chrome-data.bin").write_bytes(b"x" * 1024)
        (wf_dir / "chrome-data.bin").write_bytes(b"y" * 1024)

        for b in (sub_browser, wf_browser):
            b.session._state = "ready"
            b.session._started_once = True
            b.session._owns_state_dir = True
            b.session._last_active = 0.0

        await asyncio.wait_for(
            asyncio.gather(
                sub_browser.session._idle_watchdog(),
                wf_browser.session._idle_watchdog(),
            ),
            timeout=2.0,
        )
        manifest = registry.manifest_path(sub_ctx)
        return sub_id, sub_dir, wf_id, wf_dir, manifest

    sub_id, sub_dir, wf_id, wf_dir, manifest = asyncio.run(scenario())
    records, _ = registry.load(manifest)
    record_ids = {r.browser_id for r in records}

    # One-shot sub-agent is completely reclaimed on idle timeout.
    assert sub_id not in ext._browsers
    assert sub_id not in record_ids
    assert not sub_dir.exists()

    # Retryable workflow_node keeps its handle, manifest record, and profile dir.
    assert wf_id in ext._browsers
    assert ext._browsers[wf_id].session.state == "idle"
    assert wf_id in record_ids
    assert wf_dir.is_dir()


def test_navigate_lock_conflict_rollback_does_not_delete_lock_holders_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from icode_browser_use import locking
    from icode_browser_use import session as session_mod

    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    wf_ctx = ctx(
        tmp_path,
        session="s-lock-conflict",
        agent="Worker",
        caller_name="step_fetch",
        invocation_type="workflow_node",
        invocation_id="inv-wf-locked",
        with_session_dir=True,
    )
    owner_key = ext._owner_key(wf_ctx)
    slot_dir = state_root / "agents" / owner_key / "slot-0"
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder_file = slot_dir / "holder.txt"
    holder_file.write_text("owned-by-holder", encoding="utf-8")

    # Fail fast on lock contention instead of waiting the default lock timeout.
    real_acquire = locking.acquire_instance_lock
    monkeypatch.setattr(session_mod, "acquire_instance_lock", lambda path: real_acquire(path, wait=0.0))

    async def scenario() -> None:
        lock = await real_acquire(slot_dir, wait=0.0)
        try:
            with pytest.raises(ToolError) as info:
                await ext.browser_navigate(wf_ctx, "https://example.com")
            assert "state_dir_locked" in info.value.message
            # Holder's directory, sentinel file, and lock must still be intact.
            assert slot_dir.is_dir()
            assert holder_file.is_file()
            assert holder_file.read_text(encoding="utf-8") == "owned-by-holder"
            assert locking.probe_instance_lock(slot_dir).locked
        finally:
            lock.release()

    asyncio.run(scenario())
    # Implicitly allocated handle and manifest record were rolled back.
    assert ext._browsers == {}
    records, _ = registry.load(registry.manifest_path(wf_ctx))
    assert records == []


def test_closing_unstarted_handle_skips_cleanup_and_never_deletes_later_slot_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    sub_ctx = ctx(
        tmp_path,
        session="s-unstarted",
        agent="Worker",
        caller_name="worker_tool",
        invocation_type="sub_agent",
        invocation_id="inv-unstarted",
    )

    cleaned: list[Path] = []
    monkeypatch.setattr(ext, "_cleanup_ephemeral_dir", lambda target: cleaned.append(target))

    async def scenario() -> Path:
        b1_id = (await ext.browser_open(sub_ctx)).removeprefix("browser_id: ").strip()
        await ext.browser_close(sub_ctx, b1_id)
        b2_id = (await ext.browser_open(sub_ctx)).removeprefix("browser_id: ").strip()
        b2_dir = ext._browsers[b2_id].session.cfg.state_dir
        b2_dir.mkdir(parents=True, exist_ok=True)
        sentinel = b2_dir / "new-edge.txt"
        sentinel.write_text("alive", encoding="utf-8")
        return sentinel

    sentinel = asyncio.run(scenario())
    assert cleaned == [], "closing an unstarted handle with no dir must not schedule background rmtree"
    assert sentinel.is_file()


def test_concurrent_close_of_same_handle_shares_single_cleanup_task(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    sub_ctx = ctx(
        tmp_path,
        session="s-dup-close",
        agent="Worker",
        caller_name="worker_tool",
        invocation_type="sub_agent",
        invocation_id="inv-dup-close",
    )

    entered = threading.Event()
    release = threading.Event()
    cleanup_calls: list[Path] = []
    real_cleanup = ext._cleanup_ephemeral_dir

    def paused_cleanup(target: Path) -> None:
        cleanup_calls.append(target)
        entered.set()
        release.wait(timeout=5.0)
        real_cleanup(target)

    monkeypatch.setattr(ext, "_cleanup_ephemeral_dir", paused_cleanup)

    async def scenario() -> Path:
        b1_id = (await ext.browser_open(sub_ctx)).removeprefix("browser_id: ").strip()
        b1 = ext._browsers[b1_id]
        b1.session._owns_state_dir = True
        old_dir = b1.session.cfg.state_dir
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "old.txt").write_text("old", encoding="utf-8")

        # Slow down aclose slightly so both concurrent browser_close calls see b1 in _browsers.
        real_aclose = b1.session.aclose

        async def slow_aclose() -> None:
            await asyncio.sleep(0.02)
            await real_aclose()

        monkeypatch.setattr(b1.session, "aclose", slow_aclose)

        close1 = asyncio.create_task(ext.browser_close(sub_ctx, b1_id))
        close2 = asyncio.create_task(ext.browser_close(sub_ctx, b1_id))
        await asyncio.to_thread(entered.wait, 5.0)
        assert entered.is_set()

        b2_id = (await ext.browser_open(sub_ctx)).removeprefix("browser_id: ").strip()
        b2_dir = ext._browsers[b2_id].session.cfg.state_dir
        b2_dir.mkdir(parents=True, exist_ok=True)
        sentinel = b2_dir / "fresh.txt"
        sentinel.write_text("fresh", encoding="utf-8")

        release.set()
        await asyncio.gather(close1, close2)
        return sentinel

    sentinel = asyncio.run(scenario())
    assert len(cleanup_calls) == 1, "concurrent closes of the same handle must share one cleanup task"
    assert sentinel.is_file()
    assert sentinel.read_text(encoding="utf-8") == "fresh"


def test_save_state_isolates_archives_across_slots_and_sessions(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from icode_browser_use.actions import save_state

    state_root = tmp_path / "state"
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(state_root))
    monkeypatch.setenv("ICODE_BROWSER_SAVE_STATE", "allow")

    chat1_ctx = ctx(tmp_path, session="chat-1", agent="A", invocation_type="turn")
    chat2_ctx = ctx(tmp_path, session="chat-2", agent="A", invocation_type="turn")

    def fake_client(marker: str):
        class FakeClient:
            async def send(self, method: str, params=None, session_id=None, timeout=None):
                if method == "Runtime.evaluate":
                    expr = (params or {}).get("expression", "")
                    if "localStorage" in expr:
                        return {"result": {"value": json.dumps({"marker": marker})}}
                    return {"result": {"value": "https://example.com"}}
                if method == "Network.getCookies":
                    return {"cookies": [{"name": "m", "value": marker}]}
                return {}

        return FakeClient()

    async def scenario() -> list[tuple[Path, str]]:
        b1_id = (await ext.browser_open(chat1_ctx)).removeprefix("browser_id: ").strip()
        b2_id = (await ext.browser_open(chat1_ctx)).removeprefix("browser_id: ").strip()
        b3_id = (await ext.browser_open(chat2_ctx)).removeprefix("browser_id: ").strip()

        results: list[tuple[Path, str]] = []
        for bid, marker in ((b1_id, "slot0-chat1"), (b2_id, "slot1-chat1"), (b3_id, "slot0-chat2")):
            browser = ext._browsers[bid]
            monkeypatch.setattr(browser.session, "require_client", lambda m=marker: fake_client(m))
            blocks = await save_state.run(browser.session, {"name": "example"})
            exported = Path(blocks[0]["text"].splitlines()[0].split("：", 1)[1].strip())
            results.append((exported, marker))
            assert browser.session.cfg.state_dir not in exported.parents

        await ext.browser_close(chat1_ctx, b1_id)
        await ext.browser_close(chat1_ctx, b2_id)
        await ext.browser_close(chat2_ctx, b3_id)
        return results

    results = asyncio.run(scenario())
    paths = [p for p, _ in results]
    assert len(set(paths)) == 3, f"expected 3 distinct archive paths, got {paths}"
    for path, marker in results:
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["localStorage"]["https://example.com"]["marker"] == marker
