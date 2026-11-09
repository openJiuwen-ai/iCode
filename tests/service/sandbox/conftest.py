# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reset the process-wide sandbox id and retained server between tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _reset_sandbox_process_state():
    from chrys.service.sandbox import backend, supervisor

    backend._sandbox_id = None
    backend._lock = None
    supervisor._lock = None
    await supervisor._shutdown()
    yield
    await supervisor._shutdown()
    backend._sandbox_id = None
    backend._lock = None
    supervisor._lock = None
