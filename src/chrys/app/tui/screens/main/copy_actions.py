# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Copy and fold actions for the main screen."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from chrys.app.tui.copy_messages import COPIED_TITLE, COPY_TITLE
from chrys.app.tui.widgets.chat.messages import format_chat_copy_payload
from chrys.foundation.i18n import msg

_INVALID_ARGUMENT = msg("tui.copy.invalid_argument", fallback="Invalid argument: {argument}")
_NO_MESSAGES = msg("tui.copy.empty.messages", fallback="No messages to copy")
_NO_USER_TURNS = msg("tui.copy.empty.user_turns", fallback="No user turns to copy")
_NO_AGENT_RESPONSES = msg("tui.copy.empty.agent_responses", fallback="No agent responses to copy")
_COPIED_MESSAGES = msg(
    "tui.copy.copied.messages",
    fallback="Copied {count} message",
    plural_fallback="Copied {count} messages",
)
_COPIED_USER_TURNS = msg(
    "tui.copy.copied.user_turns",
    fallback="Copied {count} user turn",
    plural_fallback="Copied {count} user turns",
)
_COPIED_RESPONSES = msg(
    "tui.copy.copied.responses",
    fallback="Copied {count} response",
    plural_fallback="Copied {count} responses",
)

if TYPE_CHECKING:
    from chrys.app.tui.screens.main.ports import CopyActionView


def parse_copy_arguments(arg: str) -> tuple[str, int | None] | None:
    """Return ``(target, count)`` for /copy, or ``None`` for invalid arguments."""
    parts = arg.split()
    if not parts:
        return "agent", 1

    def parse_count(value: str) -> int | None:
        try:
            count = int(value)
        except ValueError:
            return None
        return count if count > 0 else None

    first = parts[0].lower()
    if first == "all":
        return ("all", None) if len(parts) == 1 else None

    if first in {"agent", "user"}:
        if len(parts) == 1:
            return first, 1
        if len(parts) == 2:
            second = parts[1].lower()
            if second == "all":
                return first, None
            count = parse_count(second)
            return (first, count) if count is not None else None
        return None

    if len(parts) == 1:
        count = parse_count(first)
        return ("agent", count) if count is not None else None
    return None


class CopyActionController:
    """Coordinate slash copy and fold actions."""

    def __init__(self, *, view: CopyActionView, debug: Callable[[str, str], None]) -> None:
        self._view = view
        self._debug = debug

    def copy_agent_responses(self, arg: str) -> None:
        """Handle /copy for agent, user, or full-conversation turns."""
        arg = arg.strip()
        parsed = parse_copy_arguments(arg)
        if parsed is None:
            self._view.notify(
                _INVALID_ARGUMENT.bind(argument=arg),
                title=COPY_TITLE.bind(),
                severity="error",
                timeout=3,
            )
            return

        target, count = parsed
        messages = self._view.chat_copy_messages(target)
        empty_message = {
            "all": _NO_MESSAGES,
            "user": _NO_USER_TURNS,
        }.get(target, _NO_AGENT_RESPONSES)
        if not messages:
            self._view.notify(empty_message.bind(), title=COPY_TITLE.bind(), severity="warning", timeout=3)
            return

        selected = messages if count is None else messages[-min(count, len(messages)) :]
        count = len(selected)
        if target == "all":
            message = _COPIED_MESSAGES.bind(count=count)
        elif target == "user":
            message = _COPIED_USER_TURNS.bind(count=count)
        else:
            message = _COPIED_RESPONSES.bind(count=count)
        self._view.copy_text(format_chat_copy_payload(selected), message=message, title=COPIED_TITLE.bind())

    def toggle_fold(self) -> None:
        """Collapse or expand all process sections, tool groups, thinking and compaction summaries."""
        collapsed = self._view.toggle_chat_fold_all()
        label = "Folded" if collapsed else "Unfolded"
        self._debug("Fold", label)
