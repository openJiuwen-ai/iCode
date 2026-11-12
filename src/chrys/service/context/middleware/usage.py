# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Usage tracking middleware — token usage for both streaming and non-streaming.

``ChatMiddleware.process()`` fires for **each LLM call** within the tool loop
(``ToolLoopLayer`` calls the inner ``ChatMiddlewareLayer`` once per wire
iteration).  This gives per-call usage tracking natively.

For **streaming**, the middleware attaches hooks to the ``ResponseStream`` so
it captures ``Content.usage`` items during iteration and publishes one usage
event when the stream finalizes.

For **non-streaming**, usage is captured directly from the ``ChatResponse``
returned by ``call_next()`` — no monkey-patching needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.trajectory.context import forwarded_exchange_trace
from chrys.kernel import ChatResponse, ResponseStream, normalize_stream_usage
from chrys.kernel.instrumentation import _stream_error_of
from chrys.kernel.middleware import ChatContext, ChatMiddleware
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from chrys.foundation.trajectory.context import ExchangeTrace
    from chrys.kernel import ChatResponseUpdate, Message
    from chrys.service.context.compaction import UnifiedContextStrategy


logger = logging.getLogger(__name__)


class UsageTrackingMiddleware(ChatMiddleware):
    """ChatMiddleware that tracks token usage and calibrates compaction overhead.

    Fires per LLM call within the tool loop:

    **Streaming** — attaches hooks to the ``ResponseStream``:
    * ``transform_hook``: captures ``Content.usage`` items from stream updates.
    * ``result_hook``: publishes one usage event after finalization. Providers
      may emit multiple cumulative usage snapshots during a stream, so firing
      every chunk would double-count session totals.

    **Non-streaming** — captures usage from the ``ChatResponse`` after
    ``call_next()`` completes, and fires the callback immediately.

    When ``compaction_strategy`` is provided, the middleware calls
    ``calibrate()`` so the strategy learns the overhead (system prompt +
    tool definitions) for accurate threshold checks.
    """

    def __init__(
        self,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        warn_threshold_pct: float = 0.50,
        on_usage: Callable[..., Any] | None = None,
        compaction_strategy: UnifiedContextStrategy | None = None,
        use_local_context_estimate_for_hosted_usage: bool = False,
    ):
        self.max_context_tokens = max_context_tokens
        self.warn_threshold_pct = warn_threshold_pct
        self.on_usage = on_usage
        self._compaction_strategy = compaction_strategy
        self._use_local_context_estimate_for_hosted_usage = use_local_context_estimate_for_hosted_usage
        self._last_usage: Mapping[str, Any] | None = None
        self._call_count: int = 0

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        self._call_count += 1

        # Execute the model call.  A response the adapter failed still
        # consumed provider tokens, so count its usage before re-raising to
        # the executor error path or a retry boundary.  A landed response
        # that response validation, above this middleware, rejects is counted
        # like any other.
        # The exchange this call acquires: what its usage says is recorded
        # under it, not under the run the whole tool loop shares.
        exchange = forwarded_exchange_trace(context.kwargs)
        try:
            await call_next()
        except ProviderResponseError as err:
            if err.usage_details:
                self._handle_usage(
                    dict(err.usage_details),
                    use_local_context_estimate=self._should_guard_failed_response_usage(),
                    exchange=exchange,
                )
            raise

        result = context.result

        if context.stream and isinstance(result, ResponseStream):
            stream_usage_chunks: list[dict[str, Any]] = []
            stream_usage_handled = False

            def on_stream_update(update: ChatResponseUpdate) -> ChatResponseUpdate:
                stream_usage_chunks.extend(self._extract_stream_update_usages(update))
                return update

            def on_stream_final(response: ChatResponse) -> ChatResponse:
                nonlocal stream_usage_handled
                stream_usage_handled = True
                return self._on_stream_final(response, stream_usage_chunks=stream_usage_chunks, exchange=exchange)

            def on_stream_cleanup() -> None:
                nonlocal stream_usage_handled
                stream_error = _stream_error_of(result)
                if (
                    not stream_usage_handled
                    and isinstance(stream_error, ProviderResponseError)
                    and stream_error.usage_details
                ):
                    stream_usage_handled = True
                    try:
                        self._handle_usage(
                            dict(stream_error.usage_details),
                            use_local_context_estimate=self._should_guard_failed_response_usage(),
                            exchange=exchange,
                        )
                    except Exception:
                        logger.exception("Failed to report usage from a failed stream")

            # Streaming: attach hooks to intercept per-iteration usage updates
            result.with_transform_hook(on_stream_update)
            result.with_result_hook(on_stream_final)
            # The adapter raises a failed stream during iteration, after this
            # middleware's ``call_next`` frame has returned. Recover the
            # consumed provider usage from that stream error during cleanup.
            result.with_cleanup_hook(on_stream_cleanup)
        # Non-streaming: capture usage directly from the ChatResponse
        elif isinstance(result, ChatResponse) and result.usage_details:
            self._handle_response_usage(result, dict(result.usage_details), exchange=exchange)

    def _extract_stream_update_usages(self, update: ChatResponseUpdate) -> list[dict[str, Any]]:
        """Return usage payloads from a streaming update."""
        return [
            dict(content.usage_details)
            for content in update.contents or []
            if content.type == "usage" and content.usage_details
        ]

    def _on_stream_final(
        self,
        response: ChatResponse,
        *,
        stream_usage_chunks: list[dict[str, Any]] | None = None,
        exchange: ExchangeTrace | None = None,
    ) -> ChatResponse:
        """Result hook — fires after stream finalization.

        Some providers only populate ``response.usage_details`` at stream
        finalization. Others emit one or more ``Content.usage`` stream chunks.
        When chunks are present, the normalized stream usage wins so multiple
        cumulative snapshots collapse into a single per-call payload.
        """
        usage = normalize_stream_usage(stream_usage_chunks or [])
        if usage is not None:
            response.usage_details = usage
            self._handle_response_usage(response, usage, exchange=exchange)
        elif response.usage_details:
            self._handle_response_usage(response, dict(response.usage_details), exchange=exchange)
        return response

    def _handle_response_usage(
        self, response: ChatResponse, usage: Mapping[str, Any], *, exchange: ExchangeTrace | None = None
    ) -> None:
        """Apply the hosted aggregate guard with any provider occupancy view."""
        use_local_context_estimate = self._should_use_local_context_estimate(response)
        self._handle_usage(
            usage,
            use_local_context_estimate=use_local_context_estimate,
            exchange=exchange,
        )

    def _should_use_local_context_estimate(self, response: ChatResponse) -> bool:
        return (
            self._use_local_context_estimate_for_hosted_usage
            and self._compaction_strategy is not None
            and contains_provider_hosted_content(response.messages)
        )

    def _should_guard_failed_response_usage(self) -> bool:
        """Return the conservative calibration guard for failed responses.

        Adapter errors retain usage but not a reliable assembled response
        shape, so a Responses dialect configured for hosted aggregate usage
        must not calibrate from that provider input count.
        """
        return self._use_local_context_estimate_for_hosted_usage and self._compaction_strategy is not None

    def _handle_usage(
        self,
        usage: Mapping[str, Any],
        *,
        use_local_context_estimate: bool = False,
        context_input_tokens: int | None = None,
        exchange: ExchangeTrace | None = None,
    ) -> None:
        """Process captured usage details of the call that *exchange* acquired.

        Calibrates BEFORE firing so system overhead is available in the event.
        """
        self._last_usage = usage
        if not use_local_context_estimate:
            self._calibrate_strategy(usage, exchange)
        elif context_input_tokens is None and self._compaction_strategy is not None:
            context_input_tokens = resolve_hosted_context_input_tokens(
                usage,
                local_estimate=self._compaction_strategy.estimated_context_input_tokens,
            )
        self._fire_callback(
            usage,
            use_local_context_estimate=use_local_context_estimate,
            context_input_tokens=context_input_tokens,
        )

    def _fire_callback(
        self,
        usage: Mapping[str, Any],
        *,
        use_local_context_estimate: bool = False,
        context_input_tokens: int | None = None,
    ) -> None:
        """Notify the frontend of updated usage."""
        if self.on_usage is None:
            return
        input_tokens = usage.get("input_token_count") or 0
        output_tokens = usage.get("output_token_count") or 0
        total = usage.get("total_token_count") or (input_tokens + output_tokens)
        cache_hit_tokens = extract_cache_hit_tokens(usage)
        local_tokens = 0
        calibration_ratio = 1.0
        system_overhead = 0
        calibration_initialized = False
        if self._compaction_strategy:
            s = self._compaction_strategy
            calibration_ratio = s.calibration_ratio
            system_overhead = s.system_overhead_tokens
            calibration_initialized = s.calibration_initialized
            # Report the calibrated estimate so `local` tracks the API count
            # instead of diverging due to heuristic tokenizer bias.
            local_tokens = (
                context_input_tokens
                if use_local_context_estimate and context_input_tokens is not None
                else s.estimated_context_input_tokens
                if use_local_context_estimate
                else int((s.last_included_tokens + system_overhead) * calibration_ratio)
            )
        self.on_usage(
            total,
            input_tokens,
            output_tokens,
            local_tokens,
            calibration_ratio,
            system_overhead,
            cache_hit_tokens,
            calibration_initialized,
            use_local_context_estimate,
        )

    def _calibrate_strategy(self, usage: Mapping[str, Any], exchange: ExchangeTrace | None = None) -> None:
        """Let the compaction strategy learn overhead from the API response.

        Uses ``_last_included_tokens`` (set by the strategy's ``__call__``
        on the same iteration) as the local baseline.  This ensures the
        calibration ratio scales the same value that ``_usage_pct`` uses,
        preventing mismatch between different token counting methods.
        """
        if not self._compaction_strategy:
            return
        api_input = usage.get("input_token_count") or 0
        self._compaction_strategy.calibrate(api_input)
        # The same provider count says how much of the previous request this
        # one read from the cache; a hosted aggregate (skipped by the caller)
        # says neither.
        cache_read = usage.get("cache_read_input_token_count")
        if cache_read is None:
            cache_read = extract_cache_hit_tokens(usage)
        self._compaction_strategy.observe_response_usage(
            input_tokens=api_input,
            cache_read_tokens=int(cache_read) if cache_read is not None else 0,
            exchange=exchange,
        )


def contains_provider_hosted_content(messages: Sequence[Message]) -> bool:
    """Return whether messages contain provider-executed tool content."""
    return any(content.provider_hosted for message in messages for content in message.contents)


def extract_context_input_tokens(usage: Mapping[str, Any]) -> int | None:
    """Return a provider-derived final context occupancy when available."""
    value = usage.get("context_input_token_count")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def resolve_hosted_context_input_tokens(usage: Mapping[str, Any], *, local_estimate: int) -> int:
    """Resolve hosted context occupancy from an exact value, floor, or local estimate."""
    if (exact := extract_context_input_tokens(usage)) is not None:
        return exact
    estimate = usage.get("context_input_token_estimate")
    if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0:
        return max(local_estimate, estimate)
    floor = usage.get("context_input_token_floor")
    if isinstance(floor, int) and not isinstance(floor, bool) and floor > 0:
        return max(local_estimate, floor)
    return local_estimate


# Provider-namespaced cache-read keys in Chrys UsageDetails, in priority
# order. First match wins — providers may mirror their
# native field into the OpenAI-compatible ``prompt/cached_tokens`` slot
# (DeepSeek does this), so summing would double-count. Cache *write* /
# creation fields are intentionally excluded — they're billable but not
# hits.
#
# Ordering constraint: every provider-specific key MUST come before
# ``prompt/cached_tokens`` so a mirror doesn't shadow the authoritative
# native value. When adding a new provider, append its key above the
# generic Chat Completions key, not after it.
_CACHE_HIT_KEYS: tuple[str, ...] = (
    "anthropic.cache_read_input_tokens",
    "deepseek.prompt_cache_hit_tokens",
    "openai.cached_input_tokens",
    "prompt/cached_tokens",
)


def extract_cache_hit_tokens(usage: Mapping[str, Any]) -> int | None:
    """Return cache-read tokens from the first reporting provider, or ``None``.

    Returns ``None`` when no known cache field is present so the TUI can
    distinguish "provider did not report" from "provider reported 0".
    """
    for key in _CACHE_HIT_KEYS:
        value = usage.get(key)
        if value is not None:
            return int(value)
    return None
