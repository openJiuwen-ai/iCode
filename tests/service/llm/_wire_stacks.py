# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Production OpenAI client stacks, shared by the wire-client test modules."""

from __future__ import annotations

from typing import Any

from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.clients import _assemble_stack
from chrys.service.llm.openai_responses import ResponsesApiClient
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer


def assemble_openai_stack(
    client_cls: type[Any],
    sdk_client: Any,
    *,
    model_id: str = "gpt-test",
    session_id: str | None = None,
    parent_session_id: str | None = None,
    use_route_session_context: bool = False,
) -> Any:
    """Assemble the production stack the client factory builds, over *sdk_client*.

    Its loop layer is swapped for the transcript-checked one, so every tool
    loop a test drives through it runs the transcript-invariant oracle.
    """
    stack = _assemble_stack(
        client_cls,
        sdk_client,
        model_id=model_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        use_route_session_context=use_route_session_context,
        on_intermediate_text=None,
        connection={},
        max_iterations=7777,
        max_consecutive_errors=10,
        tool_result_ceiling_tokens=None,
    )
    return InvariantCheckedToolLoopLayer(
        stack.inner,
        max_iterations=stack.max_iterations,
        max_consecutive_errors=stack.max_consecutive_errors,
        tool_result_ceiling_tokens=stack.tool_result_ceiling_tokens,
    )


def make_chat_client(
    session_id: str | None = None,
    chat_client_cls: type[Any] = ChatCompletionsClient,
    parent_session_id: str | None = None,
    use_route_session_context: bool = False,
) -> Any:
    """Construct the production Chat Completions stack with a fake API key."""
    from openai import AsyncOpenAI

    return assemble_openai_stack(
        chat_client_cls,
        AsyncOpenAI(api_key="sk-fake"),
        session_id=session_id,
        parent_session_id=parent_session_id,
        use_route_session_context=use_route_session_context,
    )


def make_responses_chat_client(
    session_id: str | None = None,
    parent_session_id: str | None = None,
    use_route_session_context: bool = False,
    chat_client_cls: type[Any] = ResponsesApiClient,
) -> Any:
    """Construct the production Responses stack with a fake API key."""
    from openai import AsyncOpenAI

    return assemble_openai_stack(
        chat_client_cls,
        AsyncOpenAI(api_key="sk-fake"),
        session_id=session_id,
        parent_session_id=parent_session_id,
        use_route_session_context=use_route_session_context,
    )
