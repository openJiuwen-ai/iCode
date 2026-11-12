# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hook event names — string enum for every lifecycle point.

The string values are part of the user-facing YAML schema, so renaming
one is a breaking change.  All event payloads share a ``schema`` /
``event`` / ``session_id`` / ``cwd`` / ``profile`` envelope; event-
specific fields are listed in each member's docstring.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class HookEvent(StrEnum):
    """Canonical hook event names.

    Each event maps to a specific runtime insertion point.  Subclassing
    :class:`StrEnum` means ``HookEvent.BEFORE_TOOL_CALL == "before_tool_call"``
    is always true, so config files can use the bare string and the
    manager's matcher can compare against both.
    """

    # ── Session lifecycle ────────────────────────────────────────────
    SESSION_START = "session_start"
    """Engine started successfully and the agent is ready for input.

    Fired after ``engine.start()`` finishes its first build.  Payload:
    base envelope only. Workflow sessions fire on their first admitted run;
    resource reconstruction between runs does not create another session.
    """

    SESSION_RESTORED = "session_restored"
    """A persisted session was restored.

    Distinct from :data:`SESSION_START` so hooks can tell a fresh boot
    apart from a resume.  Both can fire in the same startup if the user
    immediately restores after launch.  Payload: base envelope plus
    ``restored_session_id``. For Workflow this fires when a persisted session
    first executes in a host; browsing its history alone is passive.
    """

    SESSION_END = "session_end"
    """Engine is shutting down.

    Last chance to fire telemetry hooks; the shutdown grace timer is the
    upper bound on how long the manager waits before terminating
    in-flight tasks.  For "must not lose" hooks use ``delivery: durable``
    plus ``detach: true`` (see schema). Payload: base envelope. Workflow
    attachments end at host shutdown or session deletion, after run-end hooks.
    """

    WORKFLOW_RUN_START = "workflow_run_start"
    """An admitted workflow run is about to execute. Observer; payload adds run_id and input_text."""

    WORKFLOW_RUN_END = "workflow_run_end"
    """A workflow run finished after checkpoint. Observer; payload adds run_id, outcome and reason."""

    # ── Turn lifecycle ───────────────────────────────────────────────
    BEFORE_TURN = "before_turn"
    """A new user-initiated turn is about to run.

    Fired from ``run/lifecycle.pre_run`` after the turn counter and
    mutation tracker are set up, before the executor's ``agent.run()``.
    Payload: base envelope plus ``turn`` (1-indexed turn number) and
    ``user_text``.
    """

    AFTER_TURN = "after_turn"
    """A user-initiated turn finished (or was interrupted / failed).

    Fired from ``run/lifecycle.post_run`` after markers and session save.
    In-flight ``async`` hooks from this turn are drained before the next
    turn starts.  Payload: base envelope plus ``turn`` and ``status``
    (``"ok"`` | ``"interrupted"`` | ``"failed"``).
    """

    USER_PROMPT_SUBMIT = "user_prompt_submit"
    """The user submitted a prompt.

    Fires before the engine routes the message into the executor.
    Blocking hooks may return ``action: block`` (with a reason that is
    surfaced to the user) or ``system_reminder`` text that the manager
    queues for the executor to append.  Payload: base envelope plus
    ``text`` and ``injected`` (true when the prompt is a mid-turn
    injection into an active run).
    """

    # ── Tool lifecycle ───────────────────────────────────────────────
    APPROVAL_REQUESTED = "approval_requested"
    """A tool is waiting for a human approval decision.

    Auto-review runs first; an automatically accepted request does not fire
    this event. Observer only: hook decisions cannot grant or deny approval.
    Payload: base envelope, ``tool``, ``request_id`` and ``caller_name``.
    """

    APPROVAL_RESOLVED = "approval_resolved"
    """A human approval wait ended with acceptance or rejection, before execution.

    Payload: as ``approval_requested`` plus ``approved``. An interrupted wait
    instead ends through the normal turn lifecycle hooks.
    """

    BEFORE_TOOL_CALL = "before_tool_call"
    """A tool invocation is about to execute.

    Runs before ``ToolCallStart``, mutation tracking, file locks, and
    approval so blocking hooks can deny early and modifying hooks can
    rewrite args before the rest of the tool pipeline observes them.
    Cannot grant approval; it can only deny or modify args, or add
    ``extra_context`` that reaches the model with the call's result once
    the call has run (a note, not a decision).  Payload:
    base envelope plus ``tool`` (``name`` / ``kind`` / ``call_id`` /
    ``args``) and ``origin``: the calling invocation (``kind`` of
    ``"turn"`` | ``"sub_agent"`` | ``"workflow_node"``, ``session_id``,
    ``invocation_id``, ``attempt``) with its bound ancestry nested under
    ``parent`` (``null`` at the root), or ``null`` when the call has no
    bound invocation.  A sub-agent's tool calls carry the ``invocation_id``
    its ``sub_agent_start`` announced, so concurrent instances of one
    profile stay distinguishable.  Denied calls still publish rejected tool
    events with the final call id for UI and history consistency.
    """

    AFTER_TOOL_CALL = "after_tool_call"
    """A tool invocation completed.

    Fires regardless of success / approval rejection / cancellation, as
    long as the inner ``call_next()`` returned (cancellations short-
    circuit).  Use for linting, post-write checks, telemetry.  Payload:
    base envelope plus ``tool``, ``origin`` (as :data:`BEFORE_TOOL_CALL`)
    and ``result`` (``text`` / ``duration_ms`` / ``error`` /
    ``approval_rejected``).
    """

    TOOL_ERROR = "tool_error"
    """A tool invocation raised.

    Distinct from :data:`AFTER_TOOL_CALL` with ``error`` set so users can
    write hooks that only care about failures.  Payload: as
    :data:`AFTER_TOOL_CALL`.
    """

    # ── Sub-agent lifecycle ──────────────────────────────────────────
    SUB_AGENT_START = "sub_agent_start"
    """A sub-agent invocation began.  Payload: base envelope plus
    ``sub_agent`` (``name`` / ``tool_name`` / ``invocation_id`` /
    ``parent_call_id``).
    """

    SUB_AGENT_END = "sub_agent_end"
    """A sub-agent invocation finished.  Payload: as
    :data:`SUB_AGENT_START` plus ``status`` and ``result_summary``.
    """

    # ── Context management ───────────────────────────────────────────
    PRE_COMPACT = "pre_compact"
    """Compaction is about to fire.  Payload: base envelope plus
    ``trigger`` (``"phase1"`` | ``"phase2"`` | ``"phase3"`` |
    ``"phase4"`` | ``"force"``), ``usage_pct``, and ``tokens_before``.
    """

    # ── Interrupt ────────────────────────────────────────────────────
    USER_INTERRUPT = "user_interrupt"
    """The user pressed interrupt mid-turn.  Payload: base envelope."""


GATED_EVENTS: Final = frozenset({HookEvent.BEFORE_TOOL_CALL, HookEvent.USER_PROMPT_SUBMIT})
"""The events a blocking hook can stop, by its decision or by ``on_error: block``."""

TOOL_EVENTS: Final = frozenset(
    {
        HookEvent.BEFORE_TOOL_CALL,
        HookEvent.AFTER_TOOL_CALL,
        HookEvent.TOOL_ERROR,
        HookEvent.APPROVAL_REQUESTED,
        HookEvent.APPROVAL_RESOLVED,
    }
)
"""The events whose payload carries a ``tool``, the only ones ``match.tool_kind``, ``tool_name`` and ``args`` match."""
