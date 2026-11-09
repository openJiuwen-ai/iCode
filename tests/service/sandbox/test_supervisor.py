# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Supervisor unit tests. The launcher is never executed and no socket is bound."""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from chrys.service.sandbox import client, supervisor
from chrys.service.sandbox.backend import SandboxError, run_shell

_REAL_CHOOSE = supervisor._choose_listen_url
_REAL_POLICY = supervisor._bundled_policy_path
_REAL_SPAWN = supervisor._spawn_launcher
_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src" / "chrys"
# Project dependencies that are ranges on purpose. Everything else stays an exact == pin.
_RANGE_PINS = frozenset(
    {
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-grpc",
        "opentelemetry-instrumentation-logging",
        "pyjwt",
    }
)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


@dataclass
class _FakeProcess:
    returncode: int | None = None


@pytest.fixture(autouse=True)
def _block_real_launcher(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(supervisor, "_choose_listen_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(supervisor, "_bundled_policy_path", lambda: "policy.yaml")

    @asynccontextmanager
    async def blocked(_env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        raise AssertionError("real launcher")
        yield _FakeProcess()

    monkeypatch.setattr(supervisor, "_spawn_launcher", blocked)


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    @asynccontextmanager
    async def spawn(env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        seen.append(dict(env))
        yield proc

    monkeypatch.setattr(supervisor, "_spawn_launcher", spawn)
    return seen


async def test_live_child_is_reused_until_it_exits(monkeypatch: pytest.MonkeyPatch):
    proc = _FakeProcess()
    seen = _patch_spawn(monkeypatch, proc)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    first = await supervisor.ensure_started("sekret")
    second = await supervisor.ensure_started("sekret")
    assert first == second == "http://127.0.0.1:9"
    assert len(seen) == 1
    assert seen[0]["JIUWENBOX_LISTEN"] == "http://127.0.0.1:9"
    assert seen[0]["JIUWENBOX_POLICY_PATH"] == "policy.yaml"
    assert seen[0]["JIUWENBOX_API_TOKEN"] == "sekret"
    proc.returncode = 1
    replacement = _FakeProcess()

    @asynccontextmanager
    async def spawn_again(env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        seen.append(dict(env))
        yield replacement

    monkeypatch.setattr(supervisor, "_spawn_launcher", spawn_again)
    third = await supervisor.ensure_started("")
    assert third == "http://127.0.0.1:9"
    assert len(seen) == 2
    assert "JIUWENBOX_API_TOKEN" not in seen[1]


async def test_child_exit_before_health_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(monkeypatch, _FakeProcess(returncode=3))
    probed = False

    async def healthy(_base_url: str, _token: str) -> bool:
        nonlocal probed
        probed = True
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError, match="exited") as caught:
        await supervisor.ensure_started("sekret")
    assert probed is False
    assert "sekret" not in str(caught.value)


async def test_health_that_never_arrives_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(monkeypatch, _FakeProcess())

    async def unhealthy(_base_url: str, _token: str) -> bool:
        return False

    monkeypatch.setattr(supervisor, "_health_ok", unhealthy)
    with pytest.raises(SandboxError, match="did not become healthy") as caught:
        await supervisor.ensure_started("sekret", health_timeout=0)
    assert "sekret" not in str(caught.value)


def test_listen_address_is_loopback_on_an_ephemeral_port(monkeypatch: pytest.MonkeyPatch):
    opened: list[_FakeSocket] = []

    class _FakeSocket:
        def __init__(self, family: int, sock_type: int, *_args: object, **_kwargs: object) -> None:
            self.family = family
            self.sock_type = sock_type
            self.bound: tuple[str, int] | None = None
            self.closed = False
            opened.append(self)

        def bind(self, address: tuple[str, int]) -> None:
            self.bound = address

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 43210)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(supervisor.socket, "socket", _FakeSocket)
    assert _REAL_CHOOSE() == "http://127.0.0.1:43210"
    assert opened[0].family == socket.AF_INET
    assert opened[0].sock_type == socket.SOCK_STREAM
    assert opened[0].bound == ("127.0.0.1", 0)
    assert opened[0].closed is True


def test_listen_failure_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    class _BadSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.closed = False

        def bind(self, _address: tuple[str, int]) -> None:
            raise OSError("denied")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(supervisor.socket, "socket", _BadSocket)
    with pytest.raises(SandboxError, match="local port"):
        _REAL_CHOOSE()


def test_child_env_sets_policy_listen_and_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "inherited")
    env = supervisor._child_env("C:/policy.yaml", "http://127.0.0.1:9", "next-token")
    assert env["JIUWENBOX_POLICY_PATH"] == "C:/policy.yaml"
    assert env["JIUWENBOX_LISTEN"] == "http://127.0.0.1:9"
    assert env["JIUWENBOX_API_TOKEN"] == "next-token"

    empty = supervisor._child_env("C:/policy.yaml", "http://127.0.0.1:9", "")
    assert "JIUWENBOX_API_TOKEN" not in empty


def test_launcher_argv_and_spawn_use_devnull():
    argv = supervisor._launcher_argv()
    assert argv == [sys.executable, "-m", "jiuwenbox.server.launcher"]
    source = (_SRC / "service" / "sandbox" / "supervisor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    spawn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_spawn_launcher"
    )
    text = ast.get_source_segment(source, spawn)
    assert text is not None
    assert "managed_subprocess" in text
    assert "stdin=subprocess.DEVNULL" in text
    assert "stdout=subprocess.DEVNULL" in text
    assert "stderr=subprocess.DEVNULL" in text


def test_bundled_policy_path_is_the_vendored_code_agent_file():
    path = _REAL_POLICY()
    assert os.path.isfile(path)
    assert os.path.basename(path) == "code-agent-policy.yaml"


async def test_health_probe_requires_status_ok(monkeypatch: pytest.MonkeyPatch):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if len(seen) == 1:
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"status": "down"})

    def new_client(**kwargs: object) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client, "_new_client", new_client)
    assert await supervisor._health_ok("http://sandbox.test", "sekret") is True
    assert await supervisor._health_ok("http://sandbox.test", "") is False
    assert seen == ["Bearer sekret", None]


async def test_non_linux_does_not_spawn_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_shell(linux=False)`` refuses before the supervisor is asked to spawn."""
    spawned = _patch_spawn(monkeypatch, _FakeProcess())

    async def health_must_not_run(_base_url: str, _token: str) -> bool:
        raise AssertionError(f"launcher spawned: {spawned}")

    monkeypatch.setattr(supervisor, "_health_ok", health_must_not_run)

    class _Settings:
        sandbox_enabled = True
        sandbox_url = ""
        sandbox_token = "sekret"

    with pytest.raises(SandboxError, match="only on Linux"):
        await run_shell(
            _Settings(),
            argv=["echo"],
            cwd=".",
            timeout_seconds=5,
            linux=False,
        )
    assert spawned == []


async def test_shutdown_close_does_not_raise_async_generator_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the retained child goes through the real stack close and must not raise."""
    from chrys.foundation.platform import process as platform_process

    proc = _FakeProcess()
    entered = False

    @asynccontextmanager
    async def fake_managed(*args: object, **kwargs: object) -> AsyncIterator[_FakeProcess]:
        nonlocal entered
        entered = True
        assert args == tuple(supervisor._launcher_argv())
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        yield proc

    monkeypatch.setattr(platform_process, "managed_subprocess", fake_managed)
    monkeypatch.setattr(supervisor, "_spawn_launcher", _REAL_SPAWN)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    await supervisor.ensure_started("sekret")
    await supervisor._shutdown()
    assert entered is True
    assert proc.returncode is None


def test_jiuwenbox_appears_only_inside_supervisor_functions() -> None:
    allowed = (_SRC / "service" / "sandbox" / "supervisor.py").resolve()
    assert _boundary_violations(_SRC, allowed_file=allowed) == []


def test_import_boundary_scanner_rejects_a_planted_snippet(tmp_path: Path) -> None:
    allowed = tmp_path / "service" / "sandbox" / "supervisor.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text(
        "import jiuwenbox\nLISTENER = 'http://jiuwenbox'\ndef nested() -> str:\n    return 'jiuwenswarm'\n",
        encoding="utf-8",
    )
    violations = _boundary_violations(tmp_path, allowed_file=allowed)
    assert any(item.endswith("jiuwenbox-import:in_function=False") for item in violations)
    assert any(item.endswith("jiuwenbox-string:in_function=False") for item in violations)
    assert any(":jiuwenswarm:" in item for item in violations)


def test_sandbox_package_import_does_not_load_jiuwenbox_fastapi_or_bwrap() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_DELTA_PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_jiuwenbox_is_an_exact_pin_and_other_pins_stay_exact() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    parsed = dict(_dependency_name_and_specifier(item) for item in project["project"]["dependencies"])
    assert parsed["jiuwenbox"] == "==0.1.0"
    loosened = sorted(
        name for name, specifier in parsed.items() if name not in _RANGE_PINS and not _is_exact_pin(specifier)
    )
    assert loosened == []
    unbounded = sorted(name for name, specifier in parsed.items() if name in _RANGE_PINS and "<" not in specifier)
    assert unbounded == []

    lock = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    boxed = [package for package in lock["package"] if package["name"] == "jiuwenbox"]
    assert len(boxed) == 1
    assert boxed[0]["version"] == "0.1.0"
    assert boxed[0]["source"] == {"editable": "third_party/jiuwenbox"}
    requires = next(package for package in lock["package"] if package["name"] == "icode-tui")
    by_name = {item["name"]: item for item in requires["metadata"]["requires-dist"]}
    for name, specifier in parsed.items():
        recorded = by_name[name]
        if name == "jiuwenbox":
            recorded_spec = recorded.get("specifier")
            assert recorded_spec is None or _is_exact_pin(recorded_spec)
            assert recorded.get("editable") == "third_party/jiuwenbox"
            continue
        assert recorded["specifier"] == specifier


def test_pytest_does_not_collect_third_party_jiuwenbox() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    options = project["tool"]["pytest"]["ini_options"]
    normalized = [str(path).replace("\\", "/").strip("/") for path in options["testpaths"]]
    assert set(normalized) <= {"src", "tests"}
    assert "tests" in normalized
    assert all("third_party" not in path for path in normalized)
    assert isinstance(options["norecursedirs"], list)


def test_engine_import_is_inside_a_function_and_import_does_not_load_it():
    mentions = _engine_mentions()
    assert mentions["top_level_imports"] == []
    assert mentions["module_strings"] == []
    nested = mentions["nested_imports"]
    assert isinstance(nested, list) and nested
    assert all(name == "jiuwenbox" for _depth, _line, name in nested)
    assert mentions["other_files"] == []
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


_IMPORT_PROBE = """
import sys
import chrys.service.sandbox
import chrys.service.sandbox.types
import chrys.service.sandbox.client
import chrys.service.sandbox.backend
import chrys.service.sandbox.supervisor
banned = [
    name
    for name in sys.modules
    if name == "jiuwenbox"
    or name.startswith("jiuwenbox.")
    or name == "fastapi"
    or name.startswith("fastapi.")
]
if banned:
    sys.stderr.write("\\n".join(sorted(banned)))
    raise SystemExit(1)
"""


def _engine_mentions() -> dict[str, object]:
    supervisor_path = _SRC / "service" / "sandbox" / "supervisor.py"
    tree = ast.parse(supervisor_path.read_text(encoding="utf-8"))
    imports: list[tuple[int, int, str]] = []
    module_strings: list[int] = []
    _walk(tree, 0, imports, module_strings)
    other: list[tuple[str, list[str]]] = []
    for path in _SRC.rglob("*.py"):
        if path.resolve() == supervisor_path.resolve():
            continue
        text = path.read_text(encoding="utf-8").lower()
        problems: list[str] = []
        if "jiuwenbox" in text or "jiuwenswarm" in text:
            problems.append(path.relative_to(_SRC).as_posix())
        other.append((path.as_posix(), problems))
    return {
        "top_level_imports": [item for item in imports if item[0] == 0],
        "nested_imports": [item for item in imports if item[0] > 0],
        "module_strings": module_strings,
        "other_files": [(path, problems) for path, problems in other if problems],
    }


def _walk(
    node: ast.AST,
    depth: int,
    imports: list[tuple[int, int, str]],
    module_strings: list[int],
) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "jiuwenbox" or alias.name.startswith("jiuwenbox."):
                imports.append((depth, node.lineno, alias.name))
    elif isinstance(node, ast.ImportFrom) and node.module:
        if node.module == "jiuwenbox" or node.module.startswith("jiuwenbox."):
            imports.append((depth, node.lineno, node.module))
    elif (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "jiuwenbox" in node.value.lower()
        and depth == 0
    ):
        module_strings.append(node.lineno)
    child_depth = (
        depth + 1 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) else depth
    )
    for child in ast.iter_child_nodes(node):
        _walk(child, child_depth, imports, module_strings)


_IMPORT_DELTA_PROBE = """
import sys
before = set(sys.modules)
import chrys.service.sandbox
import chrys.service.sandbox.types
import chrys.service.sandbox.client
import chrys.service.sandbox.backend
import chrys.service.sandbox.supervisor
added = set(sys.modules) - before
banned = [
    name
    for name in added
    if name == "jiuwenbox"
    or name.startswith("jiuwenbox.")
    or name == "fastapi"
    or name.startswith("fastapi.")
    or "bwrap" in name
]
if banned:
    sys.stderr.write("\\n".join(sorted(banned)))
    raise SystemExit(1)
"""


@dataclass(frozen=True)
class _BoundaryHit:
    path: Path
    line: int
    kind: str
    in_function: bool


def _boundary_violations(root: Path, *, allowed_file: Path) -> list[str]:
    """jiuwenbox strings and imports may live only inside functions of ``allowed_file``."""
    allowed = allowed_file.resolve()
    root_resolved = root.resolve()
    violations: list[str] = []
    for hit in _scan_boundary(root_resolved):
        if hit.kind == "jiuwenswarm" or hit.path.resolve() != allowed or not hit.in_function:
            relative = hit.path.resolve().relative_to(root_resolved).as_posix()
            violations.append(f"{relative}:{hit.line}:{hit.kind}:in_function={hit.in_function}")
    return violations


def _scan_boundary(root: Path) -> list[_BoundaryHit]:
    hits: list[_BoundaryHit] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        if b"jiuwenswarm" in data.lower():
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "jiuwenswarm" in line.lower():
                    hits.append(_BoundaryHit(path, lineno, "jiuwenswarm", False))
        if path.suffix != ".py":
            continue
        _walk_boundary(ast.parse(data.decode("utf-8")), path, False, hits)
    return hits


def _walk_boundary(node: ast.AST, path: Path, in_function: bool, hits: list[_BoundaryHit]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "jiuwenbox" or alias.name.startswith("jiuwenbox."):
                hits.append(_BoundaryHit(path, node.lineno, "jiuwenbox-import", in_function))
    elif isinstance(node, ast.ImportFrom) and node.module is not None:
        if node.module == "jiuwenbox" or node.module.startswith("jiuwenbox."):
            hits.append(_BoundaryHit(path, node.lineno, "jiuwenbox-import", in_function))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "jiuwenbox" in node.value.lower():
        hits.append(_BoundaryHit(path, node.lineno, "jiuwenbox-string", in_function))
    child_in_function = in_function or isinstance(node, _FUNCTION_NODES)
    for child in ast.iter_child_nodes(node):
        _walk_boundary(child, path, child_in_function, hits)


def _dependency_name_and_specifier(requirement: str) -> tuple[str, str]:
    body = requirement.split(";", 1)[0].strip()
    if "[" in body:
        name, rest = body.split("[", 1)
        _extras, specifier = rest.split("]", 1)
        return name.strip(), specifier.strip()
    for operator in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
        index = body.find(operator)
        if index != -1:
            return body[:index].strip(), body[index:].strip()
    return body.strip(), ""


def _is_exact_pin(specifier: str) -> bool:
    return specifier.startswith("==") and not specifier.startswith("===") and "," not in specifier
