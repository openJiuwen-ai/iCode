# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One operation a gating hook event guards: a tool call or a user input, until it runs or is dropped.

Every entry builds one, whether or not any hook is configured, and every
entry goes the same way: the event's hooks check the operation's current
version (``HookManager.check``), then the code that performs it commits that
version at its last step (``commit``). Nothing runs on a version the hooks
did not check through to the end; an argument change, by a hook or an
approval edit, makes a new version that has to be checked again.

The owner that knows the operation's lifetime (an injection target, a
workflow node) calls ``invalidate`` when it ends; a confirmation waiting on
the operation ends with it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Literal
from uuid import uuid4

from chrys.foundation.models.hook_confirmations import HookConfirmationSubjectKind
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_execution_stamp import canonicalize_effective_arguments

logger = logging.getLogger(__name__)

OperationState = Literal["pending", "committed", "invalidated"]

CommitRefusal = Literal["invalidated", "committed", "unchecked", "changed"]
"""Why ``commit`` refused: the operation ended, already ran, its version was not checked
through, or what is about to run is not the version that was."""


def args_key(args: Any) -> str | None:
    """*args* in the canonical form of a tool call's effective arguments, which two equal argument sets share.

    The execution stamp's form (``canonicalize_effective_arguments``): sorted
    keys, typed keys and models normalized, and compared as written, so 1 and
    true differ though Python finds them equal. None when the arguments have no
    such form (a value whose text form fails, nesting deeper than the encoder
    writes). Never raises: tool arguments are whatever the model and the hooks
    made them.
    """
    try:
        return canonicalize_effective_arguments(args)
    except Exception:
        return None


def _snapshot(arguments: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """A copy of *arguments* that shares nothing with them, and its key; key None when there is no such copy.

    Arguments without a canonical form, or that cannot be deep-copied, have no
    snapshot: hooks cannot be shown them, so ``check_operation`` refuses such a
    call wherever a hook would check it, and only an event with no hooks, which
    has nothing to compare, passes it.
    """
    key = args_key(arguments)
    if key is not None:
        try:
            return copy.deepcopy(arguments), key
        except Exception:
            logger.debug("Hook operation snapshot: deepcopy failed; hooks get no snapshot for this call", exc_info=True)
    return dict(arguments), None


class HookOperation:
    """The current version of one tool call or input, how far the hooks checked it, and whether it may still run."""

    __slots__ = (
        "_arguments",
        "_arguments_key",
        "_checked_revision",
        "_checks",
        "_confirmed_arguments",
        "_end_reason",
        "_ended",
        "_ended_by_closing_hooks",
        "_input_text",
        "_revision",
        "_state",
        "caller_name",
        "operation_id",
        "origin",
        "session_id",
        "subject",
        "tool_kind",
        "tool_name",
    )

    def __init__(
        self,
        *,
        subject: HookConfirmationSubjectKind,
        session_id: str | None,
        origin: InvocationOrigin | None = None,
        caller_name: str = "",
        tool_name: str = "",
        tool_kind: str = "",
        arguments: dict[str, Any] | None = None,
        input_text: str = "",
    ) -> None:
        self.operation_id = uuid4().hex
        self.subject: HookConfirmationSubjectKind = subject
        self.session_id = session_id
        self.origin = origin
        self.caller_name = caller_name
        """The agent profile that wants the operation: the main agent's, a sub-agent's, a node's."""
        self.tool_name = tool_name
        self.tool_kind = tool_kind
        # A copy: the caller's dict is the one hooks and approval edits rewrite in place.
        given = arguments if arguments is not None else {}
        self._arguments, self._arguments_key = _snapshot(given)
        self._input_text = input_text
        self._revision = 1
        self._checked_revision: int | None = None
        self._checks = 0
        self._confirmed_arguments: dict[str, Any] | None = None
        self._state: OperationState = "pending"
        self._end_reason = ""
        self._ended_by_closing_hooks = False
        self._ended = asyncio.Event()

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def checked_revision(self) -> int | None:
        """The version the event's hooks checked through to the end; None until they did."""
        return self._checked_revision

    @property
    def check_count(self) -> int:
        """How many checks of this operation have started; 2 and up are re-checks of the same call."""
        return self._checks

    @property
    def state(self) -> OperationState:
        return self._state

    @property
    def end_reason(self) -> str:
        """Why the operation was invalidated: a code its owner gave (``turn_ended``, ``withdrawn``, …)."""
        return self._end_reason

    @property
    def ended_by_closing_hooks(self) -> bool:
        """Whether hooks closing under it (a drain, a shutdown) ended it, rather than its owner withdrawing it.

        Set by whoever ended it first, so a later close never turns a withdrawal into one. What it
        means is fixed here, not read off ``end_reason``, whose codes are for display.
        """
        return self._ended_by_closing_hooks

    @property
    def pending(self) -> bool:
        return self._state == "pending"

    @property
    def input_text(self) -> str:
        return self._input_text

    @property
    def has_snapshot(self) -> bool:
        """Whether the current version's arguments have a snapshot hooks can check (see ``_snapshot``)."""
        return self._arguments_key is not None

    def matches(self, arguments: Any) -> bool:
        """Whether *arguments* are the current version: the same key (two without one count alike, see ``_snapshot``)."""
        return args_key(arguments) == self._arguments_key

    @property
    def confirmed_arguments(self) -> dict[str, Any] | None:
        """The tool arguments of the version the user last continued; None until they did."""
        return self._confirmed_arguments

    def note_confirmed(self, arguments: dict[str, Any]) -> None:
        """The user continued the version with *arguments* (a copy the caller no longer changes)."""
        self._confirmed_arguments = arguments

    def arguments(self) -> dict[str, Any]:
        """A copy of the current version's tool arguments."""
        return _snapshot(self._arguments)[0]

    def replace_arguments(self, arguments: dict[str, Any]) -> bool:
        """Make *arguments* the current version when they differ from it; True when they did.

        A new version is unchecked.
        """
        if not self.pending:
            return False
        if self.matches(arguments):
            return False
        self._arguments, self._arguments_key = _snapshot(arguments)
        self._revision += 1
        self._checked_revision = None
        return True

    def begin_check(self) -> None:
        """A check of the current version starts: what an earlier check passed no longer counts."""
        self._checks += 1
        self._checked_revision = None

    def mark_checked(self, revision: int) -> bool:
        """Record that the event's hooks passed version *revision*; False when it is no longer current."""
        if not self.pending or revision != self._revision:
            return False
        self._checked_revision = revision
        return True

    def invalidate(self, reason: str, *, closing_hooks: bool = False) -> bool:
        """End a pending operation: it never runs, and whatever waits on it is told. Once only.

        *closing_hooks* when the hooks closing under it end it, not its owner (``ended_by_closing_hooks``).
        """
        if not self.pending:
            return False
        self._state = "invalidated"
        self._end_reason = reason
        self._ended_by_closing_hooks = closing_hooks
        self._ended.set()
        return True

    async def wait_invalidated(self) -> None:
        """Return once the operation is invalidated."""
        await self._ended.wait()

    def commit_arguments(self, arguments: Any) -> CommitRefusal | None:
        """Take the right to run the tool with *arguments*; the refusal, or None once taken.

        Synchronous: nothing can change the operation between the check and
        the state change. Committed at most once.
        """
        refusal = self._refusal()
        if refusal is not None:
            return refusal
        if not self.matches(arguments):
            return "changed"
        self._state = "committed"
        return None

    def commit_input(self, text: str) -> CommitRefusal | None:
        """Take the right to submit *text*; the refusal, or None once taken."""
        refusal = self._refusal()
        if refusal is not None:
            return refusal
        if text != self._input_text:
            return "changed"
        self._state = "committed"
        return None

    def _refusal(self) -> CommitRefusal | None:
        if self._state == "invalidated":
            return "invalidated"
        if self._state == "committed":
            return "committed"
        if self._checked_revision != self._revision:
            return "unchecked"
        return None
