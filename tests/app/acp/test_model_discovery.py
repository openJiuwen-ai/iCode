# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fetch provider models through the sessionless ACP extension."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from acp import RequestError

from chrys.app.acp import model_discovery
from chrys.app.acp.server import ChrysAcpServer
from chrys.service.llm.model_list import ConnectionSnapshot, ModelDiscovery, discover_models
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile
from tests.app.acp._session_manager_fakes import _profile_manager


@pytest.fixture
def server() -> ChrysAcpServer:
    manager = _profile_manager("Code", AgentProfileRegistry(), ModelProfileRegistry())
    return ChrysAcpServer(manager, initial_vision=False)


def _mock_provider(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    async def discover(snapshot: ConnectionSnapshot, *, literal_connection: bool) -> ModelDiscovery:
        return await discover_models(
            snapshot, transport=httpx.MockTransport(handler), literal_connection=literal_connection
        )

    monkeypatch.setattr(model_discovery, "discover_models", discover)


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    pytest.fail("Invalid requests must not contact a provider")


_METHOD = "providers/models/list"
_URL = "https://example.test/v1"


@pytest.mark.anyio
async def test_discover_unsaved_form(server, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]})

    _mock_provider(monkeypatch, handler)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient")
    monkeypatch.setenv("DRAFT_TOKEN", "ambient-header")
    result = await server.ext_method(
        _METHOD,
        {
            "connection": {
                "provider": "openai",
                "baseUrl": _URL,
                "httpHeaders": {"X-Tenant": "old"},
                "extraHeaders": {"x-tenant": "{{DRAFT_TOKEN}}"},
            }
        },
    )
    assert result == {
        "models": [{"id": "model-a"}, {"id": "model-b"}],
        "authRejected": False,
        "unreachable": False,
        "correctedBaseUrl": None,
        "urlCorrections": [],
    }
    assert len(calls) == 1
    assert str(calls[0].url) == _URL + "/models"
    assert "authorization" not in calls[0].headers and "x-api-key" not in calls[0].headers
    assert calls[0].headers["x-tenant"] == "{{DRAFT_TOKEN}}"
    assert await server.ext_method("profiles/models/list", {}) == {"models": []}


@pytest.mark.anyio
async def test_discover_saved_masked_key_from_cold_registry(server, monkeypatch):
    profile = ModelProfile(
        "saved",
        "Saved",
        model_id="old",
        base_url=_URL,
        api_key="{{SAVED_KEY}}",
        verify_ssl=False,
        http_headers='{"X-Old":"removed"}',
    )
    path = save_profile(profile)
    original = path.read_bytes()
    monkeypatch.setenv("SAVED_KEY", "literal-{{!braces}}")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    _mock_provider(monkeypatch, handler)
    result = await server.ext_method(
        _METHOD,
        {
            "connection": {"provider": "openai", "baseUrl": _URL + "/", "apiKey": "***", "bypassProxy": True},
            "profileId": "saved",
        },
    )
    assert result == {
        "models": [],
        "authRejected": True,
        "unreachable": False,
        "correctedBaseUrl": None,
        "urlCorrections": [],
    }
    assert calls and calls[0].headers["authorization"] == "Bearer literal-{{!braces}}"
    assert all("x-old" not in call.headers for call in calls)
    assert path.read_bytes() == original


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fields",
    [
        {"provider": "mock"},
        {"httpHeaders": {"X": 23}},
        {"verifySsl": 1},
        {"bypassProxy": "yes"},
        {"connectTimeout": True},
        {"connectTimeout": 0},
        {"connectTimeout": float("inf")},
        {"apiKey": "***"},
        {"apiKey": "{{!never-run}}"},
        {"extraHeaders": {"X": "bad\nheader"}},
        {"baseUrl": "ftp://example.test"},
        {"baseUrl": "https://example.test:65536"},
        {"baseUrl": "https://example.test:0"},
        {"baseUrl": "https://bad host/v1"},
    ],
)
async def test_invalid_draft_rejected_before_discovery(server, monkeypatch, fields):
    _mock_provider(monkeypatch, _unexpected_request)
    with pytest.raises(RequestError) as caught:
        await server.ext_method(_METHOD, {"connection": {"provider": "openai", "baseUrl": _URL, **fields}})
    assert caught.value.code == -32602


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changed",
    [
        {"baseUrl": "https://another.test/v1"},
        {"provider": "anthropic"},
        {"verifySsl": False},
        {"bypassProxy": False},
        {"extraHeaders": {"Host": "other.test"}},
    ],
)
async def test_saved_key_requires_original_target(server, monkeypatch, changed):
    save_profile(ModelProfile("saved", "Saved", model_id="old", base_url=_URL, api_key="fixture", bypass_proxy=True))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient")

    _mock_provider(monkeypatch, _unexpected_request)
    with pytest.raises(RequestError) as caught:
        await server.ext_method(
            _METHOD,
            {
                "profileId": "saved",
                "connection": {"provider": "openai", "baseUrl": _URL, "apiKey": "***", "bypassProxy": True, **changed},
            },
        )
    assert caught.value.code == -32602


@pytest.mark.anyio
async def test_discover_corrects_draft_without_saving(server, monkeypatch):
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "available", "object": "model"}]})
        return httpx.Response(404)

    _mock_provider(monkeypatch, handler)
    result = await server.ext_method(_METHOD, {"connection": {"provider": "openai", "baseUrl": _URL + "/models"}})
    assert result == {
        "models": [{"id": "available"}],
        "authRejected": False,
        "unreachable": False,
        "correctedBaseUrl": _URL,
        "urlCorrections": ["removed_endpoint"],
    }
    assert await server.ext_method("profiles/models/list", {}) == {"models": []}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scheme", "auth"),
    [("http", {"apiKey": "draft-key"}), ("https", {"httpHeaders": {"Authorization": "Bearer draft-key"}})],
)
async def test_provider_unreachable_includes_only_credentialed_origin(server, monkeypatch, scheme, auth):
    from chrys.service.llm import model_list

    monkeypatch.setattr(model_list, "_TOTAL_ROUNDS", 1)
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("test service unavailable")

    _mock_provider(monkeypatch, handler)
    result = await server.ext_method(
        _METHOD, {"connection": {"provider": "openai", "baseUrl": f"{scheme}://localhost:12345/v1", **auth}}
    )
    assert result == {
        "models": [],
        "authRejected": False,
        "unreachable": True,
        "correctedBaseUrl": None,
        "urlCorrections": [],
    }
    assert requests
    assert all(request.url.scheme == scheme and request.url.netloc == b"localhost:12345" for request in requests)
    assert requests[0].headers["authorization"] == "Bearer draft-key"


@pytest.mark.anyio
@pytest.mark.parametrize("saved_key", ["   ", "{{NOT_SET_FOR_DISCOVERY}}", "{{!unavailable}}"])
async def test_saved_mask_must_resolve_to_a_key(server, monkeypatch, saved_key):
    monkeypatch.setenv("NOT_SET_FOR_DISCOVERY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "unused")
    save_profile(ModelProfile("saved", "Saved", model_id="old", base_url=_URL, api_key=saved_key))
    _mock_provider(monkeypatch, _unexpected_request)
    with pytest.raises(RequestError) as caught:
        await server.ext_method(
            _METHOD, {"profileId": "saved", "connection": {"provider": "openai", "baseUrl": _URL, "apiKey": "***"}}
        )
    assert caught.value.code == -32602


@pytest.mark.anyio
async def test_provider_default_address_and_explicit_anthropic_key(server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "   https://example.test   ")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    _mock_provider(monkeypatch, handler)
    result = await server.ext_method(_METHOD, {"connection": {"provider": "anthropic", "apiKey": "new-key"}})
    assert result["models"] == [{"id": "test-model"}]
    assert len(calls) == 1
    assert calls[0].url.host == "example.test" and calls[0].url.path == "/v1/models"
    assert calls[0].headers["x-api-key"] == "new-key"
