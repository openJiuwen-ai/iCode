# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A prompt carrying an undecodable byte still reaches the provider.

The engine, the OpenAI SDK and Chrys's client stack are real; only the HTTP
answers are scripted. A lone surrogate is what ``icode run`` holds when argv
came in with a byte that is not UTF-8, and what an MCP server's JSON text
decodes to — a legal ``str`` that strict UTF-8 refuses.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from chrys.foundation.events.types import InvocationMessage, UserMessage
from tests.support.mock_provider_turns import mock_provider_profile, run_mock_provider_turn
from tests.support.wire_cases._kit import cc_replies, cc_text


async def test_a_lone_surrogate_in_the_prompt_is_sent_as_its_escape(
    agent_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (reply,) = cc_replies([cc_text("Sunny.", response_id="chatcmpl-1")], stream=False)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(reply.status, headers=list(reply.headers), content=reply.body, request=request)

    turn = await run_mock_provider_turn(
        agent_engine,
        monkeypatch,
        mock_provider_profile("openai", stream=False),
        respond,
        user_message=UserMessage(text="bad \udcff byte"),
    )

    # The request left and the turn answered: before the fix, encoding the body
    # failed here and the turn ended on an Error with no request sent at all.
    assert isinstance(turn.terminal, InvocationMessage), turn.terminal
    (request,) = turn.requests
    assert "\udcff" not in request.content.decode("utf-8")
    messages = json.loads(request.content)["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "bad \\udcff byte"}
