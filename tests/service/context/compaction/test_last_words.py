# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LastWordsGenerator lifecycle: transport retries, status publishing, cancellation, client wiring and logging."""

from __future__ import annotations

import asyncio
import os
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from chrys.foundation.retry import RetryAttemptInfo
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType, RetryMode
from chrys.kernel import Content, Message
from chrys.service.context.compaction.last_words import (
    LastWordsGenerationError,
    LastWordsSpendBudgetExceeded,
    _format_dropped,
)
from chrys.service.context.compaction.scoped import ScopedGroup
from tests.orchestration.invoker._main_pass import fresh_pass
from tests.service.context.compaction._last_words_helpers import (
    generate,
    make_generator,
    retry_collector,
    status_collector,
    structured_note,
    user,
)
from tests.service.trajectory._fakes import FakeSink, make_context

pytestmark = pytest.mark.usefixtures("no_note_floor")


class TestFormatDropped:
    def test_handles_empty_list(self) -> None:
        out = _format_dropped([])
        assert out == "(nothing dropped)"

    def test_pairs_reused_call_ids_within_each_group(self) -> None:
        big = "x" * 2000
        big_args = {"path": "y" * 500}
        groups = [
            ScopedGroup(
                "opener",
                "user",
                (user("do it"),),
                True,
            ),
            ScopedGroup(
                "first",
                "tool_call",
                (
                    Message(
                        role="assistant",
                        contents=[Content.from_function_call("c1", "tool_1", arguments=big_args)],
                    ),
                    Message(role="tool", contents=[Content.from_function_result("c1", result=big)]),
                ),
                True,
            ),
            ScopedGroup(
                "second",
                "tool_call",
                (
                    Message(
                        role="assistant",
                        contents=[Content.from_function_call("c1", "tool_2", arguments={})],
                    ),
                    Message(role="tool", contents=[Content.from_function_result("c1", result="second")]),
                ),
                True,
            ),
        ]
        out = _format_dropped(groups)
        assert "tool_1" in out
        assert "x" * 2000 in out
        assert "y" * 500 in out
        assert "result[tool_2]: second" in out

    def test_bounds_timeline_by_collapsing_oldest_tool_groups(self) -> None:
        groups = [ScopedGroup("opener", "user", (user("do it"),), True)]
        for index in range(8):
            call_id = f"c{index}"
            groups.append(
                ScopedGroup(
                    f"tool-{index}",
                    "tool_call",
                    (
                        Message("assistant", [Content.from_function_call(call_id, f"tool_{index}", arguments={})]),
                        Message("tool", [Content.from_function_result(call_id, result=str(index) * 1_000)]),
                    ),
                    True,
                )
            )

        out = _format_dropped(groups, max_chars=3_500)

        assert len(out) <= 3_500
        assert "earlier calls omitted" in out
        assert "indexed in the dropped-record manifest" in out
        assert "tool_0" not in out
        assert "tool_7" in out

    def test_attributes_only_user_authored_followup_content(self) -> None:
        reminder = "<system-reminder>\n[Runtime Environment] secret\n</system-reminder>"
        groups = [
            ScopedGroup("opener", "user", (user("do it"),), True),
            ScopedGroup("followup", "user", (Message("user", ["also test", reminder]),), True),
        ]

        out = _format_dropped(groups)

        assert "- user said: also test" in out
        assert "Runtime Environment" not in out


# Prior-turn prompt reconstruction was deleted by the scoped Phase-4 design.


async def test_last_words_generator_raises_on_llm_failure(tmp_path):
    """Non-retryable failures are wrapped without replaying the outer agent run."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)

    class _BrokenClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("network down")

    client = _BrokenClient()
    gen._client = client  # type: ignore[assignment]

    with pytest.raises(LastWordsGenerationError) as excinfo:
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
        )
    assert client.calls == 1
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "network down" in str(excinfo.value.__cause__)


async def test_last_words_generator_retries_empty_response_then_raises(tmp_path, monkeypatch):
    """An empty LLM response is retried locally against the same compaction input."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 2)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))

    class _EmptyResponse:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = ""

    class _EmptyClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return _EmptyResponse()

    from chrys.service.profiles.models.resolver import default_profile

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    client = _EmptyClient()
    gen._client = client  # type: ignore[assignment]
    retry_events, publish_retry = retry_collector()

    gen._publish_retry = publish_retry

    with pytest.raises(LastWordsGenerationError):
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
        )
    assert client.calls == 3
    assert retry_events == [
        RetryAttemptInfo(reason="empty response", attempt=1, max_attempts=2, delay_seconds=0),
        RetryAttemptInfo(reason="empty response", attempt=2, max_attempts=2, delay_seconds=0),
    ]


async def test_last_words_generator_retries_transient_failure_and_succeeds(tmp_path, monkeypatch):
    """Transient LAST_WORDS failures resend only the summariser prompt."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    monkeypatch.setattr(LastWordsGenerator, "_MAX_RETRIES", 2)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("connection dropped")
            return _Response()

    client = _FlakyClient()
    retry_events, publish_retry = retry_collector()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_retry=publish_retry)
    gen._client = client  # type: ignore[assignment]

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        out = await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
        )

    assert out == structured_note()
    assert client.calls == 2
    assert retry_events == [RetryAttemptInfo(reason="connection dropped", attempt=1, max_attempts=2, delay_seconds=0)]
    scheduled = sink.only(EventType.RETRY_SCHEDULED)
    started = sink.only(EventType.RETRY_STARTED)
    assert scheduled.payload["retry_mode"] == started.payload["retry_mode"] == RetryMode.COMPACTION


async def test_fallback_attempts_report_wire_progress_before_each_dispatch(tmp_path, monkeypatch):
    """Each reconstruction attempt restarts the waiting pull's stall watchdog,
    so a retried note gets a full idle window per attempt."""
    from chrys.kernel.client import start_with_wire_progress
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    monkeypatch.setattr(LastWordsGenerator, "_MAX_RETRIES", 2)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))
    reports = 0

    def _on_progress() -> None:
        nonlocal reports
        reports += 1

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    at_dispatch: list[int] = []

    class _FlakyClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            at_dispatch.append(reports)
            if len(at_dispatch) == 1:
                raise ConnectionError("connection dropped")
            return _Response()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _FlakyClient()  # type: ignore[assignment]

    out = await start_with_wire_progress(
        generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[]), _on_progress
    )

    assert out == structured_note()
    assert at_dispatch == [1, 2]


async def test_injected_zero_transient_budget_disables_fallback_transport_retry(tmp_path):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _FailingClient:
        calls = 0

        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise ConnectionError("connection dropped")

    client = _FailingClient()
    gen = LastWordsGenerator(
        profile=default_profile(),
        log_dir=tmp_path,
        max_transient_retries=0,
    )
    gen._client = client  # type: ignore[assignment]

    with pytest.raises(LastWordsGenerationError):
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert client.calls == 1


async def test_zero_transient_budget_keeps_fixed_corrective_retry_events(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    retry_events, publish_retry = retry_collector()

    class _Client:
        calls = 0

        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = "" if self.calls == 1 else structured_note()

            return _Response()

    client = _Client()
    gen = LastWordsGenerator(
        profile=default_profile(),
        log_dir=tmp_path,
        max_transient_retries=0,
        publish_retry=publish_retry,
    )
    gen._client = client  # type: ignore[assignment]

    assert await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[]) == structured_note()
    assert client.calls == 2
    assert retry_events == [RetryAttemptInfo(reason="empty response", attempt=1, max_attempts=5, delay_seconds=0)]


async def test_last_words_generator_publishes_status_around_success(tmp_path):
    """A started/finished status pair brackets successful note generation."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _Client:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return _Response()

    statuses, publish_status = status_collector()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=publish_status)
    gen._client = _Client()  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == structured_note()
    assert [s.stage for s in statuses] == ["started", "finished"]
    started, finished = statuses
    assert started.compaction_id
    assert started.compaction_id == finished.compaction_id
    assert finished.outcome == "ok"
    assert finished.last_words == structured_note()
    assert finished.duration_ms >= 0


async def test_last_words_generator_publishes_failed_status_on_terminal_error(tmp_path):
    """Terminal generation failure still emits the finished status (no dangling UX)."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _BrokenClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("network down")

    statuses, publish_status = status_collector()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=publish_status)
    gen._client = _BrokenClient()  # type: ignore[assignment]

    with pytest.raises(LastWordsGenerationError):
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert [s.stage for s in statuses] == ["started", "finished"]
    assert statuses[1].outcome == "failed"
    assert statuses[1].last_words == ""
    assert statuses[1].failure_reason == ""


async def test_last_words_generator_spend_refusal_sets_failure_reason(tmp_path):
    """A spend-budget refusal names its cause on the finished status."""
    from chrys.service.context.compaction.last_words import (
        SPEND_BUDGET_FAILURE_REASON,
        LastWordsGenerator,
    )
    from chrys.service.profiles.models.resolver import default_profile

    statuses, publish_status = status_collector()

    class _NeverCalledClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("refused attempt must not reach the provider")

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=publish_status)
    gen._client = _NeverCalledClient()  # type: ignore[assignment]

    with pytest.raises(LastWordsSpendBudgetExceeded):
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            spend_side_call_tokens=lambda _tokens: False,
        )

    assert [s.stage for s in statuses] == ["started", "finished"]
    assert statuses[1].outcome == "failed"
    assert statuses[1].failure_reason == SPEND_BUDGET_FAILURE_REASON


async def test_last_words_generator_publish_breaker_trip_emits_failed_pair(tmp_path):
    """An entry-time breaker trip publishes an immediate started+failed pair."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    statuses, publish_status = status_collector()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=publish_status)

    await gen.publish_breaker_trip("20 attempts limit exceeded for current turn")

    assert [s.stage for s in statuses] == ["started", "finished"]
    started, finished = statuses
    assert started.compaction_id == finished.compaction_id
    assert finished.outcome == "failed"
    assert finished.failure_reason == "20 attempts limit exceeded for current turn"
    assert finished.last_words == ""


async def test_last_words_generator_publish_committed_correlates_with_last_generate(tmp_path):
    """publish_committed emits stage="committed" with the last generate's id; no-op before any."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _Client:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return _Response()

    statuses, publish_status = status_collector()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=publish_status)
    gen._client = _Client()  # type: ignore[assignment]

    # Before any generate there is nothing to correlate — publish nothing.
    await gen.publish_committed()
    assert statuses == []

    await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
    await gen.publish_committed()

    assert [s.stage for s in statuses] == ["started", "finished", "committed"]
    assert statuses[2].compaction_id == statuses[0].compaction_id
    assert statuses[2].outcome == ""

    # The id is consumed on publish — a stray second call must not re-emit
    # "committed" for an already-signalled round.
    await gen.publish_committed()
    assert [s.stage for s in statuses] == ["started", "finished", "committed"]


async def test_publish_committed_delivers_despite_cancellation(tmp_path):
    """A cancel landing mid-publish must not erase the committed signal.

    The round is already durable when publish_committed runs, so the
    delivery is shielded: the cancellation propagates to the caller while
    the detached publish finishes in the background.
    """
    from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile
    from tests.support.waiting import wait_until

    delivered: list[str] = []
    reached = asyncio.Event()
    gate = asyncio.Event()

    async def _publish_status(status: CompactionStatus) -> None:
        reached.set()
        await gate.wait()
        delivered.append(status.stage)

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=_publish_status)
    gen._last_compaction_id = "c0ffee00"

    task = asyncio.create_task(gen.publish_committed())
    assert await wait_until(reached.is_set)  # the publish is suspended mid-delivery
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert delivered == []  # cancellation propagated before delivery...
    gate.set()
    assert await wait_until(lambda: bool(delivered))
    assert delivered == ["committed"]  # ...but the detached publish completed


async def test_last_words_generator_publishes_canceled_status_on_interrupt(tmp_path):
    """Cancellation mid-generation emits finished(canceled) and re-raises."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _CancelledClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError

    statuses, publish_status = status_collector()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=publish_status)
    gen._client = _CancelledClient()  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert [s.stage for s in statuses] == ["started", "finished"]
    assert statuses[1].outcome == "canceled"
    assert statuses[1].last_words == ""


async def test_last_words_generator_cancelled_during_started_publish_skips_generation(tmp_path):
    """An interrupt delivered while the started signal awaits frontend
    handlers must abort compaction BEFORE the LLM call — not be swallowed
    as a UX-publish failure — while still emitting finished(canceled) so
    the live indicator never dangles."""
    from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    llm_calls: list[str] = []

    class _Client:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            llm_calls.append("get_response")
            raise AssertionError("LLM call must not start after cancellation")

    statuses: list[CompactionStatus] = []

    async def _publish_status(status: CompactionStatus) -> None:
        statuses.append(status)
        if status.stage == "started":
            raise asyncio.CancelledError

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=_publish_status)
    gen._client = _Client()  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert llm_calls == []
    assert [s.stage for s in statuses] == ["started", "finished"]
    assert statuses[1].outcome == "canceled"
    assert statuses[1].last_words == ""


async def test_last_words_generator_cancelled_during_finished_publish_raises(tmp_path):
    """An interrupt delivered while the finished signal awaits frontend
    handlers must propagate — returning the already-generated note would
    silently swallow the user's cancellation."""
    from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _Client:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return _Response()

    statuses: list[CompactionStatus] = []

    async def _publish_status(status: CompactionStatus) -> None:
        statuses.append(status)
        if status.stage == "finished":
            raise asyncio.CancelledError

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=_publish_status)
    gen._client = _Client()  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    # The finished signal still carried the real outcome before the cancel.
    assert [s.stage for s in statuses] == ["started", "finished"]
    assert statuses[1].outcome == "ok"


async def test_last_words_generator_fresh_cancel_in_finished_publish_keeps_original_cancel(tmp_path):
    """When the finally is already unwinding a cancellation, a fresh
    CancelledError from the finished publish is absorbed so the original
    cancellation keeps propagating (not replaced, not swallowed)."""
    from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _CancelledClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError

    statuses: list[CompactionStatus] = []

    async def _publish_status(status: CompactionStatus) -> None:
        statuses.append(status)
        if status.stage == "finished":
            raise asyncio.CancelledError

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=_publish_status)
    gen._client = _CancelledClient()  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert [s.stage for s in statuses] == ["started", "finished"]
    assert statuses[1].outcome == "canceled"


async def test_last_words_generator_swallows_status_publish_failures(tmp_path):
    """Status publication is UX-only; a broken publisher must not fail generation."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _Client:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return _Response()

    async def _publish_status(_status: object) -> None:
        raise RuntimeError("bus is down")

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, publish_status=_publish_status)
    gen._client = _Client()  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == structured_note()


async def test_last_words_generator_uses_model_profile_stream_setting(tmp_path):
    """Phase 4 LAST_WORDS calls should honor ``ModelProfile.stream``."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _Response:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _Stream:
        def __init__(self) -> None:
            self._done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return object()

        async def get_final_response(self):
            return _Response()

    class _Client:
        def __init__(self) -> None:
            self.streams: list[bool] = []

        async def get_response(self, _messages, *, stream=False, **_kwargs):
            self.streams.append(stream)
            if stream:
                return _Stream()
            return _Response()

    profile = default_profile()
    profile.stream = True
    client = _Client()
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    gen._client = client  # type: ignore[assignment]

    output = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
    )

    assert output == structured_note()
    assert client.streams == [True]


async def test_last_words_generator_passes_session_ids_to_client(tmp_path):
    """Phase 4 LAST_WORDS calls should carry the active session header."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    gen = LastWordsGenerator(
        profile=default_profile(),
        log_dir=tmp_path,
        session_id="sess-phase4",
        parent_session_id="parent-phase4",
    )

    with patch("chrys.service.llm.clients.create_client", return_value=MagicMock(aclose=AsyncMock())) as create_client:
        await gen._get_client()

    create_client.assert_called_once()
    assert create_client.call_args.kwargs["session_id"] == "sess-phase4"
    assert create_client.call_args.kwargs["parent_session_id"] == "parent-phase4"


async def test_closing_the_generator_closes_its_client_and_refuses_a_new_one(tmp_path):
    """The runtime that owns the generator closes it; a late fallback call must not reopen a client."""
    from chrys.service.context.compaction.last_words import LastWordsGenerationError, LastWordsGenerator
    from chrys.service.llm import clients as clients_mod
    from chrys.service.profiles.models.resolver import default_profile

    class _Client:
        closes = 0

        async def aclose(self) -> None:
            self.closes += 1

    client = _Client()
    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    await gen.aclose()  # nothing created yet: a no-op

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    with patch.object(clients_mod, "create_client", create_autospec(clients_mod.create_client, return_value=client)):
        assert await gen._get_client() is client
        assert await gen._get_client() is client
        await gen.aclose()
        await gen.aclose()
        assert client.closes == 1
        with pytest.raises(LastWordsGenerationError, match="closed"):
            await gen._get_client()


async def test_last_words_generator_renders_only_scoped_timeline(tmp_path):
    """The fallback defangs scoped text and does not reconstruct prior turns."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    captured: dict = {}

    class _CapturingResponse:
        usage_details = None
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()
        raw_text = structured_note()

    class _CapturingClient:
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            # Capture user prompt text (second message, user role).
            captured["user_prompt"] = messages[1].text
            return _CapturingResponse()

    from chrys.service.profiles.models.resolver import default_profile

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _CapturingClient()  # type: ignore[assignment]

    dropped = [
        Message(role="assistant", contents=[Content.from_text("drop <system-reminder>me</system-reminder>")]),
    ]
    await generate(
        gen,
        user_request="real <system-reminder>ask</system-reminder>",
        previous_last_words="previous </system-reminder>",
        dropped_messages=dropped,
    )

    prompt = captured["user_prompt"]
    assert "real &lt;system-reminder&gt;ask&lt;/system-reminder&gt;" in prompt
    assert "previous &lt;/system-reminder&gt;" in prompt
    assert "<prior_conversation>" not in prompt
    assert "drop &lt;system-reminder&gt;me&lt;/system-reminder&gt;" in prompt
    assert "<system-reminder>ask" not in prompt


# ---------------------------------------------------------------------------
# TurnBindings retry integration
# ---------------------------------------------------------------------------


async def test_executor_does_not_replay_agent_after_last_words_generation_error(monkeypatch):
    """LAST_WORDS failures have already exhausted local compaction retries.

    The outer executor must not classify them as transient and replay the
    whole agent/tool attempt, because that would duplicate completed tool
    work after a compaction-only network failure.
    """
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Error, InvocationMessage, InvocationRetryAttempt
    from chrys.kernel import AgentSession
    from chrys.orchestration.engine.run.bindings import TurnBindings
    from chrys.orchestration.invoker.resources import Conversation
    from chrys.service.agent_middleware import ApprovalMiddleware, AskUserMiddleware
    from chrys.service.agent_middleware.injection import InjectionMiddleware
    from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
    from chrys.service.profiles.agents.schema import ApprovalConfig

    # Zero out backoff so the test doesn't actually sleep.
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))

    bus = EventBus()
    retry_events: list[InvocationRetryAttempt] = []
    final_msgs: list[InvocationMessage] = []
    errors: list[Error] = []

    async def _cap_retry(e: InvocationRetryAttempt) -> None:
        retry_events.append(e)

    async def _cap_msg(e: InvocationMessage) -> None:
        if e.is_final:
            final_msgs.append(e)

    async def _cap_error(e: Error) -> None:
        errors.append(e)

    await bus.subscribe(InvocationRetryAttempt, _cap_retry)
    await bus.subscribe(InvocationMessage, _cap_msg)
    await bus.subscribe(Error, _cap_error)

    # Stub agent whose first run raises LastWordsGenerationError after the
    # generator's local retry budget has already been exhausted. A second
    # scripted response exists only to prove the outer executor does not
    # re-invoke agent.run().
    call_count = 0

    class _StubResponse:
        def __init__(self) -> None:
            self.messages = [Message(role="assistant", contents=[Content.from_text("Done.")])]

    class _StubAgent:
        async def run(self, _input, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Mirror the real wrapping in last_words.py: even with a
                # retryable transport cause, the outer loop must not retry
                # LastWordsGenerationError.
                try:
                    raise ConnectionError("network down")
                except ConnectionError as exc:
                    raise LastWordsGenerationError("Failed to generate last words for the current task") from exc
            return _StubResponse()

    approval_mw = ApprovalMiddleware(
        approval_policy=ApprovalPolicy(ApprovalConfig(default="auto"), tools=[]),
        event_bus=bus,
        approval_mode=ApprovalMode.BYPASS,
    )
    ask_user_mw = AskUserMiddleware(event_bus=bus)
    injection_mw = InjectionMiddleware()
    session = AgentSession()

    executor = TurnBindings(
        conversation=Conversation(),
        agent=_StubAgent(),  # type: ignore[arg-type]
        session=session,
        event_bus=bus,
        approval_middleware=approval_mw,
        ask_user_middleware=ask_user_mw,
        injection_middleware=injection_mw,
        stream=False,
    )

    await fresh_pass(executor, ["hello"])

    assert call_count == 1
    assert retry_events == []
    assert final_msgs == []
    assert len(errors) == 1
    assert "network down" in errors[0].message
    assert executor.state.run_failed
    assert not executor.state.was_interrupted


def test_write_log_creates_owner_only_file(tmp_path):
    """Debug logs carry note text and raw model output — owner-only like spill records."""
    gen = make_generator(tmp_path)
    gen._write_log("instruction text", "raw response", "final note", request_note="stats line")
    logs = list(tmp_path.glob("last_words_*.log"))
    assert len(logs) == 1
    if os.name == "posix":
        assert (logs[0].stat().st_mode & 0o777) == 0o600
    text = logs[0].read_text(encoding="utf-8")
    assert "--- REQUEST ---\nstats line" in text
    assert "--- INSTRUCTION ---\ninstruction text" in text
    assert "--- RAW RESPONSE ---\nraw response" in text
    assert "--- LAST_WORDS ---\nfinal note" in text
    assert "--- ERROR ---" not in text
