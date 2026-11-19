# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""IPC protocol shared between box-server and the in-sandbox daemon.

The protocol is intentionally tiny: every message is a single 4-byte
big-endian length prefix followed by exactly that many bytes of payload.
JSON objects describe the request/response semantics, while raw bytes
(stdin contents, captured stdout/stderr) ride alongside as additional
length-prefixed frames so we can carry binary payloads safely.

Stays standard-library only so the daemon (loaded with ``python3 -S``
inside the sandbox) does not have to drag in the rest of jiuwenbox to
participate in IPC.
"""

from __future__ import annotations

import array
import socket
import struct
from typing import Any

# Names match between server and daemon; do not rename without bumping the
# protocol version below. Both the daemon script and the Landlock launcher
# live under ``/jiuwenbox`` (a fresh tmpfs created per sandbox by the
# runtime). The directory is reserved by ``PolicyEngine`` - user policies
# cannot reference any path under it - so collisions with
# ``read_only`` / ``read_write`` / ``bind_mounts`` / ``device`` entries
# are rejected at policy-validation time, and ``/jiuwenbox`` is also kept
# *outside* the Landlock allowlist so user code spawned by the daemon
# cannot read either script after Landlock applies.
SANDBOX_RESERVED_DIR = "/jiuwenbox"
SANDBOX_DAEMON_SANDBOX_PATH = f"{SANDBOX_RESERVED_DIR}/sandbox-daemon.py"
SANDBOX_CONTROL_SOCKET_NAME = "control.sock"
SANDBOX_LAUNCHER_PATH = f"{SANDBOX_RESERVED_DIR}/landlock-launcher.py"

# Box-server binds the listener Unix socket on a host directory the sandbox
# replaces with a private tmpfs, never under the home directory (``/home``
# is bind-mounted). It passes the listener fd into the sandbox via
# ``subprocess.Popen(pass_fds=...)``. Bubblewrap's user command path never
# closes arbitrary inherited fds, so the listener flows untouched through
# bwrap → launcher → daemon. The daemon recovers the fd number from this
# environment variable and runs ``accept`` against it directly. That
# directory has no path inside the sandbox that points at the control
# socket, so user code spawned by the daemon cannot reach it (and the
# daemon spawns user children with ``close_fds=True`` so the inherited fd
# is not exposed either).
LISTENER_FD_ENV = "JIUWENBOX_CONTROL_LISTENER_FD"
SANDBOX_IP_ENV = "SANDBOX_IP"
# "1" tells the daemon it runs in a pid namespace bubblewrap created for
# this sandbox (``--unshare-pid``), where it may kill processes outside its
# own tree. Only ``BwrapConfig.to_args`` sets it; it unsets it otherwise.
PRIVATE_PID_NAMESPACE_ENV = "JIUWENBOX_PRIVATE_PID_NAMESPACE"


def apply_sandbox_ip_env(
    env: dict[str, str],
    sandbox_ip: str | None,
) -> None:
    """Force ``SANDBOX_IP`` to the lifecycle snapshot (or remove if unavailable).

    Callers must not invent a value when discovery fails; strip any prior
    entry first so policy / create / host env cannot leave a fabricated IP.
    """
    env.pop(SANDBOX_IP_ENV, None)
    if sandbox_ip:
        env[SANDBOX_IP_ENV] = sandbox_ip


# Daemon argv vector. ``-S`` shaves the ``import site`` cost so the daemon
# starts faster; the daemon is stdlib-only so ``site`` is unnecessary.
SANDBOX_DAEMON_COMMAND = ["python3", "-S", SANDBOX_DAEMON_SANDBOX_PATH]

# Outgoing request types accepted by the daemon.
REQUEST_TYPE_EXEC = "exec"
REQUEST_TYPE_SHUTDOWN = "shutdown"
# File-ops fast paths. Box-server uses these instead of spawning bash/
# python helpers via ``REQUEST_TYPE_EXEC`` so upload/download/list pay no
# python cold-start or fork+exec cost. The daemon performs the operation
# directly in its own process, so namespaces, mount layout, the policy
# uid/gid, seccomp and Landlock all apply identically to what a child
# would see.
REQUEST_TYPE_WRITE_FILE = "write_file"
REQUEST_TYPE_READ_FILE = "read_file"
REQUEST_TYPE_LIST_DIR = "list_dir"
REQUEST_TYPE_EXEC_BACKGROUND = "exec_background"
REQUEST_TYPE_BG_STATUS = "bg_status"
REQUEST_TYPE_BG_KILL = "bg_kill"

PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 1 * 1024 * 1024  # 1 MiB JSON header upper bound
MAX_STDIN_BYTES = 64 * 1024 * 1024  # 64 MiB stdin upper bound
MAX_STDOUT_BYTES = 64 * 1024 * 1024  # 64 MiB stdout upper bound
MAX_STDERR_BYTES = 64 * 1024 * 1024  # 64 MiB stderr upper bound
MAX_FILE_BYTES = 256 * 1024 * 1024  # 256 MiB read/write upper bound

ACCEPT_BACKLOG = 128


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``sock`` or raise ``ConnectionError``."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"socket closed after {len(buf)}/{n} bytes",
            )
        buf.extend(chunk)
    return bytes(buf)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    """Send a single length-prefixed frame on ``sock``."""
    if len(payload) > 0xFFFFFFFF:
        raise ValueError(f"frame size {len(payload)} exceeds 4 GiB")
    sock.sendall(struct.pack(">I", len(payload)))
    if payload:
        sock.sendall(payload)


def send_frame_with_fds(
    sock: socket.socket,
    payload: bytes,
    fds: list[int],
) -> None:
    """Send one length-prefixed frame and attach ``fds`` with ``SCM_RIGHTS``.

    The descriptors ride on the first byte of this frame. An empty list
    is a normal :func:`send_frame`. When descriptors are required and this
    platform cannot pass them, raise ``OSError`` before any byte of the
    frame is written.
    """
    if not fds:
        send_frame(sock, payload)
        return
    if len(payload) > 0xFFFFFFFF:
        raise ValueError(f"frame size {len(payload)} exceeds 4 GiB")
    rights = getattr(socket, "SCM_RIGHTS", None)
    sendmsg = getattr(sock, "sendmsg", None)
    if rights is None or sendmsg is None:
        raise OSError("SCM_RIGHTS is unavailable; cannot pass a command cgroup fd")
    frame = struct.pack(">I", len(payload)) + payload
    ancillary = [(socket.SOL_SOCKET, int(rights), array.array("i", fds).tobytes())]
    view = memoryview(frame)
    sent = sendmsg([view], ancillary)
    if sent <= 0:
        raise ConnectionError("socket closed while sending command cgroup fds")
    rest = frame[sent:]
    if rest:
        sock.sendall(rest)


def recv_frame(sock: socket.socket, max_size: int) -> bytes:
    """Receive a single length-prefixed frame, enforcing ``max_size``."""
    header = recv_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size > max_size:
        raise ValueError(
            f"incoming frame size {size} exceeds limit {max_size}",
        )
    return recv_exact(sock, size)


def encode_request(
    *,
    request_type: str,
    payload: dict[str, Any] | None = None,
) -> bytes:
    import json

    body: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": request_type,
    }
    if payload:
        body.update(payload)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")
