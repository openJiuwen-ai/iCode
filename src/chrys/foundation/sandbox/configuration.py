# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure parser for the proposed versioned sandbox section.

Callers supply already-read, trust-separated inputs. No file/environment
reads, legacy migration, settings registration or runtime wiring happens
here. An absent section returns None for the caller's unchanged legacy path;
an explicit but unversioned section is rejected, never treated as disabled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from chrys.foundation.config.coercion import bool_coercer
from chrys.foundation.config.spec import Source
from chrys.foundation.sandbox.models import Capability, FailurePolicy, JiuwenboxOptions, SandboxDecision, SandboxError
from chrys.foundation.sandbox.options import parse_options

_BOOL = bool_coercer()
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_OVERRIDES = frozenset({"enabled", "profile", "on_unavailable"})


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SandboxError(f"invalid_configuration: {location} must be a string-keyed mapping")
    return dict(value)


def _identifier(value: object, location: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SandboxError(f"invalid_configuration: {location} must be a lowercase identifier")
    return value


def _boolean(value: object) -> bool:
    result = _BOOL(value)
    if not result.usable or not isinstance(result.value, bool):
        raise SandboxError("invalid_configuration: enabled must be a boolean")
    return result.value


def _policy(value: object) -> FailurePolicy:
    if not isinstance(value, str):
        raise SandboxError("invalid_configuration: on_unavailable must be refuse or warn")
    try:
        return FailurePolicy(value)
    except ValueError:
        raise SandboxError("invalid_configuration: on_unavailable must be refuse or warn") from None


def _requirements(value: object) -> frozenset[Capability]:
    result: set[Capability] = set()
    for name, requested in _mapping(value, "require").items():
        try:
            capability = Capability(name)
        except ValueError:
            raise SandboxError("unknown_requirement: require contains an unknown capability") from None
        # False means no requirement, not a request to disable a protection.
        if type(requested) is not bool:
            raise SandboxError("invalid_configuration: require values must be YAML booleans")
        if requested:
            result.add(capability)
    return frozenset(result)


def resolve_configuration(
    user: Mapping[str, object] | None,
    *,
    environment: Mapping[str, object] | None = None,
    cli: Mapping[str, object] | None = None,
    workdir: Mapping[str, object] | None = None,
) -> SandboxDecision | None:
    """Resolve one sandbox section, with USER < ENV < CLI overrides.

    ``environment`` and ``cli`` contain extracted fields, not raw OS env/argv;
    public names/flag parsing remain a later integration decision. Only user
    input defines profiles. Workdir values never influence versioned policy.
    Every supplied trusted field is validated, including shadowed overrides.
    """
    if user is None:
        if environment or cli:
            raise SandboxError("missing_configuration: overrides require a versioned user section")
        return None
    section = _mapping(user, "sandbox")
    if section.keys() - {"version", "enabled", "profile", "on_unavailable", "profiles"}:
        raise SandboxError("invalid_configuration: unknown or legacy sandbox fields; migration required")
    version = section.get("version")
    if type(version) is not int or version != 1:
        raise SandboxError("invalid_configuration: sandbox.version must be 1; legacy migration is not implemented")
    profiles = _mapping(section.get("profiles"), "profiles")
    parsed: dict[str, tuple[str, frozenset[Capability], dict[str, object]]] = {}
    for name, raw in profiles.items():
        _identifier(name, "profile name")
        profile = _mapping(raw, "profile definition")
        if profile.keys() - {"backend", "require", "options"}:
            raise SandboxError("invalid_configuration: unknown profile field")
        backend = _identifier(profile.get("backend"), "backend")
        required = _requirements(profile.get("require", {}))
        options = _mapping(profile.get("options", {}), "options")
        parsed[name] = backend, required, options

    enabled = False
    selected = ""
    policy = FailurePolicy.REFUSE
    origins = dict.fromkeys(("enabled", "profile", "on_unavailable"), Source.DEFAULT)
    for source, raw in ((Source.USER, section), (Source.ENV, environment), (Source.CLI, cli)):
        layer = _mapping(raw, "override") if raw is not None else {}
        if source != Source.USER and layer.keys() - _OVERRIDES:
            raise SandboxError(
                "invalid_configuration: overrides may only select/enable a profile or set failure policy"
            )
        if "enabled" in layer:
            enabled = _boolean(layer["enabled"])
            origins["enabled"] = source
        if "profile" in layer:
            selected = _identifier(layer["profile"], "selected profile")
            if selected not in parsed:
                raise SandboxError("unknown_profile: selected profile has no user definition")
            origins["profile"] = source
        if "on_unavailable" in layer:
            policy = _policy(layer["on_unavailable"])
            origins["on_unavailable"] = source
    if selected not in parsed:
        raise SandboxError("unknown_profile: a versioned section requires a selected user profile")
    backend, required, options = parsed[selected]
    parsed_options = parse_options(backend, options)
    if isinstance(parsed_options, JiuwenboxOptions):
        network_requirement = {"offline": Capability.NETWORK_OFFLINE, "guarded": Capability.NETWORK_GUARDED}
        if parsed_options.network in network_requirement:
            required = required | {network_requirement[parsed_options.network]}
    return SandboxDecision(
        enabled=enabled,
        profile=selected,
        backend=backend,
        on_unavailable=policy,
        required=required,
        options=parsed_options,
        origins=(*origins.items(), ("profiles", Source.USER)),
        diagnostics=("workdir_sandbox_ignored",) if workdir else (),
    )
