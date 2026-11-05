# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Owner of the execution lifetime: the Turn run task, or the one workflow run, from admission to release.

Multiple preparations and a single promoted task are preserved for turns.
Pending retry remains the existing Turn policy; a Conversation is a borrowed
resource and its lifetime never acquires this lease. A workflow run holds the
lease exclusively through :attr:`ExecutionLease.workflow`: at most one run
globally, admitted only while no turn is live, and every turn admission and
rebuild is refused while it is held. :meth:`ExecutionLease.execution` is the
snapshot frontends read to tell the two apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakSet

from chrys.foundation.events.types import Error, ExecutionChanged
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationTrace
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.run.turn_state import ActiveInjectionTarget
    from chrys.service.agent_middleware.system_reminder import CurrentRunReminderScope


@dataclass
class WorkflowExecution:
    """The one workflow run the lease admits at a time; ``task`` is installed once the run is spawned."""

    run_id: str
    request_id: str
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class RunTaskDrainOutcome:
    """Outcome from observing a run-task chain for a rebuild/session boundary."""

    cancelled: bool = False


@dataclass
class PreAdmissionPreparationEntry:
    """Registered pre-turn preparation that has not reached an admission record."""

    preparation: PreparationTrace
    current_wait: WaitTrace | None = field(default=None, repr=False, compare=False)


@dataclass
class PreAdmissionPreparationTracker:
    """Mutable worker-local pointer to the current pre-admission preparation."""

    current: PreAdmissionPreparationEntry | None = None
    preparation_handed_off: bool = False
    """Whether the current trace moved to a longer-lived owner."""

    @property
    def preparation(self) -> PreparationTrace | None:
        """Return the worker's current preparation trace, if recording is enabled."""
        return self.current.preparation if self.current is not None else None


@dataclass(frozen=True)
class PromptAdmissionRecord:
    """Visible prompt/retry admission while awaited validation is in progress."""

    admission_id: int
    kind: Literal["fresh", "retry"]
    session_generation: int
    build_generation: int
    created_at_monotonic: float
    preparation_trace: PreparationTrace | None = field(default=None, repr=False, compare=False)


@dataclass
class PendingRetry:
    """Retry request queued to run after the current executor call returns."""

    text: str = ""
    created_at: datetime | str | None = None
    session_generation: int = 0
    run_generation: int = 0
    updated_by_admission_id: int | None = None
    owner_admission_id: int | None = None
    dispatch_disabled: bool = False
    preparation_trace: PreparationTrace | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PromptAdmissionScope:
    """Exact prompt/retry admission token."""

    admission_id: int
    kind: Literal["fresh", "retry"]
    session_generation: int
    build_generation: int
    preparation_trace: PreparationTrace | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CurrentRunScope:
    """Orchestration-owned identity for one logical main-agent run."""

    run_generation: int
    session_generation: int
    build_generation: int
    owner_admission_id: int
    reminder_scope: CurrentRunReminderScope


@dataclass(frozen=True)
class CurrentRunInjectionWindow:
    """Injectable executor-pass window for a current run."""

    run_generation: int
    window_generation: int


@dataclass(frozen=True)
class PromptPromotionResult:
    """Result from promoting a fresh prompt admission into a run task."""

    promoted: bool
    conflict: bool = False
    stale: bool = False


@dataclass(frozen=True)
class RetryPromotionResult:
    """Result from promoting a retry admission into a task or pending retry."""

    outcome: Literal["task", "pending_installed", "pending_updated", "stale", "invalid"]
    task: asyncio.Task[None] | None = None


@dataclass
class ExecutionLease:
    """One top-level task truth, its generation fences, admission and save evidence."""

    bus: EventBus | None = field(default=None, repr=False, compare=False)
    _last_execution: ExecutionSnapshot = field(default_factory=lambda: ExecutionSnapshot("idle"), init=False)
    _notification_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False, compare=False)

    next_admission_id: int = 1

    active_admissions: dict[int, PromptAdmissionRecord] = field(default_factory=dict)

    # Active admissions running a prompt shortcut's ``!`command``` placeholders: the
    # execution counts as busy from their start until the admission ends (its turn
    # started, or it was refused), and their trajectory waits close with the admission.
    prompt_command_admissions: set[int] = field(default_factory=set, repr=False, compare=False)
    prompt_command_waits: dict[int, WaitTrace] = field(default_factory=dict, repr=False, compare=False)

    pre_admission_preparations: dict[str, PreAdmissionPreparationEntry] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    active_admissions_idle: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    prompt_admission_closed: bool = False

    prompt_admission_close_owner: str | None = None

    prompt_admission_open: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    _run_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    workflow: WorkflowExecution | None = None

    # Session-family operations in flight (new, clear, fork, restore, delete, rollback), reads and fences
    # included: a workflow run is not admitted under one, or it would run inside a session being replaced.
    session_operations: int = 0

    current_run_scope: CurrentRunScope | None = None

    next_run_generation: int = 1

    conversation_revision: int = 0

    injection_window_generation: int = 0

    injection_admission_open: bool = False

    active_injection_commits: int = 0

    active_injection_commits_idle: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    pending_retry: PendingRetry = field(default_factory=PendingRetry)

    pending_retry_dispatch_disabled_for_session_generation: int | None = None

    _successfully_saved_run_tasks: WeakSet[asyncio.Task[Any]] = field(
        default_factory=WeakSet,
        repr=False,
        compare=False,
    )

    _pre_executor_interrupt_tasks: WeakSet[asyncio.Task[Any]] = field(
        default_factory=WeakSet,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Initialize waitable idle markers for freshly constructed state."""
        if not self.active_admissions:
            self.active_admissions_idle.set()
        if not self.prompt_admission_closed:
            self.prompt_admission_open.set()
        if self.active_injection_commits == 0:
            self.active_injection_commits_idle.set()

    @property
    def run_task(self) -> asyncio.Task[None] | None:
        return self._run_task

    @run_task.setter
    def run_task(self, task: asyncio.Task[None] | None) -> None:
        if self._run_task is task:
            return
        if self._run_task is not None:
            self._run_task.remove_done_callback(self._run_task_finished)
        self._run_task = task
        if task is not None:
            task.add_done_callback(self._run_task_finished)
        self._notify_execution(turn_started=task is not None and not task.done())

    def _run_task_finished(self, task: asyncio.Task[None]) -> None:
        if task is self._run_task:
            self._notify_execution()

    def _notify_execution(self, *, turn_started: bool = False) -> None:
        snapshot = self.execution()
        # A queued retry can replace its predecessor before the lease goes
        # idle. The new owner must be announced even when both are turns.
        if snapshot == self._last_execution and not turn_started:
            return
        self._last_execution = snapshot
        bus = self.bus
        if bus is None:
            return
        previous = self._notification_task

        async def publish() -> None:
            if previous is not None:
                await previous
            await bus.publish(ExecutionChanged(snapshot=snapshot))

        self._notification_task = asyncio.create_task(publish(), name="chrys.execution.changed")

    async def settle_notifications(self) -> None:
        """Drain ordered snapshots before the execution owner releases its resources."""
        while (task := self._notification_task) is not None:
            if task is asyncio.current_task():
                return
            await asyncio.shield(task)
            if self._notification_task is task:
                self._notification_task = None

    def record_current_run_final_save(self) -> None:
        """Record that the current lifecycle task completed its final save."""
        task = asyncio.current_task()
        if task is not None:
            self._successfully_saved_run_tasks.add(task)

    def was_run_task_finally_saved(self, task: asyncio.Task[Any]) -> bool:
        """Return whether *task* completed a successful final session save."""
        return task in self._successfully_saved_run_tasks

    def request_pre_executor_interrupt(self, task: asyncio.Task[Any] | None) -> bool:
        """Bind a pre-executor interrupt to an exact captured run task."""
        if task is None or task.done():
            return False
        self._pre_executor_interrupt_tasks.add(task)
        return True

    def consume_pre_executor_interrupt(self) -> bool:
        """Consume an interrupt only when it belongs to the calling run task."""
        task = asyncio.current_task()
        if task is None or task not in self._pre_executor_interrupt_tasks:
            return False
        self._pre_executor_interrupt_tasks.discard(task)
        return True

    def discard_pre_executor_interrupt(self, task: asyncio.Task[Any] | None) -> None:
        """Drop a task-scoped interrupt at that task's terminal boundary."""
        if task is not None:
            self._pre_executor_interrupt_tasks.discard(task)

    def advance_conversation_revision(self) -> int:
        """Advance the monotonic revision for one fresh or retry lifecycle."""
        self.conversation_revision += 1
        return self.conversation_revision

    def register_pre_admission_preparation(self, entry: PreAdmissionPreparationEntry) -> None:
        """Expose a started preparation until it reaches durable admission state."""
        self.pre_admission_preparations[entry.preparation.operation_id] = entry

    def deregister_pre_admission_preparation(self, entry: PreAdmissionPreparationEntry | None) -> None:
        """Remove *entry* when it is still the registered owner."""
        if entry is None:
            return
        operation_id = entry.preparation.operation_id
        if self.pre_admission_preparations.get(operation_id) is entry:
            del self.pre_admission_preparations[operation_id]

    def clear_pre_admission_preparations(self) -> None:
        """Settle preparations and straddling waits that have not reached admission."""
        entries = tuple(self.pre_admission_preparations.values())
        self.pre_admission_preparations.clear()
        for entry in entries:
            wait = entry.current_wait
            if wait is not None:
                wait.finished_soon(outcome=WaitOutcome.CANCELLED)
                entry.current_wait = None
            entry.preparation.finished_soon(outcome=PreparationOutcome.OWNER_CHANGED)

    def reserve_prompt_admission(
        self,
        *,
        kind: Literal["fresh", "retry"],
        session_generation: int,
        build_generation: int,
        preparation_trace: PreparationTrace | None = None,
    ) -> PromptAdmissionScope | None:
        """Reserve an exact prompt/retry admission unless admission is closed."""
        if self.prompt_admission_closed:
            return None
        admission_id = self.next_admission_id
        self.next_admission_id += 1
        self.active_admissions_idle.clear()
        self.active_admissions[admission_id] = PromptAdmissionRecord(
            admission_id=admission_id,
            kind=kind,
            session_generation=session_generation,
            build_generation=build_generation,
            preparation_trace=preparation_trace,
            created_at_monotonic=time.monotonic(),
        )
        return PromptAdmissionScope(
            admission_id=admission_id,
            kind=kind,
            session_generation=session_generation,
            build_generation=build_generation,
            preparation_trace=preparation_trace,
        )

    def active_admission_count(self) -> int:
        """Return the number of exact active admissions."""
        return len(self.active_admissions)

    def has_active_admission_kind(self, kind: Literal["fresh", "retry"]) -> bool:
        """Return whether an active admission of *kind* is currently preparing."""
        return any(record.kind == kind for record in self.active_admissions.values())

    def release_prompt_admission(self, admission: PromptAdmissionScope) -> bool:
        """Release an exact admission; duplicate releases are no-ops."""
        record = self.active_admissions.get(admission.admission_id)
        if record is None:
            return False
        if (
            record.kind != admission.kind
            or record.session_generation != admission.session_generation
            or record.build_generation != admission.build_generation
        ):
            return False
        del self.active_admissions[admission.admission_id]
        if not self.active_admissions:
            self.active_admissions_idle.set()
        return True

    async def wait_for_active_admissions_idle(self) -> None:
        """Wait until no exact prompt/retry admission is active."""
        while self.active_admissions:
            await self.active_admissions_idle.wait()

    def begin_current_run_scope(
        self,
        *,
        owner_admission_id: int,
        session_generation: int,
        build_generation: int,
        reminder_scope: CurrentRunReminderScope,
    ) -> CurrentRunScope:
        """Create and install a current-run scope."""
        scope = CurrentRunScope(
            run_generation=self.next_run_generation,
            session_generation=session_generation,
            build_generation=build_generation,
            owner_admission_id=owner_admission_id,
            reminder_scope=reminder_scope,
        )
        self.next_run_generation += 1
        self.current_run_scope = scope
        return scope

    def open_injection_admission(self, scope: CurrentRunScope) -> CurrentRunInjectionWindow:
        """Open injection admission for the current executor pass."""
        self.injection_window_generation += 1
        self.injection_admission_open = self.current_run_scope == scope
        return CurrentRunInjectionWindow(
            run_generation=scope.run_generation,
            window_generation=self.injection_window_generation,
        )

    def close_injection_admission(self, scope: CurrentRunScope) -> None:
        """Close injection admission for *scope* if it is current."""
        if self.current_run_scope == scope:
            self.injection_admission_open = False

    def capture_current_injection_window(self, scope: CurrentRunScope) -> CurrentRunInjectionWindow | None:
        """Capture the currently open injection window for *scope*, if any."""
        if self.current_run_scope != scope or not self.injection_admission_open:
            return None
        return CurrentRunInjectionWindow(
            run_generation=scope.run_generation,
            window_generation=self.injection_window_generation,
        )

    def is_injection_admission_current(self, window: CurrentRunInjectionWindow) -> bool:
        """Return whether *window* still admits active-turn injection."""
        scope = self.current_run_scope
        return (
            self.injection_admission_open
            and scope is not None
            and scope.run_generation == window.run_generation
            and self.injection_window_generation == window.window_generation
        )

    def begin_active_injection_commit(self, target: ActiveInjectionTarget) -> bool:
        """Begin a commit guard when the captured active-injection target is current."""
        if target.current_run_scope != self.current_run_scope:
            return False
        if not self.is_injection_admission_current(target.injection_window):
            return False
        self.active_injection_commits += 1
        self.active_injection_commits_idle.clear()
        return True

    def finish_active_injection_commit(self) -> None:
        """Release one active-injection commit guard."""
        if self.active_injection_commits > 0:
            self.active_injection_commits -= 1
        if self.active_injection_commits == 0:
            self.active_injection_commits_idle.set()

    async def wait_for_active_injection_commits_idle(self) -> None:
        """Wait until active-injection commit guards have drained."""
        while self.active_injection_commits > 0:
            await self.active_injection_commits_idle.wait()

    def promote_fresh_admission_to_run(
        self,
        admission: PromptAdmissionScope,
        *,
        reminder_scope: CurrentRunReminderScope,
        make_task: Callable[[CurrentRunScope, CurrentRunInjectionWindow], asyncio.Task[None]],
    ) -> PromptPromotionResult:
        """Promote an exact fresh admission into a visible run task."""
        if not self._admission_matches(admission, expected_kind="fresh"):
            return PromptPromotionResult(promoted=False, stale=True)
        if self.current_run_scope is not None or (self.run_task is not None and not self.run_task.done()):
            return PromptPromotionResult(promoted=False, conflict=True)
        scope = self.begin_current_run_scope(
            owner_admission_id=admission.admission_id,
            session_generation=admission.session_generation,
            build_generation=admission.build_generation,
            reminder_scope=reminder_scope,
        )
        window = self.open_injection_admission(scope)
        self.run_task = make_task(scope, window)
        self.release_prompt_admission(admission)
        return PromptPromotionResult(promoted=True)

    def promote_retry_admission(
        self,
        admission: PromptAdmissionScope,
        *,
        reminder_scope: CurrentRunReminderScope,
        make_task: Callable[[CurrentRunScope, CurrentRunInjectionWindow], asyncio.Task[None]],
        text: str,
        created_at: datetime | str | None,
        pending: bool = False,
    ) -> RetryPromotionResult:
        """Promote an exact retry admission into a task or pending retry."""
        if not self._admission_matches(admission, expected_kind="retry"):
            return RetryPromotionResult(outcome="stale")
        if pending:
            updated = self.upsert_pending_retry_from_admission(admission, text, created_at)
            self.release_prompt_admission(admission)
            return RetryPromotionResult(outcome="pending_updated" if updated else "pending_installed")
        if self.run_task is not None and not self.run_task.done():
            return RetryPromotionResult(outcome="invalid")
        scope = self._retry_scope_for_admission(admission, reminder_scope=reminder_scope)
        window = self.open_injection_admission(scope)
        task = make_task(scope, window)
        self.run_task = task
        self.release_prompt_admission(admission)
        return RetryPromotionResult(outcome="task", task=task)

    def close_prompt_admission_for_rebuild(self, owner: str = "rebuild") -> None:
        """Close fresh prompt/retry admission for a future rebuild fence."""
        self.prompt_admission_closed = True
        self.prompt_admission_close_owner = owner
        self.prompt_admission_open.clear()

    def reopen_prompt_admission_after_rebuild(self, owner: str = "rebuild") -> None:
        """Reopen prompt/retry admission if *owner* owns the close."""
        if self.prompt_admission_close_owner in (None, owner):
            self.prompt_admission_closed = False
            self.prompt_admission_close_owner = None
            self.prompt_admission_open.set()

    async def wait_for_prompt_admission_open(self) -> None:
        """Wait until fresh prompt/retry admission is open."""
        while self.prompt_admission_closed:
            await self.prompt_admission_open.wait()

    def clear_current_run_scope(self, scope: CurrentRunScope) -> None:
        """Clear the current-run scope if *scope* is still installed."""
        if self.current_run_scope == scope:
            self.current_run_scope = None
            self.injection_admission_open = False

    def clear_pending_retry(self, *, outcome: Literal["dropped", "retry_turn"]) -> PendingRetry:
        """Clear pending retry state and terminalize its preparation scope."""
        pending = self.pending_retry
        if pending.preparation_trace is not None:
            pending.preparation_trace.finished_soon(outcome=outcome)
        self.pending_retry = PendingRetry()
        return pending

    def clear_active_admissions(self, *, outcome: str) -> None:
        """Clear active admissions and terminalize their preparation scopes, child waits first."""
        for wait in self.prompt_command_waits.values():
            wait.finished_soon(outcome=WaitOutcome.CANCELLED)
        self.prompt_command_waits.clear()
        for admission in self.active_admissions.values():
            if admission.preparation_trace is not None:
                admission.preparation_trace.finished_soon(outcome=outcome)
        self.active_admissions.clear()
        self.active_admissions_idle.set()
        if self.prompt_command_admissions:
            self.prompt_command_admissions.clear()
            self._notify_execution()

    def begin_prompt_commands(self, admission: PromptAdmissionScope, wait: WaitTrace | None) -> bool:
        """Mark *admission* as running its prompt-shortcut commands; False when it is no longer active.

        The execution is busy from now until :meth:`end_prompt_commands`, called
        when the admission itself ends — after its turn started, so the state
        goes from the commands to the turn without passing through idle. *wait*
        is the commands' trajectory wait: closed here if the admission is
        cleared first (:meth:`clear_active_admissions`).
        """
        if admission.admission_id not in self.active_admissions:
            return False
        self.prompt_command_admissions.add(admission.admission_id)
        if wait is not None:
            self.prompt_command_waits[admission.admission_id] = wait
        self._notify_execution()
        return True

    def take_prompt_commands_wait(self, admission: PromptAdmissionScope) -> WaitTrace | None:
        """Hand the commands' wait back to the step that closes it; None when a clear already closed it."""
        return self.prompt_command_waits.pop(admission.admission_id, None)

    def end_prompt_commands(self, admission: PromptAdmissionScope) -> None:
        """The admission that ran prompt-shortcut commands ended; a no-op for any other."""
        if admission.admission_id in self.prompt_command_admissions:
            self.prompt_command_admissions.discard(admission.admission_id)
            self._notify_execution()

    def upsert_pending_retry_from_admission(
        self,
        admission: PromptAdmissionScope,
        text: str,
        created_at: datetime | str | None,
    ) -> bool:
        """Install or update pending retry for the current run from *admission*."""
        run_generation = self.current_run_scope.run_generation if self.current_run_scope is not None else 0
        existing_same_run = (
            self.pending_retry.owner_admission_id is not None
            and self.pending_retry.session_generation == admission.session_generation
            and self.pending_retry.run_generation == run_generation
        )
        if (
            existing_same_run
            and self.pending_retry.updated_by_admission_id is not None
            and admission.admission_id < self.pending_retry.updated_by_admission_id
        ):
            if admission.preparation_trace is not None:
                admission.preparation_trace.finished_soon(outcome=PreparationOutcome.SUPERSEDED)
            return True
        if existing_same_run and self.pending_retry.preparation_trace is not None:
            self.pending_retry.preparation_trace.finished_soon(outcome=PreparationOutcome.SUPERSEDED)
        if admission.preparation_trace is not None:
            admission.preparation_trace.state_soon("requeued" if existing_same_run else "queued")
        self.pending_retry = PendingRetry(
            text=text,
            created_at=created_at,
            session_generation=admission.session_generation,
            run_generation=run_generation,
            updated_by_admission_id=admission.admission_id,
            owner_admission_id=self.pending_retry.owner_admission_id if existing_same_run else admission.admission_id,
            dispatch_disabled=False,
            preparation_trace=admission.preparation_trace,
        )
        return existing_same_run

    def disable_pending_retry_dispatch_for_session_transition(self, session_generation: int) -> None:
        """Disable pending retry dispatch for an abandoned session generation."""
        self.pending_retry_dispatch_disabled_for_session_generation = session_generation
        if self.pending_retry.session_generation == session_generation:
            self.pending_retry.dispatch_disabled = True

    def invalidate_for_session_transition_pre_shutdown(
        self,
        *,
        old_session_generation: int,
        prompt_admission_owner: str | None = None,
    ) -> None:
        """Invalidate old-session admission/injection/pending-retry state before shutdown."""
        if prompt_admission_owner is not None:
            self.close_prompt_admission_for_rebuild(prompt_admission_owner)
            self.clear_active_admissions(outcome=PreparationOutcome.OWNER_CHANGED)
        self.clear_pre_admission_preparations()
        self.injection_admission_open = False
        self.disable_pending_retry_dispatch_for_session_transition(old_session_generation)

    def reset_after_session_shutdown(
        self,
        *,
        prompt_admission_owner: str | None = None,
    ) -> CurrentRunScope | None:
        """Clear turn-owned state after shutdown has observed the old task."""
        old_scope = self.current_run_scope
        self.run_task = None
        self.current_run_scope = None
        self.injection_admission_open = False
        # ``AgentEngine.shutdown()`` terminalizes pending-retry and admission
        # preparations before it closes the trajectory recorder. This reset
        # runs after shutdown and must never try to emit against the closed writer.
        self.pending_retry = PendingRetry()
        self._pre_executor_interrupt_tasks.clear()
        self.active_admissions.clear()
        self.pre_admission_preparations.clear()
        self.active_admissions_idle.set()
        self.active_injection_commits = 0
        self.active_injection_commits_idle.set()
        if prompt_admission_owner is None:
            self.prompt_admission_closed = False
            self.prompt_admission_close_owner = None
            self.prompt_admission_open.set()
        else:
            self.prompt_admission_closed = True
            self.prompt_admission_close_owner = prompt_admission_owner
            self.prompt_admission_open.clear()
        return old_scope

    def _admission_matches(
        self,
        admission: PromptAdmissionScope,
        *,
        expected_kind: Literal["fresh", "retry"],
    ) -> bool:
        record = self.active_admissions.get(admission.admission_id)
        return (
            record is not None
            and admission.kind == expected_kind
            and record.kind == expected_kind
            and record.session_generation == admission.session_generation
            and record.build_generation == admission.build_generation
        )

    def _retry_scope_for_admission(
        self,
        admission: PromptAdmissionScope,
        *,
        reminder_scope: CurrentRunReminderScope,
    ) -> CurrentRunScope:
        """Return the retry scope for *admission*, rebinding an existing logical retry scope."""
        current = self.current_run_scope
        if (
            current is not None
            and current.session_generation == admission.session_generation
            and current.build_generation == admission.build_generation
        ):
            scope = CurrentRunScope(
                run_generation=current.run_generation,
                session_generation=current.session_generation,
                build_generation=current.build_generation,
                owner_admission_id=admission.admission_id,
                reminder_scope=current.reminder_scope,
            )
            self.current_run_scope = scope
            return scope
        return self.begin_current_run_scope(
            owner_admission_id=admission.admission_id,
            session_generation=admission.session_generation,
            build_generation=admission.build_generation,
            reminder_scope=reminder_scope,
        )

    async def observe_run_task_chain(self, *, propagate_inner_cancel: bool) -> RunTaskDrainOutcome:
        """Observe each installed run task, including tasks installed during finalization."""
        outcome = RunTaskDrainOutcome()
        observed: set[asyncio.Task[None]] = set()
        while True:
            task = self.run_task
            if task is None or task in observed:
                return outcome
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                waiter = asyncio.current_task()
                caller_cancelled = waiter is not None and waiter.cancelling() > 0
                if task.cancelled() and not caller_cancelled:
                    outcome = RunTaskDrainOutcome(cancelled=True)
                    observed.add(task)
                    if propagate_inner_cancel:
                        raise
                else:
                    raise
            else:
                observed.add(task)

    def execution_busy(self) -> bool:
        """Lifecycle-busy predicate: a live turn task (prepare through save), a held workflow run, or an
        admission running a prompt shortcut's commands."""
        return self.turn_busy() or self.workflow is not None or bool(self.prompt_command_admissions)

    def turn_busy(self) -> bool:
        """The turn half of :meth:`execution_busy`: the top-level task includes prepare and save."""
        return self.run_task is not None and not self.run_task.done()

    def release_run_task(self) -> None:
        """Clear the installed task after the caller has drained the task chain."""
        self.run_task = None

    def execution(self) -> ExecutionSnapshot:
        """The frontend view of the lease; a workflow run is always cancellable, a turn while its task lives."""
        if self.workflow is not None:
            return ExecutionSnapshot(
                "workflow", run_id=self.workflow.run_id, cancellable=True, request_id=self.workflow.request_id
            )
        if self.turn_busy():
            return ExecutionSnapshot("turn", cancellable=True)
        if self.prompt_command_admissions:
            # The turn is being prepared: Esc withdraws the message, there is no run to interrupt yet.
            return ExecutionSnapshot("turn", cancellable=False)
        return ExecutionSnapshot("idle")

    def workflow_start_allowed(self) -> bool:
        """Whether the lease is free for workflow admission."""
        return self.workflow_start_refusal() is None

    def workflow_start_refusal(self) -> Literal["workflow_active", "turn_active", "engine_busy"] | None:
        """The first execution boundary that prevents workflow admission."""
        if self.workflow is not None:
            return "workflow_active"
        if self.turn_busy() or self.active_admission_count() > 0 or self.pre_admission_preparations:
            return "turn_active"
        if self.prompt_admission_closed or self.session_operations:
            return "engine_busy"
        return None

    async def refuse_while_workflow_active(self, bus: EventBus, session_id: str | None) -> bool:
        """Answer a turn/retry request while a workflow owns the lease."""
        if self.workflow is None:
            return False
        await bus.publish(
            Error(
                code="workflow_active",
                message="A workflow run is active. Cancel the workflow run first.",
                session_id=session_id,
            )
        )
        return True

    @contextlib.contextmanager
    def session_operation(self) -> Generator[None]:
        """Hold the lease against workflow admission for one session-family operation, its awaits included."""
        self.session_operations += 1
        try:
            yield
        finally:
            self.session_operations -= 1

    def begin_workflow(self, run_id: str, request_id: str) -> WorkflowExecution:
        """Reserve the lease for one workflow run; the caller checked :meth:`workflow_start_allowed` first."""
        if not self.workflow_start_allowed():
            raise RuntimeError("The execution lease is not free for a workflow run")
        execution = WorkflowExecution(run_id=run_id, request_id=request_id)
        self.workflow = execution
        self._notify_execution()
        return execution

    def end_workflow(self, execution: WorkflowExecution) -> None:
        """Release the lease held by *execution*; a stale release (another run installed since) is ignored."""
        if self.workflow is execution:
            self.workflow = None
            self._notify_execution()
