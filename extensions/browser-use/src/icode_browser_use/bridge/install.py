# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native-messaging host registration for Chrome/Edge.

Windows registers a registry pointer to the manifest; macOS/Linux place the
manifest in the browser's ``NativeMessagingHosts`` directory. The manifest
itself pins the extension id through ``allowed_origins``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from .. import __version__
from .._proc import (
    atomic_write_private,
    harden_private,
    pid_alive as _pid_alive,
    which_trusted,
    windows_system_exe,
)
from .protocol import PROTOCOL_VERSION

HOST_NAME = "com.chrys.browser"
_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")

_REGISTRY_KEYS = {
    "chrome": rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    "chromium": rf"Software\Chromium\NativeMessagingHosts\{HOST_NAME}",
    "edge": rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
}


def extension_id_from_key(public_key: str) -> str:
    """Chrome's extension id: sha256(public key DER)[:32], hex mapped 0-f -> a-p."""
    try:
        der = base64.b64decode(public_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("manifest key is not base64") from exc
    if not der:
        raise ValueError("manifest key is empty")
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def extension_id_from_manifest(extension_dir: Path) -> str:
    """Read a pinned extension id from ``<extension_dir>/manifest.json``."""
    manifest = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
    return extension_id_from_key(str(manifest["key"]))


def native_manifest_path() -> Path:
    """Where the host manifest lives for all browsers."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / ".local" / "share"
    return base / "icode-browser-use" / "native-host" / f"{HOST_NAME}.json"


def packaged_extension_dir() -> Path:
    """The unpacked extension shipped inside this package."""
    return Path(__file__).resolve().parents[1] / "extension"


def default_extension_install_dir() -> Path:
    """A stable directory to load unpacked from, independent of site-packages."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / ".local" / "share"
    return base / "icode-browser-use" / "extension"


def install_extension(source: Path | None = None, target: Path | None = None) -> Path:
    """Atomically replace *target* with a fresh copy of the unpacked extension."""
    from ..distribution import _is_link_or_junction

    raw_target = target or default_extension_install_dir()
    if _is_link_or_junction(raw_target):
        raise ValueError(
            f"refusing to replace symlink/junction {raw_target}; remove the link manually before installing"
        )
    source = (source or packaged_extension_dir()).resolve()
    target = raw_target.resolve()
    if source == target:
        raise ValueError(f"source and target must be different directories: {source}")
    if not (source / "manifest.json").is_file():
        raise ValueError(f"{source} has no manifest.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not (target / "manifest.json").is_file():
        raise ValueError(f"refusing to replace {target}: it does not contain a Chrome extension manifest.json")
    with tempfile.TemporaryDirectory(dir=target.parent, ignore_cleanup_errors=True) as scratch:
        staged = Path(scratch) / "incoming"
        shutil.copytree(source, staged)
        previous: Path | None = None
        if target.exists():
            previous = target.with_name(f"{target.name}-backup-{Path(scratch).name}")
            try:
                target.rename(previous)
            except OSError as exc:
                raise RuntimeError(f"cannot replace {target}; close the browser and retry") from exc
        try:
            os.replace(staged, target)
        except OSError as exc:
            if previous is not None and previous.exists():
                with contextlib.suppress(OSError):
                    previous.rename(target)
            raise RuntimeError(f"cannot replace {target}; close the browser and retry") from exc
        if previous is not None and previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
    return target


def update_extension() -> Path:
    """Replace the unpacked Bridge at its current path and retain its Chrome id."""
    source = packaged_extension_dir()
    target = default_extension_install_dir()
    if not (target / "manifest.json").is_file():
        raise RuntimeError(f"The unpacked Chrome Bridge was not found at {target}.")
    if extension_id_from_manifest(source) != extension_id_from_manifest(target):
        raise RuntimeError(f"The extension at {target} has a different Chrome id.")
    with tempfile.TemporaryDirectory(dir=target.parent, ignore_cleanup_errors=True) as scratch:
        staged = Path(scratch) / "incoming"
        previous = target.with_name(f"{target.name}-backup-{Path(scratch).name}")
        shutil.copytree(source, staged)
        target.rename(previous)
        try:
            staged.rename(target)
        except OSError:
            try:
                previous.rename(target)
            except OSError as restore_error:
                raise RuntimeError(
                    f"Could not restore the previous Bridge. Its files are in {previous}."
                ) from restore_error
            raise
        shutil.rmtree(previous, ignore_errors=True)
    return target


def native_host_manifest(host_path: Path, extension_id: str) -> dict[str, object]:
    """Build the manifest for *host_path*, allowing only *extension_id*."""
    if not isinstance(extension_id, str) or not _EXTENSION_ID_RE.fullmatch(extension_id):
        raise ValueError("extension_id must be a 32-character a-p Chrome extension id")
    return {
        "name": HOST_NAME,
        "description": "iCode browser bridge",
        "path": str(host_path.resolve()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def write_native_host_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Write the manifest atomically owner-only; the OS registration still has to point at it."""
    atomic_write_private(path, json.dumps(manifest, ensure_ascii=False, indent=2), strict=False)


def _harden_file(path: Path) -> None:
    harden_private(path, is_dir=False, strict=False)


def install_native_host(
    host_path: Path,
    extension_id: str,
    *,
    manifest_path: Path | None = None,
    browsers: Sequence[str] = ("chrome", "edge"),
    registry_writer: Callable[[str, Path], None] | None = None,
) -> Path:
    """Write the manifest and register it for each browser; return the manifest path."""
    path = manifest_path or native_manifest_path()
    manifest = native_host_manifest(host_path, extension_id)
    write_native_host_manifest(path, manifest)
    writer = registry_writer or _default_registry_writer
    for browser in browsers:
        if browser not in _REGISTRY_KEYS:
            raise ValueError(f"unknown browser {browser!r}")
        writer(browser, path)
    return path


def uninstall_native_host(
    *,
    manifest_path: Path | None = None,
    browsers: Sequence[str] = ("chrome", "edge"),
    registry_deleter: Callable[[str, Path], None] | None = None,
    registry_reader: Callable[[str], Path | None] | None = None,
) -> None:
    """Remove the requested registrations; delete the shared manifest only when no browser still references it."""
    path = manifest_path or native_manifest_path()
    deleter = registry_deleter or _default_registry_deleter
    for browser in browsers:
        if browser not in _REGISTRY_KEYS:
            raise ValueError(f"unknown browser {browser!r}")
        deleter(browser, path)
    remaining = [browser for browser in _REGISTRY_KEYS if browser not in set(browsers)]
    reader = registry_reader or (registered_manifest if registry_deleter is None else None)
    if remaining and reader is not None and _manifest_still_referenced(path, remaining, reader):
        return
    with contextlib.suppress(OSError):
        path.unlink()


def _manifest_still_referenced(
    path: Path,
    remaining_browsers: Sequence[str],
    reader: Callable[[str], Path | None],
) -> bool:
    """True when any remaining browser still has a native-host registration pointing to *path*."""
    resolved_target = path.resolve()
    for browser in remaining_browsers:
        registered = reader(browser)
        if registered is None:
            continue
        try:
            if registered.resolve() == resolved_target:
                return True
        except OSError:
            if str(registered) == str(path):
                return True
        if registered.is_file() and path.is_file() and same_manifest(registered, path):
            return True
    return False


def _default_registry_writer(browser: str, manifest_path: Path) -> None:
    if sys.platform == "win32":
        import winreg

        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REGISTRY_KEYS[browser], 0, winreg.KEY_WRITE)
        try:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(manifest_path))
        finally:
            winreg.CloseKey(key)
        return
    target = _native_messaging_dir(browser) / f"{HOST_NAME}.json"
    write_native_host_manifest(target, json.loads(manifest_path.read_text(encoding="utf-8")))


def _default_registry_deleter(browser: str, _manifest_path: Path) -> None:
    if sys.platform == "win32":
        import winreg

        with contextlib.suppress(FileNotFoundError, OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEYS[browser])
        return
    with contextlib.suppress(OSError):
        (_native_messaging_dir(browser) / f"{HOST_NAME}.json").unlink()


def _native_messaging_dir(browser: str) -> Path:
    if browser == "chromium":
        folder = "chromium"
    elif browser == "chrome":
        folder = "google-chrome"
    else:
        folder = "microsoft-edge"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        names = {"chrome": "Google/Chrome", "chromium": "Chromium", "edge": "Microsoft Edge"}
        return base / names[browser] / "NativeMessagingHosts"
    return Path.home() / ".config" / folder / "NativeMessagingHosts"


def default_chrome_executable() -> Path | None:
    """Best-effort Chrome/Chromium executable (e.g. for Chrome 144+ DevTools remote debugging)."""
    from ..cdp.launcher import find_browser_executable

    return find_browser_executable(purpose="chrome_devtools", log_result=False)


def _default_chrome_user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def chrome_devtools_ws(user_data_dir: Path | None = None) -> str:
    """Read Chrome 144+'s DevToolsActivePort and build the browser WebSocket URL."""
    base = user_data_dir or _default_chrome_user_data_dir()
    try:
        lines = (base / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip() if len(lines) > 1 else "/devtools/browser"
        path = "/" + path.lstrip("/")
        if not 1 <= port <= 65535 or not re.fullmatch(r"/devtools/browser(?:/[a-zA-Z0-9_-]+)?", path):
            raise ValueError("invalid Chrome DevTools endpoint")
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError(
            "Chrome remote debugging is not enabled. Open chrome://inspect/#remote-debugging, "
            "enable 'Allow remote debugging', then approve the connection."
        ) from exc
    return f"ws://127.0.0.1:{port}{path}"


def load_extension_via_cdp(
    extension_dir: Path,
    *,
    ws_url: str | None = None,
    client_factory: Callable[[str], Awaitable[Any]] | None = None,
) -> str:
    """Load an unpacked extension through Chrome's DevTools browser socket."""
    url = ws_url or chrome_devtools_ws()

    async def run() -> str:
        client: Any
        if client_factory is None:
            from ..cdp.client import CDPClient

            client = await CDPClient.connect(url)
        else:
            client = await client_factory(url)
        try:
            result = await client.send(
                "Extensions.loadUnpacked",
                {"path": str(extension_dir)},
                timeout=30,
            )
            return str(result.get("id", ""))
        finally:
            await client.aclose()

    return asyncio.run(run())


def _launch(command: list[str]) -> None:
    """Launch an interactive browser/explorer window (never pass SW_HIDE)."""
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_folder(path: Path) -> None:
    if sys.platform == "win32":
        _launch([windows_system_exe("explorer.exe"), str(path)])
    elif sys.platform == "darwin":
        _launch(["open", str(path)])
    else:
        _launch(["xdg-open", str(path)])


def open_browser_setup(extension_dir: Path, *, chrome_path: Path | None = None) -> None:
    """Open the two things the one-time Load unpacked step needs."""
    chrome = chrome_path if chrome_path is not None else default_browser_executable()
    if chrome is not None:
        try:
            _launch([str(chrome), "chrome://extensions"])
            print("opened chrome://extensions")
        except OSError as exc:
            print(f"could not open Chrome: {exc}")
    else:
        print("open chrome://extensions manually")
    try:
        _open_folder(extension_dir)
    except OSError as exc:
        print(f"could not open the extension folder: {exc}")
    print("Enable Developer mode, click Load unpacked, and choose:")
    print(f"  {extension_dir}")


def default_browser_executable() -> Path | None:
    """Best-effort Chrome/Chromium/Edge executable for Bridge setup and diagnostics."""
    from ..cdp.launcher import find_browser_executable

    return find_browser_executable(purpose="bridge", log_result=False)


def registered_manifest(browser: str) -> Path | None:
    """The manifest the browser actually reads for *browser*, if registered."""
    if sys.platform == "win32":
        import winreg

        key_name = _REGISTRY_KEYS[browser]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            return None
        return Path(str(value))
    path = _native_messaging_dir(browser) / f"{HOST_NAME}.json"
    return path if path.is_file() else None


def same_manifest(left: Path, right: Path) -> bool:
    try:
        first = json.loads(left.read_text(encoding="utf-8"))
        second = json.loads(right.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    return (
        first.get("name") == second.get("name")
        and Path(str(first.get("path", ""))).resolve() == Path(str(second.get("path", ""))).resolve()
        and first.get("allowed_origins") == second.get("allowed_origins")
    )


def doctor(*, manifest_path: Path | None = None, extension_dir: Path | None = None) -> int:
    """Print actionable bridge setup diagnostics; return non-zero on problems."""
    problems = 0
    extension_id = ""
    chrome = default_browser_executable()
    if chrome and chrome.is_file():
        print(f"browser: {chrome}")
    else:
        print("browser: not found (install Chrome/Chromium/Edge or open chrome://extensions manually)")
        problems += 1
    extension = extension_dir or default_extension_install_dir()
    ext_manifest = extension / "manifest.json"
    print(f"extension: {extension if ext_manifest.is_file() else 'not installed'}")
    if not ext_manifest.is_file():
        problems += 1
    else:
        try:
            extension_id = extension_id_from_manifest(extension)
            extension_manifest = json.loads(ext_manifest.read_text(encoding="utf-8"))
            extension_version = str(extension_manifest.get("version", ""))
            print(f"extension version: {extension_version} (plugin {__version__}, protocol {PROTOCOL_VERSION})")
            if extension_version != __version__:
                print("extension: version differs from the plugin (re-run install)")
                problems += 1
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            extension_id = ""
            print("extension: manifest has no usable key")
            problems += 1
        if extension_id:
            print(f"extension id: {extension_id}")
    manifest_file = manifest_path or native_manifest_path()
    print(f"native host manifest: {manifest_file if manifest_file.is_file() else 'not installed'}")
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
            origins = manifest.get("allowed_origins") or []
            if extension_id and f"chrome-extension://{extension_id}/" not in origins:
                print("native host: extension id does not match allowed_origins")
                problems += 1
            host_path = Path(str(manifest.get("path", "")))
            if host_path.is_file():
                print(f"native host: {host_path}")
            else:
                print(f"native host: missing executable {host_path}")
                problems += 1
        except (OSError, ValueError, TypeError, AttributeError):
            print("native host: unreadable manifest")
            problems += 1
    else:
        problems += 1
    registered = [path for path in (registered_manifest(name) for name in ("chrome", "chromium", "edge")) if path]
    if not any(path.is_file() for path in registered):
        print("native host registration: not found (run: icode-browser-use-bridge install)")
        problems += 1
    elif not any(same_manifest(path, manifest_file) for path in registered if path.is_file()):
        print("native host registration: points at a different manifest (re-run install)")
        problems += 1
    try:
        from .host import bridge_file

        discovery = bridge_file()
    except Exception as exc:
        print(f"bridge file: configuration error: {exc}")
        problems += 1
        discovery = None
    if discovery is not None and discovery.is_file():
        try:
            payload = json.loads(discovery.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            pid = int(payload.get("pid", 0))
            generation = str(payload.get("generation", ""))
            if not _pid_alive(pid):
                print(f"bridge file: stale pid={pid} (plugin not running)")
                problems += 1
            else:
                print(f"bridge file: pid={pid} generation={generation[:8]}")
        except (OSError, ValueError, TypeError):
            print("bridge file: unreadable")
            problems += 1
    else:
        print("bridge file: none (plugin not running)")
    broker = discovery.parent / "broker" / "broker.json" if discovery is not None else None
    if broker is not None and broker.is_file():
        try:
            payload = json.loads(broker.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            pid = int(payload.get("pid", 0))
            protocol = payload.get("protocol_version", "?")
            if not _pid_alive(pid):
                print(f"broker file: stale pid={pid} (broker not running)")
                problems += 1
            else:
                print(f"broker file: pid={pid} protocol={protocol}")
        except (OSError, ValueError, TypeError):
            print("broker file: unreadable")
            problems += 1
    else:
        print("broker file: none (broker not running)")
    print(f"doctor: {'ok' if problems == 0 else f'{problems} problem(s)'}")
    return 0 if problems == 0 else 1


def main(argv: Sequence[str] | None = None) -> None:
    """``icode-browser-use-bridge install|uninstall``."""
    parser = argparse.ArgumentParser(prog="icode-browser-use-bridge")
    subcommands = parser.add_subparsers(dest="command", required=True)

    install = subcommands.add_parser("install", help="register the native host")
    install.add_argument("--host-path", default=None, help="icode-browser-use-host executable")
    install.add_argument("--extension-dir", default=None, help="source extension folder")
    install.add_argument("--extension-copy", default=None, help="where to copy the unpacked extension")
    install.add_argument("--browser", action="append", choices=["chrome", "chromium", "edge"])
    install.add_argument("--manifest-path", default=None)
    install.add_argument("--open", action="store_true", help="open chrome://extensions and the extension folder")
    install.add_argument(
        "--chrome-cdp",
        action="store_true",
        help="load via Chrome 144+ chrome://inspect remote debugging",
    )

    uninstall = subcommands.add_parser("uninstall", help="remove the native host registration")
    uninstall.add_argument("--browser", action="append", choices=["chrome", "chromium", "edge"])
    uninstall.add_argument("--manifest-path", default=None)

    check = subcommands.add_parser("doctor", help="diagnose bridge setup")
    check.add_argument("--manifest-path", default=None)
    check.add_argument("--extension-dir", default=None)

    args = parser.parse_args(argv)
    if args.command == "doctor":
        raise SystemExit(
            doctor(
                manifest_path=Path(args.manifest_path) if args.manifest_path else None,
                extension_dir=Path(args.extension_dir) if args.extension_dir else None,
            )
        )
    browsers = tuple(args.browser or ("chrome", "chromium", "edge"))
    if args.command == "install":
        raw_host = args.host_path or which_trusted("icode-browser-use-host") or ""
        host = Path(raw_host).expanduser().resolve() if raw_host else Path("")
        if not raw_host or not host.is_file():
            raise SystemExit("icode-browser-use-host was not found; pass --host-path")
        source = Path(args.extension_dir).expanduser().resolve() if args.extension_dir else packaged_extension_dir()
        target = (
            Path(args.extension_copy).expanduser().resolve() if args.extension_copy else default_extension_install_dir()
        )
        extension_dir = install_extension(source, target)
        extension_id = extension_id_from_manifest(extension_dir)
        manifest_path = Path(args.manifest_path).expanduser().resolve() if args.manifest_path else None
        path = install_native_host(host, extension_id, manifest_path=manifest_path, browsers=browsers)
        print(f"extension: {extension_dir}")
        print(f"installed: {path}")
        if args.open:
            open_browser_setup(extension_dir)
        if args.chrome_cdp:
            try:
                loaded = load_extension_via_cdp(extension_dir)
            except Exception as exc:  # noqa: BLE001 - CLI must report the setup failure
                raise SystemExit(f"cannot load extension via Chrome CDP: {exc}") from exc
            print(f"loaded: {loaded}")
        return
    manifest_path = Path(args.manifest_path) if args.manifest_path else None
    uninstall_native_host(manifest_path=manifest_path, browsers=browsers)
    print("uninstalled")


if __name__ == "__main__":
    main()
