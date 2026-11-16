# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dataclass schema for the on-disk hooks config.

The on-disk shape is::

    version: 1
    settings:
      shutdown_grace_seconds: 5
      max_parallel_hooks: 4
    hooks:
      - id: ...
        event: before_tool_call
        enabled: true
        match: { ... }
        run: { type: script, path: ..., ... }
        execution: { mode: blocking, timeout_seconds: 10, ... }

YAML and JSON parse to the same intermediate dict; loaders in
:mod:`chrys.service.hooks.loader` convert that dict into these dataclasses and
validate combinations (e.g. ``mode: blocking`` + ``detach: true`` is
rejected at load time).

These classes are deliberately validation-only: they carry no runtime
behaviour.  :mod:`chrys.service.hooks.runner` and :mod:`chrys.service.hooks.manager`
operate on instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from chrys.foundation.models.hook_confirmations import HookConfirmationOutcome
from chrys.service.hooks.events import HookEvent

# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


@dataclass
class HookArgMatch:
    """Operator-tagged matcher for a single tool-call argument.

    All three fields are optional and combine with AND.  ``regex`` is
    compiled lazily by :mod:`chrys.service.hooks.matcher` and cached on the
    instance.
    """

    equals: str | None = None
    contains: str | None = None
    regex: str | None = None
    _compiled_regex: Any = field(default=None, init=False, repr=False, compare=False)
    _regex_compiled: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass
class HookMatch:
    """Optional matcher clause.

    All set clauses combine with AND.  ``profiles`` is the any-of form;
    ``profile`` is the singular form, kept because it reads better when
    you only care about one profile.

    Tool clauses require a tool payload.  If ``tool_kind``, ``tool_name``,
    or ``args`` is set on an event that does not carry ``tool``, the hook
    does not match.
    """

    profile: str | None = None
    profiles: list[str] = field(default_factory=list)
    tool_kind: str | None = None
    tool_name: str | None = None
    args: dict[str, HookArgMatch] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Run / Execution
# ---------------------------------------------------------------------------

HookRunType = Literal["script", "command", "shell"]
"""``script``: file path, interpreter inferred from extension (matches
:class:`chrys.service.skills.runner.SubprocessScriptRunner`).
``command``: explicit argv list, no shell.
``shell``: a single shell-syntax string, executed via the user's shell
(intended for one-liners; use sparingly — argv is safer).
"""

HookExecutionMode = Literal["blocking", "async", "fire_and_forget"]
"""See :mod:`chrys.service.hooks.manager` for the wait semantics each mode
implies.  Combined with :attr:`HookExecution.detach` and
:attr:`HookExecution.delivery` to express the full "must this survive
chrys exit / must we never lose it" matrix.
"""

HookDelivery = Literal["best_effort", "durable"]
"""``durable`` writes a pending-job file to the outbox before spawning
the subprocess; the runner moves it to ``done/`` on success or
``failed/`` on non-zero exit.  On chrys startup the manager scans
``pending/`` and retries stale entries.  ``best_effort`` skips the
outbox dance entirely.  Blocking hooks are awaited inline and therefore
do not use the outbox even when configured as ``durable``.
"""

HookErrorPolicy = Literal["block", "warn", "ignore"]
"""``block``: a non-zero exit (or timeout) on a ``blocking`` hook denies
a gated event and surfaces the hook's stderr.  ``warn``: log a warning,
allow the action.  ``ignore``: log at debug only.  For non-blocking
hooks and non-gated events, ``block`` falls back to ``warn`` — there's
nothing to gate.
"""


@dataclass
class HookRun:
    """What to run and where."""

    type: HookRunType
    """``script`` | ``command`` | ``shell``."""

    path: str = ""
    """Path to the script file.  Required when ``type == 'script'``.

    When loaded from disk, relative paths are resolved against the
    directory containing the hooks file, so both global and project
    hook files can keep scripts next to ``hooks.yaml``. Interpreter is
    inferred from the extension (see ``chrys/service/skills/runner.py``).
    """

    argv: list[str] = field(default_factory=list)
    """Explicit argv.  Required when ``type == 'command'``."""

    shell: str = ""
    """Single shell-syntax string.  Required when ``type == 'shell'``."""

    args: list[str] = field(default_factory=list)
    """Extra positional arguments appended after the interpreter +
    script path (``type: script``) or after ``argv`` (``type: command``).
    Ignored for ``type: shell`` — the string is opaque to chrys.
    """

    env: dict[str, str] = field(default_factory=dict)
    """Extra env vars; merged over Chrys's sanitized parent process env."""

    cwd: str = ""
    """Working directory.  Supports template vars: ``${workspace_cwd}``,
    ``${chrys_home}``, ``${session_id}``, ``${profile}``.  Empty falls
    back to the workspace cwd.
    """


@dataclass
class HookExecution:
    """Wait / lifetime / delivery semantics for this hook."""

    mode: HookExecutionMode = "fire_and_forget"
    """How chrys waits for the subprocess.  See :data:`HookExecutionMode`."""

    detach: bool = False
    """Detach the subprocess from chrys's process group (POSIX:
    ``setsid``; Windows: ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS``)
    so it survives chrys exit.  Stdio is redirected to a log file under
    ``<config_dir>/hooks/logs/``.

    Only meaningful with ``mode: fire_and_forget`` — the load-time
    validator rejects ``detach: true`` paired with ``blocking`` or
    ``async`` (chrys would deadlock waiting on a process it can't read).
    """

    delivery: HookDelivery = "best_effort"
    """``durable`` writes a job file to the outbox before spawning, so a
    crash between spawn and completion lets the next chrys startup
    retry.  Requires hooks to be idempotent.  Applies only to
    non-blocking hook modes; blocking hooks are already fully observed by
    the caller.
    """

    timeout_seconds: float = 30.0
    """Hard timeout for ``blocking``, ``async``, and non-detached
    ``fire_and_forget`` modes.  Ignored for detached hooks because the
    detached worker owns the child after Chrys exits.
    """

    on_error: HookErrorPolicy = "warn"
    """What to do when the hook fails (non-zero exit, timeout, or — on a gating
    event — a result file iCode cannot act on).  See :data:`HookErrorPolicy`.
    """


# ---------------------------------------------------------------------------
# Top-level hook config
# ---------------------------------------------------------------------------


@dataclass
class HookConfig:
    """A single hook entry from ``hooks.yaml``."""

    id: str
    """Stable identifier used in logs and durable outbox job metadata.

    IDs must be unique within one file. Same-id hooks across project and
    global files are allowed by the merged loader and both hooks run.
    """

    event: HookEvent
    """Which lifecycle point triggers this hook."""

    run: HookRun
    """What to execute."""

    execution: HookExecution = field(default_factory=HookExecution)
    """Wait / lifetime / delivery semantics."""

    match: HookMatch = field(default_factory=HookMatch)
    """Filter — omit to fire on every event of this type."""

    enabled: bool = True
    """Set to ``false`` to keep the entry around without firing it.
    Cheaper than deleting + re-adding.
    """

    description: str = ""
    """Free-form description.  Surfaced in the future ``/hooks`` TUI
    screen; ignored at runtime.
    """


# ---------------------------------------------------------------------------
# Top-level settings + file
# ---------------------------------------------------------------------------


@dataclass
class HookSettings:
    """File-level knobs (apply to all hooks in the same file)."""

    shutdown_grace_seconds: float = 5.0
    """Upper bound on how long :meth:`HookManager.drain_session` waits
    for in-flight ``async`` hooks at shutdown.  ``fire_and_forget`` and
    detached hooks are never waited on.
    """

    max_parallel_hooks: int = 4
    """Cap on how many subprocesses the manager will keep alive at once.
    Additional dispatches queue until a slot frees.  Detached hooks
    don't count toward the cap.
    """

    outbox_retry_age_seconds: float = 60.0
    """On startup, ``pending/`` entries older than this are eligible for
    retry.  Younger entries are left alone (assumed to be in-flight from
    another chrys instance — unlikely but cheap to handle).
    """

    outbox_max_retries: int = 3
    """Each pending entry tracks its retry count; entries that hit this
    cap are moved straight to ``failed/`` on the next startup.
    """


@dataclass
class HooksFile:
    """In-memory representation of a parsed ``hooks.yaml`` / ``hooks.json``."""

    version: int = 1
    settings: HookSettings = field(default_factory=HookSettings)
    settings_overrides: set[str] = field(default_factory=set)
    """Names of settings fields explicitly present in the source file."""
    hooks: list[HookConfig] = field(default_factory=list)
    skipped_hooks: list[SkippedHook] = field(default_factory=list)
    """Hooks left out of :attr:`hooks` because their configuration can never
    work; the rest of the file still loads.  Reported to the user at session
    start.
    """
    source: str = ""
    """Path the file was loaded from; empty for synthetic configs.  Used
    in log messages and the future ``/hooks`` screen.
    """


@dataclass(frozen=True)
class SkippedHook:
    """A hook the loader left out, and why (English, names the offending field)."""

    id: str
    reason: str


# ---------------------------------------------------------------------------
# Runtime decision
# ---------------------------------------------------------------------------


@dataclass
class HookDecision:
    """Aggregated outcome of all blocking hooks for a single event.

    Returned by :meth:`HookManager.fire`.  The caller (middleware /
    engine) inspects :attr:`blocked` first; if false, applies
    :attr:`args_override`, then queues :attr:`system_reminders` and
    :attr:`extra_context` for downstream consumption.

    Each caller applies only the fields meaningful for its event.  For
    example, ``after_tool_call`` consumes :attr:`extra_context` but ignores
    :attr:`blocked`, while pure observer events such as ``session_end``
    ignore the return value entirely.
    """

    blocked: bool = False
    """A blocking hook returned ``action: block`` for a gated event.  The
    caller should short-circuit and report :attr:`block_reason` to the model
    / user.
    """

    block_reason: str = ""
    """Reason text from the first hook that blocked, with the hook id
    prefixed for traceability.
    """

    args_override: dict[str, Any] | None = None
    """When set, merges over ``context.arguments`` before the inner tool
    runs.  Multiple blocking hooks with ``action: modify`` merge
    left-to-right (later hook wins on conflict); whoever wrote the file
    is on the hook to order them deliberately.  Non-blocking hook
    decisions are ignored because their subprocess may finish after the
    gated action has already continued.
    """

    system_reminders: list[str] = field(default_factory=list)
    """Text to inject as ``<system-reminder>`` blocks.  Used by
    ``user_prompt_submit`` hooks.
    """

    extra_context: list[str] = field(default_factory=list)
    """Text to append to textual tool results (``after_tool_call``) or
    surface to the user.  Kept distinct from :attr:`system_reminders`
    because the consumers are different.
    """

    result_override: str = ""
    """Replaces the text of a textual tool result (``after_tool_call``,
    ``tool_error``) before the model reads it.  The last hook that sets it
    wins; a result with images keeps them, a non-text result is left as it is.
    """

    confirmation: HookConfirmationOutcome | None = None
    """Set when the block came from an ``ask``, not an ``action: block``: how
    that ask ended (``declined``, ``timed_out`` or ``unavailable``)."""

    invalidated: bool = False
    """The operation the hooks checked ended before they finished (its owner
    invalidated it, or the manager closed): drop it the way its owner drops a
    stale operation. Never set together with :attr:`blocked`."""


# ---------------------------------------------------------------------------
# Multi-source merge
# ---------------------------------------------------------------------------


@dataclass
class MergedHooksFile:
    """Combined view of a project-level and a global-level :class:`HooksFile`.

    Produced by :func:`chrys.service.hooks.loader.merge_hooks_files`. Exposes the
    same ``hooks`` and ``settings`` attributes as :class:`HooksFile` so the
    existing :class:`HookManager` consumes it without code changes (duck
    typing). The ``project`` and ``global_`` fields are kept for logging,
    debugging, and the future ``/hooks`` TUI screen.

    Ordering: ``hooks`` is project entries first (in project YAML order),
    then global entries (in global YAML order). The "first block
    short-circuits" rule applies to this combined list as a single chain,
    so a project block ends the chain before any global hook runs.
    """

    project: HooksFile | None = None
    """The project-side :class:`HooksFile`, or ``None`` if no project file
    was found or it failed to load."""

    global_: HooksFile | None = None
    """The global-side :class:`HooksFile`, or ``None`` if no global file
    was found (the existing single-source case)."""

    settings: HookSettings = field(default_factory=HookSettings)
    """Per-field merge: project wins, then global, then defaults."""

    hooks: list[HookConfig] = field(default_factory=list)
    """Combined hook list, project first then global. Empty when both
    sources are absent."""

    sources: list[str] = field(default_factory=list)
    """Source paths included in this merge, in order. Empty when neither
    source was found. Used for log lines and the TUI screen; not used
    for any decision logic."""

    @classmethod
    def from_single(cls, file: HooksFile, *, as_project: bool = False) -> MergedHooksFile:
        """Wrap a single :class:`HooksFile` as a :class:`MergedHooksFile`.

        Used by the manager to normalize the single-source case (legacy
        global-only setups) into the merged view, so the manager never
        has to branch on input type. ``as_project=True`` treats the file
        as the project side; default treats it as the global side.
        """
        wrapped: HooksFile | None = file
        return cls(
            project=wrapped if as_project else None,
            global_=None if as_project else wrapped,
            settings=file.settings,
            hooks=list(file.hooks),
            sources=[file.source] if file.source else [],
        )

    @property
    def source(self) -> str:
        """Synthetic source label for compatibility with the manager's
        logging helpers. Format: ``"<project> + <global>"``, ``"<project>"``,
        ``"<global>"``, or ``""`` when neither source is present."""
        parts = [p.source for p in (self.project, self.global_) if p is not None and p.source]
        return " + ".join(parts)
