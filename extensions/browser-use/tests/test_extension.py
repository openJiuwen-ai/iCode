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
from chrys.extensions import ToolCall, ToolContext, ToolError, TurnContext

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
    yield
    ext._browsers.clear()


def ctx(
    tmp_path,
    *,
    agent: str = "A",
    session: str = "s1",
    with_session_dir: bool = False,
) -> ToolContext:
    return ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=(tmp_path / "session") if with_session_dir else None,
        agent_name=agent,
        session_id=session,
    )


def build_ctx(
    tmp_path,
    *,
    agent: str = "A",
    session: str = "s1",
    agent_id: str | None = None,
):
    """A context as the host builds it; installed hosts before 0.36 have no ``agent_id``."""
    base = ctx(tmp_path, agent=agent, session=session)
    if agent_id is None:
        return base
    return SimpleNamespace(
        cwd=base.cwd,
        workspace_dirs=base.workspace_dirs,
        agent_name=base.agent_name,
        session_id=base.session_id,
        agent_id=agent_id,
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
        for _ in range(10):
            await asyncio.sleep(0)
            if session.closed and "b1" not in ext._browsers:
                break
        assert session.closed
        assert "b1" not in ext._browsers
        assert registry.load(manifest)[0] == []
        assert "recoverable=0" in await ext.browser_list(context)

    asyncio.run(scenario())


def test_builds_sharing_session_and_profile_do_not_share_browsers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    first = build_ctx(tmp_path, agent_id="sub:one")
    second = build_ctx(tmp_path, agent_id="sub:two")
    assert ext._owner_key(first) != ext._owner_key(second)

    mine = ext._open(first)
    theirs = ext._open(second)
    # Each build gets its own slots, profiles and journals despite identical fields.
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


def test_the_main_agent_keeps_its_existing_owner_key(tmp_path) -> None:
    # The main agent has no agent_id, so its profile/journal key must not move.
    context = ctx(tmp_path)
    legacy_key = hashlib.sha256(f"{context.session_id}\x00{context.agent_name}".encode()).hexdigest()[:16]
    assert ext._owner_key(context) == legacy_key
    assert ext._owner_key(build_ctx(tmp_path, agent_id=None)) == legacy_key
    assert ext._owner_key(context) != ext._owner_key(build_ctx(tmp_path, agent_id="sub:one"))


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
    browser.session.cfg = BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state")
    file = tmp_path / "input.txt"
    file.write_text("x", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_run(_browser, _name, arguments):
        captured.update(arguments)
        return "ok"

    monkeypatch.setattr(ext, "_run", fake_run)
    assert asyncio.run(ext.browser_upload(ctx(tmp_path), "b1", "@e1#1", "input.txt")) == "ok"
    assert captured["path"] == str(file.resolve())


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
