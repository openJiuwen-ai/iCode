# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ask_user tool and AskUserMiddleware."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AskUserResponse, AskUserTimedOut, QuestionToUser
from chrys.foundation.models.ask_user import (
    ASK_USER_EVIDENCE_KEY,
    AskUserAnswer,
    AskUserOption,
    AskUserQuestion,
)
from chrys.foundation.tool_kinds import KIND_ASK_USER, set_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_DETAILS_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_ERROR_RETRYABLE_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.kernel import ChatResponse, Content, Message
from chrys.kernel.middleware import ChatMiddlewareLayer, FunctionInvocationContext, FunctionMiddleware
from chrys.kernel.tools import FunctionTool
from chrys.service.agent_middleware import AskUserMiddleware
from chrys.service.agent_middleware.events.hook_dispatch import finish_result_hooks
from chrys.service.agent_middleware.events.result_persistence import write_result_carriage
from chrys.service.hooks.schema import HookDecision
from chrys.service.tools.builtins.ask_user import AskUserQuestionParam, ask_user
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.support.cpu_guard import cpu_bounded
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer
from tests.support.waiting import wait_for


class _AskUserResultHook(FunctionMiddleware):
    async def process(self, context, call_next):
        await call_next()
        write_result_carriage(context, metadata={TOOL_FAILED_METADATA_KEY: False})
        finish_result_hooks(
            context,
            context.result,
            None,
            HookDecision(extra_context=["Hook note"], result_override="Hook replacement"),
        )


class _ScriptedAskUserClient:
    def __init__(self, first_arguments: dict[str, object] | None = None) -> None:
        self.calls = 0
        self.messages: list[list[Message]] = []
        self._first_arguments = (
            first_arguments
            if first_arguments is not None
            else {
                "questions": [
                    {
                        "question": "Pick?",
                        "options": [
                            {"label": "One"},
                            {"description": "Two", "item": {"label": "Three"}},
                        ],
                    }
                ]
            }
        )

    def get_response(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        options: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> ChatResponse:
        self.calls += 1
        self.messages.append(list(messages))
        assert not stream
        assert options is not None
        if self.calls == 1:
            return ChatResponse(
                messages=[
                    Message(
                        role="assistant",
                        contents=[
                            Content.from_function_call(
                                call_id="call-ask",
                                name="ask_user",
                                arguments=self._first_arguments,
                            )
                        ],
                    )
                ]
            )
        return ChatResponse(messages=[Message(role="assistant", contents=["done"])])


@pytest.mark.asyncio
async def test_ask_user_without_middleware() -> None:
    """Without middleware, the tool returns the fallback message."""
    result = await ask_user(questions=[AskUserQuestionParam(question="What language?")])
    assert "[Questions for user]" in result
    assert "What language?" in result


def test_ask_user_schema_advertises_only_the_questions_form() -> None:
    schema = ask_user.parameters()
    assert set(schema["properties"]) == {"questions"}
    questions_schema = schema["properties"]["questions"]
    assert questions_schema["minItems"] == 1
    assert questions_schema["maxItems"] == 5
    nested = schema["$defs"]["AskUserQuestionParam"]
    assert "multi_select" in nested["properties"]
    assert schema["required"] == ["questions"]
    assert AskUserQuestionParam(question="Pick?").options == []
    tool_description = ask_user.to_json_schema_spec()["function"]["description"]
    assert "Bundle up to 5 questions" in tool_description
    assert "user decision that changes the next action" in tool_description
    assert "Do not add an Other option" in tool_description
    assert "Unanswered entries are skipped, not approval" in tool_description


def _validated_ask_user_args(arguments: dict[str, object]) -> dict[str, object]:
    assert ask_user.input_model is not None
    return ask_user.input_model.model_validate(arguments).model_dump(exclude_none=True)


def _validated_options(options: object) -> list[dict[str, str]]:
    args = _validated_ask_user_args({"questions": [{"question": "Pick?", "options": options}]})
    return args["questions"][0]["options"]


def _validated_option_labels(options: object) -> list[str]:
    return [option["label"] for option in _validated_options(options)]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["x", "y"], ["x", "y"]),
        (
            [
                {"content": "晚饭吃什么?", "label": "晚饭吃什么"},
                {"content": "今天要不要加点加班?", "label": "今天要不要加班"},
            ],
            ["晚饭吃什么", "今天要不要加班"],
        ),
        ([{"label": "One"}, {"label": "Two"}], ["One", "Two"]),
        (
            [
                {
                    "description": "阅读、修改或重构 Chrys 代码",
                    "item": {
                        "description": "运行 uv run chrys 体验 CLI / TUI / ACP",
                        "item": {
                            "description": "整理 AGENTS.md / README / 文档",
                            "item": {"description": "只是随便聊聊"},
                        },
                    },
                }
            ],
            [
                "阅读、修改或重构 Chrys 代码",
                "运行 uv run chrys 体验 CLI / TUI / ACP",
                "整理 AGENTS.md / README / 文档",
                "只是随便聊聊",
            ],
        ),
        ([{"choice": "A"}], ["A"]),
        ([{}, {"label": "ok"}], ["ok"]),
        ("Yes", ["Yes"]),
        ({"options": [{"label": "A"}, {"label": "B"}]}, ["A", "B"]),
        ('["One", "Two"]', ["One", "Two"]),
        (['["One", "Two"]'], ["One", "Two"]),
        (['["One", {"label": "Two"}]'], ["One", "Two"]),
        (["[not JSON]"], ["[not JSON]"]),
        (["[1, 100]", "[100, 200]"], ["[1, 100]", "[100, 200]"]),
        ([{"label": '["One", "Two"]'}], ["One", "Two"]),
        ([{"note": '["A", "B"]'}], ["A", "B"]),
        ([{"label": "[1, 100]"}], ["[1, 100]"]),
        # json.loads raises plain ValueError (int digit limit), not
        # JSONDecodeError; the literal string must survive as an option.
        (["[" + "1" * 5000 + "]"], ["[" + "1" * 5000 + "]"]),
    ],
)
def test_ask_user_options_normalizes_common_model_shapes(options: object, expected: list[str]) -> None:
    assert _validated_option_labels(options) == expected


def test_ask_user_options_normalization_preserves_absent_and_null_options() -> None:
    assert _validated_ask_user_args({"questions": [{"question": "Pick?"}]})["questions"][0]["options"] == []
    for options in (None, [{}], "[]", ["[]"]):
        assert _validated_options(options) == []
    assert _validated_ask_user_args({"questions": None})["questions"] == []


def test_ask_user_options_normalization_bounds_shared_reference_graphs() -> None:
    # Eight shared levels of seven references each are only eight list
    # objects, but without a total-visit budget the depth-capped walk would
    # traverse ~7^8 paths. The budget must keep validation fast; exhausting it
    # is an atomic argument error instead of a runaway walk.
    node: object = ["opt"]
    for _ in range(8):
        node = [node] * 7
    with pytest.raises(ValidationError):
        cpu_bounded(lambda: _validated_options(node))

    # The budget must also stop ITERATION, not just deeper visits — a flat
    # exact list far past the visit budget must not be walked to the end.
    with pytest.raises(ValidationError):
        cpu_bounded(lambda: _validated_options([None] * 100_000))

    # Container subclasses are never iterated by the flatten itself — their
    # hooks are user code that can raise or block mid-validation. A
    # top-level list subclass passes through to pydantic's C-level list
    # validation; a nested one degrades to a dropped option.
    class BoomList(list):
        def __iter__(self):  # type: ignore[override]
            raise RuntimeError("flatten must not iterate a list subclass")

    assert _validated_options(BoomList(["x"])) == []
    assert _validated_options([BoomList(["x"])]) == []

    # ``json.loads`` runs before the visit budget can meter its output, so a
    # giant stringified array must be length-gated up front — the literal
    # text survives as an option instead of materializing a huge decoded
    # array the budget would immediately discard.
    giant = "[" + "0," * 100_000 + "0]"
    assert cpu_bounded(lambda: _validated_option_labels([giant, "keep-me"])) == [giant, "keep-me"]

    # Under the gate, decode cost is charged to the budget by SIZE: a stack
    # of near-gate arrays exhausts it after a few parses instead of each
    # paying full parse price. Shared references hit the expansion memo, and
    # a memo hit still charges for the options it emits — repeated refs to
    # one decoded array must not multiply its options past the budget.
    near_gate = "[" + ",".join(f'"o{index}"' for index in range(9_000)) + "]"
    with pytest.raises(ValidationError):
        cpu_bounded(lambda: _validated_options([near_gate] * 5_000))

    # Stripping is O(len) and must be memoized by object identity: a small
    # list of many references to one huge whitespace string would otherwise
    # pay a full strip pass per reference (round 26: 0.67s -> ~1ms).
    shared_whitespace = " " * 2_000_000
    assert cpu_bounded(lambda: _validated_options([shared_whitespace] * 5_000)) == []
    assert (
        cpu_bounded(lambda: _validated_options([{"label": shared_whitespace, "zzz": shared_whitespace}] * 1_000)) == []
    )

    # The memos key by id() but must RETAIN their source objects: a freed
    # temporary (a decoded array element) can otherwise hand its reused
    # address — and its stale cached expansion — to a later, distinct string
    # (round 27: alternating decodes left arrays un-expanded).
    aliasing_options = []
    for index in range(20):
        aliasing_options.append('["          "]')
        aliasing_options.append(f'["value-{index}"]')
    labels = _validated_option_labels(aliasing_options)
    assert [option for option in labels if option.startswith("value-")] == [f"value-{index}" for index in range(20)]
    assert not any(option.startswith('["value-') for option in labels)


def test_ask_user_normalizers_do_not_invoke_hostile_string_or_dict_subclass_hooks() -> None:
    class HostileString(str):
        def __str__(self) -> str:
            raise AssertionError("normalization must bypass subclass __str__")

        def strip(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("normalization must bypass subclass strip")

        def startswith(self, *_args: object, **_kwargs: object) -> bool:
            raise AssertionError("normalization must bypass subclass startswith")

        def endswith(self, *_args: object, **_kwargs: object) -> bool:
            raise AssertionError("normalization must bypass subclass endswith")

        def __len__(self) -> int:
            raise AssertionError("normalization must bypass subclass __len__")

    class HostileDict(dict[object, object]):
        def __iter__(self):  # type: ignore[override]
            raise AssertionError("normalization must not iterate a dict subclass")

        def items(self):  # type: ignore[override]
            raise AssertionError("normalization must not read a dict subclass")

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("normalization must not query a dict subclass")

    rich_native = {
        "questions": [
            {
                "question": HostileString("Native?"),
                "header": HostileString("Choice"),
                "options": [
                    {
                        "label": HostileString("A"),
                        "description": HostileString("First"),
                    }
                ],
            },
            HostileDict({"question": "must be dropped"}),
        ]
    }
    args = cpu_bounded(lambda: _validated_ask_user_args(rich_native))
    assert args["questions"] == [
        {
            "question": "Native?",
            "header": "Choice",
            "options": [{"label": "A", "description": "First"}],
            "multi_select": False,
        }
    ]
    options = cpu_bounded(lambda: _validated_options([HostileString("safe"), HostileDict({"label": "dropped"})]))
    assert options == [{"label": "safe", "description": ""}]


@pytest.mark.parametrize(
    ("options", "expected_options"),
    [
        (
            [
                {"label": "One"},
                {"description": "Two", "item": {"label": "Three"}},
            ],
            ["One", "Two", "Three"],
        ),
        (['["One", "Two"]'], ["One", "Two"]),
    ],
)
@pytest.mark.asyncio
async def test_ask_user_loop_validation_passes_coerced_options_to_middleware(
    options: object,
    expected_options: list[str],
) -> None:
    first_arguments = {"questions": [{"question": "Pick?", "options": options}]}
    bus = EventBus()
    questions: list[QuestionToUser] = []

    async def reply(event: QuestionToUser) -> None:
        questions.append(event)
        await bus.publish(AskUserResponse(request_id=event.request_id, answers=(AskUserAnswer(values=("One",)),)))

    await bus.subscribe(QuestionToUser, reply)

    wire = _ScriptedAskUserClient(first_arguments)
    client = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))
    response = await client.get_response(
        [Message(role="user", contents=["go"])],
        options={"tools": [ask_user]},
        middleware=[_AskUserResultHook(), AskUserMiddleware(bus)],
    )

    assert response.text == "done"
    assert wire.calls == 2
    assert len(questions) == 1
    assert questions[0].questions[0].question == "Pick?"
    assert [option.label for option in questions[0].questions[0].options] == expected_options
    result = next(
        content for message in wire.messages[1] for content in message.contents if content.type == "function_result"
    )
    assert result.result == "Hook replacement\n\nHook note"
    evidence = result.additional_properties[TOOL_RESULT_METADATA_KEY][ASK_USER_EVIDENCE_KEY]
    assert evidence["result"] == "User response: One"
    assert evidence["hook"] == {"extra_context": "Hook note", "result_override": "Hook replacement"}
    assert evidence["questions"][0]["question"] == "Pick?"
    assert [option["label"] for option in evidence["questions"][0]["options"]] == expected_options


async def _first_result_text(first_arguments: dict[str, object]) -> tuple[str, dict[str, object]]:
    wire = _ScriptedAskUserClient(first_arguments)
    client = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))
    response = await client.get_response(
        [Message(role="user", contents=["go"])],
        options={"tools": [ask_user]},
    )
    assert response.text == "done"
    assert wire.calls == 2
    results = [
        content for message in wire.messages[1] for content in message.contents if content.type == "function_result"
    ]
    assert len(results) == 1
    return str(results[0].result), dict(results[0].additional_properties)


@pytest.mark.asyncio
async def test_ask_user_invalid_options_shape_degrades_to_open_ended() -> None:
    result_text, _metadata = await _first_result_text({"questions": [{"question": "Pick?", "options": 7}]})
    assert result_text == "[Questions for user]\n1. Pick?"


@pytest.mark.asyncio
async def test_ask_user_legacy_single_question_arguments_are_an_argument_error() -> None:
    result_text, metadata = await _first_result_text({"question": "Pick?", "options": ["A", "B"]})
    assert result_text.startswith("Error: Invalid arguments for 'ask_user'")
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "argument_parsing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_keys",
    [{"question": "Pick?"}, {"options": ["A", "B"]}, {"question": "Pick?", "options": ["A", "B"]}],
    ids=["question", "options", "both"],
)
async def test_ask_user_native_questions_with_legacy_keys_are_an_argument_error(legacy_keys: dict[str, object]) -> None:
    result_text, metadata = await _first_result_text({"questions": [{"question": "Native?"}], **legacy_keys})
    assert result_text.startswith("Error: Invalid arguments for 'ask_user'")
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "argument_parsing"


@pytest.mark.asyncio
async def test_ask_user_repairs_model_surrogates_before_they_reach_the_user() -> None:
    result_text, _metadata = await _first_result_text({"questions": [{"question": "Pick\ud800?"}]})
    assert result_text == "[Questions for user]\n1. Pick\ufffd?"


@pytest.mark.asyncio
async def test_ask_user_middleware_publishes_and_waits() -> None:
    """AskUserMiddleware publishes QuestionToUser and waits for AskUserResponse."""
    from unittest.mock import AsyncMock, MagicMock

    from chrys.service.agent_middleware import AskUserMiddleware

    bus = EventBus()
    mw = AskUserMiddleware(event_bus=bus, session_id="test")

    questions: list[QuestionToUser] = []

    async def capture_and_reply(event: QuestionToUser) -> None:
        questions.append(event)
        await bus.publish(AskUserResponse(request_id=event.request_id, answers=(AskUserAnswer(values=("Python",)),)))

    await bus.subscribe(QuestionToUser, capture_and_reply)

    # Build a mock FunctionInvocationContext
    mock_function = MagicMock()
    mock_function.chrys_kind = "ask_user"
    mock_function.name = "ask_user"

    context = MagicMock()
    context.function = mock_function
    context.arguments = {
        "questions": [{"question": "What language?", "options": [{"label": "Python"}, {"label": "Go"}]}]
    }
    context.metadata = {}
    context.result = None
    from chrys.service.agent_middleware.events.hook_dispatch import set_call_id

    set_call_id(context, "call-123")

    call_next = AsyncMock()
    await mw.process(context, call_next)

    assert "Python" in context.result
    assert len(questions) == 1
    assert questions[0].questions[0].question == "What language?"
    assert [option.label for option in questions[0].questions[0].options] == ["Python", "Go"]
    assert questions[0].request_id  # should be set
    assert questions[0].call_id == "call-123"
    call_next.assert_not_called()  # middleware handles entirely, never calls through


@pytest.mark.asyncio
async def test_ask_user_middleware_passes_through_non_ask_user() -> None:
    """Non-ask_user tools are passed through to call_next."""
    from unittest.mock import AsyncMock, MagicMock

    from chrys.service.agent_middleware import AskUserMiddleware

    bus = EventBus()
    mw = AskUserMiddleware(event_bus=bus, session_id="test")

    mock_function = MagicMock()
    mock_function.chrys_kind = "shell"

    context = MagicMock()
    context.function = mock_function

    call_next = AsyncMock()
    await mw.process(context, call_next)

    call_next.assert_called_once()


@pytest.mark.asyncio
async def test_ask_user_middleware_ignores_mismatched_request_id() -> None:
    """Responses with wrong request_id are ignored."""
    from unittest.mock import AsyncMock, MagicMock

    from chrys.service.agent_middleware import AskUserMiddleware

    bus = EventBus()
    mw = AskUserMiddleware(event_bus=bus, session_id="test")

    async def reply_wrong_then_right(event: QuestionToUser) -> None:
        # ``bus.publish`` awaits each subscriber inline and in order, so the
        # wrong-id response is fully delivered to (and rejected by) the
        # middleware before the next publish runs — no settle sleep needed.
        await bus.publish(AskUserResponse(request_id="wrong_id", answers=(AskUserAnswer(values=("bad",)),)))
        await bus.publish(AskUserResponse(request_id=event.request_id, answers=(AskUserAnswer(values=("good",)),)))

    await bus.subscribe(QuestionToUser, reply_wrong_then_right)

    mock_function = MagicMock()
    mock_function.chrys_kind = "ask_user"
    mock_function.name = "ask_user"

    context = MagicMock()
    context.function = mock_function
    context.arguments = {"questions": [{"question": "Pick?"}]}
    context.result = None

    await mw.process(context, AsyncMock())
    assert "good" in context.result


@pytest.mark.asyncio
async def test_ask_user_middleware_timeout_defaults_to_fifteen_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ask_user response wait defaults to 15 minutes without sleeping in the test."""
    from unittest.mock import AsyncMock, MagicMock

    import chrys.service.agent_middleware.control.ask_user as ask_user_middleware_module
    from chrys.service.agent_middleware import AskUserMiddleware

    bus = EventBus()
    mw = AskUserMiddleware(event_bus=bus, session_id="test")
    seen_timeouts: list[float] = []
    questions: list[QuestionToUser] = []
    timed_out: list[AskUserTimedOut] = []

    async def collect_question(event: QuestionToUser) -> None:
        questions.append(event)

    async def collect_timeout(event: AskUserTimedOut) -> None:
        timed_out.append(event)

    await bus.subscribe(QuestionToUser, collect_question)
    await bus.subscribe(AskUserTimedOut, collect_timeout)

    async def fake_wait_for(_future: asyncio.Future[str], *, timeout: float) -> str:
        seen_timeouts.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(ask_user_middleware_module.asyncio, "wait_for", fake_wait_for)

    mock_function = MagicMock()
    mock_function.chrys_kind = "ask_user"
    mock_function.name = "ask_user"

    context = MagicMock()
    context.function = mock_function
    context.arguments = {"questions": [{"question": "Pick?"}]}
    context.result = None

    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        await mw.process(context, AsyncMock())
    finally:
        tool_result_metadata.reset(token)

    assert seen_timeouts == [900]
    assert context.result == "Error: user did not respond within 15 minutes."
    assert metadata == {
        TOOL_FAILED_METADATA_KEY: True,
        TOOL_ERROR_KIND_METADATA_KEY: "ask_user_timeout",
        TOOL_ERROR_MESSAGE_METADATA_KEY: "user did not respond within 15 minutes.",
        TOOL_ERROR_RETRYABLE_METADATA_KEY: True,
        TOOL_ERROR_DETAILS_METADATA_KEY: {"timeout_seconds": 900},
    }
    assert len(timed_out) == 1
    assert timed_out[0].request_id == questions[0].request_id
    assert timed_out[0].session_id == "test"


@pytest.mark.asyncio
async def test_ask_user_middleware_custom_timeout_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default timeout is honored and reported in seconds when not whole minutes."""
    from unittest.mock import AsyncMock, MagicMock

    import chrys.service.agent_middleware.control.ask_user as ask_user_middleware_module
    from chrys.service.agent_middleware import AskUserMiddleware

    bus = EventBus()
    mw = AskUserMiddleware(event_bus=bus, session_id="test", timeout_seconds=90)
    seen_timeouts: list[float] = []

    async def fake_wait_for(_future: asyncio.Future[str], *, timeout: float) -> str:
        seen_timeouts.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(ask_user_middleware_module.asyncio, "wait_for", fake_wait_for)

    mock_function = MagicMock()
    mock_function.chrys_kind = "ask_user"
    mock_function.name = "ask_user"
    context = MagicMock()
    context.function = mock_function
    context.arguments = {"questions": [{"question": "Pick?"}]}
    context.result = None

    await mw.process(context, AsyncMock())

    assert seen_timeouts == [90]
    assert context.result == "Error: user did not respond within 90 seconds."


@pytest.mark.asyncio
async def test_ask_user_middleware_no_timeout_waits_indefinitely(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout_seconds=None waits on the response without arming asyncio.wait_for."""
    from unittest.mock import AsyncMock, MagicMock

    import chrys.service.agent_middleware.control.ask_user as ask_user_middleware_module
    from chrys.service.agent_middleware import AskUserMiddleware

    bus = EventBus()
    mw = AskUserMiddleware(event_bus=bus, session_id="test", timeout_seconds=None)
    timed_out: list[AskUserTimedOut] = []

    async def reply(event: QuestionToUser) -> None:
        await bus.publish(AskUserResponse(request_id=event.request_id, answers=(AskUserAnswer(values=("later",)),)))

    async def collect_timeout(event: AskUserTimedOut) -> None:
        timed_out.append(event)

    async def boom_wait_for(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("wait_for must not be used when timeout is disabled")

    await bus.subscribe(QuestionToUser, reply)
    await bus.subscribe(AskUserTimedOut, collect_timeout)
    monkeypatch.setattr(ask_user_middleware_module.asyncio, "wait_for", boom_wait_for)

    mock_function = MagicMock()
    mock_function.chrys_kind = "ask_user"
    mock_function.name = "ask_user"
    context = MagicMock()
    context.function = mock_function
    context.arguments = {"questions": [{"question": "Pick?"}]}
    context.result = None

    await mw.process(context, AsyncMock())

    assert context.result == "User response: later"
    assert timed_out == []


@pytest.mark.asyncio
async def test_ask_user_middleware_cancelled_wait_withdraws_the_question_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import chrys.service.agent_middleware.control.ask_user as ask_user_middleware_module

    bus = EventBus()
    middleware = AskUserMiddleware(bus, session_id="test", timeout_seconds=None)
    wait_started = asyncio.Event()
    finished: list[str] = []
    finished_soon: list[str] = []

    class InstrumentedWait:
        async def started(self) -> None:
            wait_started.set()

        async def finished(self, *, outcome: str = "completed") -> None:
            finished.append(outcome)

        def finished_soon(self, *, outcome: str) -> None:
            finished_soon.append(outcome)

    trace = InstrumentedWait()
    monkeypatch.setattr(ask_user_middleware_module.WaitTrace, "open", lambda *_args, **_kwargs: trace)
    asked: list[QuestionToUser] = []
    withdrawn: list[AskUserTimedOut] = []

    async def ask(event: QuestionToUser) -> None:
        asked.append(event)

    async def withdraw(event: AskUserTimedOut) -> None:
        withdrawn.append(event)

    await bus.subscribe(QuestionToUser, ask)
    await bus.subscribe(AskUserTimedOut, withdraw)

    function = MagicMock()
    function.chrys_kind = "ask_user"
    function.name = "ask_user"
    context = MagicMock()
    context.function = function
    context.arguments = {"questions": [{"question": "Pick?"}]}
    context.metadata = {}
    context.result = None

    task = asyncio.create_task(middleware.process(context, AsyncMock()))
    await wait_started.wait()
    assert len(bus._handlers[AskUserResponse]) == 1
    assert withdrawn == []

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished == []
    assert finished_soon == [ask_user_middleware_module.WaitOutcome.CANCELLED]
    assert bus._handlers[AskUserResponse] == []
    # The frontend stops showing a question nobody waits on.
    assert [(event.request_id, event.session_id) for event in withdrawn] == [(asked[0].request_id, "test")]


@pytest.mark.asyncio
async def test_ask_user_middleware_cancel_after_the_answer_withdraws_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import chrys.service.agent_middleware.control.ask_user as ask_user_middleware_module

    bus = EventBus()
    middleware = AskUserMiddleware(bus, session_id="test", timeout_seconds=None)
    finishing = asyncio.Event()
    held = asyncio.Event()

    class HeldFinish:
        async def started(self) -> None:
            pass

        async def finished(self, *, outcome: str = "completed") -> None:
            finishing.set()
            await held.wait()

        def finished_soon(self, *, outcome: str) -> None:
            pass

    trace = HeldFinish()
    monkeypatch.setattr(ask_user_middleware_module.WaitTrace, "open", lambda *_args, **_kwargs: trace)
    withdrawn: list[AskUserTimedOut] = []

    async def reply(event: QuestionToUser) -> None:
        await bus.publish(AskUserResponse(request_id=event.request_id, answers=(AskUserAnswer(values=("yes",)),)))

    async def withdraw(event: AskUserTimedOut) -> None:
        withdrawn.append(event)

    await bus.subscribe(QuestionToUser, reply)
    await bus.subscribe(AskUserTimedOut, withdraw)

    function = MagicMock()
    function.chrys_kind = "ask_user"
    function.name = "ask_user"
    context = MagicMock()
    context.function = function
    context.arguments = {"questions": [{"question": "Pick?"}]}
    context.metadata = {}
    context.result = None

    task = asyncio.create_task(middleware.process(context, AsyncMock()))
    await wait_for(lambda: finishing.is_set() or task.done(), description="answered wait closing")
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert withdrawn == []


@pytest.mark.asyncio
async def test_ask_user_withdrawal_finishes_through_a_second_cancel() -> None:
    bus = EventBus()
    middleware = AskUserMiddleware(bus, session_id="test", timeout_seconds=None)
    withdrawing = asyncio.Event()
    release = asyncio.Event()
    withdrawn: list[str] = []
    asked = asyncio.Event()

    async def ask(_event: QuestionToUser) -> None:
        asked.set()

    async def withdraw(event: AskUserTimedOut) -> None:
        withdrawing.set()
        await release.wait()
        withdrawn.append(event.request_id)

    await bus.subscribe(QuestionToUser, ask)
    await bus.subscribe(AskUserTimedOut, withdraw)
    function = FunctionTool(name="ask_user")
    set_tool_kind(function, KIND_ASK_USER)
    context = FunctionInvocationContext(function, {"questions": [{"question": "Pick?"}]})

    async def call_next() -> None:
        raise AssertionError("ask_user is answered by the middleware, never by the tool body")

    task = asyncio.create_task(middleware.process(context, call_next))
    try:
        await wait_for(lambda: asked.is_set() or task.done(), description="question shown")
        assert not task.done()
        task.cancel()
        await wait_for(lambda: withdrawing.is_set() or task.done(), description="withdrawal publishing")
        assert not task.done()
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(withdrawn) == 1


@pytest.mark.asyncio
async def test_ask_user_timeout_withdraws_the_question_when_a_cancel_lands_while_it_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.service.agent_middleware.control.ask_user as ask_user_middleware_module

    bus = EventBus()
    # Times out at once.
    middleware = AskUserMiddleware(bus, session_id="test", timeout_seconds=0)
    finishing = asyncio.Event()
    held = asyncio.Event()
    finished: list[str] = []
    finished_soon: list[str] = []

    class HeldFinish:
        async def started(self) -> None:
            pass

        async def finished(self, *, outcome: str = "completed") -> None:
            finished.append(outcome)
            finishing.set()
            await held.wait()

        def finished_soon(self, *, outcome: str) -> None:
            finished_soon.append(outcome)

    trace = HeldFinish()
    monkeypatch.setattr(ask_user_middleware_module.WaitTrace, "open", lambda *_args, **_kwargs: trace)
    asked: list[QuestionToUser] = []
    withdrawn: list[AskUserTimedOut] = []

    async def ask(event: QuestionToUser) -> None:
        asked.append(event)

    async def withdraw(event: AskUserTimedOut) -> None:
        withdrawn.append(event)

    await bus.subscribe(QuestionToUser, ask)
    await bus.subscribe(AskUserTimedOut, withdraw)
    function = FunctionTool(name="ask_user")
    set_tool_kind(function, KIND_ASK_USER)
    context = FunctionInvocationContext(function, {"questions": [{"question": "Pick?"}]})

    async def call_next() -> None:
        raise AssertionError("ask_user is answered by the middleware, never by the tool body")

    task = asyncio.create_task(middleware.process(context, call_next))
    try:
        await wait_for(lambda: finishing.is_set() or task.done(), description="timed-out wait closing")
        assert not task.done()
        assert withdrawn == []
        task.cancel()
    finally:
        held.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished == [ask_user_middleware_module.WaitOutcome.TIMED_OUT]
    assert finished_soon == []
    assert [(event.request_id, event.session_id) for event in withdrawn] == [(asked[0].request_id, "test")]


def test_ask_user_native_shape_aliases_validate() -> None:
    native_camel = _validated_ask_user_args(
        {
            "questions": [
                {
                    "question": "Pick?",
                    "header": "Choice",
                    "options": [{"label": "A", "description": "First"}],
                    "multiSelect": True,
                }
            ]
        }
    )
    native_snake = _validated_ask_user_args({"questions": [{"question": "Pick?", "multi_select": True}]})
    assert native_camel["questions"][0]["multi_select"] is True
    assert native_snake["questions"][0]["multi_select"] is True


def test_ask_user_input_model_ignores_unknown_top_level_fields_without_stringifying() -> None:
    # Pydantic-layer behaviour only: the kernel rejects these keys before the
    # model ever sees them (see the argument-error tests above).
    class Hostile:
        def __str__(self) -> str:
            raise AssertionError("ignored unknown values must not stringify")

    for question in (None, [], {}, Hostile()):
        for options in (None, 7, {}, Hostile()):
            args = _validated_ask_user_args(
                {
                    "questions": [{"question": "Native?"}],
                    "question": question,
                    "options": options,
                }
            )
            assert args["questions"][0]["question"] == "Native?"

    exhausting = [None] * 10_100
    args = cpu_bounded(
        lambda: _validated_ask_user_args(
            {
                "questions": [
                    {
                        "question": "Native?",
                        "header": "Choice",
                        "options": [{"label": "A", "description": "First"}],
                        "multi_select": True,
                    }
                ],
                "question": exhausting,
                "options": exhausting,
            }
        )
    )
    assert args["questions"] == [
        {
            "question": "Native?",
            "header": "Choice",
            "options": [{"label": "A", "description": "First"}],
            "multi_select": True,
        }
    ]


@pytest.mark.asyncio
async def test_ask_user_direct_native_fallback_renders_all_questions() -> None:
    result = await ask_user(
        questions=[
            AskUserQuestionParam(
                question="First?",
                options=[{"label": "A"}],  # type: ignore[list-item]
            ),
            AskUserQuestionParam(question="Second?"),
        ]
    )
    assert result == "[Questions for user]\n1. First?\n   options: A\n2. Second?"


@pytest.mark.asyncio
async def test_ask_user_questions_budget_exhaustion_is_atomic_argument_error() -> None:
    first_arguments = {
        "questions": [[None] * 10_100, {"question": "must not survive"}],
    }
    wire = _ScriptedAskUserClient(first_arguments)
    client = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))
    response = await client.get_response(
        [Message(role="user", contents=["go"])],
        options={"tools": [ask_user]},
    )
    assert response.text == "done"
    results = [
        content for message in wire.messages[1] for content in message.contents if content.type == "function_result"
    ]
    assert len(results) == 1
    assert str(results[0].result).startswith("Error: Invalid arguments for 'ask_user'")
    assert results[0].additional_properties[TOOL_ERROR_KIND_METADATA_KEY] == "argument_parsing"


async def _run_middleware(
    arguments: dict[str, object],
    response: AskUserResponse | None = None,
) -> tuple[str, list[QuestionToUser]]:
    from unittest.mock import AsyncMock, MagicMock

    bus = EventBus()
    middleware = AskUserMiddleware(bus, session_id="test")
    published: list[QuestionToUser] = []

    async def reply(event: QuestionToUser) -> None:
        published.append(event)
        if response is not None:
            response.request_id = event.request_id
            await bus.publish(response)

    await bus.subscribe(QuestionToUser, reply)
    function = MagicMock()
    function.chrys_kind = "ask_user"
    function.name = "ask_user"
    context = MagicMock()
    context.function = function
    context.arguments = arguments
    context.metadata = {}
    context.result = None
    await middleware.process(context, AsyncMock())
    return context.result, published


@pytest.mark.parametrize(
    ("arguments", "kind", "message", "details"),
    [
        ({}, "ask_user_no_question", "no usable question was provided.", None),
        (
            {"questions": [{"question": f"Q{index}?"} for index in range(6)]},
            "ask_user_too_many_questions",
            "ask_user accepts at most 5 questions.",
            {"max_questions": 5},
        ),
        (
            {"questions": [{"question": "Pick?", "options": [{"label": str(index)} for index in range(9)]}]},
            "ask_user_too_many_options",
            "question 1 has more than 8 options.",
            {"question_index": 0, "max_options": 8},
        ),
        (
            {"questions": [{"question": "x" * 10_001}]},
            "ask_user_field_too_long",
            "question 1 field 'question' exceeds 10000 characters.",
            {"question_index": 0, "field": "question", "max_chars": 10_000},
        ),
        (
            {"questions": [{"question": "Pick?", "header": "h" * 65}]},
            "ask_user_field_too_long",
            "question 1 field 'header' exceeds 64 characters.",
            {"question_index": 0, "field": "header", "max_chars": 64},
        ),
        (
            {"questions": [{"question": "Pick?", "options": [{"label": "L" * 201}]}]},
            "ask_user_field_too_long",
            "question 1 option 1 field 'label' exceeds 200 characters.",
            {"question_index": 0, "option_index": 0, "field": "label", "max_chars": 200},
        ),
        (
            {"questions": [{"question": "Pick?", "options": [{"label": "A", "description": "D" * 501}]}]},
            "ask_user_field_too_long",
            "question 1 option 1 field 'description' exceeds 500 characters.",
            {"question_index": 0, "option_index": 0, "field": "description", "max_chars": 500},
        ),
        (
            {"questions": [{"question": "Pick?", "options": [{"label": "A"}, {"label": "A\u200b"}]}]},
            "ask_user_duplicate_options",
            "question 1 contains duplicate option labels.",
            {"question_index": 0, "label": "A"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_middleware_owns_named_question_cap_errors(
    arguments: dict[str, object],
    kind: str,
    message: str,
    details: dict[str, object] | None,
) -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        result, published = await _run_middleware(arguments)
    finally:
        tool_result_metadata.reset(token)
    assert result.startswith("Error: ")
    expected_metadata: dict[str, object] = {
        TOOL_FAILED_METADATA_KEY: True,
        TOOL_ERROR_KIND_METADATA_KEY: kind,
        TOOL_ERROR_MESSAGE_METADATA_KEY: message,
        TOOL_ERROR_RETRYABLE_METADATA_KEY: True,
    }
    if details is not None:
        expected_metadata[TOOL_ERROR_DETAILS_METADATA_KEY] = details
    assert metadata == expected_metadata
    assert published == []


@pytest.mark.asyncio
async def test_middleware_structured_response_padding_and_cancellation() -> None:
    arguments = {
        "questions": [
            {"question": "First?", "options": [{"label": "A"}]},
            {"question": "Second?"},
        ]
    }
    result, published = await _run_middleware(
        arguments,
        AskUserResponse(answers=(AskUserAnswer(values=("A",)), AskUserAnswer())),
    )
    assert len(published) == 1
    assert published[0].questions == (
        AskUserQuestion(question="First?", options=(AskUserOption("A"),)),
        AskUserQuestion(question="Second?"),
    )
    assert '"unanswered": true' in result
    for answers in (None, ()):
        padded, _ = await _run_middleware(arguments, AskUserResponse(answers=answers))
        assert padded.count('"unanswered": true') == 2
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        cancelled, _ = await _run_middleware(arguments, AskUserResponse(cancelled=True))
    finally:
        tool_result_metadata.reset(token)
    assert cancelled == "Error: user did not provide a response."
    assert metadata == {
        TOOL_FAILED_METADATA_KEY: True,
        TOOL_ERROR_KIND_METADATA_KEY: "ask_user_no_response",
        TOOL_ERROR_MESSAGE_METADATA_KEY: "user did not provide a response.",
        TOOL_ERROR_RETRYABLE_METADATA_KEY: True,
    }


@pytest.mark.asyncio
async def test_ask_user_cancelled_during_question_publication_unsubscribes() -> None:
    bus = EventBus()
    publishing = asyncio.Event()
    blocked = asyncio.Event()

    async def block_question(_event: QuestionToUser) -> None:
        publishing.set()
        await blocked.wait()

    await bus.subscribe(QuestionToUser, block_question)
    withdrawn: list[AskUserTimedOut] = []

    async def withdraw(event: AskUserTimedOut) -> None:
        withdrawn.append(event)

    await bus.subscribe(AskUserTimedOut, withdraw)
    middleware = AskUserMiddleware(bus, session_id="test", timeout_seconds=None)
    function = FunctionTool(name="ask_user")
    set_tool_kind(function, KIND_ASK_USER)
    context = FunctionInvocationContext(function, {"questions": [{"question": "Pick?"}]})

    async def call_next() -> None:
        raise AssertionError("ask_user is answered by the middleware, never by the tool body")

    task = asyncio.create_task(middleware.process(context, call_next))
    try:
        await wait_for(lambda: publishing.is_set() or task.done(), description="question publication or early exit")
        if task.done():
            await task  # Surface an early failure instead of a publication timeout.
        assert publishing.is_set()
        assert len(bus._handlers[AskUserResponse]) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert bus._handlers[AskUserResponse] == []
    # The handler that already ran shows the question: it is withdrawn.
    assert len(withdrawn) == 1


async def test_ask_user_cancel_during_publication_after_an_answer_withdraws_nothing() -> None:
    bus = EventBus()
    answered = asyncio.Event()
    blocked = asyncio.Event()

    async def answer_then_block(event: QuestionToUser) -> None:
        await bus.publish(AskUserResponse(request_id=event.request_id, answers=(AskUserAnswer(values=("yes",)),)))
        answered.set()
        await blocked.wait()

    await bus.subscribe(QuestionToUser, answer_then_block)
    withdrawn: list[AskUserTimedOut] = []

    async def withdraw(event: AskUserTimedOut) -> None:
        withdrawn.append(event)

    await bus.subscribe(AskUserTimedOut, withdraw)
    middleware = AskUserMiddleware(bus, session_id="test", timeout_seconds=None)
    function = FunctionTool(name="ask_user")
    set_tool_kind(function, KIND_ASK_USER)
    context = FunctionInvocationContext(function, {"questions": [{"question": "Pick?"}]})

    async def call_next() -> None:
        raise AssertionError("ask_user is answered by the middleware, never by the tool body")

    task = asyncio.create_task(middleware.process(context, call_next))
    try:
        await wait_for(lambda: answered.is_set() or task.done(), description="question answered during publication")
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert bus._handlers[AskUserResponse] == []
    assert withdrawn == []
