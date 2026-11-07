# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ModelProfile data model — defines the structure of a model profile configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

API_STYLE_CHAT_COMPLETIONS = "chat_completions"
API_STYLE_RESPONSES = "responses"
VALID_API_STYLES = frozenset({API_STYLE_CHAT_COMPLETIONS, API_STYLE_RESPONSES})
VALID_PROVIDERS = frozenset({"openai", "anthropic", "deepseek-openai", "glm-openai", "mock"})
UNCONFIGURED_MODEL_ID = "<Model Not Configured>"
ApiStyle = Literal["chat_completions", "responses"]
ThinkingBlockBinding = Literal["auto", "drop_block", "error", "off"]
VALID_THINKING_BLOCK_BINDINGS: frozenset[str] = frozenset({"auto", "drop_block", "error", "off"})

# Default per-response output-token cap for every provider.  Applied as the
# live ``max_tokens`` default (``effective_chat_options``) and as the
# LAST_WORDS note-call ceiling; models with a lower provider hard cap
# (e.g. DeepSeek chat 8192) need an explicit lower value on the profile.
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_MAX_CONTEXT_TOKENS = 200_000


@dataclass
class ModelProfile:
    """A named model configuration profile.

    Each profile stores all settings needed to connect to an LLM provider:
    provider, OpenAI API style, model ID, API credentials, HTTP transport
    tuning, and extra options.  Profiles are stored as YAML files in
    ``~/.chrys/models/`` with UUID-based filenames.
    """

    id: str  # UUID hex — disk filename stem
    name: str  # Human-readable display name
    provider: str = "openai"  # "openai", "anthropic", "deepseek-openai", or "glm-openai"
    api_style: ApiStyle = API_STYLE_CHAT_COMPLETIONS  # OpenAI-compatible API wire style
    model_id: str = ""  # Provider-specific model identifier (must be set before use)
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS  # Output cap, always positive; live max_tokens default
    base_url: str = ""
    api_key: str = ""
    http_connect_timeout: float = 10.0  # Provider connection timeout in seconds
    http_read_timeout: float = 300.0  # Provider read timeout in seconds
    http_max_retries: int = 2  # SDK-internal retries on top of the initial HTTP request
    verify_ssl: bool = True  # Set False to skip TLS certificate verification (insecure)
    bypass_proxy: bool = False  # Route this profile's LLM traffic around configured proxies
    http_headers: str = ""  # JSON object of additional provider HTTP headers
    chat_options: str = ""  # JSON object of provider request options
    # Marks the endpoint as a third-party relay: prompts to this profile are
    # routed through an operator Chrys does not control.  While any configured
    # profile is a relay, the approval judge must be a different, independently
    # configured direct service (see service/profiles/models/relay_guard.py).
    is_relay: bool = False
    stream: bool = True  # Stream response
    vision: bool = False  # Supports image input
    # Chat Completions: a stream that ends without a finish reason fails as
    # truncated instead of being accepted with a warning.
    stream_requires_finish_reason: bool = False
    # Anthropic: what a request tells the service to do with replayed thinking
    # that no longer matches the conversation before it ("auto" chooses).
    thinking_block_binding: ThinkingBlockBinding = "auto"
    # Anthropic: add the interleaved-thinking beta to budgeted thinking.
    auto_interleaved_thinking: bool = True


def is_model_profile_selectable(profile: ModelProfile) -> bool:
    """Return whether a profile is structurally complete enough to select."""
    string_fields = (profile.id, profile.name, profile.provider, profile.api_style, profile.model_id)
    if not all(isinstance(value, str) for value in string_fields):
        return False
    if not profile.id.strip() or not profile.name.strip():
        return False
    if profile.provider not in VALID_PROVIDERS or profile.api_style not in VALID_API_STYLES:
        return False
    if profile.provider != "mock" and (
        not profile.model_id.strip() or profile.model_id.strip() == UNCONFIGURED_MODEL_ID
    ):
        return False
    return all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (profile.max_context_tokens, profile.max_output_tokens)
    )


def is_responses_wire_dialect(provider: str, api_style: str) -> bool:
    """Return whether a provider uses the OpenAI Responses wire shape."""
    return provider in {"openai", "deepseek-openai"} and api_style == API_STYLE_RESPONSES


def uses_responses_wire_dialect(profile: ModelProfile) -> bool:
    """Return whether a model profile uses the OpenAI Responses wire shape."""
    return is_responses_wire_dialect(profile.provider, profile.api_style)
