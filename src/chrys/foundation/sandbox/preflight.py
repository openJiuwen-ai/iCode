# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Capability admission checks, without starting engines or executing commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from chrys.foundation.sandbox.models import CapabilityReport, SandboxDecision, SandboxError


class SandboxProbe(Protocol):
    """Trusted adapter seam for preflight only; not an execution/lifecycle API.

    Probe implementations must not activate confinement, spawn a service or
    execute commands. Network negotiation and runtime ownership are deferred.
    """

    def probe(self, decision: SandboxDecision) -> CapabilityReport: ...


def check_requirements(decision: SandboxDecision, report: CapabilityReport, *, activated: bool = False) -> None:
    """Check availability before activation, actual enforcement after it.

    ``warn`` never overrides a mandatory requirement or a mismatched report.
    The caller must obtain a fresh report for its owned runtime after activation;
    this function cannot independently establish kernel enforcement.
    """
    if report.backend != decision.backend:
        raise SandboxError("backend_mismatch: capability report belongs to a different backend")
    effective = report.enforced if activated else report.available
    missing = decision.required - effective
    if missing:
        names = ", ".join(sorted(missing))
        raise SandboxError(f"requirements_not_met: {names}")


def preflight(decision: SandboxDecision, adapters: Mapping[str, SandboxProbe]) -> CapabilityReport | None:
    """Probe only the selected trusted adapter; disabled never touches adapters.

    There is no production registry, discovery or automatic fallback. The caller
    owns the registry; user configuration can select an ID, never an import path.
    No decision/report produced here means sandbox activation has occurred.
    """
    if not decision.enabled:
        return None
    adapter = adapters.get(decision.backend)
    if adapter is None:
        raise SandboxError("adapter_unavailable: selected backend has no registered adapter")
    report = adapter.probe(decision)
    if report.enforced:
        raise SandboxError("invalid_probe: preflight cannot claim activation")
    check_requirements(decision, report)
    return report
