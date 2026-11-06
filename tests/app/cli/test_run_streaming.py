# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``icode run`` on a real host: progress streams to stderr while the answer waits for stdout."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.app.cli import headless
from chrys.app.cli import run as run_cli
from chrys.app.cli.progress import display_path
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.i18n import Localizer
from chrys.foundation.models.session_surface import SessionSurface
from chrys.kernel import ChatResponseUpdate, FunctionTool
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.workflows._hosting import make_host
from tests.support.tool_packs import install_tools
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_DURATION = re.compile(r"\b\d+\.\ds\b")
_SESSION = re.compile(r"\bsession [0-9a-z]{12}\b")


def _progress(err: str) -> list[str]:
    return [_SESSION.sub("session <id>", _DURATION.sub("<dur>", line)) for line in err.splitlines()]


def _real_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: MockChatClient, tool: FunctionTool) -> None:
    """The command builds a real host (``make_host``) on a mock model, with *tool* as its only builtin.

    Each replacement is autospecced from the production callable, so a call production would reject fails here too.
    """

    async def create_client(
        _profile: Any,
        on_intermediate_text: Callable[[str], None] | None = None,
        **_routing: object,
    ) -> MockChatClient:
        client._on_intermediate_text = on_intermediate_text
        return client

    def build(
        *,
        profile_name: str,
        loaded_settings: LoadedSettings,
        approval_mode: ApprovalMode,
        surface: SessionSurface | None = None,
        **_kwargs: object,
    ):
        assert approval_mode is ApprovalMode.BYPASS
        return make_host(
            tmp_path,
            project=tmp_path,
            profile_name=profile_name,
            loaded_settings=loaded_settings,
            stream=True,
            surface=surface,
        )

    def prepare_runtime(*, restoring_session: bool = False) -> headless.PreparedRuntime:
        assert not restoring_session
        loaded = LoadedSettings(settings=Settings(model_profile="mock-profile"), provenance={})
        return headless.PreparedRuntime(loaded=loaded, localizer=Localizer("en"), pending_warnings=[])

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, side_effect=create_client)
    )
    install_tools(monkeypatch, [tool])
    monkeypatch.setattr(run_cli, "ChrysSessionHost", create_autospec(ChrysSessionHost, side_effect=build))
    monkeypatch.setattr(
        headless, "prepare_runtime", create_autospec(headless.prepare_runtime, side_effect=prepare_runtime)
    )


@pytest.mark.parametrize("mode", ["text", "json", "quiet"])
async def test_progress_streams_while_a_tool_runs_and_the_answer_lands_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held(message: Annotated[str, "Text to echo"]) -> str:
        entered.set()
        await release.wait()
        return f"held: {message}"

    client = MockChatClient(
        responses=[
            MockResponse(text="Checking first.", tool_calls=[("held_tool", "c1", {"message": "hi"})]),
            MockResponse(text="final answer"),
        ]
    )
    _real_host(monkeypatch, tmp_path, client, FunctionTool(func=held, name="held_tool", description="Waits"))
    flags = {"text": [], "json": ["--json"], "quiet": ["--quiet"]}[mode]
    args = run_cli.build_parser().parse_args(["hello", "--agent", "Headless", *flags])

    task = asyncio.create_task(run_cli.run_command(args, run_cli.PreparedRuntimeHolder()))
    try:
        await wait_for(
            lambda: entered.is_set() or task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the held tool is running",
        )
        assert not task.done()
        during = capsys.readouterr()
    finally:
        # The run owns a host: it always finishes (and shuts it down) before the test does.
        release.set()
        exit_code = await task
    assert exit_code == 0
    after = capsys.readouterr()

    # Nothing reaches stdout before the final answer, in every mode.
    assert during.out == ""
    if mode == "text":
        assert _progress(during.err) == [
            f"• Headless ready · mock · session <id> · {display_path(str(tmp_path))}",
            "Checking first.",
            "→ held_tool",
        ]
        assert after.out == "final answer\n"
        assert _progress(after.err) == ["  ✓ <dur>", "", "✓ Done · <dur> · 1 tool call · session <id>"]
    elif mode == "json":
        assert during.err == after.err == ""
        payload = json.loads(after.out)
        assert payload["result"] == "final answer"
    else:
        assert during.err == after.err == ""
        assert after.out == "final answer\n"


class _DroppingClient(MockChatClient):
    """Its first stream drops right after its first text."""

    def __init__(self, responses: list[MockResponse]) -> None:
        super().__init__(responses=responses)
        self._dropped = False

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: Any
    ) -> AsyncIterator[ChatResponseUpdate]:
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update
            if not self._dropped and any(content.type == "text" for content in update.contents):
                self._dropped = True
                raise ProviderResponseError("stream_truncated", "the connection dropped", retryable=True)


async def test_a_reply_that_drops_before_it_printed_is_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`icode run` holds a reply's text until it knows what it is: text it never printed stops no retry."""
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    client = _DroppingClient([MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="Whole answer")])

    async def unused() -> str:
        return ""

    _real_host(monkeypatch, tmp_path, client, FunctionTool(func=unused, name="unused_tool", description="Unused"))
    args = run_cli.build_parser().parse_args(["hello", "--agent", "Headless"])

    exit_code = await asyncio.wait_for(
        run_cli.run_command(args, run_cli.PreparedRuntimeHolder()), timeout=ENGINE_TURN_TIMEOUT
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client.call_count == 2
    assert captured.out == "Whole answer\n"
    lines = _progress(captured.err)
    assert lines[0] == f"• Headless ready · mock · session <id> · {display_path(str(tmp_path))}"
    # The retry notice, and none of the text the retry gave up.
    assert lines[1].startswith("↻ The model's response was cut off. Retrying in 0s")
    assert lines[2:] == ["", "✓ Done · <dur> · 0 tool calls · session <id>"]
