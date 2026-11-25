# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared message builders, strategy factories and provider-state helpers for the compaction test modules."""

from typing import Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import (
    EXCLUDED_KEY,
    Content,
    Message,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
)
from chrys.service.context.compaction import (
    CompactionInfo,
    MixedLanguageTokenizer,
    UnifiedContextStrategy,
    _group_kind_map,
    _group_messages_by_id,
    _ordered_group_ids,
)
from chrys.service.context.compaction.last_words_state import LastWordsState
from chrys.service.context.providers.history import (
    CompressibleHistoryProvider,
)
from tests.support.phase4_stubs import StubLastWordsGenerator, StubReminderMiddleware

_tokenizer = MixedLanguageTokenizer()


def _async_appender(target: list) -> object:
    """Create an async callback that appends to *target*."""

    async def _cb(info: CompactionInfo) -> None:
        target.append(info)

    return _cb


def _user(text: str) -> Message:
    return Message(role="user", contents=[Content.from_text(text)])


def _assistant_text(text: str) -> Message:
    return Message(role="assistant", contents=[Content.from_text(text)])


def _assistant_tool_call(call_id: str, name: str, args: dict | None = None) -> Message:
    return Message(
        role="assistant",
        contents=[Content.from_function_call(call_id, name, arguments=args or {})],
    )


def _tool_result(call_id: str, result: str) -> Message:
    return Message(
        role="tool",
        contents=[Content.from_function_result(call_id, result=result)],
    )


def _build_tool_group(call_id: str, name: str, result: str, args: dict | None = None) -> list[Message]:
    """Build a tool-call group: assistant function_call + tool result."""
    return [
        _assistant_tool_call(call_id, name, args=args),
        _tool_result(call_id, result),
    ]


def _has_call_id(msg: Message, call_id: str) -> bool:
    return any(c.call_id == call_id for c in msg.contents if getattr(c, "call_id", None))


def _estimate_tokens(messages: list[Message]) -> int:
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=_tokenizer)
    return included_token_count(messages)


def _build_single_turn(num_groups: int, result_size: int = 2000) -> list[Message]:
    """Build a single-turn message list: user + N tool groups + assistant text."""
    messages = [_user("start")]
    for i in range(num_groups):
        messages.extend(_build_tool_group(f"call_{i}", f"tool_{i}", "x" * result_size))
    messages.append(_assistant_text("done"))
    return messages


def _build_multi_turn(turns: int, groups_per_turn: int = 3, result_size: int = 2000) -> list[Message]:
    """Build a multi-turn message list.

    Each turn: user + N tool groups + assistant text.
    """
    messages: list[Message] = []
    for t in range(turns):
        messages.append(_user(f"Turn {t + 1} request"))
        for g in range(groups_per_turn):
            cid = f"t{t}_call_{g}"
            messages.extend(_build_tool_group(cid, f"tool_{g}", "x" * result_size))
        messages.append(_assistant_text(f"Turn {t + 1} response"))
    return messages


# Collapses the target, satisfied and force lines onto one unreachable value, so no
# escalation gate holds a pass back: every phase that applies runs.
_EVERY_PHASE_LINES = {"target_pct": 0.01, "satisfied_pct": 0.01, "force_pct": 0.01}


def _make_strategy(
    *,
    last_words_generator: object | None = None,
    reminder_middleware: object | None = None,
    last_words: LastWordsState | None = None,
    **kwargs,
) -> UnifiedContextStrategy:
    """Create a strategy with Phase 4 collaborators stubbed by default.

    Tests that want to observe the LAST_WORDS pipeline can pass their own
    stub instances; tests that don't care still get drop-all Phase 4
    behaviour out of the box.  A real middleware comes with the state it
    renders (``reminder_pair``/``make_reminder_stack``), passed as *last_words*.
    """
    defaults = {
        "max_context_tokens": 100_000,
        "trigger_pct": 0.85,
        "target_pct": 0.50,
    }
    defaults.update(kwargs)
    strategy = UnifiedContextStrategy(**defaults)
    strategy.set_last_words_generator(last_words_generator or StubLastWordsGenerator())
    if reminder_middleware is None:
        reminder_middleware = StubReminderMiddleware(last_words)
    if last_words is None:
        if not isinstance(reminder_middleware, StubReminderMiddleware):
            raise TypeError("A real reminder middleware needs the LAST_WORDS state it renders: pass last_words=.")
        last_words = reminder_middleware.last_words
    strategy.bind_reminder(reminder_middleware, last_words)
    return strategy


def _scoped_messages(call: dict) -> list[Message]:
    return [message for group in call["scoped_groups"] for message in group.messages]


def _scoped_user_texts(call: dict) -> list[str]:
    return [message.text for group in call["scoped_groups"] if group.kind == "user" for message in group.messages]


def _injected(text: str) -> Message:
    msg = _user(text)
    msg.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    return msg


def _nudge(text: str = "continue") -> Message:
    msg = _user(text)
    msg.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    return msg


def _turn_marker(turn_index: int) -> Message:
    msg = _assistant_text("")
    msg.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    msg.additional_properties["_turn_id"] = f"turn_{turn_index}"
    msg.additional_properties["_turn"] = turn_index
    return msg


def _status_marker(kind: str) -> Message:
    msg = _assistant_text("")
    msg.additional_properties[HistoryMarkerKind.KEY] = kind
    return msg


def _wire_view(state: dict) -> list[Message]:
    """Marker-less wire view, mirroring ``CompressibleHistoryProvider.get_messages``."""
    return [
        m
        for m in state["messages"]
        if m.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.TURN
        and not m.additional_properties.get(EXCLUDED_KEY, False)
        and not (m.role == "user" and m.additional_properties.get(HistoryMarkerKind.CONTINUATION_KEY))
    ]


def _tool_group_id_of(messages: list[Message], call_id: str) -> str:
    grouped = _group_messages_by_id(messages)
    kinds = _group_kind_map(messages)
    return next(
        gid
        for gid in _ordered_group_ids(messages)
        if kinds.get(gid) == "tool_call"
        and any(
            getattr(content, "call_id", None) == call_id for message in grouped[gid] for content in message.contents
        )
    )


def _folded_text_state(turns: int, fill: int = 2000) -> dict:
    """Provider state holding *turns* completed text-only turns, each closed by a TURN marker."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for idx in range(turns):
        state["messages"].append(_user(f"Turn {idx + 1} request " + ("x " * fill)))
        state["messages"].append(_assistant_text(f"Turn {idx + 1} answer " + ("y " * fill)))
        CompressibleHistoryProvider.insert_marker(state, idx + 1)
    return state


def _markerless_wire(state: dict) -> list[Message]:
    """The wire list projected from *state*: every message except the TURN markers."""
    return [
        msg
        for msg in state["messages"]
        if msg.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.TURN
    ]


def _forced_phase4(messages: list[Message], **strategy_kwargs: Any) -> UnifiedContextStrategy:
    """Strategy whose budget trips immediately over *messages* with an unreachable target.

    ``max_context_tokens`` sits just above the current estimate and the target,
    satisfied and force lines all sit impossibly low, so every phase that applies
    fires and the pass always falls through to a Phase 4 drop round of the current turn.
    """
    total = _estimate_tokens(messages)
    return _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        **{**_EVERY_PHASE_LINES, **strategy_kwargs},
    )


def _anthropic_fetched_pdf_exchange(payload: str) -> list[Message]:
    """An Anthropic web_fetch of a PDF, parsed by the real adapter: the result keeps the base64 ``source`` dict."""
    from anthropic.types.beta import BetaMessage

    from chrys.service.llm.anthropic_messages.decode import decode_blocks

    response = BetaMessage.model_validate(
        {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "fetch-1",
                    "name": "web_fetch",
                    "input": {"url": "https://example.test/paper.pdf"},
                },
                {
                    "type": "web_fetch_tool_result",
                    "tool_use_id": "fetch-1",
                    "content": {
                        "type": "web_fetch_result",
                        "url": "https://example.test/paper.pdf",
                        "content": {
                            "type": "document",
                            "source": {"type": "base64", "media_type": "application/pdf", "data": payload},
                        },
                    },
                },
            ],
        }
    )
    call, result = decode_blocks(response.content)
    return [Message("assistant", [call]), Message("assistant", [result])]
