# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Page ownership, listing and switching (W09 tabs step)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from icode_browser_use.cdp.targets import TargetManager
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import StaleRef, TargetNotFound
from icode_browser_use.session import BrowserSession
from icode_browser_use.snapshot.fusion import FusedNode


class FakeClient:
    closed = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.frames: dict[str, str] = {}
        self.target_infos: list[dict] = []
        self.create_count = 0
        self.handlers: list[str] = []
        self.fail_once: dict[str, int] = {}

    def on(self, method: str, handler) -> None:  # noqa: ANN001 - client contract
        self.handlers.append(method)

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {}), session_id))
        if self.fail_once.get(method, 0) > 0:
            self.fail_once[method] -= 1
            raise RuntimeError(f"{method} boom")
        if method == "Target.createTarget":
            self.create_count += 1
            return {"targetId": f"t-new-{self.create_count}"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s-new"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": f"frame-{session_id}", "url": self.frames.get(session_id, "")}}}
        if method == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "probe-1"}
        if method == "Target.getTargets":
            return {"targetInfos": list(self.target_infos)}
        return {}

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]


def make_session(tmp_path: Path) -> tuple[BrowserSession, FakeClient]:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state"))
    client = FakeClient()
    client.frames["s-root"] = "https://example.com/"
    session._client = client
    session._targets = TargetManager(client)
    session._state = "ready"
    session._target_id = "t-root"
    session._session_id = "s-root"
    session._main_frame_id = "frame-s-root"
    session._snapshot.url = "https://example.com/"
    session._snapshot.title = "Root"
    session._record_page("t-root", "s-root", opener_id=None, url="https://example.com/", title="Root")
    return session, client


def popup_attach(target_id: str = "t-popup", session_id: str = "s-popup", opener: str | None = "t-root") -> dict:
    info = {"type": "page", "targetId": target_id, "url": f"https://example.com/{target_id}", "title": target_id}
    if opener is not None:
        info["openerId"] = opener
    return {"targetInfo": info, "sessionId": session_id}


def node(name: str = "Sign in") -> FusedNode:
    return FusedNode(
        backend_node_id=1,
        frame_id="frame-s-root",
        target_id="t-root",
        role="button",
        name=name,
        tag="button",
        nth=0,
        bbox=(0.0, 0.0, 10.0, 10.0),
        in_viewport=True,
        visible=True,
        interactive=True,
    )


def test_pages_lists_the_root_tab(tmp_path: Path) -> None:
    session, _client = make_session(tmp_path)
    pages = session.pages()
    assert len(pages) == 1
    assert pages[0].index == 1 and pages[0].current
    assert pages[0].target_id == "t-root"
    assert pages[0].opener_id is None


def test_a_popup_opened_by_our_page_is_adopted_not_closed(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))

    pages = session.pages()
    assert [info.target_id for info in pages] == ["t-root", "t-popup"]
    assert pages[1].opener_id == "t-root"
    assert pages[1].current is False
    assert "Target.closeTarget" not in client.methods()
    # Page domains are enabled lazily on the first switch, after the debugger resumed it.
    assert not [call for call in client.calls if call[2] == "s-popup"]
    asyncio.run(session.switch_page("t-popup"))
    assert ("Page.enable", {}, "s-popup") in client.calls


def test_a_foreign_page_is_never_adopted_or_closed(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    asyncio.run(session._on_attached_to_target(popup_attach("t-user", "s-user", opener=None), None))

    assert [info.target_id for info in session.pages()] == ["t-root"]
    assert "Target.closeTarget" not in client.methods()
    assert "Target.detachFromTarget" not in client.methods()
    assert "t-user" in session._foreign_targets


def test_attach_mode_detaches_a_foreign_page_instead_of_leaving_it(tmp_path: Path) -> None:
    class Attached:
        owns_browser = False
        browser_commands = True

    session, client = make_session(tmp_path)
    session._transport = Attached()

    asyncio.run(session._on_attached_to_target(popup_attach("t-user", "s-user", opener=None), None))

    assert ("Target.detachFromTarget", {"sessionId": "s-user"}, None) in client.calls
    assert "Target.closeTarget" not in client.methods()


def test_switching_pages_moves_the_context_and_invalidates_refs(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    session.register_snapshot([node()], url="https://example.com/", title="Root")
    token = f"@e1#{session.page_epoch}"
    epoch = session.page_epoch

    info = asyncio.run(session.switch_page("t-popup"))

    assert info.current is True and info.index == 2
    assert session.target_id == "t-popup"
    assert session.session_id == "s-popup"
    assert session.snapshot.url == "https://example.com/t-popup"
    assert session.page_epoch != epoch
    assert session.snapshot.nodes == []
    with pytest.raises(StaleRef):
        session.resolve_target(token)
    assert ("Target.activateTarget", {"targetId": "t-popup"}, None) in client.calls


def test_switching_to_an_unknown_tab_is_refused_with_the_available_ids(tmp_path: Path) -> None:
    session, _client = make_session(tmp_path)
    with pytest.raises(TargetNotFound) as info:
        asyncio.run(session.switch_page("t-missing"))
    assert "t-root" in str(info.value)


def test_closing_the_current_popup_returns_to_its_opener(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-start"] = "https://example.com/start"
    client.frames["s-auth"] = "https://provider.example/auth"
    asyncio.run(session._on_attached_to_target(popup_attach("t-start", "s-start"), None))
    asyncio.run(session._on_attached_to_target(popup_attach("t-auth", "s-auth", opener="t-start"), None))
    asyncio.run(session.switch_page("t-auth"))

    asyncio.run(session._on_detached_from_target({"sessionId": "s-auth"}, None))

    assert [info.target_id for info in session.pages()] == ["t-root", "t-start"]
    assert session.target_id == "t-start"
    assert session.snapshot.url == "https://example.com/start"
    assert "Target.closeTarget" not in client.methods()


def test_closing_the_last_page_opens_a_fresh_tab(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-new"] = "about:blank"

    asyncio.run(session._on_detached_from_target({"sessionId": "s-root"}, None))

    assert session.state == "ready"
    assert session.target_id == "t-new-1"
    assert [info.target_id for info in session.pages()] == ["t-new-1"]
    assert info_current(session) is True


def info_current(session: BrowserSession) -> bool:
    pages = session.pages()
    return bool(pages) and pages[0].current


def test_oopif_children_are_scoped_to_the_current_page(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    asyncio.run(
        session._on_attached_to_target(
            {"targetInfo": {"type": "iframe", "targetId": "if-root"}, "sessionId": "s-if-root"},
            "s-root",
        )
    )
    asyncio.run(
        session._on_attached_to_target(
            {"targetInfo": {"type": "iframe", "targetId": "if-popup"}, "sessionId": "s-if-popup"},
            "s-popup",
        )
    )
    assert session.oopif_children(None) == ["s-if-root"]

    asyncio.run(session.switch_page("t-popup"))
    assert session.oopif_children(None) == ["s-if-popup"]


def test_a_background_page_does_not_bump_the_current_epoch(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    epoch = session.page_epoch

    frame = {"frame": {"id": "frame-s-popup", "url": "https://example.com/next", "loaderId": "l1"}}
    asyncio.run(session._on_frame_navigated(frame, "s-popup"))
    asyncio.run(session._on_document_updated({}, "s-popup"))
    asyncio.run(session._on_lifecycle_event({"name": "load", "loaderId": "l1"}, "s-popup"))

    assert session.page_epoch == epoch
    assert session.snapshot.url == "https://example.com/"
    assert next(info for info in session.pages() if info.target_id == "t-popup").url.endswith("/next")


def test_open_page_failure_restores_the_context_and_closes_the_new_tab(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.fail_once["Page.enable"] = 1

    with pytest.raises(RuntimeError):
        asyncio.run(session._open_page())

    assert session.target_id == "t-root"
    assert session.session_id == "s-root"
    assert session._main_frame_id == "frame-s-root"
    assert [info.target_id for info in session.pages()] == ["t-root"]
    assert ("Target.closeTarget", {"targetId": "t-new-1"}, None) in client.calls


def test_recycle_does_not_reopen_a_second_tab_when_the_old_one_detaches(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-new"] = "about:blank"
    seen_create_calls: list[int] = []

    async def detach_during_close() -> None:
        await asyncio.sleep(0)
        await session._on_detached_from_target({"sessionId": "s-root"}, None)
        seen_create_calls.append(client.create_count)

    async def scenario() -> None:
        task = asyncio.create_task(detach_during_close())
        await session.recycle_page()
        await task

    asyncio.run(scenario())
    assert client.create_count == 1
    assert [info.target_id for info in session.pages()] == ["t-new-1"]
    assert session.target_id == "t-new-1"


def test_an_auto_attach_during_opening_is_detached_but_the_manual_session_is_not(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    session._opening_page = True
    session._pages.clear()
    session._page_order.clear()
    session._target_id = "t-open"
    session._session_id = "s-manual"

    asyncio.run(
        session._on_attached_to_target(
            {"targetInfo": {"type": "page", "targetId": "t-open"}, "sessionId": "s-manual"},
            None,
        )
    )
    assert not [call for call in client.calls if call[0] == "Target.detachFromTarget"]

    asyncio.run(
        session._on_attached_to_target(
            {
                "targetInfo": {"type": "page", "targetId": "t-open"},
                "sessionId": "s-duplicate",
                "waitingForDebugger": True,
            },
            None,
        )
    )
    assert ("Target.detachFromTarget", {"sessionId": "s-duplicate"}, None) in client.calls


def test_switch_page_reports_a_tab_that_is_gone_instead_of_leaking_StopIteration(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    original = session._activate_page

    async def activates_then_loses(page):  # noqa: ANN001 - test double
        await original(page)
        session._forget_page(page.target_id)

    session._activate_page = activates_then_loses  # type: ignore[method-assign]

    with pytest.raises(TargetNotFound) as info:
        asyncio.run(session.switch_page("t-popup"))
    assert "切换过程中关闭" in str(info.value)


def test_forget_page_clears_its_timeout_and_document_status(tmp_path: Path) -> None:
    session, _client = make_session(tmp_path)
    session._timeouts["t-root"] = 3
    session._document_status[("s-root", "frame-s-root", "l1")] = 200

    session._forget_page("t-root")

    assert "t-root" not in session._timeouts
    assert ("s-root", "frame-s-root", "l1") not in session._document_status


def test_a_transport_without_browser_commands_uses_cached_page_info(tmp_path: Path) -> None:
    class Bridge:
        owns_browser = False
        browser_commands = False

    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    session._transport = Bridge()

    pages = asyncio.run(session.list_pages())
    assert len(pages) == 2
    assert "Target.getTargets" not in client.methods()

    asyncio.run(session.switch_page("t-popup"))
    assert session.target_id == "t-popup"
    assert "Target.activateTarget" not in client.methods()


def test_popup_adoption_is_bounded(tmp_path: Path) -> None:
    session, _client = make_session(tmp_path)
    for index in range(7):  # root + 7 popups = the cap
        target = f"t-popup-{index}"
        asyncio.run(
            session._on_attached_to_target(
                {"targetInfo": {"type": "page", "targetId": target, "openerId": "t-root"}, "sessionId": f"s-{index}"},
                None,
            )
        )
    assert len(session.pages()) == 8

    asyncio.run(
        session._on_attached_to_target(
            {"targetInfo": {"type": "page", "targetId": "t-extra", "openerId": "t-root"}, "sessionId": "s-extra"},
            None,
        )
    )
    assert len(session.pages()) == 8
    assert "t-extra" in session._foreign_targets


def test_list_pages_drops_tabs_the_browser_no_longer_reports(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    client.target_infos = [{"targetId": "t-root", "url": "https://example.com/", "title": "Root"}]

    pages = asyncio.run(session.list_pages())

    assert [info.target_id for info in pages] == ["t-root"]
    assert pages[0].current is True


def test_list_pages_refreshes_urls_from_the_browser(tmp_path: Path) -> None:
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))
    client.target_infos = [
        {"targetId": "t-popup", "url": "https://example.com/final", "title": "Final title"},
    ]

    pages = asyncio.run(session.list_pages())

    assert pages[1].url == "https://example.com/final"
    assert pages[1].title == "Final title"
    assert any(method == "Target.getTargets" for method in client.methods())


def test_event_worker_detach_last_page_reopens_without_deadlocking(tmp_path: Path) -> None:
    """Wave 3 P1-13 Path A: detaching the last page inside CDPClient._event_worker must not self-deadlock."""
    import json

    from icode_browser_use.cdp.client import CDPClient

    class LoopbackSocket:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[str] = asyncio.Queue()
            self.sent: list[dict] = []
            self.closed = False
            self.pong_event = asyncio.Event()

        async def send(self, payload: str) -> None:
            msg = json.loads(payload)
            self.sent.append(msg)
            req_id = msg["id"]
            method = msg["method"]
            if method == "Target.createTarget":
                # Emit an early auto-attach event BEFORE responding to createTarget:
                # if _drop_page ran inline on _event_worker, this queued event could never run.
                await self.incoming.put(
                    json.dumps(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {
                                "targetInfo": {"type": "page", "targetId": "t-reopened"},
                                "sessionId": "s-auto-dup",
                                "waitingForDebugger": True,
                            },
                        }
                    )
                )
                await self.incoming.put(json.dumps({"id": req_id, "result": {"targetId": "t-reopened"}}))
            elif method == "Target.attachToTarget":
                await self.incoming.put(json.dumps({"id": req_id, "result": {"sessionId": "s-reopened"}}))
            elif method == "Page.getFrameTree":
                await self.incoming.put(
                    json.dumps(
                        {"id": req_id, "result": {"frameTree": {"frame": {"id": "f-new", "url": "about:blank"}}}}
                    )
                )
            elif method == "Page.addScriptToEvaluateOnNewDocument":
                await self.incoming.put(json.dumps({"id": req_id, "result": {"identifier": "p1"}}))
            else:
                await self.incoming.put(json.dumps({"id": req_id, "result": {}}))

        async def recv(self) -> str:
            return await self.incoming.get()

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> None:
        sock = LoopbackSocket()
        client = CDPClient(sock)  # type: ignore[arg-type]
        client._start()
        session = BrowserSession(BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state"))
        session._client = client
        session._targets = TargetManager(client)
        session._state = "ready"
        session._started_once = True
        session._target_id = "t-root"
        session._session_id = "s-root"
        session._main_frame_id = "f-root"
        session._record_page("t-root", "s-root", opener_id=None, url="https://example.com/", title="Root")
        session._register_handlers()
        try:
            await sock.incoming.put(
                json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "s-root"}})
            )
            # Recovery must run off _event_worker and finish well below the 20s CDP timeout.
            for _ in range(100):
                if session.target_id == "t-reopened" and session._page_recovery_task is None:
                    break
                await asyncio.sleep(0.02)
            assert session.target_id == "t-reopened"
            assert session.state == "ready"
            assert any(
                m.get("method") == "Target.detachFromTarget" and m.get("params") == {"sessionId": "s-auto-dup"}
                for m in sock.sent
            )
        finally:
            await client.aclose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_opening_page_early_auto_attach_detaches_duplicate_session(tmp_path: Path) -> None:
    """Wave 3 P1-13 Path B: early waitingForDebugger attach during _opening_page with empty _target_id is detached."""
    session, client = make_session(tmp_path)
    session._opening_page = True
    session._target_id = ""
    session._session_id = ""

    asyncio.run(
        session._on_attached_to_target(
            {
                "targetInfo": {"type": "page", "targetId": "t-early"},
                "sessionId": "s-early-dup",
                "waitingForDebugger": True,
            },
            None,
        )
    )
    assert ("Target.detachFromTarget", {"sessionId": "s-early-dup"}, None) in client.calls
    assert session._duplicate_sessions.get("s-early-dup") == "t-early"


def test_foreign_tab_iframe_not_registered_and_failed_oopif_arm_is_dropped(tmp_path: Path) -> None:
    """Wave 3 P1-18 & P2-10: foreign iframes never enter _oopifs; fail-closed OOPIFs are dropped from _oopifs."""
    session, _client = make_session(tmp_path)
    routed: list[str] = []

    class FakePolicy:
        def __init__(self, arm_ok: bool) -> None:
            self.arm_ok = arm_ok

        async def handle_attached_to_target(self, params, _session_id):
            routed.append(str(params.get("sessionId") or ""))
            return self.arm_ok

    session._policy = FakePolicy(arm_ok=True)
    # Foreign tab's iframe: policy still sees it, but _oopifs stays clean.
    asyncio.run(
        session._on_attached_to_target(
            {"targetInfo": {"type": "iframe", "targetId": "if-foreign"}, "sessionId": "s-if-foreign"},
            "s-foreign-page",
        )
    )
    assert routed == ["s-if-foreign"]
    assert session.oopif_sessions == []
    assert session.current_page_oopif_sessions() == []

    # Owned tab's iframe whose Fetch arm fails (returns False) must be removed from _oopifs.
    session._policy = FakePolicy(arm_ok=False)
    asyncio.run(
        session._on_attached_to_target(
            {"targetInfo": {"type": "iframe", "targetId": "if-failed"}, "sessionId": "s-if-failed"},
            "s-root",
        )
    )
    assert "s-if-failed" in routed
    assert session.oopif_sessions == []


def test_enable_page_deduplicates_mutation_probe_and_activate_resets_title_and_loader(tmp_path: Path) -> None:
    """Wave 3 P2-11: switching tabs installs MUTATION_PROBE_SCRIPT once per session and resets loader/title."""
    session, client = make_session(tmp_path)
    client.frames["s-popup"] = "https://example.com/t-popup"
    session._frame_loader["frame-s-root"] = "old-loader"
    asyncio.run(session._on_attached_to_target(popup_attach(), None))

    asyncio.run(session.switch_page("t-popup"))
    assert session.snapshot.title == "t-popup"
    assert session._frame_loader == {}

    asyncio.run(session.switch_page("t-root"))
    asyncio.run(session.switch_page("t-popup"))
    probe_calls_for_popup = [
        c for c in client.calls if c[0] == "Page.addScriptToEvaluateOnNewDocument" and c[2] == "s-popup"
    ]
    assert len(probe_calls_for_popup) == 1
