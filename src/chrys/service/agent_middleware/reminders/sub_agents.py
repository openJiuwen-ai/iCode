# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``sub_agents`` catalog: the tip naming the sub-agents the agent can delegate to."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class SubAgentsSource:
    """The sub-agent tip one middleware offers."""

    name = "sub_agents"
    """The catalog's record name."""
    withdrawn = "No sub-agents are available to the current agent; earlier sub-agent tips no longer apply."
    """Replaces the tip once the current agent has no sub-agents."""

    def __init__(self, sub_agent_names: Sequence[str] | None) -> None:
        self._sub_agent_names = sub_agent_names

    def snapshot(self) -> str | None:
        """The tip naming the sub-agents available for delegation, or None without any."""
        if not self._sub_agent_names:
            return None
        names = ", ".join(f"`{n}`" for n in self._sub_agent_names)
        return (
            f"TIP: Sub-agents are available ({names}). Delegate only a scoped task whose "
            "benefit outweighs coordination; handle routine lookups directly. "
            "High context usage alone does not require delegation."
        )
