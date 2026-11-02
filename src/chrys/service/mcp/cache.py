# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared live MCP connection cache.

The cache stores successful, connected Chrys MCPTool instances and hands each
consumer fresh ``FunctionTool`` wrappers.  Sharing the live
transport avoids repeated MCP connect/list latency across agent rebuilds while
keeping wrapper-level mutable state, such as approval mode counters, isolated
per parent or sub-agent consumer.

Identical parent/sub-agent MCP configs intentionally share one MCP session.
Stateful servers may therefore observe interleaved requests from multiple
agents; add a per-server opt-out only if a real server needs isolation.
The likely shape of that opt-out is a profile flag such as
``share_session: false`` on ``MCPServerConfig``.  When adding it, include an
agent/session/share-boundary component in the cache key for those configs so
they still benefit from caching inside the chosen boundary without sharing one
server-side client session across independent agents.

A manual reload opens one refresh round (:meth:`MCPConnectionCache.begin_refresh`).
Every key the rebuild asks for that already has a connection gets one fresh
attempt, shared by the main agent and its sub-agents. Old leases keep working
meanwhile: the candidate stays beside the entry it would replace until the
rebuild commits, and an aborted round closes it and leaves the entry in place.
Ordinary rebuilds open no round and reuse live connections as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.process_env import canonical_env_name
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_ENV
from chrys.foundation.tool_call_context import get_tool_context, set_tool_context
from chrys.foundation.tool_kinds import KIND_MCP, get_tool_kind, set_tool_kind
from chrys.foundation.util.env_templates import resolve_env_template_mapping
from chrys.kernel import FunctionTool
from chrys.service.mcp import _connection
from chrys.service.mcp._stdio_transport import _inherited_stdio_environment
from chrys.service.mcp.errors import MCPConnectionError, mcp_failure_diagnostic
from chrys.service.mcp.result_limits import _MCP_PROMPT_TOOL_KEY, truncate_mcp_result

if TYPE_CHECKING:
    from chrys.service.mcp.owned import MCPTool
    from chrys.service.profiles.agents.schema import MCPServerConfig


logger = logging.getLogger(__name__)

IDLE_TTL_SECONDS = 300
SESSION_PROBE_TIMEOUT_SECONDS = 5


@dataclass(eq=False)
class MCPConnectionEntry:
    """Internal cache entry for one canonical MCP server config; identity is what tells generations apart."""

    key: str
    config: MCPServerConfig
    mcp_tool: MCPTool | None
    refcount: int
    last_used: datetime
    connect_task: asyncio.Task[MCPTool] | None


@dataclass(frozen=True)
class MCPConnectionLease:
    """A per-consumer lease on a cached MCP connection."""

    key: str
    server_name: str
    config: MCPServerConfig
    functions: list[FunctionTool]
    mcp_tool: MCPTool
    # The generation this lease holds: a refresh can install another entry under the same key.
    entry: MCPConnectionEntry = field(repr=False, compare=False)


@dataclass(frozen=True)
class MCPRefreshReport:
    """One server a refresh could not simply reconnect; ``detail`` holds nothing a server or a library wrote."""

    server_name: str
    kept_existing: bool
    detail: str


@dataclass
class _RefreshRound:
    """What one refresh tried, kept beside the live entries until it commits or aborts."""

    attempts: dict[str, asyncio.Task[MCPConnectionEntry]] = field(default_factory=dict)
    # Each candidate holds one reference of the round's, so an early release cannot close it.
    candidates: list[MCPConnectionEntry] = field(default_factory=list)
    unverified: list[MCPConnectionEntry] = field(default_factory=list)
    requested: set[str] = field(default_factory=set)
    first_connections: set[str] = field(default_factory=set)
    failures: dict[str, tuple[bool, BaseException]] = field(default_factory=dict)
    probe_problems: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPEffectiveConfig:
    """Resolved MCP config values used for both keying and connecting."""

    config: MCPServerConfig
    key: str
    stdio_env: dict[str, str] | None = None
    resolved_stdio_cwd: str | None = None
    http_headers: dict[str, str] | None = None


def _stable_mapping(mapping: dict[str, str]) -> list[tuple[str, str]]:
    """Return a deterministic representation for JSON cache-key payloads."""
    return sorted(mapping.items())


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Mark background connect-task exceptions retrieved if every waiter left."""
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


def _resolved_stdio_env(config: MCPServerConfig) -> dict[str, str]:
    return resolve_env_template_mapping(config.env, location=f"MCP server {config.name!r} env")


def _resolved_http_headers(config: MCPServerConfig) -> dict[str, str]:
    if not config.resolve_header_templates:
        return dict(config.headers)
    return resolve_env_template_mapping(config.headers, location=f"MCP server {config.name!r} headers")


def resolve_mcp_effective_config(config: MCPServerConfig, *, stdio_cwd: str | None = None) -> MCPEffectiveConfig:
    """Resolve the effective config used for keying and connection creation.

    Stdio MCP child processes inherit the supplied session cwd and process
    environment at spawn time, so both are part of the key.

    The key is intentionally independent of parent/sub-agent identity today:
    identical configs share one live MCP session.  If a future
    ``share_session: false`` config option is added for stateful MCP servers,
    add the relevant isolation boundary to this payload before hashing.
    """
    payload: dict[str, Any] = {
        "name": config.name,
        "transport": config.transport,
        "load_prompts": config.load_prompts,
        "tool_name_prefix": config.tool_name_prefix,
        "allowed_tools": None if config.allowed_tools is None else sorted(config.allowed_tools),
        "request_timeout": config.request_timeout,
    }
    stdio_env: dict[str, str] | None = None
    resolved_stdio_cwd: str | None = None
    http_headers: dict[str, str] | None = None
    if config.transport == "stdio":
        stdio_env = _resolved_stdio_env(config)
        child_env = _inherited_stdio_environment(stdio_env)
        key_env = {k: v for k, v in child_env.items() if canonical_env_name(k) != MODEL_POINTER_ENV}
        for name, value in stdio_env.items():
            if canonical_env_name(name) == MODEL_POINTER_ENV:
                key_env[MODEL_POINTER_ENV] = value
        resolved_stdio_cwd = stdio_cwd or os.getcwd()
        payload["stdio"] = {
            "command": config.command,
            "args": list(config.args),
            "encoding": config.encoding,
            # Switching models does not change the server unless its configuration uses the pointer.
            "env": _stable_mapping(key_env),
            "cwd": resolved_stdio_cwd,
        }
        stdio_env = child_env
    elif config.transport == "http":
        http_headers = _resolved_http_headers(config)
        payload["http"] = {
            "url": config.url,
            "headers": _stable_mapping(http_headers),
            "resolve_header_templates": config.resolve_header_templates,
            "terminate_on_close": config.terminate_on_close,
            "verify_ssl": config.verify_ssl,
            "bypass_proxy": config.bypass_proxy,
        }
    else:
        payload["unknown_transport"] = config.transport
    key_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    return MCPEffectiveConfig(
        config=config,
        key=key,
        stdio_env=stdio_env,
        resolved_stdio_cwd=resolved_stdio_cwd,
        http_headers=http_headers,
    )


def mcp_config_cache_key(config: MCPServerConfig, *, stdio_cwd: str | None = None) -> str:
    """Build a canonical fingerprint for the effective MCP server config."""
    return resolve_mcp_effective_config(config, stdio_cwd=stdio_cwd).key


def clone_mcp_function_tool(
    source: Any,
    *,
    result_cap: int | None = None,
    spill_dir: Path | None = None,
) -> FunctionTool:
    """Return a fresh chrys FunctionTool wrapper for a cached MCP function.

    Everything delivered outward goes through this clone so mutable invocation
    state stays isolated per consumer.
    """
    if not isinstance(source, FunctionTool):
        raise TypeError(f"Cached MCP tool {source!r} is not a FunctionTool")
    func = source.func
    additional_properties = dict(source.additional_properties or {})
    if result_cap is not None and result_cap > 0 and not additional_properties.get(_MCP_PROMPT_TOOL_KEY):

        @wraps(func)
        async def _bounded(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return await truncate_mcp_result(result, result_cap, spill_dir)

        clone_func = _bounded
    else:
        clone_func = func
    clone = FunctionTool(
        func=clone_func,
        name=source.name,
        description=source.description,
        max_invocations=source.max_invocations,
        max_invocation_exceptions=source.max_invocation_exceptions,
        additional_properties=additional_properties,
        input_model=source.parameters(),
        result_parser=source.result_parser,
    )
    set_tool_kind(clone, get_tool_kind(source) or KIND_MCP)
    # The additional_properties dict-copy above does not carry out-of-band
    # attributes — re-apply the provenance context like the kind.
    source_context = get_tool_context(source)
    if source_context is not None:
        set_tool_context(clone, source_context)
    cached_schema = source._input_schema_cached
    if cached_schema is not None:
        clone._input_schema_cached = cached_schema
        clone._cached_parameters = source._cached_parameters
    return clone


class MCPConnectionCache:
    """Ref-counted cache of live MCP server connections."""

    def __init__(self, *, idle_ttl_seconds: float = IDLE_TTL_SECONDS) -> None:
        self._idle_ttl_seconds = idle_ttl_seconds
        self._entries: dict[str, MCPConnectionEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._refresh: _RefreshRound | None = None
        # No longer handed out; each closes when its last lease is released.
        self._retired: set[MCPConnectionEntry] = set()
        # Cancelled connects still closing. A handshake that hangs holds its connection's
        # lifecycle until its timeout, so nothing waits for these but close_all().
        self._detached: set[asyncio.Task[Any]] = set()

    @property
    def closed(self) -> bool:
        """Whether ``close_all()`` has permanently closed this cache."""
        return self._closed

    @property
    def refreshing(self) -> bool:
        """Whether a refresh round is open, whose report names each failure."""
        return self._refresh is not None

    def begin_refresh(self) -> None:
        """Open a refresh round; the caller ends it with :meth:`commit_refresh` or :meth:`abort_refresh`."""
        if self._refresh is not None:
            raise RuntimeError("An MCP refresh is already open")
        self._refresh = _RefreshRound()

    def note_connect_failure(self, config: MCPServerConfig, error: BaseException) -> None:
        """Record that a consumer of the open round, if any, could not use *config*'s server."""
        if self._refresh is not None:
            self._refresh.failures[config.name] = (False, error)

    async def commit_refresh(self) -> tuple[MCPRefreshReport, ...]:
        """Make the round's connections current and retire what the rebuilt runtime no longer uses.

        Call once the rebuilt runtime is installed, before releasing the one it
        replaced: an old entry then closes with its last lease. An entry the round
        never asked for belongs to a server that was removed, disabled or
        reconfigured, so nothing may reconnect to it through this cache again.
        """
        round_ = self._refresh
        self._refresh = None
        if round_ is None:
            return ()
        self._detach_attempts(round_)
        stale: list[MCPTool] = []
        async with self._lock:
            for candidate in round_.candidates:
                candidate.refcount -= 1
                previous = self._entries.get(candidate.key)
                self._entries[candidate.key] = candidate
                if previous is not None:
                    stale.extend(self._retire_locked(previous))
            for entry in round_.unverified:
                stale.extend(self._retire_locked(entry))
            for key, entry in list(self._entries.items()):
                if key not in round_.requested:
                    stale.extend(self._retire_locked(entry))
        await self._close_untracked_tools(stale)
        return self._refresh_reports(round_)

    async def abort_refresh(self) -> None:
        """Close what the round opened and leave every connection the running runtime holds untouched."""
        round_ = self._refresh
        self._refresh = None
        if round_ is None:
            return
        self._detach_attempts(round_)
        stale: list[MCPTool] = []
        async with self._lock:
            for candidate in round_.candidates:
                candidate.refcount -= 1
                stale.extend(self._retire_locked(candidate))
            for key in round_.first_connections:
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if entry.connect_task is not None:
                    self._detach(entry.connect_task)
                stale.extend(self._retire_locked(entry))
        await self._close_untracked_tools(stale)

    def _detach_attempts(self, round_: _RefreshRound) -> None:
        for attempt in round_.attempts.values():
            self._detach(attempt)

    def _detach(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    def _retire_locked(self, entry: MCPConnectionEntry) -> list[MCPTool]:
        """Stop handing *entry* out; returns its tool when no lease still holds it."""
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]
        if entry.mcp_tool is None:
            return []
        if entry.refcount > 0:
            self._retired.add(entry)
            return []
        self._retired.discard(entry)
        return [entry.mcp_tool]

    @staticmethod
    def _refresh_reports(round_: _RefreshRound) -> tuple[MCPRefreshReport, ...]:
        reports = []
        for name, (kept_existing, error) in sorted(round_.failures.items()):
            detail = mcp_failure_diagnostic(error)
            if (problem := round_.probe_problems.get(name)) is not None:
                detail = f"{detail}; existing connection: {problem}"
            reports.append(MCPRefreshReport(name, kept_existing, detail))
        return tuple(reports)

    async def _refresh_connection(
        self, round_: _RefreshRound, entry: MCPConnectionEntry, effective: MCPEffectiveConfig
    ) -> MCPConnectionEntry:
        """Try one fresh connection beside *entry*, falling back to *entry* only while it proves healthy."""
        config = effective.config
        try:
            mcp_tool = await self._open_marked_tool(effective)
        except Exception as exc:
            logger.warning("MCP server '%s' did not reconnect: %s", config.name, exc)
            error = self._connection_error(config, exc)
            problem = (
                await entry.mcp_tool.probe_session(timeout=SESSION_PROBE_TIMEOUT_SECONDS)
                if entry.mcp_tool is not None
                else "the connection is closed"
            )
            if problem is None:
                round_.failures.setdefault(config.name, (True, error))
                return entry
            round_.probe_problems[config.name] = problem
            round_.unverified.append(entry)
            raise error from exc
        candidate = MCPConnectionEntry(
            key=entry.key,
            config=config,
            mcp_tool=mcp_tool,
            refcount=1,
            last_used=datetime.now(UTC),
            connect_task=None,
        )
        round_.candidates.append(candidate)
        return candidate

    async def acquire(self, config: MCPServerConfig, *, stdio_cwd: str | None = None) -> MCPConnectionLease:
        """Acquire a lease for ``config``, connecting once per cache key."""
        try:
            effective = resolve_mcp_effective_config(config, stdio_cwd=stdio_cwd)
        except Exception as exc:
            raise self._connection_error(config, exc) from exc
        key = effective.key

        lease: MCPConnectionLease | None = None
        task: asyncio.Task[MCPTool] | None = None
        attempt: asyncio.Task[MCPConnectionEntry] | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP connection cache is closed")
            stale_tools = self._collect_idle_locked(datetime.now(UTC), self._idle_ttl_seconds)

        await self._close_untracked_tools(stale_tools)

        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP connection cache is closed")
            entry = self._entries.get(key)
            round_ = self._refresh
            if round_ is not None:
                round_.requested.add(key)
                attempt = round_.attempts.get(key)
                # A connection this round opened itself is already fresh.
                refreshable = key not in round_.first_connections and entry is not None and entry.mcp_tool is not None
                if attempt is None and refreshable:
                    attempt = asyncio.create_task(self._refresh_connection(round_, entry, effective))
                    attempt.add_done_callback(_consume_task_exception)
                    round_.attempts[key] = attempt
                elif attempt is None:
                    round_.first_connections.add(key)
            if attempt is not None:
                pass
            elif entry is not None and entry.mcp_tool is not None:
                lease = self._lease_from_entry(entry)
                entry.refcount += 1
            else:
                if entry is None:
                    entry = MCPConnectionEntry(
                        key=key,
                        config=config,
                        mcp_tool=None,
                        refcount=0,
                        last_used=datetime.now(UTC),
                        connect_task=None,
                    )
                    self._entries[key] = entry
                if entry.connect_task is None:
                    entry.connect_task = asyncio.create_task(self._connect_and_store(key, effective))
                    entry.connect_task.add_done_callback(_consume_task_exception)
                task = entry.connect_task

        if attempt is not None:
            refreshed = await asyncio.shield(attempt)
            async with self._lock:
                if self._closed:
                    raise RuntimeError("MCP connection cache is closed")
                lease = self._lease_from_entry(refreshed)
                refreshed.refcount += 1
                return lease
        if lease is not None:
            return lease
        if task is None:
            raise RuntimeError(f"MCP server {config.name!r} did not start connecting")
        await asyncio.shield(task)

        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP connection cache is closed")
            entry = self._entries.get(key)
            if entry is None or entry.mcp_tool is None:
                raise RuntimeError(f"MCP server {config.name!r} did not register after connect")
            lease = self._lease_from_entry(entry)
            entry.refcount += 1
            return lease

    async def release(self, lease: MCPConnectionLease) -> None:
        """Release a previously acquired lease."""
        stale_tools: list[MCPTool] = []
        async with self._lock:
            entry = lease.entry
            if entry.refcount > 0:
                entry.refcount -= 1
                if entry.refcount == 0:
                    entry.last_used = datetime.now(UTC)
            stale_tools = self._collect_idle_locked(datetime.now(UTC), self._idle_ttl_seconds)
            if entry.refcount == 0 and entry in self._retired:
                stale_tools.extend(self._retire_locked(entry))
        await self._close_untracked_tools(stale_tools)

    async def prune_idle(self, *, max_idle_seconds: float | None = None) -> None:
        """Close idle entries that have exceeded ``max_idle_seconds``.

        This is intentionally lazy.  There is no background task; callers prune
        on acquire/release, and private adapter caches can pass ``0`` to close
        released entries immediately.
        """
        ttl = self._idle_ttl_seconds if max_idle_seconds is None else max_idle_seconds
        async with self._lock:
            stale_tools = self._collect_idle_locked(datetime.now(UTC), ttl)
        await self._close_untracked_tools(stale_tools)

    async def close_all(self) -> None:
        """Close every live MCP connection and reject future acquires."""
        round_ = self._refresh
        self._refresh = None
        if round_ is not None:
            self._detach_attempts(round_)
        async with self._lock:
            self._closed = True
            entries = [*self._entries.values(), *self._retired]
            if round_ is not None:
                entries.extend(round_.candidates)
            self._entries.clear()
            self._retired.clear()

        tasks: list[asyncio.Task[Any]] = [
            *self._detached,
            *(entry.connect_task for entry in entries if entry.connect_task is not None),
        ]
        tools = [entry.mcp_tool for entry in entries if entry.mcp_tool is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_untracked_tools(tools)

    async def _connect_and_store(self, key: str, effective: MCPEffectiveConfig) -> MCPTool:
        """Connect an MCP server and publish the result into the cache entry."""
        config = effective.config
        mcp_tool: MCPTool | None = None
        stored = False
        try:
            mcp_tool = await self._open_marked_tool(effective)
            try:
                async with self._lock:
                    entry = self._entries.get(key)
                    current_task = asyncio.current_task()
                    if self._closed or entry is None or entry.connect_task is not current_task:
                        return mcp_tool
                    entry.mcp_tool = mcp_tool
                    entry.connect_task = None
                    entry.last_used = datetime.now(UTC)
                    stored = True
                    return mcp_tool
            finally:
                if not stored:
                    await self._close_tool(config.name, mcp_tool)
        except asyncio.CancelledError:
            if mcp_tool is not None and not stored:
                await self._close_tool(config.name, mcp_tool)
            raise
        except Exception:
            async with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.connect_task is asyncio.current_task():
                    self._entries.pop(key, None)
            raise

    async def _open_marked_tool(self, effective: MCPEffectiveConfig) -> MCPTool:
        mcp_tool = await self._open_mcp_tool(effective)
        for tool in list(mcp_tool.functions):
            set_tool_kind(tool, KIND_MCP)
        return mcp_tool

    async def _open_mcp_tool(self, effective: MCPEffectiveConfig) -> MCPTool:
        """Create and enter one live MCPTool, wrapping failures like MCPAdapter."""
        config = effective.config
        mcp_tool: MCPTool | None = None
        try:
            mcp_tool = _connection._create_tool_with_timeout_floor(
                config,
                _connection.DEFAULT_CONNECT_TIMEOUT_SECONDS,
                resolved_stdio_env=effective.stdio_env,
                stdio_cwd=effective.resolved_stdio_cwd,
                resolved_http_headers=effective.http_headers,
            )
            await mcp_tool.__aenter__()
        except asyncio.CancelledError:
            if mcp_tool is not None:
                with contextlib.suppress(Exception):
                    await mcp_tool.__aexit__(None, None, None)
            raise
        except MCPConnectionError:
            if mcp_tool is not None:
                with contextlib.suppress(Exception):
                    await mcp_tool.__aexit__(None, None, None)
            raise
        except Exception as exc:
            if mcp_tool is not None:
                with contextlib.suppress(Exception):
                    await mcp_tool.__aexit__(None, None, None)
            diagnostics = _connection._stdio_failure_diagnostics_from(mcp_tool)
            cause: BaseException = exc
            if _connection._is_timeout_cause(exc):
                timeout = (
                    mcp_tool.request_timeout if mcp_tool is not None else _connection.DEFAULT_CONNECT_TIMEOUT_SECONDS
                )
                cause = TimeoutError(f"connection did not complete within {timeout}s (server may not be speaking MCP)")
            raise MCPConnectionError(
                config.name,
                config.transport,
                cause,
                banner_lines=_connection._banner_lines_from(mcp_tool),
                stderr_tail=diagnostics.stderr_tail,
                stderr_dropped_bytes=diagnostics.stderr_dropped_bytes,
                process_exit_code=diagnostics.process_exit_code,
                resolved_executable=diagnostics.resolved_executable,
                effective_cwd=diagnostics.effective_cwd,
            ) from exc
        if mcp_tool is None:
            raise MCPConnectionError(config.name, config.transport)
        return mcp_tool

    def _lease_from_entry(self, entry: MCPConnectionEntry) -> MCPConnectionLease:
        if entry.mcp_tool is None:
            raise RuntimeError(f"MCP server {entry.config.name!r} is not connected")
        functions = [clone_mcp_function_tool(tool) for tool in entry.mcp_tool.functions]
        return MCPConnectionLease(
            key=entry.key,
            server_name=entry.config.name,
            config=entry.config,
            functions=functions,
            mcp_tool=entry.mcp_tool,
            entry=entry,
        )

    def _collect_idle_locked(self, now: datetime, max_idle_seconds: float) -> list[MCPTool]:
        stale: list[MCPTool] = []
        for key, entry in list(self._entries.items()):
            if entry.refcount > 0 or entry.mcp_tool is None:
                continue
            idle_seconds = (now - entry.last_used).total_seconds()
            if idle_seconds < max_idle_seconds:
                continue
            self._entries.pop(key, None)
            stale.append(entry.mcp_tool)
        return stale

    async def _close_tools(self, tools: Sequence[MCPTool | None]) -> None:
        if not tools:
            return
        await asyncio.gather(
            *(self._close_tool(getattr(tool, "name", "<unknown>"), tool) for tool in tools if tool is not None),
            return_exceptions=True,
        )

    async def _close_untracked_tools(self, tools: Sequence[MCPTool | None]) -> None:
        """Close tools that have already been removed from cache tracking.

        Once entries are removed from ``_entries``, ``close_all()`` cannot find
        them.  Shield the close task so caller cancellation does not cancel the
        actual transport cleanup, then wait for cleanup before re-raising.
        """
        if not tools:
            return
        close_task = asyncio.create_task(self._close_tools(tools))
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await close_task
            raise

    async def _close_tool(self, name: str, tool: MCPTool) -> None:
        try:
            await tool.__aexit__(None, None, None)
        except Exception:
            logger.exception("Error disconnecting cached MCP server '%s'", name)

    def _connection_error(self, config: MCPServerConfig, exc: Exception) -> Exception:
        if isinstance(exc, MCPConnectionError):
            return exc
        return MCPConnectionError(config.name, config.transport, exc)
