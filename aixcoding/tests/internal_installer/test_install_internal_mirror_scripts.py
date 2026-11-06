"""Contract tests for the internal mirror installers, with no real network access."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_INSTALL_SH = REPO_ROOT / "aixcoding" / "release" / "install-internal-mirror.sh"
_INSTALL_PS1 = REPO_ROOT / "aixcoding" / "release" / "install-internal-mirror.ps1"
_INSTALL_MD = REPO_ROOT / "aixcoding" / "release" / "INSTALL.md"
_SUPPORT_MD = REPO_ROOT / "aixcoding" / "docs" / "install-uninstall-support.md"
_PUBLISHING_MD = REPO_ROOT / "aixcoding" / "docs" / "batches" / "internal-installer" / "publishing.md"
_DEFAULT_BASE = "http://82.187.34.98/aixcoding/file/aixcoding"
_BASE = "http://mirror.test/aixcoding/file/aixcoding"
_LEGACY_BASE = "http://legacy.test/aixcoding/file/aixcoding"
_NEW_BASE = "http://new.test/aixcoding/file/aixcoding"
_EXPLICIT_BASE = "http://explicit.test/aixcoding/file/aixcoding"
_VERSION = "0.33.0"


@dataclass
class _Server:
    root: Path

    def serve(self, url: str, payload: bytes) -> None:
        (self.root / url.replace("/", "_").replace(":", "_")).write_bytes(payload)

    def channel(self, base: str, channel: str, version: str = _VERSION) -> None:
        self.serve(f"{base}/channels/cli/{channel}.txt", f"  v{version}\r\n".encode())

    def release(self, base: str, version: str, packages: dict[str, bytes]) -> None:
        for name, data in packages.items():
            self.serve(f"{base}/releases/cli/{version}/{name}", data)

    def requests(self) -> list[str]:
        log = self.root / "requests.log"
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def _server(tmp_path: Path) -> _Server:
    root = tmp_path / "served"
    root.mkdir()
    return _Server(root)


def _package_name(version: str = _VERSION, *, archive_format: str = "tar.gz", platform: str = "linux-x86_64") -> str:
    return f"aixcoding-cli-{platform}-v{version}-offline.{archive_format}"


def _binary(version: str = _VERSION, *, install_exit: int = 0, read_stdin: bool = False) -> str:
    read_block = ""
    if read_stdin:
        read_block = """if IFS= read -r line; then
  printf 'data:%s' \"$line\" > \"$HOME/stdin-result\"
else
  printf eof > \"$HOME/stdin-result\"
fi
"""
    return f"""#!/bin/sh
if [ \"$1\" = --version ]; then
  printf '%s\\n' '{version}'
  exit 0
fi
if [ \"$1\" = install ]; then
  {read_block}
  printf '%s\\n' \"$*\" > \"$HOME/install-ran\"
  exit {install_exit}
fi
exit 2
"""


def _installing_binary(version: str = _VERSION, *, install_exit: int = 0, read_stdin: bool = False) -> str:
    read_block = ""
    if read_stdin:
        read_block = """if IFS= read -r line; then
  printf 'data:%s' \"$line\" > \"$HOME/stdin-result\"
else
  printf eof > \"$HOME/stdin-result\"
fi
"""
    return f"""#!/bin/sh
if [ \"$1\" = --version ]; then
  printf '%s\\n' '{version}'
  exit 0
fi
if [ \"$1\" = install ]; then
  {read_block}
  printf '%s\\n' \"$*\" > \"$HOME/install-ran\"
  if [ {install_exit} -ne 0 ]; then
    exit {install_exit}
  fi
  mkdir -p \"$HOME/.local/bin\"
  cp \"$0\" \"$HOME/.local/bin/chrys\"
  chmod +x \"$HOME/.local/bin/chrys\"
  if [ ! -e \"$HOME/.local/bin/aixcoding-cli\" ] && [ ! -L \"$HOME/.local/bin/aixcoding-cli\" ]; then
    ln -s chrys \"$HOME/.local/bin/aixcoding-cli\"
  fi
  exit 0
fi
exit 2
"""


def _archive(data: str, *, archive_format: str = "tar.gz", binary_name: str = "aixcoding-cli") -> bytes:
    buffer = io.BytesIO()
    raw = data.encode()
    if archive_format == "zip":
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(binary_name, raw)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo(binary_name)
            info.size = len(raw)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


# R13: the real Bash installer targets only Linux/macOS; approved by internal-installer design section 10.
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="The real Bash installer targets Linux/macOS.")

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
if [ "${FAKE_CURL_STATUS:-0}" -ne 0 ]; then
    exit "$FAKE_CURL_STATUS"
fi
file="$FAKE_SERVED/$(printf '%s' "$url" | tr '/:' '__')"
[ -f "$file" ] || exit 22
cp "$file" "$out"
"""


@dataclass
class _ShMachine:
    home: Path
    env: dict[str, str]
    server: _Server

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(_INSTALL_SH), *args],
            env={**self.env, **(env or {})},
            cwd=self.home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    def run_piped(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-s", "--", *args],
            env={**self.env, **(env or {})},
            cwd=self.home,
            input=_INSTALL_SH.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    def install_old_copy(self, version: str, *, alias: bool = True, executable: bool = True) -> None:
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        chrys = bin_dir / "chrys"
        chrys.write_text(_binary(version), encoding="utf-8")
        chrys.chmod(0o755 if executable else 0o644)
        if alias:
            (bin_dir / "aixcoding-cli").symlink_to("chrys")

    def installed_version(self, command: str = "aixcoding-cli") -> str:
        result = subprocess.run(
            [str(self.home / ".local" / "bin" / command), "--version"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()


def _sh_machine(
    tmp_path: Path,
    *,
    os_name: str = "Linux",
    arch: str = "x86_64",
    ldd: str = "ldd (GNU libc) 2.35",
    translated: str = "0",
) -> _ShMachine:
    server = _server(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tools = {
        "curl": _FAKE_CURL,
        "uname": f'#!/bin/sh\ncase "$1" in -s) echo {os_name} ;; -m) echo {arch} ;; esac\n',
        "ldd": f"#!/bin/sh\nprintf '%s\\n' '{ldd}'\n",
        "sysctl": f"#!/bin/sh\nprintf '%s\\n' '{translated}'\n",
    }
    for name, body in tools.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "SHELL": "/bin/bash",
        "TMPDIR": str(temp),
        "FAKE_SERVED": str(server.root),
    }
    return _ShMachine(home, env, server)


def _serve_posix(
    machine: _ShMachine, *, base: str = _DEFAULT_BASE, channel: str = "prod", version: str = _VERSION
) -> bytes:
    package = _archive(_installing_binary(version))
    machine.server.channel(base, channel, version)
    machine.server.release(base, version, {_package_name(version): package})
    return package


@_POSIX_ONLY
@pytest.mark.parametrize("how", ["file", "piped"])
@pytest.mark.parametrize("channel", ["prod", "test"])
def test_install_sh_online_channel_contract(tmp_path: Path, how: str, channel: str) -> None:
    machine = _sh_machine(tmp_path)
    _serve_posix(machine, channel=channel)
    args = () if channel == "prod" else ("--channel", "test")

    result = machine.run(*args) if how == "file" else machine.run_piped(*args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests() == [
        f"{_DEFAULT_BASE}/channels/cli/{channel}.txt",
        f"{_DEFAULT_BASE}/releases/cli/{_VERSION}/{_package_name()}",
    ]
    assert (machine.home / "install-ran").read_text(encoding="utf-8").strip() == "install"
    assert machine.installed_version() == _VERSION
    assert not any("SHA256SUMS" in url for url in machine.server.requests())


@_POSIX_ONLY
@pytest.mark.parametrize("option", [("--version", f"v{_VERSION}"), (f"--version=v{_VERSION}",)])
def test_install_sh_explicit_version_skips_channel(tmp_path: Path, option: tuple[str, ...]) -> None:
    machine = _sh_machine(tmp_path)
    machine.server.release(_BASE, _VERSION, {_package_name(): _archive(_installing_binary())})

    result = machine.run("--base-url", _BASE, *option)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests() == [f"{_BASE}/releases/cli/{_VERSION}/{_package_name()}"]


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ((), {}, _DEFAULT_BASE),
        ((), {"ICODE_BASE_URL": _LEGACY_BASE}, _LEGACY_BASE),
        ((), {"ICODE_BASE_URL": _LEGACY_BASE, "AIXCODING_BASE_URL": _NEW_BASE}, _NEW_BASE),
        (
            ("--base-url", _EXPLICIT_BASE),
            {"ICODE_BASE_URL": _LEGACY_BASE, "AIXCODING_BASE_URL": _NEW_BASE},
            _EXPLICIT_BASE,
        ),
        (("--base-url=" + _EXPLICIT_BASE,), {}, _EXPLICIT_BASE),
    ],
    ids=["default", "legacy", "new-over-legacy", "explicit", "explicit-equals"],
)
def test_install_sh_base_url_precedence(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str], expected: str
) -> None:
    machine = _sh_machine(tmp_path)
    _serve_posix(machine, base=expected)

    result = machine.run(*args, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests()[0] == f"{expected}/channels/cli/prod.txt"


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ((), {}, "prod"),
        ((), {"ICODE_CHANNEL": "test"}, "test"),
        ((), {"ICODE_CHANNEL": "test", "AIXCODING_CHANNEL": "prod"}, "prod"),
        (("--channel", "test"), {"ICODE_CHANNEL": "prod", "AIXCODING_CHANNEL": "prod"}, "test"),
        (("--channel=test",), {}, "test"),
    ],
    ids=["default", "legacy", "new-over-legacy", "explicit", "explicit-equals"],
)
def test_install_sh_channel_precedence(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str], expected: str
) -> None:
    machine = _sh_machine(tmp_path)
    _serve_posix(machine, channel=expected)

    result = machine.run(*args, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests()[0] == f"{_DEFAULT_BASE}/channels/cli/{expected}.txt"


@_POSIX_ONLY
@pytest.mark.parametrize("zip_available", [True, False], ids=["zip-fallback", "missing"])
def test_install_sh_current_asset_candidates_only(tmp_path: Path, zip_available: bool) -> None:
    machine = _sh_machine(tmp_path)
    machine.server.channel(_BASE, "prod")
    packages = (
        {_package_name(archive_format="zip"): _archive(_installing_binary(), archive_format="zip")}
        if zip_available
        else {}
    )
    machine.server.release(_BASE, _VERSION, packages)

    result = machine.run("--base-url", _BASE)

    assert result.returncode == (0 if zip_available else 1), result.stdout + result.stderr
    requests = machine.server.requests()
    assert requests == [
        f"{_BASE}/channels/cli/prod.txt",
        f"{_BASE}/releases/cli/{_VERSION}/{_package_name()}",
        f"{_BASE}/releases/cli/{_VERSION}/{_package_name(archive_format='zip')}",
    ]
    assert not any("icode-" in url or "SHA256SUMS" in url for url in requests)


@_POSIX_ONLY
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_install_sh_offline_from_local_archive(tmp_path: Path, archive_format: str) -> None:
    machine = _sh_machine(tmp_path)
    archive = tmp_path / _package_name(archive_format=archive_format)
    archive.write_bytes(_archive(_installing_binary(), archive_format=archive_format))

    result = machine.run(f"--from={archive}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests() == []
    assert machine.installed_version() == _VERSION


@_POSIX_ONLY
def test_install_sh_rejects_invalid_arguments_before_download(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    unsupported = tmp_path / "package.rar"
    unsupported.write_bytes(b"not an archive")
    cases = [
        ("--from", "package.tar.gz", "--version", _VERSION),
        ("--from", ""),
        ("--from", str(tmp_path / "missing.tar.gz")),
        ("--from", str(unsupported)),
        ("--version", "not-a-version"),
    ]

    for args in cases:
        result = machine.run(*args)
        assert result.returncode == 1, args
        assert machine.server.requests() == []


@_POSIX_ONLY
def test_install_sh_rejects_legacy_named_archive(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    archive = tmp_path / f"icode-linux-x86_64-v{_VERSION}-offline.tar.gz"
    archive.write_bytes(_archive(_binary(), binary_name="icode"))

    result = machine.run("--from", str(archive))

    assert result.returncode == 1
    assert "从镜像获取最新版" in result.stderr
    assert not (machine.home / "install-ran").exists()


@_POSIX_ONLY
def test_install_sh_rejects_legacy_binary(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    archive = tmp_path / _package_name()
    archive.write_bytes(_archive(_binary(), binary_name="icode"))

    result = machine.run("--from", str(archive))

    assert result.returncode == 1
    assert "aixcoding-cli" in result.stderr
    assert not (machine.home / "install-ran").exists()


@_POSIX_ONLY
@pytest.mark.parametrize("online", [True, False], ids=["online", "offline"])
@pytest.mark.parametrize("matching", [True, False], ids=["matching", "mismatch"])
def test_install_sh_explicit_sha256(tmp_path: Path, online: bool, matching: bool) -> None:
    machine = _sh_machine(tmp_path)
    package = _archive(_installing_binary())
    args: list[str]
    if online:
        machine.server.channel(_BASE, "prod")
        machine.server.release(_BASE, _VERSION, {_package_name(): package})
        args = ["--base-url", _BASE]
    else:
        archive = tmp_path / _package_name()
        archive.write_bytes(package)
        args = ["--from", str(archive)]
    digest = hashlib.sha256(package).hexdigest()
    expected = digest.upper() if matching else "0" * 64

    result = machine.run(*args, "--sha256", expected)

    assert result.returncode == (0 if matching else 1), result.stdout + result.stderr
    assert (machine.home / "install-ran").exists() is matching
    assert not any("SHA256SUMS" in url for url in machine.server.requests())


@_POSIX_ONLY
@pytest.mark.parametrize("digest", ["xyz", "0" * 63])
def test_install_sh_rejects_invalid_sha256_before_download(tmp_path: Path, digest: str) -> None:
    machine = _sh_machine(tmp_path)

    result = machine.run("--sha256", digest)

    assert result.returncode == 1
    assert machine.server.requests() == []


@_POSIX_ONLY
@pytest.mark.parametrize("own", ["none", "executable", "not-executable"])
def test_install_sh_external_command_conflict(tmp_path: Path, own: str) -> None:
    machine = _sh_machine(tmp_path)
    external = tmp_path / "fake-bin" / "aixcoding-cli"
    external.write_bytes(b"external command\n")
    external.chmod(0o755)
    if own != "none":
        machine.install_old_copy("0.32.0", alias=False, executable=own == "executable")
    if own == "executable":
        _serve_posix(machine)

    result = machine.run()

    assert external.read_bytes() == b"external command\n"
    if own == "executable":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "chrys" in result.stderr + result.stdout
        assert machine.installed_version("chrys") == _VERSION
    else:
        assert result.returncode == 1
        assert machine.server.requests() == []
        assert not (machine.home / "install-ran").exists()


@_POSIX_ONLY
@pytest.mark.parametrize("alias", [True, False], ids=["complete", "alias-gone"])
def test_install_sh_idempotency(tmp_path: Path, alias: bool) -> None:
    machine = _sh_machine(tmp_path)
    machine.install_old_copy(_VERSION, alias=alias)
    machine.server.channel(_BASE, "prod")
    machine.server.release(_BASE, _VERSION, {_package_name(): _archive(_installing_binary())})

    result = machine.run("--base-url", _BASE)

    assert result.returncode == 0, result.stdout + result.stderr
    expected = [f"{_BASE}/channels/cli/prod.txt"]
    if not alias:
        expected.append(f"{_BASE}/releases/cli/{_VERSION}/{_package_name()}")
    assert machine.server.requests() == expected
    assert (machine.home / "install-ran").exists() is not alias


@_POSIX_ONLY
def test_install_sh_channel_switch_allows_lower_test_version(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    machine.install_old_copy("0.34.0")
    _serve_posix(machine, channel="test", version=_VERSION)

    result = machine.run("--channel", "test")

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.installed_version("chrys") == _VERSION
    assert "无需重复安装" not in result.stdout


@_POSIX_ONLY
def test_install_sh_piped_stdin_is_closed(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    package = _archive(_installing_binary(read_stdin=True))
    machine.server.channel(_BASE, "prod")
    machine.server.release(_BASE, _VERSION, {_package_name(): package})

    result = machine.run_piped("--base-url", _BASE)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (machine.home / "stdin-result").read_text(encoding="utf-8") == "eof"


@_POSIX_ONLY
@pytest.mark.parametrize("status", [3, 126])
def test_install_sh_install_failure(tmp_path: Path, status: int) -> None:
    machine = _sh_machine(tmp_path)
    machine.server.channel(_BASE, "prod")
    machine.server.release(_BASE, _VERSION, {_package_name(): _archive(_installing_binary(install_exit=status))})

    result = machine.run("--base-url", _BASE)

    assert result.returncode == 1
    if status == 126:
        assert "TMPDIR" in result.stderr
    else:
        assert f"退出码 {status}" in result.stderr
    assert "安装完成" not in result.stdout


@_POSIX_ONLY
@pytest.mark.parametrize(("translated", "expected_arch"), [("1", "aarch64"), ("0", "x86_64")])
def test_install_sh_rosetta_selects_aarch64(tmp_path: Path, translated: str, expected_arch: str) -> None:
    machine = _sh_machine(tmp_path, os_name="Darwin", translated=translated)
    name = _package_name(platform=f"macos-{expected_arch}")
    machine.server.channel(_BASE, "prod")
    machine.server.release(_BASE, _VERSION, {name: _archive(_installing_binary())})

    result = machine.run("--base-url", _BASE)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests()[-1] == f"{_BASE}/releases/cli/{_VERSION}/{name}"


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ldd": "musl libc (x86_64)"}, "AIxCoding 不支持 musl"),
        ({"ldd": "ldd (GNU libc) 2.16"}, "最低要求"),
        ({"arch": "ppc64le"}, "不支持的处理器架构"),
    ],
    ids=["musl", "old-glibc", "unknown-architecture"],
)
def test_install_sh_platform_and_libc_guards(tmp_path: Path, kwargs: dict[str, str], message: str) -> None:
    machine = _sh_machine(tmp_path, **kwargs)

    result = machine.run()

    assert result.returncode == 1
    assert message in result.stderr
    assert machine.server.requests() == []


@_POSIX_ONLY
def test_install_sh_normal_install_command_contract(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    _serve_posix(machine)

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    bin_dir = machine.home / ".local" / "bin"
    legacy_alias = bin_dir / "aixcoding"
    assert not legacy_alias.exists() and not legacy_alias.is_symlink()
    assert machine.installed_version() == _VERSION
    assert "# Added by AIxCoding installer" in (machine.home / ".bashrc").read_text(encoding="utf-8")
    assert "icode-install" not in result.stdout + result.stderr


@_POSIX_ONLY
def test_install_sh_normal_install_has_no_legacy_brand_output(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)
    _serve_posix(machine)

    help_result = machine.run("--help")
    install_result = machine.run()

    assert help_result.returncode == 0
    assert "iCode" not in help_result.stdout and "icode" not in help_result.stdout.lower()
    combined = install_result.stdout + install_result.stderr
    assert "iCode" not in combined and "icode" not in combined.lower()


@_POSIX_ONLY
def test_install_sh_download_failure_mentions_base_url_escape(tmp_path: Path) -> None:
    machine = _sh_machine(tmp_path)

    result = machine.run(env={"FAKE_CURL_STATUS": "7"})

    assert result.returncode == 1
    assert "--base-url <地址>" in result.stderr
    assert "AIXCODING_BASE_URL=<地址>" in result.stderr


# R13: the real installer and chrys.exe install path require Windows; approved by internal-installer design section 10.
_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="The real installer and chrys.exe path require Windows."
)
_SHELLS = pytest.mark.parametrize("shell", ["pwsh", "powershell"])

_PS_STUBS = r"""
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
function Get-Served([string]$Uri) {
    Add-Content -LiteralPath (Join-Path $env:FAKE_SERVED 'requests.log') -Value $Uri
    $file = Join-Path $env:FAKE_SERVED ($Uri -replace '[/:]', '_')
    if (-not (Test-Path -LiteralPath $file)) { throw "404 Not Found: $Uri" }
    $file
}
function Invoke-WebRequest([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing) {
    Copy-Item -LiteralPath (Get-Served $Uri) -Destination $OutFile
}
function Invoke-RestMethod([string]$Uri, [switch]$UseBasicParsing) {
    Get-Content -LiteralPath (Get-Served $Uri) -Raw
}
function Start-Process([string]$FilePath, [string[]]$ArgumentList, [switch]$NoNewWindow, [switch]$PassThru) {
    $command = $ArgumentList -join ' '
    Add-Content -LiteralPath (Join-Path $env:FAKE_SERVED 'started.log') -Value "$FilePath|$command"
    $exitCode = if ($command -eq 'install' -and $env:FAKE_EXIT_CODE) { [int]$env:FAKE_EXIT_CODE } else { 0 }
    if ($command -eq 'install' -and $exitCode -eq 0) {
        $binDir = Join-Path $env:LOCALAPPDATA 'chrys\bin'
        [IO.Directory]::CreateDirectory($binDir) | Out-Null
        Copy-Item -LiteralPath $env:FAKE_EXE -Destination (Join-Path $binDir 'chrys.exe') -Force
        Copy-Item -LiteralPath $env:FAKE_EXE -Destination (Join-Path $binDir 'aixcoding-cli.exe') -Force
    }
    if ($command -eq '--version' -and $env:FAKE_SELF_CHECK_FAIL) {
        $exitCode = 1
    }
    [pscustomobject]@{ Handle = 1; ExitCode = $exitCode } |
        Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { } -PassThru
}
"""


def _windows_executable(*, failing: bool = False) -> bytes:
    return b"fake failing executable" if failing else b"fake executable"


def _windows_zip(*, binary_name: str = "aixcoding-cli.exe", failing: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(binary_name, _windows_executable(failing=failing))
    return buffer.getvalue()


@dataclass
class _PsMachine:
    shell: str
    tmp_path: Path
    env: dict[str, str]
    server: _Server

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        command = _PS_STUBS + "Get-Content -Raw -LiteralPath $env:INSTALL_PS1 | Invoke-Expression\n"
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        argv = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-EncodedCommand", encoded]
        return self._run(argv, env)

    def run_file(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        runner = self.tmp_path / "run.ps1"
        runner.write_text(_PS_STUBS + "& $env:INSTALL_PS1 @args\n", encoding="utf-8")
        return self._run(
            ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner), *args], env
        )

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.shell, *argv],
            env={**self.env, **(env or {})},
            cwd=self.tmp_path,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            # AIxCoding tests: pin UTF-8 (CLIXML stderr may carry non-ASCII user names) so
            # the default locale encoding (GBK on Chinese Windows) cannot null out streams.
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def install_old_copy(self) -> None:
        bin_dir = Path(self.env["LOCALAPPDATA"]) / "chrys" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "chrys.exe").write_bytes(_windows_executable())

    def started(self) -> list[str]:
        log = self.server.root / "started.log"
        return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def _ps_machine(tmp_path: Path, shell: str) -> _PsMachine:
    executable = shutil.which(shell)
    assert executable is not None, f"{shell} is required for the Windows installer contract"
    server = _server(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_exe = tmp_path / "fake-package.exe"
    fake_exe.write_bytes(_windows_executable())
    profile = tmp_path / "profile"
    (profile / "Local").mkdir(parents=True)
    (profile / "Roaming").mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("AIXCODING_", "ICODE_", "FAKE_", "PYAPP", "CHRYS_"))
    }
    env.update(
        {
            "PATH": os.pathsep.join([str(fake_bin), str(Path(executable).parent)]),
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "APPDATA": str(profile / "Roaming"),
            "LOCALAPPDATA": str(profile / "Local"),
            "TEMP": str(temp),
            "TMP": str(temp),
            "FAKE_SERVED": str(server.root),
            "FAKE_EXE": str(fake_exe),
            "INSTALL_PS1": str(_INSTALL_PS1),
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
        }
    )
    return _PsMachine(executable, tmp_path, env, server)


def _serve_windows(machine: _PsMachine, *, base: str = _DEFAULT_BASE, channel: str = "prod") -> None:
    machine.server.channel(base, channel)
    machine.server.release(
        base, _VERSION, {_package_name(archive_format="zip", platform="windows-x86_64"): _windows_zip()}
    )


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize("channel", ["prod", "test"])
def test_install_ps1_online_channel_contract(tmp_path: Path, shell: str, channel: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    _serve_windows(machine, channel=channel)
    env = {"AIXCODING_CHANNEL": "test"} if channel == "test" else {}

    result = machine.run(**env)

    name = _package_name(archive_format="zip", platform="windows-x86_64")
    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests() == [
        f"{_DEFAULT_BASE}/channels/cli/{channel}.txt",
        f"{_DEFAULT_BASE}/releases/cli/{_VERSION}/{name}",
    ]
    assert any(line.endswith("|install") for line in machine.started())
    assert not any("SHA256SUMS" in url for url in machine.server.requests())


@_WINDOWS_ONLY
@_SHELLS
def test_install_ps1_explicit_version_skips_channel(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    name = _package_name(archive_format="zip", platform="windows-x86_64")
    machine.server.release(_BASE, _VERSION, {name: _windows_zip()})

    result = machine.run_file("-BaseUrl", _BASE, "-Version", f"v{_VERSION}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests() == [f"{_BASE}/releases/cli/{_VERSION}/{name}"]


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize("version", ["", "not-a-version"])
def test_install_ps1_rejects_invalid_version_before_download(tmp_path: Path, shell: str, version: str) -> None:
    machine = _ps_machine(tmp_path, shell)

    result = machine.run_file("-BaseUrl", _BASE, "-Version", version)

    assert result.returncode != 0
    assert machine.server.requests() == []


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ((), {}, _DEFAULT_BASE),
        ((), {"ICODE_BASE_URL": _LEGACY_BASE}, _LEGACY_BASE),
        ((), {"ICODE_BASE_URL": _LEGACY_BASE, "AIXCODING_BASE_URL": _NEW_BASE}, _NEW_BASE),
        (
            ("-BaseUrl", _EXPLICIT_BASE),
            {"ICODE_BASE_URL": _LEGACY_BASE, "AIXCODING_BASE_URL": _NEW_BASE},
            _EXPLICIT_BASE,
        ),
    ],
    ids=["default", "legacy", "new-over-legacy", "explicit"],
)
def test_install_ps1_base_url_precedence(
    tmp_path: Path, shell: str, args: tuple[str, ...], env: dict[str, str], expected: str
) -> None:
    machine = _ps_machine(tmp_path, shell)
    _serve_windows(machine, base=expected)

    result = machine.run_file(*args, env=env) if args else machine.run(**env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests()[0] == f"{expected}/channels/cli/prod.txt"


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ((), {}, "prod"),
        ((), {"ICODE_CHANNEL": "test"}, "test"),
        ((), {"ICODE_CHANNEL": "test", "AIXCODING_CHANNEL": "prod"}, "prod"),
        (("-Channel", "test"), {"ICODE_CHANNEL": "prod", "AIXCODING_CHANNEL": "prod"}, "test"),
    ],
    ids=["default", "legacy", "new-over-legacy", "explicit"],
)
def test_install_ps1_channel_precedence(
    tmp_path: Path, shell: str, args: tuple[str, ...], env: dict[str, str], expected: str
) -> None:
    machine = _ps_machine(tmp_path, shell)
    _serve_windows(machine, channel=expected)

    result = machine.run_file(*args, env=env) if args else machine.run(**env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert machine.server.requests()[0] == f"{_DEFAULT_BASE}/channels/cli/{expected}.txt"


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize("own", [False, True], ids=["abort", "continue"])
def test_install_ps1_external_command_conflict(tmp_path: Path, shell: str, own: bool) -> None:
    machine = _ps_machine(tmp_path, shell)
    external = tmp_path / "fake-bin" / "aixcoding-cli.exe"
    external.write_bytes(b"external command")
    if own:
        machine.install_old_copy()
        _serve_windows(machine)

    result = machine.run()

    assert external.read_bytes() == b"external command"
    if own:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "run chrys" in result.stdout
    else:
        assert result.returncode != 0
        assert machine.server.requests() == []
        assert machine.started() == []


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize("failure", ["missing-binary", "self-check", "install"])
def test_install_ps1_rejects_bad_archive_and_failed_install(tmp_path: Path, shell: str, failure: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    machine.server.channel(_BASE, "prod")
    name = _package_name(archive_format="zip", platform="windows-x86_64")
    if failure == "missing-binary":
        package = _windows_zip(binary_name="other.exe")
    else:
        package = _windows_zip(failing=failure == "self-check")
    machine.server.release(_BASE, _VERSION, {name: package})
    env = {"AIXCODING_BASE_URL": _BASE}
    if failure == "install":
        env["FAKE_EXIT_CODE"] = "3"
    if failure == "self-check":
        env["FAKE_SELF_CHECK_FAIL"] = "1"

    result = machine.run(**env)

    assert result.returncode != 0
    assert "Installation complete" not in result.stdout
    if failure == "install":
        assert "exit code 3" in result.stdout + result.stderr


@_WINDOWS_ONLY
@_SHELLS
@pytest.mark.parametrize("missing", ["channel", "package"])
def test_install_ps1_download_failure_mentions_base_url_escape(tmp_path: Path, shell: str, missing: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    if missing == "package":
        machine.server.channel(_BASE, "prod")

    result = machine.run(AIXCODING_BASE_URL=_BASE)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "-BaseUrl" in combined
    assert "AIXCODING_BASE_URL" in combined


@_WINDOWS_ONLY
@_SHELLS
def test_install_ps1_current_brand_and_asymmetry(tmp_path: Path, shell: str) -> None:
    machine = _ps_machine(tmp_path, shell)
    _serve_windows(machine)

    result = machine.run()

    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "iCode" not in combined and "icode" not in combined.lower()
    source = _INSTALL_PS1.read_text(encoding="utf-8")
    assert "--from" not in source
    assert "SHA256SUMS" not in source
    assert "windows-x86_64" in source


def test_distribution_materials_static_contract() -> None:
    sh = _INSTALL_SH.read_text(encoding="utf-8")
    ps1 = _INSTALL_PS1.read_text(encoding="utf-8")
    install_doc = _INSTALL_MD.read_text(encoding="utf-8")
    support_doc = _SUPPORT_MD.read_text(encoding="utf-8")
    publishing = _PUBLISHING_MD.read_text(encoding="utf-8")

    for source in (sh, ps1):
        assert _DEFAULT_BASE in source
        assert "AIXCODING_BASE_URL" in source and "ICODE_BASE_URL" in source
        assert "AIXCODING_CHANNEL" in source and "ICODE_CHANNEL" in source
        assert "GitHub" not in source and "GitCode" not in source
        assert "SHA256SUMS" not in source
    assert "create_launch_command" not in sh
    assert "aixcoding-install." in sh
    assert "icode-install" not in sh
    assert "install-internal-mirror.sh" in publishing and "install-internal-mirror.ps1" in publishing
    assert "channels/cli/prod.txt" in publishing and "channels/cli/test.txt" in publishing

    assert "aixcoding-cli uninstall" in install_doc
    assert "--purge" in install_doc
    assert "Windows on Arm" in install_doc
    for internal_detail in ("--channel", "AIXCODING_", "ICODE_", "chrys", "channels/cli"):
        assert internal_detail not in install_doc

    assert "--sha256" in support_doc
    assert "AIXCODING_BASE_URL" in support_doc and "ICODE_BASE_URL" in support_doc
    assert "AIXCODING_CHANNEL" in support_doc and "ICODE_CHANNEL" in support_doc
    assert "channels/cli/prod.txt" in support_doc and "channels/cli/test.txt" in support_doc
    assert "releases/cli/<完整版本>" in support_doc
    assert "../release/INSTALL.md" in support_doc


def test_distribution_materials_brand_context() -> None:
    sh = _INSTALL_SH.read_text(encoding="utf-8")
    ps1 = _INSTALL_PS1.read_text(encoding="utf-8")
    install_doc = _INSTALL_MD.read_text(encoding="utf-8")
    support_doc = _SUPPORT_MD.read_text(encoding="utf-8")

    assert "# Added by iCode installer" not in sh
    assert "安装后的启动命令: aixcoding\n" not in sh
    for line in (sh + "\n" + ps1).splitlines():
        if "iCode-TUI" in line:
            assert "uv tool upgrade" in line
        if "icode-*" in line:
            assert "旧版" in line or "历史" in line
    assert "iCode" not in install_doc and "icode" not in install_doc.lower()
    for line in support_doc.splitlines():
        if "`icode`" in line:
            assert "旧版" in line or "历史" in line
    assert "当前安装脚本不安装旧包" in support_doc
