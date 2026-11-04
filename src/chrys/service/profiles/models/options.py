# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared parsers for ``ModelProfile`` JSON-string fields.

``ModelProfile.chat_options`` and ``ModelProfile.http_headers`` are
stored as JSON strings (so the YAML form stays human-friendly).  Five
call sites historically each re-implemented the same try/except parse;
this module centralises that so future option layering (e.g. merging a
per-agent override on top of the profile) has one entry point.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from chrys.foundation.util.chrys_headers import is_chrys_managed_header_name
from chrys.foundation.util.env_templates import resolve_env_template_mapping, resolve_env_template_values
from chrys.kernel.client import OUTPUT_CAP_OPTION_ALIASES
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES, uses_responses_wire_dialect

if TYPE_CHECKING:
    from chrys.service.profiles.models.schema import ModelProfile

_log = logging.getLogger(__name__)
_FORCED_STATELESS_STORE_WARNINGS: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class ProtectedChatOptionsWarning:
    """Protected option paths plus their byte-stable legacy warning."""

    keys: tuple[str, ...]

    @property
    def message(self) -> str:
        """Return the compatibility warning rendered from the protected paths."""
        return (
            f"Protected chat option key(s) were stripped: {', '.join(self.keys)}. "
            "These keys can no longer be configured in profile YAML because they bypass context admission. "
            "Reusable prompts and profile-pinned continuation IDs are unavailable there; store: true only enables "
            "automatically managed Responses continuation."
        )


# The Chat Completions option that carries ``ModelProfile.stream_requires_finish_reason``
# to the client; it is never sent.
STREAM_REQUIRES_FINISH_REASON_OPTION = "stream_requires_finish_reason"
# The Responses option that routes OpenAI's prompt cache; a client sets one
# itself unless the options do (a null: none).
PROMPT_CACHE_KEY_OPTION = "prompt_cache_key"
# The Anthropic options that carry ``ModelProfile.thinking_block_binding`` and
# ``ModelProfile.auto_interleaved_thinking`` to the client when they are not
# the defaults; neither is sent.
THINKING_BLOCK_BINDING_OPTION = "thinking_block_binding"
AUTO_INTERLEAVED_THINKING_OPTION = "auto_interleaved_thinking"
_CHAT_COMPLETIONS_PROVIDERS = frozenset({"openai", "deepseek-openai", "glm-openai"})

PROTECTED_TOP_LEVEL_CHAT_OPTION_KEYS = frozenset(
    {
        "messages",
        "prompt",
        "conversation_id",
        "previous_response_id",
        "conversation",
        "continuation_token",
    }
)
PROTECTED_EXTRA_BODY_CHAT_OPTION_KEYS = frozenset(
    {
        *PROTECTED_TOP_LEVEL_CHAT_OPTION_KEYS,
        "input",
        "tools",
        "system",
        "instructions",
        *OUTPUT_CAP_OPTION_ALIASES,
    }
)


def _protected_chat_option_paths(options: Mapping[str, Any]) -> list[str]:
    paths = sorted(PROTECTED_TOP_LEVEL_CHAT_OPTION_KEYS & options.keys())
    extra_body = options.get("extra_body")
    if isinstance(extra_body, Mapping):
        paths.extend(f"extra_body.{key}" for key in sorted(PROTECTED_EXTRA_BODY_CHAT_OPTION_KEYS & extra_body.keys()))
    return paths


def _drop_protected_chat_options(profile_name: str, options: dict[str, Any]) -> dict[str, Any]:
    protected = _protected_chat_option_paths(options)
    if not protected:
        return options
    sanitized = dict(options)
    for key in PROTECTED_TOP_LEVEL_CHAT_OPTION_KEYS:
        sanitized.pop(key, None)
    extra_body = sanitized.get("extra_body")
    if isinstance(extra_body, Mapping):
        cleaned_extra_body = dict(extra_body)
        for key in PROTECTED_EXTRA_BODY_CHAT_OPTION_KEYS:
            cleaned_extra_body.pop(key, None)
        sanitized["extra_body"] = cleaned_extra_body
    for path in protected:
        _log.warning("ModelProfile %r chat_options key %r is protected and will be ignored", profile_name, path)
    return sanitized


def _normalize_instructions_option(profile_name: str, options: dict[str, Any]) -> dict[str, Any]:
    """Join a list-of-strings ``instructions`` one item per line; drop any other non-string.

    The kernel concatenates instructions as text, so any other value would
    reach the prompt as its Python repr.
    """
    value = options.get("instructions")
    if value is None or isinstance(value, str):
        return options
    sanitized = dict(options)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        sanitized["instructions"] = "\n".join(cast("list[str]", value))
        return sanitized
    _log.warning(
        "ModelProfile %r chat_options key 'instructions' must be a string or a list of strings; ignoring %s",
        profile_name,
        type(value).__name__,
    )
    del sanitized["instructions"]
    return sanitized


def _managed_header_names(headers: dict[Any, Any]) -> list[str]:
    return sorted((str(k) for k in headers if is_chrys_managed_header_name(str(k))), key=str.casefold)


def _log_managed_headers(profile_name: str, field: str, names: list[str]) -> None:
    if names:
        _log.warning(
            "ModelProfile %r %s contains Chrys-managed header(s), ignoring: %s",
            profile_name,
            field,
            ", ".join(names),
        )


def _drop_managed_extra_headers(profile_name: str, opts: dict[str, Any]) -> dict[str, Any]:
    """Keep per-request extra headers from overriding Chrys-managed metadata."""
    extra_headers = opts.get("extra_headers")
    if not isinstance(extra_headers, dict):
        return opts

    managed = _managed_header_names(extra_headers)
    if not managed:
        return opts
    _log_managed_headers(profile_name, "chat_options.extra_headers", managed)

    cleaned = {k: v for k, v in extra_headers.items() if not is_chrys_managed_header_name(str(k))}
    sanitized = dict(opts)
    if cleaned:
        sanitized["extra_headers"] = cleaned
    else:
        sanitized.pop("extra_headers", None)
    return sanitized


def parse_chat_options(profile: ModelProfile) -> dict[str, Any] | None:
    """Parse ``profile.chat_options`` JSON into an ``agent.run(options=...)`` dict.

    Returns ``None`` when the field is empty or not a valid JSON object.
    Logs a warning on JSON decode errors so misconfigured profiles
    surface to the user without crashing the run.
    """
    raw = profile.chat_options
    if not raw:
        return None
    try:
        opts = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        _log.warning("ModelProfile %r chat_options is not valid JSON, ignoring it", profile.name)
        return None
    if not isinstance(opts, dict):
        _log.warning("ModelProfile %r chat_options is not a JSON object, ignoring it", profile.name)
        return None
    opts = _drop_protected_chat_options(profile.name, opts)
    opts = _normalize_instructions_option(profile.name, opts)
    opts = _drop_managed_extra_headers(profile.name, opts)
    # Only a header is swapped for its token at send time; the rest goes out as the JSON body.
    return {
        key: resolve_env_template_values(
            value,
            location=f"model profile {profile.name!r} chat option[{key!r}]",
            allow_commands=key == "extra_headers",
        )
        for key, value in opts.items()
    }


def output_cap_option_value(options: Mapping[str, Any] | None, key: str) -> int | None:
    """Return ``options[key]`` when it is a usable output cap (positive int)."""
    value = None if options is None else options.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def effective_store_option(options: Mapping[str, Any] | None) -> Any:
    """Return the top-level ``store`` value with the ``extra_body`` veto applied.

    A present ``extra_body.store`` overrides the named parameter on the wire
    (request-body merge is extra_body-last), so an explicit false there means
    the service will NOT store the conversation — consumers reading the
    top-level value must see that veto or they record service handles the
    service never persisted.  The reverse never applies: a truthy
    ``extra_body.store`` does not promote the top-level value, because
    client-side history over a storing wire is a supported degenerate mode
    and must not silently enable service-side features.  A present ``None``
    is not a veto either: it reaches the wire as JSON null and selects the
    provider's storage default.
    """
    if options is None:
        return None
    extra = options.get("extra_body")
    if isinstance(extra, Mapping) and "store" in extra:
        value = extra["store"]
        if value is not None and not value:
            return False
    return options.get("store")


def effective_chat_options(profile: ModelProfile) -> dict[str, Any] | None:
    """Return chat options after applying Chrys provider and dialect defaults.

    ``ModelProfile.max_output_tokens`` is applied as the ``max_tokens``
    default: the loader migrates legacy chat-options output-cap spellings
    into that field, and re-applying it here keeps migrated profiles sending
    byte-identical live requests.  Any output-cap spelling already present
    in the parsed options (programmatic callers) wins — injecting canonical
    ``max_tokens`` next to a provider-native alias would silently override
    it at serialization time.
    """
    opts = parse_chat_options(profile)
    # ``effective is opts`` marks the not-yet-copied state; the first default
    # that applies copies once, later ones mutate in place.  Returns None
    # (not ``{}``) when no options and no defaults apply.
    effective = opts
    if uses_responses_wire_dialect(profile):
        effective = dict(opts or {})
        if profile.provider == "deepseek-openai":
            extra_body = effective.get("extra_body")
            declared_store = "store" in effective or (isinstance(extra_body, Mapping) and "store" in extra_body)
            store_values = [effective["store"]] if "store" in effective else []
            if isinstance(extra_body, Mapping) and "store" in extra_body:
                store_values.append(extra_body["store"])
            warning_key = (profile.id, profile.chat_options)
            if (
                declared_store
                and any(value is not False for value in store_values)
                and warning_key not in _FORCED_STATELESS_STORE_WARNINGS
            ):
                _FORCED_STATELESS_STORE_WARNINGS.add(warning_key)
                _log.warning(
                    "ModelProfile %r uses stateless DeepSeek Responses; store options are forced to false",
                    profile.name,
                )
            effective["store"] = False
            if isinstance(extra_body, Mapping) and "store" in extra_body:
                effective["extra_body"] = {**extra_body, "store": False}
        elif effective.get("store") is None:
            effective["store"] = False
        elif effective["store"] and effective_store_option(effective) is False:
            # Mirror the wire's extra_body veto on the top-level value so
            # every downstream reader of the effective options (continuation
            # gate, conversation-id learning, session persistence) agrees
            # with what the service actually stores.
            effective["store"] = False
        extra_body = effective.get("extra_body")
        if (
            PROMPT_CACHE_KEY_OPTION in effective
            and effective[PROMPT_CACHE_KEY_OPTION] is None
            and (extra_body is None or isinstance(extra_body, Mapping))
        ):
            # A null asks for no prompt cache key, but the agent drops options
            # set to null, which would leave the client choosing one: the null
            # rides in extra_body, which keeps it.
            del effective[PROMPT_CACHE_KEY_OPTION]
            effective["extra_body"] = {**(extra_body or {}), PROMPT_CACHE_KEY_OPTION: None}
    if profile.max_output_tokens > 0 and (
        effective is None or all(effective.get(alias) is None for alias in OUTPUT_CAP_OPTION_ALIASES)
    ):
        if effective is None or effective is opts:
            effective = dict(opts or {})
        effective["max_tokens"] = profile.max_output_tokens
    if (
        profile.stream_requires_finish_reason
        and profile.provider in _CHAT_COMPLETIONS_PROVIDERS
        and not uses_responses_wire_dialect(profile)
    ):
        if effective is None or effective is opts:
            effective = dict(opts or {})
        effective[STREAM_REQUIRES_FINISH_REASON_OPTION] = True
    if profile.provider == "anthropic":
        if profile.thinking_block_binding != "auto":
            if effective is None or effective is opts:
                effective = dict(opts or {})
            effective[THINKING_BLOCK_BINDING_OPTION] = profile.thinking_block_binding
        if not profile.auto_interleaved_thinking:
            if effective is None or effective is opts:
                effective = dict(opts or {})
            effective[AUTO_INTERLEAVED_THINKING_OPTION] = False
    return effective


def uses_responses_compact_continuation(profile: ModelProfile, chat_options: dict[str, Any] | None) -> bool:
    """Return True when Responses should use compact service-side continuations."""
    return (
        profile.provider == "openai"
        and profile.api_style == API_STYLE_RESPONSES
        and chat_options is not None
        and effective_store_option(chat_options) is True
    )


def responses_store_continuation_warning(profile: ModelProfile) -> str | None:
    """Return a user-facing warning when the profile opts into Responses store mode.

    Store-mode continuation sends only per-call deltas (the service holds the
    conversation), so client-side context compaction never sees the real
    context size and cannot trigger.  Surfaced wherever a profile is saved or
    activated, until a store-aware compaction design exists.

    Deliberately checks the raw parsed options rather than
    ``effective_chat_options``: env-template resolution may legitimately fail
    in the saving process's environment (templates resolve at run time), and a
    save-time warning must never raise.  ``store`` only enables the mode as
    the JSON literal ``true`` either way — template resolution yields strings,
    never bools — so this matches the engine's post-resolution judgment.
    """
    raw = profile.chat_options
    if not raw:
        return None
    try:
        opts = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return None
    if not isinstance(opts, dict) or not uses_responses_compact_continuation(profile, opts):
        return None
    return (
        'chat_options {"store": true} enables service-side continuation. '
        "Context compaction is not supported in this mode yet: long "
        "sessions will not compact and may overflow the context window."
    )


# Anthropic automatic prompt caching: one breakpoint the service places on the
# last cacheable block of each request.
ANTHROPIC_PROMPT_CACHE_CONTROL: Mapping[str, str] = {"type": "ephemeral"}


def is_anthropic_claude_profile(profile: ModelProfile) -> bool:
    """Return whether *profile* sends a Claude model over the Anthropic protocol."""
    return profile.provider == "anthropic" and "claude" in profile.model_id.casefold()


def lacks_anthropic_prompt_cache_option(profile: ModelProfile) -> bool:
    """Return whether a Claude profile on the Anthropic protocol never asks for prompt caching.

    Anthropic caches a prompt prefix only when the request carries
    ``cache_control``, and Chrys does not add one itself (some gateways
    reject the field), so such a profile pays full input price on every
    call.  Either spelling counts: the top-level option or the same key
    inside ``extra_body``.  Reads the raw options like the other save-time
    checks; a value this helper cannot merge into (options that are not a
    JSON object, an ``extra_body`` that is not one) returns False, since the
    one-click fix could not apply.
    """
    if not is_anthropic_claude_profile(profile):
        return False
    raw = profile.chat_options
    if not raw:
        return True
    try:
        options = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return False
    if not isinstance(options, dict) or "cache_control" in options:
        return False
    extra_body = options.get("extra_body")
    if extra_body is None:
        return True
    return isinstance(extra_body, Mapping) and "cache_control" not in extra_body


def with_anthropic_prompt_cache_option(extra_body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return *extra_body* plus Anthropic's automatic prompt-caching switch."""
    return {**(extra_body or {}), "cache_control": dict(ANTHROPIC_PROMPT_CACHE_CONTROL)}


def protected_chat_option_keys_warning_structured(profile: ModelProfile) -> ProtectedChatOptionsWarning | None:
    """Return protected option paths for a profile warning."""
    raw = profile.chat_options
    if not raw:
        return None
    try:
        options = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return None
    if not isinstance(options, dict):
        return None
    protected = _protected_chat_option_paths(options)
    if not protected:
        return None
    return ProtectedChatOptionsWarning(keys=tuple(protected))


def protected_chat_option_keys_warning(profile: ModelProfile) -> str | None:
    """Return a user-facing warning for profile options Chrys now strips."""
    warning = protected_chat_option_keys_warning_structured(profile)
    return warning.message if warning is not None else None


def parse_http_headers(profile: ModelProfile) -> dict[str, str]:
    """Parse ``profile.http_headers`` JSON into a header dict.

    Returns an empty dict when the field is empty, malformed, or not a
    JSON object.  Non-string values are coerced via ``str()`` and
    Chrys-managed header names are dropped so the caller always gets a
    clean ``dict[str, str]``.
    """
    raw = profile.http_headers
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        _log.warning("ModelProfile %r http_headers is not valid JSON, ignoring it", profile.name)
        return {}
    if not isinstance(data, dict):
        _log.warning("ModelProfile %r http_headers is not a JSON object, ignoring it", profile.name)
        return {}
    _log_managed_headers(profile.name, "http_headers", _managed_header_names(data))
    headers = {str(k): str(v) for k, v in data.items() if not is_chrys_managed_header_name(str(k))}
    return resolve_env_template_mapping(headers, location=f"model profile {profile.name!r} extra header")
