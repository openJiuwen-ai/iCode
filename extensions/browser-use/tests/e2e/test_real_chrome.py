# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in real-Chrome regression tests for browser-use.

Run explicitly with::

    CHRYS_BROWSER_E2E=1 pytest tests/e2e/test_real_chrome.py -n0

Skipped by default so unit CI runs stay fast and never launch Chrome.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.skipif(
    os.environ.get("CHRYS_BROWSER_E2E") != "1",
    reason="Real-Chrome E2E tests require CHRYS_BROWSER_E2E=1",
)

_REF_RE = re.compile(r"@e(\d+)#(\d+)")
_ORIG_USERPROFILE = os.environ.get("USERPROFILE")
_ORIG_HOME = os.environ.get("HOME")
_ORIG_APPDATA = os.environ.get("APPDATA")
_ORIG_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")


@contextlib.asynccontextmanager
async def _chrome_session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[str, object, object, str]]:
    """Launch a fixture server and isolated real Owned Chrome handle."""
    from chrys.extensions import ToolContext
    from fixture_site import serve
    from smoke_common import chrome_executable

    import icode_browser_use.extension as ext

    with tempfile.TemporaryDirectory(prefix="browser-use-e2e-", ignore_cleanup_errors=True) as td:
        root = Path(td).resolve()
        (root / "workspace").mkdir()
        with serve() as (origin, state):
            if _ORIG_USERPROFILE is not None:
                monkeypatch.setenv("USERPROFILE", _ORIG_USERPROFILE)
            if _ORIG_HOME is not None:
                monkeypatch.setenv("HOME", _ORIG_HOME)
            if _ORIG_APPDATA is not None:
                monkeypatch.setenv("APPDATA", _ORIG_APPDATA)
            if _ORIG_LOCALAPPDATA is not None:
                monkeypatch.setenv("LOCALAPPDATA", _ORIG_LOCALAPPDATA)
            monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", origin)
            monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(root / "state"))
            monkeypatch.setenv("ICODE_BROWSER_HEADLESS", "1")
            monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0")
            monkeypatch.setenv("ICODE_BROWSER_EXECUTABLE", str(chrome_executable()))
            ctx = ToolContext.for_testing(
                root / "workspace",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="e2e-chrome",
                session_id="e2e-chrome",
            )
            opened = await ext.browser_open(ctx)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            try:
                yield origin, state, ctx, browser_id
            finally:
                if browser_id in ext._browsers:
                    with contextlib.suppress(Exception):
                        await ext.browser_close(ctx, browser_id)


def test_real_chrome_offset_iframe_click(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/iframe-offset", bid)
            found = await ext.browser_find(ctx, bid, "Offset Frame Click")
            match = _REF_RE.search(found)
            assert match is not None, found
            clicked = await ext.browser_click(ctx, bid, match.group(0))
            assert "dispatch=sent" in clicked, clicked
            snap = await ext.browser_snapshot(ctx, bid)
            assert "iframe-clicks:1" in snap, snap

    asyncio.run(scenario())


def test_real_chrome_small_scroller_marker_and_step(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/small-scroller", bid)
            sess = ext._browsers[bid].session
            scrollable_nodes = [n for n in sess.snapshot.nodes if n.scrollable]
            assert scrollable_nodes, "expected small overflow:auto container to be marked scrollable"
            list_node = next(
                (n for n in scrollable_nodes if "Small Scrollable List" in (n.name or "")),
                scrollable_nodes[0],
            )
            tok = f"@e{list_node.ref}#{list_node.epoch}"
            scrolled = await ext.browser_scroll(ctx, bid, direction="down", amount=1, target=tok)
            assert "y=" in scrolled, scrolled
            # 200px container * 0.85 should move ~170px, not jump straight to ~800px
            m = re.search(r"y=(\d+(?:\.\d+)?)", scrolled)
            assert m is not None and 50 <= float(m.group(1)) <= 300, scrolled
            # Non-scrollable item should truthfully report no movement
            static_find = await ext.browser_find(ctx, bid, "Static Non-Scrollable Item")
            static_ref = _REF_RE.search(static_find)
            assert static_ref is not None, static_find
            no_move = await ext.browser_scroll(ctx, bid, direction="down", amount=1, target=static_ref.group(0))
            assert "未滚动" in no_move or "y=0" in no_move, no_move

    asyncio.run(scenario())


def test_real_chrome_huge_node_survives_navigate(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            result = await ext.browser_navigate(ctx, f"{origin}/huge-node", bid)
            assert f"browser_id: {bid}" in result, result
            # Connection must remain alive for subsequent actions
            nav2 = await ext.browser_navigate(ctx, f"{origin}/button-no-form", bid)
            assert "Menu" in nav2, nav2

    asyncio.run(scenario())


def test_real_chrome_long_label_iframe_not_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/long-label-iframe", bid)
            found = await ext.browser_find(ctx, bid, "quarterly financial audit")
            match = _REF_RE.search(found)
            assert match is not None, found
            clicked = await ext.browser_click(ctx, bid, match.group(0))
            assert "dispatch=sent" in clicked, clicked
            snap = await ext.browser_snapshot(ctx, bid)
            assert "clicked:1" in snap, snap

    asyncio.run(scenario())


def test_real_chrome_bare_button_outside_form_not_submit_like(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext
    from icode_browser_use.actions.actionability import target_facts
    from icode_browser_use.actions.click import _is_submit_like

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/button-no-form", bid)
            sess = ext._browsers[bid].session
            entry = sess.resolve_target("Menu")
            oid = await sess.resolve_object_id(entry)
            facts = await target_facts(sess, oid, entry.cdp_session_id)
            assert facts.get("inForm") is False
            assert _is_submit_like(facts) is False

    asyncio.run(scenario())


def test_real_chrome_login_hidden_inputs_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.extensions import ToolError

    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            with pytest.raises(ToolError) as exc_info:
                await ext.browser_navigate(ctx, f"{origin}/login-hidden-inputs", bid)
            assert exc_info.value.code == "browser_login_required"

    asyncio.run(scenario())


def test_real_chrome_nested_slot_click(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/slot-nested", bid)
            found = await ext.browser_find(ctx, bid, "SlottedSubmit", role="button")
            match = _REF_RE.search(found)
            assert match is not None, found
            clicked = await ext.browser_click(ctx, bid, match.group(0))
            assert "dispatch=sent" in clicked, clicked
            extracted = await ext.browser_extract(ctx, bid)
            assert "slot-clicks:1" in extracted, extracted

    asyncio.run(scenario())


def test_real_chrome_paragraph_contenteditable_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/p-editor", bid)
            found = await ext.browser_find(ctx, bid, "Paragraph Editor")
            match = _REF_RE.search(found)
            assert match is not None, found
            filled = await ext.browser_fill(ctx, bid, match.group(0), " third", clear=False)
            assert "status=succeeded" in filled, filled

    asyncio.run(scenario())


def test_real_chrome_dialog_on_mousedown_cancels_trailing_mouseup(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/dialog-on-mousedown", bid)
            blocked = await ext.browser_click(ctx, bid, "Trigger Mousedown Alert")
            assert "status=blocked" in blocked, blocked
            handled = await ext.browser_dialog(ctx, bid, "accept")
            assert "accepted" in handled or "接受" in handled, handled
            text = await ext.browser_extract(ctx, bid)
            assert "mousedown:1 mouseup:0" in text, text
            # Next click on another button must work cleanly without stuck mouse state
            after = await ext.browser_click(ctx, bid, "After Dialog Button")
            assert "dispatch=sent" in after, after
            text2 = await ext.browser_extract(ctx, bid)
            assert "after:1" in text2, text2

    asyncio.run(scenario())


def test_real_chrome_below_fold_iframe_viewport_after_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/iframe-below-fold", bid)
            sess = ext._browsers[bid].session
            btn_before = next(
                (n for n in sess.snapshot.nodes if "Offset Frame Click" in (n.name or n.text_hint or "")),
                None,
            )
            assert btn_before is not None, "expected iframe button in snapshot"
            assert btn_before.in_viewport is False, "button below fold must NOT be in_viewport before scroll"

            # Scroll down so the iframe (at y≈950) enters the viewport
            await ext.browser_scroll(ctx, bid, direction="down", amount=2)
            await ext.browser_snapshot(ctx, bid)
            btn_after = next(
                (n for n in sess.snapshot.nodes if "Offset Frame Click" in (n.name or n.text_hint or "")),
                None,
            )
            assert btn_after is not None, "expected iframe button in snapshot after scroll"
            assert btn_after.in_viewport is True, "button inside scrolled-in iframe MUST be in_viewport after scroll"

    asyncio.run(scenario())


def test_real_chrome_extract_pages_count_emoji_as_one_character(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/emoji-text", bid)
            short = _REF_RE.search(await ext.browser_find(ctx, bid, "Short Paragraph"))
            assert short is not None
            whole = await ext.browser_extract(ctx, bid, query=short.group(0), max_chars=200)
            assert whole == "Hello \U0001f600", whole  # fits in one page: no "next offset" hint
            # 199×'a' then the emoji straddling the 200-character page edge.
            found = _REF_RE.search(await ext.browser_find(ctx, bid, "Boundary Paragraph"))
            assert found is not None
            first = await ext.browser_extract(ctx, bid, query=found.group(0), max_chars=200)
            assert first.startswith("a" * 199 + "\U0001f600"), first[190:]
            match = re.search(r"next offset=(\d+)", first)
            assert match is not None and match.group(1) == "200", first[190:]
            second = await ext.browser_extract(ctx, bid, query=found.group(0), max_chars=200, offset=200)
            assert second == "tail-after-emoji", second
            for text in (first, second):
                text.encode("utf-8")  # never a lone surrogate
            page = await ext.browser_extract(ctx, bid, max_chars=200)
            page.encode("utf-8")
            page_next = re.search(r"next offset=(\d+)", page)
            assert page_next is not None
            rest = await ext.browser_extract(ctx, bid, max_chars=200, offset=int(page_next.group(1)))
            rest.encode("utf-8")
            assert "tail-after-emoji" in page + rest

    asyncio.run(scenario())


def test_real_chrome_scroll_moves_the_targets_own_frame_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/scroll-iframe", bid)
            found = _REF_RE.search(await ext.browser_find(ctx, bid, "Inner Frame Button"))
            assert found is not None
            scrolled = await ext.browser_scroll(ctx, bid, direction="down", amount=1, target=found.group(0))
            sess = ext._browsers[bid].session
            client = sess.require_client()
            outer = await client.send(
                "Runtime.evaluate",
                {
                    "expression": "[window.scrollY, document.getElementById('tall-frame').contentWindow.scrollY]",
                    "returnByValue": True,
                },
                session_id=sess.session_id,
                timeout=5,
            )
            outer_y, frame_y = outer["result"]["value"]
            assert outer_y == 0, f"outer page must not move: {scrolled}"
            assert frame_y > 0, f"the iframe's own window must move: {scrolled}"
            assert f"y={frame_y}" in scrolled, scrolled

    asyncio.run(scenario())


def test_real_chrome_scroll_reports_position_after_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/scroll-to-text", bid)
            found = _REF_RE.search(await ext.browser_find(ctx, bid, "Scroll Panel"))
            assert found is not None
            bottom = _REF_RE.search(await ext.browser_find(ctx, bid, "Bottom Marker Row"))
            assert bottom is not None
            scrolled = await ext.browser_scroll(
                ctx, bid, direction="down", amount=1, target=found.group(0), to_text=bottom.group(0)
            )
            sess = ext._browsers[bid].session
            final = await sess.require_client().send(
                "Runtime.evaluate",
                {"expression": "document.getElementById('panel').scrollTop", "returnByValue": True},
                session_id=sess.session_id,
                timeout=5,
            )
            final_y = final["result"]["value"]
            assert final_y > 200, final_y
            assert f"y={final_y}" in scrolled, scrolled
            assert "到底=是" in scrolled, scrolled

    asyncio.run(scenario())
