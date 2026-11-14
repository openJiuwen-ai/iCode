# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A profile file nobody can read must not report itself as a profile nobody wrote.

``icode run -a NAME`` used to answer "not found" whether NAME was absent or its
file had a YAML error, so the one action that could never work — writing the
profile again — was the one the message asked for.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from chrys.app.cli import headless
from chrys.app.cli import run as run_cli
from chrys.foundation.i18n import DisplaySequence
from chrys.foundation.i18n.formatting import format_message
from chrys.orchestration.session_host import agent_profile_not_found
from chrys.service.profiles.agents.loader import read_profile_dir, scan_profile_dir
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.file_scan import ProfileDirScan, ProfileFileIssue, unreadable_file_notes
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile

BROKEN_YAML = "name: [unclosed\n"
BROKEN_REASON = "Invalid YAML at line 2, column 1"
VALID_PROFILE = "name: my-custom\ndescription: user profile\n"


def _registry(tmp_path: Path, *files: tuple[str, str]) -> AgentProfileRegistry:
    for name, body in files:
        (tmp_path / name).write_text(body, encoding="utf-8")
    registry = AgentProfileRegistry()
    registry.load_user_profiles(tmp_path)
    return registry


def test_a_broken_file_is_named_instead_of_listing_other_profiles(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ("myagent.yaml", BROKEN_YAML), ("other.yaml", VALID_PROFILE))

    error = agent_profile_not_found("myagent", registry)

    broken = str(tmp_path / "myagent.yaml")
    message = headless.exception_message(error)

    assert message == (
        f"Agent profile not found: myagent. Could not read these profile files: {broken} ({BROKEN_REASON})"
    )
    # The display text tracks the raw text: --json and the terminal say the same thing.
    assert format_message(error.display_message) == message


def test_a_broken_file_still_reads_as_not_found_when_no_other_profile_loaded(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ("myagent.yaml", BROKEN_YAML))

    error = agent_profile_not_found("myagent", registry)

    assert "Could not read these profile files" in str(error)
    assert "Available profiles" not in str(error)


def test_the_reason_is_the_one_the_listing_prints(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ("myagent.yaml", BROKEN_YAML))

    error = agent_profile_not_found("myagent", registry)

    assert BROKEN_REASON in str(error)
    assert "YAML 2:1" not in str(error), "the coordinates alone do not say what failed"


def test_a_missing_name_with_every_file_readable_keeps_the_old_message(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ("other.yaml", VALID_PROFILE))

    error = agent_profile_not_found("ghost", registry)

    message = headless.exception_message(error)

    assert message == "Agent profile not found: ghost. Available profiles: my-custom"
    assert "Could not read" not in message


def test_nothing_loaded_and_nothing_broken_still_names_the_selector(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert headless.exception_message(agent_profile_not_found("ghost", registry)) == "Agent profile not found: ghost"


def test_reading_the_directory_again_does_not_repeat_the_listing_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The failure path re-reads the directory; a second copy of the same ERROR would be noise."""
    (tmp_path / "myagent.yaml").write_text(BROKEN_YAML, encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        read_profile_dir(tmp_path)
    assert caplog.records == []

    with caplog.at_level(logging.ERROR):
        scan_profile_dir(tmp_path)
    assert caplog.records


def test_no_unreadable_file_yields_no_notes() -> None:
    assert unreadable_file_notes({}) is None


def test_notes_name_every_file_in_path_order(tmp_path: Path) -> None:
    invalid = {
        tmp_path / "b.yaml": ProfileFileIssue("YAML 2:1", BROKEN_REASON),
        tmp_path / "a.yaml": ProfileFileIssue("model.profile_id", "Invalid or missing field model.profile_id"),
    }

    notes = unreadable_file_notes(invalid)

    assert isinstance(notes, DisplaySequence)
    assert notes.values == (
        f"{tmp_path / 'a.yaml'} (Invalid or missing field model.profile_id)",
        f"{tmp_path / 'b.yaml'} ({BROKEN_REASON})",
    )


def test_a_broken_model_file_is_named_too(tmp_path: Path) -> None:
    broken = tmp_path / "mymodel.yaml"
    scan = ProfileDirScan[ModelProfile](invalid={broken: ProfileFileIssue("YAML 2:1", BROKEN_REASON)})

    error = run_cli._model_profile_not_found("mymodel", ModelProfileRegistry(), scan)

    message = headless.exception_message(error)

    assert isinstance(error, run_cli.ModelProfileNotFoundError)
    assert (
        message == f"Model profile not found: mymodel. Could not read these profile files: {broken} ({BROKEN_REASON})"
    )
    assert format_message(error.display_message) == message


def test_a_broken_model_file_outranks_the_available_list(tmp_path: Path) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-id", name="Friendly Model"))
    scan = ProfileDirScan[ModelProfile](
        invalid={tmp_path / "mymodel.yaml": ProfileFileIssue("YAML 2:1", BROKEN_REASON)}
    )

    error = run_cli._model_profile_not_found("mymodel", registry, scan)

    assert "Could not read these profile files" in str(error)
    assert "Available model profiles" not in str(error)
