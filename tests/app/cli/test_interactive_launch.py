# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request identity and full-package boundaries of the interactive handoff."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chrys.app.cli.interactive_launch import InteractiveLaunch, LaunchManifest, read_receipt
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeSkillDetails
from chrys.service.profiles.agents.schema import AgentProfile
from tests.support.secure_files import plant_owner_only_bytes


def manifest_file(tmp_path: Path, request_id: str = "one", *, text: str = "Read the project context") -> Path:
    path = tmp_path / f"{request_id}.json"
    value = {
        "protocolVersion": 1,
        "requestId": request_id,
        "cwd": str(tmp_path),
        "mode": "new",
        "configuration": {},
        "context": {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()},
        "skills": [],
    }
    plant_owner_only_bytes(path, json.dumps(value).encode())
    return path


def test_interactive_claim_survives_restart_and_rejects_changed_material(tmp_path: Path) -> None:
    first = InteractiveLaunch(LaunchManifest.read(manifest_file(tmp_path)))
    assert first.claim() is True
    second = InteractiveLaunch(first.manifest)
    assert second.claim() is False
    assert second.receipt["status"] == "unknown"
    changed = InteractiveLaunch(LaunchManifest.read(manifest_file(tmp_path, "other", text="Changed")))
    from dataclasses import replace

    changed = InteractiveLaunch(replace(changed.manifest, request_id="one"))
    with pytest.raises(ValueError, match="conflicts"):
        changed.claim()
    assert read_receipt("one")["requestSha256"] == first.manifest.request_sha256


def test_interactive_claim_does_not_treat_corrupt_receipt_as_missing(tmp_path: Path) -> None:
    launch = InteractiveLaunch(LaunchManifest.read(manifest_file(tmp_path)))
    launch.path.parent.mkdir(parents=True, exist_ok=True)
    plant_owner_only_bytes(launch.path, b"broken-json")
    with pytest.raises(ValueError):
        launch.claim()
    assert launch.path.read_bytes() == b"broken-json"


def skill_manifest(tmp_path: Path) -> LaunchManifest:
    from dataclasses import replace

    from chrys.app.cli.interactive_launch import LaunchSkill

    directory = tmp_path / ".agents" / "skills" / "review"
    directory.mkdir(parents=True)
    text = "---\nname: review\ndescription: Review the work\n---\nRead the context first.\n"
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    (directory / "check.py").write_text("print('check')\n", encoding="utf-8")
    files = tuple(
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "executable": False}
        for path in sorted(directory.iterdir())
    )
    pin = LaunchSkill(
        "review",
        directory,
        hashlib.sha256(text.encode()).hexdigest(),
        True,
        "repository",
        files,
        {"repositoryId": "primary", "relativePath": ".agents/skills/review"},
    )
    return replace(LaunchManifest.read(manifest_file(tmp_path)), skills=(pin,))


def test_interactive_skill_pin_checks_script_bytes_and_extra_members(tmp_path: Path) -> None:
    manifest = skill_manifest(tmp_path)
    launch = InteractiveLaunch(manifest)
    pin = manifest.skills[0]
    details = AgentRuntimeDetails(
        skill_details=[
            RuntimeSkillDetails(
                name=pin.name, source=str(pin.directory), content_sha256=pin.content_sha256, file_based=True
            )
        ]
    )
    launch.verify_skills(details, project_enabled=True, profile=AgentProfile(name="Code"))
    assert launch.receipt["skills"][0]["status"] == "verified"
    (pin.directory / "check.py").write_text("print('different')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="required"):
        launch.verify_skills(details, project_enabled=True, profile=AgentProfile(name="Code"))
    assert launch.receipt["skills"][0]["status"] == "content-mismatch"
    (pin.directory / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="required"):
        launch.verify_skills(details, project_enabled=True, profile=AgentProfile(name="Code"))


def test_interactive_skill_pin_respects_project_trust_and_native_shadowing(tmp_path: Path) -> None:
    manifest = skill_manifest(tmp_path)
    launch = InteractiveLaunch(manifest)
    pin = manifest.skills[0]
    details = AgentRuntimeDetails(
        skill_details=[
            RuntimeSkillDetails(
                name=pin.name, source=str(pin.directory), content_sha256=pin.content_sha256, file_based=True
            )
        ]
    )
    with pytest.raises(ValueError, match="required"):
        launch.verify_skills(details, project_enabled=False, profile=AgentProfile(name="Code"))
    assert launch.receipt["skills"][0]["status"] == "unavailable"
    details.skill_details[0].source = str(tmp_path / "other-skill")
    with pytest.raises(ValueError, match="required"):
        launch.verify_skills(details, project_enabled=True, profile=AgentProfile(name="Code"))
    assert launch.receipt["skills"][0]["status"] == "shadowed"


def test_interactive_confirm_preserves_draft_semantics_and_strict_identity(tmp_path: Path) -> None:
    from dataclasses import replace

    manifest = LaunchManifest.read(manifest_file(tmp_path))
    launch = InteractiveLaunch(manifest)
    assert launch.claim()
    launch.confirm("native-session-one", str(tmp_path))
    receipt = read_receipt("one")
    assert receipt["materials"] == "composer-staged"
    assert receipt["submitted"] == receipt["read"] == receipt["adopted"] == "unverified"
    launch.manifest = replace(manifest, mode="resume", session_id="native-session-one")
    with pytest.raises(ValueError, match="different session"):
        launch.confirm("replacement-session", str(tmp_path))
    assert read_receipt("one")["sessionId"] == "native-session-one"
