# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Count the runtimes in this process that may run sandboxed commands.

Importing this module does not import the supervisor or its HTTP client; the
last release imports them to stop the bundled server and to delete what this
process created on a configured ``sandbox.url``.
"""

from __future__ import annotations

_runtimes = 0
_bundled_started = False
_external_used = False


def note_bundled_server_started() -> None:
    """Record that this process started a bundled server, which ``retire_bundled_server`` may stop."""
    global _bundled_started
    _bundled_started = True


def note_external_server_used() -> None:
    """Record that a command ran on a configured ``sandbox.url``, whose sandboxes the last release deletes."""
    global _external_used
    _external_used = True


async def retire_bundled_server() -> None:
    """Stop the bundled server for a command that does not use it.

    A command with the sandbox disabled, or with ``sandbox.url`` set, calls
    this so a long session does not keep the bundled server it used before.
    A server a command still leases stops when that lease ends. Before any
    bundled start this returns without importing the supervisor.
    """
    if not _bundled_started:
        return
    from chrys.service.sandbox.supervisor import retire

    await retire()


class RuntimeHold:
    """One runtime's claim on the sandbox servers for as long as it is live.

    Engine assembly builds one per engine; the engine acquires it once it takes
    events and releases it once at shutdown. Both are idempotent, and a hold that
    was never acquired releases nothing. When the last held runtime releases,
    this process has no runtime left to run sandboxed commands: the sandboxes
    it created on a configured ``sandbox.url`` are deleted there, and
    ``supervisor.shutdown`` stops every bundled server.
    """

    def __init__(self) -> None:
        self._held = False

    def acquire(self) -> None:
        global _runtimes
        if self._held:
            return
        self._held = True
        _runtimes += 1

    async def release(self) -> None:
        global _runtimes
        if not self._held:
            return
        self._held = False
        _runtimes -= 1
        if _runtimes:
            return
        try:
            if _external_used:
                from chrys.service.sandbox.backend import release_external_sandboxes

                await release_external_sandboxes()
        finally:
            from chrys.service.sandbox.supervisor import shutdown

            await shutdown()
