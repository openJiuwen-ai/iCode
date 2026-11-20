# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in installed-wheel smoke; never installs into an existing environment.

Example (the wheel must already exist; host version must be an exact release)::

    python scripts/wheel_extension_smoke.py --wheel dist/icode_browser_use-0.3.2-py3-none-any.whl \
        --host-version 0.35.2 --allow-public-network \
        --executable "C:/Program Files/Google/Chrome/Application/chrome.exe"

Use the actual supported iCode-TUI release instead of the example version. The
run uses a fresh isolated interpreter in a temporary uv venv. Only the specified
wheel and pinned host are installed. Browser state, profiles and image artifacts
live in temporary directories. No source-path injection, bridge, personal
configuration edits or network-policy workarounds are used. JSON lines on stdout
record provenance and checks; redirect stdout to retain the report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from email.parser import Parser
from pathlib import Path
from types import SimpleNamespace

ENV_PREFIX = "ICODE_BROWSER_"


def clean_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(ENV_PREFIX)
        and key.upper()
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "ICODE_SRC",
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
            "UV_CONFIG_FILE",
            "UV_TARGET",
            "UV_PREFIX",
            "UV_SYSTEM_PYTHON",
            "UV_BREAK_SYSTEM_PACKAGES",
        }
    }


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def wheel_identity(wheel: Path) -> tuple[str, str]:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise RuntimeError("Expected one distribution in wheel")
        metadata = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    if metadata["Name"].lower().replace("_", "-") != "icode-browser-use":
        raise RuntimeError("--wheel must be a icode-browser-use wheel")
    return digest, metadata["Version"]


def verify_installation(wheel: Path, dist: importlib.metadata.Distribution, digest: str) -> None:
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    require(direct.get("url") == wheel.resolve().as_uri(), "Installed wheel URL differs from requested artifact")
    archive_info = direct.get("archive_info", {})
    hashes = list(archive_info.get("hashes", {}).items())
    if "hash" in archive_info:
        algorithm, separator, value = archive_info["hash"].partition("=")
        require(bool(separator), "Invalid installed wheel hash")
        hashes.append((algorithm, value))
    for algorithm, expected in hashes:
        actual = digest if algorithm == "sha256" else hashlib.new(algorithm, wheel.read_bytes()).hexdigest()
        require(actual == expected, f"Installed wheel {algorithm} differs from requested artifact")

    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        require(len(metadata) == 1, "Expected one distribution in wheel")
        entry_points = metadata[0].removesuffix("METADATA") + "entry_points.txt"
        package_files = [
            name
            for name in archive.namelist()
            if name.startswith("icode_browser_use/")
            and not name.endswith(("/", ".pyc"))
            and "__pycache__" not in Path(name).parts
        ]
        require(bool(package_files), "Wheel contains no plugin package files")
        require(entry_points in archive.namelist(), "Wheel contains no entry_points.txt")
        for name in [*package_files, metadata[0], entry_points]:
            installed = Path(dist.locate_file(name))
            require(
                installed.is_file() and installed.read_bytes() == archive.read(name),
                f"Installed file differs from requested wheel: {name}",
            )


async def invoke(tools, name: str, **arguments: object) -> str:
    contents = await tools[name].invoke(arguments=arguments)
    if not isinstance(contents, list) or not contents or any(item.type != "text" for item in contents):
        raise RuntimeError(f"{name}: expected normally parsed text Content, got {contents!r}")
    return "\n".join(item.text for item in contents)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def doctor_report(root: Path) -> None:
    """The wheel's own diagnostics entry: it must see a distribution install and a usable config."""
    command = Path(sys.executable).parent / ("icode-browser-use.exe" if os.name == "nt" else "icode-browser-use")
    require(command.is_file(), f"the wheel did not install the icode-browser-use command: {command}")
    completed = subprocess.run(
        [str(command), "doctor", "--json", "--config-dir", str(root / "config")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    require(completed.returncode == 0, f"doctor exited {completed.returncode}: {completed.stderr}")
    document = json.loads(completed.stdout)
    require(document["plugin"]["install"] == "distribution", f"doctor install differs: {document['plugin']}")
    require(document["plugin"]["folder"] is None, f"doctor found a folder source: {document['plugin']}")
    require(
        (document["plugin"]["distribution"] or "").startswith("icode-browser-use"),
        f"doctor distribution label differs: {document['plugin']}",
    )
    require(document["plugin"]["import"] == "ok", f"the host cannot import the extension: {document['plugin']}")
    require(document["host"]["extension_api"] == "ok", f"host API incomplete: {document['host']}")
    require(document["config"]["backend"] == "owned", f"doctor backend differs: {document['config']}")
    require(document["config"]["network"] == "public-web", f"unexpected network mode: {document['config']}")
    require(document["broker"]["launchable"] is True, "an installed wheel must be able to start the Broker")
    require(document["ok"] is True, f"doctor is not clean: {document['problems']}")
    report(
        "doctor",
        install=document["plugin"]["install"],
        host_version=document["host"]["version"],
        codes=sorted(problem["code"] for problem in document["problems"]),
    )


async def run_installed(args, root: Path) -> None:
    # Must precede *all* host/plugin imports: optional tools register at import time.
    clean = clean_environment()
    os.environ.clear()
    os.environ.update(clean)
    settings = {
        "STATE_DIR": str(root / "state"),
        "HEADLESS": "1",
        "IDLE_CLOSE_SECONDS": "0",
        "BROKER": "0",
        "BRIDGE": "0",
        "EVALUATE": "allow",
    }
    if args.executable:
        settings["EXECUTABLE"] = args.executable
    os.environ.update({ENV_PREFIX + key: value for key, value in settings.items()})

    digest, version = wheel_identity(args.wheel)
    require(
        sys.prefix != sys.base_prefix and sys.flags.isolated == 1, "Runner requires an isolated venv interpreter (-I)"
    )
    require(importlib.metadata.version("iCode-TUI") == args.host_version, "Installed host version differs from pin")
    dist = importlib.metadata.distribution("icode-browser-use")
    require(dist.version == version, "Installed plugin version differs from wheel")
    verify_installation(args.wheel, dist, digest)
    points = [point for point in dist.entry_points if point.group == "chrys.extensions" and point.name == "browser"]
    require(len(points) == 1, "Wheel must provide exactly one native browser entry point")
    extension = points[0].load()

    import chrys
    from chrys.service.extensions import bind_extension, load_extensions

    import icode_browser_use.extension as adapter

    plugin_path = Path(adapter.__file__).resolve()
    host_path = Path(chrys.__file__).resolve()
    for path in (plugin_path, host_path):
        require(path.is_relative_to(Path(sys.prefix).resolve()), f"Import escaped isolated environment: {path}")
    report(
        "provenance",
        wheel=str(args.wheel),
        sha256=digest,
        version=version,
        host_version=args.host_version,
        plugin_import=str(plugin_path),
        host_import=str(host_path),
        python=sys.executable,
        env_prefix=ENV_PREFIX,
        entry_point=points[0].value,
    )
    loaded = await load_extensions(["browser"])
    require(
        len(loaded) == 1 and loaded[0].extension is extension,
        "Native host discovery did not load the wheel entry point",
    )
    doctor_report(root)
    build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=root, working_dirs=()),
        agent_name="wheel-smoke",
        agent_id=None,
        session_id="wheel-smoke",
        session_dir=root / "session",
    )
    tools = {tool.name: tool for tool in bind_extension("browser", extension, build).tools}
    require("browser_evaluate" in tools and "browser_save_state" not in tools, "Opt-in registration mismatch")
    require(
        {
            "browser_tabs",
            "browser_tab_switch",
            "browser_press_key",
            "browser_select_option",
            "browser_wait_for",
            "browser_dialog",
            "browser_downloads",
            "browser_download_cancel",
        }
        <= set(tools),
        "Installed wheel is missing the interaction tools",
    )
    handles = []
    sessions = []
    children = []
    try:
        # Two allocated handles prove slot/profile isolation without sharing user state.
        for _ in range(2):
            opened = await invoke(tools, "browser_open")
            match = re.fullmatch(r"browser_id: (b[0-9a-f]+)", opened.strip())
            require(match is not None, f"Unexpected browser_open result: {opened}")
            browser_id = match.group(1)
            handles.append(browser_id)
            session = adapter._browsers[browser_id].session
            sessions.append(session)
            require(
                session.state == "idle" and session._child is None and session._client is None,
                "browser_open must leave the session idle without launching Chrome",
            )
            require(session._transport.owns_browser, "Smoke must use Owned transport")
            require(
                not session.cfg.bridge and not session.cfg.broker and not session.cfg.cdp_url,
                "Unexpected attach/bridge/broker mode",
            )
            require(session.cfg.state_dir.is_relative_to(root / "state"), "State escaped temporary directory")
            require(
                session.cfg.chrome_profile_dir.is_relative_to(root / "state"), "Profile escaped temporary directory"
            )
        require(sessions[0].cfg.state_dir != sessions[1].cfg.state_dir, "Handles share state")
        require(sessions[0].cfg.chrome_profile_dir != sessions[1].cfg.chrome_profile_dir, "Handles share profile")
        for browser_id, session in zip(handles, sessions):
            nav = await invoke(
                tools, "browser_navigate", browser_id=browser_id, url="https://www.iana.org/domains/reserved"
            )
            require("IANA" in nav, f"IANA navigation failed: {nav}")
            child = session._child
            require(child is not None and child.alive, "Owned browser process is not alive")
            children.append(child)
            require(session.cfg.chrome_profile_dir.is_dir(), "Owned Chrome did not create its isolated profile")
            # Read-only diagnostics only; all browser actions go through the extension.
            chrome_version = await session.client.send("Browser.getVersion")
            require(bool(chrome_version.get("product")), "Browser.getVersion returned no product version")
            report("browser_version", browser_id=browser_id, env_prefix=ENV_PREFIX, **chrome_version)
        require(children[0].pid != children[1].pid, "Owned handles share a browser process")
        report(
            "owned",
            pids=[child.pid for child in children],
            state_dirs=[str(s.cfg.state_dir) for s in sessions],
            profiles=[str(s.cfg.chrome_profile_dir) for s in sessions],
        )
        browser_id = handles[0]
        relative_path = "/domains/reserved?wheel_smoke=relative"
        relative = await invoke(tools, "browser_navigate", browser_id=browser_id, url=relative_path)
        require(
            "IANA" in relative and sessions[0].snapshot.url == "https://www.iana.org" + relative_path,
            f"Relative navigation failed: {relative}",
        )
        require(
            sessions[1].snapshot.url.rstrip("/") == "https://www.iana.org/domains/reserved",
            "Sibling browser navigation changed",
        )
        snapshot = await invoke(tools, "browser_snapshot", browser_id=browser_id)
        require("IANA" in snapshot, f"Snapshot failed: {snapshot}")
        shot = await invoke(tools, "browser_screenshot", browser_id=browser_id)
        match = re.search(r"Screenshot saved to (.+?) \(([^)]*)\)\n", shot)
        require(match is not None, f"Screenshot failed: {shot}")
        require(re.search(r"image/", match.group(2)) and re.search(r"\d+x\d+", match.group(2)), f"no pixels: {shot}")
        require("bytes" in match.group(2), f"no byte size: {shot}")
        require("Session file: screenshots/" in shot, f"no session-relative path: {shot}")
        image = Path(match.group(1)).resolve()
        require(image.is_relative_to(root) and image.is_file(), "Screenshot escaped temporary session or is missing")
        require(image.read_bytes().startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8")), "Screenshot is not PNG/JPEG")
        marker = "wheel-smoke-page-exception"
        result = await invoke(
            tools, "browser_evaluate", browser_id=browser_id, expression=f"(() => {{ throw new Error('{marker}'); }})()"
        )
        require("页面异常" in result and result.count(marker) == 1, f"Page exception missing or duplicated: {result}")
        public = await invoke(tools, "browser_navigate", browser_id=browser_id, url="https://example.com/")
        require("Example Domain" in public, f"second public domain failed: {public}")
        local = await invoke(tools, "browser_navigate", browser_id=browser_id, url="http://127.0.0.1/")
        require("browser_blocked" in local, f"loopback was not blocked: {local}")
        report(
            "checks",
            env_prefix=ENV_PREFIX,
            navigate=True,
            public_web_without_allowlist=True,
            loopback_blocked=True,
            relative_url=True,
            snapshot=True,
            screenshot_bytes=image.stat().st_size,
            evaluate_exception_deduplicated=True,
        )
    finally:

        async def cleanup():
            # Capture children even if a page check failed before the normal capture.
            for session in sessions:
                if session._child is not None and session._child not in children:
                    children.append(session._child)
            failures = []
            for browser_id in reversed(handles):
                try:
                    closed = await invoke(tools, "browser_close", browser_id=browser_id)
                    require("closed" in closed, f"Close failed: {closed}")
                except Exception as exc:  # noqa: BLE001 - finish cleaning every owned session before raising
                    failures.append(str(exc))
            # Teardown fallback only for our sessions, never kill unrelated processes.
            for session in sessions:
                try:
                    await session.aclose()
                except Exception as exc:  # noqa: BLE001 - finish cleaning every owned session before raising
                    failures.append(str(exc))
            for child in children:
                for _ in range(100):
                    if not child.alive:
                        break
                    await asyncio.sleep(0.1)
                report("process_exit", pid=child.pid, alive=child.alive)
                if child.alive:
                    failures.append(f"Owned PID {child.pid} survived close")
            if failures:
                raise RuntimeError("; ".join(failures))

        # The cleanup deadline keeps running even if the runner is cancelled here.
        cleanup_task = asyncio.create_task(asyncio.wait_for(cleanup(), timeout=30))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as cancelled:
            try:
                await asyncio.shield(cleanup_task)
            finally:
                raise cancelled
    report("passed", env_prefix=ENV_PREFIX)


async def run_with_timeout(args, root: Path) -> None:
    # Leave 30s for shielded cleanup and another 30s before the outer 180s backstop.
    await asyncio.wait_for(run_installed(args, root), timeout=120)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wheel", type=Path, required=True, help="Already-built icode-browser-use wheel")
    parser.add_argument("--host-version", required=True, help="Exact iCode-TUI version (no ranges)")
    parser.add_argument("--python", default="3.14", help="uv Python interpreter/version for the temporary venv")
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise use plugin discovery")
    parser.add_argument(
        "--allow-public-network", action="store_true", help="Opt in to IANA page access and test-only evaluate"
    )
    parser.add_argument("--runner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.allow_public_network:
        parser.error("Explicit --allow-public-network is required; no installation or browser access performed")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9.+-]*)", args.host_version):
        parser.error("--host-version must be an exact release version, not a requirement or range")
    args.wheel = args.wheel.resolve(strict=True)
    if args.wheel.suffix != ".whl":
        parser.error("--wheel must name an already-built .whl")
    wheel_identity(args.wheel)
    if args.runner:
        state = tempfile.TemporaryDirectory(prefix="wheel-browser-state-")
        try:
            asyncio.run(run_with_timeout(args, Path(state.name).resolve()))
        finally:
            for attempt in range(5):
                try:
                    state.cleanup()
                    break
                except PermissionError as exc:
                    # Chrome's Windows cache handles can outlive its main process briefly.
                    if getattr(exc, "winerror", None) not in {5, 32} or attempt == 4:
                        raise
                    time.sleep(0.25)
        return
    with tempfile.TemporaryDirectory(prefix="wheel-browser-venv-") as directory:
        root = Path(directory).resolve()
        env = clean_environment()
        venv = root / "venv"
        subprocess.run(
            ["uv", "venv", "--python", args.python, str(venv), "--no-config"],
            cwd=root,
            env=env,
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                f"iCode-TUI=={args.host_version}",
                str(args.wheel),
                "--no-config",
            ],
            cwd=root,
            env=env,
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=180,
        )
        command = [
            str(python),
            "-I",
            str(Path(__file__).resolve()),
            "--runner",
            "--wheel",
            str(args.wheel),
            "--host-version",
            args.host_version,
            "--allow-public-network",
        ]
        if args.executable:
            command.extend(["--executable", args.executable])
        subprocess.run(command, cwd=root, env=env, check=True, stdin=subprocess.DEVNULL, timeout=180)
        _require_uninstall(python, root, env)


UNINSTALLED = """\
import importlib.metadata, importlib.util
if importlib.util.find_spec("icode_browser_use") is not None:
    raise SystemExit("the package is still importable")
if [point for point in importlib.metadata.entry_points(group="chrys.extensions") if point.name == "browser"]:
    raise SystemExit("the extension entry point is still installed")
"""


def _require_uninstall(python: Path, root: Path, env: dict[str, str]) -> None:
    """A clean uninstall leaves neither the package nor its extension entry point behind."""
    subprocess.run(
        ["uv", "pip", "uninstall", "--python", str(python), "icode-browser-use", "--no-config"],
        cwd=root,
        env=env,
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    subprocess.run(
        [str(python), "-I", "-c", UNINSTALLED],
        cwd=root,
        env=env,
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    script = python.parent / ("icode-browser-use.exe" if os.name == "nt" else "icode-browser-use")
    require(not script.exists(), f"the console script survived uninstall: {script}")
    require(not (root / "config" / "extensions" / "browser").exists(), "uninstall left a folder package")
    report("uninstalled", wheel="icode-browser-use")


if __name__ == "__main__":
    main()
