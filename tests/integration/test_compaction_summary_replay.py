# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compaction tool summaries survive a restore for the model; a reopened transcript shows neither them nor the
tool calls they replaced, as before summaries were persisted. Fold summaries the archive index lists leave the
model's request but keep their fold in the reopened transcript."""

from __future__ import annotations

import json
from typing import Any

import pytest
from acp import RequestError
from acp import schema as acp_schema

from chrys.app.acp.history import replay_session_history, session_history_payload
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.tui.theme import TUI_VARIABLE_DEFAULTS, with_tui_css_variables
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.replay import (
    HistoryReplayPlanner,
    ReplayAgentMessage,
    ReplayToolCall,
    ReplayUserMessage,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.kernel import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    Agent,
    AgentSession,
    Content,
    Message,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
)
from chrys.service.context.compaction import MixedLanguageTokenizer, UnifiedContextStrategy
from chrys.service.context.providers.archive_index import is_archived_summary, merge_fold_summaries
from chrys.service.context.providers.history import CompressedBlock, CompressibleHistoryProvider
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.session.message_metadata import is_compaction_tool_summary
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.state import TurnRecord, append_turn
from tests.app.acp._server_fakes import _FakeClient, _FakeHost, _FakeLoadManager, _FakeManager
from tests.support.acp_wire import acp_outgoing_json
from tests.support.phase4_stubs import StubLastWordsGenerator, StubReminderMiddleware
from tests.support.tui_helpers import ChatPanelApp


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


async def test_reopened_session_keeps_the_fold_of_each_archived_summary(tmp_path) -> None:
    """An archived summary leaves the model's request; in the TUI its turns still replay from their compressed block."""
    state: dict[str, Any] = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for turn in range(1, 6):
        state["messages"].append(Message("user", [f"Request of turn {turn}"]))
        state["messages"].append(Message("assistant", [f"Answer of turn {turn}"]))
        CompressibleHistoryProvider.insert_marker(state, turn)
    block_ids = [
        CompressibleHistoryProvider.compress(state, f"turn_{turn}", f"Summary of turn {turn}. " + "s" * 800)
        for turn in range(1, 5)
    ]
    merge = merge_fold_summaries(state, max_context_tokens=10_000, count_tokens=lambda text: len(text) // 4)
    assert merge is not None
    archived = [summary.additional_properties["_block_id"] for summary in merge.merged_summaries]
    assert archived == block_ids[:2]

    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", state, agent_profile="Code", primary_cwd=str(tmp_path))

    folds, prompts = await _tui_replay(store)
    assert folds == block_ids
    assert prompts == [f"Request of turn {turn}" for turn in range(1, 6)]

    client = _ReplayClient()
    await _load_acp(store, client)
    assert [item["compressedContextId"] for item in client.folds] == block_ids
    assert [entry[1] for entry in _text_entries(client) if entry[0] == "user_message_chunk"] == prompts

    payload = await _history(store)
    assert [item["compressedContextId"] for item in payload["compressedBlocks"]] == block_ids
    assert [
        row["message"]["additional_properties"]["_block_id"]
        for row in payload["messages"]
        if "_block_id" in row["message"].get("additional_properties", {})
    ] == block_ids[2:]

    restored = await store.load_session("s1")
    assert restored is not None
    still_archived = [message for message in restored["messages"] if is_archived_summary(message)]
    assert [message.additional_properties["_block_id"] for message in still_archived] == archived
    assert all(message.additional_properties[EXCLUDED_KEY] for message in still_archived)


class _ReplayClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.timeline: list[tuple[str, Any]] = []
        self.folds: list[dict[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        await super().session_update(session_id, update, **kwargs)
        self.timeline.append((update.session_update, update))

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        await super().ext_notification(method, params)
        if method == "chrys/context_compressed":
            self.timeline.append(("fold", params))
            self.folds.append(params)


async def _load_acp(store: JsonFileStateStore, client: _ReplayClient) -> None:
    manager = _FakeLoadManager(reused_existing=True)
    manager.state_store = store
    server = ChrysAcpServer(manager, initial_vision=False)  # ty: ignore[invalid-argument-type]
    server.on_connect(client)  # ty: ignore[invalid-argument-type]
    await server.load_session("s1")


async def _history(store: JsonFileStateStore) -> dict[str, Any]:
    manager = _FakeManager(_FakeHost(event_bus=EventBus()))
    manager.state_store = store
    server = ChrysAcpServer(manager, initial_vision=False)  # ty: ignore[invalid-argument-type]
    return await server.ext_method("session/history", {"sessionId": "s1"})


class _ReplayApp(ChatPanelApp):
    def get_theme_variable_defaults(self) -> dict[str, str]:
        return {**super().get_theme_variable_defaults(), **TUI_VARIABLE_DEFAULTS}

    def get_css_variables(self) -> dict[str, str]:
        variables = with_tui_css_variables(self.current_theme, super().get_css_variables())
        self.theme_variables = variables
        return variables


async def _tui_replay(store: JsonFileStateStore) -> tuple[list[str], list[str]]:
    loaded = await store.load_session_for_replay("s1")
    assert loaded is not None
    transcript = loaded.transcript
    async with _ReplayApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(transcript.messages, folds=transcript.folds)
        await panel.wait_replay_complete()
        return (
            [widget._compressed_context_id for widget in panel.query(ContextFoldWidget)],
            [widget.text for widget in panel.query(UserMessage)],
        )


def _text_entries(client: _ReplayClient) -> list[tuple[str, str, str | None]]:
    return [
        (kind, update.content.text, (update.field_meta or {}).get("chrys", {}).get("turnId"))
        for kind, update in client.timeline
        if kind in {"user_message_chunk", "agent_message_chunk"}
    ]


def _summary(context_id: str | None, text: str, **properties: Any) -> Message:
    if context_id is not None:
        properties["_block_id"] = context_id
    return Message(
        "assistant",
        [f"Summary: {text}"],
        additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, **properties},
    )


def _block(context_id: str, text: str, turn: int) -> CompressedBlock:
    return CompressedBlock(
        context_id, [Message("user", [text])], text, f"turn_{turn}", (turn, turn), "2026-11-23T00:00:00+00:00"
    )


@pytest.mark.parametrize("case", ["no-folds", "blocks-only", "missing-and-orphan"])
async def test_replay_preserves_fold_positions_on_each_surface(tmp_path, case: str) -> None:
    first = _block("first", "first prompt", 1)
    middle = _block("middle", "middle prompt", 2)
    third = _block("third", "third prompt", 3)
    last = _block("last", "last prompt", 4)
    tail = Message("user", ["active prompt"])
    if case == "no-folds":
        messages, blocks = [tail], []
        expected_ids, expected_prompts = [], ["active prompt"]
    elif case == "blocks-only":
        messages, blocks = [], [first]
        expected_ids, expected_prompts = ["first"], ["first prompt"]
    else:
        messages = [
            _summary(None, "archive index", **{HistoryMarkerKind.ARCHIVE_INDEX_KEY: []}),
            _summary("first", "first summary"),
            _summary("missing", "card between the others"),
            _summary("third", "third summary"),
            _summary("middle", "summary stored after a later block"),
            tail,
        ]
        blocks = [first, middle, third, last]
        expected_ids = ["first", "missing", "third", "middle", "last"]
        expected_prompts = ["first prompt", "third prompt", "middle prompt", "last prompt", "active prompt"]
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", {"messages": messages, "compressed_msgs": blocks})

    assert await _tui_replay(store) == (expected_ids, expected_prompts)
    client = _ReplayClient()
    await _load_acp(store, client)
    assert [entry["compressedContextId"] for entry in client.folds] == expected_ids
    assert [text for _, text, _ in _text_entries(client)] == expected_prompts
    history = await _history(store)
    assert [entry["compressedContextId"] for entry in history["compressedBlocks"]] == expected_ids
    if case == "missing-and-orphan":
        assert client.folds[1] == {
            "sessionId": "s1",
            "compressedContextId": "missing",
            "summary": "card between the others",
            "turnRange": [0, 0],
            "freedMessages": 0,
            "source": None,
        }
        assert history["compressedBlocks"][1] == {
            **{key: value for key, value in client.folds[1].items() if key != "sessionId"},
            "markerId": None,
            "createdAt": None,
            "messages": [],
        }
        assert [entry["summary"] for entry in client.folds] == [
            "first summary",
            "card between the others",
            "third summary",
            "summary stored after a later block",
            "last prompt",
        ]


async def test_bad_slot_is_claimed_before_later_block_with_same_id(tmp_path, caplog) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                _summary("same", "archived card", **{EXCLUDED_KEY: True}),
                _summary("same", "surviving block"),
                _summary("unknown", "unknown content card"),
            ],
            "compressed_msgs": [
                _block("orphan", "orphan prompt", 1),
                _block("same", "bad slot original", 2),
                _block("same", "good slot original", 3),
                _block("unknown", "unknown type original", 4),
            ],
        },
    )
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    bad_blocks = raw["state"]["compressed_msgs"]
    del bad_blocks[1]["summary_text"]
    bad_blocks[1]["turn_range"] = None
    bad_blocks[2]["messages"].append(
        Message("assistant", [Content.from_function_call("c", "read_file", arguments={})]).to_dict()
    )
    undecodable_contents = [{"type": "unknown_future_type"}, {"text": "missing content type"}]
    bad_blocks[3]["messages"][0]["contents"] = undecodable_contents
    path.write_text(json.dumps(raw), encoding="utf-8")

    client = _ReplayClient()
    await _load_acp(store, client)
    assert [(item["compressedContextId"], item["summary"], item["freedMessages"]) for item in client.folds] == [
        ("orphan", "orphan prompt", 1),
        ("same", "archived card", 0),
        ("same", "surviving block", 2),
        ("unknown", "unknown content card", 0),
    ]
    assert [(kind, update.tool_call_id) for kind, update in client.timeline if kind == "tool_call"] == [
        ("tool_call", "history:c2:1:0:1"),
    ]
    assert "slot 1 (same)" in caplog.text
    assert "slot 3 (unknown)" in caplog.text
    # session/history reaches the same verdict per slot as session/load.
    history = (await _history(store))["compressedBlocks"]
    assert [(entry["compressedContextId"], entry["freedMessages"], len(entry["messages"])) for entry in history] == [
        ("orphan", 1, 1),
        ("same", 0, 0),
        ("same", 2, 2),
        ("unknown", 0, 0),
    ]
    assert [entry["turnRange"] for entry in history] == [[1, 1], [0, 0], [3, 3], [0, 0]]
    assert [entry["markerId"] for entry in history] == ["turn_1", None, "turn_3", None]


async def test_block_with_malformed_turn_range_still_replays_its_turns(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                _summary("first", "first summary", **{EXCLUDED_KEY: True}),
                Message("user", ["active prompt"]),
            ],
            "compressed_msgs": [_block("first", "first prompt", 1)],
        },
    )
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["compressed_msgs"][0]["turn_range"] = [1]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert await _tui_replay(store) == (["first"], ["first prompt", "active prompt"])
    client = _ReplayClient()
    await _load_acp(store, client)
    assert [(item["compressedContextId"], item["turnRange"]) for item in client.folds] == [("first", [0, 0])]
    assert [text for kind, text, _turn in _text_entries(client) if kind == "user_message_chunk"] == [
        "first prompt",
        "active prompt",
    ]
    history = (await _history(store))["compressedBlocks"]
    assert [(entry["turnRange"], len(entry["messages"])) for entry in history] == [([0, 0], 1)]


def _tool_summary(text: str) -> Message:
    return Message("assistant", [text], additional_properties={GROUP_ANNOTATION_KEY: {SUMMARY_OF_GROUP_IDS_KEY: ["g"]}})


def _continuation() -> Message:
    return Message("user", ["continue"], additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True})


def _render_history_segment(rows: list[dict[str, Any]]) -> list[tuple[str, str, str | None]]:
    """Render the fixture's text and local tools using the documented client rules."""
    rendered = []
    pending: dict[str, str | None] = {}
    for row in rows:
        message = row["message"]
        props = message.get("additional_properties", {})
        if "_chrys_kind" in props or "_summary_of_group_ids" in props.get("_group", {}) or props.get("_continuation"):
            continue
        for content in message["contents"]:
            item_type = content["type"]
            if item_type == "text":
                kind = "user_message_chunk" if message["role"] == "user" else "agent_message_chunk"
                rendered.append((kind, content["text"], row["turnId"]))
            if item_type == "function_call":
                pending[content["call_id"]] = row["turnId"]
                rendered.append(("tool_call", content["name"].replace("_", " "), row["turnId"]))
            if item_type == "function_result" and content["call_id"] in pending:
                pending.pop(content["call_id"])
                rendered.append(("tool_call_update", "completed", row["turnId"]))
    rendered.extend(("tool_call_update", "failed", turn_id) for turn_id in pending.values())
    return rendered


def _replay_entries(client: _ReplayClient) -> list[tuple[str, str, str | None]]:
    entries = []
    for kind, update in client.timeline:
        if kind == "fold":
            entries.append((kind, update["compressedContextId"], None))
            continue
        if kind not in {"user_message_chunk", "agent_message_chunk", "tool_call", "tool_call_update"}:
            continue
        turn_id = (update.field_meta or {}).get("chrys", {}).get("turnId")
        text = (
            update.title
            if kind == "tool_call"
            else update.status
            if kind == "tool_call_update"
            else update.content.text
        )
        entries.append((kind, text, turn_id))
    return entries


async def test_block_turns_and_active_tail_share_attribution_but_pair_tools_per_segment(tmp_path) -> None:
    opener = Message("user", ["old opener"], additional_properties={ANALYTICS_ITEM_ID_KEY: "opening-old"})
    second = Message("user", ["new opener"], additional_properties={ANALYTICS_ITEM_ID_KEY: "opening-new"})
    excluded = Message("assistant", ["excluded but archived"], additional_properties={EXCLUDED_KEY: True})
    image = Content.from_uri("data:image/png;base64,AA==", media_type="image/png")
    archived = [
        opener,
        excluded,
        _tool_summary("hidden in block"),
        _continuation(),
        Message("assistant", [Content.from_function_call("same", "view_image", arguments={})]),
        Message("tool", [Content.from_function_result("same", result=[image])]),
        Message("assistant", [Content.from_function_call("same", "read_file", arguments={})]),
    ]
    CompressibleHistoryProvider.insert_marker({"messages": archived}, 1)
    tail = [
        Message("assistant", ["late answer"]),
        _tool_summary("hidden in active"),
        _continuation(),
        Message("tool", [Content.from_function_result("same", result="orphan result from another segment")]),
        second,
        Message("assistant", [Content.from_function_call("same", "read_file", arguments={})]),
        Message("tool", [Content.from_function_result("same", result="active result")]),
    ]
    state = {
        "messages": [_summary("folded", "fold summary"), *tail],
        "compressed_msgs": [
            CompressedBlock("folded", archived, "fold summary", "turn_1", (1, 1), "2026-11-23T00:00:00+00:00"),
        ],
    }
    append_turn(state, TurnRecord(1, "turn-old", opening_item_id="opening-old", user_message_id="client-old"))
    append_turn(state, TurnRecord(2, "turn-new", opening_item_id="opening-new", user_message_id="client-new"))
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", state)
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["compressed_msgs"][0]["messages"][0]["contents"] = ["old opener"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    client = _ReplayClient()
    await _load_acp(store, client)
    history = await _history(store)
    (fold,) = history["compressedBlocks"]
    assert {key: value for key, value in fold.items() if key != "messages"} == {
        "compressedContextId": "folded",
        "summary": "fold summary",
        "turnRange": [1, 1],
        "freedMessages": 8,
        "markerId": "turn_1",
        "createdAt": "2026-11-23T00:00:00+00:00",
        "source": None,
    }
    assert fold["messages"][0]["message"]["contents"] == [{"type": "text", "text": "old opener"}]
    assert fold["messages"][1]["message"]["additional_properties"][EXCLUDED_KEY] is True
    assert fold["messages"][5]["message"]["contents"][0]["items"] == [image.to_dict()]
    assert [entry["turnId"] for entry in fold["messages"]] == ["turn-old"] * 8
    assert [entry["turnId"] for entry in history["messages"]] == [None, *["turn-old"] * 4, *["turn-new"] * 3]

    # Compare with the same conversation laid out without folding.
    uncompressed = {
        "messages": [*archived, *tail],
        "compressed_msgs": [],
        "chrys_trajectory": state["chrys_trajectory"],
    }
    await store.save_session("flat", uncompressed)
    flat = await store.load_session_transcript("flat")
    assert flat is not None
    # In the active transcript, the store filters excluded entries. Compare
    # every occurrence common to both views.
    flat_labels = session_history_payload("flat", flat)["messages"]
    compressed_labels = [*fold["messages"], *history["messages"][1:]]
    assert [
        row["turnId"]
        for row in compressed_labels
        if not row["message"].get("additional_properties", {}).get(EXCLUDED_KEY)
    ] == [row["turnId"] for row in flat_labels]

    assert _replay_entries(client) == [
        *_render_history_segment(fold["messages"]),
        ("fold", "folded", None),
        *_render_history_segment(history["messages"]),
    ]
    assert _text_entries(client) == [
        ("user_message_chunk", "old opener", "turn-old"),
        ("agent_message_chunk", "excluded but archived", "turn-old"),
        ("agent_message_chunk", "late answer", "turn-old"),
        ("user_message_chunk", "new opener", "turn-new"),
    ]
    assert [update.message_id for kind, update in client.timeline if kind == "user_message_chunk"] == [
        "client-old",
        "client-new",
    ]

    tools_and_folds = []
    for kind, update in client.timeline:
        if kind == "fold":
            tools_and_folds.append(("fold", update["compressedContextId"], None))
        elif kind in {"tool_call", "tool_call_update"}:
            tools_and_folds.append((kind, update.tool_call_id, update.status))
    assert tools_and_folds == [
        ("tool_call", "history:c0:4:0:1", "in_progress"),
        ("tool_call_update", "history:c0:4:0:1", "completed"),
        ("tool_call", "history:c0:6:0:2", "in_progress"),
        ("tool_call_update", "history:c0:6:0:2", "failed"),
        ("fold", "folded", None),
        ("tool_call", "history:5:0:1", "in_progress"),
        ("tool_call_update", "history:5:0:1", "completed"),
    ]
    before_fold = [
        update
        for _, update in client.timeline[: next(i for i, row in enumerate(client.timeline) if row[0] == "fold")]
        if (getattr(update, "field_meta", None) or {}).get("chrys", {}).get("turnId") == "turn-old"
    ]
    assert len(before_fold) == 6
    assert all(update.field_meta["chrys"]["compressedContextId"] == "folded" for update in before_fold)
    assert await _tui_replay(store) == (["folded"], ["old opener", "new opener"])


@pytest.mark.parametrize("target_turn", [0, 1, 2])
async def test_rollback_projects_the_restored_snapshot(tmp_path, target_turn: int) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    before = {"messages": [Message("user", ["first"])]}
    after = {
        "messages": [_summary("first-fold", "folded first"), Message("user", ["second"])],
        "compressed_msgs": [_block("first-fold", "first", 1)],
    }
    await store.save_session("s1", after)

    class SnapshotManager(_FakeManager):
        async def rollback(self, session_id, *, target_turn, revert_changes, selected_paths):
            await store.save_session(session_id, {0: {}, 1: before, 2: after}[target_turn])
            return await super().rollback(
                session_id,
                target_turn=target_turn,
                revert_changes=revert_changes,
                selected_paths=selected_paths,
            )

    manager = SnapshotManager(_FakeHost(event_bus=EventBus()))
    manager.state_store = store
    server = ChrysAcpServer(manager, initial_vision=False)  # ty: ignore[invalid-argument-type]
    client = _ReplayClient()
    server.on_connect(client)  # ty: ignore[invalid-argument-type]
    await server.ext_method("session/rollback", {"sessionId": "s1", "targetTurn": target_turn})

    expected = {0: ([], []), 1: ([], ["first"]), 2: (["first-fold"], ["first", "second"])}[target_turn]
    assert (
        [entry["compressedContextId"] for entry in client.folds],
        [text for _, text, _ in _text_entries(client)],
    ) == expected
    assert await _tui_replay(store) == expected
    assert [entry["compressedContextId"] for entry in (await _history(store))["compressedBlocks"]] == expected[0]


async def test_session_history_with_no_checkpoint_includes_empty_blocks(tmp_path) -> None:
    assert await _history(JsonFileStateStore(tmp_path / "sessions")) == {
        "sessionId": "s1",
        "turns": [],
        "messages": [],
        "compressedBlocks": [],
    }


async def test_summary_with_null_contents_still_replays_its_block(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [_summary("first", "first summary"), Message("user", ["active prompt"])],
            "compressed_msgs": [_block("first", "first prompt", 1)],
        },
    )
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["messages"][0]["contents"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")

    folds = (await store.load_session_transcript("s1", include_folds=True)).folds
    assert [(fold.context_id, fold.summary, fold.block is not None) for fold in folds] == [("first", "", True)]
    client = _ReplayClient()
    await _load_acp(store, client)
    assert [text for kind, text, _turn in _text_entries(client) if kind == "user_message_chunk"] == [
        "first prompt",
        "active prompt",
    ]


def _turn_block(context_id: str, prompt: str, item_id: str, turn: int) -> CompressedBlock:
    return CompressedBlock(
        context_id,
        [Message("user", [prompt], additional_properties={ANALYTICS_ITEM_ID_KEY: item_id})],
        f"{context_id} summary",
        f"turn_{turn}",
        (turn, turn),
        "2026-11-23T00:00:00+00:00",
    )


async def test_a_missing_block_does_not_let_a_turn_span_the_gap(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    state: dict[str, Any] = {
        "messages": [
            _summary("first", "first summary"),
            _summary("second", "second summary"),
            Message("assistant", ["continuation answer"]),
        ],
        "compressed_msgs": [
            _turn_block("first", "first opener", "u1", 1),
            _turn_block("second", "second opener", "u2", 2),
        ],
    }
    append_turn(state, TurnRecord(1, "turn-1", opening_item_id="u1"))
    append_turn(state, TurnRecord(2, "turn-2", opening_item_id="u2"))
    await store.save_session("s1", state)

    history = await _history(store)
    assert [entry["turnId"] for entry in history["compressedBlocks"][1]["messages"]] == ["turn-2"]
    assert history["messages"][-1]["turnId"] == "turn-2"
    client = _ReplayClient()
    await _load_acp(store, client)
    assert _text_entries(client)[-1] == ("agent_message_chunk", "continuation answer", "turn-2")

    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["state"]["compressed_msgs"][1]
    path.write_text(json.dumps(raw), encoding="utf-8")

    history = await _history(store)
    assert history["compressedBlocks"][1]["messages"] == []
    assert history["messages"][-1]["turnId"] is None
    client = _ReplayClient()
    await _load_acp(store, client)
    assert _text_entries(client)[-1] == ("agent_message_chunk", "continuation answer", None)


async def test_plain_string_contents_become_one_text_block_in_history_and_replay(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [_summary("first", "first summary"), Message("user", ["active prompt"])],
            "compressed_msgs": [_block("first", "first prompt", 1)],
        },
    )
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["messages"][1]["contents"] = "hello"
    raw["state"]["compressed_msgs"][0]["messages"][0]["contents"] = "hello"
    path.write_text(json.dumps(raw), encoding="utf-8")

    history = await _history(store)
    assert history["compressedBlocks"][0]["messages"][0]["message"]["contents"] == [{"type": "text", "text": "hello"}]
    assert history["messages"][1]["message"]["contents"] == [{"type": "text", "text": "hello"}]
    client = _ReplayClient()
    await _load_acp(store, client)
    assert [text for kind, text, _turn in _text_entries(client) if kind == "user_message_chunk"] == ["hello", "hello"]


async def test_a_plain_string_fold_summary_is_read_by_every_surface(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [_summary("first", "first summary"), Message("user", ["active prompt"])],
            "compressed_msgs": [_block("first", "first prompt", 1)],
        },
    )
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["messages"][0]["contents"] = "Summary: hello"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = await store.load_session_for_replay("s1")
    assert loaded is not None
    assert loaded.transcript.folds[0].summary == "hello"

    client = _ReplayClient()
    await _load_acp(store, client)
    assert [entry["summary"] for entry in client.folds] == ["hello"]

    history = await _history(store)
    assert history["compressedBlocks"][0]["summary"] == "hello"


async def test_null_additional_properties_does_not_break_the_replay_load(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", {"messages": [Message("user", ["hello"])]})
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["messages"][0]["additional_properties"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = await store.load_session_for_replay("s1")
    assert loaded is not None
    assert loaded.transcript.messages == [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "hello", "additional_properties": {}}],
        },
    ]
    assert "additional_properties" not in loaded.transcript.messages[0]
    assert loaded.meta is not None
    assert loaded.meta.title == "hello"


async def test_summaries_stored_out_of_block_order_emit_each_block_once(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                _summary("B", "B summary"),
                _summary("A", "A summary"),
                Message("user", ["active prompt"]),
            ],
            "compressed_msgs": [_block("A", "A prompt", 1), _block("B", "B prompt", 2)],
        },
    )

    assert await _tui_replay(store) == (["B", "A"], ["B prompt", "A prompt", "active prompt"])
    history = await _history(store)
    assert [entry["compressedContextId"] for entry in history["compressedBlocks"]] == ["B", "A"]
    assert all(entry["messages"] for entry in history["compressedBlocks"])


class _RestoringLoadManager(_FakeLoadManager):
    """A fresh load restores the engine state, which decodes every saved block."""

    async def load_session(
        self,
        *,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Any,
        warnings: list[Any] | None = None,
    ):
        await self.state_store.load_session(session_id)
        return await super().load_session(
            cwd=cwd,
            session_id=session_id,
            additional_directories=additional_directories,
            mcp_servers=mcp_servers,
            warnings=warnings,
        )


async def test_an_unreadable_block_fails_a_fresh_load_but_history_degrades(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [_summary("first", "first summary"), Message("user", ["active prompt"])],
            "compressed_msgs": [_block("first", "first prompt", 1)],
        },
    )
    path = store.session_dir("s1") / "session.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state"]["compressed_msgs"][0]["messages"][0]["contents"] = [{"text": "missing content type"}]
    path.write_text(json.dumps(raw), encoding="utf-8")

    # The engine's restore decodes the saved state; an unreadable block cannot be loaded.
    with pytest.raises(ValueError):
        await store.load_session("s1")

    manager = _RestoringLoadManager(reused_existing=False)
    manager.state_store = store
    server = ChrysAcpServer(manager, initial_vision=False)  # ty: ignore[invalid-argument-type]
    client = _ReplayClient()
    server.on_connect(client)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RequestError):
        await server.load_session("s1")

    # session/history still degrades the unreadable block to its summary.
    history = await _history(store)
    (entry,) = history["compressedBlocks"]
    assert (entry["compressedContextId"], entry["freedMessages"], entry["turnRange"], entry["messages"]) == (
        "first",
        0,
        [0, 0],
        [],
    )
