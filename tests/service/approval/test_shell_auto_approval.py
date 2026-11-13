# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The approval boundary must not auto-approve expandable or exec-capable shell text."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools
from tests.support.waiting import wait_for


def _middleware(bus: EventBus) -> ApprovalMiddleware:
    return ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require")),
        bus,
        approval_mode=ApprovalMode.MANUAL,
    )


def _shell_tool():
    runtime = SessionEnvironment.capture()
    return ShellTools(runtime).tools()[0]


async def _record_requests(bus: EventBus) -> list[ApprovalRequest]:
    requests: list[ApprovalRequest] = []

    async def record(event: ApprovalRequest) -> None:
        requests.append(event)

    await bus.subscribe(ApprovalRequest, record)
    return requests


@pytest.mark.skipif(sys.platform == "win32", reason="Windows shells are intentionally not auto-approved yet")
@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat README.md",
        "grep TODO src/chrys/service",
        "git log --oneline",
        "echo hello",
    ],
)
async def test_safe_shell_commands_skip_the_dialog(command: str) -> None:
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus)
    called = AsyncMock()

    await middleware.process(FunctionInvocationContext(_shell_tool(), {"command": command}), called)

    called.assert_awaited_once()
    assert requests == []


@pytest.mark.parametrize(
    "command",
    [
        "rg --pre ./x.sh -e y .",
        "rg --pre=./x.sh -e y .",
        "rg --pre$IFS./x.sh -e y .",
        "rg --p''re ./x.sh -e y .",
        "r''g --pre ./x.sh -e y .",
        "\\rg --pre ./x.sh -e y .",
        "rg -n TODO .",
        "cat .env*",
        "cat .env",
        "cat .en''v",
        "cat .gi''t/con''fig",
        "cat .ss''h/id_r''sa",
        "cat /proc/self/environ",
        "cat /proc/self/envi''ron",
        "git status --short",
        "tree -o out .",
        "date -s 2020-01-01",
        "hostname evil",
        "less README.md",
    ],
)
async def test_executable_or_expandable_shell_commands_require_the_dialog(command: str) -> None:
    bus = EventBus()
    requests = await _record_requests(bus)
    middleware = _middleware(bus)
    called = AsyncMock()

    task = asyncio.create_task(
        middleware.process(FunctionInvocationContext(_shell_tool(), {"command": command}), called)
    )
    await wait_for(lambda: len(requests) == 1, description="shell approval request")
    assert called.await_count == 0

    await bus.publish(ApprovalResponse(request_id=requests[0].request_id, approved=False, reason="test"))
    await task

    assert called.await_count == 0
    assert requests[0].args["command"] == command
