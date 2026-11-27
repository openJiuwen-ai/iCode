# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Immutable sandbox policy and scoped capability vocabulary.

These types describe decisions, not proof of confinement. Only an adapter's
post-activation report can describe enforcement; a preflight report cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from chrys.foundation.config.spec import Source


class SandboxError(ValueError):
    """A safe diagnostic containing schema locations/codes, never raw options."""


class Capability(StrEnum):
    FILE_READ = "file_tools.read_boundary"
    FILE_WRITE = "file_tools.write_boundary"
    SHELL_WRITE = "shell.write_boundary"
    CREDENTIAL_FLOOR = "process.credential_floor"
    NETWORK_OFFLINE = "shell.network_offline"
    NETWORK_GUARDED = "shell.network_guarded"
    MEMORY_LIMIT = "shell.memory_limit"
    PROCESS_LIMIT = "shell.process_limit"
    STREAMING = "shell.streaming"
    PTY = "shell.pty"
    BACKGROUND = "shell.background"


class FailurePolicy(StrEnum):
    REFUSE = "refuse"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class ProcessOptions:
    """No new process options yet; legacy path/grant migration is separate."""


@dataclass(frozen=True, slots=True)
class JiuwenboxOptions:
    network: str = "guarded"
    url: str = field(default="", repr=False)
    token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class SandboxDecision:
    enabled: bool
    profile: str
    backend: str
    on_unavailable: FailurePolicy
    required: frozenset[Capability]
    options: ProcessOptions | JiuwenboxOptions = field(repr=False)
    origins: tuple[tuple[str, Source], ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """Absent capabilities are unsupported; available/enforced are subsets.

    Each capability names its scope. A successful probe is not activation and
    must leave ``enforced`` empty.
    """

    backend: str
    supported: frozenset[Capability] = frozenset()
    available: frozenset[Capability] = frozenset()
    enforced: frozenset[Capability] = frozenset()

    def __post_init__(self) -> None:
        if not all(isinstance(value, Capability) for value in self.supported | self.available | self.enforced):
            raise SandboxError("invalid_capability_report: unknown capability")
        if not self.enforced <= self.available <= self.supported:
            raise SandboxError("invalid_capability_report: enforced <= available <= supported is required")
