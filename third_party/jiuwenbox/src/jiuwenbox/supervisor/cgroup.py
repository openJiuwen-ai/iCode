# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Per-sandbox cgroup resource limits.

This module abstracts the cgroup v2 / v1 differences behind a small set of
operations consumed by :mod:`jiuwenbox.server.runtime.process`:

- :func:`detect_backend` - probe the host once for the preferred backend.
- :func:`setup` - create a per-sandbox cgroup and write the limit files.
- :func:`attach` - place the bwrap host-side PID into the ``supervisor`` child.
- :func:`create_command_cgroup` - one ``cmd-<id>`` under ``supervisor/commands``.
- :func:`kill_cgroup_processes` - stop every pid in one cgroup directory.
- :func:`teardown` - kill stragglers, then rmdir the whole sandbox cgroup.

When neither cgroup v2 nor v1 is writable but :class:`CgroupPolicy` is
non-empty, :func:`setup` raises :class:`CgroupSetupError` so the runtime
can abort sandbox creation rather than silently dropping the limits.

The limit parent stays empty. The daemon joins ``supervisor``. Each command
joins ``supervisor/commands/cmd-<id>``, which is inside the daemon's cgroup
namespace. ``/sys`` is read-only inside the sandbox, so that ``commands``
directory is bind-mounted read-write at :data:`SANDBOX_COMMAND_CGROUP_ROOT`
and the command writes its own pid there before exec. The host still kills
and removes command cgroups through the host path.
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jiuwenbox.models.policy import CgroupPolicy

logger = logging.getLogger(__name__)


class CgroupSetupError(RuntimeError):
    """Raised when cgroup limits are requested but cannot be applied."""


class CgroupBackend(str, enum.Enum):
    V2 = "v2"
    V1 = "v1"


# Required controllers for the minimal v1/v2 surface we support.
_REQUIRED_CONTROLLERS: tuple[str, ...] = ("memory", "cpu", "pids")

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_JIUWENBOX_ROOT_NAME = "jiuwenbox"
# The limit-holding parent stays empty of processes.
# The daemon joins ``supervisor``. Commands join children of
# ``supervisor/commands``, which stays inside the cgroup namespace whose
# root is ``supervisor``. ``/sys`` is read-only in the sandbox, so this
# directory is bind-mounted at ``SANDBOX_COMMAND_CGROUP_ROOT``.
_SUPERVISOR_CHILD_NAME = "supervisor"
_COMMANDS_CHILD_NAME = "commands"
_COMMAND_CHILD_PREFIX = "cmd-"
SANDBOX_COMMAND_CGROUP_ROOT = "/run/jiuwenbox-cgroup"

# Maximum number of attempts when rmdir-ing a cgroup that still has
# transient processes attached.
_TEARDOWN_RETRIES = 5
_TEARDOWN_BACKOFF_SECONDS = 0.1


@dataclass
class CgroupHandle:
    """Bookkeeping handed back from :func:`setup`.

    ``paths`` are the limit-holding parents: one unified directory on v2,
    one directory per controller the policy actually set on v1. Those
    parents stay empty of processes. ``supervisor_paths`` are the children
    the daemon joins. ``command_parent_paths`` are ``supervisor/commands``
    directories. Command children are created later and are not stored here.
    """

    backend: CgroupBackend
    sandbox_id: str
    paths: list[Path] = field(default_factory=list)
    supervisor_paths: list[Path] = field(default_factory=list)
    command_parent_paths: list[Path] = field(default_factory=list)


@dataclass
class CommandCgroup:
    """One command's cgroup directories, siblings of ``supervisor``.

    v2 has one directory. v1 has one directory per controller the sandbox
    cgroup uses. Releasing this object does not touch the supervisor child
    or any other command.
    """

    backend: CgroupBackend
    paths: list[Path]

    def procs_paths(self) -> list[str]:
        """Host ``cgroup.procs`` paths the command must join before exec."""
        return [str(path / "cgroup.procs") for path in self.paths]

    def sandbox_procs_paths(self) -> list[str]:
        """In-sandbox ``cgroup.procs`` paths corresponding to :meth:`procs_paths`.

        The host path sits on a read-only ``/sys`` bind. The command writes
        the sandbox path, which is the read-write bind of ``commands``.
        """
        if len(self.paths) == 1:
            return [f"{SANDBOX_COMMAND_CGROUP_ROOT}/{self.paths[0].name}/cgroup.procs"]
        return [
            f"{SANDBOX_COMMAND_CGROUP_ROOT}/{index}/{path.name}/cgroup.procs"
            for index, path in enumerate(self.paths)
        ]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _v2_controllers_enabled_at(path: Path) -> set[str]:
    try:
        text = (path / "cgroup.controllers").read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(text.split())


def _v2_available() -> bool:
    """Return True when cgroup v2 is mounted and our controllers are exposed.

    We require :data:`_CGROUP_ROOT` / cgroup.controllers to exist and to
    expose ``memory``, ``cpu`` and ``pids``. Writability is verified at
    setup time (creating the jiuwenbox subtree) rather than here, so the
    detection is cheap and side-effect-free.
    """
    controllers_file = _CGROUP_ROOT / "cgroup.controllers"
    if not controllers_file.is_file():
        return False
    available = _v2_controllers_enabled_at(_CGROUP_ROOT)
    return all(name in available for name in _REQUIRED_CONTROLLERS)


def _v1_available() -> bool:
    """Return True when cgroup v1 exposes a writable hierarchy for each of
    our required controllers under :data:`_CGROUP_ROOT`.
    """
    for controller in _REQUIRED_CONTROLLERS:
        ctrl_root = _CGROUP_ROOT / controller
        if not ctrl_root.is_dir():
            return False
        # cgroup v1 controller root is always writable by root; we don't
        # touch it here to avoid leaving leftover dirs in detection.
    return True


@lru_cache(maxsize=1)
def detect_backend() -> CgroupBackend | None:
    """Probe the host for a usable cgroup backend.

    Returns ``CgroupBackend.V2`` when cgroup v2 is mounted with our
    controllers exposed, falling back to ``CgroupBackend.V1`` when each
    required controller has its own hierarchy. Returns ``None`` if
    neither is present (the caller decides whether that's fatal).

    Cached because the cgroup hierarchy is fixed for the lifetime of the
    host kernel: every sandbox creation otherwise re-runs ``read_text``
    on ``/sys/fs/cgroup/cgroup.controllers`` (or ``is_dir`` for v1
    controllers) which is wasted work on the hot path.
    """
    if _v2_available():
        return CgroupBackend.V2
    if _v1_available():
        return CgroupBackend.V1
    return None


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _write_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` using ``open(..., "w")`` semantics.

    Cgroup interface files are sysfs-like and require a single write of the
    exact value (no newline appended by the kernel parser); using ``"w"``
    keeps the behaviour predictable across both v2 and v1.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _try_write_file(path: Path, content: str, *, what: str) -> None:
    try:
        _write_file(path, content)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to write {what} ({path}): {exc.strerror or exc}"
        ) from exc


def _mkdir_cgroup(parent: Path, name: str) -> Path:
    child = parent / name
    try:
        child.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {child}: {exc.strerror or exc}"
        ) from exc
    return child


def _enable_v2_subtree(group_dir: Path) -> None:
    """Let children use the controllers the parent is allowed to delegate.

    cgroup v2 forbids member processes in a cgroup that enables domain
    controllers in ``cgroup.subtree_control``. The parent stays empty.
    The daemon and each command live in children underneath it. An
    unprivileged process often cannot enable every controller; the ones
    that are refused are skipped so the directory can still be used for
    ``cgroup.kill``.
    """
    available = _v2_controllers_enabled_at(group_dir)
    wanted = [name for name in _REQUIRED_CONTROLLERS if name in available]
    if not wanted:
        return
    enable_line = " ".join(f"+{name}" for name in wanted)
    try:
        _write_file(group_dir / "cgroup.subtree_control", enable_line)
    except OSError as exc:
        logger.warning(
            "cgroup v2: controllers %s not enabled on %s: %s",
            enable_line,
            group_dir,
            exc.strerror or exc,
        )


def _write_limit(path: Path, content: str, *, what: str) -> None:
    """Write a limit file. A missing file or a refusal is not fatal.

    Delegated cgroups often expose ``cgroup.kill`` without ``memory.max``.
    The sandbox still starts; that one limit is absent.
    """
    if not path.exists():
        logger.warning("cgroup: %s is not available at %s", what, path)
        return
    try:
        _write_file(path, content)
    except OSError as exc:
        logger.warning(
            "cgroup: failed to write %s (%s): %s",
            what,
            path,
            exc.strerror or exc,
        )


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        logger.warning("cgroup: failed to remove %s", path)


def _process_cgroup_v2() -> Path | None:
    """Return this process's cgroup v2 directory, if the file names one."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or parts[0] != "0" or parts[1] != "":
            continue
        relative = parts[2].strip()
        if relative in {"", "/"}:
            return _CGROUP_ROOT
        return _CGROUP_ROOT / relative.lstrip("/")
    return None


def _ensure_jiuwenbox_root_v2() -> Path:
    """Create a ``jiuwenbox`` directory the current user can write.

    Root may enable controllers on the unified hierarchy. An unprivileged
    process cannot write ``/sys/fs/cgroup/cgroup.subtree_control`` and must
    use its own cgroup instead. The first directory that accepts the mkdir
    wins. Controller delegation is best-effort after that.
    """
    bases: list[Path] = []
    own = _process_cgroup_v2()
    if own is not None:
        bases.append(own)
    if _CGROUP_ROOT not in bases:
        bases.append(_CGROUP_ROOT)
    errors: list[str] = []
    for base in bases:
        if base == _CGROUP_ROOT and os.geteuid() == 0:
            _enable_v2_subtree(_CGROUP_ROOT)
        root = base / _JIUWENBOX_ROOT_NAME
        try:
            root.mkdir(parents=False, exist_ok=True)
        except OSError as exc:
            errors.append(f"{root}: {exc.strerror or exc}")
            continue
        _enable_v2_subtree(root)
        return root
    detail = "; ".join(errors) if errors else "no cgroup directory to try"
    raise CgroupSetupError(f"failed to create a writable cgroup directory: {detail}")


def _setup_v2(sandbox_id: str, policy: CgroupPolicy) -> CgroupHandle:
    parent = _ensure_jiuwenbox_root_v2()
    group_dir = parent / sandbox_id
    try:
        group_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise CgroupSetupError(
            f"cgroup directory {group_dir} already exists"
        ) from exc
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {group_dir}: {exc.strerror or exc}"
        ) from exc

    supervisor_dir: Path | None = None
    command_parent: Path | None = None
    try:
        if policy.memory_max is not None:
            _write_limit(
                group_dir / "memory.max",
                str(policy.memory_max),
                what="memory.max",
            )
            # Keep the OOM killer on a single victim. ``memory.oom.group=1``
            # would SIGKILL every process in the cgroup, including the
            # sandbox daemon, and the exec client would only see the IPC
            # socket close.
            oom_group = group_dir / "memory.oom.group"
            if oom_group.exists():
                try:
                    _write_file(oom_group, "0")
                except OSError as exc:
                    logger.warning(
                        "cgroup v2: failed to set memory.oom.group=0 for %s: %s",
                        sandbox_id,
                        exc,
                    )
            # Also clamp swap to zero so ``memory.max`` behaves as a true
            # hard cap. Without this, a memory-hungry process can simply
            # page out to swap once it hits ``memory.max`` and keep running
            # -- the kernel only invokes the OOM killer when reclaim cannot
            # free pages, and "evict to swap" counts as reclaim. WSL2 and
            # most desktop Linuxes ship with swap enabled, so this is the
            # common case rather than an edge case.
            #
            # The swap controller may be absent on hosts that booted with
            # ``swapaccount=0`` (or without ``CONFIG_MEMCG_SWAP``); missing
            # ``memory.swap.max`` is non-fatal because such hosts have no
            # swap to begin with and ``memory.max`` already behaves as the
            # hard cap users expect.
            swap_max_file = group_dir / "memory.swap.max"
            if swap_max_file.exists():
                try:
                    _write_file(swap_max_file, "0")
                except OSError as exc:
                    logger.warning(
                        "cgroup v2: failed to clamp memory.swap.max=0 for %s: %s",
                        sandbox_id,
                        exc,
                    )
        if policy.cpu_max is not None:
            quota, period = policy.cpu_max
            _write_limit(
                group_dir / "cpu.max",
                f"{quota} {period}",
                what="cpu.max",
            )
        if policy.pids_max is not None:
            _write_limit(
                group_dir / "pids.max",
                str(policy.pids_max),
                what="pids.max",
            )
        # Delegate after the limits are written, while the parent is still
        # empty, so the supervisor child can exist under it.
        _enable_v2_subtree(group_dir)
        supervisor_dir = _mkdir_cgroup(group_dir, _SUPERVISOR_CHILD_NAME)
        command_parent = _mkdir_cgroup(supervisor_dir, _COMMANDS_CHILD_NAME)
    except CgroupSetupError:
        # Roll back the empty cgroup so we don't leak it on failure.
        if command_parent is not None:
            _rmdir_quiet(command_parent)
        if supervisor_dir is not None:
            _rmdir_quiet(supervisor_dir)
        try:
            group_dir.rmdir()
        except OSError:
            logger.warning(
                "cgroup: failed to remove %s after setup error; manual cleanup needed",
                group_dir,
            )
        raise

    if supervisor_dir is None or command_parent is None:
        raise CgroupSetupError(
            f"cgroup v2 setup for {sandbox_id} did not create the supervisor child"
        )
    return CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id=sandbox_id,
        paths=[group_dir],
        supervisor_paths=[supervisor_dir],
        command_parent_paths=[command_parent],
    )


def _setup_v1(sandbox_id: str, policy: CgroupPolicy) -> CgroupHandle:
    """Create one v1 cgroup per controller actually used by the policy.

    Unlike v2 (unified hierarchy), each cgroup v1 controller has its own
    mount under ``/sys/fs/cgroup/<controller>/``. We only create the
    sub-directories for controllers the policy actually constrains so we
    don't leave empty memory/cpu/pids cgroups behind when the user only
    set, e.g., ``pids_max``.
    """
    created_paths: list[Path] = []
    supervisor_paths: list[Path] = []
    command_parents: list[Path] = []
    try:
        if policy.memory_max is not None:
            path = _create_v1_group("memory", sandbox_id)
            created_paths.append(path)
            _try_write_file(
                path / "memory.limit_in_bytes",
                str(policy.memory_max),
                what="memory.limit_in_bytes",
            )
            # Cap memory+swap to the same value so the swap controller can't
            # be used to bypass ``memory.limit_in_bytes``. ``memory.memsw``
            # is only present when the kernel was booted with swap accounting
            # enabled (``swapaccount=1`` or built-in default); a missing
            # file is non-fatal -- such hosts can't account swap usage per
            # cgroup anyway, so the user's ``memory_max`` setting is already
            # the hardest cap available.
            memsw_file = path / "memory.memsw.limit_in_bytes"
            if memsw_file.exists():
                try:
                    _write_file(memsw_file, str(policy.memory_max))
                except OSError as exc:
                    logger.warning(
                        "cgroup v1: failed to set memory.memsw.limit_in_bytes "
                        "for %s: %s",
                        sandbox_id,
                        exc,
                    )
        if policy.cpu_max is not None:
            quota, period = policy.cpu_max
            path = _create_v1_group("cpu", sandbox_id)
            created_paths.append(path)
            # ``cpu.cfs_period_us`` must be written before ``cpu.cfs_quota_us``
            # if the new quota exceeds the old period; flipping the order
            # would briefly violate the kernel's quota <= period * cpus
            # invariant. Writing period first is always safe.
            _try_write_file(
                path / "cpu.cfs_period_us",
                str(period),
                what="cpu.cfs_period_us",
            )
            _try_write_file(
                path / "cpu.cfs_quota_us",
                str(quota),
                what="cpu.cfs_quota_us",
            )
        if policy.pids_max is not None:
            path = _create_v1_group("pids", sandbox_id)
            created_paths.append(path)
            _try_write_file(
                path / "pids.max",
                str(policy.pids_max),
                what="pids.max",
            )
        for path in created_paths:
            supervisor = _mkdir_cgroup(path, _SUPERVISOR_CHILD_NAME)
            supervisor_paths.append(supervisor)
            command_parents.append(_mkdir_cgroup(supervisor, _COMMANDS_CHILD_NAME))
    except CgroupSetupError:
        for path in reversed(command_parents):
            _rmdir_quiet(path)
        for path in reversed(supervisor_paths):
            _rmdir_quiet(path)
        for path in created_paths:
            try:
                path.rmdir()
            except OSError:
                logger.warning(
                    "cgroup v1: failed to remove %s after setup error", path
                )
        raise

    return CgroupHandle(
        backend=CgroupBackend.V1,
        sandbox_id=sandbox_id,
        paths=created_paths,
        supervisor_paths=supervisor_paths,
        command_parent_paths=command_parents,
    )


def _create_v1_group(controller: str, sandbox_id: str) -> Path:
    parent = _CGROUP_ROOT / controller / _JIUWENBOX_ROOT_NAME
    try:
        parent.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {parent}: {exc.strerror or exc}"
        ) from exc
    group_dir = parent / sandbox_id
    try:
        group_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise CgroupSetupError(
            f"cgroup directory {group_dir} already exists"
        ) from exc
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {group_dir}: {exc.strerror or exc}"
        ) from exc
    return group_dir


def setup(sandbox_id: str, policy: CgroupPolicy) -> CgroupHandle:
    """Create a cgroup for ``sandbox_id`` and apply ``policy``'s limits.

    Tries cgroup v2 first, then falls back to v1. Raises
    :class:`CgroupSetupError` if neither backend is available or any of
    the writes fails; callers should treat that as a sandbox-create
    failure.
    """
    if policy.is_empty():
        raise ValueError(
            "cgroup.setup called with an empty policy; caller must "
            "check CgroupPolicy.is_empty() and skip cgroup entirely"
        )
    backend = detect_backend()
    if backend is None:
        raise CgroupSetupError(
            "no writable cgroup backend available: cgroup v2 "
            f"({_CGROUP_ROOT}/cgroup.controllers) is missing/incomplete "
            "and cgroup v1 controllers (memory/cpu/pids) are not mounted"
        )
    if backend is CgroupBackend.V2:
        return _setup_v2(sandbox_id, policy)
    return _setup_v1(sandbox_id, policy)


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


def oom_kill_count(handle: CgroupHandle) -> int:
    """Return how many OOM kills this sandbox cgroup has recorded.

    v2 exposes the counter in ``memory.events`` (``oom_kill`` and
    ``oom_group_kill``). v1 exposes ``oom_kill`` in ``memory.oom_control``.
    Missing files count as zero so a cpu-only or pids-only group is safe
    to pass in.
    """
    total = 0
    for group_dir in handle.paths:
        events = group_dir / "memory.events"
        if events.exists():
            total += _counter_in_file(events, "oom_kill")
            total += _counter_in_file(events, "oom_group_kill")
            continue
        control = group_dir / "memory.oom_control"
        if control.exists():
            total += _counter_in_file(control, "oom_kill")
    return total


def _counter_in_file(path: Path, key: str) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return 0
    return 0


def attach(handle: CgroupHandle, pid: int) -> None:
    """Migrate ``pid`` into the sandbox's supervisor cgroup.

    Writing the PID into ``cgroup.procs`` moves both the process and its
    threads; future forks automatically inherit the cgroup. The parent
    that holds the limits stays empty so command children can be created
    beside the daemon.
    """
    dirs = handle.supervisor_paths or handle.paths
    if not dirs:
        return
    for group_dir in dirs:
        procs_file = group_dir / "cgroup.procs"
        try:
            _write_file(procs_file, str(pid))
        except OSError as exc:
            raise CgroupSetupError(
                f"failed to attach pid {pid} to {procs_file}: "
                f"{exc.strerror or exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def kill_cgroup_processes(group_dir: Path, backend: CgroupBackend) -> None:
    """Kill every process in one cgroup directory.

    On v2 a write of ``"1"`` to ``cgroup.kill`` stops the directory's
    members, including processes that called ``setsid``. On v1 the
    controller hierarchies have no ``cgroup.kill``; every pid in
    ``cgroup.procs`` is signalled with SIGKILL. Sibling directories,
    including the supervisor child, are left alone. ``OSError`` is
    swallowed so a kill cannot block sandbox deletion or the exec result.
    """
    if backend is CgroupBackend.V2:
        kill_file = group_dir / "cgroup.kill"
        if kill_file.exists():
            try:
                _write_file(kill_file, "1")
            except OSError as exc:
                logger.warning(
                    "cgroup teardown: failed to write %s: %s", kill_file, exc
                )
        return
    procs_file = group_dir / "cgroup.procs"
    try:
        raw = procs_file.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid <= 1:
            continue
        try:
            os.kill(pid, 9)  # SIGKILL
        except OSError:
            pass


def _rmdir_with_retry(group_dir: Path) -> bool:
    """Attempt ``rmdir`` with short backoff while EBUSY is reported.

    Returns True on success, False when the cgroup was still busy after
    the configured retries. Callers should only log a warning on failure
    rather than propagating: a stuck cgroup is unfortunate but doesn't
    block sandbox cleanup from the user's perspective.
    """
    for attempt in range(_TEARDOWN_RETRIES):
        try:
            group_dir.rmdir()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt + 1 == _TEARDOWN_RETRIES:
                logger.warning(
                    "cgroup teardown: %s still busy after %d attempts (%s)",
                    group_dir,
                    _TEARDOWN_RETRIES,
                    exc,
                )
                return False
            time.sleep(_TEARDOWN_BACKOFF_SECONDS)
    return False


def _valid_command_id(command_id: str) -> bool:
    if not command_id or command_id in {".", ".."}:
        return False
    if "/" in command_id or "\\" in command_id:
        return False
    return True


def sandbox_mounts(handle: CgroupHandle) -> list[tuple[str, str]]:
    """Host ``commands`` directories and the sandbox paths they bind onto."""
    parents = handle.command_parent_paths
    if len(parents) == 1:
        return [(str(parents[0]), SANDBOX_COMMAND_CGROUP_ROOT)]
    return [
        (str(parent), f"{SANDBOX_COMMAND_CGROUP_ROOT}/{index}")
        for index, parent in enumerate(parents)
    ]


def sandbox_cgroup_bind_args(handle: CgroupHandle) -> list[str]:
    """bwrap arguments that expose command cgroups inside the sandbox.

    Emitted after the private ``/run`` tmpfs, which would otherwise hide a
    bind made earlier. ``--dir`` creates the mount point on that tmpfs.
    """
    mounts = sandbox_mounts(handle)
    if not mounts:
        return []
    args = ["--dir", SANDBOX_COMMAND_CGROUP_ROOT]
    for host_path, sandbox_path in mounts:
        if sandbox_path != SANDBOX_COMMAND_CGROUP_ROOT:
            args.extend(("--dir", sandbox_path))
        args.extend(("--bind", host_path, sandbox_path))
    return args


def create_command_cgroup(handle: CgroupHandle, command_id: str) -> CommandCgroup:
    """Create ``cmd-<id>`` under each ``supervisor/commands`` directory.

    Raises :class:`CgroupSetupError` when the child cannot be created.
    Callers must fail that exec rather than run it outside the cgroup.
    A handle recorded before ``command_parent_paths`` existed uses ``paths``.
    """
    if not _valid_command_id(command_id):
        raise CgroupSetupError(f"invalid command cgroup id {command_id!r}")
    parents = handle.command_parent_paths or handle.paths
    if not parents:
        raise CgroupSetupError(
            f"sandbox {handle.sandbox_id} cgroup has no parent directory"
        )
    name = f"{_COMMAND_CHILD_PREFIX}{command_id}"
    created: list[Path] = []
    try:
        for parent in parents:
            created.append(_mkdir_cgroup(parent, name))
    except CgroupSetupError:
        for path in created:
            _rmdir_quiet(path)
        raise
    return CommandCgroup(backend=handle.backend, paths=created)


def kill_live_commands(handle: CgroupHandle) -> None:
    """SIGKILL every ``cmd-*`` child under the command parents.

    The supervisor child is not a command parent, so the daemon stays up.
    Directories are left in place; the exec that owns a child removes it.
    """
    for parent in handle.command_parent_paths:
        if not parent.is_dir():
            continue
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and child.name.startswith(_COMMAND_CHILD_PREFIX):
                kill_cgroup_processes(child, handle.backend)


def release_command_cgroup(command: CommandCgroup) -> None:
    """Kill one command cgroup, then remove its empty directories.

    Does not kill the supervisor child or any other command.
    """
    for group_dir in command.paths:
        if not group_dir.exists():
            continue
        kill_cgroup_processes(group_dir, command.backend)
        _rmdir_with_retry(group_dir)


def _child_directories(group_dir: Path) -> list[Path]:
    try:
        entries = list(group_dir.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if entry.is_dir()]


def teardown(handle: CgroupHandle) -> None:
    """Remove the cgroup(s) associated with ``handle``.

    Kills processes in every child (the supervisor and any command
    cgroups) and then in the parent, then rmdir-s children before the
    parent. Failures only emit a warning and never propagate - sandbox
    deletion must remain idempotent.
    """
    for group_dir in handle.paths:
        if not group_dir.exists():
            continue
        children = _child_directories(group_dir)
        for child in children:
            kill_cgroup_processes(child, handle.backend)
        # v1 parents have an empty cgroup.procs once every process lives
        # in a child. v2 cgroup.kill on the parent also covers descendants.
        kill_cgroup_processes(group_dir, handle.backend)
        # Give the kernel a brief moment to flush the killed processes
        # before we attempt rmdir; the retry loop in _rmdir_with_retry
        # handles the still-EBUSY case.
        time.sleep(_TEARDOWN_BACKOFF_SECONDS)
        for child in children:
            _rmdir_with_retry(child)
        if not _rmdir_with_retry(group_dir):
            # Last-ditch effort: try a non-recursive rmtree to clean up
            # if rmdir keeps failing (e.g. leftover sub-cgroups created
            # by some user code inside the sandbox). shutil.rmtree on a
            # cgroup dir is generally a no-op because the interface
            # files cannot be unlinked, but it's harmless on success.
            try:
                shutil.rmtree(group_dir, ignore_errors=True)
            except OSError:
                pass
