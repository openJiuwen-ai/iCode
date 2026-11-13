# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build-local dependency preparation, shared by a root agent and its children."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.service.skills.discovery import SkillDiscoveryScope

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from chrys.service.mcp.cache import MCPAcquisitionScope, MCPConnectionCache


@dataclass(frozen=True)
class AgentInitialization:
    """Only initialization calls borrow these scopes; live providers never retain them."""

    mcp: MCPAcquisitionScope | None
    skills: SkillDiscoveryScope


@asynccontextmanager
async def initialize_dependencies(cache: MCPConnectionCache | None) -> AsyncGenerator[AgentInitialization]:
    """Keep pending preparation owned until this build finishes or rolls back."""
    async with AsyncExitStack() as stack:
        mcp = await stack.enter_async_context(cache.acquisition_scope()) if cache is not None else None
        skills = await stack.enter_async_context(SkillDiscoveryScope())
        yield AgentInitialization(mcp, skills)
