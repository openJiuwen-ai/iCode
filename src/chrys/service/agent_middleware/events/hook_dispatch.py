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

import inspect
import logging
from typing import TYPE_CHECKING, Any, TypeGuard

from chrys.extensions import ToolResult
from chrys.foundation.io.result_content import extract_result_attachments, extract_result_text
from chrys.foundation.tool_invocation_order import read_tool_invocation_order
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_MODIFIED_ARGS_KEY,
    _APPROVAL_REJECTED_KEY,
    _BEFORE_TOOL_CONTEXT_KEY,
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
    from chrys.service.agent_middleware.events.extension_hooks import ExtensionToolHooks
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
    extension_hooks: ExtensionToolHooks | None = None,
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
    The extensions' before hooks (*extension_hooks*) check each version
    first, so the external hooks check what they leave.
    On a pass, the hooks' ``extra_context`` for the checked version is kept
    in the call's metadata, for :func:`before_tool_context` to deliver with
    the result; each check replaces what an earlier one kept.
    *origin* is the invoking call, given to the payload as ``origin`` for every event. The
    ``HookOperation`` keeps it only for a sub-agent, which is what its field has always
    meant (a confirmation names the sub-agent its tool call belongs to, or None for the
    main agent); widening that would change an existing field's meaning.
    """
    metadata = context.metadata if isinstance(context.metadata, dict) else None
    if metadata is not None:
        metadata.pop(_BEFORE_TOOL_CONTEXT_KEY, None)
    existing = metadata.get(_HOOK_OPERATION_KEY) if metadata is not None else None
    if isinstance(existing, HookOperation):
        operation = existing
        operation.replace_arguments(args)
    else:
        operation = HookOperation(
            subject="tool",
            session_id=session_id,
            origin=origin if origin is not None and origin.kind != "turn" else None,
            caller_name=profile_name,
            tool_name=tool_name,
            tool_kind=kind,
            arguments=args,
        )
        if metadata is not None:
            metadata[_HOOK_OPERATION_KEY] = operation

    if extension_hooks is not None and extension_hooks.before:
        denial = await extension_hooks.check_call(operation, name=tool_name, kind=kind, call_id=call_id)
        if denial is not None:
            reject_tool_call(context, denial)
            return True

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
            origin=origin,
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
    if metadata is not None and decision.extra_context:
        metadata[_BEFORE_TOOL_CONTEXT_KEY] = list(decision.extra_context)
    return False


def before_tool_context(context: FunctionInvocationContext) -> list[str]:
    """The ``extra_context`` ``before_tool_call`` hooks returned for the version of the call that ran.

    Empty for a call that was rejected: it never ran, so there is no result to
    attach a note to, and the model already reads the rejection reason.
    """
    metadata = context.metadata if isinstance(context.metadata, dict) else None
    if metadata is None or metadata.get(_APPROVAL_REJECTED_KEY, False):
        return []
    kept = metadata.get(_BEFORE_TOOL_CONTEXT_KEY)
    return [text for text in kept if isinstance(text, str) and text] if isinstance(kept, list) else []


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
        "outcome": _tool_outcome_name(errored=errored, failed=failed, approval_rejected=approval_rejected),
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
        origin=origin,
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
        replaced = decision.result_override
        if replaced:
            # One call, two events about it. ``tool_error`` runs second and sees the
            # result as this one left it, as a chain of hooks on one event already
            # does; otherwise its replacement is computed from text the first hook
            # had removed, and puts that text back.
            result["text"] = replaced
    if errored and manager.has_hooks_for(HookEvent.TOOL_ERROR):
        _merge_decision(
            decision,
            await manager.fire(HookEvent.TOOL_ERROR, payload, target_operation_id=target_operation_id, origin=origin),
        )
    return decision


async def apply_after_tool_extensions(
    extension_hooks: ExtensionToolHooks | None,
    context: FunctionInvocationContext,
    *,
    tool_name: str,
    kind: str,
    call_id: str,
    args: dict[str, Any],
    result_text: str,
    raised_text: str | None,
    failed: bool,
    rejected: bool,
    duration_ms: int,
) -> str:
    """Show the extensions' after hooks how the call ended; the result text from here on.

    *raised_text* is what the model reads for a tool that raised (the kernel
    answers it after this middleware); the hooks see that, and what they
    return then changes nothing. Otherwise they see the result's text without
    its attachments (images, audio, files), and their text replaces it,
    keeping the attachments.
    """
    if extension_hooks is None or not extension_hooks.after:
        return result_text
    attachments = extract_result_attachments(context.result) if raised_text is None else []
    if raised_text is not None:
        shown = raised_text
    elif attachments and isinstance(context.result, list):
        # Not the placeholders the tool card shows for them: the model reads the attachments themselves.
        shown = extract_result_text([item for item in context.result if all(item is not a for a in attachments)])
    else:
        shown = result_text
    seen = ToolResult(text=shown, failed=failed, rejected=rejected, duration_ms=duration_ms)
    text = await extension_hooks.review_result(name=tool_name, kind=kind, call_id=call_id, arguments=args, result=seen)
    if text is None or raised_text is not None:
        return result_text
    if attachments:
        from chrys.kernel import Content

        context.result = [Content.from_text(text), *attachments]
    else:
        context.result = text
    return extract_result_text(context.result)


def apply_result_hooks(
    result: Any,
    result_text: str,
    *,
    result_override: str = "",
    extra_context: list[str] | tuple[str, ...] = (),
) -> tuple[Any, str]:
    """Write a hook-provided replacement and context onto a tool result.

    *result_override* replaces the text the model reads; *extra_context* is appended to
    it. A result that holds images keeps them, since a hook returns text only and
    dropping them would lose what the model was shown. One that is not text at all keeps
    its value; only the rendered text changes, as it does for appended context.
    """
    overridden = bool(result_override)
    if overridden:
        result_text = result_override
    clean = [text for text in extra_context if text]
    if not clean:
        return (_with_result_text(result, result_text), result_text) if overridden else (result, result_text)
    suffix = "\n\n".join(clean)
    updated_text = f"{result_text}\n\n{suffix}" if result_text else suffix
    if overridden:
        return _with_result_text(result, updated_text), updated_text
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


def _with_result_text(result: Any, text: str) -> Any:
    """*result* with its text replaced by *text*, keeping its attachments; a non-text result as it is."""
    if isinstance(result, str) or result is None:
        return text
    if inspect.isawaitable(result):
        # The kernel assigns the tool's own coroutine to ``context.result`` before
        # awaiting it, so a tool that raised leaves that coroutine there. It is not
        # a result and never resolves: close it, and write the text in its place —
        # leaving it would also drop the replacement the hook just made.
        close = getattr(result, "close", None)
        if close is not None:
            close()
        return text
    if isinstance(result, list):
        from chrys.kernel import Content

        attachments = extract_result_attachments(result)
        return [Content.from_text(text), *attachments] if attachments else text
    logger.debug(
        "Hook result_override left a non-text tool result unchanged: %s",
        type(result).__name__,
    )
    return result


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


def _tool_outcome_name(*, errored: bool, failed: bool, approval_rejected: bool) -> str:
    """How the call ended, as one word, so a hook counts each call once without inferring it.

    ``rejected``: approval refused it, so it never ran. ``errored``: it raised. ``failed``: it
    returned a failure result without raising. ``ok``: it ran and returned. Both ``after_tool_call``
    and ``tool_error`` carry the same value for the same call: they are two events about one call,
    told apart by nothing but their name.
    """
    if approval_rejected:
        return "rejected"
    if errored:
        return "errored"
    if failed:
        return "failed"
    return "ok"


def _merge_decision(target: HookDecision, source: HookDecision) -> None:
    if source.blocked and not target.blocked:
        target.blocked = True
        target.block_reason = source.block_reason
    if source.args_override is not None:
        target.args_override = {**(target.args_override or {}), **source.args_override}
    target.system_reminders.extend(source.system_reminders)
    target.extra_context.extend(source.extra_context)
    if source.result_override:
        target.result_override = source.result_override


def _build_payload(
    *,
    session_id: str | None,
    profile_name: str,
    tool_name: str,
    kind: str,
    call_id: str,
    args: dict[str, Any],
    workspace_cwd: str,
    origin: InvocationOrigin | None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "profile": profile_name,
        "cwd": workspace_cwd,
        "origin": origin_payload(origin),
        "tool": {"name": tool_name, "kind": kind, "call_id": call_id, "args": args},
    }


def origin_payload(origin: InvocationOrigin | None) -> dict[str, Any] | None:
    """The calling invocation and its bound ancestry, as hooks receive it; None when unbound.

    A sub-agent's tool calls name the invocation its ``sub_agent_start`` announced, so
    concurrent instances of one profile stay apart, and ``parent`` walks back to the
    chat turn or workflow node the call ultimately belongs to.
    """
    if origin is None:
        return None
    return {
        "kind": origin.kind,
        "session_id": origin.session_id,
        "invocation_id": origin.invocation_id,
        "attempt": origin.attempt,
        "parent": origin_payload(origin.parent),
    }
