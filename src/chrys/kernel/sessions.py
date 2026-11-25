# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Session state, provider context and conversation history storage.

``AgentSession`` holds identifiers and provider state; ``SessionContext``
collects per-run messages, instructions, tools and middleware. Context
providers contribute before a run and process the completed response after it.
History providers load and save according to their configuration. The service
state layer owns session persistence and serialization.

``SessionContext.extend_messages`` retains the supplied Message objects and
their metadata without copies or attribution stamps. Wire context and stored
history therefore share annotations, including exclusion and token counts.
Retry rollback must restore ``additional_properties`` exactly; heuristically
clearing flags would lose the previous state. See ``HistorySnapshot`` in
``chrys.foundation.retry``. Current-turn messages rebuilt by streaming still
need the service compaction anchor pass to carry their annotations forward.
Attribution stamps would pollute aliased, persisted history and have no reader.

``split_middleware`` accepts only chat/function middleware. History providers
are instantiable: their ``abstractmethod`` markers document subclass hooks,
but these classes do not use ``ABCMeta``.

Kernel dependencies stay within this package, allowed third-party packages
and foundation.
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from .client import collect_conversation_handles
from .middleware import split_middleware

if TYPE_CHECKING:
    from ._types import AgentResponse, Message
    from .middleware import ChatMiddleware, FunctionMiddleware

__all__ = [
    "LOCAL_HISTORY_CONVERSATION_ID",
    "AgentSession",
    "ContextProvider",
    "HistoryProvider",
    "InMemoryHistoryProvider",
    "SessionContext",
    "is_local_history_conversation_id",
]


class _ContextSource(Protocol):
    source_id: str


class SessionContext:
    """Per-invocation state passed through the context provider pipeline.

    Created fresh for each ``Agent.run()`` call; providers read from and write
    to the mutable fields to add context before invocation and process
    responses after.

    Attributes:
        session_id: The ID of the current session.
        service_session_id: Service-managed session ID (if present, service
            handles storage).
        input_messages: The new messages being sent to the agent (set by the
            Agent).
        context_messages: Dict mapping source_id -> messages added by that
            provider. Maintains insertion order (provider execution order).
        instructions: Additional instructions added by providers.
        tools: Additional tools added by providers.
        middleware: Dict mapping source_id -> chat/function middleware added
            by that provider. Maintains insertion order.
        response: After invocation, contains the full AgentResponse; read-only
            for providers.
        options: Options passed to ``Agent.run()`` — read-only, for reflection.
        metadata: Shared metadata dictionary for cross-provider communication.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        service_session_id: str | None = None,
        input_messages: list[Message],
        context_messages: dict[str, list[Message]] | None = None,
        instructions: list[str] | None = None,
        tools: list[Any] | None = None,
        middleware: dict[str, list[ChatMiddleware | FunctionMiddleware]] | None = None,
        options: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """Initialize empty provider contribution stores around the invocation input."""
        self.session_id = session_id
        self.service_session_id = service_session_id
        self.input_messages = input_messages
        self.context_messages: dict[str, list[Message]] = context_messages or {}
        self.instructions: list[str] = instructions or []
        self.tools: list[Any] = tools or []
        self.middleware: dict[str, list[ChatMiddleware | FunctionMiddleware]] = {}
        if middleware:
            for source_id, provider_middleware in middleware.items():
                self.extend_middleware(source_id, provider_middleware)
        self._response: AgentResponse | None = None
        self.options: dict[str, Any] = options or {}
        self.metadata: dict[str, Any] = metadata or {}

    @property
    def response(self) -> AgentResponse | None:
        """The agent's response.

        Set by the Agent finalizers via the private ``_response`` field after
        invocation (``kernel.agent`` is the only writer); read-only for
        providers.
        """
        return self._response

    def extend_messages(self, source: str | _ContextSource, messages: Sequence[Message]) -> None:
        """Add context messages from a specific source.

        The caller's ``Message`` objects are appended as-is — no shallow copy, no fresh
        ``additional_properties`` dict, no ``_attribution`` stamp (module
        docstring has the full rationale). Stored keyed by source_id,
        maintaining insertion order based on provider execution order.

        Args:
            source: Either a plain ``source_id`` string, or an object with a
                ``source_id`` attribute (e.g. a context provider).
            messages: The messages to add.
        """
        source_id = source if isinstance(source, str) else source.source_id
        if source_id not in self.context_messages:
            self.context_messages[source_id] = []
        self.context_messages[source_id].extend(messages)

    def extend_instructions(self, source_id: str, instructions: str | Sequence[str]) -> None:
        """Add instructions to be prepended to the conversation.

        ``source_id`` is accepted for provider symmetry; instructions are unkeyed.

        Args:
            source_id: The provider source_id adding these instructions.
            instructions: A single instruction string or sequence of strings.
        """
        if isinstance(instructions, str):
            instructions = [instructions]
        self.instructions.extend(instructions)

    def extend_tools(self, source_id: str, tools: Sequence[Any]) -> None:
        """Add tools to be available for this invocation.

        Tools carrying a dict ``additional_properties`` get a
        ``context_source`` attribution entry.

        Args:
            source_id: The provider source_id adding these tools.
            tools: The tools to add.
        """
        for tool in tools:
            if hasattr(tool, "additional_properties"):
                additional_properties_obj = tool.additional_properties
                if isinstance(additional_properties_obj, dict):
                    additional_properties = cast("dict[str, Any]", additional_properties_obj)
                    additional_properties["context_source"] = source_id
        self.tools.extend(tools)

    def extend_middleware(
        self,
        source_id: str,
        middleware: ChatMiddleware | FunctionMiddleware | Sequence[ChatMiddleware | FunctionMiddleware],
    ) -> None:
        """Add middleware to be applied for this invocation.

        ``split_middleware`` rejects unsupported instances with ``TypeError``.
        Storage keeps the caller's flat order.

        Args:
            source_id: The provider source_id adding this middleware.
            middleware: A single chat/function middleware instance or sequence.
        """
        raw_items: list[Any]
        if isinstance(middleware, Sequence) and not isinstance(middleware, (str, bytes)):
            raw_items = list(middleware)
        else:
            raw_items = [middleware]
        split_middleware(raw_items)
        # split_middleware guarantees every item has one of the stored types.
        middleware_items = cast("list[ChatMiddleware | FunctionMiddleware]", raw_items)
        if source_id not in self.middleware:
            self.middleware[source_id] = []
        self.middleware[source_id].extend(middleware_items)

    def get_middleware(self) -> list[ChatMiddleware | FunctionMiddleware]:
        """Get provider-added chat/function middleware in provider execution order."""
        result: list[ChatMiddleware | FunctionMiddleware] = []
        for middleware_items in self.middleware.values():
            result.extend(middleware_items)
        return result

    def get_messages(
        self,
        *,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        include_input: bool = False,
        include_response: bool = False,
    ) -> list[Message]:
        """Get context messages, optionally filtered and including input/response.

        Returns messages in provider execution order (dict insertion order),
        with input and response appended if requested. The returned list is
        fresh; the ``Message`` objects are the stored ones.

        Args:
            sources: If provided, only include context messages from these sources.
            exclude_sources: If provided, exclude context messages from these sources.
            include_input: If True, append input_messages after context.
            include_response: If True, append response.messages at the end.

        Returns:
            Flattened list of messages in conversation order.
        """
        result: list[Message] = []
        for source_id, messages in self.context_messages.items():
            if sources is not None and source_id not in sources:
                continue
            if exclude_sources is not None and source_id in exclude_sources:
                continue
            result.extend(messages)
        if include_input and self.input_messages:
            result.extend(self.input_messages)
        if include_response and self.response and self.response.messages:
            result.extend(self.response.messages)
        return result


class ContextProvider:
    """Contribute context before a run and process the response afterward.

    Context providers participate in the context engineering pipeline, adding
    context before model invocation and processing responses after. Both hooks
    default to no-ops; the class is instantiable without ``ABCMeta``.

    Attributes:
        source_id: Unique identifier for this provider instance (required).
            Used for message/tool attribution so other providers can filter.
    """

    def __init__(self, source_id: str):
        """Initialize the provider.

        Args:
            source_id: Unique identifier for this provider instance.
        """
        self.source_id = source_id

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Called before model invocation.

        Override to add context (messages, instructions, tools) to the
        SessionContext before the model is invoked.

        Args:
            agent: The agent running this invocation.
            session: The current session.
            context: The invocation context — add messages/instructions/tools/
                chat/function middleware here.
            state: The provider-scoped mutable state dict for this provider
                (``session.state[self.source_id]``). Full cross-provider state
                remains available at ``session.state``.
        """

    async def after_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Called after model invocation.

        Override to process the response (store messages, extract info, etc.).
        ``context.response`` is populated at this point.

        Args:
            agent: The agent that ran this invocation.
            session: The current session.
            context: The invocation context with response populated.
            state: The provider-scoped mutable state dict for this provider.
        """

    def contributed_instructions(self) -> str:
        """The instructions this provider adds before every run, or "" when it adds none.

        A provider that fixes its instruction text when it is built returns it here,
        so a reader that needs to know what the model is told does not have to run
        ``before_run`` — which would re-enter the session for what is only a read —
        nor reach into the provider's private fields.
        """
        return ""


class HistoryProvider(ContextProvider):
    """Load and store conversation history through configurable provider hooks.

    A single class configurable for different use cases: primary memory
    storage (loads + stores), audit/logging storage (stores only), evaluation
    storage. Subclasses implement :meth:`get_messages` and
    :meth:`save_messages`; the default ``before_run``/``after_run`` handle
    loading and storing based on the configuration flags.

    ``SessionContext.extend_messages`` aliases loaded messages. Configuring
    ``store_context_from={self.source_id}`` therefore appends this provider's
    own loaded objects back into its state, duplicating references.

    Attributes:
        load_messages: Whether to load messages before invocation
            (default True). When False, the Agent skips ``before_run``
            entirely.
        store_inputs: Whether to store input messages (default True).
        store_context_messages: Whether to store context from other providers
            (default False).
        store_context_from: If set, only store context from these source_ids.
        store_outputs: Whether to store response messages (default True).
    """

    def __init__(
        self,
        source_id: str,
        *,
        load_messages: bool = True,
        store_inputs: bool = True,
        store_context_messages: bool = False,
        store_context_from: set[str] | None = None,
        store_outputs: bool = True,
    ):
        """Initialize the history provider.

        Args:
            source_id: Unique identifier for this provider instance.
            load_messages: Whether to load messages before invocation.
            store_inputs: Whether to store input messages.
            store_context_messages: Whether to store context from other providers.
            store_context_from: If set, only store context from these source_ids.
            store_outputs: Whether to store response messages.
        """
        super().__init__(source_id)
        self.load_messages = load_messages
        self.store_inputs = store_inputs
        self.store_context_messages = store_context_messages
        self.store_context_from = store_context_from
        self.store_outputs = store_outputs

    @abstractmethod
    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        """Retrieve stored messages for this session.

        Args:
            session_id: The session ID to retrieve messages for.
            state: Optional session state for providers that persist in
                session state. Not used by all providers.
            **kwargs: Additional subclass-specific extensibility arguments.

        Returns:
            List of stored messages.
        """
        ...

    @abstractmethod
    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist messages for this session.

        Args:
            session_id: The session ID to store messages for.
            messages: The messages to persist.
            state: Optional session state for providers that persist in
                session state. Not used by all providers.
            **kwargs: Additional subclass-specific extensibility arguments.
        """
        ...

    def _get_context_messages_to_store(self, context: SessionContext) -> list[Message]:
        """Get context messages that should be stored based on configuration."""
        if not self.store_context_messages:
            return []
        if self.store_context_from is not None:
            return context.get_messages(sources=self.store_context_from)
        return context.get_messages(exclude_sources={self.source_id})

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Load history into context. Skipped by the Agent when ``load_messages=False``."""
        history = await self.get_messages(context.session_id, state=state)
        context.extend_messages(self, history)

    async def after_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Store messages based on configuration.

        Storage order: context-to-store, then inputs, then outputs; nothing is
        saved when all buckets are empty.
        """
        messages_to_store: list[Message] = []
        messages_to_store.extend(self._get_context_messages_to_store(context))
        if self.store_inputs:
            messages_to_store.extend(context.input_messages)
        if self.store_outputs and context.response and context.response.messages:
            messages_to_store.extend(context.response.messages)
        if messages_to_store:
            await self.save_messages(context.session_id, messages_to_store, state=state)


# Sentinel conversation id for chrys-local history persistence. The Agent
# guards conversation-id write-back against it (see
# ``is_local_history_conversation_id``); pinned by a test in
# tests/kernel/test_sessions.py.
LOCAL_HISTORY_CONVERSATION_ID = "chrys_local_history_persistence"


def is_local_history_conversation_id(conversation_id: str | None) -> bool:
    """Return whether a conversation id is the local history-persistence sentinel."""
    return conversation_id == LOCAL_HISTORY_CONVERSATION_ID


class AgentSession:
    """Conversation identifiers and mutable provider state for an agent session.

    Lightweight state container. Provider instances are owned by the agent,
    not the session. The session only holds session IDs and a mutable state
    dict. ``chrys.service.state.serializers`` serializes that state.

    Attributes:
        session_id: Unique identifier for this session.
        service_session_id: Service-managed session ID (if using service-side
            storage).
        state: Mutable state dict shared with all providers.
        invalidated_service_session_ids: Service handles the exhaustion tail
            has withheld. The transcripts behind them hold stripped,
            never-answered calls, so a caller-supplied ``conversation_id``
            matching one is suppressed instead of re-sent alongside the local
            fallback replay. Runtime-only; not persisted.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        service_session_id: str | None = None,
    ):
        """Initialize the session.

        Args:
            session_id: Optional session ID (generated if not provided).
            service_session_id: Optional service-managed session ID.
        """
        self._session_id = session_id or str(uuid.uuid4())
        self.service_session_id = service_session_id
        self.state: dict[str, Any] = {}
        self.invalidated_service_session_ids: set[str] = set()

    @property
    def session_id(self) -> str:
        """The unique identifier for this session."""
        return self._session_id


class InMemoryHistoryProvider(HistoryProvider):
    """Built-in history provider that stores messages in session state.

    Messages are stored in ``state["messages"]`` as a list of ``Message``
    objects. This provider holds no instance state — all data lives in the
    session's state dict, passed as the named ``state`` parameter to
    :meth:`get_messages`/:meth:`save_messages`.

    This is the default provider auto-added by the Agent for local sessions
    when no providers are configured and service-side storage is not requested
    (``kernel.agent._prepare_run_context``).

    Exclusion filtering belongs to ``CompressibleHistoryProvider.get_messages``;
    this provider returns all stored messages.
    """

    DEFAULT_SOURCE_ID: ClassVar[str] = "in_memory"

    def __init__(
        self,
        source_id: str | None = None,
        *,
        load_messages: bool = True,
        store_inputs: bool = True,
        store_context_messages: bool = False,
        store_context_from: set[str] | None = None,
        store_outputs: bool = True,
    ) -> None:
        """Configure loading and storage for session-backed history.

        Args:
            source_id: Unique identifier for this provider instance.
                Defaults to ``DEFAULT_SOURCE_ID`` when not provided.
            load_messages: Whether to load messages before invocation.
            store_inputs: Whether to store input messages.
            store_context_messages: Whether to store context from other providers.
            store_context_from: If set, only store context from these source_ids.
            store_outputs: Whether to store response messages.
        """
        super().__init__(
            source_id=source_id or self.DEFAULT_SOURCE_ID,
            load_messages=load_messages,
            store_inputs=store_inputs,
            store_context_messages=store_context_messages,
            store_context_from=store_context_from,
            store_outputs=store_outputs,
        )

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        """Retrieve messages from session state.

        Returns a fresh list sharing the stored ``Message`` objects.
        """
        if state is None:
            return []
        return list(state.get("messages", []))

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist messages to session state.

        Replaces ``state["messages"]`` with a new list (never mutates the
        previous list object in place).
        """
        if state is None:
            return
        existing = state.get("messages", [])
        state["messages"] = [*existing, *messages]


class ServiceFallbackHistoryProvider(InMemoryHistoryProvider):
    """Local history fallback for sessions whose service-side handle was discarded.

    Installed by the Agent when the exhaustion tail invalidates a
    service-stored conversation handle: from that point on the service
    transcript is unreachable, so this provider persists every run's
    input/output locally through the standard ``after_run`` path.

    Loading is gated on the handles: while ``session.service_session_id`` is
    live — or the request itself carries a live conversation handle under any
    provider spelling — the service already holds the transcript that run
    continues, and a loaded local copy would ride the request alongside the
    handle as duplicated history contaminating that remote conversation.
    Only invalidated request handles don't count: the wire choke point
    strips those, so the replay must proceed in their place. When no live
    handle remains, ``before_run`` replays the locally persisted turns and
    the next run keeps its context instead of starting from the bare new
    user message.
    """

    DEFAULT_SOURCE_ID: ClassVar[str] = "service_history_fallback"

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Load persisted history only when no live continuation handle exists."""
        if session.service_session_id:
            return
        for handle in collect_conversation_handles(context.options):
            if handle not in session.invalidated_service_session_ids:
                return
        await super().before_run(agent=agent, session=session, context=context, state=state)
