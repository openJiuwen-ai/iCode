# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one-line install scripts, run against release files served from a temp folder.

``install.sh`` downloads through a fake ``curl`` on PATH and ``install.ps1`` through stand-ins
for the web cmdlets, so the release hosts never see these tests. Each served file is named after
its URL with ``/`` and ``:`` replaced by ``_``, and every URL the script asks for is logged.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT

_INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
_INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
_GITHUB = "https://github.com/openJiuwen-ai/iCode/releases"
_GITCODE = "https://gitcode.com/openJiuwen/iCode/releases"
_GITCODE_API = "https://api.gitcode.com/api/v5/repos/openJiuwen/iCode/releases/latest"
_GITCODE_PYPROJECT = "https://raw.gitcode.com/openJiuwen/iCode/raw/main/pyproject.toml"
_LATEST = "0.30.0"


@dataclass
class _Server:
    root: Path

    def serve(self, url: str, payload: bytes) -> None:
        (self.root / url.replace("/", "_").replace(":", "_")).write_bytes(payload)

    def release(self, base: str, tag: str, packages: dict[str, bytes], *, sums_at: str | None = None) -> None:
        """Serve a release's packages and its SHA256SUMS.txt (at ``sums_at``, if it differs)."""
        sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in packages.items())
        sums += f"{'0' * 64}  icode_tui-{tag.removeprefix('v')}-py3-none-any.whl\n"
        self.serve(sums_at or f"{base}/download/{tag}/SHA256SUMS.txt", sums.encode())
        for name, data in packages.items():
            self.serve(f"{base}/download/{tag}/{name}", data)

    def requests(self) -> list[str]:
        log = self.root / "requests.log"
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def _server(tmp_path: Path) -> _Server:
    root = tmp_path / "served"
    root.mkdir()
    return _Server(root)


# --- install.sh ---------------------------------------------------------------------------------

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is the macOS and Linux installer.")

_FAKE_CURL = r"""#!/bin/sh
out=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        --retry | --connect-timeout | --speed-limit | --speed-time) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
printf '%s\n' "$url" >> "$FAKE_SERVED/requests.log"
file="$FAKE_SERVED/$(printf '%s' "$url" | tr '/:' '__')"
[ -f "$file" ] || exit 22
cp "$file" "$out"
"""

# The package's binary records how it was started instead of installing anything.
_FAKE_ICODE = '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$HOME/icode-ran"\n'


def _tarball(binary: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = binary.encode()
        info = tarfile.TarInfo("aixcoding-cli")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _linux_packages(version: str = _LATEST, *, binary: str = _FAKE_ICODE) -> dict[str, bytes]:
    return {
        f"aixcoding-cli-linux-aarch64-v{version}-offline.tar.gz": _tarball("#!/bin/sh\nexit 3\n"),
        f"aixcoding-cli-linux-x86_64-v{version}-offline.tar.gz": _tarball(binary),
    }


@dataclass
class _ShMachine:
    home: Path
    env: dict[str, str]
    server: _Server

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(_INSTALL_SH), *args],
            env=self.env,
            cwd=self.home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            # A test may run it twice within pytest's 60 s limit.
            timeout=25,
            check=False,
        )

    def run_piped(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run it as ``curl ... | sh -s -- <args>`` does, reading the script from standard input."""
        return subprocess.run(
            ["/bin/sh", "-s", "--", *args],
            env=self.env,
            cwd=self.home,
            input=_INSTALL_SH.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    def installed_with(self) -> str | None:
        ran = self.home / "icode-ran"
        return ran.read_text(encoding="utf-8").strip() if ran.is_file() else None

    def chrys_ran(self) -> str | None:
        ran = self.home / "chrys-ran"
        return ran.read_text(encoding="utf-8").strip() if ran.is_file() else None

    def install_old_copy(self, version: str, *, alias: bool = True) -> None:
        """An earlier offline install: a `chrys` that reports ``version`` and its `aixcoding-cli` alias."""
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "chrys").write_text(
            f'#!/bin/sh\n[ "$1" = install ] && {{ echo install > "$HOME/chrys-ran"; exit 0; }}\necho {version}\n',
            encoding="utf-8",
        )
        (bin_dir / "chrys").chmod(0o755)
        if alias:
            (bin_dir / "aixcoding-cli").symlink_to("chrys")


def _sh_machine(tmp_path: Path, *, ldd: str = "ldd (GNU libc) 2.35") -> _ShMachine:
    """A Linux x86-64 machine with an empty home, whose `curl` reads from the test's server."""
    server = _server(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "curl": _FAKE_CURL,
        "uname": '#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n',
        "ldd": f"#!/bin/sh\necho '{ldd}'\n",
    }.items():
        (fake_bin / name).write_text(body, encoding="utf-8")
        (fake_bin / name).chmod(0o755)
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin", "TMPDIR": str(temp), "FAKE_SERVED": str(server.root)}
    return _ShMachine(home, env, server)


@_POSIX_ONLY
@pytest.mark.parametrize("how", ["file", "piped"])
# AIxCoding branding: install script contract tests
def test_install_sh_installs_the_latest_github_release(tmp_path: Path, how: str) -> None:
    machine = _sh_machine(tmp_path)
    latest_sums = f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    machine.server.release(_GITHUB, f"v{_LATEST}", _linux_packages(), sums_at=latest_sums)

    result = machine.run() if how == "file" else machine.run_piped()

    assert result.returncode == 0, result.stderr
    assert machine.installed_with() == "install"
    assert machine.server.requests() == [
        latest_sums,
        f"{_GITHUB}/download/v{_LATEST}/aixcoding-cli-linux-x86_64-v{_LATEST}-offline.tar.gz",
    ]
    assert f"Downloading AIxCoding {_LATEST} for linux (x86_64) from GitHub..." in result.stdout


@_POSIX_ONLY
@pytest.mark.parametrize("finder", ["api", "pyproject"])
def test_install_sh_falls_back_to_gitcode_and_finds_its_latest_release(tmp_path: Path, finder: str) -> None:
    machine = _sh_machine(tmp_path)
    if finder == "api":
        machine.server.serve(_GITCODE_API, json.dumps({"tag_name": f"v{_LATEST}", "name": "x"}).encode())
    else:
        # The API's shared anonymous quota ran out, so the version on main stands in.
        machine.server.serve(_GITCODE_PYPROJECT, f'[project]\nname = "iCode-TUI"\nversion = "{_LATEST}"\n'.encode())
    machine.server.release(_GITCODE, f"v{_LATEST}", _linux_packages())

    result = machine.run()

    assert result.returncode == 0, result.stderr
    assert machine.installed_with() == "install"
    assert "Could not install from GitHub." in result.stdout
    assert f"{_GITCODE}/download/v{_LATEST}/SHA256SUMS.txt" in machine.server.requests()


@_POSIX_ONLY
def test_install_sh_runs_nothing_from_a_damaged_download(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    machine.server.release(_GITHUB, "v0.29.1", _linux_packages("0.29.1"))
    # The package served differs from the one SHA256SUMS.txt describes.
    for name, data in _linux_packages("0.29.1").items():
        machine.server.serve(f"{_GITHUB}/download/v0.29.1/{name}", data + b"!")

    result = machine.run("--version", "v0.29.1", "--source", "github")

    assert result.returncode == 1
    assert machine.installed_with() is None
    assert "is damaged" in result.stdout
    assert machine.server.requests()[0] == f"{_GITHUB}/download/v0.29.1/SHA256SUMS.txt"
    assert not any(_GITCODE in url for url in machine.server.requests())


@_POSIX_ONLY
def test_install_sh_points_at_the_releases_when_no_host_has_that_version(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)

    result = machine.run("--version", "0.29.9")

    assert result.returncode == 1
    assert machine.installed_with() is None
    # Both hosts were asked, and the message does not blame the network alone.
    assert f"{_GITCODE}/download/v0.29.9/SHA256SUMS.txt" in machine.server.requests()
    assert (
        "Error: could not install AIxCoding 0.29.9. Check that this version is listed at"
        " https://github.com/openJiuwen-ai/iCode/releases and that you are online."
    ) in result.stderr


@_POSIX_ONLY
# AIxCoding branding: install script contract tests
def test_install_sh_skips_the_download_when_that_version_is_installed(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    machine.install_old_copy(_LATEST)
    machine.server.release(_GITHUB, f"v{_LATEST}", _linux_packages())

    result = machine.run("--version", _LATEST)

    assert result.returncode == 0, result.stderr
    assert f"AIxCoding {_LATEST} is already installed." in result.stdout
    assert machine.server.requests() == [f"{_GITHUB}/download/v{_LATEST}/SHA256SUMS.txt"]
    assert machine.installed_with() is None


@_POSIX_ONLY
def test_install_sh_installs_again_when_the_aixcoding_cli_alias_is_gone(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    machine.install_old_copy(_LATEST, alias=False)
    machine.server.release(_GITHUB, f"v{_LATEST}", _linux_packages())

    result = machine.run("--version", _LATEST)

    assert result.returncode == 0, result.stderr
    assert machine.installed_with() == "install"


@_POSIX_ONLY
@pytest.mark.parametrize("alias", [True, False], ids=["complete", "alias-gone"])
def test_install_sh_does_not_downgrade_to_a_host_that_lags_behind(tmp_path: Path, alias: bool) -> None:
    machine = _sh_machine(tmp_path)
    machine.install_old_copy("0.31.0", alias=alias)
    # GitHub is out of reach, and GitCode has not got the newest release yet.
    machine.server.serve(_GITCODE_API, json.dumps({"tag_name": f"v{_LATEST}"}).encode())
    machine.server.release(_GITCODE, f"v{_LATEST}", _linux_packages())

    result = machine.run()

    assert result.returncode == 0, result.stderr
    assert f"AIxCoding 0.31.0 is already installed, which is newer than the latest on GitCode ({_LATEST})." in (
        result.stdout
    )
    assert machine.installed_with() is None
    # The installed copy puts its own alias back.
    assert machine.chrys_ran() == (None if alias else "install")


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("status", "message"),
    [(3, "aixcoding-cli install failed"), (126, "set TMPDIR to another folder")],
    ids=["install-failed", "temp-folder-cannot-run-programs"],
)
def test_install_sh_fails_when_aixcoding_cli_install_does(tmp_path: Path, status: int, message: str) -> None:
    machine = _sh_machine(tmp_path)
    packages = _linux_packages(binary=f"#!/bin/sh\nexit {status}\n")
    machine.server.release(_GITHUB, f"v{_LATEST}", packages, sums_at=f"{_GITHUB}/latest/download/SHA256SUMS.txt")

    result = machine.run()

    assert result.returncode == 1
    assert message in result.stderr
    # Not the download's fault, so GitCode is not tried.
    assert not any(_GITCODE in url for url in machine.server.requests())


@_POSIX_ONLY
# AIxCoding branding: install script contract tests
def test_install_sh_leaves_an_aixcoding_cli_it_did_not_install_alone(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    bin_dir = machine.home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "aixcoding-cli").write_text("#!/bin/sh\n", encoding="utf-8")

    result = machine.run()

    assert result.returncode == 1
    assert f"{bin_dir / 'aixcoding-cli'} is not an AIxCoding offline install" in result.stderr
    assert "uv tool upgrade iCode-TUI" in result.stderr
    assert machine.server.requests() == []


@_POSIX_ONLY
def test_install_sh_upgrades_an_offline_install_whose_aixcoding_cli_starts_something_else(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    # Installed by hand next to another program's `aixcoding-cli`, so it is started with `chrys`.
    machine.install_old_copy("0.29.0", alias=False)
    bin_dir = machine.home / ".local" / "bin"
    (bin_dir / "aixcoding-cli").write_text("#!/bin/sh\n", encoding="utf-8")
    machine.server.release(_GITHUB, f"v{_LATEST}", _linux_packages())

    result = machine.run("--version", _LATEST)

    assert result.returncode == 0, result.stderr
    assert (
        f"Warning: {bin_dir / 'aixcoding-cli'} does not start the AIxCoding offline install; start that with chrys."
    ) in result.stderr
    assert machine.installed_with() == "install"


@_POSIX_ONLY
def test_install_sh_installs_when_only_the_legacy_icode_alias_exists(tmp_path: Path) -> None:
    # A pre-rename install: only the legacy `icode` alias, so the install is incomplete
    # under the new command and runs again (the real installer migrates the alias).
    machine = _sh_machine(tmp_path)
    machine.install_old_copy(_LATEST, alias=False)
    bin_dir = machine.home / ".local" / "bin"
    (bin_dir / "icode").symlink_to("chrys")
    machine.server.release(_GITHUB, f"v{_LATEST}", _linux_packages())

    result = machine.run("--version", _LATEST)

    assert result.returncode == 0, result.stderr
    assert machine.installed_with() == "install"
    # The script itself never removes the legacy alias; that is the installer's job.
    assert (bin_dir / "icode").is_symlink()


@_POSIX_ONLY
def test_install_sh_does_not_block_on_an_external_legacy_icode(tmp_path: Path) -> None:
    # An `icode` that is not ours is no longer the installer's command: it neither
    # blocks the install nor gets touched.
    machine = _sh_machine(tmp_path)
    bin_dir = machine.home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "icode").write_text("#!/bin/sh\n", encoding="utf-8")
    machine.server.release(
        _GITHUB, f"v{_LATEST}", _linux_packages(), sums_at=f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    )

    result = machine.run()

    assert result.returncode == 0, result.stderr
    assert machine.installed_with() == "install"
    assert (bin_dir / "icode").read_text(encoding="utf-8") == "#!/bin/sh\n"


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "musl"),
        (("--version", "latest"), "--version takes a version number"),
        (("--version=",), "--version needs a value"),
        (("--version", ""), "--version needs a value"),
        (("--source", "pypi"), "--source must be github or gitcode"),
        (("--source=",), "--source needs a value"),
        (("--source", ""), "--source needs a value"),
    ],
    ids=[
        "musl",
        "bad-version",
        "empty-version",
        "empty-version-separated",
        "bad-source",
        "empty-source",
        "empty-source-separated",
    ],
)
def test_install_sh_refuses_what_it_cannot_install(tmp_path: Path, args: tuple[str, ...], message: str) -> None:
    machine = _sh_machine(tmp_path, ldd="musl libc (x86_64)" if not args else "ldd (GNU libc) 2.35")

    result = machine.run(*args)

    assert result.returncode == 1
    assert message in result.stderr
    assert machine.server.requests() == []


# --- install.ps1 --------------------------------------------------------------------------------

# Stand-ins the script's commands resolve to before the real cmdlets. The download stand-in
# takes the stall timeouts of pwsh 7.4 and later, as the real cmdlet there does.
_PS_STUBS = r"""
$ErrorActionPreference = 'Stop'
function Get-Served([string]$Uri) {
    Add-Content -LiteralPath (Join-Path $env:FAKE_SERVED 'requests.log') -Value $Uri
    $file = Join-Path $env:FAKE_SERVED ($Uri -replace '[/:]', '_')
    if (-not (Test-Path -LiteralPath $file)) { throw "404 Not Found: $Uri" }
    $file
}
function Invoke-WebRequest(
    [string]$Uri, [string]$OutFile, [int]$TimeoutSec, [switch]$UseBasicParsing,
    [int]$ConnectionTimeoutSeconds, [int]$OperationTimeoutSeconds
) {
    if ($OperationTimeoutSeconds) {
        Add-Content -LiteralPath (Join-Path $env:FAKE_SERVED 'stall-bounded.log') -Value $Uri
    }
    Copy-Item -LiteralPath (Get-Served $Uri) -Destination $OutFile
}
function Invoke-RestMethod([string]$Uri, [int]$TimeoutSec, [switch]$UseBasicParsing) {
    $text = Get-Content -LiteralPath (Get-Served $Uri) -Raw
    if ($Uri -like '*/releases/latest') { $text | ConvertFrom-Json } else { $text }
}
function Start-Process([string]$FilePath, [string[]]$ArgumentList, [switch]$NoNewWindow, [switch]$PassThru) {
    $binary = (Get-Content -LiteralPath $FilePath -Raw).Trim()
    Set-Content -LiteralPath (Join-Path $env:FAKE_SERVED 'started.txt') -Value "$binary|$($ArgumentList -join ' ')|$NoNewWindow"
    $exitCode = if ($env:FAKE_EXIT_CODE) { [int]$env:FAKE_EXIT_CODE } else { 0 }
    [pscustomobject]@{ Handle = 1; ExitCode = $exitCode } |
        Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { } -PassThru
}
"""


def _powershell(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not installed")
    return path


# Windows PowerShell 5.1 is what `irm | iex` usually runs in; pwsh runs on every OS.
_SHELLS = pytest.mark.parametrize(
    "shell",
    [
        "pwsh",
        pytest.param("powershell", marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")),
    ],
)


def _zip(binary: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("aixcoding-cli.exe", binary)
    return buffer.getvalue()


def _windows_packages(version: str = _LATEST) -> dict[str, bytes]:
    return {
        f"aixcoding-cli-windows-{arch}-v{version}-offline.zip": _zip(f"fake icode {arch}")
        for arch in ("x86_64", "aarch64")
    }


@dataclass
class _PsMachine:
    shell: str
    tmp_path: Path
    env: dict[str, str]
    server: _Server

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        """Run it as ``irm ... | iex`` does: the script's text, under the default execution policy.

        Windows PowerShell's default policy loads no script module, which is what Get-FileHash and
        Expand-Archive come from there; an installer that needs one fails only when run this way.
        """
        command = _PS_STUBS + "Get-Content -Raw -LiteralPath $env:INSTALL_PS1 | Invoke-Expression\n"
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        argv = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-EncodedCommand", encoded]
        return self._run(argv, env)

    def run_file(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the file with parameters, as ``install.ps1 -Version ...`` does."""
        runner = self.tmp_path / "run.ps1"
        runner.write_text(_PS_STUBS + "& $env:INSTALL_PS1 @args\n", encoding="utf-8")
        return self._run(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner), *args])

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.shell, *argv],
            env={**self.env, **(env or {})},
            cwd=self.tmp_path,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    def install_old_copy(self, version: str, *, alias: bool = True) -> None:
        """An earlier offline install: a `chrys.exe` that reports ``version`` and its `aixcoding-cli.exe`."""
        bin_dir = Path(self.env["LOCALAPPDATA"]) / "chrys" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "chrys.exe").write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
        (bin_dir / "chrys.exe").chmod(0o755)
        if alias:
            (bin_dir / "aixcoding-cli.exe").write_text("", encoding="utf-8")

    def stall_bounded(self) -> list[str]:
        log = self.server.root / "stall-bounded.log"
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []

    def started(self) -> list[str] | None:
        started = self.server.root / "started.txt"
        return started.read_text(encoding="utf-8").strip().split("|") if started.is_file() else None


def _ps_machine(tmp_path: Path, shell: str) -> _PsMachine:
    executable = _powershell(shell)
    server = _server(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    profile = tmp_path / "profile"
    (profile / "Local").mkdir(parents=True)
    (profile / "Roaming").mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {
        name: value for name, value in os.environ.items() if not name.upper().startswith(("ICODE_", "PYAPP", "CHRYS_"))
    }
    env.update(
        {
            # Only the shell's own folder and the test's, so no real `icode` is in sight.
            "PATH": os.pathsep.join([str(fake_bin), str(Path(executable).parent)]),
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "APPDATA": str(profile / "Roaming"),
            "LOCALAPPDATA": str(profile / "Local"),
            "TMPDIR": str(temp),
            "TEMP": str(temp),
            "TMP": str(temp),
            "FAKE_SERVED": str(server.root),
            "INSTALL_PS1": str(_INSTALL_PS1),
            # pwsh otherwise reports its start and looks for updates over the network.
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
        }
    )
    return _PsMachine(executable, tmp_path, env, server)


def _downloaded_package(requests: list[str]) -> str:
    packages = [url.rsplit("/", 1)[1] for url in requests if url.endswith("-offline.zip")]
    assert len(packages) == 1, requests
    return packages[0]


@_SHELLS
# AIxCoding branding: install script contract tests
def test_install_ps1_installs_the_latest_github_release(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    latest_sums = f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    machine.server.release(_GITHUB, f"v{_LATEST}", _windows_packages(), sums_at=latest_sums)

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    requests = machine.server.requests()
    package = _downloaded_package(requests)
    assert requests == [latest_sums, f"{_GITHUB}/download/v{_LATEST}/{package}"]
    arch = package.split("-")[3]
    # Started on this console, so it can ask the user to quit a running AIxCoding.
    assert machine.started() == [f"fake icode {arch}", "install", "True"]
    assert machine.stall_bounded() == [f"{_GITHUB}/download/v{_LATEST}/{package}"]


@_SHELLS
@pytest.mark.parametrize("finder", ["api", "pyproject"])
def test_install_ps1_falls_back_to_gitcode_and_finds_its_latest_release(
    tmp_path: Path, shell: str, finder: str
) -> None:
    machine = _ps_machine(tmp_path, shell)
    if finder == "api":
        machine.server.serve(_GITCODE_API, json.dumps({"tag_name": f"v{_LATEST}", "name": "x"}).encode())
    else:
        machine.server.serve(_GITCODE_PYPROJECT, f'[project]\nname = "iCode-TUI"\nversion = "{_LATEST}"\n'.encode())
    machine.server.release(_GITCODE, f"v{_LATEST}", _windows_packages())

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not install from GitHub." in result.stdout
    assert f"{_GITCODE}/download/v{_LATEST}/SHA256SUMS.txt" in machine.server.requests()
    assert machine.started() is not None


@_SHELLS
def test_install_ps1_runs_nothing_from_a_damaged_download(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    machine.server.release(_GITHUB, "v0.29.1", _windows_packages("0.29.1"))
    for name, data in _windows_packages("0.29.1").items():
        machine.server.serve(f"{_GITHUB}/download/v0.29.1/{name}", data + b"!")

    # Passed as the file's own parameters, as `install.ps1 -Version ...` would be.
    result = machine.run_file("-Version", "v0.29.1", "-Source", "github")

    assert result.returncode != 0
    assert "is damaged" in result.stdout
    assert "Could not install AIxCoding 0.29.1." in result.stdout + result.stderr
    assert machine.server.requests()[0] == f"{_GITHUB}/download/v0.29.1/SHA256SUMS.txt"
    assert machine.started() is None


@_SHELLS
def test_install_ps1_points_at_the_releases_when_no_host_has_that_version(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)

    result = machine.run(ICODE_VERSION="0.29.9")

    assert result.returncode != 0
    assert f"{_GITCODE}/download/v0.29.9/SHA256SUMS.txt" in machine.server.requests()
    assert "Could not install AIxCoding 0.29.9." in result.stdout + result.stderr
    assert machine.started() is None


@_SHELLS
# AIxCoding branding: install script contract tests
def test_install_ps1_leaves_an_aixcoding_cli_it_did_not_install_alone(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    foreign = tmp_path / "fake-bin" / ("aixcoding-cli.exe" if sys.platform == "win32" else "aixcoding-cli")
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    foreign.chmod(0o755)

    result = machine.run()

    assert result.returncode != 0
    assert "is not an AIxCoding offline install" in result.stdout + result.stderr
    assert machine.server.requests() == []


@_SHELLS
def test_install_ps1_upgrades_an_offline_install_while_another_aixcoding_cli_is_on_path(
    tmp_path: Path, shell: str
) -> None:
    machine = _ps_machine(tmp_path, shell)
    machine.install_old_copy("0.29.0", alias=False)
    foreign = tmp_path / "fake-bin" / ("aixcoding-cli.exe" if sys.platform == "win32" else "aixcoding-cli")
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    foreign.chmod(0o755)
    machine.server.release(_GITHUB, f"v{_LATEST}", _windows_packages())

    result = machine.run(ICODE_VERSION=_LATEST)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{foreign.name} does not start the AIxCoding offline install; start that with chrys." in result.stdout
    started = machine.started()
    assert started is not None and started[0].startswith("fake icode")


@_SHELLS
def test_install_ps1_does_not_block_on_an_external_legacy_icode(tmp_path: Path, shell: str) -> None:
    # An `icode` that is not ours is no longer the installer's command: it neither
    # blocks the install nor gets touched.
    machine = _ps_machine(tmp_path, shell)
    foreign = tmp_path / "fake-bin" / ("icode.exe" if sys.platform == "win32" else "icode")
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    foreign.chmod(0o755)
    machine.server.release(
        _GITHUB, f"v{_LATEST}", _windows_packages(), sums_at=f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    )

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    started = machine.started()
    assert started is not None and started[0].startswith("fake icode")
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\n"


@_SHELLS
def test_install_ps1_fails_when_aixcoding_cli_install_does(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    sums = f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    machine.server.release(_GITHUB, f"v{_LATEST}", _windows_packages(), sums_at=sums)

    result = machine.run(FAKE_EXIT_CODE="3")

    assert result.returncode != 0
    assert "aixcoding-cli install failed (exit code 3)." in result.stdout + result.stderr
    assert not any(_GITCODE in url for url in machine.server.requests())


# Windows cannot run the shell scripts that stand in for an installed `chrys.exe` here.
_RUNS_FAKE_CHRYS = pytest.mark.skipif(sys.platform == "win32", reason="The fake chrys.exe is a shell script.")


@_RUNS_FAKE_CHRYS
@pytest.mark.parametrize(
    ("installed", "alias", "downloads"),
    [(_LATEST, True, False), (_LATEST, False, True), ("0.31.0", True, False), ("0.31.0", False, False)],
    ids=["same-version", "alias-gone", "newer-installed", "newer-installed-alias-gone"],
)
# AIxCoding branding: install script contract tests
def test_install_ps1_checks_the_installed_copy_first(
    tmp_path: Path, installed: str, alias: bool, downloads: bool
) -> None:
    machine = _ps_machine(tmp_path, "pwsh")
    machine.install_old_copy(installed, alias=alias)
    sums = f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    machine.server.release(_GITHUB, f"v{_LATEST}", _windows_packages(), sums_at=sums)

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    started = machine.started()
    assert (started is not None and started[0].startswith("fake icode")) is downloads
    # A newer copy without its alias runs its own `install` to put the alias back.
    repaired = installed != _LATEST and not alias
    assert (started is not None and f"echo {installed}" in started[0] and started[1] == "install") is repaired
    if installed != _LATEST:
        assert f"AIxCoding {installed} is already installed, which is newer than the latest on GitHub" in result.stdout
    elif alias:
        assert f"AIxCoding {_LATEST} is already installed." in result.stdout


@_RUNS_FAKE_CHRYS
def test_install_ps1_installs_when_only_the_legacy_icode_alias_exists(tmp_path: Path) -> None:
    # A pre-rename install: only the legacy `icode.exe` alias, so the install is
    # incomplete under the new command and runs again (F-3).
    machine = _ps_machine(tmp_path, "pwsh")
    machine.install_old_copy(_LATEST, alias=False)
    bin_dir = Path(machine.env["LOCALAPPDATA"]) / "chrys" / "bin"
    (bin_dir / "icode.exe").write_text("", encoding="utf-8")
    machine.server.release(
        _GITHUB, f"v{_LATEST}", _windows_packages(), sums_at=f"{_GITHUB}/latest/download/SHA256SUMS.txt"
    )

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    started = machine.started()
    assert started is not None and started[0].startswith("fake icode")
    # The script itself never removes the legacy alias; that is the installer's job.
    assert (bin_dir / "icode.exe").is_file()
