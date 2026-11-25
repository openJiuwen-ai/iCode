# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import importlib
import os
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import build_tool_defs
from icode_browser_use.validation import validate

# Adapter-only handles/lifecycle and screenshot persistence are deliberate, not drift.
CORE_ONLY_PARAMS = {"browser_screenshot": {"annotate"}}
NULLABLE_OMISSIONS = {
    ("browser_scroll", "target"),
    ("browser_scroll", "to_text"),
    ("browser_extract", "query"),
    ("browser_screenshot", "target"),
}


@pytest.mark.parametrize("tool", build_tool_defs(evaluate_enabled=True, save_state_enabled=True), ids=lambda t: t.name)
def test_core_schema_preserves_parameter_contract(tool):
    assert set(tool.schema["properties"]) == set(tool.params)
    assert set(tool.schema.get("required", ())) == {name for name, spec in tool.params.items() if spec.get("required")}
    for name, spec in tool.params.items():
        prop = tool.schema["properties"][name]
        assert not {"min", "max", "required"} & prop.keys()
        for key in ("type", "default", "enum", "pattern", "description"):
            if key in spec:
                assert prop[key] == spec[key]
        for internal, public in (("min", "minimum"), ("max", "maximum")):
            if internal in spec:
                assert prop[public] == spec[internal]


@pytest.fixture(
    params=[("ICODE_BROWSER_", False), ("ICODE_BROWSER_", True), ("ICODE_BROWSER_", False), ("ICODE_BROWSER_", True)]
)
def native(request, monkeypatch, tmp_path):
    binding = pytest.importorskip("chrys.service.extensions")
    import icode_browser_use.extension as ext

    prefix, enabled = request.param
    with monkeypatch.context() as env:
        for key in tuple(os.environ):
            if key.startswith("ICODE_BROWSER_"):
                env.delenv(key)
        env.setenv(prefix + "ALLOWED_DOMAINS", "www.iana.org")
        env.setenv(prefix + "STATE_DIR", str(tmp_path / "state"))
        if enabled:
            env.setenv(prefix + "EVALUATE", "allow")
            env.setenv(prefix + "SAVE_STATE", "allow")
        importlib.reload(ext)
        build = SimpleNamespace(
            runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
            agent_name="contract",
            caller_name=None,
            session_id="contract",
            session_dir=tmp_path / "session",
        )
        bound = binding.bind_extension("browser", ext.extension, build)
        try:
            yield ext, {tool.name: tool for tool in bound.tools}, enabled
        finally:
            for browser in list(ext._browsers.values()):
                asyncio.run(browser.session.aclose())
            ext._browsers.clear()
    importlib.reload(ext)


def test_native_contract_compares_semantics_not_whole_schemas(native):
    _, tools, enabled = native
    core = {tool.name: tool for tool in build_tool_defs(evaluate_enabled=enabled, save_state_enabled=enabled)}
    adapter_only = {"browser_open", "browser_list", "browser_status", "browser_resume"}
    assert set(tools) == set(core) | adapter_only
    assert not tools["browser_open"].parameters().get("properties")
    for name in ("browser_list", "browser_status", "browser_resume"):
        assert set(tools[name].parameters()["properties"]) <= {"browser_id", "resume_id"}
    assert not tools["browser_list"].parameters().get("properties")
    assert set(tools["browser_status"].parameters()["required"]) == {"browser_id"}
    assert set(tools["browser_resume"].parameters()["required"]) == {"resume_id"}
    for name, definition in core.items():
        schema = tools[name].parameters()
        props = schema["properties"]
        assert set(props) == (set(definition.params) - CORE_ONLY_PARAMS.get(name, set())) | {"browser_id"}
        required = set(schema.get("required", ()))
        expected = {key for key, spec in definition.params.items() if spec.get("required")}
        if name == "browser_action_status":
            assert props["browser_id"]["default"] is None
        elif name == "browser_navigate":
            assert props["browser_id"]["default"] == ""
        else:
            expected.add("browser_id")
        assert required == expected
        assert "ctx" not in props  # Host injects ToolContext, never advertises it.
        for key, spec in definition.params.items():
            if key in CORE_ONLY_PARAMS.get(name, set()):
                assert spec["default"] is True  # Native screenshot retains core annotations.
                continue
            prop = props[key]
            if "default" in spec:
                assert prop["default"] == spec["default"]
            elif (name, key) in NULLABLE_OMISSIONS:
                assert prop["default"] is None
                assert {"type": "null"} in prop["anyOf"]
            else:
                assert "default" not in prop
            if "enum" in spec:
                assert prop["enum"] == spec["enum"]
            if spec.get("type") == "array":
                # A bare ``list`` would project an empty items schema; the element type must survive.
                assert prop["items"]["type"] == spec["items"]["type"]
            # Native annotations need not copy core bounds/patterns/descriptions.
            # The core still validates those constraints after adapter projection.


def _text(contents):
    assert isinstance(contents, list)
    assert contents and all(content.type == "text" for content in contents)
    return "\n".join(content.text for content in contents)


def test_native_invoke_parses_and_projects_defaults(native, monkeypatch):
    ext, tools, _ = native
    calls = []

    async def record(browser, name, arguments):
        calls.append((name, arguments, validate(ext._TOOLS[name].params, arguments)))
        return [{"type": "text", "text": "contract result"}]

    monkeypatch.setattr(ext, "_call", record)

    async def run():
        browser_id = _text(await tools["browser_open"].invoke(arguments={})).removeprefix("browser_id: ")
        try:
            for name, arguments, expected in (
                ("browser_find", {"query": "iana"}, {"query": "iana", "role": "", "limit": 20, "fresh": False}),
                ("browser_snapshot", {}, {"scope": "viewport", "interactive_only": False, "target": "", "since": 0}),
                (
                    "browser_click",
                    {"target": "link"},
                    {"target": "link", "button": "left", "click_count": 1, "dispatch": "mouse"},
                ),
                ("browser_tabs", {}, {}),
                ("browser_tab_switch", {"tab_id": "t-1"}, {"tab_id": "t-1"}),
                ("browser_press_key", {"key": "Enter"}, {"key": "Enter", "target": ""}),
                (
                    "browser_select_option",
                    {"target": "select", "values": ["alpha"]},
                    {"target": "select", "values": ["alpha"]},
                ),
                (
                    "browser_wait_for",
                    {"condition": "url", "value": "/slow"},
                    {"condition": "url", "target": "", "value": "/slow", "timeout_ms": 5000},
                ),
                ("browser_dialog", {"action": "accept", "text": "typed"}, {"action": "accept", "text": "typed"}),
                ("browser_downloads", {}, {}),
                ("browser_download_cancel", {"guid": "g-1"}, {"guid": "g-1"}),
                ("browser_scroll", {"target": None, "to_text": None}, {"direction": "down", "amount": 3}),
                ("browser_extract", {"query": None}, {"format": "text", "max_chars": 7000}),
                (
                    "browser_extract",
                    {"query": None, "offset": 7000},
                    {"format": "text", "max_chars": 7000, "offset": 7000},
                ),
                ("browser_screenshot", {"target": None}, {"full_page": False}),
            ):
                assert (
                    _text(await tools[name].invoke(arguments={"browser_id": browser_id, **arguments}))
                    == "contract result"
                )
                assert calls[-1][:2] == (name, expected)
                if name == "browser_screenshot":
                    assert calls[-1][2] == expected | {"annotate": True}
                elif name == "browser_extract":
                    # offset is only forwarded for paged reads; the core still defaults it to 0.
                    assert calls[-1][2] == {"offset": 0} | expected
                elif name == "browser_click":
                    assert calls[-1][2] == expected | {
                        "expect_condition": "",
                        "expect_target": "",
                        "expect_value": "",
                        "expect_timeout_ms": 5000,
                        "expect_return": "none",
                    }
                elif name == "browser_wait_for":
                    assert calls[-1][2] == expected | {"return_mode": "none", "max_chars": 2000}
                else:
                    assert calls[-1][2] == expected
            count = len(calls)
            for name, arguments in (
                ("browser_navigate", {"browser_id": browser_id}),
                ("browser_click", {"browser_id": browser_id, "target": "link", "button": "invalid"}),
                ("browser_screenshot", {"browser_id": browser_id, "annotate": False}),
            ):
                with pytest.raises(TypeError):
                    await tools[name].invoke(arguments=arguments)
            assert len(calls) == count
        finally:
            assert "closed" in _text(await tools["browser_close"].invoke(arguments={"browser_id": browser_id}))
        assert not ext._browsers

    asyncio.run(run())


def test_wheel_smoke_environment_and_artifact_identity(monkeypatch, tmp_path):
    import hashlib
    import runpy
    import zipfile
    from pathlib import Path

    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "wheel_extension_smoke.py"))
    for key in ("ICODE_BROWSER_CDP_URL", "ICODE_BROWSER_BRIDGE", "PYTHONPATH", "ICODE_SRC", "UV_TARGET", "VIRTUAL_ENV"):
        monkeypatch.setenv(key, "must-not-inherit")
    monkeypatch.setenv("UV_CONFIG_FILE", str(tmp_path / "hostile-uv.toml"))
    monkeypatch.setenv("HTTPS_PROXY", "keep-existing-network-policy")
    clean = smoke["clean_environment"]()
    assert not any(key.startswith("ICODE_BROWSER_") for key in clean)
    assert not {"PYTHONPATH", "ICODE_SRC", "UV_TARGET", "VIRTUAL_ENV", "UV_CONFIG_FILE"} & clean.keys()
    assert clean["HTTPS_PROXY"] == "keep-existing-network-policy"
    wheel = tmp_path / "icode_browser_use-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("icode_browser_use-1.2.3.dist-info/METADATA", "Name: icode-browser-use\nVersion: 1.2.3\n")
    assert smoke["wheel_identity"](wheel) == (hashlib.sha256(wheel.read_bytes()).hexdigest(), "1.2.3")


@pytest.fixture
def installed_wheel(tmp_path):
    import importlib.metadata
    import json
    import runpy
    import zipfile
    from pathlib import Path

    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "wheel_extension_smoke.py"))
    wheel = tmp_path / "icode_browser_use-1.2.3-py3-none-any.whl"
    dist_info = "icode_browser_use-1.2.3.dist-info"
    files = {
        "icode_browser_use/__init__.py": b"",
        "icode_browser_use/extension.py": b"extension = None\n",
        "icode_browser_use/scripts/snapshot.js": b"(() => 'snapshot')()\n",
        f"{dist_info}/METADATA": b"Name: icode-browser-use\nVersion: 1.2.3\n",
        f"{dist_info}/entry_points.txt": b"[chrys.extensions]\nbrowser = icode_browser_use.extension:extension\n",
        f"{dist_info}/RECORD": b"wheel record\n",
        "icode_browser_use/__pycache__/extension.cpython-314.pyc": b"wheel bytecode",
    }
    site = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
            installed = site / name
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(content)
    (site / dist_info / "RECORD").write_bytes(b"installer generated record\n")
    (site / "icode_browser_use/__pycache__/extension.cpython-314.pyc").write_bytes(b"installer generated bytecode")
    (site / dist_info / "direct_url.json").write_text(
        json.dumps({"url": wheel.as_uri(), "archive_info": {}}), encoding="utf-8"
    )
    dist = importlib.metadata.Distribution.at(site / dist_info)
    return smoke, wheel, dist


def test_wheel_smoke_uv_without_hash_verifies_installed_bytes(installed_wheel):
    import hashlib

    smoke, wheel, dist = installed_wheel
    digest, version = smoke["wheel_identity"](wheel)
    assert digest == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert version == dist.version
    smoke["verify_installation"](wheel, dist, digest)


@pytest.mark.parametrize(
    "name",
    [
        "icode_browser_use/__init__.py",
        "icode_browser_use/extension.py",
        "icode_browser_use/scripts/snapshot.js",
        "icode_browser_use-1.2.3.dist-info/METADATA",
        "icode_browser_use-1.2.3.dist-info/entry_points.txt",
    ],
)
@pytest.mark.parametrize("change", ["tamper", "delete"])
def test_wheel_smoke_uv_without_hash_rejects_changed_files(installed_wheel, name, change):
    smoke, wheel, dist = installed_wheel
    digest, _ = smoke["wheel_identity"](wheel)
    installed = dist.locate_file(name)
    if change == "delete":
        installed.unlink()
    else:
        installed.write_bytes(installed.read_bytes() + b"tampered\n")
    with pytest.raises(RuntimeError, match="Installed file differs") as exc:
        smoke["verify_installation"](wheel, dist, digest)
    assert name in str(exc.value)


@pytest.mark.parametrize("wrong_url", [None, "file:///elsewhere/icode_browser_use-1.2.3-py3-none-any.whl"])
def test_wheel_smoke_rejects_missing_or_different_wheel_url(installed_wheel, wrong_url):
    import json

    smoke, wheel, dist = installed_wheel
    direct = dist.locate_file("icode_browser_use-1.2.3.dist-info/direct_url.json")
    direct.write_text(json.dumps({"url": wrong_url, "archive_info": {}}), encoding="utf-8")
    digest, _ = smoke["wheel_identity"](wheel)
    with pytest.raises(RuntimeError, match="Installed wheel URL differs"):
        smoke["verify_installation"](wheel, dist, digest)


@pytest.mark.parametrize("form", ["hash", "hashes", "both"])
@pytest.mark.parametrize("valid", [True, False])
def test_wheel_smoke_checks_explicit_archive_hashes(installed_wheel, form, valid):
    import hashlib
    import json

    smoke, wheel, dist = installed_wheel
    digest, _ = smoke["wheel_identity"](wheel)
    archive_info = {}
    if form in ("hashes", "both"):
        archive_info["hashes"] = {"sha256": digest, "sha512": hashlib.sha512(wheel.read_bytes()).hexdigest()}
    if form in ("hash", "both"):
        archive_info["hash"] = "sha256=" + (digest if valid else "0" * 64)
    elif not valid:
        archive_info["hashes"]["sha256"] = "0" * 64
    direct = dist.locate_file("icode_browser_use-1.2.3.dist-info/direct_url.json")
    direct.write_text(json.dumps({"url": wheel.as_uri(), "archive_info": archive_info}), encoding="utf-8")
    if valid:
        smoke["verify_installation"](wheel, dist, digest)
    else:
        with pytest.raises(RuntimeError, match="Installed wheel sha256 differs"):
            smoke["verify_installation"](wheel, dist, digest)


def test_wheel_smoke_runner_timeout_unwinds_finally(monkeypatch, installed_wheel):
    import sys

    smoke, wheel, _ = installed_wheel
    events = []
    roots = []
    wait_for = asyncio.wait_for

    async def stalled_runner(args, root):
        roots.append(root)
        try:
            events.append("started")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append("cleaned")

    async def short_timeout(awaitable, timeout):
        assert timeout == 120
        return await wait_for(awaitable, timeout=0.01)

    monkeypatch.setitem(smoke["run_with_timeout"].__globals__, "run_installed", stalled_runner)
    monkeypatch.setattr(asyncio, "wait_for", short_timeout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--runner",
            "--wheel",
            str(wheel),
            "--host-version",
            "1.2.3",
            "--allow-public-network",
        ],
    )
    with pytest.raises(asyncio.TimeoutError):
        smoke["main"]()
    assert events == ["started", "cleaned"]
    assert len(roots) == 1 and not roots[0].exists()


def test_wheel_smoke_requires_network_opt_in_before_install(monkeypatch):
    import runpy
    import sys
    from pathlib import Path

    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "wheel_extension_smoke.py"))
    monkeypatch.setattr(sys, "argv", ["smoke", "--wheel", "missing.whl", "--host-version", "1.2.3"])
    with pytest.raises(SystemExit) as exc:
        smoke["main"]()
    assert exc.value.code == 2


def test_wheel_smoke_installs_only_in_temporary_venv(monkeypatch, tmp_path):
    import runpy
    import subprocess
    import sys
    import zipfile
    from pathlib import Path

    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "wheel_extension_smoke.py"))
    wheel = tmp_path / "icode_browser_use-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("icode_browser_use-1.2.3.dist-info/METADATA", "Name: icode-browser-use\nVersion: 1.2.3\n")
    calls = []

    def record(command, *, cwd, env, check, stdin, timeout):
        assert stdin == subprocess.DEVNULL
        assert timeout in {60, 120, 180}
        assert check is True
        assert cwd.is_dir() and cwd != tmp_path
        assert not {"PYTHONPATH", "ICODE_SRC", "VIRTUAL_ENV", "UV_TARGET", "UV_CONFIG_FILE"} & env.keys()
        assert not any(key.startswith("ICODE_BROWSER_") for key in env)
        assert env["HTTPS_PROXY"] == "keep-existing-network-policy"
        if command[0] == "uv":
            assert "--no-config" in command
        calls.append((command, cwd))

    hostile = tmp_path / "uv.toml"
    hostile.write_text('[pip]\ntarget = "outside-venv"\nprefix = "outside-prefix"\n', encoding="utf-8")
    monkeypatch.setenv("UV_CONFIG_FILE", str(hostile))
    monkeypatch.setenv("HTTPS_PROXY", "keep-existing-network-policy")
    monkeypatch.setenv("PYTHONPATH", "source-checkout")
    monkeypatch.setenv("ICODE_SRC", "source-checkout")
    monkeypatch.setenv("UV_TARGET", "existing-environment")
    monkeypatch.setattr(subprocess, "run", record)  # No installer or browser runs in this unit test.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--wheel",
            str(wheel),
            "--host-version",
            "1.2.3",
            "--allow-public-network",
        ],
    )
    smoke["main"]()
    create, root = calls[0]
    assert create[:4] == ["uv", "venv", "--python", "3.14"]
    assert Path(create[4]) == root / "venv"
    install = calls[1][0]
    assert install[:4] == ["uv", "pip", "install", "--python"]
    assert Path(install[4]).is_relative_to(root / "venv")
    assert install[5:] == ["iCode-TUI==1.2.3", str(wheel.resolve()), "--no-config"]
    assert len(calls) == 2 + 1 + 2
    command, cwd = calls[2]
    assert command[0] == install[4]
    assert command[1] == "-I"
    assert "--runner" in command
    assert cwd == root
    uninstall, verify = calls[-2][0], calls[-1][0]
    assert uninstall[1:5] == ["pip", "uninstall", "--python", install[4]]
    assert uninstall[5:] == ["icode-browser-use", "--no-config"]
    assert verify[0] == install[4] and verify[1] == "-I" and verify[2] == "-c"
    assert "chrys.extensions" in verify[3]
    assert not root.exists()


@pytest.mark.parametrize("location", ["target", "prefix"])
def test_wheel_smoke_uv_ignores_hostile_config_offline(monkeypatch, installed_wheel, tmp_path, location):
    import json
    import shutil
    import subprocess
    import sys
    import zipfile
    from pathlib import Path

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("Optional offline probe requires uv")
    smoke, wheel, _ = installed_wheel
    with zipfile.ZipFile(wheel) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    dist_info = "icode_browser_use-1.2.3.dist-info"
    files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{dist_info}/RECORD"] = "".join(f"{name},,\n" for name in files).encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    outside = tmp_path / "outside-venv"
    config_home = tmp_path / "config"
    config = config_home / "uv" / "uv.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"[pip]\n{location} = {json.dumps(str(outside))}\n", encoding="utf-8")
    monkeypatch.setenv("UV_CONFIG_FILE", str(config))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    run = subprocess.run
    installed = []

    def probe(command, **kwargs):
        if command[0] != "uv":
            return  # No browser or pinned host is needed for this installer probe.
        assert "--no-config" in command
        assert "UV_CONFIG_FILE" not in kwargs["env"]
        command = [uv, *command[1:], "--offline", "--no-python-downloads", "--no-cache"]
        if command[1:3] == ["pip", "install"]:
            command.remove("iCode-TUI==1.2.3")
        run(command, **kwargs)
        if command[1:3] == ["pip", "install"]:
            python = command[command.index("--python") + 1]
            result = run(
                [python, "-I", "-c", "import icode_browser_use; print(icode_browser_use.__file__)"],
                capture_output=True,
                text=True,
                **kwargs,
            )
            assert Path(result.stdout.strip()).is_relative_to(kwargs["cwd"] / "venv")
            installed.append(True)

    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--wheel",
            str(wheel),
            "--host-version",
            "1.2.3",
            "--python",
            sys.executable,
            "--allow-public-network",
        ],
    )
    smoke["main"]()
    assert installed == [True]
    assert not outside.exists()


@pytest.mark.parametrize("failure", [False, True, "timeout"])
def test_wheel_smoke_cancellation_during_real_cleanup(monkeypatch, installed_wheel, tmp_path, failure):
    import sys

    binding = pytest.importorskip("chrys.service.extensions")
    import icode_browser_use.extension as adapter

    smoke, wheel, dist = installed_wheel
    globals_ = smoke["run_installed"].__globals__
    import chrys

    monkeypatch.setitem(globals_, "doctor_report", lambda root: None)
    # Keep the actual runner, assertions and finally; fake installation and browser I/O.
    monkeypatch.setitem(
        globals_,
        "sys",
        SimpleNamespace(
            prefix=str(tmp_path),
            base_prefix=sys.base_prefix,
            flags=SimpleNamespace(isolated=1),
            executable=sys.executable,
        ),
    )
    monkeypatch.setattr(adapter, "__file__", str(tmp_path / "plugin.py"))
    monkeypatch.setattr(chrys, "__file__", str(tmp_path / "host.py"))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: dist)
    monkeypatch.setattr(
        binding,
        "bind_extension",
        lambda *args: SimpleNamespace(
            tools=[
                SimpleNamespace(name="browser_evaluate"),
                SimpleNamespace(name="browser_tabs"),
                SimpleNamespace(name="browser_tab_switch"),
                SimpleNamespace(name="browser_press_key"),
                SimpleNamespace(name="browser_select_option"),
                SimpleNamespace(name="browser_wait_for"),
                SimpleNamespace(name="browser_dialog"),
                SimpleNamespace(name="browser_downloads"),
                SimpleNamespace(name="browser_download_cancel"),
            ],
        ),
    )

    async def load_extensions(names):
        return [SimpleNamespace(extension=adapter.extension)]

    monkeypatch.setattr(binding, "load_extensions", load_extensions)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    wait_for = asyncio.wait_for

    async def short_cleanup(awaitable, timeout):
        return await wait_for(awaitable, 0.05 if timeout == 30 else timeout)

    if failure == "timeout":
        monkeypatch.setattr(asyncio, "wait_for", short_cleanup)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []
        sessions = []

        async def close(index):
            events.append(("fallback", index))
            if failure and index == 0:
                raise RuntimeError("first fallback failed")

        for index in range(2):
            sessions.append(
                SimpleNamespace(
                    _child=None,
                    _client=None,
                    state="idle",
                    _transport=SimpleNamespace(owns_browser=True),
                    cfg=SimpleNamespace(
                        bridge=False,
                        broker=False,
                        cdp_url=None,
                        state_dir=tmp_path / "state" / str(index),
                        chrome_profile_dir=tmp_path / "state" / str(index) / "profile",
                    ),
                    aclose=lambda index=index: close(index),
                )
            )
        opened = 0

        async def invoke(tools, name, **arguments):
            nonlocal opened
            if name == "browser_open":
                browser_id = f"b{opened}"
                adapter._browsers[browser_id] = SimpleNamespace(session=sessions[opened])
                opened += 1
                return f"browser_id: {browser_id}"
            if name == "browser_navigate":
                raise RuntimeError("body stopped")
            assert name == "browser_close"
            events.append(("close", arguments["browser_id"]))
            if arguments["browser_id"] == "b1":
                entered.set()
                await release.wait()
                if failure:
                    raise RuntimeError("first close failed")
            return "closed"

        monkeypatch.setattr(adapter, "_browsers", {})
        monkeypatch.setitem(globals_, "invoke", invoke)
        args = SimpleNamespace(executable=None, wheel=wheel, host_version="1.2.3")
        task = asyncio.create_task(smoke["run_installed"](args, tmp_path))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        if failure != "timeout":
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await wait_for(task, 2)
        if failure == "timeout":
            assert events == [("close", "b1")]
        else:
            assert events == [("close", "b1"), ("close", "b0"), ("fallback", 0), ("fallback", 1)]
        assert not [pending for pending in asyncio.all_tasks() if pending is not asyncio.current_task()]

    asyncio.run(run())
