# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for ``{{!command}}`` tokens: when a command runs, what comes of its output, and what a request sends."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import psutil
import pytest

import chrys.service.command_credentials as credentials_module
from chrys.foundation.util.env_templates import EnvVarResolutionError, resolve_env_templates
from chrys.service.command_credentials import (
    CommandCredentialAuth,
    _CommandFailedError,
    ensure_fresh,
    refresh,
    substitute,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_URL = "https://provider.example/v1"


@pytest.fixture(autouse=True)
def _no_tokens_from_other_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials_module, "_credentials", {})


class _Commands:
    """Stands in for running a command: answers from a script and counts the runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tokens: list[str | BaseException] = []
        self.runs = 0
        self.release: asyncio.Event | None = None
        monkeypatch.setattr(credentials_module, "_run_command", self._run)

    async def _run(self, command: str) -> str:
        self.runs += 1
        if self.release is not None:
            await self.release.wait()
        outcome = self.tokens[0] if len(self.tokens) == 1 else self.tokens.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> _Commands:
    return _Commands(monkeypatch)


def _handle(command: str = "tool") -> str:
    return resolve_env_templates(f"{{{{!{command}}}}}", location="the test setting")


def _python_command(tmp_path: Path, code: str) -> str:
    script = tmp_path / "command.py"
    script.write_text(code, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


# ── What a run gives ─────────────────────────────────────────────────


async def test_a_token_is_what_the_command_printed_to_stdout_alone(tmp_path: Path) -> None:
    handle = _handle(_python_command(tmp_path, "import sys\nprint('the-token')\nprint('noise', file=sys.stderr)\n"))

    await ensure_fresh([handle])

    assert substitute(f"Bearer {handle}") == "Bearer the-token"


async def test_getting_a_token_leaves_the_process_environment_alone(tmp_path: Path) -> None:
    before = dict(os.environ)

    await ensure_fresh([_handle(_python_command(tmp_path, "print('the-token')\n"))])

    assert dict(os.environ) == before


async def test_a_command_runs_in_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    report = tmp_path / "report"
    code = (
        "import os, pathlib\n"
        f"pathlib.Path({str(report)!r}).write_text(os.path.realpath(os.getcwd()))\n"
        "print('the-token')\n"
    )

    await ensure_fresh([_handle(_python_command(tmp_path, code))])

    assert report.read_text() == os.path.realpath(home)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("import sys\nprint('secret-output')\nsys.exit(3)\n", "exited with code 3"),
        ("pass\n", "printed nothing"),
        ("print('secret-output')\nprint('second line')\n", "one line of printable ASCII"),
        ("import sys\nsys.stdout.buffer.write('secret-output\\u00e9'.encode())\n", "one line of printable ASCII"),
        (f"print('secret-output' * {64 * 1024})\n", "one line of printable ASCII"),
    ],
)
async def test_a_command_that_gives_no_usable_token_fails_without_showing_its_output_or_text(
    tmp_path: Path, code: str, reason: str
) -> None:
    handle = _handle(_python_command(tmp_path, code))

    with pytest.raises(EnvVarResolutionError) as raised:
        await ensure_fresh([handle])

    message = str(raised.value)
    assert reason in message
    assert "the test setting" in message
    assert "secret-output" not in message
    assert "command.py" not in message


async def test_a_command_that_outlives_its_time_is_ended_with_its_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    code = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(300)\n"
    )
    wait_for_subprocess = credentials_module.wait_for_subprocess
    children: list[psutil.Process] = []

    async def run_out_once_started(work: Awaitable[None], *, timeout: float, process_group_id: int | None) -> None:
        await wait_for(lambda: pid_file.exists() and pid_file.read_text(), timeout=ENGINE_TURN_TIMEOUT)
        children.append(psutil.Process(int(pid_file.read_text())))
        await wait_for_subprocess(work, timeout=0.1, process_group_id=process_group_id)

    monkeypatch.setattr(credentials_module, "wait_for_subprocess", run_out_once_started)

    with pytest.raises(EnvVarResolutionError, match="timed out"):
        await ensure_fresh([_handle(_python_command(tmp_path, code))])

    await wait_for(lambda: not children[0].is_running() or children[0].status() == psutil.STATUS_ZOMBIE)


# ── When a command runs ──────────────────────────────────────────────


async def test_a_token_is_reused_until_it_is_older_than_the_setting(
    commands: _Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHRYS_CREDENTIAL_COMMAND_TTL_SECONDS", "120")
    commands.tokens = ["first", "second"]
    handle = _handle()

    await ensure_fresh([handle])
    credentials_module._credentials[handle].fetched_at -= 100
    await ensure_fresh([handle])
    assert commands.runs == 1
    assert substitute(handle) == "first"

    credentials_module._credentials[handle].fetched_at -= 21
    await ensure_fresh([handle])
    assert commands.runs == 2
    assert substitute(handle) == "second"


async def test_a_token_lasts_an_hour_by_default(commands: _Commands) -> None:
    commands.tokens = ["first", "second"]
    handle = _handle()

    await ensure_fresh([handle])
    credentials_module._credentials[handle].fetched_at -= 3500
    await ensure_fresh([handle])
    assert commands.runs == 1

    credentials_module._credentials[handle].fetched_at -= 101
    await ensure_fresh([handle])
    assert commands.runs == 2


async def test_refresh_runs_the_command_whatever_the_age_and_once_for_a_repeated_handle(commands: _Commands) -> None:
    commands.tokens = ["first", "second"]
    handle = _handle()
    await ensure_fresh([handle])

    await refresh([handle, f"Bearer {handle}"])

    assert commands.runs == 2
    assert substitute(handle) == "second"


async def test_values_without_a_handle_run_nothing(commands: _Commands) -> None:
    await ensure_fresh(["sk-plain", f"chrys-cmd-{'0' * 32}"])
    await refresh(["sk-plain"])

    assert commands.runs == 0
    assert substitute("sk-plain") == "sk-plain"


async def test_callers_at_the_same_time_share_one_run(commands: _Commands) -> None:
    commands.tokens = ["only"]
    commands.release = asyncio.Event()
    handle = _handle()
    waiters = [asyncio.create_task(ensure_fresh([handle])) for _ in range(3)]
    await wait_for(lambda: commands.runs == 1)

    commands.release.set()
    await asyncio.gather(*waiters)

    assert commands.runs == 1
    assert substitute(handle) == "only"


async def test_a_cancelled_caller_stops_nobody_elses_wait(commands: _Commands) -> None:
    commands.tokens = ["only"]
    commands.release = asyncio.Event()
    handle = _handle()
    leaving = asyncio.create_task(ensure_fresh([handle]))
    staying = asyncio.create_task(ensure_fresh([handle]))
    await wait_for(lambda: commands.runs == 1)

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    assert not staying.done()

    commands.release.set()
    await staying
    assert substitute(handle) == "only"


# ── A command that stops working ─────────────────────────────────────


async def test_a_failed_refresh_keeps_the_token_and_starts_its_age_over(
    commands: _Commands, caplog: pytest.LogCaptureFixture
) -> None:
    commands.tokens = ["kept", _CommandFailedError("exited with code 1"), "next"]
    handle = _handle("secret-command")
    await ensure_fresh([handle])

    credentials_module._credentials[handle].fetched_at -= 3601
    with caplog.at_level(logging.WARNING, logger=credentials_module.__name__):
        await ensure_fresh([handle])

    assert substitute(handle) == "kept"
    assert "exited with code 1" in caplog.text
    assert "the test setting" in caplog.text
    assert "secret-command" not in caplog.text

    await ensure_fresh([handle])
    assert commands.runs == 2


async def test_a_command_that_has_never_worked_is_tried_again_and_can_recover(commands: _Commands) -> None:
    commands.tokens = [_CommandFailedError("exited with code 1"), "works"]
    handle = _handle()

    with pytest.raises(EnvVarResolutionError):
        await ensure_fresh([handle])
    assert substitute(handle) == handle

    await ensure_fresh([handle])
    assert substitute(handle) == "works"


# ── What a request sends ─────────────────────────────────────────────


def _client(handler: Callable[[httpx.Request], httpx.Response], auth: httpx.Auth) -> httpx.AsyncClient:
    return httpx.AsyncClient(auth=auth, transport=httpx.MockTransport(handler))


def _accepting(token: str, seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200 if request.headers["Authorization"] == f"Bearer {token}" else 401)

    return handler


async def test_a_request_goes_out_with_the_current_token_and_no_handle(commands: _Commands) -> None:
    commands.tokens = ["good"]
    seen: list[str] = []
    async with _client(_accepting("good", seen), CommandCredentialAuth([_handle()])) as client:
        response = await client.post(_URL, headers={"Authorization": f"Bearer {_handle()}"})

    assert response.status_code == 200
    assert seen == ["Bearer good"]
    assert commands.runs == 1


async def test_a_refused_token_is_replaced_and_the_request_sent_once_more(commands: _Commands) -> None:
    commands.tokens = ["expired", "good"]
    seen: list[str] = []
    async with _client(_accepting("good", seen), CommandCredentialAuth([_handle()])) as client:
        response = await client.post(_URL, headers={"Authorization": f"Bearer {_handle()}"})

    assert response.status_code == 200
    assert seen == ["Bearer expired", "Bearer good"]
    assert commands.runs == 2


async def test_the_same_token_again_is_not_sent_again(commands: _Commands) -> None:
    commands.tokens = ["refused"]
    seen: list[str] = []
    async with _client(_accepting("good", seen), CommandCredentialAuth([_handle()])) as client:
        response = await client.post(_URL, headers={"Authorization": f"Bearer {_handle()}"})

    assert response.status_code == 401
    assert seen == ["Bearer refused"]
    assert commands.runs == 2


async def test_a_second_refusal_ends_it(commands: _Commands) -> None:
    commands.tokens = ["refused-1", "refused-2", "good"]
    seen: list[str] = []
    async with _client(_accepting("good", seen), CommandCredentialAuth([_handle()])) as client:
        response = await client.post(_URL, headers={"Authorization": f"Bearer {_handle()}"})

    assert response.status_code == 401
    assert seen == ["Bearer refused-1", "Bearer refused-2"]


async def test_a_failing_command_leaves_the_refusal_to_answer(commands: _Commands) -> None:
    commands.tokens = ["expired", _CommandFailedError("exited with code 1")]
    seen: list[str] = []
    async with _client(_accepting("good", seen), CommandCredentialAuth([_handle()])) as client:
        response = await client.post(_URL, headers={"Authorization": f"Bearer {_handle()}"})

    assert response.status_code == 401
    assert seen == ["Bearer expired"]


async def test_a_status_other_than_401_runs_nothing(commands: _Commands) -> None:
    commands.tokens = ["good"]
    async with _client(lambda _request: httpx.Response(403), CommandCredentialAuth([_handle()])) as client:
        response = await client.post(_URL, headers={"Authorization": f"Bearer {_handle()}"})

    assert response.status_code == 403
    assert commands.runs == 1


async def test_a_request_refused_after_another_brought_a_new_token_just_uses_it(commands: _Commands) -> None:
    """Both left with the old token; the first refusal's refresh also serves the later one, command unchanged."""
    commands.tokens = ["old", "new"]
    first_done = asyncio.Event()
    second_sent = asyncio.Event()
    handle = _handle()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer new":
            return httpx.Response(200)
        if request.url.path.endswith("/late"):
            second_sent.set()
            await first_done.wait()
        return httpx.Response(401)

    async with httpx.AsyncClient(
        auth=CommandCredentialAuth([handle]), transport=httpx.MockTransport(handler)
    ) as client:
        await ensure_fresh([handle])
        late = asyncio.create_task(client.post(f"{_URL}/late", headers={"Authorization": f"Bearer {handle}"}))
        await second_sent.wait()
        early = await client.post(_URL, headers={"Authorization": f"Bearer {handle}"})
        first_done.set()
        late_response = await late

    assert early.status_code == 200
    assert late_response.status_code == 200
    assert commands.runs == 2


async def test_credentials_in_the_url_still_send_basic_auth(commands: _Commands) -> None:
    commands.tokens = ["good"]
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(httpx.Headers(request.headers))
        return httpx.Response(200)

    async with _client(handler, CommandCredentialAuth([_handle()])) as client:
        await client.post("https://user:pass@provider.example/v1", headers={"X-Gateway": _handle()})

    assert seen[0]["Authorization"] == f"Basic {base64.b64encode(b'user:pass').decode()}"
    assert seen[0]["X-Gateway"] == "good"


@pytest.mark.parametrize("other_has_a_token", [False, True])
async def test_only_the_handles_it_was_given_become_tokens(commands: _Commands, other_has_a_token: bool) -> None:
    """Anything else shaped like a handle, on its own or beside one of them, stays what it is and runs nothing."""
    commands.tokens = ["good"]
    own, other = _handle(), _handle("other")
    if other_has_a_token:
        await refresh([other])
    runs_before = commands.runs
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(httpx.Headers(request.headers))
        return httpx.Response(200)

    async with _client(handler, CommandCredentialAuth([f"Bearer {own}"])) as client:
        await client.post(_URL, headers={"X-Both": f"{own} {other}", "X-Other": other})

    assert seen[0]["X-Both"] == f"good {other}"
    assert seen[0]["X-Other"] == other
    assert commands.runs == runs_before + 1


async def test_a_request_without_a_handle_is_left_alone(commands: _Commands) -> None:
    seen: list[str] = []
    async with _client(_accepting("plain", seen), CommandCredentialAuth([])) as client:
        response = await client.post(_URL, headers={"Authorization": "Bearer plain"})

    assert response.status_code == 200
    assert seen == ["Bearer plain"]
    assert commands.runs == 0
