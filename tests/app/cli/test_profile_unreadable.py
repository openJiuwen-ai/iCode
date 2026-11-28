# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A profile file nobody can read must not report itself as a profile nobody wrote.

``icode run -a NAME`` used to answer "not found" whether NAME was absent or its
file had a YAML error, so the one action that could never work — writing the
profile again — was the one the message asked for.
"""

from __future__ import annotations

import errno
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from chrys.app.cli import headless
from chrys.app.cli import run as run_cli
from chrys.foundation.i18n import DisplaySequence
from chrys.foundation.i18n.formatting import format_message
from chrys.orchestration import session_host as session_host_module
from chrys.orchestration.session_host import agent_profile_not_found
from chrys.service.profiles.agents.loader import read_profile_dir, scan_profile_dir
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.file_scan import (
    ProfileDirScan,
    ProfileFileIssue,
    unreadable_file_notes,
    unreadable_notes,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.platform_fakes import platform_with_config_dir

BROKEN_YAML = "name: [unclosed\n"
BROKEN_REASON = "Invalid YAML at line 2, column 1"
VALID_PROFILE = "name: my-custom\ndescription: user profile\n"
# A section the parser drops with a WARNING and still loads: the profile is usable,
# but reading it is not silent, which is what a second read must not repeat.
LEGACY_HOOKS_PROFILE = "name: legacy-agent\ndescription: loads with a warning\nhooks:\n  - event: pre_tool\n"


def _registry(tmp_path: Path, *files: tuple[str, str]) -> AgentProfileRegistry:
    for name, body in files:
        (tmp_path / name).write_text(body, encoding="utf-8")
    registry = AgentProfileRegistry()
    registry.load_user_profiles(tmp_path)
    return registry


def test_a_broken_file_is_named_alongside_the_profiles_that_loaded(tmp_path: Path) -> None:
    """The two facts answer different questions, so one must not replace the other.

    A misspelt selector needs the list — that is how the typo is found.  A broken
    file needs naming — that is why the spelling was not the problem.  Reporting
    only the second leaves the user with no way to tell which mistake they made.
    """
    registry = _registry(tmp_path, ("myagent.yaml", BROKEN_YAML), ("other.yaml", VALID_PROFILE))

    error = agent_profile_not_found("myagent", registry)

    broken = str(tmp_path / "myagent.yaml")
    message = headless.exception_message(error)

    assert message == (
        f"Agent profile not found: myagent. Available profiles: my-custom. "
        f"Could not read these profile files: {broken} ({BROKEN_REASON})"
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


def test_reading_the_directory_again_says_nothing_at_all(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The failure path re-reads the directory only to find out what it could not read.

    Every record that read produces has already been said when the registry loaded,
    and both levels count: the scan's own ERROR *and* the parse-time WARNING, which
    is the one a user actually sees twice.
    """
    (tmp_path / "legacy.yaml").write_text(LEGACY_HOOKS_PROFILE, encoding="utf-8")
    (tmp_path / "myagent.yaml").write_text(BROKEN_YAML, encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        read_profile_dir(tmp_path)

    assert [record.getMessage() for record in caplog.records] == []


def test_the_listing_still_reports_what_it_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Silencing the re-read must not silence the listing: the same directory still speaks."""
    (tmp_path / "legacy.yaml").write_text(LEGACY_HOOKS_PROFILE, encoding="utf-8")
    (tmp_path / "myagent.yaml").write_text(BROKEN_YAML, encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        scan_profile_dir(tmp_path)

    levels = [record.levelname for record in caplog.records]
    assert levels == ["WARNING", "ERROR"], "the dropped section and the skipped file are both reported"


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


def test_a_broken_model_file_is_named_alongside_the_available_list(tmp_path: Path) -> None:
    """``--model`` with two profiles sharing a name: the ids are the only way to pick one.

    Naming the unreadable file instead of the list would hide the duplicate, and the
    user would have no idea there was a choice to make at all.
    """
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="first-id", name="Same"))
    registry.register(ModelProfile(id="second-id", name="Same"))
    scan = ProfileDirScan[ModelProfile](
        invalid={tmp_path / "mymodel.yaml": ProfileFileIssue("YAML 2:1", BROKEN_REASON)}
    )

    error = run_cli._model_profile_not_found("Same", registry, scan)

    message = headless.exception_message(error)

    assert "Available model profiles: Same (first-id), Same (second-id)" in message
    assert "Could not read these profile files" in message
    assert message.index("Available model profiles") < message.index("Could not read"), "the list comes first"


def test_a_model_selector_with_nothing_to_offer_keeps_the_plain_message() -> None:
    scan = ProfileDirScan[ModelProfile]()

    error = run_cli._model_profile_not_found("mymodel", ModelProfileRegistry(), scan)

    assert headless.exception_message(error) == "Model profile not found: mymodel"


# --- directory-level read failure -------------------------------------------------


def test_an_unlistable_directory_is_named_with_its_os_error(tmp_path: Path) -> None:
    """A directory with no read permission hides every file inside it.

    ``invalid`` is empty in that case, so relying on it alone reports the selector
    as an ordinary miss.  The directory path and OS cause must be named instead.
    """
    directory = tmp_path / "agents"
    scan = ProfileDirScan[AgentProfile](
        directory=directory,
        directory_error=OSError(errno.EACCES, "Permission denied"),
    )

    notes = unreadable_notes(scan)

    assert notes is not None
    assert str(notes.values[0]) == f"{directory} (EACCES: Permission denied)"


def test_a_broken_file_and_a_directory_error_are_both_reported(tmp_path: Path) -> None:
    """The file cause and the directory cause answer different questions."""
    broken = tmp_path / "broken.yaml"
    directory = tmp_path / "agents"
    scan = ProfileDirScan[AgentProfile](
        invalid={broken: ProfileFileIssue("YAML 2:1", BROKEN_REASON)},
        directory=directory,
        directory_error=OSError(errno.EACCES, "Permission denied"),
    )

    notes = unreadable_notes(scan)

    assert notes is not None
    assert notes.values == (
        f"{broken} ({BROKEN_REASON})",
        f"{directory} (EACCES: Permission denied)",
    )


def test_a_clean_empty_scan_still_yields_no_notes(tmp_path: Path) -> None:
    scan = ProfileDirScan[AgentProfile](directory=tmp_path)

    assert unreadable_notes(scan) is None


# --- in-memory registry must not scan a host default ------------------------------


def test_an_in_memory_registry_never_re_reads_the_host_default(tmp_path: Path) -> None:
    """A registry built only by register() has no user directory to re-read.

    Re-reading the host default in that case blames a selector for broken files
    it never had anything to do with.
    """
    registry = AgentProfileRegistry()
    registry.register(_acp_profile_stub())
    hostile_default = tmp_path / "home-agents"
    hostile_default.mkdir()
    (hostile_default / "broken.yaml").write_text(BROKEN_YAML, encoding="utf-8")

    with patch.object(session_host_module, "read_profile_dir", wraps=read_profile_dir) as reread:
        error = agent_profile_not_found("ghost", registry)

    reread.assert_not_called()
    assert "Could not read" not in str(error)


def _acp_profile_stub() -> AgentProfile:
    return AgentProfile(name="built-in", instructions="Built in.")


# --- the read-only precheck load keeps the directory it scanned -------------------


def test_a_readonly_precheck_load_keeps_the_directory_it_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``icode run --workspace`` loads through the read-only path and then reports a miss.

    That load scans the real user directory through a throw-away view, so the scan
    does see the broken file — but the live registry used to come out of it with no
    user directory, and the miss report skips the re-read when there is none.  The
    file was therefore named for a plain ``run`` and silently dropped for
    ``--workspace``.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    broken = agents_dir / "broken.yaml"
    broken.write_text(BROKEN_YAML, encoding="utf-8")
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform_with_config_dir(tmp_path))

    registry = AgentProfileRegistry()
    registry.load_all_readonly()
    message = headless.exception_message(agent_profile_not_found("broken", registry))

    assert registry.user_directory == agents_dir, "the precheck load recorded what it scanned"
    assert str(broken) in message
    assert "Invalid YAML at line" in message


def test_a_readonly_precheck_load_names_no_file_when_every_file_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping the directory must not invent an unreadable file on a healthy machine."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "other.yaml").write_text(VALID_PROFILE, encoding="utf-8")
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform_with_config_dir(tmp_path))

    registry = AgentProfileRegistry()
    registry.load_all_readonly()
    message = headless.exception_message(agent_profile_not_found("ghost", registry))

    assert registry.user_directory == agents_dir
    assert "Could not read" not in message


# --- silent re-read covers the cross-module legacy-kind logger --------------------


LEGACY_APPROVAL_PROFILE = """\
name: legacy-approval
description: loads with a warning from another module
approval:
  overrides:
    chrys.shell: require
"""


def test_re_read_silences_the_cross_module_legacy_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Strip-legacy-kind logs from chrys.foundation.tool_kinds, not the loader.

    A filter on the loader logger alone leaves that WARNING audible on a
    diagnostic re-read, even though the initial load already reported it.
    """
    (tmp_path / "legacy.yaml").write_text(LEGACY_APPROVAL_PROFILE, encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        read_profile_dir(tmp_path)

    assert [record.getMessage() for record in caplog.records] == []


def test_the_listing_still_reports_the_cross_module_legacy_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Silencing the re-read must not silence the listing itself."""
    (tmp_path / "legacy.yaml").write_text(LEGACY_APPROVAL_PROFILE, encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="chrys.foundation.tool_kinds"):
        scan_profile_dir(tmp_path)

    assert any("legacy" in record.getMessage().lower() for record in caplog.records)
