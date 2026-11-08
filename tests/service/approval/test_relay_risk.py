# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one-way relay-risk latch: set once, seen everywhere the build flag is read."""

from __future__ import annotations

from chrys.service.approval.relay_risk import RelayRiskLatch


def test_set_latches_once_and_runs_on_set_exactly_once() -> None:
    seen: list[int] = []
    latch = RelayRiskLatch(on_set=lambda: seen.append(1))

    assert not latch

    latch.set()
    latch.set()
    latch.set()

    assert latch
    assert seen == [1]


def test_latch_without_owner_callback_still_latches() -> None:
    latch = RelayRiskLatch()

    latch.set()

    assert latch
