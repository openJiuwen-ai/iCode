# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A dispatched sandbox command is not executed again after an IPC timeout.

The runtime is a fake. Nothing starts jiuwenbox, bubblewrap, or a socket.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_SANDBOX_ID = "sb-1"
_COMMAND = ["npm", "test"]


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows.

    ``pwd`` and ``grp`` are imported at module scope. The stubs only exist
    so those imports succeed; no account lookup runs.
    """
    import os

    if not hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    def _missing(*_args: object) -> None:
        raise KeyError(_args)

    def _install(name: str) -> None:
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ModuleNotFoundError:
            module = types.ModuleType(name)
            if name == "pwd":
                module.getpwuid = _missing  # type: ignore[attr-defined]
                module.getpwnam = _missing  # type: ignore[attr-defined]
            else:
                module.getgrnam = _missing  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, module)

    _install("pwd")
    _install("grp")


class _Runtime:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, object]] = []

    async def exec(self, sandbox_id: str, request: object) -> object:
        self.calls.append((sandbox_id, request))
        if not self._results:
            raise AssertionError("runtime.exec was called again")
        return self._results.pop(0)


class _Audit:
    def log(self, *_args: object, **_kwargs: object) -> None:
        return None


def _manager(tmp_path: Path, runtime: _Runtime) -> object:
    from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.sandbox_manager import SandboxManager

    manager = SandboxManager(
        runtime=runtime,  # type: ignore[arg-type]
        policy_engine=PolicyEngine(policies_dir=tmp_path / "policies"),
        audit_logger=_Audit(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        policy_path=tmp_path / "missing-policy.yaml",
        update_policy_path=tmp_path / "missing-updates.yaml",
    )
    manager._sandboxes[_SANDBOX_ID] = SandboxRef(
        id=_SANDBOX_ID,
        phase=SandboxPhase.READY,
        runtime="process",
    )
    return manager


async def _exec(manager: object, runtime: _Runtime) -> tuple[object, list[str]]:
    from jiuwenbox.server.sandbox_manager import SandboxExecRequest

    restarted: list[str] = []

    async def _restart(sandbox_id: str) -> object:
        restarted.append(sandbox_id)
        return manager._sandboxes[sandbox_id]  # type: ignore[attr-defined]

    manager.restart_sandbox = _restart  # type: ignore[attr-defined]
    result = await manager.exec_in_sandbox(  # type: ignore[attr-defined]
        _SANDBOX_ID,
        SandboxExecRequest(command=list(_COMMAND), timeout=30),
    )
    return result, restarted


@pytest.mark.parametrize(
    ("exit_code", "stderr"),
    [
        (124, "daemon IPC timeout: timed out"),
        (124, "Command timed out"),
        (1, "daemon IPC transport failure: Connection reset"),
        (1, "daemon IPC unavailable: [Errno 111] Connection refused"),
    ],
)
async def test_exec_that_may_have_run_is_not_sent_again(
    jiuwenbox_importable: None,
    tmp_path: Path,
    exit_code: int,
    stderr: str,
) -> None:
    from jiuwenbox.models.sandbox import ExecResult

    failure = ExecResult(exit_code=exit_code, stderr=stderr)
    second = ExecResult(exit_code=0, stdout="ran again", stderr="")
    runtime = _Runtime([failure, second])
    result, restarted = await _exec(_manager(tmp_path, runtime), runtime)

    assert len(runtime.calls) == 1
    assert restarted == []
    assert result.exit_code == exit_code  # type: ignore[attr-defined]
    assert result.stderr == stderr  # type: ignore[attr-defined]
    assert runtime.calls[0][0] == _SANDBOX_ID
    assert runtime.calls[0][1].command == _COMMAND  # type: ignore[attr-defined]


async def test_channel_unavailable_before_dispatch_retries_once(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.models.sandbox import ExecResult

    unavailable = ExecResult(
        exit_code=1,
        stderr=(
            f"sandbox {_SANDBOX_ID!r} daemon IPC channel unavailable; "
            "the daemon is not running or its control socket is gone"
        ),
    )
    recovered = ExecResult(exit_code=0, stdout="ok", stderr="")
    runtime = _Runtime([unavailable, recovered])
    result, restarted = await _exec(_manager(tmp_path, runtime), runtime)

    assert len(runtime.calls) == 2
    assert restarted == [_SANDBOX_ID]
    assert result is recovered
    assert runtime.calls[0][1].command == _COMMAND  # type: ignore[attr-defined]
    assert runtime.calls[1][1].command == _COMMAND  # type: ignore[attr-defined]


def test_client_read_is_strictly_longer_than_post_kill_drain(jiuwenbox_importable: None) -> None:
    from jiuwenbox.server.runtime import process
    from jiuwenbox.supervisor import sandbox_daemon

    drain = sandbox_daemon._POST_KILL_DRAIN_SECONDS
    assert drain < sandbox_daemon._IPC_CLIENT_MARGIN_SECONDS
    assert drain < process.EXEC_IPC_READ_MARGIN_SECONDS
    assert process.DEFAULT_EXEC_IPC_READ_TIMEOUT_SECONDS == 300.0 + process.EXEC_IPC_READ_MARGIN_SECONDS
