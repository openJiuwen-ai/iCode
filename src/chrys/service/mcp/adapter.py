# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP adapter — wraps MCP server tools as Chrys FunctionTools.

Uses Chrys-owned MCPStdioTool and MCPStreamableHTTPTool implementations to
connect to MCP servers and expose their tools as FunctionTool instances.

Failure model
-------------
``connect()`` raises :class:`MCPConnectionError` on failure; ``connect_all()``
catches ordinary per-server connection failures so one unavailable server
cannot block the agent build. Invalid or colliding tool names are configuration
errors and fail closed through :class:`MCPToolConfigurationError`; otherwise
the provider may reject the request or Chrys's last-wins tool map could execute
the wrong function. Ordinary failures are published to the ``EventBus`` as
``Warning`` events (``code=
"mcp.connect_failed"``) and retained on :attr:`MCPAdapter.failures`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import re
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from html import escape as xml_escape
from typing import TYPE_CHECKING, Any

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.tool_call_context import get_tool_context
from chrys.kernel.tools import FunctionTool
from chrys.service.mcp import _connection
from chrys.service.mcp._connection import DEFAULT_CONNECT_TIMEOUT_SECONDS as DEFAULT_CONNECT_TIMEOUT_SECONDS
from chrys.service.mcp._progressive import ProgressiveMCPResumeProvider, _ProgressiveMCPExposure
from chrys.service.mcp._stdio_transport import MAX_BANNER_LINE_CHARS as MAX_BANNER_LINE_CHARS
from chrys.service.mcp._stdio_transport import MAX_BANNER_LINES_CAPTURED as MAX_BANNER_LINES_CAPTURED
from chrys.service.mcp._stdio_transport import MAX_STDIO_PATH_DISPLAY_CHARS as MAX_STDIO_PATH_DISPLAY_CHARS
from chrys.service.mcp._stdio_transport import MAX_STDIO_STDERR_BYTES_CAPTURED as MAX_STDIO_STDERR_BYTES_CAPTURED
from chrys.service.mcp._stdio_transport import MAX_STDIO_STDERR_PREVIEW_LINES as MAX_STDIO_STDERR_PREVIEW_LINES
from chrys.service.mcp._stdio_transport import _StdioFailureDiagnostics
from chrys.service.mcp._stdio_transport import tolerant_stdio_client as tolerant_stdio_client
from chrys.service.mcp.cache import MCPConnectionCache, MCPConnectionLease, clone_mcp_function_tool
from chrys.service.mcp.errors import (
    MCPConnectionError,
    MCPToolConfigurationError,
    MCPToolNameAmbiguityError,
    MCPToolNameCollisionError,
    MCPToolNameValidationError,
)
from chrys.service.mcp.owned import _MCP_REMOTE_NAME_KEY, _mcp_config_names_for
from chrys.service.mcp.result_limits import (
    DEFAULT_MCP_TOOL_RESULT_MAX_TOKENS as DEFAULT_MCP_TOOL_RESULT_MAX_TOKENS,
)
from chrys.service.mcp.result_limits import resolve_mcp_result_cap
from chrys.service.mcp.validation import validate_mcp_exposed_tool_name

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.events.bus import EventBus
    from chrys.service.mcp.owned import MCPTool
    from chrys.service.profiles.agents.schema import MCPServerConfig

logger = logging.getLogger(__name__)

# Hard timeouts that bound how long a misbehaving MCP server can hold up the
# UI / agent build.  The MCP SDK's per-request timeout
# (``read_timeout_seconds``) defaults to ``None`` (wait forever), and a
# server that emits welcome banner text instead of a valid ``initialize``
# response will leave the client blocked indefinitely on
# ``response_stream_reader.receive()``.  These floors are injected into
# the MCP tool's ``request_timeout`` when the user didn't set one, so the
# SDK's own timeout fires from *inside* MCPTool's lifecycle-owner
# task.  We intentionally do **not** wrap ``__aenter__`` in
# ``asyncio.wait_for`` from the outside: the lifecycle owner is a
# separate ``asyncio.Task`` (see ``MCPTool._run_lifecycle_owner``) which
# ``wait_for`` cannot cancel safely (anyio cancel scopes are scoped per
# task and cross-task cancellation raises "Attempted to exit cancel scope
# in a different task").  ``MCPServerConfig.request_timeout`` (when set)
# always takes precedence — these are only the defaults applied when no
# per-server timeout is configured.
DEFAULT_TEST_TIMEOUT_SECONDS = 30

# Cap per-server InitializeResult.instructions rendered into the
# <mcp_instructions> reminder.  The value is server-controlled and repeated
# on every LLM call, so it must stay bounded no matter what the server
# sends.  The budget is charged against the *rendered* (escaped + indented)
# text — capping the raw input instead would let escape expansion multiply
# the cap (e.g. every '"' becomes '&quot;').  4k rendered characters fits
# real-world server instructions comfortably; the aggregate is bounded by
# the number of user-configured servers.
MCP_INSTRUCTIONS_CHAR_LIMIT = 4_000

# A trailing entity fragment left behind when a rendered line is cut at the
# budget boundary (e.g. '&quo').  Complete entities end in ';' and don't match.
_SEVERED_ENTITY_TAIL_RE = re.compile(r"&[#0-9A-Za-z]{0,9}$")


@dataclasses.dataclass(frozen=True)
class MCPTestReport:
    """What a successful one-shot connection test learned about a server.

    Every string field is server-controlled: renderers must sanitize each
    one against markup/structure injection before display. ``capabilities``
    holds dotted feature names the server advertised (``tools``,
    ``tools.listChanged``, …) flattened from ``ServerCapabilities``.
    ``initial_tool_names`` is the progressive-disclosure starting surface,
    or ``None`` when progressive disclosure is off.
    """

    server_name: str | None
    server_title: str | None
    server_version: str | None
    protocol_version: str | None
    capabilities: tuple[str, ...]
    instructions: str | None
    tools: tuple[tuple[str, str], ...]
    prompts: tuple[tuple[str, str], ...]
    initial_tool_names: tuple[str, ...] | None


# Standard MCP server capability groups (the spec's named feature sets).
# The report renderer always shows these; the flattener records them ahead
# of extras so the entry cap can never make a declared one look
# unadvertised.
STANDARD_CAPABILITY_GROUPS = ("completions", "logging", "prompts", "resources", "tools")

# Bounds for flattening server-controlled capability trees: a hostile
# server could otherwise inflate the report with unbounded extra keys or
# deep nesting (``ServerCapabilities`` accepts extras).
_MAX_CAPABILITY_ENTRIES = 128
_MAX_CAPABILITY_DEPTH = 4


def _top_capability_order(key: object) -> tuple[bool, str]:
    return (str(key) not in STANDARD_CAPABILITY_GROUPS, str(key))


def _flatten_server_capabilities(capabilities: Any) -> tuple[str, ...]:
    """Flatten advertised ``ServerCapabilities`` into sorted dotted feature names.

    Walks nested capability objects (e.g. ``tasks.requests.tools``):
    ``False`` leaves are omitted, any other advertised value marks presence.
    Traversal is breadth-first with standard groups first at the top level,
    so the entry cap only ever drops the deepest/extra branches — a declared
    standard capability is always recorded.
    """
    if capabilities is None:
        return ()
    try:
        dumped = capabilities.model_dump(exclude_none=True)
    except Exception:
        return ()
    if not isinstance(dumped, dict):
        return ()
    flattened: list[str] = []
    queue: deque[tuple[str, dict[Any, Any], int]] = deque([("", dumped, 1)])
    while queue:
        prefix, mapping, depth = queue.popleft()
        for key in sorted(mapping, key=_top_capability_order if depth == 1 else str):
            if len(flattened) >= _MAX_CAPABILITY_ENTRIES:
                return tuple(sorted(flattened))
            value = mapping[key]
            if value is False or value is None:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.append(path)
            if isinstance(value, dict) and depth < _MAX_CAPABILITY_DEPTH:
                queue.append((path, value, depth + 1))
    return tuple(sorted(flattened))


def _mcp_remote_name(tool: FunctionTool) -> str:
    remote_name = (tool.additional_properties or {}).get(_MCP_REMOTE_NAME_KEY)
    return remote_name if isinstance(remote_name, str) else tool.name


def _sole_config_name(tool: FunctionTool, catalog: list[FunctionTool]) -> str | None:
    """Return the first ``allowed_tools``/``always_load`` name that selects *tool* and no other catalog tool."""
    others = [other for other in catalog if other is not tool]
    return next(
        (
            name
            for name in _mcp_config_names_for(tool)
            if all(name not in _mcp_config_names_for(other) for other in others)
        ),
        None,
    )


class MCPAdapter:
    """Manages MCP server connections and exposes their tools.

    Supports stdio and HTTP (Streamable HTTP/SSE) transports as configured
    in the agent profile YAML.

    An ordinary per-server connection failure does not abort the agent build.
    Deterministic tool-name collisions do abort it before any ambiguous tool
    surface can reach the model. Ordinary failures are:

    * published on the optional ``EventBus`` as ``Warning`` events
      (``code="mcp.connect_failed"``) so the TUI can display them, and
    * retained on :attr:`failures` for inspection by diagnostic UIs.
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        session_id: str | None = None,
        *,
        cache: MCPConnectionCache | None = None,
        stdio_cwd: str | None = None,
        session_dir: Path | None = None,
        reserved_tool_names: Collection[str] = (),
    ) -> None:
        self._cache = cache or MCPConnectionCache()
        self._owns_cache = cache is None
        self._leases: dict[str, MCPConnectionLease] = {}
        # Compatibility/debug view of this adapter's acquired underlying
        # MCPTool objects.  Ownership still flows through ``_leases``.
        self._servers: dict[str, MCPTool] = {}
        self._functions_by_server: dict[str, list[FunctionTool]] = {}
        # Per-server ``expose_instructions`` config, captured at registration.
        # Adapter-level (not on the shared MCPTool): profiles sharing a cached
        # connection may disagree on exposure.
        self._instructions_exposure: dict[str, bool] = {}
        self._progressive_exposures: dict[str, _ProgressiveMCPExposure] = {}
        self._tool_namespace_by_server: dict[str, set[str]] = {}
        self._connecting: dict[str, asyncio.Task[list[Any]]] = {}
        self._failures: dict[str, MCPConnectionError] = {}
        self._tool_names_by_server: dict[str, list[str]] = {}
        self._bus = bus
        self._session_id = session_id
        self._stdio_cwd = stdio_cwd
        self._session_dir = session_dir
        self._reserved_tool_names = frozenset(reserved_tool_names)
        # Serializes the registration critical section shared by ``connect``
        # and ``disconnect``/``disconnect_all``.  Without this, a ``disconnect_all``
        # that snapshots ``_servers`` while a concurrent ``connect`` is mid-
        # ``__aenter__`` would miss the about-to-be-registered server, leaving
        # an orphaned subprocess.  Tool ``__aexit__`` calls are intentionally
        # performed outside the lock so parallel disconnect is preserved.
        self._lock = asyncio.Lock()

    def set_reserved_tool_names(self, reserved_tool_names: Collection[str]) -> None:
        """Replace names reserved from MCP namespace validation.

        This is primarily used by configuration diagnostics whose surrounding
        agent draft can change while the test panel remains mounted. Active
        production adapters keep a stable namespace for their acquired leases.
        """
        if self._leases or self._connecting:
            raise RuntimeError("Cannot replace reserved MCP tool names while connections are active.")
        self._reserved_tool_names = frozenset(reserved_tool_names)

    @staticmethod
    def _create_tool_with_timeout_floor(config: MCPServerConfig, default_timeout: int) -> Any:
        """Build the MCP tool, injecting the timeout floor when none is configured."""
        return _connection._create_tool_with_timeout_floor(config, default_timeout)

    @staticmethod
    def _banner_lines_from(tool: Any) -> list[str]:
        """Extract banner diagnostics from a stdio tool, empty list otherwise."""
        return _connection._banner_lines_from(tool)

    @staticmethod
    def _stdio_failure_diagnostics_from(tool: Any) -> _StdioFailureDiagnostics:
        """Snapshot bounded stdio subprocess diagnostics, empty otherwise."""
        return _connection._stdio_failure_diagnostics_from(tool)

    @staticmethod
    def _is_timeout_cause(exc: BaseException) -> bool:
        """True if the exception chain contains an MCP/anyio read-timeout signal."""
        return _connection._is_timeout_cause(exc)

    def create_resume_provider(self) -> ProgressiveMCPResumeProvider | None:
        """Return the per-run refresh/resume provider when progressive servers exist."""
        if not self._progressive_exposures:
            return None
        return ProgressiveMCPResumeProvider(self)

    def get_server_instructions_map(self) -> dict[str, str]:
        """Return {server_name: instructions} for connected servers with instructions.

        Servers whose config disabled ``expose_instructions`` are excluded.
        """
        result: dict[str, str] = {}
        for name, server in self._servers.items():
            if not self._instructions_exposure.get(name, True):
                continue
            instructions = getattr(server, "_server_instructions", None)
            if instructions:
                result[name] = instructions
        for name, lease in self._leases.items():
            if name not in result and self._instructions_exposure.get(name, True):
                instructions = getattr(lease.mcp_tool, "_server_instructions", None)
                if instructions:
                    result[name] = instructions
        return result

    def render_instructions_reminder(self) -> str | None:
        """Return an ``<mcp_instructions>`` block with all server instructions, or None.

        Servers render in name order: ``connect_all`` registers concurrently,
        so insertion order varies run to run and would leak that
        nondeterminism into the prompt text.  Per-server text is capped at
        ``MCP_INSTRUCTIONS_CHAR_LIMIT``: instructions are server-controlled
        and injected into every LLM call, so an unbounded value from one
        misbehaving server could push every request past the context limit.
        """
        instructions_map = self.get_server_instructions_map()
        if not instructions_map:
            return None
        lines = ["<mcp_instructions>"]
        for name in sorted(instructions_map):
            escaped_name = xml_escape(name, quote=True)
            lines.append(f'  <server name="{escaped_name}">')
            budget = MCP_INSTRUCTIONS_CHAR_LIMIT
            truncated = False
            for line in instructions_map[name].strip().splitlines():
                rendered = f"    {xml_escape(line)}".rstrip()
                # +1 charges the joining newline so a blank-line flood
                # (zero-length rendered lines) cannot bypass the budget.
                cost = len(rendered) + 1
                if cost > budget:
                    if budget > 1:
                        lines.append(_SEVERED_ENTITY_TAIL_RE.sub("", rendered[: budget - 1]))
                    truncated = True
                    break
                lines.append(rendered)
                budget -= cost
            if truncated:
                lines.append(f"    [instructions truncated: exceeded {MCP_INSTRUCTIONS_CHAR_LIMIT} characters]")
            lines.append("  </server>")
        lines.append("</mcp_instructions>")
        return "\n".join(lines)

    def _progressive_surface_tools(self) -> list[FunctionTool]:
        """Return current controls and always-loaded tools for a fresh run."""
        return [tool for exposure in self._progressive_exposures.values() for tool in exposure.current_surface_tools]

    def _validate_server_namespace(
        self,
        config: MCPServerConfig,
        catalog: list[FunctionTool],
        control_names: set[str],
    ) -> None:
        """Reject invalid or colliding names before tools reach the model."""
        violations: list[str] = []
        for tool in catalog:
            error = validate_mcp_exposed_tool_name(tool.name)
            if error is None:
                continue
            additional = tool.additional_properties or {}
            remote_name = additional.get(_MCP_REMOTE_NAME_KEY)
            origin = f" Remote catalog name: {remote_name!r}." if isinstance(remote_name, str) else ""
            violations.append(f"{error}{origin}")
        violations.extend(
            f"{error} Generated progressive-disclosure control."
            for control_name in sorted(control_names)
            if (error := validate_mcp_exposed_tool_name(control_name)) is not None
        )
        if violations:
            raise MCPToolNameValidationError(
                config.name,
                config.transport,
                violations=violations,
            )

        catalog_names = [tool.name for tool in catalog]
        duplicate_catalog_names = {name for name, count in Counter(catalog_names).items() if count > 1}
        if duplicate_catalog_names:
            raise MCPToolNameCollisionError(
                config.name,
                config.transport,
                conflicting_names=duplicate_catalog_names,
                conflict_with=f"another permitted tool from MCP server {config.name!r}",
                guidance=(
                    "Rename one of the remote entries, exclude the shared name from the Permitted Tool Set, "
                    "or disable 'Expose server prompts' when the duplicate is a prompt."
                ),
            )

        # One configured name can be one tool's original name and another's
        # prefixed name; both would pass the allowlist. Every tool a name
        # selects survives that filter, so the permitted catalog shows it.
        configured_names = dict.fromkeys([*(config.allowed_tools or ()), *config.always_load])
        matches_by_name = {
            name: [(_mcp_remote_name(tool), _sole_config_name(tool, catalog)) for tool in matches]
            for name in configured_names
            if len(matches := [tool for tool in catalog if name in _mcp_config_names_for(tool)]) > 1
        }
        if matches_by_name:
            raise MCPToolNameAmbiguityError(config.name, config.transport, matches_by_name=matches_by_name)

        catalog_name_set = set(catalog_names)
        if control_collisions := catalog_name_set & control_names:
            raise MCPToolNameCollisionError(
                config.name,
                config.transport,
                conflicting_names=control_collisions,
                conflict_with="a generated progressive-disclosure control",
                guidance=(
                    "Exclude the remote tool, choose a non-conflicting Tool Name Prefix, "
                    "or disable progressive loading."
                ),
            )

        namespace = catalog_name_set | control_names
        if chrys_collisions := namespace & self._reserved_tool_names:
            raise MCPToolNameCollisionError(
                config.name,
                config.transport,
                conflicting_names=chrys_collisions,
                conflict_with=f"a reserved {APP_DISPLAY_NAME} tool",
                guidance="Exclude the remote tool from the Permitted Tool Set or configure a Tool Name Prefix.",
            )

        for other_server, other_namespace in self._tool_namespace_by_server.items():
            if other_server == config.name:
                continue
            if cross_server_collisions := namespace & other_namespace:
                raise MCPToolNameCollisionError(
                    config.name,
                    config.transport,
                    conflicting_names=cross_server_collisions,
                    conflict_with=f"MCP server {other_server!r}",
                    guidance="Configure distinct Tool Name Prefix values or exclude the duplicate tool.",
                )

        if config.name in self._leases:
            self._tool_namespace_by_server[config.name] = namespace

    def _current_functions_for_server(
        self,
        name: str,
        lease: MCPConnectionLease,
    ) -> list[FunctionTool]:
        """Return a fresh progressive surface or the ordinary cached functions."""
        exposure = self._progressive_exposures.get(name)
        if exposure is not None:
            return exposure.initial_tools
        return list(self._functions_by_server.get(name, lease.functions))

    def _context_tools_without_collisions(
        self,
        visible_tools: list[Any],
        candidates: list[FunctionTool],
    ) -> list[FunctionTool]:
        """Return missing candidates, rejecting foreign same-name tools loudly."""
        visible_by_name: dict[str, list[Any]] = {}
        for tool in visible_tools:
            if isinstance(tool, FunctionTool):
                visible_by_name.setdefault(tool.name, []).append(tool)
                continue
            if isinstance(tool, Mapping):
                function = tool.get("function")
                if isinstance(function, Mapping) and isinstance(function.get("name"), str):
                    visible_by_name.setdefault(function["name"], []).append(tool)
        additions: list[FunctionTool] = []
        for candidate in candidates:
            existing = visible_by_name.get(candidate.name, [])
            if not existing:
                additions.append(candidate)
                visible_by_name[candidate.name] = [candidate]
                continue

            provenance = get_tool_context(candidate)
            server_name = provenance.get("server_name") if provenance is not None else None
            exposure = self._progressive_exposures.get(server_name) if isinstance(server_name, str) else None
            if (
                len(existing) == 1
                and exposure is not None
                and isinstance(existing[0], FunctionTool)
                and exposure._same_surface_identity(existing[0], candidate)
            ):
                continue
            config = exposure._config if exposure is not None else None
            raise MCPToolNameCollisionError(
                config.name if config is not None else server_name or "unknown",
                config.transport if config is not None else "unknown",
                conflicting_names={candidate.name},
                conflict_with="another visible tool contributed to this run",
                guidance="Configure a distinct Tool Name Prefix or remove the conflicting tool.",
            )
        return additions

    def _resume_tools(self, calls: list[tuple[str, str | None]]) -> list[FunctionTool]:
        """Clone uniquely resolved hidden MCP tools referenced by pending calls."""
        tools: list[FunctionTool] = []
        added_names: set[str] = set()
        for name, server_name in calls:
            if name in added_names:
                continue
            if server_name is not None:
                exposure = self._progressive_exposures.get(server_name)
                source = exposure.resume_source(name) if exposure is not None else None
                matches = [source] if source is not None else []
            else:
                matches = [
                    source
                    for exposure in self._progressive_exposures.values()
                    if (source := exposure.resume_source(name)) is not None
                ]
            if len(matches) == 1:
                match_context = get_tool_context(matches[0])
                matched_server = (
                    server_name
                    if server_name is not None
                    else match_context.get("server_name")
                    if match_context is not None
                    else None
                )
                if (
                    isinstance(matched_server, str)
                    and (exposure := self._progressive_exposures.get(matched_server)) is not None
                ):
                    tools.append(exposure.clone_source(matches[0]))
                else:
                    tools.append(clone_mcp_function_tool(matches[0]))
                added_names.add(name)
            elif len(matches) > 1:
                logger.warning(
                    "Cannot resume ambiguous progressive MCP tool %r; configure distinct tool_name_prefix values.",
                    name,
                )
        return tools

    async def connect(self, config: MCPServerConfig) -> list[Any]:
        """Connect to an MCP server and return its tools as FunctionTools.

        Idempotent: calling with a previously-connected name returns the
        cached tool list.  Bounded by a hard timeout floor
        (``DEFAULT_CONNECT_TIMEOUT_SECONDS``) injected into the SDK's
        ``request_timeout`` when the user didn't configure one — so a
        hung server cannot block the agent build.

        Raises:
            MCPConnectionError: If the server fails to connect, initialize,
                or exceeds the connect timeout.  ``asyncio.CancelledError``
                is propagated unchanged after the partially-opened transport
                is closed.
        """
        # Validate before cache lookup as well as connection creation: a cache
        # hit must not let malformed wrapper-only progressive fields bypass the
        # checks performed by ``_create_mcp_tool`` on a cold connection.
        _connection._validate_config(config)
        async with self._lock:
            lease = self._leases.get(config.name)
            if lease is not None:
                return self._current_functions_for_server(config.name, lease)
            task = self._connecting.get(config.name)
            if task is None:
                task = asyncio.create_task(self._acquire_new(config))
                self._connecting[config.name] = task

                def _discard_finished(done: asyncio.Task[list[Any]], name: str = config.name) -> None:
                    if self._connecting.get(name) is done:
                        self._connecting.pop(name, None)

                task.add_done_callback(_discard_finished)

        try:
            return await task
        finally:
            if task.done():
                async with self._lock:
                    if self._connecting.get(config.name) is task:
                        self._connecting.pop(config.name, None)

    async def _connect_new(self, config: MCPServerConfig) -> list[Any]:
        """Open or lease a new MCP server connection and register it on success."""
        current_task = asyncio.current_task()
        inserted = False
        if current_task is not None:
            async with self._lock:
                if config.name not in self._connecting:
                    self._connecting[config.name] = current_task
                    inserted = True
        try:
            return await self._acquire_new(config)
        finally:
            if inserted:
                async with self._lock:
                    if self._connecting.get(config.name) is current_task:
                        self._connecting.pop(config.name, None)

    async def _acquire_new(self, config: MCPServerConfig) -> list[Any]:
        """Acquire a cache lease and register it on this adapter."""
        lease: MCPConnectionLease | None = None
        duplicate_tools: list[Any] | None = None
        release_duplicate = False
        prune_private_cache = False
        registered = False
        exposure: _ProgressiveMCPExposure | None = None
        delivered_tools: list[FunctionTool]
        catalog: list[FunctionTool]
        control_names: set[str] = set()
        namespace: set[str]

        try:
            lease = await self._cache.acquire(config, stdio_cwd=self._stdio_cwd)
        except MCPConnectionError as err:
            self._failures[config.name] = err
            raise
        if lease is None:
            raise RuntimeError("Acquiring an MCP connection did not return a lease.")

        try:
            catalog = [tool for tool in lease.mcp_tool.functions if isinstance(tool, FunctionTool)]
            result_cap = resolve_mcp_result_cap(config.max_tool_result_tokens)
            if config.use_progressive_disclosure:
                exposure = _ProgressiveMCPExposure(
                    config,
                    lease.mcp_tool,
                    self._validate_server_namespace,
                    result_cap=result_cap,
                    spill_dir=self._session_dir,
                )
                control_names = exposure.control_names
                delivered_tools = exposure.initial_tools
            else:
                self._validate_server_namespace(config, catalog, control_names)
                delivered_tools = [
                    clone_mcp_function_tool(source, result_cap=result_cap, spill_dir=self._session_dir)
                    for source in catalog
                ]
            namespace = {tool.name for tool in catalog} | control_names
        except Exception:
            await self._release_unregistered_lease(config.name, lease)
            raise

        try:
            async with self._lock:
                existing = self._leases.get(config.name)
                if existing is not None:
                    duplicate_tools = self._current_functions_for_server(config.name, existing)
                    release_duplicate = True
                elif (existing_server := self._servers.get(config.name)) is not None:
                    # existing_server.functions belong to the owned MCP engine.
                    # Every outward path must deliver Chrys FunctionTool clones
                    # so each acquisition keeps its own tool state and result cap.
                    result_cap = resolve_mcp_result_cap(config.max_tool_result_tokens)
                    duplicate_tools = [
                        clone_mcp_function_tool(t, result_cap=result_cap, spill_dir=self._session_dir)
                        for t in existing_server.functions
                    ]
                    release_duplicate = True
                    prune_private_cache = self._owns_cache
                else:
                    current_task = asyncio.current_task()
                    if self._connecting.get(config.name) is not current_task:
                        raise asyncio.CancelledError
                    # Re-check under the registration lock so parallel server
                    # connects cannot both pass against an empty namespace.
                    self._validate_server_namespace(config, catalog, control_names)
                    self._leases[config.name] = lease
                    self._servers[config.name] = lease.mcp_tool
                    self._functions_by_server[config.name] = delivered_tools
                    self._instructions_exposure[config.name] = config.expose_instructions
                    self._tool_namespace_by_server[config.name] = namespace
                    if exposure is not None:
                        self._progressive_exposures[config.name] = exposure
                    registered = True
        except BaseException:
            if lease is not None and not registered:
                await self._release_unregistered_lease(config.name, lease)
            raise

        if release_duplicate:
            await self._release_unregistered_lease(config.name, lease)
            if prune_private_cache:
                await self._cache.prune_idle(max_idle_seconds=0)
            if duplicate_tools is None:
                raise RuntimeError("A duplicate MCP registration has no registered tools.")
            return duplicate_tools

        self._failures.pop(config.name, None)
        if exposure is not None and (missing := exposure.unmatched_always_load_names):
            missing_names = ", ".join(missing)
            await self._publish_warning(
                code="mcp.always_load_missing",
                message=(
                    f"MCP server '{config.name}' did not advertise configured initially visible "
                    f"tool(s): {missing_names}. They will be picked up automatically if advertised later."
                ),
            )
        logger.debug(
            "Connected to MCP server '%s' (%s): %d tools loaded",
            config.name,
            config.transport,
            len(lease.functions),
        )
        return list(delivered_tools)

    async def _release_unregistered_lease(self, name: str, lease: MCPConnectionLease) -> None:
        try:
            await self._finish_cleanup_on_cancel(self._cache.release(lease))
        except Exception:
            logger.exception("Error releasing duplicate MCP lease for '%s'", name)
            raise

    async def _finish_cleanup_on_cancel(self, awaitable: Awaitable[Any]) -> None:
        """Let cleanup finish before propagating caller cancellation."""
        cleanup = asyncio.ensure_future(awaitable)
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await cleanup
            raise

    async def test_connection(self, config: MCPServerConfig) -> MCPTestReport:
        """Attempt a one-shot connection test for an MCP server.

        Establishes the server connection, snapshots what it advertised
        (identity, capabilities, instructions, tool/prompt catalog) into an
        :class:`MCPTestReport`, and immediately closes it.  Bounded by
        ``DEFAULT_TEST_TIMEOUT_SECONDS`` injected into the SDK's
        ``request_timeout`` when none is configured — so the Test button
        can never hang indefinitely.

        Raises:
            MCPConnectionError: If the server fails to connect, initialize,
                or exceeds the test timeout.  Carries any captured stdout
                banner lines, bounded stderr tail, and child exit code so the
                UI can show why a misbehaving server failed.
        """
        mcp_tool: Any | None = None
        try:
            try:
                mcp_tool = self._create_tool_with_timeout_floor(config, DEFAULT_TEST_TIMEOUT_SECONDS)
                await mcp_tool.__aenter__()
                catalog = [tool for tool in mcp_tool.functions if isinstance(tool, FunctionTool)]
                initial_tool_names: tuple[str, ...] | None = None
                if config.use_progressive_disclosure:
                    exposure = _ProgressiveMCPExposure(config, mcp_tool, self._validate_server_namespace)
                    # Materializing the surface validates both catalog names
                    # and generated controls through the production path.
                    initial_tool_names = tuple(tool.name for tool in exposure.initial_tools)
                else:
                    self._validate_server_namespace(config, catalog, set())
                return self._build_test_report(mcp_tool, catalog, initial_tool_names)
            except MCPConnectionError:
                raise
            except Exception as exc:
                diagnostics = self._stdio_failure_diagnostics_from(mcp_tool)
                cause: BaseException = exc
                if self._is_timeout_cause(exc):
                    timeout = getattr(mcp_tool, "request_timeout", DEFAULT_TEST_TIMEOUT_SECONDS)
                    cause = TimeoutError(
                        f"connection did not complete within {timeout}s (server may not be speaking MCP)"
                    )
                raise MCPConnectionError(
                    config.name,
                    config.transport,
                    cause,
                    banner_lines=self._banner_lines_from(mcp_tool),
                    stderr_tail=diagnostics.stderr_tail,
                    stderr_dropped_bytes=diagnostics.stderr_dropped_bytes,
                    process_exit_code=diagnostics.process_exit_code,
                    resolved_executable=diagnostics.resolved_executable,
                    effective_cwd=diagnostics.effective_cwd,
                ) from exc
        finally:
            if mcp_tool is not None:
                with contextlib.suppress(Exception):
                    await mcp_tool.__aexit__(None, None, None)

    @staticmethod
    def _build_test_report(
        mcp_tool: Any,
        catalog: list[FunctionTool],
        initial_tool_names: tuple[str, ...] | None,
    ) -> MCPTestReport:
        """Snapshot server-advertised state before the test connection closes."""
        server_info = getattr(mcp_tool, "_server_info", None)
        prompt_remote_names = getattr(mcp_tool, "_loaded_prompt_remote_names", None) or set()
        tools: list[tuple[str, str]] = []
        prompts: list[tuple[str, str]] = []
        for tool in catalog:
            additional = tool.additional_properties or {}
            remote_name = additional.get(_MCP_REMOTE_NAME_KEY)
            entry = (tool.name, tool.description or "")
            if isinstance(remote_name, str) and remote_name in prompt_remote_names:
                prompts.append(entry)
            else:
                tools.append(entry)
        return MCPTestReport(
            server_name=getattr(server_info, "name", None),
            server_title=getattr(server_info, "title", None),
            server_version=getattr(server_info, "version", None),
            protocol_version=getattr(mcp_tool, "_protocol_version", None),
            capabilities=_flatten_server_capabilities(getattr(mcp_tool, "_server_capabilities", None)),
            instructions=getattr(mcp_tool, "_server_instructions", None),
            tools=tuple(tools),
            prompts=tuple(prompts),
            initial_tool_names=initial_tool_names,
        )

    async def connect_all(
        self,
        configs: list[MCPServerConfig],
        *,
        progress: Callable[[MCPServerConfig, str, int, int, int], Awaitable[None]] | None = None,
    ) -> list[Any]:
        """Connect to multiple enabled MCP servers and return all tools.

        Per-server failures do not abort the sequence — they are recorded on
        :attr:`failures` and published on the event bus as ``Warning`` events
        so the TUI can display them to the user.

        Connections run concurrently, so progress callbacks arrive in
        completion order rather than profile declaration order.  Callers
        should treat ``current`` / ``failed`` / ``total`` as aggregate
        counters and key any per-server UI by server name.

        Any ordinary ``Exception`` is treated as a per-server failure and
        dropped from the returned tool list.  This includes the documented
        ``MCPConnectionError`` path and also defensive handling of
        unexpected errors (e.g. ``ValueError`` from a malformed config
        caught by ``_validate_config``, owned MCP engine errors,
        ``ExceptionGroup`` leaks from anyio task groups) — none of which
        should be allowed to abort the agent build and leave the engine
        half-initialised. ``MCPToolConfigurationError`` is the deliberate
        exception: deterministic invalid/colliding tool names must abort the
        build before a provider rejects the request or a last-wins tool map
        selects the wrong function.
        ``BaseException`` subclasses (``CancelledError``,
        ``KeyboardInterrupt``, ``SystemExit``, and bare ``BaseExceptionGroup``)
        propagate so structural cancellation / shutdown signals are never
        swallowed.
        """
        enabled_configs: list[MCPServerConfig] = []
        for config in configs:
            if not config.enabled:
                logger.debug("Skipping disabled MCP server '%s'", config.name)
                continue
            enabled_configs.append(config)
        self._tool_names_by_server = {}

        total = len(enabled_configs)
        connected = 0
        failed = 0

        async def _connect_one(config: MCPServerConfig) -> list[Any]:
            nonlocal connected, failed
            if progress is not None:
                await progress(config, "starting", connected, total, failed)
            try:
                tools = await self.connect(config)
            except asyncio.CancelledError:
                raise
            except MCPToolConfigurationError:
                raise
            except MCPConnectionError as exc:
                self._cache.note_connect_failure(config, exc)
                await self._publish_failure(exc)
                failed += 1
                if progress is not None:
                    await progress(config, "failed", connected, total, failed)
                return []
            except Exception as exc:
                err = MCPConnectionError(config.name, config.transport, exc)
                self._failures[config.name] = err
                self._cache.note_connect_failure(config, err)
                await self._publish_failure(err)
                failed += 1
                if progress is not None:
                    await progress(config, "failed", connected, total, failed)
                return []
            connected += 1
            exposure = self._progressive_exposures.get(config.name)
            self._tool_names_by_server[config.name] = (
                exposure.catalog_tool_names
                if exposure is not None
                else [getattr(tool, "name", str(tool)) for tool in tools]
            )
            if progress is not None:
                await progress(config, "connected", connected, total, failed)
            return tools

        results = await asyncio.gather(*(_connect_one(config) for config in enabled_configs))
        self._tool_names_by_server = {
            config.name: self._tool_names_by_server[config.name]
            for config in enabled_configs
            if config.name in self._tool_names_by_server
        }
        return [tool for tools in results for tool in tools]

    async def disconnect(self, name: str) -> None:
        """Disconnect from an MCP server.

        The lock is held only for the pop so that a concurrent ``connect``
        cannot stamp the server back into ``_servers`` after we snapshotted
        its absence.  ``__aexit__`` runs outside the lock.
        """
        async with self._lock:
            task = self._connecting.pop(name, None)
            lease = self._leases.pop(name, None)
            server = self._servers.pop(name, None)
            self._functions_by_server.pop(name, None)
            self._instructions_exposure.pop(name, None)
            self._progressive_exposures.pop(name, None)
            self._tool_namespace_by_server.pop(name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if lease is None:
            if server is not None:
                try:
                    await server.__aexit__(None, None, None)
                except Exception:
                    logger.exception("Error disconnecting MCP server '%s'", name)
            return
        try:
            await self._cache.release(lease)
            if self._owns_cache:
                await self._cache.prune_idle(max_idle_seconds=0)
        except Exception:
            logger.exception("Error disconnecting MCP server '%s'", name)

    async def disconnect_all(self) -> None:
        """Disconnect from all connected MCP servers in parallel.

        The snapshot+clear runs under ``_lock`` so connected servers are
        disconnected and in-flight connects are cancelled before they can
        register a server.  The per-tool ``__aexit__`` calls run outside
        the lock so they proceed concurrently and a slow shutdown does not
        block new connects.
        """
        async with self._lock:
            items = list(self._leases.items())
            leased_server_names = set(self._leases)
            fallback_items = [
                (name, server) for name, server in self._servers.items() if name not in leased_server_names
            ]
            connecting = list(self._connecting.values())
            self._leases.clear()
            self._connecting.clear()
            self._servers.clear()
            self._functions_by_server.clear()
            self._instructions_exposure.clear()
            self._progressive_exposures.clear()
            self._tool_namespace_by_server.clear()
        for task in connecting:
            task.cancel()
        if connecting:
            await self._finish_cleanup_on_cancel(asyncio.gather(*connecting, return_exceptions=True))

        async def _release_one(name: str, lease: MCPConnectionLease) -> None:
            try:
                await self._cache.release(lease)
            except Exception:
                logger.exception("Error disconnecting MCP server '%s'", name)

        if items:
            await self._finish_cleanup_on_cancel(
                asyncio.gather(*(_release_one(n, lease) for n, lease in items), return_exceptions=True)
            )
        if fallback_items:

            async def _exit_one(name: str, tool: MCPTool) -> None:
                try:
                    await tool.__aexit__(None, None, None)
                except Exception:
                    logger.exception("Error disconnecting MCP server '%s'", name)

            await self._finish_cleanup_on_cancel(
                asyncio.gather(*(_exit_one(n, t) for n, t in fallback_items), return_exceptions=True)
            )
        if self._owns_cache:
            try:
                await self._finish_cleanup_on_cancel(self._cache.close_all())
            finally:
                self._cache = MCPConnectionCache()

    async def _publish_warning(self, *, code: str, message: str) -> None:
        """Log and best-effort publish one user-visible non-fatal warning."""
        logger.warning("%s", message)
        if self._bus is None:
            return
        from chrys.foundation.events.types import Warning as WarningEvent

        with contextlib.suppress(Exception):
            await self._bus.publish(
                WarningEvent(
                    code=code,
                    message=message,
                    session_id=self._session_id,
                )
            )

    async def _publish_failure(self, err: MCPConnectionError) -> None:
        """Log and, if a bus is configured, publish a user-visible Warning."""
        logger.error(
            "MCP server '%s' (%s) failed to connect: %s",
            err.server_name,
            err.transport,
            err.cause,
            exc_info=err.cause,
        )
        # A refresh's own report names this failure, without the server's stderr.
        if self._bus is None or self._cache.refreshing:
            return
        from chrys.foundation.events.types import Warning as WarningEvent

        with contextlib.suppress(Exception):
            await self._bus.publish(
                WarningEvent(
                    code="mcp.connect_failed",
                    message=str(err),
                    session_id=self._session_id,
                )
            )

    @property
    def server_names(self) -> list[str]:
        """Names of currently connected MCP servers."""
        return list(dict.fromkeys([*self._leases.keys(), *self._servers.keys()]))

    @property
    def tool_namespace(self) -> frozenset[str]:
        """Every tool name the connected servers take, deferred tools and progressive-loading controls included."""
        return frozenset().union(*self._tool_namespace_by_server.values())

    @property
    def tool_names_by_server(self) -> dict[str, list[str]]:
        """Snapshot of server-name -> loaded tool names from the last connect_all call."""
        return {name: list(tools) for name, tools in self._tool_names_by_server.items()}

    @property
    def failures(self) -> dict[str, MCPConnectionError]:
        """Snapshot of server-name → last connect failure."""
        return dict(self._failures)
