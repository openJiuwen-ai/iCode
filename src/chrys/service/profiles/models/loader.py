# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model profile loader — load model profile definitions from YAML files and directories."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import yaml

from chrys.foundation.text.encoding import decode_bytes
from chrys.service.profiles.coercion import coerce_bool
from chrys.service.profiles.models.options import OUTPUT_CAP_OPTION_ALIASES, output_cap_option_value
from chrys.service.profiles.models.schema import (
    API_STYLE_CHAT_COMPLETIONS,
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    VALID_API_STYLES,
    VALID_THINKING_BLOCK_BINDINGS,
    ApiStyle,
    ModelProfile,
    ThinkingBlockBinding,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class ModelProfileLoadError(Exception):
    """Raised when a model profile YAML file cannot be loaded or validated."""


def _coerce_int(data: dict[str, object], field: str, default: int, path: Path) -> int:
    """Coerce an integer field and report profile-local validation errors.

    Bools are rejected explicitly: YAML ``true`` would otherwise coerce to
    ``1`` and silently become a nonsense numeric setting (e.g. a 1-token
    output cap).
    """
    value: Any = data.get(field, default)
    if isinstance(value, bool):
        msg = f"Model profile field '{field}' must be an integer in {path}, got {value!r}"
        raise ModelProfileLoadError(msg)
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        msg = f"Model profile field '{field}' must be an integer in {path}, got {value!r}"
        raise ModelProfileLoadError(msg) from e


def _coerce_float(data: dict[str, object], field: str, default: float, path: Path) -> float:
    """Coerce a float field and report profile-local validation errors.

    Bools are rejected explicitly, same as :func:`_coerce_int`.
    """
    value: Any = data.get(field, default)
    if isinstance(value, bool):
        msg = f"Model profile field '{field}' must be a number in {path}, got {value!r}"
        raise ModelProfileLoadError(msg)
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        msg = f"Model profile field '{field}' must be a number in {path}, got {value!r}"
        raise ModelProfileLoadError(msg) from e


def _coerce_api_style(data: dict[str, object], path: Path) -> ApiStyle:
    """Return a supported OpenAI-compatible API style, falling back on malformed input."""
    raw = data.get("api_style", API_STYLE_CHAT_COMPLETIONS)
    if isinstance(raw, str):
        value = raw.strip()
        if value in VALID_API_STYLES:
            return cast("ApiStyle", value)
    logger.warning(
        "Model profile field 'api_style' must be one of %s in %s, got %r; using %r",
        ", ".join(sorted(VALID_API_STYLES)),
        path,
        raw,
        API_STYLE_CHAT_COMPLETIONS,
    )
    return API_STYLE_CHAT_COMPLETIONS


def _coerce_thinking_block_binding(data: dict[str, object], path: Path) -> ThinkingBlockBinding:
    value = data.get("thinking_block_binding", "auto")
    if value is False:
        # YAML reads a bare ``off`` as false.
        return "off"
    if not isinstance(value, str) or value not in VALID_THINKING_BLOCK_BINDINGS:
        msg = (
            f"Model profile field 'thinking_block_binding' must be one of "
            f"{', '.join(sorted(VALID_THINKING_BLOCK_BINDINGS))} in {path}, got {value!r}"
        )
        raise ModelProfileLoadError(msg)
    return cast("ThinkingBlockBinding", value)


def _migrate_chat_options_output_cap(data: dict[str, object], path: Path) -> tuple[Any, int]:
    """Legacy migration: move chat-options output-cap spellings into ``max_output_tokens``.

    ``max_output_tokens`` is the profile's one output-cap field; chat options
    historically spelled it as canonical ``max_tokens`` or a provider-native
    alias (``OUTPUT_CAP_OPTION_ALIASES``).  ``effective_chat_options``
    re-applies the field as the ``max_tokens`` default for live calls, so
    migrated profiles send byte-identical requests.  Returns the (possibly
    rewritten) ``chat_options`` value and the effective ``max_output_tokens``.
    The returned cap is always positive: an explicit positive field or a
    migrated legacy alias sticks; anything else — field absent, or an
    invalid non-positive value with nothing to migrate — gets the default
    cap with a warning.  Untouched chat-options cases: missing/blank/
    malformed ``chat_options``, or alias values that are not positive
    integers (left in place for the provider to reject).  On conflict — an
    explicit positive ``max_output_tokens`` field, or several differing
    aliases — the highest-priority value wins and the rest are dropped with
    a warning.  A non-positive field value does not win conflicts.
    """
    max_output_tokens = _coerce_int(data, "max_output_tokens", 0, path)

    raw = data.get("chat_options", "")
    opts: dict | None = None
    if isinstance(raw, str):
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                opts = parsed
    elif raw is not None:
        # The runtime parser ignores non-string chat_options with a generic
        # warning; flag it here too so the log names the offending file.
        logger.warning(
            "Model profile field 'chat_options' must be a JSON string in %s, got %s; it will be ignored",
            path,
            type(raw).__name__,
        )

    migrated = False
    if opts is not None:
        for alias in OUTPUT_CAP_OPTION_ALIASES:
            value = output_cap_option_value(opts, alias)
            if value is None:
                continue
            del opts[alias]
            migrated = True
            if max_output_tokens > 0 and max_output_tokens != value:
                logger.warning(
                    "Model profile in %s sets max_output_tokens (%d) and chat_options %s (%d); "
                    "keeping max_output_tokens and dropping the chat_options value",
                    path,
                    max_output_tokens,
                    alias,
                    value,
                )
            elif max_output_tokens <= 0:
                max_output_tokens = value
                logger.info(
                    "Model profile in %s: migrated chat_options %s (%d) to max_output_tokens",
                    path,
                    alias,
                    value,
                )

    if max_output_tokens <= 0:
        if "max_output_tokens" in data:
            logger.warning(
                "Model profile field 'max_output_tokens' must be a positive integer in %s, got %r; using %d",
                path,
                data["max_output_tokens"],
                DEFAULT_MAX_OUTPUT_TOKENS,
            )
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    if not migrated:
        return raw, max_output_tokens
    return json.dumps(opts, ensure_ascii=False) if opts else "", max_output_tokens


def load_profile_from_yaml(path: Path) -> ModelProfile:
    """Load a model profile from a YAML file.

    The ``id`` field defaults to the filename stem (expected to be a UUID)
    if not present in the YAML.

    Raises:
        ModelProfileLoadError: If the file cannot be read or is missing required fields.
    """
    try:
        text = decode_bytes(path.read_bytes(), errors="strict")
    except FileNotFoundError as e:
        raise ModelProfileLoadError(f"Model profile file not found: {path}") from e
    except (OSError, UnicodeError) as e:
        raise ModelProfileLoadError(f"Cannot read model profile file {path}: {e}") from e

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ModelProfileLoadError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(data, dict):
        raise ModelProfileLoadError(f"Model profile YAML must be a mapping, got {type(data).__name__} in {path}")

    if "name" not in data:
        raise ModelProfileLoadError(f"Model profile YAML missing required 'name' field in {path}")

    from chrys.service.context.compaction.budgets import MIN_DERIVABLE_CONTEXT_TOKENS

    profile_id = data.get("id", path.stem)
    chat_options, max_output_tokens = _migrate_chat_options_output_cap(data, path)
    max_context_tokens = _coerce_int(
        data,
        "max_context_tokens",
        DEFAULT_MAX_CONTEXT_TOKENS,
        path,
    )
    if max_context_tokens < MIN_DERIVABLE_CONTEXT_TOKENS:
        logger.warning(
            "Model profile field 'max_context_tokens' must be at least %d in %s, got %r; using %d",
            MIN_DERIVABLE_CONTEXT_TOKENS,
            path,
            data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS),
            DEFAULT_MAX_CONTEXT_TOKENS,
        )
        max_context_tokens = DEFAULT_MAX_CONTEXT_TOKENS

    try:
        return ModelProfile(
            id=profile_id,
            name=data["name"],
            provider=data.get("provider", "openai"),
            api_style=_coerce_api_style(data, path),
            model_id=data.get("model_id", ""),
            max_context_tokens=max_context_tokens,
            max_output_tokens=max_output_tokens,
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            http_connect_timeout=_coerce_float(data, "http_connect_timeout", 10.0, path),
            http_read_timeout=_coerce_float(data, "http_read_timeout", 300.0, path),
            http_max_retries=_coerce_int(data, "http_max_retries", 2, path),
            verify_ssl=coerce_bool(data.get("verify_ssl"), default=True),
            bypass_proxy=coerce_bool(data.get("bypass_proxy"), default=False),
            http_headers=data.get("http_headers", ""),
            chat_options=chat_options,
            stream=coerce_bool(data.get("stream"), default=True),
            vision=coerce_bool(data.get("vision"), default=False),
            stream_requires_finish_reason=coerce_bool(data.get("stream_requires_finish_reason"), default=False),
            thinking_block_binding=_coerce_thinking_block_binding(data, path),
            auto_interleaved_thinking=coerce_bool(data.get("auto_interleaved_thinking"), default=True),
        )
    except ModelProfileLoadError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise ModelProfileLoadError(f"Invalid model profile structure in {path}: {e}") from e


def load_profiles_from_dir(directory: Path) -> list[ModelProfile]:
    """Load all model profile YAML files from a directory.

    Non-YAML files and files that fail to parse are skipped.
    """
    if not directory.is_dir():
        return []

    profiles: list[ModelProfile] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            profiles.append(load_profile_from_yaml(path))
        except ModelProfileLoadError as exc:
            logger.warning("Skipping invalid model profile %s: %s", path, exc)
            continue
    for path in sorted(directory.glob("*.yml")):
        try:
            profiles.append(load_profile_from_yaml(path))
        except ModelProfileLoadError as exc:
            logger.warning("Skipping invalid model profile %s: %s", path, exc)
            continue

    return profiles
