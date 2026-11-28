# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract examples for both backend shapes; these are not enforcing engines."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from chrys.foundation.sandbox.configuration import resolve_configuration
from chrys.foundation.sandbox.models import Capability, CapabilityReport, SandboxDecision, SandboxError
from chrys.foundation.sandbox.preflight import check_requirements, preflight


@dataclass
class ExampleProbe:
    report: CapabilityReport
    decisions: list[SandboxDecision] = field(default_factory=list)

    def probe(self, decision: SandboxDecision) -> CapabilityReport:
        self.decisions.append(decision)
        return self.report


def decision(backend: str, required: Capability, *, enabled: bool = True) -> SandboxDecision:
    resolved = resolve_configuration(
        {
            "version": 1,
            "enabled": enabled,
            "profile": "selected",
            "on_unavailable": "warn",
            "profiles": {
                "selected": {
                    "backend": backend,
                    "require": {required.value: True},
                    "options": {"network": "host"} if backend == "jiuwenbox" else {},
                }
            },
        }
    )
    assert resolved is not None
    return resolved


@pytest.mark.parametrize(
    "backend,capability",
    [
        ("process", Capability.FILE_READ),
        ("jiuwenbox", Capability.SHELL_WRITE),
    ],
)
def test_both_adapter_shapes_receive_one_decision(backend: str, capability: Capability) -> None:
    policy = decision(backend, capability)
    report = CapabilityReport(backend, frozenset({capability}), frozenset({capability}))
    probe = ExampleProbe(report)
    unused = ExampleProbe(CapabilityReport("unused"))
    result = preflight(policy, {backend: probe, "unused": unused})
    assert result == report
    assert result.enforced == frozenset()
    assert probe.decisions == [policy]
    assert unused.decisions == []
    # Availability is insufficient evidence for command admission after activation.
    with pytest.raises(SandboxError, match="requirements_not_met"):
        check_requirements(policy, report, activated=True)
    check_requirements(policy, replace(report, enforced=frozenset({capability})), activated=True)


@pytest.mark.parametrize(
    "backend,required,supported",
    [
        ("jiuwenbox", Capability.FILE_READ, Capability.SHELL_WRITE),
        ("process", Capability.NETWORK_OFFLINE, Capability.FILE_READ),
        ("jiuwenbox", Capability.MEMORY_LIMIT, Capability.MEMORY_LIMIT),
    ],
)
def test_missing_scope_or_host_capability_refuses_even_with_warn(
    backend: str,
    required: Capability,
    supported: Capability,
) -> None:
    policy = decision(backend, required)
    probe = ExampleProbe(CapabilityReport(backend, frozenset({supported})))
    with pytest.raises(SandboxError, match="requirements_not_met"):
        preflight(policy, {backend: probe})
    assert probe.decisions == [policy]


def test_missing_selected_adapter_does_not_try_an_available_alternative() -> None:
    alternate = ExampleProbe(CapabilityReport("jiuwenbox"))
    with pytest.raises(SandboxError, match="adapter_unavailable"):
        preflight(decision("process", Capability.FILE_READ), {"jiuwenbox": alternate})
    assert alternate.decisions == []


def test_disabled_does_not_probe_or_require_an_adapter() -> None:
    policy = decision("process", Capability.FILE_READ, enabled=False)
    probe = ExampleProbe(CapabilityReport("process"))
    assert preflight(policy, {}) is None
    assert preflight(policy, {"process": probe}) is None
    assert probe.decisions == []


def test_report_cannot_claim_a_different_backend() -> None:
    probe = ExampleProbe(CapabilityReport("jiuwenbox"))
    with pytest.raises(SandboxError, match="backend_mismatch"):
        preflight(decision("process", Capability.FILE_READ), {"process": probe})


def test_probe_cannot_claim_it_activated_a_sandbox() -> None:
    capabilities = frozenset({Capability.FILE_READ})
    probe = ExampleProbe(CapabilityReport("process", capabilities, capabilities, capabilities))
    with pytest.raises(SandboxError, match="invalid_probe"):
        preflight(decision("process", Capability.FILE_READ), {"process": probe})


@pytest.mark.parametrize(
    "available,enforced",
    [
        (frozenset({Capability.FILE_READ}), frozenset()),
        (frozenset(), frozenset({Capability.FILE_READ})),
    ],
)
def test_reports_reject_inconsistent_capability_sets(
    available: frozenset[Capability],
    enforced: frozenset[Capability],
) -> None:
    with pytest.raises(SandboxError, match="invalid_capability_report"):
        CapabilityReport("process", available=available, enforced=enforced)


@pytest.mark.parametrize(
    "mode,capability",
    [
        ("offline", Capability.NETWORK_OFFLINE),
        ("guarded", Capability.NETWORK_GUARDED),
    ],
)
def test_network_mode_is_mandatory_without_explicit_require(mode: str, capability: Capability) -> None:
    policy = resolve_configuration(
        {
            "version": 1,
            "enabled": True,
            "profile": "remote",
            "profiles": {"remote": {"backend": "jiuwenbox", "options": {"network": mode}}},
        }
    )
    assert policy is not None
    assert policy.required == frozenset({capability})
    with pytest.raises(SandboxError, match="requirements_not_met"):
        preflight(policy, {"jiuwenbox": ExampleProbe(CapabilityReport("jiuwenbox"))})
    supported = frozenset({capability})
    report = CapabilityReport("jiuwenbox", supported, supported)
    assert preflight(policy, {"jiuwenbox": ExampleProbe(report)}) == report
