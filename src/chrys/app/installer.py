# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PyApp installer support for the Chrys CLI."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psutil
from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from chrys import DISTRIBUTION_NAME
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.platform import get_platform
from chrys.kernel.aixcoding.command_migration import LEGACY_APP_COMMAND, remove_legacy_alias

_INSTALLER_THEME = Theme(
    {
        "error": "bold red",
        "warning": "yellow",
        "success": "green",
    }
)

# The binary keeps its original name on PATH; the product-named command is an alias of it.
_INSTALLED_BINARY = "chrys"

# AIxCoding branding: legacy command process recognition
_CHRYS_PROCESS_BASENAMES = frozenset(
    {
        "chrys",
        "chrys.exe",
        APP_COMMAND,
        f"{APP_COMMAND}.exe",
        LEGACY_APP_COMMAND,
        f"{LEGACY_APP_COMMAND}.exe",
        "chrys-runtime",
        "chrys-runtime.exe",
        "chrys-runtimew.exe",
    }
)
_CHRYS_CMDLINE_MARKERS = (
    "chrys.app.cli.",
    "chrys.app.tui.app",
)
_PROCESS_SCAN_ATTRS = ("pid", "name", "exe", "cmdline")
_PROCESS_INFO_UNAVAILABLE = object()


@dataclass(frozen=True)
class _RunningChrysProcess:
    """A Chrys-looking process found by the check before install or uninstall."""

    pid: int
    name: str
    command: str


@dataclass(frozen=True)
class _ChrysProcessScan:
    """Running Chrys processes found by a scan and whether enumeration was complete."""

    processes: tuple[_RunningChrysProcess, ...]
    complete: bool


def _print_text(text: Text) -> None:
    """Render installer text through the current stdout capture/terminal."""
    Console(file=sys.stdout, theme=_INSTALLER_THEME, highlight=False).print(text, soft_wrap=True)


def _print_line(message: str = "", *, style: str | None = None) -> None:
    """Print literal installer text with optional Rich styling."""
    _print_text(Text(message) if style is None else Text(message, style=style))


def _print_status(label: str, message: str, *, style: str, leading_blank: bool = False) -> None:
    """Print a status line with a styled label and literal message body."""
    text = Text("\n" if leading_blank else "")
    text.append(f"{label}: {message}", style=style)
    _print_text(text)


def _print_error(message: str, *, leading_blank: bool = False) -> None:
    _print_status("Error", message, style="error", leading_blank=leading_blank)


def _print_warning(message: str, *, leading_blank: bool = False) -> None:
    _print_status("Warning", message, style="warning", leading_blank=leading_blank)


def _print_success(message: str) -> None:
    _print_status("Success", message, style="success")


def _parse_pyapp_version_key(name: str) -> tuple[int, ...] | None:
    """Parse a leading 'A.B[.C[.D]]' numeric prefix from a folder name to a comparable tuple.

    Trailing suffixes like ``-dev-abc1234`` are tolerated and ignored for ordering.
    Returns ``None`` if the name does not start with a numeric version.
    """
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", name)
    if m is None:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _runtime_interpreter(version_dir: Path) -> Path | None:
    """Return the embedded interpreter of a PyApp version directory, if present."""
    for rel in ("bin/python3", "python.exe"):
        candidate = version_dir / "python" / rel
        if candidate.is_file():
            return candidate
    return None


def _interpreter_binary_tag(path: Path | None) -> tuple[object, ...] | None:
    """Identify the executable format and machine type of ``path`` from its header.

    Reads a few bytes of the ELF / Mach-O / PE header — never executes the
    binary.  Returns ``None`` for a missing file or unrecognized format, which
    callers must treat as "unknown platform", never as a match.
    """
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:4] == b"\x7fELF" and len(head) >= 20:
                # EI_CLASS, EI_DATA, e_machine
                return ("elf", head[4], head[5], head[18:20])
            if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") and len(head) >= 8:
                # Mach-O magic + cputype
                return ("macho", head[:4], head[4:8])
            if head[:2] == b"MZ" and len(head) >= 64:
                f.seek(int.from_bytes(head[60:64], "little"))
                sig = f.read(6)
                if sig[:4] == b"PE\x00\x00":
                    # COFF machine type
                    return ("pe", sig[4:6])
    except OSError:
        return None
    return None


def _pyapp_cache_dir() -> Path | None:
    """Mirror PyApp's platform cache directory (``directories`` crate, project "pyapp")."""
    platform = get_platform()
    if platform.is_windows:
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "pyapp" / "cache" if base else None
    if platform.is_macos:
        return Path.home() / "Library" / "Caches" / "pyapp"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "pyapp"


# PyApp's project directory name: the offline binaries are built with
# PYAPP_PROJECT_NAME=chrys, while the pip and uv flavors embed the wheel and
# PyApp names them after its metadata instead (iCode-TUI, normalized).
_PYAPP_PROJECT_NAMES = frozenset({"chrys", "icode-tui"})


def _is_default_pyapp_version_dir(exe_path: Path, version_dir: Path) -> bool:
    """Return whether ``version_dir`` matches PyApp's default Chrys layout."""
    if any(os.environ.get(f"PYAPP_INSTALL_DIR_{name.upper()}") for name in _PYAPP_PROJECT_NAMES):
        return False

    try:
        exe_path.relative_to(version_dir / "python")
    except ValueError:
        return False

    distribution_dir = version_dir.parent
    project_dir = distribution_dir.parent
    if distribution_dir == version_dir or project_dir == distribution_dir:
        return False
    if project_dir.name not in _PYAPP_PROJECT_NAMES:
        return False

    data_dir = project_dir.parent
    if data_dir.name == "pyapp":
        return True
    return data_dir.name == "data" and data_dir.parent.name == "pyapp"


def _find_pyapp_version_dir(exe_path: Path) -> Path | None:
    """Find the current PyApp version directory, only for the known-safe default layout."""
    for ancestor in exe_path.parents:
        if _parse_pyapp_version_key(ancestor.name) is None:
            continue
        if _is_default_pyapp_version_dir(exe_path, ancestor):
            return ancestor
    return None


def _prune_old_pyapp_versions() -> None:
    """Remove PyApp version folders older than the current one, keeping the most recent older as a fallback.

    PyApp's installation directory layout (see https://github.com/ofek/pyapp/blob/master/src/app.rs)::

        <data_local_dir>/pyapp/<project>/<distribution_id>/<version>/

    Where ``data_local_dir`` resolves to:

    * Windows: ``%LOCALAPPDATA%\\pyapp\\data`` (the ``directories`` crate appends ``\\data`` on Windows only)
    * macOS:   ``~/Library/Application Support/pyapp``
    * Linux:   ``$XDG_DATA_HOME/pyapp`` or ``~/.local/share/pyapp``

    With ``PYAPP_FULL_ISOLATION=true`` (every flavor we ship), the embedded Python sits at
    ``<version>/python/python.exe`` (Windows) or ``<version>/python/bin/python3`` (Unix), so the
    version folder is always an ancestor of ``sys.executable``.  Pruning spans every
    ``<distribution_id>`` under the project directory: the distribution ID is the hash of the
    embedded archive, and the offline flavor bakes the versioned chrys wheel into its archive, so
    every release — and every rebuild of the same version — ships a new distribution ID.  Scanning
    only the current ID would strand each superseded runtime (hundreds of MB) forever.  A runtime
    of the current version under another ID is a superseded rebuild and is removed alongside older
    versions.  Distribution IDs also differ by architecture (a home directory can be shared
    between machines), so a runtime under another ID is only touched when its interpreter's binary
    header matches the running one; the fallback is likewise chosen among matching runtimes only.

    Distribution directories emptied by the removals are dropped together with their PyApp cache
    leftovers: the cached copy of the embedded archive (``<cache>/distributions/<id>``, a plain
    re-creatable file) and this project's per-version installation locks.  The shared unpacked
    base ``<cache>/distributions/_<id>`` used by non-full-isolation apps is never touched — other
    PyApp-packaged applications symlink into it.

    Called from ``chrys install`` so users opt in by re-running install on a new version.
    """

    def _force_remove(func, path, exc):
        # A read-only bit (common on Windows) blocks deleting; clear it and retry. Any other
        # failure, such as a folder that cannot be read, stands.
        if func not in (os.unlink, os.rmdir):
            raise exc
        os.chmod(path, stat.S_IWRITE)
        func(path)

    try:
        exe_path = Path(sys.executable).resolve()

        # Find the version folder by walking up, but only accept the expected
        # default PyApp layout. Install-dir overrides are intentionally skipped:
        # their structure is user-defined, so sibling pruning is not safe.
        version_dir = _find_pyapp_version_dir(exe_path)
        if version_dir is None:
            return  # Not running from a PyApp version folder (dev/source install) — nothing to prune.

        distribution_dir = version_dir.parent
        project_dir = distribution_dir.parent
        if not project_dir.is_dir():
            return

        current_key = _parse_pyapp_version_key(version_dir.name)
        if current_key is None:
            return

        current_resolved = version_dir.resolve()
        current_tag = _interpreter_binary_tag(exe_path)

        # A symlinked (or junction'd) entry would make the scan — and the
        # shutil.rmtree below — operate on paths outside the PyApp tree.  Only
        # descend into real directories whose resolved parent is still the
        # project directory, and never consider symlinked version folders.
        project_resolved = project_dir.resolve()
        superseded: list[Path] = []  # current version under an obsolete distribution ID (rebuilds)
        older: list[tuple[tuple[int, ...], Path]] = []
        for dist_dir in project_dir.iterdir():
            if dist_dir.is_symlink() or dist_dir.is_junction() or not dist_dir.is_dir():
                continue
            if dist_dir.resolve().parent != project_resolved:
                continue
            same_distribution = dist_dir == distribution_dir
            for child in dist_dir.iterdir():
                if child.is_symlink() or child.is_junction() or not child.is_dir():
                    continue
                if child.resolve() == current_resolved:
                    continue
                key = _parse_pyapp_version_key(child.name)
                if key is None or key > current_key:
                    continue
                # Within the current distribution ID the platform matches by
                # construction; across IDs, only a runtime whose interpreter
                # header provably matches the running binary belongs to this
                # installation (IDs also differ by architecture).
                if not same_distribution and (
                    current_tag is None or _interpreter_binary_tag(_runtime_interpreter(child)) != current_tag
                ):
                    continue
                if key == current_key:
                    superseded.append(child)
                else:
                    older.append((key, child))

        # Sort descending by version; keep older[0] (most recent older) as fallback.  Superseded
        # rebuilds of the current version are never useful as a fallback — always remove them.
        older.sort(key=lambda t: t[0], reverse=True)
        kept = older[0][1].name if older else None
        to_remove = [(d, f"superseded rebuild of {d.name}") for d in superseded]
        to_remove += [(d, f"old version {d.name}") for _, d in older[1:]]
        if not to_remove:
            return

        _print_line(f"\nCleaning up old {APP_DISPLAY_NAME} versions in {project_dir}:")
        removed: list[Path] = []
        for d, label in to_remove:
            try:
                shutil.rmtree(str(d), onexc=_force_remove)
                _print_line(f"  Removed {label}.", style="success")
                removed.append(d)
            except OSError as e:
                _print_warning(f"Could not remove {label}: {e}")

        # PyApp also keeps a copy of every embedded archive in its platform
        # cache, keyed by the same distribution ID, plus a lock file per
        # installed version — reclaim the entries belonging to what was just
        # removed.  Both are re-creatable, but never touch entries that still
        # back a surviving runtime.
        cache_dir = _pyapp_cache_dir()
        if cache_dir is not None:
            for d in removed:
                lock = cache_dir / "locks" / f"installation-{project_dir.name}-{d.parent.name}-{d.name}"
                with contextlib.suppress(OSError):
                    lock.unlink(missing_ok=True)

        # Each offline release strands the previous release's <distribution_id>
        # directory; the removals above empty it, so drop the husk too.
        # ``rmdir`` refuses non-empty directories, which keeps this safe for
        # distribution dirs still holding the fallback or unexpected content.
        # Its cached archive is deleted only after the rmdir succeeds, i.e.
        # only once no runtime of that distribution ID remains.
        for dist_dir in project_dir.iterdir():
            if dist_dir.is_symlink() or dist_dir.is_junction() or not dist_dir.is_dir():
                continue
            if dist_dir == distribution_dir:
                continue
            with contextlib.suppress(OSError):
                dist_dir.rmdir()
                if cache_dir is not None:
                    (cache_dir / "distributions" / dist_dir.name).unlink(missing_ok=True)

        if removed:
            kept_summary = f"{version_dir.name}, {kept}" if kept else version_dir.name
            _print_success(f"Removed {len(removed)} obsolete runtime(s); kept {kept_summary}.")
    except Exception as e:
        _print_warning(f"Could not clean up old {APP_DISPLAY_NAME} versions: {e}")


def _find_windows_powershell() -> str:
    """Find a PowerShell executable for installer helper commands."""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return "powershell"


def _path_basename(value: str | None) -> str:
    """Return a lowercase basename for a possibly Windows-style path."""
    if not value:
        return ""
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _process_command(cmdline: list[str] | tuple[str, ...] | None) -> str:
    """Return a readable process command line."""
    if not cmdline:
        return ""
    return " ".join(str(part) for part in cmdline)


def _is_python_host_command(cmdline: list[str] | tuple[str, ...] | None) -> bool:
    """Return whether cmdline looks like a Python interpreter process."""
    if not cmdline:
        return False
    first_arg = _path_basename(str(cmdline[0]))
    return (
        re.fullmatch(
            r"(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?",
            first_arg,
        )
        is not None
    )


def _is_shell_command(program: str) -> bool:
    """Return whether ``program`` names a POSIX shell."""
    return re.fullmatch(r"(?:ba|z|da|k|fi)?sh", _path_basename(program)) is not None


def _is_chrys_process(name: str | None, exe: str | None, cmdline: list[str] | tuple[str, ...] | None) -> bool:
    """Return whether process metadata looks like a Chrys runtime.

    Only the program and, for a Python or shell host, the script it runs can name Chrys: the
    uv and pipx entry points run as ``python …/bin/icode``. Any later argument is data, such as a
    folder called iCode or the ``icode`` that ``uv run`` starts in a child of its own.
    """
    if _path_basename(name) in _CHRYS_PROCESS_BASENAMES:
        return True
    if _path_basename(exe) in _CHRYS_PROCESS_BASENAMES:
        return True

    args = [str(arg) for arg in cmdline or ()]
    if not args:
        return False
    if _path_basename(args[0]) in _CHRYS_PROCESS_BASENAMES:
        return True
    is_python_host = _is_python_host_command(cmdline)
    if is_python_host or _is_shell_command(args[0]):
        script = next((arg for arg in args[1:] if not arg.startswith("-")), None)
        if _path_basename(script) in _CHRYS_PROCESS_BASENAMES:
            return True
    return is_python_host and any(marker in arg for arg in args for marker in _CHRYS_CMDLINE_MARKERS)


def _current_install_process_ids() -> set[int]:
    """Return the installer process plus ancestor PIDs to exclude from checks."""
    ignored = {os.getpid()}
    with contextlib.suppress(psutil.Error, OSError, TypeError, ValueError):
        ignored.update(parent.pid for parent in psutil.Process().parents())
    return ignored


def _find_running_chrys_processes(*, ignored_pids: set[int] | None = None) -> _ChrysProcessScan:
    """Find other Chrys runtimes and report whether every process could be inspected."""
    ignored = ignored_pids or set()
    running: list[_RunningChrysProcess] = []
    complete = True
    try:
        for proc in psutil.process_iter(_PROCESS_SCAN_ATTRS, ad_value=_PROCESS_INFO_UNAVAILABLE):
            try:
                info = proc.info
                pid = int(info.get("pid") or proc.pid)
                if pid in ignored:
                    continue
                name = info.get("name")
                if name is _PROCESS_INFO_UNAVAILABLE:
                    name = None
                exe = info.get("exe")
                if exe is _PROCESS_INFO_UNAVAILABLE:
                    exe = None
                cmdline = info.get("cmdline")
                if cmdline is _PROCESS_INFO_UNAVAILABLE:
                    cmdline = None
                name = str(name or "")
                if not _is_chrys_process(name, str(exe) if exe else None, cmdline):
                    # Classification runs on whatever fields are readable, so a positive match (e.g. an
                    # elevated Chrys on Windows whose snapshot name stays visible while its command line
                    # is access-denied) is never lost. Unreadable fields cannot rescue a negative verdict:
                    # PyApp runtimes that can reference version directories under this user's home run as
                    # ordinary same-user processes whose fields are readable, so access-denied fields
                    # belong to processes outside the cleanup scope (other users, setuid processes, or
                    # zombies). A root Chrys launched via sudo with this user's HOME preserved is an
                    # accepted exception: the unprivileged installer cannot reliably identify it.
                    continue
                running.append(
                    _RunningChrysProcess(pid=pid, name=name or "(unknown)", command=_process_command(cmdline))
                )
            except psutil.Error, OSError, TypeError, ValueError:
                complete = False
                continue
    except (psutil.Error, OSError, TypeError, ValueError) as e:
        _print_warning(f"Could not check for running {APP_DISPLAY_NAME} processes: {e}.")
        return _ChrysProcessScan(
            processes=tuple(sorted(running, key=lambda process: process.pid)),
            complete=False,
        )
    return _ChrysProcessScan(processes=tuple(sorted(running, key=lambda process: process.pid)), complete=complete)


def _require_no_running_chrys_instances(
    *, ignored_pids: set[int] | None = None, action: str = "install", rerun: str | None = None
) -> bool:
    """Block until known Chrys instances exit, returning whether the final scan was complete.

    ``ignored_pids`` defaults to this process and its ancestors; ``action`` names the command in
    the messages, and ``rerun`` is the command line to suggest when there is no terminal to wait in.
    """
    if ignored_pids is None:
        ignored_pids = _current_install_process_ids()
    while True:
        scan = _find_running_chrys_processes(ignored_pids=ignored_pids)
        running = scan.processes
        if not running:
            return scan.complete

        _print_error(f"{APP_DISPLAY_NAME} is already running.", leading_blank=True)
        _print_line(f"Quit or kill all running {APP_DISPLAY_NAME} instances before continuing with {action}:")
        for process in running:
            summary = f"  - PID {process.pid}: {process.name}"
            if process.command:
                summary += f" - {process.command[:160]}"
            _print_line(summary)

        if not sys.stdin.isatty():
            _print_error(
                f"Rerun '{rerun or f'{APP_COMMAND} {action}'}' after those processes have exited.", leading_blank=True
            )
            sys.exit(1)

        response = input("\nPress Enter after quitting them to check again, or type 'q' to abort: ").strip().casefold()
        if response in {"q", "quit", "abort", "n", "no"}:
            _print_error(f"{action.capitalize()} aborted.")
            sys.exit(1)


def _atomic_copy_executable(src: Path, dest: Path) -> None:
    """Copy an executable into place without exposing a partially written destination.

    The temporary file lives beside ``dest`` so ``os.replace`` cannot cross a filesystem boundary.
    Copying through a distinct path also supports reinstalling from ``dest`` itself.
    """
    fd, temporary_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    temporary_path = Path(temporary_name)
    try:
        os.close(fd)
        shutil.copy2(str(src), str(temporary_path))
        temporary_path.chmod(0o755)
        os.replace(temporary_path, dest)
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink()


def _install_command_alias(src: Path, dest: Path) -> Path | None:
    """Make the product-named command start the binary installed at ``dest`` from ``src``.

    Returns the alias, or ``None`` when there is none to use. POSIX shares ``~/.local/bin`` with
    other tools, so a command of the same name that starts something else is left alone there.
    The Windows directory is the installer's own, and symlinks need privileges there, so it
    takes a hard link or, failing that, a copy.
    """
    alias = dest.with_name(APP_COMMAND + dest.suffix)
    try:
        # An alias that is the running binary was just copied to ``dest``, so it is current too,
        # and Windows would refuse to unlink it.
        if alias.exists() and (alias.samefile(dest) or alias.samefile(src)):
            # AIxCoding branding: legacy alias migration hook
            remove_legacy_alias(binary=dest, warn=_print_warning)
            return alias
        if get_platform().is_windows:
            alias.unlink(missing_ok=True)
            try:
                os.link(dest, alias)
            except OSError:
                shutil.copy2(str(dest), str(alias))
        else:
            if alias.exists():
                _print_warning(f"{alias} already exists and starts something else, so it was left alone.")
                return None
            # Only a dangling symlink can remain here.
            alias.unlink(missing_ok=True)
            alias.symlink_to(dest.name)
    except OSError as e:
        _print_warning(f"Could not create the '{APP_COMMAND}' command at {alias}: {e}")
        return None
    remove_legacy_alias(binary=dest, warn=_print_warning)
    return alias


def _installed_commands(alias: Path | None) -> str:
    """Name the commands that now start the app, the product-named one first."""
    return f"'{APP_COMMAND}' or '{_INSTALLED_BINARY}'" if alias is not None else f"'{_INSTALLED_BINARY}'"


def install_to_path() -> None:
    """Copy the running PyApp binary to a PATH-friendly location."""
    binary = os.environ.get("PYAPP", "")
    if not binary:
        # Installed with uv (or pip), whose own commands put iCode on PATH and upgrade it.
        _print_error(f"{APP_COMMAND} install is only for the offline packages.")
        _print_line(
            f"If you installed {APP_DISPLAY_NAME} with uv, it is already set up: run 'uv tool update-shell' if your "
            f"terminal cannot find '{APP_COMMAND}', and 'uv tool upgrade {DISTRIBUTION_NAME}' to upgrade."
        )
        sys.exit(1)
    if binary == "1" or not Path(binary).is_file():
        _print_error(f"{APP_COMMAND} install only works when running from a PyApp binary.")
        _print_line("PYAPP_PASS_LOCATION must be enabled at build time.")
        sys.exit(1)

    src = Path(binary)
    platform_info = get_platform()
    scan_complete = True

    if platform_info.is_windows:
        scan_complete = _require_no_running_chrys_instances()
        install_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "chrys" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        dest = install_dir / f"{_INSTALLED_BINARY}.exe"
        # Installing from the installed binary, or from its alias, has nothing to copy.
        if not (dest.exists() and src.samefile(dest)):
            try:
                shutil.copy2(str(src), str(dest))
            except OSError as e:
                # A running copy keeps its file locked. The check above leaves out the processes
                # this install runs under, such as an iCode whose terminal started it.
                _print_error(f"Could not replace {dest}: {e}")
                _print_line(f"Quit every running {APP_DISPLAY_NAME}, then install again.")
                sys.exit(1)
        alias = _install_command_alias(src, dest)
        _print_success(f"Installed {APP_DISPLAY_NAME} to {dest}. Run it with {_installed_commands(alias)}.")

        # Add to user PATH if not already there
        from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

        result = subprocess.run(
            [
                _find_windows_powershell(),
                "-NoProfile",
                "-Command",
                (
                    # A failed .NET call ends only its own statement, so without "Stop" a refused
                    # write would still report the entry as added.
                    '$ErrorActionPreference = "Stop"; '
                    f'$p = [Environment]::GetEnvironmentVariable("Path","User"); '
                    f'if ($p -notlike "*{install_dir}*") {{ '
                    f'[Environment]::SetEnvironmentVariable("Path", "{install_dir};$p", "User"); '
                    f'"Added {install_dir} to user PATH (restart terminal to take effect)" }} '
                    f'else {{ "Already in PATH" }}'
                ),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **windows_hidden_subprocess_kwargs(),
        )
        if result.returncode == 0:
            path_message = result.stdout.strip()
            if path_message:
                _print_line(path_message)
        else:
            _print_warning(f"{APP_DISPLAY_NAME} was installed, but updating the user PATH failed.")
            detail = result.stderr.strip() or result.stdout.strip()
            if detail:
                _print_line(detail)
    else:
        install_dir = Path.home() / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        dest = install_dir / _INSTALLED_BINARY
        _atomic_copy_executable(src, dest)
        alias = _install_command_alias(src, dest)
        _print_success(f"Installed {APP_DISPLAY_NAME} to {dest}.")

        # Check if ~/.local/bin is in PATH
        path_dirs = os.environ.get("PATH", "").split(":")
        if str(install_dir) not in path_dirs:
            shell = os.environ.get("SHELL", "")
            if "zsh" in shell:
                rc_file = "~/.zshrc"
            elif "bash" in shell:
                rc_file = "~/.bashrc"
            else:
                rc_file = "~/.profile"
            _print_warning(f"{install_dir} is not currently on PATH.", leading_blank=True)
            _print_line(f"Add this to {rc_file}:")
            _print_line('  export PATH="$HOME/.local/bin:$PATH"')
            _print_line("Restart your terminal after updating the file.")
        else:
            _print_success(
                f"{APP_DISPLAY_NAME} is already on PATH. You can run {_installed_commands(alias)} from anywhere."
            )

    running_instances: tuple[_RunningChrysProcess, ...] = ()
    if not platform_info.is_windows:
        # Unlike the Windows preflight, ancestors are not exempt here: installing from a Chrys
        # embedded terminal makes the still-running TUI an ancestor of this process, and its
        # version directory must stay visible to the cleanup gate. PyApp execs on POSIX, so the
        # installer itself is the only process of its own in the chain.
        scan = _find_running_chrys_processes(ignored_pids={os.getpid()})
        running_instances = scan.processes
        scan_complete = scan.complete
    if not scan_complete:
        _print_warning(
            f"Could not fully check for running {APP_DISPLAY_NAME} instances; skipped cleanup of old versions."
        )
    elif running_instances:
        _print_warning(f"Detected running {APP_DISPLAY_NAME} instances; skipped cleanup of old versions.")
    else:
        _prune_old_pyapp_versions()
