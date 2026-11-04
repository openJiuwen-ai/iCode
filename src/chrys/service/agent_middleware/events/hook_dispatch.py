# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hook helpers for tool middleware.

The hook dispatch is deliberately owned by ``ToolEventMiddleware`` and
``SubAgentEventMiddleware`` instead of a standalone ``FunctionMiddleware``.
Those event middlewares own the Chrys call id, file-lock selection, mutation
tracking, and UI tool events; running ``before_tool_call`` inside them ensures
hook-modified arguments become the single source of truth for all downstream
systems.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeGuard

from chrys.foundation.tool_invocation_order import read_tool_invocation_order
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_MODIFIED_ARGS_KEY,
    _APPROVAL_REJECTED_KEY,
    _CALL_ID_KEY,
    _HOOK_CONFIRMATION_KEY,
    _HOOK_OPERATION_KEY,
    _REJECTION_MESSAGE_KEY,
    _REJECTION_SOURCE_KEY,
    _TOOL_INVOCATION_ORDER_KEY,
)
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import check_operation
from chrys.service.hooks.operation import HookOperation
from chrys.service.hooks.schema import HookDecision

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chrys.foundation.models.invocations import InvocationOrigin
    from chrys.kernel.middleware import FunctionInvocationContext
    from chrys.service.hooks.manager import HookManager


def is_mutable_tool_args(value: object) -> TypeGuard[dict[str, Any]]:
    """Narrow validated tool argument dictionaries."""
    return isinstance(value, dict)


def tool_may_have_run(context: FunctionInvocationContext) -> bool:
    """Whether the call's tool body may have started: the commit step took its operation, or it has none.

    The commit step is the last one before the tool runs, so an operation that
    is still pending or was invalidated proves the tool never started.
    """
    metadata = context.metadata if isinstance(context.metadata, dict) else None
    operation = metadata.get(_HOOK_OPERATION_KEY) if metadata is not None else None
    return not isinstance(operation, HookOperation) or operation.state == "committed"


async def apply_before_tool_hooks(
    *,
    manager: HookManager | None,
    context: FunctionInvocationContext,
    session_id: str | None,
    profile_name: str,
    tool_name: str,
    kind: str,
    call_id: str,
    args: dict[str, Any],
    workspace_cwd: str,
    target_operation_id: str | None = None,
    origin: InvocationOrigin | None = None,
) -> bool:
    """Check the call's current arguments with ``before_tool_call`` hooks; True when it must not run.

    The first check of a call makes its ``HookOperation`` (kept in its
    metadata for the commit middleware); a later one (an approval edit)
    makes a new version of the same operation and checks it from the first
    hook. Every call is checked this way, hooks configured or not. On a pass
    ``context.arguments`` becomes the version the hooks checked. When the
    call must not run, ``context.result`` says why, and the caller should
    still publish the normal tool start/result events with the current
    arguments so the UI and persisted history reflect the denied call.
    *origin* names the sub-agent that calls the tool; None for the main agent.
    """
    metadata = context.metadata if isinstance(context.metadata, dict) else None
    existing = metadata.get(_HOOK_OPERATION_KEY) if metadata is not None else None
    if isinstance(existing, HookOperation):
        operation = existing
        operation.replace_arguments(args)
    else:
        operation = HookOperation(
            subject="tool",
            session_id=session_id,
            origin=origin,
            caller_name=profile_name,
            tool_name=tool_name,
            tool_kind=kind,
            arguments=args,
        )
        if metadata is not None:
            metadata[_HOOK_OPERATION_KEY] = operation

    decision = await check_operation(
        manager,
        HookEvent.BEFORE_TOOL_CALL,
        operation,
        lambda: _build_payload(
            session_id=session_id,
            profile_name=profile_name,
            tool_name=tool_name,
            kind=kind,
            call_id=call_id,
            args=operation.arguments(),
            workspace_cwd=workspace_cwd,
        ),
        target_operation_id=target_operation_id,
        origin=origin,
    )
    if decision.blocked or decision.invalidated:
        if decision.blocked:
            message = decision.block_reason or "Tool execution was blocked by hook."
        else:
            message = f"Tool call was dropped before it ran ({operation.end_reason or 'invalidated'})."
        reject_tool_call(context, message, confirmation=decision.confirmation or "")
        return True

    # Only a version the hooks changed is handed back: the call's own arguments, when they are
    # the checked version, keep their objects (a copy of an object without a stable text form
    # would no longer match).
    if is_mutable_tool_args(context.arguments) and not operation.matches(context.arguments):
        checked = operation.arguments()
        context.arguments.clear()
        context.arguments.update(checked)
    return False


def reject_tool_call(context: FunctionInvocationContext, message: str, *, confirmation: str = "") -> None:
    """Stop a call on the hooks' behalf: the model reads ``Error: <message>``, after-tool hooks see a hook denial."""
    if isinstance(context.metadata, dict):
        context.metadata[_APPROVAL_REJECTED_KEY] = True
        context.metadata[_REJECTION_SOURCE_KEY] = "hook"
        context.metadata[_REJECTION_MESSAGE_KEY] = message
        if confirmation:
            context.metadata[_HOOK_CONFIRMATION_KEY] = confirmation
    context.result = f"Error: {message}"


async def fire_after_tool_hooks(
    *,
    manager: HookManager | None,
    session_id: str | None,
    profile_name: str,
    tool_name: str,
    kind: str,
    call_id: str,
    args: dict[str, Any],
    result_text: str,
    duration_ms: int,
    errored: bool,
    failed: bool,
    approval_rejected: bool,
    rejection_source: str,
    workspace_cwd: str,
    hook_confirmation: str = "",
    target_operation_id: str | None = None,
    origin: InvocationOrigin | None = None,
) -> HookDecision:
    """Fire ``after_tool_call`` / ``tool_error`` hooks and return their decision.

    *origin* names the sub-agent that called the tool; None for the main agent.
    """
    if manager is None:
        return HookDecision()
    result = {
        "text": result_text,
        "duration_ms": duration_ms,
        "error": errored,
        "failed": failed,
        "approval_rejected": approval_rejected,
    }
    if rejection_source:
        result["rejection_source"] = rejection_source
        result["hook_denied"] = rejection_source == "hook"
        if hook_confirmation:
            # Denied through an ``ask`` that did not end in the user continuing, not an ``action: block``.
            result["hook_confirmation"] = hook_confirmation
    payload = _build_payload(
        session_id=session_id,
        profile_name=profile_name,
        tool_name=tool_name,
        kind=kind,
        call_id=call_id,
        args=args,
        workspace_cwd=workspace_cwd,
    )
    payload["result"] = result

    decision = HookDecision()
    if manager.has_hooks_for(HookEvent.AFTER_TOOL_CALL):
        _merge_decision(
            decision,
            await manager.fire(
                HookEvent.AFTER_TOOL_CALL, payload, target_operation_id=target_operation_id, origin=origin
            ),
        )
    if errored and manager.has_hooks_for(HookEvent.TOOL_ERROR):
        _merge_decision(
            decision,
            await manager.fire(HookEvent.TOOL_ERROR, payload, target_operation_id=target_operation_id, origin=origin),
        )
    return decision


def append_extra_context_to_result(result: Any, result_text: str, extra_context: list[str]) -> tuple[Any, str]:
    """Append hook-provided context to textual tool results."""
    clean = [text for text in extra_context if text]
    if not clean:
        return result, result_text
    suffix = "\n\n".join(clean)
    updated_text = f"{result_text}\n\n{suffix}" if result_text else suffix
    if isinstance(result, str) or result is None:
        return updated_text, updated_text
    if isinstance(result, list):
        from chrys.kernel import Content

        list_suffix = f"\n{suffix}" if result_text else suffix
        return [*result, Content.from_text(list_suffix)], updated_text
    logger.debug(
        "Hook extra_context updated rendered text but left non-text tool result unchanged: %s",
        type(result).__name__,
    )
    return result, updated_text


def final_tool_args(context: FunctionInvocationContext, args: dict[str, Any]) -> dict[str, Any]:
    """Return the final tool arguments for post-execution consumers.

    Approval can rewrite arguments after the event middleware captured its
    ``args`` local (``ApprovalMiddleware`` rebinds ``context.arguments`` and
    records the edit under ``_APPROVAL_MODIFIED_ARGS_KEY``). Both event
    middlewares use this one helper for after-tool hook payloads and
    persisted call provenance so main-agent and sub-agent inner calls stay
    behaviorally identical.
    """
    if isinstance(context.metadata, dict):
        modified = context.metadata.get(_APPROVAL_MODIFIED_ARGS_KEY)
        if is_mutable_tool_args(modified):
            return modified
    if is_mutable_tool_args(context.arguments):
        return context.arguments
    return args


def get_call_id(context: FunctionInvocationContext) -> str:
    """Return a Chrys call id from metadata, if one has already been set."""
    if isinstance(context.metadata, dict):
        return str(context.metadata.get(_CALL_ID_KEY) or "")
    return ""


def get_provider_call_id(context: FunctionInvocationContext) -> str:
    """Return the raw framework/provider function-call id from metadata."""
    if isinstance(context.metadata, dict):
        raw = context.metadata.get("call_id")
        if isinstance(raw, str):
            return raw
        if raw is not None:
            return str(raw)
    return ""


def set_call_id(context: FunctionInvocationContext, call_id: str) -> None:
    """Store the Chrys call id for downstream middleware/helpers."""
    if isinstance(context.metadata, dict):
        context.metadata[_CALL_ID_KEY] = call_id


def get_tool_invocation_order(context: FunctionInvocationContext) -> int | None:
    """Return the current-run tool invocation ordinal from metadata."""
    if not isinstance(context.metadata, dict):
        return None
    return read_tool_invocation_order(context.metadata)


def set_tool_invocation_order(context: FunctionInvocationContext, order: int) -> None:
    """Store the current-run tool invocation ordinal for downstream middleware."""
    if isinstance(context.metadata, dict):
        context.metadata[_TOOL_INVOCATION_ORDER_KEY] = order


def _merge_decision(target: HookDecision, source: HookDecision) -> None:
    if source.blocked and not target.blocked:
        target.blocked = True
        target.block_reason = source.block_reason
    if source.args_override is not None:
        target.args_override = {**(target.args_override or {}), **source.args_override}
    target.system_reminders.extend(source.system_reminders)
    target.extra_context.extend(source.extra_context)


def _build_payload(
    *,
    session_id: str | None,
    profile_name: str,
    tool_name: str,
    kind: str,
    call_id: str,
    args: dict[str, Any],
    workspace_cwd: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "profile": profile_name,
        "cwd": workspace_cwd,
        "tool": {"name": tool_name, "kind": kind, "call_id": call_id, "args": args},
    }
