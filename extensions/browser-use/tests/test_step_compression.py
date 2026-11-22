# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for step-compression P0 upgrades: wait_for return_mode, one-step navigate, and click/fill expect."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from chrys.extensions import ToolContext

from icode_browser_use import broker_client, cli, extension as ext
from icode_browser_use.actions import click, fill, wait_for
from icode_browser_use.cdp.launcher import _cmdline_matches_profile
from icode_browser_use.errors import BadRequest
from icode_browser_use.session import RefEntry
from icode_browser_use.snapshot.fusion import FrameInfo, FusedNode, fuse
from icode_browser_use.types import text_block


class _FakeWaitSession:
    def __init__(self, *, page_texts: list[str] | None = None, stable_quiet_ms: float = 1600.0) -> None:
        self.target_id = "t1"
        self.session_id = "s1"
        self.page_epoch = 7
        self.cfg = SimpleNamespace(evaluate_timeout=2.0, cdp_timeout=2.0)
        composer_node = FusedNode(
            backend_node_id=50,
            frame_id="f1",
            target_id="t1",
            nth=0,
            ref=5,
            epoch=7,
            tag="textarea",
            role="textbox",
            name="Composer",
            editable=True,
            visible=True,
            in_viewport=True,
            bbox=(0, 0, 100, 30),
        )
        self.snapshot = SimpleNamespace(nodes=[composer_node], url="https://chat.example", title="Chat")
        self._ref_map = {
            5: RefEntry(
                index=5,
                epoch=7,
                fingerprint={"role": "textbox", "name": "Composer"},
                backend_node_id=50,
                frame_id="f1",
                target_id="t1",
                bbox=(0, 0, 100, 30),
                text_hint="",
                cdp_session_id=None,
            )
        }
        self._page_texts = list(page_texts or ["Hello streaming answer"])
        self._text_idx = 0
        self._stable_quiet_ms = stable_quiet_ms
        self.client = self

    def require_client(self):
        return self

    def note_success(self, _target_id: str) -> None:
        return None

    def resolve_target(self, spec: str):
        return SimpleNamespace(ref=5, epoch=7, name="Composer", backend_node_id=50, cdp_session_id=None)

    async def send(self, method: str, params: dict, **_kw):
        if method == "Runtime.evaluate":
            expr = str(params.get("expression", ""))
            if "__chrys_stable" in expr and "delete" in expr:
                return {"result": {"value": True}}
            if "__chrys_stable" in expr:
                return {"result": {"value": {"present": True, "n": 3, "quiet_ms": self._stable_quiet_ms}}}
            if "location.href" in expr:
                return {"result": {"value": "https://chat.example/thread/1"}}
            text = self._page_texts[min(self._text_idx, len(self._page_texts) - 1)]
            self._text_idx += 1
            return {"result": {"value": text}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-5"}}
        if method == "Runtime.callFunctionOn":
            return {
                "result": {
                    "value": {
                        "present": True,
                        "visible": True,
                        "editable": True,
                        "text": self._page_texts[min(self._text_idx, len(self._page_texts) - 1)],
                    }
                }
            }
        return {"result": {"value": None}}


@pytest.mark.asyncio
async def test_wait_for_stable_marks_inferred_and_returns_bounded_text(monkeypatch):
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    long_reply = "Weather in Shanghai: 24C sunny. " * 20
    session = _FakeWaitSession(page_texts=[long_reply], stable_quiet_ms=1600.0)
    blocks = await wait_for.run(
        session,
        {
            "condition": "stable",
            "target": "",
            "value": "1500",
            "timeout_ms": 2000,
            "return_mode": "text",
            "max_chars": 200,
        },
    )
    out = blocks[0]["text"]
    assert "status=succeeded" in out
    assert "completion=inferred_stable" in out
    assert f"observed_chars={len(long_reply.strip())}" in out
    assert "content:\nWeather in Shanghai:" in out
    assert "truncated" in out


@pytest.mark.asyncio
async def test_wait_for_editable_returns_matched_ref(monkeypatch):
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    session = _FakeWaitSession()
    blocks = await wait_for.run(
        session,
        {
            "condition": "editable",
            "target": "Composer",
            "value": "",
            "timeout_ms": 1000,
            "return_mode": "ref",
            "max_chars": 2000,
        },
    )
    out = blocks[0]["text"]
    assert "status=succeeded" in out
    assert "matched_ref=@e5#7" in out


@pytest.mark.asyncio
async def test_wait_for_timeout_with_return_text_includes_partial_text(monkeypatch):
    # Force immediate timeout on second poll via monotonic progression.
    ticks = iter([0.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(wait_for.time, "monotonic", lambda: next(ticks, 10.0))
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    session = _FakeWaitSession(page_texts=["Partial streaming paragraph..."], stable_quiet_ms=100.0)
    blocks = await wait_for.run(
        session,
        {
            "condition": "text",
            "target": "",
            "value": "DONE_TOKEN",
            "timeout_ms": 200,
            "return_mode": "text",
            "max_chars": 500,
        },
    )
    out = blocks[0]["text"]
    assert "status=timeout" in out
    assert "partial_text:\nPartial streaming paragraph..." in out
    assert "observed_chars=" in out


@pytest.mark.asyncio
async def test_wait_for_run_with_baseline_detects_pre_wait_change(monkeypatch):
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    session = _FakeWaitSession(page_texts=["After click text"])
    blocks = await wait_for.run_with_baseline(
        session,
        {
            "condition": "text_changed",
            "target": "",
            "value": "",
            "timeout_ms": 1000,
            "return_mode": "text",
            "max_chars": 500,
        },
        baseline="Before click text",
    )
    out = blocks[0]["text"]
    assert "status=succeeded" in out
    assert "content:\nAfter click text" in out


@pytest.mark.asyncio
async def test_one_step_browser_navigate_allocates_and_reuses_handle(monkeypatch, tmp_path):
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    reloaded = importlib.reload(ext)
    ctx = ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        session_id="s-nav",
        agent_name="Code",
    )

    async def fake_run(browser, name: str, arguments: dict[str, object]) -> str:
        return f"Navigated {name} to {arguments['url']}"

    monkeypatch.setattr(reloaded, "_run", fake_run)
    try:
        # Step 1: omit browser_id -> allocates handle and navigates in one step.
        first = await reloaded.browser_navigate(ctx, "https://example.com/start")
        assert first.startswith("browser_id: ")
        browser_id = first.splitlines()[0].split("browser_id: ", 1)[1].strip()
        assert "Navigated browser_navigate to https://example.com/start" in first
        assert len(reloaded._browsers) == 1

        # Step 2: pass existing browser_id -> reuses handle.
        second = await reloaded.browser_navigate(ctx, "https://example.com/next", browser_id)
        assert second.startswith(f"browser_id: {browser_id}\n")
        assert len(reloaded._browsers) == 1
    finally:
        reloaded._browsers.clear()


@pytest.mark.asyncio
async def test_click_post_write_expect_uses_pre_write_baseline_and_never_retries(monkeypatch):
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    session = _FakeWaitSession(page_texts=["Old text", "New text after click"])
    click_calls = 0

    async def fake_run_click(_session, _args):
        nonlocal click_calls
        click_calls += 1
        return [text_block("已点击 @e1#7\ndispatch=sent\neffect=dom\nstatus=succeeded retryable=false\naction_id=a1")]

    monkeypatch.setattr(click, "_run_click", fake_run_click)
    blocks = await click.run(
        session,
        {
            "target": "@e1#7",
            "button": "left",
            "click_count": 1,
            "dispatch": "mouse",
            "expect_condition": "text_changed",
            "expect_target": "",
            "expect_value": "",
            "expect_timeout_ms": 1000,
            "expect_return": "text",
        },
    )
    out = blocks[0]["text"]
    assert click_calls == 1
    assert "action_id=a1" in out
    assert "expect_status=matched" in out
    assert "content:\nNew text after click" in out


@pytest.mark.asyncio
async def test_click_blocked_skips_expect_without_retrying(monkeypatch):
    session = _FakeWaitSession()
    click_calls = 0

    async def fake_run_click(_session, _args):
        nonlocal click_calls
        click_calls += 1
        return [text_block("未点击 @e1#7\ndispatch=not_sent\nstatus=blocked retryable=false\naction_id=a2")]

    monkeypatch.setattr(click, "_run_click", fake_run_click)
    blocks = await click.run(
        session,
        {
            "target": "@e1#7",
            "button": "left",
            "click_count": 1,
            "dispatch": "mouse",
            "expect_condition": "text",
            "expect_target": "",
            "expect_value": "Done",
            "expect_timeout_ms": 1000,
            "expect_return": "text",
        },
    )
    out = blocks[0]["text"]
    assert click_calls == 1
    assert "expect_status=skipped reason=write_not_confirmed" in out


@pytest.mark.asyncio
async def test_fill_post_write_expect_verifies_in_one_call(monkeypatch):
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    session = _FakeWaitSession(page_texts=["Before", "Reply: 24C sunny"])
    fill_calls = 0

    async def fake_run_fill(_session, _args):
        nonlocal fill_calls
        fill_calls += 1
        return [text_block("已填入 @e5#7\naction_id=f1\nstatus=succeeded")]

    monkeypatch.setattr(fill, "_run_fill", fake_run_fill)
    blocks = await fill.run(
        session,
        {
            "target": "@e5#7",
            "text": "上海天气如何",
            "clear": True,
            "submit": True,
            "keypress_fallback": False,
            "expect_condition": "text_changed",
            "expect_target": "",
            "expect_value": "",
            "expect_timeout_ms": 1000,
            "expect_return": "text",
        },
    )
    out = blocks[0]["text"]
    assert fill_calls == 1
    assert "action_id=f1" in out
    assert "expect_status=matched" in out
    assert "content:\nReply: 24C sunny" in out


async def _noop() -> None:
    return None


def test_fuse_handles_same_process_iframe_documents_without_crashing():
    snapshot = {
        "strings": ["main", "child", "BUTTON", "Save"],
        "documents": [
            {"frameId": 0, "nodes": {"backendNodeId": [], "nodeName": []}, "layout": {}},
            {
                "frameId": 1,
                "contentWidth": 640,
                "contentHeight": 480,
                "scrollOffsetX": 0,
                "scrollOffsetY": 10,
                "nodes": {"backendNodeId": [99], "nodeName": [2], "nodeType": [1]},
                "layout": {"nodeIndex": [0], "bounds": [[0, 0, 80, 24]]},
            },
        ],
    }
    nodes = fuse(snapshot, {"nodes": []}, frame=FrameInfo(frame_id="main", viewport=(1280.0, 800.0)), target_id="t1")
    assert any(n.frame_id == "child" and n.backend_node_id == 99 for n in nodes)


@pytest.mark.asyncio
async def test_one_step_navigate_reuses_single_open_browser_and_cleans_up_on_failure(monkeypatch, tmp_path):
    from chrys.extensions import ToolError

    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    reloaded = importlib.reload(ext)
    ctx = ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        session_id="s-nav-cleanup",
        agent_name="Code",
    )

    async def fail_run(_browser, _name, _arguments):
        raise ToolError("blocked", code="browser_policy_denied")

    monkeypatch.setattr(reloaded, "_run", fail_run)
    with pytest.raises(ToolError):
        await reloaded.browser_navigate(ctx, "https://blocked.example")
    # Failed one-step navigate must not leak a browser handle.
    assert not reloaded._browsers

    async def ok_run(_browser, _name, arguments):
        return f"loaded {arguments['url']}"

    monkeypatch.setattr(reloaded, "_run", ok_run)
    try:
        first = await reloaded.browser_navigate(ctx, "https://example.com/a")
        bid = first.splitlines()[0].split("browser_id: ", 1)[1].strip()
        # Omitting browser_id when 1 browser is already open reuses that handle.
        second = await reloaded.browser_navigate(ctx, "https://example.com/b")
        assert second.startswith(f"browser_id: {bid}\n")
        assert len(reloaded._browsers) == 1
    finally:
        reloaded._browsers.clear()


@pytest.mark.asyncio
async def test_expect_timeout_cannot_be_spoofed_by_page_text_containing_status_succeeded(monkeypatch):
    ticks = iter([0.0, 0.0, 0.5, 1.0, 2.0, 3.0])
    monkeypatch.setattr(wait_for.time, "monotonic", lambda: next(ticks, 10.0))
    monkeypatch.setattr(wait_for, "_sleep", lambda _s: _noop())
    session = _FakeWaitSession(page_texts=["malicious status=succeeded payload"])

    async def fake_run_click(_session, _args):
        return [text_block("已点击 @e1#7\ndispatch=sent\neffect=dom\nstatus=succeeded retryable=false\naction_id=a9")]

    monkeypatch.setattr(click, "_run_click", fake_run_click)
    blocks = await click.run(
        session,
        {
            "target": "@e1#7",
            "button": "left",
            "click_count": 1,
            "dispatch": "mouse",
            "expect_condition": "text",
            "expect_target": "",
            "expect_value": "NEVER_APPEARS",
            "expect_timeout_ms": 200,
            "expect_return": "text",
        },
    )
    out = blocks[0]["text"]
    assert "expect_status=timeout" in out
    assert "expect_status=matched" not in out


@pytest.mark.asyncio
async def test_invalid_expect_rejects_before_dispatching_write(monkeypatch):
    session = _FakeWaitSession()
    dispatched = 0

    async def fake_run_click(_session, _args):
        nonlocal dispatched
        dispatched += 1
        return [text_block("status=succeeded")]

    monkeypatch.setattr(click, "_run_click", fake_run_click)
    with pytest.raises(BadRequest):
        await click.run(
            session,
            {
                "target": "@e1#7",
                "button": "left",
                "click_count": 1,
                "dispatch": "mouse",
                "expect_condition": "visible",
                "expect_target": "",
                "expect_value": "",
                "expect_timeout_ms": 1000,
                "expect_return": "none",
            },
        )
    assert dispatched == 0


def test_cli_broker_help_does_not_raise_typeerror():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["broker", "--help"])
    assert exc_info.value.code == 0


def test_broker_client_wait_timeout_includes_expect_timeout_ms():
    assert broker_client._requested_wait_seconds({"arguments": {"expect_timeout_ms": 45000}}) == 45.0


def test_cmdline_matches_profile_with_spaces_and_rejects_prefix_collision(tmp_path):
    profile = tmp_path / "First Last" / "chrome"
    assert _cmdline_matches_profile(f'chrome.exe "--user-data-dir={profile}" --headless', profile)
    assert not _cmdline_matches_profile(f'chrome.exe "--user-data-dir={profile}-2" --headless', profile)
