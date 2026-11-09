# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Acceptance tests for the chrys-owned agent orchestrator.

Pins the run/stream orchestration invariants of ``chrys.kernel.agent``
against the production composition
``Agent(AgentTelemetryLayer, _AgentCore)`` over
``ToolLoopLayer(ChatMiddlewareLayer(wire))``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from chrys.kernel import (
    LOCAL_HISTORY_CONVERSATION_ID,
    TELEMETRY_GATE,
    Agent,
    AgentSession,
    ContextProvider,
    InMemoryHistoryProvider,
    LoopRecorder,
    ServiceFallbackHistoryProvider,
    SessionContext,
    UsageDetails,
)
from chrys.kernel.agent import (
    _AgentCore,
    _append_unique_tools,
    _get_tool_name,
    _merge_options,
)
from chrys.kernel.middleware import ChatMiddleware, FunctionMiddleware
from chrys.kernel.types import (
    AgentResponse,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from tests.kernel._fakes import _call_response, _call_update, _make_tool, _stack, _text_response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_update(text: str, **kwargs: Any) -> ChatResponseUpdate:
    """Local variant of the ``_fakes`` builder: forwards update-level metadata kwargs.

    The streaming tests need ``conversation_id=`` on individual updates,
    which the shared ``tests.kernel._fakes._text_update`` does not take.
    """
    return ChatResponseUpdate(contents=[Content.from_text(text)], role="assistant", **kwargs)


class _BareClient:
    """Minimal client recording the exact agent-level ``get_response`` surface.

    Stands in for the whole client stack where the loop/middleware layers are
    irrelevant. Non-stream turns are ``ChatResponse`` objects; stream turns
    are lists of ``ChatResponseUpdate`` (an ``Exception`` item raises
    mid-stream).
    """

    def __init__(self, turns: list[Any] | None = None, *, model: str | None = "bare-model") -> None:
        self.turns = list(turns) if turns is not None else None
        self.calls: list[dict[str, Any]] = []
        if model is not None:
            self.model = model

    def get_response(
        self,
        messages: Any = None,
        *,
        stream: bool = False,
        options: Any = None,
        compaction_strategy: Any = None,
        tokenizer: Any = None,
        function_invocation_kwargs: Any = None,
        client_kwargs: Any = None,
    ) -> Any:
        self.calls.append(
            {
                "messages": list(messages or []),
                "stream": stream,
                "options": dict(options or {}),
                "compaction_strategy": compaction_strategy,
                "tokenizer": tokenizer,
                "function_invocation_kwargs": dict(function_invocation_kwargs or {}),
                "client_kwargs": dict(client_kwargs or {}),
            }
        )
        default_turn: Any = [_text_update("done")] if stream else _text_response()
        turn = self.turns.pop(0) if self.turns is not None else default_turn
        if not stream:

            async def _resolve() -> ChatResponse:
                return turn

            return _resolve()

        async def _gen() -> Any:
            for update in turn:
                if isinstance(update, Exception):
                    raise update
                yield update

        return ResponseStream(_gen(), finalizer=ChatResponse.from_updates)


class _ForcedStatelessBareClient(_BareClient):
    STORES_BY_DEFAULT = True
    FORCES_STATELESS = True


class _LogChat(ChatMiddleware):
    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    async def process(self, context: Any, call_next: Callable[[], Awaitable[None]]) -> None:
        self.log.append(self.label)
        await call_next()


class _LogFn(FunctionMiddleware):
    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    async def process(self, context: Any, call_next: Callable[[], Awaitable[None]]) -> None:
        self.log.append(self.label)
        await call_next()


class _ForeignOnlyChat:
    """A middleware-shaped object that is NOT a chrys kernel middleware."""

    async def process(self, context: Any, call_next: Any) -> None:  # pragma: no cover - never executed
        await call_next()


class _RecordingProvider(ContextProvider):
    """Provider that records pipeline calls and optionally contributes context."""

    def __init__(
        self,
        source_id: str,
        *,
        instructions: str | None = None,
        messages: list[Message] | None = None,
        tools: list[Any] | None = None,
        middleware: list[Any] | None = None,
        log: list[str] | None = None,
    ) -> None:
        super().__init__(source_id)
        self._instructions = instructions
        self._messages = messages
        self._tools = tools
        self._middleware = middleware
        self.log = log if log is not None else []
        self.before_sessions: list[AgentSession] = []
        self.after_sessions: list[AgentSession] = []
        self.before_states: list[dict[str, Any]] = []
        self.after_states: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    async def before_run(self, *, agent: Any, session: AgentSession, context: SessionContext, state: dict) -> None:
        self.log.append(f"before:{self.source_id}")
        self.before_sessions.append(session)
        self.before_states.append(state)
        if self._instructions is not None:
            context.instructions.append(self._instructions)
        if self._messages is not None:
            context.extend_messages(self, self._messages)
        if self._tools is not None:
            context.tools.extend(self._tools)
        if self._middleware is not None:
            # Populate the provider's middleware bucket directly so this helper
            # exercises Agent's consumption of SessionContext.get_middleware(),
            # which flattens the buckets in provider order.
            context.middleware[self.source_id] = list(self._middleware)

    async def after_run(self, *, agent: Any, session: AgentSession, context: SessionContext, state: dict) -> None:
        self.log.append(f"after:{self.source_id}")
        self.after_sessions.append(session)
        self.after_states.append(state)
        self.responses.append(context.response)


class _SpyHistoryProvider(InMemoryHistoryProvider):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.before_calls = 0
        self.after_calls = 0

    async def before_run(self, **kwargs: Any) -> None:
        self.before_calls += 1
        await super().before_run(**kwargs)

    async def after_run(self, **kwargs: Any) -> None:
        self.after_calls += 1
        await super().after_run(**kwargs)


class _CountingRecorder(LoopRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.pre_calls = 0

    async def record_pre_call(self, messages: Any) -> None:
        self.pre_calls += 1
        await super().record_pre_call(messages)


@pytest.fixture
def otel_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TELEMETRY_GATE, "enabled", False)


@pytest.fixture
def otel_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # TELEMETRY_GATE defaults off and has no sticky-disable
    # machinery — flipping the flag is the whole opt-in.
    monkeypatch.setattr(TELEMETRY_GATE, "enabled", True)


# ---------------------------------------------------------------------------
# A. Constructor / default_options mirror
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_default_options_mirror(self) -> None:
        t = _make_tool()
        agent = Agent(client=_BareClient(), name="T", instructions="sys", tools=[t])
        assert agent.default_options == {
            "instructions": "sys",
            "tool_choice": "auto",
            "tools": [t],
            "model": "bare-model",
        }

    def test_default_options_none_instructions_filtered(self) -> None:
        agent = Agent(client=_BareClient())
        assert set(agent.default_options) == {"tool_choice", "tools", "model"}
        assert agent.default_options["tools"] == []

    def test_ctor_tools_list_copied_not_aliased(self) -> None:
        t1, t2 = _make_tool(name="t1"), _make_tool(name="t2")
        given = [t1]
        agent = Agent(client=_BareClient(), tools=given)
        given.append(t2)
        assert agent.default_options["tools"] == [t1]
        assert agent.default_options["tools"][0] is t1

    def test_ctor_rejects_distinct_tools_with_duplicate_names(self) -> None:
        first = _make_tool(name="duplicate")
        second = _make_tool(name="duplicate")

        with pytest.raises(ValueError, match="Duplicate tool name 'duplicate'"):
            Agent(client=_BareClient(), tools=[first, second])

    def test_ctor_deduplicates_repeated_same_tool_object(self) -> None:
        repeated = _make_tool(name="same")

        agent = Agent(client=_BareClient(), tools=[repeated, repeated])

        assert agent.default_options["tools"] == [repeated]

    def test_model_resolves_through_layered_client_stack(self) -> None:
        layer, wire = _stack([_text_response()])
        wire.model = "scripted-model"
        agent = Agent(client=layer)
        assert agent.default_options["model"] == "scripted-model"

    def test_no_model_attribute_omits_key(self) -> None:
        agent = Agent(client=_BareClient(model=None))
        assert "model" not in agent.default_options

    def test_id_defaults_to_uuid_and_explicit_id_kept(self) -> None:
        agent = Agent(client=_BareClient())
        uuid.UUID(agent.id)  # parses -> generated uuid4
        explicit = Agent(client=_BareClient(), id="my-id")
        assert explicit.id == "my-id"

    def test_middleware_stored_as_given(self) -> None:
        log: list[str] = []
        mw = [_LogChat("c", log)]
        agent = Agent(client=_BareClient(), middleware=mw)
        assert agent.middleware is mw
        assert Agent(client=_BareClient()).middleware is None

    def test_middleware_validated_at_construction(self) -> None:
        log: list[str] = []
        single = _LogFn("f", log)
        assert Agent(client=_BareClient(), middleware=single).middleware is single

        async def naked(context: Any, call_next: Any) -> None:  # pragma: no cover - never executed
            await call_next()

        with pytest.raises(TypeError, match="plain callables are not supported"):
            Agent(client=_BareClient(), middleware=[_LogChat("c", log), naked])  # type: ignore[list-item]
        with pytest.raises(TypeError):
            Agent(client=_BareClient(), middleware=_ForeignOnlyChat())  # type: ignore[arg-type]

    def test_context_providers_listified_and_additional_properties(self) -> None:
        provider = _RecordingProvider("p1")
        providers = (provider,)
        props: dict[str, Any] = {"k": "v"}
        agent = Agent(client=_BareClient(), context_providers=providers, additional_properties=props)
        assert agent.context_providers == [provider]
        assert isinstance(agent.context_providers, list)
        assert agent.additional_properties is props
        assert Agent(client=_BareClient()).additional_properties == {}

    def test_agent_provider_name_classvar_pin(self) -> None:
        # Must be a ClassVar: AgentTelemetryLayer reads it BEFORE
        # _AgentCore.__init__ runs, so an instance field would be too late.
        # Agent spans identify their gen_ai.provider.name as "chrys".
        assert "AGENT_PROVIDER_NAME" in vars(_AgentCore)
        assert _AgentCore.AGENT_PROVIDER_NAME == "chrys"
        agent = Agent(client=_BareClient())
        assert agent.otel_provider_name == "chrys"


# ---------------------------------------------------------------------------
# B. run() delegation and laziness
# ---------------------------------------------------------------------------


class TestRunDelegation:
    def test_non_stream_returns_unawaited_coroutine_zero_side_effects(self, otel_off: None) -> None:
        client = _BareClient()
        provider = _RecordingProvider("p1")
        agent = Agent(client=client, context_providers=[provider])
        coro = agent.run("hi")
        assert asyncio.iscoroutine(coro)
        assert client.calls == []
        assert provider.log == []
        coro.close()

    @pytest.mark.asyncio
    async def test_stream_returns_response_stream_synchronously_zero_execution(self, otel_off: None) -> None:
        client = _BareClient()
        provider = _RecordingProvider("p1")
        agent = Agent(client=client, context_providers=[provider])
        stream = agent.run("hi", stream=True)
        assert isinstance(stream, ResponseStream)
        assert client.calls == []
        assert provider.log == []
        [u async for u in stream]  # drain so the held coroutine is consumed

    @pytest.mark.asyncio
    async def test_session_injected_and_loop_recorder_passthrough(self) -> None:
        layer, wire = _stack([_text_response()])
        agent = Agent(client=layer, name="T")
        recorder = _CountingRecorder()
        session = AgentSession(service_session_id="svc")  # blocks auto-injection
        await agent.run("hi", session=session, client_kwargs={"loop_recorder": recorder})
        assert recorder.pre_calls == 1, "loop_recorder must reach the ToolLoopLayer"
        inner_client_kwargs = wire.calls[0]["kwargs"]["client_kwargs"]
        assert "loop_recorder" not in inner_client_kwargs, "the loop pops its per-run keys"
        assert "session" not in inner_client_kwargs

    @pytest.mark.asyncio
    async def test_compaction_and_tokenizer_run_level_passthrough(self) -> None:
        client = _BareClient()
        agent = Agent(client=client)
        comp, tok = object(), object()
        await agent.run("hi", compaction_strategy=comp, tokenizer=tok)
        assert client.calls[0]["compaction_strategy"] is comp
        assert client.calls[0]["tokenizer"] is tok
        # Omitted -> ctor attributes (None) via the `x or self.x` fallback.
        await agent.run("hi")
        assert client.calls[1]["compaction_strategy"] is None
        assert client.calls[1]["tokenizer"] is None

    @pytest.mark.asyncio
    async def test_function_invocation_kwargs_merge_order(self) -> None:
        client = _BareClient()
        agent = Agent(client=client)
        await agent.run(
            "hi",
            options={"additional_function_arguments": {"a": 1, "b": 2}},
            function_invocation_kwargs={"a": 9, "c": 3},
        )
        # options-level additional_function_arguments win over the kwarg (:1240).
        assert client.calls[0]["function_invocation_kwargs"] == {"a": 1, "b": 2, "c": 3}

    @pytest.mark.asyncio
    async def test_caller_options_dict_not_mutated(self) -> None:
        client = _BareClient()
        agent = Agent(client=client, instructions="sys")
        t = _make_tool()
        options: dict[str, Any] = {
            "temperature": 0.5,
            "tools": [t],
            "conversation_id": "c-1",
            "additional_function_arguments": {"x": 1},
            "custom_key": "kept",
        }
        snapshot = dict(options)
        inner_args = options["additional_function_arguments"]
        await agent.run("hi", options=options)
        assert options == snapshot
        assert options["additional_function_arguments"] is inner_args
        assert options["additional_function_arguments"] == {"x": 1}
        # ...and the provider-specific passthrough reached the client.
        assert client.calls[0]["options"]["custom_key"] == "kept"
        assert client.calls[0]["options"]["temperature"] == 0.5


# ---------------------------------------------------------------------------
# C. Middleware bucketing and forwarding
# ---------------------------------------------------------------------------


class TestMiddlewareMerge:
    @pytest.mark.asyncio
    async def test_ctor_only_middleware_forwarded_function_first(self) -> None:
        log: list[str] = []
        fn1, chat1 = _LogFn("f1", log), _LogChat("c1", log)
        client = _BareClient()
        agent = Agent(client=client, middleware=[chat1, fn1])
        await agent.run("hi")
        assert client.calls[0]["client_kwargs"]["middleware"] == [fn1, chat1]

    @pytest.mark.asyncio
    async def test_run_only_middleware_forwarded(self) -> None:
        log: list[str] = []
        fn2, chat2 = _LogFn("f2", log), _LogChat("c2", log)
        client = _BareClient()
        agent = Agent(client=client)
        await agent.run("hi", middleware=[chat2, fn2])
        assert client.calls[0]["client_kwargs"]["middleware"] == [fn2, chat2]

    @pytest.mark.asyncio
    async def test_mixed_ctor_run_merge_order(self) -> None:
        log: list[str] = []
        fn1, chat1 = _LogFn("f1", log), _LogChat("c1", log)
        fn2, chat2 = _LogFn("f2", log), _LogChat("c2", log)
        client = _BareClient()
        agent = Agent(client=client, middleware=[chat1, fn1])
        await agent.run("hi", middleware=[chat2, fn2])
        # Agent.run merges [ctor-function, ctor-chat, run-function, run-chat].
        assert client.calls[0]["client_kwargs"]["middleware"] == [fn1, chat1, fn2, chat2]

    @pytest.mark.asyncio
    async def test_pipeline_execution_order_end_to_end(self) -> None:
        """Run-level middleware are innermost — the sub_agent validation contract."""
        log: list[str] = []
        layer, _wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response("final")])
        agent = Agent(
            client=layer,
            name="T",
            tools=[_make_tool(log)],
            middleware=[_LogChat("chat:ctor", log), _LogFn("fn:ctor", log)],
        )
        await agent.run("hi", middleware=[_LogChat("chat:run", log), _LogFn("fn:run", log)])
        assert log == [
            "chat:ctor",
            "chat:run",
            "fn:ctor",
            "fn:run",
            "tool:echo:a",
            "chat:ctor",
            "chat:run",
        ]

    @pytest.mark.asyncio
    async def test_empty_middleware_omits_key(self) -> None:
        client = _BareClient()
        agent = Agent(client=client)
        await agent.run("hi")
        assert "middleware" not in client.calls[0]["client_kwargs"]

    def test_plain_callable_rejected_synchronously(self) -> None:
        agent = Agent(client=_BareClient())

        async def naked(context: Any, call_next: Any) -> None:  # pragma: no cover - never executed
            await call_next()

        with pytest.raises(TypeError, match="plain callables are not supported"):
            agent.run("hi", middleware=[naked])

    def test_framework_middleware_instance_rejected(self) -> None:
        agent = Agent(client=_BareClient())
        with pytest.raises(TypeError):
            agent.run("hi", middleware=[_ForeignOnlyChat()])

    @pytest.mark.asyncio
    async def test_run_level_middleware_not_sticky(self) -> None:
        log: list[str] = []
        fn1 = _LogFn("f1", log)
        fn2 = _LogFn("f2", log)
        client = _BareClient()
        agent = Agent(client=client, middleware=[fn1])
        await agent.run("hi", middleware=[fn2])
        await agent.run("hi")
        assert client.calls[0]["client_kwargs"]["middleware"] == [fn1, fn2]
        assert client.calls[1]["client_kwargs"]["middleware"] == [fn1]


# ---------------------------------------------------------------------------
# D. _prepare_run_context
# ---------------------------------------------------------------------------


class TestPrepareRunContext:
    @pytest.mark.asyncio
    async def test_auto_inject_in_memory_history_provider_once(self) -> None:
        agent = Agent(client=_BareClient(), name="T")
        session = AgentSession()
        await agent.run("hi", session=session)
        assert len(agent.context_providers) == 1
        assert isinstance(agent.context_providers[0], InMemoryHistoryProvider)
        # Permanent agent-state append: the second run must not duplicate it.
        await agent.run("again", session=session)
        assert len(agent.context_providers) == 1

    @pytest.mark.asyncio
    async def test_auto_inject_blocked_by_store_true(self) -> None:
        # The plain always-loading provider stays blocked (it would replay
        # local history into the service-managed thread); service storage
        # instead installs the handle-gated shadow so turns survive a later
        # handle invalidation.
        agent = Agent(client=_BareClient())
        await agent.run("hi", session=AgentSession(), options={"store": True})
        assert [type(p) for p in agent.context_providers] == [ServiceFallbackHistoryProvider]

    @pytest.mark.asyncio
    async def test_auto_inject_blocked_by_options_conversation_id(self) -> None:
        agent = Agent(client=_BareClient())
        await agent.run("hi", session=AgentSession(), options={"conversation_id": "c-1"})
        assert agent.context_providers == []

    @pytest.mark.asyncio
    async def test_auto_inject_blocked_by_native_handle_spellings(self) -> None:
        # Every CONVERSATION_HANDLE_KEYS spelling means the service owns
        # history for this run — top-level, mapping-form, or nested in
        # extra_body — so none of them may admit a local history injection.
        for options in (
            {"previous_response_id": "resp-1"},
            {"conversation": {"id": "c-1"}},
            {"extra_body": {"conversation_id": "c-1"}},
            {"continuation_token": {"response_id": "resp-1"}},
        ):
            agent = Agent(client=_BareClient())
            await agent.run("hi", session=AgentSession(), options=options)
            assert agent.context_providers == [], f"injection must stay blocked for {options}"

    @pytest.mark.asyncio
    async def test_auto_inject_blocked_by_service_session_id(self) -> None:
        agent = Agent(client=_BareClient())
        await agent.run("hi", session=AgentSession(service_session_id="svc"))
        assert agent.context_providers == []

    @pytest.mark.asyncio
    async def test_auto_inject_blocked_by_stores_by_default_via_delegation(self) -> None:
        layer, wire = _stack([_text_response()])
        wire.STORES_BY_DEFAULT = True  # resolves through two __getattr__ hops
        agent = Agent(client=layer)
        await agent.run("hi", session=AgentSession())
        assert [type(p) for p in agent.context_providers] == [ServiceFallbackHistoryProvider]

    @pytest.mark.asyncio
    async def test_auto_inject_blocked_by_explicit_store_none_with_stores_by_default(self) -> None:
        # store=None means "unset" and falls back to STORES_BY_DEFAULT.
        # (Treating None as falsy would inject the plain provider — replaying
        # local history into a service-managed thread.)
        layer, wire = _stack([_text_response()])
        wire.STORES_BY_DEFAULT = True
        agent = Agent(client=layer)
        await agent.run("hi", session=AgentSession(), options={"store": None})
        assert [type(p) for p in agent.context_providers] == [ServiceFallbackHistoryProvider]

    @pytest.mark.asyncio
    async def test_auto_inject_proceeds_with_explicit_store_false_despite_stores_by_default(self) -> None:
        # Explicit store=False outranks the client default: local in-memory
        # history injection proceeds.
        layer, wire = _stack([_text_response()])
        wire.STORES_BY_DEFAULT = True
        agent = Agent(client=layer)
        await agent.run("hi", session=AgentSession(), options={"store": False})
        assert len(agent.context_providers) == 1
        assert isinstance(agent.context_providers[0], InMemoryHistoryProvider)

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("injection_point", ["defaults", "options", "client_kwargs", "client_kwargs_extra"])
    @pytest.mark.parametrize(
        "store_options",
        [
            {"store": True},
            {"store": False},
            {"extra_body": {"store": True}},
            {"extra_body": {"store": False}},
            {"extra_body": {"store": None}},
            {"store": True, "extra_body": {"store": False}},
            {"store": False, "extra_body": {"store": True}},
        ],
    )
    async def test_forced_stateless_store_matrix_always_injects_local_history(
        self,
        stream: bool,
        injection_point: str,
        store_options: dict[str, Any],
    ) -> None:
        client = _ForcedStatelessBareClient()
        defaults = store_options if injection_point == "defaults" else None
        options = store_options if injection_point == "options" else None
        if injection_point == "client_kwargs":
            client_kwargs = store_options
        elif injection_point == "client_kwargs_extra":
            value = store_options.get("store", True)
            client_kwargs = {"extra_body": {"store": value}}
        else:
            client_kwargs = None
        agent = Agent(client=client)
        if defaults is not None:
            agent.default_options.update(defaults)

        result = agent.run(
            "hi",
            stream=stream,
            session=AgentSession(),
            options=options,
            client_kwargs=client_kwargs,
        )
        if stream:
            await result.get_final_response()
        else:
            await result

        assert [type(provider) for provider in agent.context_providers] == [InMemoryHistoryProvider]

    @pytest.mark.parametrize(
        "options",
        [
            {"conversation_id": "conv"},
            {"previous_response_id": "resp"},
            {"conversation": {"id": "thread"}},
            {"continuation_token": {"response_id": "pending"}},
            {"background": True},
            {"extra_body": {"previous_response_id": "nested"}},
        ],
    )
    async def test_forced_stateless_handle_reflection_still_injects_local_history(
        self,
        options: dict[str, Any],
    ) -> None:
        agent = Agent(client=_ForcedStatelessBareClient())

        await agent.run("hi", session=AgentSession(), options=options)

        assert [type(provider) for provider in agent.context_providers] == [InMemoryHistoryProvider]

    async def test_forced_stateless_reflection_uses_sanitized_view_without_mutating_wire_options(self) -> None:
        client = _ForcedStatelessBareClient()
        agent = Agent(client=client)
        session = AgentSession()
        session.invalidated_service_session_ids.add("old")
        options = {
            "store": True,
            "conversation_id": "live",
            "previous_response_id": "old",
            "extra_body": {"conversation": {"id": "nested"}},
        }

        await agent.run("hi", session=session, options=options)

        assert [type(provider) for provider in agent.context_providers] == [InMemoryHistoryProvider]
        assert client.calls[0]["options"]["conversation_id"] == "live"
        assert "previous_response_id" not in client.calls[0]["options"]
        assert client.calls[0]["options"]["extra_body"] == {"conversation": {"id": "nested"}}

    @pytest.mark.asyncio
    async def test_providers_with_no_session_get_temporary_session(self) -> None:
        client = _BareClient()
        provider = _RecordingProvider("p1")
        agent = Agent(client=client, context_providers=[provider])
        await agent.run("hi")
        injected = client.calls[0]["client_kwargs"]["session"]
        assert isinstance(injected, AgentSession)
        assert provider.before_sessions[0] is injected

    @pytest.mark.asyncio
    async def test_no_session_no_providers_omits_session_key(self) -> None:
        client = _BareClient()
        agent = Agent(client=client)
        await agent.run("hi")
        assert "session" not in client.calls[0]["client_kwargs"]

    @pytest.mark.asyncio
    async def test_conversation_id_tail_override_order(self) -> None:
        client = _BareClient(turns=[_text_response(), _text_response()])
        agent = Agent(client=client)
        session = AgentSession(service_session_id="svc-1")
        # Session-derived value when options carry no conversation_id...
        await agent.run("hi", session=session)
        assert client.calls[0]["options"]["conversation_id"] == "svc-1"
        # ...but an options-level conversation_id wins through the trailing
        # **opts spread because the key is NOT popped when a session exists
        # (:1246-1248,:1264).
        await agent.run("hi", session=session, options={"conversation_id": "override-9"})
        assert client.calls[1]["options"]["conversation_id"] == "override-9"

    @pytest.mark.asyncio
    async def test_run_options_model_overrides_ctor_model(self) -> None:
        client = _BareClient(turns=[_text_response(), _text_response()])
        agent = Agent(client=client)  # ctor resolves model="bare-model" from the client
        await agent.run("hi", options={"model": "run-model"})
        assert client.calls[0]["options"]["model"] == "run-model"
        await agent.run("hi")
        assert client.calls[1]["options"]["model"] == "bare-model"

    @pytest.mark.asyncio
    async def test_options_tools_used_when_named_param_absent(self) -> None:
        t_opt = _make_tool(name="opt_tool")
        t_named = _make_tool(name="named_tool")
        client = _BareClient(turns=[_text_response(), _text_response()])
        agent = Agent(client=client)
        await agent.run("hi", options={"tools": [t_opt]})
        assert client.calls[0]["options"]["tools"] == [t_opt]
        # When BOTH are given, _AgentCore._prepare_run_context short-circuits
        # opts.pop, so the options-level list survives into the trailing **opts
        # spread and replaces the named tools in run_opts. Pin this precedence
        # so refactoring option assembly cannot silently change tool selection.
        await agent.run("hi", tools=[t_named], options={"tools": [t_opt]})
        assert client.calls[1]["options"]["tools"] == [t_opt]

    @pytest.mark.asyncio
    async def test_options_tools_iterator_reaches_the_client(self) -> None:
        # The telemetry layer merges options before the run reads them; that
        # merge must not consume a one-shot iterator.
        t_opt = _make_tool(name="opt_tool")
        client = _BareClient(turns=[_text_response()])
        agent = Agent(client=client)

        await agent.run("hi", options={"tools": iter([t_opt])})

        assert client.calls[0]["options"]["tools"] == [t_opt]

    @pytest.mark.asyncio
    async def test_provider_contributed_middleware_appended_after_agent_middleware(self) -> None:
        log: list[str] = []
        agent_fn = _LogFn("agent-fn", log)
        provider_chat = _LogChat("provider-chat", log)
        provider = _RecordingProvider("p1", middleware=[provider_chat])
        client = _BareClient()
        agent = Agent(client=client, context_providers=[provider], middleware=[agent_fn])
        await agent.run("hi")
        assert client.calls[0]["client_kwargs"]["middleware"] == [agent_fn, provider_chat]

    @pytest.mark.asyncio
    async def test_provider_contributed_non_kernel_middleware_rejected(self) -> None:
        provider = _RecordingProvider("p1", middleware=[_ForeignOnlyChat()])
        agent = Agent(client=_BareClient(), context_providers=[provider])
        with pytest.raises(TypeError):
            await agent.run("hi")


# ---------------------------------------------------------------------------
# E. Context provider pipeline
# ---------------------------------------------------------------------------


class TestProviderPipeline:
    @pytest.mark.asyncio
    async def test_before_forward_after_reverse_order(self) -> None:
        log: list[str] = []
        p1 = _RecordingProvider("p1", log=log)
        p2 = _RecordingProvider("p2", log=log)
        agent = Agent(client=_BareClient(), context_providers=[p1, p2])
        await agent.run("hi")
        assert log == ["before:p1", "before:p2", "after:p2", "after:p1"]

    @pytest.mark.asyncio
    async def test_history_provider_load_messages_false_skips_before_not_after(self) -> None:
        spy = _SpyHistoryProvider(load_messages=False)
        agent = Agent(client=_BareClient(), context_providers=[spy])
        await agent.run("hi")
        assert spy.before_calls == 0
        assert spy.after_calls == 1

    @pytest.mark.asyncio
    async def test_non_history_provider_does_not_suppress_local_history(self) -> None:
        client = _BareClient(turns=[_text_response("one"), _text_response("two")])
        provider = _RecordingProvider("context-only")
        agent = Agent(client=client, context_providers=[provider])
        session = AgentSession()

        await agent.run("first", session=session)
        await agent.run("second", session=session)

        assert len(agent.context_providers) == 2
        assert isinstance(agent.context_providers[-1], InMemoryHistoryProvider)
        assert [message.text for message in client.calls[1]["messages"]] == ["first", "one", "second"]

    @pytest.mark.asyncio
    async def test_loading_history_provider_prevents_duplicate_local_history(self) -> None:
        provider = _SpyHistoryProvider(load_messages=True)
        agent = Agent(client=_BareClient(), context_providers=[provider])

        await agent.run("hi", session=AgentSession())

        assert agent.context_providers == [provider]

    @pytest.mark.asyncio
    async def test_persist_only_history_provider_does_not_suppress_local_history(self) -> None:
        provider = _SpyHistoryProvider(load_messages=False)
        agent = Agent(client=_BareClient(), context_providers=[provider])

        await agent.run("hi", session=AgentSession())

        assert len(agent.context_providers) == 2
        assert agent.context_providers[0] is provider
        assert isinstance(agent.context_providers[1], InMemoryHistoryProvider)

    @pytest.mark.asyncio
    async def test_provider_instructions_concatenated(self) -> None:
        client = _BareClient(turns=[_text_response()])
        provider = _RecordingProvider("p1", instructions="extra")
        agent = Agent(client=client, instructions="base", context_providers=[provider])
        await agent.run("hi")
        assert client.calls[0]["options"]["instructions"] == "base\nextra"

    @pytest.mark.asyncio
    async def test_provider_instructions_without_agent_instructions(self) -> None:
        client = _BareClient()
        provider = _RecordingProvider("p1", instructions="only")
        agent = Agent(client=client, context_providers=[provider])
        await agent.run("hi")
        assert client.calls[0]["options"]["instructions"] == "only"

    @pytest.mark.asyncio
    async def test_provider_tools_merged_after_ctor_tools(self) -> None:
        ctor_tool = _make_tool(name="ctor_tool")
        provider_tool = _make_tool(name="provider_tool")
        run_tool = _make_tool(name="run_tool")
        client = _BareClient()
        provider = _RecordingProvider("p1", tools=[provider_tool])
        agent = Agent(client=client, tools=[ctor_tool], context_providers=[provider])
        await agent.run("hi", tools=[run_tool])
        assert client.calls[0]["options"]["tools"] == [ctor_tool, provider_tool, run_tool]

    @pytest.mark.asyncio
    async def test_provider_state_setdefault_identity_across_runs(self) -> None:
        provider = _RecordingProvider("p1")
        agent = Agent(client=_BareClient(), context_providers=[provider])
        session = AgentSession()
        await agent.run("hi", session=session)
        await agent.run("again", session=session)
        assert provider.before_states[0] is session.state["p1"]
        assert provider.after_states[0] is session.state["p1"]
        assert provider.before_states[1] is session.state["p1"]

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.asyncio
    async def test_provider_observes_aggregate_and_latest_tool_loop_usage(self, stream: bool) -> None:
        if stream:
            turns = [
                [
                    _call_update("c1", "echo", {"text": "a"}),
                    ChatResponseUpdate(
                        contents=[Content.from_usage(UsageDetails(input_token_count=40))],
                        role="assistant",
                    ),
                ],
                [
                    _text_update("done"),
                    ChatResponseUpdate(
                        contents=[Content.from_usage(UsageDetails(input_token_count=45))],
                        role="assistant",
                    ),
                ],
            ]
        else:
            turns = [
                _call_response(
                    ("c1", "echo", {"text": "a"}),
                    usage_details=UsageDetails(input_token_count=40),
                ),
                _text_response("done", usage_details=UsageDetails(input_token_count=45)),
            ]
        client, _wire = _stack(turns)
        provider = _RecordingProvider("p1")
        agent = Agent(client=client, tools=[_make_tool()], context_providers=[provider])

        if stream:
            response_stream = agent.run("hi", stream=True)
            _ = [update async for update in response_stream]
            response = await response_stream.get_final_response()
        else:
            response = await agent.run("hi")

        assert provider.responses == [response]
        assert response.usage_details == {"input_token_count": 85}
        assert response.latest_usage_details == {"input_token_count": 45}

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.asyncio
    async def test_latest_usage_is_none_when_final_call_reports_no_usage(self, stream: bool) -> None:
        """A final call reporting no usage must surface latest=None (so
        force-compress skips conservatively) — never fall back to the
        tool-loop billing aggregate."""
        if stream:
            turns = [
                [
                    _call_update("c1", "echo", {"text": "a"}),
                    ChatResponseUpdate(
                        contents=[Content.from_usage(UsageDetails(input_token_count=40))],
                        role="assistant",
                    ),
                ],
                [_text_update("done")],
            ]
        else:
            turns = [
                _call_response(
                    ("c1", "echo", {"text": "a"}),
                    usage_details=UsageDetails(input_token_count=40),
                ),
                _text_response("done"),
            ]
        client, _wire = _stack(turns)
        provider = _RecordingProvider("p1")
        agent = Agent(client=client, tools=[_make_tool()], context_providers=[provider])

        if stream:
            response_stream = agent.run("hi", stream=True)
            _ = [update async for update in response_stream]
            response = await response_stream.get_final_response()
        else:
            response = await agent.run("hi")

        assert provider.responses == [response]
        assert response.usage_details == {"input_token_count": 40}
        assert response.latest_usage_details is None

    @pytest.mark.asyncio
    async def test_session_messages_are_context_then_input(self) -> None:
        client = _BareClient()
        ctx_msg = Message(role="system", contents=["ctx"])
        provider = _RecordingProvider("p1", messages=[ctx_msg])
        agent = Agent(client=client, context_providers=[provider])
        await agent.run("hi")
        sent = client.calls[0]["messages"]
        assert [m.text for m in sent] == ["ctx", "hi"]
        assert [m.role for m in sent] == ["system", "user"]


# ---------------------------------------------------------------------------
# F. Non-streaming finalize
# ---------------------------------------------------------------------------


class TestNonStreamingFinalize:
    @pytest.mark.asyncio
    async def test_author_name_backfilled_not_overwritten(self) -> None:
        preset = Message(role="assistant", contents=["a"], author_name="other")
        blank = Message(role="assistant", contents=["b"])
        client = _BareClient(turns=[ChatResponse(messages=[preset, blank], response_id="r")])
        agent = Agent(client=client, name="T")
        response = await agent.run("hi")
        assert response.messages[0].author_name == "other"
        assert response.messages[1].author_name == "T"

    @pytest.mark.asyncio
    async def test_conversation_id_written_back_to_session(self) -> None:
        client = _BareClient(turns=[_text_response(conversation_id="svc-2")])
        agent = Agent(client=client)
        session = AgentSession(service_session_id="svc-old")  # also blocks auto-inject
        await agent.run("hi", session=session)
        assert session.service_session_id == "svc-2"

    @pytest.mark.asyncio
    async def test_local_history_sentinel_never_written_back(self) -> None:
        client = _BareClient(turns=[_text_response(conversation_id=LOCAL_HISTORY_CONVERSATION_ID)])
        agent = Agent(client=client, context_providers=[_RecordingProvider("p1")])
        session = AgentSession()
        await agent.run("hi", session=session)
        assert session.service_session_id is None

    @pytest.mark.asyncio
    async def test_response_exposed_to_after_providers_via_property(self) -> None:
        client = _BareClient(turns=[_text_response("answer", response_id="rid-1")])
        provider = _RecordingProvider("p1")
        agent = Agent(client=client, context_providers=[provider])
        response = await agent.run("hi")
        seen = provider.responses[0]
        assert seen is response
        assert isinstance(seen, AgentResponse)
        assert seen.response_id == "rid-1"
        assert seen.messages[0].text == "answer"

    @pytest.mark.asyncio
    async def test_agent_response_field_mapping(self) -> None:
        chat_response = _text_response(
            response_id="rid-2",
            created_at="2026-01-01",
            finish_reason="stop",
            usage_details=UsageDetails(input_token_count=81, output_token_count=7),
            continuation_token={"cursor": "next"},
            additional_properties={"k": "v"},
        )
        client = _BareClient(turns=[chat_response])
        agent = Agent(client=client, name="T")
        response = await agent.run("hi")
        assert isinstance(response, AgentResponse)
        assert response.response_id == "rid-2"
        assert response.created_at == "2026-01-01"
        assert response.finish_reason == "stop"
        assert response.usage_details == {"input_token_count": 81, "output_token_count": 7}
        assert response.latest_usage_details == {"input_token_count": 81, "output_token_count": 7}
        assert response.continuation_token == {"cursor": "next"}
        assert response.additional_properties == {"k": "v"}
        assert response.raw_representation is chat_response

    @pytest.mark.asyncio
    async def test_malformed_structured_output_stays_lazy_through_after_run(self) -> None:
        chat_response = _text_response("not-json")
        provider = _RecordingProvider("p1")
        agent = Agent(client=_BareClient(turns=[chat_response]), context_providers=[provider])

        response = await agent.run("hi", options={"response_format": {"type": "object"}})

        assert provider.responses == [response]
        with pytest.raises(ValueError, match="not valid JSON"):
            _ = response.value

    @pytest.mark.asyncio
    async def test_preparsed_chat_value_is_preserved_without_reparsing(self) -> None:
        parsed = {"answer": 42}
        chat_response = ChatResponse(
            messages=[Message("assistant", ["not-json"])],
            value=parsed,
            response_format={"type": "object"},
        )
        agent = Agent(client=_BareClient(turns=[chat_response]))

        response = await agent.run("hi", options={"response_format": {"type": "object"}})

        assert response.value is parsed


# ---------------------------------------------------------------------------
# G. Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    @pytest.mark.asyncio
    async def test_updates_carry_agent_name_via_map_transform(self) -> None:
        client = _BareClient()
        agent = Agent(client=client, name="T")
        stream = agent.run("hi", stream=True)
        updates = [u async for u in stream]
        assert [u.author_name for u in updates] == ["T"]
        final = await stream.get_final_response()
        assert final.messages[0].author_name == "T"

    @pytest.mark.asyncio
    async def test_unnamed_agent_dual_source_author_name(self) -> None:
        # Deliberate framework asymmetry: the map transform uses self.name
        # (None -> updates unattributed) while the result hook backfills with
        # _get_agent_name() ("UnnamedAgent").
        client = _BareClient()
        agent = Agent(client=client)
        stream = agent.run("hi", stream=True)
        updates = [u async for u in stream]
        assert [u.author_name for u in updates] == [None]
        final = await stream.get_final_response()
        assert final.messages[0].author_name == "UnnamedAgent"

    @pytest.mark.asyncio
    async def test_conversation_id_propagates_during_iteration(self) -> None:
        client = _BareClient(turns=[[_text_update("a", conversation_id="conv-7"), _text_update("b")]])
        agent = Agent(client=client)
        session = AgentSession(service_session_id="svc")  # blocks auto-inject
        stream = agent.run("hi", stream=True, session=session)
        iterator = stream.__aiter__()
        await iterator.__anext__()
        # Mid-stream, before exhaustion: the transform hook already wrote it.
        assert session.service_session_id == "conv-7"

    @pytest.mark.asyncio
    async def test_sentinel_conversation_id_not_propagated_during_iteration(self) -> None:
        client = _BareClient(turns=[[_text_update("a", conversation_id=LOCAL_HISTORY_CONVERSATION_ID)]])
        agent = Agent(client=client, context_providers=[_RecordingProvider("p1")])
        session = AgentSession()
        stream = agent.run("hi", stream=True, session=session)
        [u async for u in stream]
        await stream.get_final_response()
        assert session.service_session_id is None

    @pytest.mark.asyncio
    async def test_after_run_fires_on_pure_iteration_exhaustion(self) -> None:
        provider = _RecordingProvider("p1")
        agent = Agent(client=_BareClient(), context_providers=[provider])
        stream = agent.run("hi", stream=True)
        async for _ in stream:
            pass
        # No explicit get_final_response(): natural exhaustion auto-finalizes
        # (_types.py:3111-3115) and must run the after_run result hook.
        assert provider.log.count("after:p1") == 1

    @pytest.mark.asyncio
    async def test_after_provider_observes_exact_streaming_response_with_metadata(self) -> None:
        provider = _RecordingProvider("p1")
        updates = [
            ChatResponseUpdate(
                contents=[
                    Content.from_text("done"),
                    Content.from_usage(UsageDetails(input_token_count=91, output_token_count=1)),
                ],
                role="assistant",
                response_id="stream-rid",
                created_at="2026-01-02",
                finish_reason="stop",
                continuation_token={"cursor": "stream-next"},
                additional_properties={"stream": True},
            ),
            ChatResponseUpdate(
                contents=[Content.from_usage(UsageDetails(input_token_count=91, output_token_count=5))],
                role="assistant",
                continuation_token={"cursor": "stream-next"},
            ),
        ]
        agent = Agent(client=_BareClient(turns=[updates]), context_providers=[provider])

        stream = agent.run("hi", stream=True)
        [update async for update in stream]
        response = await stream.get_final_response()

        assert provider.responses == [response]
        assert response.response_id == "stream-rid"
        assert response.created_at == "2026-01-02"
        assert response.finish_reason == "stop"
        assert response.usage_details == {"input_token_count": 91, "output_token_count": 5}
        assert response.latest_usage_details == {"input_token_count": 91, "output_token_count": 5}
        assert response.continuation_token == {"cursor": "stream-next"}
        assert response.additional_properties == {"stream": True}

    @pytest.mark.asyncio
    async def test_error_stream_skips_after_run(self) -> None:
        provider = _RecordingProvider("p1")
        client = _BareClient(turns=[[_text_update("a"), RuntimeError("boom")]])
        agent = Agent(client=client, context_providers=[provider])
        stream = agent.run("hi", stream=True)
        with pytest.raises(RuntimeError, match="boom"):
            async for _ in stream:
                pass
        assert provider.log.count("after:p1") == 0

    @pytest.mark.asyncio
    async def test_two_round_tool_loop_final_state_shape(self) -> None:
        events: list[str] = []
        layer, wire = _stack([[_call_update("c1", "echo", {"text": "a"})], [_text_update("final")]])
        agent = Agent(client=layer, name="T", tools=[_make_tool(events)])
        stream = agent.run("hi", stream=True)
        updates = [u async for u in stream]
        final = await stream.get_final_response()
        assert events == ["tool:echo:a"]
        assert len(wire.calls) == 2
        assert [u.role for u in updates] == ["assistant", "tool", "assistant"]
        kinds = [item.type for msg in final.messages for item in msg.contents]
        assert "function_call" in kinds
        assert "function_result" in kinds
        assert final.text == "final"
        # author backfill applies to every aggregated message (post-hook).
        assert all(m.author_name is not None for m in final.messages)

    @pytest.mark.asyncio
    async def test_empty_final_round_still_finalizes(self) -> None:
        empty_final = ChatResponseUpdate(contents=[], role="assistant", finish_reason="stop")
        layer, _wire = _stack([[_call_update("c1", "echo", {"text": "a"})], [empty_final]])
        agent = Agent(client=layer, name="T", tools=[_make_tool()])
        stream = agent.run("hi", stream=True)
        [u async for u in stream]
        final = await stream.get_final_response()
        kinds = [item.type for msg in final.messages for item in msg.contents]
        assert "function_call" in kinds
        assert "function_result" in kinds
        assert final.text == ""

    def test_extract_conversation_id_from_streaming_response(self) -> None:
        extract = _AgentCore._extract_conversation_id_from_streaming_response

        class _Raw:
            def __init__(self, conversation_id: Any) -> None:
                self.conversation_id = conversation_id

        assert extract(AgentResponse(messages=[])) is None
        assert extract(AgentResponse(messages=[], raw_representation=_Raw("c-attr"))) == "c-attr"
        assert extract(AgentResponse(messages=[], raw_representation={"conversation_id": "c-map"})) == "c-map"
        # List form scans in reverse and skips empty/non-string values.
        raw_list = [_Raw("c-old"), {"conversation_id": ""}, _Raw(None)]
        assert extract(AgentResponse(messages=[], raw_representation=raw_list)) == "c-old"

    @pytest.mark.asyncio
    async def test_manual_cleanup_idempotent_after_consumption(self) -> None:
        provider = _RecordingProvider("p1")
        agent = Agent(client=_BareClient(), context_providers=[provider])
        stream = agent.run("hi", stream=True)
        [u async for u in stream]
        await stream.get_final_response()
        assert provider.log.count("after:p1") == 1
        # The executor's stall path calls this manually; it must be reentrant.
        await stream._run_cleanup_hooks()
        await stream._run_cleanup_hooks()
        assert provider.log.count("after:p1") == 1

    @pytest.mark.asyncio
    async def test_manual_cleanup_unconsumed_otel_asymmetry(
        self, monkeypatch: pytest.MonkeyPatch, otel_off: None
    ) -> None:
        # ENABLED=False: cleanup is not finalization — after_run must NOT run.
        provider = _RecordingProvider("p1")
        agent = Agent(client=_BareClient(), context_providers=[provider])
        stream = agent.run("hi", stream=True)
        await stream._run_cleanup_hooks()
        assert provider.log.count("after:p1") == 0
        # Late consumption after manual cleanup still works and finalizes.
        [u async for u in stream]
        assert provider.log.count("after:p1") == 1
        # Gate on: the AgentTelemetryLayer registers _finalize_stream as a
        # cleanup hook, which drains via get_final_response — so the same call
        # DOES finalize. Pin this known on/off asymmetry so changes to telemetry
        # cleanup cannot silently change when providers receive after_run.
        monkeypatch.setattr(TELEMETRY_GATE, "enabled", True)
        provider2 = _RecordingProvider("p2")
        agent2 = Agent(client=_BareClient(), context_providers=[provider2])
        stream2 = agent2.run("hi", stream=True)
        await stream2._run_cleanup_hooks()
        assert provider2.log.count("after:p2") == 1


# ---------------------------------------------------------------------------
# H. AgentTelemetryLayer cooperation
# ---------------------------------------------------------------------------


class TestTelemetryLayerCooperation:
    @pytest.mark.asyncio
    async def test_enabled_false_streaming_passthrough_identity(
        self, monkeypatch: pytest.MonkeyPatch, otel_off: None
    ) -> None:
        captured: dict[str, Any] = {}
        core_run = _AgentCore.run

        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = core_run(self, *args, **kwargs)
            captured["result"] = result
            return result

        monkeypatch.setattr(_AgentCore, "run", spy)
        agent = Agent(client=_BareClient(), name="T")
        stream = agent.run("hi", stream=True)
        assert stream is captured["result"], "ENABLED=False must return the core stream untouched"
        assert stream._cleanup_hooks == []
        assert await stream.get_final_response()

    @pytest.mark.asyncio
    async def test_enabled_true_streaming_mutates_core_stream_in_place(
        self, monkeypatch: pytest.MonkeyPatch, otel_on: None
    ) -> None:
        captured: dict[str, Any] = {}
        core_run = _AgentCore.run

        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = core_run(self, *args, **kwargs)
            captured["result"] = result
            return result

        monkeypatch.setattr(_AgentCore, "run", spy)
        agent = Agent(client=_BareClient(), name="T")
        stream = agent.run("hi", stream=True)
        # In-place mutation (with_cleanup_hook/with_pull_context_manager return
        # self): the outermost object the executor holds IS the core stream, so
        # watchdog cleanup reaches the real hooks (no nested-wrapper drift).
        assert stream is captured["result"]
        assert len(stream._cleanup_hooks) == 2, "_record_duration + _finalize_stream"
        assert len(stream._pull_context_manager_factories) == 1
        final = await stream.get_final_response()
        assert final.text == "done"

    @pytest.mark.asyncio
    async def test_enabled_true_non_streaming_executor_task_pattern(self, otel_on: None) -> None:
        # AttemptRunner._run_agent calls and awaits inside one task.
        # AgentTelemetryLayer also supports creating the coroutine in another
        # task: setting and resetting INNER ContextVars happens inside it.
        agent = Agent(client=_BareClient(), name="T")

        async def call_and_await() -> Any:
            return await agent.run("hi")

        response = await asyncio.create_task(call_and_await())
        assert response.messages[0].text == "done"

    @pytest.mark.asyncio
    async def test_enabled_true_non_streaming_controller_task_pattern(self, otel_on: None) -> None:
        # Call Agent.run() here and drive its coroutine in a child task.
        # AgentTelemetryLayer confines INNER ContextVar set/reset to that
        # coroutine, avoiding "Token was created in a different Context".
        agent = Agent(client=_BareClient(), name="T")
        response = await asyncio.create_task(agent.run("hi"))  # type: ignore[arg-type]
        assert response.messages[0].text == "done"

    @pytest.mark.asyncio
    async def test_enabled_true_span_attributes_handle_default_options(self, otel_on: None) -> None:
        # Span attribute building reads getattr(self, "default_options", {})
        # including the FunctionTool list — must not raise with tools and
        # session and merged client_kwargs present.
        agent = Agent(client=_BareClient(), name="T", instructions="sys", tools=[_make_tool()])
        session = AgentSession(service_session_id="svc")
        response = await agent.run("hi", session=session, options={"temperature": 0.1})
        assert response.messages[0].text == "done"


# ---------------------------------------------------------------------------
# I. Context manager protocol and sessions
# ---------------------------------------------------------------------------


class TestContextManagerAndSessions:
    @pytest.mark.asyncio
    async def test_aenter_returns_self_for_plain_client(self) -> None:
        agent = Agent(client=_BareClient())
        async with agent as entered:
            assert entered is agent

    @pytest.mark.asyncio
    async def test_async_cm_client_entered_and_exited_once(self) -> None:
        class _CMClient(_BareClient):
            def __init__(self) -> None:
                super().__init__()
                self.entered = 0
                self.exited = 0

            async def __aenter__(self) -> _CMClient:
                self.entered += 1
                return self

            async def __aexit__(self, *exc: Any) -> None:
                self.exited += 1

        client = _CMClient()
        async with Agent(client=client):
            assert client.entered == 1
            assert client.exited == 0
        assert client.exited == 1

    def test_create_session_shapes(self) -> None:
        agent = Agent(client=_BareClient())
        session = agent.create_session()
        assert isinstance(session, AgentSession)
        assert session.session_id
        assert session.service_session_id is None
        named = agent.create_session(session_id="sid-1")
        assert named.session_id == "sid-1"

    def test_get_session_shapes(self) -> None:
        agent = Agent(client=_BareClient())
        session = agent.get_session("svc-1", session_id="sid-2")
        assert session.service_session_id == "svc-1"
        assert session.session_id == "sid-2"


# ---------------------------------------------------------------------------
# J. Helper contracts
# ---------------------------------------------------------------------------


class TestDriftPins:
    def test_append_unique_tools_contract(self) -> None:
        t1 = _make_tool(name="t1")
        t1_dup = _make_tool(name="t1")
        nameless = object()

        ours = _append_unique_tools([t1], [t1, nameless])
        assert ours == [t1, nameless], "same identity skips; nameless always appends"

        with pytest.raises(ValueError, match="Duplicate tool name 't1'"):
            _append_unique_tools([t1], [t1_dup])

        # In-place append contract: returns the same list object.
        base: list[Any] = []
        assert _append_unique_tools(base, [t1]) is base

    def test_get_tool_name_contract(self) -> None:
        class _Named:
            def __init__(self, name: Any) -> None:
                self.name = name

        cases: list[tuple[Any, str | None]] = [
            (_make_tool(name="t1"), "t1"),
            ({"function": {"name": "dict_tool"}}, "dict_tool"),
            ({"function": {"name": 123}}, None),  # non-str name -> None
            ({"function": "not-a-mapping"}, None),
            ({"other": "shape"}, None),
            (_Named(42), None),  # attribute path with a non-str name -> None
            (object(), None),
        ]
        for case, expected in cases:
            assert _get_tool_name(case) == expected, f"divergence for {case!r}"

    def test_merge_options_contract(self) -> None:
        chrys_t1, chrys_t2 = _make_tool(name="t1"), _make_tool(name="t2")

        cases: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = [
            ({"temperature": 0.1}, {"temperature": None, "seed": 7}, {"temperature": 0.1, "seed": 7}),
            # None-in-base: remaining None values are stripped in a final pass
            # (the case the rest of this battery cannot expose).
            ({"store": None, "temperature": 0.1}, {"seed": 7}, {"temperature": 0.1, "seed": 7}),
            ({"logit_bias": {"a": 1}}, {"logit_bias": {"b": 2}}, {"logit_bias": {"a": 1, "b": 2}}),
            ({"metadata": {"a": 1}}, {"metadata": {"a": 2}}, {"metadata": {"a": 2}}),
            ({"instructions": "base"}, {"instructions": "extra"}, {"instructions": "base\nextra"}),
            ({}, {"instructions": "only"}, {"instructions": "only"}),
        ]
        for base, override, expected in cases:
            assert _merge_options(dict(base), dict(override)) == expected
        ours = _merge_options({"tools": [chrys_t1]}, {"tools": [chrys_t2]})
        assert [_get_tool_name(tool_item) for tool_item in ours["tools"]] == ["t1", "t2"]
        assert ours["tools"] == [chrys_t1, chrys_t2]

        same = _make_tool(name="same")
        ours_same = _merge_options({"tools": [same]}, {"tools": [same]})
        assert [_get_tool_name(tool_item) for tool_item in ours_same["tools"]] == ["same"]
        assert len(ours_same["tools"]) == 1

        # Anchor the final-pass semantics absolutely: unset (None) options
        # never survive the merge, from either side.
        assert _merge_options({"store": None}, {"seed": None}) == {}
