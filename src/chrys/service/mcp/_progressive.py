# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Progressive MCP tool exposure and resume support."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.turns import current_turn_start, turn_slices
from chrys.foundation.tool_call_context import TOOL_CALL_CONTEXT_METADATA_KEY, get_tool_context, set_tool_context
from chrys.foundation.tool_kinds import KIND_MCP, set_tool_kind
from chrys.kernel.exceptions import ModelVisibleToolError, ToolExecutionException
from chrys.kernel.exchanges import (
    EmptyIdPolicy,
    LiveAccessor,
    NoneIdPolicy,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.sessions import ContextProvider
from chrys.kernel.tools import FunctionTool, declared_tool_name, normalize_tools
from chrys.service.mcp.cache import clone_mcp_function_tool
from chrys.service.mcp.owned import (
    _MCP_REMOTE_NAME_KEY,
    _build_prefixed_mcp_name,
    _mcp_config_names_for,
)
from chrys.service.mcp.validation import MAX_MCP_EXPOSED_TOOL_NAME_LENGTH, MCP_PROGRESSIVE_CONTROL_TOOL_NAMES

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.kernel import AgentSession, Message, SessionContext
    from chrys.service.mcp.adapter import MCPAdapter
    from chrys.service.mcp.owned import MCPTool
    from chrys.service.profiles.agents.schema import MCPServerConfig


_MCP_PROGRESSIVE_CONTROL_MARKER = "_chrys_mcp_progressive_control"
(
    _MCP_PROGRESSIVE_LIST_TOOL_NAME,
    _MCP_PROGRESSIVE_LOAD_TOOL_NAME,
    _MCP_PROGRESSIVE_UNLOAD_TOOL_NAME,
) = MCP_PROGRESSIVE_CONTROL_TOOL_NAMES
_MAX_PROGRESSIVE_CONTROL_PREFIX_LENGTH = (
    MAX_MCP_EXPOSED_TOOL_NAME_LENGTH - 1 - max(len(name) for name in MCP_PROGRESSIVE_CONTROL_TOOL_NAMES)
)
_CONTROL_PREFIX_HASH_LENGTH = 10


class _ProgressiveMCPExposure:
    """Per-adapter progressive view over one shared MCP connection catalog.

    The cached :class:`MCPTool` remains the source of truth for the complete
    allowed catalog.  This wrapper owns only stable control tools and initial
    ``always_load`` clones.  Loaded state is derived from the invocation's live
    tool list, so one adapter/run cannot leak exposure state into another
    consumer that shares the same cached MCP session.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        mcp_tool: MCPTool,
        validate_namespace: Callable[[MCPServerConfig, list[FunctionTool], set[str]], None] | None = None,
        *,
        result_cap: int | None = None,
        spill_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._mcp_tool = mcp_tool
        self._validate_namespace = validate_namespace
        self._result_cap = result_cap
        self._spill_dir = spill_dir
        self._always_load_names = set(config.always_load)
        self._control_prefix = self._build_control_prefix(config)
        self._control_tools = self._create_control_tools()
        self._control_names = {tool.name for tool in self._control_tools}

    @staticmethod
    def _build_control_prefix(config: MCPServerConfig) -> str:
        """Return a stable, provider-safe namespace for progressive controls."""
        if config.tool_name_prefix:
            return config.tool_name_prefix
        # Encode every non-ASCII-alphanumeric character instead of lossy MCP
        # normalization. Distinct raw server names such as ``a/b`` and ``a-b``
        # must not collapse onto the same control-tool namespace.
        server_name = "".join(char if char.isascii() and char.isalnum() else f"-{ord(char):x}-" for char in config.name)
        prefix = f"mcp_{server_name}"
        if len(prefix) <= _MAX_PROGRESSIVE_CONTROL_PREFIX_LENGTH:
            return prefix

        # The longest generated suffix is ``list_mcp_tools``. Keep room for
        # it while retaining a stable digest of the complete raw server name,
        # so long names with the same visible stem cannot share a namespace.
        digest = hashlib.sha256(config.name.encode("utf-8")).hexdigest()[:_CONTROL_PREFIX_HASH_LENGTH]
        stem_length = _MAX_PROGRESSIVE_CONTROL_PREFIX_LENGTH - len(digest) - 1
        stem = prefix[:stem_length].rstrip("_-")
        return f"{stem}-{digest}"

    @property
    def initial_tools(self) -> list[FunctionTool]:
        """Return controls plus fresh clones of configured always-loaded tools."""
        tools = list(self._control_tools)
        tools.extend(self.clone_source(source) for source in self.always_load_sources)
        return tools

    def clone_source(self, source: FunctionTool) -> FunctionTool:
        """Clone a catalog tool with this acquisition's result policy."""
        return clone_mcp_function_tool(source, result_cap=self._result_cap, spill_dir=self._spill_dir)

    @property
    def current_surface_tools(self) -> list[FunctionTool]:
        """Return the complete required surface for a fresh progressive run."""
        return self.initial_tools

    @property
    def control_names(self) -> set[str]:
        """Return generated control names for namespace validation."""
        return set(self._control_names)

    @property
    def always_load_sources(self) -> list[FunctionTool]:
        """Return current catalog sources selected by ``always_load``."""
        return [
            source
            for source in self._catalog()
            if self._matches_configured_name(source, self._always_load_names) and source.name not in self._control_names
        ]

    @property
    def unmatched_always_load_names(self) -> list[str]:
        """Return configured initial names not present in the current catalog."""
        matched = {
            configured_name
            for configured_name in self._always_load_names
            if any(self._matches_configured_name(source, {configured_name}) for source in self._catalog())
        }
        return [name for name in self._config.always_load if name not in matched]

    def _catalog(self) -> list[FunctionTool]:
        """Read the current allowed catalog, including list-changed additions."""
        catalog = [tool for tool in self._mcp_tool.functions if isinstance(tool, FunctionTool)]
        if self._validate_namespace is not None:
            self._validate_namespace(self._config, catalog, self._control_names)
        return catalog

    @property
    def catalog_tool_names(self) -> list[str]:
        """Return the complete allowed remote catalog for diagnostics."""
        return [tool.name for tool in self._catalog()]

    @staticmethod
    def _matches_configured_name(function: FunctionTool, names: set[str]) -> bool:
        """Match config names with the MCP raw/lossless-local safety rules."""
        return not names.isdisjoint(_mcp_config_names_for(function))

    def _resolve(self, requested_name: str) -> FunctionTool:
        """Resolve one listed, allowed remote tool without unsafe alias matching."""
        catalog = [tool for tool in self._catalog() if tool.name not in self._control_names]
        matches = [tool for tool in catalog if self._matches_configured_name(tool, {requested_name})]
        matches.extend(tool for tool in catalog if tool.name == requested_name and tool not in matches)
        if not matches:
            available = ", ".join(tool.name for tool in catalog) or "none"
            raise ToolExecutionException(
                f"MCP tool '{requested_name}' is not available from server "
                f"'{self._config.name}'. Available tools: {available}."
            )
        if len(matches) > 1:
            raise ToolExecutionException(
                f"MCP tool name '{requested_name}' is ambiguous on server '{self._config.name}'."
            )
        return matches[0]

    def _resolve_loaded(
        self, requested_name: str, live_tools_by_name: Mapping[str, list[FunctionTool]]
    ) -> FunctionTool | None:
        """Resolve a name against the run's loaded clones of tools the catalog no longer lists.

        A ``list_changed`` refresh replaces the catalog, so a server can drop a
        tool this run already loaded; the clone is still the run's to unload.
        A clone whose tool the catalog still lists answers only through the
        catalog, so an ambiguous name never unloads one of its matches.
        """
        catalog = self._catalog()
        matches = [
            tools[0]
            for name, tools in live_tools_by_name.items()
            if name not in self._control_names
            and (name == requested_name or self._matches_configured_name(tools[0], {requested_name}))
            and not any(self._same_catalog_identity(tools[0], source) for source in catalog)
        ]
        return matches[0] if len(matches) == 1 else None

    def resume_source(self, local_name: str) -> FunctionTool | None:
        """Return a hidden catalog source for one persisted local call name."""
        match = next(
            (
                source
                for source in self._catalog()
                if source.name == local_name
                and source.name not in self._control_names
                and not self._matches_configured_name(source, self._always_load_names)
            ),
            None,
        )
        return match

    @staticmethod
    def _requested_names(tool: str | list[str]) -> list[str]:
        """Normalize a control request into a validated list of names."""
        if isinstance(tool, str):
            return [tool]
        if not isinstance(tool, list) or not all(isinstance(name, str) for name in tool):
            raise ModelVisibleToolError("MCP tool request must be a string or a list of strings.")
        return tool

    def _is_owned_catalog_tool(self, tool: FunctionTool) -> bool:
        """Return whether a live function belongs to this server's remote catalog."""
        provenance = get_tool_context(tool)
        return bool(
            provenance
            and provenance.get("server_name") == self._config.name
            and isinstance(provenance.get("remote_name"), str)
        )

    def _same_catalog_identity(self, left: FunctionTool, right: FunctionTool) -> bool:
        """Compare MCP ownership without relying on clone identity or local name alone."""
        left_context = get_tool_context(left)
        right_context = get_tool_context(right)
        return bool(
            left_context
            and right_context
            and left_context.get("server_name") == right_context.get("server_name") == self._config.name
            and isinstance(left_context.get("remote_name"), str)
            and left_context.get("remote_name") == right_context.get("remote_name")
        )

    def _same_surface_identity(self, left: FunctionTool, right: FunctionTool) -> bool:
        """Compare either a catalog clone or one stable progressive control."""
        if left is right or self._same_catalog_identity(left, right):
            return True
        left_context = get_tool_context(left)
        right_context = get_tool_context(right)
        return bool(
            left_context
            and right_context
            and left_context.get("server_name") == right_context.get("server_name") == self._config.name
            and isinstance(left_context.get("progressive_control"), str)
            and left_context.get("progressive_control") == right_context.get("progressive_control")
        )

    def _live_tool_names(self, ctx: FunctionInvocationContext) -> set[str]:
        """Return live catalog names owned by this server, excluding foreign collisions."""
        return {tool.name for tool in self._live_catalog_tools(ctx)}

    def _live_catalog_tools(self, ctx: FunctionInvocationContext) -> list[FunctionTool]:
        """Return exact live catalog instances owned by this server."""
        if ctx.tools is None:
            return []
        return [tool for tool in ctx.tools if isinstance(tool, FunctionTool) and self._is_owned_catalog_tool(tool)]

    def _create_control_tools(self) -> list[FunctionTool]:
        list_name = _build_prefixed_mcp_name(_MCP_PROGRESSIVE_LIST_TOOL_NAME, self._control_prefix)
        load_name = _build_prefixed_mcp_name(_MCP_PROGRESSIVE_LOAD_TOOL_NAME, self._control_prefix)
        unload_name = _build_prefixed_mcp_name(_MCP_PROGRESSIVE_UNLOAD_TOOL_NAME, self._control_prefix)

        async def _list(ctx: FunctionInvocationContext) -> list[dict[str, Any]]:
            loaded_names = self._live_tool_names(ctx)
            result: list[dict[str, Any]] = []
            for source in self._catalog():
                if source.name in {list_name, load_name, unload_name}:
                    continue
                additional = source.additional_properties or {}
                remote_name = additional.get(_MCP_REMOTE_NAME_KEY)
                result.append(
                    {
                        "name": source.name,
                        "remote_name": remote_name if isinstance(remote_name, str) else source.name,
                        "description": source.description,
                        "loaded": source.name in loaded_names,
                        "always_loaded": self._matches_configured_name(source, self._always_load_names),
                    }
                )
            return result

        async def _load(ctx: FunctionInvocationContext, tool: str | list[str]) -> str:
            if ctx.tools is None:
                raise ToolExecutionException("MCP tools can only be loaded inside an active agent tool loop.")
            messages: list[str] = []
            loaded_names = self._live_tool_names(ctx)
            pending: list[FunctionTool] = []
            pending_names: set[str] = set()
            for requested_name in self._requested_names(tool):
                try:
                    source = self._resolve(requested_name)
                except ToolExecutionException as exc:
                    messages.append(f"Error: {exc}")
                    continue
                if source.name in loaded_names:
                    messages.append(f"MCP tool '{source.name}' is already available.")
                    continue
                if source.name in pending_names:
                    messages.append(f"MCP tool '{source.name}' is already queued to load.")
                    continue
                pending.append(self.clone_source(source))
                pending_names.add(source.name)
                messages.append(f"Loaded MCP tool '{source.name}'. It is available on the next model iteration.")
            if pending:
                try:
                    ctx.add_tools(pending)
                except ValueError as exc:
                    # Its only ValueError is a name taken by a tool this server doesn't own. The
                    # batch is added all or nothing, so none of the requested tools loaded. Names
                    # are read as add_tools reads them, function-tool mappings included.
                    live_names = {declared_tool_name(live) for live in ctx.tools}
                    clashing = ", ".join(f"'{tool.name}'" for tool in pending if tool.name in live_names)
                    raise ModelVisibleToolError(
                        f"Cannot load {clashing or 'the requested MCP tools'}: another tool already has "
                        "that name, so none of the requested MCP tools were loaded.",
                        inner_exception=exc,
                    ) from exc
            return "\n".join(messages) if messages else "No MCP tools requested."

        async def _unload(ctx: FunctionInvocationContext, tool: str | list[str]) -> str:
            if ctx.tools is None:
                raise ToolExecutionException("MCP tools can only be unloaded inside an active agent tool loop.")
            messages: list[str] = []
            live_tools_by_name: dict[str, list[FunctionTool]] = {}
            for live_tool in self._live_catalog_tools(ctx):
                live_tools_by_name.setdefault(live_tool.name, []).append(live_tool)
            queued_names: set[str] = set()
            tools_to_remove: list[FunctionTool] = []
            for requested_name in self._requested_names(tool):
                if requested_name in {list_name, load_name, unload_name}:
                    messages.append(f"MCP control tool '{requested_name}' cannot be unloaded.")
                    continue
                try:
                    source = self._resolve(requested_name)
                except ToolExecutionException as exc:
                    loaded = self._resolve_loaded(requested_name, live_tools_by_name)
                    if loaded is None:
                        messages.append(f"Error: {exc}")
                        continue
                    source = loaded
                if self._matches_configured_name(source, self._always_load_names):
                    messages.append(f"MCP tool '{source.name}' is configured in always_load and cannot be unloaded.")
                    continue
                if source.name not in live_tools_by_name:
                    messages.append(f"MCP tool '{source.name}' is not currently loaded.")
                    continue
                if source.name in queued_names:
                    messages.append(f"MCP tool '{source.name}' is already queued to unload.")
                    continue
                queued_names.add(source.name)
                tools_to_remove.extend(live_tools_by_name[source.name])
                messages.append(f"Unloaded MCP tool '{source.name}'. It will be removed on the next model iteration.")
            if tools_to_remove:
                ctx.remove_tools(tools_to_remove)
            return "\n".join(messages) if messages else "No MCP tools requested."

        input_model = {
            "type": "object",
            "properties": {
                "tool": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "The MCP tool name, or MCP tool names, to update.",
                }
            },
            "required": ["tool"],
            "additionalProperties": False,
        }
        tools = [
            FunctionTool(
                func=_list,
                name=list_name,
                description=f"List MCP tools available from server '{self._config.name}'.",
                input_model={"type": "object", "properties": {}, "additionalProperties": False},
                additional_properties={_MCP_PROGRESSIVE_CONTROL_MARKER: True},
            ),
            FunctionTool(
                func=_load,
                name=load_name,
                description=f"Load MCP tools from server '{self._config.name}' for the current run.",
                input_model=input_model,
                additional_properties={_MCP_PROGRESSIVE_CONTROL_MARKER: True},
            ),
            FunctionTool(
                func=_unload,
                name=unload_name,
                description=f"Unload MCP tools from server '{self._config.name}' for the current run.",
                input_model=input_model,
                additional_properties={_MCP_PROGRESSIVE_CONTROL_MARKER: True},
            ),
        ]
        for tool in tools:
            set_tool_kind(tool, KIND_MCP)
            set_tool_context(tool, {"server_name": self._config.name, "progressive_control": tool.name})
        return tools


# Resume re-exposes tools only for calls still lacking any answer in their
# exchange. Informational calls never need resuming. Id-less calls (None or
# empty ids; non-string ids read through their string form) can never be
# answered by any result, so they always surface as unresolved.
_RESUME_PAIRING_POLICY = PairingPolicy(
    call_types=frozenset({"function_call"}),
    include_informational_calls=False,
    result_types=frozenset({"function_result"}),
    none_id=NoneIdPolicy.UNPAIRABLE_OCCURRENCE,
    empty_id=EmptyIdPolicy.UNPAIRABLE_OCCURRENCE,
    malformed_id="stringify",
)


class ProgressiveMCPResumeProvider(ContextProvider):
    """Refresh per-run progressive tools and resume unresolved current-turn calls.

    Ordinary load/unload state remains run-local. Before each run, newly
    advertised ``always_load`` tools are added to the invocation. The narrower
    retry/restore safety case also re-exposes exact hidden functions for
    unresolved actionable calls in the current turn.
    """

    def __init__(self, adapter: MCPAdapter) -> None:
        super().__init__("progressive_mcp_resume")
        self._adapter = adapter

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        default_options = agent.default_options or {}
        visible_tools = normalize_tools(default_options.get("tools"))
        visible_tools.extend(normalize_tools(context.tools))
        candidates = self._adapter._progressive_surface_tools()

        messages = context.get_messages(include_input=True)
        current_messages = messages[current_turn_start(messages) :]
        slices = turn_slices(current_messages)
        if slices:
            current_messages = current_messages[slices[-1][0] : slices[-1][1]]
        unresolved_calls = self._unresolved_actionable_calls(current_messages)
        if unresolved_calls:
            candidates.extend(self._adapter._resume_tools(unresolved_calls))

        tools = self._adapter._context_tools_without_collisions(visible_tools, candidates)
        if tools:
            context.extend_tools(self.source_id, tools)

    @staticmethod
    def _unresolved_actionable_calls(messages: list[Message]) -> list[tuple[str, str | None]]:
        """Return unpaired call identity, preserving stamped MCP server provenance."""
        accessor = LiveAccessor()
        unresolved: list[tuple[str, str | None]] = []
        for exchange in iter_exchanges(messages, accessor):
            pairing = pair_results(messages, exchange, accessor, _RESUME_PAIRING_POLICY)
            dangling = [
                call
                for assignments in pairing.truthy_assignments.values()
                for call, result in assignments
                if result is None
            ]
            dangling.extend(pairing.unpairable_calls)
            dangling.sort(key=lambda occurrence: (occurrence.message_index, occurrence.content_index))
            for occurrence in dangling:
                call = messages[occurrence.message_index].contents[occurrence.content_index]
                if isinstance(call.name, str) and call.name:
                    context_value = call.additional_properties.get(TOOL_CALL_CONTEXT_METADATA_KEY)
                    server_name = context_value.get("server_name") if isinstance(context_value, dict) else None
                    unresolved.append((call.name, server_name if isinstance(server_name, str) else None))
        return list(dict.fromkeys(unresolved))
