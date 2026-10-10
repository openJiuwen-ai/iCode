# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Response validators for :class:`ResponseValidationMiddleware`.

Defines a small protocol + a regex-driven default validator that detects
common LLM / inference-service misbehaviours that would otherwise cause
the agent loop to terminate with a nonsense final response:

1. **Empty contents** — the final assistant message has ``contents == []``.
2. **Whitespace-only text** — the final assistant message has text content
   but every text item is empty or whitespace (``""``, ``"\\n\\n\\n"``, etc.),
   AND there are no tool calls in the same message.  (A tool-calling turn
   with no text is perfectly valid and NOT flagged.)
3. **Leaked tool-call markers in text** — provider/model leaked the raw
   tool-call tokens into the text channel instead of emitting them as
   ``function_call`` content.  Covers ``minimax:tool_call`` (MiniMax via
   vLLM), ``<tool_use>``, ``<function_call>``, ``<|tool_call|>`` and
   variations.  Text before or after the marker still triggers.
4. **Evidence gathering without a final answer** — provider-hosted search,
   fetch, or tool discovery completed after the last visible text item and
   the response stopped without a later answer or answer-bearing output.
5. **Filtered without an answer** — the provider's content filter ended the
   response before its answer. The same request meets the same filter, so
   this is terminal, never retried.
6. **Tool call without a function name** — the provider sent a call that
   names no function, which no tool can run. A fresh sample usually names
   one; when every attempt sends such a call, the response fails.

Validation runs on the **final message** of a :class:`ChatResponse` —
the message that would become the agent's final reply for the turn.
Earlier messages in the response are assumed to be tool results or
intermediate text that FunctionInvocationLayer handles separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from chrys.foundation.hosted_tools import (
    HostedToolFamily,
    HostedToolPhase,
    HostedToolStatus,
    normalize_hosted_tool_status,
)
from chrys.foundation.trajectory.event_types import ValidationReason
from chrys.kernel.exchanges import TOOL_CALL_CONTENT_TYPES, TOOL_RESULT_CONTENT_TYPES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.kernel import ChatResponse, Message


# ---------------------------------------------------------------------------
# Protocol + result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a single :class:`ChatResponse`.

    ``ok`` is the terminal signal for the middleware.  ``reason`` is a
    short human-readable string surfaced in ``RetryAttempt`` events and
    the debug log so the user can see *why* we retried.  ``retryable``
    is ``False`` when re-issuing the same request cannot help (e.g. the
    response hit the output token limit with no content generated); the
    middleware then gives up immediately instead of burning retries.
    ``terminal_on_giveup`` marks retryable failures whose exhausted
    response has nothing salvageable to return (e.g. reasoning-only
    output with no user-visible answer): when retries run out, the
    middleware raises the reason through the executor error path instead
    of returning a blank final response the caller would mistake for a
    successful (empty) answer.
    """

    ok: bool
    reason: str = ""
    retryable: bool = True
    terminal_on_giveup: bool = False
    code: str = field(default=ValidationReason.UNKNOWN, compare=False)
    """Closed reason code for analytics (the free-text ``reason`` never leaves the UI/log).

    Excluded from equality: it is a classification of ``reason``, not a second verdict.
    """

    @classmethod
    def valid(cls) -> ValidationResult:
        return cls(ok=True)

    @classmethod
    def invalid(
        cls,
        reason: str,
        *,
        code: str = ValidationReason.UNKNOWN,
        retryable: bool = True,
        terminal_on_giveup: bool = False,
    ) -> ValidationResult:
        return cls(ok=False, reason=reason, retryable=retryable, terminal_on_giveup=terminal_on_giveup, code=code)


# Surfaced (verbatim) when the model produced no usable output because it hit
# the output token limit — almost always the input filling the context window
# (or the model profile's Max Output Tokens set too small). Retrying re-sends
# the same oversized input and gets the same empty result, so this reason is
# marked non-retryable.
OUTPUT_TRUNCATED_REASON = (
    "Response hit the output token limit with no content generated — the input likely "
    "fills the model's context window (or the model profile's Max Output Tokens is too small), "
    "leaving no room to reply. Try reducing the input or raising the model profile's "
    "Max Output Tokens setting."
)

NO_VISIBLE_OUTPUT_REASON = (
    "Response contained reasoning content but no visible answer generated — the model stopped before producing "
    "user-visible text or a tool call."
)

HOSTED_EVIDENCE_MISSING_FINAL_TEXT_REASON = (
    "Response ended after provider-hosted evidence gathering without generating a final user-visible answer."
)

CONTENT_FILTERED_REASON = "The model service's content filter stopped the response before it generated an answer."

# Fixed text: retries compare reasons, and a call's id or index differs on
# every attempt.
UNNAMED_TOOL_CALL_REASON = "Response contained a tool call without a function name."

_EVIDENCE_ONLY_HOSTED_FAMILIES = {
    HostedToolFamily.SEARCH,
    HostedToolFamily.FETCH,
    HostedToolFamily.TOOL_DISCOVERY,
}

# Surfaced when the model burned its whole output budget on reasoning without
# ever producing visible text or a tool call (``finish_reason="length"`` with
# reasoning content present).  Unlike :data:`OUTPUT_TRUNCATED_REASON`, the
# generated reasoning tokens prove the input did NOT fill the context window —
# the model over-thought — so a fresh sample plausibly yields a visible answer
# and the failure is retryable.
REASONING_EXHAUSTED_OUTPUT_REASON = (
    "Response hit the output token limit while reasoning, with no visible answer generated — the model spent "
    "its output budget thinking and stopped before producing user-visible text or a tool call. If this "
    "persists, raise the model profile's Max Output Tokens setting."
)


class ResponseValidator(Protocol):
    """Protocol for pluggable response validators."""

    def validate(self, response: ChatResponse) -> ValidationResult:
        """Return :class:`ValidationResult.valid` if *response* is acceptable."""
        ...


# ---------------------------------------------------------------------------
# Built-in regex rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegexRule:
    """A single text-content regex check.

    ``pattern`` is searched (``re.search``) against each ``TextContent.text``
    in the final message.  A match flags the response as invalid with
    *reason*.
    """

    name: str
    pattern: re.Pattern[str]
    reason: str


# Patterns for known leaked tool-call markers.  Case-insensitive because
# providers vary (``minimax:tool_call`` is lowercase, some emit upper).
_LEAKED_TOOL_CALL_PATTERNS = [
    r"minimax:tool_call",
    r"</\s*minimax:tool_call\s*>",
    r"<\s*tool_use\b",
    r"</\s*tool_use\s*>",
    r"<\s*function_call\b",
    r"</\s*function_call\s*>",
    r"<\|\s*tool_call\s*\|>",
    r"<\|\s*tool_use\s*\|>",
]

DEFAULT_LEAKED_TOOL_CALL_RULE = RegexRule(
    name="leaked_tool_call",
    pattern=re.compile("|".join(_LEAKED_TOOL_CALL_PATTERNS), re.IGNORECASE),
    reason="leaked tool-call marker in text content",
)


# ---------------------------------------------------------------------------
# Default validator
# ---------------------------------------------------------------------------


@dataclass
class DefaultResponseValidator:
    """Validator that combines the three built-in rules plus optional extras.

    Behaviour:

    - **Filtered without an answer** is reported in place of the rule that
      would read the response as blank or answerless and retry it; a
      filtered answer is reported in place of a text rule it breaks.
    - **Empty contents** is always checked first (cheapest).
    - **Whitespace-only text** runs only when the message has NO tool calls
      (a pure tool-calling turn can legitimately have no text).
    - **Leaked tool-call markers** always runs on text contents — a message
      with tool calls whose text ALSO has a leaked marker is still invalid.
    - **Tool call without a function name** fails the response before the
      rules that read its other contents.
    - ``extra_rules`` are applied in order after the built-ins.  Configuring
      an empty list + setting ``disable_*`` flags to ``True`` yields a no-op
      validator that always returns ``valid`` — useful for tests.
    """

    extra_rules: Sequence[RegexRule] = field(default_factory=list)
    disable_empty_contents: bool = False
    disable_whitespace_text: bool = False
    disable_leaked_tool_call: bool = False
    disable_unnamed_tool_call: bool = False

    def validate(self, response: ChatResponse) -> ValidationResult:
        msg = _final_assistant_message(response)
        # A content filter that stopped the response is terminal: the same
        # request meets the same filter. Each rule below that would retry the
        # response, as blank, answerless or with text it rejects, reports the
        # filter instead where it fires.
        content_filtered = response.finish_reason == "content_filter"
        if msg is None:
            if content_filtered:
                return _content_filtered()
            # No message at all — empty response from the provider.
            return ValidationResult.invalid(
                "no assistant message in response", code=ValidationReason.NO_ASSISTANT_MESSAGE
            )

        # A "length" finish_reason on an otherwise-empty final message means the
        # model ran out of output budget before emitting any visible content.
        # With NO generated content at all that almost always means the input
        # filled the context window — retrying the same request is futile, so
        # the empty-output rules below surface it as a terminal, non-retryable
        # failure with an actionable reason instead of the opaque generic
        # emptiness message.  When reasoning content IS present the budget was
        # spent thinking (the context cannot be full), so the same signal is
        # retryable — see Rule 2.
        output_truncated = response.finish_reason == "length"

        # Rule 1: empty contents=[]
        if not self.disable_empty_contents and not msg.contents:
            if content_filtered:
                return _content_filtered()
            if output_truncated:
                return ValidationResult.invalid(
                    OUTPUT_TRUNCATED_REASON, code=ValidationReason.OUTPUT_TRUNCATED, retryable=False
                )
            return ValidationResult.invalid("empty contents", code=ValidationReason.EMPTY_CONTENTS)

        # Collect text + tool-call presence for the remaining rules.
        text_items: list[str] = []
        has_usable_output = False
        has_local_function_call = False
        has_unnamed_function_call = False
        has_reasoning_content = False
        last_visible_text_index = -1
        last_answer_bearing_output_index = -1
        last_terminal_evidence_index = -1
        for index, content in enumerate(msg.contents):
            if content.type == "text":
                text_items.append(content.text or "")
                if (content.text or "").strip():
                    last_visible_text_index = index
            elif content.type == "function_call" and not content.provider_hosted and not content.informational_only:
                has_usable_output = True
                has_local_function_call = True
                has_unnamed_function_call = has_unnamed_function_call or not content.name
                last_answer_bearing_output_index = index
            elif content.type == "function_call" and content.informational_only and not content.provider_hosted:
                # Responses custom_tool_call items are preserved as
                # non-executable transcript calls. They are still real model
                # output, so a call-only response must not be retried as blank.
                has_usable_output = True
                last_answer_bearing_output_index = index
            elif content.provider_hosted or content.type in (TOOL_CALL_CONTENT_TYPES | TOOL_RESULT_CONTENT_TYPES) - {
                "function_call",
                "function_result",
            }:
                status = normalize_hosted_tool_status(content.provider_status or content.status)
                if content.provider_phase == HostedToolPhase.TERMINAL or status in {
                    HostedToolStatus.COMPLETED,
                    HostedToolStatus.FAILED,
                    HostedToolStatus.INTERRUPTED,
                }:
                    has_usable_output = True
                    if content.hosted_family in _EVIDENCE_ONLY_HOSTED_FAMILIES:
                        last_terminal_evidence_index = index
                    else:
                        last_answer_bearing_output_index = index
            elif content.type == "text_reasoning":
                has_reasoning_content = has_reasoning_content or bool(
                    (content.text or "").strip() or content.protected_data
                )

        # The tool loop could answer such a call only as an unknown tool, and
        # nothing else in the response may stand without it. A fresh sample
        # usually names its function; when none does there is nothing to keep.
        if not self.disable_unnamed_tool_call and has_unnamed_function_call:
            if content_filtered:
                return _content_filtered()
            return ValidationResult.invalid(
                UNNAMED_TOOL_CALL_REASON, code=ValidationReason.UNNAMED_TOOL_CALL, terminal_on_giveup=True
            )

        # An answer is visible text, a call, or a hosted output that is an
        # answer itself, after the last hosted evidence gathering.
        if (
            content_filtered
            and not self.disable_whitespace_text
            and not has_local_function_call
            and max(last_visible_text_index, last_answer_bearing_output_index) <= last_terminal_evidence_index
        ):
            return _content_filtered()

        # Hosted search, fetch, and tool discovery are evidence gathering,
        # not the answer to a text request. Providers normally emit a message
        # or another tool call afterwards; accepting a terminal result or a
        # terminal call-only snapshot turns an omitted trailing answer into a
        # blank agent reply. Other terminal hosted outputs (for example an
        # image result) remain valid answers in their own right.
        if (
            not self.disable_whitespace_text
            and not has_local_function_call
            and last_terminal_evidence_index > max(last_visible_text_index, last_answer_bearing_output_index)
        ):
            return ValidationResult.invalid(
                HOSTED_EVIDENCE_MISSING_FINAL_TEXT_REASON,
                code=ValidationReason.HOSTED_EVIDENCE_ONLY,
                terminal_on_giveup=True,
            )

        # Rule 2: whitespace-only text (only when no tool calls).
        if not self.disable_whitespace_text and not has_usable_output:
            joined = "".join(text_items)
            if not joined.strip():
                if has_reasoning_content:
                    # The model DID generate output — its reasoning — so the
                    # input cannot have filled the context window.  This is the
                    # "over-thinking" failure mode: the reply budget went to
                    # hidden reasoning (or the model simply stopped) before any
                    # visible text or tool call, and a re-roll plausibly
                    # recovers.  Retry like the other transient shapes; if
                    # every attempt ends the same way there is still nothing
                    # visible to return, so the give-up must fail through the
                    # executor error path instead of yielding a blank final
                    # message (``terminal_on_giveup``).
                    reason = REASONING_EXHAUSTED_OUTPUT_REASON if output_truncated else NO_VISIBLE_OUTPUT_REASON
                    return ValidationResult.invalid(
                        reason, code=ValidationReason.REASONING_ONLY, terminal_on_giveup=True
                    )
                if output_truncated:
                    return ValidationResult.invalid(
                        OUTPUT_TRUNCATED_REASON, code=ValidationReason.OUTPUT_TRUNCATED, retryable=False
                    )
                return ValidationResult.invalid(
                    "empty or whitespace-only text response", code=ValidationReason.WHITESPACE_ONLY
                )

        # Rule 3: leaked tool-call markers — check each text item.
        if not self.disable_leaked_tool_call:
            rule = DEFAULT_LEAKED_TOOL_CALL_RULE
            for text in text_items:
                if text and rule.pattern.search(text):
                    if content_filtered:
                        return _content_filtered()
                    return ValidationResult.invalid(rule.reason, code=ValidationReason.LEAKED_TOOL_CALL)

        # Extra rules — run on concatenated text, one rule at a time.
        if self.extra_rules:
            joined = "".join(text_items)
            for rule in self.extra_rules:
                if joined and rule.pattern.search(joined):
                    if content_filtered:
                        return _content_filtered()
                    return ValidationResult.invalid(rule.reason, code=ValidationReason.RULE_VIOLATION)

        return ValidationResult.valid()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_filtered() -> ValidationResult:
    return ValidationResult.invalid(CONTENT_FILTERED_REASON, code=ValidationReason.CONTENT_FILTERED, retryable=False)


def _final_assistant_message(response: ChatResponse) -> Message | None:
    """Return the last assistant message, or the last message regardless of role.

    Most providers put a single assistant message in :attr:`ChatResponse.messages`,
    but some wrap tool results and intermediate messages in the same response.
    The final agent reply is the last assistant message; fall back to the last
    message if no assistant role is present (defensive — also flags as empty
    via Rule 1 when contents are missing).
    """
    messages = response.messages or []
    for msg in reversed(messages):
        if msg.role == "assistant":
            return msg
    return messages[-1] if messages else None
