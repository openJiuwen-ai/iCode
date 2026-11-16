# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MemoryProvider — injects auto-loaded reference content into the system prompt.

Companion to :mod:`chrys.service.context.memory_loader`.  Receives the already-rendered
markdown block at construction time and re-injects it via
``context.extend_instructions`` on every LLM call.

Sub-agents bypass this provider by simply not passing ``memory_text`` to
their own :class:`chrys.service.context.manager.ContextManager`, so memory
does not duplicate into concurrent sub-agent contexts.
"""

from __future__ import annotations

from typing import Any

from chrys.kernel import AgentSession, ContextProvider, SessionContext


class MemoryProvider(ContextProvider):
    """Injects pre-loaded reference content into the agent's system prompt.

    The provider is a thin pass-through: text is rendered once by
    :func:`chrys.service.context.memory_loader.load_memory_content` at agent build
    time and re-emitted on every ``before_run``.  Soft-restart (profile or
    workspace switch) rebuilds the agent and instantiates a fresh provider,
    which re-reads files — so on-disk edits are picked up automatically
    after the next build.
    """

    DEFAULT_SOURCE_ID = "chrys_memory"

    def __init__(self, rendered_text: str, source_id: str = DEFAULT_SOURCE_ID) -> None:
        super().__init__(source_id)
        self._instructions = rendered_text

    def contributed_instructions(self) -> str:
        """The instructions this provider adds before every run."""
        return self._instructions

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        if self._instructions:
            context.extend_instructions(self.source_id, self._instructions)
