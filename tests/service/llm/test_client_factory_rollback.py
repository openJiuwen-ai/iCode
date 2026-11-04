# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``create_client`` closes the HTTP client it built when a later build step fails."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import anthropic
import httpx
import openai
import pytest

import chrys.service.llm.clients as clients_module
from chrys.service.llm.clients import create_client
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.close_races import ReleaseGate, assert_cancel_during_rollback

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.llm_http_clients import HttpClientLedger

_SDK_CLASSES: dict[str, type[Any]] = {"openai": openai.AsyncOpenAI, "anthropic": anthropic.AsyncAnthropic}
_CUSTOM_HEADERS_ENVS = {"openai": "OPENAI_CUSTOM_HEADERS", "anthropic": "ANTHROPIC_CUSTOM_HEADERS"}


def _profile(provider: str) -> ModelProfile:
    return ModelProfile(id="p", name="p", provider=provider, model_id="test-model", api_key="sk-test")


def _fail_sdk_construction(provider: str, monkeypatch: pytest.MonkeyPatch) -> BaseException:
    error = RuntimeError("sdk construction failed")
    sdk_class = _SDK_CLASSES[provider]
    monkeypatch.setattr(sdk_class, "__init__", create_autospec(sdk_class.__init__, side_effect=error))
    return error


def _fail_sdk_headers(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_CUSTOM_HEADERS_ENVS[provider], "X-Sdk-Header: value▼")


def _fail_stack_assembly(monkeypatch: pytest.MonkeyPatch) -> BaseException:
    error = RuntimeError("stack assembly failed")
    monkeypatch.setattr(
        clients_module, "_assemble_stack", create_autospec(clients_module._assemble_stack, side_effect=error)
    )
    return error


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("position", ["sdk_construction", "sdk_headers", "stack_assembly"])
async def test_factory_failure_closes_what_it_created(
    provider: str, position: str, monkeypatch: pytest.MonkeyPatch, http_client_ledger: HttpClientLedger
) -> None:
    expected: BaseException | None = None
    if position == "sdk_construction":
        expected = _fail_sdk_construction(provider, monkeypatch)
    elif position == "sdk_headers":
        _fail_sdk_headers(provider, monkeypatch)
    else:
        expected = _fail_stack_assembly(monkeypatch)

    with pytest.raises(Exception) as info:
        await create_client(_profile(provider))

    if expected is None:
        assert type(info.value) is ValueError
        assert "provider SDK headers" in str(info.value)
    else:
        assert info.value is expected
    assert len(http_client_ledger.clients) == 1
    assert http_client_ledger.open() == []


async def test_rollback_completes_when_the_caller_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, http_client_ledger: HttpClientLedger
) -> None:
    _fail_stack_assembly(monkeypatch)
    release = ReleaseGate()
    build = clients_module._build_profile_http_client

    def gated_build(
        profile: ModelProfile,
        timeout: Any,
        *,
        raw_http_log_path: Path | None = None,
        session_id: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client = build(profile, timeout, raw_http_log_path=raw_http_log_path, session_id=session_id, auth=auth)
        close = client.aclose

        async def gated_close() -> None:
            await release()
            await close()

        client.aclose = gated_close  # type: ignore[method-assign]
        return client

    monkeypatch.setattr(clients_module, "_build_profile_http_client", gated_build)

    creating = asyncio.create_task(create_client(_profile("openai")))
    await assert_cancel_during_rollback(creating, release)

    assert len(http_client_ledger.clients) == 1
    assert http_client_ledger.open() == []
