# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Free-form values reach ACP clients as plain JSON.

Tool arguments, sub-agent tool-result metadata and profile metadata are typed
``Any`` and can hold dataclasses, enum members, dates, sets or paths. The ACP
SDK writes every payload with ``json.dumps``, so each of these fields is
converted on its way out; a value that slipped through would fail the send and,
inside a prompt, the whole turn. Sub-agent results leave file contents behind,
so a large edit or a command that touches many files still fits what a client
accepts.
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from acp.helpers import text_block
from pydantic import BaseModel, Field

from chrys.app.acp import server as server_module
from chrys.app.acp.bridge import AcpEventBridge
from chrys.app.acp.json_values import to_json_value
from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    InvocationToolCallArgsUpdated,
    InvocationToolCallResult,
    InvocationToolCallStart,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.session_host import EndTurn
from chrys.service.acp_client.protocol import _validate_notification_payload_caps
from chrys.service.mutations.types import FileHashDiff, FileMutationTextSnapshot, SnapshotSkipReason
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.tools.result_metadata import tool_error, tool_result_metadata
from tests.app.acp._server_fakes import _FakeClient, _FakeHost, _FakeManager
from tests.app.acp._session_manager_fakes import _profile_manager
from tests.support.acp_wire import acp_outgoing_json, take_acp_wire_failures

_SUB_AGENT = InvocationOrigin("sub_agent", "s1", "inv1", None)
_TURN = InvocationOrigin("turn", "s1", "turn-1", None)


class _Mode(Enum):
    FAST = "fast"


class _Window(BaseModel):
    start: date


class _AliasedWindow(BaseModel):
    start: date = Field(alias="startDate")


_HASHES = FileHashDiff(before="h1", after="h2", after_skip=SnapshotSkipReason.TOO_LARGE, contested=True)
_HASHES_JSON = {
    "before": "h1",
    "after": "h2",
    "before_skip": None,
    "after_skip": "too_large",
    "contested": True,
    "inferred": False,
}
_SHELL_SNAPSHOT = FileMutationTextSnapshot(
    before_text="old\n",
    after_text="",
    operation="modify",
    bytes_changed=True,
    source="shell",
    before_hash="h1",
    after_skip=SnapshotSkipReason.BINARY,
    provenance="observed",
)
_SHELL_SNAPSHOT_JSON = {
    "before_text": "old\n",
    "after_text": "",
    "operation": "modify",
    "bytes_changed": True,
    "source": "shell",
    "before_hash": "h1",
    "after_hash": None,
    "before_skip": None,
    "after_skip": "binary",
    "provenance": "observed",
    "contested": False,
}
_SHELL_SUMMARY_JSON = {key: value for key, value in _SHELL_SNAPSHOT_JSON.items() if not key.endswith("_text")}


def _shell_summary(path: str) -> dict[str, Any]:
    return {"path": path, **_SHELL_SUMMARY_JSON}


def _wire_length(value: object) -> int:
    """Bytes *value* takes as the SDK writes it."""
    return len(json.dumps(value, separators=(",", ":")))


# What validation can hand back for a tool whose parameters are not JSON types.
_TYPED_ARGS: dict[str, Any] = {
    "when": datetime(2026, 10, 9, 10, 42, tzinfo=UTC),
    "path": PurePosixPath("notes/a.txt"),
    "mode": _Mode.FAST,
    "tags": {"b", "a"},
}
_TYPED_ARGS_JSON = {"when": "2026-10-09T10:42:00+00:00", "path": "notes/a.txt", "mode": "fast", "tags": ["a", "b"]}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(SnapshotSkipReason.BINARY, "binary", id="enum"),
        pytest.param(_HASHES, _HASHES_JSON, id="dataclass-with-enum"),
        pytest.param({"/w/a.txt": _SHELL_SNAPSHOT}, {"/w/a.txt": _SHELL_SNAPSHOT_JSON}, id="nested-dataclass"),
        pytest.param(datetime(2026, 10, 9, 10, 42, tzinfo=UTC), "2026-10-09T10:42:00+00:00", id="datetime"),
        pytest.param(date(2026, 10, 9), "2026-10-09", id="date"),
        pytest.param(frozenset({"b", "a"}), ["a", "b"], id="set"),
        pytest.param(("x", 1), ["x", 1], id="tuple"),
        pytest.param(PurePosixPath("/w/a.txt"), "/w/a.txt", id="path"),
        pytest.param(b"ok\xff", "ok�", id="bytes"),
        pytest.param(math.inf, None, id="non-finite-float"),
        pytest.param(_Window(start=date(2026, 10, 9)), {"start": "2026-10-09"}, id="pydantic-model"),
        pytest.param(
            _AliasedWindow(startDate=date(2026, 10, 9)), {"startDate": "2026-10-09"}, id="pydantic-model-alias"
        ),
        pytest.param(
            {"/w/caf\udce9": ["/w/caf\udce9", PurePosixPath("/w/caf\udce9")]},
            {"/w/caf\\udce9": ["/w/caf\\udce9", "/w/caf\\udce9"]},
            id="undecodable-path-text",
        ),
        pytest.param(
            {"icon\ud83d\ude00": "Reply with \ud83d\ude00, not \ud83d or \udce9\ud83d\ude00"},
            {"icon😀": "Reply with 😀, not \\ud83d or \\udce9😀"},
            id="surrogate-pair-joined",
        ),
        pytest.param(
            {1: "one", None: "none", ("a", 2): "pair", SnapshotSkipReason.BINARY: "enum"},
            {"1": "one", "null": "none", '["a", 2]': "pair", "binary": "enum"},
            id="non-string-keys",
        ),
        pytest.param({"plain": [1, 2.5, True, None, "s"]}, {"plain": [1, 2.5, True, None, "s"]}, id="json-unchanged"),
    ],
)
def test_to_json_value_returns_plain_json(value: object, expected: object) -> None:
    converted = to_json_value(value)

    assert converted == expected
    assert acp_outgoing_json({"value": converted}) == {"value": expected}


def test_to_json_value_falls_back_to_text() -> None:
    class _Unknown:
        def __str__(self) -> str:
            return "unknown"

    assert to_json_value(_Unknown()) == "unknown"


def _sub_agent_edit_result(metadata: dict[str, Any] | None = None) -> InvocationToolCallResult:
    """A sub-agent write as the mutation tracker reports it, enum skip reasons included."""
    return InvocationToolCallResult(
        agent_name="General",
        tool_name="edit_file",
        call_id="c1",
        result="Edited a.txt",
        metadata=metadata
        or {
            "file_snapshot": ("old\n", "new\n"),
            "file_mutation_op": "modify",
            "file_mutation_hashes": _HASHES,
            "shell_file_snapshots": {"/w/a.txt": _SHELL_SNAPSHOT},
        },
        session_id="s1",
        origin=_SUB_AGENT,
    )


async def _sent_sub_agent_result(result: InvocationToolCallResult) -> dict[str, Any]:
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(_FakeHost(event_bus=EventBus())), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event("s1", result, AcpEventBridge(), {})

    [params] = [params for method, params in client.ext_notifications if method == "chrys/sub_agent_tool_call_result"]
    return acp_outgoing_json(params)


@pytest.mark.anyio
async def test_prompt_ends_normally_after_sub_agent_file_edit() -> None:
    result = _sub_agent_edit_result()
    host = _FakeHost(event_bus=EventBus(), events=[result], outcome=EndTurn())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    response = await server.prompt([text_block("edit a.txt")], session_id="s1")

    assert response.stop_reason == "end_turn"
    [params] = [params for method, params in client.ext_notifications if method == "chrys/sub_agent_tool_call_result"]
    # File contents stay in-process; the hashes and skip reasons say what changed.
    assert acp_outgoing_json(params)["metadata"] == {
        "file_mutation_op": "modify",
        "file_mutation_hashes": _HASHES_JSON,
        "shell_file_snapshots": [_shell_summary("/w/a.txt")],
    }
    # In-process consumers (the TUI) still read the typed objects and the text.
    assert result.metadata["file_mutation_hashes"] is _HASHES
    assert result.metadata["file_snapshot"] == ("old\n", "new\n")


@pytest.mark.anyio
async def test_sub_agent_result_after_a_large_edit_and_a_broad_command_stays_small() -> None:
    large = "x" * (300 * 1024)
    shell_snapshot = dataclasses.replace(_SHELL_SNAPSHOT, before_text=large, after_text=large)
    paths = [f"/w/f{index}.txt" for index in range(500)]

    sent = await _sent_sub_agent_result(
        _sub_agent_edit_result(
            {
                "file_snapshot": (large, large),
                "file_mutation_hashes": _HASHES,
                "shell_file_snapshots": dict.fromkeys(paths, shell_snapshot),
            }
        )
    )

    # Raises past what iCode's own ACP client accepts in one notification.
    _validate_notification_payload_caps(sent)
    # Smaller than one side of the edit: no file content rides along.
    assert _wire_length(sent) < len(large)
    assert sent["metadata"] == {
        "file_mutation_hashes": _HASHES_JSON,
        "shell_file_snapshots": [_shell_summary(path) for path in paths[:100]],
        "shell_file_snapshots_omitted": 400,
    }


@pytest.mark.anyio
async def test_sub_agent_shell_summaries_count_the_escaped_bytes_of_undecodable_paths() -> None:
    # An undecodable byte is seven bytes on the wire, so a hundred of these
    # paths would come to about 1.3 MB.
    paths = [f"/w/{index}/" + "\udce9" * 1800 for index in range(100)]

    sent = await _sent_sub_agent_result(
        _sub_agent_edit_result({"shell_file_snapshots": dict.fromkeys(paths, _SHELL_SNAPSHOT)})
    )

    summaries = sent["metadata"]["shell_file_snapshots"]
    assert _wire_length(summaries) <= server_module._MAX_SHELL_SNAPSHOT_SUMMARY_BYTES
    assert summaries == [_shell_summary(f"/w/{index}/" + "\\udce9" * 1800) for index in range(len(summaries))]
    assert summaries
    assert sent["metadata"]["shell_file_snapshots_omitted"] == len(paths) - len(summaries)


@pytest.mark.anyio
async def test_sub_agent_shell_summaries_fill_but_never_pass_their_byte_budget() -> None:
    budget = server_module._MAX_SHELL_SNAPSHOT_SUMMARY_BYTES
    # Sixty-four summaries whose own JSON adds up to exactly the budget, so the
    # list's brackets and commas decide where it stops.
    width = _wire_length(_shell_summary("/w/00/"))
    paths = [f"/w/{index:02d}/" + "a" * (budget // 64 - width) for index in range(64)]
    assert {_wire_length(_shell_summary(path)) for path in paths} == {budget // 64}

    sent = await _sent_sub_agent_result(
        _sub_agent_edit_result({"shell_file_snapshots": dict.fromkeys(paths, _SHELL_SNAPSHOT)})
    )

    summaries = sent["metadata"]["shell_file_snapshots"]
    assert _wire_length(summaries) <= budget
    assert summaries == [_shell_summary(path) for path in paths[: len(summaries)]]
    # Full: the next summary would not have fit.
    assert _wire_length([*summaries, _shell_summary(paths[len(summaries)])]) > budget
    assert sent["metadata"]["shell_file_snapshots_omitted"] == len(paths) - len(summaries)


@pytest.mark.anyio
async def test_sub_agent_shell_paths_that_read_the_same_are_both_sent() -> None:
    # A path with an undecodable byte, and a name that spells out its escape.
    undecodable, spelled_out = "/w/caf\udce9", "/w/caf\\udce9"
    snapshots = {undecodable: _SHELL_SNAPSHOT, spelled_out: dataclasses.replace(_SHELL_SNAPSHOT, operation="create")}

    sent = await _sent_sub_agent_result(_sub_agent_edit_result({"shell_file_snapshots": snapshots}))

    assert sent["metadata"]["shell_file_snapshots"] == [
        _shell_summary(spelled_out),
        {**_shell_summary(spelled_out), "operation": "create"},
    ]


@pytest.mark.anyio
async def test_sub_agent_failed_tool_paths_reach_the_client_as_display_text() -> None:
    # A failed tool call keeps the raw path it resolved, undecodable byte and all.
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        text = tool_error("path_not_found", "Path not found.", details={"resolved_path": "/w/caf\udce9/missing"})
    finally:
        tool_result_metadata.reset(token)
    result = InvocationToolCallResult(
        agent_name="General",
        tool_name="grep",
        call_id="c1",
        result=text,
        metadata=metadata,
        session_id="s1",
        origin=_SUB_AGENT,
    )

    sent = await _sent_sub_agent_result(result)

    assert sent["metadata"]["tool_error_details"] == {"resolved_path": "/w/caf\\udce9/missing"}


@pytest.mark.anyio
async def test_sub_agent_tool_start_sends_typed_args_as_json() -> None:
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(_FakeHost(event_bus=EventBus())), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event(
        "s1",
        InvocationToolCallStart(
            agent_name="General",
            tool_name="schedule",
            call_id="c1",
            args=_TYPED_ARGS,
            session_id="s1",
            origin=_SUB_AGENT,
        ),
        AcpEventBridge(),
        {},
    )

    [params] = [params for method, params in client.ext_notifications if method == "chrys/sub_agent_tool_call_start"]
    assert acp_outgoing_json(params)["args"] == _TYPED_ARGS_JSON


@pytest.mark.anyio
async def test_main_tool_call_sends_typed_args_as_raw_input_json() -> None:
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(_FakeHost(event_bus=EventBus())), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    bridge = AcpEventBridge()

    await server._handle_event(
        "s1",
        InvocationToolCallStart(tool_name="schedule", call_id="c1", args=_TYPED_ARGS, session_id="s1", origin=_TURN),
        bridge,
        {},
    )
    await server._handle_event(
        "s1",
        InvocationToolCallArgsUpdated(
            tool_name="schedule", call_id="c1", args=_TYPED_ARGS, session_id="s1", origin=_TURN
        ),
        bridge,
        {},
    )

    assert [acp_outgoing_json(update)["update"]["rawInput"] for update in client.updates] == [
        _TYPED_ARGS_JSON,
        _TYPED_ARGS_JSON,
    ]


@pytest.mark.anyio
async def test_permission_request_sends_typed_args_as_raw_input_json() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)

    await server._handle_event(
        "s1",
        ApprovalRequest(request_id="r1", tool_name="schedule", args=_TYPED_ARGS, session_id="s1"),
        AcpEventBridge(),
        {},
    )

    [request] = client.permission_requests
    assert acp_outgoing_json(request)["toolCall"]["rawInput"] == _TYPED_ARGS_JSON
    assert [response.approved for response in responses] == [True]


@pytest.mark.anyio
async def test_agent_profile_read_returns_yaml_typed_metadata_as_json(tmp_path: Path) -> None:
    profile_path = tmp_path / "Dated.yaml"
    profile_path.write_text(
        "name: Dated\n"
        "metadata:\n"
        "  created: 2026-10-09\n"
        "  reviewed: 2026-10-09 10:42:00\n"
        "  tags: !!set {b: null, a: null}\n"
        "  blob: !!binary aGk=\n"
        '  icon: "\\ud83d\\ude00"\n',
        encoding="utf-8",
    )
    registry = AgentProfileRegistry()
    registry.register(load_profile_from_yaml(profile_path))
    server = ChrysAcpServer(_profile_manager("Dated", registry), initial_vision=False)

    # The autouse fixture in this package's conftest encodes the reply as the SDK would.
    reply = await server.ext_method("profiles/agents/read", {"name": "Dated"})

    assert reply["profile"]["metadata"] == {
        "created": "2026-10-09",
        "reviewed": "2026-10-09T10:42:00",
        "tags": ["a", "b"],
        "blob": "hi",
        # YAML reads the escape as a surrogate pair; the client gets the emoji.
        "icon": "😀",
    }


@pytest.mark.anyio
async def test_fake_client_rejects_a_payload_the_wire_cannot_carry() -> None:
    client = _FakeClient()

    with pytest.raises(AssertionError, match=r"FileHashDiff is not JSON serializable.*'metadata'"):
        await client.ext_notification("chrys/probe", {"metadata": {"hashes": _HASHES}})
    with pytest.raises(AssertionError, match="not JSON compliant"):
        await client.ext_notification("chrys/probe", {"ratio": math.nan})
    with pytest.raises(AssertionError, match="unpaired surrogates"):
        await client.ext_notification("chrys/probe", {"path": "/w/caf\udce9"})
    assert client.ext_notifications == []
    assert len(take_acp_wire_failures()) == 3


def test_fake_wire_carries_a_surrogate_pair_whole() -> None:
    # Escaped as the SDK writes it, the pair reads back as one character.
    assert acp_outgoing_json({"text": "ok\ud83d\ude00"}) == {"text": "ok😀"}


@pytest.mark.anyio
async def test_a_failed_send_the_server_catches_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    # Undo the conversion: the permission request carries the typed arguments again.
    monkeypatch.setattr(server_module, "to_json_object", dict)
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)

    await server._handle_event(
        "s1",
        ApprovalRequest(request_id="r1", tool_name="schedule", args=_TYPED_ARGS, session_id="s1"),
        AcpEventBridge(),
        {},
    )

    # The server reads the failed send as a rejection and carries on.
    assert [response.approved for response in responses] == [False]
    [failure] = take_acp_wire_failures()
    assert "datetime is not JSON serializable" in failure


@pytest.mark.anyio
async def test_server_replies_are_checked_for_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _FakeManager(_FakeHost(event_bus=EventBus()))
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "read_agent_profile", lambda name: {"metadata": {"created": date(2026, 10, 9)}})

    with pytest.raises(AssertionError, match="date is not JSON serializable"):
        await server.ext_method("profiles/agents/read", {"name": "Dated"})
    assert len(take_acp_wire_failures()) == 1
