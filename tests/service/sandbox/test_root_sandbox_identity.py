# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bundled sandbox identity and host runtime mounts.

Root with user namespaces off must fail inside the bwrap argv builder.
The mount and environment checks read that same argv (``to_args``), not a
filtered dict. Nothing starts bubblewrap. The bundled policy also hides
account homes, requires Landlock, and sets cgroup limits.
"""

from __future__ import annotations

import os
import shutil
import sys
import types
from pathlib import Path

import pytest

_NAMED_UID = 4242
_NAMED_GID = 4243
_ROOT_REFUSAL = "Sandbox cannot start as root while user namespaces are off."
_PRIVATE_TMPFS = ("/run", "/tmp", "/var/run")
_HOST_SESSION_ENV = ("TMUX", "DBUS_SESSION_BUS_ADDRESS")
_LISTEN_URL = "http://127.0.0.1:9"
_LISTEN_ENV = "JIUWENBOX_LISTEN"
_NO_CONTROL_SOCKET_PARENT = (
    "No host directory covered by the sandbox private tmpfs is available for the control socket."
)
_MOUNT_ARITY = {
    "--ro-bind": 2,
    "--bind": 2,
    "--dev-bind": 2,
    "--tmpfs": 1,
    "--dir": 1,
    "--remount-ro": 1,
    "--proc": 1,
    "--dev": 1,
    "--symlink": 2,
}


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows.

    ``pwd`` and ``grp`` are imported at module scope. The stubs only exist
    so those imports succeed; account lookup is patched per test.
    """
    import os

    if not hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    def _missing(*_args: object) -> None:
        raise KeyError(_args)

    def _install(name: str) -> None:
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ModuleNotFoundError:
            module = types.ModuleType(name)
            if name == "pwd":
                module.getpwuid = _missing  # type: ignore[attr-defined]
                module.getpwnam = _missing  # type: ignore[attr-defined]
            else:
                module.getgrnam = _missing  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, module)

    _install("pwd")
    _install("grp")


def _policy(*, user_namespace: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        namespace=types.SimpleNamespace(user=user_namespace),
        process=types.SimpleNamespace(run_as_user="sandbox", run_as_group="sandbox"),
    )


def _stub_named_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.server.runtime import process as process_module

    account = types.SimpleNamespace(pw_uid=_NAMED_UID)
    group = types.SimpleNamespace(gr_gid=_NAMED_GID)
    monkeypatch.setattr(process_module.pwd, "getpwnam", lambda _name: account)
    monkeypatch.setattr(process_module.grp, "getgrnam", lambda _name: group)


def _bundled_policy_path() -> Path:
    import jiuwenbox

    return Path(jiuwenbox.__file__).resolve().parent / "configs" / "code-agent-policy.yaml"


def _load_bundled_policy() -> object:
    from jiuwenbox.server.runtime.process import ProcessRuntime

    return ProcessRuntime._load_policy(_bundled_policy_path())


def _patch_ids(monkeypatch: pytest.MonkeyPatch, uid: int, gid: int) -> None:
    import os

    monkeypatch.setattr(os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(os, "getegid", lambda: gid, raising=False)


def _bwrap_argv(
    policy: object,
    *,
    sandbox_env: dict[str, str] | None = None,
) -> list[str]:
    """Return the argv ``_build_sandbox_bwrap_args`` builds via ``to_args``."""
    from jiuwenbox.server.runtime.process import ProcessRuntime

    runtime = ProcessRuntime()
    return runtime._build_sandbox_bwrap_args(
        "sandbox-id",
        policy,  # type: ignore[arg-type]
        ["/bin/true"],
        workdir=None,
        sandbox_env=sandbox_env,
        netns_attached=False,
        seccomp_fd=None,
    )


def _mount_events(argv: list[str]) -> list[tuple[str, ...]]:
    events: list[tuple[str, ...]] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        arity = _MOUNT_ARITY.get(flag)
        if arity is None or index + arity >= len(argv):
            index += 1
            continue
        events.append((flag, *argv[index + 1 : index + 1 + arity]))
        index += 1 + arity
    return events


def _covers(dest: str, path: str) -> bool:
    normalized = dest.rstrip("/") or "/"
    if normalized == "/":
        return True
    if normalized == path:
        return True
    return path.startswith(normalized + "/")


def _effective_mount(argv: list[str], path: str) -> tuple[str, ...] | None:
    chosen: tuple[str, ...] | None = None
    for event in _mount_events(argv):
        if _covers(event[-1], path):
            chosen = event
    return chosen


def _explicit_env(argv: list[str]) -> tuple[dict[str, str], set[str]]:
    values: dict[str, str] = {}
    unset: set[str] = set()
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--clearenv":
            values.clear()
            unset.clear()
            index += 1
            continue
        if flag == "--setenv" and index + 2 < len(argv):
            key = argv[index + 1]
            values[key] = argv[index + 2]
            unset.discard(key)
            index += 3
            continue
        if flag == "--unsetenv" and index + 1 < len(argv):
            key = argv[index + 1]
            values.pop(key, None)
            unset.add(key)
            index += 2
            continue
        index += 1
    return values, unset


def _argv_keeps_host_root(argv: list[str]) -> bool:
    """Whether a returned argv would still run as host uid 0."""
    if "--unshare-user" in argv:
        for index, flag in enumerate(argv):
            if flag == "--uid" and index + 1 < len(argv):
                return argv[index + 1] == "0"
        return False
    command = " ".join(argv)
    if "os.setuid(0)" in command:
        return True
    return "os.setuid(" not in command


def test_bundled_policy_excludes_account_homes_and_applies_limits(
    jiuwenbox_importable: None,
) -> None:
    """Account homes stay unmounted, Landlock is required, and cgroups apply.

    ``memory_max`` is parsed to an int byte count (4GiB is 4294967296).
    ``pids_max`` stays an int. ``cpu_max`` stays unset, so only the two
    configured limits are applied. ``namespace.user`` stays off and the
    ``/`` bind itself stays, skipping ``home`` and ``root`` with ``run``
    and ``tmp``.
    """
    policy = _load_bundled_policy()
    assert policy.namespace.user is False
    assert policy.network.mode == "host"
    assert "ptrace" in policy.syscall.x86_64.blocked
    assert "userfaultfd" in policy.syscall.x86_64.blocked
    assert "bpf" in policy.syscall.x86_64.blocked
    assert "mount" in policy.syscall.arm64.blocked
    assert "kexec_load" in policy.syscall.arm64.blocked
    assert policy.landlock.compatibility == "hard_requirement"
    assert policy.cgroup.memory_max == 4294967296
    assert isinstance(policy.cgroup.memory_max, int)
    assert policy.cgroup.pids_max == 1024
    assert policy.cgroup.cpu_max is None
    assert policy.cgroup.is_empty() is False

    entries = policy.filesystem_policy.bind_root_entries
    assert len(entries) == 1
    root = entries[0]
    assert Path(root.host_root) == Path("/")
    assert Path(root.sandbox_path) == Path("/")
    assert root.mode == "ro"
    assert root.exclude == ["run", "tmp", "home", "root"]


def test_root_without_user_namespace_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    """euid 0 and the bundled policy: the bwrap builder refuses.

    ``_build_sandbox_bwrap_args`` returns ``BwrapConfig.to_args()``. This
    test calls that builder. A uid helper that still returns 0 does not
    make it pass, and an argv that runs as uid 0 does not either.
    """
    policy = _load_bundled_policy()
    assert policy.namespace.user is False
    _patch_ids(monkeypatch, 0, 0)

    try:
        argv = _bwrap_argv(policy)
    except RuntimeError as exc:
        message = str(exc)
    else:
        pytest.fail(
            f"builder returned bwrap argv instead of refusing; would run as host uid 0: {_argv_keeps_host_root(argv)}"
        )

    assert message == _ROOT_REFUSAL
    assert "\n" not in message
    assert "://" not in message
    assert len(message) <= 200
    assert all(ord(char) >= 32 for char in message)


def test_bundled_bwrap_argv_hides_host_runtime_dirs_and_session_env(
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    """Private tmpfs for /run, /tmp, and /var/run, and no host session bus.

    The management listen URL is not a sandbox environment variable. A
    create-env value must be absent from ``--setenv`` and present in
    ``--unsetenv``.
    """
    policy = _load_bundled_policy()
    assert policy.namespace.user is False
    _patch_ids(monkeypatch, 1000, 1000)
    argv = _bwrap_argv(
        policy,
        sandbox_env={
            "TMUX": "/tmp/tmux-0/default",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
            _LISTEN_ENV: _LISTEN_URL,
            "SANDBOX_TEST_KEEP": "1",
        },
    )

    # ``/run`` and ``/tmp`` must exist before ``--remount-ro /``. bubblewrap
    # mkdir's a later ``--tmpfs`` target, and that mkdir fails once ``/`` is
    # read-only. ``/tmp`` is created by its earlier tmpfs; ``/run`` by ``--dir``.
    remount_root = next(index for index, flag in enumerate(argv) if flag == "--remount-ro" and argv[index + 1] == "/")

    def _has(flag: str, path: str, *, before: int | None = None, after: int | None = None) -> bool:
        return any(
            argv[index] == flag
            and argv[index + 1] == path
            and (before is None or index < before)
            and (after is None or index > after)
            for index in range(len(argv) - 1)
        )

    assert _has("--dir", "/run", before=remount_root)
    assert _has("--tmpfs", "/run", after=remount_root)
    assert _has("--tmpfs", "/tmp", before=remount_root) or _has("--dir", "/tmp", before=remount_root)
    assert _has("--tmpfs", "/tmp", after=remount_root)
    for path in ("/run", "/tmp"):
        assert _effective_mount(argv, path) == ("--tmpfs", path), argv
    from jiuwenbox.server.runtime.process import _var_run_follows_private_run

    if _var_run_follows_private_run():
        assert not _has("--tmpfs", "/var/run")
    else:
        assert _effective_mount(argv, "/var/run") == ("--tmpfs", "/var/run"), argv

    values, unset = _explicit_env(argv)
    for key in _HOST_SESSION_ENV:
        assert key not in values
        assert key in unset
    assert _LISTEN_ENV not in values
    assert _LISTEN_ENV in unset
    assert values["SANDBOX_TEST_KEEP"] == "1"


def test_sandbox_process_env_strips_listen_url_from_the_copy(
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    """``JIUWENBOX_LISTEN`` is copied out of the server environment, then popped.

    The server process keeps the variable. An unrelated variable stays.
    """
    monkeypatch.setenv(_LISTEN_ENV, _LISTEN_URL)
    monkeypatch.setenv("SANDBOX_TEST_KEEP", "kept")
    from jiuwenbox.server.runtime.process import _sandbox_process_env

    sandbox_env = _sandbox_process_env()

    assert _LISTEN_ENV not in sandbox_env
    assert sandbox_env["SANDBOX_TEST_KEEP"] == "kept"
    assert os.environ[_LISTEN_ENV] == _LISTEN_URL


def test_build_child_env_strips_inherited_and_override_listen_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User commands never see the management listen URL.

    ``sandbox_daemon`` is stdlib-only and imports without the Windows pwd stub.
    """
    from jiuwenbox.supervisor.sandbox_daemon import build_child_env

    monkeypatch.setenv(_LISTEN_ENV, _LISTEN_URL)
    inherited = build_child_env(None)
    assert _LISTEN_ENV not in inherited

    overridden = build_child_env(
        {
            _LISTEN_ENV: "http://127.0.0.1:10",
            "SANDBOX_TEST_KEEP": "kept",
        }
    )
    assert _LISTEN_ENV not in overridden
    assert overridden["SANDBOX_TEST_KEEP"] == "kept"


def _use_home_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the control-socket home check use ``Path.home()``.

    The password database names the real account home and ignores ``HOME``.
    Raising ``KeyError`` is what the Windows import stub already does, so
    ``HOME`` / ``USERPROFILE`` select the home under test on every platform.
    """
    from jiuwenbox.server.runtime import process as process_module

    def _missing(*_args: object) -> None:
        raise KeyError(_args)

    monkeypatch.setattr(process_module.pwd, "getpwuid", _missing)


def _control_socket_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    inside = home / "runtime"
    outside = tmp_path / "run"
    inside.mkdir(parents=True)
    outside.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(inside))
    return home, inside, outside


def _path_is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def test_control_socket_dir_skips_home_and_uses_outside_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    """The first candidate is inside the home directory and must lose.

    The socket directory is created under the later outside parent. Nothing
    is bind-mounted; ``mkdtemp`` is the whole side effect.
    """
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.server.runtime.process import _control_socket_parent_candidates as real_candidates

    home, inside, outside = _control_socket_paths(tmp_path, monkeypatch)
    _use_home_env_fallback(monkeypatch)
    # The real order starts with ``XDG_RUNTIME_DIR``. Keep that inside path
    # first, then the outside directory, and do not consult host ``/run``.
    assert real_candidates()[0].resolve() == inside.resolve()

    from jiuwenbox.server.runtime import process as process_module

    # ``tmp_path`` is not always under the real ``/tmp``. Treat the outside
    # directory as the only tree the sandbox tmpfs covers.
    monkeypatch.setattr(process_module, "_CONTROL_SOCKET_HIDDEN_ROOTS", (outside,))
    monkeypatch.setattr(
        process_module,
        "_control_socket_parent_candidates",
        lambda: [inside, outside],
    )
    runtime = ProcessRuntime()
    control_dir = runtime._ensure_control_dir("sandbox-id")
    try:
        assert control_dir.name.startswith("sandbox-id-control-")
        assert _path_is_under(control_dir, outside)
        assert not _path_is_under(control_dir, home)
        assert runtime._control_dirs["sandbox-id"] == control_dir
    finally:
        shutil.rmtree(control_dir)


def test_control_socket_dir_refuses_when_every_candidate_is_inside_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    """No Windows temp fallback and no directory under the home directory."""
    from jiuwenbox.server.runtime import process as process_module
    from jiuwenbox.server.runtime.process import ProcessRuntime

    home, inside, _outside = _control_socket_paths(tmp_path, monkeypatch)
    _use_home_env_fallback(monkeypatch)
    monkeypatch.setattr(
        process_module,
        "_control_socket_parent_candidates",
        lambda: [inside],
    )
    before = {path.relative_to(home) for path in home.rglob("*")}
    runtime = ProcessRuntime()

    with pytest.raises(RuntimeError) as exc_info:
        runtime._ensure_control_dir("sandbox-id")

    assert str(exc_info.value) == _NO_CONTROL_SOCKET_PARENT
    assert {path.relative_to(home) for path in home.rglob("*")} == before
    assert runtime._control_dirs == {}
    assert list(tmp_path.rglob("*control*")) == []


def test_control_socket_dir_refuses_a_parent_the_sandbox_can_see(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    """A directory outside the home is still visible when it is not under the tmpfs.

    ``/var`` stays bind-mounted. ``XDG_RUNTIME_DIR`` pointing there must not
    become the control socket parent.
    """
    from jiuwenbox.server.runtime import process as process_module
    from jiuwenbox.server.runtime.process import ProcessRuntime

    home, _inside, outside = _control_socket_paths(tmp_path, monkeypatch)
    visible = tmp_path / "var"
    visible.mkdir()
    _use_home_env_fallback(monkeypatch)
    monkeypatch.setattr(process_module, "_CONTROL_SOCKET_HIDDEN_ROOTS", (outside,))
    monkeypatch.setattr(
        process_module,
        "_control_socket_parent_candidates",
        lambda: [visible],
    )
    runtime = ProcessRuntime()

    with pytest.raises(RuntimeError) as exc_info:
        runtime._ensure_control_dir("sandbox-id")

    assert str(exc_info.value) == _NO_CONTROL_SOCKET_PARENT
    assert runtime._control_dirs == {}
    assert list(visible.rglob("*control*")) == []
    assert not _path_is_under(visible, home)


def test_non_root_keeps_named_account_resolution(
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    import os

    from jiuwenbox.server.runtime.process import ProcessRuntime

    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    _stub_named_account(monkeypatch)
    policy = _policy(user_namespace=False)

    assert ProcessRuntime._resolve_process_uid(policy) == _NAMED_UID
    assert ProcessRuntime._resolve_backing_identity(policy) == (_NAMED_UID, _NAMED_GID)
