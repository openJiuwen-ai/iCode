# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compaction tool summaries survive a restore for the model; a reopened transcript shows neither them nor the
tool calls they replaced, as before summaries were persisted."""

from __future__ import annotations

from typing import Any

from acp import schema as acp_schema

from chrys.app.acp.history import replay_session_history
from chrys.app.tui.widgets.chat.replay import (
    HistoryReplayPlanner,
    ReplayAgentMessage,
    ReplayToolCall,
    ReplayUserMessage,
)
from chrys.kernel import (
    Agent,
    AgentSession,
    Content,
    Message,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
)
from chrys.service.context.compaction import MixedLanguageTokenizer, UnifiedContextStrategy
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.session.message_metadata import is_compaction_tool_summary
from chrys.service.state.store import JsonFileStateStore
from tests.support.acp_wire import acp_outgoing_json
from tests.support.phase4_stubs import StubLastWordsGenerator, StubReminderMiddleware


class _AcpClient:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        acp_outgoing_json(acp_schema.SessionNotification(sessionId=session_id, update=update))
        self.updates.append(update)


def _strategy(**kwargs: Any) -> UnifiedContextStrategy:
    strategy = UnifiedContextStrategy(**kwargs)
    strategy.set_last_words_generator(StubLastWordsGenerator())
    reminder = StubReminderMiddleware()
    strategy.bind_reminder(reminder, reminder.last_words)
    return strategy


def _completed_turn() -> list[Message]:
    messages = [Message("user", ["convert both documents"])]
    for index in range(2):
        call_id = f"call_{index}"
        messages.append(
            Message(
                "assistant",
                [Content.from_function_call(call_id, "convert_document", arguments={"path": f"doc_{index}.pdf"})],
            )
        )
        messages.append(Message("tool", [Content.from_function_result(call_id, result="x" * 8000)]))
    messages.append(Message("assistant", ["Both documents are converted."]))
    return messages


def _tui_transcript(raw: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """What the replayed chat panel shows: user turns, agent text and tool cards, in order."""
    shown: list[tuple[str, str]] = []
    for entry in HistoryReplayPlanner().build_plan(raw).entries:
        if isinstance(entry, ReplayUserMessage):
            shown.append(("user", entry.text))
        elif isinstance(entry, ReplayAgentMessage):
            for content in entry.contents:
                if isinstance(content, ReplayToolCall):
                    shown.append(("tool", content.name))
                elif isinstance(content, dict) and content.get("type") == "text":
                    shown.append(("agent", content["text"]))
    return shown


async def test_reopened_session_hides_compacted_tool_calls_but_the_model_keeps_their_summaries(tmp_path) -> None:
    history = _completed_turn()
    annotate_message_groups(history)
    annotate_token_counts(history, tokenizer=MixedLanguageTokenizer())
    strategy = _strategy(max_context_tokens=included_token_count(history) + 100, trigger_pct=0.8, target_pct=0.3)
    provider = CompressibleHistoryProvider(compaction_strategy=strategy)
    state: dict[str, Any] = {"messages": history}
    session = AgentSession()
    session.state[provider.source_id] = state
    agent = Agent(client=MockChatClient(responses=[MockResponse(text="done")]), context_providers=[provider])
    async with agent:
        await agent.run("next request", session=session, compaction_strategy=strategy)

    summaries = [message.text for message in state["messages"] if is_compaction_tool_summary(message.to_dict())]
    assert len(summaries) == 2
    assert all(text.startswith("[Tool call: convert_document(") for text in summaries)
    # A real answer that reads exactly like a summary must still render.
    state["messages"].append(Message("assistant", [summaries[0]]))
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", state, agent_profile="Code", primary_cwd=str(tmp_path))

    raw = await store.load_session_raw("s1")
    assert raw is not None
    assert sum(is_compaction_tool_summary(message) for message in raw) == 2
    # The compacted tool calls are excluded on disk and their summaries are
    # the model's: the reopened turn goes from the prompt to the answer.
    reopened = [
        ("user", "convert both documents"),
        ("agent", "Both documents are converted."),
        ("user", "next request"),
        ("agent", "done"),
        ("agent", summaries[0]),
    ]
    assert _tui_transcript(raw) == reopened

    acp_client = _AcpClient()
    await replay_session_history(acp_client, store, "s1")
    assert [(update.session_update, update.content.text) for update in acp_client.updates] == [
        ("user_message_chunk" if role == "user" else "agent_message_chunk", text) for role, text in reopened
    ]

    restored = await store.load_session("s1")
    assert restored is not None
    fresh_strategy = _strategy()
    fresh_provider = CompressibleHistoryProvider(compaction_strategy=fresh_strategy)
    restored_session = AgentSession()
    restored_session.state[fresh_provider.source_id] = restored
    client = MockChatClient(responses=[MockResponse(text="again")])
    restored_agent = Agent(client=client, context_providers=[fresh_provider])
    async with restored_agent:
        await restored_agent.run("follow-up", session=restored_session, compaction_strategy=fresh_strategy)
    sent = client.call_history[0][0]
    assert [message.text for message in sent if is_compaction_tool_summary(message.to_dict())] == summaries
    assert not any(content.type == "function_call" for message in sent for content in message.contents)
