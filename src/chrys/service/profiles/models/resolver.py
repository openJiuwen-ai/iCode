# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model profile resolution — pick the active profile for a given role.

Resolution lives here (rather than in ``ModelProfileRegistry``) so the
registry stays a pure data store.  Callers (``AgentEngine``, sub-agent
construction, the approval judge, the buddy's replies) ask the resolver
for the right ``ModelProfile`` to use; resolution policy can grow new
roles or override layers without touching the registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from chrys.service.profiles.models.schema import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    UNCONFIGURED_MODEL_ID,
    ModelProfile,
    is_model_profile_selectable,
)

if TYPE_CHECKING:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.settings_store import LoadedSettings
    from chrys.foundation.config.spec import Source
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry

_log = logging.getLogger(__name__)


# Hardcoded fallback used when no profile is registered or the active
# profile id can't be found.  ``model_id`` is deliberately set to a
# non-functional placeholder — first-run without configuration should
# fail loudly (the LLM API will reject "<Model Not Configured>") rather
# than silently picking an arbitrary model the user may not want.
_DEFAULT_PROFILE = ModelProfile(
    id="",
    name="Default",
    provider="openai",
    model_id=UNCONFIGURED_MODEL_ID,
    max_context_tokens=DEFAULT_MAX_CONTEXT_TOKENS,
    http_connect_timeout=10.0,
    http_read_timeout=600.0,
    http_max_retries=2,
    stream=False,
    vision=False,
)


@dataclass(frozen=True)
class ModelSelection:
    """A resolved model profile together with the policy branch that selected it."""

    profile: ModelProfile
    source: Literal["override", "agent", "inherited", "active", "default"]


def default_profile() -> ModelProfile:
    """Return a copy of the built-in fallback ``ModelProfile``.

    Returned as a fresh instance so callers can mutate without affecting
    the module-level singleton.
    """
    return replace(_DEFAULT_PROFILE)


def resolve_profile_selector(registry: ModelProfileRegistry | None, selector: str) -> ModelProfile | None:
    """Resolve a model profile selector by id or unique name."""
    if registry is None:
        return None
    profile = registry.get(selector)
    if profile is not None:
        return profile

    profiles = registry.list_profiles()
    exact_name_matches = [profile for profile in profiles if profile.name == selector]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    if len(exact_name_matches) > 1:
        _log.warning("Model profile name %r is ambiguous; use a profile id instead", selector)
        return None

    folded = selector.casefold()
    folded_name_matches = [profile for profile in profiles if profile.name.casefold() == folded]
    if len(folded_name_matches) == 1:
        return folded_name_matches[0]
    if len(folded_name_matches) > 1:
        _log.warning("Model profile name %r is ambiguous ignoring case; use a profile id instead", selector)
    return None


def resolve_selectable_profile(registry: ModelProfileRegistry | None, selector: str) -> ModelProfile | None:
    """Resolve a selector to a structurally selectable profile, or ``None``.

    A pointer that resolves to a stored-but-hollow profile (blank
    ``model_id`` — e.g. an auto-seeded "Profile 1" the user never filled
    in) is as unusable as a dangling one, so both map to ``None``.
    """
    profile = resolve_profile_selector(registry, selector)
    if profile is None or not is_model_profile_selectable(profile):
        return None
    return profile


def format_available_profile_labels(registry: ModelProfileRegistry) -> str:
    """Return a compact human-facing list of available model profiles."""
    profiles = sorted(registry.list_profiles(), key=lambda profile: profile.name.casefold())
    return ", ".join(f"{profile.name} ({profile.id})" for profile in profiles)


def settings_with_active_model_profile(settings: Settings, profile: ModelProfile) -> Settings:
    """Return ``settings`` with the active fallback model profile set to ``profile``."""
    return replace(settings, **active_model_profile_fields(profile))


def loaded_with_active_model_profile(loaded: LoadedSettings, profile: ModelProfile, layer: Source) -> LoadedSettings:
    """Same selection, credited to the layer that made it.

    ``--model`` is a command-line argument and a mid-session switch is not, so
    the panel has to be able to tell them apart; both used to arrive as an
    anonymous ``replace()`` and read back as whatever the file layers had said.
    """
    return loaded.overlay(layer, **active_model_profile_fields(profile))


def active_model_profile_fields(profile: ModelProfile) -> dict[str, Any]:
    """The fields selecting *profile* as the active fallback, in one place.

    The whole selection, never ``model_profile`` alone: a pin left in
    ``model_profile_override`` would outrank it. A session's own model (the
    settings handle's session layer) is these fields too.
    """
    return {
        "model_profile": profile.id,
        "model_profile_override": "",
        "model_profile_override_sub_agents": False,
    }


def _resolve_active_selection(
    registry: ModelProfileRegistry | None,
    settings: Settings,
) -> ModelSelection:
    if registry is None or not settings.model_profile:
        return ModelSelection(default_profile(), "default")
    profile = resolve_profile_selector(registry, settings.model_profile)
    if profile is None:
        _log.warning(
            "Active model profile %r not found in registry by id or name, falling back to default",
            settings.model_profile,
        )
        return ModelSelection(default_profile(), "default")
    if not is_model_profile_selectable(profile):
        # A stored-but-hollow profile (e.g. an auto-seeded "Profile 1" that
        # was never filled in) must not become the confirmed runtime model:
        # its blank model_id would slip past the send guard whenever some
        # other selectable profile exists.
        _log.warning(
            "Active model profile %r is not selectable (incomplete fields), falling back to default",
            settings.model_profile,
        )
        return ModelSelection(default_profile(), "default")
    return ModelSelection(profile, "active")


def resolve_active_profile(
    registry: ModelProfileRegistry | None,
    settings: Settings,
) -> ModelProfile:
    """Resolve the main agent's active ``ModelProfile``.

    Looks up ``settings.model_profile`` (the active profile id or unique name) in the
    registry.  Falls back to :func:`default_profile` if the registry is
    missing, the id is empty, or the id is not registered.
    """
    return _resolve_active_selection(registry, settings).profile


def resolve_judge_profile(
    registry: ModelProfileRegistry | None,
    settings: Settings,
    fallback: ModelProfile,
) -> ModelProfile:
    """Resolve the approval judge's ``ModelProfile``.

    Preference order:
      1. ``settings.approval_judge_model_profile``
         (env: ``CHRYS_MODEL_PROFILE_APPROVAL_JUDGE``), matched by id or
         unique name — the same selector syntax every side-call profile
         env var accepts.
      2. ``fallback`` (typically the main agent's active profile)
    """
    selector = settings.approval_judge_model_profile
    if not selector or registry is None:
        return fallback
    profile = resolve_profile_selector(registry, selector)
    if profile is None:
        _log.warning(
            "Approval judge model profile %r not found in registry, using main agent profile",
            selector,
        )
        return fallback
    return profile


def resolve_session_title_profile(
    registry: ModelProfileRegistry | None,
    settings: Settings,
    fallback: ModelProfile,
) -> ModelProfile:
    """Resolve the session-title summarizer's ``ModelProfile``.

    Preference order:
      1. ``settings.session_title_model_profile``
         (env: ``CHRYS_MODEL_PROFILE_SESSION_TITLE``), matched by id or
         unique name.
      2. ``fallback`` (typically the main agent's active profile).
    """
    selector = settings.session_title_model_profile
    if not selector or registry is None:
        return fallback
    profile = resolve_profile_selector(registry, selector)
    if profile is None:
        _log.warning(
            "Session title model profile %r not found in registry by id or name, using the active profile",
            selector,
        )
        return fallback
    return profile


def resolve_selection_for_agent(
    registry: ModelProfileRegistry | None,
    settings: Settings,
    agent_profile: AgentProfile,
    *,
    fallback: ModelProfile | None = None,
) -> ModelSelection:
    """Resolve a model profile and its selection source for an agent.

    Precedence (first match wins):

    1. ``settings.model_profile_override`` if non-empty and either resolving
       the main agent or ``settings.model_profile_override_sub_agents`` is true.
       This id/name selector is reserved for explicit per-session overrides
       such as ACP session model switches.
    2. ``agent_profile.model.profile_id`` if non-empty **and** the id is
       registered.  A set-but-missing id (e.g. the referenced profile was
       deleted) logs a warning and falls through — never raises.
    3. ``fallback`` if provided (sub-agents pass the parent's resolved
       profile here so they transparently inherit).
    4. :func:`resolve_active_profile` — the session-active profile from
       ``settings.model_profile``.
    """
    if settings.model_profile_override and (fallback is None or settings.model_profile_override_sub_agents):
        profile = resolve_profile_selector(registry, settings.model_profile_override)
        if profile is not None:
            return ModelSelection(profile, "override")
        _log.warning(
            "Explicit model profile override %r not found in registry by id or name, falling back to default",
            settings.model_profile_override,
        )
        return ModelSelection(default_profile(), "override")
    pid = agent_profile.model.profile_id if agent_profile.model else ""
    if pid and registry is not None:
        profile = registry.get(pid)
        if profile is not None:
            return ModelSelection(profile, "agent")
        _log.warning(
            "Agent %r references model profile id %r which is not registered; falling back to the active profile.",
            agent_profile.name,
            pid,
        )
    if fallback is not None:
        return ModelSelection(fallback, "inherited")
    return _resolve_active_selection(registry, settings)


def model_for_agent(
    registry: ModelProfileRegistry | None,
    settings: Settings,
    agent_profile: AgentProfile,
    *,
    session_model: str | None = None,
) -> str:
    """The id of the model a build of *agent_profile* runs, the session's own choice being *session_model*.

    *session_model* None keeps the choice *settings* carry. The one answer to
    "which model would this agent run": it is :func:`resolve_selection_for_agent`,
    which every build resolves with, so an override or a model the agent binds
    runs over the session's choice here as it does there. A model asked for the
    session takes effect on that agent only where this returns it. What a
    session runs now is the runtime's word, not this: no build is made.
    """
    if session_model is not None:
        settings = replace(settings, model_profile=session_model)
    return resolve_selection_for_agent(registry, settings, agent_profile).profile.id


def resolve_for_agent(
    registry: ModelProfileRegistry | None,
    settings: Settings,
    agent_profile: AgentProfile,
    *,
    fallback: ModelProfile | None = None,
) -> ModelProfile:
    """Resolve the ``ModelProfile`` for a given ``AgentProfile``."""
    return resolve_selection_for_agent(registry, settings, agent_profile, fallback=fallback).profile
