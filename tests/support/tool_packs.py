# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Give every agent build a fixed set of local tools.

Every build path (main agent, sub-agent, workflow node) assembles its local
tools through ``chrys.service.tools.packs.assemble_tools``, which creates the
packs' tools in ``create_packs`` alone. Replacing that one step decides what
any agent built under the patch gets, while the build's real web assembly and
filters (no image tool for a text-only model, no ``ask_user`` without a user)
still run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

from chrys.service.tools import packs as tool_packs

if TYPE_CHECKING:
    import pytest

    from chrys.kernel import FunctionTool

TEST_PACK_ID = "test"
"""The pack the installed tools appear under."""

_CREATE_PACKS = tool_packs.create_packs
"""The production step, captured before any test replaces it, so a second install specs the real one."""

CreatePacks = Callable[[tool_packs.ToolBuild, list[str]], list[tuple[str, list["FunctionTool"]]]]


def install_tools(
    monkeypatch: pytest.MonkeyPatch,
    tools: Sequence[FunctionTool] = (),
    *,
    first_party: Sequence[str] = (),
    side_effect: CreatePacks | BaseException | None = None,
) -> Any:
    """Make every agent build's packs yield exactly *tools*; return the signature-checked spy.

    The *first_party* packs a build lists are created first, as in production.
    *side_effect* replaces the fixed result (an exception fails the build where tools load).
    """

    def create(build: tool_packs.ToolBuild, pack_ids: list[str]) -> list[tuple[str, list[FunctionTool]]]:
        created = _CREATE_PACKS(build, [pack_id for pack_id in pack_ids if pack_id in first_party])
        return [*created, (TEST_PACK_ID, list(tools))]

    spy = create_autospec(_CREATE_PACKS, side_effect=side_effect if side_effect is not None else create)
    monkeypatch.setattr(tool_packs, "create_packs", spy)
    return spy
