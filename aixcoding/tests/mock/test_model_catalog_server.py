# (contract test — asserts are pytest idiom)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests for the Chrys model catalog mock (aixcoding/mock/model_catalog/server.py).

Conventions: bind port 0 (kernel-assigned), loopback only, every HTTP call goes
through ``direct_route`` (bypassing environment/system proxies).

The payload assertions anchor the reference server's own shape (``models``
array, no ``version`` field — the client fingerprints the id list); an earlier
mock generation served translated ``items``/``version`` and those assertions
were rewritten to match the current wire contract during the migration.

The last two tests are contract tests: they feed the mock's output through the
real client parser so the sample data cannot silently drift away from what the
client accepts. They auto-skip while ``chrys.service.profiles.models.catalog``
is absent (the consumer is fork customisation, migration pending); when that
module lands they unskip automatically, and the retained 0.28-era assertions
must then be re-checked against the migrated consumer's design. The mock
itself imports nothing from the product — only its tests look at both sides.
"""

from __future__ import annotations

import importlib.util
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from aixcoding.mock.auth.server import INVALID_TOKEN_MESSAGE, MockAuthConfig, MockGrant
from aixcoding.mock.model_catalog import server as catalog_server
from aixcoding.mock.model_catalog.server import RunningCatalogMock


@pytest.fixture
def mock_factory() -> Iterator[Callable[..., RunningCatalogMock]]:
    """Start mocks on demand (port 0, quiet) and reap them all."""
    started: list[RunningCatalogMock] = []

    def _factory(**kwargs: Any) -> RunningCatalogMock:
        kwargs.setdefault("quiet", True)
        running = catalog_server.start(**kwargs)
        started.append(running)
        return running

    yield _factory
    for running in started:
        running.close()


@pytest.fixture
def mock(mock_factory: Callable[..., RunningCatalogMock]) -> RunningCatalogMock:
    return mock_factory()


def _catalog(running: RunningCatalogMock) -> str:
    return f"{running.origin}{catalog_server.CATALOG_ROUTE}"


async def _get(url: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(url)


async def _control(running: RunningCatalogMock, **config: Any) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{running.origin}/mock/control", json=config)
        assert response.status_code == 200
        return response.json()


async def test_serves_builtin_sample(mock: RunningCatalogMock, direct_route: None) -> None:
    """The dispatch endpoint serves the reference server's own object shape."""
    response = await _get(_catalog(mock))
    assert response.status_code == 200
    payload = response.json()
    assert [entry["model"] for entry in payload["models"]] == ["openai/gpt-4o-mini"]
    entry = payload["models"][0]
    assert entry["title"] == "GPT-4o mini"
    assert entry["apiBase"] == "http://127.0.0.1:7777/v1"
    assert entry["contextLength"] == 128000
    # Unknown top-level keys ride through verbatim — filtering is the client's job.
    assert payload["aa"] == {"test": "as"}
    # The reference payload has no version field; the client fingerprints ids.
    assert "version" not in payload


async def test_query_string_is_ignored(mock: RunningCatalogMock, direct_route: None) -> None:
    """The client appends scope/userId query params; they must not change the answer."""
    plain = await _get(_catalog(mock))
    queried = await _get(f"{_catalog(mock)}?scopeType=global&scopeValue=config&format=json&userId=someone")
    assert queried.status_code == 200
    assert queried.json() == plain.json()


async def test_removed_routes_answer_404(mock: RunningCatalogMock, direct_route: None) -> None:
    """Only the dispatch path and the control endpoint exist."""
    assert (await _get(f"{mock.origin}/model-catalog")).status_code == 404
    assert (await _get(f"{mock.origin}/mock/state")).status_code == 404


async def test_embedded_auth_routes_share_the_catalog_origin(
    mock_factory: Callable[..., RunningCatalogMock],
    direct_route: None,
) -> None:
    """The launcher's default combo mounts the auth mock on the catalog origin
    (login + catalog from one port); anchors the CombinedMockHandler bridge."""
    from aixcoding.mock.auth.server import DEVICE_CODE_ROUTE, MockAuthConfig

    running = mock_factory(auth=MockAuthConfig(mode="auto"))
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{running.origin}{DEVICE_CODE_ROUTE}", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    device_code = body["result"]["device_code"]
    assert isinstance(device_code, str) and device_code


async def test_control_switches_to_error(mock: RunningCatalogMock, direct_route: None) -> None:
    snapshot = await _control(mock, mode="error")
    assert snapshot["mode"] == "error"
    assert snapshot["version"] == "mock-2"  # every switch is a new revision
    assert (await _get(_catalog(mock))).status_code == 500
    assert (await _control(mock, mode="ok"))["mode"] == "ok"
    assert (await _get(_catalog(mock))).status_code == 200


async def test_control_reports_request_count(mock: RunningCatalogMock, direct_route: None) -> None:
    await _get(_catalog(mock))
    snapshot = await _control(mock, mode="ok")  # the snapshot replaces /mock/state
    assert snapshot["mode"] == "ok"
    assert snapshot["requests"] == 2  # the catalog call plus this control call


async def test_unknown_mode_is_rejected(mock: RunningCatalogMock, direct_route: None) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{mock.origin}/mock/control", json={"mode": "nope"})
    assert response.status_code == 400
    assert "mode" in response.json()["error"]


async def test_empty_mode_serves_no_models(mock: RunningCatalogMock, direct_route: None) -> None:
    await _control(mock, mode="empty")
    assert (await _get(_catalog(mock))).json()["models"] == []


async def test_invalid_mode_serves_unusable_profiles(mock: RunningCatalogMock, direct_route: None) -> None:
    """Entries with no usable ``model`` — the client derives its id from it and must refuse."""
    await _control(mock, mode="invalid")
    models = (await _get(_catalog(mock))).json()["models"]
    assert len(models) == 2
    assert all(not str(entry.get("model") or "").strip() for entry in models)


async def test_stale_mode_drifts_title_but_keeps_derived_ids(mock: RunningCatalogMock, direct_route: None) -> None:
    """No version field exists, so the client fingerprints the id list — ``stale``
    drifts ``title`` while every derivable id (from ``model``) stays put, which is
    exactly why polling must skip the rewrite."""
    await _control(mock, mode="stale")
    first = (await _get(_catalog(mock))).json()
    second = (await _get(_catalog(mock))).json()
    assert "version" not in first
    assert first["models"][0]["model"] == second["models"][0]["model"]
    assert first["models"][0]["title"] != second["models"][0]["title"]


async def test_slow_mode_stalls_before_answering(
    mock_factory: Callable[..., RunningCatalogMock],
    direct_route: None,
) -> None:
    running = mock_factory(mode="slow", delay=0.3)
    started = time.monotonic()
    response = await _get(_catalog(running))
    assert response.status_code == 200
    assert time.monotonic() - started >= 0.25


async def test_catalog_file_is_reread_per_request(
    mock_factory: Callable[..., RunningCatalogMock],
    tmp_path: Path,
    direct_route: None,
) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"items": [{"id": "first", "name": "First", "provider": "mock"}]}))
    running = mock_factory(catalog_file=path)

    first = await _get(_catalog(running))
    assert [item["id"] for item in first.json()["items"]] == ["first"]

    path.write_text(json.dumps({"items": [{"id": "second", "name": "Second", "provider": "mock"}]}))
    second = await _get(_catalog(running))
    assert [item["id"] for item in second.json()["items"]] == ["second"]


async def test_unreadable_catalog_file_answers_500(
    mock_factory: Callable[..., RunningCatalogMock],
    tmp_path: Path,
    direct_route: None,
) -> None:
    path = tmp_path / "catalog.json"
    path.write_text("{ not json")
    running = mock_factory(catalog_file=path)
    response = await _get(_catalog(running))
    assert response.status_code == 500
    assert "not valid JSON" in response.json()["error"]


def test_non_loopback_bind_is_rejected() -> None:
    with pytest.raises(ValueError):
        catalog_server.start(host="0.0.0.0")
    with pytest.raises(ValueError):
        catalog_server.create_server(host="0.0.0.0")


def test_second_instance_on_a_taken_port_is_refused() -> None:
    """The Windows port-hijack hole stays closed: a live listener — even one
    bound with SO_REUSEADDR, i.e. another mock instance, the stale-process
    case — must block a second bind (fail-fast, never a silent second copy)."""
    first = catalog_server.create_server(port=0)
    try:
        with pytest.raises(OSError):
            catalog_server.create_server(port=first.server_address[1])
    finally:
        first.server_close()


async def test_rejection_keeps_the_keepalive_connection_usable(mock: RunningCatalogMock, direct_route: None) -> None:
    """A rejected request must drain its body: leftover bytes on the
    HTTP/1.1 keep-alive connection would be parsed as the next request line."""
    async with httpx.AsyncClient() as client:
        first = await client.post(f"{mock.origin}{catalog_server.CATALOG_ROUTE}", json={"junk": "x" * 200})
        assert first.status_code == 405
        second = await client.get(f"{mock.origin}{catalog_server.CATALOG_ROUTE}")
        assert second.status_code == 200


async def test_control_rejects_a_non_numeric_delay(mock: RunningCatalogMock, direct_route: None) -> None:
    """A bad delay answers 400 (not a dropped connection) and changes nothing."""
    async with httpx.AsyncClient() as client:
        before = (await _control(mock, mode="ok"))["revision"]
        response = await client.post(f"{mock.origin}/mock/control", json={"mode": "ok", "delay": "abc"})
    assert response.status_code == 400
    assert "delay" in response.json()["error"]
    async with httpx.AsyncClient() as client:
        after = (await _control(mock, mode="ok"))["revision"]
    assert after == before + 1  # only the two valid control calls bumped it


#: The consumer-side contract tests need the fork's catalog consumer code
#: (``chrys.service.profiles.models.catalog``), which exists on the 0.28
#: baseline and has not migrated to this branch yet. Skip — automatically —
#: until it lands; design.md assumption 1 fallback plan.
_CONSUMER_PARSER_PRESENT = importlib.util.find_spec("chrys.service.profiles.models.catalog") is not None

#: Skipped while the fork's catalog consumer has not migrated; re-check the
#: 0.28-era assertions below against the consumer feature design when it lands.
_SKIP_REASON = (
    "catalog consumer (parse_catalog) not yet migrated to this branch; "
    "unskips at migration time — re-check assertions against the consumer design then"
)


@pytest.mark.skipif(
    not _CONSUMER_PARSER_PRESENT,
    reason=_SKIP_REASON,
)
async def test_sample_payload_is_accepted_by_the_client_parser(mock: RunningCatalogMock, direct_route: None) -> None:
    from chrys.service.profiles.models.catalog import parse_catalog

    payload = (await _get(_catalog(mock))).json()
    catalog = parse_catalog(payload)
    assert len(catalog.items) == 5
    assert catalog.version == "mock-1"


@pytest.mark.skipif(
    not _CONSUMER_PARSER_PRESENT,
    reason=_SKIP_REASON,
)
async def test_empty_and_invalid_payloads_are_refused_by_the_client_parser(
    mock: RunningCatalogMock,
    direct_route: None,
) -> None:
    from chrys.service.profiles.models.catalog import ModelCatalogError, parse_catalog

    await _control(mock, mode="empty")
    with pytest.raises(ModelCatalogError):
        parse_catalog((await _get(_catalog(mock))).json())

    await _control(mock, mode="invalid")
    with pytest.raises(ModelCatalogError):
        parse_catalog((await _get(_catalog(mock))).json())


async def _post_chat(running: RunningCatalogMock, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(f"{running.origin}{catalog_server.LLM_ROUTE}", json=body)


async def test_llm_chat_non_streaming(mock: RunningCatalogMock, direct_route: None) -> None:
    """A non-streaming chat request gets a minimal valid ChatCompletion back."""
    response = await _post_chat(
        mock,
        {
            "model": "mock-model",
            "stream": False,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "mock-model"
    assert payload["object"] == "chat.completion"
    choice = payload["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == catalog_server.LLM_MOCK_REPLY
    assert choice["finish_reason"] == "stop"


async def test_llm_chat_streaming(mock: RunningCatalogMock, direct_route: None) -> None:
    """A ``stream: True`` request is answered on the SSE wire and ends with [DONE]."""
    response = await _post_chat(
        mock,
        {
            "model": "mock-model",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert "chat.completion.chunk" in text
    assert catalog_server.LLM_MOCK_REPLY in text
    assert text.endswith("data: [DONE]\n\n")


# ── catalog-auth-gating: the catalog is an authenticated endpoint ─────────────


async def _get_with_token(url: str, token: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(url, headers={"token": token})


def _issue_token(running: RunningCatalogMock, *, expires_in: float = 600.0) -> str:
    """Grant a token straight through the shared auth state (device-flow shortcut)."""
    state = running._server.auth_state  # type: ignore[attr-defined]
    now = time.monotonic()
    grant = MockGrant(device_code="d", user_code="u", issued_at=now, expires_at=now + expires_in)
    state._grants["d"] = grant
    state._authorize(grant)  # assigns the token and marks the grant granted
    return grant.token


async def test_catalog_without_a_token_is_401_when_auth_is_mounted(
    mock_factory: Callable[..., RunningCatalogMock], direct_route: None
) -> None:
    running = mock_factory(auth=MockAuthConfig(mode="auto"))
    response = await _get(_catalog(running))
    assert response.status_code == 401
    assert "error" in response.json()


async def test_catalog_with_an_unknown_token_is_401(
    mock_factory: Callable[..., RunningCatalogMock], direct_route: None
) -> None:
    running = mock_factory(auth=MockAuthConfig(mode="auto"))
    response = await _get_with_token(_catalog(running), "no-such-token")
    assert response.status_code == 401


async def test_grant_expiry_does_not_invalidate_an_issued_token(
    mock_factory: Callable[..., RunningCatalogMock], direct_route: None
) -> None:
    """方案五 (PROD parity): an issued token outlives its grant's login window, both endpoints."""
    running = mock_factory(auth=MockAuthConfig(mode="auto"))
    token = _issue_token(running, expires_in=-1.0)
    origin = str(running.origin).rstrip("/")
    async with httpx.AsyncClient() as client:
        info = await client.post(f"{origin}/api/v1/user/info", json={"token": token})
        catalog_response = await client.get(_catalog(running), headers={"token": token})
    assert info.status_code == 200
    assert info.json()["data"]["ehr"] == "1234567", "grant expiry must not refuse an issued token"
    assert catalog_response.status_code == 200


async def test_catalog_with_a_valid_token_is_served(
    mock_factory: Callable[..., RunningCatalogMock], direct_route: None
) -> None:
    running = mock_factory(auth=MockAuthConfig(mode="auto"))
    token = _issue_token(running)
    response = await _get_with_token(_catalog(running), token)
    assert response.status_code == 200
    payload = response.json()
    assert [entry["model"] for entry in payload["models"]] == ["openai/gpt-4o-mini"]


async def test_catalog_without_auth_stays_open(mock: RunningCatalogMock, direct_route: None) -> None:
    """The --no-auth exemption: a catalog-only mock keeps serving without any token."""
    response = await _get(_catalog(mock))
    assert response.status_code == 200


async def test_revoked_token_is_refused_by_both_user_info_and_catalog(
    mock_factory: Callable[..., RunningCatalogMock], direct_route: None
) -> None:
    """The refusal path stays: an explicitly revoked token (_revoke) is unknown to both endpoints."""
    running = mock_factory(auth=MockAuthConfig(mode="auto"))
    token = _issue_token(running)
    state = running._server.auth_state  # type: ignore[attr-defined]
    state._revoke(state._grants["d"])
    origin = str(running.origin).rstrip("/")
    async with httpx.AsyncClient() as client:
        info = await client.post(f"{origin}/api/v1/user/info", json={"token": token})
        catalog_response = await client.get(_catalog(running), headers={"token": token})
    assert info.status_code == 200  # the business-envelope refusal, not an HTTP error
    body = info.json()
    assert body["message"] == INVALID_TOKEN_MESSAGE
    assert body["code"] == 400
    assert catalog_response.status_code == 401
