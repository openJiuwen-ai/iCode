# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``@extension.before_tool`` and ``@extension.after_tool`` hooks of one agent build, and how they run.

Both event middlewares and the approval recheck run them through
``hook_dispatch.py``: before hooks first, ahead of the external
``before_tool_call`` hooks, on the version of the call those hooks then check;
after hooks once the call ended, ahead of the external ``after_tool_call``
hooks. Each hook runs under its own timeout, in the profile's extension order,
then declaration order. A hook that fails denies the call (before) or
withholds its result (after): the text the model reads names the extension and
hook but never carries the exception's own text, which goes to the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.extensions import ToolCall, ToolError, ToolResult
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.tool_execution_stamp import encode_json_like

if TYPE_CHECKING:
    from chrys.service.extensions import BoundToolHook
    from chrys.service.hooks.operation import HookOperation

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtensionToolHooks:
    """The tool hooks the agent's extensions declare, in the order they run."""

    before: tuple[BoundToolHook, ...] = ()
    after: tuple[BoundToolHook, ...] = ()

    async def check_call(self, operation: HookOperation, *, name: str, kind: str, call_id: str) -> str | None:
        """Pass the call's current version through each before hook; why it must not run, or None.

        A hook's new arguments become the operation's next version, which the
        next hook and then the external hooks check. A ``ToolError`` denies the
        call with its own message.
        """
        for hook in self.before:
            if not operation.pending:
                return None  # ended meanwhile: the external check reports it
            arguments = _json_form(operation.arguments())
            if arguments is None:
                return _denied(hook, "its arguments are not JSON")
            call = ToolCall(name=name, kind=kind, call_id=call_id, arguments=arguments, context=hook.context)
            try:
                replacement = await _run(hook, call)
            except ToolError as exc:
                # The model reads it as ``Error: <message>``, also when the author wrote that prefix.
                return surrogate_safe_text(str(exc.message)).removeprefix("Error:").lstrip()
            except _HookFailed as failure:
                return _denied(hook, failure.reason)
            if replacement is None:
                continue
            if not isinstance(replacement, dict):
                reason = f"it returned {_returned(replacement)}, not a dict of JSON arguments"
            elif _json_form(replacement) != replacement:
                # What runs is what later hooks and the approval saw: plain JSON, no objects turned into text.
                reason = "it returned arguments that are not plain JSON"
            else:
                operation.replace_arguments(replacement)
                continue
            _log_failure(hook, "returned", reason)
            return _denied(hook, reason)
        return None

    async def review_result(
        self, *, name: str, kind: str, call_id: str, arguments: dict[str, Any], result: ToolResult
    ) -> str | None:
        """The text the model reads once each after hook saw the result; None when none of them changed it."""
        text: str | None = None
        for hook in self.after:
            current = result.text if text is None else text
            # Each hook its own copy: one that edits it changes nothing for the next.
            shown = _json_form(arguments) or {}
            call = ToolCall(name=name, kind=kind, call_id=call_id, arguments=shown, context=hook.context)
            seen = ToolResult(
                text=current, failed=result.failed, rejected=result.rejected, duration_ms=result.duration_ms
            )
            try:
                replacement = await _run(hook, call, seen)
            except _HookFailed as failure:
                text = _withheld(hook, failure.reason)
                continue
            except ToolError:
                _log_failure(hook, "raised", "ToolError", exc_info=True)
                text = _withheld(hook, "ToolError")
                continue
            if replacement is None:
                continue
            if not isinstance(replacement, str):
                _log_failure(hook, "returned", _returned(replacement))
                text = _withheld(hook, f"it returned {_returned(replacement)}, not text")
                continue
            # A lone surrogate (a POSIX file name that is not UTF-8) cannot be sent to the model as is.
            text = surrogate_safe_text(replacement)
        return text


class _HookFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _run(hook: BoundToolHook, *args: Any) -> Any:
    """The hook's return value; ``ToolError`` passes through, any other failure becomes ``_HookFailed``."""
    seconds = hook.declaration.timeout
    timeout = asyncio.timeout(seconds)
    try:
        async with timeout:
            return await hook.declaration.func(*args)
    except ToolError:
        raise
    except Exception as exc:
        if timeout.expired():
            reason = f"it took longer than {seconds:g} seconds"
            _log_failure(hook, "timed out", reason)
        else:
            reason = type(exc).__name__
            _log_failure(hook, "raised", reason, exc_info=True)
        raise _HookFailed(reason) from exc


def _json_form(arguments: dict[str, Any]) -> dict[str, Any] | None:
    """*arguments* as the JSON values a hook reads (what external hooks get); None when they have none."""
    try:
        value = json.loads(encode_json_like(arguments))
    except TypeError, ValueError, RecursionError:
        return None
    return value if isinstance(value, dict) else None


def _returned(value: object) -> str:
    return type(value).__name__


def _label(hook: BoundToolHook, kind: str) -> str:
    return f"Extension {hook.extension_id}'s {kind} hook {hook.declaration.name}"


def _denied(hook: BoundToolHook, reason: str) -> str:
    return f"{_label(hook, 'before_tool')} failed ({reason}), so the call did not run."


def _withheld(hook: BoundToolHook, reason: str) -> str:
    return f"[{_label(hook, 'after_tool')} failed ({reason}), so {APP_DISPLAY_NAME} withheld this result.]"


def _log_failure(hook: BoundToolHook, what: str, reason: str, *, exc_info: bool = False) -> None:
    logger.warning(
        "Extension %s tool hook %s %s: %s",
        hook.extension_id,
        hook.declaration.name,
        what,
        reason,
        exc_info=exc_info,
    )
