# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Detached interrupt observers retain their task and scheduling identity."""

from __future__ import annotations

import asyncio
import gc
import weakref
from types import SimpleNamespace
from unittest.mock import create_autospec

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.workspace import Workspace
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.session.persistence import SessionPersistence
from tests.support.components import make_current, make_hooks, make_session, plan_workspace
from tests.support.waiting import wait_for


async def test_interrupt_hook_uses_manager_and_identity_captured_before_dispatch(tmp_path) -> None:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    original_cwd = str(tmp_path / "original")
    session.install_workspace(plan_workspace(Workspace.from_cwd(original_cwd)))
    session.reset(session_id="original")
    session.agent_profile = SimpleNamespace(name="Original")
    original = create_autospec(HookManager, instance=True)
    replacement = create_autospec(HookManager, instance=True)
    original.has_hooks_for.return_value = True
    session.hook_manager = original
    dispatched = asyncio.Event()

    async def record(event, payload, *, scope):
        dispatched.set()

    original.fire.side_effect = record
    hooks = make_hooks(session=session, current=make_current())
    hooks.schedule_user_interrupt()
    session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path / "replacement"))))
    session.reset(session_id="replacement")
    session.hook_manager = replacement
    session.agent_profile = SimpleNamespace(name="Replacement")
    await wait_for(dispatched.is_set, description="detached interrupt observer dispatches")
    original.fire.assert_awaited_once_with(
        HookEvent.USER_INTERRUPT,
        {"session_id": "original", "profile": "Original", "cwd": original_cwd},
        scope="detached",
    )
    replacement.fire.assert_not_awaited()


async def test_interrupt_dispatcher_retains_unreachable_waiting_task_until_completion() -> None:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    manager = create_autospec(HookManager, instance=True)
    manager.has_hooks_for.return_value = True
    session.hook_manager = manager
    entered = asyncio.Event()
    task_refs = []
    future_refs = []

    async def park(event, payload, *, scope):
        task_refs.append(weakref.ref(asyncio.current_task()))
        future = asyncio.get_running_loop().create_future()
        future_refs.append(weakref.ref(future))
        entered.set()
        await future

    manager.fire.side_effect = park
    hooks = make_hooks(session=session, current=make_current())
    hooks.schedule_user_interrupt()
    await wait_for(entered.is_set, description="interrupt task is parked on an otherwise unreachable future")
    gc.collect()
    assert task_refs[0]() is not None
    assert future_refs[0]() is not None
    future_refs[0]().set_result(None)
    task = task_refs[0]()
    await task
    completed_ref = weakref.ref(task)
    del task
    resumed = asyncio.Event()
    asyncio.get_running_loop().call_soon(resumed.set)
    await resumed.wait()
    gc.collect()
    assert completed_ref() is None
    manager.fire.assert_awaited_once()
