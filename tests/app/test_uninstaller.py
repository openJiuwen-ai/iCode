# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from chrys.app import uninstaller
from chrys.app.install_flavor import InstallFlavor
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.config.settings import SESSION_ROOT_DIR_ENV_VAR
from chrys.foundation.platform import get_platform
from chrys.kernel.aixcoding.command_migration import LEGACY_APP_COMMAND
from tests.support.symlinks import junction_or_skip

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises the POSIX layout (~/.local/bin); Path.home() ignores HOME on Windows.",
)


class _FakeStdin:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@dataclass
class _Machine:
    home: Path
    bin_dir: Path
    project_dir: Path
    config_dir: Path
    cache_dir: Path
    preflight: mock.MagicMock

    @property
    def preflights(self) -> list[dict[str, Any]]:
        return [call.kwargs for call in self.preflight.call_args_list]


def _machine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    flavor: InstallFlavor = InstallFlavor.OFFLINE,
    is_windows: bool = False,
) -> _Machine:
    """An offline install as ``icode install`` and PyApp leave it, with the usual data folder."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "chrys").write_text("binary", encoding="utf-8")
    (bin_dir / APP_COMMAND).symlink_to("chrys")

    project_dir = tmp_path / "pyapp" / "chrys"
    exe = project_dir / "dist-new" / "0.30.0" / "python" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("python", encoding="utf-8")
    (project_dir / "dist-old" / "0.29.1").mkdir(parents=True)

    cache_dir = tmp_path / "pyapp-cache"
    (cache_dir / "distributions").mkdir(parents=True)
    (cache_dir / "locks").mkdir()
    for name in ("dist-new", "dist-old", "dist-of-another-app"):
        (cache_dir / "distributions" / name).write_text("archive", encoding="utf-8")
    (cache_dir / "distributions" / "_dist-new").mkdir()
    # The second is another PyApp app whose name starts with this one's.
    for name in (
        "installation-chrys-dist-new-0.30.0",
        "installation-chrys-manager-15856504249695659915-0.6.3",
        "installation-other-x-1.0",
    ):
        (cache_dir / "locks" / name).write_text("", encoding="utf-8")

    # The per-test config dir, so the settings files there are the ones the uninstaller reads.
    config_dir = get_platform().config_dir
    (config_dir / "sessions").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYAPP", str(bin_dir / "chrys"))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(uninstaller, "detect_install_flavor", lambda: flavor)
    monkeypatch.setattr(
        uninstaller, "get_platform", lambda: SimpleNamespace(is_windows=is_windows, config_dir=config_dir)
    )
    monkeypatch.setattr(uninstaller, "_pyapp_cache_dir", lambda: cache_dir)
    preflight = mock.create_autospec(uninstaller._require_no_running_chrys_instances, return_value=True)
    monkeypatch.setattr(uninstaller, "_require_no_running_chrys_instances", preflight)
    return _Machine(home, bin_dir, project_dir, config_dir, cache_dir, preflight)


@_POSIX_ONLY
def test_offline_uninstall_removes_the_install_and_keeps_the_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert sorted(path.name for path in machine.bin_dir.iterdir()) == []
    assert not machine.project_dir.exists()
    assert machine.project_dir.parent.is_dir()
    assert sorted(path.name for path in (machine.cache_dir / "distributions").iterdir()) == [
        "_dist-new",
        "dist-of-another-app",
    ]
    assert sorted(path.name for path in (machine.cache_dir / "locks").iterdir()) == [
        "installation-chrys-manager-15856504249695659915-0.6.3",
        "installation-other-x-1.0",
    ]
    assert (machine.config_dir / "sessions").is_dir()
    # Waiting for other instances needs a terminal or --yes, so the command to rerun has --yes.
    assert machine.preflights == [
        {"ignored_pids": {os.getpid()}, "action": "uninstall", "rerun": f"{APP_COMMAND} uninstall --yes"}
    ]
    out = capsys.readouterr().out
    assert f"Success: Removed {APP_DISPLAY_NAME}." in out
    assert f"Your settings, sessions and other data are kept in {machine.config_dir}." in out
    # The command that could purge them is gone.
    assert "Delete that folder if you no longer need them." in out
    assert "--purge" not in out


@_POSIX_ONLY
def test_offline_uninstall_spares_an_icode_command_that_starts_something_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    foreign = machine.bin_dir / APP_COMMAND
    foreign.unlink()
    foreign.write_text("uv entry point", encoding="utf-8")
    download = tmp_path / "Downloads" / "icode"
    download.parent.mkdir()
    download.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("PYAPP", str(download))

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert [path.name for path in machine.bin_dir.iterdir()] == [APP_COMMAND]
    assert foreign.read_text(encoding="utf-8") == "uv entry point"
    assert download.is_file()
    assert f"The package you ran this from is still at {download}" in capsys.readouterr().out


@_POSIX_ONLY
def test_offline_uninstall_removes_legacy_and_current_own_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # AIxCoding branding: legacy alias uninstall tests
    machine = _machine(monkeypatch, tmp_path)
    (machine.bin_dir / LEGACY_APP_COMMAND).symlink_to("chrys")

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert sorted(path.name for path in machine.bin_dir.iterdir()) == []


@_POSIX_ONLY
def test_offline_uninstall_spares_an_external_legacy_icode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # AIxCoding branding: legacy alias uninstall tests
    machine = _machine(monkeypatch, tmp_path)
    foreign = machine.bin_dir / LEGACY_APP_COMMAND
    foreign.write_text("uv entry point", encoding="utf-8")

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert [path.name for path in machine.bin_dir.iterdir()] == [LEGACY_APP_COMMAND]
    assert foreign.read_text(encoding="utf-8") == "uv entry point"


@_POSIX_ONLY
def test_offline_uninstall_spares_a_dangling_legacy_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A dangling legacy symlink proves nothing about our chrys, so it stays (F-2).
    machine = _machine(monkeypatch, tmp_path)
    legacy = machine.bin_dir / LEGACY_APP_COMMAND
    legacy.symlink_to("removed")
    assert not legacy.exists()

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert legacy.is_symlink()


@_POSIX_ONLY
@pytest.mark.parametrize("kind", ["linked-by-uv", "pip-script"])
def test_offline_uninstall_spares_a_chrys_command_another_installer_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    chrys = machine.bin_dir / "chrys"
    chrys.unlink()
    if kind == "linked-by-uv":
        target = tmp_path / "uv" / "tools" / "chrys" / "bin" / "chrys"
        target.parent.mkdir(parents=True)
        target.write_text("binary", encoding="utf-8")
        chrys.symlink_to(target)
    else:
        chrys.write_text("#!/usr/bin/python3\nfrom chrys.app.cli.app import main\n", encoding="utf-8")
    download = tmp_path / "Downloads" / "icode"
    download.parent.mkdir()
    download.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("PYAPP", str(download))

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    # Its `icode` alias names that command, so it stays too; the runtime this ran from goes.
    assert sorted(path.name for path in machine.bin_dir.iterdir()) == sorted([APP_COMMAND, "chrys"])
    assert not machine.project_dir.exists()


@_POSIX_ONLY
def test_purge_also_deletes_the_data_and_only_the_sessions_of_a_custom_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    custom_root = tmp_path / "elsewhere"
    (custom_root / "sessions" / "abc").mkdir(parents=True)
    (custom_root / "notes.txt").write_text("the user's own", encoding="utf-8")
    # Where the settings panel saves it; the suite's own root comes from the environment, which
    # would outrank the file.
    monkeypatch.delenv(SESSION_ROOT_DIR_ENV_VAR)
    (machine.config_dir / "settings.yaml").write_text(
        f"storage:\n  session_root_dir: {json.dumps(str(custom_root))}\n", encoding="utf-8"
    )

    assert uninstaller.uninstall(purge=True, assume_yes=True) == 0

    assert not machine.config_dir.exists()
    assert not (custom_root / "sessions").exists()
    assert (custom_root / "notes.txt").is_file()
    assert machine.preflights[0]["rerun"] == f"{APP_COMMAND} uninstall --purge --yes"
    out = capsys.readouterr().out
    assert f"  {machine.config_dir} and everything in it, including your settings, API keys," in out
    assert f"  {custom_root / 'sessions'} (your sessions)\n" in out
    assert "are kept in" not in out


def test_a_junctioned_data_folder_is_described_as_only_the_link(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing a junction, like a link, leaves the folder it leads to; the prompt says so."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    entry = tmp_path / "chrys"
    junction_or_skip(entry, target)

    uninstaller._describe(None, [entry], entry)

    out = capsys.readouterr().out
    assert "(only the link:" in out
    assert "everything in it" not in out


def test_a_data_folder_whose_real_path_cannot_be_worked_out_is_kept_by_its_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows ``realpath`` raises for a network share that cannot be reached;
    uninstall still runs and names the data folder as given."""
    named = get_platform().config_dir
    named.mkdir(parents=True, exist_ok=True)
    resolve = os.path.realpath

    def unreachable(path: str | os.PathLike[str], *, strict: bool = False) -> str:
        if Path(path) == named:
            raise OSError(0, "The specified network name is no longer available", None, 64)
        return resolve(path, strict=strict)

    monkeypatch.setattr(os.path, "realpath", unreachable)
    monkeypatch.setattr(uninstaller, "conventional_config_dir", lambda: named)
    monkeypatch.setattr(uninstaller, "detect_install_flavor", lambda: InstallFlavor.UV)

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0
    assert f"are kept in {named}.\n" in capsys.readouterr().out


@_POSIX_ONLY
def test_purge_of_a_linked_data_folder_removes_only_the_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    dotfiles = tmp_path / "dotfiles" / "chrys"
    dotfiles.parent.mkdir()
    machine.config_dir.rename(dotfiles)
    link = machine.home / ".chrys"
    link.symlink_to(dotfiles)
    # As the platform reports it: the folder the link leads to, not the link.
    monkeypatch.setattr(
        uninstaller, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=dotfiles.resolve())
    )

    assert uninstaller.uninstall(purge=True, assume_yes=True) == 0

    assert not link.is_symlink()
    assert (dotfiles / "sessions").is_dir()
    out = capsys.readouterr().out
    assert f"  {link} (only the link: {dotfiles.resolve()}, the folder it points to, stays)" in out
    assert "everything in it" not in out


@_POSIX_ONLY
def test_purge_leaves_a_relative_session_root_alone_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    # Relative to whatever folder iCode starts in, so this one is only a guess.
    (Path.cwd() / "relative-root" / "sessions").mkdir(parents=True)
    monkeypatch.delenv(SESSION_ROOT_DIR_ENV_VAR)
    (machine.config_dir / "settings.yaml").write_text("storage:\n  session_root_dir: relative-root\n", encoding="utf-8")

    assert uninstaller.uninstall(purge=True, assume_yes=True) == 0

    assert not machine.config_dir.exists()
    assert (Path.cwd() / "relative-root" / "sessions").is_dir()
    out = capsys.readouterr().out
    assert (
        f"Sessions are saved in {Path('relative-root', 'sessions')}, relative to the folder {APP_DISPLAY_NAME} starts "
        "in, so they are not deleted. Delete those folders yourself."
    ) in out
    assert "(your sessions)" not in out


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("flavor", "command"),
    [(InstallFlavor.UV, "uv tool uninstall iCode-TUI"), (InstallFlavor.PIPX, "pipx uninstall iCode-TUI")],
)
def test_other_installers_only_get_their_uninstall_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flavor: InstallFlavor,
    command: str,
) -> None:
    machine = _machine(monkeypatch, tmp_path, flavor=flavor)

    assert uninstaller.uninstall(purge=False, assume_yes=False) == 0

    assert (machine.bin_dir / "chrys").is_file()
    assert machine.project_dir.is_dir()
    assert machine.config_dir.is_dir()
    assert machine.preflights == []
    out = capsys.readouterr().out
    assert f"installed with {flavor.value}. To remove it, run:\n  {command}\n" in out
    assert f"To delete them too, run '{APP_COMMAND} uninstall --purge'." in out


@_POSIX_ONLY
def test_purge_under_another_installer_deletes_the_data_then_names_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path, flavor=InstallFlavor.UV)

    assert uninstaller.uninstall(purge=True, assume_yes=True) == 0

    assert not machine.config_dir.exists()
    assert machine.project_dir.is_dir()
    assert "uv tool uninstall iCode-TUI" in capsys.readouterr().out


@_POSIX_ONLY
def test_a_source_checkout_has_nothing_to_uninstall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _machine(monkeypatch, tmp_path, flavor=InstallFlavor.SOURCE)

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert "runs from a source checkout" in capsys.readouterr().out


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("tty", "answer"),
    [(False, None), (True, ""), (True, "n"), (True, EOFError), (True, KeyboardInterrupt)],
    ids=["no-tty", "enter", "no", "ctrl-d", "ctrl-c"],
)
def test_nothing_is_removed_without_a_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tty: bool,
    answer: str | type[BaseException] | None,
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    monkeypatch.setattr(uninstaller.sys, "stdin", _FakeStdin(tty=tty))
    prompts: list[str] = []

    def reply(prompt: str) -> str:
        prompts.append(prompt)
        if answer is None:
            pytest.fail("a missing terminal must not prompt")
        if not isinstance(answer, str):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", reply)

    assert uninstaller.uninstall(purge=True, assume_yes=False) == 1

    assert (machine.bin_dir / "chrys").is_file()
    assert machine.project_dir.is_dir()
    assert machine.config_dir.is_dir()
    assert prompts == ([] if answer is None else ["Continue? [y/N] "])
    out = capsys.readouterr().out
    assert "Nothing was removed." in out
    if answer is None:
        assert f"run '{APP_COMMAND} uninstall --purge --yes'" in out
        # Refused before waiting for other instances, which would need a terminal too.
        assert machine.preflights == []
    else:
        assert len(machine.preflights) == 1


@_POSIX_ONLY
def test_the_prompt_accepts_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    machine = _machine(monkeypatch, tmp_path)
    monkeypatch.setattr(uninstaller.sys, "stdin", _FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "Yes")

    assert uninstaller.uninstall(purge=False, assume_yes=False) == 0

    assert not machine.project_dir.exists()


@_POSIX_ONLY
def test_a_failed_removal_is_reported_and_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    real_rmtree = uninstaller.shutil.rmtree

    def rmtree(path: str | Path, *, onexc: Callable[..., object] | None = None) -> None:
        if Path(path) == machine.project_dir:
            raise PermissionError("denied")
        real_rmtree(path, onexc=onexc)

    monkeypatch.setattr(uninstaller.shutil, "rmtree", rmtree)

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 1

    out = capsys.readouterr().out
    assert f"Warning: Could not remove {machine.project_dir}: denied" in out
    assert "Success" not in out


@_POSIX_ONLY
def test_an_install_dir_override_leaves_the_runtime_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine = _machine(monkeypatch, tmp_path)
    monkeypatch.setenv("PYAPP_INSTALL_DIR_CHRYS", str(machine.project_dir))

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    assert machine.project_dir.is_dir()
    assert (machine.cache_dir / "distributions" / "dist-new").is_file()
    assert str(machine.project_dir) not in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="Needs folder permissions that bind the user.")
def test_a_folder_that_cannot_be_read_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    unreadable = data / "unreadable"
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        assert uninstaller._remove(data) is False
    finally:
        unreadable.chmod(stat.S_IRWXU)

    assert f"Could not remove {data}" in capsys.readouterr().out
    assert unreadable.is_dir()


def test_windows_removes_the_locked_files_after_exit_and_drops_the_path_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = tmp_path / "Local"
    bin_dir = local / "chrys" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "chrys.exe").write_text("binary", encoding="utf-8")
    runtime = local / "pyapp" / "data" / "chrys"
    runtime.mkdir(parents=True)
    cached = tmp_path / "cache" / "distributions" / "dist-new"
    cached.parent.mkdir(parents=True)
    cached.write_text("archive", encoding="utf-8")
    monkeypatch.setattr(uninstaller, "get_platform", lambda: SimpleNamespace(is_windows=True))
    run = mock.create_autospec(
        subprocess.run, return_value=subprocess.CompletedProcess([], 0, stdout="removed\n", stderr="")
    )
    popen = mock.create_autospec(subprocess.Popen)
    monkeypatch.setattr(uninstaller.subprocess, "run", run)
    monkeypatch.setattr(uninstaller.subprocess, "Popen", popen)
    monkeypatch.setattr(uninstaller, "_find_windows_powershell", lambda: "pwsh")
    install = uninstaller._OfflineInstall(
        folders=[bin_dir, runtime], cache_entries=[cached], path_entry=bin_dir, empty_parent=bin_dir.parent
    )

    assert uninstaller._remove_offline_install(install) is True

    # Locked files stay for the helper; the cache entry is free to go now.
    assert (bin_dir / "chrys.exe").is_file()
    assert runtime.is_dir()
    assert not cached.exists()

    [((path_argv, *_), path_kwargs)] = run.call_args_list
    assert path_argv[:4] == ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
    assert path_kwargs["stdin"] is subprocess.DEVNULL
    assert path_kwargs["env"]["ICODE_UNINSTALL_DIR"] == str(bin_dir)
    assert f"Removed {bin_dir} from your user PATH" in capsys.readouterr().out

    [((helper_argv, *_), helper_kwargs)] = popen.call_args_list
    assert helper_argv[:4] == ["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert base64.b64decode(helper_argv[4]).decode("utf-16-le") == uninstaller._DEFERRED_REMOVAL
    assert helper_kwargs["stdin"] is subprocess.DEVNULL
    assert helper_kwargs["stdout"] is subprocess.DEVNULL
    assert helper_kwargs["stderr"] is subprocess.DEVNULL
    assert Path(helper_kwargs["cwd"]) not in (bin_dir, runtime)
    env = helper_kwargs["env"]
    assert env["ICODE_UNINSTALL_WAIT_PIDS"] == f"{os.getpid()},{os.getppid()}"
    assert env["ICODE_UNINSTALL_TARGETS"].split("\n") == [str(bin_dir), str(runtime)]
    assert env["ICODE_UNINSTALL_PRUNE"] == str(bin_dir.parent)


def test_windows_names_the_leftovers_when_the_helper_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "chrys" / "bin"
    monkeypatch.setattr(uninstaller, "get_platform", lambda: SimpleNamespace(is_windows=True))
    monkeypatch.setattr(uninstaller, "_find_windows_powershell", lambda: "pwsh")
    popen = mock.create_autospec(subprocess.Popen, side_effect=FileNotFoundError("pwsh"))
    monkeypatch.setattr(uninstaller.subprocess, "Popen", popen)

    assert uninstaller._remove_offline_install(uninstaller._OfflineInstall(folders=[folder])) is False

    assert f"Delete {folder} yourself after {APP_DISPLAY_NAME} exits." in capsys.readouterr().out


def test_windows_finds_the_install_and_ignores_the_pyapp_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = tmp_path / "Local"
    bin_dir = local / "chrys" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "chrys.exe").write_text("binary", encoding="utf-8")
    project_dir = local / "pyapp" / "data" / "chrys"
    exe = project_dir / "dist-new" / "0.30.0" / "python" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("python", encoding="utf-8")
    config_dir = get_platform().config_dir
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("PYAPP", str(bin_dir / "chrys.exe"))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(uninstaller, "detect_install_flavor", lambda: InstallFlavor.OFFLINE)
    monkeypatch.setattr(uninstaller, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=config_dir))
    monkeypatch.setattr(uninstaller, "_pyapp_cache_dir", lambda: None)
    monkeypatch.setattr(uninstaller, "_find_windows_powershell", lambda: "pwsh")
    preflight = mock.create_autospec(uninstaller._require_no_running_chrys_instances, return_value=True)
    monkeypatch.setattr(uninstaller, "_require_no_running_chrys_instances", preflight)
    run = mock.create_autospec(subprocess.run, return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    popen = mock.create_autospec(subprocess.Popen)
    monkeypatch.setattr(uninstaller.subprocess, "run", run)
    monkeypatch.setattr(uninstaller.subprocess, "Popen", popen)

    assert uninstaller.uninstall(purge=False, assume_yes=True) == 0

    # PyApp runs this interpreter as a child of its launcher, which is iCode too.
    assert preflight.call_args.kwargs["ignored_pids"] == {os.getpid(), os.getppid()}
    assert run.call_args.kwargs["env"]["ICODE_UNINSTALL_DIR"] == str(bin_dir)
    env = popen.call_args.kwargs["env"]
    assert env["ICODE_UNINSTALL_TARGETS"].split("\n") == [str(bin_dir), str(project_dir)]
    assert env["ICODE_UNINSTALL_PRUNE"] == str(bin_dir.parent)
    assert f"{APP_DISPLAY_NAME} finishes removing its files once this command exits." in capsys.readouterr().out


_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(_POWERSHELL is None, reason="Needs PowerShell.")
def test_the_windows_helper_renames_then_deletes_its_targets(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    bin_dir = local / "chrys" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "chrys.exe").write_text("binary", encoding="utf-8")
    runtime = local / "pyapp" / "data" / "chrys"
    (runtime / "dist-new" / "0.30.0").mkdir(parents=True)
    (runtime / "dist-new" / "0.30.0" / "python.exe").write_text("python", encoding="utf-8")
    other_app = local / "pyapp" / "data" / "other"
    other_app.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # Only the shell's own modules, so looking up a command reads none of the machine's others.
    command = "$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')\n" + uninstaller._DEFERRED_REMOVAL
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "Roaming"),
        "LOCALAPPDATA": str(home / "Local"),
        # Where pwsh keeps its user modules, settings and cache off Windows.
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        # pwsh otherwise reports its start and looks for updates over the network.
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "POWERSHELL_UPDATECHECK": "Off",
        # Nothing to wait for: the processes the real helper waits on are this test's own.
        "ICODE_UNINSTALL_WAIT_PIDS": "",
        "ICODE_UNINSTALL_TARGETS": f"{bin_dir}\n{runtime}\n{local / 'already-gone'}",
        "ICODE_UNINSTALL_PRUNE": f"{bin_dir.parent}\n{runtime.parent}",
    }

    result = subprocess.run(
        [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=45,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not bin_dir.parent.exists()
    assert sorted(path.name for path in runtime.parent.iterdir()) == ["other"]
