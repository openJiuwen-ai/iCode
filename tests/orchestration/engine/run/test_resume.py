# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``TurnResumePolicy.retry_request`` — continuation vs replay, and mid-turn stamps.

Covers the strategy choice ``resume()`` makes from conversation state (empty
input vs replaying the original prompt vs sending a mid-turn note), the
creation-time mid-turn flags and pop-time recovery registration, and the
carried-compression flush that precedes a retry.  The retry lifecycle around
``resume`` lives in ``test_retry_lifecycle.py``, its end-to-end engine
coverage in ``test_retry_integration.py``, and
``SessionHistoryManager.remove_continuation_message`` in
``tests/service/session/test_history_markers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Content, Message
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.context.providers.history import (
    PRE_OUTPUT_HISTORY_LEN_STATE_KEY,
    CompressibleHistoryProvider,
)
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from tests.orchestration.invoker._resume_policy import drive_fresh_policy, drive_retry_policy, make_resume_harness
from tests.support.components import make_turn_state


class TestResumeMode:
    """Test that resume() picks the right strategy based on conversation state."""

    async def test_resume_with_completed_work_uses_empty_input(self):
        """Completed work resumes from history without a synthetic user message."""
        executor = make_resume_harness(
            [
                Message("user", ["original prompt"]),
                Message("assistant", ["tool calls"]),
                Message("tool", ["results"]),
            ]
        )

        # Drive the real policy through its request boundary
        await drive_retry_policy(executor)

        executor.backend.run.assert_awaited_once()
        assert list(executor.backend.run.call_args[0][0].messages) == []

    async def test_legacy_nudge_is_ignored_as_anchor_and_left_persisted(self):
        """A legacy read-compatible nudge neither becomes input nor gets scrubbed."""
        legacy_nudge = Message("user", ["continue"])
        legacy_nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
        messages = [
            Message("user", ["original prompt"]),
            Message("assistant", ["completed work"]),
            legacy_nudge,
        ]
        executor = make_resume_harness(messages)

        await drive_retry_policy(executor)

        executor.backend.run.assert_awaited_once()
        assert list(executor.backend.run.call_args[0][0].messages) == []
        assert messages[-1] is legacy_nudge

    async def test_resume_without_completed_work_resends_original(self):
        """When no work after user message, resume pops and re-sends original text."""
        msgs = [Message("user", ["original prompt"])]
        executor = make_resume_harness(msgs)

        await drive_retry_policy(executor)

        executor.backend.run.assert_called_once()
        call_arg = executor.backend.run.call_args[0][0].messages
        assert len(call_arg) == 1
        assert call_arg[0].role == "user"
        assert call_arg[0].text == "original prompt"
        # User message should have been popped
        assert len(msgs) == 0

    async def test_continue_after_a_kept_answer_asks_for_it_again(self):
        """A shown answer at the end is dropped, so the request never ends with assistant text."""
        msgs = [Message("user", ["go"]), Message("assistant", ["Partial "])]
        executor = make_resume_harness(msgs)

        await drive_retry_policy(executor)

        sent = executor.backend.run.call_args[0][0].messages
        assert [(m.role, m.text) for m in sent] == [("user", "go")]
        assert msgs == []

    async def test_continue_after_tool_work_drops_only_the_kept_answer(self):
        call = Message("assistant", ["Reading", Content.from_function_call(call_id="c1", name="read", arguments={})])
        result = Message("tool", [Content.from_function_result(call_id="c1", result="ok")])
        msgs = [Message("user", ["go"]), call, result, Message("assistant", ["Partial"])]
        executor = make_resume_harness(msgs)

        await drive_retry_policy(executor)

        assert list(executor.backend.run.call_args[0][0].messages) == []
        assert msgs[1:] == [call, result]

    async def test_a_note_drops_the_kept_answer_and_goes_on_from_the_note(self):
        opener = Message("user", ["go"])
        msgs = [opener, Message("assistant", ["Partial"])]
        executor = make_resume_harness(msgs)

        await drive_retry_policy(executor, additional_text="go on")

        assert [m.text for m in executor.backend.run.call_args[0][0].messages] == ["go on"]
        assert msgs == [opener]

    @pytest.mark.parametrize("contents", [[], [""], ["", ""]], ids=["no-content", "empty-text", "empty-texts"])
    @pytest.mark.parametrize("persisted", [False, True], ids=["not-stored", "stored"])
    async def test_empty_text_replay_keeps_a_single_nonempty_fallback(self, contents, persisted):
        messages = [Message("user", contents)]
        executor = make_resume_harness(messages)
        registered = []

        def record_input(text, contents, created_at, kind="opener"):
            registered.append((text, Message("user", contents).text, kind))

        async def fail(request):
            replay = request.messages[0]
            assert replay.text == "continue"
            if persisted:
                messages.append(replay)
            executor.state.run_failed = True

        executor.inputs.recovery_input_recorder = record_input
        executor.backend.run.side_effect = fail
        await drive_retry_policy(executor)

        assert registered == [("continue", "continue", "opener")]
        assert [message.text for message in messages] == ["continue"]

    async def test_resume_with_additional_text_sends_note_not_continue(self):
        """When additional_text is provided, the user's note replaces 'continue'.

        The original user message is **not** popped; the note becomes a
        follow-up user turn within the same logical turn.
        """
        msgs = [
            Message("user", ["original prompt"]),
            Message("assistant", ["tool calls"]),
            Message("tool", ["results"]),
        ]
        executor = make_resume_harness(msgs)

        await drive_retry_policy(executor, additional_text="also check env vars")

        executor.backend.run.assert_called_once()
        call_arg = executor.backend.run.call_args[0][0].messages
        assert len(call_arg) == 1
        assert call_arg[0].role == "user"
        assert call_arg[0].text == "also check env vars"
        # Original messages are untouched — no pop, no mutation.
        assert len(msgs) == 3
        assert msgs[0].text == "original prompt"

    async def test_resume_with_additional_text_no_work_after_keeps_orphan(self):
        """Additional text + no-work-after: keep orphan, send note only.

        Without additional_text, the no-work-after branch pops the
        orphan and re-sends it.  With additional_text present we don't
        pop — the original prompt stays and the user's note is sent as
        the retry prompt.
        """
        msgs = [Message("user", ["original prompt"])]
        executor = make_resume_harness(msgs)

        await drive_retry_policy(executor, additional_text="with this extra context")

        executor.backend.run.assert_called_once()
        call_arg = executor.backend.run.call_args[0][0].messages
        assert call_arg[0].text == "with this extra context"
        # Orphan user message must be preserved.
        assert len(msgs) == 1
        assert msgs[0].text == "original prompt"


@pytest.mark.parametrize("saved_at", [None, "2026-01-02T03:04:05.000000+00:00"], ids=["legacy", "stamped"])
async def test_a_failed_bare_retry_restores_its_prompt_with_the_time_it_was_saved_with(saved_at: str | None) -> None:
    """The fallback re-creates the popped prompt as saved: one saved without a time gets no made-up one."""
    prompt = Message("user", ["original prompt"])
    if saved_at is not None:
        prompt.additional_properties[MESSAGE_CREATED_AT_KEY] = saved_at
    msgs = [prompt]
    executor = make_resume_harness(msgs)

    async def _fail_before_history_stores_it(_input):
        executor.state.run_failed = True

    executor.backend.run.side_effect = _fail_before_history_stores_it

    await drive_retry_policy(executor)

    (restored,) = msgs
    assert restored.text == "original prompt"
    assert restored.additional_properties.get(MESSAGE_CREATED_AT_KEY) == saved_at


class TestResumeMidTurnStamps:
    """Creation-time mid-turn flags and pop-time recovery registration (§2.5)."""

    async def test_empty_input_resume_keeps_pending_continuation_token(self):
        """The bare-continue branch resumes the announced background response."""

        executor = make_resume_harness([Message("user", ["prompt"]), Message("assistant", ["work"])])
        token = {"response_id": "bg-pending"}
        executor.inputs.pending_continuation_token = token

        await drive_retry_policy(executor)

        executor.backend.run.assert_awaited_once()
        assert list(executor.backend.run.call_args[0][0].messages) == []
        assert executor.inputs.pending_continuation_token == token

    async def test_note_resume_drops_pending_continuation_token(self):
        """A note needs a fresh create — a retrieve would silently ignore it."""

        noted = make_resume_harness([Message("user", ["prompt"]), Message("assistant", ["work"])])
        noted.inputs.pending_continuation_token = {"response_id": "bg-a"}
        await drive_retry_policy(noted, additional_text="also check env vars")
        assert noted.inputs.pending_continuation_token is None

    async def test_pending_token_routes_bare_resume_to_empty_input_without_landed_work(self):
        """A slow background response can announce its token before any output
        lands — bare retry must retrieve it, not replay the opener into a
        duplicate create."""

        executor = make_resume_harness([Message("user", ["prompt"])])
        token = {"response_id": "bg-in-flight"}
        executor.inputs.pending_continuation_token = token

        await drive_retry_policy(executor)

        executor.backend.run.assert_awaited_once()
        assert list(executor.backend.run.call_args[0][0].messages) == []
        assert executor.inputs.pending_continuation_token == token
        # The opener stays in history — it was not popped for replay.
        assert [m.text for m in executor.backend.session.state["chrys_history"]["messages"]] == ["prompt"]

    async def test_fresh_user_message_drops_pending_continuation_token(self):
        """A new prompt abandons the failed turn's in-flight response."""

        executor = make_resume_harness([])
        executor.inputs.pending_continuation_token = {"response_id": "bg-stale"}

        await drive_fresh_policy(executor, ["new question"])

        assert executor.inputs.pending_continuation_token is None
        executor.backend.run.assert_awaited_once()
        assert executor.backend.run.call_args[0][0].messages[0].text == "new question"

    @staticmethod
    def _flagged(text: str, key: str) -> Message:
        msg = Message("user", [text])
        msg.additional_properties[key] = True
        return msg

    async def test_bare_resume_after_work_uses_empty_input(self):

        executor = make_resume_harness(
            [Message("user", ["prompt"]), Message("assistant", ["work"]), Message("tool", ["results"])]
        )
        await drive_retry_policy(executor)

        executor.backend.run.assert_awaited_once()
        assert list(executor.backend.run.call_args[0][0].messages) == []

    async def test_no_user_message_resume_is_defensive_noop(self):

        executor = make_resume_harness([Message("assistant", ["work"])])
        await drive_retry_policy(executor)

        executor.backend.run.assert_not_awaited()
        assert executor.state.run_failed is True
        assert executor.state.last_error == "Cannot resume without a real user message in history."

    async def test_additional_text_is_flagged_injected(self):

        executor = make_resume_harness([Message("user", ["prompt"]), Message("assistant", ["work"])])
        await drive_retry_policy(executor, additional_text="also check env vars")

        sent = executor.backend.run.call_args[0][0].messages[0]
        assert sent.text == "also check env vars"
        assert sent.additional_properties[HistoryMarkerKind.INJECTED_KEY] is True
        assert HistoryMarkerKind.CONTINUATION_KEY not in sent.additional_properties

    async def test_opener_replay_unflagged_and_registered_as_opener_kind(self):

        msgs = [Message("user", ["original prompt"])]
        executor = make_resume_harness(msgs)
        registered: list[tuple] = []
        executor.inputs.recovery_input_recorder = lambda text, contents, created_at, kind="opener": registered.append(
            (text, contents, created_at, kind)
        )

        await drive_retry_policy(executor)

        sent = executor.backend.run.call_args[0][0].messages[0]
        assert sent.text == "original prompt"
        assert HistoryMarkerKind.INJECTED_KEY not in sent.additional_properties
        assert HistoryMarkerKind.CONTINUATION_KEY not in sent.additional_properties
        # The pop destroyed the only durable copy — registration happens at pop time.
        assert registered == [("original prompt", list(sent.contents), None, "opener")]

    async def test_popped_injection_replay_preserves_flag_and_registers_injected_kind(self):

        msgs = [self._flagged("crashed injection", HistoryMarkerKind.INJECTED_KEY)]
        executor = make_resume_harness(msgs)
        registered: list[tuple] = []
        executor.inputs.recovery_input_recorder = lambda text, contents, created_at, kind="opener": registered.append(
            (text, contents, created_at, kind)
        )

        await drive_retry_policy(executor)

        sent = executor.backend.run.call_args[0][0].messages[0]
        assert sent.text == "crashed injection"
        # Re-sending a popped injection unflagged would launder it into an opener.
        assert sent.additional_properties[HistoryMarkerKind.INJECTED_KEY] is True
        assert registered == [("crashed injection", list(sent.contents), None, "injected")]

    async def test_failed_opener_replay_reappend_not_suppressed_by_same_text_injection(self):
        """A same-text flagged injection in the region must not swallow the popped opener."""

        msgs = [Message("user", ["what time is it?"])]
        executor = make_resume_harness(msgs)

        async def _fake_execute(_input):
            # The failed replay run persisted only a same-text INJECTION copy.
            msgs.append(self._flagged("what time is it?", HistoryMarkerKind.INJECTED_KEY))
            executor.state.run_failed = True

        executor.backend.run.side_effect = _fake_execute

        await drive_retry_policy(executor)

        openers = [
            m for m in msgs if m.role == "user" and not m.additional_properties.get(HistoryMarkerKind.INJECTED_KEY)
        ]
        assert [m.text for m in openers] == ["what time is it?"]

    async def test_failed_injection_replay_reappends_flagged_and_dedups_against_flagged_copy(self):
        """Popped-injection replay follows the anchor's kind in both dedup directions."""

        # Direction 1: persisted flagged copy exists → no duplicate append.
        msgs = [self._flagged("my note", HistoryMarkerKind.INJECTED_KEY)]
        executor = make_resume_harness(msgs)

        async def _fail_with_copy(_input):
            msgs.append(self._flagged("my note", HistoryMarkerKind.INJECTED_KEY))
            executor.state.run_failed = True

        executor.backend.run.side_effect = _fail_with_copy
        await drive_retry_policy(executor)
        assert [m.text for m in msgs] == ["my note"]
        assert msgs[0].additional_properties[HistoryMarkerKind.INJECTED_KEY] is True

        # Direction 2: only a same-text unflagged opener exists → re-append FLAGGED.
        msgs2 = [self._flagged("my note", HistoryMarkerKind.INJECTED_KEY)]
        executor2 = make_resume_harness(msgs2)

        async def _fail_with_opener(_input):
            msgs2.append(Message("user", ["my note"]))
            executor2.state.run_failed = True

        executor2.backend.run.side_effect = _fail_with_opener
        await drive_retry_policy(executor2)
        flagged = [m for m in msgs2 if m.additional_properties.get(HistoryMarkerKind.INJECTED_KEY)]
        assert [m.text for m in flagged] == ["my note"]
        # The unflagged opener copy is untouched.
        assert sum(1 for m in msgs2 if m.role == "user") == 2

    # -- replay-branch blind spot (§2.5): a checkpoint built AFTER the pop --

    @staticmethod
    def _make_turn_state():
        """Real TurnRuntimeState wired the way the runner leaves it: cleared."""

        turn_state = make_turn_state()
        turn_state.clear_current_input()
        return turn_state

    @staticmethod
    def _crash_checkpoint(msgs: list[Message], turn_state) -> dict | None:
        """Build a recovery checkpoint from post-pop live state.

        Simulates a hard crash mid-replay-run: nothing was captured and
        nothing persisted — the registered current input is the popped
        anchor's only durable copy.  Mirrors the engine's call
        (``engine.py`` ``_save_recovery_checkpoint``): text/contents/
        created_at/kind all come from the turn state.
        """
        from chrys.kernel import LoopRecorder
        from chrys.service.session.checkpoint import build_recovery_state
        from chrys.service.session.runtime_metadata import SessionRuntimeMetadata

        current = turn_state.current_input
        return build_recovery_state(
            {"messages": msgs, "compressed_msgs": [], "turn_counter": 0},
            LoopRecorder(),
            mutation_tracker=None,
            runtime_meta=SessionRuntimeMetadata(),
            user_text=current.text,
            user_contents=current.contents,
            user_created_at=current.created_at,
            user_kind=current.kind,
        )

    async def test_crash_after_opener_pop_recovers_unflagged_opener(self):
        """The pop destroys the opener's only durable copy — recovery re-creates it."""
        from chrys.foundation.models.turns import opens_turn

        msgs = [Message("user", ["original prompt"])]
        executor = make_resume_harness(msgs)
        turn_state = self._make_turn_state()
        executor.inputs.recovery_input_recorder = turn_state.set_current_input

        # backend.run is a no-op: the run hard-crashed before persisting anything.
        await drive_retry_policy(executor)

        recovered = self._crash_checkpoint(msgs, turn_state)
        assert recovered is not None
        users = [m for m in recovered["messages"] if m.role == "user"]
        assert [m.text for m in users] == ["original prompt"]
        assert opens_turn(users[0])
        assert HistoryMarkerKind.INJECTED_KEY not in users[0].additional_properties

    @pytest.mark.parametrize("text", ["describe image", ""], ids=["with-text", "image-only"])
    @pytest.mark.parametrize("kind", ["opener", "injected"])
    @pytest.mark.parametrize("outcome", ["crash", "failed", "interrupted"])
    async def test_image_replay_preserves_request_failure_and_recovery_content(self, text, kind, outcome):
        image = Content.from_data(b"accepted image bytes", "image/png")
        original = Message("user", [*([text] if text else []), image])
        if kind == "injected":
            original.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
        messages = [original]
        executor = make_resume_harness(messages)
        turn_state = self._make_turn_state()
        executor.inputs.recovery_input_recorder = turn_state.set_current_input
        recovered = None

        async def execute(request):
            nonlocal recovered
            replay = request.messages[0]
            assert replay is not original
            assert replay.contents is not original.contents
            assert [c.to_dict() for c in replay.contents] == [c.to_dict() for c in original.contents]
            recovered = self._crash_checkpoint(messages, turn_state)
            executor.state.run_failed = outcome == "failed"
            executor.state.was_interrupted = outcome == "interrupted"

        executor.backend.run.side_effect = execute
        await drive_retry_policy(executor)
        assert recovered is not None
        users = [m for m in recovered["messages"] if m.role == "user"]
        assert len(users) == 1
        assert [c.to_dict() for c in users[0].contents] == [c.to_dict() for c in original.contents]
        assert bool(users[0].additional_properties.get(HistoryMarkerKind.INJECTED_KEY)) == (kind == "injected")
        if outcome != "crash":
            assert len(messages) == 1
            assert [c.to_dict() for c in messages[0].contents] == [c.to_dict() for c in original.contents]
            assert bool(messages[0].additional_properties.get(HistoryMarkerKind.INJECTED_KEY)) == (kind == "injected")

    async def test_crash_after_injection_pop_recovers_flagged_injection(self):
        """A popped injection comes back ``_injected`` — never laundered into an opener."""
        from chrys.foundation.models.turns import is_mid_turn_user_message

        msgs = [self._flagged("crashed injection", HistoryMarkerKind.INJECTED_KEY)]
        executor = make_resume_harness(msgs)
        turn_state = self._make_turn_state()
        executor.inputs.recovery_input_recorder = turn_state.set_current_input

        await drive_retry_policy(executor)

        recovered = self._crash_checkpoint(msgs, turn_state)
        assert recovered is not None
        users = [m for m in recovered["messages"] if m.role == "user"]
        assert [m.text for m in users] == ["crashed injection"]
        assert users[0].additional_properties[HistoryMarkerKind.INJECTED_KEY] is True
        assert is_mid_turn_user_message(users[0])

        # When the run instead persists the input before the checkpoint (a
        # normal finalization), kind-aware dedup yields no duplicate.
        msgs2 = [self._flagged("crashed injection", HistoryMarkerKind.INJECTED_KEY)]
        executor2 = make_resume_harness(msgs2)
        turn_state2 = self._make_turn_state()
        executor2.inputs.recovery_input_recorder = turn_state2.set_current_input

        async def _persist_input(_input):
            msgs2.append(self._flagged("crashed injection", HistoryMarkerKind.INJECTED_KEY))

        executor2.backend.run.side_effect = _persist_input
        await drive_retry_policy(executor2)

        recovered2 = self._crash_checkpoint(msgs2, turn_state2)
        assert recovered2 is not None
        users2 = [m for m in recovered2["messages"] if m.role == "user"]
        assert [m.text for m in users2] == ["crashed injection"]

    async def test_crash_during_empty_input_resume_synthesizes_no_user_message(self):
        """Empty-input resume keeps recovery input clear."""

        # Completed work after the opener resumes with empty input.
        msgs = [Message("user", ["prompt"]), Message("assistant", ["work"]), Message("tool", ["results"])]
        executor = make_resume_harness(msgs)
        turn_state = self._make_turn_state()
        executor.inputs.recovery_input_recorder = turn_state.set_current_input

        await drive_retry_policy(executor)

        assert turn_state.current_input.text == ""
        recovered = self._crash_checkpoint(msgs, turn_state)
        assert recovered is not None
        assert [m.text for m in recovered["messages"] if m.role == "user"] == ["prompt"]

        # The admission guard makes this unreachable; TurnBindings remains defensive.
        msgs2 = [Message("assistant", ["work"])]
        executor2 = make_resume_harness(msgs2)
        turn_state2 = self._make_turn_state()
        executor2.inputs.recovery_input_recorder = turn_state2.set_current_input

        await drive_retry_policy(executor2)

        executor2.backend.run.assert_not_awaited()
        assert executor2.state.run_failed is True
        assert turn_state2.current_input.text == ""
        recovered2 = self._crash_checkpoint(msgs2, turn_state2)
        assert recovered2 is not None
        assert [m for m in recovered2["messages"] if m.role == "user"] == []


async def test_carried_compression_records_post_fold_metadata_floor() -> None:
    """A pre-retry fold replaces the stale pre_run history boundary."""
    state: dict = {
        "messages": [Message("user", ["first"]), Message("assistant", ["done"])],
        "compressed_msgs": [],
        "turn_counter": 0,
    }
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    pre_fold_len = len(state["messages"])
    strategy = UnifiedContextStrategy(compaction_enabled=False)
    strategy.bind_state(state)
    strategy.queue_compression(marker_id, "durable summary")
    executor = cast(
        "TurnBindings", SimpleNamespace(_compaction_strategy=strategy, backend=SimpleNamespace(history_state=state))
    )

    await TurnBindings._flush_carried_compressions(executor)

    assert len(state["messages"]) < pre_fold_len
    assert state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == len(state["messages"])


class TestResumeMultipleNotes:
    """Calling resume(additional_text=...) repeatedly must not corrupt state.

    Each call should append a fresh user-note message and never re-send
    a stale note from a prior retry.  Exercises the unit surface that
    the multi-interrupt integration scenario exercises end-to-end.
    """

    async def test_two_sequential_retries_with_notes(self):
        msgs = [
            Message("user", ["original"]),
            Message("assistant", ["tool calls"]),
            Message("tool", ["results"]),
        ]
        executor = make_resume_harness(msgs)
        sent_inputs: list[list[Message]] = []

        async def _fake_execute(request):
            input_msgs = list(request.messages)
            sent_inputs.append(input_msgs)
            # Simulate backend.run persisting the user input and
            # producing a response before the next retry starts.
            msgs.extend(input_msgs)
            msgs.append(Message("assistant", ["response"]))

        executor.backend.run.side_effect = _fake_execute

        await drive_retry_policy(executor, additional_text="note one")
        await drive_retry_policy(executor, additional_text="note two")

        # Each call must have sent its own note — not the other one.
        assert [i[0].text for i in sent_inputs] == ["note one", "note two"]
        # No "continue" placeholder was ever sent.
        assert all(i[0].text != "continue" for i in sent_inputs)
        # Both notes live in state.
        texts = [m.text for m in msgs if m.role == "user"]
        assert texts == ["original", "note one", "note two"]
