# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP model-profile writes, deletions and judge-selection changes go through the relay guard."""

from __future__ import annotations

import threading

import pytest

from chrys.app.acp import session_manager as session_manager_module
from chrys.app.acp.session_manager import AcpSessionError, AcpSessionManager
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.acp._session_manager_fakes import _redirect_config_dir, _StaticListStore, _stored_setting


def _relay_guard_manager(judge_selector: str = "") -> AcpSessionManager:
    registry = ModelProfileRegistry()
    registry.register(
        ModelProfile(
            id="relay",
            name="Relay",
            model_id="gpt-x",
            base_url="https://relay.example.com/v1",
            api_key="sk-relay",
            is_relay=True,
        )
    )
    registry.register(
        ModelProfile(
            id="judge",
            name="Judge",
            model_id="gpt-y",
            base_url="https://direct.example.com/v1",
            api_key="sk-judge",
        )
    )
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    return AcpSessionManager(
        loaded_settings=LoadedSettings(settings=Settings(approval_judge_model_profile=judge_selector), provenance={}),
        profile_name="Code",
        approval_mode=ApprovalMode.MANUAL,
        process_cwd=None,
        state_store=_StaticListStore([]),
        agent_registry=agent_registry,
        model_registry=registry,
    )


def _capture_model_profile_writes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[ModelProfile], list[str]]:
    saved: list[ModelProfile] = []
    deleted: list[str] = []

    def _fake_save(profile: ModelProfile) -> str:
        saved.append(profile)
        return "/tmp/fake.yaml"

    def _fake_delete(profile_id: str) -> bool:
        deleted.append(profile_id)
        return True

    monkeypatch.setattr(session_manager_module, "save_model_profile", _fake_save)
    monkeypatch.setattr(session_manager_module, "delete_model_profile", _fake_delete)
    return saved, deleted


def test_write_relay_profile_without_judge_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _relay_guard_manager()
    saved, deleted = _capture_model_profile_writes(monkeypatch)

    with pytest.raises(AcpSessionError, match="approval judge"):
        manager.write_model_profile(
            {"id": "second-relay", "name": "Second Relay", "is_relay": True, "model_id": "gpt-x"}
        )

    assert saved == []
    assert deleted == []


def test_write_relay_profile_with_independent_judge_persists_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _relay_guard_manager(judge_selector="judge")
    saved, _deleted = _capture_model_profile_writes(monkeypatch)

    result = manager.write_model_profile(
        {"id": "second-relay", "name": "Second Relay", "is_relay": True, "model_id": "gpt-x"}
    )

    assert [profile.is_relay for profile in saved] == [True]
    assert result["profile"]["isRelay"] is True


def test_write_promoting_the_stored_judge_to_relay_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # The merged update replaces the judge record in the prospective set, so
    # the promotion cannot borrow the old direct-service record.
    manager = _relay_guard_manager(judge_selector="judge")
    saved, _deleted = _capture_model_profile_writes(monkeypatch)

    with pytest.raises(AcpSessionError, match="approval judge"):
        manager.write_model_profile({"id": "judge", "name": "Judge", "is_relay": True})

    assert saved == []


def test_write_of_unrelated_profile_with_broken_selection_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _relay_guard_manager()
    saved, _deleted = _capture_model_profile_writes(monkeypatch)

    manager.write_model_profile({"id": "third", "name": "Third", "model_id": "gpt-z"})

    assert len(saved) == 1


def test_delete_of_judge_with_dependent_relay_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _relay_guard_manager(judge_selector="judge")
    _saved, deleted = _capture_model_profile_writes(monkeypatch)

    with pytest.raises(AcpSessionError, match="approval judge"):
        manager.delete_model_profile("judge")

    assert deleted == []


def test_delete_of_relay_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _relay_guard_manager(judge_selector="judge")
    _saved, deleted = _capture_model_profile_writes(monkeypatch)

    manager.delete_model_profile("relay")

    assert deleted == ["relay"]


def test_set_config_option_judge_change_is_guarded(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _relay_guard_manager(judge_selector="judge")
    _redirect_config_dir(monkeypatch, tmp_path)

    # Clearing the selection would leave the relay unguarded.
    with pytest.raises(AcpSessionError, match="approval judge"):
        manager.set_config_option("approval_judge_model_profile", None)
    assert _stored_setting(tmp_path, "model.role.approval_judge") is None

    # Pointing it at the relay is refused too.
    with pytest.raises(AcpSessionError, match="approval judge"):
        manager.set_config_option("approval_judge_model_profile", "relay")
    assert _stored_setting(tmp_path, "model.role.approval_judge") is None


def test_set_config_option_judge_change_to_qualified_model_persists(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _relay_guard_manager(judge_selector="")
    _redirect_config_dir(monkeypatch, tmp_path)

    manager.set_config_option("approval_judge_model_profile", "judge")

    assert _stored_setting(tmp_path, "model.role.approval_judge") == "judge"


def _manager_without_relays(judge_selector: str) -> AcpSessionManager:
    registry = ModelProfileRegistry()
    registry.register(
        ModelProfile(
            id="judge",
            name="Judge",
            model_id="gpt-y",
            base_url="https://direct.example.com/v1",
            api_key="sk-judge",
        )
    )
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    return AcpSessionManager(
        loaded_settings=LoadedSettings(settings=Settings(approval_judge_model_profile=judge_selector), provenance={}),
        profile_name="Code",
        approval_mode=ApprovalMode.MANUAL,
        process_cwd=None,
        state_store=_StaticListStore([]),
        agent_registry=agent_registry,
        model_registry=registry,
    )


def test_write_renaming_a_judge_without_any_relays_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    # No relay profiles exist: ordinary edits to the existing judge profile
    # (here: rename and mock provider) must not hit the relay-specific
    # qualification at the ACP layer either.
    manager = _manager_without_relays("judge")
    saved, _deleted = _capture_model_profile_writes(monkeypatch)

    manager.write_model_profile(
        {
            "id": "judge",
            "name": "Judge Renamed",
            "provider": "mock",
            "model_id": "gpt-y",
        }
    )

    assert [profile.name for profile in saved] == ["Judge Renamed"]


def _manager_with_bad_judge_candidate(judge_selector: str) -> AcpSessionManager:
    # Two direct profiles: a qualified judge and "bad", which lives on the
    # same host the concurrent relay write below would use.
    manager = _manager_without_relays(judge_selector)
    manager._model_registry.register(
        ModelProfile(
            id="bad",
            name="Bad",
            model_id="gpt-z",
            base_url="https://shared.example.com/v1",
            api_key="sk-bad",
        )
    )
    return manager


_RELAY_ON_SHARED_HOST = {
    "id": "second-relay",
    "name": "Second Relay",
    "model_id": "gpt-x",
    "base_url": "https://shared.example.com/v1",
    "api_key": "sk-relay",
    "is_relay": True,
}


def _run_in_thread(action) -> tuple[threading.Thread, dict[str, BaseException]]:
    errors: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            action()
        except BaseException as error:
            errors["error"] = error

    thread = threading.Thread(target=_run)
    thread.start()
    return thread, errors


def test_judge_change_paused_mid_commit_blocks_a_concurrent_relay_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The judge change validates (no relays yet), then pauses inside its
    # persist. The relay write must not commit against the pre-change judge
    # selection: one lock spans both validate→commit transactions, so the
    # write cannot even reach its validation until the judge change has
    # landed — and is then refused for sharing the new judge's host.
    from chrys.service.profiles.models.relay_guard import relay_guard_issues

    manager = _manager_with_bad_judge_candidate("judge")
    _redirect_config_dir(monkeypatch, tmp_path)

    saved: list[ModelProfile] = []
    entered_persist = threading.Event()
    release_persist = threading.Event()
    save_entered = threading.Event()
    release_save = threading.Event()
    real_persist = session_manager_module.persist

    def _pausing_persist(*args: object, **kwargs: object) -> object:
        entered_persist.set()
        release_persist.wait(timeout=5)
        return real_persist(*args, **kwargs)  # type: ignore[arg-type]

    def _pausing_save(profile: ModelProfile) -> str:
        save_entered.set()
        release_save.wait(timeout=5)
        saved.append(profile)
        return "/tmp/fake.yaml"

    monkeypatch.setattr(session_manager_module, "persist", _pausing_persist)
    monkeypatch.setattr(session_manager_module, "save_model_profile", _pausing_save)

    judge_thread, judge_errors = _run_in_thread(
        lambda: manager.set_config_option("approval_judge_model_profile", "bad")
    )
    assert entered_persist.wait(timeout=5)

    write_thread, write_errors = _run_in_thread(lambda: manager.write_model_profile(dict(_RELAY_ON_SHARED_HOST)))

    # While the judge change holds the shared transaction the write cannot
    # reach its save; under the old split locks it would already be parked
    # inside it (validated against the pre-change selection). Bounded
    # absence check, not a readiness wait.
    assert not save_entered.wait(timeout=0.5)

    release_persist.set()
    judge_thread.join(timeout=10)
    release_save.set()  # only reached if the write ever parked in the save
    write_thread.join(timeout=10)

    assert "error" not in judge_errors
    assert isinstance(write_errors.get("error"), AcpSessionError)
    assert saved == []
    assert _stored_setting(tmp_path, "model.role.approval_judge") == "bad"
    # The persisted selection is self-consistent: no relay profile exists.
    assert relay_guard_issues(manager._model_registry.list_profiles(), "bad") == []


def test_relay_write_paused_mid_commit_blocks_a_concurrent_judge_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Mirror interleave: the relay write validates against the still-qualified
    # judge, then pauses inside its save. The judge change must wait for the
    # whole write transaction and then re-validate against the registered
    # relay — and be refused for sharing its host, keeping the qualified
    # selection in place.
    from chrys.service.profiles.models.relay_guard import relay_guard_issues

    manager = _manager_with_bad_judge_candidate("judge")
    _redirect_config_dir(monkeypatch, tmp_path)

    saved: list[ModelProfile] = []
    entered_persist = threading.Event()
    release_persist = threading.Event()
    save_entered = threading.Event()
    release_save = threading.Event()
    real_persist = session_manager_module.persist

    def _pausing_persist(*args: object, **kwargs: object) -> object:
        entered_persist.set()
        release_persist.wait(timeout=5)
        return real_persist(*args, **kwargs)  # type: ignore[arg-type]

    def _pausing_save(profile: ModelProfile) -> str:
        save_entered.set()
        release_save.wait(timeout=5)
        saved.append(profile)
        return "/tmp/fake.yaml"

    monkeypatch.setattr(session_manager_module, "persist", _pausing_persist)
    monkeypatch.setattr(session_manager_module, "save_model_profile", _pausing_save)

    write_thread, write_errors = _run_in_thread(lambda: manager.write_model_profile(dict(_RELAY_ON_SHARED_HOST)))
    assert save_entered.wait(timeout=5)

    judge_thread, judge_errors = _run_in_thread(
        lambda: manager.set_config_option("approval_judge_model_profile", "bad")
    )

    # The judge change cannot commit while the relay write holds the shared
    # transaction; under the old split locks it would already be parked in
    # its own persist. Bounded absence check, not a readiness wait.
    assert not entered_persist.wait(timeout=0.5)

    release_save.set()
    write_thread.join(timeout=10)
    release_persist.set()  # only reached if the judge change ever parked in persist
    judge_thread.join(timeout=10)

    assert "error" not in write_errors
    assert [profile.id for profile in saved] == ["second-relay"]
    assert isinstance(judge_errors.get("error"), AcpSessionError)
    assert _stored_setting(tmp_path, "model.role.approval_judge") is None
    # The kept selection stays consistent with the registered relay.
    assert relay_guard_issues(manager._model_registry.list_profiles(), "judge") == []
