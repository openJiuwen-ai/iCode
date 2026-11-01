# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model configuration modal — manage model profiles with sidebar + form layout."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import re
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_locale_controller, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.formatting import with_count_hint
from chrys.app.tui.util.static_update import update_static_in_place
from chrys.app.tui.widgets import Checkbox, ConfigAddButton, Select
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.runtime_pointer import set_model_pointer
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.i18n import DisplayBlock, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.text.reading_order import reading_order
from chrys.foundation.util.chrys_headers import is_chrys_managed_header_name
from chrys.foundation.util.env_templates import blank_commands
from chrys.foundation.util.header_charset import (
    api_key_charset_error,
    header_name_charset_error,
    header_value_charset_error,
    model_id_charset_error,
)
from chrys.service.context.compaction.budgets import MIN_DERIVABLE_CONTEXT_TOKENS
from chrys.service.llm.providers import CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS, PROVIDERS
from chrys.service.profiles.models.options import (
    OUTPUT_CAP_OPTION_ALIASES,
    PROTECTED_EXTRA_BODY_CHAT_OPTION_KEYS,
    PROTECTED_TOP_LEVEL_CHAT_OPTION_KEYS,
    is_anthropic_claude_profile,
    lacks_anthropic_prompt_cache_option,
    with_anthropic_prompt_cache_option,
)
from chrys.service.profiles.models.schema import (
    API_STYLE_CHAT_COMPLETIONS,
    API_STYLE_RESPONSES,
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    is_model_profile_selectable,
    is_responses_wire_dialect,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.screens.dialogs.prompt_cache import PromptCacheDialogResult
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile

_READ_ONLY_NOTICE = msg(
    "tui.model_config.read_only_notice",
    fallback="• Agent is running. This page is read-only.",
)

_MODEL_SETTINGS_TITLE = msg("tui.model_config.title.settings", fallback="Model Settings")
_VALIDATION_ERROR_TITLE = msg("tui.model_config.title.validation_error", fallback="Validation Error")
_SAVE_ERROR_TITLE = msg("tui.model_config.title.save_error", fallback="Save Error")
_MODEL_PROFILE_SAVED = msg("tui.model_config.saved", fallback="Model profile saved")
_CONFIGURATION_TITLE = msg("tui.model_config.title.configuration", fallback="Model Configuration")
_PROFILE_NAME = msg("tui.model_config.profile_name", fallback="Profile Name")
_PROFILE_NAME_PLACEHOLDER = msg(
    "tui.model_config.placeholder.profile_name",
    fallback="e.g. My Model Config",
)
_MODEL_OPTIONS = msg("tui.model_config.model_options", fallback="Model Options")
_PROVIDER = msg("tui.model_config.provider", fallback="Provider")
_API_STYLE = msg("tui.model_config.api_style", fallback="API Style")
_MODEL = msg("tui.model_config.model", fallback="Model")
_MODEL_PLACEHOLDER = msg(
    "tui.model_config.placeholder.model",
    fallback="Provider-specific model id (required)",
)
_MAX_CONTEXT_WINDOW = msg("tui.model_config.max_context_window", fallback="Max Context Window")
_MAX_OUTPUT_TOKENS = msg(
    "tui.model_config.max_output_tokens",
    fallback="Max Output Tokens ({param})",
)
_OUTPUT_CAP_PLACEHOLDER = msg(
    "tui.model_config.placeholder.output_cap",
    fallback="Model output cap (default {default})",
)
_BASE_URL = msg("tui.model_config.base_url", fallback="Base URL")
_API_KEY = msg("tui.model_config.api_key", fallback="API Key")
_STREAMING = msg("tui.model_config.streaming", fallback="Streaming")
_VISION = msg("tui.model_config.vision", fallback="Vision Model")
_CONNECTION_OPTIONS = msg("tui.model_config.connection_options", fallback="Connection Options")
_SKIP_TLS_VERIFICATION = msg("tui.model_config.skip_tls_verification", fallback="Skip TLS verification")
_INSECURE_TLS = msg(
    "tui.model_config.insecure_tls",
    fallback="Insecure: disables HTTPS certificate validation. Use only for trusted endpoints.",
)
_BYPASS_PROXY = msg("tui.model_config.bypass_proxy", fallback="Bypass proxy")
_HTTP_OPTIONS = msg("tui.model_config.http_options", fallback="HTTP Options")
_HTTP_CONNECT_TIMEOUT = msg("tui.model_config.http_connect_timeout", fallback="HTTP Connect Timeout (s)")
_HTTP_READ_TIMEOUT = msg("tui.model_config.http_read_timeout", fallback="HTTP Read Timeout (s)")
_HTTP_MAX_RETRIES = msg("tui.model_config.http_max_retries", fallback="HTTP Max Retries")
_EXTRA_OPTIONS = msg("tui.model_config.extra_options", fallback="Extra Options")
_HTTP_EXTRA_HEADERS = msg("tui.model_config.http_extra_headers", fallback="HTTP Extra Headers")
_CHAT_OPTIONS = msg("tui.model_config.chat_options", fallback="Chat Options")
_NEW = msg("tui.model_config.button.new", fallback="New")
_CLONE = msg("tui.model_config.button.clone", fallback="Clone")
_DELETE = msg("tui.model_config.button.delete", fallback="Delete")
_SAVE = msg("tui.model_config.button.save", fallback="Save")
_CLOSE = msg("tui.model_config.button.close", fallback="Close")
_ADD = msg("tui.model_config.button.add", fallback="+ Add")
_HEADER_NAME_PLACEHOLDER = msg(
    "tui.model_config.placeholder.header_name",
    fallback="e.g. X-Auth-Token",
)
_HEADER_VALUE_PLACEHOLDER = msg(
    "tui.model_config.placeholder.header_value",
    fallback="e.g. {{{{AUTH_TOKEN}}}}",
)
_OPTION_NAME_PLACEHOLDER = msg(
    "tui.model_config.placeholder.option_name",
    fallback="e.g. extra_body, temperature",
)
_OPTION_VALUE_PLACEHOLDER = msg(
    "tui.model_config.placeholder.option_value",
    fallback="e.g. 0.7, true, or {{...}}",
)
_PROVIDER_MODEL = msg("tui.model_config.provider_model", fallback="{provider} Model")
_PROVIDER_BASE_URL = msg("tui.model_config.provider_base_url", fallback="{provider} Base URL")
_PROVIDER_API_KEY = msg("tui.model_config.provider_api_key", fallback="{provider} API Key")
_LEAVE_BLANK_FOR_DEFAULT = msg(
    "tui.model_config.placeholder.leave_blank_for_default",
    fallback="Leave blank for default",
)
_DELETE_PROFILE_TITLE = msg("tui.model_config.confirm_delete.title", fallback="Delete Model Profile")
_DELETE_PROFILE_MESSAGE = msg(
    "tui.model_config.confirm_delete.message",
    fallback='Delete model profile\n"{display}"?\n\nThis cannot be undone.',
    multiline=True,
)

# Validation prose.
_CHAT_OPTION_OBJECT_EXAMPLE = msg(
    "tui.model_config.validation.chat_option.object_example",
    fallback="Chat Options row {row}: '{key}' must be a valid JSON object/map, for example {example}.",
    multiline=True,
)
_CHAT_OPTION_OBJECT_TYPE = msg(
    "tui.model_config.validation.chat_option.object_type",
    fallback="Chat Options row {row}: '{key}' must be a JSON object/map, got {type_name}.",
    multiline=True,
)
_JSON_NUMBER = msg("tui.model_config.validation.clause.json_number", fallback="a JSON number")
_JSON_NUMBER_MIN = msg(
    "tui.model_config.validation.clause.json_number_min",
    fallback="a JSON number greater than or equal to {minimum}",
)
_JSON_NUMBER_MAX = msg(
    "tui.model_config.validation.clause.json_number_max",
    fallback="a JSON number less than or equal to {maximum}",
)
_JSON_NUMBER_RANGE = msg(
    "tui.model_config.validation.clause.json_number_range",
    fallback="a JSON number between {minimum} and {maximum}",
)
_JSON_INTEGER = msg("tui.model_config.validation.clause.json_integer", fallback="a JSON integer")
_JSON_POSITIVE_INTEGER = msg(
    "tui.model_config.validation.clause.json_positive_integer",
    fallback="a positive JSON integer",
)
_JSON_INTEGER_MIN = msg(
    "tui.model_config.validation.clause.json_integer_min",
    fallback="a JSON integer greater than or equal to {minimum}",
)
_JSON_INTEGER_MAX = msg(
    "tui.model_config.validation.clause.json_integer_max",
    fallback="a JSON integer less than or equal to {maximum}",
)
_JSON_INTEGER_RANGE = msg(
    "tui.model_config.validation.clause.json_integer_range",
    fallback="a JSON integer between {minimum} and {maximum}",
)
_CHAT_OPTION_MUST_BE = msg(
    "tui.model_config.validation.chat_option.must_be",
    fallback="Chat Options row {row}: '{key}' must be {clause}.",
    multiline=True,
)
_CHAT_OPTION_BOOLEAN = msg(
    "tui.model_config.validation.chat_option.boolean",
    fallback="Chat Options row {row}: '{key}' must be a JSON boolean (true or false).",
    multiline=True,
)
_CHAT_OPTION_STRING = msg(
    "tui.model_config.validation.chat_option.string",
    fallback="Chat Options row {row}: '{key}' must be a string.",
    multiline=True,
)
_CHAT_OPTION_DETAIL = msg(
    "tui.model_config.validation.chat_option.detail",
    fallback="Chat Options row {row}: '{key}': {detail}",
    multiline=True,
)
_CHAT_OPTION_STOP = msg(
    "tui.model_config.validation.chat_option.stop",
    fallback="Chat Options row {row}: 'stop' must be a string or a JSON array of strings.",
)
_CHAT_OPTION_LOGIT_BIAS = msg(
    "tui.model_config.validation.chat_option.logit_bias",
    fallback=(
        "Chat Options row {row}: 'logit_bias' value for token {token} must be a JSON number between -100 and 100."
    ),
    multiline=True,
)
_CHAT_OPTION_HEADER_RESERVED = msg(
    "tui.model_config.validation.chat_option.header_reserved",
    fallback="Chat Options row {row}: 'extra_headers' header {header} is reserved for {app}.",
    multiline=True,
)
_CHAT_OPTION_HEADER_VALUE_STRING = msg(
    "tui.model_config.validation.chat_option.header_value_string",
    fallback="Chat Options row {row}: 'extra_headers' value for header {header} must be a string.",
    multiline=True,
)
_CHAT_OPTION_OUTPUT_CAP = msg(
    "tui.model_config.validation.chat_option.output_cap",
    fallback=(
        "Chat Options row {row}: {key} is not saved on model profiles — remove this row and set the Max Output "
        "Tokens field above instead."
    ),
    multiline=True,
)
_CHAT_OPTION_PROTECTED = msg(
    "tui.model_config.validation.chat_option.protected",
    fallback="Chat Options row {row}: {key} is protected because it bypasses context admission.",
    multiline=True,
)
_CHAT_OPTION_NESTED_PROTECTED = msg(
    "tui.model_config.validation.chat_option.nested_protected",
    fallback="Chat Options row {row}: extra_body contains protected key(s): {keys}.",
    multiline=True,
)
_NAME_REQUIRED = msg("tui.model_config.validation.name_required", fallback="Name is required.")
_PROFILE_EXISTS = msg(
    "tui.model_config.validation.profile_exists",
    fallback="A model profile named '{name}' already exists (names are case-insensitive).",
    multiline=True,
)
_PROVIDER_ALLOWED = msg(
    "tui.model_config.validation.provider_allowed",
    fallback="Provider must be one of: {providers}.",
)
_MODEL_REQUIRED = msg("tui.model_config.validation.model_required", fallback="Model name is required.")
_FIELD_REQUIRED_EXAMPLE = msg(
    "tui.model_config.validation.field_required_example",
    fallback="{label} is required (e.g. {example}).",
)
_FIELD_VALID_INTEGER = msg(
    "tui.model_config.validation.field_valid_integer",
    fallback="{label} must be a valid integer.",
)
_FIELD_POSITIVE_INTEGER = msg(
    "tui.model_config.validation.field_positive_integer",
    fallback="{label} must be a positive integer.",
)
_MAX_CONTEXT_TOKENS_LABEL = msg(
    "tui.model_config.validation.label.max_context_tokens",
    fallback="Max context tokens",
)
_MAX_OUTPUT_TOKENS_LABEL = msg(
    "tui.model_config.validation.label.max_output_tokens",
    fallback="Max output tokens",
)
_MAX_CONTEXT_MINIMUM = msg(
    "tui.model_config.validation.max_context_minimum",
    fallback="Max context tokens must be at least {minimum}.",
)
_MAX_OUTPUT_LESS_THAN_CONTEXT = msg(
    "tui.model_config.validation.max_output_less_than_context",
    fallback="Max output tokens must be less than max context tokens.",
)
_BASE_URL_SCHEME = msg(
    "tui.model_config.validation.base_url_scheme",
    fallback="Base URL must start with http:// or https://.",
)
_HTTP_CONNECT_TIMEOUT_LABEL = msg(
    "tui.model_config.validation.label.http_connect_timeout",
    fallback="HTTP Connect Timeout",
)
_HTTP_READ_TIMEOUT_LABEL = msg(
    "tui.model_config.validation.label.http_read_timeout",
    fallback="HTTP Read Timeout",
)
_FIELD_POSITIVE_NUMBER = msg(
    "tui.model_config.validation.field_positive_number",
    fallback="{label} must be a positive number.",
)
_FIELD_VALID_NUMBER = msg(
    "tui.model_config.validation.field_valid_number",
    fallback="{label} must be a valid number.",
)
_HTTP_RETRIES_NON_NEGATIVE = msg(
    "tui.model_config.validation.http_retries_non_negative",
    fallback="HTTP Max Retries must be a non-negative integer.",
)
_HTTP_RETRIES_VALID = msg(
    "tui.model_config.validation.http_retries_valid",
    fallback="HTTP Max Retries must be a valid integer.",
)
_KV_VALUE_REQUIRED = msg(
    "tui.model_config.validation.kv_value_required",
    fallback="{label} row {row}: value is required for key '{key}'.",
    multiline=True,
)
_KV_KEY_REQUIRED = msg(
    "tui.model_config.validation.kv_key_required",
    fallback="{label} row {row}: key name is required when a value is set.",
)
_KV_HEADER_RESERVED = msg(
    "tui.model_config.validation.kv_header_reserved",
    fallback="{label} row {row}: header key '{key}' is reserved for {app}.",
    multiline=True,
)
_KV_DETAIL = msg(
    "tui.model_config.validation.kv_detail",
    fallback="{label} row {row}: {detail}",
    multiline=True,
)
_KV_DUPLICATE_KEY = msg(
    "tui.model_config.validation.kv_duplicate_key",
    fallback="{label} row {row}: duplicate key '{key}'.",
    multiline=True,
)
_RESPONSES_STORE_WARNING = msg(
    "tui.model_config.warning.responses_store_continuation",
    fallback=(
        'chat_options {{"store": true}} enables service-side continuation. Context compaction is not supported in '
        "this mode yet: long sessions will not compact and may overflow the context window."
    ),
)
_PROTECTED_CHAT_OPTIONS_WARNING = msg(
    "tui.model_config.warning.protected_chat_options",
    fallback=(
        "Protected chat option key(s) were stripped: {keys}. These keys can no longer be configured in profile YAML "
        "because they bypass context admission. Reusable prompts and profile-pinned continuation IDs are unavailable "
        "there; store: true only enables automatically managed Responses continuation."
    ),
    multiline=True,
)

# ── Provider helpers ─────────────────────────────────────────────────

_PROVIDERS = [
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("DeepSeek (OpenAI)", "deepseek-openai"),
    ("GLM (OpenAI)", "glm-openai"),
]
_API_STYLE_CHAT_COMPLETIONS = msg(
    "tui.model_config.api_style.chat_completions",
    fallback="Chat Completions",
)
_API_STYLE_RESPONSES = msg("tui.model_config.api_style.responses", fallback="Responses")
_API_STYLES = [
    (_API_STYLE_CHAT_COMPLETIONS, API_STYLE_CHAT_COMPLETIONS),
    (_API_STYLE_RESPONSES, API_STYLE_RESPONSES),
]

_PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "deepseek-openai": "DeepSeek (OpenAI)",
    "glm-openai": "GLM (OpenAI)",
}

_PROVIDER_DEFAULT_BASE_URLS: dict[str, str] = {name: spec.default_base_url for name, spec in PROVIDERS.items()}


def _token_limit_param_name(provider: str, api_style: str) -> str:
    """Wire parameter that carries the Max Output Tokens value for this selection.

    Chat-completions values come from the table the client variants take their
    ``max_output_param`` from, so the label names what the client sends without
    importing the SDK-backed client modules while the form renders.
    """
    if provider == "anthropic":
        return "max_tokens"
    if is_responses_wire_dialect(provider, api_style):
        return "max_output_tokens"
    return CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS.get(provider, CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS["openai"])


_JSON_OBJECT_CHAT_OPTION_KEYS = frozenset(
    {
        "extra_body",
        "extra_headers",
        "extra_query",
        "logit_bias",
        "metadata",
        "response_format",
    }
)
_FLOAT_CHAT_OPTION_RULES: dict[str, tuple[float | None, float | None]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}
_INT_CHAT_OPTION_RULES: dict[str, tuple[int | None, int | None]] = {
    "seed": (None, None),
}
_BOOL_CHAT_OPTION_KEYS = frozenset({"allow_multiple_tool_calls", "store"})
_STRING_CHAT_OPTION_KEYS = frozenset({"conversation_id", "instructions", "model", "user"})
_CHAT_OPTION_INVALID_JSON_VALUE = object()
type _RenderMessage = Callable[[MessageRef], str]

# ── UID counter for key-value rows ──────────────────────────────────

_uid_counter = 0


def _next_uid() -> int:
    global _uid_counter
    _uid_counter += 1
    return _uid_counter


def _loads_or(text: str, default: object) -> object:
    """Return ``json.loads(text)``, or *default* for any text JSON rejects.

    Catches ``ValueError``, not only ``JSONDecodeError``: an integer past
    Python's digit limit raises the plain base class.
    """
    try:
        return json.loads(text)
    except ValueError:
        return default


def _parse_chat_option_value_for_validation(raw: str) -> object:
    """Parse a Chat Options value for validation."""
    return _loads_or(raw, _CHAT_OPTION_INVALID_JSON_VALUE)


def _validate_chat_option_object(
    index: int,
    key: str,
    parsed: object,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    if parsed is _CHAT_OPTION_INVALID_JSON_VALUE:
        example = '{"key": "value"}'
        return [
            render_message(
                _CHAT_OPTION_OBJECT_EXAMPLE.bind(
                    row=index,
                    key=DisplayBlock(key),
                    example=DisplayBlock(example),
                )
            )
        ]
    if not isinstance(parsed, dict):
        return [
            render_message(
                _CHAT_OPTION_OBJECT_TYPE.bind(
                    row=index,
                    key=DisplayBlock(key),
                    type_name=type(parsed).__name__,
                )
            )
        ]
    return []


def _json_number_description(
    min_value: float | None,
    max_value: float | None,
    *,
    render_message: _RenderMessage = format_message,
) -> str:
    if min_value is not None and max_value is not None:
        return render_message(
            _JSON_NUMBER_RANGE.bind(
                minimum=f"{min_value:.1f}",
                maximum=f"{max_value:.1f}",
            )
        )
    if min_value is not None:
        return render_message(_JSON_NUMBER_MIN.bind(minimum=f"{min_value:.1f}"))
    if max_value is not None:
        return render_message(_JSON_NUMBER_MAX.bind(maximum=f"{max_value:.1f}"))
    return render_message(_JSON_NUMBER.bind())


def _json_integer_description(
    min_value: int | None,
    max_value: int | None,
    *,
    render_message: _RenderMessage = format_message,
) -> str:
    if min_value == 1 and max_value is None:
        return render_message(_JSON_POSITIVE_INTEGER.bind())
    if min_value is not None and max_value is not None:
        return render_message(_JSON_INTEGER_RANGE.bind(minimum=min_value, maximum=max_value))
    if min_value is not None:
        return render_message(_JSON_INTEGER_MIN.bind(minimum=min_value))
    if max_value is not None:
        return render_message(_JSON_INTEGER_MAX.bind(maximum=max_value))
    return render_message(_JSON_INTEGER.bind())


def _validate_chat_option_float(
    index: int,
    key: str,
    parsed: object,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    min_value, max_value = _FLOAT_CHAT_OPTION_RULES[key]
    clause = _json_number_description(min_value, max_value, render_message=render_message)
    if isinstance(parsed, bool) or not isinstance(parsed, int | float) or not math.isfinite(float(parsed)):
        return [
            render_message(
                _CHAT_OPTION_MUST_BE.bind(
                    row=index,
                    key=DisplayBlock(key),
                    clause=clause,
                )
            )
        ]

    value = float(parsed)
    if min_value is not None and value < min_value:
        return [render_message(_CHAT_OPTION_MUST_BE.bind(row=index, key=DisplayBlock(key), clause=clause))]
    if max_value is not None and value > max_value:
        return [render_message(_CHAT_OPTION_MUST_BE.bind(row=index, key=DisplayBlock(key), clause=clause))]
    return []


def _validate_chat_option_int(
    index: int,
    key: str,
    parsed: object,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    min_value, max_value = _INT_CHAT_OPTION_RULES[key]
    clause = _json_integer_description(min_value, max_value, render_message=render_message)
    if isinstance(parsed, bool) or not isinstance(parsed, int):
        return [render_message(_CHAT_OPTION_MUST_BE.bind(row=index, key=DisplayBlock(key), clause=clause))]

    if min_value is not None and parsed < min_value:
        return [render_message(_CHAT_OPTION_MUST_BE.bind(row=index, key=DisplayBlock(key), clause=clause))]
    if max_value is not None and parsed > max_value:
        return [render_message(_CHAT_OPTION_MUST_BE.bind(row=index, key=DisplayBlock(key), clause=clause))]
    return []


def _validate_chat_option_bool(
    index: int,
    key: str,
    parsed: object,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    if not isinstance(parsed, bool):
        return [render_message(_CHAT_OPTION_BOOLEAN.bind(row=index, key=DisplayBlock(key)))]
    return []


def _chat_option_string_value(parsed: object, raw: str) -> str | None:
    if parsed is _CHAT_OPTION_INVALID_JSON_VALUE:
        return raw
    if isinstance(parsed, str):
        return parsed
    return None


def _validate_chat_option_string(
    index: int,
    key: str,
    parsed: object,
    raw: str,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    value = _chat_option_string_value(parsed, raw)
    if value is None:
        return [render_message(_CHAT_OPTION_STRING.bind(row=index, key=DisplayBlock(key)))]
    if key == "model":
        # A model override replaces the profile's model id on the wire —
        # including the ``Chrys-Model-Id`` header — so it carries the
        # same charset constraint.  ``{{ENV_VAR}}`` templates are ASCII
        # and pass; resolved values are re-checked at request time.
        charset_error = model_id_charset_error(value)
        if charset_error:
            return [
                render_message(
                    _CHAT_OPTION_DETAIL.bind(
                        row=index,
                        key=DisplayBlock("model"),
                        detail=DisplayBlock(charset_error),
                    )
                )
            ]
    return []


def _validate_chat_option_stop(
    index: int,
    parsed: object,
    raw: str,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    if _chat_option_string_value(parsed, raw) is not None:
        return []
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return []
    return [render_message(_CHAT_OPTION_STOP.bind(row=index))]


def _validate_chat_option_logit_bias(
    index: int,
    parsed: object,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    if not isinstance(parsed, dict):
        return _validate_chat_option_object(index, "logit_bias", parsed, render_message=render_message)

    for token_id, bias in parsed.items():
        if isinstance(bias, bool) or not isinstance(bias, int | float) or not math.isfinite(float(bias)):
            return [
                render_message(
                    _CHAT_OPTION_LOGIT_BIAS.bind(
                        row=index,
                        token=DisplayBlock(repr(token_id)),
                    )
                )
            ]
        if not -100 <= float(bias) <= 100:
            return [
                render_message(
                    _CHAT_OPTION_LOGIT_BIAS.bind(
                        row=index,
                        token=DisplayBlock(repr(token_id)),
                    )
                )
            ]
    return []


def _validate_chat_option_extra_headers(
    index: int,
    parsed: object,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    if not isinstance(parsed, dict):
        return _validate_chat_option_object(index, "extra_headers", parsed, render_message=render_message)

    for header, value in parsed.items():
        if is_chrys_managed_header_name(str(header)):
            return [
                render_message(
                    _CHAT_OPTION_HEADER_RESERVED.bind(
                        row=index,
                        header=DisplayBlock(repr(header)),
                        app=APP_DISPLAY_NAME,
                    )
                )
            ]
        if not isinstance(value, str):
            return [
                render_message(
                    _CHAT_OPTION_HEADER_VALUE_STRING.bind(
                        row=index,
                        header=DisplayBlock(repr(header)),
                    )
                )
            ]
        for charset_error in (
            header_name_charset_error(str(header)),
            header_value_charset_error(str(header), blank_commands(value)),
        ):
            if charset_error:
                return [
                    render_message(
                        _CHAT_OPTION_DETAIL.bind(
                            row=index,
                            key=DisplayBlock("extra_headers"),
                            detail=DisplayBlock(charset_error),
                        )
                    )
                ]
    return []


# ── Modal screen ─────────────────────────────────────────────────────


class ModelConfigScreen(BaseDialog[str]):
    """Modal for managing model profiles with sidebar selection.

    Left sidebar lists all model profiles; selecting one loads its
    configuration in the right panel form.

    Save writes the current form to YAML and updates the in-memory
    registry. When the edited profile is the global default, its file
    pointer is refreshed without changing the runtime-effective model.
    The modal stays open and a success/error toast is shown.

    In read-only mode, the same screen can be opened while an agent turn
    is running: profile navigation stays available, but mutating controls
    are hidden/disabled and Close/Escape always dismisses with ``""``.

    Dismisses with ``"deleted"`` when a profile was deleted (a backend
    reload drops whatever the session used that is gone — the process
    pointer's, the session's own, an agent's), ``"updated"`` after a Save,
    or ``""`` on Close / Escape when the runtime can remain unchanged.
    """

    CSS_PATH = "screen.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]

    def __init__(
        self,
        registry: ModelProfileRegistry,
        global_default_profile_id: str = "",
        *,
        read_only: bool = False,
    ) -> None:
        self._registry = registry
        self._global_default_profile_id = global_default_profile_id
        self._read_only = read_only
        self._selected_profile_id: str = ""
        # A successful Save requires a soft restart even when the global
        # default pointer is unchanged because profile fields may affect
        # the runtime-effective model.
        self._saved_anything = False
        self._requires_runtime_reload = False
        # Set while the prompt-caching reminder is pushed or showing: a second
        # Save pressed before it mounts would push a second reminder.
        self._prompt_cache_offer_open = False
        super().__init__()

    # ── Compose ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with Vertical(id="mc-container") as container:
            container.border_title = Text(render_str(localizer, _CONFIGURATION_TITLE.bind()))

            with Horizontal(id="mc-main"):
                # Left sidebar
                with Vertical(id="mc-sidebar"):
                    yield OptionList(id="mc-list")

                # Right panel
                with Vertical(id="mc-right"), VerticalScroll(id="mc-scroll"):
                    # Profile name
                    yield Label(
                        f"[red]*[/red] {escape(render_str(localizer, _PROFILE_NAME.bind()))}", classes="mc-label"
                    )
                    yield Input(
                        placeholder=render_str(localizer, _PROFILE_NAME_PLACEHOLDER.bind()),
                        id="mc-name",
                    )

                    with Vertical(id="mc-model-options", classes="mc-option-section") as options:
                        options.border_title = render_str(localizer, _MODEL_OPTIONS.bind())
                        # Provider
                        yield Label(
                            f"[red]*[/red] {escape(render_str(localizer, _PROVIDER.bind()))}", classes="mc-label"
                        )
                        yield Select(
                            _PROVIDERS,
                            value="openai",
                            id="mc-provider",
                            allow_blank=False,
                        )
                        yield Label(
                            render_str(localizer, _API_STYLE.bind()),
                            classes="mc-label",
                            id="mc-api-style-label",
                        )
                        yield Select(
                            [(render_str(localizer, label.bind()), value) for label, value in _API_STYLES],
                            value=API_STYLE_CHAT_COMPLETIONS,
                            id="mc-api-style",
                            allow_blank=False,
                        )

                        # Model name
                        yield Label(
                            f"[red]*[/red] {escape(render_str(localizer, _MODEL.bind()))}",
                            classes="mc-label",
                            id="mc-model-label",
                        )
                        yield Input(
                            placeholder=render_str(localizer, _MODEL_PLACEHOLDER.bind()),
                            id="mc-model",
                        )

                        # Max context tokens
                        yield Label(
                            self._max_context_label(str(DEFAULT_MAX_CONTEXT_TOKENS)),
                            classes="mc-label",
                            id="mc-max-tokens-label",
                        )
                        yield Input(
                            value=str(DEFAULT_MAX_CONTEXT_TOKENS),
                            placeholder=str(DEFAULT_MAX_CONTEXT_TOKENS),
                            id="mc-max-tokens",
                        )

                        # Max output tokens (the model's hard per-response cap)
                        yield Label(
                            self._max_output_label("openai", API_STYLE_CHAT_COMPLETIONS),
                            classes="mc-label",
                            id="mc-max-output-label",
                        )
                        yield Input(
                            placeholder=render_str(
                                localizer,
                                _OUTPUT_CAP_PLACEHOLDER.bind(default=DEFAULT_MAX_OUTPUT_TOKENS),
                            ),
                            id="mc-max-output-tokens",
                        )

                        # Base URL — placeholder is updated per-provider in
                        # ``_update_provider_labels``; the initial value matches
                        # the default provider above (``openai``).
                        yield Label(render_str(localizer, _BASE_URL.bind()), classes="mc-label", id="mc-url-label")
                        yield Input(
                            placeholder=_PROVIDER_DEFAULT_BASE_URLS["openai"],
                            id="mc-base-url",
                        )

                        # API Key
                        yield Label(render_str(localizer, _API_KEY.bind()), classes="mc-label", id="mc-key-label")
                        yield Input(
                            placeholder="sk-...",
                            password=True,
                            id="mc-api-key",
                        )

                        yield Checkbox(
                            render_str(localizer, _STREAMING.bind()),
                            value=True,
                            id="mc-stream",
                            classes="mc-checkbox",
                        )
                        yield Checkbox(
                            render_str(localizer, _VISION.bind()),
                            value=False,
                            id="mc-vision",
                            classes="mc-checkbox",
                        )

                    with Vertical(id="mc-connection-options", classes="mc-option-section") as options:
                        options.border_title = render_str(localizer, _CONNECTION_OPTIONS.bind())
                        yield Checkbox(
                            render_str(localizer, _SKIP_TLS_VERIFICATION.bind()),
                            value=False,
                            id="mc-skip-tls",
                            classes="mc-checkbox",
                        )
                        tls_hint = Label(
                            render_str(localizer, _INSECURE_TLS.bind()),
                            id="mc-skip-tls-hint",
                            classes="mc-hint mc-tls-hint",
                        )
                        tls_hint.display = False
                        yield tls_hint
                        yield Checkbox(
                            render_str(localizer, _BYPASS_PROXY.bind()),
                            value=False,
                            id="mc-bypass-proxy",
                            classes="mc-checkbox",
                        )

                    with Vertical(id="mc-http-options", classes="mc-option-section") as options:
                        options.border_title = render_str(localizer, _HTTP_OPTIONS.bind())
                        # HTTP Connect Timeout
                        yield Label(render_str(localizer, _HTTP_CONNECT_TIMEOUT.bind()), classes="mc-label")
                        yield Input(
                            value="10.0",
                            placeholder="10.0",
                            id="mc-connect-timeout",
                        )

                        # HTTP Read Timeout
                        yield Label(render_str(localizer, _HTTP_READ_TIMEOUT.bind()), classes="mc-label")
                        yield Input(
                            value="300.0",
                            placeholder="300.0",
                            id="mc-read-timeout",
                        )

                        # HTTP Max Retries
                        yield Label(render_str(localizer, _HTTP_MAX_RETRIES.bind()), classes="mc-label")
                        yield Input(
                            value="2",
                            placeholder="2",
                            id="mc-max-retries",
                        )

                    with Vertical(id="mc-extra-options", classes="mc-option-section") as options:
                        options.border_title = render_str(localizer, _EXTRA_OPTIONS.bind())
                        # HTTP Extra Headers — key-value list
                        yield Label(render_str(localizer, _HTTP_EXTRA_HEADERS.bind()), classes="mc-label")
                        with Vertical(classes="mc-kv-list", id="mc-headers-list"):
                            yield self._compose_kv_row("", "", "h", read_only=self._read_only)
                            yield self._compose_kv_add_row("h", read_only=self._read_only)

                        # Chat Options — key-value list
                        yield Label(render_str(localizer, _CHAT_OPTIONS.bind()), classes="mc-label")
                        with Vertical(classes="mc-kv-list", id="mc-options-list"):
                            yield self._compose_kv_row("", "", "o", read_only=self._read_only)
                            yield self._compose_kv_add_row("o", read_only=self._read_only)

            # Footer buttons
            with Vertical(id="mc-footer") as footer:
                if self._read_only:
                    footer.add_class("-read-only")
                with Horizontal(id="mc-buttons"):
                    yield Button(render_str(localizer, _NEW.bind()), variant="primary", id="mc-new", flat=True)
                    yield Button(render_str(localizer, _CLONE.bind()), variant="primary", id="mc-clone", flat=True)
                    yield Button(render_str(localizer, _DELETE.bind()), variant="error", id="mc-delete", flat=True)
                    yield Static(id="mc-buttons-spacer")
                    yield Button(render_str(localizer, _SAVE.bind()), variant="primary", id="mc-save", flat=True)
                    yield Button(render_str(localizer, _CLOSE.bind()), variant="warning", id="mc-cancel", flat=True)
                notice = Static(Text(render_str(localizer, _READ_ONLY_NOTICE.bind())), id="mc-read-only-notice")
                notice.display = self._read_only
                yield notice

    # ── Mount ────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        profiles = self._listed_profiles()
        if not profiles:
            if self._read_only:
                self._set_form_enabled(False)
                self._apply_read_only_state()
                return
            # Auto-create a default profile so the user can start editing immediately
            await self._create_new_profile()
            return

        self._populate_sidebar()
        # Select the global default profile, or the first available profile.
        target_id = self._global_default_profile_id
        if not target_id or self._registry.get(target_id) is None:
            target_id = profiles[0].id
        ol = self.query_one("#mc-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(target_id)
        self._load_profile(target_id)
        self._apply_read_only_state()

    def _listed_profiles(self) -> list[ModelProfile]:
        """The registry's profiles in the order the sidebar lists them: as a person reads their names."""
        return sorted(self._registry.list_profiles(), key=lambda profile: reading_order(profile.name))

    def _populate_sidebar(self) -> None:
        ol = self.query_one("#mc-list", OptionList)
        ol.clear_options()
        profiles = self._listed_profiles()
        for i, p in enumerate(profiles):
            if i > 0:
                ol.add_option(None)  # Divider
            content = Content.assemble((p.name, "bold"))
            ol.add_option(Option(content, id=p.id))

    def _set_form_enabled(self, enabled: bool) -> None:
        """Enable or disable the right-panel form and Save/Delete buttons."""
        interactive = enabled and not self._read_only
        self.query_one("#mc-save", Button).disabled = not interactive
        self._update_delete_button()
        self._apply_read_only_state()

    def _update_delete_button(self) -> None:
        """Disable Delete when there is only one profile (or none selected)."""
        count = len(self._registry.list_profiles())
        button = self.query_one("#mc-delete", Button)
        button.disabled = self._read_only or count <= 1 or not self._selected_profile_id
        if self._read_only:
            button.display = False

    def _apply_read_only_state(self) -> None:
        """Make the mounted form view-only while keeping sidebar navigation usable."""
        if not self._read_only or not self.is_attached:
            return
        try:
            self.query_one("#mc-footer", Vertical).add_class("-read-only")
            self.query_one("#mc-read-only-notice", Static).display = True
        except NoMatches:
            return
        for input_widget in self.query(Input):
            input_widget.disabled = True
        for select in self.query(Select):
            select.disabled = True
        for checkbox in self.query(Checkbox):
            checkbox.disabled = True
        for button in self.query(Button):
            if button.id == "mc-cancel":
                button.disabled = False
                button.display = True
            else:
                button.disabled = True
                button.display = False

    # ── Key-value list helpers ───────────────────────────────────────

    def _compose_kv_row(
        self,
        key: str,
        val: str,
        prefix: str,
        *,
        read_only: bool = False,
    ) -> Horizontal:
        uid = _next_uid()
        key_placeholder, value_placeholder = self._kv_placeholders(prefix)
        key_input = Input(value=key, placeholder=key_placeholder, classes="mc-kv-field mc-kv-key-input")
        value_input = Input(value=val, placeholder=value_placeholder, classes="mc-kv-field mc-kv-value-input")
        remove_button = Button("✕", classes="mc-kv-remove-btn", id=f"mc-{prefix}del-{uid}")
        if read_only:
            # Seed read-only state here so mounted rows never have a pre-refresh editable window.
            key_input.disabled = True
            value_input.disabled = True
            remove_button.disabled = True
            remove_button.display = False
        return Horizontal(
            key_input,
            value_input,
            remove_button,
            classes="mc-kv-item-row",
        )

    def _compose_kv_add_row(self, prefix: str, *, read_only: bool = False) -> Horizontal:
        add_button = ConfigAddButton(
            self._render_toast(_ADD.bind()),
            compact=True,
            classes="mc-kv-add-btn",
            id=f"mc-{prefix}add",
        )
        if read_only:
            add_button.disabled = True
            add_button.display = False
        return Horizontal(
            add_button,
            classes="mc-kv-add-row",
            id=f"mc-{prefix}add-row",
        )

    def _populate_kv_list(self, container_id: str, items: dict[str, str], prefix: str) -> None:
        """Clear and repopulate a key-value list container."""
        container = self.query_one(f"#{container_id}", Vertical)
        # Remove all existing item rows (keep the add row)
        for row in list(container.query(".mc-kv-item-row")):
            row.remove()
        # Add rows before the add-row
        add_row_id = f"mc-{prefix}add-row"
        add_row = self.query_one(f"#{add_row_id}", Horizontal)
        for key, val in items.items():
            container.mount(self._compose_kv_row(key, val, prefix, read_only=self._read_only), before=add_row)
        if not items:
            container.mount(self._compose_kv_row("", "", prefix, read_only=self._read_only), before=add_row)
        self._apply_read_only_state()
        self.call_after_refresh(self._apply_read_only_state)

    def _read_kv_list(self, container_id: str) -> dict[str, str]:
        """Read all key-value pairs from a list container."""
        result: dict[str, str] = {}
        container = self.query_one(f"#{container_id}", Vertical)
        for row in container.query(".mc-kv-item-row"):
            key = row.query_one(".mc-kv-key-input", Input).value.strip()
            value = row.query_one(".mc-kv-value-input", Input).value.strip()
            if key:
                result[key] = value
        return result

    def _validate_kv_list(
        self,
        container_id: str,
        label_definition: MessageDef,
        *,
        http_headers: bool = False,
    ) -> list[str]:
        """Validate that partially-filled key-value rows are not saved silently.

        ``http_headers`` marks rows destined for HTTP headers: managed
        header names are rejected, and names/values must survive ASCII
        header encoding (``{{ENV_VAR}}`` templates are ASCII and pass;
        their resolved values are re-checked when the client is built).
        """
        label = self._render_toast(label_definition.bind())
        errors: list[str] = []
        seen: set[str] = set()
        container = self.query_one(f"#{container_id}", Vertical)
        for index, row in enumerate(container.query(".mc-kv-item-row"), start=1):
            key = row.query_one(".mc-kv-key-input", Input).value.strip()
            value = row.query_one(".mc-kv-value-input", Input).value.strip()
            if not key and not value:
                continue
            if key and not value:
                errors.append(
                    self._render_toast(
                        _KV_VALUE_REQUIRED.bind(
                            label=label,
                            row=index,
                            key=DisplayBlock(key),
                        )
                    )
                )
            elif value and not key:
                errors.append(self._render_toast(_KV_KEY_REQUIRED.bind(label=label, row=index)))
            if key and http_headers and is_chrys_managed_header_name(key):
                errors.append(
                    self._render_toast(
                        _KV_HEADER_RESERVED.bind(
                            label=label,
                            row=index,
                            key=DisplayBlock(key),
                            app=APP_DISPLAY_NAME,
                        )
                    )
                )
            if key and http_headers:
                errors.extend(
                    self._render_toast(
                        _KV_DETAIL.bind(
                            label=label,
                            row=index,
                            detail=DisplayBlock(charset_error),
                        )
                    )
                    for charset_error in (
                        header_name_charset_error(key),
                        header_value_charset_error(key, blank_commands(value)),
                    )
                    if charset_error
                )
            if key in seen:
                errors.append(
                    self._render_toast(
                        _KV_DUPLICATE_KEY.bind(
                            label=label,
                            row=index,
                            key=DisplayBlock(key),
                        )
                    )
                )
            if key:
                seen.add(key)
        return errors

    def _validate_required_positive_int(
        self,
        field_id: str,
        label_definition: MessageDef,
        *,
        example: int,
    ) -> list[str]:
        """Required positive-integer rule shared by the token-count fields."""
        label = self._render_toast(label_definition.bind())
        value = self.query_one(field_id, Input).value.strip()
        if not value:
            return [self._render_toast(_FIELD_REQUIRED_EXAMPLE.bind(label=label, example=example))]
        try:
            parsed = int(value)
        except ValueError:
            return [self._render_toast(_FIELD_VALID_INTEGER.bind(label=label))]
        if parsed <= 0:
            return [self._render_toast(_FIELD_POSITIVE_INTEGER.bind(label=label))]
        return []

    def _validate_chat_option_values(self) -> list[str]:
        """Validate common ChatOptions values before saving."""
        errors: list[str] = []
        container = self.query_one("#mc-options-list", Vertical)
        for index, row in enumerate(container.query(".mc-kv-item-row"), start=1):
            key = row.query_one(".mc-kv-key-input", Input).value.strip()
            value = row.query_one(".mc-kv-value-input", Input).value.strip()
            if not key or not value:
                continue

            parsed = _parse_chat_option_value_for_validation(value)

            if key in OUTPUT_CAP_OPTION_ALIASES:
                # A profile-level output cap (any provider spelling) would
                # fight the dedicated field: internal calls (e.g. LAST_WORDS
                # notes) size max_tokens per request and clamp to Max Output
                # Tokens, so a chat-options copy is at best redundant and at
                # worst overrides the clamp.
                errors.append(
                    self._render_toast(
                        _CHAT_OPTION_OUTPUT_CAP.bind(
                            row=index,
                            key=DisplayBlock(repr(key)),
                        )
                    )
                )
                continue
            if key in PROTECTED_TOP_LEVEL_CHAT_OPTION_KEYS:
                errors.append(
                    self._render_toast(
                        _CHAT_OPTION_PROTECTED.bind(
                            row=index,
                            key=DisplayBlock(repr(key)),
                        )
                    )
                )
                continue
            if key == "extra_body" and isinstance(parsed, dict):
                protected_nested = sorted(PROTECTED_EXTRA_BODY_CHAT_OPTION_KEYS & parsed.keys())
                if protected_nested:
                    errors.append(
                        self._render_toast(
                            _CHAT_OPTION_NESTED_PROTECTED.bind(
                                row=index,
                                keys=DisplayBlock(", ".join(protected_nested)),
                            )
                        )
                    )
                    continue
            if key in _JSON_OBJECT_CHAT_OPTION_KEYS:
                if key == "logit_bias":
                    errors.extend(_validate_chat_option_logit_bias(index, parsed, render_message=self._render_toast))
                elif key == "extra_headers":
                    errors.extend(_validate_chat_option_extra_headers(index, parsed, render_message=self._render_toast))
                else:
                    errors.extend(_validate_chat_option_object(index, key, parsed, render_message=self._render_toast))
            elif key in _FLOAT_CHAT_OPTION_RULES:
                errors.extend(_validate_chat_option_float(index, key, parsed, render_message=self._render_toast))
            elif key in _INT_CHAT_OPTION_RULES:
                errors.extend(_validate_chat_option_int(index, key, parsed, render_message=self._render_toast))
            elif key in _BOOL_CHAT_OPTION_KEYS:
                errors.extend(_validate_chat_option_bool(index, key, parsed, render_message=self._render_toast))
            elif key in _STRING_CHAT_OPTION_KEYS:
                errors.extend(
                    _validate_chat_option_string(
                        index,
                        key,
                        parsed,
                        value,
                        render_message=self._render_toast,
                    )
                )
            elif key == "stop":
                errors.extend(_validate_chat_option_stop(index, parsed, value, render_message=self._render_toast))
        return errors

    def _kv_placeholders(self, prefix: str) -> tuple[str, str]:
        if prefix == "h":
            return (
                self._render_toast(_HEADER_NAME_PLACEHOLDER.bind()),
                self._render_toast(_HEADER_VALUE_PLACEHOLDER.bind()),
            )
        return (
            self._render_toast(_OPTION_NAME_PLACEHOLDER.bind()),
            self._render_toast(_OPTION_VALUE_PLACEHOLDER.bind()),
        )

    async def _add_kv_item(self, container_id: str, prefix: str) -> None:
        """Append another editable key-value row."""
        if self._read_only:
            return
        try:
            container = self.query_one(f"#{container_id}", Vertical)
            add_row = self.query_one(f"#mc-{prefix}add-row", Horizontal)
        except Exception:
            return

        await container.mount(self._compose_kv_row("", "", prefix), before=add_row)
        container.refresh(layout=True)

    # ── Profile loading ──────────────────────────────────────────────

    def _load_profile(self, profile_id: str) -> None:
        """Populate the form fields from the given profile."""
        profile = self._registry.get(profile_id)
        if profile is None:
            return
        self._selected_profile_id = profile_id
        self._set_form_enabled(True)

        self.query_one("#mc-name", Input).value = profile.name
        self.query_one("#mc-provider", Select).value = profile.provider
        self.query_one("#mc-api-style", Select).value = (
            profile.api_style if profile.provider in ("openai", "deepseek-openai") else API_STYLE_CHAT_COMPLETIONS
        )
        self.query_one("#mc-model", Input).value = profile.model_id
        self.query_one("#mc-max-tokens", Input).value = str(profile.max_context_tokens)
        self.query_one("#mc-max-output-tokens", Input).value = str(
            profile.max_output_tokens if profile.max_output_tokens > 0 else DEFAULT_MAX_OUTPUT_TOKENS
        )
        self.query_one("#mc-base-url", Input).value = profile.base_url
        self.query_one("#mc-api-key", Input).value = profile.api_key
        self.query_one("#mc-connect-timeout", Input).value = str(profile.http_connect_timeout)
        self.query_one("#mc-read-timeout", Input).value = str(profile.http_read_timeout)
        self.query_one("#mc-max-retries", Input).value = str(profile.http_max_retries)
        skip_tls = not profile.verify_ssl
        self.query_one("#mc-skip-tls", Checkbox).value = skip_tls
        self.query_one("#mc-skip-tls-hint", Label).display = skip_tls
        self.query_one("#mc-bypass-proxy", Checkbox).value = profile.bypass_proxy
        self.query_one("#mc-stream", Checkbox).value = profile.stream
        self.query_one("#mc-vision", Checkbox).value = profile.vision

        # Populate key-value lists
        headers = _parse_json_dict(profile.http_headers, parse_values=False)
        self._populate_kv_list("mc-headers-list", headers, "h")

        options = _parse_json_dict(profile.chat_options)
        self._populate_kv_list("mc-options-list", options, "o")

        self._update_provider_labels(profile.provider)
        self._apply_read_only_state()
        self.call_after_refresh(self._apply_read_only_state)

    # ── Provider-dependent labels ────────────────────────────────────

    def _update_provider_labels(self, provider: str) -> None:
        """Update field labels and the Base URL placeholder for the selected provider."""
        prefix = _PROVIDER_LABELS.get(provider, provider.title())
        self.query_one("#mc-model-label", Label).update(
            f"[red]*[/red] {escape(self._render_toast(_PROVIDER_MODEL.bind(provider=prefix)))}"
        )
        self.query_one("#mc-url-label", Label).update(self._render_toast(_PROVIDER_BASE_URL.bind(provider=prefix)))
        self.query_one("#mc-key-label", Label).update(self._render_toast(_PROVIDER_API_KEY.bind(provider=prefix)))

        default_base_url = _PROVIDER_DEFAULT_BASE_URLS.get(provider, "")
        self.query_one("#mc-base-url", Input).placeholder = default_base_url or self._render_toast(
            _LEAVE_BLANK_FOR_DEFAULT.bind()
        )
        api_style_label = self.query_one("#mc-api-style-label", Label)
        api_style_select = self.query_one("#mc-api-style", Select)
        supports_api_style = provider in ("openai", "deepseek-openai")
        api_style_label.display = supports_api_style
        api_style_select.display = supports_api_style
        if not supports_api_style:
            api_style_select.value = API_STYLE_CHAT_COMPLETIONS
        self._refresh_max_output_label(provider, str(api_style_select.value))

    def _refresh_max_output_label(self, provider: str, api_style: str) -> None:
        raw = self.query_one("#mc-max-output-tokens", Input).value
        # Updated per keystroke: lay out only when the hint changes the wrapped line count.
        update_static_in_place(
            self.query_one("#mc-max-output-label", Label), self._max_output_label(provider, api_style, raw)
        )

    def _max_output_label(self, provider: str, api_style: str, raw: str = "") -> Text:
        param = _token_limit_param_name(provider, api_style)
        rendered = self._render_toast(_MAX_OUTPUT_TOKENS.bind(param=param))
        label = Text(rendered)
        start = rendered.find(param)
        if start >= 0:
            label.stylize("dim", start, start + len(param))
        return with_count_hint(label, raw)

    def _max_context_label(self, raw: str) -> Text:
        label = Text.assemble(("*", "red"), " ", self._render_toast(_MAX_CONTEXT_WINDOW.bind()))
        return with_count_hint(label, raw)

    # ── Events ───────────────────────────────────────────────────────

    @on(Select.Changed, "#mc-provider")
    def _on_provider_changed(self, event: Select.Changed) -> None:
        self._update_provider_labels(str(event.value))

    @on(Select.Changed, "#mc-api-style")
    def _on_api_style_changed(self, _event: Select.Changed) -> None:
        self._refresh_max_output_label(
            str(self.query_one("#mc-provider", Select).value),
            str(self.query_one("#mc-api-style", Select).value),
        )

    @on(Input.Changed, "#mc-max-tokens")
    def _on_max_context_changed(self, event: Input.Changed) -> None:
        update_static_in_place(self.query_one("#mc-max-tokens-label", Label), self._max_context_label(event.value))

    @on(Input.Changed, "#mc-max-output-tokens")
    def _on_max_output_changed(self, _event: Input.Changed) -> None:
        self._refresh_max_output_label(
            str(self.query_one("#mc-provider", Select).value),
            str(self.query_one("#mc-api-style", Select).value),
        )

    @on(Checkbox.Changed, "#mc-skip-tls")
    def _on_skip_tls_changed(self, event: Checkbox.Changed) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#mc-skip-tls-hint", Label).display = bool(event.value)

    @on(OptionList.OptionSelected, "#mc-list")
    def _on_profile_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and event.option.id != self._selected_profile_id:
            self._load_profile(event.option.id)

    @on(Button.Pressed, "#mc-hadd")
    async def _on_header_add(self, _event: Button.Pressed) -> None:
        await self._add_kv_item("mc-headers-list", "h")

    @on(Button.Pressed, "#mc-oadd")
    async def _on_option_add(self, _event: Button.Pressed) -> None:
        await self._add_kv_item("mc-options-list", "o")

    @on(Button.Pressed)
    def _on_kv_remove(self, event: Button.Pressed) -> None:
        """Handle removal of key-value rows (header or option)."""
        if self._read_only:
            return
        btn_id = event.button.id or ""
        if btn_id.startswith(("mc-hdel-", "mc-odel-")):
            row = event.button.parent
            if isinstance(row, Widget) and isinstance(row.parent, Vertical):
                container = row.parent
                was_last_row = len(list(container.query(".mc-kv-item-row"))) <= 1
                if was_last_row:
                    row.query_one(".mc-kv-key-input", Input).value = ""
                    row.query_one(".mc-kv-value-input", Input).value = ""
                    row.refresh(layout=True)
                else:
                    row.remove()

    async def _create_new_profile(self) -> None:
        """Create a new blank profile, add to sidebar, and select it.

        Names follow the pattern "Profile N" (starting at N=1) and
        increment until an unused name is found — so the first auto-
        seeded profile is "Profile 1", the next "Profile 2", etc.
        """
        if self._read_only:
            return
        from chrys.service.profiles.models.schema import ModelProfile
        from chrys.service.profiles.models.serializer import save_profile

        # Case-insensitive collision check: "Profile 1" / "profile 1" are
        # treated as duplicates to match the uniqueness rule used elsewhere.
        existing_names = {p.name.casefold() for p in self._registry.list_profiles()}
        base = "Profile"
        counter = 1
        name = f"{base} {counter}"
        while name.casefold() in existing_names:
            counter += 1
            name = f"{base} {counter}"

        profile_id = self._next_profile_id()
        profile = ModelProfile(id=profile_id, name=name)
        await asyncio.to_thread(save_profile, profile)
        self._registry.register(profile)

        self._populate_sidebar()
        ol = self.query_one("#mc-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(profile_id)
        self._load_profile(profile_id)
        self._update_delete_button()

    def _next_profile_id(self) -> str:
        """Generate a fresh model profile id."""
        while True:
            profile_id = uuid.uuid4().hex[:12]
            if self._registry.get(profile_id) is None:
                return profile_id

    @staticmethod
    def _clone_name_parts(name: str) -> tuple[str, int] | None:
        """Return ``(root, suffix_index)`` for generated clone names."""
        match = re.fullmatch(r"(.+?) copy(?: (\d+))?", name, flags=re.IGNORECASE)
        if match is None:
            return None
        suffix = match.group(2)
        return match.group(1), int(suffix) if suffix is not None else 1

    def _clone_name_indices(self, root_name: str) -> list[int]:
        """Return existing generated clone suffixes for the given root."""
        indices: list[int] = []
        for profile in self._registry.list_profiles():
            parts = self._clone_name_parts(profile.name)
            if parts is not None and parts[0].casefold() == root_name.casefold():
                indices.append(parts[1])
        return indices

    def _next_clone_name(self, source_name: str) -> str:
        """Generate a unique display name for a cloned model profile."""
        existing_names = {p.name.casefold() for p in self._registry.list_profiles()}
        source_parts = self._clone_name_parts(source_name)
        if source_parts is None:
            root_name = source_name
            counter = 1
        else:
            root_name, source_index = source_parts
            counter = max([source_index, *self._clone_name_indices(root_name)]) + 1
        base = f"{root_name} Copy"
        name = base if counter == 1 else f"{base} {counter}"
        while name.casefold() in existing_names:
            counter += 1
            name = f"{base} {counter}"
        return name

    @on(Button.Pressed, "#mc-new")
    async def _on_new(self, _event: Button.Pressed) -> None:
        """Create a new blank profile and select it."""
        if self._read_only:
            return
        await self._create_new_profile()

    @on(Button.Pressed, "#mc-clone")
    async def _on_clone(self, _event: Button.Pressed) -> None:
        """Create a saved copy of the selected profile and select it."""
        if self._read_only:
            return
        source = self._registry.get(self._selected_profile_id)
        if source is None:
            return

        from chrys.service.profiles.models.serializer import save_profile

        duplicate = copy.deepcopy(source)
        duplicate.id = self._next_profile_id()
        duplicate.name = self._next_clone_name(source.name)
        await asyncio.to_thread(save_profile, duplicate)
        self._registry.register(duplicate)

        self._populate_sidebar()
        ol = self.query_one("#mc-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(duplicate.id)
        self._load_profile(duplicate.id)
        self._update_delete_button()
        self._notify_unsupported_options(duplicate)

    @on(Button.Pressed, "#mc-delete")
    def _on_delete(self, _event: Button.Pressed) -> None:
        """Delete the selected profile after user confirmation."""
        if self._read_only:
            return
        profile_id = self._selected_profile_id
        if not profile_id:
            return
        profile = self._registry.get(profile_id)
        display = profile.name if profile else profile_id

        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(
            title=_DELETE_PROFILE_TITLE.bind(),
            message=_DELETE_PROFILE_MESSAGE.bind(display=display),
            confirm_label=_DELETE.bind(),
            confirm_variant="error",
            locale_controller=widget_locale_controller(self),
        )

        async def _on_confirmed(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                await self._do_delete(profile_id)
            except Exception as exc:
                self.notify(
                    str(exc),
                    title=self._render_toast(_MODEL_SETTINGS_TITLE.bind()),
                    severity="error",
                    timeout=5,
                    markup=False,
                )

        self.app.push_screen(dialog, callback=_on_confirmed)

    async def _do_delete(self, profile_id: str) -> None:
        """Actually delete the profile after confirmation.

        Deleting the global default promotes, in the user settings document,
        the first remaining profile as the sidebar lists them. Deleting the process pointer's profile also
        resynchronizes the pointer to a selectable profile — the file
        default when it resolves, else the first selectable one. Any
        deletion requests a backend reload, which drops whatever the session
        used that is gone. If no profiles remain, a fresh editable profile
        is created before either pointer is reconciled.
        """
        if self._read_only:
            return
        from chrys.service.profiles.models.env_bridge import get_active_profile_id, get_global_default_profile_id
        from chrys.service.profiles.models.resolver import resolve_profile_selector, resolve_selectable_profile
        from chrys.service.profiles.models.schema import is_model_profile_selectable
        from chrys.service.profiles.models.serializer import delete_profile

        was_global_default = profile_id == self._global_default_profile_id
        # The process pointer may hold a profile NAME (id-or-unique-name
        # selection is supported); resolve it against the still-complete
        # registry before deleting, or a name pointer at the deleted profile
        # would silently keep the dead executor running.
        active_profile = resolve_profile_selector(self._registry, get_active_profile_id())
        was_runtime_effective = active_profile is not None and active_profile.id == profile_id

        await asyncio.to_thread(delete_profile, profile_id)
        self._registry.remove(profile_id)

        profiles = self._listed_profiles()
        seeded_default = not profiles
        if seeded_default:
            await self._create_new_profile()
            profiles = self._listed_profiles()

        if was_global_default:
            # Prefer a selectable replacement — the listed order can put a
            # hollow auto-seeded profile first, and promoting it would point
            # both pointers at an unusable model while real ones exist.
            new_global_default = next(
                (candidate for candidate in profiles if is_model_profile_selectable(candidate)),
                profiles[0],
            )
            await self._persist_active_profile(new_global_default)
            self._global_default_profile_id = new_global_default.id

        if was_runtime_effective:
            # The file default may itself be dangling or hollow (deleting a
            # non-default profile never repairs the file pointer); adopting
            # it verbatim would strand the session on the placeholder while
            # a selectable profile exists. Resolve it first and fall back
            # the same way global-default promotion does.
            global_default_profile_id = await asyncio.to_thread(get_global_default_profile_id)
            replacement = resolve_selectable_profile(self._registry, global_default_profile_id)
            if replacement is None:
                replacement = next(
                    (candidate for candidate in profiles if is_model_profile_selectable(candidate)),
                    None,
                )
            if replacement is not None:
                set_model_pointer(replacement.id, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))
            else:
                set_model_pointer(None, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))
        # Whichever of the session's models it was — the pointer's, the session's own, an
        # agent's — the reload finds out; telling them apart here would be a second definition.
        self._requires_runtime_reload = True

        if seeded_default:
            return

        self._populate_sidebar()
        first = profiles[0].id
        ol = self.query_one("#mc-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(first)
        self._load_profile(first)

    async def _persist_active_profile(self, profile: ModelProfile | None) -> None:
        """Write the global default profile pointer to the user settings document only.

        ``None`` removes ``model.profile.active`` so resolvers fall back to
        the built-in default profile.
        """
        if self._read_only:
            return
        from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

        await asyncio.to_thread(set_global_default_profile_id, profile.id if profile is not None else "")

    @on(Button.Pressed, "#mc-save")
    async def _on_save(self, _event: Button.Pressed) -> None:
        """Save the form without changing the runtime-effective pointer."""
        if self._read_only or self._prompt_cache_offer_open:
            return
        errors = self._validate()
        if errors:
            self.notify(
                "\n".join(errors),
                title=self._render_toast(_VALIDATION_ERROR_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return
        if self._should_offer_prompt_cache():
            self._offer_prompt_cache()
            return
        await self._save_and_report()

    async def _save_and_report(self) -> None:
        try:
            profile = await self._save_only()
        except Exception as exc:
            self.notify(
                str(exc),
                title=self._render_toast(_SAVE_ERROR_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return
        self.notify(
            self._render_toast(_MODEL_PROFILE_SAVED.bind()),
            title=self._render_toast(_MODEL_SETTINGS_TITLE.bind()),
            severity="information",
            timeout=3,
            markup=False,
        )
        self._notify_unsupported_options(profile)

    def _should_offer_prompt_cache(self) -> bool:
        """Whether this save makes the profile a Claude profile that never asks for prompt caching.

        A profile already saved as a complete Claude profile on the Anthropic
        protocol has had its answer; one saved as something else since (another
        provider or model) is asked again when a save makes it one.
        """
        if not lacks_anthropic_prompt_cache_option(self._build_profile_from_form()):
            return False
        previous = self._registry.get(self._selected_profile_id)
        return previous is None or not (is_model_profile_selectable(previous) and is_anthropic_claude_profile(previous))

    def _offer_prompt_cache(self) -> None:
        from chrys.app.tui.screens.dialogs.prompt_cache import ADD_AND_SAVE_RESULT, PromptCacheDialog

        async def _on_choice(choice: PromptCacheDialogResult) -> None:
            self._prompt_cache_offer_open = False
            if choice is None or not self.is_attached:
                return
            if choice == ADD_AND_SAVE_RESULT:
                await self._add_prompt_cache_option()
            await self._save_and_report()

        self._prompt_cache_offer_open = True
        self.app.push_screen(PromptCacheDialog(locale_controller=widget_locale_controller(self)), callback=_on_choice)

    async def _add_prompt_cache_option(self) -> None:
        """Merge Anthropic's prompt-caching switch into the form's ``extra_body`` option.

        Edits the existing ``extra_body`` row, else fills the first blank
        row, else mounts a new one; the save then reads the form as usual.
        """
        container = self.query_one("#mc-options-list", Vertical)
        rows = list(container.query(".mc-kv-item-row"))
        blank: tuple[Input, Input] | None = None
        for row in rows:
            key_input = row.query_one(".mc-kv-key-input", Input)
            value_input = row.query_one(".mc-kv-value-input", Input)
            key = key_input.value.strip()
            if key == "extra_body":
                raw = value_input.value.strip()
                value_input.value = json.dumps(with_anthropic_prompt_cache_option(json.loads(raw) if raw else None))
                return
            if blank is None and not key and not value_input.value.strip():
                blank = (key_input, value_input)
        value = json.dumps(with_anthropic_prompt_cache_option(None))
        if blank is not None:
            blank[0].value = "extra_body"
            blank[1].value = value
            return
        add_row = self.query_one("#mc-oadd-row", Horizontal)
        await container.mount(self._compose_kv_row("extra_body", value, "o"), before=add_row)

    @on(Button.Pressed, "#mc-cancel")
    def _on_cancel(self, _event: Button.Pressed) -> None:
        self.dismiss(self._cancel_result())

    def action_cancel(self) -> None:
        self.dismiss(self._cancel_result())

    def _cancel_result(self) -> str:
        """Return the dismiss result from explicit reload requirements.

        * ``"deleted"`` — a profile was deleted (after reconciling the
          process pointer): the reload drops whatever is gone.
        * ``"updated"`` — at least one Save reached disk + registry;
          the engine must rebuild so profile edits take effect.
        * ``""`` — no runtime reload is required.
        """
        if self._read_only:
            return ""
        if self._requires_runtime_reload:
            return "deleted"
        if self._saved_anything:
            return "updated"
        return ""

    def _default_dismiss_result(self) -> str:  # type: ignore[override]
        return self._cancel_result()

    # ── Validation ───────────────────────────────────────────────────

    def _validate(self) -> list[str]:
        errors: list[str] = []

        name = self.query_one("#mc-name", Input).value.strip()
        if not name:
            errors.append(self._render_toast(_NAME_REQUIRED.bind()))
        else:
            # Uniqueness: no other profile may share this name (case-
            # insensitive — "Default" and "default" collide).  Compare by
            # id so the currently-edited profile is excluded.
            lowered = name.casefold()
            for other in self._registry.list_profiles():
                if other.id != self._selected_profile_id and other.name.casefold() == lowered:
                    errors.append(self._render_toast(_PROFILE_EXISTS.bind(name=DisplayBlock(other.name))))
                    break

        provider = str(self.query_one("#mc-provider", Select).value)
        if provider not in {p[1] for p in _PROVIDERS}:
            allowed = ", ".join(p[0] for p in _PROVIDERS)
            errors.append(self._render_toast(_PROVIDER_ALLOWED.bind(providers=allowed)))

        model = self.query_one("#mc-model", Input).value.strip()
        if not model:
            errors.append(self._render_toast(_MODEL_REQUIRED.bind()))
        else:
            model_error = model_id_charset_error(model)
            if model_error:
                errors.append(model_error)

        errors.extend(
            self._validate_required_positive_int(
                "#mc-max-tokens",
                _MAX_CONTEXT_TOKENS_LABEL,
                example=DEFAULT_MAX_CONTEXT_TOKENS,
            )
        )
        errors.extend(
            self._validate_required_positive_int(
                "#mc-max-output-tokens",
                _MAX_OUTPUT_TOKENS_LABEL,
                example=DEFAULT_MAX_OUTPUT_TOKENS,
            )
        )
        max_context_text = self.query_one("#mc-max-tokens", Input).value.strip()
        max_output_text = self.query_one("#mc-max-output-tokens", Input).value.strip()
        try:
            max_context_tokens = int(max_context_text)
        except ValueError:
            max_context_tokens = 0
        try:
            max_output_tokens = int(max_output_text)
        except ValueError:
            max_output_tokens = 0
        if 0 < max_context_tokens < MIN_DERIVABLE_CONTEXT_TOKENS:
            errors.append(self._render_toast(_MAX_CONTEXT_MINIMUM.bind(minimum=MIN_DERIVABLE_CONTEXT_TOKENS)))
        if max_context_tokens > 0 and max_output_tokens > 0 and max_output_tokens >= max_context_tokens:
            errors.append(self._render_toast(_MAX_OUTPUT_LESS_THAN_CONTEXT.bind()))

        base_url = self.query_one("#mc-base-url", Input).value.strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            errors.append(self._render_toast(_BASE_URL_SCHEME.bind()))

        # A pasted key with a stray non-ASCII character (emoji, CJK text,
        # smart quotes) would otherwise only fail at request time, as an
        # opaque encode error.  ``{{ENV_VAR}}`` templates are ASCII and
        # pass untouched; their resolved values are re-checked when the
        # client is built.
        api_key = self.query_one("#mc-api-key", Input).value.strip()
        api_key_error = api_key_charset_error(blank_commands(api_key))
        if api_key_error:
            errors.append(api_key_error)

        # HTTP timeouts
        for field_id, label_definition in [
            ("#mc-connect-timeout", _HTTP_CONNECT_TIMEOUT_LABEL),
            ("#mc-read-timeout", _HTTP_READ_TIMEOUT_LABEL),
        ]:
            label = self._render_toast(label_definition.bind())
            val = self.query_one(field_id, Input).value.strip()
            if val:
                try:
                    f = float(val)
                    if f <= 0:
                        errors.append(self._render_toast(_FIELD_POSITIVE_NUMBER.bind(label=label)))
                except ValueError:
                    errors.append(self._render_toast(_FIELD_VALID_NUMBER.bind(label=label)))

        retries = self.query_one("#mc-max-retries", Input).value.strip()
        if retries:
            try:
                r = int(retries)
                if r < 0:
                    errors.append(self._render_toast(_HTTP_RETRIES_NON_NEGATIVE.bind()))
            except ValueError:
                errors.append(self._render_toast(_HTTP_RETRIES_VALID.bind()))

        errors.extend(self._validate_kv_list("mc-headers-list", _HTTP_EXTRA_HEADERS, http_headers=True))
        errors.extend(self._validate_kv_list("mc-options-list", _CHAT_OPTIONS))
        errors.extend(self._validate_chat_option_values())

        return errors

    # ── Save / Apply ─────────────────────────────────────────────────

    def _build_profile_from_form(self) -> ModelProfile:
        """Construct a ``ModelProfile`` from the current form state."""
        from chrys.service.profiles.models.schema import ModelProfile

        headers = self._read_kv_list("mc-headers-list")
        options = self._read_kv_list("mc-options-list")
        provider = str(self.query_one("#mc-provider", Select).value)
        api_style_value = str(self.query_one("#mc-api-style", Select).value)
        api_style = (
            API_STYLE_RESPONSES if is_responses_wire_dialect(provider, api_style_value) else API_STYLE_CHAT_COMPLETIONS
        )
        # Fields the form has no control for keep their stored value.
        stored = self._registry.get(self._selected_profile_id)

        return ModelProfile(
            id=self._selected_profile_id,
            name=self.query_one("#mc-name", Input).value.strip(),
            provider=provider,
            api_style=api_style,
            model_id=self.query_one("#mc-model", Input).value.strip(),
            max_context_tokens=int(self.query_one("#mc-max-tokens", Input).value.strip()),
            max_output_tokens=int(self.query_one("#mc-max-output-tokens", Input).value.strip()),
            base_url=self.query_one("#mc-base-url", Input).value.strip(),
            api_key=self.query_one("#mc-api-key", Input).value.strip(),
            http_connect_timeout=float(self.query_one("#mc-connect-timeout", Input).value.strip() or "10.0"),
            http_read_timeout=float(self.query_one("#mc-read-timeout", Input).value.strip() or "300.0"),
            http_max_retries=int(self.query_one("#mc-max-retries", Input).value.strip() or "2"),
            verify_ssl=not self.query_one("#mc-skip-tls", Checkbox).value,
            bypass_proxy=self.query_one("#mc-bypass-proxy", Checkbox).value,
            http_headers=_kv_to_json(headers, parse_values=False),
            chat_options=_kv_to_json(options),
            stream=self.query_one("#mc-stream", Checkbox).value,
            vision=self.query_one("#mc-vision", Checkbox).value,
            stream_requires_finish_reason=stored is not None and stored.stream_requires_finish_reason,
            thinking_block_binding=stored.thinking_block_binding if stored is not None else "auto",
            auto_interleaved_thinking=stored.auto_interleaved_thinking if stored is not None else True,
        )

    async def _save_only(self) -> ModelProfile | None:
        """Persist the form to YAML + registry and return the saved profile.

        If the edited profile is the global default — or no usable global
        default exists yet, in which case the first successful save claims
        the pointer — also refresh that durable document pointer. A
        stored default that no longer resolves to a selectable profile
        (deleted or hollowed out behind our back) counts as absent. The
        runtime-effective process pointer is deliberately left unchanged.
        """
        if self._read_only:
            return None
        from chrys.service.profiles.models.env_bridge import get_active_profile_id
        from chrys.service.profiles.models.resolver import resolve_profile_selector, resolve_selectable_profile
        from chrys.service.profiles.models.serializer import save_profile

        profile = self._build_profile_from_form()
        # The process pointer may reference this profile by NAME; capture
        # that against the pre-save registry, because a rename would strand
        # such a pointer and modal close would then misread the runtime as
        # inactive and adopt the global default — silently switching models.
        process_selector = get_active_profile_id()
        process_target = resolve_profile_selector(self._registry, process_selector)
        process_points_here = process_target is not None and process_target.id == profile.id
        await asyncio.to_thread(save_profile, profile)
        self._registry.register(profile)
        if process_points_here and resolve_profile_selector(self._registry, process_selector) is None:
            set_model_pointer(profile.id, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))
        self._saved_anything = True
        if (
            self._selected_profile_id == self._global_default_profile_id
            or resolve_selectable_profile(self._registry, self._global_default_profile_id) is None
        ):
            await self._persist_active_profile(profile)
            self._global_default_profile_id = profile.id
        # Refresh sidebar so a renamed profile's label reflects the new name.
        self._populate_sidebar()
        ol = self.query_one("#mc-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(self._selected_profile_id)
        return profile

    def _notify_unsupported_options(self, profile: ModelProfile | None) -> None:
        """Surface non-blocking warnings for saved-but-unsupported option combos."""
        if profile is None:
            return
        from chrys.service.profiles.models.options import (
            protected_chat_option_keys_warning_structured,
            responses_store_continuation_warning,
        )

        warnings: list[str] = []
        if responses_store_continuation_warning(profile) is not None:
            warnings.append(self._render_toast(_RESPONSES_STORE_WARNING.bind()))
        protected_warning = protected_chat_option_keys_warning_structured(profile)
        if protected_warning is not None:
            warnings.append(
                self._render_toast(
                    _PROTECTED_CHAT_OPTIONS_WARNING.bind(
                        keys=DisplayBlock(", ".join(protected_warning.keys)),
                    )
                )
            )
        for warning in warnings:
            self.notify(
                warning,
                title=self._render_toast(_MODEL_SETTINGS_TITLE.bind()),
                severity="warning",
                timeout=8,
                markup=False,
            )

    def _render_toast(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)


def _parse_json_dict(raw: str, *, parse_values: bool = True) -> dict[str, str]:
    """Parse a JSON string into a dict, returning empty dict on failure.

    Non-string values (objects, arrays, booleans, numbers) are
    re-serialised via ``json.dumps`` so that ``_kv_to_json`` can
    parse them back losslessly (e.g. ``True`` → ``"true"``, not
    Python's ``"True"``). With *parse_values* (``_kv_to_json``'s
    default), a string that would read back as JSON (``"1"``,
    ``"true"``) or that the form's whitespace trim would change is
    shown quoted too, so saving the form again keeps it the same string.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            result: dict[str, str] = {}
            for k, v in parsed.items():
                if not isinstance(v, str) or (parse_values and (v != v.strip() or _reads_as_json(v))):
                    v = json.dumps(v)
                result[str(k)] = v
            return result
    except json.JSONDecodeError, TypeError:
        pass
    return {}


_NOT_JSON = object()


def _reads_as_json(text: str) -> bool:
    return _loads_or(text, _NOT_JSON) is not _NOT_JSON


def _kv_to_json(kv: dict[str, str], *, parse_values: bool = True) -> str:
    """Serialise a key-value dict to a JSON string.

    Values that are valid JSON (objects, arrays, numbers, booleans) are
    embedded as-is so that nested structures survive the round-trip.
    Plain strings are kept as strings.
    """
    if not kv:
        return ""
    if not parse_values:
        return json.dumps(kv)
    return json.dumps({k: _loads_or(v, v) for k, v in kv.items()})
