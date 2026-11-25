# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Persistent local Broker for browser sessions.

The Broker owns a :class:`BrowserSession` and exposes acquire/call/release over
loopback TCP. Plugin processes are clients; a rebuilt plugin reacquires the same
``browser_session_id`` and continues on the existing tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ._proc import atomic_write_private
from .actions import build_tool_defs
from .bridge.protocol import MAX_TCP_MESSAGE_BYTES, ProtocolError, encode_message, read_async_frame
from .config import BrowserConfig, BrowserEnv, ConfigError
from .errors import BrowserError, Unavailable
from .locking import InstanceLock, acquire_instance_lock
from .logging import log
from .runtime import call_tool
from .session import BrowserSession


@dataclass
class Lease:
    lease_id: str
    owner: str
    session_id: str
    connection: Any | None = None
    generation: str = ""
    last_seen: float = 0.0
    in_flight: int = 0


def _session_state_dir(root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return root / "sessions" / digest


MAX_BROKER_SESSIONS = 8
MAX_BROKER_SESSIONS_PER_OWNER = 2

#: Read-only tools that have no side effects and may be cancelled immediately when
#: the calling client disconnects (releasing the session lease and action lock).
#: Mutating tools are never cancelled on disconnect so in-flight writes keep their
#: uncertain semantics.
_CANCELLABLE_READ_TOOLS = frozenset(
    {
        "browser_wait_for",
        "browser_snapshot",
        "browser_find",
        "browser_extract",
        "browser_screenshot",
        "browser_tabs",
        "browser_downloads",
        "browser_logs",
        "browser_action_status",
    }
)


def _consume_task_error(task: asyncio.Task[None]) -> None:
    """Retrieve a detached teardown task's exception so asyncio never warns."""
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


class BrokerServer:
    """One Broker process owning named BrowserSessions."""

    def __init__(
        self,
        cfg: BrowserConfig,
        *,
        session_factory: Callable[[BrowserConfig], Any] = BrowserSession,
        tool_caller: Callable[[Any, Any, Any], Any] = call_tool,
        broker_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self._session_factory = session_factory
        self._tool_caller = tool_caller
        self.broker_dir = broker_dir or (cfg.state_dir / "broker")
        self.file = self.broker_dir / "broker.json"
        self.token = secrets.token_hex(16)
        self.generation = secrets.token_hex(8)
        self.port = 0
        self.sessions: dict[str, Any] = {}
        self.owners: dict[str, str] = {}
        self.leases: dict[str, Lease] = {}
        self._closing_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing_owners: dict[str, str] = {}
        self._tools = {
            tool.name: tool
            for tool in build_tool_defs(
                evaluate_enabled=cfg.evaluate_enabled,
                save_state_enabled=cfg.save_state_enabled,
            )
        }
        self._server: asyncio.AbstractServer | None = None
        self._lock: InstanceLock | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self.stop_event = asyncio.Event()
        self._sweep_task: asyncio.Task[None] | None = None
        self._last_activity = time.monotonic()

    async def start(self) -> None:
        self.broker_dir.mkdir(parents=True, exist_ok=True)
        self._lock = await acquire_instance_lock(self.broker_dir, wait=0.0)
        try:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
            sockets = self._server.sockets or ()
            if not sockets:
                raise Unavailable("broker server has no listening socket")
            self.port = int(sockets[0].getsockname()[1])
            payload = {
                "port": self.port,
                "token": self.token,
                "pid": os.getpid(),
                "generation": self.generation,
                "protocol_version": 1,
                "allowed_domains": list(self.cfg.allowed_domains),
                "allowed_origins": list(self.cfg.allowed_origins),
                "headless": self.cfg.headless,
                "bridge": self.cfg.bridge,
                "evaluate": self.cfg.evaluate_enabled,
                "save_state": self.cfg.save_state_enabled,
                "chrome_devtools": self.cfg.chrome_devtools,
                "cdp_url": self.cfg.cdp_url or "",
                "upload_roots": [str(p) for p in self.cfg.upload_roots],
            }
            await asyncio.to_thread(atomic_write_private, self.file, json.dumps(payload), strict=False)
            self._sweep_task = asyncio.get_running_loop().create_task(self._sweep())
        except BaseException:
            self._release_lock()
            raise

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        self._last_activity = time.monotonic()
        if len(self._clients) > 32:
            self._clients.discard(writer)
            writer.close()
            return
        try:
            auth = await asyncio.wait_for(
                read_async_frame(reader, limit=65536),
                timeout=5.0,
            )
            token = auth.get("token") if isinstance(auth, dict) else None
            if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
                return
            while True:
                message = await read_async_frame(reader, limit=MAX_TCP_MESSAGE_BYTES)
                if message is None:
                    return
                if message.get("op") == "call" and str(message.get("tool") or "") in _CANCELLABLE_READ_TOOLS:
                    response = await self._dispatch_cancellable_read(message, reader, writer)
                    if response is None:
                        return
                else:
                    response = await self._dispatch(message, writer)
                writer.write(encode_message(response, limit=MAX_TCP_MESSAGE_BYTES))
                await writer.drain()
        except (
            ConnectionError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
            ProtocolError,
        ):
            return
        finally:
            self._release_for_connection(writer)
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            await self._evict_idle_sessions()

    async def _watch_client_eof(self, reader: asyncio.StreamReader) -> bool:
        """Wait until the client closes the connection (EOF/error) while a read-only call runs."""
        try:
            data = await reader.read(1)
        except (ConnectionError, OSError):
            return True
        return data == b""

    async def _dispatch_cancellable_read(
        self,
        message: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> dict[str, Any] | None:
        """Run a read-only tool call, cancelling it promptly if the client disconnects."""
        dispatch_task = asyncio.create_task(self._dispatch(message, writer))
        eof_task = asyncio.create_task(self._watch_client_eof(reader))
        try:
            done, _pending = await asyncio.wait(
                {dispatch_task, eof_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if eof_task in done:
                is_eof = False
                with contextlib.suppress(Exception):
                    is_eof = bool(eof_task.result())
                if not dispatch_task.done():
                    dispatch_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await dispatch_task
                if is_eof:
                    return None
                # Non-EOF byte was consumed from the TCP stream; continuing would desync framing.
                raise ProtocolError("unexpected pipelined data during broker read call")
            return dispatch_task.result()
        finally:
            if not eof_task.done():
                eof_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await eof_task

    def _release_for_connection(self, writer: asyncio.StreamWriter) -> None:
        for lease_id, lease in list(self.leases.items()):
            if lease.connection is writer:
                self.leases.pop(lease_id, None)

    def _begin_close_session(self, session_id: str, *, log_errors: bool = False) -> asyncio.Task[None] | None:
        """Detach *session_id* and start (or join) its single teardown task."""
        existing = self._closing_tasks.get(session_id)
        if existing is not None:
            return existing
        for active_id, active_lease in list(self.leases.items()):
            if active_lease.session_id == session_id:
                self.leases.pop(active_id, None)
        session = self.sessions.pop(session_id, None)
        if session is None:
            self.owners.pop(session_id, None)
            return None
        self._closing_owners[session_id] = self.owners.get(session_id, "")
        task = asyncio.get_running_loop().create_task(
            self._teardown_session(session_id, session, log_errors=log_errors)
        )
        task.add_done_callback(_consume_task_error)
        self._closing_tasks[session_id] = task
        return task

    async def _teardown_session(self, session_id: str, session: Any, *, log_errors: bool) -> None:
        try:
            aclose = getattr(session, "aclose", None)
            if callable(aclose):
                if log_errors:
                    try:
                        await aclose()
                    except Exception as exc:
                        log(f"broker evicted session aclose failed: {exc!r}")
                else:
                    await aclose()
        finally:
            for active_id, active_lease in list(self.leases.items()):
                if active_lease.session_id == session_id:
                    self.leases.pop(active_id, None)
            if self.sessions.get(session_id) is session:
                self.sessions.pop(session_id, None)
            self.owners.pop(session_id, None)
            self._closing_owners.pop(session_id, None)
            self._closing_tasks.pop(session_id, None)

    async def _close_session(self, session_id: str, *, log_errors: bool = False) -> None:
        task = self._begin_close_session(session_id, log_errors=log_errors)
        if task is not None:
            await asyncio.shield(task)

    def _evictable_session_ids(self) -> list[str]:
        candidates: list[str] = []
        for session_id, session in list(self.sessions.items()):
            if session_id in self._closing_tasks:
                continue
            if any(lease.session_id == session_id for lease in self.leases.values()):
                continue
            if getattr(session, "state", None) in {"idle", "closed", "failed"}:
                candidates.append(session_id)
        return candidates

    async def _evict_idle_sessions(self) -> bool:
        """Detach and close every unleased idle/closed/failed session via the unified teardown path."""
        tasks = [
            task
            for session_id in self._evictable_session_ids()
            if (task := self._begin_close_session(session_id, log_errors=True)) is not None
        ]
        for task in tasks:
            await asyncio.shield(task)
        return bool(tasks)

    async def _sweep(self) -> None:
        interval = 1.0 if self.cfg.broker_lease_ttl < 30 else 30.0
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            now = time.monotonic()
            if self.cfg.broker_lease_ttl > 0:
                for lease_id, lease in list(self.leases.items()):
                    if lease.in_flight > 0:
                        continue
                    if now - lease.last_seen > self.cfg.broker_lease_ttl:
                        self.leases.pop(lease_id, None)
            await self._evict_idle_sessions()
            if (
                self.cfg.broker_idle_exit > 0
                and not self._clients
                and not self.leases
                and not self.sessions
                and not self._closing_tasks
                and now - self._last_activity > self.cfg.broker_idle_exit
            ):
                self.stop_event.set()
                return

    async def _dispatch(self, message: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
        request_id = message.get("id")
        op = message.get("op")
        try:
            if op == "acquire":
                lease = await self._acquire(
                    str(message.get("owner", "")),
                    str(message.get("session_id", "")),
                    writer,
                )
                return {
                    "id": request_id,
                    "ok": True,
                    "lease_id": lease.lease_id,
                    "session_id": lease.session_id,
                    "generation": lease.generation,
                }
            if op == "call":
                blocks, url, title = await self._call(
                    str(message.get("lease_id", "")),
                    str(message.get("generation", "")),
                    str(message.get("tool", "")),
                    message.get("arguments"),
                )
                return {"id": request_id, "ok": True, "blocks": blocks, "url": url, "title": title}
            if op == "release":
                lease = self.leases.get(str(message.get("lease_id", "")))
                if lease is None or lease.generation != str(message.get("generation", "")):
                    return {"id": request_id, "ok": False, "error": "broker lease is not active"}
                self.leases.pop(lease.lease_id, None)
                await self._evict_idle_sessions()
                return {"id": request_id, "ok": True}
            if op == "close":
                lease = self.leases.get(str(message.get("lease_id", "")))
                req_generation = str(message.get("generation", ""))
                req_owner = str(message.get("owner", ""))
                req_session_id = str(message.get("session_id", ""))
                if lease is not None and lease.generation == req_generation:
                    session_id = lease.session_id
                else:
                    session_id = req_session_id
                    if not session_id or not req_owner:
                        return {"id": request_id, "ok": False, "error": "broker lease is not active"}
                    known_owner = self.owners.get(session_id) or self._closing_owners.get(session_id)
                    if known_owner is not None and known_owner != req_owner:
                        return {"id": request_id, "ok": False, "error": "broker lease is not active"}
                    if (
                        known_owner is None
                        and session_id not in self.sessions
                        and session_id not in self._closing_tasks
                    ):
                        # Idempotent close: the session does not exist (never created or already closed).
                        return {"id": request_id, "ok": True}
                await self._close_session(session_id, log_errors=False)
                return {"id": request_id, "ok": True}
            if op == "shutdown":
                self.stop_event.set()
                return {"id": request_id, "ok": True}
            if op == "status":
                return {
                    "id": request_id,
                    "ok": True,
                    "sessions": list(self.sessions),
                    "leases": len(self.leases),
                }
            raise Unavailable(f"unknown broker operation: {op}")
        except BrowserError as exc:
            payload: dict[str, Any] = {"id": request_id, "ok": False, "error": exc.message, "code": exc.code}
            if exc.blocked_origin:
                payload["blocked_origin"] = exc.blocked_origin
            return payload
        except Exception as exc:  # noqa: BLE001 - broker boundary
            return {"id": request_id, "ok": False, "error": f"broker_error: {exc}"}

    async def _acquire(self, owner: str, session_id: str, connection: Any) -> Lease:
        if not owner or not session_id:
            raise Unavailable("broker acquire requires owner and session_id")
        while True:
            closing = self._closing_tasks.get(session_id)
            if closing is not None:
                closing_owner = self._closing_owners.get(session_id)
                if closing_owner and closing_owner != owner:
                    raise Unavailable(f"browser session {session_id} is owned by another client")
                with contextlib.suppress(Exception):
                    await asyncio.shield(closing)
                continue
            for lease in list(self.leases.values()):
                if lease.session_id != session_id:
                    continue
                if lease.connection is connection:
                    if session_id in self.sessions:
                        return lease
                    self.leases.pop(lease.lease_id, None)
                    continue
                if lease.connection is not None and lease.connection.is_closing():
                    self.leases.pop(lease.lease_id, None)
                    continue
                if lease.owner == owner:
                    raise Unavailable(
                        f"browser session {session_id} already has a live lease; close the previous client"
                    )
                raise Unavailable(f"browser session {session_id} is owned by another client")
            known_owner = self.owners.get(session_id)
            if known_owner is not None and known_owner != owner:
                raise Unavailable(f"browser session {session_id} is owned by another client")
            session = self.sessions.get(session_id)
            if session is not None and getattr(session, "state", None) == "closed":
                await self._close_session(session_id, log_errors=True)
                continue
            if session is None:
                if self._evictable_session_ids():
                    await self._evict_idle_sessions()
                    continue
                if self.cfg.bridge:
                    if self._closing_tasks and not self.sessions:
                        for task in list(self._closing_tasks.values()):
                            with contextlib.suppress(Exception):
                                await asyncio.shield(task)
                        continue
                    if self.sessions:
                        raise Unavailable("bridge mode supports one Broker session per state dir")
                    session_cfg = self.cfg
                elif session_id == "default" and (self.cfg.state_dir / "chrome").is_dir():
                    # Preserve the pre-multi-session profile/login for the default id.
                    session_cfg = self.cfg
                else:
                    if len(self.sessions) + len(self._closing_tasks) >= MAX_BROKER_SESSIONS:
                        if self._closing_tasks:
                            for task in list(self._closing_tasks.values()):
                                with contextlib.suppress(Exception):
                                    await asyncio.shield(task)
                            continue
                        raise Unavailable(f"at most {MAX_BROKER_SESSIONS} Broker sessions are supported")
                    if sum(1 for current in self.owners.values() if current == owner) >= (
                        MAX_BROKER_SESSIONS_PER_OWNER
                    ):
                        owner_closing = [
                            task
                            for sid, task in list(self._closing_tasks.items())
                            if self._closing_owners.get(sid) == owner
                        ]
                        if owner_closing:
                            for task in owner_closing:
                                with contextlib.suppress(Exception):
                                    await asyncio.shield(task)
                            continue
                        raise Unavailable(
                            f"at most {MAX_BROKER_SESSIONS_PER_OWNER} Broker sessions per owner are supported"
                        )
                    session_cfg = replace(
                        self.cfg,
                        state_dir=_session_state_dir(self.cfg.state_dir, session_id),
                    )
                if self._session_factory is BrowserSession:
                    session = BrowserSession(session_cfg, journal_owner=owner, journal_session=session_id)
                else:
                    session = self._session_factory(session_cfg)
                self.sessions[session_id] = session
                self.owners[session_id] = owner
            if isinstance(session, BrowserSession):
                session.reset_action_budget()
            lease = Lease(
                secrets.token_hex(12),
                owner,
                session_id,
                connection,
                secrets.token_hex(8),
                time.monotonic(),
            )
            self.leases[lease.lease_id] = lease
            return lease

    async def _call(
        self, lease_id: str, generation: str, tool_name: str, arguments: Any
    ) -> tuple[list[dict[str, Any]], str, str]:
        lease = self.leases.get(lease_id)
        if lease is None or lease.generation != generation:
            raise Unavailable("broker lease is not active")
        lease.last_seen = time.monotonic()
        self._last_activity = lease.last_seen
        tool = self._tools.get(tool_name)
        if tool is None:
            raise Unavailable(
                f"unknown tool: {tool_name}; this Broker process may predate the installed plugin - restart it"
            )
        session = self.sessions.get(lease.session_id)
        if session is None:
            raise Unavailable("broker lease is not active")
        lease.in_flight += 1
        try:
            result = await self._tool_caller(session, tool, arguments)
        finally:
            lease.in_flight = max(0, lease.in_flight - 1)
            lease.last_seen = time.monotonic()
            self._last_activity = lease.last_seen
        if not isinstance(result, list) or not all(isinstance(block, dict) for block in result):
            raise Unavailable("broker tool returned an invalid result")
        snapshot = getattr(session, "snapshot", None)
        return result, str(getattr(snapshot, "url", "") or ""), str(getattr(snapshot, "title", "") or "")

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            # Close accepted clients first: wait_closed waits for their handlers.
            for writer in list(self._clients):
                writer.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for writer in list(self._clients):
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._clients.clear()
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        for session_id in list(self.sessions):
            self._begin_close_session(session_id, log_errors=True)
        for task in list(self._closing_tasks.values()):
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
        self.sessions.clear()
        self.owners.clear()
        self.leases.clear()
        self._closing_tasks.clear()
        self._closing_owners.clear()
        if self.file.is_file():
            with contextlib.suppress(OSError, ValueError):
                payload = json.loads(self.file.read_text(encoding="utf-8"))
                if payload.get("generation") == self.generation:
                    self.file.unlink()
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None


def _run_config(state_dir: Path | None = None) -> BrowserConfig:
    """Broker-wide configuration; downloads stay off (it has no session folder)."""
    cfg = BrowserConfig.from_env()
    if state_dir is not None:
        return replace(cfg, state_dir=state_dir.expanduser(), download_dir=None)
    return replace(cfg, download_dir=None)


async def _run(state_dir: Path | None = None) -> None:
    cfg = _run_config(state_dir)
    server = BrokerServer(cfg)
    await server.start()
    try:
        await server.stop_event.wait()
    finally:
        await server.aclose()


def _purge_profiles(state_dir: Path) -> None:
    import shutil

    profile_root = state_dir / "sessions"
    removed = sum(1 for child in profile_root.glob("*") if child.is_dir()) if profile_root.exists() else 0
    if profile_root.exists():
        try:
            shutil.rmtree(profile_root)
        except OSError as exc:
            print(f"could not purge profiles at {profile_root}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    print(f"purged {removed} session profile(s) at {profile_root}")


def main(argv: list[str] | tuple[str, ...] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="icode-browser-use-broker")
    parser.add_argument("--stop", action="store_true", help="stop the Broker for this state dir")
    parser.add_argument("--purge-profiles", action="store_true", help="also delete session profiles")
    parser.add_argument("--state-dir", default=None, help="override ICODE_BROWSER_STATE_DIR")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.purge_profiles and not args.stop:
        print("--purge-profiles requires --stop", file=sys.stderr)
        raise SystemExit(2)
    if args.stop:
        state_dir = Path(args.state_dir).expanduser() if args.state_dir else BrowserEnv().state_dir()
        path = state_dir / "broker" / "broker.json"
        if not path.is_file():
            if args.purge_profiles:
                _purge_profiles(state_dir)
                return 0
            print(f"no broker discovery at {path}", file=sys.stderr)
            raise SystemExit(1)

        async def stop() -> bool:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", int(payload["port"])), timeout=3
                )
                writer.write(encode_message({"token": str(payload["token"])}))
                await writer.drain()
                writer.write(encode_message({"id": 0, "op": "shutdown"}))
                await writer.drain()
                await asyncio.wait_for(read_async_frame(reader), timeout=3)
                writer.close()
                await writer.wait_closed()
            except (OSError, ValueError, KeyError, asyncio.TimeoutError):
                return False
            return True

        if not asyncio.run(stop()):
            print(f"could not stop the broker at {path}", file=sys.stderr)
            raise SystemExit(1)
        for _ in range(300):
            if not path.exists():
                break
            import time

            time.sleep(0.1)
        else:
            print(f"broker at {path} did not clean up", file=sys.stderr)
            raise SystemExit(1)
        if args.purge_profiles:
            _purge_profiles(state_dir)
        return 0
    override_state_dir = Path(args.state_dir).expanduser() if args.state_dir else None
    try:
        _run_config(override_state_dir)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    asyncio.run(_run(override_state_dir))
    return 0


if __name__ == "__main__":
    main()
