# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retry lifecycle around ``TurnRunner.run_retry`` — approval context, workspace notice, dispatch.

Drives the run package (``TurnRunner``, ``TurnCoordinator``,
``RetryCoordinator``) against an engine stand-in, so a retry's approval
context, workspace-change notice identity, marker hygiene, and reminder
carry-over are asserted without building a real engine.  The end-to-end
engine coverage lives in ``test_retry_integration.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeModelDetails, UserRetry
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import UserMessageKind
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.text.images import LoadedImage
from chrys.kernel import Content, Message
from chrys.orchestration.engine.state.machine import EngineState, EngineStateMachine, Trigger
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.session.history import OversizedImage, SessionHistoryManager
from tests.support.components import make_current, make_turn_state
from tests.support.loaded_agents import SkillRefreshLoader, install_loaded_agent, make_manifest
from tests.support.reminder_inputs import manifest_entry
from tests.support.reminder_stack import reminder_pair
from tests.support.turn_services import make_turn_coordinator, make_turn_retry, make_turn_runner

# ---------------------------------------------------------------------------
# Drivers — the run package is a set of collaborators over an engine host
# ---------------------------------------------------------------------------


async def retry_and_save(
    host: object,
    additional_text: str = "",
    created_at: object = None,
    **kwargs: object,
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
        persistence=host._persistence,
        on_successful_turn=host._on_successful_turn,
        retry_and_save=host.retry_and_save,
        settings_handle=SimpleNamespace(settings=host.settings),
    ).run_retry(additional_text, created_at=created_at, **kwargs)  # type: ignore[arg-type]


async def post_run(host: object) -> None:
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
        persistence=host._persistence,
        on_successful_turn=host._on_successful_turn,
        retry_and_save=host.retry_and_save,
        settings_handle=SimpleNamespace(settings=host.settings),
    ).finalize_current_run()  # type: ignore[arg-type]


async def on_user_retry(host: object, event: UserRetry) -> None:
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
        persistence=host._persistence,
        on_successful_turn=host._on_successful_turn,
        retry_and_save=host.retry_and_save,
        settings_handle=SimpleNamespace(settings=host.settings),
    ).on_user_retry(event)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _Executor:
    def __init__(self) -> None:
        self.conversation = self
        self.user_messages: list[str] = []
        self.resume_texts: list[str] = []
        self.run_failed = False
        self.was_interrupted = False
        self.last_error = None
        self.partial_answer = ()
        self.service_session_id = ""
        self.running = False
        self.on_resume = None
        self.history_state: dict[str, object] = {}
        self.input_properties: dict[str, object] | None = None
        self.trajectory_context = None
        self.opening_item_ids: list[str | None] = []
        self.reset_counter_calls: list[bool] = []

    def set_opening_item_id(self, item_id: str | None) -> None:
        self.opening_item_ids.append(item_id)

    def set_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def set_user_messages(self, messages: list[str]) -> None:
        self.user_messages.extend(messages)

    def reset_counters(self, *, reset_batch_id: bool) -> None:
        self.reset_counter_calls.append(reset_batch_id)

    @asynccontextmanager
    async def retry_request(self, additional_text: str = "", created_at: object = None, *, withdraw: object = None):
        yield additional_text

    def record_outcome(self, outcome):
        pass

    async def run(self, request) -> None:
        self.resume_texts.append(request)
        if self.on_resume is not None:
            self.on_resume()

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
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.tags: list[tuple[str, str]] = []
        self._input_history = SessionHistoryManager()
        self._input_history.bind({"messages": messages})

    def ensure_user_message(
        self,
        text: str,
        created_at: datetime | str | None = None,
        contents: list[Any] | None = None,
        *,
        kind: UserMessageKind = "opener",
        item_id: str | None = None,
        reminder_source: Mapping[str, Any] | None = None,
    ) -> None:
        self._input_history.ensure_user_message(
            text, created_at, contents, kind=kind, item_id=item_id, reminder_source=reminder_source
        )

    def tag_last_user_message(self, key: str, value: str) -> None:
        self.tags.append((key, value))

    def images_over(self, max_bytes: int) -> list[OversizedImage]:
        return self._input_history.images_over(max_bytes)

    def shrink_images(self, shrunk: Sequence[tuple[OversizedImage, LoadedImage]]) -> int:
        return self._input_history.shrink_images(shrunk)

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

    def remove_trailing_markers(self) -> None:
        return None


class _RuntimeMeta:
    def __init__(self) -> None:
        self.last_usage_details = {"input_token_count": 7}


class _ProfileSwitch:
    def __init__(self) -> None:
        self.consumed_switch_to: str | None = None


class _Sources:
    def __init__(self) -> None:
        self.profile_switch = _ProfileSwitch()


class _Reminder:
    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self.sources = _Sources()

    def prepare_turn(self, **kwargs: object) -> None:
        self.prepare_calls.append(kwargs)
        self.sources.profile_switch.consumed_switch_to = None

    def take_undelivered_file_change(self) -> str | None:
        return None


class _Injection:
    def drain_pending(self) -> list[object]:
        return []


class _HostPermits:
    async def wait_for_agent_load_idle(self) -> None:
        return None


class _Host:
    """Engine stand-in exposing exactly the surface the run package touches.

    ``reminder_middleware`` swaps the recording fake for a real
    :class:`SystemReminderMiddleware` when a test needs live reminder state,
    and ``dispatch_retries`` opts into the real retry task the
    :class:`TurnCoordinator` creates — the direct ``run_retry`` tests keep the
    no-op so a queued retry can never re-enter the runner behind their backs.
    """

    def __init__(
        self,
        messages: list[Message],
        *,
        reminder_middleware: SystemReminderMiddleware | None = None,
        dispatch_retries: bool = False,
    ) -> None:
        self.current = make_current(loaded=SimpleNamespace(), manifest=make_manifest())
        self._current = self.current
        self.permits = _HostPermits()
        self.session = SimpleNamespace(mark_surface=lambda: None)
        self._turn_state = make_turn_state()
        self._bus = EventBus()
        self.session.session_id = None
        self.permits.agent_loading = False
        self._dispatch_retries = dispatch_retries
        install_loaded_agent(self, bindings=_Executor())
        self._history = _History(messages)
        self.session.runtime_meta = _RuntimeMeta()
        install_loaded_agent(
            self, reminder_middleware=reminder_middleware if reminder_middleware is not None else _Reminder()
        )
        self.session.hook_manager = None
        self.session.outbox_recovery_task = None
        self.session.agent_profile = None
        self.session.turn_number = 0
        self._fsm = EngineStateMachine()
        install_loaded_agent(self, consumed_injections=[])
        install_loaded_agent(self, intermediate_texts={})
        install_loaded_agent(self, loop_recorder=None)
        self.session.mutation_tracker = None
        install_loaded_agent(self, agent_profile_fingerprint="")
        install_loaded_agent(self, model_profile_fingerprint="")
        self._persistence = SimpleNamespace(checkpoint_for=lambda _session_id: None, state_store=None)
        self._trajectory_recorder = TrajectoryRecorder()
        self._workspace_change_tracker = WorkspaceChangeTracker()
        self.settings = Settings(workspace_change_notice=False)
        self.session.mutation_coordinator = None
        install_loaded_agent(self, injection=_Injection())
        self.session.shutting_down = False
        self._turn_state.paused_sub_agents: set[str] = set()
        self.session.workspace = None
        install_loaded_agent(self, runtime_details=AgentRuntimeDetails(model=RuntimeModelDetails(vision=True)))
        install_loaded_agent(self, skills_provider=None)
        self.permits.session_generation = 0
        self.permits.build_generation = 0
        self.post_run_calls = 0
        self.writer = SimpleNamespace(save_current_session=self._record_session_save)
        self.loader = SkillRefreshLoader(self.current)

    async def _record_session_save(self) -> bool:
        self.post_run_calls += 1
        return True

    def _on_successful_turn(self) -> None:
        return None

    async def retry_and_save(
        self,
        additional_text: str = "",
        created_at: object = None,
        **kwargs: object,
    ) -> None:
        if not self._dispatch_retries:
            return
        await retry_and_save(self, additional_text, created_at, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryLifecycleApprovalContext:
    """retry_and_save restores approval middleware context after pre-run reset."""

    async def test_retry_without_note_uses_latest_user_message(self) -> None:
        host = _Host(
            [
                Message("user", ["original request"]),
                Message("assistant", ["tool call"]),
                Message("tool", ["result"]),
            ]
        )

        await retry_and_save(host)

        assert host.current.loaded.bindings.user_messages == ["original request"]
        assert host.current.loaded.bindings.resume_texts == [""]
        assert host.current.loaded.reminder_middleware.prepare_calls == [
            {"usage": {"input_token_count": 7}, "preserve_last_words": True, "preserve_turn_reminders": True}
        ]
        assert host.current.loaded.bindings.reset_counter_calls == [False]
        assert host.session.turn_number == 0
        assert host.post_run_calls == 1

    async def test_retry_with_note_uses_note_for_approval_context(self) -> None:
        host = _Host([Message("user", ["original request"])])

        await retry_and_save(host, additional_text=" also inspect env ")

        assert host.current.loaded.bindings.user_messages == ["original request", "also inspect env"]
        assert host.current.loaded.bindings.resume_texts == [" also inspect env "]
        assert host.current.loaded.reminder_middleware.prepare_calls == [
            {"usage": {"input_token_count": 7}, "preserve_last_words": True, "preserve_turn_reminders": True}
        ]

    @pytest.mark.parametrize(
        ("is_retry", "agent_turn", "overlap_turn"),
        [(False, 7, 7), (True, None, 8)],
    )
    async def test_workspace_notice_identity_uses_live_turn_number(
        self,
        is_retry: bool,
        agent_turn: int | None,
        overlap_turn: int,
    ) -> None:
        host = _Host([Message("user", ["request"])])
        host.session.turn_number = 8
        host.current.loaded.bindings.backend.history_state["turn_counter"] = 999
        host.settings = Settings(workspace_change_notice=True)
        host.session.recovered_from_sidecar = False
        calls: list[dict[str, object]] = []

        def _compute(**kwargs: object) -> None:
            calls.append(kwargs)

        host._workspace_change_tracker.compute_turn_notice = _compute  # type: ignore[method-assign]

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
            persistence=host._persistence,
            on_successful_turn=host._on_successful_turn,
            retry_and_save=host.retry_and_save,
            settings_handle=SimpleNamespace(settings=host.settings),
        )._compute_workspace_notice(is_retry=is_retry)

        assert len(calls) == 1
        assert calls[0]["turn_id"] == 8
        assert calls[0]["agent_turn_id"] == agent_turn
        assert calls[0]["overlap_turn_id"] == overlap_turn

    async def test_workspace_notice_timeout_leaves_persistent_caveat(self) -> None:
        """The timeout branch must queue the degraded warning — bare
        cancellation would let finalization's fresh baseline absorb the
        lost comparison silently."""
        host = _Host([Message("user", ["request"])])
        host.session.turn_number = 3
        host.settings = Settings(workspace_change_notice=True)
        host.session.recovered_from_sidecar = False

        def _compute(**_kwargs: object) -> None:
            raise TimeoutError

        host._workspace_change_tracker.compute_turn_notice = _compute  # type: ignore[method-assign]

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
            persistence=host._persistence,
            on_successful_turn=host._on_successful_turn,
            retry_and_save=host.retry_and_save,
            settings_handle=SimpleNamespace(settings=host.settings),
        )._compute_workspace_notice(is_retry=False)

        notice = host._workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert "Workspace change detection could not be completed" in notice

    async def test_retry_computes_notice_before_prepare_turn(self) -> None:
        host = _Host([Message("user", ["request"])])
        host.session.turn_number = 3
        host.settings = Settings(workspace_change_notice=True)
        host.session.recovered_from_sidecar = False
        order: list[str] = []

        def _compute(**_kwargs: object) -> None:
            order.append("compute")

        original_prepare = host.current.loaded.reminder_middleware.prepare_turn

        def _prepare(**kwargs: object) -> None:
            order.append("prepare")
            original_prepare(**kwargs)

        host._workspace_change_tracker.compute_turn_notice = _compute  # type: ignore[method-assign]
        host.current.loaded.reminder_middleware.prepare_turn = _prepare  # type: ignore[method-assign]

        await retry_and_save(host)

        assert order[:2] == ["compute", "prepare"]

    async def test_retry_pre_executor_interrupt_requeues_drained_notice(self) -> None:
        host = _Host([Message("user", ["request"])])
        tracker = WorkspaceChangeTracker()
        tracker.queue_safety_notice("safety")
        host._workspace_change_tracker = tracker
        host.settings = Settings(workspace_change_notice=True)
        host.session.recovered_from_sidecar = False
        install_loaded_agent(
            host, reminder_middleware=SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
        )

        def _record_pre_run_interrupt() -> None:
            host.current.loaded.bindings.state.was_interrupted = True

        host.current.loaded.bindings.record_pre_run_interrupt = _record_pre_run_interrupt  # type: ignore[attr-defined]
        current = asyncio.current_task()
        assert current is not None
        assert host._turn_state.lease.request_pre_executor_interrupt(current) is True

        await retry_and_save(host)

        tracker.requeue_notice("new boundary")
        assert tracker.take_pending_notice() == "safety\n\nnew boundary"
        assert tracker.take_pending_notice() is None

    async def test_approval_context_skips_nudges_keeps_injections(self) -> None:
        """§2.2: the current-turn approval context excludes synthetic
        ``continue`` nudges (orchestration placeholders, not user input) while
        keeping user-authored injections."""
        marker = Message("assistant", [""])
        marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
        injected = Message("user", ["also check the docs"])
        injected.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
        nudge = Message("user", ["continue"])
        nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
        host = _Host(
            [
                Message("user", ["old request"]),
                Message("assistant", ["done"]),
                marker,
                Message("user", ["current request"]),
                Message("assistant", ["working"]),
                injected,
                nudge,
            ]
        )

        await retry_and_save(host)

        assert host.current.loaded.bindings.user_messages == ["current request", "also check the docs"]

    async def test_synthetic_only_region_falls_back_to_previous_real_opener(self) -> None:
        """§2.2: when the current region holds only a flagged nudge, the
        fallback hands the judge the previous REAL opener, never "continue"."""
        marker = Message("assistant", [""])
        marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
        nudge = Message("user", ["continue"])
        nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
        host = _Host(
            [
                Message("user", ["real question"]),
                Message("assistant", ["answer"]),
                marker,
                nudge,
                Message("assistant", ["resumed work"]),
            ]
        )

        await retry_and_save(host)

        assert host.current.loaded.bindings.user_messages == ["real question"]

    async def test_all_synthetic_history_yields_empty_approval_context(self) -> None:
        """§2.2: a history holding only flagged nudges (no real user input)
        yields an empty approval context — no fabricated "continue"."""
        nudge = Message("user", ["continue"])
        nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
        host = _Host(
            [
                Message("assistant", ["crash-leftover work"]),
                nudge,
            ]
        )

        await retry_and_save(host)

        assert host.current.loaded.bindings.user_messages == []

    async def test_retry_tags_consumed_profile_switch(self) -> None:
        host = _Host([Message("user", ["after switch"])])

        def _consume_switch() -> None:
            host.current.loaded.reminder_middleware.sources.profile_switch.consumed_switch_to = "Explore Agent"

        host.current.loaded.bindings.on_resume = _consume_switch

        await retry_and_save(host)

        assert host._history.tags == [(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY, "Explore Agent")]


async def test_pending_retry_dispatch_strips_trailing_markers_before_task() -> None:
    """Interrupt→resume marker hygiene (§7): the pending-retry dispatch path
    (``RetryCoordinator.start_pending_retry_if_due``) strips trailing markers
    BEFORE creating the retry task, so the resumed task's history carries no
    interior turn marker.  The immediate-dispatch paths are covered end-to-end
    by ``test_retry_turn_number.py`` (exactly one ``_turn=1`` marker after
    interrupt→retry)."""
    from chrys.orchestration.engine.execution import PendingRetry

    order: list[str] = []

    class _OrderedHistory:
        def remove_trailing_markers(self) -> None:
            order.append("remove_trailing_markers")

    class _OrderedHost:
        def __init__(self) -> None:
            self.current = make_current(loaded=SimpleNamespace(), manifest=make_manifest())
            self._current = self.current
            self.session = SimpleNamespace(mark_surface=lambda: None, workspace=None)
            self._turn_state = make_turn_state()
            self._history = _OrderedHistory()
            self._fsm = EngineStateMachine()
            self._fsm.try_transition(Trigger.START)
            self._fsm.try_transition(Trigger.USER_MESSAGE)  # RUNNING
            install_loaded_agent(self, reminder_middleware=SystemReminderMiddleware())
            self.session.shutting_down = False
            self.permits = SimpleNamespace()
            self.permits.session_generation = 0
            self.permits.build_generation = 0

        async def retry_and_save(self, *_args: object, **_kwargs: object) -> None:
            order.append("retry_task")

    host = _OrderedHost()
    host._turn_state.lease.pending_retry = PendingRetry(owner_admission_id=1)

    make_turn_retry(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        fsm=host._fsm,
        history=host._history,
        retry_and_save=host.retry_and_save,
    ).start_pending_retry_if_due()

    task = host._turn_state.lease.run_task
    assert task is not None
    await task
    assert order == ["remove_trailing_markers", "retry_task"]


@pytest.mark.parametrize(
    ("cwd_exists", "shutting_down", "dropped_for_missing_cwd"),
    [(False, False, True), (True, False, False), (False, True, False)],
    ids=["missing-cwd-drop", "dispatch", "shutdown-drop-first"],
)
async def test_pending_retry_reports_only_the_missing_cwd_drop(
    tmp_path: Path, cwd_exists: bool, shutting_down: bool, dropped_for_missing_cwd: bool
) -> None:
    """Only the missing-cwd drop returns the directory, so the runner settles the FSM the pass left RUNNING."""
    from chrys.orchestration.engine.execution import PendingRetry

    work = tmp_path / "work"
    if cwd_exists:
        work.mkdir()
    order: list[str] = []

    class _History:
        def remove_trailing_markers(self) -> None:
            order.append("remove_trailing_markers")

    fsm = EngineStateMachine()
    fsm.try_transition(Trigger.START)
    fsm.try_transition(Trigger.USER_MESSAGE)  # RUNNING
    host = SimpleNamespace(
        current=make_current(loaded=SimpleNamespace(), manifest=make_manifest()),
        session=SimpleNamespace(
            mark_surface=lambda: None, workspace=Workspace.from_cwd(str(work)), shutting_down=shutting_down
        ),
        _turn_state=make_turn_state(),
        permits=SimpleNamespace(session_generation=0, build_generation=0),
    )
    install_loaded_agent(host, reminder_middleware=SystemReminderMiddleware())
    host._turn_state.lease.pending_retry = PendingRetry(owner_admission_id=1)

    async def _retry_and_save(*_args: object, **_kwargs: object) -> None:
        order.append("retry_task")

    result = make_turn_retry(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        fsm=fsm,
        history=_History(),
        retry_and_save=_retry_and_save,
    ).start_pending_retry_if_due()

    assert result == (str(work) if dropped_for_missing_cwd else None)
    assert fsm.state is EngineState.RUNNING
    task = host._turn_state.lease.run_task
    if cwd_exists:
        assert task is not None
        await task
        assert order == ["remove_trailing_markers", "retry_task"]
    else:
        assert task is None
        assert order == []
        assert host._turn_state.lease.pending_retry == PendingRetry()


async def test_later_retry_after_interrupted_finalization_preserves_last_words_and_turn_reminders() -> None:
    """A retry dispatched after an interrupted finalization still observes the
    interrupted run's last words, its dropped-tool manifest and its stable turn reminders."""
    reminder_middleware, last_words = reminder_pair()
    host = _Host(
        [Message("user", ["original request"])],
        reminder_middleware=reminder_middleware,
        dispatch_retries=True,
    )
    host.session.session_id = "s1"
    host.session.turn_number = 1
    host.current.loaded.bindings.state.was_interrupted = True
    host.current.loaded.bindings.backend.service_session_id = "service-session"
    host._fsm.transition(Trigger.START)
    host._fsm.transition(Trigger.USER_MESSAGE)

    # The reminder state a retry must still see is only live inside
    # ``TurnResumePolicy.retry_request`` — capture it there, from the retry's own turn.
    observed: dict[str, object] = {}
    host.current.loaded.bindings.on_resume = lambda: observed.update(
        last_words=last_words.get_last_words(),
        manifest=last_words.get_last_words_manifest(),
        turn_reminders=reminder_middleware._build_reminders(),
        last_words_reminders=last_words.render(),
    )

    reminder_scope = reminder_middleware.create_current_run_scope()
    run_scope = host._turn_state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=host.permits.session_generation,
        build_generation=host.permits.build_generation,
        reminder_scope=reminder_scope,
    )
    host._turn_state.lease.open_injection_admission(run_scope)
    reminder_middleware.prepare_turn(reminder_scope=reminder_scope)
    reminder_middleware.queue_hook_reminders(["stable turn reminder"])
    last_words.set_last_words("[progress before interrupt]")
    dropped = manifest_entry(1, 1, "read_file", "src/a.py")
    last_words.append_manifest([dropped])

    finalization_task = asyncio.create_task(post_run(host))
    host._turn_state.lease.run_task = finalization_task
    await finalization_task

    assert host._turn_state.lease.current_run_scope == run_scope
    assert last_words.get_last_words() == "[progress before interrupt]"

    await on_user_retry(host, UserRetry(text="retry note"))
    assert host._turn_state.lease.run_task is not None
    await host._turn_state.lease.run_task

    assert host.current.loaded.bindings.resume_texts == ["retry note"]
    assert host.current.loaded.bindings.reset_counter_calls == [False]
    assert observed["last_words"] == "[progress before interrupt]"
    assert observed["manifest"] == [dropped.to_state()]
    assert "stable turn reminder" in observed["turn_reminders"]
    assert any("[progress before interrupt]" in reminder for reminder in observed["last_words_reminders"])
    assert host._turn_state.current_input.text == ""
    assert host._turn_state.current_input.contents is None
    assert host._turn_state.current_input.created_at is None


class TestImmediateRetryEligibility:
    """``can_retry`` is the read-only half of retry admission.

    The engine publishes it so a frontend can ask before offering retry, and
    a real retry re-reads the same predicate before installing its task. The
    query must not move the FSM: a frontend asks it while the user is merely
    looking at a Retry button.
    """

    @staticmethod
    def _can_retry(host: _Host) -> bool:
        return make_turn_coordinator(
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
            persistence=host._persistence,
            on_successful_turn=host._on_successful_turn,
            retry_and_save=host.retry_and_save,
            settings_handle=SimpleNamespace(settings=host.settings),
        ).can_retry  # type: ignore[arg-type]

    @staticmethod
    def _run_turn_that_finished(host: _Host) -> None:
        """Leave the FSM RUNNING with a run task that has already completed."""
        host._fsm.try_transition(Trigger.START)
        host._fsm.try_transition(Trigger.USER_MESSAGE)
        task = asyncio.get_running_loop().create_future()
        task.set_result(None)
        host._turn_state.lease.run_task = task  # type: ignore[assignment]

    @pytest.mark.parametrize("state", [EngineState.INTERRUPTED, EngineState.FAILED])
    async def test_a_stopped_turn_can_be_retried(self, state: EngineState) -> None:
        host = _Host([Message("user", ["request"])])
        host._fsm.try_transition(Trigger.START)
        host._fsm.try_transition(
            Trigger.RESTORE_INTERRUPTED if state is EngineState.INTERRUPTED else Trigger.RESTORE_FAILED
        )

        assert self._can_retry(host) is True
        # The query left the FSM exactly where it found it.
        assert host._fsm.state is state

    async def test_a_finished_run_under_a_running_fsm_can_be_retried(self) -> None:
        """Finalization leaves the FSM RUNNING while the run task finishes its save."""
        host = _Host([Message("user", ["request"])])
        self._run_turn_that_finished(host)

        assert self._can_retry(host) is True
        assert host._fsm.state is EngineState.RUNNING

    async def test_a_turn_that_is_still_running_cannot_be_retried(self) -> None:
        host = _Host([Message("user", ["request"])])
        host._fsm.try_transition(Trigger.START)
        host._fsm.try_transition(Trigger.USER_MESSAGE)
        host._turn_state.lease.run_task = asyncio.get_running_loop().create_future()

        assert self._can_retry(host) is False

    async def test_an_idle_engine_cannot_be_retried(self) -> None:
        host = _Host([Message("user", ["request"])])
        host._fsm.try_transition(Trigger.START)

        assert host._fsm.state is EngineState.IDLE
        assert self._can_retry(host) is False

    async def test_a_pending_retry_cannot_start_another_one(self) -> None:
        host = _Host([Message("user", ["request"])])
        self._run_turn_that_finished(host)
        host._fsm.try_transition(Trigger.RETRY_REQUESTED)

        assert host._fsm.state is EngineState.PENDING_RETRY
        assert self._can_retry(host) is False

    async def test_an_unloaded_agent_cannot_be_retried(self) -> None:
        host = _Host([Message("user", ["request"])])
        self._run_turn_that_finished(host)
        install_loaded_agent(host, loaded=None)

        assert self._can_retry(host) is False

    async def test_a_busy_executor_cannot_be_retried(self) -> None:
        host = _Host([Message("user", ["request"])])
        self._run_turn_that_finished(host)
        host.current.loaded.bindings.state.running = True

        assert self._can_retry(host) is False

    async def test_a_shutting_down_session_cannot_be_retried(self) -> None:
        host = _Host([Message("user", ["request"])])
        host._fsm.try_transition(Trigger.START)
        host._fsm.try_transition(Trigger.RESTORE_FAILED)
        host.session.shutting_down = True

        assert self._can_retry(host) is False
