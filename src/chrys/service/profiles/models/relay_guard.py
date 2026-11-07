# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Save-time guard tying relay model profiles to an independent approval judge.

A profile marked ``is_relay`` routes prompts through a third-party relay
service.  The AUTO approval flow asks a judge model to review tool calls,
so while any configured profile is a relay the judge must be a different,
independently configured direct service: a judge reached through the same
host — or the relay itself — would let the relay's operator approve the
very traffic the relay carries.

Every check judges the *prospective* profile set, the state as it will
exist after the pending write, never the pre-write registry: a profile
being promoted to a relay cannot borrow the record it is about to
replace, and deleting the current judge is caught before its file is
gone.  "Usable" here means structurally valid and credentialed, not
verified reachable — the guard never sends a request and never runs a
credential command; a live connectivity check is a separate, explicit
user action.

Host independence is deliberately conservative: only a different
effective hostname counts.  The same host under a different path, API
protocol or model id is not another provider, and a different host is
still the user's trust declaration (``is_relay == false``), not operator
authentication.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from chrys.foundation.i18n import msg
from chrys.service.llm.providers import PROVIDERS
from chrys.service.profiles.models.schema import ModelProfile, is_model_profile_selectable

if TYPE_CHECKING:
    from chrys.foundation.i18n import MessageRef

# Endpoints on these hosts are local no-auth services (ollama-style); the
# provider SDKs send an empty key to them, so "credentialed" needs no key.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_JUDGE_UNSET = msg(
    "profiles.relay_guard.judge_unset",
    fallback=(
        "There are relay model profiles, but no approval judge model is set. "
        "Configure an independent non-relay model profile and select it as the approval judge first."
    ),
)
_JUDGE_UNRESOLVED = msg(
    "profiles.relay_guard.judge_unresolved",
    fallback="The approval judge {selector} does not match any configured model profile.",
)
_JUDGE_DELETE_DEPENDENCY = msg(
    "profiles.relay_guard.judge_delete_dependency",
    fallback=(
        "Cannot delete {name}: it is the approval judge required by relay model profiles. "
        "Select another independent approval judge first."
    ),
)
_JUDGE_IS_RELAY = msg(
    "profiles.relay_guard.judge_is_relay",
    fallback="The approval judge {judge} is itself marked as a relay; the judge must be an independent non-relay model.",
)
_JUDGE_MOCK = msg(
    "profiles.relay_guard.judge_mock",
    fallback="The approval judge cannot use the mock provider.",
)
_JUDGE_NOT_CONFIGURED = msg(
    "profiles.relay_guard.judge_not_configured",
    fallback="The approval judge {judge} is incomplete: model, provider or token limits are not fully configured.",
)
_JUDGE_TLS_DISABLED = msg(
    "profiles.relay_guard.judge_tls_disabled",
    fallback="The approval judge {judge} skips TLS certificate verification; the approval channel must verify certificates.",
)
_JUDGE_NO_CREDENTIALS = msg(
    "profiles.relay_guard.judge_no_credentials",
    fallback="The approval judge {judge} has no credentials: set an API key or its provider environment variable.",
)
_JUDGE_HOST_SHARED = msg(
    "profiles.relay_guard.judge_host_shared",
    fallback="The approval judge {judge} is served by the same host as the relay profile {relay}; "
    "the judge must be an independent service.",
)
_JUDGE_URL_INVALID = msg(
    "profiles.relay_guard.judge_url_invalid",
    fallback="The approval judge {judge} has no usable service address.",
)


@dataclass(frozen=True, slots=True)
class RelayGuardIssue:
    """One reason the prospective configuration violates the relay guard.

    ``message`` is the localized user-facing reference; ``text`` is the
    plain-English rendering for surfaces that do not localize (ACP).
    """

    code: str
    message: MessageRef
    text: str


def _issue(code: str, definition, **slots: str) -> RelayGuardIssue:
    return RelayGuardIssue(
        code=code,
        message=definition.bind(**slots),
        text=definition.fallback.format(**slots),
    )


def _display_name(profile: ModelProfile) -> str:
    return profile.name or profile.id


def effective_host(profile: ModelProfile) -> str | None:
    """The hostname of the base URL the provider client will actually use.

    Resolves through the same precedence as the real client (explicit
    ``base_url``, then the provider's environment override, then its
    default), so comparing raw YAML strings cannot be fooled by one side
    relying on a default.  ``None`` when the effective URL carries no host.
    """
    from chrys.service.llm.clients import effective_model_base_url

    try:
        url = effective_model_base_url(profile)
    except ValueError:
        return None
    return urlparse(url).hostname


def _has_call_credentials(judge: ModelProfile, judge_host: str | None) -> bool:
    """Whether the judge has some configured way to authenticate.

    Mirrors the client's own resolution order: an explicit key (literal,
    ``{{ENV}}`` or ``{{!command}}`` template — presence counts, the guard
    never resolves them), then the provider's credential environment
    variable, then local no-auth loopback endpoints.
    """
    if judge.api_key.strip():
        return True
    spec = PROVIDERS.get(judge.provider.lower())
    if spec is not None and os.environ.get(spec.api_key_env, "").strip():
        return True
    return judge_host in _LOOPBACK_HOSTS


def _resolve_in_set(profiles: Sequence[ModelProfile], selector: str) -> ModelProfile | None:
    """Resolve a selector by id or unique name inside a prospective set.

    Same matching order and ambiguity rules as the registry-based
    resolver: id first, then exact unique name, then casefolded unique
    name; an ambiguous name resolves to nothing.
    """
    for profile in profiles:
        if profile.id == selector:
            return profile
    exact = [profile for profile in profiles if profile.name == selector]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    folded = selector.casefold()
    folded_matches = [profile for profile in profiles if profile.name.casefold() == folded]
    if len(folded_matches) == 1:
        return folded_matches[0]
    return None


def _judge_issues(judge: ModelProfile, relays: Sequence[ModelProfile]) -> list[RelayGuardIssue]:
    issues: list[RelayGuardIssue] = []
    name = _display_name(judge)
    if judge.is_relay:
        issues.append(_issue("judge_is_relay", _JUDGE_IS_RELAY, judge=name))
    if judge.provider.lower() == "mock":
        # A mock judge is never usable; further per-field issues would only be noise.
        issues.append(_issue("judge_mock", _JUDGE_MOCK))
        return issues
    if not is_model_profile_selectable(judge):
        issues.append(_issue("judge_not_configured", _JUDGE_NOT_CONFIGURED, judge=name))
    if not judge.verify_ssl:
        issues.append(_issue("judge_tls_disabled", _JUDGE_TLS_DISABLED, judge=name))
    judge_host = effective_host(judge)
    if judge_host is None:
        issues.append(_issue("judge_url_invalid", _JUDGE_URL_INVALID, judge=name))
        return issues
    if not _has_call_credentials(judge, judge_host):
        issues.append(_issue("judge_no_credentials", _JUDGE_NO_CREDENTIALS, judge=name))
    for relay in relays:
        relay_host = effective_host(relay)
        if relay_host is not None and relay_host == judge_host:
            issues.append(_issue("judge_host_shared", _JUDGE_HOST_SHARED, relay=_display_name(relay), judge=name))
    return issues


def is_qualified_relay_judge(judge: ModelProfile, relays: Sequence[ModelProfile]) -> bool:
    """Runtime predicate: whether *judge* may review traffic carried by *relays*.

    The same qualification the save-time guard enforces, as one bool for the
    runtime resolution paths: not a relay, not mock, complete, TLS-verifying,
    credentialed, and served from a different effective host than every relay.
    """
    return not _judge_issues(judge, relays)


def relay_guard_issues(profiles: Sequence[ModelProfile], judge_selector: str) -> list[RelayGuardIssue]:
    """Validate the relay/approval-judge invariant over one profile set.

    The invariant: when any profile is a relay, ``judge_selector`` must
    resolve (by id or unique name) to a profile that is itself not a
    relay, not the mock provider, structurally complete, TLS-verifying,
    credentialed, and served from a different effective host than every
    relay.  With no relay profiles the setting stays free (the empty
    selector keeps its "follow the active model" meaning).
    """
    relays = [profile for profile in profiles if profile.is_relay]
    if not relays:
        return []
    if not judge_selector.strip():
        return [_issue("judge_unset", _JUDGE_UNSET)]
    judge = _resolve_in_set(profiles, judge_selector)
    if judge is None:
        return [_issue("judge_unresolved", _JUDGE_UNRESOLVED, selector=judge_selector)]
    return _judge_issues(judge, relays)


def check_profile_write(
    profiles: Sequence[ModelProfile],
    profile: ModelProfile,
    judge_selector: str,
) -> list[RelayGuardIssue]:
    """Guard a save/create/clone: validate the set as it will be after the write.

    A relay write owns the full invariant over the prospective set.  A
    non-relay write owns only its own corner of it: when the written
    profile is the resolved judge it must stay qualified (address,
    credentials, TLS), and a selection that is already broken elsewhere
    never blocks the writes that repair it — most importantly creating
    the future judge profile.
    """
    existed = any(existing.id == profile.id for existing in profiles)
    prospective = [profile if existing.id == profile.id else existing for existing in profiles]
    if not existed:
        prospective.append(profile)
    if profile.is_relay:
        return relay_guard_issues(prospective, judge_selector)
    judge = _resolve_in_set(prospective, judge_selector) if judge_selector.strip() else None
    if judge is None or judge.id != profile.id:
        return []
    relays = [candidate for candidate in prospective if candidate.is_relay]
    if not relays:
        # With no relay profiles the invariant is vacuous; ordinary edits to
        # an existing judge (rename, mock provider, missing key) stay free.
        return []
    return _judge_issues(profile, relays)


def check_profile_delete(
    profiles: Sequence[ModelProfile],
    profile_id: str,
    judge_selector: str,
) -> list[RelayGuardIssue]:
    """Guard a deletion: only removing the judge the relays depend on is refused.

    Deleting a relay (or any non-judge profile) always relaxes or leaves
    the invariant alone; an already-broken selection elsewhere stays a
    matter for reload-time revalidation, not a reason to lock the delete
    button.
    """
    if not any(profile.is_relay for profile in profiles if profile.id != profile_id):
        return []
    judge = _resolve_in_set(profiles, judge_selector) if judge_selector.strip() else None
    if judge is not None and judge.id == profile_id:
        return [_issue("judge_delete_dependency", _JUDGE_DELETE_DEPENDENCY, name=_display_name(judge))]
    return []


def issue_text(issues: Sequence[RelayGuardIssue]) -> str:
    """Plain-English rendering for surfaces that do not localize (ACP)."""
    return " ".join(issue.text for issue in issues)
