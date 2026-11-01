# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run ``{{!command}}`` placeholders and put their output where it is used.

``resolve_env_templates`` turns a placeholder into a handle; this module owns the
token each handle stands for.  A token is kept only here, in this process's
memory: it is never written to ``os.environ``, which every tool's subprocess
inherits and whose whole content keys the stdio MCP cache.

Handles become tokens in two places.  :class:`CommandCredentialAuth` does it for
each HTTP request, so a request whose token aged past the TTL, or was refused
with a 401, gets a new one.  A child process cannot be given a new token once
running, so its launch runs :func:`refresh` first and then :func:`substitute`.

Errors and logs name the setting and how the command failed, never the command
or its output: an ACP configuration error becomes a tool result the model reads.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from chrys.foundation.config.process_settings import process_settings
from chrys.foundation.platform.output_capture import BoundedCapture, drain_process_pipes
from chrys.foundation.platform.process import SubprocessStoppedError, managed_subprocess, wait_for_subprocess
from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env
from chrys.foundation.util.env_templates import (
    COMMAND_HANDLE_RE,
    EnvVarResolutionError,
    command_handles_in,
    command_placeholder,
)
from chrys.foundation.util.header_charset import find_non_printable_ascii
from chrys.service.prompt_shortcuts.shell import shell_argv

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 30.0
_OUTPUT_LIMIT_BYTES = 64 * 1024


class _CommandFailedError(Exception):
    """A command gave no usable token; the message says how and holds none of its output."""


@dataclass(slots=True)
class _Credential:
    token: str | None = None
    fetched_at: float = 0.0
    running: asyncio.Task[None] | None = None


_credentials: dict[str, _Credential] = {}


async def _run_command(command: str) -> str:
    """Return what *command* printed to stdout. Stderr is discarded: it can hold what stdout does."""
    stdout = BoundedCapture(_OUTPUT_LIMIT_BYTES)
    try:
        async with managed_subprocess(
            *shell_argv(command),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(Path.home()),
            env=build_shell_subprocess_env(),
        ) as proc:
            await wait_for_subprocess(
                drain_process_pipes(proc, stdout, stdout),
                timeout=COMMAND_TIMEOUT_SECONDS,
                process_group_id=proc.pid if sys.platform != "win32" else None,
            )
    except TimeoutError as exc:
        raise _CommandFailedError(f"timed out after {COMMAND_TIMEOUT_SECONDS:.0f} seconds") from exc
    except SubprocessStoppedError as exc:
        raise _CommandFailedError("stopped, waiting for input it cannot get") from exc
    except (OSError, ValueError) as exc:
        raise _CommandFailedError("could not start") from exc
    if proc.returncode != 0:
        raise _CommandFailedError(f"exited with code {proc.returncode}")
    captured = stdout.snapshot()
    token = captured.text().strip()
    if not token:
        raise _CommandFailedError("printed nothing")
    if captured.dropped or find_non_printable_ascii(token) is not None:
        raise _CommandFailedError("printed something other than one line of printable ASCII")
    return token


async def _run_and_keep(handle: str, credential: _Credential) -> None:
    placeholder = command_placeholder(handle)
    try:
        token = await _run_command(placeholder.command)
    except _CommandFailedError as failure:
        # The TTL is only a guess, so a token that aged past it may still work: a 401 says
        # when it stops, and starting the clock over keeps a broken command from running
        # before every request.
        credential.fetched_at = time.time()
        message = f"The command in {placeholder.location} {failure}. Run it in a terminal to see why."
        if credential.token is None:
            raise EnvVarResolutionError(message) from None
        logger.warning("%s Keeping its previous output.", message)
    else:
        credential.token = token
        credential.fetched_at = time.time()
    finally:
        credential.running = None


async def _fetch(handle: str) -> None:
    """Run *handle*'s command, or join the run in progress: one waiter's cancel stops no one else's."""
    credential = _credentials.setdefault(handle, _Credential())
    if credential.running is None:
        credential.running = asyncio.create_task(_run_and_keep(handle, credential))
    await asyncio.shield(credential.running)


async def ensure_fresh(values: Iterable[str]) -> None:
    """Have a token younger than the TTL for each handle in *values*.

    Raises :class:`EnvVarResolutionError` only for a command that has never given a token.
    """
    handles = command_handles_in(values)
    if not handles:
        return
    ttl = process_settings().credential_command_ttl_seconds
    for handle in handles:
        credential = _credentials.get(handle)
        if credential is None or credential.token is None or time.time() - credential.fetched_at >= ttl:
            await _fetch(handle)


async def refresh(values: Iterable[str]) -> None:
    """Run the command of each handle in *values* again, whatever its token's age."""
    for handle in command_handles_in(values):
        await _fetch(handle)


def substitute(value: str) -> str:
    """Replace each handle in *value* with its current token."""

    def token(match: re.Match[str]) -> str:
        credential = _credentials.get(match.group())
        if credential is None or credential.token is None:
            return match.group()
        return credential.token

    return COMMAND_HANDLE_RE.sub(token, value)


class CommandCredentialAuth(httpx.Auth):
    """Send each request with current tokens; after a 401, once more with a newer one.

    It runs below the SDKs and every retry lane, so the second try costs none of
    their budgets.
    """

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # An installed Auth takes the place of httpx's own Basic auth from the URL's credentials.
        if request.url.username or request.url.password:
            next(httpx.BasicAuth(request.url.username, request.url.password).auth_flow(request))
        templates = {name: value for name, value in request.headers.items() if COMMAND_HANDLE_RE.search(value)}

        def current() -> dict[str, str]:
            return {name: substitute(value) for name, value in templates.items()}

        await ensure_fresh(templates.values())
        sent = current()
        request.headers.update(sent)
        response = yield request
        if response.status_code != 401:
            return
        # Requests sharing this client race: another one's 401 may already have brought a newer token.
        if current() == sent:
            await refresh(templates.values())
        newer = current()
        if newer != sent:
            request.headers.update(newer)
            yield request
