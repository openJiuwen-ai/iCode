# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-way runtime flag for relay-sourced risk discovered outside a build."""

from __future__ import annotations

from collections.abc import Callable


class RelayRiskLatch:
    """One-way flag: relay-routed model traffic was seen in this conversation.

    A build computes its relay enhancement from the models it was configured
    with, but risk can also appear at run time — a sub-agent whose own model
    profile is a relay returns its output into the parent's conversation, and
    from then on the parent's later calls must be reviewed under the same
    strict policy. Owners poll the latch (``bool(latch)``) wherever the
    build-time flag is read; ``set()`` is idempotent and the latch never
    clears, because relay-sourced content stays in the conversation history
    (the session persists the same fact as ``relay_risk_seen``).

    ``on_set`` runs synchronously inside ``set()`` exactly once; it is the
    owner's hook to persist the fact (never to mutate review policy —
    consumers read the latch itself).
    """

    __slots__ = ("_on_set", "_seen")

    def __init__(self, on_set: Callable[[], None] | None = None) -> None:
        self._seen = False
        self._on_set = on_set

    def set(self) -> None:
        """Latch relay risk; first call only."""
        if self._seen:
            return
        self._seen = True
        if self._on_set is not None:
            self._on_set()

    def __bool__(self) -> bool:
        return self._seen
