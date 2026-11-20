# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Copy-freshness pin battery: one pin per production copy class.

Weak-identity registries make copy identity load-bearing: a FRESH layer of a
copy must never inherit its source's registration (a copy is never a member),
while a SHARED layer must keep it (the shared content object stays the echo
memo's currency). Each pin below names its production copy class and asserts
that class's exact fresh-vs-shared split, so a future change to any copy site
that silently flips a layer breaks a named test instead of a registry.

Two classes are pinned elsewhere and deliberately not duplicated:

- The owned middleware-termination result copy (loop tool invocation) is
  pinned by the ``result is not prebuilt`` battery in
  ``tests/kernel/test_loop_invocation_order.py``.
- The retry history snapshot's message-LIST shallow copy is composed by the
  executor around ``StreamRetryLoop``; the foundation-level property
  snapshot/restore trio it relies on is pinned here.
"""

from __future__ import annotations

import copy
import datetime as dt
import gc
import types
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.retry import restore_message_properties, snapshot_message_properties
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY
from chrys.kernel import AgentSession, LoopRecorder
from chrys.kernel._loop_recorder import _message_snapshot
from chrys.kernel._types import (
    _coalesce_code_interpreter_content,
    _coalesce_text_content,
    _merge_content_item_lists,
)
from chrys.kernel.client import _wire_message_view
from chrys.kernel.compaction import EXCLUDED_KEY, CompactionCallContext, apply_compaction, set_excluded
from chrys.kernel.identity import ContentList, WeakIdentityRegistry
from chrys.kernel.loop import _strip_echoed_update
from chrys.kernel.types import ChatResponseUpdate, Content, Message
from chrys.orchestration.engine.loader import _preserved_history_state
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.engine.session_lifecycle import _reset_restore_history_state
from chrys.orchestration.invoker.attempts import HistoryRollback
from chrys.service.agent_middleware.injection import ConsumedInjection, InjectionAnchor
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.context.compaction.last_words_state import LastWordsState
from chrys.service.context.compaction.scoped import clone_for_slice
from chrys.service.context.compaction.strategy import UnifiedContextStrategy
from chrys.service.context.providers.history import _TURN_ID_KEY, _compress_state
from chrys.service.session import checkpoint as session_checkpoint
from chrys.service.session.checkpoint import _DetachedLoopRecorder, build_recovery_state
from chrys.service.session.history import SessionHistoryManager
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from tests.support.ci import CI_LINUX_ONLY
from tests.support.reminder_stack import reminder_pair

# Platform-independent copy-semantics pins: the Linux CI job covers them.
pytestmark = CI_LINUX_ONLY


class _RecorderStub:
    """Attribute-level stand-in for ``LoopRecorder`` (the checkpoint shaper
    only reads ``initial_count`` and ``loop_messages``)."""

    def __init__(self, loop_messages: list[Message] | None) -> None:
        self.initial_count = 0
        self.loop_messages = loop_messages


# ---------------------------------------------------------------------------
# Deepcopy family: every layer fresh, SDK payloads shared by reference
# ---------------------------------------------------------------------------


class TestDeepcopyFamily:
    def test_message_deepcopy_is_identity_fresh_and_shares_raw_representation(self) -> None:
        """Copy class: whole-Message deepcopy (``SerializationMixin.__deepcopy__``).

        Fresh wrapper, fresh ``ContentList``, fresh content objects — none of
        them inherit the source's registration — while ``raw_representation``
        stays shared by reference (SDK payloads are unsafe to deep-copy).
        """
        sdk_payload = object()
        content = Content.from_text("hello", raw_representation=sdk_payload)
        message = Message("assistant", [content])
        registry = WeakIdentityRegistry()
        registry.register(message.contents)
        registry.register(content)

        clone = copy.deepcopy(message)

        assert clone is not message
        assert clone.contents is not message.contents
        assert type(clone.contents) is ContentList
        assert clone.contents[0] is not content
        assert clone.contents[0].raw_representation is sdk_payload
        assert clone.contents not in registry
        assert clone.contents[0] not in registry
        assert message.contents in registry

    def test_content_deepcopy_is_identity_fresh_and_shares_raw_representation(self) -> None:
        """Copy class: single-Content deepcopy (``Content.__deepcopy__``)."""
        sdk_payload = object()
        content = Content.from_text("hello", raw_representation=sdk_payload)
        registry = WeakIdentityRegistry()
        registry.register(content)

        clone = copy.deepcopy(content)

        assert clone is not content
        assert clone.raw_representation is sdk_payload
        assert clone not in registry
        assert content in registry

    def test_detached_loop_recorder_deep_copies_loop_messages(self) -> None:
        """Copy class: checkpoint recorder detachment (``_DetachedLoopRecorder``
        deep-copies ``loop_messages`` so checkpoint shaping cannot mutate live
        framework objects)."""
        content = Content.from_text("tool step")
        message = Message("assistant", [content])

        detached = _DetachedLoopRecorder(_RecorderStub([message]))

        assert detached.loop_messages is not None
        (copied,) = detached.loop_messages
        assert copied is not message
        assert copied.contents is not message.contents
        assert copied.contents[0] is not content

    def test_build_recovery_state_returns_identity_fresh_messages(self) -> None:
        """Copy class: crash-recovery state (``build_recovery_state`` deep-copies
        the live state once and shapes that copy; live objects never leak into
        the recovery payload). ``TestRecoverySnapshotIsolation`` walks every
        merged input."""
        user_content = Content.from_text("question")
        user_message = Message("user", [user_content])
        live_state = {"messages": [user_message]}

        recovered = build_recovery_state(
            live_state,
            _RecorderStub(None),
            mutation_tracker=None,
            runtime_meta=SessionRuntimeMetadata(),
            user_text=None,
            user_contents=None,
            user_created_at=None,
        )

        assert recovered is not None
        recovered_user = next(m for m in recovered["messages"] if m.text == "question")
        assert recovered_user is not user_message
        assert recovered_user.contents is not user_message.contents
        assert recovered_user.contents[0] is not user_content
        assert live_state["messages"] == [user_message]
        assert live_state["messages"][0] is user_message

    def test_agent_switch_preserved_history_state_is_identity_fresh(self) -> None:
        """Copy class: agent-switch preserved history (engine construction
        deep-copies the predecessor's state dict via ``_preserved_history_state``
        and installs the copy as the successor's live history; falsy/empty
        history preserves the no-carryover contract by returning ``None``)."""
        content = Content.from_text("carried")
        message = Message("user", [content])
        raw = {"messages": [message]}
        registry = WeakIdentityRegistry()
        registry.register(message.contents)
        registry.register(content)

        preserved = _preserved_history_state(raw)

        assert preserved is not None
        copied = preserved["messages"][0]
        assert copied is not message
        assert copied.contents not in registry
        assert copied.contents[0] not in registry
        assert raw["messages"][0] is message

        assert _preserved_history_state(None) is None
        assert _preserved_history_state({}) is None

    def test_failed_reset_restore_state_is_identity_fresh(self) -> None:
        """Copy class: failed-reset restore snapshot (session reset deep-copies
        the live history via ``_reset_restore_history_state`` before deleting
        session files, so a failed reset restarts from identity-fresh state
        instead of aliasing the live objects it is about to shut down)."""
        content = Content.from_text("pre-reset")
        message = Message("user", [content])
        state = {"messages": [message]}

        restored = _reset_restore_history_state(state)

        assert restored is not state
        assert restored["messages"][0] is not message
        assert restored["messages"][0].contents is not message.contents
        assert restored["messages"][0].contents[0] is not content
        assert state["messages"][0] is message
        assert _reset_restore_history_state({}) == {}

    def test_session_history_get_deep_copy_is_identity_fresh(self) -> None:
        """Copy class: session-history export (``SessionHistoryManager.get_deep_copy``)."""
        content = Content.from_text("hi")
        message = Message("user", [content])
        state = {"messages": [message]}
        manager = SessionHistoryManager()
        manager.bind(state)

        copied = manager.get_deep_copy()

        assert copied is not None
        assert copied is not state
        assert copied["messages"][0] is not message
        assert copied["messages"][0].contents[0] is not content
        assert state["messages"][0] is message


# ---------------------------------------------------------------------------
# Crash-recovery snapshot: no mutable object shared with any live input
# ---------------------------------------------------------------------------

# Objects a deepcopy may hand back unchanged; sharing them is harmless.
_IMMUTABLE_LEAVES = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    Enum,
    dt.date,
    dt.time,
    dt.timedelta,
    type,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.ModuleType,
    weakref.ref,
)
# SDK payloads every Message/Content deepcopy shares by reference (pinned by
# ``TestDeepcopyFamily``); the walk does not descend into them.
_SHARED_BY_REFERENCE_FIELDS = frozenset({"raw_representation"})


def _mutable_objects(*roots: object) -> dict[int, object]:
    """Every mutable object reachable from *roots*, keyed by id.

    Tuples and frozensets are walked but not recorded: a deepcopy returns the
    same tuple when every member is immutable, so only their members count.
    """
    found: dict[int, object] = {}
    walked: set[int] = set()
    pending: list[object] = list(roots)
    while pending:
        node = pending.pop()
        if isinstance(node, _IMMUTABLE_LEAVES) or id(node) in walked:
            continue
        walked.add(id(node))
        if isinstance(node, dict):
            found[id(node)] = node
            pending.extend(node.keys())
            pending.extend(node.values())
        elif isinstance(node, list | set | bytearray):
            found[id(node)] = node
            pending.extend(node)
        elif isinstance(node, tuple | frozenset):
            pending.extend(node)
        else:
            found[id(node)] = node
            try:
                attributes = vars(node)
            except TypeError:
                # Slotted objects: walk the values they hold, never their class.
                pending.extend(referent for referent in gc.get_referents(node) if not isinstance(referent, type))
            else:
                found[id(attributes)] = attributes
                pending.extend(value for name, value in attributes.items() if name not in _SHARED_BY_REFERENCE_FIELDS)
    return found


def _shared_mutables(result: object, live: dict[int, object]) -> list[object]:
    return [node for key, node in _mutable_objects(result).items() if key in live]


class _RetainingMutationTracker:
    """Hands out the ledger payload it keeps, as a caching producer could."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] = {
            "turns": [{"turn_id": 7, "mutations": [{"path": "a.py", "operation": "edit"}]}],
            "snapshots": {"a.py@7": {"path": "a.py", "existed": True}},
        }

    def serialize(self) -> dict[str, Any]:
        return self.payload


@dataclass
class _RetainingRuntimeMetadata(SessionRuntimeMetadata):
    """Runtime metadata that keeps the state dict it last handed out."""

    handed_out: dict[str, Any] = field(default_factory=dict)

    def to_state_dict(self) -> dict[str, Any]:
        self.handed_out = super().to_state_dict()
        return self.handed_out


@dataclass
class _RecoveryInputs:
    """One populated value for every input ``build_recovery_state`` merges."""

    live_state: dict[str, Any]
    recorder: LoopRecorder
    loop_result_message: Message
    mutation_tracker: _RetainingMutationTracker
    runtime_meta: _RetainingRuntimeMetadata
    user_contents: list[Content]
    user_reminder_source: dict[str, Any]
    consumed_injections: list[ConsumedInjection]
    last_words_manifest: list[dict[str, Any]]
    last_words_breaker: dict[str, Any]
    todos: list[dict[str, str]]

    @classmethod
    def create(cls) -> _RecoveryInputs:
        earlier_answer = Message("assistant", [Content.from_text("earlier answer")])
        earlier_answer.additional_properties["nested"] = {"key": ["value"]}
        live_state: dict[str, Any] = {
            "messages": [Message("user", [Content.from_text("earlier question")]), earlier_answer],
            "compressed_msgs": [],
            "turn_counter": 1,
            "chrys_workspace_baseline": {"roots": {"/work": {"files": ["a.py"]}}},
            "chrys_todos": [{"content": "stale", "status": "pending", "active_form": "Staling"}],
        }
        call = Content.from_function_call("call-1", "write_file", arguments={"path": "a.py"})
        call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
        assistant = Message("assistant", [call])
        recorder = LoopRecorder()
        commit = recorder.stage_exchange([assistant], [call], result_carrier_item_id="a" * 32)[0]
        result = Content.from_function_result("call-1", result="written")
        commit.commit_final(result)
        loop_result_message = Message("tool", [result])
        recorder.seal_exchange(loop_result_message)
        return cls(
            live_state=live_state,
            recorder=recorder,
            loop_result_message=loop_result_message,
            mutation_tracker=_RetainingMutationTracker(),
            runtime_meta=_RetainingRuntimeMetadata(
                total_session_tokens=42,
                last_usage_details={"input_token_count": 10, "output_token_count": 5},
                context_calibration={"v": 2, "calibration_ratio": 1.0},
            ),
            user_contents=[Content.from_text("do work")],
            user_reminder_source={
                HistoryMarkerKind.SYSTEM_REMINDERS_KEY: [{"kind": "turn", "text": "runtime"}],
            },
            consumed_injections=[
                ConsumedInjection(
                    text="mid-run note",
                    anchor=InjectionAnchor.from_message(assistant),
                    created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    consumption_id="inj-1",
                    wire_properties={
                        HistoryMarkerKind.SYSTEM_REMINDERS_KEY: [{"kind": "event", "text": "hook note"}],
                    },
                )
            ],
            last_words_manifest=[{"tool": "write_file", "paths": ["a.py"]}],
            last_words_breaker={"failures": [{"reason": "timeout"}]},
            todos=[{"content": "ship it", "status": "in_progress", "active_form": "Shipping it"}],
        )

    def live_roots(self) -> tuple[object, ...]:
        return (
            self.live_state,
            self.recorder,
            self.mutation_tracker,
            self.runtime_meta,
            self.user_contents,
            self.user_reminder_source,
            self.consumed_injections,
            self.last_words_manifest,
            self.last_words_breaker,
            self.todos,
        )

    def build(self) -> dict[str, Any]:
        recovered = build_recovery_state(
            self.live_state,
            self.recorder,
            mutation_tracker=self.mutation_tracker,
            runtime_meta=self.runtime_meta,
            user_text="do work",
            user_contents=self.user_contents,
            user_created_at="2026-01-01T00:00:00+00:00",
            user_reminder_source=self.user_reminder_source,
            consumed_injections=self.consumed_injections,
            last_words="note",
            last_words_manifest=self.last_words_manifest,
            last_words_breaker=self.last_words_breaker,
            catalog_pointer_record_count=3,
            todos=self.todos,
        )
        assert recovered is not None
        return recovered


class TestRecoverySnapshotIsolation:
    """Copy class: crash-recovery snapshot isolation. The snapshot goes to a
    writer thread while the turn keeps mutating live state, so no mutable
    object in it may be reachable from any input: the live history, the
    recorder, or a value merged over the history copy."""

    def test_recovery_state_shares_no_mutable_object_with_any_input(self) -> None:
        inputs = _RecoveryInputs.create()

        recovered = inputs.build()

        # Walked after the build: producers hand out their objects during it.
        live = _mutable_objects(*inputs.live_roots())
        assert _shared_mutables(recovered, live) == []
        # Every input reached the snapshot, so the walk above covered it.
        assert recovered["chrys_mutations"] == inputs.mutation_tracker.payload
        assert recovered["last_usage"] == {"input_token_count": 10, "output_token_count": 5}
        assert recovered["context_calibration"] == {"v": 2, "calibration_ratio": 1.0}
        assert recovered["last_words_manifest"] == inputs.last_words_manifest
        assert recovered["last_words_breaker"] == inputs.last_words_breaker
        assert recovered["chrys_todos"] == inputs.todos
        assert recovered["chrys_workspace_baseline"] == inputs.live_state["chrys_workspace_baseline"]
        texts = [message.text for message in recovered["messages"]]
        assert "do work" in texts
        assert "mid-run note" in texts
        records = {
            message.text: message.additional_properties.get(HistoryMarkerKind.SYSTEM_REMINDERS_KEY)
            for message in recovered["messages"]
        }
        assert records["do work"] == [{"kind": "turn", "text": "runtime"}]
        assert records["mid-run note"] == [{"kind": "event", "text": "hook note"}]
        assert any(
            content.type == "function_result" and content.result == "written"
            for message in recovered["messages"]
            for content in message.contents
        )

    def test_history_is_deep_copied_once_per_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole history is copied once; shaping works on that copy and
        hands it out without a second whole-state copy."""
        inputs = _RecoveryInputs.create()
        history_copies = 0

        def counting_deepcopy(value: object, memo: dict[int, Any] | None = None) -> object:
            nonlocal history_copies
            if isinstance(value, dict) and "messages" in value:
                history_copies += 1
            return copy.deepcopy(value, memo)

        shadow_copy = types.ModuleType("copy")
        shadow_copy.deepcopy = counting_deepcopy
        monkeypatch.setattr(session_checkpoint, "copy", shadow_copy)

        inputs.build()

        assert history_copies == 1

    @pytest.mark.parametrize(
        "uncopied",
        [
            "live_state",
            "loop_messages",
            "mutations",
            "runtime_meta",
            "last_words_manifest",
            "last_words_breaker",
            "todos",
            "user_contents",
        ],
    )
    def test_walk_reports_any_input_left_uncopied(self, monkeypatch: pytest.MonkeyPatch, uncopied: str) -> None:
        """Red path: skipping the copy of any one input shares live objects."""
        inputs = _RecoveryInputs.create()
        handed_out: dict[str, Callable[[object], bool]] = {
            "live_state": lambda value: value is inputs.live_state,
            "loop_messages": lambda value: (
                isinstance(value, list) and any(item is inputs.loop_result_message for item in value)
            ),
            "mutations": lambda value: value is inputs.mutation_tracker.payload,
            "runtime_meta": lambda value: value is inputs.runtime_meta.handed_out,
            "last_words_manifest": lambda value: value is inputs.last_words_manifest,
            "last_words_breaker": lambda value: value is inputs.last_words_breaker,
            "todos": lambda value: value is inputs.todos,
            "user_contents": lambda value: value is inputs.user_contents,
        }
        skip_copy = handed_out[uncopied]
        skipped: list[object] = []

        def deepcopy_except(value: object, memo: dict[int, Any] | None = None) -> object:
            if skip_copy(value):
                skipped.append(value)
                return value
            return copy.deepcopy(value, memo)

        shadow_copy = types.ModuleType("copy")
        shadow_copy.deepcopy = deepcopy_except
        monkeypatch.setattr(session_checkpoint, "copy", shadow_copy)

        recovered = inputs.build()

        assert len(skipped) == 1
        assert _shared_mutables(recovered, _mutable_objects(*inputs.live_roots())) != []


# ---------------------------------------------------------------------------
# Retry rollback: shared message objects, exact-and-fresh property restore
# ---------------------------------------------------------------------------


class TestRetryPropertyRollback:
    def test_property_restore_is_exact_and_hands_out_fresh_dicts_each_time(self) -> None:
        """Copy class: retry rollback property snapshot/restore. The snapshot
        copies one nested level at capture; restore is copy-on-restore, so the
        reusable snapshot's dicts are never handed to live messages."""
        message = Message("assistant", [Content.from_text("step")])
        message.additional_properties["_group"] = {"id": "g1"}

        snapshot = snapshot_message_properties([message])
        message.additional_properties["_group"]["id"] = "mutated"
        message.additional_properties["extra"] = True

        restore_message_properties([message], snapshot)
        assert message.additional_properties == {"_group": {"id": "g1"}}
        assert message.additional_properties is not snapshot[0]
        assert message.additional_properties["_group"] is not snapshot[0]["_group"]

        first_restored = message.additional_properties
        restore_message_properties([message], snapshot)
        assert message.additional_properties is not first_restored

    def test_executor_rollback_installs_fresh_list_and_shares_messages(self) -> None:
        """Copy class: executor retry history rollback. ``HistoryRollback.snapshot``
        takes a fresh shallow LIST copy that SHARES message objects (weak
        identities must survive rollback), and ``HistoryRollback.restore`` installs a
        fresh list copy on every rollback so later attempts can never mutate
        the reusable snapshot's own list."""
        executor = object.__new__(TurnBindings)
        session = AgentSession(session_id="local", service_session_id="svc-1")
        content = Content.from_text("before retry")
        message = Message("user", [content])
        live_messages = [message]
        session.state["chrys_history"] = {"messages": live_messages, "compressed_msgs": []}
        executor._session = session
        executor._loop_recorder = None
        executor.tool_events = SimpleNamespace(
            snapshot_retry_state=lambda: "tool-events-snapshot",
            restore_retry_state=lambda _snapshot: None,
        )
        executor.approval = SimpleNamespace(
            snapshot_retry_state=lambda: "approval-snapshot",
            restore_retry_state=lambda _snapshot: None,
        )
        executor._compaction_strategy = None

        rollback = HistoryRollback(
            session,
            snapshot_caller=executor._snapshot_retry_state,
            restore_caller=executor._restore_retry_state,
            history_state=lambda: session.state.get("chrys_history", {}),
        )
        snapshot = rollback.snapshot()

        assert snapshot.messages is not live_messages
        assert snapshot.messages[0] is message

        session.state["chrys_history"]["messages"].append(Message("assistant", ["partial attempt"]))
        rollback.restore(snapshot)

        first_restored = session.state["chrys_history"]["messages"]
        assert first_restored is not snapshot.messages
        assert len(first_restored) == 1
        assert first_restored[0] is message
        assert first_restored[0].contents[0] is content

        rollback.restore(snapshot)
        second_restored = session.state["chrys_history"]["messages"]
        assert second_restored is not first_restored
        assert second_restored is not snapshot.messages


# ---------------------------------------------------------------------------
# Loop-owned wrappers: fresh wrapper/list, shared content objects
# ---------------------------------------------------------------------------


class TestLoopOwnedWrapperCopies:
    def test_wire_and_provider_views_share_contents_and_props_dict(self) -> None:
        """Copy class: outgoing wire/provider views. ONE shared builder
        (``_wire_message_view``) serves both the tool loop's wire path and the
        provider-request path — the loop module re-exports the client's
        function, so the two paths cannot drift apart. Fresh wrapper + fresh
        contents list so client-side in-place mutation cannot rewrite history;
        content objects stay shared (echo-memo currency) and
        ``additional_properties`` stays THE wrapper's dict (the sanctioned
        metadata write-through channel)."""
        from chrys.kernel import loop as kernel_loop

        assert kernel_loop._wire_message_view is _wire_message_view

        content = Content.from_text("history")
        message = Message("user", [content])
        message.additional_properties["meta"] = "value"

        view = _wire_message_view(message)
        assert view is not message
        assert view.contents is not message.contents
        assert view.contents[0] is content
        assert view.additional_properties is message.additional_properties

    def test_landing_snapshot_copies_containers_and_shares_contents(self) -> None:
        """Copy class: loop landing snapshot (``_message_snapshot``). Fresh
        wrapper + fresh containers (contents list, props dict), shared content
        objects, and assembly-local echo-strip provenance reset."""
        content = Content.from_text("landed")
        message = Message("assistant", [content])
        message.additional_properties["meta"] = "value"
        message._chrys_echo_content_stripped = True

        snapshot = _message_snapshot(message, [content])

        assert snapshot is not message
        assert type(snapshot.contents) is ContentList
        assert snapshot.contents is not message.contents
        assert snapshot.contents[0] is content
        assert snapshot.additional_properties == message.additional_properties
        assert snapshot.additional_properties is not message.additional_properties
        assert snapshot._chrys_echo_content_stripped is False

    def test_echo_strip_copies_wrapper_and_list_only_when_stripping(self) -> None:
        """Copy class: echo-filter update copy (``_strip_echoed_update``). An
        update carrying an echo is shallow-copied with a filtered list — the
        incoming update is never mutated, surviving content objects stay
        shared — while an echo-free update passes through as the same object."""
        echoed = Content.from_text("old")
        fresh = Content.from_text("new")
        registry = WeakIdentityRegistry(ignore_usage=True)
        registry.register(echoed)

        update = ChatResponseUpdate(contents=[echoed, fresh], role="assistant")
        stripped = _strip_echoed_update(update, registry)

        assert stripped is not update
        assert list(stripped.contents) == [fresh]
        assert stripped.contents[0] is fresh
        assert stripped._chrys_echo_content_stripped is True
        assert list(update.contents) == [echoed, fresh]

        clean = ChatResponseUpdate(contents=[fresh], role="assistant")
        assert _strip_echoed_update(clean, registry) is clean


# ---------------------------------------------------------------------------
# Streaming assembly: coalescing mints fresh objects, fragments stay intact
# ---------------------------------------------------------------------------


class TestStreamingAssemblyCopies:
    def test_text_coalescing_mints_fresh_objects_and_leaves_fragments_unmutated(self) -> None:
        """Copy class: streamed-text coalescing (``_coalesce_text_content``
        deep-copies the first fragment and merges into the copy)."""
        first = Content.from_text("Hel")
        second = Content.from_text("lo")
        contents: list[Content] = [first, second]

        _coalesce_text_content(contents, "text")

        assert len(contents) == 1
        merged = contents[0]
        assert merged is not first
        assert merged is not second
        assert merged.text == "Hello"
        assert first.text == "Hel"
        assert second.text == "lo"

    def test_code_interpreter_coalescing_mints_fresh_objects(self) -> None:
        """Copy class: code-interpreter chunk coalescing
        (``_coalesce_code_interpreter_content`` deep-copies the first chunk
        per call id and merges later chunks into the copy)."""
        first = Content.from_code_interpreter_tool_call(call_id="ci-1", inputs=[Content.from_text("x = 1")])
        second = Content.from_code_interpreter_tool_call(call_id="ci-1", inputs=[Content.from_text("x = 1\nx")])
        contents: list[Content] = [first, second]

        _coalesce_code_interpreter_content(contents)

        assert len(contents) == 1
        merged = contents[0]
        assert merged is not first
        assert merged is not second
        assert first.inputs is not None
        assert len(first.inputs) == 1
        assert first.inputs[0].text == "x = 1"
        assert merged.inputs is not None
        assert merged.inputs[0] is not second.inputs[0]

    def test_merge_content_item_lists_never_aliases_incoming_items(self) -> None:
        """Copy class: nested content-list merge (``_merge_content_item_lists``).
        The incoming side is always deep-copied into the result; only the
        already-owned existing side may stay shared."""
        incoming_only = [Content.from_text("fresh")]
        minted = _merge_content_item_lists(None, incoming_only)
        assert minted is not incoming_only
        assert minted[0] is not incoming_only[0]

        existing_item = Content.from_function_call("c1", "tool_a")
        incoming_item = Content.from_function_call("c2", "tool_b")
        extended = _merge_content_item_lists([existing_item], [incoming_item])
        assert extended[0] is existing_item
        assert extended[1] is not incoming_item


# ---------------------------------------------------------------------------
# Compression and compaction: originals preserved, stored copies fresh
# ---------------------------------------------------------------------------


class TestCompressionAndCompactionCopies:
    def test_compress_state_deep_copies_fold_range_and_returns_originals(self) -> None:
        """Copy class: compressed-block storage (``_compress_state`` deep-copies
        the fold range into the block; the ORIGINAL objects are returned to the
        caller for tool-loop visibility flags)."""
        content = Content.from_text("old turn")
        folded = Message("user", [content])
        marker = Message("assistant", [Content.from_text("turn marker")])
        marker.additional_properties[_TURN_ID_KEY] = "marker-1"
        state: dict = {"messages": [folded, marker]}

        _context_id, fold_range = _compress_state(state, "marker-1", "summary text")

        assert fold_range[0] is folded
        assert fold_range[1] is marker
        block = state["compressed_msgs"][0]
        assert block.messages[0] is not folded
        assert block.messages[0].contents[0] is not content
        assert folded.contents[0] is content

    async def test_strict_compaction_commit_installs_fresh_working_copies(self) -> None:
        """Copy class: strict-atomicity transactional commit
        (``apply_compaction`` runs the strategy on a deep copy and commits via
        ``messages[:] = working`` — after a strict apply the live list holds
        the fresh working copies, never the pre-strategy originals)."""
        content = Content.from_text("kept")
        message = Message("user", [content])
        messages = [message]

        class _NoOpStrategy:
            async def __call__(self, msgs: list[Message], context: object = None) -> bool:
                del msgs, context
                return True

        await apply_compaction(messages, strategy=_NoOpStrategy(), strict_projection_atomicity=True)

        assert len(messages) == 1
        assert messages[0] is not message
        assert messages[0].contents[0] is not content
        assert message.contents[0] is content

    def test_entry_request_snapshot_owns_its_lists_and_shares_the_content_objects(self) -> None:
        """Copy class: the request a pass found, kept for the note's extension call
        (``UnifiedContextStrategy._snapshot_entry_request``).  The pass rewrites
        the live list from Phase 1 on — flags in the properties dict, results
        swapped for summaries in the contents list — so each view owns its
        wrapper, its contents list and the top level of its properties, and an
        excluded message is not taken.  The content objects are shared: they are
        what the provider has cached, and a phase replaces them rather than
        writing into them, as it does a nested property value."""
        content = Content.from_text("tool output")
        message = Message("assistant", [content])
        nested = {"key": "value"}
        message.additional_properties["nested"] = nested
        excluded = Message("assistant", [Content.from_text("already compacted")])
        set_excluded(excluded, excluded=True)
        registry = WeakIdentityRegistry()
        registry.register(message.contents)
        registry.register(content)
        context = CompactionCallContext(last_words_completer=MagicMock(), last_words_extends_request=True)

        snapshot = UnifiedContextStrategy()._snapshot_entry_request([excluded, message], context, 1.0)

        assert snapshot is not None
        [view] = snapshot
        assert view is not message
        assert view.contents is not message.contents
        assert type(view.contents) is ContentList
        assert view.contents[0] is content
        assert view.additional_properties is not message.additional_properties
        assert view.additional_properties["nested"] is nested
        assert view.contents not in registry
        assert view.contents[0] in registry
        assert message.contents in registry

        # What a phase does to the live message afterwards stays off the view.
        message.contents[0] = Content.from_text("[summary]")
        set_excluded(message, excluded=True)
        assert view.contents[0] is content
        assert EXCLUDED_KEY not in view.additional_properties


# ---------------------------------------------------------------------------
# Middleware-owned copies: scoped clipping and consumed-injection retention
# ---------------------------------------------------------------------------


class TestMiddlewareOwnedCopies:
    def test_scoped_clone_for_slice_deep_copies_contents_and_props(self) -> None:
        """Copy class: scoped-compaction slice clone (``clone_for_slice`` owns
        every mutable layer — contents AND props — so sanitizing or clipping a
        slice can never write through to the live message)."""
        content = Content.from_text("sliced")
        message = Message("assistant", [content])
        message.additional_properties["nested"] = {"key": "value"}

        cloned = clone_for_slice(message)

        assert cloned is not message
        assert cloned.contents is not message.contents
        assert cloned.contents[0] is not content
        assert cloned.additional_properties is not message.additional_properties
        assert cloned.additional_properties["nested"] == {"key": "value"}
        assert cloned.additional_properties["nested"] is not message.additional_properties["nested"]

    def test_injection_retained_copy_breaks_list_alias_and_shares_contents_and_props(self) -> None:
        """Copy class: consumed-injection retention (the middleware retains
        ``copy(msg)`` + a fresh list built from the wire object's contents, so
        a client mutating the received message in place cannot rewrite the
        retained/persisted copy; the content object and the props dict stay
        shared — props are the sanctioned write-through channel)."""
        content = Content.from_text("injected")
        message = Message("user", [content])
        message.additional_properties["marker"] = True

        retained = copy.copy(message)
        retained.contents = list(message.contents)

        assert retained is not message
        assert retained.contents is not message.contents
        assert type(retained.contents) is ContentList
        assert retained.contents[0] is content
        assert retained.additional_properties is message.additional_properties

        message.contents.append(Content.from_text("client mutation"))
        assert [item.text for item in retained.contents] == ["injected"]


# ---------------------------------------------------------------------------
# System-reminder per-call rebuilds: fresh wrapper/list, shared props dict
# ---------------------------------------------------------------------------


class TestSystemReminderRebuilds:
    """Copy class: the reminder middleware's per-call user-message rebuild
    (``SystemReminderMiddleware._rebuild``), used by enrichment, the
    LAST_WORDS refresh and the fold restore. Each replaces a list entry with a
    fresh wrapper and contents list — the original may be the live history
    message — while ``additional_properties`` stays THE original's dict, the
    write-through channel for exclusion flags and the reminder record."""

    @staticmethod
    def _middleware(monkeypatch: pytest.MonkeyPatch, *, catalog: str | None = None) -> SystemReminderMiddleware:
        return TestSystemReminderRebuilds._middleware_pair(monkeypatch, catalog=catalog)[0]

    @staticmethod
    def _middleware_pair(
        monkeypatch: pytest.MonkeyPatch, *, catalog: str | None = None
    ) -> tuple[SystemReminderMiddleware, LastWordsState]:
        middleware, last_words = reminder_pair(
            runtime=MagicMock(),
            skill_catalog_provider=(lambda: catalog) if catalog is not None else None,
        )
        monkeypatch.setattr(middleware.sources.runtime_env, "snapshot", lambda: "runtime hint")
        middleware.prepare_turn()
        return middleware, last_words

    def test_enrichment_rebuilds_text_and_shares_other_contents_and_props(self) -> None:
        text = Content.from_text("question")
        image = Content.from_uri("data:image/png;base64,AAA", media_type="image/png")
        message = Message("user", [text, image], message_id="msg_1")
        message.additional_properties["marker"] = True

        enriched = SystemReminderMiddleware._create_enriched(message, ["turn reminder"], [])

        assert enriched is not message
        assert type(enriched.contents) is ContentList
        assert enriched.contents is not message.contents
        # User text is re-minted escaped; any other content is shared.
        assert enriched.contents[0] is not text
        assert enriched.contents[1] is image
        assert enriched.additional_properties is message.additional_properties
        assert enriched.message_id == "msg_1"
        assert list(message.contents) == [text, image]

    def test_last_words_refresh_shares_kept_contents_and_props(self, monkeypatch: pytest.MonkeyPatch) -> None:
        middleware, last_words = self._middleware_pair(monkeypatch)
        last_words.set_last_words("progress note")
        content = Content.from_text("question")
        message = Message("user", [content])
        message.additional_properties["marker"] = True
        messages = [message]

        assert middleware.refresh_last_words_reminder(messages) == 0

        rebuilt = messages[0]
        assert rebuilt is not message
        assert type(rebuilt.contents) is ContentList
        assert rebuilt.contents is not message.contents
        assert rebuilt.contents[0] is content
        assert rebuilt.additional_properties is message.additional_properties
        assert list(message.contents) == [content]

    def test_fold_restore_shares_kept_contents_and_records_through_props(self, monkeypatch: pytest.MonkeyPatch) -> None:
        middleware = self._middleware(monkeypatch, catalog="skills catalog")
        content = Content.from_text("question")
        message = Message("user", [content])
        messages = [message]

        assert middleware.restore_folded_reminders(messages) == 0

        rebuilt = messages[0]
        assert rebuilt is not message
        assert type(rebuilt.contents) is ContentList
        assert rebuilt.contents is not message.contents
        assert rebuilt.contents[0] is content
        assert rebuilt.additional_properties is message.additional_properties
        assert list(message.contents) == [content]
        # The record lands on the shared dict, so the history message keeps it.
        assert HistoryMarkerKind.SYSTEM_REMINDERS_KEY in message.additional_properties


# ---------------------------------------------------------------------------
# History-manager trims: fresh wrapper/list/props, shared content objects
# ---------------------------------------------------------------------------


class TestHistoryTrimCopies:
    def test_answer_tail_trim_replaces_the_wrapper_and_shares_kept_contents(self) -> None:
        """Copy class: a bare Continue's exchange-tail trim
        (``SessionHistoryManager.remove_trailing_answer``). The trimmed message
        is a fresh wrapper with a fresh contents list and props dict, so a
        holder of the landed message (a retry snapshot) still sees it whole;
        the kept content objects stay shared, and so does the analytics id."""
        kept = Content.from_function_call("c1", "read_file", arguments={})
        dropped = Content.from_text("Partial")
        message = Message("assistant", [kept, dropped])
        message.additional_properties["meta"] = "value"
        state: dict[str, Any] = {"messages": [Message("user", ["go"]), message]}
        history = SessionHistoryManager()
        history.bind(state)

        history.remove_trailing_answer()

        trimmed = state["messages"][-1]
        assert trimmed is not message
        assert type(trimmed.contents) is ContentList
        assert trimmed.contents is not message.contents
        assert list(trimmed.contents) == [kept]
        assert trimmed.contents[0] is kept
        assert trimmed.additional_properties == message.additional_properties
        assert trimmed.additional_properties is not message.additional_properties
        assert list(message.contents) == [kept, dropped]
