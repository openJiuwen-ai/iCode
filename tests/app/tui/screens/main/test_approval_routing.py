# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for approval-request routing: pre-mount verdicts, parallel judges and MANUAL mode."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main.event_handlers import (
    BackendEventHandler,
)
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.widgets.chrome.app_header import AppHeader
from chrys.foundation.events.types import (
    ApprovalAutoFulfillBlocked,
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalReviewed,
    InvocationToolCallArgsUpdated,
)
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE, KIND_MCP
from tests.support.tui_helpers import (
    FakeNotificationService,
    make_backend_handler,
)

# ──────────── approval flow: parallel-judge race ───────────────────────
#
# In AUTO mode with parallel tool calls the engine publishes N
# ``ApprovalRequest`` events concurrently and spawns N judge tasks.  The
# TUI only shows one dialog at a time (the rest sit in ``_approval_queue``
# as raw events), so judges for queued requests can finish and publish
# ``ApprovalReviewed`` *before* their dialog is ever mounted.  The handler
# must:
#
#  1. Cache such verdicts in ``_pending_verdicts`` keyed by request_id.
#  2. When ``_show_next_approval`` pops that request, consume the cached
#     verdict: if approved, skip the dialog entirely; if flagged, push the
#     dialog and deliver the verdict after mount.
#  3. Drop late arrivals silently when the matching request has already
#     been resolved (not in queue, not in live dialogs dict).


class _FakeApprovalDialog:
    """Mock that mimics ``ApprovalDialog`` without touching the Textual runtime.

    Records the verdict it was constructed with (a flag that arrived before
    the dialog) and the one ``receive_verdict`` delivers later.
    """

    def __init__(
        self,
        *,
        caller_name: str,
        tool_name: str,
        tool_kind: str = "",
        args: dict | None = None,
        judging: bool = False,
        approval_body=None,
        presentation_kind: str = "",
        reuse_offer=None,
        verdict=None,
    ) -> None:
        self.caller_name = caller_name
        self.tool_name = tool_name
        self.tool_kind = tool_kind
        self.args = args or {}
        self.judging = judging
        self.approval_body = approval_body
        self.presentation_kind = presentation_kind
        self.reuse_offer = reuse_offer
        self.remember_choice = ""
        self._dismissed = False
        self._user_decision_submitted = False
        self.constructed_verdict = verdict
        self.received_verdict = None

    @property
    def is_dismissed(self) -> bool:
        return self._dismissed

    @property
    def user_decision_submitted(self) -> bool:
        return self._user_decision_submitted

    def receive_verdict(self, verdict) -> None:
        self.received_verdict = verdict


class _FakeApp:
    """Mock ``App`` that captures pushed screens and their result callbacks."""

    def __init__(self) -> None:
        self.pushed: list[tuple[object, object]] = []  # [(screen, callback)]
        self.notification_service = FakeNotificationService()

    def push_screen(self, screen, callback) -> None:
        self.pushed.append((screen, callback))


class _FakeHeader:
    """Records the review counts the header badge is told to show."""

    def __init__(self) -> None:
        self.review_counts: list[int] = []

    def set_auto_review_count(self, count: int) -> None:
        self.review_counts.append(count)


class _FakeBus:
    """Mock bus that captures frontend → backend events."""

    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, event) -> None:
        self.published.append(event)


def _make_approval_handler(
    monkeypatch, *, defer_while_judging: bool = False
) -> tuple[BackendEventHandler, _FakeApp, list, list]:
    """Build a ``BackendEventHandler`` wired to mocks for approval flow.

    Requests the judge reviews show at once unless *defer_while_judging*;
    the header the screen's ``query_one`` finds is ``app.header``.

    Returns ``(handler, fake_app, debug_log, response_log)``:
    - ``debug_log`` — every ``screen._debug(event_type, detail)`` call.
    - ``response_log`` — every ``screen._handle_approval_response`` call.
    """
    from collections import deque

    # Patch the dialog class the handler imports lazily inside
    # ``_show_next_approval`` so our mock is used in its place.
    import chrys.app.tui.screens.dialogs.approval as _approval_mod

    monkeypatch.setattr(_approval_mod, "ApprovalDialog", _FakeApprovalDialog)

    app = _FakeApp()
    app.header = _FakeHeader()
    bus = _FakeBus()
    debug_log: list[tuple[str, str]] = []
    response_log: list[tuple[str, bool, str]] = []
    worker_tasks: list[asyncio.Task[object]] = []

    def _debug(event_type: str, detail: str = "") -> None:
        debug_log.append((event_type, detail))

    def _handle_approval_response(
        request_id: str,
        approved: bool,
        reason: str = "",
        _modified_args: dict[str, object] | None = None,
        _remember_choice: ReuseChoice = "",
    ) -> None:
        response_log.append((request_id, approved, reason))

    def _run_worker(work, **_kwargs) -> SimpleNamespace:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if hasattr(work, "close"):
                work.close()
            return SimpleNamespace()
        task = loop.create_task(work)
        worker_tasks.append(task)
        return SimpleNamespace(task=task)

    def _query_one(widget_type: type) -> object:
        if widget_type is not AppHeader:
            raise LookupError(widget_type)
        return app.header

    screen = SimpleNamespace(
        app=app,
        _services=MainScreenServices(bus=bus),
        _debug=_debug,
        _handle_approval_response=_handle_approval_response,
        run_worker=_run_worker,
        worker_tasks=worker_tasks,
        query_one=_query_one,
    )
    app.screen = screen
    app.bus = bus
    app.worker_tasks = worker_tasks
    handler = make_backend_handler(screen, approval_defer_while_judging=lambda: defer_while_judging)
    handler._approval_queue = deque()
    handler._approval_request_lock = asyncio.Lock()
    handler._approval_dialog_open = False
    handler._approval_bodies = {}
    handler._open_approval_dialogs = {}
    handler._pending_verdicts = {}
    handler._dismissed_approval_requests = set()
    handler._reviewed_dismissed_approval_requests = set()
    return handler, app, debug_log, response_log


def _make_request(
    request_id: str,
    tool_name: str = "zsh",
    judging: bool = True,
    tool_kind: str = "shell",
    args: dict | None = None,
    call_id: str = "",
):
    return ApprovalRequest(
        request_id=request_id,
        call_id=call_id,
        tool_name=tool_name,
        tool_kind=tool_kind,
        args=args or {"command": "ls"},
        judging=judging,
        caller_name="Explore Agent",
    )


def _make_reviewed(request_id: str, approved: bool, reason: str = ""):
    return ApprovalReviewed(request_id=request_id, approved=approved, reason=reason)


def test_modified_approval_args_refresh_visible_tool_card(monkeypatch) -> None:
    handler, app, _debug_log, _response_log = _make_approval_handler(monkeypatch)
    updates: list[tuple[str, dict[str, object]]] = []

    class _FakePanel:
        def update_tool_args(self, call_id: str, args: dict[str, object]) -> None:
            updates.append((call_id, args))

    app.screen.query_one = lambda _cls: _FakePanel()

    asyncio.run(
        handler.on_approval_request(
            _make_request(
                "req-1",
                tool_name="explore_agent",
                tool_kind="sub_agent",
                args={"prompt": "old"},
                call_id="call-1",
                judging=False,
            )
        )
    )
    _dialog, on_result = app.pushed[0]

    on_result((True, "", {"prompt": "new"}))

    assert updates == [("call-1", {"prompt": "new"})]


def test_final_tool_args_update_refreshes_visible_tool_card(monkeypatch) -> None:
    handler, app, debug_log, _response_log = _make_approval_handler(monkeypatch)
    handler.set_agent_running(True)
    updates: list[tuple[str, dict[str, object]]] = []

    class _FakePanel:
        def update_tool_args(self, call_id: str, args: dict[str, object]) -> None:
            updates.append((call_id, args))

    app.screen.query_one = lambda _cls: _FakePanel()

    asyncio.run(
        handler.on_tool_args_updated(
            InvocationToolCallArgsUpdated(
                tool_name="explore_agent",
                tool_kind="sub_agent",
                call_id="call-1",
                args={"prompt": "hook rewritten"},
                origin=InvocationOrigin("turn", "", "turn-test", None),
            )
        )
    )

    assert updates == [("call-1", {"prompt": "hook rewritten"})]
    assert debug_log[-1] == ("InvocationToolCallArgsUpdated", "explore_agent")


# ──────────── baseline: verdict reaches live dialog ────────────────────


def test_approval_verdict_delivered_to_live_dialog(monkeypatch) -> None:
    """Judge finishes *after* its dialog is mounted — verdict goes straight
    to ``dialog.receive_verdict`` (existing happy path, must still work)."""
    handler, app, _debug, _ = _make_approval_handler(monkeypatch)

    asyncio.run(handler.on_approval_request(_make_request("req-1")))
    assert len(app.pushed) == 1
    dialog, _cb = app.pushed[0]

    asyncio.run(handler.on_approval_reviewed(_make_reviewed("req-1", approved=True)))

    assert dialog.received_verdict is not None
    assert dialog.received_verdict.approved is True
    # Nothing got cached — dialog handled it directly.
    assert handler._pending_verdicts == {}


# ──────────── approved pre-mount → dialog skipped ──────────────────────


def test_approved_pre_mount_verdict_skips_dialog(monkeypatch) -> None:
    """Judge for a queued request finishes before its dialog is pushed.
    When the preceding dialog dismisses, the queued request is dropped
    entirely (tool already ran in the backend)."""
    handler, app, debug_log, _response_log = _make_approval_handler(monkeypatch)

    # Two parallel requests — first shows a dialog, second queues.
    asyncio.run(handler.on_approval_request(_make_request("req-1", tool_name="zsh")))
    asyncio.run(handler.on_approval_request(_make_request("req-2", tool_name="grep")))
    assert len(app.pushed) == 1
    assert len(handler._approval_queue) == 1

    # Judge approves req-2 before its dialog is shown → verdict cached.
    asyncio.run(handler.on_approval_reviewed(_make_reviewed("req-2", approved=True)))
    assert "req-2" in handler._pending_verdicts

    # User/judge dismisses dialog 1.  The result callback on the first
    # pushed dialog drives the next-draining logic.
    _dialog1, on_result = app.pushed[0]
    on_result((True, "", None))

    # Dialog 2 must NOT have been pushed (approved pre-mount → skipped).
    assert len(app.pushed) == 1
    # Cache drained and queue empty.
    assert handler._pending_verdicts == {}
    assert len(handler._approval_queue) == 0
    # Flag cleared because no dialog remains open.
    assert handler._approval_dialog_open is False
    # Debug log records the pre-mount skip for req-2.
    assert any(evt == "ApprovalJudge" and "pre-mount" in detail and "grep" in detail for evt, detail in debug_log)


# ──────────── flagged pre-mount → dialog shown with verdict ────────────


async def test_flagged_pre_mount_verdict_opens_the_dialog_flagged(monkeypatch) -> None:
    """Judge flags a queued request before its dialog is pushed.  The dialog
    is built with the verdict, so it opens flagged with nothing left to
    deliver after mount."""
    handler, app, _debug, _ = _make_approval_handler(monkeypatch)

    await handler.on_approval_request(_make_request("req-1"))
    await handler.on_approval_request(_make_request("req-2", tool_name="rm"))

    # Judge flags req-2 while still queued.
    await handler.on_approval_reviewed(_make_reviewed("req-2", approved=False, reason="rm -rf"))
    assert "req-2" in handler._pending_verdicts

    # Dismiss dialog 1 → drains queue → dialog 2 is pushed.
    _dialog1, on_result_1 = app.pushed[0]
    on_result_1((True, "", None))

    assert len(app.pushed) == 2
    dialog2, _cb2 = app.pushed[1]

    assert dialog2.constructed_verdict is not None
    assert dialog2.constructed_verdict.approved is False
    assert "rm -rf" in dialog2.constructed_verdict.reason
    assert dialog2.received_verdict is None
    # Pending verdict was consumed.
    assert "req-2" not in handler._pending_verdicts


# ──────────── late arrival after resolution → dropped ──────────────────


def test_reviewed_after_dismissal_is_dropped(monkeypatch) -> None:
    """A verdict that arrives after its request has already been resolved
    (dialog dismissed and not in queue) is silently dropped — not cached."""
    handler, app, _debug, _ = _make_approval_handler(monkeypatch)

    asyncio.run(handler.on_approval_request(_make_request("req-1")))
    _dialog1, on_result = app.pushed[0]
    # User dismisses before the judge finishes.
    on_result((True, "", None))
    assert handler._open_approval_dialogs == {}
    assert len(handler._approval_queue) == 0

    # Late verdict arrives — should NOT be cached (request is gone).
    asyncio.run(handler.on_approval_reviewed(_make_reviewed("req-1", approved=True)))
    assert handler._pending_verdicts == {}


_ApprovalResult = tuple[bool, str, object | None]


def _enter_window_a(dialog: _FakeApprovalDialog, _on_result: Callable[[_ApprovalResult], None]) -> None:
    """Window A: the user clicked, so the dialog is dismissed but still registered."""
    dialog._dismissed = True
    dialog._user_decision_submitted = True


def _finish_window_a(_dialog: _FakeApprovalDialog, on_result: Callable[[_ApprovalResult], None]) -> None:
    on_result((False, "use safer path", None))


def _enter_window_b(dialog: _FakeApprovalDialog, on_result: Callable[[_ApprovalResult], None]) -> None:
    """Window B: the dialog callback already ran, but the ApprovalResponse worker may be pending."""
    dialog._user_decision_submitted = True
    on_result((False, "use safer path", None))


def _finish_nothing(_dialog: _FakeApprovalDialog, _on_result: Callable[[_ApprovalResult], None]) -> None:
    return None


@pytest.mark.parametrize(
    ("enter_window", "finish_window"),
    [
        pytest.param(_enter_window_a, _finish_window_a, id="dismissed-before-result-callback"),
        pytest.param(_enter_window_b, _finish_nothing, id="result-callback-before-response-publish"),
    ],
)
def test_approved_review_inside_user_decision_window_blocks_auto_fulfill(
    enter_window: Callable[[_FakeApprovalDialog, Callable[[_ApprovalResult], None]], None],
    finish_window: Callable[[_FakeApprovalDialog, Callable[[_ApprovalResult], None]], None],
    monkeypatch,
) -> None:
    handler, app, _debug, _response_log = _make_approval_handler(monkeypatch)

    asyncio.run(handler.on_approval_request(_make_request("req-1")))
    dialog, on_result = app.pushed[0]
    enter_window(dialog, on_result)

    asyncio.run(handler.on_approval_reviewed(_make_reviewed("req-1", approved=True)))

    published = app.bus.published
    assert len(published) == 1
    assert isinstance(published[0], ApprovalAutoFulfillBlocked)
    assert published[0].request_id == "req-1"

    finish_window(dialog, on_result)
    assert handler._dismissed_approval_requests == set()
    assert handler._reviewed_dismissed_approval_requests == set()


def test_manual_user_decision_does_not_track_dismissed_request(monkeypatch) -> None:
    """MANUAL approvals never receive judge reviews, so no race marker is needed."""
    handler, app, _debug, _response_log = _make_approval_handler(monkeypatch)

    asyncio.run(handler.on_approval_request(_make_request("req-1", judging=False)))
    dialog, on_result = app.pushed[0]
    dialog._user_decision_submitted = True

    on_result((False, "manual decline", None))

    assert handler._dismissed_approval_requests == set()
    assert handler._reviewed_dismissed_approval_requests == set()


def test_auto_user_decision_marker_clears_when_response_worker_finishes(monkeypatch) -> None:
    """AUTO race markers are only kept while the ApprovalResponse worker is pending."""

    async def _run() -> None:
        handler, app, _debug, response_log = _make_approval_handler(monkeypatch)

        class _Worker:
            def __init__(self) -> None:
                self._done = asyncio.Event()

            async def wait(self) -> None:
                await self._done.wait()

            def finish(self) -> None:
                self._done.set()

        response_worker = _Worker()

        def _handle_approval_response(
            request_id: str,
            approved: bool,
            reason: str = "",
            _modified_args: dict[str, object] | None = None,
            _remember_choice: ReuseChoice = "",
        ) -> _Worker:
            response_log.append((request_id, approved, reason))
            return response_worker

        app.screen._handle_approval_response = _handle_approval_response

        await handler.on_approval_request(_make_request("req-1", judging=True))
        dialog, on_result = app.pushed[0]
        dialog._user_decision_submitted = True

        on_result((False, "auto decline", None))

        assert handler._dismissed_approval_requests == {"req-1"}
        response_worker.finish()
        await asyncio.gather(*app.worker_tasks)

        assert handler._dismissed_approval_requests == set()

    asyncio.run(_run())


# ──────────── out-of-order parallel judges ─────────────────────────────


async def test_parallel_judges_finish_out_of_order(monkeypatch) -> None:
    """Three parallel requests; judges fire for req-2 (approved) and req-3
    (flagged) while req-1's dialog is still visible.  Dismissing req-1
    drains the queue: req-2 is skipped silently, req-3 shows a dialog built
    with the flag concern."""
    handler, app, _debug, _ = _make_approval_handler(monkeypatch)

    await handler.on_approval_request(_make_request("req-1", tool_name="t1"))
    await handler.on_approval_request(_make_request("req-2", tool_name="t2"))
    await handler.on_approval_request(_make_request("req-3", tool_name="t3"))
    assert len(app.pushed) == 1  # only req-1 mounted

    # Judges finish out of order.
    await handler.on_approval_reviewed(_make_reviewed("req-2", approved=True))
    await handler.on_approval_reviewed(_make_reviewed("req-3", approved=False, reason="danger"))
    assert set(handler._pending_verdicts.keys()) == {"req-2", "req-3"}

    # Dismiss dialog 1 (judge eventually approves it too).
    _d1, on_result_1 = app.pushed[0]
    on_result_1((True, "", None))

    # req-2 skipped, req-3 pushed already flagged.
    assert len(app.pushed) == 2
    d3, _ = app.pushed[1]
    assert d3.tool_name == "t3"
    assert d3.constructed_verdict.approved is False
    assert d3.constructed_verdict.reason == "danger"
    assert d3.received_verdict is None

    # Cache fully drained.
    assert handler._pending_verdicts == {}
    # Dialog 3 is the only one still open — flag stays True until user acts.
    assert handler._approval_dialog_open is True

    # Dismiss dialog 3 → queue empty → flag cleared.
    _d3, on_result_3 = app.pushed[1]
    on_result_3((False, "", None))
    assert handler._approval_dialog_open is False


# ──────────── MANUAL mode regression ───────────────────────────────────


async def test_manual_mode_shows_dialogs_sequentially(monkeypatch) -> None:
    """MANUAL mode (no judge, no ApprovalReviewed events) still queues
    and shows dialogs one at a time — each user decision drains the next.
    Guards the refactored ``_show_next_approval`` loop against breaking the
    no-cache path."""
    handler, app, _debug, response_log = _make_approval_handler(monkeypatch)

    # Three MANUAL requests (judging=False) — no judge verdicts will ever
    # arrive; the TUI must still serialize the dialogs.
    await handler.on_approval_request(_make_request("req-1", judging=False))
    await handler.on_approval_request(_make_request("req-2", judging=False))
    await handler.on_approval_request(_make_request("req-3", judging=False))

    assert len(app.pushed) == 1
    assert len(handler._approval_queue) == 2
    assert handler._pending_verdicts == {}

    # User approves each one — every dismissal must push the next queued.
    _d1, cb1 = app.pushed[0]
    cb1((True, "", None))
    assert len(app.pushed) == 2

    _d2, cb2 = app.pushed[1]
    cb2((False, "", None))
    assert len(app.pushed) == 3

    _d3, cb3 = app.pushed[2]
    cb3((True, "", None))
    assert len(app.pushed) == 3
    assert handler._approval_dialog_open is False
    assert len(handler._approval_queue) == 0

    # All three responses were published back to the engine with the
    # correct approve/decline outcomes.
    assert response_log == [
        ("req-1", True, ""),
        ("req-2", False, ""),
        ("req-3", True, ""),
    ]


@pytest.mark.parametrize(
    ("tool_name", "existing_text", "build_args", "expected_reason"),
    [
        pytest.param(
            "write_file",
            "old",
            lambda path: {"path": path, "content": "new"},
            "skipped dialog: exists",
            id="write-file-conflict",
        ),
        pytest.param(
            "edit_file",
            "hello\n",
            lambda path: {"path": path, "old_string": "missing", "new_string": "replacement"},
            "skipped dialog: not_found",
            id="edit-file-not-found",
        ),
    ],
)
def test_unappliable_filesystem_write_skips_approval_dialog(
    tool_name: str,
    existing_text: str,
    build_args: Callable[[str], dict[str, str]],
    expected_reason: str,
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text(existing_text, encoding="utf-8")
    handler, app, debug_log, response_log = _make_approval_handler(monkeypatch)

    asyncio.run(
        handler.on_approval_request(
            _make_request(
                "req-1",
                tool_name=tool_name,
                judging=False,
                tool_kind=KIND_FILESYSTEM_WRITE,
                args=build_args(str(target)),
            )
        )
    )

    assert app.pushed == []
    assert response_log == [("req-1", True, "")]
    assert len(handler._approval_queue) == 0
    assert target.read_text(encoding="utf-8") == existing_text
    assert any(expected_reason in detail for event, detail in debug_log if event == "ApprovalRequest")


def test_write_file_conflict_for_non_filesystem_kind_shows_generic_dialog(monkeypatch, tmp_path) -> None:
    target = tmp_path / "exists.txt"
    target.write_text("old", encoding="utf-8")
    handler, app, debug_log, response_log = _make_approval_handler(monkeypatch)

    asyncio.run(
        handler.on_approval_request(
            _make_request(
                "req-1",
                tool_name="write_file",
                judging=False,
                tool_kind=KIND_MCP,
                args={"path": str(target), "content": "new"},
            )
        )
    )

    assert response_log == []
    assert len(app.pushed) == 1
    dialog, _callback = app.pushed[0]
    assert dialog.approval_body is None
    assert not any("skipped dialog" in detail for event, detail in debug_log if event == "ApprovalRequest")


def test_write_file_diff_body_is_passed_to_dialog(monkeypatch, tmp_path) -> None:
    target = tmp_path / "new.txt"
    handler, app, _debug_log, _response_log = _make_approval_handler(monkeypatch)

    asyncio.run(
        handler.on_approval_request(
            _make_request(
                "req-1",
                tool_name="write_file",
                judging=False,
                tool_kind=KIND_FILESYSTEM_WRITE,
                args={"path": str(target), "content": "hello\n"},
            )
        )
    )

    assert len(app.pushed) == 1
    dialog, _callback = app.pushed[0]
    assert dialog.approval_body is not None
    assert dialog.approval_body.hidden_arg_keys == frozenset({"content"})
    assert len(dialog.approval_body.widgets) == 1


# ──────────── deferral while the judge reviews (setting on) ────────────


def test_deferred_request_the_judge_approves_never_reaches_the_screen(monkeypatch) -> None:
    handler, app, _debug, response_log = _make_approval_handler(monkeypatch, defer_while_judging=True)

    asyncio.run(handler.on_approval_request(_make_request("req-1")))

    assert app.pushed == []
    assert app.header.review_counts == [1]

    asyncio.run(handler.on_approval_reviewed(_make_reviewed("req-1", approved=True, reason="safe")))

    assert app.pushed == []
    assert app.header.review_counts == [1, 0]
    assert app.notification_service.events == []
    assert response_log == []
    assert app.bus.published == []


def test_deferred_request_the_judge_flags_opens_flagged_and_notifies(monkeypatch) -> None:
    handler, app, _debug, response_log = _make_approval_handler(monkeypatch, defer_while_judging=True)

    asyncio.run(handler.on_approval_request(_make_request("req-1", tool_name="rm")))
    asyncio.run(handler.on_approval_reviewed(_make_reviewed("req-1", approved=False, reason="rm -rf")))

    ((dialog, on_result),) = app.pushed
    assert dialog.tool_name == "rm"
    assert (dialog.constructed_verdict.approved, dialog.constructed_verdict.reason) == (False, "rm -rf")
    assert app.header.review_counts == [1, 0]
    assert len(app.notification_service.events) == 1

    on_result((False, "no", None))

    assert response_log == [("req-1", False, "no")]


def test_deferred_request_cancelled_by_the_backend_leaves_nothing_behind(monkeypatch) -> None:
    handler, app, _debug, response_log = _make_approval_handler(monkeypatch, defer_while_judging=True)

    asyncio.run(handler.on_approval_request(_make_request("req-1")))
    asyncio.run(handler.on_approval_cancelled(ApprovalCancelled(request_id="req-1")))

    assert app.pushed == []
    assert app.header.review_counts == [1, 0]
    assert response_log == []
