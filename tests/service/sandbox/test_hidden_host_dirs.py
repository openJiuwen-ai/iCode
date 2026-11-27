# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hidden host directories get an empty read-only tmpfs wherever a bind shows them.

The argv checks build ``BwrapConfig`` and read ``to_args``. The last test runs
the argv under a real bubblewrap when this host can start one.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.symlinks import symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the sandbox engine runs on POSIX hosts")


def _masks(argv: list[str]) -> list[str]:
    """Paths that get ``--tmpfs P --remount-ro P``, in argv order."""
    found: list[str] = []
    for index, flag in enumerate(argv[:-3]):
        if flag == "--tmpfs" and argv[index + 2] == "--remount-ro" and argv[index + 1] == argv[index + 3]:
            found.append(argv[index + 1])
    return found


def _last_bind_index(argv: list[str]) -> int:
    return max(index for index, flag in enumerate(argv) if flag in {"--bind", "--ro-bind"})


def _home(tmp_path: Path) -> tuple[str, str]:
    """A real account home and the ``.chrys`` directory inside it."""
    home = tmp_path / "home" / "user"
    (home / ".chrys").mkdir(parents=True)
    (home / ".chrys" / "settings.yaml").write_text("token: secret\n", encoding="utf-8")
    real_home = os.path.realpath(home)
    return real_home, os.path.join(real_home, ".chrys")


def test_a_writable_home_bind_hides_the_directory_after_every_other_mount(tmp_path: Path) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)
    config = BwrapConfig(
        command=["true"],
        rw_binds=[(home, home)],
        ro_binds=[(os.path.join(home, ".chrys"), os.path.join(home, ".chrys"))],
        extra_args=["--tmpfs", "/run"],
        hidden_host_dirs=[chrys],
    )
    argv = config.to_args()
    assert _masks(argv) == [chrys]
    mask = next(index for index in range(len(argv) - 1) if argv[index : index + 2] == ["--tmpfs", chrys])
    assert mask > _last_bind_index(argv)
    assert mask > argv.index("/run")


def test_a_writable_root_bind_that_drops_the_read_only_cover_still_hides_it(tmp_path: Path) -> None:
    """A root bind drops same-path covers; the tmpfs is computed from what is emitted."""
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    _home_path, chrys = _home(tmp_path)
    config = BwrapConfig(
        command=["true"],
        rw_binds=[("/", "/")],
        ro_binds=[(chrys, chrys)],
        hidden_host_dirs=[chrys],
    )
    argv = config.to_args()
    assert ("--ro-bind", chrys, chrys) not in zip(argv, argv[1:], argv[2:], strict=False)
    assert _masks(argv) == [chrys]


def test_a_remapped_bind_hides_the_directory_at_its_sandbox_location(tmp_path: Path) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)
    config = BwrapConfig(command=["true"], ro_binds=[(home, "/mnt/user")], hidden_host_dirs=[chrys])
    assert _masks(config.to_args()) == ["/mnt/user/.chrys"]


def test_a_bind_reached_through_a_symlink_is_matched_by_its_real_source(tmp_path: Path) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)
    link = tmp_path / "link-to-home"
    symlink_or_skip(link, Path(home), target_is_directory=True)
    config = BwrapConfig(command=["true"], rw_binds=[(str(link), "/work")], hidden_host_dirs=[chrys])
    assert _masks(config.to_args()) == ["/work/.chrys"]


def test_a_deeper_bind_over_the_location_decides_what_is_there(tmp_path: Path) -> None:
    """The later bind shows other content, so no tmpfs, and none is created on its source."""
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    config = BwrapConfig(
        command=["true"],
        rw_binds=[(home, home)],
        ro_binds=[(str(other), os.path.join(home, ".chrys"))],
        hidden_host_dirs=[chrys],
    )
    assert _masks(config.to_args()) == []


def test_a_bind_dropped_for_an_explicit_writable_destination_hides_nothing(tmp_path: Path) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    config = BwrapConfig(
        command=["true"],
        rw_binds=[(str(work), "/w")],
        ro_binds=[(home, "/w")],
        enumerated_ro_binds=[(home, "/w")],
        hidden_host_dirs=[chrys],
    )
    assert _masks(config.to_args()) == []


def test_a_bind_from_inside_a_hidden_directory_stays_visible(tmp_path: Path) -> None:
    """The engine binds its scripts and backing directories from inside its own home."""
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    _home_path, chrys = _home(tmp_path)
    backing = Path(chrys) / "sandbox-engine" / "workspace"
    backing.mkdir(parents=True)
    config = BwrapConfig(command=["true"], rw_binds=[(str(backing), "/workspace")], hidden_host_dirs=[chrys])
    assert _masks(config.to_args()) == []


def test_a_hidden_directory_inside_another_gets_no_tmpfs_of_its_own(tmp_path: Path) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)
    inner = Path(chrys) / "sandbox-engine"
    inner.mkdir()
    config = BwrapConfig(
        command=["true"],
        rw_binds=[(home, home)],
        hidden_host_dirs=[str(inner), chrys],
    )
    assert _masks(config.to_args()) == [chrys]


def test_runtime_hides_its_own_home_and_the_policy_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.models.policy import BindMount, FilesystemPolicy, NamespacePolicy, SecurityPolicy
    from jiuwenbox.server.runtime import process as process_module
    from jiuwenbox.server.runtime.process import ProcessRuntime

    home, chrys = _home(tmp_path)
    engine_home = os.path.join(home, ".jiuwenbox")
    os.mkdir(engine_home)
    monkeypatch.setattr(process_module, "JIUWENBOX_HOME", Path(engine_home))
    policy = SecurityPolicy(
        namespace=NamespacePolicy(user=True),
        filesystem_policy=FilesystemPolicy(
            bind_mounts=[BindMount(host_path=home, sandbox_path=home, mode="rw")],
            hidden_host_paths=[chrys, str(tmp_path / "missing")],
        ),
    )
    argv = ProcessRuntime()._build_sandbox_bwrap_args(
        "box",
        policy,
        ["true"],
        workdir=None,
        sandbox_env=None,
        netns_attached=False,
        seccomp_fd=None,
    )
    assert sorted(_masks(argv)) == sorted([engine_home, chrys])


def test_runtime_refuses_a_hidden_path_it_cannot_cover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.models.policy import FilesystemPolicy, SecurityPolicy
    from jiuwenbox.server.runtime import process as process_module

    monkeypatch.setattr(process_module, "JIUWENBOX_HOME", tmp_path / "missing-home")
    secret = tmp_path / "token"
    secret.write_text("secret\n", encoding="utf-8")
    root_link = tmp_path / "root-link"
    symlink_or_skip(root_link, Path("/"), target_is_directory=True)
    for path in (secret, root_link):
        policy = SecurityPolicy(filesystem_policy=FilesystemPolicy(hidden_host_paths=[str(path)]))
        with pytest.raises(RuntimeError, match="cannot be covered"):
            process_module._hidden_host_dirs(policy)
    empty = SecurityPolicy(filesystem_policy=FilesystemPolicy(hidden_host_paths=[str(tmp_path / "gone")]))
    assert process_module._hidden_host_dirs(empty) == []


@pytest.mark.parametrize(("configured", "own"), [("absolute", True), ("relative/home", False), ("", False)])
def test_the_server_home_follows_an_absolute_environment_value(tmp_path: Path, configured: str, own: bool) -> None:
    home = str(tmp_path / "engine-home") if configured == "absolute" else configured
    env = {**os.environ, "HOME": str(tmp_path), "JIUWENBOX_HOME": home}
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jiuwenbox.server import workspace; print(workspace.JIUWENBOX_HOME); print(workspace.SANDBOX_WORKSPACE)",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=ENGINE_TURN_TIMEOUT,
        check=True,
    )
    resolved, workspace = probe.stdout.splitlines()
    if own:
        assert resolved == home
    else:
        assert resolved.endswith(os.sep + ".jiuwenbox")
    assert workspace == os.path.join(resolved, "workspace")


def _bwrap_runs(argv: list[str]) -> bool:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        return False
    try:
        probe = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=ENGINE_TURN_TIMEOUT,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0


def test_a_command_under_real_bubblewrap_cannot_read_or_write_the_hidden_directory(tmp_path: Path) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    home, chrys = _home(tmp_path)

    def _argv(command: list[str], hidden: list[str]) -> list[str]:
        return BwrapConfig(
            command=command,
            unshare_net=False,
            unshare_ipc=False,
            unshare_cgroup=False,
            unshare_uts=False,
            rw_binds=[("/", "/"), (home, home)],
            hidden_host_dirs=hidden,
        ).to_args()

    settings = os.path.join(chrys, "settings.yaml")
    if not _bwrap_runs(_argv(["test", "-f", settings], [])):
        pytest.skip("bubblewrap cannot start a sandbox on this host")
    listing = subprocess.run(
        _argv(["ls", "-A", chrys], [chrys]),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=ENGINE_TURN_TIMEOUT,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout == ""
    write = subprocess.run(
        _argv(["sh", "-c", f"echo x > {shlex.quote(os.path.join(chrys, 'planted'))}"], [chrys]),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=ENGINE_TURN_TIMEOUT,
        check=False,
    )
    assert write.returncode != 0
    assert sorted(os.listdir(chrys)) == ["settings.yaml"]
    assert Path(settings).read_text(encoding="utf-8") == "token: secret\n"
