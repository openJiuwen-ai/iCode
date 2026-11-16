# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the TUI's Hooks panel shows of the hook runs since iCode started."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from chrys.foundation.models.hook_confirmations import HookConfirmationOutcome

HookRunState = Literal["queued", "running", "ended"]

HookRunEffect = Literal["blocked", "modified_args", "added_reminder", "added_context", "replaced_result", "asked"]
"""What iCode took from a run; the row names one, the first that applies in that order.
``asked``: the run asked the user to confirm; what the user chose is the run's ``confirmation``.

A run may do more than one at once — ``after_tool_call`` takes both ``result_override``
and ``extra_context`` from one file — so this is the headline, not everything the
run changed. ``HookRunApplied`` keeps each text; read that for what it actually did."""

HookNotApplied = Literal["nonblocking", "unsupported", "failed", "failed_blocked"]
"""Why a run's decision was not taken: an ``async``/``fire_and_forget`` run's never is, the event
does not take that action, or the run failed (its result file ignored; with ``on_error: block`` on a
gated event the action was blocked instead)."""

HookRunConfirmation = HookConfirmationOutcome | Literal["pending"]
"""What the user chose about an ``asked`` run: ``pending`` while the question is open."""

HookResultFileStatus = Literal["ok", "missing", "invalid"]
"""``missing`` covers an absent or empty result file; ``invalid`` one too large, not JSON or not an object."""


@dataclass(frozen=True, slots=True)
class HookRunRecord:
    """One hook run, as it is at one moment."""

    run_id: str
    """Also the run's ``hook.operation.*`` ``operation_id`` when the trajectory records it."""
    session_id: str
    hook_id: str
    hook_event: str
    execution_mode: str
    detach: bool
    tool_name: str
    """The tool of a tool event; empty for other events."""
    sub_agent: str
    """The sub-agent that fired it; empty for the main agent."""
    retry: bool
    """An outbox retry of an earlier run."""
    registered_at: float
    """``time.monotonic()`` when registered, queued or not."""
    started_at: float | None
    """``time.monotonic()`` when it took a ``max_parallel_hooks`` slot; None when it never did."""
    ended_at: float | None
    registered_wall: float
    """``time.time()`` when registered."""
    state: HookRunState
    outcome: str | None
    """How the hook's process ended, once it did: a ``HookOutcome`` but ``blocked``.

    A run that blocked the action succeeded, with the ``blocked`` effect; its
    ``hook.operation.finished`` says ``blocked`` instead.
    """
    exit_code: int | None
    effect: HookRunEffect | None
    number: int = 0
    """The run's place among every run since the process started, from 1; kept as older runs are dropped."""
    confirmation: HookRunConfirmation | None = None
    """For a run whose effect is ``asked``: what came of the question; None for any other run."""
    call_rejected: bool = False
    """The run's argument change was one too many after the user continued: iCode rejected the tool call."""


@dataclass(frozen=True, slots=True)
class HookRunIO:
    """What a run read and wrote: kept for the runs the panel lists, within a memory cap."""

    payload: str
    """The payload file's text as the hook read it, cut short when longer than the run log keeps."""
    payload_size: int
    """The payload's full length in UTF-8 bytes."""
    result_file: HookResultFileStatus | None
    """None until the run ends, and for runs whose result is never read (detached)."""
    decision: str
    """The result file's JSON object, as read."""
    stdout: str
    stderr: str
    stdout_dropped: int
    stderr_dropped: int
    """Bytes cut from the middle of each stream."""


@dataclass(frozen=True, slots=True)
class HookRunApplied:
    """What iCode took from a run, or why it took nothing."""

    block_reason: str = ""
    args: str = ""
    """The tool arguments after the run's override, as JSON; empty when unchanged."""
    args_size: int = 0
    """The changed arguments' full length in UTF-8 bytes when ``args`` was cut short; 0 when it is whole."""
    reminder: str = ""
    extra_context: str = ""
    result_override: str = ""
    """The tool result text the run replaced; empty when it replaced nothing."""
    not_applied: HookNotApplied | None = None


@dataclass(frozen=True, slots=True)
class HookRunDetail:
    """A run's record with what its details dialog shows."""

    record: HookRunRecord
    tool_call_id: str
    timeout_seconds: float
    on_error: str
    delivery: str
    source: str
    """``project`` or ``global``; empty when unknown."""
    source_path: str
    run_type: str
    """``script``, ``command`` or ``shell``."""
    run_argv: tuple[str, ...]
    """The script path and its args, the command's argv, or the shell command line."""
    run_cwd: str
    run_env_names: tuple[str, ...]
    """The names ``run.env`` sets; never their values, which may be expanded secrets."""
    log_path: str
    """A detached run's log file."""
    shared_log: bool
    """Another run of the hook writes to the same log (they started in the same second)."""
    io: HookRunIO | None
    """None once the details are no longer kept; ``applied`` then keeps only its block reason."""
    applied: HookRunApplied | None
    """None until the run ends."""
