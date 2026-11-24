# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Environment planning: lexical metadata, interpreter choice, probe facts and the fingerprint."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import pytest

from chrys.foundation.platform import get_platform
from chrys.service.workflows import environment as environment_module
from chrys.service.workflows.environment import (
    ByoPlan,
    DefaultPlan,
    EnvironmentRequest,
    PreparedEnvironment,
    WorkflowEnvironmentError,
    WorkflowEnvironmentManager,
    metadata_block_line,
    parse_environment_request,
    plan_environment,
    satisfies_requires_python,
)
from chrys.service.workflows.interpreter import InterpreterProbe
from tests.support.workflow_workers import create_venv, require_py39

EMPTY = EnvironmentRequest(None, (), None)
DECLARED = b"""# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.31"]
#
# [tool.chrys]
# python = ".venv"
# ///
from chrys.workflows import WorkflowBuilder
"""


def _request(
    *, requires_python: str | None = None, dependencies: tuple[str, ...] = (), python: str | None = None
) -> EnvironmentRequest:
    return EnvironmentRequest(requires_python, dependencies, python)


def _fake_venv(root: Path) -> Path:
    """A directory shaped like a virtual environment on this platform, with a file where the interpreter goes."""
    root.mkdir()
    (root / "pyvenv.cfg").write_text("home = /nowhere\n")
    executable = root / "Scripts" / "python.exe" if get_platform().is_windows else root / "bin" / "python"
    executable.parent.mkdir()
    executable.write_bytes(b"")
    return executable


# --------------------------------------------------------------------------- parse


def test_a_file_without_a_metadata_block_is_an_empty_request() -> None:
    request = parse_environment_request(b"from chrys.workflows import WorkflowBuilder\n")
    assert request == EMPTY
    assert request == parse_environment_request(b"# /// other\n# x = 1\n# ///\n")


def test_the_script_block_is_read_lexically() -> None:
    request = parse_environment_request(DECLARED)
    assert request.requires_python == ">=3.9"
    assert request.dependencies == ("requests>=2.31",)
    assert request.python == ".venv"
    assert parse_environment_request(b"\xef\xbb\xbf" + DECLARED) == request  # a BOM does not hide the block
    assert parse_environment_request(DECLARED.replace(b"\n", b"\r\n")) == request  # nor do CRLF line endings


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"# /// script\n# requires-python = \n# ///\n", "not valid TOML"),
        (b"# /// script\n# dependencies = 'requests'\n# ///\n", "list of strings"),
        (b"# /// script\n# requires-python = 3\n# ///\n", "must be a string"),
        (b"# /// script\n# [tool.chrys]\n# python = 1\n# ///\n", "must be a string"),
        (b"# /// script\n# [[tool]]\n# [tool.chrys]\n# python = '.venv'\n# ///\n", r"`\[tool\]` must be a table"),
        (b"# /// script\n# [tool]\n# chrys = 1\n# ///\n", r"`\[tool.chrys\]` must be a table"),
        (b"# /// script\n# a = 1\n# ///\n\n# /// script\n# b = 2\n# ///\n", "more than one"),
    ],
)
def test_malformed_blocks_are_rejected_not_ignored(source: bytes, message: str) -> None:
    with pytest.raises(WorkflowEnvironmentError, match=message):
        parse_environment_request(source)


@pytest.mark.parametrize(
    ("source", "line", "column"),
    [
        (b"x = 1\n# /// script\n# requires-python = \n# ///\n", 3, 21),
        (b"x = 1\n# /// script\n# [tool.chrys]\n#\n# python = = 1\n# ///\n", 5, 12),
        (b"x = 1\r\n\r\n# /// script\r\n# requires-python = 3\r\n# ///\r\n", 3, None),
        (b"# /// script\n# a = 1\n# ///\n\n# /// script\n# b = 2\n# ///\n", 5, None),
    ],
)
def test_a_malformed_block_says_where_in_the_file(source: bytes, line: int, column: int | None) -> None:
    with pytest.raises(WorkflowEnvironmentError) as caught:
        parse_environment_request(source)
    assert (caught.value.line, caught.value.column) == (line, column)


def test_the_block_line_is_found_without_parsing_the_block() -> None:
    assert metadata_block_line(b"x = 1\r\n# /// script\r\n# not toml = = \r\n# ///\r\n") == 2
    assert metadata_block_line(b"# /// other\n# x = 1\n# ///\n") is None


# --------------------------------------------------------------------------- plan


def test_no_declaration_runs_on_the_default_interpreter(tmp_path: Path) -> None:
    plan = plan_environment(EMPTY, entry_path=tmp_path / "wf.py", default_interpreter="/opt/py")
    assert plan == DefaultPlan(EMPTY, "/opt/py")


def test_dependencies_without_a_declared_interpreter_are_refused_up_front(tmp_path: Path) -> None:
    with pytest.raises(WorkflowEnvironmentError, match=r"\[tool.chrys\] python"):
        plan_environment(_request(dependencies=("requests",)), entry_path=tmp_path / "wf.py")


def test_a_relative_venv_is_taken_from_the_workflow_files_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    executable = _fake_venv(workdir / ".venv")
    monkeypatch.chdir(tmp_path)  # the current directory must play no part
    plan = plan_environment(_request(python=".venv"), entry_path=workdir / "wf.py")
    assert plan == ByoPlan(_request(python=".venv"), str(executable))


def test_an_absolute_declaration_is_used_as_is(tmp_path: Path) -> None:
    executable = _fake_venv(tmp_path / "elsewhere")
    plan = plan_environment(_request(python=str(tmp_path / "elsewhere")), entry_path=tmp_path / "a" / "wf.py")
    assert isinstance(plan, ByoPlan)
    assert plan.interpreter == str(executable)


def test_a_declared_executable_is_accepted_directly(tmp_path: Path) -> None:
    plan = plan_environment(_request(python=sys.executable), entry_path=tmp_path / "wf.py")
    assert plan == ByoPlan(_request(python=sys.executable), sys.executable)


def test_a_directory_without_pyvenv_cfg_is_not_a_venv(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(WorkflowEnvironmentError, match=r"no pyvenv\.cfg"):
        plan_environment(_request(python="plain"), entry_path=tmp_path / "wf.py")


def test_a_venv_without_an_interpreter_and_a_missing_path_are_named(tmp_path: Path) -> None:
    (tmp_path / "hollow").mkdir()
    (tmp_path / "hollow" / "pyvenv.cfg").write_text("")
    with pytest.raises(WorkflowEnvironmentError, match="has no interpreter"):
        plan_environment(_request(python="hollow"), entry_path=tmp_path / "wf.py")
    with pytest.raises(WorkflowEnvironmentError, match="does not exist"):
        plan_environment(_request(python="missing"), entry_path=tmp_path / "wf.py")


# --------------------------------------------------------------------------- requires-python


@pytest.mark.parametrize(
    ("version", "specifier", "expected"),
    [
        ((3, 14, 0), ">=3.9", True),
        ((3, 14, 0), "<3.12", False),
        ((3, 9, 0), ">=3.9, <3.12", True),
        ((3, 12, 0), ">=3.9,<3.12", False),
        ((3, 9, 0), "==3.9", True),
        ((3, 9, 7), "==3.9", False),
        ((3, 9, 7), "==3.9.*", True),
        ((3, 10, 0), "!=3.10.*", False),
        ((3, 10, 0), "~=3.9", True),
        ((4, 0, 0), "~=3.9", False),
        ((3, 9, 5), "~=3.9.1", True),
        ((3, 10, 0), "~=3.9.1", False),
        ((3, 9, 0), "", True),
        ((3, 15), "==3.15.0", True),  # a pre-release's release segment: judged as its final release
        ((3, 15), "==3.15.0.*", True),  # ...by the wildcard too, which pads the same way
        ((3, 15), "!=3.15.0.*", False),
        ((3, 15), "~=3.15.0.0", True),
        ((3, 14, 0), "==3.14.0.0.*", True),
    ],
)
def test_requires_python_clauses(version: tuple[int, ...], specifier: str, expected: bool) -> None:
    assert satisfies_requires_python(version, specifier) is expected


@pytest.mark.parametrize("specifier", ["3.9", "===3.9", ">=3.9.*", "~=3", ">= three"])
def test_unsupported_requires_python_clauses_are_errors(specifier: str) -> None:
    with pytest.raises(WorkflowEnvironmentError, match="Unsupported"):
        satisfies_requires_python((3, 9, 0), specifier)


def test_an_unsupported_clause_is_reported_even_after_a_failing_one() -> None:
    """The declaration is at fault, not the interpreter: no clause is judged before every clause is understood."""
    with pytest.raises(WorkflowEnvironmentError, match="Unsupported"):
        satisfies_requires_python((3, 14, 0), "<3.12, >=3.9.*")


# --------------------------------------------------------------------------- prepare


async def test_prepare_probes_the_default_interpreter_and_fingerprints_it() -> None:
    manager = WorkflowEnvironmentManager(sdk_digest="d" * 64)
    prepared = await manager.prepare(DefaultPlan(EMPTY, sys.executable))
    assert prepared.mode == "default"
    assert prepared.executable == sys.executable
    assert prepared.python_version == platform.python_version()
    assert prepared.implementation == platform.python_implementation()
    assert (prepared.platform, prepared.machine, prepared.libc) == (
        sys.platform,
        platform.machine(),
        platform.libc_ver()[0],
    )
    assert prepared.sdk_digest == "d" * 64
    assert len(prepared.environment_fingerprint) == 64
    assert await manager.prepare(DefaultPlan(EMPTY, sys.executable)) == prepared


async def test_a_surrogateescaped_interpreter_path_still_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    """A surrogateescaped venv path is identity like any other: it fingerprints instead of raising."""
    executable = os.path.abspath("/venv-\udcff/bin/python")

    async def probed(candidate: str, *, timeout: float = 0.0) -> InterpreterProbe:
        assert candidate == executable
        return InterpreterProbe(candidate, "3.9.0", "CPython", "linux", "x86_64", "glibc")

    monkeypatch.setattr(environment_module, "probe_interpreter", probed)
    prepared = await WorkflowEnvironmentManager(sdk_digest="d").prepare(DefaultPlan(EMPTY, executable))
    assert prepared.executable == executable
    assert len(prepared.environment_fingerprint) == 64


@pytest.mark.parametrize(("requires", "prepared"), [(">=3.9.1", True), ("<3.9.1", False), ("!=3.9.1", False)])
async def test_a_pre_release_is_judged_as_its_final_release_from_the_probe_on(
    monkeypatch: pytest.MonkeyPatch, requires: str, prepared: bool
) -> None:
    """The probe's ``3.9.1rc1`` reaches the comparison as 3.9.1, not as 3.9.0."""

    async def probed(candidate: str, *, timeout: float = 0.0) -> InterpreterProbe:
        return InterpreterProbe(candidate, "3.9.1rc1", "CPython", "linux", "x86_64", "glibc")

    monkeypatch.setattr(environment_module, "probe_interpreter", probed)
    plan = DefaultPlan(_request(requires_python=requires), sys.executable)
    manager = WorkflowEnvironmentManager(sdk_digest="d")
    if prepared:
        assert (await manager.prepare(plan)).python_version == "3.9.1rc1"
    else:
        with pytest.raises(WorkflowEnvironmentError, match="requires"):
            await manager.prepare(plan)


async def test_the_sdk_build_is_part_of_the_fingerprint() -> None:
    first = await WorkflowEnvironmentManager(sdk_digest="a").prepare(DefaultPlan(EMPTY, sys.executable))
    second = await WorkflowEnvironmentManager(sdk_digest="b").prepare(DefaultPlan(EMPTY, sys.executable))
    assert first.environment_fingerprint != second.environment_fingerprint
    assert (first.executable, first.python_version) == (second.executable, second.python_version)


@pytest.mark.parametrize("mode", ["default", "byo"])
async def test_a_requires_python_mismatch_is_refused_before_any_worker_starts(mode: str) -> None:
    request = _request(requires_python="<3.9")
    plan = DefaultPlan(request, sys.executable) if mode == "default" else ByoPlan(request, sys.executable)
    with pytest.raises(WorkflowEnvironmentError, match=f"The {mode} interpreter .* requires '<3.9'"):
        await WorkflowEnvironmentManager(sdk_digest="d").prepare(plan)


async def test_an_unusable_interpreter_is_an_environment_error(tmp_path: Path) -> None:
    with pytest.raises(WorkflowEnvironmentError, match="Cannot start"):
        await WorkflowEnvironmentManager(sdk_digest="d").prepare(ByoPlan(EMPTY, str(tmp_path / "python")))


@pytest.mark.skipif(sys.platform == "win32", reason="a bare executable name next to the file needs a symlink")
async def test_a_bare_executable_name_is_probed_where_it_points_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative plan names the file next to the workflow; the probe must run that file, not PATH's python."""
    (tmp_path / "python").symlink_to(require_py39())
    monkeypatch.chdir(tmp_path)
    plan = plan_environment(_request(python="./python"), entry_path=Path("wf.py"))
    prepared = await WorkflowEnvironmentManager(sdk_digest="d").prepare(plan)
    assert prepared.executable == str(tmp_path / "python")
    assert prepared.python_version.startswith("3.9.")


@pytest.mark.skipif(sys.platform == "win32", reason="a directory symlink needs privileges on Windows")
async def test_a_parent_step_through_a_symlinked_directory_keeps_the_venv_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`../.venv` beside a symlinked workflow directory is the venv beside the physical one, as the OS reads it."""
    (tmp_path / "tools" / "workflows").mkdir(parents=True)
    wanted = _fake_venv(tmp_path / "tools" / ".venv")
    _fake_venv(tmp_path / ".venv")  # the decoy a lexical `..` would pick
    (tmp_path / "workflows").symlink_to(tmp_path / "tools" / "workflows", target_is_directory=True)
    probed: list[str] = []

    async def probe(candidate: str, *, timeout: float = 0.0) -> InterpreterProbe:
        probed.append(candidate)
        return InterpreterProbe(candidate, "3.9.0", "CPython", "linux", "x86_64", "glibc")

    monkeypatch.setattr(environment_module, "probe_interpreter", probe)
    plan = plan_environment(_request(python="../.venv"), entry_path=tmp_path / "workflows" / "wf.py")
    prepared = await WorkflowEnvironmentManager(sdk_digest="d").prepare(plan)
    assert probed == [str(wanted)]
    assert prepared.executable == str(wanted)


@pytest.fixture
def real_venv(tmp_path: Path) -> Path:
    return create_venv(tmp_path / ".venv")


async def test_a_real_venv_is_prepared_end_to_end(tmp_path: Path, real_venv: Path) -> None:
    request = parse_environment_request(b"# /// script\n# [tool.chrys]\n# python = '.venv'\n# ///\n")
    plan = plan_environment(request, entry_path=tmp_path / "wf.py")
    prepared = await WorkflowEnvironmentManager(sdk_digest="d").prepare(plan)
    assert isinstance(plan, ByoPlan)
    assert prepared.mode == "byo"
    assert Path(prepared.executable).parent.parent == real_venv
    assert prepared.python_version == platform.python_version()


def test_prepared_environment_is_plain_frozen_data() -> None:
    prepared = PreparedEnvironment("default", "/py", "CPython", "3.14.0", "linux", "x86_64", "glibc", "d", "f")
    with pytest.raises(AttributeError):
        prepared.mode = "byo"  # type: ignore[misc]
