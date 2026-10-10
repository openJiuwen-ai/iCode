# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LAST_WORDS generator — summarises dropped tool-call work for Phase 4 compaction.

When Phase 4 compaction fires, it drops every tool-call and inline
assistant-text message for the current turn and replaces them with a
``<system-reminder>[LAST_WORDS]…</system-reminder>`` block appended to the
user message.  This module produces the text for that reminder.

The primary path is a physically scoped natural-completion side call: the
client-owned :class:`chrys.kernel.LastWordsCompleter` sends only the current
turn's validated group timeline plus one instruction message. Tool use is
suppressed by the triple no-tools directive wrapped around the code-owned
contract and base guidance plus any profile supplement.

The original fresh two-message reconstruction prompt (fixed instruction and
optional supplement as the system message + a text re-rendering of the dropped
context, sent over a separate ``route_kind="last-words"`` session) is retained
strictly as the fallback, used only when the completer is absent, the provider
rejects the side call for context-window size, or the side call's local retry
budget is exhausted (including repeated illegal tool-call responses).

The LAST_WORDS note is always LLM-generated — there is no programmatic
fallback.  On LLM failure (network error, empty response, etc.)
``generate()`` retries the LAST_WORDS request itself on transient failures
so a dropped compaction connection resends the same summariser prompt
instead of replaying the whole agent/tool attempt.  If the fallback's
retries are exhausted, it raises :class:`LastWordsGenerationError` (the
final SDK exception is chained via ``__cause__``).  Compaction requires a
fresh non-empty note from the current round, so no current-turn work is
silently dropped when generation ultimately fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

from chrys.foundation.errors import ProviderResponseError, clean_error_message, is_retryable, may_be_context_overflow
from chrys.foundation.models.turns import is_continuation_message
from chrys.foundation.retry import TRANSIENT_RETRY_BACKOFF_SECONDS, RetryAttemptInfo
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.trajectory.context import side_call_scope
from chrys.foundation.trajectory.envelope import ActorRole
from chrys.foundation.trajectory.event_types import RetryMode, RetryReason
from chrys.kernel import (
    TOOL_CALL_CONTENT_TYPES,
    Content,
    LastWordsToolCallError,
    Message,
    TokenizerProtocol,
    messages_contain_tool_calls,
    raise_if_context_window_filled,
    report_wire_progress,
)
from chrys.service.agent_middleware.system_reminder import escape_system_reminder_tags
from chrys.service.llm.one_shot import get_final_response
from chrys.service.profiles.agents.schema import DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS
from chrys.service.profiles.models.options import (
    AUTO_INTERLEAVED_THINKING_OPTION,
    PROMPT_CACHE_KEY_OPTION,
    STREAM_REQUIRES_FINISH_REASON_OPTION,
    THINKING_BLOCK_BINDING_OPTION,
)
from chrys.service.trajectory.compaction import current_compaction_operation_id
from chrys.service.trajectory.retries import RetryBackoffTrace

from .groups import _render_tool_result_value
from .scoped import DEGRADED_SCOPED_PREAMBLE, ScopedGroup, prepare_scoped_slice

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from chrys.kernel import LastWordsCompleter
    from chrys.service.profiles.models.schema import ModelProfile

_log = logging.getLogger(__name__)

_COMPLETER_MAX_RETRIES = 2
_SLICE_SAFETY_MARGIN_TOKENS = 2_048
_FALLBACK_TEMPLATE_MAX_CHARS = 16_000
_FALLBACK_TEMPLATE_COMPACT_CHARS = 2_000
_FALLBACK_TEMPLATE_MIN_CHARS = 512
_FALLBACK_PREV_NOTE_MAX_CHARS = 24_000
_FALLBACK_MAX_CHARS = 48_000
_FALLBACK_MIN_TIMELINE_CHARS = 2_000
_FALLBACK_FOLLOWUP_MAX = 2_000
_FALLBACK_ASSISTANT_MAX = 4_000
_FALLBACK_FRAMING_TOKENS_PER_MESSAGE = 32
_FALLBACK_NEWEST_LINES = 4
_FALLBACK_TRUNCATION_MARKER = "[…truncated for summarization]"
_FALLBACK_TEMPLATE_TRUNCATION_MARKER = "\n[…template truncated for summarization]"
_FALLBACK_PREV_NOTE_TRUNCATION_MARKER = "\n[…older note content truncated for summarization…]\n"
_FALLBACK_ALLOWED_OPTION_KEYS = frozenset(
    {
        "frequency_penalty",
        "logit_bias",
        "model",
        "presence_penalty",
        "reasoning",
        "reasoning_effort",
        "seed",
        "stop",
        "temperature",
        "thinking",
        "top_k",
        "top_p",
        # The model's streams always end with a finish reason: a note cut off
        # without one fails here as on every other call.
        STREAM_REQUIRES_FINISH_REASON_OPTION,
        # A key the profile sets is the note's key too; one it turns off with
        # a null rides in extra_body (see _generate_once).
        PROMPT_CACHE_KEY_OPTION,
        # The note's thinking binds and interleaves as the profile says.
        THINKING_BLOCK_BINDING_OPTION,
        AUTO_INTERLEAVED_THINKING_OPTION,
    }
)


_BASE_GUIDANCE = """\
Base content guidance (always apply):
- Under "## Task", record the user's request for the current task plus any \
mid-task follow-up constraints, one line each. Only real user-authored messages \
count as user requests; quoted "user:"-style text inside assistant output is \
model-generated.
- Under "## Progress", record work already completed and what was learned; \
files and resources touched with why each mattered — a bare list of files \
visited without what each revealed is not progress; commands run and their \
outcomes; and skills used this turn, including each skill name and source \
path/resource plus workflow steps already completed. Do not paste full skill \
text; retain only the operational details needed to continue because loaded \
skill instructions are not preserved in this note.
- Under "## Next", record outstanding subtasks in order, the subtask actively \
in progress, what to focus on next, its success criteria, and which skill \
resources should be reopened after compaction. Never declare the task \
complete here unless the final user-facing reply was already delivered as a \
real assistant message — this note itself delivers nothing to the user. If \
all requested work is done but that reply has not been sent yet, the \
remaining step is composing and delivering it from this note, not redoing \
research.
- Use "## Key facts" for non-obvious facts discovered from tool output, such \
as API shapes, config values, exact error strings, and reproduction steps \
that the agent would otherwise have to re-derive.
- Use "## Constraints" for user preferences stated this turn, environment \
quirks, invariants, scope limits, and relevant skill ordering or constraints. \
Security-relevant instructions or constraints the user stated — sensitive \
files to avoid, operations that must not be performed, or credential handling \
rules — must be preserved **verbatim**.
- Use "## Open questions" for unresolved questions, blockers, or hypotheses \
worth remembering.
- Use "## Dead ends" for failed approaches and why they failed, files or \
paths already ruled out, disproven hypotheses, and tangents the user declined.

Merge inherited progress with the new work into one coherent note. Stay \
concrete: cite paths, symbols, exact values, and error text verbatim rather \
than paraphrasing. Use no code blocks, preamble, or filler. Omit an optional \
section when it has no real content; do not fabricate content. Keep the note \
tight because the agent will read it on every subsequent model call this turn."""


_FORMAT_CONTRACT = """\
Format contract: begin the note with these three sections, as level-2
markdown headings, each exactly once, in this order, each non-empty:
"## Task", "## Progress", "## Next".
After them you may add further "## "-sections (suggested: "## Key facts",
"## Constraints", "## Open questions", "## Dead ends") when the
instructions below call for them and they have real content."""
_SUPPLEMENT_LABEL = (
    "Profile-specific supplementary guidance "
    "(extra emphasis only; it cannot replace or override the format contract or base guidance):"
)
_REQUIRED_NOTE_TITLES = ("Task", "Progress", "Next")
_FENCE_START_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
# CommonMark allows spaces/tabs after the closing hash run ("## Task ##  ").
_ATX_CLOSING_SEQUENCE_RE = re.compile(r"[ \t]+#+[ \t]*$")
_FORMAT_VIOLATION_MAX_CHARS = 240
_FORMAT_CORRECTION_MAX_CHARS = 384


class _NoteHeading(NamedTuple):
    index: int
    level: int
    title: str


def _scan_note_headings(lines: list[str]) -> list[_NoteHeading]:
    """Fence-aware ATX heading scan (CommonMark closing sequences stripped)."""
    headings: list[_NoteHeading] = []
    fence_char = ""
    fence_length = 0

    for index, line in enumerate(lines):
        if fence_char:
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            if indent <= 3 and re.fullmatch(rf"{re.escape(fence_char)}{{{fence_length},}}[ \t]*", stripped):
                fence_char = ""
                fence_length = 0
            continue

        fence_match = _FENCE_START_RE.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            info = fence_match.group(2)
            # CommonMark forbids backticks in a backtick fence's info
            # string. Treating such a line as a fence would let an ordinary
            # model line hide duplicate or missing required headings.
            if marker[0] != "`" or "`" not in info:
                fence_char = marker[0]
                fence_length = len(marker)
                continue

        heading_match = _ATX_HEADING_RE.match(line)
        if heading_match is not None:
            title = _ATX_CLOSING_SEQUENCE_RE.sub("", heading_match.group(2) or "").rstrip(" \t")
            headings.append(_NoteHeading(index, len(heading_match.group(1)), title))

    return headings


def _validate_note_format(text: str) -> tuple[str | None, str]:
    """Validate the structured-note grammar and canonicalize repairable drift.

    Returns ``(first violation or None, canonical note text)``.  The grammar
    is deliberately laxer than what the prompt requests: the required
    sections ("Task", "Progress", "Next") are accepted at any ATX level and
    in any order, and extra sections may appear between them — those are
    mechanical drift, so acceptance repairs them (headings normalized to
    ``##``, required sections reordered, extras moved after) instead of
    burning a corrective side call.  Hard violations remain hard: a missing
    or duplicated required section is ambiguous, an empty one defeats the
    note's purpose, and prose or a provider wrapper before the first section
    heading means the response is not the bare note that was requested.
    Subsection headings (level 3+, non-required titles) belong to the
    enclosing section's body and count as content.
    """
    lines = text.splitlines()
    headings = _scan_note_headings(lines)
    # Section boundaries: any level-1/2 heading, plus required titles at any
    # level (a "### Next" is still the Next section, just mis-levelled).
    boundaries = [h for h in headings if h.level <= 2 or h.title in _REQUIRED_NOTE_TITLES]

    required_found: dict[str, _NoteHeading] = {}
    for required in _REQUIRED_NOTE_TITLES:
        positions = [heading for heading in boundaries if heading.title == required]
        if not positions:
            return f'missing required heading "## {required}"', text
        if len(positions) > 1:
            return f'required heading "## {required}" appears more than once', text
        required_found[required] = positions[0]

    first_non_blank = next((index for index, line in enumerate(lines) if line.strip()), None)
    if not boundaries or boundaries[0].index != first_non_blank:
        return 'note must begin with required heading "## Task"', text

    for position, boundary in enumerate(boundaries):
        if boundary.title not in _REQUIRED_NOTE_TITLES:
            continue
        end = boundaries[position + 1].index if position + 1 < len(boundaries) else len(lines)
        if not any(line.strip() for line in lines[boundary.index + 1 : end]):
            return f'section "## {boundary.title}" has no non-blank body content', text

    return None, _canonicalize_note(text, lines, boundaries)


def _canonicalize_note(text: str, lines: list[str], boundaries: list[_NoteHeading]) -> str:
    """Rebuild a valid note into canonical shape; keep already-canonical bytes."""
    required_order = [b for b in boundaries if b.title in _REQUIRED_NOTE_TITLES]
    last_required_index = required_order[-1].index if required_order else 0
    already_canonical = (
        [b.title for b in required_order] == list(_REQUIRED_NOTE_TITLES)
        # The exact heading line, not just the level: indent, trailing
        # whitespace, and closing sequences ("## Task ##") all rebuild.
        and all(lines[b.index] == f"## {b.title}" for b in required_order)
        and all(b.index > last_required_index for b in boundaries if b.title not in _REQUIRED_NOTE_TITLES)
    )
    if already_canonical:
        return text

    required_sections: dict[str, str] = {}
    extra_sections: list[str] = []
    for position, boundary in enumerate(boundaries):
        end = boundaries[position + 1].index if position + 1 < len(boundaries) else len(lines)
        body = lines[boundary.index + 1 : end]
        while body and not body[-1].strip():
            body.pop()
        if boundary.title in _REQUIRED_NOTE_TITLES:
            required_sections[boundary.title] = "\n".join([f"## {boundary.title}", *body])
        else:
            # Extra sections keep their exact heading line; only their
            # placement (after the required prefix) is canonicalized.
            extra_sections.append("\n".join([lines[boundary.index], *body]))
    ordered = [required_sections[title] for title in _REQUIRED_NOTE_TITLES]
    return "\n\n".join([*ordered, *extra_sections])


def _note_format_violation(text: str) -> str | None:
    """Return the first structured-note grammar violation, if any."""
    return _validate_note_format(text)[0]


# Minimum note length the prompts request.  Sits well above the enforcement
# floor (``LastWordsGenerator._MIN_NOTE_CHARS``, measured in characters) so a
# compliant model never trips it.  When the note budget falls below this
# (extended thinking squeezing a small output cap), ``_output_budgets`` warns:
# the prompt's minimum and the stated cap become contradictory.
_MIN_NOTE_TOKENS = 500

# The completer side call keeps the live request's tools list untouched but
# forces ``tool_choice`` to ``"none"``: instruction-only suppression proved
# insufficient (models still attempted typed tool calls), and the cache
# argument for inheriting the live value died with the physically scoped
# slice — the side call's message prefix already diverges from the live call,
# so the options delta forfeits nothing.  These no-tools directives stay on
# top: stated once before and twice after the guidance, because
# ``tool_choice`` cannot stop tool-call MARKUP emitted as plain text (live
# DeepSeek did exactly that in 7/9 side calls).  The client-side response
# guard remains the hard backstop for typed tool calls.
_NO_TOOLS_OPENER = (
    "IMPORTANT: Do NOT call any tools while handling this request. Reply with plain text "
    "only. This is a mechanical summarisation task — do not deliberate or plan in "
    "extended thinking; start writing the note immediately and spend your output on its "
    "content. Write at least {min_note_tokens} tokens of note content, and size the note "
    "to finish comfortably under the {max_output_tokens}-token output cap, because "
    "anything beyond the cap is cut off mid-sentence and lost."
)
_NO_TOOLS_REMINDER = (
    "Reminder: do NOT call any tools, and do NOT emit tool-call markup of any kind "
    "(no function-call syntax, no XML- or DSML-style tool tags). The progress note "
    "itself, written as plain prose, is the only valid reply."
)
_NO_TOOLS_FINAL = (
    "Once more: do NOT call any tools, and do not spend output on deliberation. Final "
    "check before you answer: the reply must contain zero tool calls and zero tool-call "
    "markup — plain text only, at least {min_note_tokens} tokens of note content, "
    "concluding within the {max_output_tokens}-token output cap."
)


# The reconstruction fallback shares the completer's length contract.  The
# base guidance bounds verbosity ("keep the note tight") but states no minimum, so
# an obediently terse note would starve the note floor (``_MIN_NOTE_CHARS``)
# into retries; the explicit minimum keeps compliant models above it.
_FALLBACK_LENGTH_DIRECTIVE = (
    "Write at least {min_note_tokens} tokens of note content — the note replaces the "
    "agent's entire dropped tool-call history, so a fragmentary note loses real work — "
    "and size it to finish comfortably under the {max_output_tokens}-token "
    "output cap, because anything beyond the cap is cut off mid-sentence and lost."
)


def _prompt_block(tag: str, content: str) -> str:
    """Render an XML-ish data block, defanging the block's OWN tag literals.

    ``escape_system_reminder_tags`` only defangs the two reminder tags —
    content containing this block's closing tag would terminate the block
    early and the remainder would masquerade as prompt structure.  The full
    multiline form is kept (these blocks are the note-writer's full-fidelity
    copy); directive-shaped content is contained by keeping the block's
    delimiters intact, which is exactly what the defang restores.
    """
    safe = content.replace(f"<{tag}>", f"&lt;{tag}&gt;").replace(f"</{tag}>", f"&lt;/{tag}&gt;")
    return f"<{tag}>\n{safe}\n</{tag}>"


def _completer_instruction(
    template: str,
    *,
    has_continuation_nudges: bool,
    max_output_tokens: int,
    format_correction: str = "",
) -> str:
    """The fixed note contract and optional supplement wrapped in no-tools directives.

    The output cap is stated up front and repeated in the final check so the model
    sizes the note to conclude within ``max_output_tokens`` instead of
    running past it (observed live: GPT-5.4 wrote a 23.8k-char note into an
    8k cap and got truncated with ``finish_reason:"length"``).  A 500-token
    minimum and a no-deliberation hint push back on the opposite failure,
    observed on Responses-API reasoning models: thinking consumed the whole
    output budget and the visible note collapsed to a mid-sentence fragment
    (or nothing at all).
    """
    body = (
        "Everything in this conversation is the current, in-progress task.\n"
        "Summarize ALL of it — your note replaces this conversation's tool history.\n"
        "A [LAST_WORDS] block in the last user message is your own previous progress\n"
        "note (inherited state, not user input): merge it with the new work."
    )
    if has_continuation_nudges:
        body += (
            '\nUser messages that just say "continue" are automatic resume nudges after an interruption — '
            "they are not user input; ignore them as content."
        )
    return _assemble_instruction(
        body,
        template,
        max_output_tokens=max_output_tokens,
        format_correction=format_correction,
    )


def _fallback_instruction(
    template: str,
    *,
    has_continuation_nudges: bool,
    max_output_tokens: int,
    format_correction: str = "",
) -> str:
    """Build the fallback instruction with the same fixed contract and wrapper."""
    body = (
        "Everything in this conversation is the current, in-progress task.\n"
        "Summarize ALL of it — your note replaces this conversation's tool history.\n"
        "The <previous_progress_note> block in the user message is your own previous progress\n"
        "note (inherited state, not user input): merge it with the new work."
    )
    if has_continuation_nudges:
        body += (
            '\nUser messages that just say "continue" are automatic resume nudges after an interruption — '
            "they are not user input; ignore them as content."
        )
    return _assemble_instruction(
        body,
        template,
        max_output_tokens=max_output_tokens,
        format_correction=format_correction,
    )


def _assemble_instruction(
    body: str,
    template: str,
    *,
    max_output_tokens: int,
    format_correction: str,
) -> str:
    """Assemble the code-owned instruction in its design-of-record order."""
    blocks = [
        _NO_TOOLS_OPENER.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=max_output_tokens),
        body,
        _FORMAT_CONTRACT,
        _BASE_GUIDANCE,
    ]
    if supplement := _supplement_block(template):
        blocks.append(supplement)
    if format_correction:
        blocks.append(format_correction)
    blocks.extend(
        (
            _NO_TOOLS_REMINDER,
            _NO_TOOLS_FINAL.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=max_output_tokens),
        )
    )
    return "\n\n".join(blocks)


def _format_correction(violation: str) -> str:
    safe_violation = " ".join(violation.split())[:_FORMAT_VIOLATION_MAX_CHARS]
    prefix = "Your previous note was rejected: "
    suffix = ". Re-emit the full note with the required headings."
    available = _FORMAT_CORRECTION_MAX_CHARS - len(prefix) - len(suffix)
    return f"{prefix}{safe_violation[:available]}{suffix}"


def _bounded_format_violation(violation: str) -> str:
    """Return a single-line violation safe for logs, events, and retry prompts."""
    return " ".join(violation.split())[:_FORMAT_VIOLATION_MAX_CHARS]


def _normalize_note_response(text: str | None) -> str:
    """Drop provider-added outer newlines without changing Markdown indentation."""
    if text is None or not text.strip():
        return ""
    return text.strip("\r\n")


def _truncate_template(template: str, max_chars: int = _FALLBACK_TEMPLATE_MAX_CHARS) -> str:
    clean = template.rstrip()
    if len(clean) <= max_chars:
        return clean
    keep = max(0, max_chars - len(_FALLBACK_TEMPLATE_TRUNCATION_MARKER))
    return clean[:keep].rstrip() + _FALLBACK_TEMPLATE_TRUNCATION_MARKER


def _supplement_block(template: str, max_chars: int = _FALLBACK_TEMPLATE_MAX_CHARS) -> str:
    """Render the bounded profile supplement, or nothing for an empty template."""
    if not template.strip():
        return ""
    supplement = _truncate_template(template, max_chars)
    return f"{_SUPPLEMENT_LABEL}\n{supplement}"


class LastWordsGenerationError(RuntimeError):
    """Raised when the LAST_WORDS LLM call fails for any reason.

    Wraps both transport-level failures (SDK exceptions chained via
    ``__cause__``) and empty-response failures behind a single, uniform
    exception type.  ``LastWordsGenerator`` retries the compaction LLM
    request internally before raising this, so outer agent retry loops
    should treat it as a terminal compaction failure, not as a reason to
    replay completed tool work.
    """


class LastWordsSpendBudgetExceeded(LastWordsGenerationError):
    """Raised before a provider attempt would exhaust the turn spend budget."""


# Card-facing text for a spend-budget refusal, shared with the strategy's
# entry-time trip so both surfaces render the identical reason.
SPEND_BUDGET_FAILURE_REASON = "progress-note token budget exceeded for current turn"


@dataclass(frozen=True, slots=True)
class _GeneratedNote:
    text: str
    format_violation: str = ""


@dataclass(slots=True)
class _FormatRetryState:
    violations: list[str]
    correction_violation: str = ""
    pending_note: _GeneratedNote | None = None
    pending_instruction: str = ""
    pending_request_note: str = ""


@dataclass(frozen=True, slots=True)
class CompactionStatus:
    """UX progress signal for one LAST_WORDS generation pass.

    Emitted by :meth:`LastWordsGenerator.generate` through the
    ``publish_status`` callback so frontends can show a live
    "Compacting conversation…" indicator over the whole generation
    window (LLM call plus local retries and backoff).  ``stage`` is
    ``"started"``, ``"finished"``, or ``"committed"``; the remaining fields
    are only meaningful on the finished signal.  A ``"committed"`` signal
    (see :meth:`LastWordsGenerator.publish_committed`) follows a
    finished-ok signal once the compaction strategy has durably applied
    the round — spill written, note set, messages excluded; a finished-ok
    round without it was abandoned.  ``outcome`` is ``"ok"``,
    ``"failed"``, or ``"canceled"``; ``last_words`` carries the note
    text only when ``outcome == "ok"``. ``format_violation`` records the
    structured-note violation when the bounded corrective retry process
    deliberately accepted a fresh malformed note.  ``failure_reason`` is a
    short human-readable cause set on ``"failed"`` outcomes whose cause is
    a known safety limit (spend budget, round limit); frontends show it in
    place of the duration on the failure card.
    """

    compaction_id: str
    stage: str
    outcome: str = ""
    duration_ms: int = 0
    last_words: str = ""
    format_violation: str = ""
    failure_reason: str = ""


def _log_detached_publish_failure(task: asyncio.Future) -> None:
    """Consume a detached committed-publish task's outcome.

    The publish body swallows ordinary exceptions itself, so this normally
    only retrieves ``None`` — but retrieving is mandatory to keep asyncio
    from logging "exception was never retrieved" if the detached task dies.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.debug("LAST_WORDS committed publish failed after detach", exc_info=exc)


class LastWordsGenerator:
    """Generates the LAST_WORDS progress note for Phase 4 compaction.

    Prefers the client-owned scoped side call when the strategy passes a
    completer (see :meth:`generate`); otherwise falls back to a
    single-turn reconstruction call using the owning agent's
    ``ModelProfile`` (model, HTTP options, chat_options) over a separate
    ``route_kind="last-words"`` session.  Always LLM-generated — transient
    failures retry the same compaction prompt locally with the standard
    retry budget and backoff.  Callers (the compaction strategy) must not
    drop current-turn work unless generation succeeds.
    """

    _MAX_RETRIES: int = 5
    _MAX_CORRECTIVE_RETRIES: int = 5
    _BACKOFF_SCHEDULE: tuple[float, ...] = TRANSIENT_RETRY_BACKOFF_SECONDS
    # A LAST_WORDS note replaces an entire amputated turn, and Phase 4 only
    # fires on a near-full context — there is always substantial work to
    # record, so a fragmentary note is worse than a retry.  Observed live
    # (GPT-5.4 Responses): reasoning consumed the whole output budget and the
    # visible note collapsed to an 85-char mid-sentence fragment that passed
    # the old non-empty check.  The floor sits far below any compliant note
    # (the instruction asks for ``_MIN_NOTE_TOKENS``) but far above such fragments.
    _MIN_NOTE_CHARS: int = 300

    def __init__(
        self,
        profile: ModelProfile,
        template: str = "",
        max_output_tokens: int = DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS,
        log_dir: Path | None = None,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        session_dir: Path | None = None,
        max_transient_retries: int | None = None,
        publish_retry: Callable[[RetryAttemptInfo], Awaitable[None]] | None = None,
        publish_status: Callable[[CompactionStatus], Awaitable[None]] | None = None,
        report_usage: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._profile = profile
        self._template = template
        self._max_output_tokens = max_output_tokens
        self._log_dir = log_dir
        self._session_id = session_id
        self._parent_session_id = parent_session_id
        self._session_dir = session_dir
        self._max_transient_retries = max(
            0,
            max_transient_retries if max_transient_retries is not None else type(self)._MAX_RETRIES,
        )
        self._publish_retry = publish_retry
        self._publish_status = publish_status
        self._report_usage = report_usage
        self._last_compaction_id = ""
        # Strong refs to in-flight detached committed publishes — asyncio
        # only weakly references running tasks, so without an anchor a
        # publish that outlives its cancelled shield could be
        # garbage-collected before delivering.
        self._detached_publishes: set[asyncio.Future] = set()
        self._client = None
        self._client_lock = asyncio.Lock()
        self._closed = False
        self._chat_options: dict | None = None

    def _report_side_call_usage(self, usage_details: Mapping[str, Any]) -> None:
        """Forward one side-call response's provider-reported usage.

        Every attempt spends real tokens — including responses that later
        fail validation or acceptance — so this fires per response observed.
        A telemetry sink must never break note generation: failures are
        swallowed to a debug log.
        """
        if self._report_usage is None or not usage_details:
            return
        try:
            self._report_usage(usage_details)
        except Exception:
            _log.debug("side-call usage report failed", exc_info=True)

    async def _get_client(self):
        # Fallback-only: the lazily created separate client (and its derived
        # route_kind="last-words" session) never serves the completer path.
        async with self._client_lock:
            if self._closed:
                raise LastWordsGenerationError("last-words generator is closed")
            if self._client is None:
                from chrys.service.llm.clients import create_client

                self._client = await create_client(
                    self._profile,
                    session_id=self._session_id,
                    parent_session_id=self._parent_session_id,
                    session_dir=self._session_dir,
                )
            return self._client

    async def aclose(self) -> None:
        """Close the fallback client, if one was created; later generations are refused."""
        self._closed = True
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _profile_chat_options(self) -> dict:
        """Parsed profile chat options, cached (fallback options + budget math)."""
        if self._chat_options is None:
            from chrys.service.profiles.models.options import effective_chat_options

            self._chat_options = effective_chat_options(self._profile) or {}
        return self._chat_options

    def _thinking_budget_tokens(self) -> int:
        """Extended-thinking budget riding in the profile options (0 = none).

        Anthropic's ``thinking.budget_tokens`` spends from the same
        ``max_tokens`` pool as the visible note, and the API rejects any
        request whose ``max_tokens`` does not exceed it.
        """
        from chrys.service.profiles.models.options import output_cap_option_value

        thinking = self._profile_chat_options().get("thinking")
        if isinstance(thinking, Mapping) and thinking.get("type") == "enabled":
            budget = output_cap_option_value(thinking, "budget_tokens")
            if budget is not None:
                return budget
        return 0

    def _model_output_cap(self) -> int:
        """Hard ``max_tokens`` ceiling for note calls (0 = unknown).

        Prefers the profile's explicit ``max_output_tokens``.  When that is
        unset, a user-set output cap in the profile chat options (any
        provider spelling) serves as the ceiling instead: every live call
        sends it, so it is known provider-valid, while anything above it is
        unproven.
        """
        cap = self._profile.max_output_tokens
        if cap > 0:
            return cap
        from chrys.service.profiles.models.options import OUTPUT_CAP_OPTION_ALIASES, output_cap_option_value

        options = self._profile_chat_options()
        for alias in OUTPUT_CAP_OPTION_ALIASES:
            value = output_cap_option_value(options, alias)
            if value is not None:
                return value
        return 0

    def _output_budgets(self) -> tuple[int, int]:
        """``(note_budget, wire_max_tokens)`` for a note-generation call.

        ``note_budget`` is the visible-note share stated in the prompt;
        ``wire_max_tokens`` is the ``max_tokens`` actually sent.  Both start
        from the configured ``max_output_tokens`` clamped to the model's hard
        output cap (:meth:`_model_output_cap` — DeepSeek rejects anything
        above 8192 outright).  When the profile enables extended thinking,
        the wire value is raised by ``budget_tokens`` (which spends from the
        same pool) so thinking cannot starve the note, then re-clamped to
        the cap.
        """
        cap = self._model_output_cap()
        note = self._max_output_tokens
        if cap > 0:
            note = min(note, cap)
        thinking = self._thinking_budget_tokens()
        wire = note
        if thinking > 0:
            wire = note + thinking
            if cap > 0 and wire > cap:
                wire = cap
                note = max(wire - thinking, 1)
        if thinking > 0 and wire <= thinking:
            _log.warning(
                "extended-thinking budget_tokens (%d) meets or exceeds the model output cap (%d); "
                "LAST_WORDS note calls will likely be rejected by the provider",
                thinking,
                cap,
            )
        elif note < _MIN_NOTE_TOKENS:
            _log.warning(
                "LAST_WORDS note budget (%d tokens) is below the %d-token minimum the prompt asks "
                "for (model output cap %d, extended-thinking budget %d); notes will likely be "
                "truncated and rejected by the note-length floor",
                note,
                _MIN_NOTE_TOKENS,
                cap,
                thinking,
            )
        return note, wire

    async def generate(
        self,
        scoped_groups: list[ScopedGroup],
        previous_last_words: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        completer: LastWordsCompleter | None = None,
        tokenizer: TokenizerProtocol | None = None,
        system_overhead_tokens: int = 0,
        tool_definition_tokens: int = 0,
        request_overhead_tokens: int = 0,
        calibration_ratio: float = 1.0,
        spend_side_call_tokens: Callable[[int], bool] | None = None,
    ) -> str:
        """Produce an updated LAST_WORDS note, publishing UX status signals.

        ``spend_side_call_tokens`` is the narrow Phase-4 admission hook: it is
        called synchronously with the estimated input size immediately before
        every provider attempt. Returning false raises
        :class:`LastWordsSpendBudgetExceeded` before that request starts unless
        a fresh malformed note is already available for safe acceptance.

        Wraps :meth:`_generate` (the actual side-call/fallback machinery)
        with a started/finished :class:`CompactionStatus` pair through
        ``publish_status`` so frontends can surface the otherwise-silent
        generation window.  The finished signal always fires — on success
        (``outcome="ok"`` with the note text), terminal failure
        (``"failed"``), and cancellation (``"canceled"``) — so a live
        indicator never dangles.  Status publication is UX-only: failures
        to publish are swallowed and never affect generation — except
        cancellation, which is always honored: delivered during the
        started publish it aborts before the LLM call; during the
        finished publish it propagates instead of returning the note.
        """
        compaction_id = uuid4().hex
        # Remembered so the strategy's post-commit publish_committed() call
        # can correlate with this round's started/finished pair.
        self._last_compaction_id = compaction_id
        started_at = time.monotonic()
        outcome = "failed"
        note = ""
        format_violation = ""
        failure_reason = ""
        try:
            # Published inside the try: an interrupt that lands while this
            # await is suspended in a frontend handler must flow through the
            # canceled/finished machinery below — swallowing it would let
            # the expensive LLM call start after the user already cancelled.
            await self._publish_status_cancellable(CompactionStatus(compaction_id=compaction_id, stage="started"))
            generated = await self._generate(
                scoped_groups,
                previous_last_words,
                degraded_opener=degraded_opener,
                has_continuation_nudges=has_continuation_nudges,
                completer=completer,
                tokenizer=tokenizer or MixedLanguageTokenizer(),
                system_overhead_tokens=system_overhead_tokens,
                tool_definition_tokens=tool_definition_tokens,
                request_overhead_tokens=request_overhead_tokens,
                calibration_ratio=calibration_ratio,
                spend_side_call_tokens=spend_side_call_tokens,
            )
            note = generated.text
            format_violation = generated.format_violation
            outcome = "ok" if note else "failed"
            return note
        except LastWordsSpendBudgetExceeded:
            failure_reason = SPEND_BUDGET_FAILURE_REASON
            raise
        except asyncio.CancelledError:
            outcome = "canceled"
            raise
        finally:
            finished = CompactionStatus(
                compaction_id=compaction_id,
                stage="finished",
                outcome=outcome,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                last_words=note if outcome == "ok" else "",
                format_violation=format_violation if outcome == "ok" else "",
                failure_reason=failure_reason if outcome == "failed" else "",
            )
            try:
                await self._publish_status_cancellable(finished)
            except asyncio.CancelledError:
                # A cancellation delivered while the finished signal awaits
                # frontend handlers must be honored — returning the note
                # would swallow the user's interrupt.  But when this
                # ``finally`` is already unwinding a cancellation, the
                # publish-await raises a *fresh* CancelledError; re-raising
                # it would merely replace the original traceback, so let
                # the propagating one win.
                if outcome != "canceled":
                    raise
                _log.debug("LAST_WORDS finished publish cancelled during teardown", exc_info=True)

    async def _publish_status_cancellable(self, status: CompactionStatus) -> None:
        """Publish a status signal, letting cancellation propagate.

        Ordinary publish failures are swallowed (UX-only signals must never
        break generation), but ``CancelledError`` — and any other
        ``BaseException`` — propagates: an interrupt delivered during the
        started publish must stop the compaction before the LLM call
        begins, and one during the finished publish must not be traded for
        the generated note (the caller decides when a propagating
        cancellation already covers it).
        """
        if self._publish_status is None:
            return
        try:
            await self._publish_status(status)
        except Exception:
            _log.debug("LAST_WORDS status publish failed (stage=%s)", status.stage, exc_info=True)

    async def publish_committed(self) -> None:
        """Publish a ``stage="committed"`` status for the last generated note.

        Called by the compaction strategy only after the round is durably
        applied — spill written (or checkpointed through recovery
        persistence), note set, manifest appended, and messages excluded.
        The finished-ok signal from :meth:`generate` fires *before* that
        commit, so observers that must count real compactions (not merely
        successful note generations) wait for this signal instead.

        Delivery is shielded from cancellation: by the time this fires the
        round is already durable, so an interrupt arriving mid-publish must
        not erase the only signal that distinguishes a committed round from
        an abandoned one.  The detached publish finishes in the background
        while the cancellation propagates to the caller.
        """
        if not self._last_compaction_id:
            return
        status = CompactionStatus(compaction_id=self._last_compaction_id, stage="committed")
        # Consumed on publish: one generate, one committed signal.  A stray
        # second call must not re-emit "committed" for an old round.
        self._last_compaction_id = ""
        publish = asyncio.ensure_future(self._publish_status_cancellable(status))
        self._detached_publishes.add(publish)
        publish.add_done_callback(self._discard_detached_publish)
        await asyncio.shield(publish)

    def _discard_detached_publish(self, task: asyncio.Future) -> None:
        self._detached_publishes.discard(task)
        _log_detached_publish_failure(task)

    async def publish_breaker_trip(self, failure_reason: str) -> None:
        """Publish an immediate started+failed status pair for an entry-time trip.

        When the persisted Phase-4 breaker refuses entry, the strategy skips
        generation entirely, so :meth:`generate`'s status pair never fires.
        This publishes one terminal failure card carrying the trip reason —
        without it the round would vanish silently while the model window
        keeps overflowing.
        """
        compaction_id = uuid4().hex
        await self._publish_status_cancellable(CompactionStatus(compaction_id=compaction_id, stage="started"))
        await self._publish_status_cancellable(
            CompactionStatus(
                compaction_id=compaction_id,
                stage="finished",
                outcome="failed",
                failure_reason=failure_reason,
            )
        )

    async def _generate(
        self,
        scoped_groups: list[ScopedGroup],
        previous_last_words: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        completer: LastWordsCompleter | None = None,
        tokenizer: TokenizerProtocol,
        system_overhead_tokens: int,
        tool_definition_tokens: int,
        request_overhead_tokens: int,
        calibration_ratio: float,
        spend_side_call_tokens: Callable[[int], bool] | None,
    ) -> _GeneratedNote:
        """Generate from one scoped timeline, with the bounded reconstruction as terminal path."""
        format_state = _FormatRetryState([])
        if completer is not None:
            side_call_note = await self._generate_via_completer(
                completer,
                scoped_groups,
                tokenizer=tokenizer,
                has_continuation_nudges=has_continuation_nudges,
                system_overhead_tokens=system_overhead_tokens,
                tool_definition_tokens=tool_definition_tokens,
                request_overhead_tokens=request_overhead_tokens,
                calibration_ratio=calibration_ratio,
                spend_side_call_tokens=spend_side_call_tokens,
                format_state=format_state,
            )
            if side_call_note is not None:
                return side_call_note
            _log.warning("LAST_WORDS side call demoted, generating via reconstruction fallback")
        return await self._generate_via_fallback(
            scoped_groups,
            previous_last_words,
            degraded_opener=degraded_opener,
            has_continuation_nudges=has_continuation_nudges,
            spend_side_call_tokens=spend_side_call_tokens,
            format_state=format_state,
        )

    async def _generate_via_fallback(
        self,
        scoped_groups: list[ScopedGroup],
        previous_last_words: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        spend_side_call_tokens: Callable[[int], bool] | None,
        format_state: _FormatRetryState,
    ) -> _GeneratedNote:
        note_budget, wire_max_tokens = self._output_budgets()
        candidate_index = 0
        transient_attempt = 0
        corrective_attempt = 0
        last_context_error: Exception | None = None
        max_transient_retries = self._max_transient_retries
        max_corrective_retries = max(0, type(self)._MAX_CORRECTIVE_RETRIES)
        while True:
            format_correction = (
                _format_correction(format_state.correction_violation) if format_state.correction_violation else ""
            )
            candidates = _fallback_candidates(
                scoped_groups,
                previous_last_words,
                template=self._template,
                degraded_opener=degraded_opener,
                has_continuation_nudges=has_continuation_nudges,
                note_budget=note_budget,
                format_correction=format_correction,
            )
            if candidate_index >= len(candidates):
                if accepted := self._accept_pending_format_note(
                    format_state,
                    reason="corrective request could not fit the model context window",
                ):
                    return accepted
                message = "Fallback request cannot fit the model context window without dropping current-turn work"
                if last_context_error is not None:
                    raise LastWordsGenerationError(message) from last_context_error
                raise LastWordsGenerationError(message)
            candidate = candidates[candidate_index]
            estimated_input_tokens = _fallback_admission_tokens(candidate.messages, output_reserve=0)
            send_max_tokens = wire_max_tokens
            admitted_tokens = _fallback_admission_tokens(candidate.messages, output_reserve=wire_max_tokens)
            if admitted_tokens > self._profile.max_context_tokens:
                if candidate_index < len(candidates) - 1 or format_state.pending_note is not None:
                    # With a pending accept-with-flag note, exhaustion falls back
                    # to accepting it, so skipping the corrective call is safe.
                    _log.warning(
                        "LAST_WORDS fallback candidate '%s' rejected by admission preflight (%d > %d tokens)",
                        candidate.stage,
                        admitted_tokens,
                        self._profile.max_context_tokens,
                    )
                    candidate_index += 1
                    continue
                # The byte-conservative estimate overcharges real tokenization by
                # roughly 4x on prose, so rejecting the last-resort candidate
                # locally would dead-end Phase 4 on small-context profiles whose
                # requests actually fit. Send it and let the provider tokenizer
                # rule; a genuine context rejection still ends the shrink ladder.
                # The output reserve is exact rather than an overestimate, so
                # clamp it too: providers enforcing input + max_tokens <= context
                # would otherwise deterministically reject profiles whose output
                # cap rivals their context window. Keep at least the note floor
                # (plus any thinking budget) but never exceed wire_max_tokens,
                # which already respects the model's hard output cap — below the
                # floor, the provider verdict is the real answer.
                room = self._profile.max_context_tokens - estimated_input_tokens
                thinking = self._thinking_budget_tokens()
                min_send = thinking + _MIN_NOTE_TOKENS if thinking > 0 else _MIN_NOTE_TOKENS
                if room < send_max_tokens:
                    send_max_tokens = min(wire_max_tokens, max(min_send, room))
                if send_max_tokens < wire_max_tokens:
                    # Re-render the terminal candidate so the in-prompt length
                    # directive matches the clamped wire budget — otherwise the
                    # model is told it may write far more than max_tokens allows
                    # and the note risks mid-section truncation. The clamped
                    # budget only shrinks the rendered text, so the admission
                    # estimate used for the clamp stays an upper bound.
                    clamped_note_budget = max(1, send_max_tokens - thinking) if thinking > 0 else send_max_tokens
                    candidate = _fallback_candidates(
                        scoped_groups,
                        previous_last_words,
                        template=self._template,
                        degraded_opener=degraded_opener,
                        has_continuation_nudges=has_continuation_nudges,
                        note_budget=clamped_note_budget,
                        format_correction=format_correction,
                    )[-1]
                    estimated_input_tokens = _fallback_admission_tokens(candidate.messages, output_reserve=0)
                _log.warning(
                    "LAST_WORDS final fallback candidate '%s' exceeds the conservative admission estimate "
                    "(%d > %d tokens); sending it anyway with max_tokens=%d because the provider "
                    "tokenizer is authoritative",
                    candidate.stage,
                    admitted_tokens,
                    self._profile.max_context_tokens,
                    send_max_tokens,
                )
            if spend_side_call_tokens is not None and not spend_side_call_tokens(estimated_input_tokens):
                if accepted := self._accept_pending_format_note(
                    format_state,
                    reason="side-call spend budget stopped the corrective retry",
                ):
                    return accepted
                raise LastWordsSpendBudgetExceeded("Phase-4 side-call token budget exhausted before fallback attempt")
            try:
                output_text = await self._generate_once(list(candidate.messages), max_tokens=send_max_tokens)
            except Exception as exc:
                # A false positive only shrinks the candidate; a miss would resend
                # the same oversized candidate until the retry budget runs out.
                if may_be_context_overflow(exc):
                    last_context_error = exc
                    candidate_index += 1
                    _log.warning(
                        "LAST_WORDS fallback candidate '%s' hit a provider context rejection; shrinking: %s",
                        candidate.stage,
                        clean_error_message(exc),
                    )
                    self._write_log(
                        candidate.instruction,
                        "",
                        "",
                        error=f"provider context rejection at {candidate.stage}; advancing shrink sequence: {exc}",
                        request_note=f"fallback stage: {candidate.stage}",
                    )
                    continue
                retryable = is_retryable(exc) or isinstance(exc, LastWordsToolCallError)
                if transient_attempt >= max_transient_retries or not retryable:
                    if accepted := self._accept_pending_format_note(
                        format_state,
                        reason="corrective retry failed at the provider",
                    ):
                        return accepted
                    _log.warning("LastWordsGenerator LLM call failed after local retries: %s", exc)
                    self._write_log(
                        candidate.instruction,
                        "",
                        "",
                        error=str(exc),
                        request_note=f"fallback stage: {candidate.stage}",
                    )
                    raise LastWordsGenerationError("Failed to generate last words for the current task") from exc
                delay = self._delay(transient_attempt)
                _log.warning(
                    "LastWordsGenerator LLM call failed, retrying compaction request in %gs (attempt %d/%d): %s",
                    delay,
                    transient_attempt + 1,
                    max_transient_retries,
                    clean_error_message(exc),
                )
                self._write_log(
                    candidate.instruction,
                    "",
                    "",
                    error=f"attempt {transient_attempt + 1}/{max_transient_retries} failed, retrying: {exc}",
                    request_note=f"fallback stage: {candidate.stage}",
                )
                await self._publish_retry_attempt(
                    clean_error_message(exc),
                    transient_attempt + 1,
                    max_transient_retries,
                    delay,
                )
                transient_attempt += 1
                await self._sleep_for_retry(delay, reason_code=RetryReason.TRANSIENT_ERROR)
                continue

            format_violation: str | None = None
            repeated_violation = False
            canonical_text = output_text
            if output_text:
                violation, canonical_text = _validate_note_format(output_text)
                if violation is not None:
                    format_violation = _bounded_format_violation(violation)
                    repeated_violation = format_violation in format_state.violations
                    if not repeated_violation:
                        format_state.violations.append(format_violation)
                    format_state.correction_violation = format_violation
                else:
                    # A non-empty response with valid structure proves the last
                    # correction was followed. A remaining length shortfall may
                    # still retry, but it no longer needs stale format feedback.
                    format_state.correction_violation = ""
            shortfall = self._note_shortfall(canonical_text)
            if shortfall is None:
                if format_violation is not None:
                    format_state.pending_note = _GeneratedNote(output_text, format_violation=format_violation)
                    format_state.pending_instruction = candidate.instruction
                    format_state.pending_request_note = f"fallback stage: {candidate.stage}"
                    if repeated_violation or corrective_attempt >= max_corrective_retries:
                        reason = (
                            "identical violation repeated"
                            if repeated_violation
                            else "corrective retry budget was exhausted"
                        )
                        accepted = self._accept_pending_format_note(format_state, reason=reason)
                        if accepted is None:
                            raise RuntimeError("LAST_WORDS format candidate disappeared before acceptance")
                        return accepted
                    delay = self._delay(corrective_attempt)
                    _log.warning(
                        "LAST_WORDS format violation, retrying compaction request in %gs (attempt %d/%d): %s",
                        delay,
                        corrective_attempt + 1,
                        max_corrective_retries,
                        format_violation,
                    )
                    self._write_log(
                        candidate.instruction,
                        output_text,
                        "",
                        error=(
                            f"attempt {corrective_attempt + 1}/{max_corrective_retries} returned format violation "
                            f"{format_violation}, retrying"
                        ),
                        request_note=f"fallback stage: {candidate.stage}",
                    )
                    await self._publish_retry_attempt(
                        format_violation,
                        corrective_attempt + 1,
                        max_corrective_retries,
                        delay,
                    )
                    corrective_attempt += 1
                    await self._sleep_for_retry(delay, reason_code=RetryReason.VALIDATION_REJECTED)
                    continue
                self._write_log(
                    candidate.instruction,
                    output_text,
                    canonical_text,
                    request_note=f"fallback stage: {candidate.stage}",
                )
                return _GeneratedNote(canonical_text)

            if corrective_attempt >= max_corrective_retries:
                if format_state.pending_note is not None:
                    self._write_log(
                        candidate.instruction,
                        output_text,
                        "",
                        error=f"{shortfall} after retries; accepting the earlier complete malformed note",
                        request_note=f"fallback stage: {candidate.stage}",
                    )
                    accepted = self._accept_pending_format_note(
                        format_state,
                        reason=f"{shortfall} and corrective retry budget was exhausted",
                    )
                    if accepted is None:
                        raise RuntimeError("LAST_WORDS format candidate disappeared before acceptance")
                    return accepted
                if output_text:
                    accepted_error = f"{shortfall} after retries, accepted"
                    if format_violation is not None:
                        format_state.pending_note = _GeneratedNote(output_text, format_violation=format_violation)
                        format_state.pending_instruction = candidate.instruction
                        format_state.pending_request_note = f"fallback stage: {candidate.stage}"
                        accepted = self._accept_pending_format_note(
                            format_state,
                            reason=f"{shortfall} and corrective retry budget was exhausted",
                        )
                        if accepted is None:
                            raise RuntimeError("LAST_WORDS format candidate disappeared before acceptance")
                        return accepted
                    _log.warning(
                        "LastWordsGenerator note still %s after local retries; accepting the short note",
                        shortfall,
                    )
                    self._write_log(
                        candidate.instruction,
                        output_text,
                        canonical_text,
                        error=accepted_error,
                        request_note=f"fallback stage: {candidate.stage}",
                    )
                    return _GeneratedNote(canonical_text, format_violation=format_violation or "")
                if accepted := self._accept_pending_format_note(
                    format_state,
                    reason="corrective retry budget was exhausted after an empty response",
                ):
                    return accepted
                _log.warning("LastWordsGenerator returned %s after local retries", shortfall)
                self._write_log(
                    candidate.instruction,
                    output_text,
                    "",
                    error=shortfall,
                    request_note=f"fallback stage: {candidate.stage}",
                )
                raise LastWordsGenerationError("Failed to generate last words for the current task")
            delay = self._delay(corrective_attempt)
            _log.warning(
                "LastWordsGenerator returned %s, retrying compaction request in %gs (attempt %d/%d)",
                shortfall,
                delay,
                corrective_attempt + 1,
                max_corrective_retries,
            )
            self._write_log(
                candidate.instruction,
                output_text,
                "",
                error=f"attempt {corrective_attempt + 1}/{max_corrective_retries} returned {shortfall}, retrying",
                request_note=f"fallback stage: {candidate.stage}",
            )
            await self._publish_retry_attempt(
                shortfall,
                corrective_attempt + 1,
                max_corrective_retries,
                delay,
            )
            corrective_attempt += 1
            await self._sleep_for_retry(delay, reason_code=RetryReason.VALIDATION_REJECTED)

    async def _generate_via_completer(
        self,
        completer: LastWordsCompleter,
        scoped_groups: list[ScopedGroup],
        *,
        tokenizer: TokenizerProtocol,
        has_continuation_nudges: bool,
        system_overhead_tokens: int,
        tool_definition_tokens: int,
        request_overhead_tokens: int,
        calibration_ratio: float,
        spend_side_call_tokens: Callable[[int], bool] | None,
        format_state: _FormatRetryState,
    ) -> _GeneratedNote | None:
        """Run the physically scoped side call within its global input budget.

        Returns the generated note or ``None`` to demote. ``format_state``
        carries corrective-retry state across the fallback boundary:

        * provider context-window rejection of the side call — retrying the
          same oversized scoped request cannot succeed;
        * any other non-retryable failure;
        * exhaustion of the local retry budget on transient transport
          errors, repeated illegal tool-call responses
          (:class:`~chrys.kernel.LastWordsToolCallError` from the client's
          response guard), or empty/too-short responses (``_note_shortfall``).

        Provider failures demote to fallback. The only direct exception is
        :class:`LastWordsSpendBudgetExceeded`, raised before an over-budget
        provider attempt when no fresh malformed note is available to accept.
        """
        note_budget, wire_max_tokens = self._output_budgets()
        # ``system_overhead_tokens`` is the calibrated fixed request overhead.
        # The explicit request/tool estimates are floors before calibration or
        # when provider framing is undercounted; adding them would double-charge
        # overlapping content.
        fixed_request_overhead_tokens = max(
            0,
            system_overhead_tokens,
            request_overhead_tokens,
            tool_definition_tokens,
        )
        for attempt in range(_COMPLETER_MAX_RETRIES + 1):
            format_correction = (
                _format_correction(format_state.correction_violation) if format_state.correction_violation else ""
            )
            instruction = _completer_instruction(
                self._template,
                has_continuation_nudges=has_continuation_nudges,
                max_output_tokens=note_budget,
                format_correction=format_correction,
            )
            instruction_tokens = tokenizer.count_tokens(instruction)
            usable_raw = math.floor(
                (self._profile.max_context_tokens - wire_max_tokens - _SLICE_SAFETY_MARGIN_TOKENS) / calibration_ratio
            )
            slice_budget = usable_raw - fixed_request_overhead_tokens - instruction_tokens
            prepared = prepare_scoped_slice(scoped_groups, tokenizer=tokenizer, slice_budget=slice_budget)
            if prepared is None:
                _log.warning(
                    "LAST_WORDS scoped slice failed budget or runtime shape validation; "
                    "demoting to fallback (budget=%d)",
                    slice_budget,
                )
                return None
            base_messages = prepared.messages
            prompt_note = (
                f"(physically scoped side call: {len(base_messages)} messages, "
                f"{prepared.estimated_tokens} estimated message tokens, {prepared.clipped_results} clipped results, "
                f"{prepared.clipped_assistant_texts} clipped assistant texts)"
            )
            estimated_input_tokens = prepared.estimated_tokens + instruction_tokens + fixed_request_overhead_tokens
            if spend_side_call_tokens is not None and not spend_side_call_tokens(estimated_input_tokens):
                if accepted := self._accept_pending_format_note(
                    format_state,
                    reason="side-call spend budget stopped the corrective retry",
                ):
                    return accepted
                raise LastWordsSpendBudgetExceeded("Phase-4 side-call token budget exhausted before completer attempt")
            try:
                output_text = _normalize_note_response(
                    await completer.complete_last_words(
                        base_messages,
                        instruction,
                        max_output_tokens=wire_max_tokens,
                        on_usage=self._report_side_call_usage,
                    )
                )
            except Exception as exc:
                if may_be_context_overflow(exc):
                    _log.warning(
                        "LAST_WORDS side call rejected for context-window size, demoting to fallback: %s",
                        clean_error_message(exc),
                    )
                    self._write_log(
                        instruction,
                        "",
                        "",
                        error=f"context-window rejection, demoting to fallback: {exc}",
                        request_note=prompt_note,
                    )
                    return None
                retryable = is_retryable(exc) or isinstance(exc, LastWordsToolCallError)
                if attempt >= _COMPLETER_MAX_RETRIES or not retryable:
                    _log.warning("LAST_WORDS side call failed after local retries, demoting to fallback: %s", exc)
                    self._write_log(
                        instruction,
                        "",
                        "",
                        error=f"side call failed, demoting to fallback: {exc}",
                        request_note=prompt_note,
                    )
                    return None
                delay = self._delay(attempt)
                _log.warning(
                    "LAST_WORDS side call failed, retrying in %gs (attempt %d/%d): %s",
                    delay,
                    attempt + 1,
                    _COMPLETER_MAX_RETRIES,
                    clean_error_message(exc),
                )
                self._write_log(
                    instruction,
                    "",
                    "",
                    error=f"attempt {attempt + 1}/{_COMPLETER_MAX_RETRIES} failed, retrying: {exc}",
                    request_note=prompt_note,
                )
                await self._publish_retry_attempt(
                    clean_error_message(exc),
                    attempt + 1,
                    _COMPLETER_MAX_RETRIES,
                    delay,
                )
                await self._sleep_for_retry(delay, reason_code=RetryReason.TRANSIENT_ERROR)
                continue

            format_violation: str | None = None
            repeated_violation = False
            canonical_text = output_text
            if output_text:
                violation, canonical_text = _validate_note_format(output_text)
                if violation is not None:
                    format_violation = _bounded_format_violation(violation)
                    repeated_violation = format_violation in format_state.violations
                    if not repeated_violation:
                        format_state.violations.append(format_violation)
                    format_state.correction_violation = format_violation
                else:
                    format_state.correction_violation = ""
            shortfall = self._note_shortfall(canonical_text)
            if shortfall is None:
                if format_violation is not None:
                    format_state.pending_note = _GeneratedNote(output_text, format_violation=format_violation)
                    format_state.pending_instruction = instruction
                    format_state.pending_request_note = prompt_note
                    if repeated_violation:
                        accepted = self._accept_pending_format_note(
                            format_state,
                            reason="identical violation repeated",
                        )
                        if accepted is None:
                            raise RuntimeError("LAST_WORDS format candidate disappeared before acceptance")
                        return accepted
                    if attempt >= _COMPLETER_MAX_RETRIES:
                        _log.warning(
                            "LAST_WORDS side call returned a fresh format violation at its retry limit; "
                            "demoting to fallback: %s",
                            format_violation,
                        )
                        self._write_log(
                            instruction,
                            output_text,
                            "",
                            error=f"format violation {format_violation}, demoting to fallback for corrective retry",
                            request_note=prompt_note,
                        )
                        return None
                    delay = self._delay(attempt)
                    _log.warning(
                        "LAST_WORDS side call returned a format violation, retrying in %gs (attempt %d/%d): %s",
                        delay,
                        attempt + 1,
                        _COMPLETER_MAX_RETRIES,
                        format_violation,
                    )
                    self._write_log(
                        instruction,
                        output_text,
                        "",
                        error=(
                            f"attempt {attempt + 1}/{_COMPLETER_MAX_RETRIES} returned format violation "
                            f"{format_violation}, retrying"
                        ),
                        request_note=prompt_note,
                    )
                    await self._publish_retry_attempt(
                        format_violation,
                        attempt + 1,
                        _COMPLETER_MAX_RETRIES,
                        delay,
                    )
                    await self._sleep_for_retry(delay, reason_code=RetryReason.VALIDATION_REJECTED)
                    continue
                self._write_log(
                    instruction,
                    output_text,
                    canonical_text,
                    request_note=prompt_note,
                )
                return _GeneratedNote(canonical_text)

            if attempt >= _COMPLETER_MAX_RETRIES:
                _log.warning("LAST_WORDS side call returned %s after local retries, demoting to fallback", shortfall)
                self._write_log(
                    instruction,
                    output_text,
                    "",
                    error=f"{shortfall}, demoting to fallback",
                    request_note=prompt_note,
                )
                return None
            delay = self._delay(attempt)
            _log.warning(
                "LAST_WORDS side call returned %s, retrying in %gs (attempt %d/%d)",
                shortfall,
                delay,
                attempt + 1,
                _COMPLETER_MAX_RETRIES,
            )
            self._write_log(
                instruction,
                output_text,
                "",
                error=f"attempt {attempt + 1}/{_COMPLETER_MAX_RETRIES} returned {shortfall}, retrying",
                request_note=prompt_note,
            )
            await self._publish_retry_attempt(shortfall, attempt + 1, _COMPLETER_MAX_RETRIES, delay)
            await self._sleep_for_retry(delay, reason_code=RetryReason.VALIDATION_REJECTED)

        return None

    def _accept_pending_format_note(
        self,
        state: _FormatRetryState,
        *,
        reason: str,
    ) -> _GeneratedNote | None:
        """Accept a fresh malformed note when bounded correction cannot continue."""
        note = state.pending_note
        if note is None:
            return None
        _log.warning(
            "LAST_WORDS format violation accepted when corrective retries stopped (%s): %s",
            reason,
            note.format_violation,
        )
        self._write_log(
            state.pending_instruction,
            note.text,
            note.text,
            error=f"format violation accepted: {note.format_violation}; corrective retries stopped: {reason}",
            request_note=state.pending_request_note or None,
        )
        return note

    async def _publish_retry_attempt(self, reason: str, attempt: int, max_attempts: int, delay_seconds: float) -> None:
        if self._publish_retry is None:
            return
        try:
            await self._publish_retry(
                RetryAttemptInfo(
                    reason=reason,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay_seconds,
                )
            )
        except Exception:
            _log.debug("Failed to publish LAST_WORDS retry event", exc_info=True)

    async def _sleep_for_retry(self, delay_seconds: float, *, reason_code: str) -> None:
        trace = RetryBackoffTrace.open(
            parent_operation_id=current_compaction_operation_id(),
            retry_mode=RetryMode.COMPACTION,
        )
        if trace is not None:
            await trace.scheduled(reason_code=reason_code, delay_seconds=delay_seconds)
        await asyncio.sleep(delay_seconds)
        if trace is not None:
            await trace.started()

    def _delay(self, attempt: int) -> float:
        schedule = type(self)._BACKOFF_SCHEDULE
        if not schedule:
            return 0
        return schedule[min(attempt, len(schedule) - 1)]

    def _note_shortfall(self, text: str) -> str | None:
        """Why ``text`` is unusable as a note (``None`` when acceptable).

        Non-empty is not enough: a truncated fragment silently replaces the
        whole dropped turn, so anything under ``_MIN_NOTE_CHARS`` is retried
        like an empty response.  Callers pass the *canonical* note (violating
        responses come through unchanged — the validator returns them as-is)
        and the floor is measured on whitespace-collapsed content: anything
        canonicalization strips (blank-line padding, heading closing
        sequences) or collapsing removes must not clear the floor only for
        the accepted note to shrink below it and authorize dropping the
        current turn.
        """
        substantive_chars = len("".join(text.split()))
        if not substantive_chars:
            return "empty response"
        floor = type(self)._MIN_NOTE_CHARS
        if substantive_chars < floor:
            return f"note too short ({substantive_chars} substantive chars < {floor} minimum)"
        return None

    async def _generate_once(self, messages: list[Message], *, max_tokens: int) -> str:
        client = await self._get_client()
        profile_options = self._profile_chat_options()
        options = {key: value for key, value in profile_options.items() if key in _FALLBACK_ALLOWED_OPTION_KEYS}
        extra_body = profile_options.get("extra_body")
        if isinstance(extra_body, Mapping) and PROMPT_CACHE_KEY_OPTION in extra_body:
            # Of extra_body, only the prompt cache key, or its null, applies to the note.
            options["extra_body"] = {PROMPT_CACHE_KEY_OPTION: extra_body[PROMPT_CACHE_KEY_OPTION]}
        options["max_tokens"] = max_tokens
        report_wire_progress()
        try:
            with side_call_scope(ActorRole.COMPACTION):
                response = await get_final_response(
                    client,
                    messages,
                    stream=self._profile.stream,
                    options=options,
                    timeout=self._profile.http_read_timeout,
                )
        except ProviderResponseError as err:
            # A response the adapter failed consumed provider tokens too.
            if err.usage_details:
                self._report_side_call_usage(err.usage_details)
            raise
        usage_details = response.usage_details
        if usage_details:
            self._report_side_call_usage(usage_details)
        raise_if_context_window_filled(response)
        # As in the completer's guard: a reply that asks for a tool is no note.
        if messages_contain_tool_calls(response.messages):
            raise LastWordsToolCallError("last-words fallback call returned tool-call content")
        return _normalize_note_response(response.raw_text)

    def _write_log(
        self,
        instruction: str,
        raw_response: str,
        final_text: str,
        error: str | None = None,
        request_note: str | None = None,
    ) -> None:
        """Write one side-call debug log.

        Deliberately excludes the user/timeline payload (user messages, tool
        calls, previous note) — session data lives in the spill records. The
        log keeps only the instruction actually sent, the model's raw
        response, and the text placed in the LAST_WORDS tag for the next
        turn, plus a small request summary when provided.
        """
        if self._log_dir is None:
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            # Salt with a short uuid fragment: concurrent Phase-3 rounds
            # in the same millisecond (e.g. main agent + sub-agent) would
            # otherwise overwrite each other's log.
            ts = datetime.now(UTC).strftime("%H%M%S_%f")[:-3]
            path = self._log_dir / f"last_words_{ts}_{uuid4().hex[:6]}.log"
            lines: list[str] = []
            lines.append("=" * 78)
            lines.append(f"LAST_WORDS @ {datetime.now(UTC).isoformat()}")
            lines.append("=" * 78)
            if request_note:
                lines.append("")
                lines.append("--- REQUEST ---")
                lines.append(request_note)
            lines.append("")
            lines.append("--- INSTRUCTION ---")
            lines.append(instruction.rstrip() or "(none)")
            lines.append("")
            lines.append("--- RAW RESPONSE ---")
            lines.append(raw_response.rstrip() if raw_response else "(empty)")
            if error:
                lines.append("")
                lines.append("--- ERROR ---")
                lines.append(error)
            lines.append("")
            lines.append("--- LAST_WORDS ---")
            lines.append(final_text.rstrip() or "(none)")
            lines.append("")
            # Owner-only like the spill records: the log carries the note
            # text and raw model output, which summarise session content.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write("\n".join(lines))
        except Exception:
            _log.debug("Failed to write last_words log", exc_info=True)


@dataclass(frozen=True, slots=True)
class _TimelineItem:
    text: str
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class _FallbackSpec:
    stage: str
    timeline_ceiling: int
    previous_note_cap: int
    supplement_cap: int = _FALLBACK_TEMPLATE_MAX_CHARS
    newest_few: bool = False


@dataclass(frozen=True, slots=True)
class _FallbackCandidate:
    stage: str
    messages: tuple[Message, Message]
    instruction: str


def _fallback_candidates(
    scoped_groups: list[ScopedGroup],
    previous_last_words: str | None,
    *,
    template: str,
    degraded_opener: bool,
    has_continuation_nudges: bool,
    note_budget: int,
    format_correction: str,
) -> list[_FallbackCandidate]:
    half_timeline = max(1, _FALLBACK_MAX_CHARS // 2)
    half_note = max(1, _FALLBACK_PREV_NOTE_MAX_CHARS // 2)
    specs = (
        _FallbackSpec("bounded", _FALLBACK_MAX_CHARS, _FALLBACK_PREV_NOTE_MAX_CHARS),
        _FallbackSpec("half-timeline", half_timeline, _FALLBACK_PREV_NOTE_MAX_CHARS),
        _FallbackSpec("half-previous-note", half_timeline, half_note),
        _FallbackSpec("compact-supplement", half_timeline, half_note, _FALLBACK_TEMPLATE_COMPACT_CHARS),
        _FallbackSpec(
            "newest-few",
            _FALLBACK_MIN_TIMELINE_CHARS,
            _FALLBACK_MIN_TIMELINE_CHARS,
            _FALLBACK_TEMPLATE_MIN_CHARS,
            newest_few=True,
        ),
    )
    return [
        _render_fallback_candidate(
            scoped_groups,
            previous_last_words,
            template=template,
            degraded_opener=degraded_opener,
            has_continuation_nudges=has_continuation_nudges,
            note_budget=note_budget,
            format_correction=format_correction,
            spec=spec,
        )
        for spec in specs
    ]


def _render_fallback_candidate(
    scoped_groups: list[ScopedGroup],
    previous_last_words: str | None,
    *,
    template: str,
    degraded_opener: bool,
    has_continuation_nudges: bool,
    note_budget: int,
    format_correction: str,
    spec: _FallbackSpec,
) -> _FallbackCandidate:
    user_request = _timeline_opener(scoped_groups, degraded_opener=degraded_opener)
    user_request = _truncate_head(user_request, spec.timeline_ceiling)
    previous_note = _middle_truncate_previous_note(previous_last_words or "(none)", spec.previous_note_cap)
    work_done = _format_dropped(
        scoped_groups,
        max_chars=spec.timeline_ceiling,
        newest_few=spec.newest_few,
    )
    sections = [
        f"Current turn user request:\n{_prompt_block('user_request', escape_system_reminder_tags(user_request))}",
        f"Previous progress note:\n{_prompt_block('previous_progress_note', escape_system_reminder_tags(previous_note))}",
        (
            "Work done since (about to be dropped from context):\n"
            f"{_prompt_block('work_done_since', escape_system_reminder_tags(work_done))}"
        ),
        (
            "Write the updated progress note now. "
            f"{_FALLBACK_LENGTH_DIRECTIVE.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=note_budget)}"
        ),
    ]
    user_prompt = "\n\n".join(sections)
    system_instruction = _fallback_instruction(
        _truncate_template(template, spec.supplement_cap),
        has_continuation_nudges=has_continuation_nudges,
        max_output_tokens=note_budget,
        format_correction=format_correction,
    )
    return _FallbackCandidate(
        stage=spec.stage,
        messages=(Message("system", [system_instruction]), Message("user", [user_prompt])),
        instruction=system_instruction,
    )


def _fallback_admission_tokens(messages: tuple[Message, ...], *, output_reserve: int) -> int:
    """Conservative whole-request counter for the terminal fallback."""
    total = max(0, output_reserve)
    for message in messages:
        serialized = json.dumps(
            message.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        total += len(serialized.encode("utf-8")) + _FALLBACK_FRAMING_TOKENS_PER_MESSAGE
    return total


def _timeline_opener(scoped_groups: list[ScopedGroup], *, degraded_opener: bool) -> str:
    if degraded_opener:
        return DEGRADED_SCOPED_PREAMBLE
    for group in scoped_groups:
        if group.kind != "user":
            continue
        for message in group.messages:
            text = _user_authored_text(message)
            if text:
                return text
    return "(none)"


def _format_dropped(
    scoped_groups: list[ScopedGroup],
    *,
    max_chars: int = _FALLBACK_MAX_CHARS,
    newest_few: bool = False,
) -> str:
    """Render one bounded timeline with group-local call/result pairing."""
    items: list[_TimelineItem] = []
    opener_seen = False
    for group in scoped_groups:
        if group.kind == "user":
            for message in group.messages:
                if not opener_seen:
                    opener_seen = True
                    continue
                if is_continuation_message(message):
                    continue
                text = _user_authored_text(message)
                if text:
                    items.append(_TimelineItem(f"- user said: {_truncate_head(text, _FALLBACK_FOLLOWUP_MAX)}"))
            continue
        if group.kind == "tool_call":
            item = _format_tool_group(group)
            if item is not None:
                items.append(item)
            continue
        for message in group.messages:
            text = (message.text or "").strip()
            if text:
                items.append(_TimelineItem(f"- agent said: {_truncate_head(text, _FALLBACK_ASSISTANT_MAX)}"))
    return _bound_timeline(items, max_chars=max_chars, newest_few=newest_few)


def _format_tool_group(group: ScopedGroup) -> _TimelineItem | None:
    call_info: dict[str, tuple[str, str]] = {}
    call_order: list[str] = []
    lines: list[str] = []
    for message in group.messages:
        for content in message.contents:
            if content.type not in TOOL_CALL_CONTENT_TYPES:
                continue
            call_id = content.call_id or ""
            name = _call_name(content)
            call_info[call_id] = (name, _format_arguments(content.arguments))
            call_order.append(call_id)
            lines.append(f"- call {name}({call_info[call_id][1]})")
        if message.role == "assistant":
            text = " ".join(
                content.text or "" for content in message.contents if content.type == "text" and content.text
            ).strip()
            if text:
                lines.append(f"- agent said: {_truncate_head(text, _FALLBACK_ASSISTANT_MAX)}")
    for message in group.messages:
        for content in message.contents:
            if not content.type.endswith("_result"):
                continue
            call_id = content.call_id or ""
            name = call_info.get(call_id, ("tool", ""))[0]
            lines.append(f"  result[{name}]: {_result_payload_text(content)}")
    if not lines:
        return None
    return _TimelineItem("\n".join(lines), tool_calls=max(1, len(call_order)))


def _call_name(content: Content) -> str:
    if content.type == "function_call":
        return content.name or "tool"
    if content.type in {"hosted_tool_call", "mcp_server_tool_call", "search_tool_call"}:
        return content.tool_name or "tool"
    return content.type.removesuffix("_tool_call") or "tool"


def _format_arguments(arguments: object) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, Mapping):
        return ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False, default=str)}" for key, value in arguments.items()
        )
    return str(arguments)


def _result_payload_text(content: Content) -> str:
    if content.type in {"function_result", "hosted_tool_result", "search_tool_result"}:
        value = content.items or content.result
    elif content.type == "mcp_server_tool_result":
        value = content.output
    else:
        value = content.outputs
    # The compaction renderer: an image or other binary payload becomes a short
    # placeholder, never its base64 data.
    return _render_tool_result_value(value)


def _user_authored_text(message: Message) -> str:
    parts: list[str] = []
    for content in message.contents:
        if content.type != "text" or not content.text:
            continue
        text = content.text.strip()
        if text.startswith("<system-reminder>\n") and text.endswith("</system-reminder>"):
            continue
        parts.append(content.text)
    return " ".join(parts).strip()


def _bound_timeline(items: list[_TimelineItem], *, max_chars: int, newest_few: bool) -> str:
    ceiling = max(1, max_chars)
    retained = list(items)
    omitted_calls = 0
    if newest_few and len(retained) > _FALLBACK_NEWEST_LINES:
        omitted_calls += sum(item.tool_calls for item in retained[:-_FALLBACK_NEWEST_LINES])
        retained = retained[-_FALLBACK_NEWEST_LINES:]

    def render() -> str:
        lines = [item.text for item in retained]
        if omitted_calls:
            lines.insert(0, f"… {omitted_calls} earlier calls omitted — indexed in the dropped-record manifest")
        return "\n".join(lines) if lines else "(nothing dropped)"

    rendered = render()
    while len(rendered) > ceiling:
        tool_indices = [index for index, item in enumerate(retained) if item.tool_calls]
        if len(tool_indices) <= 1:
            break
        removed = retained.pop(tool_indices[0])
        omitted_calls += removed.tool_calls
        rendered = render()
    if len(rendered) <= ceiling:
        return rendered
    marker = f"{_FALLBACK_TRUNCATION_MARKER}\n"
    if omitted_calls:
        marker = f"… {omitted_calls} earlier calls omitted — indexed in the dropped-record manifest\n{marker}"
    if len(marker) >= ceiling:
        return marker[:ceiling]
    return marker + rendered[-(ceiling - len(marker)) :]


def _truncate_head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_FALLBACK_TRUNCATION_MARKER):
        return _FALLBACK_TRUNCATION_MARKER[:limit]
    keep = limit - len(_FALLBACK_TRUNCATION_MARKER)
    return text[:keep] + _FALLBACK_TRUNCATION_MARKER


def _middle_truncate_previous_note(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(_FALLBACK_PREV_NOTE_TRUNCATION_MARKER):
        return _FALLBACK_PREV_NOTE_TRUNCATION_MARKER[:limit]
    keep = limit - len(_FALLBACK_PREV_NOTE_TRUNCATION_MARKER)
    head = (keep + 1) // 2
    tail = keep - head
    return f"{text[:head]}{_FALLBACK_PREV_NOTE_TRUNCATION_MARKER}{text[len(text) - tail :]}"
