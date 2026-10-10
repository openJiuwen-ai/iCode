# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human approval deadlines exclude queueing and model review time."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.app.tui.screens.main.dialog_controllers import ApprovalQueueController
from chrys.foundation.events.types import ApprovalCancelled, ApprovalRequest, ApprovalReviewed
from tests.app.tui.screens.test_dialog_controllers import _approval_request, _ApprovalPort

_TIMED_OUT = "Human approval request timed out."


@pytest.fixture
def approval_wait() -> Iterator[tuple[ApprovalQueueController, _ApprovalPort]]:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port, timeout_seconds=lambda: 45)
    try:
        yield controller, port
    finally:
        controller.close()


def _delays(port: _ApprovalPort) -> list[float]:
    return [timer.delay for timer in port.timers]


async def test_timeout_rejects_once_and_gives_the_next_request_its_own_budget(approval_wait) -> None:
    controller, port = approval_wait
    await controller.on_request(_approval_request("first", judging=False))
    await controller.on_request(_approval_request("second", judging=False))
    assert _delays(port) == [45]
    assert set(controller._timeouts) == {"first"}

    port.timers[0].fire()
    assert port.responses == [("first", False, _TIMED_OUT, None)]
    assert port.cancelled_dialogs == ["first"]
    assert _delays(port) == [45, 45]
    assert set(controller._timeouts) == {"second"}
    assert set(controller.open_dialogs) == {"second"}
    # An expiry that runs again for the closed request publishes nothing more.
    port.timers[0].callback()
    assert len(port.responses) == 1

    await controller.on_cancelled(ApprovalCancelled(request_id="second"))
    assert port.timers[1].stopped
    port.timers[1].callback()
    assert len(port.responses) == 1
    assert controller._timeouts == {}


@pytest.mark.parametrize("defer", [False, True])
async def test_judge_time_is_excluded_and_flagged_request_gets_full_budget(approval_wait, defer: bool) -> None:
    controller, port = approval_wait
    port.defer_while_judging = defer
    await controller.on_request(_approval_request("reviewing"))
    assert port.timers == []
    assert port.responses == []

    await controller.on_reviewed(ApprovalReviewed(request_id="reviewing", approved=False, reason="Review this"))
    assert _delays(port) == [45]
    assert set(controller._timeouts) == {"reviewing"}
    # Repeated presentation updates do not extend an existing deadline.
    await controller.on_reviewed(ApprovalReviewed(request_id="reviewing", approved=False, reason="Review this"))
    assert len(port.timers) == 1
    port.timers[0].fire()
    assert port.responses == [("reviewing", False, _TIMED_OUT, None)]


@pytest.mark.parametrize("defer", [False, True])
async def test_auto_approval_never_starts_a_human_timeout(approval_wait, defer: bool) -> None:
    controller, port = approval_wait
    port.defer_while_judging = defer
    await controller.on_request(_approval_request("safe"))
    await controller.on_reviewed(ApprovalReviewed(request_id="safe", approved=True))
    assert port.timers == []
    assert port.responses == []


async def test_cached_flag_starts_timing_only_when_its_dialog_is_shown(approval_wait) -> None:
    controller, port = approval_wait
    await controller.on_request(_approval_request("first", judging=False))
    await controller.on_request(_approval_request("flagged"))
    await controller.on_reviewed(ApprovalReviewed(request_id="flagged", approved=False, reason="Review"))
    assert len(port.timers) == 1
    assert set(controller._timeouts) == {"first"}
    port.dialogs[0].user_decision_submitted = True
    port.dialogs[0].callback((True, "", None))
    assert port.timers[0].stopped
    assert _delays(port) == [45, 45]
    assert set(controller._timeouts) == {"flagged"}


@pytest.mark.parametrize("approved", [False, True])
async def test_user_response_wins_and_cancels_the_deadline(approval_wait, approved: bool) -> None:
    controller, port = approval_wait
    await controller.on_request(_approval_request("human", judging=False))
    port.dialogs[0].user_decision_submitted = True
    port.dialogs[0].callback((approved, "my choice", None))
    assert port.timers[0].stopped
    # An expiry already queued when the user answered changes nothing.
    port.timers[0].callback()
    assert port.responses == [("human", approved, "my choice", None)]
    assert controller._timeouts == {}


async def test_timeout_does_not_wait_for_a_covered_dialog_to_pop(
    approval_wait, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, port = approval_wait
    await controller.on_request(_approval_request("covered", judging=False))
    dialog = port.dialogs[0]

    def defer_dismissal(handle: SimpleNamespace) -> None:
        handle.is_dismissed = True

    monkeypatch.setattr(port, "dismiss_approval_dialog", defer_dismissal)
    port.timers[0].fire()
    assert port.responses == [("covered", False, _TIMED_OUT, None)]
    assert "covered" in controller.cancelled_requests
    dialog.callback(None)
    await controller.on_reviewed(ApprovalReviewed(request_id="covered", approved=True))
    assert len(port.responses) == 1
    assert not controller.open_dialogs
    assert not controller.cancelled_requests


async def test_screen_teardown_stops_pending_timers(approval_wait) -> None:
    controller, port = approval_wait
    await controller.on_request(_approval_request("pending", judging=False))
    controller.close()
    assert port.timers[0].stopped
    # An expiry already queued at teardown changes nothing.
    port.timers[0].callback()
    assert port.responses == []
    assert controller._timeouts == {}


@pytest.mark.parametrize("approved", [False, True])
async def test_zero_disables_timer_but_allows_user_response(approval_wait, approved: bool) -> None:
    controller, port = approval_wait
    controller._timeout_seconds = lambda: 0
    await controller.on_request(_approval_request("unlimited", judging=False))
    assert port.timers == []
    assert port.responses == []
    port.dialogs[0].user_decision_submitted = True
    port.dialogs[0].callback((approved, "human choice", None))
    assert port.responses == [("unlimited", approved, "human choice", None)]


async def test_default_unlimited_wait_can_be_cancelled() -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)
    try:
        await controller.on_request(_approval_request("unlimited", judging=False))
        assert port.timers == []
        await controller.on_cancelled(ApprovalCancelled(request_id="unlimited"))
        assert port.cancelled_dialogs == ["unlimited"]
        assert port.responses == []
        assert not controller.open_dialogs
    finally:
        controller.close()


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_deadline_waits_for_dialog_readiness(
    approval_wait, monkeypatch: pytest.MonkeyPatch, cancel_first: bool
) -> None:
    controller, port = approval_wait
    callbacks: list[Callable[[], None]] = []
    original = port.show_approval_dialog

    def delayed_dialog(
        event: ApprovalRequest,
        approval_body: object | None,
        on_result: Callable[[tuple[bool, str, dict[str, Any] | None] | None], None],
        *,
        verdict: ApprovalReviewed | None,
    ) -> SimpleNamespace:
        dialog = original(event, approval_body, on_result, verdict=verdict)
        dialog.when_ready = callbacks.append
        return dialog

    monkeypatch.setattr(port, "show_approval_dialog", delayed_dialog)
    await controller.on_request(_approval_request("mounting", judging=False))
    assert not port.timers
    assert len(callbacks) == 1
    if cancel_first:
        await controller.on_cancelled(ApprovalCancelled(request_id="mounting"))
    callbacks[0]()
    if cancel_first:
        assert not port.timers
    else:
        assert _delays(port) == [45]
        assert set(controller._timeouts) == {"mounting"}
        callbacks[0]()
        assert len(port.timers) == 1
