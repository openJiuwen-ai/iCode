# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Record every HTTP client ``create_client`` builds, without replacing any.

Every provider SDK client is built over the pool that
``chrys.service.llm.clients._build_profile_http_client`` returns, so the ledger
sees each LLM connection pool Chrys opens and can tell which are still open.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from chrys.service.profiles.models.schema import ModelProfile


@dataclass(frozen=True, slots=True)
class HttpClientBuild:
    """One built pool and the model profile and route session it was built for."""

    client: httpx.AsyncClient
    profile_id: str
    session_id: str | None


@dataclass(slots=True)
class HttpClientLedger:
    """The HTTP clients built so far, in build order."""

    builds: list[HttpClientBuild] = field(default_factory=list)

    @property
    def clients(self) -> list[httpx.AsyncClient]:
        return [build.client for build in self.builds]

    def open(self) -> list[httpx.AsyncClient]:
        return [client for client in self.clients if not client.is_closed]

    def for_profile(self, profile_id: str) -> list[httpx.AsyncClient]:
        return [build.client for build in self.builds if build.profile_id == profile_id]


@pytest.fixture
async def http_client_ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[HttpClientLedger]:
    """Wrap the profile HTTP client builder; teardown closes whatever a test left open."""
    import chrys.service.llm.clients as clients_module

    ledger = HttpClientLedger()
    build = clients_module._build_profile_http_client

    def recording_build(
        profile: ModelProfile,
        timeout: Any,
        *,
        raw_http_log_path: Path | None = None,
        session_id: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client = build(profile, timeout, raw_http_log_path=raw_http_log_path, session_id=session_id, auth=auth)
        ledger.builds.append(HttpClientBuild(client=client, profile_id=profile.id, session_id=session_id))
        return client

    monkeypatch.setattr(clients_module, "_build_profile_http_client", recording_build)
    try:
        yield ledger
    finally:
        for client in ledger.open():
            await client.aclose()
