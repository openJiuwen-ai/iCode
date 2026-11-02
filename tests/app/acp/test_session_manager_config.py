# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP config-option get/set, settings scoping, and project-settings warnings."""

from __future__ import annotations

import dataclasses
import itertools
import os
import threading
from pathlib import Path

import pytest

from chrys.app.acp import session_manager as session_manager_module
from chrys.app.acp.session_manager import AcpSessionError, ManagedSession
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_ENV, get_model_pointer
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, PersistResult, load_settings, persist
from chrys.foundation.config.spec import Source, specs_by_key
from chrys.foundation.events.types import Warning
from chrys.kernel import Message
from chrys.service.state.store import JsonFileStateStore
from tests.app.acp._session_manager_fakes import (
    _manager,
    _redirect_config_dir,
    _SettingsScopeHost,
    _StartedHost,
    _StaticListStore,
    _stored_setting,
)


def _dormant_project(tmp_path) -> Path:
    """A project dir holding a settings file that only a trusted project would apply."""
    project = tmp_path / "project"
    (project / ".chrys").mkdir(parents=True)
    (project / ".chrys" / "settings.yaml").write_text("session:\n  title:\n    auto: false\n", encoding="utf-8")
    return project


def test_set_config_option_stores_newline_value_without_injection(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """YAML stores a newline verbatim without creating a second setting."""
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    manager.set_config_option("theme", "dark\nCHRYS_INJECTED=evil")

    assert _stored_setting(tmp_path, "ui.theme") == "dark\nCHRYS_INJECTED=evil"
    assert "CHRYS_INJECTED" not in os.environ
    assert not (tmp_path / ".env").exists()


def test_set_config_option_maps_empty_value_to_removal(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    env_path = tmp_path / ".env"
    env_original = "CHRYS_THEME=dark\nKEEP=value\n"
    env_path.write_text(env_original, encoding="utf-8")
    manager.set_config_option("theme", "dark")

    result = manager.set_config_option("theme", None)

    assert result["value"] == ""
    assert _stored_setting(tmp_path, "ui.theme") is None
    # Legacy files are left untouched.
    assert env_path.read_text(encoding="utf-8") == env_original


def test_two_concurrent_config_writes_both_reach_the_base_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Neither client's value may be lost from the settings new sessions start on."""
    # Two clients configuring two sessions run this transaction in separate
    # worker threads, and each one writes the document and then re-reads it.
    # A refresh that publishes a snapshot taken before the other client's write
    # leaves the manager handing every session created afterwards a value that
    # is already on disk.
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    # Without a frozen snapshot the file layers are not read at all, and both
    # refreshes would agree on the defaults for the wrong reason.
    freeze_process_env()
    first_read = threading.Event()
    second_published = threading.Event()
    reads = itertools.count()

    def load_with_a_gap(*args: object, **kwargs: object) -> LoadedSettings:
        first = next(reads) == 0
        loaded = load_settings(*args, **kwargs)  # type: ignore[arg-type]
        if first:
            # Hold the first reader between its read and its publish: that gap
            # is the only window in which the other write can be lost.
            first_read.set()
            second_published.wait(0.3)
        return loaded

    monkeypatch.setattr("chrys.app.acp.session_manager.load_settings", load_with_a_gap)

    def write_theme() -> None:
        manager.set_config_option("theme", "dark")

    def write_default_agent() -> None:
        first_read.wait(10)
        manager.set_config_option("default_agent", "Reviewer")
        second_published.set()

    writers = [threading.Thread(target=write_theme), threading.Thread(target=write_default_agent)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(30)

    assert _stored_setting(tmp_path, "ui.theme") == "dark"
    assert _stored_setting(tmp_path, "agent.default_profile") == "Reviewer"
    base = {entry["key"]: entry["baseValue"] for entry in manager.get_config_options()["options"]}
    assert (base["theme"], base["default_agent"]) == ("dark", "Reviewer")


def test_session_loaded_settings_derives_each_sessions_own_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Two sessions in two roots each get their own project trust domain,
    and neither leaks into the manager's deliberately project-free base."""
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path / "config")
    monkeypatch.delenv("CHRYS_SESSION_TITLE_AUTO", raising=False)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text("project:\n  config_enabled: true\n", encoding="utf-8")
    quiet = tmp_path / "quiet"
    (quiet / ".chrys").mkdir(parents=True)
    (quiet / ".chrys" / "settings.yaml").write_text("session:\n  title:\n    auto: false\n", encoding="utf-8")
    loud = tmp_path / "loud"
    loud.mkdir()
    freeze_process_env()

    quiet_loaded = manager._session_loaded_settings(str(quiet))
    loud_loaded = manager._session_loaded_settings(str(loud))

    assert quiet_loaded.settings.session_title_auto is False
    assert quiet_loaded.source_for("session.title.auto").layer is Source.PROJECT
    assert loud_loaded.settings.session_title_auto is True
    assert manager._loaded_settings.settings.session_title_auto is True
    # The launch-time timeout stays the CLI layer's, not a re-read's.
    assert quiet_loaded.source_for("tools.ask_user.timeout_seconds").layer is Source.CLI


@pytest.mark.asyncio
async def test_new_session_collects_project_settings_warnings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Session creation runs outside any prompt turn, so the load's verdicts
    reach the caller through the collector — here, a dormant project file."""
    _redirect_config_dir(monkeypatch, tmp_path / "config")
    project = _dormant_project(tmp_path)
    freeze_process_env()
    manager = _manager(None, JsonFileStateStore(tmp_path / "sessions"))
    _StartedHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)
    warnings: list[Warning] = []

    await manager.new_session(cwd=str(project), mcp_servers=None, warnings=warnings)

    assert [event.code for event in warnings] == ["project_config_dormant"]


@pytest.mark.asyncio
async def test_load_session_collects_project_settings_warnings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The committed restore load's verdicts reach the caller through the
    same collector new_session uses."""
    _redirect_config_dir(monkeypatch, tmp_path / "config")
    project = _dormant_project(tmp_path)
    freeze_process_env()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(None, store)
    _StartedHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)
    warnings: list[Warning] = []

    loaded = await manager.load_session(
        cwd=str(project), session_id="project-session", mcp_servers=None, warnings=warnings
    )

    assert loaded.reused_existing is False
    assert [event.code for event in warnings] == ["project_config_dormant"]


@pytest.mark.asyncio
async def test_load_session_reports_the_committed_restore_loads_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The restore re-reads settings; an edit landing between the manager's
    pre-restore snapshot and that committed load must be reported as the
    session will actually run — the committed verdicts, not the snapshot's."""
    _redirect_config_dir(monkeypatch, tmp_path / "config")
    project = _dormant_project(tmp_path)
    freeze_process_env()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(None, store)
    _StartedHost.instances = []

    class _RestoringHost(_StartedHost):
        async def start(self) -> None:
            # The restore's own load saw one more problem than the snapshot.
            self.engine.loaded_settings = dataclasses.replace(
                self.kwargs["loaded_settings"], unknown_keys=("mystery.key",)
            )

    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _RestoringHost)
    warnings: list[Warning] = []

    await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=None, warnings=warnings)

    assert [event.code for event in warnings] == ["setting_unknown_keys", "project_config_dormant"]


def test_two_concurrent_model_writes_leave_the_pointer_agreeing_with_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The live model and the stored model must not be two different profiles."""
    # The pointer outranks the document it was written from, so an interleave
    # that commits one profile to disk and installs the other as the pointer is
    # not merely a lost update: this process runs one model and would come back
    # on the other after a restart, with no write in between to explain it.
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    freeze_process_env()
    # Claim the carrier so an installed pointer never outlives the test.
    monkeypatch.setenv(MODEL_POINTER_ENV, "claimed")
    monkeypatch.delenv(MODEL_POINTER_ENV)
    first_stored = threading.Event()
    second_done = threading.Event()
    writes = itertools.count()

    def persist_with_a_gap(*args: object, **kwargs: object) -> PersistResult:
        result = persist(*args, **kwargs)  # type: ignore[arg-type]
        if next(writes) == 0:
            # Hold the first writer between its document commit and its pointer
            # write — the window where the two can end up disagreeing.
            first_stored.set()
            second_done.wait(0.3)
        return result

    monkeypatch.setattr("chrys.app.acp.session_manager.persist", persist_with_a_gap)

    def write_model_a() -> None:
        manager.set_config_option("model_profile", "model-a")

    def write_model_b() -> None:
        first_stored.wait(10)
        manager.set_config_option("model_profile", "model-b")
        second_done.set()

    writers = [threading.Thread(target=write_model_a), threading.Thread(target=write_model_b)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(30)

    stored = _stored_setting(tmp_path, "model.profile.active")
    base = {entry["key"]: entry["baseValue"] for entry in manager.get_config_options()["options"]}
    assert stored in {"model-a", "model-b"}
    assert (get_model_pointer()[0], base["model_profile"]) == (stored, stored)


def test_a_config_read_cannot_land_between_a_write_and_its_republish(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``value`` and ``baseValue`` must describe one state, or the reply is a lie."""
    # The document's own lock is released at the commit; the base settings are
    # only republished at the end of the transaction. A read in that gap pairs
    # the newly stored value with the superseded effective one — two answers to
    # "what is this option" that contradict each other.
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    freeze_process_env()
    committed = threading.Event()
    released = threading.Event()

    def persist_then_stall(*args: object, **kwargs: object) -> PersistResult:
        result = persist(*args, **kwargs)  # type: ignore[arg-type]
        committed.set()
        released.wait(10)
        return result

    monkeypatch.setattr("chrys.app.acp.session_manager.persist", persist_then_stall)

    payloads: list[dict[str, object]] = []
    writer = threading.Thread(target=lambda: manager.set_config_option("theme", "dark"))
    reader = threading.Thread(target=lambda: payloads.append(manager.get_config_options()))

    writer.start()
    assert committed.wait(10)
    reader.start()
    reader.join(0.3)
    assert reader.is_alive(), "the read entered the writer's transaction"
    released.set()
    for thread in (writer, reader):
        thread.join(30)

    options = {entry["key"]: entry for entry in payloads[0]["options"]}  # type: ignore[union-attr]
    assert (options["theme"]["value"], options["theme"]["baseValue"]) == ("dark", "dark")


def test_set_config_option_downgrades_bypass_approval_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    result = manager.set_config_option("default_approval_mode", "bypass")

    assert _stored_setting(tmp_path, "approval.default_mode") == "auto"
    assert result["value"] == "auto"
    # No mirror: an environment copy would come back as the ENV layer and
    # outrank the document this just wrote.
    assert "CHRYS_DEFAULT_APPROVAL_MODE" not in os.environ


def test_set_config_option_keeps_non_bypass_approval_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    manager.set_config_option("default_approval_mode", "manual")

    assert _stored_setting(tmp_path, "approval.default_mode") == "manual"


@pytest.mark.parametrize("key", ["approval_auto_review", "CHRYS_APPROVAL_AUTO_REVIEW"])
def test_auto_review_option_writes_user_setting(monkeypatch, tmp_path, key):
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    result = manager.set_config_option(key, "formal")
    assert _stored_setting(tmp_path, "approval.auto_review") == "formal"
    assert result["settingKey"] == "approval.auto_review"
    assert result["envKey"] == "CHRYS_APPROVAL_AUTO_REVIEW"


def test_set_config_option_rejects_non_int_rollback_snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    # A non-integer would be persisted then break the next settings load,
    # so it must be rejected before any document write.
    with pytest.raises(AcpSessionError, match="rollback_snapshots_keep"):
        manager.set_config_option("rollback_snapshots_keep", "abc")

    assert not (tmp_path / "settings.yaml").exists()


def test_set_config_option_accepts_int_rollback_snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    result = manager.set_config_option("rollback_snapshots_keep", "5")

    # Stored canonical (an int), rendered on the wire as text.
    assert _stored_setting(tmp_path, "rollback.snapshots_keep") == 5
    assert result["value"] == "5"


def test_set_config_option_accepts_the_legacy_env_spelling(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Old clients send ``CHRYS_*`` names; both grammars land on the same key."""
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    result = manager.set_config_option("CHRYS_THEME", "dark")

    assert _stored_setting(tmp_path, "ui.theme") == "dark"
    assert result["key"] == "CHRYS_THEME"
    assert result["envKey"] == "CHRYS_THEME"
    assert result["settingKey"] == "ui.theme"


def test_set_config_option_rejects_unknown_keys_in_either_grammar(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    with pytest.raises(AcpSessionError, match="Unsupported config option"):
        manager.set_config_option("mystery", "x")
    with pytest.raises(AcpSessionError, match="Unsupported config option"):
        manager.set_config_option("CHRYS_MYSTERY", "x")
    assert not (tmp_path / "settings.yaml").exists()


def test_config_option_descriptors_agree_with_the_settings_specs() -> None:
    """Three names, one key: the wire aliases must track the spec declarations."""
    specs = specs_by_key(Settings)
    for option in session_manager_module._SUPPORTED_CONFIG_OPTIONS:
        assert specs[option.setting_key].env == option.env_alias, option.logical_key
        assert specs[option.setting_key].persist, option.logical_key


def test_get_config_options_reports_document_value_and_base_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    freeze_process_env()
    manager.set_config_option("theme", "dark")

    result = manager.get_config_options()

    assert "sessionId" not in result
    options = {entry["key"]: entry for entry in result["options"]}
    assert options["theme"] == {
        "key": "theme",
        "envKey": "CHRYS_THEME",
        "settingKey": "ui.theme",
        "value": "dark",
        "baseValue": "dark",
        "baseSource": "user",
    }
    # A key the document does not hold has no durable value, but the base
    # pair still answers with the built-in default.
    assert options["rollback_snapshots_keep"]["value"] == ""
    assert options["rollback_snapshots_keep"]["baseValue"] == str(Settings().rollback_snapshots_keep)
    assert options["rollback_snapshots_keep"]["baseSource"] == "default"


def test_get_config_options_with_a_session_id_adds_that_sessions_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)
    session_loaded = LoadedSettings(settings=Settings(), provenance={}).overlay(Source.CLI, theme="session-dark")
    manager._sessions["s1"] = ManagedSession(
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_SettingsScopeHost(session_loaded),  # type: ignore[arg-type]
    )

    result = manager.get_config_options("s1")

    assert result["sessionId"] == "s1"
    options = {entry["key"]: entry for entry in result["options"]}
    assert options["theme"]["sessionValue"] == "session-dark"
    assert options["theme"]["sessionSource"] == "cli"
    # The base pair keeps answering for the manager, never renamed to look
    # like the session's own view.
    assert options["theme"]["baseValue"] == Settings().theme
    assert options["theme"]["baseSource"] == "default"


def test_get_config_options_rejects_an_unknown_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(None, _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    with pytest.raises(AcpSessionError, match="not active"):
        manager.get_config_options("ghost")


@pytest.mark.asyncio
async def test_set_config_option_refreshes_settings_for_future_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _StartedHost.instances.clear()
    manager = _manager(str(tmp_path), _StaticListStore([]))
    manager._loaded_settings = LoadedSettings(
        settings=Settings(model_profile="old-model", ask_user_timeout_seconds=None),
        provenance={},
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "old-model")
    _redirect_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)

    manager.set_config_option("model_profile", "new-model")
    await manager.new_session(cwd=str(tmp_path), mcp_servers=None)

    loaded = _StartedHost.instances[-1].kwargs["loaded_settings"]
    assert manager._loaded_settings.settings.model_profile == "new-model"
    assert loaded.settings.model_profile == "new-model"
    # The pointer write registered this process as the writer, so the refresh
    # attributes it to the runtime instead of blaming the environment.
    assert loaded.source_for("model.profile.active").layer is Source.PROCESS_RUNTIME
    assert loaded.settings.ask_user_timeout_seconds is None
    # The launch-time --ask-user-timeout is a CLI value; a re-read of the
    # environment cannot produce it, so it has to be carried across as one.
    assert loaded.source_for("tools.ask_user.timeout_seconds").layer is Source.CLI


@pytest.mark.asyncio
async def test_apply_config_option_rejects_inactive_session_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    _redirect_config_dir(monkeypatch, tmp_path)

    # A missing/stale/inactive session must fail BEFORE the global document
    # write, so a failed RPC never leaves persisted config mutated.
    with pytest.raises(AcpSessionError, match="not active"):
        await manager.apply_config_option("ghost", "model_profile", "new-model")

    assert not (tmp_path / "settings.yaml").exists()
