# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hook orchestrator — single entry point for every wire-up site.

The manager owns:

* The parsed :class:`HooksFile` (matched + filtered per dispatch).
* The :class:`HookRunner` (subprocess execution).
* The :class:`Outbox` (durable jobs).
* A set of in-flight non-detached tasks split into "this turn" /
  "this session" / "background" so drains can target only awaited work.

Callers fire events through :meth:`fire`; the return value is a
:class:`HookDecision` that aggregates the event-supported outputs from
blocking hooks.  Pure observer events can ignore the return value.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.hook_executions import HookExecutionView, HookWaitPoint
from chrys.foundation.models.hook_runs import HookRunApplied, HookRunEffect
from chrys.foundation.tool_execution_stamp import encode_json_like
from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.hooks.confirmation import HookConfirmationResult
from chrys.service.hooks.events import GATED_EVENTS, HookEvent
from chrys.service.hooks.matcher import matches
from chrys.service.hooks.operation import args_key
from chrys.service.hooks.outbox import Outbox, OutboxJob
from chrys.service.hooks.run_log import indented_json
from chrys.service.hooks.runner import HookResult, HookRunner
from chrys.service.hooks.schema import HookConfig, HookDecision, HooksFile, MergedHooksFile
from chrys.service.trajectory.hooks import HookOperationTrace, HookOutcome, TrajectoryContextProvider

if TYPE_CHECKING:
    from chrys.foundation.models.hook_confirmations import HookConfirmationOutcome
    from chrys.foundation.models.invocations import InvocationOrigin
    from chrys.service.hooks.confirmation import HookConfirmationService
    from chrys.service.hooks.operation import HookOperation
    from chrys.service.hooks.run_log import HookRunLog

logger = logging.getLogger(__name__)
_ARGS_OVERRIDE_EVENTS = frozenset({HookEvent.BEFORE_TOOL_CALL})
_SYSTEM_REMINDER_EVENTS = frozenset({HookEvent.USER_PROMPT_SUBMIT})
_EXTRA_CONTEXT_EVENTS = frozenset({HookEvent.AFTER_TOOL_CALL, HookEvent.TOOL_ERROR})
MAX_CONFIRMED_RECHECKS = 3
"""Passes after the first that one dispatch runs because an argument changed after the user approved an ask."""
ASK_REASON_MAX_CHARS = 1000
"""The longest ``reason`` an ``ask`` shows the user and the model; longer ones are cut."""


# ---------------------------------------------------------------------------
# Tracked in-flight task
# ---------------------------------------------------------------------------


class _Tracked:
    """Bookkeeping wrapper around an in-flight hook task.

    ``scope`` lets :meth:`HookManager.drain_turn` filter to just turn-
    scoped tasks without touching background or session-scoped ones.
    ``run`` is the run, queued before the task exists, and ``trace`` its span,
    opened before: for a task cancelled before its first step, whose own
    handlers never ran, the drain or the reap callback ends the one and closes
    the other.
    """

    __slots__ = ("hook", "job", "operation_id", "run", "scope", "task", "trace")

    def __init__(
        self,
        *,
        hook: HookConfig,
        task: asyncio.Task[HookResult],
        scope: str,
        job: OutboxJob | None,
        operation_id: str | None,
        run: _Run,
        trace: HookOperationTrace | None = None,
    ) -> None:
        self.hook = hook
        self.task = task
        self.scope = scope  # "turn" | "session" | "background"
        self.job = job
        self.operation_id = operation_id
        self.run = run
        self.trace = trace


class _Run:
    """One run: its entry in the runs in flight, its entry in the run log, and how it was started.

    The manager moves it through three steps, each changing both entries
    together and saying what changed: ``_queue_run``, ``_take_slot`` and
    ``_finish_run``. The last is called the moment the run's outcome is known,
    before anything else is awaited, and takes effect once.
    """

    __slots__ = (
        "key",
        "output",
        "payload_text",
        "recorded",
        "retry",
        "run_id",
        "sub_agent",
        "tool_call_id",
        "tool_name",
    )

    def __init__(
        self,
        run_id: str,
        *,
        recorded: bool = False,
        payload_text: str | None = None,
        tool_name: str = "",
        tool_call_id: str = "",
        sub_agent: str = "",
        retry: bool = False,
    ) -> None:
        self.run_id = run_id
        self.key: int | None = None
        """Its entry in the runs in flight; None until queued, and for a detached run."""
        self.recorded = recorded
        """Whether the run log records it."""
        self.payload_text = payload_text
        """The payload file's text, serialized once when the run is made; None when the
        runner serializes it, and once the run ended (it is the largest thing a run holds)."""
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.sub_agent = sub_agent
        self.retry = retry
        self.output: HookResult | None = None
        """The result the runner fills while the hook runs; None until it starts, and
        once the run ended. A cancelled run keeps what the hook printed from it."""


@dataclass(frozen=True, slots=True)
class _Changed:
    """What a step of a run changed: the runs in flight, the run log, or both."""

    executions: bool = False
    runs: bool = False

    def __or__(self, other: _Changed) -> _Changed:
        return _Changed(executions=self.executions or other.executions, runs=self.runs or other.runs)


@dataclass(frozen=True, slots=True)
class _Dispatch:
    """What every blocking run of one ``fire`` shares."""

    event: HookEvent
    scope: str
    target_operation_id: str | None
    trajectory_context: TrajectoryContext | None
    origin: InvocationOrigin | None
    operation: HookOperation | None
    session_drain_generation: int


@dataclass(frozen=True, slots=True)
class _BlockingStep:
    """What one blocking run asked for beyond its decision: an ``ask`` (its reason) and whether it changed the arguments."""

    run_id: str
    ask: str | None = None
    args_changed: bool = False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class HookManager:
    """Async orchestrator for all hook activity.

    Construct once per session when a hook config file exists.  The
    manager is dispatch-no-op when the loaded :class:`HooksFile` carries
    no hooks, but construction still creates runtime directories for the
    durable outbox.
    """

    def __init__(
        self,
        *,
        file: HooksFile | MergedHooksFile,
        hooks_dir: Path,
    ) -> None:
        if isinstance(file, HooksFile):
            file = MergedHooksFile.from_single(file)
        self._file = file
        self._hook_sources = _hook_sources(file)
        self._hook_source_paths = _hook_source_paths(file)
        self._hooks_by_id = _hooks_by_id(file.hooks)
        self._hooks_by_recovery_key = _hooks_by_recovery_key(file.hooks, self._hook_sources, self._hook_source_paths)
        self._hooks_dir = hooks_dir
        self._runner = HookRunner(hooks_dir=hooks_dir)
        self._outbox = Outbox(hooks_dir / "outbox")
        # Cap concurrency across non-detached subprocesses.
        self._sem = asyncio.Semaphore(max(1, file.settings.max_parallel_hooks))
        # Open non-detached tasks by scope.  Detached hooks are handed
        # off to a worker process and are not tracked here.
        self._inflight: list[_Tracked] = []
        self._active_turn_drain_operation_ids: tuple[str, ...] = ()
        # Listeners registered by middleware for "out-of-band" results
        # (e.g. ``after_tool_call`` decisions that arrive after the
        # tool already returned to the model).  Indexed by event for
        # cheap dispatch.
        self._closed = False
        self._session_drain_generation = 0
        # Where hook runs are recorded when no model run binds an ambient
        # trajectory scope (session / turn lifecycle hooks); the engine
        # installs its recorder's resolver.
        self.trajectory_context_provider: TrajectoryContextProvider | None = None
        # Runs in flight, for the TUI. Each change is reported through
        # ``on_change`` right after it is made, on the task that made it (the
        # firing task registers a run, the run's own task does the rest), so
        # the last snapshot a subscriber stores is the current one.
        self._executions: dict[int, HookExecutionView] = {}
        self._execution_keys = itertools.count()
        # Whether the last snapshot handed to ``on_change`` listed runs:
        # closing clears the set behind its runs' backs, and reports the empty
        # set only when a subscriber may still show some.
        self._reported_runs = False
        # The single observer, installed by the session that owns this manager
        # (``wire_hook_reporting``); a second assignment replaces the first.
        self.on_change: Callable[[HookManager], Awaitable[None]] | None = None
        # Where every run is recorded with its input and output (the TUI's
        # Hooks panel), and who is told after each write; none unless the
        # engine records runs (``record_runs``).
        self._run_log: HookRunLog | None = None
        self._run_log_session_id = ""
        self._on_runs_change: Callable[[], Awaitable[None]] | None = None
        # Where an ``ask`` waits for the user: the engine's service, attached by
        # the factory that builds its managers. None: nobody can be asked.
        self.confirmations: HookConfirmationService | None = None
        # The operations whose blocking hooks run (or whose ask waits) now: a session
        # drain or close ends them, since the hooks that would vouch for them stop.
        self._checking: set[HookOperation] = set()

    @property
    def hooks_dir(self) -> Path:
        return self._hooks_dir

    @property
    def file(self) -> MergedHooksFile:
        return self._file

    def sources(self) -> list[str]:
        """List of source paths included in the manager's config.

        Empty when no source had a path (e.g. a synthetic empty
        config). Populated by :class:`MergedHooksFile` with both
        project and global paths in load order. Used for log lines
        and the future ``/hooks`` TUI screen.
        """
        return list(self._file.sources)

    def has_hooks_for(self, event: HookEvent) -> bool:
        """Cheap fast-path check.

        Wire-up sites guard with this so we don't even build payloads
        when no hooks would match.  Saves a dict allocation per tool
        call when hooks are unused — and "no hooks at all" is the
        common case.
        """
        return any(h.enabled and h.event == event for h in self._file.hooks)

    def executions(self) -> tuple[HookExecutionView, ...]:
        """The runs in flight now (queued or running, waited on or not), oldest first."""
        return tuple(self._executions.values())

    def record_runs(
        self, log: HookRunLog, *, session_id: str, on_change: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """Record every run of this manager in *log* under *session_id*, calling *on_change* after each write.

        A run that ends after the manager closed is recorded too.
        """
        self._run_log = log
        self._run_log_session_id = session_id
        self._on_runs_change = on_change

    # ── dispatch ─────────────────────────────────────────────────────

    async def fire(
        self,
        event: HookEvent,
        payload: dict[str, Any],
        *,
        scope: str = "turn",
        target_operation_id: str | None = None,
        trajectory_context: TrajectoryContext | None = None,
        origin: InvocationOrigin | None = None,
        operation: HookOperation | None = None,
    ) -> HookDecision:
        """Fire every matching hook for *event*.

        Args:
            event: The lifecycle point.
            payload: Event-specific data; the manager adds ``event``,
                ``schema``, and ``timestamp`` envelope fields.
            scope: ``"turn"`` (drained at end of turn), ``"session"``
                (drained at shutdown), or ``"detached"`` (never
                awaited).  Caller picks the appropriate scope for the
                event.
            target_operation_id: The trajectory operation the hooks act
                on (the tool operation for tool events), recorded on
                every ``hook.operation.*`` this dispatch emits.
            trajectory_context: Explicit recording owner for blocking hooks.
                Non-blocking turn-scoped hooks deliberately retain the normal
                provider context so response-fence membership remains resolvable.
            origin: The invocation that fired the event, when a sub-agent
                did; the run log names that sub-agent. None is the main agent.
            operation: For a gating event, the operation the hooks check
                (use :func:`check_operation`): an ``ask`` asks about it, an
                argument change makes its new version, and a check through to
                the end marks its version checked. Without one, an ``ask`` is
                refused as if nobody could be asked. An ``ask`` waits on
                :attr:`confirmations`.

        Returns:
            Aggregated :class:`HookDecision`.  For events with no
            matching hooks, an empty (allow / no-op) decision.
        """
        session_drain_generation = self._session_drain_generation
        candidates = [h for h in self._file.hooks if h.event == event and h.enabled]
        if not candidates:
            return HookDecision()
        if self._closed:
            # Hooks are configured for this event, and none can run: nothing vouches for the operation.
            if operation is not None and operation.invalidate("hooks_closed", closing_hooks=True):
                return HookDecision(invalidated=True)
            return HookDecision()
        envelope = _build_envelope(event, payload)
        if event in GATED_EVENTS:
            envelope["confirmation_available"] = (
                operation is not None and self.confirmations is not None and self.confirmations.available()
            )

        # Two passes: blocking hooks run sequentially so the first
        # ``block`` short-circuits; non-blocking hooks then spawn in
        # parallel.  Sequential blocking is intentional — users
        # ordering hooks in the file should be able to reason about
        # which fires first.  Blocking ``modify`` decisions update the
        # envelope immediately so later blocking hooks can validate the
        # rewritten arguments.
        blocking = [hook for hook in candidates if hook.execution.mode == "blocking"]
        nonblocking = [hook for hook in candidates if hook.execution.mode != "blocking"]
        dispatch = _Dispatch(
            event=event,
            scope=scope,
            target_operation_id=target_operation_id,
            trajectory_context=trajectory_context,
            origin=origin,
            operation=operation,
            session_drain_generation=session_drain_generation,
        )
        if operation is not None:
            self._checking.add(operation)
        try:
            return await self._dispatch(blocking, nonblocking, envelope, dispatch)
        finally:
            if operation is not None:
                self._checking.discard(operation)

    async def _dispatch(
        self,
        blocking: list[HookConfig],
        nonblocking: list[HookConfig],
        envelope: dict[str, Any],
        dispatch: _Dispatch,
    ) -> HookDecision:
        """The blocking passes, then the non-blocking starts; the operation, if any, tracked throughout."""
        operation, event = dispatch.operation, dispatch.event
        session_drain_generation = dispatch.session_drain_generation
        decision = await self._run_blocking_passes(blocking, envelope, dispatch)
        if decision.blocked or decision.invalidated:
            # Nothing else executes on this dispatch: no non-blocking hook starts.
            return decision
        if self._closed or self._session_drain_generation != session_drain_generation:
            if operation is not None and operation.invalidate("hooks_closed", closing_hooks=True):
                decision.invalidated = True
            return decision

        if event in GATED_EVENTS:
            # An ``async`` or ``fire_and_forget`` run returns after the operation went on: it cannot ask.
            envelope["confirmation_available"] = False
        for hook in nonblocking:
            if operation is not None and not operation.pending:
                # Withdrawn meanwhile (while an earlier one started, say): no later one starts.
                break
            if not matches(hook, envelope):
                continue
            try:
                await self._spawn_nonblocking(
                    hook,
                    envelope,
                    scope=dispatch.scope,
                    session_drain_generation=session_drain_generation,
                    target_operation_id=dispatch.target_operation_id,
                    origin=dispatch.origin,
                    operation=operation,
                )
            except Exception as exc:
                _log_nonblocking_spawn_failure(hook, exc)
        # Checked again once every start was awaited: a drain meanwhile ends the operation too.
        drained = self._closed or self._session_drain_generation != session_drain_generation
        if drained and operation is not None and operation.invalidate("hooks_closed", closing_hooks=True):
            decision.invalidated = True
        if operation is not None and not operation.pending:
            decision.invalidated = True
        return decision

    async def _run_blocking_passes(
        self, blocking: list[HookConfig], envelope: dict[str, Any], dispatch: _Dispatch
    ) -> HookDecision:
        """Run the blocking hooks in order, as many passes as the operation's changes need; the last pass's decision.

        One pass runs every matching hook once. Once the user approved an ask
        in this dispatch, an argument change ends the pass: what was approved
        is no longer what would run, so the next pass checks the new version
        from the first hook, asking again where a hook asks. A pass starts
        from an empty decision on the envelope as the last one left it.
        Before any approval, a change only goes on to the later hooks, as an
        override always did.
        """
        operation = dispatch.operation
        approved = False
        rechecks = 0
        changed_by = ""
        seen = {args_key(_tool_args(envelope))}
        while True:
            decision = HookDecision()
            recheck = False
            for hook in blocking:
                if self._closed or self._session_drain_generation != dispatch.session_drain_generation:
                    if operation is not None and operation.invalidate("hooks_closed", closing_hooks=True):
                        decision.invalidated = True
                    return decision
                if operation is not None and not operation.pending:
                    # Withdrawn meanwhile (an interrupt, the user's answer going stale): no later hook runs for it.
                    decision.invalidated = True
                    return decision
                if not matches(hook, envelope):
                    continue
                step = await self._run_one_blocking(hook, envelope, decision, dispatch)
                if decision.blocked or decision.invalidated:
                    return decision
                if step.ask is not None:
                    if not await self._confirm(hook, step, decision, dispatch):
                        return decision
                    approved = True
                if step.args_changed and operation is not None:
                    changed = operation.replace_arguments(_tool_args(envelope))
                    if changed and approved:
                        recheck = True
                        changed_by = step.run_id
                        break
            if not recheck:
                return decision
            rechecks += 1
            key = args_key(_tool_args(envelope))
            if rechecks > MAX_CONFIRMED_RECHECKS or key is None or key in seen:
                if self._run_log is not None:
                    await self._report(_Changed(runs=self._run_log.reject_call(changed_by)))
                if rechecks > MAX_CONFIRMED_RECHECKS:
                    why = f"stopped after {MAX_CONFIRMED_RECHECKS} rechecks"
                elif key is None:
                    why = "the new arguments cannot be compared with the earlier ones"
                else:
                    why = "they changed back to arguments already checked, which would repeat forever"
                decision.blocked = True
                decision.block_reason = f"hooks kept changing the arguments after the user confirmed them; {why}"
                return decision
            seen.add(key)

    async def _confirm(
        self, hook: HookConfig, step: _BlockingStep, decision: HookDecision, dispatch: _Dispatch
    ) -> bool:
        """Ask the user about *hook*'s ``ask``; True when they approved, else *decision* says why not."""
        operation, confirmations = dispatch.operation, self.confirmations
        if operation is None or confirmations is None:
            result = HookConfirmationResult("unavailable", "cannot_ask")
        else:
            try:
                result = await confirmations.ask(
                    operation,
                    run_id=step.run_id,
                    hook_id=hook.id,
                    hook_source=self._hook_sources.get(id(hook), ""),
                    reason=step.ask or "",
                    trajectory_context=dispatch.trajectory_context,
                    trajectory_provider=self.trajectory_context_provider,
                )
            except asyncio.CancelledError:
                await self._report(self._record_confirmation(step.run_id, "interrupted"))
                raise
        await self._report(self._record_confirmation(step.run_id, result.outcome))
        if result.outcome == "approved":
            return True
        if result.outcome == "invalidated":
            decision.invalidated = True
            return False
        decision.blocked = True
        decision.confirmation = result.outcome
        timeout = confirmations.timeout_seconds if confirmations is not None else None
        tool_call = hook.event == HookEvent.BEFORE_TOOL_CALL
        decision.block_reason = _ask_refusal(hook.id, step.ask or "", result, timeout, tool_call=tool_call)
        return False

    def _record_confirmation(self, run_id: str, outcome: HookConfirmationOutcome) -> _Changed:
        """Set what came of the question on the run that asked; nothing when the run log no longer keeps it."""
        if self._run_log is None:
            return _Changed()
        return _Changed(runs=self._run_log.confirm(run_id, outcome))

    async def _run_one_blocking(
        self, hook: HookConfig, envelope: dict[str, Any], decision: HookDecision, dispatch: _Dispatch
    ) -> _BlockingStep:
        """Run one blocking *hook*, fold its result into *decision*, and end its run; what else it asked for."""
        run = self._new_run(envelope, origin=dispatch.origin)
        trace = self._open_trace(
            dispatch.target_operation_id, context=dispatch.trajectory_context, operation_id=run.run_id
        )
        try:
            # Inside the block that closes the span: the start marker
            # awaits its write ack, and an interrupt landing there would
            # otherwise leave the hook operation open forever.
            if trace is not None:
                await trace.started(
                    hook_id=hook.id,
                    hook_event=str(dispatch.event),
                    execution_mode=hook.execution.mode,
                    detach=hook.execution.detach,
                    delivery=hook.execution.delivery,
                    scope=dispatch.scope,
                    drain_scope=dispatch.scope,
                )
            if self._closed or self._session_drain_generation != dispatch.session_drain_generation:
                if trace is not None:
                    trace.finished_soon(outcome=HookOutcome.ABANDONED)
                if dispatch.operation is not None and dispatch.operation.invalidate("hooks_closed", closing_hooks=True):
                    decision.invalidated = True
                return _BlockingStep(run_id=run.run_id)
            result = await self._run_blocking(
                hook,
                envelope,
                # Interrupt observers run on a task nobody waits on.
                waited_at="none" if dispatch.scope == "detached" else "call",
                run=run,
                operation=dispatch.operation,
            )
        except asyncio.CancelledError:
            if trace is not None:
                trace.finished_soon(outcome=HookOutcome.CANCELLED)
            await self._report(self._finish_run(run, outcome=HookOutcome.CANCELLED))
            raise
        except Exception as exc:
            logger.exception("Blocking hook %r failed before producing a result", hook.id)
            result = HookResult(hook_id=hook.id, exit_code=None, stderr=str(exc))
        if result is None:
            # The operation was withdrawn while the run queued: its script never started.
            if trace is not None:
                trace.finished_soon(outcome=HookOutcome.ABANDONED)
            await self._report(self._finish_run(run, outcome=HookOutcome.CANCELLED))
            decision.invalidated = True
            return _BlockingStep(run_id=run.run_id)
        event = dispatch.event
        blocked_before = decision.blocked
        override_before = decision.args_override
        reminders_before = len(decision.system_reminders)
        contexts_before = len(decision.extra_context)
        try:
            ask = _merge_blocking_result(decision, hook, result, event=event)
            # Compared as JSON, what the tool gets: a hook returning the arguments as they were
            # changed nothing, while 1 turned to true did, though Python finds them equal.
            args_changed = False
            if not decision.blocked and decision.args_override is not override_before:
                args_before = args_key(_tool_args(envelope))
                _apply_args_override_to_envelope(envelope, decision.args_override or {})
                # Either side too deep to write as JSON: counted as changed, since the hook
                # returned an override.
                args_changed = args_before is None or args_before != args_key(_tool_args(envelope))
            blocked = decision.blocked and not blocked_before
            try:
                arguments_modified = decision.args_override != override_before
            except RecursionError:
                # Too deep to compare: the hook returned an override, all that can be told.
                arguments_modified = decision.args_override is not override_before
            effect, applied = _blocking_effect(
                event,
                result,
                decision,
                blocked=blocked,
                asked=ask is not None,
                args_changed=args_changed,
                args_after=_tool_args(envelope),
                reminders_before=reminders_before,
                contexts_before=contexts_before,
            )
        except Exception:
            # The hook's result, written by the hook, could not be taken: its run ended all
            # the same, and is ended and reported before the error goes on to the caller.
            outcome = _hook_outcome(result, blocked=False)
            await self._report_end(
                self._finish_run(run, outcome=outcome, result=result),
                trace,
                outcome=outcome,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            )
            raise
        await self._report_end(
            self._finish_run(
                run, outcome=_hook_outcome(result, blocked=False), result=result, effect=effect, applied=applied
            ),
            trace,
            outcome=_hook_outcome(result, blocked=blocked, asked=ask is not None),
            arguments_modified=arguments_modified,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
        )
        return _BlockingStep(run_id=run.run_id, ask=ask, args_changed=args_changed)

    # ── runs ─────────────────────────────────────────────────────────

    def _new_run(self, payload: dict[str, Any], *, origin: InvocationOrigin | None = None, retry: bool = False) -> _Run:
        """A run's id, and, when runs are recorded, the payload it will be handed as it is now.

        Serialized here, not kept by reference: a later blocking hook's
        argument override rewrites the same dict in place. Never raises: a
        payload that cannot be serialized is left to the runner, whose own
        attempt fails the run, as is one that is no object, which a pending
        outbox job read back can hold.
        """
        run_id = new_analytics_id()
        if self._run_log is None:
            return _Run(run_id)
        fields = payload if isinstance(payload, dict) else {}
        tool = fields.get("tool")
        tool_name = tool_call_id = ""
        if isinstance(tool, dict):
            tool_name = _text_field(tool, "name")
            tool_call_id = _text_field(tool, "call_id")
        # Every event a sub-agent fires carries its profile name; only some carry its display name.
        sub_agent = _text_field(fields, "profile") if origin is not None and origin.kind == "sub_agent" else ""
        try:
            payload_text: str | None = encode_json_like(payload)
        except TypeError, ValueError, RecursionError:
            payload_text = None
        return _Run(
            run_id,
            recorded=True,
            payload_text=payload_text,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            sub_agent=sub_agent,
            retry=retry,
        )

    def _register_run(self, hook: HookConfig, run: _Run) -> _Changed:
        """Add *run* to the run log, queued for a slot."""
        if self._run_log is None or not run.recorded:
            return _Changed()
        self._run_log.register(
            run_id=run.run_id,
            session_id=self._run_log_session_id,
            hook=hook,
            source=self._hook_sources.get(id(hook), ""),
            source_path=self._hook_source_paths.get(id(hook), ""),
            payload_text=run.payload_text or "",
            tool_name=run.tool_name,
            tool_call_id=run.tool_call_id,
            sub_agent=run.sub_agent,
            retry=run.retry,
        )
        return _Changed(runs=True)

    def _queue_run(self, hook: HookConfig, run: _Run, *, waited_at: HookWaitPoint) -> _Changed:
        """Add *run* to the runs in flight and to the run log, queued for a slot."""
        run.key = next(self._execution_keys)
        self._executions[run.key] = HookExecutionView(
            hook_id=hook.id, waited_at=waited_at, queued=True, started_at=time.monotonic(), run_id=run.run_id
        )
        return _Changed(executions=True) | self._register_run(hook, run)

    def _take_slot(self, run: _Run) -> _Changed:
        """Mark *run* as holding its slot.

        Not in the runs in flight once the manager closed and cleared them,
        and then reported no more there; the run log records it still.
        """
        view = self._executions.get(run.key) if run.key is not None else None
        if view is not None and run.key is not None:
            self._executions[run.key] = replace(view, queued=False)
        return _Changed(
            executions=view is not None, runs=self._run_log is not None and self._run_log.mark_running(run.run_id)
        )

    def _finish_run(
        self,
        run: _Run,
        *,
        outcome: str,
        result: HookResult | None = None,
        effect: HookRunEffect | None = None,
        applied: HookRunApplied | None = None,
        log_path: str = "",
        error: str = "",
    ) -> _Changed:
        """End *run*: drop it from the runs in flight, and end it in the run log.

        *outcome* is how the hook's process ended, as its span records it but
        for a block: the run log keeps that as the *effect* of a run that
        succeeded. Takes effect once; nothing changes when called again.
        Without *result*, the run log keeps what the hook printed before it
        was cancelled, or *error* as its stderr.
        """
        run.payload_text = None
        output, run.output = run.output, None
        removed = run.key is not None and self._executions.pop(run.key, None) is not None
        if self._run_log is None or not run.recorded:
            return _Changed(executions=removed)
        if result is None:
            ended = self._run_log.finish(
                run.run_id,
                outcome=outcome,
                stdout=output.stdout if output is not None else "",
                stderr=error or (output.stderr if output is not None else ""),
                stdout_dropped=output.stdout_dropped if output is not None else 0,
                stderr_dropped=output.stderr_dropped if output is not None else 0,
                log_path=log_path,
            )
        else:
            ended = self._run_log.finish(
                run.run_id,
                outcome=outcome,
                exit_code=result.exit_code,
                effect=effect,
                applied=applied,
                result_file=result.result_file,
                # An empty object is still what the hook wrote.
                decision=indented_json(result.decision) if result.result_file == "ok" else "",
                stdout=result.stdout,
                stderr=result.stderr,
                stdout_dropped=result.stdout_dropped,
                stderr_dropped=result.stderr_dropped,
                log_path=log_path,
            )
        return _Changed(executions=removed, runs=ended)

    async def _report(self, changed: _Changed) -> None:
        if changed.executions or changed.runs:
            await self._notify_change(executions=changed.executions, runs=changed.runs)

    async def _report_end(
        self,
        changed: _Changed,
        trace: HookOperationTrace | None,
        *,
        outcome: str,
        arguments_modified: bool = False,
        exit_code: int | None = None,
        timed_out: bool = False,
    ) -> None:
        """Report what a run's end changed, then close its span with *outcome*.

        In that order: the panel and the indicator never wait on the
        trajectory's write ack. A cancel landing in the report still closes the span.
        """
        try:
            await self._report(changed)
        except asyncio.CancelledError:
            if trace is not None:
                trace.finished_soon(outcome=outcome)
            raise
        if trace is not None:
            await trace.finished(
                outcome=outcome, arguments_modified=arguments_modified, exit_code=exit_code, timed_out=timed_out
            )

    async def _notify_change(self, *, executions: bool = True, runs: bool = True) -> None:
        """Report the runs in flight (*executions*), then that the run log changed (*runs*).

        Separate conditions: a run ending after the manager closed changes the
        log but publishes no snapshot. A failing subscriber never touches the hook.
        """
        if executions and self.on_change is not None:
            self._reported_runs = bool(self._executions)
            try:
                await self.on_change(self)
            except Exception:
                logger.exception("Hook execution change notification failed")
        if runs and self._run_log is not None and self._on_runs_change is not None:
            try:
                await self._on_runs_change()
            except Exception:
                logger.exception("Hook run log change notification failed")

    def _open_trace(
        self,
        target_operation_id: str | None,
        *,
        context: TrajectoryContext | None = None,
        operation_id: str | None = None,
    ) -> HookOperationTrace | None:
        return HookOperationTrace.open(
            target_operation_id=target_operation_id,
            context=context,
            provider=self.trajectory_context_provider,
            operation_id=operation_id,
        )

    # ── drains ───────────────────────────────────────────────────────

    @property
    def active_turn_drain_operation_ids(self) -> tuple[str, ...]:
        """Hook operations captured by the latest turn drain fence."""
        return self._active_turn_drain_operation_ids

    async def drain_turn(self) -> tuple[str, ...]:
        """Await every ``async`` hook spawned in this turn.

        ``fire_and_forget`` tasks are observed for logging/outbox updates
        but skipped by drains.  ``detached`` tasks are already independent
        of chrys.  Errors are logged at WARN, not raised, so a flaky hook
        cannot fail the turn that just succeeded.
        """
        operation_ids = tuple(
            tracked.operation_id
            for tracked in self._inflight
            if tracked.scope == "turn" and not tracked.task.done() and tracked.operation_id is not None
        )
        self._active_turn_drain_operation_ids = operation_ids
        await self._drain(scopes={"turn"}, timeout=None)
        return operation_ids

    async def drain_session(self, *, close: bool = True) -> None:
        """Await every still-running ``async`` hook (turn + session scopes) up to
        ``shutdown_grace_seconds``.

        On grace expiry surviving tasks are cancelled (their
        subprocesses get killed by ``managed_subprocess``).  Detached
        subprocesses are never touched. Tasks outside the drained scopes keep
        running and reporting results. The recorder settles unfinished traces
        when its log closes; replacing this manager does not end observation.

        ``close=False`` waits the same way but leaves the manager open:
        used when the live session ends before the engine shuts down
        (deleting the active session), so ``session_end`` hooks finish
        while the session files still exist and later events still fire.
        """
        self._session_drain_generation += 1
        # The checks in flight belong to the session being drained: what they would vouch for ends with
        # it, and an ask waiting for the user ends at once instead of holding the drain.
        for operation in list(self._checking):
            operation.invalidate("hooks_closed", closing_hooks=True)
        await self._drain(
            scopes={"turn", "session"},
            timeout=self._file.settings.shutdown_grace_seconds,
        )
        if close:
            self._closed = True
            # The last word on this manager's runs: a manager replaced in the
            # same session (working directory change) gets no idle or
            # session-switch event that would clear what it reported.
            self._executions.clear()
            if self._reported_runs:
                await self._notify_change(runs=False)

    async def _drain(self, *, scopes: set[str], timeout: float | None) -> None:
        tracked = [t for t in self._inflight if t.scope in scopes]
        targets = [t for t in tracked if not t.task.done()]
        try:
            if targets:
                tasks = [t.task for t in targets]
                try:
                    if timeout is None:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    else:
                        await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True),
                            timeout=timeout,
                        )
                except TimeoutError:
                    logger.warning(
                        "Hook drain timed out after %.1fs; cancelling %d task(s)",
                        timeout,
                        sum(1 for t in tasks if not t.done()),
                    )
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    # Best-effort await of cancellation so subprocess cleanup runs.
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # Also when this drain is cancelled (an interrupt during
            # ``drain_turn``): ``gather`` passes the cancel on to the runs and
            # raises only once every one of them is done.
            changed = _Changed()
            for tracked_task in tracked:
                changed |= self._settle_unstarted(tracked_task)
            await self._report(changed)

        for tracked_task in tracked:
            if not tracked_task.task.done():
                continue
            try:
                tracked_task.task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Hook task %r failed outside normal result handling", tracked_task.hook.id)

        # Reap done tasks of these scopes, including hooks that completed
        # before this drain call started.
        self._inflight = [t for t in self._inflight if not (t.scope in scopes and t.task.done())]

    # ── outbox recovery ──────────────────────────────────────────────

    async def recover_outbox(self) -> int:
        """Scan ``outbox/pending/`` and retry stale jobs.

        Returns the number of jobs queued for retry.  Called once at
        engine startup.  Durable detached jobs are recovered through the
        same detached worker path as fresh dispatches; semantics remain
        at-least-once.
        """
        stale = self._outbox.list_stale_pending(
            retry_age_seconds=self._file.settings.outbox_retry_age_seconds,
        )
        if not stale:
            return 0

        max_retries = self._file.settings.outbox_max_retries
        queued = 0
        for job in stale:
            if job.retries >= max_retries:
                logger.warning(
                    "Outbox job %s (hook=%s) exceeded max_retries=%d; moving to failed/",
                    job.job_id,
                    job.hook_id,
                    max_retries,
                )
                with contextlib.suppress(OSError):
                    self._outbox.force_fail(job, reason=f"retries>={max_retries}")
                continue
            hook, missing_reason = self._resolve_recovery_hook(job)
            if hook is None:
                logger.warning(
                    "Outbox job %s references %s; moving to failed/",
                    job.job_id,
                    missing_reason,
                )
                with contextlib.suppress(OSError):
                    self._outbox.force_fail(job, reason=missing_reason)
                continue
            if not hook.enabled:
                disabled_reason = f"disabled hook {job.hook_id!r}"
                logger.warning(
                    "Outbox job %s references %s; moving to failed/",
                    job.job_id,
                    disabled_reason,
                )
                with contextlib.suppress(OSError):
                    self._outbox.force_fail(job, reason=disabled_reason)
                continue
            # Schedule a retry with the original payload.  We reuse the
            # same job_id so done/failed can be correlated back to the
            # original incident if anyone goes looking.
            await self._spawn_nonblocking(
                hook,
                job.payload,
                scope="session",
                session_drain_generation=self._session_drain_generation,
                existing_job=job,
            )
            queued += 1
        return queued

    def _resolve_recovery_hook(self, job: OutboxJob) -> tuple[HookConfig | None, str]:
        """Return the hook a pending outbox job should retry, plus a failure label."""
        if job.hook_source:
            hook = self._hooks_by_recovery_key.get((job.hook_id, job.hook_source, job.hook_source_path))
            if hook is None and not job.hook_source_path:
                hook = self._resolve_legacy_recovery_hook_by_source(job)
            if hook is None:
                source_label = job.hook_source
                if job.hook_source_path:
                    source_label = f"{job.hook_source!r} source at {job.hook_source_path!r}"
                else:
                    source_label = f"{job.hook_source!r} source"
                return None, f"missing hook {job.hook_id!r} from {source_label}"
            return hook, ""

        matches = self._hooks_by_id.get(job.hook_id, [])
        if len(matches) > 1:
            return None, f"ambiguous legacy hook id {job.hook_id!r} with no source marker"
        if not matches:
            return None, f"missing hook {job.hook_id!r}"
        return matches[0], ""

    def _resolve_legacy_recovery_hook_by_source(self, job: OutboxJob) -> HookConfig | None:
        """Resolve pre-source-path jobs that only recorded project/global."""
        matches = [
            hook
            for hook in self._hooks_by_id.get(job.hook_id, [])
            if self._hook_sources.get(id(hook), "") == job.hook_source
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    # ── internals ────────────────────────────────────────────────────

    async def _run_blocking(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        *,
        waited_at: HookWaitPoint,
        run: _Run,
        operation: HookOperation | None = None,
    ) -> HookResult | None:
        """Queue *run*, take a slot and run it; the caller ends the run, however this ends.

        None when *operation* was withdrawn by the time the script would start
        (while the run queued for its slot or reported taking it): no script
        runs for an operation that will not go on.

        Every end is reported, a cancelled run's too: the report runs on the
        run's own task and reads the set when it is stored, so the last stored
        snapshot stays the current one. A run ending after the manager closed
        (an undrained ``user_interrupt`` observer) was already cleared and
        reported: publishing again would overwrite the snapshot of the manager
        that replaced this one.
        """
        # Queued before the slot is taken: probing the semaphore first would
        # race other runs taking or releasing it.
        await self._report(self._queue_run(hook, run, waited_at=waited_at))
        async with self._sem:
            # Published while holding the slot: ``on_change`` must stay a
            # quick store (the TUI defers its render), or it delays every
            # run queued behind this one.
            await self._report(self._take_slot(run))
            # Checked last, with no wait before the start: nothing can withdraw it in between.
            if operation is not None and not operation.pending:
                return None
            run.output = HookResult(hook_id=hook.id, exit_code=None)
            return await self._runner.run_and_wait(hook, payload, payload_text=run.payload_text, result=run.output)

    def _drop_withdrawn_job(self, job: OutboxJob | None, operation: HookOperation | None) -> None:
        """End *job*, written for *operation* and never handed to a run, when the operation was withdrawn.

        A withdrawn operation never happens, so nothing is owed to recover, also when hooks close
        after it was withdrawn. One that hooks closing ended keeps its job: recovery runs it later, as
        durable promises. Decided by what ended the operation first (``ended_by_closing_hooks``), never
        by ``end_reason``, whose codes are display text.
        """
        if job is None or operation is None or operation.state != "invalidated":
            return
        if operation.ended_by_closing_hooks:
            return
        try:
            self._outbox.force_fail(job, reason=f"not run: its operation was withdrawn ({operation.end_reason})")
        except OSError:
            logger.warning("Could not end the outbox job of a withdrawn operation: %s", job.job_id, exc_info=True)

    async def _spawn_nonblocking(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        *,
        scope: str,
        session_drain_generation: int,
        existing_job: OutboxJob | None = None,
        target_operation_id: str | None = None,
        origin: InvocationOrigin | None = None,
        operation: HookOperation | None = None,
    ) -> None:
        """Start a non-blocking *hook*; not when hooks closed or *operation* was withdrawn by then."""
        if self._closed or self._session_drain_generation != session_drain_generation:
            return
        if operation is not None and not operation.pending:
            return
        run = self._new_run(payload, origin=origin, retry=existing_job is not None)
        job = existing_job
        # Written here, the job is a promise to run the hook, kept by recovery should this process die.
        written: OutboxJob | None = None
        if hook.execution.delivery == "durable" and job is None:
            job = written = self._outbox.write_pending(
                hook_id=hook.id,
                event=str(hook.event),
                payload=payload,
                hook_source=self._hook_sources.get(id(hook), ""),
                hook_source_path=self._hook_source_paths.get(id(hook), ""),
            )
        drain_scope: str | None
        if hook.execution.detach:
            drain_scope = None
        elif hook.execution.mode == "fire_and_forget":
            drain_scope = "background"
        else:
            drain_scope = scope
        # Async turn-scope hooks must stay in the active turn: drain_turn()
        # publishes their ids in the response fence, whose analyzer lookup is
        # intentionally confined to that turn's node membership.
        trace = self._open_trace(target_operation_id, operation_id=run.run_id)
        if trace is not None:
            try:
                await trace.started(
                    hook_id=hook.id,
                    hook_event=str(hook.event),
                    execution_mode=hook.execution.mode,
                    detach=hook.execution.detach,
                    delivery=hook.execution.delivery,
                    scope=scope,
                    drain_scope=drain_scope,
                )
            except BaseException:
                # Interrupted in the start marker's ack wait: no task exists
                # yet that would close the span, so it is closed here — and
                # the trace drops the terminal by itself if that marker never
                # took a sequence.
                trace.finished_soon(outcome=HookOutcome.CANCELLED)
                self._drop_withdrawn_job(written, operation)
                raise
        withdrawn = operation is not None and not operation.pending
        if withdrawn or self._closed or self._session_drain_generation != session_drain_generation:
            if trace is not None:
                trace.finished_soon(outcome=HookOutcome.ABANDONED)
            self._drop_withdrawn_job(written, operation)
            return

        # async / fire_and_forget — wrap in our internal task so we can
        # observe completion and move outbox entries.
        if hook.execution.mode == "async":
            # The drain of the run's scope waits on it; nobody waits on an interrupt observer.
            waited_at: HookWaitPoint = "turn" if scope == "turn" else "session" if scope == "session" else "none"
            self._start_tracked(hook, payload, job, trace, run, scope=scope, waited_at=waited_at)
            await self._notify_change()
            return

        # fire_and_forget
        if hook.execution.detach:
            try:
                if job is not None:
                    self._outbox.mark_started(job)
                log_path = await self._runner.spawn_detached(hook, payload, job=job, payload_text=run.payload_text)
            except asyncio.CancelledError:
                if trace is not None:
                    trace.finished_soon(outcome=HookOutcome.CANCELLED)
                # Recorded as its span is: interrupted while it was handed off.
                self._register_run(hook, run)
                await self._report(self._finish_run(run, outcome=HookOutcome.CANCELLED))
                raise
            except Exception as exc:
                _log_nonblocking_spawn_failure(hook, exc)
                if job is not None:
                    with contextlib.suppress(OSError):
                        self._outbox.mark_failed(job, exit_code=None, error=str(exc))
                # Recorded ended at once: nobody observes a detached run.
                self._register_run(hook, run)
                changed = self._finish_run(run, outcome=HookOutcome.SPAWN_FAILED, error=str(exc))
                await self._report_end(changed, trace, outcome=HookOutcome.SPAWN_FAILED)
                return
            # Handed to the detached worker: the parent never observes its
            # exit, so the run, and its recorded span, end at the hand-off.
            self._register_run(hook, run)
            changed = self._finish_run(run, outcome=HookOutcome.DETACHED, log_path=str(log_path))
            await self._report_end(changed, trace, outcome=HookOutcome.DETACHED)
            return

        # Observed to its end like any other run, though nobody waits on it.
        self._start_tracked(hook, payload, job, trace, run, scope="background", waited_at="none")
        await self._notify_change()

    def _start_tracked(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        job: OutboxJob | None,
        trace: HookOperationTrace | None,
        run: _Run,
        *,
        scope: str,
        waited_at: HookWaitPoint,
    ) -> None:
        """Queue a non-blocking run, start its task and track it, all without suspending.

        The caller checked that the manager is open and reports the run only
        after this: a drain or close that runs during the report already sees
        the task, which owns the run and its span from here on.
        """
        self._queue_run(hook, run, waited_at=waited_at)
        task = asyncio.create_task(self._run_async_tracked(hook, payload, job, trace, run=run))
        operation_id = trace.operation_id if trace is not None and trace.start_committed else None
        tracked = _Tracked(hook=hook, task=task, scope=scope, job=job, operation_id=operation_id, run=run, trace=trace)
        self._inflight.append(tracked)
        if scope in ("detached", "background"):
            task.add_done_callback(lambda _task, tracked=tracked: self._reap_background(tracked))

    def _settle_unstarted(self, tracked: _Tracked) -> _Changed:
        """Settle a run cancelled before its first step, and say what that changed.

        Such a task never ran the body that closes the run's span and ends
        the run. A no-op for a run that has not ended, or whose body ran: that
        body closed the span (closing twice keeps the first) and ended the run.
        """
        if not tracked.task.cancelled():
            return _Changed()
        if tracked.trace is not None:
            tracked.trace.finished_soon(outcome=HookOutcome.CANCELLED)
        return self._finish_run(tracked.run, outcome=HookOutcome.CANCELLED)

    def _reap_background(self, tracked: _Tracked) -> None:
        with contextlib.suppress(ValueError):
            self._inflight.remove(tracked)
        # Nothing here cancels these runs (loop teardown does), so a run ended
        # here goes unreported: the next report leaves it out.
        self._settle_unstarted(tracked)
        try:
            tracked.task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background hook task %r failed outside normal result handling", tracked.hook.id)

    async def _run_async_tracked(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        job: OutboxJob | None,
        trace: HookOperationTrace | None = None,
        *,
        run: _Run | None = None,
    ) -> HookResult:
        """Wrap ``runner.run_and_wait`` with outbox + semaphore + error logging."""
        run = run if run is not None else _Run(new_analytics_id())
        try:
            return await self._run_async_tracked_inner(hook, payload, job, trace, run)
        finally:
            # A run ended once its outcome was known; this ends one cancelled
            # before then, and reports it, as for a blocking run. No snapshot
            # after the manager closed first: ``background`` runs are not
            # drained and may end long after the close. The run log still
            # records them.
            await self._report(self._finish_run(run, outcome=HookOutcome.CANCELLED))

    async def _run_async_tracked_inner(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        job: OutboxJob | None,
        trace: HookOperationTrace | None,
        run: _Run,
    ) -> HookResult:
        try:
            # The slot is taken inside the ``try``: a hook cancelled while it
            # queues behind ``max_parallel_hooks`` others never ran, but its
            # span was opened before the task was created, and once the task
            # took a step only this handler closes it.
            async with self._sem:
                await self._report(self._take_slot(run))
                if job is not None:
                    self._outbox.mark_started(job)
                run.output = HookResult(hook_id=hook.id, exit_code=None)
                result = await self._runner.run_and_wait(
                    hook, payload, payload_text=run.payload_text, result=run.output
                )
        except asyncio.CancelledError:
            if job is not None and hook.execution.delivery == "durable":
                logger.info(
                    "Durable hook %r was cancelled; leaving outbox job %s pending for retry",
                    hook.id,
                    job.job_id,
                )
            if trace is not None:
                trace.finished_soon(outcome=HookOutcome.CANCELLED)
            raise
        except Exception as exc:
            logger.exception("Hook %r raised", hook.id)
            result = HookResult(hook_id=hook.id, exit_code=None, stderr=str(exc))
            launch_error: str | None = str(exc)
        else:
            launch_error = None
        # The hook has run: what it did is settled before anything is awaited,
        # so an interrupt landing on an await below neither logs it cancelled
        # nor leaves a finished durable job pending for the next runtime to
        # replay (the outbox decides whether it runs again).
        outcome = _hook_outcome(result, blocked=False)
        changed = self._finish_run(run, outcome=outcome, result=result, applied=_nonblocking_applied(result))
        if job is not None:
            with contextlib.suppress(OSError):
                if launch_error is not None:
                    self._outbox.mark_failed(job, exit_code=None, error=launch_error)
                elif _result_failed(result):
                    self._outbox.mark_failed(
                        job,
                        exit_code=result.exit_code,
                        error=_result_failure_label(result),
                        stderr_tail=result.stderr,
                    )
                else:
                    self._outbox.mark_done(job, exit_code=result.exit_code or 0)
        await self._report_end(changed, trace, outcome=outcome, exit_code=result.exit_code, timed_out=result.timed_out)
        if launch_error is None and _result_failed(result):
            _log_hook_failure(hook, result)
        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_envelope(event: HookEvent, payload: dict[str, Any]) -> dict[str, Any]:
    """Combine *payload* with the standard envelope fields."""
    envelope = {
        **copy.deepcopy(payload),
        "schema": 1,
        "event": str(event),
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return envelope


def _hook_sources(file: MergedHooksFile) -> dict[int, str]:
    """Map hook object identity to its merged configuration layer."""
    sources: dict[int, str] = {}
    if file.project is not None:
        for hook in file.project.hooks:
            sources[id(hook)] = "project"
    if file.global_ is not None:
        for hook in file.global_.hooks:
            sources[id(hook)] = "global"
    return sources


def _hook_source_paths(file: MergedHooksFile) -> dict[int, str]:
    """Map hook object identity to the hooks config file that supplied it."""
    sources: dict[int, str] = {}
    if file.project is not None and file.project.source:
        for hook in file.project.hooks:
            sources[id(hook)] = file.project.source
    if file.global_ is not None and file.global_.source:
        for hook in file.global_.hooks:
            sources[id(hook)] = file.global_.source
    return sources


def _hooks_by_recovery_key(
    hooks: list[HookConfig],
    hook_sources: dict[int, str],
    hook_source_paths: dict[int, str],
) -> dict[tuple[str, str, str], HookConfig]:
    """Index hooks by id plus exact source layer/path for durable outbox retry."""
    keyed: dict[tuple[str, str, str], HookConfig] = {}
    for hook in hooks:
        source = hook_sources.get(id(hook), "")
        source_path = hook_source_paths.get(id(hook), "")
        if not source and not source_path:
            continue
        keyed[(hook.id, source, source_path)] = hook
    return keyed


def _hooks_by_id(hooks: list[HookConfig]) -> dict[str, list[HookConfig]]:
    """Group hooks by id while preserving merged order within each id."""
    grouped: dict[str, list[HookConfig]] = {}
    for hook in hooks:
        grouped.setdefault(hook.id, []).append(hook)
    return grouped


def _tool_args(envelope: dict[str, Any]) -> Any:
    tool = envelope.get("tool")
    return tool.get("args") if isinstance(tool, dict) else None


def _text_field(fields: dict[str, Any], key: str) -> str:
    """*fields*' *key* when it is a string, else empty."""
    value = fields.get(key)
    return value if isinstance(value, str) else ""


def _decision_action(decision: dict[str, Any]) -> str:
    """The ``action`` a result file names, lowercased; ``allow`` unless it names one as a string."""
    action = decision.get("action")
    return action.lower() if isinstance(action, str) and action else "allow"


def _asks_unsupported(event: HookEvent, decision: dict[str, Any]) -> bool:
    """Whether a result file asks for something *event* does not take."""
    action = _decision_action(decision)
    return (
        (action in ("block", "ask") and event not in GATED_EVENTS)
        or (action == "modify" and event not in _ARGS_OVERRIDE_EVENTS)
        or (bool(decision.get("system_reminder")) and event not in _SYSTEM_REMINDER_EVENTS)
        or (bool(decision.get("extra_context")) and event not in _EXTRA_CONTEXT_EVENTS)
    )


def _asks_anything(decision: dict[str, Any]) -> bool:
    action = _decision_action(decision)
    return action in ("block", "modify", "ask") or bool(
        decision.get("system_reminder") or decision.get("extra_context")
    )


def _blocking_effect(
    event: HookEvent,
    result: HookResult,
    decision: HookDecision,
    *,
    blocked: bool,
    asked: bool,
    args_changed: bool,
    args_after: Any,
    reminders_before: int,
    contexts_before: int,
) -> tuple[HookRunEffect | None, HookRunApplied]:
    """What iCode took from one blocking run, folded into *decision*; at most one effect.

    *blocked* when this run blocked; *asked* when it asked the user to confirm;
    *args_changed* when its override changed the arguments, *args_after*.
    """
    failed = _result_failed(result)
    if blocked:
        return "blocked", HookRunApplied(
            block_reason=decision.block_reason, not_applied="failed_blocked" if failed else None
        )
    if failed:
        return None, HookRunApplied(not_applied="failed")
    if asked:
        return "asked", HookRunApplied()
    if args_changed:
        return "modified_args", HookRunApplied(args=indented_json(args_after))
    if len(decision.system_reminders) > reminders_before:
        return "added_reminder", HookRunApplied(reminder=decision.system_reminders[-1])
    if len(decision.extra_context) > contexts_before:
        return "added_context", HookRunApplied(extra_context=decision.extra_context[-1])
    if _asks_unsupported(event, result.decision):
        return None, HookRunApplied(not_applied="unsupported")
    return None, HookRunApplied()


def _nonblocking_applied(result: HookResult) -> HookRunApplied:
    """An ``async`` or ``fire_and_forget`` run's decision is never taken."""
    if _result_failed(result):
        return HookRunApplied(not_applied="failed")
    if _asks_anything(result.decision):
        return HookRunApplied(not_applied="nonblocking")
    return HookRunApplied()


def _apply_args_override_to_envelope(envelope: dict[str, Any], args_override: dict[str, Any]) -> None:
    """Merge blocking hook argument overrides into the current tool payload."""
    tool = envelope.get("tool")
    if not isinstance(tool, dict):
        return
    args = tool.get("args")
    if not isinstance(args, dict):
        args = {}
        tool["args"] = args
    args.update(args_override)


def _merge_blocking_result(
    decision: HookDecision,
    hook: HookConfig,
    result: HookResult,
    *,
    event: HookEvent,
) -> str | None:
    """Fold one blocking hook's outcome into the running :class:`HookDecision`.

    Returns the reason of an ``ask`` on a gating event, which the caller puts
    to the user before the next hook runs; None when the run did not ask. An
    ``ask`` takes nothing else from its result file: not an argument override,
    a reminder or context.
    """
    on_error = hook.execution.on_error
    failed = _result_failed(result)

    if failed:
        msg_kind = _result_failure_label(result)
        if on_error == "block" and event in GATED_EVENTS:
            decision.blocked = True
            decision.block_reason = f"hook '{hook.id}' failed ({msg_kind}): {result.stderr.strip()[:200]}"
            return None
        if on_error == "warn":
            logger.warning("Hook %r failed (%s) but on_error=warn; continuing", hook.id, msg_kind)
        elif on_error == "block":
            logger.warning(
                "Hook %r failed (%s) but event %s is not gated; treating on_error=block as warn",
                hook.id,
                msg_kind,
                event,
            )
        # A failed hook's decision file may be partial or stale.  Honour
        # only the configured error policy and ignore any action payload.
        return None

    action = _decision_action(result.decision)
    if action == "block":
        if event in GATED_EVENTS:
            decision.blocked = True
            reason = str(result.decision.get("reason") or "")
            decision.block_reason = f"hook '{hook.id}' blocked: {reason}" if reason else f"hook '{hook.id}' blocked"
            return None
        logger.warning("Hook %r returned action=block for non-gated event %s; ignoring block", hook.id, event)
    if action == "ask":
        if event in GATED_EVENTS:
            reason = result.decision.get("reason")
            return reason[:ASK_REASON_MAX_CHARS] if isinstance(reason, str) else ""
        logger.warning("Hook %r returned action=ask for non-gated event %s; ignoring ask", hook.id, event)
    if action == "modify" and event in _ARGS_OVERRIDE_EVENTS:
        args_override = result.decision.get("args_override")
        if isinstance(args_override, dict):
            decision.args_override = {**(decision.args_override or {}), **args_override}

    reminder = result.decision.get("system_reminder")
    if event in _SYSTEM_REMINDER_EVENTS and isinstance(reminder, str) and reminder:
        decision.system_reminders.append(reminder)
    extra = result.decision.get("extra_context")
    if event in _EXTRA_CONTEXT_EVENTS and isinstance(extra, str) and extra:
        decision.extra_context.append(extra)
    return None


def _ask_refusal(
    hook_id: str, reason: str, result: HookConfirmationResult, timeout: float | None, *, tool_call: bool
) -> str:
    """Why an ``ask`` stopped the operation (English, no ``Error:`` prefix).

    A tool call's is what the model reads in its result, so it also says that the call
    did not run and whether calling it again can help: a model told only that nobody
    answered tends to call it again, and ask the user once more. A refused input's is
    shown to the user, who needs only the why.
    """
    asked = (
        f"hook '{hook_id}' asked for confirmation ({reason})" if reason else f"hook '{hook_id}' asked for confirmation"
    )
    if result.outcome == "declined":
        why = "the user declined"
        then = "Do not call it again unless the user asks you to."
    elif result.outcome == "timed_out":
        why = f"the user did not answer within {timeout:g} s" if timeout else "the user did not answer in time"
        then = "Ask the user before calling it again."
    elif result.end_reason in ("frontend_unavailable", "error"):
        why = "the confirmation could not be shown to the user"
        then = "Tell the user; do not call it again."
    else:
        why = "no one can confirm it in this session"
        then = "Calling it again will be refused the same way."
    return f"{asked}; {why}, so the tool call did not run. {then}" if tool_call else f"{asked}; {why}"


def _log_hook_failure(hook: HookConfig, result: HookResult) -> None:
    if hook.execution.on_error == "ignore":
        logger.debug("Hook %r failed (on_error=ignore)", hook.id)
        return
    detail = _result_failure_label(result)
    logger.warning("Hook %r failed (%s); stderr=%s", hook.id, detail, result.stderr.strip()[:500])


def _log_nonblocking_spawn_failure(hook: HookConfig, exc: Exception) -> None:
    if hook.execution.on_error == "ignore":
        logger.debug("Non-blocking hook %r could not be spawned", hook.id, exc_info=True)
        return
    logger.warning("Non-blocking hook %r could not be spawned: %s", hook.id, exc, exc_info=True)


def _result_failed(result: HookResult) -> bool:
    return result.timed_out or result.exit_code is None or result.exit_code != 0


def _hook_outcome(result: HookResult, *, blocked: bool, asked: bool = False) -> str:
    """Closed ``hook.operation.finished.outcome`` for one subprocess result."""
    if result.timed_out:
        return HookOutcome.TIMED_OUT
    if result.exit_code is None:
        return HookOutcome.LAUNCH_ERROR
    if result.exit_code != 0:
        return HookOutcome.FAILED
    if blocked:
        return HookOutcome.BLOCKED
    if asked:
        return HookOutcome.ASKED
    return HookOutcome.SUCCESS


def _result_failure_label(result: HookResult) -> str:
    # Named apart from a timeout: stderr only ends with the note, which a cut may drop.
    if result.stopped:
        return "stopped"
    if result.timed_out:
        return "timeout"
    if result.exit_code is None:
        return "launch_error"
    return f"exit_code={result.exit_code}"


_NO_SNAPSHOT_REASON = "the before_tool_call hooks cannot check these tool arguments: they have no JSON form"


async def check_operation(
    manager: HookManager | None,
    event: HookEvent,
    operation: HookOperation,
    payload: Callable[[], dict[str, Any]],
    *,
    scope: str = "turn",
    target_operation_id: str | None = None,
    trajectory_context: TrajectoryContext | None = None,
    origin: InvocationOrigin | None = None,
) -> HookDecision:
    """Check *operation*'s current version with *event*'s hooks: the one way every gating entry does.

    The one place a version is marked checked: once every hook configured for
    *event* passed it. With none configured (no hooks file, a ``None``
    manager, no hook for *event*) that holds at once, so the operation's
    commit treats every entry alike. A manager that cannot run the hooks it
    has (closed before or while they ran) passes nothing: ``fire`` reports the
    operation invalidated. *payload* is built only when a hook will read it.
    An operation already ended is not checked again, and a tool call whose
    arguments have no snapshot (no canonical form) is refused where hooks
    would check it: they cannot be shown it.
    """
    operation.begin_check()
    if not operation.pending:
        return HookDecision(invalidated=True)
    if manager is None or not manager.has_hooks_for(event):
        return _passed(operation, HookDecision())
    if operation.subject == "tool" and not operation.has_snapshot:
        return HookDecision(blocked=True, block_reason=_NO_SNAPSHOT_REASON)
    decision = await manager.fire(
        event,
        payload(),
        scope=scope,
        target_operation_id=target_operation_id,
        trajectory_context=trajectory_context,
        origin=origin,
        operation=operation,
    )
    if decision.args_override and not (decision.blocked or decision.invalidated):
        # The override the decision reports is already the operation's (``fire`` applied it as
        # each hook returned it); folding it again changes nothing then, and keeps the decision's
        # own contract for any caller that reads it.
        operation.replace_arguments({**operation.arguments(), **decision.args_override})
    if decision.blocked or decision.invalidated:
        return decision
    return _passed(operation, decision)


def _passed(operation: HookOperation, decision: HookDecision) -> HookDecision:
    """Mark *operation*'s current version checked; invalidated instead when it ended meanwhile."""
    if not operation.mark_checked(operation.revision):
        decision.invalidated = True
    return decision
