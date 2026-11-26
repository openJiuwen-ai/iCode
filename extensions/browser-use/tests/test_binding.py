# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("chrys.service.extensions")

from chrys.service.extensions import bind_extension, discover_extensions, load_extensions

EXPECTED_TOOLS = [
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
    "browser_drag",
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
    "browser_network_body",
    "browser_back",
    "browser_forward",
    "browser_reload",
    "browser_screenshot",
    "browser_close",
    "browser_upload",
    "browser_action_status",
]


def test_real_iCode_binding_exposes_nine_tools_and_hooks(tmp_path: Path) -> None:
    if "browser" not in discover_extensions().sources:
        pytest.skip("icode-browser-use is not installed as a distribution in this environment")

    loaded = asyncio.run(load_extensions(["browser"]))
    assert len(loaded) == 1
    assert loaded[0].extension is not None, loaded[0].problem

    build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="browser-test",
        caller_name=None,
        session_id=None,
        session_dir=tmp_path / "session",
    )
    bound = bind_extension("browser", loaded[0].extension, build)

    assert [tool.name for tool in bound.tools] == EXPECTED_TOOLS
    assert [context.declaration.name for context in bound.contexts] == ["browser_context"]
    assert [hook.declaration.name for hook in bound.before_tool_hooks] == ["browser_guard"]
    assert bound.after_tool_hooks == []
    assert bound.details.tools == EXPECTED_TOOLS


def test_real_binding_same_profile_requires_explicit_browser_id_when_open(tmp_path: Path, monkeypatch) -> None:
    """P1 #3: two real bindings sharing (session_id, agent_name) must not auto-select the other's handle."""
    if "browser" not in discover_extensions().sources:
        pytest.skip("icode-browser-use is not installed as a distribution in this environment")

    from icode_browser_use import extension as ext

    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    ext._browsers.clear()
    loaded = asyncio.run(load_extensions(["browser"]))
    assert len(loaded) == 1 and loaded[0].extension is not None

    async def fake_run(browser, name: str, arguments: dict[str, object]) -> str:
        return f"loaded {arguments.get('url', '')}"

    monkeypatch.setattr(ext, "_run", fake_run)

    build1 = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="Browser",
        caller_name=None,
        session_id="chat-1",
        session_dir=tmp_path / "session",
    )
    build2 = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="Browser",
        caller_name=None,
        session_id="chat-1",
        session_dir=tmp_path / "session",
    )
    bound1 = bind_extension("browser", loaded[0].extension, build1)
    bound2 = bind_extension("browser", loaded[0].extension, build2)
    nav1 = next(tool for tool in bound1.tools if tool.name == "browser_navigate")
    nav2 = next(tool for tool in bound2.tools if tool.name == "browser_navigate")

    try:
        first = asyncio.run(nav1.func(url="https://example.com/one"))
        assert "browser_id:" in first
        second = asyncio.run(nav2.func(url="https://example.com/two"))
        assert "Error:" in second
        assert "browser_id" in second
    finally:
        ext._browsers.clear()


def test_real_binding_isolates_concurrent_sub_agents_and_shares_main_turns(tmp_path: Path, monkeypatch) -> None:
    """PR #348: real host binding isolates concurrent sub-agents via invocation metadata while sharing main turns."""
    if "browser" not in discover_extensions().sources:
        pytest.skip("icode-browser-use is not installed as a distribution in this environment")

    from chrys.foundation.models.invocations import InvocationOrigin, current_invocation_origin
    from icode_browser_use import extension as ext

    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    ext._browsers.clear()
    loaded = asyncio.run(load_extensions(["browser"]))
    assert len(loaded) == 1 and loaded[0].extension is not None

    async def fake_run(browser, name: str, arguments: dict[str, object]) -> str:
        return f"loaded {arguments.get('url', '')}"

    monkeypatch.setattr(ext, "_run", fake_run)

    sub_build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="Browser",
        caller_name="Explore",
        session_id="chat-1",
        session_dir=tmp_path / "session",
    )
    main_build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="Code",
        caller_name="Code",
        session_id="chat-1",
        session_dir=tmp_path / "session",
    )
    sub_bound = bind_extension("browser", loaded[0].extension, sub_build)
    main_bound = bind_extension("browser", loaded[0].extension, main_build)
    sub_nav = next(tool for tool in sub_bound.tools if tool.name == "browser_navigate")
    main_nav = next(tool for tool in main_bound.tools if tool.name == "browser_navigate")
    main_list = next(tool for tool in main_bound.tools if tool.name == "browser_list")

    async def run_scenario() -> None:
        turn1 = InvocationOrigin(kind="turn", session_id="chat-1", invocation_id="turn-1", parent=None)
        turn2 = InvocationOrigin(kind="turn", session_id="chat-1", invocation_id="turn-2", parent=None)
        sub1 = InvocationOrigin(kind="sub_agent", session_id="chat-1", invocation_id="sub-1", parent=turn1)
        sub2 = InvocationOrigin(kind="sub_agent", session_id="chat-1", invocation_id="sub-2", parent=turn1)

        tok = current_invocation_origin.set(sub1)
        try:
            first_sub = await sub_nav.func(url="https://example.com/sub-one")
        finally:
            current_invocation_origin.reset(tok)
        assert "browser_id:" in first_sub
        assert "Error:" not in first_sub

        tok = current_invocation_origin.set(sub2)
        try:
            second_sub = await sub_nav.func(url="https://example.com/sub-two")
        finally:
            current_invocation_origin.reset(tok)
        assert "browser_id:" in second_sub
        assert "Error:" not in second_sub
        assert first_sub.split("browser_id: ", 1)[1].split()[0] != second_sub.split("browser_id: ", 1)[1].split()[0]

        tok = current_invocation_origin.set(turn1)
        try:
            first_main = await main_nav.func(url="https://example.com/main-one")
        finally:
            current_invocation_origin.reset(tok)
        main_id = first_main.split("browser_id: ", 1)[1].split()[0]

        tok = current_invocation_origin.set(turn2)
        try:
            listing = await main_list.func()
        finally:
            current_invocation_origin.reset(tok)
        assert main_id in listing

    try:
        asyncio.run(run_scenario())
    finally:
        ext._browsers.clear()
