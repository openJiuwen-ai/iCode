# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for compact toolset mode, isolated Browser sub-agent, session facts, and unified CLI."""

from __future__ import annotations

import base64
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from chrys.extensions import ToolContext, ToolError, TurnContext

from icode_browser_use import cli, extension as ext, subagent
from icode_browser_use.config import BrowserConfig, ConfigError
from icode_browser_use.prompts import COMPACT_BROWSER_PLAYBOOK


def _tool_ctx(tmp_path: Path, *, session_id: str = "s-compact", agent_name: str = "Browser") -> ToolContext:
    return ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        session_id=session_id,
        agent_name=agent_name,
    )


def _turn_ctx(tmp_path: Path, *, session_id: str = "s-compact", agent_name: str = "Browser") -> TurnContext:
    return TurnContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        session_id=session_id,
        agent_name=agent_name,
    )


def test_config_toolset_default_compact_and_invalid():
    assert BrowserConfig.from_env({}).toolset == "full"
    assert BrowserConfig.from_env({"ICODE_BROWSER_TOOLSET": "compact"}).toolset == "compact"
    assert BrowserConfig.from_env({"ICODE_BROWSER_TOOLSET": "FULL"}).toolset == "full"
    with pytest.raises(ConfigError, match="ICODE_BROWSER_TOOLSET"):
        BrowserConfig.from_env({"ICODE_BROWSER_TOOLSET": "minimal"})


def test_compact_toolset_registers_only_thirteen_core_tools(monkeypatch, tmp_path):
    monkeypatch.delenv("ICODE_BROWSER_EVALUATE", raising=False)
    monkeypatch.delenv("ICODE_BROWSER_SAVE_STATE", raising=False)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ICODE_BROWSER_TOOLSET", "compact")
    try:
        reloaded = importlib.reload(ext)
        tool_names = {tool.name for tool in reloaded.extension.tools}
        assert tool_names == reloaded.COMPACT_TOOL_NAMES
        assert len(tool_names) == 13
        assert "browser_list" in tool_names
        assert "browser_tabs" not in tool_names
        assert "browser_back" not in tool_names
        assert "browser_action_status" not in tool_names
    finally:
        monkeypatch.delenv("ICODE_BROWSER_TOOLSET", raising=False)
        importlib.reload(ext)


def test_full_toolset_remains_default(monkeypatch):
    monkeypatch.delenv("ICODE_BROWSER_TOOLSET", raising=False)
    monkeypatch.delenv("ICODE_BROWSER_EVALUATE", raising=False)
    monkeypatch.delenv("ICODE_BROWSER_SAVE_STATE", raising=False)
    reloaded = importlib.reload(ext)
    tool_names = {tool.name for tool in reloaded.extension.tools}
    assert reloaded.COMPACT_TOOL_NAMES.issubset(tool_names)
    assert len(tool_names) == 29


@pytest.mark.asyncio
async def test_compact_context_uses_compact_playbook(monkeypatch, tmp_path):
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ICODE_BROWSER_TOOLSET", "compact")
    try:
        reloaded = importlib.reload(ext)
        ctx = _tool_ctx(tmp_path)
        tctx = _turn_ctx(tmp_path)
        await reloaded.browser_open(ctx)
        rendered = await reloaded.browser_context(tctx)
        assert rendered is not None
        assert "browser_list" in rendered
        assert COMPACT_BROWSER_PLAYBOOK in rendered
        assert "BROWSER_SESSION_FACTS" in rendered
        listed = await reloaded.browser_list(ctx)
        assert "open=1" in listed
        assert "browser_id=" in listed

        # Simulate host restart: live registry cleared, recorded manifest entry remains.
        reloaded._browsers.clear()
        after_restart = await reloaded.browser_list(ctx)
        assert "recoverable=1" in after_restart
        assert "call browser_resume to reopen" not in after_restart
        assert "browser_navigate" in after_restart or "browser_open" in after_restart
    finally:
        ext._browsers.clear()
        monkeypatch.delenv("ICODE_BROWSER_TOOLSET", raising=False)
        importlib.reload(ext)


@pytest.mark.asyncio
async def test_session_facts_record_origins_blocks_writes_and_screenshots(monkeypatch, tmp_path):
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    reloaded = importlib.reload(ext)
    ctx = _tool_ctx(tmp_path)

    opened = await reloaded.browser_open(ctx)
    browser_id = opened.split("browser_id: ", 1)[1].strip()
    browser = reloaded._browsers[browser_id]

    async def fake_aclose():
        return None

    fake_session = SimpleNamespace(
        state="ready",
        snapshot=SimpleNamespace(url="", title=""),
        cfg=SimpleNamespace(screenshot_keep=20),
        aclose=fake_aclose,
    )
    browser.session = fake_session  # type: ignore[assignment]

    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
        b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    async def fake_call(_browser, name: str, arguments: dict[str, object]):
        if name == "browser_navigate":
            url = str(arguments["url"])
            if "blocked.example" in url:
                raise ToolError("Blocked by policy (site_blocked)", code="browser_policy_denied")
            fake_session.snapshot = SimpleNamespace(url=url, title="Example")
            return [{"type": "text", "text": "Navigated"}]
        if name == "browser_click":
            return [{"type": "text", "text": "Clicked"}]
        if name == "browser_screenshot":
            return [{"type": "image", "data": base64.b64encode(png_bytes).decode("ascii"), "mimeType": "image/png"}]
        return [{"type": "text", "text": "ok"}]

    monkeypatch.setattr(reloaded, "_call", fake_call)

    try:
        await reloaded.browser_navigate(ctx, "https://docs.example.com/path?token=secret#frag", browser_id)
        with pytest.raises(ToolError):
            await reloaded.browser_navigate(ctx, "https://blocked.example/admin?key=123", browser_id)
        await reloaded.browser_click(ctx, browser_id, "@e1#1")
        await reloaded.browser_screenshot(ctx, browser_id)

        closed = await reloaded.browser_close(ctx, browser_id)
        assert "[BROWSER_SESSION_FACTS]" in closed
        assert "visited_origins: https://docs.example.com" in closed
        assert "token=secret" not in closed
        assert "blocked_origins: https://blocked.example" in closed
        assert "key=123" not in closed
        assert "write_actions: 1" in closed
        assert "screenshots: screenshots/screenshot-" in closed
    finally:
        reloaded._browsers.clear()


def test_unified_cli_routes_all_subcommands_and_exports_cli_main(monkeypatch):
    assert ext.cli_main is cli.main

    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli.chrome, "main", lambda argv, **_kw: (seen.append(("chrome", list(argv))), 11)[1])
    monkeypatch.setattr(cli.bridge_install, "main", lambda argv: (seen.append(("bridge", list(argv))), 12)[1])
    monkeypatch.setattr(cli.broker, "main", lambda argv: (seen.append(("broker", list(argv))), 13)[1])
    monkeypatch.setattr(cli.subagent, "main", lambda argv: (seen.append(("subagent", list(argv))), 14)[1])

    assert cli.main(["connect", "--json"]) == 11
    assert cli.main(["status"]) == 11
    assert cli.main(["disconnect", "--keep-running"]) == 11
    assert cli.main(["update-bridge"]) == 11
    assert cli.main(["bridge", "status"]) == 12
    assert cli.main(["broker", "--idle-seconds", "60"]) == 13
    assert cli.main(["subagent", "--print"]) == 14

    assert seen == [
        ("chrome", ["connect", "--json"]),
        ("chrome", ["status"]),
        ("chrome", ["disconnect", "--keep-running"]),
        ("chrome", ["update-bridge"]),
        ("bridge", ["status"]),
        ("broker", ["--idle-seconds", "60"]),
        ("subagent", ["--print"]),
    ]


def test_subagent_install_writes_least_privilege_profile(tmp_path, capsys):
    target_dir = tmp_path / "agents"
    assert subagent.main(["--install", "--config-dir", str(tmp_path)]) == 0
    profile = target_dir / "Browser.yaml"
    assert profile.is_file()
    content = profile.read_text(encoding="utf-8")
    assert "name: Browser" in content
    assert "extensions:" in content
    assert "- browser" in content
    assert "- filesystem.read" in content
    assert "- shell" not in content
    assert "- filesystem.write" not in content
    assert "BROWSER_SESSION_FACTS" in content

    # Second install without --force refuses to overwrite.
    assert subagent.main(["--install", "--config-dir", str(tmp_path)]) == 1
    assert "already exists" in capsys.readouterr().err

    # --force overwrites cleanly.
    assert subagent.main(["--install", "--force", "--config-dir", str(tmp_path)]) == 0
