# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The RESTART-scoped process snapshot: what it carries, and what it must not."""

from __future__ import annotations

import os
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.foundation.config import settings as settings_module
from chrys.foundation.config.process_settings import (
    ProcessSettings,
    freeze_process_settings,
    install_process_settings,
    process_settings,
    reset_process_settings,
    route_restart_settings,
    settle_session_root,
)
from chrys.foundation.config.settings import (
    SESSION_ROOT_DIR_ENV_VAR,
    Settings,
    probe_session_root,
    resolve_session_root_dir,
    resolve_sessions_dir,
)
from chrys.foundation.config.settings_store import LoadedSettings, load_settings
from chrys.foundation.config.spec import Apply, Source, specs_by_field
from chrys.foundation.config.warnings import settings_warning_events
from chrys.service.state.store import JsonFileStateStore


def _loaded(**overrides: object) -> LoadedSettings:
    """A bootstrap result carrying *overrides*, with no layer claiming them."""
    return LoadedSettings(settings=replace(Settings(), **overrides), provenance={})


def test_every_field_it_carries_is_declared_restart() -> None:
    """A RELOAD field read from here would keep a stale value for the process.

    This is the invariant that makes the snapshot safe at all, so it is checked
    against the specs rather than trusted to review.
    """
    specs = specs_by_field(Settings)

    for entry in fields(ProcessSettings):
        found = specs.get(entry.name)
        assert found is not None, f"{entry.name} is not a settings field"
        assert found.apply is Apply.RESTART, f"{entry.name} is {found.apply.name}, not RESTART"


# Every ``Apply.RESTART`` field the snapshot does *not* hold, and why. These
# are still fixed for the process — ``route_restart_settings`` holds them at
# the in-force values on every reload — but by routing rather than by a
# process-wide reader taking them from the snapshot, so the set is pinned
# here with the reason each one has no such reader.
_RESTART_FIELDS_OUTSIDE_THE_SNAPSHOT = {
    # Never comes from a file. The reload carries it across explicitly, as the
    # eval context it is an input to, so it is already fixed for the process.
    "frontend_default_max_transient_retries": "carried by eval_context",
    # Read once during startup and never again; the routing keeps the reload's
    # report agreeing with that read.
    "workspace_mru_max_entries": "read once when TUI services are wired",
    "approval_timeout_seconds": "ACP captures it at startup; TUI reads the restart-routed settings",
    "otel_enabled": "read once by setup_otel at bootstrap",
    "otel_sensitive_data": "read once by setup_otel at bootstrap",
    "otel_endpoint": "read once by setup_otel at bootstrap",
    "mutation_snapshot_max_file_mb": "policy captured when the snapshot store is first built",
    "mutation_snapshot_skip_binary": "policy captured when the snapshot store is first built",
    "update_check": "read once when the TUI starts, to decide whether it checks for a newer release",
}


def test_every_restart_field_is_either_frozen_or_listed_as_deliberately_not() -> None:
    """The reverse of the check above, which alone is satisfied by an empty snapshot.

    Declaring a field ``RESTART`` promises the process will not act on a new
    value until it restarts, and only the snapshot delivers that. This pins the
    exact set that carries the label without the guarantee, so a field cannot
    join it silently — and so the Apply-tier routing has a list to work from.
    """
    specs = specs_by_field(Settings)
    restart = {name for name, entry in specs.items() if entry.apply is Apply.RESTART}
    frozen = {entry.name for entry in fields(ProcessSettings)}

    unaccounted = restart - frozen - set(_RESTART_FIELDS_OUTSIDE_THE_SNAPSHOT)
    assert unaccounted == set(), f"declared RESTART but neither frozen nor explained: {sorted(unaccounted)}"

    stale = set(_RESTART_FIELDS_OUTSIDE_THE_SNAPSHOT) - restart
    assert stale == set(), f"listed as unfrozen RESTART but no longer RESTART: {sorted(stale)}"

    overlap = frozen & set(_RESTART_FIELDS_OUTSIDE_THE_SNAPSHOT)
    assert overlap == set(), f"both frozen and listed as unfrozen: {sorted(overlap)}"


def test_routing_holds_every_restart_field_and_lists_the_deferrals() -> None:
    """A reload may neither apply a RESTART value nor silently discard it."""
    install_process_settings(_loaded(raw_http_capture=True, otel_enabled=False))
    in_force = _loaded(raw_http_capture=True, otel_enabled=False)
    candidate = _loaded(raw_http_capture=False, otel_enabled=True, dev_mode=True)

    routed, deferred = route_restart_settings(candidate, in_force)

    # A snapshot field keeps the bootstrap value its readers hold; a RESTART
    # field with no process-wide reader is held at the value in force; a
    # RELOAD field passes through, because the rebuild genuinely re-reads it.
    assert routed.settings.raw_http_capture is True
    assert routed.settings.otel_enabled is False
    assert routed.settings.dev_mode is True
    assert deferred == ("otel.enabled", "log.raw_http_capture")


def test_routing_restores_the_in_force_origin_with_the_value() -> None:
    """Value and origin describe one decision, so they are held together."""
    install_process_settings(_loaded())
    in_force = _loaded(otel_enabled=False).overlay(Source.CLI, otel_enabled=False)
    candidate = load_settings(env={"CHRYS_OTEL": "1"})
    assert candidate.source_for("otel.enabled").layer is Source.ENV

    routed, deferred = route_restart_settings(candidate, in_force)

    assert routed.settings.otel_enabled is False
    assert routed.source_for("otel.enabled").layer is Source.CLI
    assert deferred == ("otel.enabled",)


def test_routing_reports_nothing_when_the_reload_changes_nothing() -> None:
    install_process_settings(_loaded())

    routed, deferred = route_restart_settings(_loaded(), _loaded())

    assert routed.settings == Settings()
    assert deferred == ()


def test_routing_is_a_noop_before_bootstrap() -> None:
    """Uninstalled means unheld: tests and tooling keep seeing what they load."""
    candidate = _loaded(otel_enabled=True)

    routed, deferred = route_restart_settings(candidate, _loaded())

    assert routed is candidate
    assert deferred == ()


def test_the_freeze_carries_the_unknown_keys_report_through() -> None:
    """A reload re-reports unknown keys; the freeze must not eat the list."""
    install_process_settings(_loaded())
    candidate = LoadedSettings(settings=Settings(), provenance={}, unknown_keys=("mystery.key",))

    assert freeze_process_settings(candidate).unknown_keys == ("mystery.key",)


def test_reads_fall_back_to_the_environment_until_bootstrap_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests and tooling never bootstrap, so the uninstalled path has to work."""
    monkeypatch.setenv("CHRYS_DEBUG_LLM_RAW_HTTP_LOG", "1")
    reset_process_settings()

    assert process_settings().raw_http_capture is True


def test_an_installed_snapshot_outranks_a_later_environment_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """That is what RESTART means: changing it now does not reach this process."""
    install_process_settings(_loaded(raw_http_capture=True))
    monkeypatch.delenv("CHRYS_DEBUG_LLM_RAW_HTTP_LOG", raising=False)

    assert process_settings().raw_http_capture is True

    reset_process_settings()

    assert process_settings().raw_http_capture is False


def test_the_session_root_reaches_the_resolver_through_the_snapshot(tmp_path: Path) -> None:
    """The field existed with nothing reading it, so it configured nothing.

    Twelve call sites resolve a sessions directory and none of them holds a
    ``Settings``; the snapshot is what connects the declared field to them.
    """
    root = tmp_path / "elsewhere"
    install_process_settings(_loaded(session_root_dir=str(root)))

    assert resolve_session_root_dir(tmp_path / "config") == root
    assert resolve_sessions_dir(tmp_path / "config") == root / "sessions"


def test_an_unusable_session_root_falls_back_to_the_config_dir(tmp_path: Path) -> None:
    """A path that is a file cannot hold sessions; losing them is not an option."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")
    config_dir = tmp_path / "config"
    install_process_settings(_loaded(session_root_dir=str(blocked)))

    assert resolve_session_root_dir(config_dir) == config_dir


def test_settling_makes_the_settings_agree_with_where_sessions_actually_go(tmp_path: Path) -> None:
    """Otherwise the panel shows an effective value nothing in the process uses.

    The loader cannot judge a path — that answer is on disk — so it accepted the
    variable and the consumer quietly fell back, leaving the settings object and
    its provenance describing a layer that had in fact lost.
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")
    loaded = load_settings(env={"CHRYS_SESSION_ROOT_DIR": str(blocked)})
    assert loaded.source_for("storage.session_root_dir").layer is Source.ENV

    settled = settle_session_root(loaded)

    assert settled.settings.session_root_dir == ""
    assert settled.source_for("storage.session_root_dir").layer is Source.DEFAULT
    assert [warning.key for warning in settled.warnings] == ["storage.session_root_dir"]
    assert str(blocked) in settings_warning_events(settled)[0].message


# --- the environment path into the resolver: CHRYS_SESSION_ROOT_DIR before bootstrap installs ---


def _patch_platform_config(monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:
    fake_platform = type("P", (), {"config_dir": config_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)


def test_resolve_session_root_dir_defaults_to_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.delenv(SESSION_ROOT_DIR_ENV_VAR, raising=False)
    _patch_platform_config(monkeypatch, config_dir)

    root = resolve_session_root_dir()
    sessions_dir = resolve_sessions_dir()

    assert root == config_dir
    assert sessions_dir == config_dir / "sessions"
    assert sessions_dir.is_dir()
    assert JsonFileStateStore().session_dir("sess1").parent == sessions_dir


def test_resolve_sessions_dir_appends_sessions_to_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(custom_root))
    _patch_platform_config(monkeypatch, config_dir)

    root = resolve_session_root_dir()
    sessions_dir = resolve_sessions_dir()

    assert root == custom_root
    assert sessions_dir == custom_root / "sessions"
    assert sessions_dir.is_dir()
    assert JsonFileStateStore().session_dir("sess1").parent == sessions_dir


def test_resolve_session_root_dir_falls_back_when_env_points_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("", encoding="utf-8")
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(not_a_directory))
    _patch_platform_config(monkeypatch, config_dir)

    root = resolve_session_root_dir()
    sessions_dir = resolve_sessions_dir()

    assert root == config_dir
    assert sessions_dir == config_dir / "sessions"
    assert sessions_dir.is_dir()
    assert JsonFileStateStore().session_dir("sess1").parent == sessions_dir


def test_resolve_sessions_dir_falls_back_when_env_sessions_path_is_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    custom_root = tmp_path / "custom-root"
    custom_root.mkdir()
    (custom_root / "sessions").write_text("", encoding="utf-8")
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(custom_root))
    _patch_platform_config(monkeypatch, config_dir)

    sessions_dir = resolve_sessions_dir()

    assert sessions_dir == config_dir / "sessions"
    assert sessions_dir.is_dir()
    assert JsonFileStateStore().session_dir("sess1").parent == sessions_dir


def test_resolve_sessions_dir_caches_successful_write_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_module, "_RESOLVED_SESSIONS_DIR_CACHE", {})
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(custom_root))
    calls = 0
    real_temporary_file = settings_module.tempfile.TemporaryFile

    def counting_temporary_file(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return real_temporary_file(*args, **kwargs)

    monkeypatch.setattr(settings_module.tempfile, "TemporaryFile", counting_temporary_file)

    first = resolve_sessions_dir()
    second = resolve_sessions_dir()

    assert first == custom_root / "sessions"
    assert second == first
    assert calls == 1


def _link(link: Path, target: Path, *, to_a_directory: bool) -> None:
    """Create *link*, skipping the test where the platform will not have it.

    Windows needs a privilege the workers may not hold, and needs to be told
    whether the target is a directory — it makes a file link otherwise, which is
    a different shape than the one under test even where the call succeeds.
    """
    try:
        link.symlink_to(target, target_is_directory=to_a_directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")


def _unmapped_drive() -> Path:
    """A drive letter nothing is mounted on, for a path whose anchor is missing.

    Windows is where a path can bottom out at something that is not there —
    ``Z:\\`` has no parent above it, so a walk up the components ends without
    ever finding a directory. POSIX has no equivalent: the walk ends at ``/``.
    """
    for letter in "ZYXWV":
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            return drive
    pytest.skip("every drive letter is mapped here")


def _build_root(tmp_path: Path, shape: str) -> Path:
    """The root a shape names, laid out on disk where the shape puts something there.

    Some shapes are about what is at the path and some are about the path
    itself, so the shape picks the name as well as what is under it.
    """
    if shape == "root_name_is_too_long":
        return tmp_path / ("n" * 300)
    if shape == "root_contains_a_nul":
        return tmp_path / "ro\0ot"
    if shape == "long_name_below_a_missing_parent":
        return tmp_path / "missing" / ("n" * 300)
    if shape == "root_is_under_a_file":
        (tmp_path / "occupied").write_text("", encoding="utf-8")
        return tmp_path / "occupied" / "root"
    if shape == "root_is_under_a_dangling_link":
        _link(tmp_path / "parent", tmp_path / "gone", to_a_directory=True)
        return tmp_path / "parent" / "root"
    if shape == "root_is_on_a_missing_volume":
        return _unmapped_drive() / "root"

    root = tmp_path / "root"
    if shape == "absent":
        return root
    if shape == "root_is_a_file":
        root.write_text("", encoding="utf-8")
    elif shape == "root_is_a_dangling_link":
        _link(root, root.parent / "gone", to_a_directory=True)
    elif shape == "root_links_to_a_directory":
        (root.parent / "target").mkdir()
        _link(root, root.parent / "target", to_a_directory=True)
    elif shape == "child_is_a_file":
        root.mkdir()
        (root / "sessions").write_text("", encoding="utf-8")
    elif shape == "child_is_a_dangling_link":
        root.mkdir()
        _link(root / "sessions", root / "gone", to_a_directory=True)
    elif shape == "child_is_a_directory":
        root.mkdir()
        (root / "sessions").mkdir()
    else:  # pragma: no cover - a shape name that does not exist is a test bug
        raise AssertionError(shape)
    return root


@pytest.mark.parametrize(
    ("name", "encoded_length", "refused"),
    [
        pytest.param("n" * 251, 251, False, id="at-limit"),
        pytest.param("n" * 252, 252, True, id="over-limit"),
        pytest.param(os.fsdecode("界".encode() * 84), 252, True, id="multibyte-over-limit"),
    ],
)
def test_missing_name_is_checked_against_filesystem_name_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    encoded_length: int,
    refused: bool,
) -> None:
    """An ambiguous ENOENT is rejected only when the encoded name exceeds the mount's limit."""

    def missing(_path: Path) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "lstat", missing)
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_namemax=251), raising=False)

    assert len(os.fsencode(name)) == encoded_length
    assert settings_module._name_is_refused(tmp_path, name) is refused


def test_missing_name_is_not_refused_when_filesystem_limit_query_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failure to query the mount keeps the structural probe conservative."""

    def missing(_path: Path) -> None:
        raise FileNotFoundError

    def statvfs_fails(_path: Path) -> None:
        raise OSError

    monkeypatch.setattr(Path, "lstat", missing)
    monkeypatch.setattr(os, "statvfs", statvfs_fails, raising=False)

    assert settings_module._name_is_refused(tmp_path, "n" * 252) is False


@pytest.mark.parametrize(
    ("shape", "kept"),
    [
        # Nothing there yet: created at first use, so settling must not reject it.
        ("absent", True),
        ("root_is_a_file", False),
        # ``exists()`` follows the link and calls these absent; the mkdir that
        # follows then fails on an entry that is very much there.
        ("root_is_a_dangling_link", False),
        ("root_links_to_a_directory", True),
        ("child_is_a_file", False),
        ("child_is_a_dangling_link", False),
        ("child_is_a_directory", True),
        # Names the system will not take. ``exists``/``lexists`` answer False for
        # all three — the same False they give a name that is merely free — so
        # each one reads as creatable right up until the mkdir that isn't.
        ("root_name_is_too_long", False),
        # Same name, one level further down than resolution ever gets: asking
        # about the whole path stops at the parent that is not there.
        ("long_name_below_a_missing_parent", False),
        ("root_contains_a_nul", False),
        ("root_is_under_a_file", False),
        # ``lstat`` stops following at the last component only, so anything the
        # path passes through on the way down is still resolved, and a parent
        # that leads nowhere makes the root itself read as merely absent.
        ("root_is_under_a_dangling_link", False),
        # And the walk up has to end somewhere: on Windows that is the drive or
        # share, which can be as absent as anything under it.
        ("root_is_on_a_missing_volume", False),
    ],
)
def test_settling_reaches_the_same_verdict_as_the_resolver(tmp_path: Path, shape: str, kept: bool) -> None:
    """Settling and the resolver agree over every shape settling claims to judge.

    Settling exists so that what the settings say is where sessions go, and the
    way it fails is always the same — a root the resolver quietly abandons while
    the settings go on naming it. What is enumerated here is the part it can
    decide by looking: whether the system takes the path, and whether anything
    already there rules out a directory. Whether the write would be *permitted*
    is deliberately outside this — see :func:`session_root_is_ruled_out` — so
    read-only and ACL shapes are absent by design rather than by oversight.
    """
    if shape == "root_is_on_a_missing_volume" and os.name != "nt":
        pytest.skip("only a Windows path can bottom out at an anchor that is not there")

    root = _build_root(tmp_path, shape)
    settled = settle_session_root(load_settings(env={"CHRYS_SESSION_ROOT_DIR": str(root)}))
    install_process_settings(settled)

    assert (settled.settings.session_root_dir == str(root)) is kept
    # The half that matters: whichever way settling went, storage agrees.
    assert (resolve_sessions_dir(tmp_path / "config") == root / "sessions") is kept
    assert bool(settled.warnings) is not kept


def test_settling_judges_the_sessions_child_the_root_is_only_used_through(tmp_path: Path) -> None:
    """A usable root whose ``sessions`` child is not is still a root nothing writes to.

    ``resolve_sessions_dir`` falls back to the default location when the child
    cannot be used, so judging only the root reproduces one level down exactly
    the divergence settling exists to remove: settings naming a directory, and a
    log line as the only sign that sessions went somewhere else.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "sessions").write_text("", encoding="utf-8")
    loaded = load_settings(env={"CHRYS_SESSION_ROOT_DIR": str(root)})
    assert loaded.source_for("storage.session_root_dir").layer is Source.ENV

    settled = settle_session_root(loaded)

    assert settled.settings.session_root_dir == ""
    assert settled.source_for("storage.session_root_dir").layer is Source.DEFAULT
    assert [warning.key for warning in settled.warnings] == ["storage.session_root_dir"]


def test_settling_leaves_a_root_whose_sessions_child_is_yet_to_exist(tmp_path: Path) -> None:
    """The child is created on first use, exactly like the root above it."""
    root = tmp_path / "root"
    root.mkdir()
    loaded = settle_session_root(load_settings(env={"CHRYS_SESSION_ROOT_DIR": str(root)}))

    assert loaded.settings.session_root_dir == str(root)
    assert loaded.warnings == ()


def test_settling_leaves_a_root_that_does_not_exist_yet_alone(tmp_path: Path) -> None:
    """It is created on first use; rejecting it here would break lazy storage."""
    root = tmp_path / "made-later"
    loaded = settle_session_root(load_settings(env={"CHRYS_SESSION_ROOT_DIR": str(root)}))

    assert loaded.settings.session_root_dir == str(root)
    assert loaded.warnings == ()


# ── probe_session_root: the panel's create-and-write check ─────────────


def test_probing_a_blank_root_lands_on_the_default_sessions_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank value means the default location, whatever the current directory holds.

    The ruled-out check parses ``""`` as the current directory, so a *file*
    named ``sessions`` sitting there would reject the one value that never
    goes near it. The blank case is therefore probed at the default root
    directly.
    """
    import dataclasses

    from chrys.foundation import platform as platform_mod

    config_dir = tmp_path / "config"
    fake = dataclasses.replace(platform_mod.get_platform(), config_dir=config_dir)
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "sessions").write_text("", encoding="utf-8")
    monkeypatch.chdir(cwd)

    probed = probe_session_root("   ")

    assert probed is not None
    assert probed == config_dir / "sessions"
    assert probed.is_dir()


def test_probing_a_usable_root_creates_its_sessions_child(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    probed = probe_session_root(str(root))

    assert probed is not None
    assert probed == root / "sessions"
    assert probed.is_dir()


def test_probing_a_padded_root_checks_the_value_that_will_be_saved(tmp_path: Path) -> None:
    """The panel persists the trimmed string; a probe of ``"<root> "`` must not
    create and validate a differently named ``<root >/sessions`` instead."""
    root = tmp_path / "root"
    root.mkdir()

    probed = probe_session_root(f"  {root}  ")

    assert probed == root / "sessions"
    assert probed.is_dir()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["root"]


def test_probing_a_ruled_out_root_answers_none(tmp_path: Path) -> None:
    (tmp_path / "occupied").write_text("", encoding="utf-8")

    assert probe_session_root(str(tmp_path / "occupied" / "root")) is None
    assert probe_session_root("ro\0ot") is None


def test_probing_a_root_whose_sessions_child_is_a_file_fails(tmp_path: Path) -> None:
    """Past the ruled-out check by nothing: the child sits exactly where storage goes."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "sessions").write_text("", encoding="utf-8")

    assert probe_session_root(str(root)) is None


def test_a_reload_reports_the_value_this_process_will_actually_use() -> None:
    """A reload re-reads a RESTART key, but nothing in the process re-reads it.

    Without the freeze the reloaded settings say capture is on, the snapshot
    every consumer holds says it is off, and the panel shows the one that is
    not running.
    """
    install_process_settings(load_settings(env={}))

    reloaded = load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "1"})
    assert reloaded.settings.raw_http_capture is True

    frozen = freeze_process_settings(reloaded)

    assert frozen.settings.raw_http_capture is process_settings().raw_http_capture is False


def test_the_freeze_credits_the_layer_the_value_in_force_came_from() -> None:
    """Restoring the value but not its origin is the same lie one step along.

    Here the variable is gone by the time of the reload, so the reloaded
    provenance says DEFAULT — of a value that came from the environment and is
    still in force.
    """
    install_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "1"}))

    frozen = freeze_process_settings(load_settings(env={}))

    assert frozen.settings.raw_http_capture is True
    assert frozen.source_for("log.raw_http_capture").layer is Source.ENV


def test_the_freeze_drops_a_seal_the_reload_invented() -> None:
    """Value, origin and seal describe one decision and travel together.

    The reload's typo seals the key, but this process is still running the good
    value the environment supplied at bootstrap. A seal carried across would
    mark an ENV-sourced value as the built-in default.
    """
    install_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "1"}))

    frozen = freeze_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"}))

    assert frozen.settings.raw_http_capture is True
    assert frozen.source_for("log.raw_http_capture").layer is Source.ENV
    assert frozen.sealed_keys == frozenset()


def test_the_freeze_keeps_a_seal_the_reload_would_have_lifted() -> None:
    """The other direction: the default is still standing in for a refused value.

    Fixing the typo does not un-refuse it for this process, so the explanation
    the panel needs — "this is the built-in default, on purpose" — has to
    survive the reload that made the file valid again.
    """
    install_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"}))

    frozen = freeze_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "1"}))

    assert frozen.settings.raw_http_capture is False
    assert frozen.source_for("log.raw_http_capture").layer is Source.DEFAULT
    assert frozen.sealed_keys == frozenset({"log.raw_http_capture"})


def test_the_freeze_leaves_seals_on_keys_it_does_not_hold() -> None:
    """Only the snapshot's own keys are its business."""
    install_process_settings(load_settings(env={}))

    frozen = freeze_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"}))

    assert frozen.sealed_keys == frozenset()

    install_process_settings(load_settings(env={}))
    frozen = freeze_process_settings(load_settings(env={"CHRYS_TOOL_RESULT_CEILING_TOKENS": "-1"}))

    assert frozen.sealed_keys == frozenset({"tools.result.ceiling_tokens"})


def test_a_frozen_seal_always_still_reports_the_default_as_its_source() -> None:
    """The panel-facing invariant, checked across every bootstrap/reload pairing.

    ``sealed`` means "the built-in default is deliberately what is running", so
    a sealed key that names a layer is a contradiction however it was reached.
    """
    for bootstrap_value, reload_value in (("1", "fales"), ("fales", "1"), ("1", "1"), ("fales", "fales")):
        install_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": bootstrap_value}))
        frozen = freeze_process_settings(load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": reload_value}))

        for key in frozen.sealed_keys:
            assert frozen.source_for(key).layer is Source.DEFAULT, (bootstrap_value, reload_value, key)


def test_the_freeze_still_reports_a_restart_value_that_was_typed_wrong() -> None:
    """The value cannot take effect yet; the typo is still news to the user."""
    install_process_settings(load_settings(env={}))

    frozen = freeze_process_settings(load_settings(env={"CHRYS_TUI_FILE_SNAPSHOT_INLINE_CHARS": "lots"}))

    assert [warning.key for warning in frozen.warnings] == ["ui.chat.file_snapshot_inline_chars"]


def test_the_freeze_leaves_reload_scoped_fields_alone() -> None:
    """It exists to describe what is fixed, not to fix more than the snapshot."""
    install_process_settings(load_settings(env={}))

    frozen = freeze_process_settings(load_settings(env={"CHRYS_SESSION_TITLE_AUTO": "0"}))

    assert frozen.settings.session_title_auto is False
    assert frozen.source_for("session.title.auto").layer is Source.ENV


def test_nothing_is_frozen_before_bootstrap_installs() -> None:
    """With no snapshot taken, no reader can be holding a superseded value."""
    reset_process_settings()
    loaded = load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "1"})

    assert freeze_process_settings(loaded) is loaded
