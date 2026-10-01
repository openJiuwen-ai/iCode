# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval middleware — gates tool execution with user approval."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    Event,
    InvocationToolCallArgsUpdated,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SHELL,
    KIND_SUB_AGENT,
    get_tool_kind,
)
from chrys.foundation.trajectory.context import side_call_scope
from chrys.foundation.trajectory.envelope import ActorRole
from chrys.kernel.middleware import FunctionMiddleware
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_MODIFIED_ARGS_KEY,
    _APPROVAL_REJECTED_KEY,
    _REJECTION_MESSAGE_KEY,
    _REJECTION_SOURCE_KEY,
    _SHORT_ID_LEN,
)
from chrys.service.agent_middleware.control.approval_hooks import await_approval_with_hooks
from chrys.service.agent_middleware.events.hook_dispatch import (
    apply_before_tool_hooks,
    get_call_id,
    get_tool_invocation_order,
)
from chrys.service.approval.arbitration import ApprovalDecisionArbiter, ApprovalJudgeInput
from chrys.service.approval.argument_snapshot import argument_snapshot
from chrys.service.approval.correlation import OneShotCorrelation
from chrys.service.approval.daa import canonical
from chrys.service.approval.daa_binding import DAABinding
from chrys.service.approval.policy import ApprovalMode
from chrys.service.approval.safety_classifier import (
    path_arg_may_access_sensitive_data,
    shell_arg_may_access_sensitive_data,
    shell_command_may_access_sensitive_data,
    target_may_access_sensitive_data,
)
from chrys.service.tools.file_approval import approved_file_targets
from chrys.service.trajectory.approvals import ApprovalDecider, ApprovalTrace
from chrys.service.trajectory.tools import tool_operation_id

_GITDIR_POINTER_PREFIX = "gitdir:"

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from chrys.foundation.events.bus import EventBus
    from chrys.kernel.middleware import FunctionInvocationContext
    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.approval.policy import ApprovalPolicy
    from chrys.service.approval.turn_context import TurnContextHolder
    from chrys.service.hooks.manager import HookManager


def _daa_argument_snapshot(arguments: object) -> str | None:
    """Unsupported identities use ordinary approval and cannot reuse a grant."""
    try:
        return canonical(arguments)
    except ValueError, TypeError, RecursionError:
        return None


def _path_is_at_or_under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + os.sep)


def _is_session_archive_read_path(file_path: str, resolved_roots: list[str], *, base_cwd: str | None = None) -> bool:
    """Check if *file_path* resolves inside a session-owned archive root.

    *resolved_roots* must already be resolved via ``os.path.realpath``. The
    candidate is fully resolved (symlinks included) before the containment
    check, so a link planted under the archive cannot smuggle an outside
    read through the auto-approval.
    """
    try:
        resolved = os.path.realpath(resolve_workspace_path(file_path, base_cwd=base_cwd))
    except OSError, ValueError:
        return False
    return any(_path_is_at_or_under(resolved, root) for root in resolved_roots)


def _gitdir_pointer_target(git_marker: str) -> str | None:
    """Return the resolved gitdir target for a worktree/submodule .git file."""
    if not os.path.isfile(git_marker):
        return None
    try:
        with open(git_marker, encoding="utf-8", errors="replace") as f:
            line = f.readline(4096).strip()
    except OSError:
        return None
    if not line.lower().startswith(_GITDIR_POINTER_PREFIX):
        return None

    target = line[len(_GITDIR_POINTER_PREFIX) :].strip()
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(git_marker), target)
    try:
        return os.path.realpath(target)
    except OSError, ValueError:
        return None


def _is_workspace_git_path(file_path: str, resolved_roots: list[str], *, base_cwd: str | None = None) -> bool:
    """Check if *file_path* is inside a workspace root AND under git version control.

    *resolved_roots* must already be resolved via ``os.path.realpath``.

    Returns ``True`` only when BOTH conditions hold:
    1. The resolved absolute path starts with one of the *resolved_roots*.
    2. A ``.git`` directory or file exists in the file's directory or any ancestor.
    Git metadata internals are excluded even when a worktree/submodule ``.git``
    pointer places the real gitdir under the workspace root by another name.
    """
    try:
        resolved = os.path.realpath(resolve_workspace_path(file_path, base_cwd=base_cwd))
    except OSError, ValueError:
        return False

    # Check if inside any workspace root (roots are pre-resolved)
    if not any(resolved == root or resolved.startswith(root + os.sep) for root in resolved_roots):
        return False

    # Walk up from file's directory to check for .git. Worktrees and submodules
    # commonly use a .git file containing a gitdir pointer instead of a directory.
    check_dir = os.path.dirname(resolved) if not os.path.isdir(resolved) else resolved
    while True:
        git_marker = os.path.join(check_dir, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            resolved_git_marker = os.path.realpath(git_marker)
            if _path_is_at_or_under(resolved, resolved_git_marker):
                return False
            gitdir_target = _gitdir_pointer_target(git_marker)
            return gitdir_target is None or not _path_is_at_or_under(resolved, gitdir_target)
        parent = os.path.dirname(check_dir)
        if parent == check_dir:
            break  # reached filesystem root
        check_dir = parent

    return False


def _decision(
    *,
    request_id: str,
    tool_name: str,
    status: str,
    call_id: str = "",
    tool_order: int | None = None,
) -> dict[str, str]:
    decision = {"request_id": request_id, "tool_name": tool_name, "status": status}
    if call_id:
        decision["call_id"] = call_id
    if tool_order is not None:
        # Approval decisions are typed as dict[str, str] for persistence handoff.
        decision["tool_order"] = str(tool_order)
    return decision


def _approval_level(*, policy: bool, dev_sub_agent_review: bool, sensitive: bool) -> str:
    """Why the dialog was shown, for ``approval.requested.approval_level``."""
    if policy:
        return "policy"
    if dev_sub_agent_review:
        return "dev_sub_agent_review"
    if sensitive:
        return "sensitive_target"
    return "unknown"


@dataclass(frozen=True, slots=True)
class ApprovalRetrySnapshot:
    """Approval decisions that predate one retryable Agent.run attempt."""

    decisions: tuple[dict[str, str], ...]


class InvocationPublisher(Protocol):
    """Bound execution identity supplied by the caller's invocation wiring."""

    @property
    def origin(self) -> InvocationOrigin: ...
    async def publish(self, event: Event) -> None: ...


class ApprovalMiddleware(FunctionMiddleware):
    """Gates tool execution with user approval inside the Chrys tool loop.

    Replaces the old snapshot-restore approval loop.  When a tool needs
    approval, the middleware pauses (``await`` on a future), publishes an
    ``ApprovalRequest`` event, and waits for the TUI to respond with an
    ``ApprovalResponse``.  Approved tools proceed to ``call_next()``; rejected
    tools get an error result returned to the LLM.

    Because the middleware runs *inside* the Chrys tool loop,
    ``agent.run()`` never exits early for approval — no re-submission,
    no snapshot/restore, no loop-message capture for approval iterations.
    """

    def __init__(
        self,
        approval_policy: ApprovalPolicy,
        event_bus: EventBus,
        session_id: str | None = None,
        tool_kinds: dict[str, str] | None = None,
        caller_name: str = "",
        workspace_roots: list[str] | None = None,
        approval_mode: ApprovalMode = ApprovalMode.MANUAL,
        approval_judge: ApprovalJudge | None = None,
        approval_log_dir: Path | None = None,
        workspace_cwd: str | None = None,
        dev_mode: bool = False,
        hook_manager: HookManager | None = None,
        profile_name: str = "",
        session_archive_read_roots: list[Path] | None = None,
        turn_context: TurnContextHolder | None = None,
        daa: DAABinding | None = None,
    ) -> None:
        from chrys.service.approval.turn_context import TurnContextHolder

        self._daa = daa
        self._policy = approval_policy
        self._publisher: InvocationPublisher | None = None
        self._bus = event_bus
        self._session_id = session_id
        self._tool_kinds: dict[str, str] = tool_kinds or {}
        self._caller_name = caller_name
        self._decisions: list[dict[str, str]] = []
        self._turn_context = turn_context or TurnContextHolder()
        self._approval_mode = approval_mode
        self._approval_judge = approval_judge
        self._approval_log_dir = approval_log_dir
        self._workspace_cwd = workspace_cwd
        self._dev_mode = dev_mode
        self._hook_manager = hook_manager
        self._profile_name = profile_name
        self._approval_arbiter = ApprovalDecisionArbiter(event_bus, session_id=session_id)
        # Keep the long-standing private test seams as aliases while the race
        # itself is owned by the shared arbiter.
        self._blocked_auto_fulfill_ids = self._approval_arbiter._blocked
        self._on_auto_fulfill_blocked = self._approval_arbiter._on_blocked
        # Pre-resolve workspace roots once (avoids repeated realpath per tool call)
        self._workspace_roots: list[str] = []
        for r in workspace_roots or []:
            with contextlib.suppress(OSError, ValueError):
                self._workspace_roots.append(os.path.realpath(os.path.abspath(r)))
        # Session-owned archive dirs (compaction spill records, superseded
        # LAST_WORDS notes) whose reads are always auto-approved: chrys wrote
        # those files from context the agent already saw.
        self._session_archive_read_roots: list[str] = []
        for root in session_archive_read_roots or []:
            with contextlib.suppress(OSError, ValueError):
                self._session_archive_read_roots.append(os.path.realpath(os.path.abspath(root)))

    def bind_publisher(self, publisher: InvocationPublisher | None) -> None:
        """Bind future tool callbacks; in-flight approvals retain their publisher."""
        self._publisher = publisher

    def set_user_message(self, text: str) -> None:
        """Store the current user message for inclusion in approval requests."""
        self.set_user_messages([text] if text else [])

    def set_user_messages(self, messages: list[str]) -> None:
        """Store current-turn user messages for inclusion in approval judge context."""
        self._turn_context.replace(messages)

    def append_user_message(self, text: str) -> None:
        """Append a current-turn user message for later approval judge context."""
        self._turn_context.append(text)

    def remove_user_message(self, text: str) -> None:
        """Remove the most recent occurrence of *text* from judge context.

        Used when a queued injection is cancelled after its text was already
        appended, so later approval requests do not cite withdrawn input.
        """
        self._turn_context.remove(text)

    @property
    def _user_message(self) -> str:
        return self._turn_context.user_message

    @property
    def _user_messages(self) -> list[str]:
        return self._turn_context.user_messages

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """Update the active approval mode.

        Only affects subsequent tool calls — in-flight requests keep the
        mode they were created with.
        """
        self._approval_mode = mode

    @property
    def approval_mode(self) -> ApprovalMode:
        return self._approval_mode

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        reapprove = False
        while retry := await self._process(context, call_next, reapprove=reapprove):
            reapprove = True
            if retry == "reapprove":
                # The UI-edit path already ran hooks on this request. A hook
                # rewrite needs confirmation, not a second transformation.
                continue
            # The upstream hook saw the old request. Check the changed request
            # before presenting it again, just as for an explicit UI edit.
            if await apply_before_tool_hooks(
                manager=self._hook_manager,
                context=context,
                session_id=self._session_id,
                profile_name=self._profile_name,
                tool_name=context.function.name,
                kind=get_tool_kind(context.function) or self._tool_kinds.get(context.function.name, ""),
                call_id=get_call_id(context),
                args=cast("dict[str, Any]", context.arguments),
                workspace_cwd=self._workspace_cwd or "",
                target_operation_id=tool_operation_id(context.metadata),
            ):
                return

    async def _process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
        *,
        reapprove: bool,
    ) -> Literal[False, "rehook", "reapprove"]:
        publisher = self._publisher
        tool_name = context.function.name
        call_id = get_call_id(context)
        tool_order = get_tool_invocation_order(context)

        tool_kind = get_tool_kind(context.function) or self._tool_kinds.get(tool_name, "")
        dev_sub_agent_review = (
            self._dev_mode and tool_kind == KIND_SUB_AGENT and self._approval_mode != ApprovalMode.BYPASS
        )
        sensitive_shell = tool_kind == KIND_SHELL and shell_arg_may_access_sensitive_data(
            context.arguments,
            shell_name=tool_name,
        )
        sensitive_filesystem_read = tool_kind == KIND_FILESYSTEM_READ and path_arg_may_access_sensitive_data(
            context.arguments
        )
        sensitive_filesystem_write = tool_kind == KIND_FILESYSTEM_WRITE and path_arg_may_access_sensitive_data(
            context.arguments
        )
        policy_requires_approval = self._policy.should_require_approval(tool_name, tool_kind)
        if (
            not reapprove
            and not policy_requires_approval
            and not dev_sub_agent_review
            and not sensitive_shell
            and not sensitive_filesystem_read
            and not sensitive_filesystem_write
        ):
            await call_next()
            return False

        # Auto-approve safe read-only shell commands (e.g. ls, cat, grep)
        # without showing the approval dialog.
        if tool_kind == KIND_SHELL and not reapprove:
            raw = context.arguments
            cmd = raw.get("command", "") if isinstance(raw, dict) else ""
            if cmd:
                from chrys.service.tools.builtins.shell_filter import is_safe_readonly_command

                if is_safe_readonly_command(cmd, shell_name=tool_name) and not shell_command_may_access_sensitive_data(
                    cmd, shell_name=tool_name
                ):
                    self._decisions.append(
                        _decision(
                            request_id="",
                            tool_name=tool_name,
                            status="auto_approved",
                            call_id=call_id,
                            tool_order=tool_order,
                        )
                    )
                    await call_next()
                    return False

        # Auto-approve file writes inside workspace git repos.
        if tool_kind == KIND_FILESYSTEM_WRITE and self._workspace_roots and not reapprove:
            raw = context.arguments
            file_path = raw.get("path", "") if isinstance(raw, dict) else ""
            if (
                file_path
                and _is_workspace_git_path(file_path, self._workspace_roots, base_cwd=self._workspace_cwd)
                and not target_may_access_sensitive_data(file_path)
            ):
                self._decisions.append(
                    _decision(
                        request_id="",
                        tool_name=tool_name,
                        status="auto_approved",
                        call_id=call_id,
                        tool_order=tool_order,
                    )
                )
                await call_next()
                return False

        # Auto-approve reads of session-owned compaction archives (spilled
        # tool records, superseded LAST_WORDS notes): the agent is reading
        # back content chrys itself archived from the conversation, so the
        # dialog would gate nothing — even under a ``require`` rule or a
        # sensitive-looking archived tool name in the filename.
        if tool_kind == KIND_FILESYSTEM_READ and self._session_archive_read_roots and not reapprove:
            raw = context.arguments
            file_path = raw.get("path", "") if isinstance(raw, dict) else ""
            if (
                isinstance(file_path, str)
                and file_path
                and _is_session_archive_read_path(
                    file_path,
                    self._session_archive_read_roots,
                    base_cwd=self._workspace_cwd,
                )
            ):
                self._decisions.append(
                    _decision(
                        request_id="",
                        tool_name=tool_name,
                        status="auto_approved",
                        call_id=call_id,
                        tool_order=tool_order,
                    )
                )
                await call_next()
                return False

        # BYPASS — silently auto-approve without ever publishing a request.
        if self._approval_mode == ApprovalMode.BYPASS and not reapprove:
            self._decisions.append(
                _decision(
                    request_id="",
                    tool_name=tool_name,
                    status="bypass_approved",
                    call_id=call_id,
                    tool_order=tool_order,
                )
            )
            await call_next()
            return False

        # Tool needs approval — parse args and publish request.
        raw_args = context.arguments
        if isinstance(raw_args, dict):
            parsed_args = raw_args
        elif isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError, TypeError:
                parsed_args = {}
        else:
            # Pydantic model or other — try dict conversion
            parsed_args = dict(raw_args) if raw_args else {}
        # Kernel tool-call validation guarantees object-shaped, string-keyed
        # arguments before this middleware runs; retain the tolerant parsing
        # branches above without copying or coercing their result.
        parsed_args = cast("dict[str, Any]", parsed_args)

        if self._daa is not None:
            # DAA-only confirmation snapshot, never a future reuse key. Keep
            # ordinary approval free of serialization and tool identity checks.
            confirmed_arguments = argument_snapshot(context.arguments)
            file_targets = self._daa.file_targets
            confirmed_file_targets = file_targets(context)
            parsed_args = deepcopy(parsed_args)
            confirmed_function = (context.function, context.function.name, context.function.func)

            # Copy structured values, keeping opaque host handles (including the
            # kernel session) by identity instead of manufacturing unequal clones.
            def snapshot_kwargs(value: Any) -> Any:
                if isinstance(value, dict):
                    return {key: snapshot_kwargs(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [snapshot_kwargs(item) for item in value]
                if isinstance(value, tuple):
                    return tuple(snapshot_kwargs(item) for item in value)
                return value

            confirmed_kwargs = snapshot_kwargs(context.kwargs)

            def request_identity_changed() -> bool:
                return (
                    context.function,
                    context.function.name,
                    context.function.func,
                ) != confirmed_function or context.kwargs != confirmed_kwargs

            def request_changed() -> bool:
                return (
                    argument_snapshot(context.arguments) != confirmed_arguments
                    or file_targets(context) != confirmed_file_targets
                    or request_identity_changed()
                )

        daa_candidate = None
        if self._daa is not None and _daa_argument_snapshot(context.arguments) is not None:
            daa_candidate = self._daa.candidate(
                context,
                non_reusable=dev_sub_agent_review
                or sensitive_shell
                or sensitive_filesystem_read
                or sensitive_filesystem_write,
            )
            if not reapprove and daa_candidate is not None:
                matched = await asyncio.to_thread(self._daa.service.match, daa_candidate)
                # A worker lookup yields the event loop. The grant must still
                # describe the live request when execution resumes.
                if request_changed():
                    return "rehook"
            else:
                matched = "MISS"
            if matched == "HIT_ALLOW":
                self._decisions.append(
                    _decision(
                        request_id="",
                        tool_name=tool_name,
                        status="daa_approved",
                        call_id=call_id,
                        tool_order=tool_order,
                    )
                )
                with approved_file_targets(confirmed_file_targets):
                    await call_next()
                return False

        request_id = uuid4().hex[:_SHORT_ID_LEN]
        decision = _decision(
            request_id=request_id,
            tool_name=tool_name,
            status="approval_pending",
            call_id=call_id,
            tool_order=tool_order,
        )
        # Keep this dict in _decisions and mutate it after the approval
        # response so list order stays tied to invocation order, not response
        # arrival order.
        self._decisions.append(decision)

        judge_task: asyncio.Task[None] | None = None
        approval_trace = None
        try:
            # Subscribe BEFORE publishing (a frontend may answer synchronously)
            # and hold the subscription until the decision is in, whatever
            # interrupts the wait.
            async with OneShotCorrelation(
                self._bus,
                ApprovalResponse,
                request_id=request_id,
                snapshot=deepcopy if self._daa is not None else None,
            ) as correlation:
                future = correlation.future
                try:
                    judging = (
                        self._approval_mode == ApprovalMode.AUTO
                        and self._approval_judge is not None
                        and not dev_sub_agent_review
                        and not reapprove
                    )
                    if judging:
                        # Frontends may synchronously block auto-fulfilment while handling
                        # ApprovalRequest, so install the shared arbitration subscription
                        # before publishing the request.
                        await self._ensure_auto_fulfill_block_subscription()

                    approval_trace = ApprovalTrace.open(context.metadata)

                    await self._bus.publish(
                        ApprovalRequest(
                            request_id=request_id,
                            call_id=call_id,
                            tool_name=tool_name,
                            tool_kind=tool_kind,
                            args=deepcopy(parsed_args) if self._daa is not None else parsed_args,
                            # intent_summary stays empty: the middleware has no real intent
                            # to report, and a fabricated "Execute {tool_name}" placeholder
                            # would win over the informative title fallbacks downstream
                            # (the ACP server titles shell calls by their command).
                            session_id=self._session_id,
                            caller_name=self._caller_name,
                            user_message=self._user_message,
                            workspace_roots=list(self._workspace_roots),
                            workspace_cwd=self._workspace_cwd or "",
                            judging=judging,
                            daa_exact=daa_candidate.display if daa_candidate is not None else "",
                            daa_prefix=daa_candidate.prefix or () if daa_candidate is not None else (),
                        )
                    )

                    if judging:
                        # The judge's model call is a side call of this session: rebind
                        # the ambient trajectory actor so its exchange is attributed to
                        # the judge, never to the main agent.
                        with side_call_scope(ActorRole.APPROVAL_JUDGE):
                            judge_task = asyncio.create_task(
                                self._approval_arbiter.judge(
                                    request_id=request_id,
                                    judge=self._approval_judge,
                                    judge_input=ApprovalJudgeInput(
                                        user_message=self._turn_context.user_message,
                                        user_messages=self._turn_context.user_messages,
                                        tool_name=tool_name,
                                        tool_kind=tool_kind,
                                        args=parsed_args,
                                        workspace_roots=list(self._workspace_roots),
                                    ),
                                    decision_future=future,
                                    approved_value=ApprovalResponse(
                                        request_id=request_id,
                                        approved=True,
                                        session_id=self._session_id,
                                    ),
                                    log_dir=self._approval_log_dir,
                                )
                            )

                    # Inside the block that lets the request go: the marker awaits
                    # its write ack, and an interrupt landing there would otherwise
                    # leave this request's handler subscribed and its judge still
                    # calling the model. The judge task cannot run before this
                    # either, so its own events still follow the request.
                    if approval_trace is not None:
                        await approval_trace.requested(
                            tool_name=tool_name,
                            approval_mode=self._approval_mode.value,
                            approval_level=_approval_level(
                                policy=policy_requires_approval,
                                dev_sub_agent_review=dev_sub_agent_review,
                                sensitive=sensitive_shell or sensitive_filesystem_read or sensitive_filesystem_write,
                            ),
                        )
                    response = await await_approval_with_hooks(
                        future=future,
                        judge_task=judge_task,
                        manager=self._hook_manager,
                        session_id=self._session_id,
                        profile_name=self._profile_name,
                        workspace_cwd=self._workspace_cwd or "",
                        caller_name=self._caller_name,
                        request_id=request_id,
                        tool_name=tool_name,
                        tool_kind=tool_kind,
                        call_id=call_id,
                        args=parsed_args,
                        target_operation_id=approval_trace.request_id if approval_trace is not None else None,
                    )
                finally:
                    # Cancel the judge if the user decided first (or reason is in — the
                    # judge still may have published a verdict that raced in; the dialog
                    # is already dismissed by then so the TUI drops it).
                    if judge_task is not None and not judge_task.done():
                        judge_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await judge_task

            user_decided = correlation.resolved_by_event
            approved = response.approved
            daa_choice = response.daa_choice if approved else ""
            reason = response.reason.strip()
            modified_args = response.modified_args
            status = "user_approved" if approved else "user_rejected"
            decision["status"] = status
            if reason:
                decision["reason"] = reason
            else:
                decision.pop("reason", None)
            if approval_trace is not None:
                await approval_trace.resolved(
                    approved=approved,
                    decider=ApprovalDecider.USER if correlation.resolved_by_event else ApprovalDecider.JUDGE,
                    reason_code="user_reason" if reason else "",
                    arguments_modified=bool(modified_args),
                )
        except BaseException:
            # Interrupted (or failed) while the dialog was still open: the
            # request is abandoned, and only this path can say so.
            if approval_trace is not None:
                approval_trace.interrupted_soon()
            raise

        if approved:
            if self._daa is not None and request_changed():
                # Only the confirmation of the actual request belongs in the
                # persisted tool decision; a stale approval must not win by ID.
                self._decisions.remove(decision)
                return "rehook"
            if modified_args:
                context.arguments = {**parsed_args, **modified_args}
                if self._daa is not None:
                    confirmed_arguments = argument_snapshot(context.arguments)
                    confirmed_file_targets = self._daa.file_targets(context)
                # Re-dispatch ``before_tool_call`` hooks with the edited args.
                # ``ToolEventMiddleware`` already fired hooks once with the
                # original args before approval ran; without this second pass,
                # a hook that gates on ``args.prompt`` would approve one prompt
                # and let the user's edited prompt execute unchecked.
                hook_blocked = await apply_before_tool_hooks(
                    manager=self._hook_manager,
                    context=context,
                    session_id=self._session_id,
                    profile_name=self._profile_name,
                    tool_name=tool_name,
                    kind=tool_kind,
                    call_id=call_id,
                    args=context.arguments,
                    workspace_cwd=self._workspace_cwd or "",
                    # Same target as the first dispatch: a hook that gates the
                    # edited arguments belongs to the tool it can still block,
                    # not to the exchange the call came in on.
                    target_operation_id=tool_operation_id(context.metadata),
                )
                # Hooks may have rewritten args further — capture the final form.
                hooked_arguments = argument_snapshot(context.arguments) if self._daa is not None else None
                hooked_file_targets = self._daa.file_targets(context) if self._daa is not None else None
                final_args = context.arguments if isinstance(context.arguments, dict) else modified_args
                context.metadata[_APPROVAL_MODIFIED_ARGS_KEY] = final_args
                if call_id and isinstance(final_args, dict):
                    if publisher is None:
                        raise ValueError("Cannot route an unbound approval callback")
                    await publisher.publish(
                        InvocationToolCallArgsUpdated(
                            origin=publisher.origin,
                            agent_name=self._caller_name,
                            tool_name=tool_name,
                            tool_kind=tool_kind,
                            call_id=call_id,
                            args=final_args,
                            session_id=self._session_id,
                        )
                    )
                try:
                    decision["modified_args"] = json.dumps(final_args, ensure_ascii=False)
                except TypeError:
                    decision["modified_args"] = json.dumps(modified_args, ensure_ascii=False, default=str)
                if hook_blocked:
                    # Hook intercepted the edited args. ``apply_before_tool_hooks``
                    # already set ``context.result`` and ``_APPROVAL_REJECTED_KEY``;
                    # skip ``call_next`` so the tool doesn't run.
                    return False
                if self._daa is not None and request_changed():
                    self._decisions.remove(decision)
                    if (
                        request_identity_changed()
                        or argument_snapshot(context.arguments) != hooked_arguments
                        or self._daa.file_targets(context) != hooked_file_targets
                    ):
                        # A later event handler changed the request again; that
                        # new request has not passed before_tool_call hooks.
                        return "rehook"
                    return "reapprove"
            if (
                user_decided
                and daa_choice
                and daa_candidate is not None
                and self._daa is not None
                and not modified_args
            ):
                # UI edits are explicit one-time approvals; they never save the
                # original candidate selected before editing.
                await asyncio.to_thread(self._daa.service.remember, daa_candidate, daa_choice)
                if request_changed():
                    self._decisions.remove(decision)
                    return "rehook"
            with approved_file_targets(confirmed_file_targets if self._daa is not None else None):
                await call_next()
        else:
            # Return error result — LLM sees rejection and can respond naturally.
            # Set metadata flag so ToolEventMiddleware can detect rejection
            # without string matching (decoupled from the user-facing message).
            context.metadata[_APPROVAL_REJECTED_KEY] = True
            message = "Tool execution was rejected by user."
            if reason:
                message = f"{message} User reason: {reason}"
            context.metadata[_REJECTION_SOURCE_KEY] = "user"
            context.metadata[_REJECTION_MESSAGE_KEY] = message
            context.result = "Error: Tool execution was rejected by user."
            if reason:
                context.result = f"{context.result}\nUser reason: {reason}"
        return False

    async def _ensure_auto_fulfill_block_subscription(self) -> None:
        """Subscribe once to frontend blocks for judge auto-fulfilment."""
        await self._approval_arbiter.ensure_subscription()

    async def close(self) -> None:
        """Release EventBus subscriptions owned by this middleware."""
        await self._approval_arbiter.close()

    def drain_decisions(self) -> list[dict[str, str]]:
        """Return and clear collected approval decisions."""
        decisions = list(self._decisions)
        self._decisions.clear()
        return decisions

    def snapshot_retry_state(self) -> ApprovalRetrySnapshot:
        """Capture completed decisions that must survive a later retry."""
        return ApprovalRetrySnapshot(tuple(dict(decision) for decision in self._decisions))

    def restore_retry_state(self, snapshot: ApprovalRetrySnapshot) -> None:
        """Drop failed-attempt decisions while preserving the prior baseline."""
        self._decisions = [dict(decision) for decision in snapshot.decisions]

    def reset(self) -> None:
        """Clear per-run state."""
        self._decisions.clear()
        # Keep the EventBus subscription alive across runs; close() owns resource teardown.
        self._approval_arbiter.clear()
        self._turn_context.reset()
