# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``icode uninstall``: remove an offline install, or say how to remove any other kind.

An offline install is the PyApp binary ``icode install`` copied onto PATH plus the runtime PyApp
unpacked on first run. uv, pipx and pip keep their own records of what they installed, so for
those this only prints the command that removes the app. The data folder, which holds settings,
API keys, model profiles, agents, skills and sessions, stays unless ``--purge`` is given.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from chrys.app.install_flavor import InstallFlavor, detect_install_flavor, uninstall_command
from chrys.app.installer import (
    _INSTALLED_BINARY,
    _find_pyapp_version_dir,
    _find_windows_powershell,
    _print_error,
    _print_line,
    _print_success,
    _print_warning,
    _pyapp_cache_dir,
    _require_no_running_chrys_instances,
)
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.platform import conventional_config_dir, get_platform
from chrys.foundation.platform.paths import real_path

# Drops the folder named by ICODE_UNINSTALL_DIR from the user PATH and prints "removed" when it
# was there. Like `icode install`, it goes through .NET, which writes the other entries back with
# any %VARIABLE% in them expanded. A .NET call that fails ends only its own statement, so without
# "Stop" a refused write would still print "removed" and exit 0.
_DROP_USER_PATH_ENTRY = (
    '$ErrorActionPreference = "Stop"; '
    '$dir = $env:ICODE_UNINSTALL_DIR.TrimEnd("\\"); '
    '$path = [Environment]::GetEnvironmentVariable("Path", "User"); '
    "if ($path) { "
    '$parts = @($path -split ";"); '
    '$kept = @($parts | Where-Object { $_.TrimEnd("\\") -ne $dir }); '
    "if ($kept.Count -ne $parts.Count) { "
    '[Environment]::SetEnvironmentVariable("Path", ($kept -join ";"), "User"); "removed" } }'
)

# Windows keeps a running program's files locked, and this process runs from the very files it
# removes, so a hidden PowerShell waits for it (and its PyApp launcher) to exit, then removes the
# targets, retrying for about 20 seconds while handles are released. Each target is renamed first,
# which succeeds only once nothing in it is open, and only the renamed copy is deleted, so an
# install made into the same place after that is never touched; the short window keeps a reinstall
# from landing in a folder still waiting to be renamed. Empty parents go last.
_DEFERRED_REMOVAL = """
$ErrorActionPreference = 'SilentlyContinue'
foreach ($id in @($env:ICODE_UNINSTALL_WAIT_PIDS -split ',' | Where-Object { $_ })) {
    Wait-Process -Id ([int]$id) -Timeout 600
}
$targets = @($env:ICODE_UNINSTALL_TARGETS -split "`n" | Where-Object { $_ })
$renamed = @()
for ($try = 0; $try -lt 20; $try++) {
    $waiting = @()
    foreach ($target in $targets) {
        $name = "$(Split-Path -Leaf $target).removing-$PID"
        if (-not (Test-Path -LiteralPath $target)) { continue }
        Rename-Item -LiteralPath $target -NewName $name
        if ($?) { $renamed += Join-Path (Split-Path -Parent $target) $name } else { $waiting += $target }
    }
    $targets = $waiting
    foreach ($path in $renamed) { Remove-Item -LiteralPath $path -Recurse -Force }
    $renamed = @($renamed | Where-Object { Test-Path -LiteralPath $_ })
    if ($targets.Count -eq 0 -and $renamed.Count -eq 0) { break }
    Start-Sleep -Seconds 1
}
foreach ($dir in @($env:ICODE_UNINSTALL_PRUNE -split "`n" | Where-Object { $_ })) {
    if ((Test-Path -LiteralPath $dir) -and -not (Get-ChildItem -LiteralPath $dir -Force)) {
        Remove-Item -LiteralPath $dir -Force
    }
}
"""


@dataclass
class _OfflineInstall:
    """What an offline install left on this machine."""

    files: list[Path] = field(default_factory=list)
    """Commands ``icode install`` put on PATH (POSIX)."""
    folders: list[Path] = field(default_factory=list)
    """The unpacked runtime and, on Windows, the folder ``icode install`` put on PATH."""
    cache_entries: list[Path] = field(default_factory=list)
    """PyApp's cached copies of the embedded archive and its installation locks."""
    path_entry: Path | None = None
    """The Windows user PATH entry ``icode install`` added."""
    empty_parent: Path | None = None
    """A folder of the installer's own that goes too once nothing is left in it."""
    download: Path | None = None
    """The binary this ran from when it is none of the above; the user downloaded it."""

    def has_files(self) -> bool:
        return bool(self.files or self.folders or self.cache_entries)


def _is_alias_of(alias: Path, binary: Path) -> bool:
    """Whether ``alias`` is the product-named command ``icode install`` made for ``binary``."""
    try:
        if alias.is_symlink():
            return Path(os.readlink(alias)) in (Path(binary.name), binary)
        return alias.is_file() and binary.is_file() and alias.samefile(binary)
    except OSError:
        return False


def _is_offline_binary(path: Path) -> bool:
    """Whether ``path`` is a binary ``icode install`` copied, not a command another installer made."""
    try:
        # uv and pipx link their commands into ~/.local/bin, and pip writes scripts there.
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as file:
            return file.read(2) != b"#!"
    except OSError:
        return False


def _is_within(path: Path, folders: list[Path]) -> bool:
    return any(path == folder or path.is_relative_to(folder) for folder in folders)


def _find_offline_install() -> _OfflineInstall:
    """Collect what the running offline package and ``icode install`` put on this machine."""
    found = _OfflineInstall()
    if get_platform().is_windows:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        bin_dir = local / "chrys" / "bin"
        if bin_dir.is_dir():
            found.folders.append(bin_dir)
        found.path_entry = bin_dir
        found.empty_parent = bin_dir.parent
    else:
        bin_dir = Path.home() / ".local" / "bin"
        binary = bin_dir / _INSTALLED_BINARY
        if _is_offline_binary(binary):
            found.files.append(binary)
            alias = bin_dir / APP_COMMAND
            # ~/.local/bin is shared: an `icode` that starts something else, such as a uv install,
            # stays.
            if _is_alias_of(alias, binary):
                found.files.append(alias)

    # Only PyApp's default layout is recognized; an install-dir override has a user-defined
    # structure that is not safe to delete wholesale.
    version_dir = _find_pyapp_version_dir(Path(sys.executable).resolve())
    if version_dir is not None:
        project_dir = version_dir.parent.parent
        if project_dir.is_dir() and not (project_dir.is_symlink() or project_dir.is_junction()):
            found.folders.append(project_dir)
            cache_dir = _pyapp_cache_dir()
            if cache_dir is not None:
                locks: list[Path] = []
                for dist_dir in project_dir.iterdir():
                    cached = cache_dir / "distributions" / dist_dir.name
                    if cached.is_file():
                        found.cache_entries.append(cached)
                    # Named per distribution, so another app whose name starts with this one's
                    # keeps its own.
                    locks.extend((cache_dir / "locks").glob(f"installation-{project_dir.name}-{dist_dir.name}-*"))
                found.cache_entries.extend(sorted(locks))

    pyapp = os.environ.get("PYAPP", "")
    if pyapp and pyapp != "1" and Path(pyapp).is_file():
        download = Path(pyapp).resolve()
        installed = {path.resolve() for path in found.files}
        if download not in installed and not _is_within(download, [folder.resolve() for folder in found.folders]):
            found.download = Path(pyapp)
    return found


def _config_dir_entry() -> Path:
    """The data folder under the name the user knows, which may be a link to it.

    The platform hands out the folder's real path; removing a link a dotfiles
    manager placed must remove only that link, so the conventional name is used
    whenever it leads to this very folder.
    """
    config_dir = get_platform().config_dir
    named = conventional_config_dir()
    return named if real_path(named) == config_dir else config_dir


def _data_folders(config_dir: Path) -> tuple[list[Path], Path | None]:
    """The data folder and, when sessions are saved elsewhere, that sessions folder.

    Also the sessions folder of a relative session root, which is not taken.
    """
    folders = [config_dir] if config_dir.exists() or config_dir.is_symlink() else []
    try:
        from chrys.foundation.config.process_env import freeze_process_env
        from chrys.foundation.config.settings import resolve_session_root_dir

        # This command skips the runtime bootstrap, and settings files are read only once the
        # environment is frozen, so without this a session root chosen in the settings is missed.
        freeze_process_env()
        sessions = resolve_session_root_dir(config_dir, create=False) / "sessions"
    except Exception:
        # A settings file this cannot read leaves the sessions where the config dir says.
        return folders, None
    # The session root is a folder the user chose, so only its `sessions` child is ours. A relative
    # one names a different folder in every working directory, so none of them is taken.
    if not sessions.is_absolute():
        return folders, sessions
    if sessions.is_dir() and not sessions.is_relative_to(config_dir):
        folders.append(sessions)
    return folders, None


def _force_remove(func, path, exc) -> None:
    # A read-only bit (common on Windows) blocks deleting; clear it and retry. Any other failure,
    # such as a folder that cannot be read, stands.
    if func not in (os.unlink, os.rmdir):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove(path: Path) -> bool:
    """Remove a file, link or folder, reporting the outcome."""
    try:
        if path.is_dir() and not (path.is_symlink() or path.is_junction()):
            shutil.rmtree(path, onexc=_force_remove)
        else:
            path.unlink(missing_ok=True)
    except OSError as e:
        _print_warning(f"Could not remove {path}: {e}")
        return False
    _print_line(f"  Removed {path}", style="success")
    return True


def _drop_windows_path_entry(entry: Path) -> bool:
    from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

    result = subprocess.run(  # noqa: S603
        [_find_windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", _DROP_USER_PATH_ENTRY],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "ICODE_UNINSTALL_DIR": str(entry)},
        check=False,
        **windows_hidden_subprocess_kwargs(),
    )
    if result.returncode != 0:
        _print_warning(f"Could not remove {entry} from your user PATH.")
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            _print_line(detail)
        return False
    if result.stdout.strip() == "removed":
        _print_line(f"  Removed {entry} from your user PATH", style="success")
    return True


def _schedule_windows_removal(targets: list[Path], empty_parent: Path | None) -> bool:
    """Start the helper that removes ``targets`` once this process and its launcher have exited."""
    from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

    encoded = base64.b64encode(_DEFERRED_REMOVAL.encode("utf-16-le")).decode("ascii")
    env = {
        **os.environ,
        "ICODE_UNINSTALL_WAIT_PIDS": f"{os.getpid()},{os.getppid()}",
        "ICODE_UNINSTALL_TARGETS": "\n".join(str(target) for target in targets),
        "ICODE_UNINSTALL_PRUNE": str(empty_parent) if empty_parent is not None else "",
    }
    try:
        # Its own hidden console (never CREATE_NO_WINDOW) lets it outlive this terminal, and a
        # neutral cwd keeps it from holding one of the folders it deletes open.
        subprocess.Popen(  # noqa: S603
            [_find_windows_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
            env=env,
            **windows_hidden_subprocess_kwargs(),
        )
    except OSError as e:
        _print_warning(f"Could not start removing {APP_DISPLAY_NAME}'s files: {e}")
        for target in targets:
            _print_line(f"  Delete {target} yourself after {APP_DISPLAY_NAME} exits.")
        return False
    return True


def _describe(offline: _OfflineInstall | None, data: list[Path], config_dir: Path) -> None:
    _print_line(f"This removes {APP_DISPLAY_NAME}:" if offline is not None else "This removes:")
    if offline is not None:
        for path in [*offline.files, *offline.folders]:
            _print_line(f"  {path}")
        if offline.cache_entries:
            _print_line(f"  {len(offline.cache_entries)} cached file(s) in {offline.cache_entries[0].parent.parent}")
        if offline.path_entry is not None:
            _print_line(f"  {offline.path_entry} from your user PATH, if it is there")
    for folder in data:
        if folder.is_symlink() or folder.is_junction():
            # Removing a link leaves what it points to, and deleting a folder elsewhere, perhaps
            # one a dotfiles manager keeps, is not for this command to decide.
            _print_line(f"  {folder} (only the link: {folder.resolve()}, the folder it points to, stays)")
        elif folder == config_dir:
            _print_line(
                f"  {folder} and everything in it, including your settings, API keys, model profiles, agents, "
                "skills and sessions"
            )
        else:
            _print_line(f"  {folder} (your sessions)")


def _confirm() -> bool:
    try:
        answer = input("Continue? [y/N] ").strip().casefold()
    except EOFError, KeyboardInterrupt:
        _print_line("")
        answer = ""
    if answer in {"y", "yes"}:
        return True
    _print_line("Nothing was removed.")
    return False


def _remove_offline_install(offline: _OfflineInstall) -> bool:
    ok = True
    for entry in offline.cache_entries:
        ok = _remove(entry) and ok
    if get_platform().is_windows:
        if offline.path_entry is not None:
            ok = _drop_windows_path_entry(offline.path_entry) and ok
        if offline.folders:
            ok = _schedule_windows_removal(offline.folders, offline.empty_parent) and ok
        return ok
    for path in offline.files:
        ok = _remove(path) and ok
    # POSIX keeps unlinked files readable by whoever has them open, so the runtime this process
    # runs from can go now; it goes last all the same.
    for folder in offline.folders:
        ok = _remove(folder) and ok
    return ok


def uninstall(*, purge: bool, assume_yes: bool) -> int:
    """Run ``icode uninstall``; returns the exit status."""
    flavor = detect_install_flavor()
    offline = _find_offline_install() if flavor is InstallFlavor.OFFLINE else None
    config_dir = _config_dir_entry()
    kept, relative_sessions = _data_folders(config_dir)
    data = kept if purge else []
    if purge and relative_sessions is not None:
        _print_warning(
            f"Sessions are saved in {relative_sessions}, relative to the folder {APP_DISPLAY_NAME} starts in, "
            "so they are not deleted. Delete those folders yourself."
        )

    if offline is not None and not offline.has_files() and offline.path_entry is None and not data:
        _print_line(f"Found no files of the {APP_DISPLAY_NAME} offline package to remove.")
        if offline.download is not None:
            _print_line(f"The package you ran this from is at {offline.download}; delete it to remove it.")
        _print_kept_data(kept, command_remains=True)
        return 0
    if offline is None and not data:
        _explain_flavor(flavor)
        if purge:
            _print_line(f"There are no settings or sessions to delete in {config_dir}.")
        else:
            _print_kept_data(kept, command_remains=True)
        return 0

    # Without a terminal, only --yes can confirm, so every command suggested from here has it.
    unattended = f"{APP_COMMAND} uninstall{' --purge' if purge else ''} --yes"
    if not assume_yes and not sys.stdin.isatty():
        _describe(offline, data, config_dir)
        _print_error(f"Nothing was removed. To uninstall without a prompt, run '{unattended}'.")
        return 1
    # Windows: PyApp runs this interpreter as a child of its launcher, which must not count.
    ignored = {os.getpid(), os.getppid()} if get_platform().is_windows else {os.getpid()}
    if not _require_no_running_chrys_instances(ignored_pids=ignored, action="uninstall", rerun=unattended):
        _print_warning(f"Could not check every process; quit any {APP_DISPLAY_NAME} still running.")

    _describe(offline, data, config_dir)
    if not assume_yes and not _confirm():
        return 1

    ok = True
    for folder in data:
        ok = _remove(folder) and ok
    if offline is not None:
        ok = _remove_offline_install(offline) and ok
        if ok and get_platform().is_windows and offline.folders:
            _print_success(f"{APP_DISPLAY_NAME} finishes removing its files once this command exits.")
        elif ok:
            _print_success(f"Removed {APP_DISPLAY_NAME}.")
        if offline.download is not None:
            _print_line(
                f"The package you ran this from is still at {offline.download}; delete it if you no longer need it."
            )
    else:
        _explain_flavor(flavor)
    if not purge:
        # The offline package's commands are gone now, so the user deletes the data by hand.
        _print_kept_data(kept, command_remains=offline is None)
    return 0 if ok else 1


def _explain_flavor(flavor: InstallFlavor) -> None:
    """Say how to remove a flavor this command does not remove itself."""
    command = uninstall_command(flavor)
    if command is not None:
        _print_line(f"{APP_DISPLAY_NAME} was installed with {flavor.value}. To remove it, run:")
        _print_line(f"  {command}")
    elif flavor is InstallFlavor.SOURCE:
        _print_line(f"This {APP_DISPLAY_NAME} runs from a source checkout. Delete the checkout to remove it.")


def _print_kept_data(folders: list[Path], *, command_remains: bool) -> None:
    if not folders:
        return
    _print_line(f"Your settings, sessions and other data are kept in {' and '.join(str(f) for f in folders)}.")
    if command_remains:
        _print_line(f"To delete them too, run '{APP_COMMAND} uninstall --purge'.")
    else:
        _print_line(f"Delete {'that folder' if len(folders) == 1 else 'those folders'} if you no longer need them.")
