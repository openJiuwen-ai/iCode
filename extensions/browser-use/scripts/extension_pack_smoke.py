# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in acceptance of the exported folder package in a clean environment.

One temporary uv venv contains only the pinned iCode host -- never the
``icode-browser-use`` wheel. In that environment the script exports
``extensions/browser/`` from this checkout and runs it the way iCode's extension
loader does:

* neither the wheel nor this checkout is importable there;
* the pack's own ``doctor.py --json`` reports the folder origin, the host API, a
  clean configuration and the "cannot start the Broker here" advice;
* a real browser runs from the pack: Owned (launched and closed by the plugin)
  and Attach (the plugin leaves the browser this script started alive).

Everything runs on loopback against ``scripts/fixture_site.py``; browser state
lives in temporary folders. Run from the repository root::

    python scripts/extension_pack_smoke.py --host-version 0.35.2 --allow-browser

Use the actual supported iCode-TUI release instead of the example version. JSON
lines on stdout record what ran.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PACK_MODULE = "_chrys_extension_browser"
BUILDER = runpy.run_path(str(ROOT / "scripts" / "export_extension_pack.py"))


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def parser() -> argparse.ArgumentParser:
    check = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    check.add_argument("--host-version", required=True, help="Exact iCode-TUI version (no ranges)")
    check.add_argument("--python", default="3.14", help="uv Python interpreter/version for the temporary venv")
    check.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise plugin discovery")
    check.add_argument(
        "--allow-browser", action="store_true", help="Opt in to starting a real browser from the folder package"
    )
    check.add_argument("--runner", action="store_true", help=argparse.SUPPRESS)
    check.add_argument("--pack", help=argparse.SUPPRESS)
    check.add_argument("--repo", help=argparse.SUPPRESS)
    return check


def main() -> None:
    args = parser().parse_args()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9.+-]*)", args.host_version):
        parser().error("--host-version must be an exact release version, not a requirement or range")
    if args.runner:
        asyncio.run(runner(args))
        return
    if not args.allow_browser:
        parser().error("--allow-browser is required; no environment was created and no browser was started")

    from smoke_common import chrome_executable, clean_environment

    executable = args.executable or str(chrome_executable())
    env = clean_environment()
    with tempfile.TemporaryDirectory(prefix="pack-smoke-", ignore_cleanup_errors=True) as directory:
        root = Path(directory).resolve()
        pack = BUILDER["build"](root / "dist", ROOT / "src" / "icode_browser_use")
        report("exported", pack=str(pack))
        venv = root / "venv"
        _run(["uv", "venv", "--python", args.python, str(venv), "--no-config"], root, env, timeout=60)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run(
            ["uv", "pip", "install", "--python", str(python), f"iCode-TUI=={args.host_version}", "--no-config"],
            root,
            env,
            timeout=180,
        )
        _run(
            [
                str(python),
                str(Path(__file__).resolve()),
                "--runner",
                "--pack",
                str(pack),
                "--host-version",
                args.host_version,
                "--repo",
                str(ROOT),
                "--executable",
                executable,
            ],
            root,
            _runner_environment(python.parent, env),
            timeout=300,
        )


def _runner_environment(scripts: Path, env: dict[str, str]) -> dict[str, str]:
    """PATH without another environment's scripts, so the temporary venv really is clean."""
    prefix = Path(sys.prefix).resolve()
    kept = [
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part and not Path(part).resolve().is_relative_to(prefix)
    ]
    return {**env, "PATH": os.pathsep.join([str(scripts), *kept])}


def _run(command: list[str], root: Path, env: dict[str, str], *, timeout: int) -> None:
    subprocess.run(command, cwd=root, env=env, check=True, stdin=subprocess.DEVNULL, timeout=timeout)


async def runner(args) -> None:
    require(sys.prefix != sys.base_prefix, "runner must use the temporary venv interpreter")
    require(importlib.metadata.version("iCode-TUI") == args.host_version, "the host version differs from the pin")
    require(importlib.util.find_spec("icode_browser_use") is None, "the wheel must not be installed")
    require(str(Path(args.repo).resolve() / "src") not in sys.path, "the checkout must not be importable")
    report("path_host", icode_browser_use_host=shutil.which("icode-browser-use-host"))
    pack = Path(args.pack).resolve()
    require((pack / "icode_browser_use" / "extension" / "manifest.json").is_file(), "the exported pack is incomplete")
    report("environment", python=sys.executable, host_version=args.host_version, pack=str(pack))

    from fixture_site import serve

    with tempfile.TemporaryDirectory(prefix="pack-state-", ignore_cleanup_errors=True) as directory:
        root = Path(directory).resolve()
        config = root / "config"
        env = {
            "ICODE_BROWSER_ALLOWED_ORIGINS": "http://127.0.0.1:1",
            "ICODE_BROWSER_HEADLESS": "1",
            "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
            "ICODE_BROWSER_EXECUTABLE": args.executable,
            "ICODE_BROWSER_STATE_DIR": str(root / "state"),
        }
        installed = install(pack, root, config, env)
        with serve() as (origin, _state):
            env = {**env, "ICODE_BROWSER_ALLOWED_ORIGINS": origin}
            doctor(installed, root, env, args)
            extension = host_extension(installed)
            await owned(extension, root, origin, env)
            await attach(extension, root, origin, env)
        uninstall(installed, root, config, env)


def install(pack: Path, root: Path, config: Path, env: dict[str, str]) -> Path:
    """Install the exported folder where iCode reads user extensions — from the pack's own command."""
    environment = _environment(env)
    completed = subprocess.run(
        [sys.executable, "-I", str(pack / "doctor.py"), "install", "--config-dir", str(config)],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    installed = config / "extensions" / "browser"
    require(completed.returncode == 0, f"install exited {completed.returncode}: {completed.stderr}")
    require((installed / "__init__.py").is_file(), "install did not copy the folder package")
    report("installed", folder=str(installed))
    return installed


def uninstall(installed: Path, root: Path, config: Path, env: dict[str, str]) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", str(installed / "doctor.py"), "uninstall", "--config-dir", str(config)],
        env=_environment(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    require(completed.returncode == 0, f"uninstall exited {completed.returncode}: {completed.stderr}")
    require(not installed.exists(), "uninstall left the folder package behind")
    profiles = list((root / "state-owned").rglob("chrome"))
    require(profiles, "uninstall removed browser profiles")
    report("uninstalled", folder=str(installed), profiles=[str(path) for path in profiles])


def host_extension(installed: Path) -> object:
    """Load the *installed* folder through iCode's own discovery and file loader."""
    from chrys.service.extensions import _import_uncached, discover_extensions

    catalog = discover_extensions(installed.parent)
    require(not catalog.conflicts, f"the host sees conflicting sources: {catalog.conflicts}")
    source = catalog.sources.get("browser")
    require(source is not None, f"the host did not discover the installed package: {catalog.sources}")
    require(source.origin == "file" and source.path == installed / "__init__.py", f"unexpected source: {source}")
    extension = _import_uncached(source)
    report("host_load", origin=source.origin, location=source.location)
    return extension


def _environment(env: dict[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ICODE_BROWSER_")}
    environment.update(env)
    return environment


def doctor(pack: Path, root: Path, env: dict[str, str], args) -> None:
    environment = _environment({**env, "ICODE_BROWSER_STATE_DIR": str(root / "state-doctor")})
    completed = subprocess.run(
        [sys.executable, "-I", str(pack / "doctor.py"), "--json", "--config-dir", str(root / "config")],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    require(completed.returncode == 0, f"doctor exited {completed.returncode}: {completed.stderr}")
    document = json.loads(completed.stdout)
    codes = {problem["code"] for problem in document["problems"]}
    require(document["plugin"]["install"] == "folder", f"doctor did not report a folder install: {document['plugin']}")
    require(document["plugin"]["distribution"] is None, "the wheel must not appear in the report")
    require(document["plugin"]["module"].startswith(PACK_MODULE + "."), "doctor did not run from the pack")
    require(
        document["plugin"]["import"] == "ok", f"the host cannot import the installed extension: {document['plugin']}"
    )
    require(document["host"]["version"] == args.host_version, f"host version differs: {document['host']}")
    require(document["host"]["extension_api"] == "ok", f"host API incomplete: {document['host']}")
    require(document["config"]["ok"] and document["config"]["backend"] == "owned", "configuration not usable")
    require(document["state"]["writable"] is True, "the state folder is not writable")
    require(document["broker"]["launchable"] is False, "a folder package must not claim it can start the Broker")
    require("broker_not_running" in codes and "bridge_not_running" in codes, f"missing advice: {sorted(codes)}")
    host = document["bridge"]["host_executable"]
    if host is None:
        require("bridge_host_missing" in codes, "an absent icode-browser-use-host was not reported with advice")
    else:
        require("bridge_host_missing" not in codes, "an executable on PATH must not be reported as missing")
    require(all(problem["advice"] for problem in document["problems"]), "every problem needs advice")
    require(document["ok"] is True, f"doctor is not clean: {document['problems']}")

    connected = subprocess.run(
        [sys.executable, "-I", str(pack / "doctor.py"), "--json", "--connect", "--config-dir", str(root / "config")],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    require(connected.returncode == 0, f"doctor --connect exited {connected.returncode}: {connected.stderr}")
    report("doctor", codes=sorted(codes), backend=document["config"]["backend"])


async def owned(extension, root: Path, origin: str, env: dict[str, str]) -> None:
    os.environ.update(env, ICODE_BROWSER_STATE_DIR=str(root / "state-owned"))
    tools, core = _tools(extension, root, "owned")
    browser_id = _open(await _invoke(tools, "browser_open"))
    session = core._browsers[browser_id].session
    navigate = await _invoke(tools, "browser_navigate", browser_id=browser_id, url=f"{origin}/")
    require("fixture-home" in navigate, f"navigation failed: {navigate}")
    child = session._child
    require(child is not None and child.alive, "the plugin did not own a live browser")
    pid = child.pid
    await _invoke(tools, "browser_close", browser_id=browser_id)
    require(_waited_gone(child), "the plugin closed its handle but left the browser running")
    report("owned", pid=pid, url=session.snapshot.url)


async def attach(extension, root: Path, origin: str, env: dict[str, str]) -> None:
    profile = root / "attach-chrome"
    profile.mkdir()
    launcher = importlib.import_module(f"{PACK_MODULE}.icode_browser_use.cdp.launcher")
    chrome = subprocess.Popen(
        launcher.chrome_argv(str(Path(env["ICODE_BROWSER_EXECUTABLE"]).resolve()), profile, headless=True),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cdp_url = _cdp_url(profile, chrome)
        os.environ.update(env, ICODE_BROWSER_STATE_DIR=str(root / "state-attach"), ICODE_BROWSER_CDP_URL=cdp_url)
        tools, core = _tools(extension, root, "attach")
        browser_id = _open(await _invoke(tools, "browser_open"))
        session = core._browsers[browser_id].session
        require(session.cfg.backend == "attach", "the pack did not attach")
        navigate = await _invoke(tools, "browser_navigate", browser_id=browser_id, url=f"{origin}/")
        require("fixture-home" in navigate, f"attach navigation failed: {navigate}")
        await _invoke(tools, "browser_close", browser_id=browser_id)
        require(chrome.poll() is None, "closing plugin resources must not close the user's browser")
        report("attach", pid=chrome.pid, cdp=cdp_url)
    finally:
        os.environ.pop("ICODE_BROWSER_CDP_URL", None)
        if chrome.poll() is None:
            chrome.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                chrome.wait(timeout=10)


def _tools(extension, root: Path, name: str):
    """Bind the installed extension's tools the way iCode does."""
    from chrys.service.extensions import bind_extension

    build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=root, working_dirs=()),
        agent_name="pack-smoke",
        caller_name=None,
        session_id=f"pack-smoke-{name}",
        session_dir=root / f"session-{name}",
    )
    bound = bind_extension("browser", extension, build).tools
    core = importlib.import_module(f"{PACK_MODULE}.icode_browser_use.extension")
    return {tool.name: tool for tool in bound}, core


async def _invoke(tools, name: str, **arguments: object) -> str:
    contents = await tools[name].invoke(arguments=arguments)
    require(isinstance(contents, list) and contents, f"{name}: expected Content results")
    return "\n".join(item.text for item in contents)


def _open(result: str) -> str:
    match = re.fullmatch(r"browser_id: (b[0-9a-f]+)", result.strip())
    if match is None:
        raise RuntimeError(f"unexpected browser_open result: {result}")
    return match.group(1)


def _cdp_url(profile: Path, chrome: subprocess.Popen[bytes]) -> str:
    port_file = profile / "DevToolsActivePort"
    for _ in range(300):
        if chrome.poll() is not None:
            raise RuntimeError(f"Chrome exited with code {chrome.returncode} before exposing its CDP port")
        try:
            port = int(port_file.read_text(encoding="utf-8").splitlines()[0].strip())
        except (OSError, IndexError, ValueError):
            time.sleep(0.1)
            continue
        return f"http://127.0.0.1:{port}"
    raise RuntimeError("Chrome did not expose its CDP port")


def _waited_gone(child, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not child.alive:
            return True
        time.sleep(0.1)
    return not child.alive


if __name__ == "__main__":
    main()
