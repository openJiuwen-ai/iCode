# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared note builders, provider fakes and the scoped-timeline harness for the LastWordsGenerator tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from chrys.foundation.retry import RetryAttemptInfo
from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator
from chrys.service.context.compaction.scoped import DEGRADED_SCOPED_PREAMBLE, ScopedGroup
from chrys.service.profiles.models.resolver import default_profile
from tests.service.context.compaction._compaction_helpers import _user as user

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from chrys.kernel import Message

__all__ = [
    "CharacterTokenizer",
    "FailingFallbackClient",
    "FakeCompleter",
    "FallbackClient",
    "SequenceFallbackClient",
    "generate",
    "long_structured_note",
    "make_generator",
    "retry_collector",
    "status_collector",
    "structured_note",
    "user",
]


def structured_note(*, task: str = "Do the task", progress: str = "Work is underway", next_: str = "Finish it") -> str:
    """Build the shortest note that satisfies the required-heading contract."""
    return f"## Task\n{task}\n\n## Progress\n{progress}\n\n## Next\n{next_}"


def long_structured_note(fill: str = "detail") -> str:
    """Build a structured note long enough to clear the production note-length floor."""
    return structured_note(progress=(fill + " ") * 200)


class FakeCompleter:
    """LastWordsCompleter fake: replays a scripted sequence of results.

    Each entry is either the note text to return or an exception to raise.
    The final entry repeats once the script is exhausted.
    """

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    async def complete_last_words(self, base_messages, instruction, *, max_output_tokens, on_usage=None):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "base_messages": list(base_messages),
                "instruction": instruction,
                "max_output_tokens": max_output_tokens,
                "on_usage": on_usage,
            }
        )
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, Exception):
            raise result
        return result


class FallbackClient:
    """Reconstruction-path client fake returning a fixed note.

    It records the prompts it was sent, which is what the scenarios that build a
    corrective instruction assert on.
    """

    def __init__(self, text: str | None = None) -> None:
        self.text = text if text is not None else structured_note()
        self.calls = 0
        self.messages: list[list[Message]] = []

    async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(list(messages))

        class _Response:
            usage_details = None
            additional_properties: ClassVar[dict[str, object]] = {}
            messages: ClassVar[tuple[Message, ...]] = ()
            raw_text = self.text

        return _Response()


class SequenceFallbackClient:
    """Reconstruction-path client fake replaying a scripted sequence of notes.

    The final entry repeats once the script is exhausted. Only the call count is
    observable: these scenarios assert on how many attempts the retry ladder
    spent, not on the prompts it sent.
    """

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        text = self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]

        class _Response:
            usage_details = None
            additional_properties: ClassVar[dict[str, object]] = {}
            messages: ClassVar[tuple[Message, ...]] = ()
            raw_text = text

        return _Response()


class FailingFallbackClient:
    """Reconstruction-path client fake whose every call raises.

    Only the call count is observable — a scenario about the retry ladder never
    gets a response to inspect.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise self._error


def status_collector() -> tuple[list[CompactionStatus], Callable[[CompactionStatus], Awaitable[None]]]:
    """A ``publish_status`` callback and the list it records into.

    The two are kept apart on purpose: a callback that were also a collection
    would let production code read the statuses back out of it and still pass.
    """
    statuses: list[CompactionStatus] = []

    async def publish_status(status: CompactionStatus) -> None:
        statuses.append(status)

    return statuses, publish_status


def retry_collector() -> tuple[list[RetryAttemptInfo], Callable[[RetryAttemptInfo], Awaitable[None]]]:
    """A ``publish_retry`` callback and the list it records into, kept apart as above."""
    attempts: list[RetryAttemptInfo] = []

    async def publish_retry(info: RetryAttemptInfo) -> None:
        attempts.append(info)

    return attempts, publish_retry


class CharacterTokenizer:
    """Tokenizer whose token count is the character count, for exact budget math."""

    def count_tokens(self, text: str) -> int:
        return len(text)


def make_generator(tmp_path: Path, **kwargs) -> LastWordsGenerator:  # type: ignore[no-untyped-def]
    """Build a generator on the default model profile logging into *tmp_path*."""
    return LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, **kwargs)


async def generate(
    generator,  # type: ignore[no-untyped-def]
    user_request: str,
    previous_last_words: str | None,
    dropped_messages: list[Message],
    *,
    followup_texts: list[str] | None = None,
    has_continuation_nudges: bool = False,
    degraded_opener: bool = False,
    completer=None,  # type: ignore[no-untyped-def]
    spend_side_call_tokens=None,  # type: ignore[no-untyped-def]
):
    """Build a compact scoped timeline for generator-focused tests."""
    opener = DEGRADED_SCOPED_PREAMBLE if degraded_opener else user_request
    groups = [ScopedGroup("opener", "user", (user(opener),), True)]
    groups.extend(
        ScopedGroup(f"followup-{index}", "user", (user(text),), True) for index, text in enumerate(followup_texts or [])
    )
    tool_messages = tuple(
        message
        for message in dropped_messages
        if any(content.type.endswith("_call") or content.type.endswith("_result") for content in message.contents)
    )
    if tool_messages:
        groups.append(ScopedGroup("tools", "tool_call", tool_messages, True))
    groups.extend(
        ScopedGroup(f"assistant-{index}", "assistant_text", (message,), bool(message.contents))
        for index, message in enumerate(dropped_messages)
        if message not in tool_messages
    )
    return await generator.generate(
        groups,
        previous_last_words,
        degraded_opener=degraded_opener,
        has_continuation_nudges=has_continuation_nudges,
        completer=completer,
        spend_side_call_tokens=spend_side_call_tokens,
    )
