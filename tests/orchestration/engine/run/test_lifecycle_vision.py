# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle tests for user-message image input gating."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chrys.orchestration.engine.run.attachments as attachment_helpers
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    Error,
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
    RuntimeModelDetails,
    TurnStarted,
    UserMessage,
    Warning,
)
from chrys.foundation.i18n import DisplayBlock
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel import Content, Message
from chrys.kernel.middleware import ChatContext
from chrys.orchestration.engine.run.attachments import (
    MAX_IMAGE_BYTES,
    AttachmentDiscoveryResult,
    AttachmentParseResult,
    ImageAttachment,
    ImageMention,
)
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.state.machine import EngineState, EngineStateMachine, Trigger
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.schema import HookDecision
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationTrace
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY
from tests.service.trajectory._fakes import CancelAckSink, FakeSink, make_context
from tests.support.components import make_current, make_turn_state
from tests.support.images import image_bytes
from tests.support.loaded_agents import SkillRefreshLoader, install_loaded_agent, make_manifest
from tests.support.reminder_calls import establish_request
from tests.support.turn_services import make_turn_coordinator, make_turn_runner
from tests.support.waiting import wait_for

_PNG = image_bytes()

_IMAGE_COMPRESSION_TIMEOUT_SECONDS = 30.0


def _prompt_content_preparer(host: object) -> PromptContentPreparer:
    return PromptContentPreparer(
        session=host.session,
        current=host.current,
        bus=host._bus,
        history=host._history,
        fsm=host._fsm,
        discover_mentions=attachment_helpers.discover_image_mentions,
        discover_references=attachment_helpers.discover_image_references,
        load_attachments=attachment_helpers.load_image_attachments,
        compression_timeout_seconds=_IMAGE_COMPRESSION_TIMEOUT_SECONDS,
    )


async def run_and_save(
    host: object,
    text: str,
    *,
    created_at: object = None,
    contents: list[object] | None = None,
    admission_preparation: PreparationTrace | None = None,
    run_scope=None,
    injection_window=None,
    user_message_id: str | None = None,
    typed_text: str | None = None,
) -> None:
    await make_turn_runner(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        writer=host.writer,
        loader=host.loader,
        bus=host._bus,
        fsm=host._fsm,
        history=host._history,
        trajectory_recorder=host._trajectory_recorder,
        workspace_change_tracker=host._workspace_change_tracker,
        settings_handle=host._settings_handle,
        persistence=host._persistence,
        on_successful_turn=host._on_successful_turn,
        retry_and_save=host.retry_and_save,
        prompt_content_preparer=_prompt_content_preparer(host),
    ).run_fresh(  # type: ignore[arg-type]
        text,
        created_at=created_at,
        contents=contents,
        admission_preparation=admission_preparation,
        run_scope=run_scope,
        injection_window=injection_window,
        user_message_id=user_message_id,
        typed_text=typed_text,
    )


async def on_user_message(host: object, event: UserMessage) -> None:
    await make_turn_coordinator(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        writer=host.writer,
        loader=host.loader,
        bus=host._bus,
        fsm=host._fsm,
        history=host._history,
        trajectory_recorder=host._trajectory_recorder,
        workspace_change_tracker=host._workspace_change_tracker,
        settings_handle=host._settings_handle,
        persistence=host._persistence,
        on_successful_turn=host._on_successful_turn,
        run_and_save=host.run_and_save,
        retry_and_save=host.retry_and_save,
        prompt_content_preparer=_prompt_content_preparer(host),
    ).on_user_message(  # type: ignore[arg-type]
        event
    )


class _PromptHookManager:
    def __init__(self, decision: HookDecision) -> None:
        self.decision = decision
        self.payloads: list[dict[str, Any]] = []

    def has_hooks_for(self, event: HookEvent) -> bool:
        return event == HookEvent.USER_PROMPT_SUBMIT

    async def fire(self, _event: HookEvent, payload: dict[str, Any], **_kwargs: object) -> HookDecision:
        self.payloads.append(payload)
        return self.decision


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class _Executor:
    def __init__(self) -> None:
        self.conversation = self
        self.user_message = ""
        self.run_calls: list[tuple[list[Any], Any]] = []
        self.run_failed = False
        self.was_interrupted = False
        self.fail_run = False
        self.last_error = None
        self.partial_answer = ()
        self.service_session_id = ""
        self.history_state: dict[str, object] = {}
        self.input_properties: dict[str, object] | None = None
        self.trajectory_context = None
        self.opening_item_ids: list[str | None] = []
        self.reset_counter_calls: list[bool] = []
        self.run_side_effect: Callable[[], Any] | None = None

    def set_opening_item_id(self, item_id: str | None) -> None:
        self.opening_item_ids.append(item_id)

    def set_user_message(self, text: str) -> None:
        self.user_message = text

    def set_user_messages(self, messages: list[str]) -> None:
        self.user_message = messages[-1] if messages else ""

    def reset_counters(self, *, reset_batch_id: bool) -> None:
        self.reset_counter_calls.append(reset_batch_id)

    def record_pre_run_interrupt(self) -> None:
        self.was_interrupted = True

    def begin_invocation(self, *, typed_text: str | None = None) -> None:
        pass

    def fresh_request(self, contents: list[Any], created_at: Any = None, *, typed_text: str | None = None):
        return contents, created_at

    @asynccontextmanager
    async def retry_request(self, additional_text: str = "", created_at: Any = None, *, withdraw: Any = None):
        yield ([additional_text] if additional_text else []), created_at

    def record_outcome(self, outcome):
        pass

    async def run(self, request) -> None:
        contents, created_at = request
        self.run_calls.append((contents, created_at))
        if self.run_side_effect is not None:
            await self.run_side_effect()
        if self.fail_run:
            self.run_failed = True

    @property
    def running(self) -> bool:
        return False

    def drain_decisions(self) -> list[object]:
        return []

    def drain_batch_records(self) -> list[object]:
        return []

    @property
    def backend(self):
        return self

    @property
    def inputs(self):
        return self

    @property
    def state(self):
        return self

    @property
    def approval(self):
        return self

    @property
    def tool_events(self):
        return self


class _History:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def has_trailing_error_markers(self) -> bool:
        raise AssertionError("history markers should not be inspected")

    def remove_trailing_markers(self) -> None:
        raise AssertionError("history markers should not be removed")

    def remove_orphaned_user_message(self) -> None:
        raise AssertionError("orphaned user message should not be removed")

    def ensure_user_message(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ensure_user_message should not be called")

    def tag_last_user_message(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("tag_last_user_message should not be called")

    def images_over(self, _max_bytes: int) -> list[object]:
        return []

    def merge_loop_messages(self, _loop_recorder: object, *, insert_index: int | None = None) -> None:
        return None

    def persist_approval_decisions(self, *_args: object, **_kwargs: object) -> None:
        return None

    def trim_to_last_complete_tool_results(self) -> None:
        return None

    def keep_partial_answer(self, _contents: Sequence[Content]) -> None:
        return None

    def persist_batch_ids(self, _batch_records: list[object]) -> dict[int, object]:
        return {}

    def persist_intermediate_texts(self, _texts: dict[int, str], _batch_anchors: dict[int, object]) -> None:
        return None

    def persist_consumed_injections(self, _injections: list[object]) -> None:
        return None

    def backfill_missing_created_at(self, *, start_index: int) -> None:
        return None

    def remove_awaiting_sub_agents_marker(self) -> None:
        return None

    def insert_interrupted_marker(self, *_args: object, **_kwargs: object) -> None:
        return None

    def remove_all_status_markers(self) -> None:
        return None

    def insert_turn_marker(self, started_at: object = None) -> None:
        return None


class _CapturingHistory(_History):
    def __init__(self) -> None:
        super().__init__()
        self.ensure_calls: list[dict[str, Any]] = []

    def ensure_user_message(self, text: str, **kwargs: Any) -> None:
        self.ensure_calls.append({"text": text, **kwargs})


class _MutableHistory(_History):
    def __init__(self, messages: list[Message]) -> None:
        super().__init__()
        self.messages = messages
        self.removed_trailing_markers = 0
        self.removed_orphans = 0

    def has_trailing_error_markers(self) -> bool:
        for message in reversed(self.messages):
            chrys_kind = message.additional_properties.get(HistoryMarkerKind.KEY)
            if chrys_kind == HistoryMarkerKind.TURN:
                continue
            return chrys_kind in HistoryMarkerKind.STATUS_MARKERS
        return False

    def remove_trailing_markers(self) -> None:
        while self.messages:
            chrys_kind = self.messages[-1].additional_properties.get(HistoryMarkerKind.KEY)
            if chrys_kind in HistoryMarkerKind.SESSION_COUNT_EXCLUDED:
                self.messages.pop()
                self.removed_trailing_markers += 1
                continue
            break

    def remove_orphaned_user_message(self) -> None:
        if self.messages and self.messages[-1].role == "user":
            self.messages.pop()
            self.removed_orphans += 1

    def ensure_user_message(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ensure_user_message should not be called")

    def tag_last_user_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Injection:
    def drain_pending(self) -> list[object]:
        return []


class _RunnerTrajectoryRecorder:
    """Keep a supplied in-memory context active across one runner pass."""

    def __init__(self, sink: FakeSink) -> None:
        self._context = make_context(sink)

    async def turn_started(self, **_kwargs: object) -> None:
        return None

    def context(self):
        return self._context


class _HostPermits:
    async def wait_for_agent_load_idle(self) -> None:
        return None


class _Host:
    def __init__(self, tmp_path: Path, *, vision: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        self.current = make_current(loaded=SimpleNamespace(), manifest=make_manifest())
        self._current = self.current
        self.permits = _HostPermits()
        self.session = SimpleNamespace(mark_surface=lambda: None)
        self._bus = _Bus()
        self.session.session_id = "session-1"
        self.session.workspace = Workspace(primary_cwd=str(tmp_path))
        install_loaded_agent(
            self,
            runtime_details=AgentRuntimeDetails(
                model=RuntimeModelDetails(name="Model", vision=vision),
            ),
        )
        self.session.hook_manager = None
        self.session.agent_profile = None
        install_loaded_agent(self, active_profile=None)
        self.session.runtime_meta = SessionRuntimeMetadata()
        install_loaded_agent(self, reminder_middleware=SystemReminderMiddleware())
        install_loaded_agent(self, skills_provider=None)
        install_loaded_agent(self, bindings=_Executor())
        self._fsm = EngineStateMachine()
        self._turn_state = make_turn_state()
        self._history = _History()
        self.session.shutting_down = False
        install_loaded_agent(self, consumed_injections=[])
        install_loaded_agent(self, intermediate_texts={})
        self.session.turn_number = 0
        install_loaded_agent(self, loop_recorder=None)
        self.session.mutation_tracker = None
        install_loaded_agent(self, agent_profile_fingerprint="")
        install_loaded_agent(self, model_profile_fingerprint="")
        self._persistence = SimpleNamespace(checkpoint_for=lambda _session_id: None, state_store=None)
        self._trajectory_recorder = TrajectoryRecorder()
        self._workspace_change_tracker = WorkspaceChangeTracker()
        self.settings = Settings(workspace_change_notice=False)
        self._settings_handle = SettingsHandle(LoadedSettings(settings=self.settings, provenance={}))
        monkeypatch.setattr(
            "chrys.orchestration.engine.run.runner.capture_snapshot_writer", self._capture_snapshot_writer
        )
        self.session.recovered_from_sidecar = False
        self.session.mutation_coordinator = None
        install_loaded_agent(self, injection=_Injection())
        self._turn_state.paused_sub_agents: set[str] = set()
        self.permits.session_generation = 0
        self.permits.build_generation = 0
        self.post_run_calls = 0
        self.rollback_snapshots = 0
        self.rollback_snapshot_capture_threads: list[threading.Thread] = []
        self.rollback_snapshot_threads: list[threading.Thread] = []
        self.rollback_snapshot_before_executor: list[bool] = []
        self.writer = SimpleNamespace(save_current_session=self._record_session_save)
        self.loader = SkillRefreshLoader(self.current)

    @property
    def pre_run_calls(self) -> int:
        return len(self.current.loaded.bindings.reset_counter_calls)

    async def run_and_save(
        self,
        text: str,
        created_at: Any = None,
        contents: list[Any] | None = None,
        *,
        admission_preparation: PreparationTrace | None = None,
        run_scope=None,
        injection_window=None,
        user_message_id: str | None = None,
        typed_text: str | None = None,
    ) -> None:
        await run_and_save(
            self,
            text,
            created_at=created_at,
            contents=contents,
            admission_preparation=admission_preparation,
            run_scope=run_scope,
            injection_window=injection_window,
            user_message_id=user_message_id,
            typed_text=typed_text,
        )

    async def _post_run(self) -> None:
        self.post_run_calls += 1

    async def _record_session_save(self) -> bool:
        self.post_run_calls += 1
        return True

    def _on_successful_turn(self) -> None:
        return None

    async def retry_and_save(self, *_args: object, **_kwargs: object) -> None:
        return None

    def _write_rollback_snapshot(self) -> None:
        self.rollback_snapshot_threads.append(threading.current_thread())
        self.rollback_snapshot_before_executor.append(not self.current.loaded.bindings.run_calls)
        self.rollback_snapshots += 1

    def _capture_snapshot_writer(self, session, settings_handle) -> Callable[[], None]:
        assert session is self.session
        assert settings_handle is self._settings_handle
        self.rollback_snapshot_capture_threads.append(threading.current_thread())
        return self._write_rollback_snapshot


class _FailingBeforeTurnHooks:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def has_hooks_for(self, event: HookEvent) -> bool:
        return event == HookEvent.BEFORE_TURN

    async def fire(self, *_args: object, **_kwargs: object) -> HookDecision:
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize("is_retry", [False, True], ids=["run_and_save", "run_retry"])
@pytest.mark.parametrize(
    ("terminal", "expected_outcome"),
    [
        ("handoff", PreparationOutcome.HANDOFF),
        ("interrupted", PreparationOutcome.INTERRUPTED),
        ("failed", PreparationOutcome.FAILED),
    ],
)
async def test_turn_preamble_closes_exactly_once_on_every_dispatch_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    is_retry: bool,
    terminal: str,
    expected_outcome: str,
) -> None:
    """Fresh and retry dispatches settle handoff, interruption, and failure once."""
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    sink = FakeSink()
    host._trajectory_recorder = _RunnerTrajectoryRecorder(sink)
    if terminal == "handoff":

        async def stop_after_handoff() -> None:
            raise asyncio.CancelledError

        host.current.loaded.bindings.run_side_effect = stop_after_handoff
        expected_error: type[BaseException] = asyncio.CancelledError
    elif terminal == "interrupted":
        host.session.hook_manager = _FailingBeforeTurnHooks(asyncio.CancelledError())  # type: ignore[assignment]
        expected_error = asyncio.CancelledError
    else:
        host.session.hook_manager = _FailingBeforeTurnHooks(RuntimeError("before-turn failure"))  # type: ignore[assignment]
        expected_error = RuntimeError

    with pytest.raises(expected_error):
        if is_retry:
            await make_turn_runner(
                current=host.current,
                session=host.session,
                turn_state=host._turn_state,
                permits=host.permits,
                writer=host.writer,
                loader=host.loader,
                bus=host._bus,
                fsm=host._fsm,
                history=host._history,
                trajectory_recorder=host._trajectory_recorder,
                workspace_change_tracker=host._workspace_change_tracker,
                settings_handle=host._settings_handle,
                persistence=host._persistence,
                on_successful_turn=host._on_successful_turn,
                retry_and_save=host.retry_and_save,
            ).run_retry("retry note")  # type: ignore[arg-type]
        else:
            await run_and_save(host, "hello", contents=["hello"])

    await wait_for(
        lambda: len(sink.of_type(EventType.PREPARATION_FINISHED)) == 1,
        description="turn preamble terminal",
    )
    preamble_starts = [
        draft for draft in sink.of_type(EventType.PREPARATION_STARTED) if draft.payload["scope"] == "turn_preamble"
    ]
    preamble_finishes = [
        draft for draft in sink.of_type(EventType.PREPARATION_FINISHED) if draft.payload["scope"] == "turn_preamble"
    ]
    assert len(preamble_starts) == len(preamble_finishes) == 1
    assert preamble_finishes[0].operation_id == preamble_starts[0].operation_id
    assert preamble_finishes[0].payload["outcome"] == expected_outcome
    sink.assert_operations_settled()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_retry", [False, True], ids=["fresh", "retry"])
async def test_turn_preamble_start_ack_cancellation_leaves_no_open_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    is_retry: bool,
) -> None:
    """Cancellation of the committed start acknowledgement schedules its terminal."""
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    sink = CancelAckSink(at=1)
    host._trajectory_recorder = _RunnerTrajectoryRecorder(sink)

    with pytest.raises(asyncio.CancelledError):
        if is_retry:
            await make_turn_runner(
                current=host.current,
                session=host.session,
                turn_state=host._turn_state,
                permits=host.permits,
                writer=host.writer,
                loader=host.loader,
                bus=host._bus,
                fsm=host._fsm,
                history=host._history,
                trajectory_recorder=host._trajectory_recorder,
                workspace_change_tracker=host._workspace_change_tracker,
                settings_handle=host._settings_handle,
                persistence=host._persistence,
                on_successful_turn=host._on_successful_turn,
                retry_and_save=host.retry_and_save,
            ).run_retry("retry note")  # type: ignore[arg-type]
        else:
            await run_and_save(host, "hello", contents=["hello"])

    await wait_for(
        lambda: sink.event_types == [EventType.PREPARATION_STARTED, EventType.PREPARATION_FINISHED],
        description="cancelled preamble terminal",
    )
    assert sink.event_types == [EventType.PREPARATION_STARTED, EventType.PREPARATION_FINISHED]
    assert sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.INTERRUPTED
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_run_and_save_rejects_image_when_model_profile_is_text_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)

    await run_and_save(host, "describe @shot.png")

    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    error = errors[0]
    image_path = tmp_path / "shot.png"
    assert (error.code, error.message, error.session_id) == (
        "vision_unsupported",
        (
            'The active model profile "Model" does not support image input.\n\n'
            'Enable "Vision Model" in Models, or switch to a multimodal profile and send the message again.\n\n'
            f"Image not attached:\n- {image_path}"
        ),
        "session-1",
    )
    assert error.display_message is not None
    assert error.display_message.definition.key == "attachments.vision_unsupported"
    assert dict(error.display_message.args) == {"files": DisplayBlock(f"- {image_path}"), "label": "Model"}
    assert error.display_message.count == 1
    assert host.current.loaded.bindings.run_calls == []
    assert host.pre_run_calls == 0
    assert host.post_run_calls == 0
    assert host.rollback_snapshots == 0


@pytest.mark.asyncio
async def test_run_and_save_does_not_read_image_bytes_before_text_only_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)

    def fail_read_bytes(_path: Path) -> bytes:
        raise AssertionError("text-only rejection should not read image bytes")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    await run_and_save(host, "describe @shot.png")

    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].code == "vision_unsupported"
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_run_and_save_text_only_missing_image_reports_vision_without_filesystem_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)

    await run_and_save(host, "describe @missing.png")

    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].code == "vision_unsupported"
    assert "does not support image input" in errors[0].message
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_run_and_save_text_only_rejects_existing_image_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)
    host._history.messages.append(
        Message(
            "user",
            [
                "describe @shot.png",
                Content.from_data(data=b"abc", media_type="image/png"),
            ],
        )
    )

    await run_and_save(host, "plain follow-up")

    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    error = errors[0]
    assert (error.code, error.message, error.session_id) == (
        "vision_unsupported",
        (
            'The active model profile "Model" does not support image input.\n\n'
            "This session already contains image attachments in its active history. "
            "Switch to a multimodal profile, or start a new text-only session before continuing."
        ),
        "session-1",
    )
    assert error.display_message is not None
    assert error.display_message.definition.key == "attachments.history_vision_unsupported"
    assert dict(error.display_message.args) == {"label": "Model"}
    assert error.display_message.count is None
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_run_and_save_sends_image_content_when_model_profile_supports_vision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    created_at = SimpleNamespace(marker="timestamp")

    await run_and_save(host, "describe @shot.png", created_at=created_at)

    events = [event for event in host._bus.events if not isinstance(event, TurnStarted)]
    assert events == []
    assert host.current.loaded.bindings.user_message == "describe @shot.png"
    assert len(host.current.loaded.bindings.run_calls) == 1
    contents, call_created_at = host.current.loaded.bindings.run_calls[0]
    assert call_created_at is created_at
    assert contents[0] == "describe @shot.png"
    assert contents[1].type == "data"
    assert contents[1].media_type == "image/png"
    assert host.pre_run_calls == 1
    assert host.current.loaded.bindings.reset_counter_calls == [True]
    assert host.post_run_calls == 1
    assert host.rollback_snapshots == 1


@pytest.mark.asyncio
async def test_run_and_save_writes_rollback_snapshot_off_event_loop_before_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    loop_thread = threading.current_thread()

    await run_and_save(host, "hello", contents=["hello"])

    assert host.rollback_snapshot_capture_threads == [loop_thread]
    assert len(host.rollback_snapshot_threads) == 1
    assert host.rollback_snapshot_threads[0] is not loop_thread
    assert host.rollback_snapshot_before_executor == [True]
    assert len(host.current.loaded.bindings.run_calls) == 1


@pytest.mark.asyncio
async def test_fresh_workspace_notice_computes_before_reminder_prepare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._settings_handle.override(workspace_change_notice=True)
    install_loaded_agent(host, reminder_middleware=SystemReminderMiddleware())
    order: list[str] = []
    original_compute = host._workspace_change_tracker.compute_turn_notice
    original_prepare = host.current.loaded.reminder_middleware.prepare_turn

    def _compute(**kwargs: Any) -> str | None:
        order.append("compute")
        return original_compute(**kwargs)

    def _prepare(**kwargs: Any) -> None:
        order.append("prepare")
        original_prepare(**kwargs)

    host._workspace_change_tracker.compute_turn_notice = _compute  # type: ignore[method-assign]
    host.current.loaded.reminder_middleware.prepare_turn = _prepare  # type: ignore[method-assign]

    await run_and_save(host, "hello", contents=["hello"])

    assert order[:2] == ["compute", "prepare"]


async def _deliver_reminders(middleware: SystemReminderMiddleware, text: str) -> list[str]:
    """Run one model-bound middleware pass and return the enriched user contents."""
    history_message = Message(role="user", contents=[text])
    context = ChatContext(client=None, messages=[history_message], options=None)

    async def _call_next() -> None:
        await establish_request(context)

    await middleware.process(context, _call_next)
    model_message = next(message for message in reversed(context.messages) if message.role == "user")
    return [content.text for content in model_message.contents if content.type == "text" and content.text]


@pytest.mark.asyncio
async def test_fresh_pre_executor_interrupt_requeues_drained_workspace_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    tracker = host._workspace_change_tracker
    tracker.queue_safety_notice("retained files")
    host._settings_handle.override(workspace_change_notice=True)
    install_loaded_agent(
        host, reminder_middleware=SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    )
    host._history = _CapturingHistory()
    current = asyncio.current_task()
    assert current is not None
    assert host._turn_state.lease.request_pre_executor_interrupt(current) is True

    await run_and_save(host, "hello", contents=["hello"])

    assert tracker.take_pending_notice() == "retained files"
    assert tracker.take_pending_notice() is None
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_requeued_notice_delivers_once_joined_before_new_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    tracker = host._workspace_change_tracker
    host._settings_handle.override(workspace_change_notice=True)
    tracker.retarget_roots(host.session.workspace)
    tracker.capture_baseline(1)
    tracker.queue_safety_notice("retained files")
    middleware = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    install_loaded_agent(host, reminder_middleware=middleware)
    host._history = _CapturingHistory()
    current = asyncio.current_task()
    assert current is not None
    assert host._turn_state.lease.request_pre_executor_interrupt(current) is True
    await run_and_save(host, "hello", contents=["hello"])
    assert host.current.loaded.bindings.run_calls == []

    (tmp_path / "external.txt").write_text("x", encoding="utf-8")
    delivered: list[str] = []

    async def _model_pass() -> None:
        delivered.extend(await _deliver_reminders(middleware, "again"))

    host.current.loaded.bindings.run_side_effect = _model_pass
    await run_and_save(host, "again", contents=["again"])

    staged = next(content for content in delivered if "retained files" in content)
    assert 'created: "external.txt"' in staged
    assert staged.index("retained files") < staged.index("Workspace changes since the previous turn")
    assert sum("retained files" in content for content in delivered) == 1
    assert tracker.take_pending_notice() is None
    assert middleware.take_undelivered_file_change() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_boundary", [False, True])
async def test_target_zero_safety_delivers_with_empty_and_nonempty_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_boundary: bool,
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._settings_handle.override(workspace_change_notice=True)
    tracker = host._workspace_change_tracker
    tracker.retarget_roots(host.session.workspace)
    tracker.capture_baseline(1)
    tracker.queue_safety_notice("Files retained from the discarded conversation:")
    if with_boundary:
        (tmp_path / "external.txt").write_text("x", encoding="utf-8")
    middleware = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    install_loaded_agent(host, reminder_middleware=middleware)
    delivered: list[str] = []

    async def _model_pass() -> None:
        delivered.extend(await _deliver_reminders(middleware, "hello"))

    host.current.loaded.bindings.run_side_effect = _model_pass

    await run_and_save(host, "hello", contents=["hello"])

    staged = next(content for content in delivered if "Files retained from the discarded conversation:" in content)
    if with_boundary:
        assert 'created: "external.txt"' in staged
        assert staged.index("Files retained") < staged.index("Workspace changes since the previous turn")
    else:
        assert "Workspace changes since the previous turn" not in staged
    assert middleware.take_undelivered_file_change() is None
    assert tracker.take_pending_notice() is None


@pytest.mark.asyncio
async def test_disabled_setting_delivers_queued_safety_once_without_compute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._settings_handle.override(workspace_change_notice=False)
    tracker = host._workspace_change_tracker
    computes: list[dict[str, Any]] = []

    def _compute(**kwargs: Any) -> None:
        computes.append(kwargs)

    tracker.compute_turn_notice = _compute  # type: ignore[method-assign]
    tracker.queue_safety_notice("Files retained from the discarded conversation:")
    middleware = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    install_loaded_agent(host, reminder_middleware=middleware)
    delivered: list[str] = []

    async def _model_pass() -> None:
        delivered.extend(await _deliver_reminders(middleware, "turn"))

    host.current.loaded.bindings.run_side_effect = _model_pass

    await run_and_save(host, "hello", contents=["hello"])
    assert computes == []
    assert sum("Files retained from the discarded conversation:" in content for content in delivered) == 1

    await run_and_save(host, "again", contents=["again"])
    assert computes == []
    assert sum("Files retained from the discarded conversation:" in content for content in delivered) == 1
    assert middleware.take_undelivered_file_change() is None
    assert tracker.take_pending_notice() is None


@pytest.mark.asyncio
async def test_reenable_after_invalidate_does_not_flood(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    tracker = host._workspace_change_tracker
    tracker.retarget_roots(host.session.workspace)
    tracker.capture_baseline(1)
    (tmp_path / "offline.txt").write_text("x", encoding="utf-8")
    tracker.invalidate()
    host._settings_handle.override(workspace_change_notice=True)
    middleware = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    install_loaded_agent(host, reminder_middleware=middleware)

    await run_and_save(host, "hello", contents=["hello"])

    assert middleware.take_undelivered_file_change() is None


@pytest.mark.asyncio
async def test_failed_run_requeues_notice_that_never_reached_a_model_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._settings_handle.override(workspace_change_notice=False)
    tracker = host._workspace_change_tracker
    tracker.queue_safety_notice("retained files")
    middleware = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    install_loaded_agent(host, reminder_middleware=middleware)
    host._history = _CapturingHistory()
    host.current.loaded.bindings.fail_run = True

    await run_and_save(host, "hello", contents=["hello"])

    assert middleware.take_undelivered_file_change() is None
    assert tracker.take_pending_notice() == "retained files"
    assert tracker.take_pending_notice() is None


@pytest.mark.asyncio
async def test_cancelled_run_requeues_notice_that_never_reached_a_model_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._settings_handle.override(workspace_change_notice=False)
    tracker = host._workspace_change_tracker
    tracker.queue_safety_notice("retained files")
    middleware = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
    install_loaded_agent(host, reminder_middleware=middleware)

    async def _cancel() -> None:
        raise asyncio.CancelledError

    host.current.loaded.bindings.run_side_effect = _cancel

    with pytest.raises(asyncio.CancelledError):
        await run_and_save(host, "hello", contents=["hello"])

    assert middleware.take_undelivered_file_change() is None
    assert tracker.take_pending_notice() == "retained files"


@pytest.mark.asyncio
async def test_mid_turn_injection_does_not_compute_workspace_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._settings_handle.override(workspace_change_notice=True)
    calls: list[dict[str, Any]] = []

    def _compute(**kwargs: Any) -> None:
        calls.append(kwargs)

    host._workspace_change_tracker.compute_turn_notice = _compute  # type: ignore[method-assign]
    host._fsm.try_transition(Trigger.START)
    host._fsm.try_transition(Trigger.USER_MESSAGE)

    await on_user_message(host, UserMessage(text="injected guidance"))

    assert calls == []


@pytest.mark.asyncio
async def test_run_and_save_loads_small_image_attachments_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    loop_progress_before_executor: list[bool] = []
    progress_marked = threading.Event()

    def slow_load_image_attachments(mentions: list[Any], *, max_bytes: int) -> AttachmentParseResult:
        # Hold the load open until the marker task has provably run on
        # the event loop.  Loaded off-loop (executor thread), this
        # handshake completes immediately; if the load ever regresses
        # to running ON the loop thread, the marker cannot run while
        # the loop is blocked here — the wait times out and the marker
        # then observes the post-executor state.  Deterministic in both
        # directions, unlike the previous fixed-sleep race (flaky on
        # loaded CI runners).
        progress_marked.wait(timeout=5)
        mention = mentions[0]
        return AttachmentParseResult(
            attachments=[
                ImageAttachment(
                    path=mention.path,
                    mention=mention.mention,
                    media_type=mention.media_type,
                    data=b"abc",
                    size=3,
                )
            ],
            errors=[],
        )

    async def mark_loop_progress() -> None:
        loop_progress_before_executor.append(host.current.loaded.bindings.run_calls == [])
        progress_marked.set()

    monkeypatch.setattr(attachment_helpers, "load_image_attachments", slow_load_image_attachments)

    run_task = asyncio.create_task(run_and_save(host, "describe @shot.png"))
    progress_task = asyncio.create_task(mark_loop_progress())
    await run_task
    await progress_task

    assert loop_progress_before_executor == [True]
    assert len(host.current.loaded.bindings.run_calls) == 1


@pytest.mark.asyncio
async def test_on_user_message_exposes_prepared_contents_for_tui_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._turn_state.lease.run_task = None
    host._history = _MutableHistory([])
    event = UserMessage(text="describe @shot.png")

    await on_user_message(host, event)
    assert host._turn_state.lease.run_task is not None
    await host._turn_state.lease.run_task

    assert event.prepared_contents is not None
    assert event.prepared_contents[0] == "describe @shot.png"
    assert event.prepared_contents[1].type == "data"
    assert event.prepared_contents[1].media_type == "image/png"


@pytest.mark.asyncio
async def test_user_message_publish_returns_after_prepared_contents_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    bus = EventBus()
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._bus = bus
    host._turn_state.lease.run_task = None
    host._history = _MutableHistory([])
    await bus.subscribe(UserMessage, lambda event: on_user_message(host, event))
    event = UserMessage(text="describe @shot.png")

    await bus.publish(event)
    assert host._turn_state.lease.run_task is not None
    await host._turn_state.lease.run_task

    assert event.prepared_contents is not None
    assert event.prepared_contents[0] == "describe @shot.png"
    assert event.prepared_contents[1].type == "data"
    assert event.prepared_contents[1].media_type == "image/png"


@pytest.mark.asyncio
async def test_fresh_user_message_stale_admission_suppresses_attachment_error_after_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._history = _MutableHistory([])
    entered = threading.Event()
    release = threading.Event()

    def discover_mentions(_text: str, _cwd: Path) -> AttachmentDiscoveryResult:
        return AttachmentDiscoveryResult(
            mentions=[ImageMention(path=tmp_path / "stale.png", mention="@stale.png", media_type="image/png", size=1)],
            errors=[],
        )

    def load_attachments(_mentions: list[ImageMention], *, max_bytes: int) -> AttachmentParseResult:
        entered.set()
        release.wait(timeout=2)
        return AttachmentParseResult(attachments=[], errors=["@stale.png: stale load error"])

    def prompt_content_preparer(stale_host: object) -> PromptContentPreparer:
        return PromptContentPreparer(
            session=stale_host.session,
            current=stale_host.current,
            bus=stale_host._bus,
            history=stale_host._history,
            fsm=stale_host._fsm,
            discover_mentions=discover_mentions,
            discover_references=lambda _text, _cwd: [],
            load_attachments=load_attachments,
        )

    task = asyncio.create_task(
        make_turn_coordinator(
            current=host.current,
            session=host.session,
            turn_state=host._turn_state,
            permits=host.permits,
            writer=host.writer,
            loader=host.loader,
            bus=host._bus,
            fsm=host._fsm,
            history=host._history,
            trajectory_recorder=host._trajectory_recorder,
            workspace_change_tracker=host._workspace_change_tracker,
            settings_handle=host._settings_handle,
            persistence=host._persistence,
            on_successful_turn=host._on_successful_turn,
            run_and_save=host.run_and_save,
            retry_and_save=host.retry_and_save,
            prompt_content_preparer=prompt_content_preparer(host),
        ).on_user_message(UserMessage(text="describe @stale.png"))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)

        old_generation = host.permits.session_generation
        host.session.session_id = "session-2"
        host.permits.session_generation += 1
        host._turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=old_generation,
            prompt_admission_owner="session:restore:1",
        )
        release.set()
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert [event for event in host._bus.events if isinstance(event, Error)] == []
    assert host._turn_state.lease.run_task is None
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_user_message_rejects_text_only_prepared_image_contents_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)
    host._history = _MutableHistory([])
    event = UserMessage(
        text="describe embedded image",
        prepared_contents=["describe embedded image", Content.from_data(data=b"abc", media_type="image/png")],
    )

    await on_user_message(host, event)

    errors = [published for published in host._bus.events if isinstance(published, Error)]
    assert len(errors) == 1
    assert errors[0].code == "vision_unsupported"
    assert host._turn_state.lease.run_task is None
    assert host.current.loaded.bindings.run_calls == []
    assert host._fsm.state is EngineState.UNINITIALIZED
    # A refused input never becomes a turn: no identity is minted, nothing is
    # registered, and no client hears about a turn that did not start.
    assert [published for published in host._bus.events if isinstance(published, TurnStarted)] == []
    assert host.current.loaded.bindings.backend.history_state.get(TRAJECTORY_STATE_KEY) is None


@pytest.mark.asyncio
async def test_run_and_save_emits_compression_events_for_oversize_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "huge.png"
    with image.open("wb") as f:
        f.truncate(MAX_IMAGE_BYTES + 1)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)

    def fake_load_image_attachments(mentions: list[Any], *, max_bytes: int) -> AttachmentParseResult:
        mention = mentions[0]
        return AttachmentParseResult(
            attachments=[
                ImageAttachment(
                    path=mention.path,
                    mention=mention.mention,
                    media_type="image/jpeg",
                    data=b"compressed",
                    size=len(b"compressed"),
                )
            ],
            errors=[],
        )

    monkeypatch.setattr(attachment_helpers, "load_image_attachments", fake_load_image_attachments)

    await run_and_save(host, "describe @huge.png")

    events = [event for event in host._bus.events if not isinstance(event, TurnStarted)]
    assert [type(event) for event in events] == [
        ImageAttachmentCompressionStarted,
        ImageAttachmentCompressionFinished,
    ]
    assert events[0].image_count == 1
    assert events[1].image_count == 1
    contents, _created_at = host.current.loaded.bindings.run_calls[0]
    assert contents[0] == "describe @huge.png"
    assert contents[1].type == "data"
    assert contents[1].media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_run_and_save_times_out_slow_image_compression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "huge.png"
    with image.open("wb") as f:
        f.truncate(MAX_IMAGE_BYTES + 1)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)

    def slow_load_image_attachments(_mentions: list[Any], *, max_bytes: int) -> AttachmentParseResult:
        time.sleep(0.05)
        return AttachmentParseResult(attachments=[], errors=[])

    monkeypatch.setattr(sys.modules[__name__], "_IMAGE_COMPRESSION_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(attachment_helpers, "load_image_attachments", slow_load_image_attachments)

    await run_and_save(host, "describe @huge.png")

    events = [event for event in host._bus.events if not isinstance(event, TurnStarted)]
    assert [type(event) for event in events] == [
        ImageAttachmentCompressionStarted,
        ImageAttachmentCompressionFinished,
        Error,
    ]
    error = events[2]
    item = (
        "@huge.png: Image preparation took longer than 1 second. "
        "Resize oversized images or send fewer images, then try again."
    )
    assert (error.code, error.message, error.session_id) == (
        "image_attachment_error",
        f"We couldn't attach this image.\n\n- {item}",
        "session-1",
    )
    assert error.display_message is not None
    assert error.display_message.definition.key == "attachments.attachment_error"
    assert dict(error.display_message.args) == {"items": DisplayBlock(f"- {item}")}
    assert error.display_message.count == 1
    assert host.current.loaded.bindings.run_calls == []
    assert host.pre_run_calls == 0


@pytest.mark.asyncio
async def test_run_and_save_failure_fallback_preserves_image_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._history = _CapturingHistory()
    host.current.loaded.bindings.fail_run = True

    await run_and_save(host, "describe @shot.png")

    assert len(host._history.ensure_calls) == 1
    call = host._history.ensure_calls[0]
    assert call["text"] == "describe @shot.png"
    contents = call["contents"]
    assert contents[0] == "describe @shot.png"
    assert contents[1].type == "data"
    assert contents[1].media_type == "image/png"


@pytest.mark.asyncio
async def test_run_and_save_rejects_unreadable_image_before_run_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    discovered = attachment_helpers.discover_image_mentions("describe @missing.png", tmp_path)

    await run_and_save(host, "describe @missing.png")

    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    error = errors[0]
    items = DisplayBlock("\n".join(f"- {item}" for item in discovered.errors))
    assert (error.code, error.message, error.session_id) == (
        "image_attachment_error",
        attachment_helpers.format_attachment_error_message(discovered.errors),
        "session-1",
    )
    assert error.display_message is not None
    assert error.display_message.definition.key == "attachments.attachment_error"
    assert dict(error.display_message.args) == {"items": items}
    assert error.display_message.count == 1
    assert host.current.loaded.bindings.run_calls == []
    assert host.pre_run_calls == 0


@pytest.mark.asyncio
async def test_fresh_image_load_error_publishes_legacy_and_localized_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    mention = ImageMention(
        path=tmp_path / "load error.png",
        mention='@"load error.png"',
        media_type="image/png",
        size=3,
    )
    load_errors = ['@"load error.png": decoder failed {bad}\ncontinued']
    preparer = PromptContentPreparer(
        session=host.session,
        current=host.current,
        bus=host._bus,
        history=host._history,
        fsm=host._fsm,
        load_attachments=lambda _mentions, *, max_bytes: AttachmentParseResult(attachments=[], errors=load_errors),
    )

    result = await preparer.prepare_fresh(
        'describe @"load error.png"',
        discovered=AttachmentDiscoveryResult(mentions=[mention], errors=[]),
    )

    assert result is None
    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    error = errors[0]
    assert (error.code, error.message, error.session_id) == (
        "image_attachment_error",
        'We couldn\'t attach this image.\n\n- @"load error.png": decoder failed {bad}\ncontinued',
        "session-1",
    )
    assert error.display_message is not None
    assert error.display_message.definition.key == "attachments.attachment_error"
    assert dict(error.display_message.args) == {
        "items": DisplayBlock('- @"load error.png": decoder failed {bad}\ncontinued')
    }
    assert error.display_message.count == 1


@pytest.mark.asyncio
async def test_running_image_attachment_rejection_is_warning_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._fsm = EngineStateMachine()
    host._fsm._state = EngineState.RUNNING
    host._turn_state.lease.run_task = None

    await on_user_message(host, UserMessage(text="describe @shot.png"))

    assert len(host._bus.events) == 1
    event = host._bus.events[0]
    assert isinstance(event, Warning)
    assert (event.code, event.message, event.session_id) == (
        "image_attachment_while_running",
        (
            "Images cannot be added while the agent is responding.\n\n"
            "Wait for the current response to finish, then send the image in a new message."
        ),
        "session-1",
    )
    assert event.display_message is not None
    assert event.display_message.definition.key == "prompt_content.image_attachment_while_running"
    assert dict(event.display_message.args) == {}
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_running_invalid_image_attachment_publishes_legacy_and_localized_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=True)
    host._fsm = EngineStateMachine()
    host._fsm._state = EngineState.RUNNING
    host._turn_state.lease.run_task = None
    discovered = attachment_helpers.discover_image_mentions("describe @missing.png", tmp_path)

    await on_user_message(host, UserMessage(text="describe @missing.png"))

    assert len(host._bus.events) == 1
    warning = host._bus.events[0]
    assert isinstance(warning, Warning)
    items = DisplayBlock("\n".join(f"- {item}" for item in discovered.errors))
    assert (warning.code, warning.message, warning.session_id) == (
        "image_attachment_error",
        attachment_helpers.format_attachment_error_message(discovered.errors),
        "session-1",
    )
    assert warning.display_message is not None
    assert warning.display_message.definition.key == "attachments.attachment_error"
    assert dict(warning.display_message.args) == {"items": items}
    assert warning.display_message.count == 1
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_rejected_image_does_not_mutate_failed_run_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)
    host._fsm = EngineStateMachine()
    host._fsm._state = EngineState.FAILED
    host._turn_state.lease.run_task = None

    await on_user_message(host, UserMessage(text="describe @shot.png"))

    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].code == "vision_unsupported"
    assert host._fsm.state is EngineState.FAILED
    assert host.current.loaded.bindings.run_calls == []


@pytest.mark.asyncio
async def test_text_only_follow_up_ignores_failed_orphan_image_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image_message = Message(
        "user",
        [
            "describe @shot.png",
            Content.from_data(data=b"abc", media_type="image/png"),
        ],
    )
    interrupted = Message("assistant", ["Execution interrupted"])
    interrupted.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)
    host._fsm._state = EngineState.FAILED
    host._turn_state.lease.run_task = None
    history = _MutableHistory([image_message, interrupted, turn])
    host._history = history

    await on_user_message(host, UserMessage(text="plain follow-up"))
    assert host._turn_state.lease.run_task is not None
    await host._turn_state.lease.run_task

    assert [event for event in host._bus.events if isinstance(event, Error)] == []
    assert len(host.current.loaded.bindings.run_calls) == 1
    contents, _created_at = host.current.loaded.bindings.run_calls[0]
    assert contents == ["plain follow-up"]
    assert history.removed_trailing_markers == 2
    assert history.removed_orphans == 1


@pytest.mark.asyncio
async def test_user_prompt_submit_hook_sees_unsupported_image_prompt_before_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(_PNG)
    host = _Host(tmp_path, monkeypatch=monkeypatch, vision=False)
    host._fsm = EngineStateMachine()
    host._turn_state.lease.run_task = None
    hook_manager = _PromptHookManager(HookDecision())
    host.session.hook_manager = hook_manager

    await on_user_message(host, UserMessage(text="describe @shot.png"))

    assert len(hook_manager.payloads) == 1
    assert hook_manager.payloads[0]["text"] == "describe @shot.png"
    errors = [event for event in host._bus.events if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].code == "vision_unsupported"
