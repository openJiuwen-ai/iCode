# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Everything the ACP server sends or answers in these tests must fit the wire.

The fake clients in ``_server_fakes.py`` check what the server sends; the
autouse fixture checks what its handlers answer. It wraps each handler the
SDK's ``Agent`` protocol names, so a handler added later is checked as well.
Each check raises at the failing send and records the failure; the fixture
fails the test at teardown for any failure left unclaimed, so a send whose
error the server catches (a permission request reads a failure as a
rejection) still fails the test.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from acp import RequestError
from acp.interfaces import Agent

from chrys.app.acp.server import ChrysAcpServer
from tests.support.acp_wire import acp_error_json, acp_response_json, take_acp_wire_failures

type _Handler = Callable[..., Coroutine[Any, Any, Any]]

AGENT_HANDLER_NAMES = tuple(
    name
    for name, member in vars(Agent).items()
    if not name.startswith("_") and inspect.iscoroutinefunction(member) and name in vars(ChrysAcpServer)
)


def _replies_with_json(handler: _Handler) -> _Handler:
    @functools.wraps(handler)
    async def checked(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await handler(*args, **kwargs)
        except RequestError as error:
            acp_error_json(error)
            raise
        acp_response_json(result)
        return result

    return checked


@pytest.fixture(autouse=True)
def _acp_traffic_is_json(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    take_acp_wire_failures()
    for name in AGENT_HANDLER_NAMES:
        monkeypatch.setattr(ChrysAcpServer, name, _replies_with_json(vars(ChrysAcpServer)[name]))
    yield
    failures = take_acp_wire_failures()
    assert not failures, "\n".join(failures)
