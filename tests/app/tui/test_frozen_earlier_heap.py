# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consecutive TUI tests can freeze the live heap without retaining earlier widget cycles."""

import gc
import weakref

import pytest


class _Cycle:
    def __init__(self) -> None:
        self.link = self


def test_fixture_reclaims_garbage_before_freezing_survivors(request: pytest.FixtureRequest) -> None:
    survivor = _Cycle()
    survivor_ref = weakref.ref(survivor)
    enabled = gc.isenabled()
    gc.disable()
    try:
        discarded = weakref.ref(_Cycle())
        assert discarded() is not None
        request.getfixturevalue("frozen_earlier_heap")
        assert discarded() is None
        del survivor
        current = weakref.ref(_Cycle())
        gc.collect()
        assert current() is None
        assert survivor_ref() is not None
    finally:
        if enabled:
            gc.enable()
