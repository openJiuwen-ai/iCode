# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LLM client factory — creates chat clients for supported providers.

Takes a ``ModelProfile`` (not ``Settings``) so each call site can pass
its own profile — main agent, sub-agent, approval judge, last-words
generator, recall-context call, buddy reply — without sharing global
env state.  All HTTP transport, model id, headers, and credentials are
read from the profile.

Four provider types are supported:

- ``anthropic`` — native Anthropic API via Chrys' owned Anthropic wire client.
- ``openai`` — OpenAI Chat Completions or Responses API via Chrys' owned
  OpenAI wire clients.
- ``deepseek-openai`` — DeepSeek via its OpenAI-compatible Chat Completions
  or Responses endpoint; reuses the OpenAI HTTP transport and routes through
  the DeepSeek variant of the Chat Completions or Responses client. Named
  ``deepseek-openai`` (not
  ``deepseek``) to make it explicit that this routes through the
  OpenAI contract; a future native DeepSeek transport could be added
  alongside it under a different id.
- ``glm-openai`` — GLM (Zhipu AI / z.ai) via its OpenAI-compatible Chat
  Completions endpoint; routes through ``GlmChatCompletionsClient`` so
  ``reasoning_content`` is replayed on every multi-turn request per
  z.ai's preserved-thinking contract — see ``chrys.service.llm.chat_completions``.

All four providers share the same chrys touch surfaces (request
reporting and intermediate text, ``function_invocation`` config, default
headers) so the engine, executor, approval judge, last-words, recall and
sub-agents behave identically regardless of provider.  What differs per
provider id as plain data (credential and endpoint fallbacks, SDK family,
API styles) lives in ``chrys.service.llm.providers``.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.util.chrys_headers import (
    MODEL_ID_HEADER,
    PARENT_SESSION_ID_HEADER,
    SESSION_ID_HEADER,
    X_PARENT_SESSION_ID_HEADER,
    X_SESSION_ID_HEADER,
)
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.foundation.util.header_charset import (
    api_key_charset_error,
    header_name_charset_error,
    header_value_charset_error,
    model_id_charset_error,
)
from chrys.foundation.util.httpx_helpers import BYPASS_PROXY_MOUNTS
from chrys.foundation.util.once_close import OnceClose
from chrys.service.llm.providers import PROVIDERS, ProviderSpec
from chrys.service.profiles.models.options import parse_http_headers
from chrys.service.profiles.models.schema import ModelProfile

if TYPE_CHECKING:
    import httpx
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    from chrys.kernel import ToolLoopLayer
    from chrys.service.llm.wire_client import WireClient

_log = logging.getLogger(__name__)


def _build_user_agent(sdk_user_agent: str | None = None) -> str:
    """Return Chrys' HTTP user agent with optional provider SDK attribution."""
    from chrys import __version__

    runtime = sys.version_info
    products = [
        f"Chrys/{__version__}",
        f"Python/{runtime.major}.{runtime.minor}.{runtime.micro}",
    ]
    if sdk_user_agent:
        products.append(sdk_user_agent)
    return " ".join(products)


def _provider_sdk_user_agent(provider: str) -> str | None:
    """Return the provider SDK user-agent token Chrys would otherwise replace."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return None
    if spec.sdk == "anthropic":
        import anthropic

        return f"AsyncAnthropic/Python {anthropic.__version__}"
    import openai

    return f"AsyncOpenAI/Python {openai.__version__}"


def _build_default_headers(
    session_id: str | None,
    profile: ModelProfile,
    *,
    parent_session_id: str | None = None,
    sdk_user_agent: str | None = None,
) -> dict[str, str]:
    """Build default HTTP headers for LLM client requests.

    Includes chrys platform headers (client name, version, user agent,
    ``X-Session-ID``, ``Chrys-Session-Id``, optional parent-session headers,
    and ``Chrys-Model-Id``) merged with the profile's ``http_headers`` (parsed
    from JSON). Profile headers take precedence on key conflicts except for
    Chrys-managed request metadata. The wire clients overwrite the model
    header per request with the final provider ``model`` value.
    """
    from chrys import __version__

    headers: dict[str, str] = {
        "User-Agent": _build_user_agent(sdk_user_agent),
        "X-Client-Name": "chrys",
        "X-Client-Version": __version__,
    }
    headers.update(parse_http_headers(profile))
    if session_id:
        headers[X_SESSION_ID_HEADER] = session_id
        headers[SESSION_ID_HEADER] = session_id
    if parent_session_id:
        headers[X_PARENT_SESSION_ID_HEADER] = parent_session_id
        headers[PARENT_SESSION_ID_HEADER] = parent_session_id
    headers[MODEL_ID_HEADER] = profile.model_id
    return headers


def _resolve_profile_api_key(profile: ModelProfile) -> str:
    """Resolve explicit API-key env templates before provider env fallback."""
    return resolve_env_templates(profile.api_key, location=f"model profile {profile.name!r} API Key")


def _validate_wire_charset(profile: ModelProfile, *, api_key: str, headers: dict[str, str]) -> None:
    """Reject profile values that httpx cannot encode into HTTP headers.

    Runs after ``{{ENV_VAR}}`` templates and the provider env-fallback
    key resolve, so hand-edited YAML, environment credentials, and
    template values are all covered — not only what the profile editor
    validated at save time.  Raising here turns what would surface as an
    opaque ``UnicodeEncodeError`` inside the first chat request into a
    configuration error that names the offending field, without echoing
    secret content.
    """
    problems: list[str] = []
    spec = PROVIDERS.get(profile.provider)
    effective_key = api_key or (os.environ.get(spec.api_key_env, "") if spec is not None else "")
    key_error = api_key_charset_error(effective_key)
    if key_error:
        problems.append(key_error)
    model_error = model_id_charset_error(profile.model_id)
    if model_error:
        problems.append(model_error)
    for name, value in headers.items():
        name_error = header_name_charset_error(name)
        if name_error:
            problems.append(name_error)
        if name == MODEL_ID_HEADER:
            # Mirrors ``profile.model_id`` — already reported above under
            # its own field name.
            continue
        value_error = header_value_charset_error(name, value)
        if value_error:
            problems.append(value_error)
    if problems:
        raise ValueError(
            f"Model profile {profile.name!r} has values that cannot be sent over HTTP: " + " ".join(problems)
        )


def _validate_sdk_wire_charset(profile: ModelProfile, client: Any) -> None:
    """Reject unsafe static headers added internally by a provider SDK.

    Both pinned SDKs add environment-derived values after Chrys supplies
    ``default_headers``: OpenAI organization/project/custom headers, and
    Anthropic bearer/custom headers.  Inspecting the constructed client's
    effective static headers keeps those SDK-owned inputs behind the same
    pre-request charset gate as profile-owned values.  Non-string sentinels
    such as OpenAI's ``Omit`` are ignored because the SDK removes them before
    constructing ``httpx.Headers``.
    """
    effective_headers = dict(client.auth_headers)
    effective_headers.update(client.default_headers)

    problems: list[str] = []
    for name, value in effective_headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        name_error = header_name_charset_error(name)
        if name_error:
            problems.append(name_error)
        value_error = header_value_charset_error(name, value)
        if value_error:
            problems.append(value_error)
    if problems:
        raise ValueError(
            f"Model profile {profile.name!r} has provider SDK headers that cannot be sent over HTTP: "
            + " ".join(problems)
        )


async def _empty_openai_api_key_provider() -> str:
    """Return an empty key for unauthenticated OpenAI-compatible endpoints."""
    return ""


def effective_model_base_url(profile: ModelProfile) -> str:
    """Return the base URL the provider client will use for display/provenance."""
    provider = profile.provider.lower()
    if provider == "mock":
        return profile.base_url
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise _unknown_provider(provider)
    return profile.base_url or os.environ.get(spec.base_url_env, "") or spec.default_base_url


def _build_profile_http_client(
    profile: ModelProfile,
    timeout: Any,
    *,
    raw_http_log_path: Path | None = None,
    session_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build the profile's ``httpx.AsyncClient``; Chrys, not the SDK, owns every pool.

    Uses the provider SDK's public ``DefaultAsyncHttpxClient`` so a default
    profile keeps the SDK's own pool limits, redirects and (Anthropic) TCP
    keepalive socket options.  When proxy bypass is enabled, explicit ``None``
    mounts override httpx's environment-derived proxy transports while
    leaving ``trust_env=True`` in place for CA bundle env such as
    ``SSL_CERT_FILE``.

    Route hooks come first on both lists, so every request carries its route
    snapshot before any other hook (the raw HTTP log) runs or fails.

    *transport* replaces the network transport and nothing else; production
    callers never pass it, the client contract tests answer through it.
    """
    spec = PROVIDERS.get(profile.provider)
    if spec is not None and spec.sdk == "anthropic":
        from anthropic import DefaultAsyncHttpxClient as HTTPClient
    else:
        from openai import DefaultAsyncHttpxClient as HTTPClient

    from chrys.service.llm.proxy_route import ProxyRouter
    from chrys.service.llm.request_tracking import build_request_tracking_hooks
    from chrys.service.llm.route_facts import build_route_hooks

    kwargs: dict[str, Any] = {
        "verify": profile.verify_ssl,
        "timeout": timeout,
        "follow_redirects": True,
    }
    if profile.bypass_proxy:
        kwargs["mounts"] = dict(BYPASS_PROXY_MOUNTS)
    event_hooks = build_route_hooks(ProxyRouter.from_client_config(bypass_proxy=profile.bypass_proxy))
    tracking_hooks = build_request_tracking_hooks()
    for event in ("request", "response"):
        event_hooks[event].extend(tracking_hooks[event])
    if raw_http_log_path is not None:
        from chrys.service.llm.raw_http_log import build_raw_http_event_hooks

        raw_hooks = build_raw_http_event_hooks(
            log_path=raw_http_log_path,
            profile=profile,
            session_id=session_id,
        )
        for event in ("request", "response"):
            event_hooks[event] = [*event_hooks[event], *raw_hooks.get(event, ())]
    kwargs["event_hooks"] = event_hooks
    if transport is not None:
        kwargs["transport"] = transport
    return HTTPClient(**kwargs)


def _build_anthropic_sdk_client(
    *,
    api_key: str,
    base_url: str,
    timeout: Any,
    max_retries: int,
    default_headers: dict[str, str] | None,
    http_client: Any | None = None,
) -> AsyncAnthropic:
    """Create a pre-configured ``AsyncAnthropic`` client.

    ``api_key`` and ``base_url`` are passed explicitly when set so the
    profile wins over any process-level env (the SDK falls back to
    ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` only when the kwargs
    are absent).
    """
    from chrys.service.llm.sdk_anthropic import RetryGuardedAsyncAnthropic

    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "max_retries": max_retries,
        "default_headers": default_headers,
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return RetryGuardedAsyncAnthropic(**kwargs)


def _build_openai_sdk_client(
    *,
    api_key: str,
    base_url: str,
    timeout: Any,
    max_retries: int,
    default_headers: dict[str, str] | None,
    http_client: Any | None = None,
    api_key_env: str = "OPENAI_API_KEY",
    base_url_env: str = "OPENAI_BASE_URL",
    default_base_url: str = "",
) -> AsyncOpenAI:
    """Create a pre-configured ``AsyncOpenAI`` client.

    ``api_key`` and ``base_url`` come from the profile when set;
    otherwise they fall back to ``api_key_env`` / ``base_url_env`` from
    the environment, finally to ``default_base_url`` for the base URL.
    OpenAI-compatible providers pass their own env names and default
    base URL, so the SDK reads ``OPENAI_API_KEY`` only for OpenAI itself.
    """
    from chrys.service.llm.sdk_openai import RetryGuardedAsyncOpenAI

    # Newer OpenAI SDKs require the client to receive an api_key argument
    # unless the provider env var is present.  When neither exists, pass an
    # explicit provider that resolves to an empty key.  This avoids
    # construction-time credential errors while preserving no-auth behavior
    # for unauthenticated OpenAI-compatible local/gateway endpoints.
    effective_key: str | Callable[[], Awaitable[str]] = (
        api_key or os.environ.get(api_key_env) or _empty_openai_api_key_provider
    )
    effective_base = base_url or os.environ.get(base_url_env) or default_base_url or None

    kwargs: dict[str, Any] = {
        "api_key": effective_key,
        "timeout": timeout,
        "max_retries": max_retries,
        "default_headers": default_headers,
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    if effective_base:
        kwargs["base_url"] = effective_base
    return RetryGuardedAsyncOpenAI(**kwargs)


async def create_client(
    profile: ModelProfile,
    on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None,
    on_intermediate_text_sync: Callable[[str], None] | None = None,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    use_route_session_context: bool = False,
    session_dir: Path | None = None,
    tool_result_ceiling_tokens: int | None = None,
) -> Any:
    """Create a chat client stack based on the configured ``ModelProfile``.

    The factory rolls back its own failures: once the HTTP client exists,
    any later failure (SDK construction, SDK header validation, stack
    assembly, cancellation) closes it before the error propagates.  After a
    successful return the caller owns the stack and must register
    ``client.aclose`` with its one close owner before its next ``await``.

    Args:
        profile: The ``ModelProfile`` to bind this client to.  Provides
            provider, model id, HTTP transport tuning, headers, and
            credentials.
        on_intermediate_text_async: Async callback for **non-streaming** mode.
            Awaited when the LLM returns text alongside tool calls,
            enabling real-time rendering of intermediate agent messages.
        on_intermediate_text_sync: Sync callback for **streaming** mode.
            Called from a ``result_hook`` after the stream finalizes.
            Must not block — typically stores text in an
            ``IntermediateTextBuffer``.
        session_id: Current session ID for the ``X-Session-ID`` and
            ``Chrys-Session-Id`` headers.
        parent_session_id: Optional parent session ID for sub-agent LLM
            requests. Sent as ``X-Parent-Session-ID`` and
            ``Chrys-Parent-Session-Id``.
        use_route_session_context: Whether the wire client should prefer
            per-invocation route-session ContextVars over its default
            session ids. Intended for shared sub-agent clients.
        session_dir: Optional active session directory. Used by raw HTTP
            logging so tests and custom stores can keep logs beside the
            session artifacts.
        tool_result_ceiling_tokens: Optional kernel backstop for local tool results.

    Returns:
        A chat client stack compatible with Chrys' kernel runtime; closing it
        (``aclose``) closes the provider SDK client and its HTTP pool.
    """
    provider = profile.provider
    # Tool-loop knobs for ToolLoopLayer. Intentional headroom on iterations:
    # long autonomous sessions can chain many tool calls.
    stack_kwargs: dict[str, Any] = {
        "on_intermediate_text_async": on_intermediate_text_async,
        "on_intermediate_text_sync": on_intermediate_text_sync,
        "tool_result_ceiling_tokens": tool_result_ceiling_tokens,
    }
    if provider == "mock":
        from chrys.service.llm.mock import MockChatClient

        return MockChatClient(**stack_kwargs)
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise _unknown_provider(provider)

    api_key = _resolve_profile_api_key(profile)
    headers = _build_default_headers(
        session_id,
        profile,
        parent_session_id=parent_session_id,
        sdk_user_agent=_provider_sdk_user_agent(provider),
    )
    _validate_wire_charset(profile, api_key=api_key, headers=headers)

    import httpx

    timeout = httpx.Timeout(
        connect=profile.http_connect_timeout,
        read=profile.http_read_timeout,
        write=profile.http_read_timeout,
        pool=profile.http_read_timeout,
    )
    from chrys.service.llm.raw_http_log import raw_http_log_path as resolve_raw_http_log_path

    http_client = _build_profile_http_client(
        profile,
        timeout,
        raw_http_log_path=resolve_raw_http_log_path(session_id, session_dir),
        session_id=session_id,
    )
    close_http_client = OnceClose(http_client.aclose)
    stack_kwargs.update(
        model_id=profile.model_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        use_route_session_context=use_route_session_context,
        max_iterations=7777,
        max_consecutive_errors=10,
    )
    try:
        return _build_client_stack(
            profile,
            spec,
            api_key=api_key,
            headers=headers,
            timeout=timeout,
            http_client=http_client,
            stack_kwargs=stack_kwargs,
        )
    except BaseException:
        try:
            await close_http_client()
        except Exception:
            _log.warning("Closing the HTTP client of a failed %s client build failed", provider, exc_info=True)
        raise


def _unknown_provider(provider: str) -> ValueError:
    return ValueError(
        f"Unknown provider: {provider!r}. Use 'anthropic', 'openai', 'deepseek-openai', 'glm-openai', or 'mock'."
    )


def _build_client_stack(
    profile: ModelProfile,
    spec: ProviderSpec,
    *,
    api_key: str,
    headers: dict[str, str],
    timeout: Any,
    http_client: httpx.AsyncClient,
    stack_kwargs: dict[str, Any],
) -> ToolLoopLayer:
    """Build the provider SDK client over *http_client* and wrap it in the Chrys stack."""
    sdk_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": profile.base_url,
        "timeout": timeout,
        "max_retries": profile.http_max_retries,
        "default_headers": headers,
        "http_client": http_client,
    }
    sdk_client: AsyncAnthropic | AsyncOpenAI
    if spec.sdk == "anthropic":
        sdk_client = _build_anthropic_sdk_client(**sdk_kwargs)
    else:
        sdk_client = _build_openai_sdk_client(
            **sdk_kwargs,
            api_key_env=spec.api_key_env,
            base_url_env=spec.base_url_env,
            default_base_url="" if spec.native_sdk else spec.default_base_url,
        )
    _validate_sdk_wire_charset(profile, sdk_client)
    client_cls = _wire_client_class(profile.provider, _api_style(profile, spec))
    return _assemble_stack(client_cls, sdk_client, **stack_kwargs)


def _api_style(profile: ModelProfile, spec: ProviderSpec) -> str | None:
    """The profile's API style, or None for a provider that speaks only one."""
    if spec.api_styles is None:
        return None
    if profile.api_style not in spec.api_styles:
        raise ValueError(
            f"Unknown {spec.label} api_style: {profile.api_style!r}. Use 'chat_completions' or 'responses'."
        )
    return profile.api_style


def _wire_client_class(provider: str, api_style: str | None) -> type[WireClient]:
    """The wire client class for one provider and API style, imported on demand."""
    match provider, api_style:
        case "anthropic", None:
            from chrys.service.llm.anthropic_messages import AnthropicMessagesClient

            return AnthropicMessagesClient
        case "openai", "chat_completions":
            from chrys.service.llm.chat_completions import ChatCompletionsClient

            return ChatCompletionsClient
        case "openai", "responses":
            from chrys.service.llm.openai_responses import ResponsesApiClient

            return ResponsesApiClient
        case "deepseek-openai", "chat_completions":
            from chrys.service.llm.chat_completions import DeepSeekChatCompletionsClient

            return DeepSeekChatCompletionsClient
        case "deepseek-openai", "responses":
            from chrys.service.llm.openai_responses import DeepSeekResponsesApiClient

            return DeepSeekResponsesApiClient
        case "glm-openai", None:
            from chrys.service.llm.chat_completions import GlmChatCompletionsClient

            return GlmChatCompletionsClient
        case _:
            # A provider with a table entry but no wire client: never build
            # another provider's client for it.
            raise _unknown_provider(provider)


def _assemble_stack(
    client_cls: type[WireClient],
    sdk_client: AsyncAnthropic | AsyncOpenAI,
    *,
    model_id: str,
    session_id: str | None,
    parent_session_id: str | None,
    use_route_session_context: bool,
    on_intermediate_text_async: Callable[[str], Awaitable[None]] | None,
    on_intermediate_text_sync: Callable[[str], None] | None,
    max_iterations: int,
    max_consecutive_errors: int,
    tool_result_ceiling_tokens: int | None,
) -> ToolLoopLayer:
    """Wrap the wire client over *sdk_client* in the chrys loop + chat-middleware stack.

    Every call path — the main agent, judges, last-words, recall and
    compression, sub-agents — gets the same request headers, reporting and
    telemetry, whether or not it asks for intermediate text.
    """
    from chrys.kernel import ChatMiddlewareLayer, ToolLoopLayer
    from chrys.service.llm.observer import WireCallObserver
    from chrys.service.llm.wire_client import RequestHeaders

    # Any, not SupportsChatInner: the telemetry layer's get_response names the
    # keywords the middleware layer passes instead of taking **kwargs.
    wire_client: Any = client_cls.from_sdk_client(
        sdk_client,
        model=model_id,
        observer=WireCallObserver(
            on_intermediate_text_async=on_intermediate_text_async,
            on_intermediate_text_sync=on_intermediate_text_sync,
        ),
        request_headers=RequestHeaders(
            session_id=session_id,
            parent_session_id=parent_session_id,
            use_route_session_context=use_route_session_context,
        ),
    )
    return ToolLoopLayer(
        ChatMiddlewareLayer(wire_client),
        max_iterations=max_iterations,
        max_consecutive_errors=max_consecutive_errors,
        tool_result_ceiling_tokens=tool_result_ceiling_tokens,
    )


@asynccontextmanager
async def scoped_client(profile: ModelProfile, **kwargs: Any) -> AsyncIterator[Any]:
    """Create a client stack for one bounded use and close it on every exit."""
    client = await create_client(profile, **kwargs)
    try:
        yield client
    finally:
        await client.aclose()
