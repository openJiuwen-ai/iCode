# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned MCP client tool engine.

This module owns the MCP surface Chrys consumes:
stdio and streamable-HTTP transports, connection lifecycle, tool/prompt
loading, tool/prompt calls, result parsing, and MCP client spans. WebSocket
and SEP-2663 long-running task driving are intentionally out of scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Self, cast

from opentelemetry import propagate
from opentelemetry import trace as otel_trace

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.text.model_json import model_json
from chrys.foundation.tool_call_context import set_tool_context
from chrys.foundation.trajectory.context import current_tool_operation_id
from chrys.foundation.trajectory.event_types import WaitCategory
from chrys.foundation.util.imports import import_off_loop
from chrys.kernel import Content, normalize_tools
from chrys.kernel._tool_expansion import _set_tool_expander
from chrys.kernel.exceptions import ModelVisibleToolError, ToolException, ToolExecutionException
from chrys.kernel.instrumentation import OtelAttr, create_mcp_client_span, set_mcp_span_error
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.tools import FunctionTool
from chrys.service.mcp.content import INVALID_AUDIO_TEXT, INVALID_IMAGE_TEXT, INVALID_RESOURCE_TEXT, decode_media_base64
from chrys.service.mcp.result_limits import (
    _MCP_PROMPT_TOOL_KEY,
    DEFAULT_MCP_TOOL_RESULT_MAX_TOKENS,
    sanitize_mcp_result_binary,
    truncate_mcp_error,
)
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace

if TYPE_CHECKING:
    from contextlib import _AsyncGeneratorContextManager  # type: ignore[attr-defined]

    from httpx import AsyncClient
    from mcp import types
    from mcp.client.session import ClientSession
    from mcp.shared.session import RequestResponder

    from chrys.kernel.types import ToolTypes

logger = logging.getLogger(__name__)


_MCP_REMOTE_NAME_KEY = "_mcp_remote_name"
_MCP_NORMALIZED_NAME_KEY = "_mcp_normalized_name"
_MCP_GLOBAL_EXTRA_ARGS_KEY = "*"
# Runtime argument names retained for diagnostics and regression coverage.
# Never use this catalog to reject an argument declared by the remote tool:
# provenance, not spelling, is the security boundary.
_MCP_FRAMEWORK_DENYLIST: frozenset[str] = frozenset(
    {
        "chat_options",
        "tools",
        "tool_choice",
        "session",
        "thread",
        "conversation_id",
        "options",
        "response_format",
        "_meta",
    }
)
# ``ErrorData.data`` of the JSON-RPC errors the HTTP transport makes up when its
# own request fails. Their message is local httpx detail — the configured URL,
# which can carry credentials, included — not an answer from the server.
LOCAL_HTTP_FAILURE_ERROR_DATA = "chrys.local_http_failure"

LOG_LEVEL_MAPPING: dict[types.LoggingLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "alert": logging.CRITICAL,
    "emergency": logging.CRITICAL,
}


def _get_input_model_from_mcp_prompt(prompt: types.Prompt) -> dict[str, Any]:
    if not prompt.arguments:
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for prompt_argument in prompt.arguments:
        properties[prompt_argument.name] = {
            "type": "string",
            "description": prompt_argument.description if hasattr(prompt_argument, "description") else "",
        }
        if prompt_argument.required:
            required.append(prompt_argument.name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _normalize_mcp_name(name: str) -> str:
    # Keep the exposed name portable across model providers. MCP servers may
    # advertise a broader character set, but OpenAI-compatible function tools
    # reject periods and other punctuation before the first model response.
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def _build_prefixed_mcp_name(normalized_name: str, tool_name_prefix: str | None) -> str:
    if not tool_name_prefix:
        return normalized_name
    normalized_prefix = _normalize_mcp_name(tool_name_prefix).rstrip("_.-")
    if not normalized_prefix:
        return normalized_name
    trimmed_name = normalized_name.lstrip("_.-")
    return f"{normalized_prefix}_{trimmed_name}" if trimmed_name else normalized_prefix


def _normalize_additional_tool_argument_names(
    additional_tool_argument_names: Sequence[str] | Mapping[str, str | Sequence[str]] | None,
) -> tuple[set[str], dict[str, set[str]]]:
    if additional_tool_argument_names is None:
        return set(), {}
    if isinstance(additional_tool_argument_names, str):
        return {additional_tool_argument_names}, {}
    if isinstance(additional_tool_argument_names, Mapping):
        global_extras: set[str] = set()
        per_tool_extras: dict[str, set[str]] = {}
        for tool_name, names in additional_tool_argument_names.items():
            # The input contract leaves only a string sequence after the string case.
            names_set = {names} if isinstance(names, str) else set(cast(Sequence[str], names))
            if tool_name == _MCP_GLOBAL_EXTRA_ARGS_KEY:
                global_extras.update(names_set)
            else:
                # Mapping keys are declared strings; the literal comparison only refines their value.
                per_tool_extras[cast(str, tool_name)] = names_set
        return global_extras, per_tool_extras
    return set(additional_tool_argument_names), {}


def _mcp_config_candidate_names(*, local_name: str, normalized_name: str, remote_name: str) -> tuple[str, ...]:
    """Return safe configuration names for MCP allow/approval matching."""
    names = [remote_name]
    if normalized_name == remote_name and local_name != remote_name:
        names.append(local_name)
    return tuple(names)


def _mcp_config_names_for(function: FunctionTool) -> tuple[str, ...]:
    """Return the ``allowed_tools``/``always_load`` names that select *function*; none without MCP name stamps."""
    additional = function.additional_properties or {}
    normalized_name = additional.get(_MCP_NORMALIZED_NAME_KEY)
    remote_name = additional.get(_MCP_REMOTE_NAME_KEY)
    if not isinstance(normalized_name, str) or not isinstance(remote_name, str):
        return ()
    return _mcp_config_candidate_names(
        local_name=function.name,
        normalized_name=normalized_name,
        remote_name=remote_name,
    )


def _inject_otel_into_mcp_meta(
    meta: dict[str, Any] | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, Any] | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    if not carrier:
        return meta

    if meta is None:
        meta = {}
    for key, value in carrier.items():
        if overwrite or key not in meta:
            meta[key] = value
    return meta


def _server_error(message: str, subject: str, *, inner_exception: Exception | None = None) -> ModelVisibleToolError:
    """Return the error the model reads for the server's answer to a call: *message*, already bounded.

    A blank message (or one that is only ``Error:``) says so, naming *subject*
    (``MCP tool 'x'``), rather than leave the model the generic fixed line.
    """
    error = ModelVisibleToolError(message, inner_exception=inner_exception)
    if error.result_text is None:
        error = ModelVisibleToolError(
            f"{subject} reported an error without an error message.", inner_exception=inner_exception
        )
    return error


def _mcp_error_exception(error: Exception, message: str, subject: str) -> ToolExecutionException:
    """Choose what to raise for a request that ended in *error*, whose bounded text is *message*.

    A JSON-RPC error the server sent back (``-32602 Invalid params``, say) is
    its answer to this call, and the MCP SDK's own errors (timeout, connection
    closed) are fixed text, so the model reads either one. The HTTP
    transport's made-up errors carry local detail and reach only the user.
    """
    from mcp.shared.exceptions import McpError

    if isinstance(error, McpError) and error.error.data != LOCAL_HTTP_FAILURE_ERROR_DATA:
        return _server_error(message, subject, inner_exception=error)
    return ToolExecutionException(message, inner_exception=error)


def _should_propagate_cancelled_error(ex: BaseException) -> bool:
    if not isinstance(ex, asyncio.CancelledError):
        return False
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _describe_error(ex: BaseException) -> str:
    """Unwrap task-group and cancellation wrappers to name the actual failure."""
    current = ex
    seen: set[int] = set()
    for _ in range(20):
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup) and len(current.exceptions) == 1:
            cause = current.exceptions[0]
        else:
            cause = current.__cause__ or current.__context__
        if cause is None or id(cause) in seen:
            break
        if isinstance(current, asyncio.CancelledError) and isinstance(cause, asyncio.CancelledError):
            break
        current = cause
    return str(current) or repr(current)


def _describe_with_cleanup(ex: BaseException, cleanup_error: BaseException | None) -> str:
    """A cancelled initialize may reveal its real error only during teardown."""
    if isinstance(ex, asyncio.CancelledError) and cleanup_error is not None:
        return _describe_error(cleanup_error)
    return _describe_error(ex)


def _media_content(item: types.ImageContent | types.AudioContent) -> Content:
    """An image or audio item as data; a placeholder text when its data is not non-empty base64."""
    if data := decode_media_base64(item.data):
        return Content.from_data(data=data, media_type=item.mimeType)
    return Content.from_text(INVALID_IMAGE_TEXT if item.type == "image" else INVALID_AUDIO_TEXT)


def _blob_content(resource: types.BlobResourceContents) -> Content:
    """A blob resource as data, empty allowed; a placeholder text when its blob is not base64."""
    if (data := decode_media_base64(resource.blob)) is None:
        return Content.from_text(INVALID_RESOURCE_TEXT)
    return Content.from_data(data=data, media_type=resource.mimeType or "application/octet-stream")


def _resource_link_text(item: types.ResourceLink) -> Content:
    """A link as text the model reads, never as the linked item itself.

    Model APIs would fetch a linked item on every request, and one they can't
    reach (a local file, a localhost or expired URL) would fail every later
    request of the session.
    """
    lines = [f"Resource link: {item.name}", f"URI: {item.uri}"]
    if item.mimeType:
        lines.append(f"MIME type: {item.mimeType}")
    if item.description:
        lines.append(f"Description: {item.description}")
    return Content.from_text("\n".join(lines))


class MCPTool:
    """Base class for Chrys-owned MCP stdio and streamable-HTTP tools."""

    def __init__(
        self,
        name: str,
        description: str | None = None,
        allowed_tools: Collection[str] | None = None,
        tool_name_prefix: str | None = None,
        load_tools: bool = True,
        parse_tool_results: Callable[[types.CallToolResult], str | list[Content]] | None = None,
        load_prompts: bool = True,
        parse_prompt_results: Callable[[types.GetPromptResult], str] | None = None,
        session: ClientSession | None = None,
        request_timeout: int | None = None,
        additional_properties: dict[str, Any] | None = None,
        additional_tool_argument_names: Sequence[str] | Mapping[str, str | Sequence[str]] | None = None,
    ) -> None:
        self.name = name
        self.description = description or ""
        self.allowed_tools = allowed_tools
        self.tool_name_prefix = _normalize_mcp_name(tool_name_prefix).rstrip("_.-") if tool_name_prefix else None
        self.additional_properties = additional_properties
        self.load_tools_flag = load_tools
        self.parse_tool_results = parse_tool_results
        self.load_prompts_flag = load_prompts
        self.parse_prompt_results = parse_prompt_results
        self._exit_stack = AsyncExitStack()
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_request_lock = asyncio.Lock()
        self._function_load_lock = asyncio.Lock()
        self._lifecycle_queue: asyncio.Queue[tuple[str, bool, bool, asyncio.Future[None]]] | None = None
        self._lifecycle_owner_task: asyncio.Task[None] | None = None
        self.session = session
        self.request_timeout = request_timeout
        self._functions: list[FunctionTool] = []
        # Reloads must skip an already wrapped remote declaration without
        # collapsing *different* remote names that normalize to the same local
        # name. The adapter validates those duplicates before exposure.
        self._loaded_tool_remote_names: set[str] = set()
        self._loaded_prompt_remote_names: set[str] = set()
        self._tool_call_meta_by_name: dict[str, dict[str, Any]] = {}
        self._tool_task_support_by_name: dict[str, str] = {}
        self._tool_param_names_by_name: dict[str, set[str]] = {}
        self._global_extra_arg_names, self._tool_extra_arg_names = _normalize_additional_tool_argument_names(
            additional_tool_argument_names
        )
        self.is_connected = False
        self._tools_loaded = False
        self._prompts_loaded = False
        self._server_capabilities: types.ServerCapabilities | None = None
        self._server_instructions: str | None = None
        self._server_info: types.Implementation | None = None
        self._protocol_version: str | None = None
        self._supports_tools = True
        self._supports_prompts = True
        self._supports_logging: bool | None = None
        self._ping_available = True
        self._pending_reload_tasks: set[asyncio.Task[None]] = set()

    def __str__(self) -> str:
        return f"MCPTool(name={self.name}, description={self.description})"

    def _mcp_base_span_attributes(self) -> dict[str, Any]:
        return {}

    @property
    def functions(self) -> list[FunctionTool]:
        """Return exposed functions; ``None`` allows all tools, while ``[]`` allows none."""
        if self.allowed_tools is None:
            return self._functions
        allowed_names = set(self.allowed_tools)
        return [func for func in self._functions if not allowed_names.isdisjoint(_mcp_config_names_for(func))]

    async def _safe_close_exit_stack(self) -> BaseException | None:
        try:
            await self._exit_stack.aclose()
        except RuntimeError as err:
            if "cancel scope" in str(err).lower():
                logger.warning("Could not cleanly close MCP exit stack due to cancel scope error: %s", err)
                return err
            raise
        except asyncio.CancelledError as err:
            logger.warning("Could not cleanly close MCP exit stack because the task was cancelled.")
            return err
        except Exception as err:
            if type(err).__name__ == "ExceptionGroup":
                logger.warning("Could not cleanly close MCP exit stack due to cleanup error group: %s", err)
                return err
            raise
        return None

    async def _close_and_check_cancelled(self, ex: BaseException) -> tuple[bool, BaseException | None]:
        cleanup_error = await self._safe_close_exit_stack()
        return _should_propagate_cancelled_error(ex), cleanup_error

    def _reset_session_state(self) -> None:
        self._server_capabilities = None
        self._server_instructions = None
        self._server_info = None
        self._protocol_version = None
        self._supports_tools = True
        self._supports_prompts = True
        self._supports_logging = None
        self._ping_available = True

    def _set_server_capabilities(self, capabilities: types.ServerCapabilities | None) -> None:
        self._server_capabilities = capabilities
        if capabilities is None:
            self._supports_tools = False
            self._supports_prompts = False
            self._supports_logging = False
            return

        self._supports_tools = getattr(capabilities, "tools", None) is not None
        self._supports_prompts = getattr(capabilities, "prompts", None) is not None
        self._supports_logging = getattr(capabilities, "logging", None) is not None

    def _set_server_instructions(self, instructions: object) -> None:
        self._server_instructions = (
            instructions.strip() if isinstance(instructions, str) and instructions.strip() else None
        )

    def _set_server_identity(self, initialize_result: object) -> None:
        # Duck-typed: ``mcp.types`` is a deferred import, so don't isinstance
        # against Implementation here — a serverInfo with a name is enough.
        server_info = getattr(initialize_result, "serverInfo", None)
        self._server_info = server_info if getattr(server_info, "name", None) else None
        protocol_version = getattr(initialize_result, "protocolVersion", None)
        self._protocol_version = str(protocol_version) if protocol_version is not None else None

    async def _ensure_lifecycle_owner(self) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle_owner_task is not None and not self._lifecycle_owner_task.done():
                return

            self._lifecycle_queue = asyncio.Queue()
            self._lifecycle_owner_task = asyncio.create_task(
                self._run_lifecycle_owner(),
                name=f"mcp-lifecycle:{self.name}",
            )

    async def _run_lifecycle_owner(self) -> None:
        queue = self._lifecycle_queue
        if queue is None:
            return

        stop_error: BaseException | None = None
        try:
            while True:
                action, reset, load_configured, future = await queue.get()
                try:
                    if action == "connect":
                        await self._connect_on_owner(reset=reset, load_configured=load_configured)
                    elif action == "close":
                        await self._close_on_owner()
                    else:
                        raise RuntimeError(f"Unknown MCP lifecycle action: {action}")
                except asyncio.CancelledError as ex:
                    stop_error = ex
                    if not future.done():
                        future.set_exception(ex)
                    raise
                except Exception as ex:
                    if not future.done():
                        future.set_exception(ex)
                else:
                    if not future.done():
                        future.set_result(None)

                if action == "close":
                    return
        except asyncio.CancelledError as ex:
            stop_error = ex
            raise
        finally:
            while True:
                try:
                    _, _, _, future = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not future.done():
                    future.set_exception(stop_error or RuntimeError("MCP lifecycle owner stopped unexpectedly."))
            self._lifecycle_queue = None
            self._lifecycle_owner_task = None

    def _is_lifecycle_owner_task(self) -> bool:
        owner_task = self._lifecycle_owner_task
        return owner_task is not None and asyncio.current_task() is owner_task

    async def _run_on_lifecycle_owner(
        self,
        action: str,
        *,
        reset: bool = False,
        load_configured: bool = True,
    ) -> None:
        await self._ensure_lifecycle_owner()

        if self._is_lifecycle_owner_task():
            if action == "connect":
                await self._connect_on_owner(reset=reset, load_configured=load_configured)
            elif action == "close":
                await self._close_on_owner()
            else:
                raise RuntimeError(f"Unknown MCP lifecycle action: {action}")
            return

        queue = self._lifecycle_queue
        if queue is None:
            raise RuntimeError("MCP lifecycle owner is not available.")

        future = asyncio.get_running_loop().create_future()
        await queue.put((action, reset, load_configured, future))
        await future

    async def connect(self, *, reset: bool = False) -> None:
        # A connect inside a model run (lazy first use, reconnect after a
        # dropped session) is a wait the turn timeline must account for.
        wait = WaitTrace.open(
            WaitCategory.MCP_CONNECT,
            target_operation_id=current_tool_operation_id(),
            server_name=self.name,
        )
        try:
            # Inside the block that closes the wait: the start marker awaits
            # its write ack, and an interrupt landing there would otherwise
            # leave the wait open forever.
            if wait is not None:
                await wait.started()
            await self._connect_unrecorded(reset=reset)
        except asyncio.CancelledError:
            if wait is not None:
                wait.finished_soon(outcome=WaitOutcome.CANCELLED)
            raise
        except BaseException:
            if wait is not None:
                await wait.finished(outcome=WaitOutcome.FAILED)
            raise
        if wait is not None:
            await wait.finished()

    async def _connect_unrecorded(self, *, reset: bool) -> None:
        if self._is_lifecycle_owner_task():
            await self._connect_on_owner(reset=reset)
            return

        async with self._lifecycle_request_lock:
            await self._run_on_lifecycle_owner("connect", reset=reset)

    async def _connect_on_owner(self, *, reset: bool = False, load_configured: bool = True) -> None:
        """Connect, initialize, and load configured MCP functions."""
        if reset:
            await self._safe_close_exit_stack()
            self.session = None
            self.is_connected = False
            self._reset_session_state()
            self._exit_stack = AsyncExitStack()

        if not self.session:
            try:
                # The SDK is hundreds of modules, which the first connect of a run imports.
                await import_off_loop("mcp")
                transport = await self._exit_stack.enter_async_context(self.get_mcp_client())
            except (Exception, asyncio.CancelledError) as ex:
                cancelled, cleanup_error = await self._close_and_check_cancelled(ex)
                if cancelled:
                    raise
                command = getattr(self, "command", None)
                if command:
                    message = f"Failed to start MCP server '{command}': {_describe_with_cleanup(ex, cleanup_error)}"
                else:
                    message = f"Failed to connect to MCP server: {_describe_with_cleanup(ex, cleanup_error)}"
                raise ToolException(message, inner_exception=ex if isinstance(ex, Exception) else None) from ex

            try:
                from mcp.client.session import ClientSession
            except ModuleNotFoundError as ex:
                await self._safe_close_exit_stack()
                raise ToolException("MCP support requires `mcp`. Please install `mcp`.", inner_exception=ex) from ex

            try:
                session = await self._exit_stack.enter_async_context(
                    ClientSession(
                        read_stream=transport[0],
                        write_stream=transport[1],
                        read_timeout_seconds=timedelta(seconds=self.request_timeout) if self.request_timeout else None,
                        message_handler=self.message_handler,
                        logging_callback=self.logging_callback,
                    )
                )
            except (Exception, asyncio.CancelledError) as ex:
                cancelled, cleanup_error = await self._close_and_check_cancelled(ex)
                if cancelled:
                    raise
                message = f"Failed to create MCP session: {_describe_with_cleanup(ex, cleanup_error)}"
                raise ToolException(message, inner_exception=ex if isinstance(ex, Exception) else None) from ex

            try:
                with create_mcp_client_span("initialize", attributes=self._mcp_base_span_attributes()) as init_span:
                    initialize_result = await session.initialize()
                    init_span.set_attribute(OtelAttr.MCP_PROTOCOL_VERSION, initialize_result.protocolVersion)
                    self._set_server_capabilities(getattr(initialize_result, "capabilities", None))
                    self._set_server_instructions(getattr(initialize_result, "instructions", None))
                    self._set_server_identity(initialize_result)
            except (Exception, asyncio.CancelledError) as ex:
                cancelled, cleanup_error = await self._close_and_check_cancelled(ex)
                if cancelled:
                    raise
                command = getattr(self, "command", None)
                if command:
                    args_str = " ".join(getattr(self, "args", []))
                    full_command = f"{command} {args_str}".strip()
                    message = (
                        f"MCP server '{full_command}' failed to initialize: {_describe_with_cleanup(ex, cleanup_error)}"
                    )
                else:
                    message = f"MCP server failed to initialize: {_describe_with_cleanup(ex, cleanup_error)}"
                raise ToolException(message, inner_exception=ex if isinstance(ex, Exception) else None) from ex
            self.session = session

        elif getattr(self.session, "_request_id", 0) == 0:
            with create_mcp_client_span("initialize", attributes=self._mcp_base_span_attributes()) as init_span:
                initialize_result = await self.session.initialize()
                init_span.set_attribute(OtelAttr.MCP_PROTOCOL_VERSION, initialize_result.protocolVersion)
                self._set_server_capabilities(getattr(initialize_result, "capabilities", None))
                self._set_server_instructions(getattr(initialize_result, "instructions", None))
                self._set_server_identity(initialize_result)
        elif self._server_capabilities is None:
            self._set_server_capabilities(getattr(self.session, "_server_capabilities", None))

        self.is_connected = True
        if load_configured and self.load_tools_flag:
            if self._supports_tools:
                await self.load_tools()
            self._tools_loaded = True
        if load_configured and self.load_prompts_flag:
            if self._supports_prompts:
                await self.load_prompts()
            self._prompts_loaded = True

        if logger.level != logging.NOTSET and self._supports_logging is not False:
            with contextlib.suppress(Exception):
                level_name = next(level for level, value in LOG_LEVEL_MAPPING.items() if value == logger.level)
                await self.session.set_logging_level(level_name)

    async def _reconnect_without_loading(self) -> None:
        if self._is_lifecycle_owner_task():
            await self._connect_on_owner(reset=True, load_configured=False)
            return

        await self._run_on_lifecycle_owner("connect", reset=True, load_configured=False)

    async def _close_on_owner(self) -> None:
        tasks = list(self._pending_reload_tasks)
        for task in tasks:
            task.cancel()
        self._pending_reload_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._safe_close_exit_stack()
        self._exit_stack = AsyncExitStack()
        self.session = None
        self.is_connected = False
        self._reset_session_state()

    async def close(self) -> None:
        if self._is_lifecycle_owner_task():
            await self._close_on_owner()
            return

        async with self._lifecycle_request_lock:
            await self._run_on_lifecycle_owner("close")

    @abstractmethod
    def get_mcp_client(self) -> _AsyncGeneratorContextManager[Any, None]:
        """Return the MCP SDK transport context manager."""

    async def _ensure_connected(self) -> None:
        from mcp.shared.exceptions import McpError

        if not self._ping_available:
            return
        # Connected operations run only after the lifecycle installs a session.
        session = cast("ClientSession", self.session)
        try:
            await session.send_ping()
        except McpError as mcp_exc:
            if mcp_exc.error.code == -32601:
                self._ping_available = False
                logger.debug("Skipping future MCP pings because the server does not support ping.")
                return
            await self._reconnect_or_raise(mcp_exc)
        except Exception as ex:
            await self._reconnect_or_raise(ex)

    async def probe_session(self, *, timeout: float) -> str | None:
        """Ask the current session for one bounded ping, never reconnecting.

        Returns None when that same session answered; a server that does not
        implement ping answers -32601, which still proves the session is alive.
        Otherwise returns why the session could not be verified, in text that
        holds no server-sent content.
        """
        from mcp.shared.exceptions import McpError

        session = self.session
        if session is None or not self.is_connected:
            return "the connection is closed"
        try:
            async with asyncio.timeout(timeout):
                await session.send_ping()
        except TimeoutError:
            return f"no answer to ping within {timeout:g}s"
        except McpError as exc:
            if exc.error.code != -32601:
                return f"ping failed with error {exc.error.code}"
        except Exception as exc:
            return f"ping failed ({type(exc).__name__})"
        return None if self.session is session else "the session was replaced during the check"

    async def _reconnect_or_raise(self, ex: BaseException) -> None:
        logger.info("MCP connection invalid or closed. Reconnecting...")
        try:
            await self._reconnect_without_loading()
        except Exception as reconn_ex:
            raise ToolExecutionException("Failed to establish MCP connection.", inner_exception=reconn_ex) from ex

    async def logging_callback(self, params: types.LoggingMessageNotificationParams) -> None:
        logger.log(LOG_LEVEL_MAPPING[params.level], params.data)

    async def message_handler(
        self,
        message: RequestResponder[types.ServerRequest, types.ClientResult] | types.ServerNotification | Exception,
    ) -> None:
        from mcp import types

        if isinstance(message, Exception):
            logger.error("Error from MCP server: %s", message, exc_info=message)
            return
        if isinstance(message, types.ServerNotification):
            match message.root.method:
                case "notifications/tools/list_changed":
                    self._schedule_reload(self.load_tools())
                case "notifications/prompts/list_changed":
                    self._schedule_reload(self.load_prompts())
                case _:
                    logger.debug("Unhandled notification: %s", message.root.method)

    def _schedule_reload(self, coro: Any) -> None:
        reload_name = f"mcp-reload:{self.name}:{coro.__qualname__}"
        for existing in list(self._pending_reload_tasks):
            if existing.get_name() == reload_name and not existing.done():
                existing.cancel()

        async def _safe_reload() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Background MCP reload failed", exc_info=True)

        task = asyncio.create_task(_safe_reload(), name=reload_name)
        self._pending_reload_tasks.add(task)
        task.add_done_callback(self._pending_reload_tasks.discard)

    async def load_prompts(self) -> None:
        async with self._function_load_lock:
            await self._load_prompts_locked()

    async def _load_prompts_locked(self) -> None:
        from anyio import ClosedResourceError
        from mcp import types

        if not self._supports_prompts:
            return

        params: types.PaginatedRequestParams | None = None
        while True:
            prompt_list: types.ListPromptsResult | None = None
            for attempt in range(2):
                try:
                    await self._ensure_connected()
                    # Prompt loading is entered only for a connected lifecycle session.
                    session = cast("ClientSession", self.session)
                    if not self._supports_prompts:
                        return
                    with create_mcp_client_span("prompts/list", attributes=self._mcp_base_span_attributes()):
                        prompt_list = await session.list_prompts(params=params)
                    break
                except ClosedResourceError as cl_ex:
                    if attempt == 0:
                        try:
                            await self._reconnect_without_loading()
                        except Exception as reconn_ex:
                            raise ToolExecutionException(
                                "Failed to reconnect to MCP server.",
                                inner_exception=reconn_ex,
                            ) from reconn_ex
                        continue
                    raise ToolExecutionException(
                        "Failed to load prompts - connection lost.", inner_exception=cl_ex
                    ) from cl_ex
            if prompt_list is None:
                raise ToolExecutionException("Failed to load prompts.")

            for prompt in prompt_list.prompts:
                normalized_name = _normalize_mcp_name(prompt.name)
                local_name = _build_prefixed_mcp_name(normalized_name, self.tool_name_prefix)
                if prompt.name in self._loaded_prompt_remote_names:
                    continue
                func = FunctionTool(
                    func=partial(self.get_prompt, prompt.name),
                    name=local_name,
                    description=prompt.description or "",
                    input_model=_get_input_model_from_mcp_prompt(prompt),
                    additional_properties={
                        _MCP_REMOTE_NAME_KEY: prompt.name,
                        _MCP_NORMALIZED_NAME_KEY: normalized_name,
                        _MCP_PROMPT_TOOL_KEY: True,
                    },
                )
                # Static provenance persisted on the function_call content;
                # identity only — never URLs, headers, env, or command lines.
                set_tool_context(
                    func,
                    {
                        "server_name": self.name,
                        "remote_name": prompt.name,
                        "normalized_name": normalized_name,
                    },
                )
                self._functions.append(func)
                self._loaded_prompt_remote_names.add(prompt.name)

            if not prompt_list.nextCursor:
                break
            params = types.PaginatedRequestParams(cursor=prompt_list.nextCursor)

    async def load_tools(self) -> None:
        async with self._function_load_lock:
            await self._load_tools_locked()

    def _make_remote_tool_call(self, remote_tool_name: str) -> Callable[..., Awaitable[str | list[Content]]]:
        """Bind one trusted remote name outside the model-callable signature."""

        async def _call_tool_with_runtime_kwargs(
            ctx: FunctionInvocationContext,
            **kwargs: Any,
        ) -> str | list[Content]:
            trusted_meta = ctx.kwargs.get("_meta")
            extras = self._resolved_extra_args(remote_tool_name)
            runtime_arguments = {key: value for key, value in ctx.kwargs.items() if key != "_meta" and key in extras}
            tool_arguments = dict(ctx.arguments)

            # ``_meta`` is an MCP request-metadata channel, not a tool
            # argument.  Model arguments are untrusted even when the
            # remote schema accepts additional properties; only the
            # runtime context may populate request metadata.
            tool_arguments.pop("_meta", None)

            # Explicit runtime extras are trusted injections and therefore
            # win over a same-named value supplied by the model.
            call_kwargs = {**tool_arguments, **runtime_arguments}
            if trusted_meta is not None:
                call_kwargs["_meta"] = trusted_meta
            return await self.call_tool(remote_tool_name, **call_kwargs)

        return _call_tool_with_runtime_kwargs

    async def _load_tools_locked(self) -> None:
        from anyio import ClosedResourceError
        from mcp import types

        if not self._supports_tools:
            return

        tool_call_meta_by_name: dict[str, dict[str, Any]] = {}
        tool_task_support_by_name: dict[str, str] = {}
        tool_param_names_by_name: dict[str, set[str]] = {}

        params: types.PaginatedRequestParams | None = None
        while True:
            tool_list: types.ListToolsResult | None = None
            for attempt in range(2):
                try:
                    await self._ensure_connected()
                    # Tool loading is entered only for a connected lifecycle session.
                    session = cast("ClientSession", self.session)
                    if not self._supports_tools:
                        return
                    with create_mcp_client_span("tools/list", attributes=self._mcp_base_span_attributes()):
                        tool_list = await session.list_tools(params=params)
                    break
                except ClosedResourceError as cl_ex:
                    if attempt == 0:
                        try:
                            await self._reconnect_without_loading()
                        except Exception as reconn_ex:
                            raise ToolExecutionException(
                                "Failed to reconnect to MCP server.",
                                inner_exception=reconn_ex,
                            ) from reconn_ex
                        continue
                    raise ToolExecutionException(
                        "Failed to load tools - connection lost.", inner_exception=cl_ex
                    ) from cl_ex
            if tool_list is None:
                raise ToolExecutionException("Failed to load tools.")

            for tool in tool_list.tools:
                if tool.meta is not None:
                    tool_call_meta_by_name[tool.name] = dict(tool.meta)
                task_support = getattr(getattr(tool, "execution", None), "taskSupport", None)
                if task_support is not None:
                    tool_task_support_by_name[tool.name] = task_support

                input_schema = dict(tool.inputSchema or {})
                if input_schema.get("type") == "object" and "properties" not in input_schema:
                    input_schema["properties"] = {}
                properties = input_schema.get("properties")
                tool_param_names_by_name[tool.name] = (
                    set(cast(dict[str, Any], properties)) if isinstance(properties, dict) else set()
                )

                normalized_name = _normalize_mcp_name(tool.name)
                local_name = _build_prefixed_mcp_name(normalized_name, self.tool_name_prefix)
                if tool.name in self._loaded_tool_remote_names:
                    continue

                func = FunctionTool(
                    func=self._make_remote_tool_call(tool.name),
                    name=local_name,
                    description=tool.description or "",
                    input_model=input_schema,
                    additional_properties={
                        _MCP_REMOTE_NAME_KEY: tool.name,
                        _MCP_NORMALIZED_NAME_KEY: normalized_name,
                    },
                )
                # Static provenance persisted on the function_call content;
                # identity only — never URLs, headers, env, or command lines.
                set_tool_context(
                    func,
                    {
                        "server_name": self.name,
                        "remote_name": tool.name,
                        "normalized_name": normalized_name,
                    },
                )
                self._functions.append(func)
                self._loaded_tool_remote_names.add(tool.name)

            if not tool_list.nextCursor:
                break
            params = types.PaginatedRequestParams(cursor=tool_list.nextCursor)

        self._tool_call_meta_by_name = tool_call_meta_by_name
        self._tool_task_support_by_name = tool_task_support_by_name
        self._tool_param_names_by_name = tool_param_names_by_name

    def _parse_prompt_result_from_mcp(self, mcp_type: types.GetPromptResult) -> str:
        from mcp import types

        parts: list[str] = []
        for message in mcp_type.messages:
            content = message.content
            if isinstance(content, types.TextContent):
                parts.append(content.text)
            elif isinstance(content, (types.ImageContent, types.AudioContent)):
                parts.append(
                    json.dumps(
                        {
                            "type": "image" if isinstance(content, types.ImageContent) else "audio",
                            "data": content.data,
                            "mimeType": content.mimeType,
                        },
                        default=str,
                    )
                )
            elif isinstance(content, types.EmbeddedResource):
                match content.resource:
                    case types.TextResourceContents():
                        parts.append(content.resource.text)
                    case types.BlobResourceContents():
                        parts.append(
                            json.dumps(
                                {"type": "blob", "data": content.resource.blob, "mimeType": content.resource.mimeType},
                                default=str,
                            )
                        )
            else:
                parts.append(str(content))
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return model_json(parts, default=str)

    def _parse_tool_result_from_mcp(self, mcp_type: types.CallToolResult) -> list[Content]:
        from mcp import types

        result: list[Content] = []
        for item in mcp_type.content:
            match item:
                case types.TextContent():
                    result.append(Content.from_text(item.text))
                case types.ImageContent() | types.AudioContent():
                    result.append(_media_content(item))
                case types.ResourceLink():
                    result.append(_resource_link_text(item))
                case types.EmbeddedResource():
                    match item.resource:
                        case types.TextResourceContents():
                            result.append(Content.from_text(item.resource.text))
                        case types.BlobResourceContents():
                            result.append(_blob_content(item.resource))
                case _:
                    result.append(Content.from_text(str(item)))
        if not result:
            result.append(Content.from_text("null"))
        return result

    def _resolved_extra_args(self, tool_name: str) -> set[str]:
        return self._global_extra_arg_names | self._tool_extra_arg_names.get(tool_name, set())

    def _prepare_call_kwargs(
        self, tool_name: str, kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Build wire arguments from source-separated or directly trusted kwargs.

        Generated MCP functions isolate Chrys runtime kwargs before this
        method.  Direct Python callers of :meth:`call_tool` are trusted, while
        undeclared names still require an explicit extra-argument opt-in.
        """
        raw_user_meta = kwargs.get("_meta")
        user_meta: dict[str, Any] | None = None
        if raw_user_meta is not None and not isinstance(raw_user_meta, dict):
            raise ToolExecutionException("MCP tool metadata provided via _meta must be a dict.")
        if isinstance(raw_user_meta, dict):
            user_meta = {}
            for key, value in raw_user_meta.items():
                if not isinstance(key, str):
                    raise ToolExecutionException("MCP tool metadata provided via _meta must use string keys.")
                user_meta[key] = value

        declared = self._tool_param_names_by_name.get(tool_name, set())
        extras = self._resolved_extra_args(tool_name)
        filtered_kwargs = {k: v for k, v in kwargs.items() if k != "_meta" and (k in declared or k in extras)}
        request_meta = dict(user_meta) if user_meta is not None else None
        request_meta = _inject_otel_into_mcp_meta(request_meta, overwrite=True)
        tool_meta = self._tool_call_meta_by_name.get(tool_name)
        if tool_meta is not None:
            request_meta = {**(request_meta or {}), **tool_meta}
        return filtered_kwargs, request_meta

    async def call_tool(self, tool_name: str, **kwargs: Any) -> str | list[Content]:
        if not self.load_tools_flag:
            raise ToolExecutionException(
                "Tools are not loaded for this server, please set load_tools=True in the constructor."
            )

        if self._tool_task_support_by_name.get(tool_name) == "required":
            raise ModelVisibleToolError(
                f"MCP tool '{tool_name}' requires long-running task support, which {APP_DISPLAY_NAME} does not implement yet."
            )

        filtered_kwargs, meta = self._prepare_call_kwargs(tool_name, kwargs)
        parser = self.parse_tool_results or self._parse_tool_result_from_mcp
        attrs = self._mcp_base_span_attributes()
        attrs.update({OtelAttr.TOOL_NAME: tool_name, OtelAttr.OPERATION: OtelAttr.TOOL_EXECUTION_OPERATION})
        with create_mcp_client_span("tools/call", target=tool_name, attributes=attrs) as span:
            return await self._call_tool_with_retries(tool_name, filtered_kwargs, meta, parser, span)

    async def _call_tool_with_retries(
        self,
        tool_name: str,
        filtered_kwargs: dict[str, Any],
        meta: dict[str, Any] | None,
        parser: Callable[..., str | list[Content]],
        span: otel_trace.Span,
    ) -> str | list[Content]:
        from anyio import ClosedResourceError
        from mcp.shared.exceptions import McpError

        for attempt in range(2):
            # Tool calls are exposed only after the lifecycle installs a session.
            session = cast("ClientSession", self.session)
            try:
                result = sanitize_mcp_result_binary(
                    await session.call_tool(tool_name, arguments=filtered_kwargs, meta=meta)
                )
                if result.isError:
                    # With neither content nor structuredContent, skip the parser and its "null"
                    # placeholder. A structured-only error still parses: the transports' parsers
                    # turn structuredContent into text when content is empty.
                    has_payload = bool(result.content) or result.structuredContent is not None
                    parsed = parser(result) if has_payload else ""
                    text = (
                        "\n".join(c.text for c in parsed if c.type == "text" and c.text)
                        if isinstance(parsed, list)
                        else parsed
                    )
                    # The server's execution error is written for the model to correct its call. With no
                    # text (only media, links and binary resources, or nothing at all) it says so.
                    error = _server_error(truncate_mcp_error(text), f"MCP tool '{tool_name}'")
                    if span.is_recording():
                        set_mcp_span_error(span, "tool_error", error.model_message)
                    raise error
                return parser(result)
            except ToolExecutionException:
                raise
            except (ClosedResourceError, McpError) as call_ex:
                is_session_terminated = (
                    isinstance(call_ex, McpError) and "session terminated" in call_ex.error.message.lower()
                )
                is_connection_lost = isinstance(call_ex, ClosedResourceError) or is_session_terminated
                if not is_connection_lost:
                    message = truncate_mcp_error(
                        call_ex.error.message if isinstance(call_ex, McpError) else str(call_ex)
                    )
                    if span.is_recording():
                        set_mcp_span_error(span, type(call_ex).__name__, message)
                    raise _mcp_error_exception(call_ex, message, f"MCP tool '{tool_name}'") from call_ex

                if attempt == 0:
                    try:
                        await self.connect(reset=True)
                        continue
                    except Exception as reconn_ex:
                        raise ToolExecutionException(
                            "Failed to reconnect to MCP server.", inner_exception=reconn_ex
                        ) from reconn_ex
                if span.is_recording():
                    set_mcp_span_error(span, type(call_ex).__name__, truncate_mcp_error(str(call_ex)))
                raise ToolExecutionException(
                    f"Failed to call tool '{tool_name}' - connection lost.",
                    inner_exception=call_ex,
                ) from call_ex
            except Exception as ex:
                if span.is_recording():
                    set_mcp_span_error(span, type(ex).__name__, str(ex))
                raise ToolExecutionException(f"Failed to call tool '{tool_name}'.", inner_exception=ex) from ex
        raise ToolExecutionException(f"Failed to call tool '{tool_name}' after retries.")

    async def get_prompt(self, prompt_name: str, **kwargs: Any) -> str:
        from anyio import ClosedResourceError
        from mcp.shared.exceptions import McpError

        if not self.load_prompts_flag:
            raise ToolExecutionException(
                "Prompts are not loaded for this server, please set load_prompts=True in the constructor."
            )
        parser = self.parse_prompt_results or self._parse_prompt_result_from_mcp
        attrs = self._mcp_base_span_attributes()
        attrs.update({OtelAttr.PROMPT_NAME: prompt_name})
        with create_mcp_client_span("prompts/get", target=prompt_name, attributes=attrs) as span:
            for attempt in range(2):
                # Prompt functions are exposed only after the lifecycle installs a session.
                session = cast("ClientSession", self.session)
                try:
                    return parser(await session.get_prompt(prompt_name, arguments=kwargs))
                except ClosedResourceError as cl_ex:
                    if attempt == 0:
                        try:
                            await self.connect(reset=True)
                        except Exception as reconn_ex:
                            raise ToolExecutionException(
                                "Failed to reconnect to MCP server.",
                                inner_exception=reconn_ex,
                            ) from reconn_ex
                        continue
                    set_mcp_span_error(span, type(cl_ex).__name__, truncate_mcp_error(str(cl_ex)))
                    raise ToolExecutionException(
                        f"Failed to call prompt '{prompt_name}' - connection lost.", inner_exception=cl_ex
                    ) from cl_ex
                except McpError as mcp_exc:
                    message = truncate_mcp_error(mcp_exc.error.message)
                    set_mcp_span_error(span, type(mcp_exc).__name__, message)
                    raise _mcp_error_exception(mcp_exc, message, f"MCP prompt '{prompt_name}'") from mcp_exc
                except Exception as ex:
                    set_mcp_span_error(span, type(ex).__name__, str(ex))
                    raise ToolExecutionException(f"Failed to call prompt '{prompt_name}'.", inner_exception=ex) from ex
        raise ToolExecutionException(f"Failed to get prompt '{prompt_name}' after retries.")

    async def __aenter__(self) -> Self:
        try:
            await self.connect()
            return self
        except ToolException:
            raise
        except Exception as ex:
            await self.close()
            raise ToolExecutionException("Failed to enter context manager.", inner_exception=ex) from ex

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.close()


async def _expand_mcp_tool(tool_: ToolTypes) -> list[ToolTypes] | None:
    if not isinstance(tool_, MCPTool):
        return None
    if not tool_.is_connected:
        await tool_.connect()
    from chrys.service.mcp.cache import clone_mcp_function_tool

    return normalize_tools(
        clone_mcp_function_tool(function, result_cap=DEFAULT_MCP_TOOL_RESULT_MAX_TOKENS) for function in tool_.functions
    )


_set_tool_expander(_expand_mcp_tool)


class MCPStdioTool(MCPTool):
    """MCP tool for stdio-based servers."""

    def __init__(
        self,
        name: str,
        command: str,
        *,
        tool_name_prefix: str | None = None,
        load_tools: bool = True,
        parse_tool_results: Callable[[types.CallToolResult], str | list[Content]] | None = None,
        load_prompts: bool = True,
        parse_prompt_results: Callable[[types.GetPromptResult], str] | None = None,
        request_timeout: int | None = None,
        session: ClientSession | None = None,
        description: str | None = None,
        allowed_tools: Collection[str] | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        encoding: str | None = None,
        additional_properties: dict[str, Any] | None = None,
        additional_tool_argument_names: Sequence[str] | Mapping[str, str | Sequence[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            allowed_tools=allowed_tools,
            tool_name_prefix=tool_name_prefix,
            additional_properties=additional_properties,
            session=session,
            load_tools=load_tools,
            parse_tool_results=parse_tool_results,
            load_prompts=load_prompts,
            parse_prompt_results=parse_prompt_results,
            request_timeout=request_timeout,
            additional_tool_argument_names=additional_tool_argument_names,
        )
        self.command = command
        self.args = args or []
        self.env = env
        self.encoding = encoding
        self._client_kwargs = kwargs

    def _mcp_base_span_attributes(self) -> dict[str, Any]:
        attrs = super()._mcp_base_span_attributes()
        attrs[OtelAttr.NETWORK_TRANSPORT] = "pipe"
        return attrs

    def get_mcp_client(self) -> _AsyncGeneratorContextManager[Any, None]:
        args: dict[str, Any] = {"command": self.command, "args": self.args, "env": self.env}
        if self.encoding:
            args["encoding"] = self.encoding
        if self._client_kwargs:
            args.update(self._client_kwargs)
        try:
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ModuleNotFoundError as ex:
            raise ModuleNotFoundError("`mcp` is required to use `MCPStdioTool`. Please install `mcp`.") from ex
        return stdio_client(server=StdioServerParameters(**args))


class MCPStreamableHTTPTool(MCPTool):
    """MCP tool for streamable HTTP/SSE servers."""

    def __init__(
        self,
        name: str,
        url: str,
        *,
        tool_name_prefix: str | None = None,
        load_tools: bool = True,
        parse_tool_results: Callable[[types.CallToolResult], str | list[Content]] | None = None,
        load_prompts: bool = True,
        parse_prompt_results: Callable[[types.GetPromptResult], str] | None = None,
        request_timeout: int | None = None,
        session: ClientSession | None = None,
        description: str | None = None,
        allowed_tools: Collection[str] | None = None,
        terminate_on_close: bool | None = None,
        additional_properties: dict[str, Any] | None = None,
        http_client: AsyncClient | None = None,
        additional_tool_argument_names: Sequence[str] | Mapping[str, str | Sequence[str]] | None = None,
        **_kwargs: Any,
    ) -> None:
        """Initialize a streamable HTTP MCP tool.

        Keyword Args:
            http_client: Optional caller-managed HTTP client. Sensitive credentials
                configured directly on this client, such as with
                ``AsyncClient(headers=...)``, bypass Chrys's origin-scoped header
                hooks. If the client follows cross-origin redirects, those
                credentials can be sent to another origin. The caller is responsible
                for scoping them to the configured URL's origin.
        """
        super().__init__(
            name=name,
            description=description,
            allowed_tools=allowed_tools,
            tool_name_prefix=tool_name_prefix,
            additional_properties=additional_properties,
            session=session,
            load_tools=load_tools,
            parse_tool_results=parse_tool_results,
            load_prompts=load_prompts,
            parse_prompt_results=parse_prompt_results,
            request_timeout=request_timeout,
            additional_tool_argument_names=additional_tool_argument_names,
        )
        self.url = url
        self.terminate_on_close = terminate_on_close
        self._httpx_client: AsyncClient | None = http_client

    def _mcp_base_span_attributes(self) -> dict[str, Any]:
        attrs = super()._mcp_base_span_attributes()
        attrs[OtelAttr.NETWORK_TRANSPORT] = "tcp"
        attrs[OtelAttr.NETWORK_PROTOCOL_NAME] = "http"
        try:
            from httpx import URL

            parsed = URL(self.url)
            if parsed.host:
                attrs[OtelAttr.ADDRESS] = parsed.host
            attrs[OtelAttr.PORT] = parsed.port or (443 if parsed.scheme == "https" else 80)
        except Exception:
            logger.debug("Failed to parse URL for MCP span transport attributes", exc_info=True)
        return attrs

    def get_mcp_client(self) -> _AsyncGeneratorContextManager[Any, None]:
        return streamable_http_client(
            url=self.url,
            http_client=self._httpx_client,
            terminate_on_close=self.terminate_on_close if self.terminate_on_close is not None else True,
        )


def streamable_http_client(*args: Any, **kwargs: Any) -> _AsyncGeneratorContextManager[Any, None]:
    try:
        from mcp.client.streamable_http import streamable_http_client as _streamable_http_client
    except ModuleNotFoundError as ex:
        missing_name = ex.name or str(ex)
        if missing_name == "mcp" or missing_name.startswith("mcp.") or "mcp" in missing_name:
            raise ModuleNotFoundError("`MCPStreamableHTTPTool` requires `mcp`. Please install `mcp`.") from ex
        raise ModuleNotFoundError(
            "`MCPStreamableHTTPTool` requires streamable HTTP transport support. "
            f"The optional dependency `{missing_name}` is not installed. Please update your dependencies."
        ) from ex

    return _streamable_http_client(*args, **kwargs)
