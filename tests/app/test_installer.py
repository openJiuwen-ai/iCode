# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from chrys.app import installer
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.kernel.aixcoding.command_migration import LEGACY_APP_COMMAND

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeProcess:
    def __init__(self, *, pid: int, name: str, exe: str | None = None, cmdline: list[str] | None = None) -> None:
        self.pid = pid
        self.info = {"pid": pid, "name": name, "exe": exe, "cmdline": cmdline}


class _FakeStdin:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _scan(
    *processes: installer._RunningChrysProcess,
    complete: bool = True,
    ignored_pids: set[int] | None = None,
) -> installer._ChrysProcessScan:
    del ignored_pids
    return installer._ChrysProcessScan(processes=processes, complete=complete)


def test_find_windows_powershell_prefers_pwsh(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = {
        "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    }

    def which(name: str) -> str | None:
        return paths.get(name)

    monkeypatch.setattr(installer.shutil, "which", which)

    assert installer._find_windows_powershell() == paths["pwsh"]


def test_find_windows_powershell_falls_back_to_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    path = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    def which(name: str) -> str | None:
        return path if name == "powershell" else None

    monkeypatch.setattr(installer.shutil, "which", which)

    assert installer._find_windows_powershell() == path


def test_find_running_chrys_processes_detects_aliases_and_cli_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [
        _FakeProcess(pid=10, name="chrys-runtime", cmdline=["chrys-runtime", "-I"]),
        _FakeProcess(pid=11, name="python3", cmdline=["python3", "-c", "import chrys.app.cli.app"]),
        _FakeProcess(pid=12, name="python3", cmdline=["python3", "script.py"]),
        _FakeProcess(pid=13, name="chrys", cmdline=["chrys", "install"]),
        # The product-named command is the same binary, so it holds the same version directory.
        _FakeProcess(pid=14, name=f"{APP_COMMAND}.exe", cmdline=None),
        _FakeProcess(pid=15, name="sh", cmdline=["sh", f"/home/user/.local/bin/{APP_COMMAND}"]),
    ]
    monkeypatch.setattr(installer.psutil, "process_iter", lambda _attrs, *, ad_value: iter(processes))

    found = installer._find_running_chrys_processes(ignored_pids={13})

    assert [(process.pid, process.name) for process in found.processes] == [
        (10, "chrys-runtime"),
        (11, "python3"),
        (14, f"{APP_COMMAND}.exe"),
        (15, "sh"),
    ]
    assert found.complete is True


def test_find_running_chrys_processes_ignores_non_python_marker_false_positives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [
        _FakeProcess(pid=10, name="rg", cmdline=["rg", "chrys.app.cli.app"]),
        _FakeProcess(pid=11, name="vim", cmdline=["vim", "import chrys.app.cli.app"]),
        _FakeProcess(pid=12, name="python3.14", cmdline=["python3.14", "-m", "chrys.app.cli.app"]),
    ]
    monkeypatch.setattr(installer.psutil, "process_iter", lambda _attrs, *, ad_value: iter(processes))

    found = installer._find_running_chrys_processes()

    assert [(process.pid, process.name) for process in found.processes] == [(12, "python3.14")]
    assert found.complete is True


def test_find_running_chrys_processes_reads_only_the_program_and_its_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [
        # A uv or pipx install: its entry point is a Python script named after the command.
        _FakeProcess(pid=10, name="python3.14", cmdline=["/venv/bin/python3.14", f"/home/u/.local/bin/{APP_COMMAND}"]),
        _FakeProcess(pid=11, name="python3", cmdline=["python3", "-I", f"/opt/bin/{APP_COMMAND}", "run", "x"]),
        _FakeProcess(pid=12, name="zsh", cmdline=["-zsh"]),
        # Arguments after the program are data: a folder named iCode, or the child `uv run` starts.
        _FakeProcess(pid=13, name="node", cmdline=["node", "worker.js", "/Users/u/Repos/iCode"]),
        _FakeProcess(pid=14, name="vim", cmdline=["vim", f"/home/u/{APP_COMMAND}"]),
        _FakeProcess(pid=15, name="uv", cmdline=["uv", "run", APP_COMMAND, "uninstall"]),
        _FakeProcess(pid=16, name="python3", cmdline=["python3", "tool.py", f"/home/u/{APP_COMMAND}"]),
    ]
    monkeypatch.setattr(installer.psutil, "process_iter", lambda _attrs, *, ad_value: iter(processes))

    found = installer._find_running_chrys_processes()

    assert [process.pid for process in found.processes] == [10, 11]


def test_find_running_chrys_processes_reports_incomplete_when_scan_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_scan(_attrs, *, ad_value):
        raise OSError("permission denied")

    monkeypatch.setattr(installer.psutil, "process_iter", fail_scan)

    assert installer._find_running_chrys_processes() == _scan(complete=False)
    assert f"Warning: Could not check for running {APP_DISPLAY_NAME} processes: permission denied." in (
        capsys.readouterr().out
    )


def test_find_running_chrys_processes_ignores_process_with_unavailable_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readable = _FakeProcess(pid=10, name="chrys", cmdline=["chrys"])

    def process_iter(attrs: tuple[str, ...], *, ad_value: object):
        assert attrs == installer._PROCESS_SCAN_ATTRS
        assert ad_value is installer._PROCESS_INFO_UNAVAILABLE
        unreadable = _FakeProcess(pid=12, name="python3", cmdline=None)
        unreadable.info["cmdline"] = ad_value
        return iter([readable, unreadable])

    monkeypatch.setattr(installer.psutil, "process_iter", process_iter)

    scan = installer._find_running_chrys_processes()

    assert [(process.pid, process.name) for process in scan.processes] == [(10, "chrys")]
    assert scan.complete is True


def test_find_running_chrys_processes_detects_chrys_by_name_despite_unreadable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def process_iter(_attrs: tuple[str, ...], *, ad_value: object):
        elevated = _FakeProcess(pid=14, name="chrys.exe", cmdline=None)
        elevated.info["exe"] = ad_value
        elevated.info["cmdline"] = ad_value
        return iter([elevated])

    monkeypatch.setattr(installer.psutil, "process_iter", process_iter)

    scan = installer._find_running_chrys_processes()

    assert [(process.pid, process.name) for process in scan.processes] == [(14, "chrys.exe")]
    assert scan.processes[0].command == ""
    assert scan.complete is True


def test_find_running_chrys_processes_reports_incomplete_when_process_info_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenProcess:
        @property
        def info(self) -> dict[str, object]:
            raise OSError("unexpected read failure")

    monkeypatch.setattr(
        installer.psutil,
        "process_iter",
        lambda _attrs, *, ad_value: iter([_BrokenProcess()]),
    )

    scan = installer._find_running_chrys_processes()

    assert scan.processes == ()
    assert scan.complete is False


def test_require_no_running_chrys_instances_aborts_when_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(installer, "_current_install_process_ids", lambda: {1})
    monkeypatch.setattr(
        installer,
        "_find_running_chrys_processes",
        lambda *, ignored_pids: _scan(installer._RunningChrysProcess(pid=22, name="chrys", command="chrys")),
    )
    monkeypatch.setattr(installer.sys, "stdin", _FakeStdin(tty=False))

    with pytest.raises(SystemExit) as exc_info:
        installer._require_no_running_chrys_instances()

    out = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert f"Error: {APP_DISPLAY_NAME} is already running." in out
    assert "PID 22: chrys" in out
    assert f"Rerun '{APP_COMMAND} install' after those processes have exited" in out


def test_require_no_running_chrys_instances_names_the_command_it_guards(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[set[int]] = []

    def scan(*, ignored_pids: set[int]) -> installer._ChrysProcessScan:
        seen.append(ignored_pids)
        return _scan(installer._RunningChrysProcess(pid=22, name="chrys", command="chrys"))

    monkeypatch.setattr(installer, "_current_install_process_ids", lambda: pytest.fail("ignored_pids was given"))
    monkeypatch.setattr(installer, "_find_running_chrys_processes", scan)
    monkeypatch.setattr(installer.sys, "stdin", _FakeStdin(tty=False))

    with pytest.raises(SystemExit):
        installer._require_no_running_chrys_instances(
            ignored_pids={7}, action="uninstall", rerun=f"{APP_COMMAND} uninstall --purge"
        )

    out = capsys.readouterr().out
    assert seen == [{7}]
    assert "before continuing with uninstall:" in out
    assert f"Rerun '{APP_COMMAND} uninstall --purge' after those processes have exited" in out


def test_require_no_running_chrys_instances_rechecks_after_user_quits_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans: list[installer._ChrysProcessScan] = [
        _scan(installer._RunningChrysProcess(pid=22, name="chrys", command="chrys")),
        _scan(),
    ]
    prompts: list[str] = []
    monkeypatch.setattr(installer, "_current_install_process_ids", lambda: {1})
    monkeypatch.setattr(installer, "_find_running_chrys_processes", lambda *, ignored_pids: scans.pop(0))
    monkeypatch.setattr(installer.sys, "stdin", _FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")

    assert installer._require_no_running_chrys_instances() is True

    assert prompts == ["\nPress Enter after quitting them to check again, or type 'q' to abort: "]
    assert scans == []


def test_require_no_running_chrys_instances_aborts_on_user_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer, "_current_install_process_ids", lambda: {1})
    monkeypatch.setattr(
        installer,
        "_find_running_chrys_processes",
        lambda *, ignored_pids: _scan(installer._RunningChrysProcess(pid=22, name="chrys", command="chrys")),
    )
    monkeypatch.setattr(installer.sys, "stdin", _FakeStdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")

    with pytest.raises(SystemExit) as exc_info:
        installer._require_no_running_chrys_instances()

    assert exc_info.value.code == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises the POSIX install branch (~/.local/bin, ':' PATH separator); Path.home() ignores HOME on Windows.",
)
def test_install_to_path_reports_success_for_unix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "chrys-pyapp"
    src.write_text("binary", encoding="utf-8")
    home = tmp_path / "home"
    install_dir = home / ".local" / "bin"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(install_dir))
    monkeypatch.setenv("PYAPP", str(src))
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    monkeypatch.setattr(installer, "_find_running_chrys_processes", _scan)
    monkeypatch.setattr(
        installer,
        "_require_no_running_chrys_instances",
        lambda: pytest.fail("POSIX install must not block on running instances"),
    )
    pruned = False

    def fake_prune() -> None:
        nonlocal pruned
        pruned = True

    monkeypatch.setattr(installer, "_prune_old_pyapp_versions", fake_prune)

    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def observed_replace(source: str | Path, destination: str | Path) -> None:
        temporary_path = Path(source)
        assert temporary_path.parent == install_dir
        assert temporary_path.read_text(encoding="utf-8") == "binary"
        assert temporary_path.stat().st_mode & 0o777 == 0o755
        replacements.append((temporary_path, Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", observed_replace)

    installer.install_to_path()

    dest = install_dir / "chrys"
    out = capsys.readouterr().out
    assert dest.read_text(encoding="utf-8") == "binary"
    assert len(replacements) == 1
    assert replacements[0][0].name.startswith(".chrys.")
    assert replacements[0][0].suffix == ".tmp"
    assert replacements[0][1] == dest
    assert pruned is True
    alias = install_dir / APP_COMMAND
    assert alias.is_symlink() and alias.readlink() == Path("chrys") and alias.samefile(dest)
    assert f"Success: Installed {APP_DISPLAY_NAME} to {dest}." in out
    assert (
        f"Success: {APP_DISPLAY_NAME} is already on PATH. You can run '{APP_COMMAND}' or 'chrys' from anywhere." in out
    )


@pytest.mark.parametrize(
    ("pyapp", "expected"),
    [
        (None, "'uv tool upgrade iCode-TUI' to upgrade"),
        ("1", "PYAPP_PASS_LOCATION must be enabled at build time."),
    ],
    ids=["uv-install", "pyapp-without-location"],
)
def test_install_to_path_refuses_without_a_pyapp_binary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pyapp: str | None,
    expected: str,
) -> None:
    if pyapp is None:
        monkeypatch.delenv("PYAPP", raising=False)
    else:
        monkeypatch.setenv("PYAPP", pyapp)

    with pytest.raises(SystemExit) as exc_info:
        installer.install_to_path()

    assert exc_info.value.code == 1
    assert expected in capsys.readouterr().out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises the POSIX install branch (~/.local/bin, ':' PATH separator); Path.home() ignores HOME on Windows.",
)
def test_install_to_path_allows_source_to_equal_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    dest = home / ".local" / "bin" / "chrys"
    dest.parent.mkdir(parents=True)
    dest.write_text("binary", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(dest.parent))
    monkeypatch.setenv("PYAPP", str(dest))
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    monkeypatch.setattr(installer, "_find_running_chrys_processes", _scan)
    monkeypatch.setattr(installer, "_prune_old_pyapp_versions", lambda: None)

    installer.install_to_path()

    assert dest.read_text(encoding="utf-8") == "binary"
    assert dest.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises POSIX atomic replacement semantics.",
)
def test_atomic_copy_executable_cleans_up_temporary_file_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "chrys"
    src.write_text("new", encoding="utf-8")
    dest.write_text("old", encoding="utf-8")

    def fail_replace(_source: str | Path, _destination: str | Path) -> None:
        raise OSError("failed")

    monkeypatch.setattr(installer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="failed"):
        installer._atomic_copy_executable(src, dest)

    assert dest.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".chrys.*.tmp")) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises POSIX atomic replacement semantics.",
)
def test_atomic_copy_executable_preserves_replace_error_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src = tmp_path / "source"
    dest = tmp_path / "chrys"
    src.write_text("new", encoding="utf-8")
    dest.write_text("old", encoding="utf-8")

    def fail_replace(_source: str | Path, _destination: str | Path) -> None:
        raise OSError("replace failed")

    def fail_cleanup(_path: Path, *, missing_ok: bool = False) -> None:
        raise PermissionError("cleanup failed")

    monkeypatch.setattr(installer.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    with pytest.raises(OSError, match="replace failed"):
        installer._atomic_copy_executable(src, dest)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises the POSIX install branch (~/.local/bin, ':' PATH separator); Path.home() ignores HOME on Windows.",
)
def test_install_to_path_skips_prune_when_posix_instance_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "chrys-pyapp"
    src.write_text("binary", encoding="utf-8")
    home = tmp_path / "home"
    install_dir = home / ".local" / "bin"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(install_dir))
    monkeypatch.setenv("PYAPP", str(src))
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    scans: list[set[int]] = []

    def find_running(*, ignored_pids: set[int]) -> installer._ChrysProcessScan:
        scans.append(ignored_pids)
        return _scan(installer._RunningChrysProcess(pid=22, name="python3", command="python3 -m chrys.app.cli.app"))

    monkeypatch.setattr(installer, "_find_running_chrys_processes", find_running)
    monkeypatch.setattr(
        installer,
        "_require_no_running_chrys_instances",
        lambda: pytest.fail("POSIX install must not block on running instances"),
    )
    monkeypatch.setattr(
        installer,
        "_prune_old_pyapp_versions",
        lambda: pytest.fail("Running POSIX instances must suppress old-version cleanup"),
    )

    installer.install_to_path()

    out = capsys.readouterr().out
    # Only the installer's own pid is exempt: a Chrys TUI hosting this install in its embedded
    # terminal is an ancestor and must stay visible to the cleanup gate.
    assert scans == [{os.getpid()}]
    assert (install_dir / "chrys").read_text(encoding="utf-8") == "binary"
    assert f"Warning: Detected running {APP_DISPLAY_NAME} instances; skipped cleanup of old versions." in out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Exercises the POSIX install branch (~/.local/bin, ':' PATH separator); Path.home() ignores HOME on Windows.",
)
def test_install_to_path_skips_prune_when_posix_process_scan_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "chrys-pyapp"
    src.write_text("binary", encoding="utf-8")
    home = tmp_path / "home"
    install_dir = home / ".local" / "bin"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(install_dir))
    monkeypatch.setenv("PYAPP", str(src))
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    monkeypatch.setattr(
        installer,
        "_find_running_chrys_processes",
        lambda *, ignored_pids: _scan(complete=False),
    )
    monkeypatch.setattr(
        installer,
        "_prune_old_pyapp_versions",
        lambda: pytest.fail("An incomplete process scan must suppress old-version cleanup"),
    )

    installer.install_to_path()

    out = capsys.readouterr().out
    assert (install_dir / "chrys").read_text(encoding="utf-8") == "binary"
    assert (
        f"Warning: Could not fully check for running {APP_DISPLAY_NAME} instances; skipped cleanup of old versions."
        in out
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks need privileges on Windows; the POSIX branch owns them.")
@pytest.mark.parametrize("existing", ["absent", "own", "dangling", "foreign-file", "foreign-link"])
def test_command_alias_follows_the_installed_binary_and_spares_foreign_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], existing: str
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    src = tmp_path / "download"
    src.write_text("binary", encoding="utf-8")
    dest = tmp_path / "bin" / "chrys"
    dest.parent.mkdir()
    dest.write_text("binary", encoding="utf-8")
    other = tmp_path / "other-tool"
    other.write_text("other", encoding="utf-8")
    alias = dest.with_name(APP_COMMAND)
    if existing == "own":
        alias.symlink_to("chrys")
    elif existing == "dangling":
        alias.symlink_to("removed")
    elif existing == "foreign-file":
        alias.write_text("other", encoding="utf-8")
    elif existing == "foreign-link":
        alias.symlink_to(other)

    installed = installer._install_command_alias(src, dest)

    out = capsys.readouterr().out
    if existing.startswith("foreign"):
        # ~/.local/bin is shared: a same-named command from another tool keeps working.
        assert installed is None and alias.read_text(encoding="utf-8") == "other"
        assert f"{alias} already exists and starts something else" in out
        assert installer._installed_commands(installed) == "'chrys'"
    else:
        assert installed == alias and alias.readlink() == Path("chrys") and alias.samefile(dest)
        assert not out
        assert installer._installed_commands(installed) == f"'{APP_COMMAND}' or 'chrys'"


@pytest.mark.parametrize("hard_links", [True, False])
def test_windows_command_alias_is_a_hard_link_or_a_copy_and_is_refreshed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hard_links: bool
) -> None:
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=tmp_path))
    src = tmp_path / "download.exe"
    src.write_text("new binary", encoding="utf-8")
    dest = tmp_path / "chrys.exe"
    dest.write_text("new binary", encoding="utf-8")
    alias = tmp_path / f"{APP_COMMAND}.exe"
    alias.write_text("copy left by an older install", encoding="utf-8")
    if not hard_links:

        def no_hard_links(source: Path, target: Path) -> None:
            raise OSError("hard links are not supported on this volume")

        monkeypatch.setattr(installer.os, "link", no_hard_links)

    assert installer._install_command_alias(src, dest) == alias
    assert alias.read_text(encoding="utf-8") == "new binary"
    assert alias.samefile(dest) is hard_links

    # Installing from the alias itself: Windows cannot unlink a running binary. A hard link
    # already follows the installed binary and a copy was its source, so neither is replaced.
    def replaced(*args: object) -> None:
        pytest.fail("the current alias is kept")

    monkeypatch.setattr(installer.os, "link", replaced)
    monkeypatch.setattr(installer.shutil, "copy2", replaced)
    assert installer._install_command_alias(alias, dest) == alias


def test_find_running_chrys_processes_detects_the_legacy_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # AIxCoding branding: legacy alias migration tests
    processes = [
        _FakeProcess(pid=10, name=LEGACY_APP_COMMAND, cmdline=[LEGACY_APP_COMMAND]),
        _FakeProcess(pid=11, name=f"{LEGACY_APP_COMMAND}.exe", cmdline=None),
        _FakeProcess(pid=12, name="other-tool", cmdline=["other-tool"]),
    ]
    monkeypatch.setattr(installer.psutil, "process_iter", lambda _attrs, *, ad_value: iter(processes))

    found = installer._find_running_chrys_processes()

    assert [process.pid for process in found.processes] == [10, 11]
    assert found.complete is True


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks need privileges on Windows; the POSIX branch owns them.")
def test_posix_install_cleans_up_the_own_legacy_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # AIxCoding branding: legacy alias migration tests
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    src = tmp_path / "download"
    src.write_text("binary", encoding="utf-8")
    dest = tmp_path / "bin" / "chrys"
    dest.parent.mkdir()
    dest.write_text("binary", encoding="utf-8")
    legacy = dest.with_name(LEGACY_APP_COMMAND)
    legacy.symlink_to("chrys")

    installed = installer._install_command_alias(src, dest)

    assert installed == dest.with_name(APP_COMMAND)
    assert not legacy.exists() and not legacy.is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks need privileges on Windows; the POSIX branch owns them.")
def test_posix_install_keeps_the_legacy_alias_when_the_new_one_conflicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    src = tmp_path / "download"
    src.write_text("binary", encoding="utf-8")
    dest = tmp_path / "bin" / "chrys"
    dest.parent.mkdir()
    dest.write_text("binary", encoding="utf-8")
    legacy = dest.with_name(LEGACY_APP_COMMAND)
    legacy.symlink_to("chrys")
    alias = dest.with_name(APP_COMMAND)
    alias.write_text("other", encoding="utf-8")

    installed = installer._install_command_alias(src, dest)

    assert installed is None
    assert legacy.is_symlink() and legacy.samefile(dest)
    assert "already exists and starts something else" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks need privileges on Windows; the POSIX branch owns them.")
def test_posix_install_spares_an_external_legacy_icode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    src = tmp_path / "download"
    src.write_text("binary", encoding="utf-8")
    dest = tmp_path / "bin" / "chrys"
    dest.parent.mkdir()
    dest.write_text("binary", encoding="utf-8")
    legacy = dest.with_name(LEGACY_APP_COMMAND)
    legacy.write_text("uv entry point", encoding="utf-8")

    installed = installer._install_command_alias(src, dest)

    assert installed == dest.with_name(APP_COMMAND)
    assert legacy.read_text(encoding="utf-8") == "uv entry point"
    assert f"Kept {legacy}: cannot prove it belongs to this installation." in capsys.readouterr().out


def test_windows_install_cleans_up_a_hard_linked_legacy_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # AIxCoding branding: legacy alias migration tests
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=tmp_path))
    src = tmp_path / "download.exe"
    src.write_text("new binary", encoding="utf-8")
    dest = tmp_path / "chrys.exe"
    dest.write_text("new binary", encoding="utf-8")
    legacy = tmp_path / f"{LEGACY_APP_COMMAND}.exe"
    os.link(dest, legacy)

    installed = installer._install_command_alias(src, dest)

    assert installed == tmp_path / f"{APP_COMMAND}.exe"
    assert not legacy.exists()


def test_windows_install_spares_a_legacy_copy_whose_ownership_is_unprovable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=tmp_path))
    src = tmp_path / "download.exe"
    src.write_text("new binary", encoding="utf-8")
    dest = tmp_path / "chrys.exe"
    dest.write_text("new binary", encoding="utf-8")
    legacy = tmp_path / f"{LEGACY_APP_COMMAND}.exe"
    legacy.write_text("copy left by an older install", encoding="utf-8")

    installed = installer._install_command_alias(src, dest)

    assert installed == tmp_path / f"{APP_COMMAND}.exe"
    assert legacy.read_text(encoding="utf-8") == "copy left by an older install"
    assert f"Kept {legacy}: cannot prove it belongs to this installation." in capsys.readouterr().out


def test_windows_install_spares_a_legacy_alias_that_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=tmp_path))
    src = tmp_path / "download.exe"
    src.write_text("new binary", encoding="utf-8")
    dest = tmp_path / "chrys.exe"
    dest.write_text("new binary", encoding="utf-8")
    legacy = tmp_path / f"{LEGACY_APP_COMMAND}.exe"
    os.link(dest, legacy)

    original_unlink = Path.unlink

    def locked(self: Path, *args: object, **kwargs: object) -> None:
        if self == legacy:
            raise OSError("the file is in use by another process")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)

    installed = installer._install_command_alias(src, dest)

    assert installed == tmp_path / f"{APP_COMMAND}.exe"
    assert legacy.exists()
    assert "Could not remove the legacy 'icode' command" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks need privileges on Windows; the POSIX branch owns them.")
def test_posix_install_migrates_when_the_new_alias_already_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The own/current early-return branch migrates too (F-1).
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=False, config_dir=tmp_path))
    src = tmp_path / "download"
    src.write_text("binary", encoding="utf-8")
    dest = tmp_path / "bin" / "chrys"
    dest.parent.mkdir()
    dest.write_text("binary", encoding="utf-8")
    dest.with_name(APP_COMMAND).symlink_to("chrys")
    legacy = dest.with_name(LEGACY_APP_COMMAND)
    legacy.symlink_to("chrys")

    installed = installer._install_command_alias(src, dest)

    assert installed == dest.with_name(APP_COMMAND)
    assert not legacy.exists() and not legacy.is_symlink()


def test_install_to_path_keeps_windows_running_instance_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    src = tmp_path / "chrys-pyapp.exe"
    src.write_text("binary", encoding="utf-8")

    class _InstallBlocked(Exception):
        pass

    def block_install() -> None:
        raise _InstallBlocked

    monkeypatch.setenv("PYAPP", str(src))
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=tmp_path))
    monkeypatch.setattr(installer, "_require_no_running_chrys_instances", block_install)
    monkeypatch.setattr(
        installer,
        "_find_running_chrys_processes",
        lambda **_kwargs: pytest.fail("Windows should use the blocking preflight"),
    )

    with pytest.raises(_InstallBlocked):
        installer.install_to_path()


def test_install_to_path_on_windows_says_to_quit_a_copy_that_holds_the_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "download" / "icode.exe"
    src.parent.mkdir()
    src.write_text("new binary", encoding="utf-8")
    dest = tmp_path / "chrys" / "bin" / "chrys.exe"
    dest.parent.mkdir(parents=True)
    dest.write_text("running binary", encoding="utf-8")

    def locked(_source: str, _destination: str) -> None:
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setenv("PYAPP", str(src))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(installer, "get_platform", lambda: SimpleNamespace(is_windows=True, config_dir=tmp_path))
    # The iCode whose terminal runs this install is one of its ancestors, so the check passes.
    monkeypatch.setattr(installer, "_require_no_running_chrys_instances", lambda: True)
    monkeypatch.setattr(installer.shutil, "copy2", locked)

    with pytest.raises(SystemExit) as exit_info:
        installer.install_to_path()

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert f"Could not replace {dest}" in out
    assert f"Quit every running {APP_DISPLAY_NAME}, then install again." in out
    assert dest.read_text(encoding="utf-8") == "running binary"


# Minimal ELF headers (class + data + e_type + e_machine) — just enough for the
# prune's cross-distribution platform gate to classify the interpreter.
_FAKE_ELF_X86_64 = b"\x7fELF\x02\x01" + b"\x00" * 10 + b"\x02\x00\x3e\x00"
_FAKE_ELF_AARCH64 = b"\x7fELF\x02\x01" + b"\x00" * 10 + b"\x02\x00\xb7\x00"


def _make_pyapp_version(siblings_dir: Path, version: str, interpreter: bytes = _FAKE_ELF_X86_64) -> tuple[Path, Path]:
    version_dir = siblings_dir / version
    python_dir = version_dir / "python" / "bin"
    python_dir.mkdir(parents=True)
    exe = python_dir / "python3"
    exe.write_bytes(interpreter)
    (version_dir / "marker.txt").write_text(version, encoding="utf-8")
    return version_dir, exe


def _isolate_pyapp_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache_dir = tmp_path / "pyapp-cache"
    monkeypatch.setattr(installer, "_pyapp_cache_dir", lambda: cache_dir)
    return cache_dir


def test_prune_old_pyapp_versions_keeps_current_and_latest_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    siblings_dir = tmp_path / "pyapp" / "chrys" / "dist-id"
    current, exe = _make_pyapp_version(siblings_dir, "0.6.11")
    fallback, _ = _make_pyapp_version(siblings_dir, "0.6.10")
    older_patch, _ = _make_pyapp_version(siblings_dir, "0.6.9")
    older_minor, _ = _make_pyapp_version(siblings_dir, "0.5.0-dev")

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    assert current.is_dir()
    assert fallback.is_dir()
    assert not older_patch.exists()
    assert not older_minor.exists()


def test_prune_old_pyapp_versions_covers_the_wheel_named_flavors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The pip and uv flavors embed the wheel, and PyApp names their project
    # directory after its metadata rather than PYAPP_PROJECT_NAME.
    siblings_dir = tmp_path / "pyapp" / "icode-tui" / "dist-id"
    current, exe = _make_pyapp_version(siblings_dir, "0.6.11")
    fallback, _ = _make_pyapp_version(siblings_dir, "0.6.10")
    older, _ = _make_pyapp_version(siblings_dir, "0.6.9")

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)
    monkeypatch.delenv("PYAPP_INSTALL_DIR_ICODE-TUI", raising=False)

    installer._prune_old_pyapp_versions()

    assert current.is_dir()
    assert fallback.is_dir()
    assert not older.exists()


def test_prune_old_pyapp_versions_reclaims_obsolete_distribution_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The offline flavor bakes the versioned wheel into its archive, so every
    # release ships a new <distribution_id>; pruning must reach across them.
    project_dir = tmp_path / "pyapp" / "chrys"
    current, exe = _make_pyapp_version(project_dir / "dist-new", "0.6.11")
    fallback, _ = _make_pyapp_version(project_dir / "dist-old", "0.6.10")
    superseded, _ = _make_pyapp_version(project_dir / "dist-old", "0.6.9")
    stranded, _ = _make_pyapp_version(project_dir / "dist-older", "0.6.8")
    newer, _ = _make_pyapp_version(project_dir / "dist-next", "0.7.0")

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    assert current.is_dir()
    # The most recent older version stays as the fallback even under another ID.
    assert fallback.is_dir()
    assert not superseded.exists()
    assert not stranded.exists()
    # A distribution directory emptied by the prune goes with its versions...
    assert not (project_dir / "dist-older").exists()
    # ...while one still holding content survives, as do newer versions.
    assert (project_dir / "dist-old").is_dir()
    assert newer.is_dir()


def test_prune_old_pyapp_versions_removes_same_version_rebuilds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Rebuilding the same chrys version produces a new archive hash, i.e. a new
    # distribution ID with an identically named version directory inside.
    project_dir = tmp_path / "pyapp" / "chrys"
    current, exe = _make_pyapp_version(project_dir / "dist-new", "0.6.11")
    rebuild_a, _ = _make_pyapp_version(project_dir / "dist-prev", "0.6.11")
    rebuild_b, _ = _make_pyapp_version(project_dir / "dist-older", "0.6.11")
    fallback, _ = _make_pyapp_version(project_dir / "dist-prev", "0.6.10")

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    assert current.is_dir()
    # Superseded rebuilds are never kept as the fallback — the older version is.
    assert fallback.is_dir()
    assert not rebuild_a.exists()
    assert not rebuild_b.exists()
    assert not (project_dir / "dist-older").exists()
    assert (project_dir / "dist-prev").is_dir()


def test_prune_old_pyapp_versions_leaves_other_platform_runtimes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A shared home directory can hold runtimes installed by another machine's
    # binary; distribution IDs differ per architecture. Those must neither be
    # deleted nor claim the fallback slot from a compatible runtime.
    project_dir = tmp_path / "pyapp" / "chrys"
    current, exe = _make_pyapp_version(project_dir / "dist-new", "0.6.11")
    other_arch, _ = _make_pyapp_version(project_dir / "dist-arm", "0.6.10", interpreter=_FAKE_ELF_AARCH64)
    fallback, _ = _make_pyapp_version(project_dir / "dist-a", "0.6.9")
    stale, _ = _make_pyapp_version(project_dir / "dist-b", "0.6.8")

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    assert current.is_dir()
    assert other_arch.is_dir()
    # The newest *compatible* older version is the fallback, not the aarch64 one.
    assert fallback.is_dir()
    assert not stale.exists()
    assert not (project_dir / "dist-b").exists()


def test_prune_old_pyapp_versions_never_follows_symlinked_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_dir = tmp_path / "pyapp" / "chrys"
    current, exe = _make_pyapp_version(project_dir / "dist-new", "0.6.11")
    fallback, _ = _make_pyapp_version(project_dir / "dist-prev", "0.6.10")
    # A symlinked distribution directory must not let the scan escape the
    # PyApp tree and rmtree external data through the link.
    external = tmp_path / "external"
    victim, _ = _make_pyapp_version(external, "0.6.9")
    external_child = tmp_path / "external-child"
    external_child.mkdir()
    (external_child / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        (project_dir / "dist-linked").symlink_to(external, target_is_directory=True)
        # Same at the version level, inside a legitimate distribution dir.
        (project_dir / "dist-prev" / "0.6.8").symlink_to(external_child, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks on this platform")

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    assert victim.is_dir()
    assert (external_child / "keep.txt").is_file()
    assert (project_dir / "dist-linked").is_symlink()
    assert (project_dir / "dist-prev" / "0.6.8").is_symlink()
    assert current.is_dir()
    assert fallback.is_dir()


def test_prune_old_pyapp_versions_reclaims_cache_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_dir = tmp_path / "pyapp" / "chrys"
    _current, exe = _make_pyapp_version(project_dir / "dist-new", "0.6.11")
    _fallback, _ = _make_pyapp_version(project_dir / "dist-prev", "0.6.10")
    _stale, _ = _make_pyapp_version(project_dir / "dist-old", "0.6.9")

    cache_dir = _isolate_pyapp_cache(monkeypatch, tmp_path)
    distributions = cache_dir / "distributions"
    distributions.mkdir(parents=True)
    for dist_id in ("dist-new", "dist-prev", "dist-old"):
        (distributions / dist_id).write_bytes(b"archive")
    # Shared unpacked base of non-full-isolation apps — must never be touched.
    (distributions / "_dist-old").mkdir()
    locks = cache_dir / "locks"
    locks.mkdir()
    (locks / "installation-chrys-dist-old-0.6.9").write_bytes(b"")
    (locks / "installation-chrys-dist-new-0.6.11").write_bytes(b"")

    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    # The emptied distribution ID loses its cached archive and version lock...
    assert not (project_dir / "dist-old").exists()
    assert not (distributions / "dist-old").exists()
    assert not (locks / "installation-chrys-dist-old-0.6.9").exists()
    # ...while entries backing surviving runtimes stay, as does the shared base.
    assert (distributions / "dist-new").is_file()
    assert (distributions / "dist-prev").is_file()
    assert (distributions / "_dist-old").is_dir()
    assert (locks / "installation-chrys-dist-new-0.6.11").is_file()


def test_prune_old_pyapp_versions_ignores_version_like_non_pyapp_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    current_dir = runtimes_dir / "3.14"
    older_dir = runtimes_dir / "3.13"
    exe = current_dir / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    older_dir.mkdir(parents=True)
    (older_dir / "marker.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)

    installer._prune_old_pyapp_versions()

    assert older_dir.is_dir()


def test_prune_old_pyapp_versions_skips_install_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    siblings_dir = tmp_path / "pyapp" / "chrys" / "dist-id"
    _current, exe = _make_pyapp_version(siblings_dir, "0.6.11")
    older, _ = _make_pyapp_version(siblings_dir, "0.6.9")

    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setenv("PYAPP_INSTALL_DIR_CHRYS", str(tmp_path / "custom-install"))

    installer._prune_old_pyapp_versions()

    assert older.is_dir()


@pytest.mark.parametrize(
    ("failed", "retried"),
    [(os.rmdir, True), (os.open, False)],
    ids=["delete-is-retried", "read-is-not-retried"],
)
def test_prune_old_pyapp_versions_reports_a_removal_that_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failed: Callable[..., object],
    retried: bool,
) -> None:
    siblings_dir = tmp_path / "pyapp" / "chrys" / "dist-id"
    _current, exe = _make_pyapp_version(siblings_dir, "0.6.11")
    _fallback, _ = _make_pyapp_version(siblings_dir, "0.6.10")
    _to_remove, _ = _make_pyapp_version(siblings_dir, "0.6.9")

    def fake_rmtree(path: str, *, onexc) -> None:
        # The retried rmdir fails on its own: the folder still has content.
        onexc(failed, path, OSError("first failure"))

    _isolate_pyapp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("PYAPP_INSTALL_DIR_CHRYS", raising=False)
    monkeypatch.setattr(shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(os, "chmod", lambda _path, _mode: None)

    installer._prune_old_pyapp_versions()

    out = capsys.readouterr().out
    assert "Warning: Could not remove old version 0.6.9: " in out
    # Only a failed delete is worth another try; retrying a failed open would call it wrongly.
    assert ("first failure" not in out) is retried
    assert "Removed 1 old version" not in out
