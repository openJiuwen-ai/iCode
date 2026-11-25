# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reminder lifecycles: delivery, consumption, rollback and restore.

Each test drives the reminder stack through its roles
(``tests/support/reminder_stack.py``), so a refactor that moves an owner
changes the stack module, never these assertions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import EXCLUDED_KEY, ChatResponse, Message
from chrys.kernel.middleware import ChatContext
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.agent_middleware.reminders.context_usage import CONTEXT_USAGE_WARNING
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState
from tests.service.context.compaction._compaction_helpers import (
    _build_single_turn,
    _estimate_tokens,
    _forced_phase4,
)
from tests.support.phase4_stubs import StubLastWordsGenerator
from tests.support.reminder_calls import establish_request
from tests.support.reminder_inputs import (
    ARCHIVED,
    MAX_CONTEXT_TOKENS,
    MCP,
    RUNTIME,
    SKILLS,
    SKILLS_REFRESHED,
    TODO_A,
    TODO_B,
    LoggingSpillQuota,
    ReminderProviders,
    assistant,
    manifest_entry,
    pin_reminder_inputs,
    plant_catalog,
    require,
    spill_quota,
    usage,
    user,
)
from tests.support.reminder_stack import (
    ReminderStack,
    held_catalogs,
    make_reminder_stack,
    observe,
    reminder_generation,
    restore_phase4,
    warning_armed,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.context.compaction.spill import SpillQuota

_RECORD = HistoryMarkerKind.SYSTEM_REMINDERS_KEY
_SWITCH_NOTICE_START = "<system-reminder>\n[Agent profile switched from 'Code' to 'QA']"
_LAST_WORDS_START = "<system-reminder>\n[LAST_WORDS] "


class _CallFailed(Exception):
    """The provider failed after the call's requests (if any) went out."""


def _wrapped(text: str) -> str:
    return f"<system-reminder>\n{text}\n</system-reminder>"


def _texts(message: Message) -> list[str]:
    return [require(content.text) for content in message.contents]


def _stack(
    inputs: ReminderProviders | None = None,
    *,
    runtime: SessionEnvironment | None = None,
    shell_tool_enabled: bool = False,
    session_root: Path | None = None,
    spill_quota: SpillQuota | None = None,
    file_read_available: bool = False,
) -> ReminderStack:
    inputs = inputs if inputs is not None else ReminderProviders()
    return make_reminder_stack(
        runtime,
        max_context_tokens=MAX_CONTEXT_TOKENS,
        shell_tool_enabled=shell_tool_enabled,
        session_root=session_root,
        file_read_available=file_read_available,
        spill_quota=spill_quota,
        skill_catalog_provider=inputs.read_skills,
        todo_state_provider=inputs.read_todo,
        mcp_instructions_provider=inputs.read_mcp,
        file_change_provider=inputs.drain_file_change,
    )


async def _call(
    stack: ReminderStack,
    messages: list[Message],
    *,
    requests: int = 1,
    fail: bool = False,
    options: dict[str, Any] | None = None,
    answer: str | None = None,
    during: Callable[[], None] | None = None,
) -> list[Message]:
    """Run one call; *during* runs inside ``call_next`` before its *requests* go out, *fail* raises after them."""
    context = ChatContext(client=None, messages=list(messages), options=options if options is not None else {})

    async def _call_next() -> None:
        if during is not None:
            during()
        for _ in range(requests):
            await establish_request(context)
        if fail:
            raise _CallFailed
        context.result = ChatResponse(messages=[], conversation_id=answer)

    if fail:
        with pytest.raises(_CallFailed):
            await stack.middleware.process(context, _call_next)
    else:
        await stack.middleware.process(context, _call_next)
    return cast("list[Message]", context.messages)


# ---------------------------------------------------------------------------
# L1, L2 — file-change delivery and repeated observers
# ---------------------------------------------------------------------------


async def test_file_change_notice_is_handed_back_until_a_request_carries_it() -> None:
    inputs = ReminderProviders(file_change="src/a.py changed")
    stack = _stack(inputs)
    stack.middleware.prepare_turn()

    await _call(stack, [user("go")], requests=0, fail=True)

    assert stack.middleware.take_undelivered_file_change() == "src/a.py changed"
    assert stack.middleware.take_undelivered_file_change() is None

    inputs.file_change = "src/b.py changed"
    stack.middleware.prepare_turn()
    sent = await _call(stack, [user("go again")])

    assert _wrapped("src/b.py changed") in _texts(sent[0])
    assert stack.middleware.take_undelivered_file_change() is None


async def test_file_change_delivery_marks_the_turn_the_call_was_enriched_from() -> None:
    inputs = ReminderProviders(file_change="src/a.py changed")
    stack = _stack(inputs)
    stack.middleware.prepare_turn()

    def _retry_prepare() -> None:
        inputs.file_change = "src/b.py changed"
        stack.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)

    await _call(stack, [user("go")], during=_retry_prepare)

    assert stack.middleware.take_undelivered_file_change() == "src/b.py changed"


async def test_repeated_request_observers_record_once_and_deliver_once() -> None:
    stack = _stack(ReminderProviders(todo=TODO_A, file_change="src/a.py changed"))
    stack.middleware.prepare_turn()
    u1 = user("go")

    await _call(stack, [u1], requests=2)

    assert u1.additional_properties[_RECORD] == [
        {"kind": "event", "text": "src/a.py changed"},
        {"kind": "catalog", "text": TODO_A, "name": "todo"},
    ]
    assert stack.middleware.take_undelivered_file_change() is None


# ---------------------------------------------------------------------------
# L3 — the context-usage warning
# ---------------------------------------------------------------------------


async def test_context_warning_disarms_when_sent_and_rearms_below_the_threshold() -> None:
    stack = _stack()
    u1, a1, u2, a2, u3, a3, u4 = (
        user("one"),
        assistant("1"),
        user("two"),
        assistant("2"),
        user("three"),
        assistant("3"),
        user("four"),
    )

    stack.middleware.prepare_turn(usage=usage(60))
    sent = await _call(stack, [u1])
    assert _wrapped(CONTEXT_USAGE_WARNING) in _texts(sent[0])
    assert not warning_armed(stack)

    stack.middleware.prepare_turn(usage=usage(70))
    sent = await _call(stack, [u1, a1, u2])
    texts = _texts(sent[2])
    assert len(texts) == 2
    assert texts[1].startswith("<system-reminder>\n[Context Usage] current: 70")
    assert _wrapped(CONTEXT_USAGE_WARNING) not in texts

    stack.middleware.prepare_turn(usage=usage(30))
    assert warning_armed(stack)
    await _call(stack, [u1, a1, u2, a2, u3])

    stack.middleware.prepare_turn(usage=usage(60))
    sent = await _call(stack, [u1, a1, u2, a2, u3, a3, u4])
    assert _wrapped(CONTEXT_USAGE_WARNING) in _texts(sent[6])
    assert not warning_armed(stack)


async def test_context_warning_stays_armed_when_no_request_carried_it() -> None:
    stack = _stack()
    u1, u2 = user("one"), user("two")

    stack.middleware.prepare_turn(usage=usage(60))
    await _call(stack, [u1], requests=0, fail=True)
    assert warning_armed(stack)

    stack.middleware.prepare_turn(usage=usage(60))
    sent = await _call(stack, [u1, assistant("1"), u2])
    assert _wrapped(CONTEXT_USAGE_WARNING) in _texts(sent[2])
    assert not warning_armed(stack)


async def test_context_warning_recorded_before_a_restart_is_not_resent() -> None:
    before = _stack()
    before.middleware.prepare_turn(usage=usage(60))
    u1 = user("one")
    sent_before = await _call(before, [u1])
    assert _wrapped(CONTEXT_USAGE_WARNING) in _texts(sent_before[0])

    after = _stack()
    after.middleware.prepare_turn(usage=usage(60), preserve_turn_reminders=True, preserve_last_words=True)
    sent_after = await _call(after, [u1])

    assert _texts(sent_after[0]) == _texts(sent_before[0])
    assert warning_armed(after)


# ---------------------------------------------------------------------------
# L4 — Phase 4 retry rollback
# ---------------------------------------------------------------------------


def test_phase4_retry_rollback_restores_content_and_keeps_turn_accounting() -> None:
    inputs = ReminderProviders(todo=TODO_A)
    stack = _stack(inputs)
    stack.middleware.prepare_turn()
    first = manifest_entry(2, 1, "read_file", "src/a.py")
    lw = stack.last_words
    lw.set_last_words("note 1")
    lw.append_manifest([first])
    snapshot = lw.snapshot_phase4_retry_state()

    inputs.todo = TODO_B
    lw.set_last_words("note 2")
    lw.append_manifest([manifest_entry(2, 2, "shell", "make")])
    breaker = DropRoundBreakerState(attempts=2, side_call_tokens=500)
    lw.set_drop_round_breaker(breaker)
    assert lw.claim_context_pressure_notification() is True

    lw.restore_phase4_retry_state(snapshot)

    assert lw.get_last_words() == "note 1"
    assert [entry["relative_path"] for entry in lw.get_last_words_manifest()] == [first.relative_path]
    rendered = require(lw.render_last_words_reminder_text())
    assert TODO_A in rendered
    assert TODO_B not in rendered
    assert lw.get_last_words_breaker_state() == breaker.to_state()
    assert lw.claim_context_pressure_notification() is False


async def test_preserving_retry_resends_the_failed_calls_phase4_state() -> None:
    inputs = ReminderProviders(todo=TODO_A)
    stack = _stack(inputs)
    stack.middleware.prepare_turn()
    lw = stack.last_words
    lw.set_last_words("note before the failure")
    lw.append_manifest([manifest_entry(2, 1, "read_file", "src/a.py")])
    lw.set_drop_round_breaker(DropRoundBreakerState(attempts=1, side_call_tokens=700))
    assert lw.claim_context_pressure_notification() is True
    messages = [user("request")]
    failed = await _call(stack, messages, fail=True)
    state = observe(stack)

    inputs.todo = TODO_B
    stack.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)
    retried = await _call(stack, messages)

    sent = _texts(failed[-1])[-1]
    assert sent.startswith(_LAST_WORDS_START)
    assert "--- Dropped this turn" in sent
    assert TODO_A in sent
    assert _texts(retried[-1])[-1] == sent
    assert observe(stack) == state
    assert lw.claim_context_pressure_notification() is False


# ---------------------------------------------------------------------------
# L5 — expired scopes and stale targets
# ---------------------------------------------------------------------------


async def test_expired_scope_target_cannot_touch_the_next_turn() -> None:
    inputs = ReminderProviders(skills=SKILLS)
    stack = _stack(inputs)
    mw = stack.middleware
    old_scope = mw.create_current_run_scope()
    mw.prepare_turn(reminder_scope=old_scope)
    old_target = require(mw.capture_current_run_target(old_scope))
    assert old_target.phase == "prepared"

    mw.expire_current_run_scope(old_scope)
    assert mw.capture_current_run_target(old_scope) is None
    mw.prepare_turn(reminder_scope=mw.create_current_run_scope())
    inputs.skills = SKILLS_REFRESHED

    assert mw.update_skill_catalog_for_current_run(old_target) is False
    assert mw.set_skill_catalog_for_current_run(old_target, "stale catalog") is False
    assert mw.queue_hook_reminders_for_current_run(old_target, ["stale hook"]) is False
    sent = await _call(stack, [user("go")])
    assert _texts(sent[0]) == ["go", _wrapped(SKILLS)]


async def test_pre_prepare_target_goes_stale_when_another_turn_prepares() -> None:
    stack = _stack()
    mw = stack.middleware
    stale_scope = mw.create_current_run_scope()
    stale = require(mw.capture_current_run_target(stale_scope))
    assert stale.phase == "pre_prepare"

    mw.prepare_turn()
    assert mw.queue_hook_reminders_for_current_run(stale, []) is False
    assert mw.queue_hook_reminders_for_current_run(stale, ["late hook"]) is False

    # Positive control: captured after that prepare, a target queues for its own run.
    scope = mw.create_current_run_scope()
    target = require(mw.capture_current_run_target(scope))
    assert mw.queue_hook_reminders_for_current_run(target, ["hook"]) is True
    mw.prepare_turn(reminder_scope=scope)
    sent = await _call(stack, [user("go")])
    assert _texts(sent[0]) == ["go", _wrapped("hook")]


# ---------------------------------------------------------------------------
# L6 — profile-switch consumption
# ---------------------------------------------------------------------------


async def test_profile_switch_is_consumed_only_by_a_returning_call_that_carries_it() -> None:
    stack = _stack()
    mw, switch = stack.middleware, stack.switch
    switch.set_profile_switch("Code", "QA")
    mw.prepare_turn()
    u1 = user("one")

    await _call(stack, [u1], fail=True)
    assert switch.snapshot_pending_switch() == {"from": "Code", "to": "QA"}
    assert switch.consumed_switch_to is None

    # The retry carries the notice through u1's record, not a new copy.
    mw.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)
    sent = await _call(stack, [u1])
    assert len([text for text in _texts(sent[0]) if text.startswith(_SWITCH_NOTICE_START)]) == 1
    assert switch.snapshot_pending_switch() is None
    assert switch.consumed_switch_to == "QA"

    mw.prepare_turn()
    assert switch.consumed_switch_to is None


async def test_profile_switch_changed_during_the_call_stays_pending() -> None:
    stack = _stack()
    mw, switch = stack.middleware, stack.switch
    switch.set_profile_switch("Code", "QA")
    mw.prepare_turn()

    await _call(stack, [user("one")], during=lambda: switch.update_profile_switch_to("Explore"))

    assert switch.snapshot_pending_switch() == {"from": "Code", "to": "Explore"}
    assert switch.consumed_switch_to == "QA"


# ---------------------------------------------------------------------------
# L7 — held catalogs
# ---------------------------------------------------------------------------


async def test_held_catalogs_clear_for_the_call_and_survive_only_a_poll() -> None:
    stack = _stack(ReminderProviders(skills=SKILLS))
    stack.middleware.prepare_turn()
    u1 = user("one")
    seen: list[object] = []

    def _see() -> None:
        seen.append(held_catalogs(stack))

    await _call(stack, [u1], answer="resp_1")
    assert held_catalogs(stack) == ("resp_1", {"skills": SKILLS})

    await _call(stack, [u1], options={"conversation_id": "resp_1"}, fail=True, during=_see)
    assert seen == [None]
    assert held_catalogs(stack) is None

    await _call(stack, [u1], answer="resp_2")
    held = require(held_catalogs(stack))
    await _call(
        stack,
        [u1],
        options={"conversation_id": "resp_2", "continuation_token": "poll_token"},
        answer="resp_3",
        during=_see,
    )
    assert seen[-1] is held
    assert held_catalogs(stack) is held


# ---------------------------------------------------------------------------
# L8, L9 — direct use without prepare_turn
# ---------------------------------------------------------------------------


async def test_lazy_call_leaves_the_pending_switch_and_hooks_for_the_next_prepare() -> None:
    stack = _stack()
    mw, switch = stack.middleware, stack.switch
    switch.set_profile_switch("Code", "QA")
    mw.queue_hook_reminders(["hook"])
    generation = reminder_generation(stack)
    u1 = user("one")

    sent = await _call(stack, [u1])
    assert _texts(sent[0]) == ["one"]
    assert switch.snapshot_pending_switch() == {"from": "Code", "to": "QA"}
    assert reminder_generation(stack) == generation

    mw.prepare_turn()
    assert reminder_generation(stack) == generation + 1
    sent = await _call(stack, [u1])
    texts = _texts(sent[0])
    assert _wrapped("hook") in texts
    assert any(text.startswith(_SWITCH_NOTICE_START) for text in texts)
    assert switch.snapshot_pending_switch() is None


async def test_last_words_before_the_first_prepare_open_an_empty_turn() -> None:
    inputs = ReminderProviders(todo=TODO_A, skills=SKILLS)
    stack = _stack(inputs, runtime=RUNTIME)
    stack.last_words.set_last_words("note")

    sent = await _call(stack, [user("go")])

    texts = _texts(sent[0])
    assert texts[0] == "go"
    assert len(texts) == 2
    assert texts[1].startswith(_LAST_WORDS_START)
    assert "note" in texts[1]
    assert TODO_A in texts[1]


async def test_drained_injection_reminders_before_the_first_prepare_open_an_empty_turn() -> None:
    stack = _stack(ReminderProviders(todo=TODO_A, skills=SKILLS), runtime=RUNTIME)
    stack.middleware.queue_drained_injection_reminders(["drained"])

    sent = await _call(stack, [user("go")])

    assert _texts(sent[0]) == ["go", _wrapped("drained")]


# ---------------------------------------------------------------------------
# L10 — provider read order
# ---------------------------------------------------------------------------


async def test_providers_are_read_in_order_for_fresh_preserving_and_lazy_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    pin_reminder_inputs(monkeypatch, log=log)
    inputs = ReminderProviders(todo=TODO_A, skills=SKILLS, mcp=MCP, file_change="src/a.py changed", log=log)
    quota = LoggingSpillQuota(log)
    quota.initialize(0, [], live_relative_paths=list(ARCHIVED[:2]))
    plant_catalog(tmp_path)

    def _build() -> ReminderStack:
        return _stack(
            inputs,
            runtime=RUNTIME,
            shell_tool_enabled=True,
            session_root=tmp_path,
            spill_quota=quota,
            file_read_available=True,
        )

    stack = _build()
    stack.middleware.prepare_turn()
    assert log == ["python_paths", "todo", "clock", "skills", "mcp", "pointer_count", "file_change"]

    log.clear()
    stack.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)
    assert log == ["file_change"]

    log.clear()
    await _call(_build(), [user("go")])
    assert log == ["python_paths", "todo", "clock", "skills", "mcp"]


# ---------------------------------------------------------------------------
# L11 — the restored Phase 4 stash
# ---------------------------------------------------------------------------


def _saved_phase4() -> dict[str, Any]:
    return {
        "last_words": "restored note",
        "last_words_manifest": [manifest_entry(2, 1, "read_file", "src/a.py").to_state()],
        "last_words_breaker": DropRoundBreakerState(attempts=1, side_call_tokens=900).to_state(),
    }


def test_restored_phase4_stash_is_read_before_prepare_and_consumed_by_a_preserving_one() -> None:
    inputs = ReminderProviders(todo=TODO_A)
    saved = _saved_phase4()
    entry_path = saved["last_words_manifest"][0]["relative_path"]
    stack = _stack(inputs)
    lw = stack.last_words
    restore_phase4(stack, saved, available_relative_paths={entry_path})
    inputs.todo = TODO_B

    assert lw.get_last_words() == "restored note"
    assert [entry["relative_path"] for entry in lw.get_last_words_manifest()] == [entry_path]
    assert lw.get_last_words_breaker_state() == saved["last_words_breaker"]

    stack.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)

    rendered = require(lw.render_last_words_reminder_text())
    assert "restored note" in rendered
    assert TODO_A in rendered
    assert TODO_B not in rendered
    assert "--- Dropped this turn" in rendered
    assert lw.get_last_words_breaker_state() == saved["last_words_breaker"]


def test_fresh_prepare_discards_the_restored_phase4_stash() -> None:
    stack = _stack(ReminderProviders(todo=TODO_A))
    lw = stack.last_words
    restore_phase4(stack, _saved_phase4())

    stack.middleware.prepare_turn()

    assert lw.get_last_words() is None
    assert lw.get_last_words_manifest() == []
    assert lw.get_last_words_breaker_state() is None
    assert lw.render_last_words_reminder_text() is None


def test_empty_turn_falls_back_to_the_restored_note_but_not_the_manifest_or_breaker() -> None:
    stack = _stack(ReminderProviders(todo=TODO_A))
    lw = stack.last_words
    saved = _saved_phase4()
    restore_phase4(stack, saved, available_relative_paths={saved["last_words_manifest"][0]["relative_path"]})

    assert lw.claim_context_pressure_notification() is True

    assert lw.get_last_words() == "restored note"
    assert lw.get_last_words_manifest() == []
    assert lw.get_last_words_breaker_state() is None
    rendered = require(lw.render_last_words_reminder_text())
    assert "restored note" in rendered
    assert TODO_A in rendered
    assert "--- Dropped this turn" not in rendered


@pytest.mark.parametrize("restored", [False, True], ids=["set", "restored"])
def test_a_failing_todo_provider_leaves_the_note_without_a_todo_section(restored: bool) -> None:
    def failing_todo() -> str | None:
        raise RuntimeError("todo tracker unavailable")

    stack = make_reminder_stack(max_context_tokens=MAX_CONTEXT_TOKENS, todo_state_provider=failing_todo)
    if restored:
        restore_phase4(stack, {"last_words": "the note"})
        stack.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)
    else:
        stack.middleware.prepare_turn()
        stack.last_words.set_last_words("the note")

    assert require(stack.last_words.render_last_words_reminder_text()).endswith("\n\nthe note\n</system-reminder>")


# ---------------------------------------------------------------------------
# L12 — the archive pointer's turn-start count
# ---------------------------------------------------------------------------


async def test_pointer_count_is_taken_only_by_a_fresh_prepare(tmp_path: Path) -> None:
    plant_catalog(tmp_path)
    quota = spill_quota(live=ARCHIVED[:2])
    stack = _stack(session_root=tmp_path, spill_quota=quota, file_read_available=True)
    pointer = stack.pointer

    stack.middleware.prepare_turn()
    assert pointer.record_count_state() == 2

    quota.initialize(0, [], live_relative_paths=list(ARCHIVED))
    stack.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)
    assert pointer.record_count_state() == 2
    sent = await _call(stack, [user("go")])
    assert any("archived 2 records" in text for text in _texts(sent[0]))

    stack.middleware.prepare_turn()
    assert pointer.record_count_state() == 5


def test_restored_pointer_count_serves_only_a_preserving_prepare_without_a_turn(tmp_path: Path) -> None:
    plant_catalog(tmp_path)
    quota = spill_quota(live=ARCHIVED)

    def _restored() -> ReminderStack:
        stack = _stack(session_root=tmp_path, spill_quota=quota, file_read_available=True)
        restore_phase4(stack, {CATALOG_POINTER_RECORD_COUNT_STATE_KEY: 2})
        return stack

    preserved = _restored()
    assert preserved.pointer.record_count_state() == 2
    preserved.middleware.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)
    assert preserved.pointer.record_count_state() == 2

    fresh = _restored()
    fresh.middleware.prepare_turn()
    assert fresh.pointer.record_count_state() == 5


def test_every_prepare_clears_the_restored_pointer_count(tmp_path: Path) -> None:
    plant_catalog(tmp_path)
    stack = _stack(session_root=tmp_path, spill_quota=spill_quota(live=ARCHIVED), file_read_available=True)
    mw = stack.middleware
    restore_phase4(stack, {CATALOG_POINTER_RECORD_COUNT_STATE_KEY: 7})
    assert stack.pointer.record_count_state() == 7

    scope = mw.create_current_run_scope()
    mw.prepare_turn(reminder_scope=scope)
    mw.expire_current_run_scope(scope)
    mw.prepare_turn(preserve_turn_reminders=True, preserve_last_words=True)

    assert stack.pointer.record_count_state() == 5


# ---------------------------------------------------------------------------
# L13 — Phase 4 wiring
# ---------------------------------------------------------------------------


async def test_phase4_with_the_reminder_unbound_skips_the_drop(caplog: pytest.LogCaptureFixture) -> None:
    messages = _build_single_turn(4)
    strategy = UnifiedContextStrategy(
        max_context_tokens=_estimate_tokens(messages) + 50,
        trigger_pct=0.90,
        target_pct=0.01,
    )
    generator = StubLastWordsGenerator("note")
    strategy.set_last_words_generator(generator)

    with caplog.at_level(logging.WARNING):
        await strategy(messages)

    assert (
        "Phase 4 SKIPPED drop: fresh-note collaborators unbound "
        "(reminder_middleware=None, last_words_state=None, last_words_generator=bound)" in caplog.messages
    )
    assert generator.calls == []
    assert not any(message.additional_properties.get(EXCLUDED_KEY, False) for message in messages)


async def test_phase4_with_the_generator_unbound_skips_the_drop(caplog: pytest.LogCaptureFixture) -> None:
    stack = _stack()
    stack.middleware.prepare_turn()
    messages = _build_single_turn(4)
    strategy = UnifiedContextStrategy(
        max_context_tokens=_estimate_tokens(messages) + 50,
        trigger_pct=0.90,
        target_pct=0.01,
    )
    strategy.bind_reminder(stack.middleware, stack.last_words)

    with caplog.at_level(logging.WARNING):
        await strategy(messages)

    assert (
        "Phase 4 SKIPPED drop: fresh-note collaborators unbound "
        "(reminder_middleware=bound, last_words_state=bound, last_words_generator=None)" in caplog.messages
    )
    assert stack.last_words.get_last_words() is None
    assert not any(message.additional_properties.get(EXCLUDED_KEY, False) for message in messages)


def test_bind_reminder_refuses_a_state_the_middleware_does_not_render() -> None:
    first, second = _stack(), _stack()
    strategy = UnifiedContextStrategy(max_context_tokens=MAX_CONTEXT_TOKENS)

    for middleware in (first.middleware, SystemReminderMiddleware()):
        with pytest.raises(ValueError, match="does not render"):
            strategy.bind_reminder(middleware, second.last_words)
    # A refused pair installs neither half.
    assert strategy._reminder_middleware is None
    assert strategy._last_words_state is None

    strategy.bind_reminder(second.middleware, second.last_words)
    assert strategy._reminder_middleware is second.middleware
    assert strategy._last_words_state is second.last_words


async def test_phase4_note_set_through_the_stack_renders_on_the_next_call() -> None:
    stack = _stack()
    stack.middleware.prepare_turn()
    messages = _build_single_turn(4)
    strategy = _forced_phase4(
        messages,
        last_words_generator=StubLastWordsGenerator("note from phase 4"),
        reminder_middleware=stack.middleware,
        last_words=stack.last_words,
    )

    assert await strategy(messages)
    assert any(message.additional_properties.get(EXCLUDED_KEY, False) for message in messages)

    sent = await _call(stack, [user("next")])
    texts = _texts(sent[0])
    assert texts[-1].startswith(_LAST_WORDS_START)
    assert "note from phase 4" in texts[-1]
