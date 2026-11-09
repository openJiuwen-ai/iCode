# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the model catalog sync (migrated from the 0.28 deployment).

The sync itself is exercised against a stubbed fetch: what these cover is the
clock, the generation guard, the two-phase publish, the absence of retired-name
warnings and the in-memory replacement a sync triggers — the parts that decide whether
a server-side change reaches a running session (aixcoding/docs/batches/
catalog-sync/test-plan.md).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aixcoding.tests.catalog.conftest import wait_until

from chrys.kernel.aixcoding.auth import AccountInfo
from chrys.kernel.aixcoding.diagnostics.logging_support import DETAIL_LEVEL
from chrys.service.aixcoding.models import catalog as catalog_module
from chrys.service.aixcoding.models.catalog import (
    CatalogSyncResult,
    ModelCatalog,
    ModelCatalogError,
    sync_catalog_blocking,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile

_POLL_INTERVAL = 0.01


def _profile(profile_id: str) -> ModelProfile:
    return ModelProfile(id=profile_id, name=profile_id, provider="openai", model_id=f"{profile_id}-m")


def _catalog(*profile_ids: str) -> ModelCatalog:
    return ModelCatalog(items=tuple(_profile(pid) for pid in profile_ids), version="v1")


def _result(*profile_ids: str) -> CatalogSyncResult:
    return CatalogSyncResult(
        replaced=True,
        version="v1",
        profile_ids=profile_ids,
        default_profile_id=profile_ids[0] if profile_ids else "",
    )


def _wire_sync(
    monkeypatch: pytest.MonkeyPatch,
    *profile_ids: str,
    interval: float = _POLL_INTERVAL,
) -> ModelProfileRegistry:
    """Stub source/credential/clock/fetch/apply and return a real registry.

    The refresh callback is what observes applied results, mirroring the
    ``on_applied`` these tests had on 0.28; the registry is real so the
    snapshot install path runs as it does in production.
    """
    registry = ModelProfileRegistry()
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "has_catalog_credential", lambda: True)
    monkeypatch.setattr(catalog_module, "sync_interval_seconds", lambda: interval)
    catalog = _catalog(*profile_ids)

    async def _fetch(url: str | None = None, *, timeout: float = 3.0) -> ModelCatalog:
        return catalog

    monkeypatch.setattr(catalog_module, "fetch_catalog", _fetch)
    monkeypatch.setattr(catalog_module, "apply_catalog", lambda c, **_kwargs: _result(*c.profile_ids()))
    return registry


@pytest.fixture(autouse=True)
def _credentialed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assume a login: these tests are about the clock, not the credential gate."""
    monkeypatch.setattr(catalog_module, "has_catalog_credential", lambda: True)


@pytest.fixture(autouse=True)
def _aix_logger_isolation() -> Iterator[None]:
    """Pin the ``aixcoding`` logger tree so caplog sees the records (auth F5 pattern).

    caplog's handler sits on the root logger, so records must propagate out
    of the ``aixcoding`` subtree; other test modules may have left
    ``propagate=False`` (or a file sink) behind. Save/restore all of it.
    """
    root = logging.getLogger("aixcoding")
    saved_handlers = root.handlers[:]
    saved_propagate = root.propagate
    saved_level = root.level
    root.handlers.clear()
    root.propagate = True
    yield
    root.handlers[:] = saved_handlers
    root.propagate = saved_propagate
    root.setLevel(saved_level)


@pytest.fixture
def detail_capture() -> Iterator[list[logging.LogRecord]]:
    """Capture the file-only detail subtree (ADR-0017), which caplog cannot see.

    Pin the subtree the way ``setup_logging`` would and hook a capture
    handler at its root (mirrors the auth detail-logging tests).
    """
    detail = logging.getLogger("aixcoding.detail")
    saved = (detail.handlers[:], detail.propagate, detail.level)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture()
    capture.setLevel(logging.NOTSET)
    logging.addLevelName(DETAIL_LEVEL, "DETAIL")  # normally done by setup_logging
    detail.setLevel(DETAIL_LEVEL)
    detail.propagate = False  # mirror the production preset
    detail.addHandler(capture)
    yield records
    detail.handlers[:] = saved[0]
    detail.propagate, detail.level = saved[1], saved[2]


# ── Migrated: the poll runtime ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_syncs_on_the_clock_and_stops_when_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """One fetch per interval, and stopping the runtime ends the loop."""
    registry = _wire_sync(monkeypatch, "a")
    seen: list[CatalogSyncResult] = []

    assert catalog_module.start_sync_runtime(registry, seen.append), "a configured source must start the loop"
    try:
        assert await wait_until(lambda: len(seen) >= 2), f"expected repeated syncs, got {len(seen)}"
        assert seen[0].profile_ids == ("a",)
        assert registry.list_ids() == ["a"]
    finally:
        catalog_module.stop_sync_runtime()

    await asyncio.sleep(5 * _POLL_INTERVAL)
    settled = len(seen)
    await asyncio.sleep(20 * _POLL_INTERVAL)
    assert len(seen) == settled, "a stopped loop must not keep fetching"


def test_no_credential_means_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated, every sync would be refused: no thread, no traffic."""

    async def _must_not_fetch(url: object = None, *, timeout: float = 0) -> ModelCatalog:
        raise AssertionError("a sync without a credential must not be attempted")

    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "has_catalog_credential", lambda: False)
    monkeypatch.setattr(catalog_module, "fetch_catalog", _must_not_fetch)
    assert catalog_module.start_sync_runtime(ModelProfileRegistry(), lambda _result: None) is False


def test_no_credential_startup_log_is_chinese(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """方案七: the outer swallow and inner credential error are both Chinese."""
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "_resolve_catalog_credential", lambda: ("", ""))

    with caplog.at_level(logging.WARNING, logger="aixcoding.catalog"):
        assert catalog_module.sync_catalog_blocking() is None

    records = [r for r in detail_capture if r.name == "aixcoding.detail.catalog"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "同步模型目录失败，保留旧模型列表：没有模型目录凭证" in message
    assert catalog_module.CATALOG_TOKEN_ENV in message
    assert not [r for r in caplog.records if r.name == "aixcoding.catalog"]


@pytest.mark.asyncio
async def test_immediate_syncs_before_the_first_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login starts the loop mid-session: the list arrives now, not in a minute."""
    registry = _wire_sync(monkeypatch, "a", interval=60)
    seen: list[CatalogSyncResult] = []

    assert catalog_module.start_sync_runtime(registry, seen.append, immediate=True), (
        "a credentialed source must start the loop"
    )
    try:
        assert await wait_until(lambda: len(seen) >= 1, timeout=2.0), "immediate never synced"
        assert len(seen) == 1, "the first wait is a whole interval, so a second sync is too early"
    finally:
        catalog_module.stop_sync_runtime()


@pytest.mark.asyncio
async def test_sync_now_pulls_one_catalog_ahead_of_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login wakes a loop that is already running: the list arrives now, not at the next tick."""
    registry = _wire_sync(monkeypatch, "a", interval=60)
    seen: list[CatalogSyncResult] = []

    assert catalog_module.start_sync_runtime(registry, seen.append), "a credentialed source must start the loop"
    try:
        # The interval is a minute: on the clock alone nothing arrives in time.
        await asyncio.sleep(5 * _POLL_INTERVAL)
        assert not seen, "an untouched interval must not sync early"
        catalog_module.sync_now()
        assert await wait_until(lambda: len(seen) >= 1, timeout=2.0), "sync_now never synced"
        assert len(seen) == 1, "one request is one sync, not a loop restart"
    finally:
        catalog_module.stop_sync_runtime()


def test_the_interval_is_the_environments_or_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variable shortens the wait for debugging; a bad value must not."""
    monkeypatch.delenv(catalog_module.SYNC_INTERVAL_ENV, raising=False)
    assert catalog_module.sync_interval_seconds() == catalog_module.SYNC_INTERVAL_SECONDS == 15 * 60

    monkeypatch.setenv(catalog_module.SYNC_INTERVAL_ENV, "30")
    assert catalog_module.sync_interval_seconds() == 30.0

    for unusable in ("soon", "", "   ", "0", "-1"):
        monkeypatch.setenv(catalog_module.SYNC_INTERVAL_ENV, unusable)
        assert catalog_module.sync_interval_seconds() == catalog_module.SYNC_INTERVAL_SECONDS, (
            f"{unusable!r} must fall back to the default, not stop or hot-loop the poll"
        )


@pytest.mark.asyncio
async def test_the_loop_polls_at_the_environments_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The override reaches the loop: the interval is resolved at start, not import."""
    registry = ModelProfileRegistry()
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "fetch_catalog", _make_fetch("a"))
    monkeypatch.setattr(catalog_module, "apply_catalog", lambda c, **_kwargs: _result(*c.profile_ids()))
    monkeypatch.setenv(catalog_module.SYNC_INTERVAL_ENV, str(_POLL_INTERVAL))

    seen: list[CatalogSyncResult] = []
    assert catalog_module.start_sync_runtime(registry, seen.append), "a configured source must start the loop"
    try:
        assert await wait_until(lambda: len(seen) >= 2), f"the override never took effect: {len(seen)} sync(s)"
    finally:
        catalog_module.stop_sync_runtime()


def test_without_a_source_there_is_nothing_to_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """No catalog configured means no thread — the directory stays the user's."""
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "")
    assert catalog_module.start_sync_runtime(ModelProfileRegistry(), lambda _result: None) is False


@pytest.mark.asyncio
async def test_a_failing_callback_does_not_kill_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The callback refreshes a registry; its failure must not end the clock."""
    registry = _wire_sync(monkeypatch, "a")
    calls: list[int] = []

    def _boom_once(result: CatalogSyncResult) -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("callback blew up")

    assert catalog_module.start_sync_runtime(registry, _boom_once)
    try:
        assert await wait_until(lambda: len(calls) >= 2), f"loop died after {len(calls)} call(s)"
    finally:
        catalog_module.stop_sync_runtime()


def test_the_tier_is_auths_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """One shared deployment environment drives auth and the model catalog."""
    from chrys.kernel.aixcoding.auth import environments
    from chrys.kernel.aixcoding.auth.types import Environment
    from chrys.service.aixcoding.config import load_config
    from chrys.service.aixcoding.config import loader as config_loader

    expected_tiers = {tier.value for tier in Environment}
    assert set(catalog_module._CATALOG_BASES) == set(config_loader.VALID_ENVIRONMENTS) == expected_tiers
    assert Environment.PROD.value == catalog_module.DEFAULT_ENVIRONMENT

    for tier in Environment:
        monkeypatch.setenv(environments.ENVIRONMENT_VARIABLE, tier.value)
        load_config.cache_clear()
        assert catalog_module.catalog_tier() == tier.value
        assert catalog_module.catalog_base_url() == catalog_module._CATALOG_BASES[tier.value]

    monkeypatch.setenv(environments.ENVIRONMENT_VARIABLE, "staging")
    load_config.cache_clear()
    assert catalog_module.catalog_base_url() == catalog_module._CATALOG_BASES["prod"]


@pytest.mark.parametrize("base_url", ["http://configured.example", "http://configured.example/"])
def test_toml_base_url_appends_catalog_path(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    snapshot = SimpleNamespace(environment="dev", model_catalog=SimpleNamespace(base_url=base_url))
    monkeypatch.setattr(catalog_module, "load_config", lambda: snapshot)
    assert catalog_module.catalog_base_url() == base_url
    assert catalog_module.catalog_url() == f"http://configured.example{catalog_module.CATALOG_PATH}"


def test_full_url_env_has_highest_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = SimpleNamespace(environment="dev", model_catalog=SimpleNamespace(base_url="http://configured.example"))
    monkeypatch.setattr(catalog_module, "load_config", lambda: snapshot)
    monkeypatch.setenv(catalog_module.CATALOG_URL_ENV, "http://full.example/complete")
    assert catalog_module.catalog_url() == "http://full.example/complete"


def test_old_settings_base_url_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_settings_read():
        raise AssertionError("the model catalog must not read upstream process settings")

    snapshot = SimpleNamespace(environment="prod", model_catalog=SimpleNamespace(base_url=""))
    monkeypatch.setattr(catalog_module, "load_config", lambda: snapshot)
    monkeypatch.setattr("chrys.foundation.config.process_settings.process_settings", forbidden_settings_read)
    assert catalog_module.catalog_base_url() == "http://82.187.34.98/csas"


def test_install_snapshot_forgets_deleted_profiles(tmp_path: Path) -> None:
    """A catalog sync is a wholesale replacement, so the registry must drop too.

    The snapshot install must not re-read the directory (0.28's
    ``replace_profiles`` reloaded files under the lock): the catalog already
    holds the same items it just wrote to disk, so installing them keeps both
    sides on one version even when the disk still holds the deleted file.
    """
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")
    (directory / "b.yaml").write_text("name: B\n", encoding="utf-8")

    registry = ModelProfileRegistry()
    assert registry.load_profiles(directory) == 2

    # The deleted profile's file is still on disk: a snapshot install must not
    # rescan it back into existence.
    assert registry.install_snapshot((_profile("a"),)) == 1
    assert sorted(registry.list_ids()) == ["a"]


# ── New: the two-phase publish and its fault injection ────────────────────


def _make_fetch(*profile_ids: str):
    catalog = _catalog(*profile_ids)

    async def _fetch(url: str | None = None, *, timeout: float = 3.0) -> ModelCatalog:
        return catalog

    return _fetch


def _wire_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the catalog at a throwaway directory."""
    directory = tmp_path / "models"
    monkeypatch.setattr(catalog_module, "catalog_directory", lambda: directory)
    monkeypatch.setattr(catalog_module, "catalog_state_path", lambda: directory / catalog_module.STATE_FILENAME)
    return directory


def test_sync_chain_applies_catalog_and_installs_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One blocking sync writes the whole set, the state file, and nothing else."""
    directory = _wire_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "fetch_catalog", _make_fetch("a", "b"))

    result = sync_catalog_blocking()
    assert result is not None and result.profile_ids == ("a", "b")
    assert sorted(path.name for path in directory.iterdir()) == [".catalog-state.json", "a.yaml", "b.yaml"]
    state = json.loads((directory / ".catalog-state.json").read_text(encoding="utf-8"))
    assert state["profile_ids"] == ["a", "b"]
    assert state["version"] == "v1"
    assert not list(directory.glob(f"*{catalog_module.PROFILE_TMP_SUFFIX}")), "staging files must not survive"


def test_publish_fault_injection_stage_one_write_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A staging write failing has not touched the previous catalog at all."""
    directory = _wire_disk(monkeypatch, tmp_path)
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")

    real_write = catalog_module._write_profile
    written: list[str] = []

    def _fail_on_second_write(path: Path, profile: ModelProfile) -> None:
        written.append(path.name)
        if len(written) == 2:
            raise OSError("disk full")
        real_write(path, profile)

    monkeypatch.setattr(catalog_module, "_write_profile", _fail_on_second_write)

    with pytest.raises(ModelCatalogError):
        catalog_module.apply_catalog(_catalog("b", "c"))
    assert written == [f"b{catalog_module.PROFILE_TMP_SUFFIX}", f"c{catalog_module.PROFILE_TMP_SUFFIX}"], (
        "staging uses the exact designed name; the first write landed, the second never did"
    )
    assert (directory / "a.yaml").read_text(encoding="utf-8") == "name: A\n"
    assert not list(directory.glob(f"*{catalog_module.PROFILE_TMP_SUFFIX}")), "failed staging must be cleaned up"
    assert sorted(path.name for path in directory.iterdir()) == ["a.yaml"]


def test_publish_fault_injection_mkdir_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """A directory-creation failure is converted, never leaked to the startup path."""

    _wire_disk(monkeypatch, tmp_path)

    def _fail_mkdir(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(catalog_module.Path, "mkdir", _fail_mkdir)
    with pytest.raises(ModelCatalogError):
        catalog_module.apply_catalog(_catalog("x"))

    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "fetch_catalog", _make_fetch("x"))
    with caplog.at_level(logging.WARNING, logger="aixcoding.catalog"):
        assert sync_catalog_blocking() is None
    assert "同步模型目录失败" not in caplog.text, "方案六: technical records stay off the main volume"
    assert any("同步模型目录失败" in r.getMessage() for r in detail_capture)


def test_infinite_context_length_is_absorbed() -> None:
    """JSON 1e999 parses to float('inf'); the overflow must not escape parsing."""
    assert catalog_module._positive_int(float("inf")) == 0
    catalog = catalog_module.parse_catalog(
        {"models": [{"title": "T", "model": "openai/gpt", "contextLength": float("inf")}]}
    )
    assert catalog.items, "an overflowing number is one unusable field, not a failed catalog"


def test_boolean_context_length_is_not_a_number() -> None:
    """JSON true is a misplaced flag, not a context length of 1."""
    from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS

    assert catalog_module._positive_int(True) == 0
    catalog = catalog_module.parse_catalog({"models": [{"title": "T", "model": "openai/gpt", "contextLength": True}]})
    assert catalog.items
    assert catalog.items[0].max_context_tokens == DEFAULT_MAX_CONTEXT_TOKENS


def test_unusable_payload_field_types_are_converted() -> None:
    """Raw payload types (non-str id, unhashable provider) arrive converted, not as TypeError."""
    with pytest.raises(ModelCatalogError):
        catalog_module.parse_catalog({"items": [{"id": 123, "name": "A", "provider": "openai", "model_id": "m"}]})
    with pytest.raises(ModelCatalogError):
        catalog_module.parse_catalog({"items": [{"id": "a", "name": "A", "provider": [], "model_id": "m"}]})


def test_non_string_name_and_model_id_are_refused_before_publishing() -> None:
    """A non-str name would load on the next start and break registry ordering."""
    with pytest.raises(ModelCatalogError):
        catalog_module.parse_catalog({"items": [{"id": "a", "name": ["bad"], "provider": "openai", "model_id": "m"}]})
    with pytest.raises(ModelCatalogError):
        catalog_module.parse_catalog({"items": [{"id": "a", "name": "A", "provider": "openai", "model_id": 5}]})


@pytest.mark.asyncio
async def test_poll_parse_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, detail_capture: list[logging.LogRecord]
) -> None:
    """A payload the parser rejects stays visible from the polling path.

    The parse call lives inside fetch_catalog, below its logged fetch block;
    without a line of its own, the poll loop's "already logged" early return
    would swallow it silently on the clock.
    """

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"items": []}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, endpoint: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            return _FakeResponse()

    def _reject(payload: object) -> ModelCatalog:
        raise ModelCatalogError("injected parse failure")

    registry = ModelProfileRegistry()
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "_resolve_catalog_credential", lambda: ("t", "store"))
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(catalog_module, "parse_catalog", _reject)

    with caplog.at_level(logging.WARNING, logger="aixcoding.catalog"):
        assert catalog_module.start_sync_runtime(registry, lambda: None, immediate=True)
        try:
            assert await wait_until(lambda: any("不可用" in r.getMessage() for r in detail_capture)), (
                "expected the parse failure on the detail volume (方案六)"
            )
            assert "不可用" not in caplog.text, "the poll's technical records stay off the main volume"
        finally:
            catalog_module.stop_sync_runtime()


def test_publish_fault_injection_replace_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A mid-phase-two failure loses no file: old or new, every id survives.

    The next sync converges the directory wholesale — that is the recovery
    story, so it is asserted here rather than left to prose.
    """
    import os

    directory = _wire_disk(monkeypatch, tmp_path)
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")

    real_replace = os.replace
    calls: list[str] = []

    def _fail_first_replace(src: object, dst: object) -> None:
        calls.append(str(dst))
        if len(calls) == 1:
            raise OSError("replace interrupted")
        real_replace(src, dst)

    monkeypatch.setattr(catalog_module.os, "replace", _fail_first_replace)

    with pytest.raises(ModelCatalogError):
        catalog_module.apply_catalog(_catalog("a", "b"))
    # Whatever happened, the previous file was not lost.
    assert (directory / "a.yaml").exists()

    monkeypatch.setattr(catalog_module.os, "replace", real_replace)
    catalog_module.apply_catalog(_catalog("a", "b"))
    assert sorted(path.name for path in directory.glob("*.yaml")) == ["a.yaml", "b.yaml"]
    assert not list(directory.glob(f"*{catalog_module.PROFILE_TMP_SUFFIX}"))


def test_publish_fault_injection_state_write_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The yaml set may already be new while the state write fails: a declared window."""
    directory = _wire_disk(monkeypatch, tmp_path)
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")

    def _fail_state(state: object) -> None:
        raise OSError("state write failed")

    monkeypatch.setattr(catalog_module, "write_catalog_state", _fail_state)

    with pytest.raises(ModelCatalogError):
        catalog_module.apply_catalog(_catalog("a", "b"))
    # Directory is new, the state file is not — the next sync rewrites it.
    assert sorted(path.name for path in directory.glob("*.yaml")) == ["a.yaml", "b.yaml"]
    assert not (directory / ".catalog-state.json").exists()


def test_publish_fault_injection_stale_delete_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file the catalog no longer contains may survive a failed delete: idempotent cleanup."""
    from pathlib import Path as _Path

    directory = _wire_disk(monkeypatch, tmp_path)
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")
    (directory / "stale.yaml").write_text("name: Stale\n", encoding="utf-8")

    real_unlink = _Path.unlink

    def _fail_stale_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == "stale.yaml":
            raise OSError("delete interrupted")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(_Path, "unlink", _fail_stale_unlink)

    with pytest.raises(ModelCatalogError):
        catalog_module.apply_catalog(_catalog("a"))
    assert (directory / "a.yaml").exists(), "the new set must be in place"
    assert (directory / "stale.yaml").exists(), "the failed delete leaves the file behind this turn"

    monkeypatch.setattr(_Path, "unlink", real_unlink)
    catalog_module.apply_catalog(_catalog("a"))
    assert not (directory / "stale.yaml").exists(), "the next sync retries the cleanup"


def test_publish_fault_injection_fetch_and_parse_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fetch and validation failures keep the previous catalog exactly as it was."""
    directory = _wire_disk(monkeypatch, tmp_path)
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")

    async def _fetch_fails(url: str | None = None, *, timeout: float = 3.0) -> ModelCatalog:
        raise ModelCatalogError("request failed")

    monkeypatch.setattr(catalog_module, "fetch_catalog", _fetch_fails)
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    assert sync_catalog_blocking() is None
    assert sorted(path.name for path in directory.iterdir()) == ["a.yaml"]

    for bad_payload in (None, "not-a-list", [], {"items": "not-a-list"}):
        with pytest.raises(ModelCatalogError):
            catalog_module.parse_catalog(bad_payload)


def test_startup_degrades_on_any_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The startup path (B2) swallows every failure shape: no exception escapes."""
    _wire_disk(monkeypatch, tmp_path)

    async def _fetch_fails(url: str | None = None, *, timeout: float = 3.0) -> ModelCatalog:
        raise ModelCatalogError("request failed")

    monkeypatch.setattr(catalog_module, "fetch_catalog", _fetch_fails)
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    assert sync_catalog_blocking() is None  # logged, swallowed, previous list kept


# ── New: retired env names ────────────────────────────────────────────────


def test_startup_degrades_on_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, detail_capture: list[logging.LogRecord]
) -> None:
    """The startup swallow is not ModelCatalogError-shaped only (PR #85 review C1/C2)."""

    def _explode() -> str:
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(catalog_module, "catalog_url", _explode)
    with caplog.at_level(logging.ERROR, logger="aixcoding.catalog"):
        assert sync_catalog_blocking() is None
    assert "同步模型目录时发生意外错误" not in caplog.text
    assert any("同步模型目录时发生意外错误" in r.getMessage() for r in detail_capture)


@pytest.mark.asyncio
async def test_poll_survives_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected error in one round kills neither the thread nor the schedule."""
    calls = 0

    async def _fetch_once_then_fail(url: str | None = None, *, timeout: float = 3.0) -> ModelCatalog:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transport exploded")
        return _catalog("a")

    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "sync_interval_seconds", lambda: _POLL_INTERVAL)
    monkeypatch.setattr(catalog_module, "fetch_catalog", _fetch_once_then_fail)
    monkeypatch.setattr(catalog_module, "apply_catalog", lambda c, **_kwargs: _result(*c.profile_ids()))

    seen: list[CatalogSyncResult] = []
    registry = ModelProfileRegistry()
    assert catalog_module.start_sync_runtime(registry, seen.append, immediate=True)
    try:
        assert await wait_until(lambda: len(seen) >= 1), (
            f"the loop must survive the first round's error and fetch again (calls={calls})"
        )
        assert registry.list_ids() == ["a"]
    finally:
        catalog_module.stop_sync_runtime()


@pytest.mark.parametrize(
    "retired_name",
    [
        "CHRYS_MODEL_CATALOG_BASE_URL",
        "CHRYS_MODEL_CATALOG_URL",
        "CHRYS_MODEL_CATALOG_TOKEN",
        "CHRYS_MODEL_CATALOG_SYNC_INTERVAL",
        "CHRYS_REMOTE_MODEL_CONFIG_URL",
    ],
)
def test_retired_environment_names_have_no_effect_or_warning(
    retired_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """Retired names are not read, detected, aliased or warned about."""
    snapshot = SimpleNamespace(environment="prod", model_catalog=SimpleNamespace(base_url=""))
    monkeypatch.setattr(catalog_module, "load_config", lambda: snapshot)
    monkeypatch.setattr(
        "chrys.kernel.aixcoding.auth.get_login_session",
        lambda: SimpleNamespace(stored_token=""),
    )
    for env in (catalog_module.CATALOG_URL_ENV, catalog_module.CATALOG_TOKEN_ENV, catalog_module.SYNC_INTERVAL_ENV):
        monkeypatch.delenv(env, raising=False)
    base = catalog_module.catalog_url()

    with caplog.at_level(logging.WARNING):
        monkeypatch.setenv(retired_name, f"retired-value-for-{retired_name}")
        assert catalog_module.catalog_url() == base
        assert catalog_module.sync_interval_seconds() == 15 * 60
        assert catalog_module._catalog_token() == ""

    messages = "\n".join(record.getMessage() for record in [*caplog.records, *detail_capture])
    assert retired_name not in messages
    assert f"retired-value-for-{retired_name}" not in messages


def test_new_env_names_take_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """When old and new names coexist, the AIXCODING_ names win."""
    monkeypatch.setenv("CHRYS_MODEL_CATALOG_URL", "http://retired.example/old")
    monkeypatch.setenv(catalog_module.CATALOG_URL_ENV, "http://configured.example/new")
    monkeypatch.setenv("CHRYS_MODEL_CATALOG_SYNC_INTERVAL", "1")
    monkeypatch.setenv(catalog_module.SYNC_INTERVAL_ENV, "30")
    monkeypatch.setenv("CHRYS_MODEL_CATALOG_TOKEN", "retired-secret")
    monkeypatch.setattr(
        "chrys.kernel.aixcoding.auth.get_login_session",
        lambda: SimpleNamespace(stored_token="stored-login-token"),
    )

    assert catalog_module.catalog_url() == "http://configured.example/new"
    assert catalog_module.sync_interval_seconds() == 30.0
    monkeypatch.delenv(catalog_module.CATALOG_TOKEN_ENV, raising=False)
    assert catalog_module._catalog_token() == "stored-login-token", (
        "the retired token name must lose to the stored login credential"
    )
    monkeypatch.setenv(catalog_module.CATALOG_TOKEN_ENV, "new-token")
    assert catalog_module._catalog_token() == "new-token"


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_non_finite_intervals_fall_back(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """NaN makes Condition.wait return at once (hot loop) and inf overflows it; both fall back."""
    monkeypatch.setenv(catalog_module.SYNC_INTERVAL_ENV, raw)
    assert catalog_module.sync_interval_seconds() == 15 * 60


# ── New: generation guarding ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_generation_result_is_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A logout mid-fetch must not let the logged-out account's catalog publish."""
    directory = _wire_disk(monkeypatch, tmp_path)
    # threading.Event, not asyncio: the fetch runs on the sync thread's own
    # asyncio.run loop, so an asyncio.Event created here would not travel.
    fetch_started = threading.Event()
    fetch_release = threading.Event()

    async def _slow_fetch(url: str | None = None, *, timeout: float = 3.0) -> ModelCatalog:
        fetch_started.set()
        # ASYNC110 does not apply: a threading.Event cannot be awaited, and an
        # asyncio.Event would not cross the sync thread's own loop.
        while not fetch_release.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return _catalog("a")

    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "sync_interval_seconds", lambda: 60)
    monkeypatch.setattr(catalog_module, "fetch_catalog", _slow_fetch)

    seen: list[CatalogSyncResult] = []
    registry = ModelProfileRegistry()
    assert catalog_module.start_sync_runtime(registry, seen.append, immediate=True)
    try:
        assert await wait_until(fetch_started.is_set, timeout=2.0), "the in-flight fetch never started"
        catalog_module.stop_sync_runtime()
        fetch_release.set()
        await asyncio.sleep(20 * _POLL_INTERVAL)
        assert not seen, "a result from before the stop must not publish"
        if directory.exists():
            assert not list(directory.iterdir()), "nothing should reach the disk"
    finally:
        catalog_module.stop_sync_runtime()
        fetch_release.set()


@pytest.mark.asyncio
async def test_stale_generation_result_after_apply_is_dropped_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """A stop that lands mid-apply drops the result before the registry sees it.

    The disk writes already happened (idempotent; the next sync converges),
    but the old generation's catalog must not be installed into the registry
    or reach the UI callback of a runtime that moved on (a logout, an
    account switch) — the second, post-apply generation check.
    """
    apply_started = threading.Event()
    apply_release = threading.Event()

    def _slow_apply(catalog: ModelCatalog, **_kwargs: object) -> CatalogSyncResult:
        apply_started.set()
        assert apply_release.wait(timeout=10), "the test never released the apply"
        return _result(*catalog.profile_ids())

    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "_resolve_catalog_credential", lambda: ("t", "store"))
    monkeypatch.setattr(catalog_module, "sync_interval_seconds", lambda: 60)
    monkeypatch.setattr(catalog_module, "fetch_catalog", _make_fetch("a"))
    monkeypatch.setattr(catalog_module, "apply_catalog", _slow_apply)

    seen: list[CatalogSyncResult] = []
    registry = ModelProfileRegistry()
    with caplog.at_level(logging.INFO, logger="aixcoding.catalog"):
        assert catalog_module.start_sync_runtime(registry, seen.append, immediate=True)
        try:
            assert await wait_until(apply_started.is_set, timeout=2.0), "the first sync never reached apply"
            catalog_module.stop_sync_runtime()  # generation bump lands mid-apply
            apply_release.set()
            assert await wait_until(lambda: any("在发布前丢弃" in r.getMessage() for r in detail_capture)), (
                "the post-apply generation check must drop the stale result (detail volume, 方案六)"
            )
            assert not seen, "a result from before the stop must not reach the callback"
            assert registry.list_ids() == [], "nothing must be installed after the stop"
        finally:
            catalog_module.stop_sync_runtime()
            apply_release.set()


# ── New: loader semantics and login wiring ────────────────────────────────


def test_tmp_files_are_not_loaded_as_profiles(tmp_path: Path) -> None:
    """A half-published directory never feeds the registry (assumption 2, as a test)."""
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")
    (directory / "b.yaml.tmp").write_text("name: B\n", encoding="utf-8")

    registry = ModelProfileRegistry()
    assert registry.load_profiles(directory) == 1
    assert registry.list_ids() == ["a"]


def test_login_actions_wire_sync_calls() -> None:
    """The login/logout placeholders are real calls now (D3): source-level wiring anchor."""
    from chrys.app.aixcoding.tui import login_actions

    login_source = inspect.getsource(login_actions.open_login_dialog)
    assert "resume_sync_runtime" in login_source, "login success must re-arm the poll"
    logout_source = inspect.getsource(login_actions.perform_logout)
    assert "stop_sync_runtime" in logout_source, "logout must invalidate the poll"


def test_perform_logout_stops_the_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioural anchor: logout stops the poll even with the runtime never started."""
    from chrys.app.aixcoding.tui import login_actions
    from chrys.service.aixcoding.models import catalog

    stopped: list[bool] = []
    monkeypatch.setattr(catalog, "stop_sync_runtime", lambda: stopped.append(True))

    class _Session:
        delegated_credential = None
        stored_token = "token"

        def logout(self) -> None:
            pass

    class _Host:
        class _App:
            def push_screen(self, *_args: object, **_kwargs: object) -> None: ...

        app = _App()

        def notify(self, *_args: object, **_kwargs: object) -> None: ...

        def refresh_login_indicator(self, **_kwargs: object) -> None: ...

    monkeypatch.setattr(login_actions, "get_login_session", _Session)
    login_actions.perform_logout(_Host())
    assert stopped, "logout must stop the catalog poll"


def test_credential_rejected_callback_mirrors_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    """方案四: the rejected hook stops the poll, toasts and refreshes — via call_from_thread."""
    from chrys.app.aixcoding.tui import login_actions
    from chrys.service.aixcoding.models import catalog

    events: list[str] = []
    monkeypatch.setattr(catalog, "stop_sync_runtime", lambda: events.append("stop"))

    class _App:
        def call_from_thread(self, callback: Callable[[], None]) -> None:
            events.append("hop")
            callback()

    class _Screen:
        app = _App()

        def notify(self, message: str, *, severity: str = "information") -> None:
            events.append(f"notify:{message}:{severity}")

        def refresh_login_indicator(self, **_kwargs: object) -> None:
            events.append("refresh")

    login_actions._make_credential_rejected_callback(_Screen())()
    assert events[0] == "hop", "poll-thread UI work must hop through call_from_thread"
    assert "stop" in events, "the rejected teardown mirrors logout: no silent no-credential spinning"
    assert "refresh" in events
    assert any(e.startswith("notify:登录已失效") and e.endswith(":warning") for e in events)


def test_credential_rejected_callback_on_the_ui_thread_applies_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_from_thread raises RuntimeError on the UI thread: fall back to applying directly."""
    from chrys.app.aixcoding.tui import login_actions
    from chrys.service.aixcoding.models import catalog

    events: list[str] = []
    monkeypatch.setattr(catalog, "stop_sync_runtime", lambda: events.append("stop"))

    class _App:
        def call_from_thread(self, _callback: object) -> None:
            raise RuntimeError("call_from_thread invoked from the UI thread")

    class _Screen:
        app = _App()

        def notify(self, *_args: object, **_kwargs: object) -> None:
            events.append("notify")

        def refresh_login_indicator(self, **_kwargs: object) -> None:
            events.append("refresh")

    login_actions._make_credential_rejected_callback(_Screen())()
    assert events == ["stop", "notify", "refresh"]


def test_resume_sync_runtime_uses_remembered_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login re-arms the poll with the wiring startup left, without new wiring."""
    registry = ModelProfileRegistry()
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "has_catalog_credential", lambda: False)

    # Startup could not start (no credential) but remembered its wiring.
    assert catalog_module.start_sync_runtime(registry, lambda _result: None) is False
    assert catalog_module._last_wiring is not None

    # The login flips the credential: resume retries with the remembered wiring.
    monkeypatch.setattr(catalog_module, "has_catalog_credential", lambda: True)
    monkeypatch.setattr(catalog_module, "sync_interval_seconds", lambda: _POLL_INTERVAL)
    monkeypatch.setattr(catalog_module, "fetch_catalog", _make_fetch("a"))
    monkeypatch.setattr(catalog_module, "apply_catalog", lambda c, **_kwargs: _result(*c.profile_ids()))
    seen: list[CatalogSyncResult] = []
    assert catalog_module.resume_sync_runtime()
    try:
        assert await_synced(registry, seen), "the resumed runtime must install the remembered wiring"
    finally:
        catalog_module.stop_sync_runtime()


def await_synced(registry: ModelProfileRegistry, seen: list[CatalogSyncResult]) -> bool:
    """Synchronous helper: poll briefly for the resumed runtime's first result."""
    import time as _time

    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if seen or registry.list_ids():
            return True
        _time.sleep(0.01)
    return False


# ── New: log redaction, the detail channel and debug-level diagnostics ────


@pytest.mark.asyncio
async def test_request_log_redacts_user_id_and_detail_carries_the_full_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """One request, two records (ADR-0017, auth's split mirrored):

    the main logger gets the endpoint without its query string — even at
    DEBUG the redaction never rolls back — while the file-only detail
    subtree gets the full endpoint including userId, and never propagates.
    """

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"items": [{"id": "a", "name": "A", "provider": "openai", "model_id": "m"}]}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, endpoint: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")
    monkeypatch.setattr(catalog_module, "_resolve_catalog_credential", lambda: ("t", "store"))
    monkeypatch.setattr(catalog_module, "_catalog_user_id", lambda: "1234567")
    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _FakeClient)

    with caplog.at_level(logging.DEBUG, logger="aixcoding.catalog"):
        catalog = await catalog_module.fetch_catalog()
    assert catalog.items

    main_records = [r for r in caplog.records if r.name == "aixcoding.catalog"]
    assert not main_records, "方案六: no catalog records on the main volume, even at DEBUG"

    assert not [r for r in caplog.records if r.name.startswith("aixcoding.detail")], (
        "the detail subtree must never propagate to the root logger"
    )
    request_details = [r for r in detail_capture if "模型目录请求" in r.getMessage()]
    assert len(request_details) == 2, "redacted + full-endpoint request twins, both on detail"
    for record in request_details:
        assert record.name == "aixcoding.detail.catalog"
        assert record.levelname == "DETAIL"
        assert record.levelno == DETAIL_LEVEL == catalog_module._DETAIL_LEVEL
    detail_record = next(r for r in request_details if "userId" in r.getMessage())
    assert "userId=1234567" in detail_record.getMessage(), "the full endpoint stays on detail"


def test_unknown_fields_are_logged_only_at_debug(
    caplog: pytest.LogCaptureFixture, detail_capture: list[logging.LogRecord]
) -> None:
    """方案六: field-level diagnostics live on the detail volume only, never the main one."""
    payload = {"items": [{"id": "a", "name": "A", "provider": "openai", "model_id": "m", "futureKey": 1}]}

    with caplog.at_level(logging.DEBUG, logger="aixcoding.catalog"):
        catalog_module.parse_catalog(payload)
    assert not [r for r in caplog.records if r.name == "aixcoding.catalog"], "the main volume stays silent"
    drops = [r.getMessage() for r in detail_capture if "未知字段" in r.getMessage()]
    assert len(drops) == 1
    assert "futureKey" in drops[0]


# ── New: login pre-verification and the response detail (catalog-auth-gating) ──


def _wire_ok_httpx(monkeypatch: pytest.MonkeyPatch, payload: dict | None = None) -> list[dict]:
    """200 OK fake transport for the gating cases; returns captured requests."""
    calls: list[dict] = []

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload or {"items": [{"id": "a", "name": "A", "provider": "openai", "model_id": "m"}]}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def get(self, endpoint: str, headers: dict | None = None) -> _FakeResponse:
            calls.append({"endpoint": endpoint, "headers": headers or {}})
            return _FakeResponse()

    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _FakeClient)
    return calls


def _wire_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login whose verify returns None + a transport that fails the test if used."""
    session = SimpleNamespace(stored_token="t", verify_live_credential=AsyncMock(return_value=None))
    monkeypatch.setattr("chrys.kernel.aixcoding.auth.get_login_session", lambda: session)
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")

    class _ExplodingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("an unverified login must not issue a catalog request")

    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _ExplodingClient)


def _wire_verified(monkeypatch: pytest.MonkeyPatch, verify: AsyncMock) -> None:
    session = SimpleNamespace(stored_token="t", verify_live_credential=verify)
    monkeypatch.setattr("chrys.kernel.aixcoding.auth.get_login_session", lambda: session)
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")


async def test_verified_login_fetches_after_one_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    verify = AsyncMock(return_value=AccountInfo())
    _wire_verified(monkeypatch, verify)
    _wire_ok_httpx(monkeypatch)

    catalog = await catalog_module.fetch_catalog()
    assert catalog.items
    assert verify.await_count == 1, "a store credential is verified live exactly once per fetch"


def test_unverified_login_blocks_startup_with_two_warnings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """Startup shape: fetch-side + swallow records, both on the detail volume (方案六)."""
    _wire_unverified(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="aixcoding.catalog"):
        assert sync_catalog_blocking() is None
    records = [r for r in detail_capture if r.name == "aixcoding.detail.catalog"]
    assert len(records) == 2, f"expected pre-verification + swallow lines on detail, got {detail_capture!r}"
    assert "登录未通过验证" in records[0].getMessage()
    assert "同步模型目录失败" in records[1].getMessage()
    assert "登录未通过验证" not in caplog.text and "同步模型目录失败" not in caplog.text, (
        "the main volume carries neither record"
    )


async def test_unverified_login_poll_logs_a_single_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """Poll shape: only the fetch-side record on detail — _sync_once never adds its own."""
    _wire_unverified(monkeypatch)
    monkeypatch.setattr(catalog_module, "sync_interval_seconds", lambda: 60)
    registry = ModelProfileRegistry()
    with caplog.at_level(logging.WARNING, logger="aixcoding.catalog"):
        assert catalog_module.start_sync_runtime(registry, lambda: None, immediate=True)
        try:
            assert await wait_until(lambda: any("登录未通过验证" in r.getMessage() for r in detail_capture)), (
                "the poll must log the pre-verification skip on the detail volume (方案六)"
            )
        finally:
            catalog_module.stop_sync_runtime()
    skips = [r for r in detail_capture if "登录未通过验证" in r.getMessage()]
    assert len(skips) == 1, "the poll must not add a swallow line"
    assert not [r for r in detail_capture if "同步模型目录失败" in r.getMessage()], "no swallow line on detail either"
    assert "登录未通过验证" not in caplog.text, "the skip stays off the main volume"


async def test_env_token_skips_preverification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXCODING_MODEL_CATALOG_TOKEN", "env-token")
    verify = AsyncMock(return_value=None)  # would block the fetch if it were called
    _wire_verified(monkeypatch, verify)
    calls = _wire_ok_httpx(monkeypatch)

    catalog = await catalog_module.fetch_catalog()
    assert catalog.items
    assert verify.await_count == 0, "env credentials have no session to verify — the server judges them"
    assert calls[0]["headers"]["token"] == "env-token"


def test_catalog_token_is_a_thin_wrapper_over_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_module, "_resolve_catalog_credential", lambda: ("tok", "env"))
    assert catalog_module._catalog_token() == "tok"


async def test_unverified_real_session_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """D6 backstop: a real LoginSession, a rejected credential, no autouse double.

    The pre-verification must block the request AND clear the credential through
    the shared point, firing the rejected hook once (decision 乙 revised
    2026-11-04: the badge must not keep claiming "logged in").
    """
    from chrys.kernel.aixcoding.auth import Environment, LoginSession, StoredCredential
    from chrys.kernel.aixcoding.auth.crypto import MemoryBackend
    from chrys.kernel.aixcoding.auth.errors import AuthServerError

    session = LoginSession(
        environment=Environment.LOCAL,
        endpoints=("http://127.0.0.1:1/api", "http://127.0.0.1:1/api"),
        config_dir=tmp_path,
        backend=MemoryBackend(),
    )
    session.store.store(
        Environment.LOCAL,
        StoredCredential(environment_id="local", user_id="u", token="stale-token", ehr="1234567"),
    )

    class _FakeAuthClient:
        async def fetch_user_info(self, token: str) -> object:
            raise AuthServerError("用户不存在")

    session._make_client = _FakeAuthClient  # type: ignore[method-assign]
    monkeypatch.setattr("chrys.kernel.aixcoding.auth.get_login_session", lambda: session)
    monkeypatch.setattr(catalog_module, "catalog_url", lambda: "http://127.0.0.1:1/base")

    class _ExplodingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("an unverified login must not issue a catalog request")

    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _ExplodingClient)
    notifications: list[str] = []
    session.on_credential_rejected = lambda: notifications.append("fired")
    with pytest.raises(ModelCatalogError):
        await catalog_module.fetch_catalog()
    assert session.store.load(Environment.LOCAL) is None, "a rejected credential is cleared by the verify"
    assert session.stored_token is None
    assert notifications == ["fired"]


async def test_response_detail_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """The response detail (per-model fields) joins the request detail on the detail channel."""
    payload = {
        "version": "v9",
        "default_profile_id": "a",
        "items": [
            {"id": "a", "name": "Alpha", "provider": "openai", "model_id": "m-a", "base_url": "http://x/v1"},
            {"id": "b", "name": "Beta", "provider": "openai", "model_id": "m-b", "base_url": "http://y/v1"},
        ],
    }
    _wire_ok_httpx(monkeypatch, payload)
    with caplog.at_level(logging.INFO, logger="aixcoding.catalog"):
        await catalog_module.fetch_catalog()

    details = [r for r in detail_capture if "响应详情" in r.getMessage()]
    assert len(details) == 1
    record = details[0]
    assert record.name == "aixcoding.detail.catalog"
    assert record.levelname == "DETAIL"
    message = record.getMessage()
    assert "version=v9" in message
    assert "default_profile_id=a" in message
    for fragment in ("'id': 'a'", "'name': 'Alpha'", "'model_id': 'm-a'", "'base_url': 'http://x/v1'", "'id': 'b'"):
        assert fragment in message, f"missing {fragment} in {message}"
    assert "响应详情" not in caplog.text, "the detail stays off the main channel"


def test_reload_after_snapshot_keeps_catalog_entries(tmp_path: Path) -> None:
    """B2 (upstream-merge-v0.34.0): a reload right after a catalog snapshot converges.

    install_snapshot clears sources (catalog entries carry no disk-source identity
    yet), so reload_base keeps every catalog entry in the view (disk_ids is empty);
    the install_reload install must lose nothing and duplicate nothing.
    """
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")
    registry = ModelProfileRegistry()
    assert registry.load_profiles(directory) == 1

    assert registry.install_snapshot((_profile("a"), _profile("c"))) == 2
    view = registry.reload_base()
    registry.install_reload(view)
    assert sorted(registry.list_ids()) == ["a", "c"]


def test_reload_window_rollback_recovers_on_next_snapshot(tmp_path: Path) -> None:
    """B1 (upstream-merge-v0.34.0): the accepted reload window rolls back and self-heals.

    A catalog snapshot landing between reload_base and install_reload (with the disk
    scan already behind it) is overwritten by the stale view; the next periodic
    snapshot restore is the sanctioned recovery (design ruling 2026-11-06).
    """
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / "a.yaml").write_text("name: A\n", encoding="utf-8")
    registry = ModelProfileRegistry()
    assert registry.load_profiles(directory) == 1

    view = registry.reload_base()
    assert registry.install_snapshot((_profile("a"), _profile("c"))) == 2
    registry.install_reload(view)
    assert registry.list_ids() == [], "the stale view rolls the catalog push back"

    assert registry.install_snapshot((_profile("a"), _profile("c"))) == 2
    assert sorted(registry.list_ids()) == ["a", "c"], "the next sync heals the window"
