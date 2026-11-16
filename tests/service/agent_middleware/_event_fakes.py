# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared doubles for the tool-event middleware tests: invocation contexts, bus collectors, a hook manager."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.schema import HookDecision


def _ctx(
    name: str,
    kind: str | None = None,
    *,
    args: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build the minimal invocation-context stand-in the event middlewares read and write."""
    return SimpleNamespace(
        function=SimpleNamespace(name=name, chrys_kind=kind),
        arguments={} if args is None else args,
        result=None,
        metadata={} if metadata is None else metadata,
    )


class _TableHookManager:
    """Base for the scenario hook-manager doubles: answers ``fire`` from a fixed decision table.

    ``has_hooks_for`` is true exactly for the configured events, and firing an event
    the scenario never configured raises: production reaching an unscripted hook is a
    defect, not a no-op. Subclasses add whatever single recorder their scenario asserts
    on and nothing else, so a test still fails loudly if production reads state that
    scenario deliberately does not have.
    """

    def __init__(self, decisions: Mapping[HookEvent, HookDecision]) -> None:
        self._decisions = dict(decisions)

    def has_hooks_for(self, event: HookEvent) -> bool:
        return event in self._decisions

    async def fire(self, event: HookEvent, payload: dict[str, Any], **_kwargs: object) -> HookDecision:
        if event not in self._decisions:
            raise AssertionError(f"unscripted hook fired: {event}")
        self._record(event, payload)
        return self._decisions[event]

    def _record(self, event: HookEvent, payload: dict[str, Any]) -> None:
        """Record the call; the base double keeps no state."""


class DenyBeforeToolHookManager(_TableHookManager):
    """Blocks the before-hook and keeps only the after-hook payloads.

    The scenario asserts what the middleware reports back after a denial, so the
    after payloads are the only state; there is deliberately no record of the
    before-hook call to fall back on.
    """

    def __init__(self, decisions: Mapping[HookEvent, HookDecision]) -> None:
        super().__init__(decisions)
        self.after_payloads: list[dict[str, Any]] = []

    def _record(self, event: HookEvent, payload: dict[str, Any]) -> None:
        if event is HookEvent.AFTER_TOOL_CALL:
            self.after_payloads.append(payload)


class PayloadRecordingHookManager(_TableHookManager):
    """Keeps every payload a hook fired with, so a scenario can assert what hooks are handed."""

    def __init__(self, decisions: Mapping[HookEvent, HookDecision]) -> None:
        super().__init__(decisions)
        self.payloads: dict[HookEvent, list[dict[str, Any]]] = {}

    def _record(self, event: HookEvent, payload: dict[str, Any]) -> None:
        self.payloads.setdefault(event, []).append(payload)


class DualDecisionHookManager(_TableHookManager):
    """Records the order the after/error hooks fired in, and nothing else.

    The scenario is about merging two decisions, so only the firing order is
    observable: it carries no payloads to assert on instead.
    """

    def __init__(self, decisions: Mapping[HookEvent, HookDecision]) -> None:
        super().__init__(decisions)
        self.events: list[HookEvent] = []

    def _record(self, event: HookEvent, _payload: dict[str, Any]) -> None:
        self.events.append(event)


class RewriteArgsHookManager(_TableHookManager):
    """A stateless hook manager that only rewrites arguments.

    The scenario asserts on the rewritten arguments the middleware produces, so
    the double records nothing: a test reaching for call history here is asserting
    on the wrong thing.
    """
