# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import time

import pytest

from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import ActionBudgetExhausted, Unavailable
from icode_browser_use.session import BrowserSession


def config(**overrides) -> BrowserConfig:
    return BrowserConfig(allowed_domains=("example.com",), **overrides)


class IdleSession(BrowserSession):
    def __init__(self, cfg: BrowserConfig) -> None:
        super().__init__(cfg)
        self.closed_by_watchdog = False
        self._state = "ready"

    async def _shutdown_idle(self) -> None:
        self.closed_by_watchdog = True
        self._state = "idle"


def test_idle_expired_predicate() -> None:
    session = BrowserSession(config(idle_close_seconds=10))
    assert not session._idle_expired()
    session._last_active -= 11
    assert session._idle_expired()


def test_note_action_refreshes_idle_clock() -> None:
    session = BrowserSession(config())
    session._last_active = 0
    session.note_action()
    assert session._last_active > 0


def test_watchdog_closes_after_idle() -> None:
    session = IdleSession(config(idle_close_seconds=0.04))
    session._last_active = time.monotonic() - 1
    asyncio.run(asyncio.wait_for(session._idle_watchdog(), 2))
    assert session.closed_by_watchdog
    assert session.state == "idle"


def test_login_pending_blocks_watchdog() -> None:
    session = IdleSession(config(idle_close_seconds=0.04))
    session._last_active = time.monotonic() - 1
    session._login_pending_since = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(session._idle_watchdog(), 0.3))
    assert not session.closed_by_watchdog


def test_idle_shutdown_is_restartable() -> None:
    session = BrowserSession(config())
    asyncio.run(session._shutdown_idle())
    assert session.state == "idle"
    assert session._closed is False


def test_aclose_is_idempotent() -> None:
    session = BrowserSession(config())
    asyncio.run(session.aclose())
    asyncio.run(session.aclose())
    assert session.state == "closed"


def test_timeout_threshold_counts_per_target() -> None:
    session = BrowserSession(config())
    assert session.note_timeout_and_should_recycle("t1") is False
    assert session.note_timeout_and_should_recycle("t1") is True
    assert session.note_timeout_and_should_recycle("t2") is False
    session.note_success("t1")
    assert session.note_timeout_and_should_recycle("t1") is False


def test_rolling_action_budget_exhausts_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = 100.0
    monkeypatch.setattr("icode_browser_use.session.time.monotonic", lambda: clock)
    session = BrowserSession(config(max_actions_per_window=1, lifetime_max_actions=10))
    session.note_action()
    clock += 1.25
    with pytest.raises(ActionBudgetExhausted) as caught:
        session.note_action()
    assert caught.value.pre_dispatch
    assert "budget=rolling_window" in caught.value.render()
    assert "retry_after_seconds=599" in caught.value.render()
    assert "dispatch=not_sent" in caught.value.render()
    clock = 700.0
    session.note_action()
    assert session._lifetime_actions == 2


def test_lifetime_budget_takes_precedence_over_a_full_window() -> None:
    session = BrowserSession(config(max_actions_per_window=1, lifetime_max_actions=1))
    session.note_action()
    with pytest.raises(ActionBudgetExhausted) as caught:
        session.note_action()
    result = caught.value.render()
    assert "budget=lifetime remaining=0 retryable=false dispatch=not_sent" in result
    assert "retry_after_seconds" not in result


def test_mark_disconnected_sets_failed() -> None:
    session = BrowserSession(config())
    session.mark_disconnected()
    assert session.state == "failed"


def test_teardown_bumps_epoch_and_clears_refs() -> None:
    session = BrowserSession(config())
    session._ref_map = {1: object()}
    previous = session.page_epoch
    asyncio.run(session._teardown_transport())
    assert session.page_epoch == previous + 1
    assert session._ref_map == {}


def test_reset_budgets() -> None:
    session = BrowserSession(config())
    session._actions.append(time.monotonic())
    session._lifetime_actions = 5
    session._reset_budgets()
    assert not session._actions
    assert session._lifetime_actions == 0


def test_teardown_resets_budgets() -> None:
    session = BrowserSession(config())
    session._actions.append(time.monotonic())
    session._lifetime_actions = 5
    asyncio.run(session._teardown_transport())
    assert not session._actions
    assert session._lifetime_actions == 0


def test_close_current_keeps_the_lifetime_budget() -> None:
    session = BrowserSession(config())
    session._actions.append(time.monotonic())
    session._lifetime_actions = 5
    session._observe_actions.append(time.monotonic())
    session._lifetime_observe_actions = 12
    asyncio.run(session.close_current())
    assert session.state == "idle"
    assert session._lifetime_actions == 5
    assert len(session._actions) == 1
    assert session._lifetime_observe_actions == 12
    assert len(session._observe_actions) == 1


def test_observation_budget_pool_is_separate_and_warns_near_limit() -> None:
    session = BrowserSession(config(max_actions_per_window=2, lifetime_max_actions=10))
    session.note_action()
    warning = session.action_budget_warning(observe=False)
    assert "action_budget_warning:" in warning and "pool=action" in warning and "remaining_window=1" in warning
    session.note_action()
    with pytest.raises(ActionBudgetExhausted) as caught:
        session.note_action()
    assert "pool=action" in caught.value.render()

    # Observation pool still has its own bounded capacity (3x = 6 per window).
    for _ in range(6):
        session.note_observe_action()
    assert "pool=observe" in session.action_budget_warning(observe=True)
    with pytest.raises(ActionBudgetExhausted) as obs_caught:
        session.note_observe_action()
    assert "pool=observe" in obs_caught.value.render()

    # Zero lifetime budget still fails immediately on observation calls too.
    zero = BrowserSession(config(max_actions_per_window=10, lifetime_max_actions=0))
    with pytest.raises(ActionBudgetExhausted):
        zero.note_observe_action()


def test_ensure_started_refuses_after_close() -> None:
    session = BrowserSession(config())
    asyncio.run(session.aclose())
    with pytest.raises(Unavailable):
        asyncio.run(session.ensure_started())


class FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append(method)
        return {}

    async def aclose(self) -> None:
        self.closed = True


class FakeChild:
    def __init__(self) -> None:
        self.terminated = False

    async def terminate_tree(self) -> None:
        self.terminated = True


def test_aclose_tears_down_transport() -> None:
    session = BrowserSession(config())
    client = FakeClient()
    child = FakeChild()
    session._client = client
    session._child = child
    asyncio.run(session.aclose())
    assert client.closed is True
    assert child.terminated is True
    assert session.state == "closed"
