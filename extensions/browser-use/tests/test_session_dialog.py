# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import time

import pytest

from icode_browser_use.config import BrowserConfig
from icode_browser_use.dialog import DialogInfo
from icode_browser_use.errors import BadRequest, BrowserError, DialogPending
from icode_browser_use.session import BrowserSession, PageState


def make_session(**overrides) -> BrowserSession:
    cfg = BrowserConfig(allowed_domains=("example.com",), **overrides)
    return BrowserSession(cfg)


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {}), session_id))
        return {}

    async def aclose(self) -> None:
        return None


def attach(session: BrowserSession) -> FakeClient:
    client = FakeClient()
    session._client = client
    session._session_id = "s-main"
    return client


def test_dialog_is_recorded_with_a_redacted_message() -> None:
    session = make_session()
    attach(session)
    asyncio.run(
        session._on_dialog(
            {"type": "prompt", "message": "password=hunter2", "url": "https://example.com/x"},
            "s-main",
        )
    )
    info = session.pending_dialog()
    assert info is not None
    assert (info.kind, info.message) == ("prompt", "password=***")
    assert info.session == "s-main"
    assert session.dialog_seq == 1
    assert session.dialog_since(0) is info
    assert session.dialog_since(1) is None


def test_open_dialog_refuses_page_actions_but_not_the_dialog_tool() -> None:
    session = make_session(dialog_timeout=30)
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "confirm", "message": "Delete item token=abc"}, "s-main")
        with pytest.raises(DialogPending) as info:
            async with session.action_lock("browser_snapshot"):
                pass
        assert info.value.code == "browser_dialog_open"
        assert 'dialog=confirm message="Delete item token=***"' in info.value.details
        assert "browser_dialog" in info.value.details
        for name in ("browser_dialog", "browser_tabs", "browser_action_status", "browser_close"):
            async with session.action_lock(name):
                pass
        session._finish_dialog("dismissed", "test cleanup")

    asyncio.run(scenario())


def test_prompt_text_is_refused_for_alert_and_confirm() -> None:
    session = make_session()
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "confirm", "message": "Sure?"}, "s-main")
        with pytest.raises(BadRequest):
            await session.handle_dialog(accept=True, text="not a prompt")
        assert session.pending_dialog() is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["", "   "])
def test_accepting_a_prompt_without_an_answer_is_refused(text: str) -> None:
    session = make_session()
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "prompt", "message": "Name?"}, "s-main")
        with pytest.raises(BadRequest):
            await session.handle_dialog(accept=True, text=text)
        assert session.pending_dialog() is not None

    asyncio.run(scenario())


def test_dismiss_takes_no_answer_text() -> None:
    session = make_session()
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "prompt", "message": "Name?"}, "s-main")
        with pytest.raises(BadRequest):
            await session.handle_dialog(accept=False, text="Ada")
        assert session.pending_dialog() is not None

    asyncio.run(scenario())


def test_accepting_a_prompt_sends_the_answer() -> None:
    session = make_session()
    client = attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "prompt", "message": "Name?"}, "s-main")
        closed, sent = await session.handle_dialog(accept=True, text="Ada")
        assert sent is True
        assert closed.closed == "accepted"
        assert closed.reason == "handled by browser_dialog"
        assert session.pending_dialog() is None
        method, params, session_id = client.calls[-1]
        assert method == "Page.handleJavaScriptDialog"
        assert params == {"accept": True, "promptText": "Ada"}
        assert session_id == "s-main"

    asyncio.run(scenario())


def test_the_configured_timeout_dismisses_and_explains_itself() -> None:
    session = make_session(headless=True, dialog_timeout=0.05)
    client = attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "stuck"}, "s-main")
        await wait_until(lambda: session.pending_dialog() is None)
        assert session.pending_dialog() is None
        last = session.last_dialog()
        assert last is not None
        assert last.closed == "dismissed"
        assert "auto-dismissed after 0.05s (headless)" in last.reason
        # A closed dialog still answers "what happened after my action started?".
        assert session.dialog_since(0) is last
        assert client.calls[-1][1] == {"accept": False}

    asyncio.run(scenario())


def test_a_visible_session_says_why_nobody_answered() -> None:
    session = make_session(headless=False, dialog_timeout=0.05)
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "stuck"}, "s-main")
        await wait_until(lambda: session.pending_dialog() is None)
        last = session.last_dialog()
        assert last is not None
        assert "(no browser_dialog call)" in last.reason

    asyncio.run(scenario())


def test_a_timeout_that_finds_no_dialog_says_so() -> None:
    session = make_session(headless=True, dialog_timeout=0.05)
    attach(session)
    # No client: the dismiss command cannot reach anything, like a dialog the
    # page closed at the same moment.
    session._client = None

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "gone"}, "s-main")
        await wait_until(lambda: session.pending_dialog() is None)

    asyncio.run(scenario())
    last = session.last_dialog()
    assert last is not None
    assert "the dialog was already gone" in last.reason


def test_a_failed_dismiss_is_not_reported_as_sent() -> None:
    session = make_session()
    attach(session)

    class FailingClient(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            raise BrowserError("CDP 错误 -32602: No dialog is showing")

    session._client = FailingClient()

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "gone"}, "s-main")
        closed, sent = await session.handle_dialog(accept=True)
        assert sent is False
        assert closed.closed == "gone"
        assert "reached no dialog" in closed.reason
        assert session.pending_dialog() is None

    asyncio.run(scenario())


def test_closing_the_page_finishes_its_dialog() -> None:
    session = make_session(dialog_timeout=30)
    attach(session)
    session._pages["t-page"] = PageState(target_id="t-page", session_id="s-page", opener_id=None)
    session._page_order = ["t-page"]

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "page closes"}, "s-page")
        # A background-page dialog is dismissed, never recorded; put a current-page
        # one in place to exercise the same page teardown path.
        session._dialog = DialogInfo(seq=9, kind="alert", message="bye", page="t-page", session="s-page")
        session._forget_page("t-page")
        assert session.pending_dialog() is None
        last = session.last_dialog()
        assert last is not None and (last.closed, last.reason) == ("gone", "page closed")

    asyncio.run(scenario())


def test_closing_the_dialog_in_the_page_records_the_outcome() -> None:
    session = make_session(dialog_timeout=30)
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "prompt", "message": "Name?"}, "s-main")
        await session._on_dialog_closed({"result": False, "userInput": "typed by a user"}, "s-main")
        last = session.last_dialog()
        assert last is not None
        assert (last.closed, last.reason) == ("dismissed", "closed in the page")
        assert last.prompt_chars == len("typed by a user")

    asyncio.run(scenario())


def test_a_background_page_dialog_is_dismissed_without_touching_the_current_one() -> None:
    session = make_session(dialog_timeout=30)
    client = attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "confirm", "message": "current page"}, "s-main")
        current = session.pending_dialog()
        await session._on_dialog({"type": "alert", "message": "other tab"}, "s-other")
        # The background dismissal must not overwrite or clear the live record.
        assert session.pending_dialog() is current
        assert client.calls[-1][1] == {"accept": False}
        assert client.calls[-1][2] == "s-other"

    asyncio.run(scenario())


def test_the_old_dialogs_closed_event_cannot_finish_the_new_one() -> None:
    session = make_session(dialog_timeout=30)
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "first"}, "s-main")
        await session._on_dialog({"type": "confirm", "message": "second"}, "s-main")
        second = session.pending_dialog()
        assert second is not None and second.kind == "confirm"
        # The closed event of the superseded dialog arrives after the new opening.
        await session._on_dialog_closed({"result": True}, "s-main")
        assert session.pending_dialog() is second
        await session._on_dialog_closed({"result": False}, "s-main")
        assert session.pending_dialog() is None
        assert session.last_dialog() is second
        assert (second.closed, second.reason) == ("dismissed", "closed in the page")

    asyncio.run(scenario())


def test_teardown_clears_dialog_state_and_its_timer() -> None:
    session = make_session(dialog_timeout=30)
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "bye"}, "s-main")
        assert session._dialog_task is not None
        await session._teardown_transport()
        assert session.pending_dialog() is None
        assert session.last_dialog() is None
        assert session._dialog_task is None

    asyncio.run(scenario())


def test_a_new_dialog_supersedes_the_previous_one() -> None:
    session = make_session(dialog_timeout=30)
    attach(session)

    async def scenario() -> None:
        await session._on_dialog({"type": "alert", "message": "first"}, "s-main")
        await session._on_dialog({"type": "confirm", "message": "second"}, "s-main")
        info = session.pending_dialog()
        assert info is not None and info.kind == "confirm"
        assert session.dialog_seq == 2
        assert session.last_dialog() is info

    asyncio.run(scenario())


def test_auto_dismiss_and_handle_dialog_are_serialized_never_double_sending() -> None:
    """Wave 3 P1-19: concurrent timeout auto-dismiss and handle_dialog must never send twice."""
    session = make_session(headless=True, dialog_timeout=0.02)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            self.calls.append((method, dict(params or {}), session_id))
            entered.set()
            await release.wait()
            return {}

    client = BlockingClient()
    session._client = client
    session._session_id = "s-main"

    async def scenario() -> None:
        await session._on_dialog({"type": "confirm", "message": "Pay now?"}, "s-main")
        # Wait until _auto_dismiss_dialog enters _send_dialog_close while holding _dialog_lock.
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        handle_task = asyncio.create_task(session.handle_dialog(accept=True))
        await asyncio.sleep(0.02)
        assert not handle_task.done(), "handle_dialog must wait on _dialog_lock"
        release.set()
        with pytest.raises(BadRequest, match="当前没有打开的 JavaScript 对话框"):
            await handle_task
        assert len(client.calls) == 1
        assert client.calls[0][1] == {"accept": False}

    asyncio.run(scenario())


def test_oopif_dialog_is_treated_as_current_page_and_epoch_resets_orphans() -> None:
    """Wave 3 P1-19: current-page OOPIF dialog is recorded (not auto-dismissed); _bump_epoch resets _dialog_orphans."""
    session = make_session(dialog_timeout=30)
    client = attach(session)
    session._target_id = "t-main"
    session._record_page("t-main", "s-main", opener_id=None)
    session.register_oopif("s-oopif", "t-oopif", page_target_id="t-main")

    async def scenario() -> None:
        await session._on_dialog({"type": "confirm", "message": "From OOPIF"}, "s-oopif")
        pending = session.pending_dialog()
        assert pending is not None
        assert pending.session == "s-oopif"
        assert pending.message == "From OOPIF"
        assert client.calls == [], "OOPIF dialog on current page must not be silently dismissed"

        await session._on_dialog_closed({"result": True}, "s-oopif")
        assert session.pending_dialog() is None

        # Simulate orphaned dialog counter before navigation/epoch bump.
        session._dialog_orphans = 2
        session._bump_epoch("main_frame_navigated")
        assert session._dialog_orphans == 0

    asyncio.run(scenario())
