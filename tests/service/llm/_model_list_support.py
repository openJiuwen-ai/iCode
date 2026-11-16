# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A recording mock server and small factories shared by the ``discover_models`` spec tests."""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from chrys.service.llm import model_list as model_list_module
from chrys.service.llm.clients import _SDK_MODULES
from chrys.service.llm.model_list import ConnectionSnapshot, ModelDiscovery, ModelInfo, discover_models
from chrys.service.llm.providers import PROVIDERS

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider address or key from the developer's shell: the tests set the ones they need."""
    for spec in PROVIDERS.values():
        for name in (spec.api_key_env, spec.base_url_env):
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)


class Recorder:
    """A mock server that records every request and answers through *handler*."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = handler

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answer(request)
        if not isinstance(answer, httpx.Response):
            answer = await answer
        return answer

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def paths(self) -> list[str]:
        """``scheme://host[:port]/path`` of every request, query dropped."""
        return [str(request.url.copy_with(query=None)) for request in self.requests]


def snap(
    provider: str = "openai",
    base_url: str = "https://gw.example.test/v1",
    api_key: str = "sk-test",
    **kwargs: Any,
) -> ConnectionSnapshot:
    return ConnectionSnapshot(provider=provider, base_url=base_url, api_key=api_key, **kwargs)


# The production timing, read before any test shortens it.
REAL_TOTAL_SECONDS = model_list_module._TOTAL_SECONDS
REAL_REQUEST_SECONDS = model_list_module._REQUEST_SECONDS
REAL_RETRY_BASE_SECONDS = model_list_module._RETRY_BASE_SECONDS
REAL_RETRY_MAX_PAUSE_SECONDS = model_list_module._RETRY_MAX_PAUSE_SECONDS
REAL_TOTAL_ROUNDS = model_list_module._TOTAL_ROUNDS
# Shortened timing: five rounds finish at once, and the deadline still outlasts a loaded CI worker.
FAST_TOTAL_SECONDS = 10.0


def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the 1, 2, 4, 4 s pauses between retry rounds and the 60 s deadline out of the test run.

    The provider SDK is loaded first: a cold import inside the lookup would eat a short deadline.
    """
    preload_sdks()
    monkeypatch.setattr(model_list_module, "_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(model_list_module, "_RETRY_MAX_PAUSE_SECONDS", 0.004)
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", FAST_TOTAL_SECONDS)


def calls_per_url(recorder: Recorder) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in recorder.paths():
        counts[path] = counts.get(path, 0) + 1
    return counts


def openai_list(*ids: str, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": i, "object": "model"} for i in ids], **extra})


def status(code: int, body: Any = None) -> httpx.Response:
    return httpx.Response(code, json=body if body is not None else {"error": {"message": "nope"}})


def auth_group(request: httpx.Request) -> str | None:
    has_a = "authorization" in request.headers
    has_b = "x-api-key" in request.headers
    assert not (has_a and has_b), f"auth groups mixed in one request: {dict(request.headers)}"
    if has_a:
        return "A"
    if has_b:
        return "B"
    return None


def ids(models: list[ModelInfo]) -> list[str]:
    return [model.id for model in models]


async def discover_full(snapshot: ConnectionSnapshot, recorder: Recorder) -> ModelDiscovery:
    result = await discover_models(snapshot, transport=recorder.transport)
    assert isinstance(result, ModelDiscovery)
    return result


async def discover(snapshot: ConnectionSnapshot, recorder: Recorder) -> list[ModelInfo]:
    return (await discover_full(snapshot, recorder)).models


def always(response_factory: Callable[[], httpx.Response]) -> Handler:
    return lambda _request: response_factory()


def only_base(base_path: str, body: Callable[[], httpx.Response]) -> Handler:
    """404 everywhere except *base_path*, which answers *body*."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == base_path:
            return body()
        return status(404)

    return handler


def item_marked(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": i, "object": "model"} for i in ids]})


def flaky(answer: Callable[[httpx.Request], httpx.Response], *, fails: int = 1, paths: tuple[str, ...] = ()) -> Handler:
    """Fail to connect the first *fails* times each URL is asked (only URLs on *paths*, if given), then *answer*."""
    seen: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not paths or request.url.path in paths:
            seen[url] = seen.get(url, 0) + 1
            if seen[url] <= fails:
                raise httpx.ConnectError("flaky", request=request)
        return answer(request)

    return handler


def openai_list_answer(*ids: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: openai_list(*ids)


def preload_sdks() -> None:
    """Keep cold SDK imports outside lookup deadlines."""
    for modules in _SDK_MODULES.values():
        for module in modules:
            importlib.import_module(module)


def html() -> httpx.Response:
    return httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"})
