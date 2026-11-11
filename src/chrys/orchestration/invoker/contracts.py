# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Neutral resource-close contracts, independent of backend and caller shells."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Protocol

from chrys.foundation.models.invocations import InvocationOrigin, PassHandle
from chrys.kernel import Content, Message

from .evidence import PassEvidence


class AbortCause(Enum):
    """Why an owner asks an operation to converge."""

    USER_CANCEL = "user_cancel"
    CALLER_TIMEOUT = "caller_timeout"
    RUN_TERMINAL = "run_terminal"
    CASCADE = "cascade"
    OWNER_CLOSE = "owner_close"
    STEER = "steer"
    """The user steered: the caller goes on from the pass's committed work with a new message."""


class PreparedClosed(RuntimeError):
    """An acquire or open was attempted after its owner started closing."""


class OperationBinding(Protocol):
    """A shell's synchronous close latch and its complete cleanup barrier.

    The barrier includes the writer, parent commit, end hook and live controls;
    an empty attempt-task slot alone does not mean that it has drained.
    """

    def request_close(self, cause: AbortCause) -> None: ...

    @property
    def drained(self) -> Awaitable[None]: ...


type Unbind = Callable[[], None]


class SubAgentStatus(Enum):
    """Lifecycle state of a single sub-agent invocation."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    CASCADE_ABORTED = "cascade_aborted"


class SubAgentFailureReason(StrEnum):
    """Persisted reason for a caller's manual decision boundary."""

    STREAM_STALL = "stream_stall"
    LAST_WORDS = "last_words"
    FRAMEWORK_EXC = "framework_exc"
    ACP_TRANSPORT = "acp_transport"


class RunIntent(Enum):
    FRESH = "fresh"
    CONTINUE = "continue"
    RETRY = "retry"


class ContinuationCapability(Enum):
    CONTINUE_HISTORY = "continue_history"
    FRESH_SESSION = "fresh_session"


class StopCause(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class FailureDisposition(Enum):
    TERMINAL = "terminal"
    CALLER_DECISION = "caller_decision"


class FailureCategory(Enum):
    """What a backend knows about a failure beyond its exception, for callers that retry by themselves.

    Disposition says who decides what happens next; the category says whether repeating can help.
    """

    UNCLASSIFIED = "unclassified"
    """The backend adds nothing: the shared error classifier judges the exception."""
    TRANSPORT = "transport"
    """The connection ended before an answer: an idle timeout, a disconnect, an unexpected exit or an
    unexpected cancel."""
    REMOTE_ERROR = "remote_error"
    """The remote agent reported a failure without saying whether a repeat can succeed."""
    DEFINITIVE = "definitive"
    """Configuration, launch (including an agent the connect retries could not reach), authentication,
    refusal, truncation or empty output: a repeat ends the same way."""


class AbortResult(Enum):
    REQUESTED = "requested"
    ALREADY_CONVERGED = "already_converged"


class StaleContinuation(ValueError):
    """The live single-use ticket no longer names this conversation state."""


class OverlappingRun(RuntimeError):
    """A conversation already has an unconverged pass."""


class UnsupportedRequest(ValueError):
    """The backend rejects this intent/content before any execution side effect."""


@dataclass(frozen=True, slots=True)
class ContinuationTicket:
    conversation_id: str
    failed_pass_id: str
    state_generation: int
    capability: ContinuationCapability


@dataclass(frozen=True, slots=True)
class RunRequest:
    """A pass request, including a caller-preallocated logical identity.

    Intent x capability admission (before hooks, history writes or transport):
      continue_history: fresh requires messages and no ticket; continue accepts
        empty input or messages, without a ticket; retry requires its live ticket.
      fresh_session: fresh requires text messages and no ticket; continue is
        unsupported; retry requires its live fresh_session ticket and text input.
    A fresh empty text Message is different from an empty continuation sequence.
    ACP accepts only user messages containing text, never silently drops images.
    Kernel accepts a foreign-origin FRESH as a new logical invocation. The ACP
    controller belongs to one fixed operation: foreign FRESH without a ticket
    is UnsupportedRequest; foreign ticket-bearing input is StaleContinuation
    after intent/content validation. Both backends order admission as closing
    (ticket -> StaleContinuation, no ticket -> PreparedClosed), overlap,
    intent/content table, then live-ticket identity/origin validation.

    Exceptions: closed owner, overlapping run, stale ticket and unsupported
    input are admission errors. After admission, provider/validation/pass-hook
    failures return Failed (possibly without an exception for an ACP stop reason).
    A latched caller abort returns Aborted; unrequested task cancellation still
    propagates CancelledError after convergence. Shells retain their own terminal
    projection and caller-operation cleanup beyond the pass boundary.
    """

    messages: Sequence[Message]
    intent: RunIntent
    origin: InvocationOrigin
    continuation: ContinuationTicket | None = None


def validate_request(request: RunRequest, capability: ContinuationCapability) -> None:
    if not isinstance(request.origin, InvocationOrigin):
        raise UnsupportedRequest("A live request requires an explicit origin")
    if request.intent is RunIntent.FRESH:
        if request.continuation is not None or not request.messages:
            raise UnsupportedRequest("Fresh input requires messages and no continuation ticket")
    elif request.intent is RunIntent.CONTINUE:
        if capability is ContinuationCapability.FRESH_SESSION or request.continuation is not None:
            raise UnsupportedRequest("This backend cannot continue the requested state")
    elif request.intent is RunIntent.RETRY:
        if request.continuation is None:
            raise StaleContinuation("Retry requires a live continuation ticket")
        if request.continuation.capability is not capability:
            raise StaleContinuation("Continuation capability does not match this backend")
    else:
        raise UnsupportedRequest("Unknown run intent")
    if capability is ContinuationCapability.FRESH_SESSION and (
        not request.messages
        or any(
            message.role != "user" or any(content.type != "text" for content in message.contents)
            for message in request.messages
        )
    ):
        raise UnsupportedRequest("ACP accepts only user text input")


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """Observed spend; a missing report never becomes a complete zero."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    complete: bool = False
    unreported: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Outcome:
    handle: PassHandle
    usage: UsageDelta
    effects: PassEvidence
    stop: StopCause
    continuation: ContinuationTicket | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Ok(Outcome):
    """Raw successful output. Caller policy may fill structured_output lazily.

    Backends always leave structured_output as None; they do not parse or
    validate AgentResponse.value. Kernel backend_payload retains that response.
    """

    segments: tuple[Content, ...]
    structured_output: object = None
    backend_payload: object = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Failed(Outcome):
    disposition: FailureDisposition
    error: str
    exception: Exception | None = None
    category: FailureCategory = FailureCategory.UNCLASSIFIED


@dataclass(frozen=True, slots=True, kw_only=True)
class Aborted(Outcome):
    cause: AbortCause


type InvocationOutcome = Ok | Failed | Aborted


class AuditExport(Protocol):
    """An explicit read-only audit port, with no implied mutable history."""

    def export_audit(self) -> Mapping[str, object]: ...


class InvocationConversation(Protocol):
    """The backend-neutral pass interface; resource and caller owners are separate."""

    async def run(self, request: RunRequest) -> InvocationOutcome: ...
    async def abort(self, handle: PassHandle, cause: AbortCause) -> AbortResult: ...

    @property
    def active_handle(self) -> PassHandle | None: ...
