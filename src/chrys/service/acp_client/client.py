# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hardened, owned ACP client connection for one external sub-agent attempt."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Any, Protocol, cast

from acp import PROTOCOL_VERSION, Client
from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    ClientCapabilities,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapabilities,
    KillTerminalResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionNotification,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)
from acp.task import (
    InMemoryMessageQueue,
    MessageQueue,
    MessageStateStore,
    NotificationRunner,
    RequestRunner,
    TaskSupervisor,
)
from pydantic import ValidationError

from chrys.foundation.models.ask_user import validate_request_input_params

from . import _transport, protocol
from ._transport import (
    _CURRENT_INBOUND_ID,
    _AcpDisconnectError,
    _AcpProtocolLimitError,
    _BackpressureDispatcher,
    _consume_future_exception,
    _FrameObserver,
    _FramingPump,
    _ObserverBuffer,
    _PauseTransport,
    _PromptBarrier,
    _reap_task,
    _RetainedSender,
    _TrackingStateStore,
    _valid_usage_int,
)
from .errors import (
    AcpAuthRequiredError,
    AcpClientError,
    AcpConfigError,
    AcpConnectError,
    AcpIdleTimeoutError,
    AcpOperation,
    AcpSpawnError,
    AcpTransportError,
    classify_acp_error,
    is_best_effort_option_rejection,
)
from .protocol import _validate_payload_caps, _validate_request_payload_caps, sdk_field_values
from .protocol import encode_protocol_json as encode_protocol_json
from .protocol import parse_protocol_json as parse_protocol_json
from .protocol import validate_json_rpc_envelope as validate_json_rpc_envelope
from .protocol import validate_json_scalar_tree as validate_json_scalar_tree
from .spawn import ACP_STDIO_LIMIT_BYTES, AcpSpawnResult, spawn_acp_process, validate_agent_spec
from .spec import AcpAgentSpec, AcpHandshakeInfo, AcpPromptOutcome, PermissionDecision

logger = logging.getLogger(__name__)

_MAX_PERMISSION_OPTIONS = 128
_CLOSE_RPC_TIMEOUT_SECONDS = 2.0
_FINAL_DRAIN_TIMEOUT_SECONDS = 30.0

_INITIALIZE_METHOD = "initialize"
_NEW_SESSION_METHOD = "session/new"
# The selected outcome carries only an option id, so the id's kind is the
# sole proof that the wire outcome matches the callback's decision.
_DECISION_OPTION_KINDS: dict[str, frozenset[str]] = {
    "allow": frozenset({"allow_once", "allow_always"}),
    "deny": frozenset({"reject_once", "reject_always"}),
}
_META_COLLISION_KEYS = frozenset(
    {
        "options",
        "sessionId",
        "session_id",
        "toolCall",
        "tool_call",
        "kwargs",
        "method",
        "params",
    }
)


class AcpClientCallbacks(Protocol):
    """Orchestration-owned callbacks; the service layer publishes no events."""

    async def on_update(self, seq: int, notification: SessionNotification) -> None: ...

    async def on_permission_request(
        self,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> PermissionDecision: ...

    async def on_ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class AcpUpdateSink(Protocol):
    """Small observer seam consumed by the PR-2 translator."""

    async def put(self, seq: int, notification: SessionNotification) -> None: ...


class AcpClientWaitController(Protocol):
    """PR-2 broker seam for resolving human waits during transport teardown."""

    async def cancel_pending_waits(self) -> None: ...


class _CallbackUpdateSink:
    def __init__(self, callbacks: AcpClientCallbacks) -> None:
        self._callbacks = callbacks

    async def put(self, seq: int, notification: SessionNotification) -> None:
        await self._callbacks.on_update(seq, notification)


class AcpAgentClient:
    """One-process ACP client with strict protocol and lifecycle ownership."""

    def __init__(
        self,
        spec: AcpAgentSpec,
        callbacks: AcpClientCallbacks,
        *,
        update_sink: AcpUpdateSink | None = None,
        wait_controller: AcpClientWaitController | None = None,
    ) -> None:
        self._spec = spec
        self._callbacks = callbacks
        self._update_sink = update_sink or _CallbackUpdateSink(callbacks)
        self._wait_controller = wait_controller
        self._spawn: AcpSpawnResult | None = None
        self._conn: ClientSideConnection | None = None
        self._store: _TrackingStateStore | None = None
        self._sender: _RetainedSender | None = None
        self._dispatcher: _BackpressureDispatcher | None = None
        self._supervisor: TaskSupervisor | None = None
        self._observer: _FrameObserver | None = None
        self._updates: _ObserverBuffer | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._pump: _FramingPump | None = None
        self._terminal_failure: asyncio.Future[BaseException] | None = None
        self._completion_future: asyncio.Future[None] | None = None
        self._handshake_deadline: float | None = None
        self._stateful = False
        self._closing = False
        self._session_id: str | None = None
        self._announced_session_id: str | None = None
        self._session_response_claimed = False
        self._agent_info: Any | None = None
        self._auth_methods: tuple[Any, ...] = ()
        self._capabilities = AgentCapabilities()
        self._connect_started = False
        self._prompt_started = False
        self._cancel_requested = False
        self._cancel_task: asyncio.Task[None] | None = None
        self._force_close_task: asyncio.Task[None] | None = None
        self._failure_close_task: asyncio.Task[None] | None = None
        self._activity_event: asyncio.Event | None = None
        self._last_activity = 0.0

    @property
    def stderr_tail(self) -> str:
        return self._spawn.stderr_tail if self._spawn is not None else ""

    @property
    def stateful_phase_started(self) -> bool:
        return self._stateful

    async def connect(self) -> None:
        """Spawn and initialize; roll back every acquired resource on failure."""
        if self._conn is not None:
            raise RuntimeError("ACP client is already connected.")
        if self._closing:
            raise RuntimeError("ACP client is already closed.")
        # Claimed synchronously before the first await: _conn is only
        # installed after spawning, so two concurrent calls would otherwise
        # both spawn and the second installation would strand the first
        # child beyond force_close's reach.
        if self._connect_started:
            raise RuntimeError("An ACP connection attempt has already been started.")
        self._connect_started = True
        validate_agent_spec(self._spec)
        loop = asyncio.get_running_loop()
        self._handshake_deadline = loop.time() + self._spec.handshake_timeout_seconds
        self._terminal_failure = loop.create_future()
        self._activity_event = asyncio.Event()
        self._last_activity = loop.time()
        try:
            async with asyncio.timeout_at(self._handshake_deadline):
                spawn = await spawn_acp_process(self._spec)
                if self._closing:
                    # A force_close that completed during the spawn await saw
                    # nothing to release, and its cached task never runs
                    # again; this late acquisition must be disposed here.
                    await _dispose_spawn(spawn)
                    raise AcpConnectError("The ACP client was closed.")
                # No suspension between the check above and installation, so a
                # force_close starting any later sees the full component set.
                self._spawn = spawn
                self._install_connection()
                initialize = await self._require_conn().initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                        terminal=False,
                    ),
                    client_info=self._spec.client_info,
                )
                raw_result = self._require_store().latest_raw_result(_INITIALIZE_METHOD)
                raw_version = raw_result.get("protocolVersion") if raw_result is not None else None
                if type(raw_version) is not int or raw_version != PROTOCOL_VERSION:
                    raise AcpConfigError("The ACP agent returned an unsupported protocol version.")
                self._agent_info = initialize.agent_info
                self._auth_methods = tuple(initialize.auth_methods or ())
                self._capabilities = initialize.agent_capabilities or AgentCapabilities()
                terminal = self._require_terminal_failure()
                if terminal.done():
                    raise terminal.result()
        except asyncio.CancelledError:
            await self.force_close()
            raise
        except BaseException as exc:
            classified, failure = self._classified_connect_failure(exc)
            await self.force_close()
            raise classified from failure

    def _classified_connect_failure(self, exc: BaseException) -> tuple[AcpClientError, BaseException]:
        """Pick the classified failure and root cause for a failed connect.

        The terminal future usually holds the most specific failure (the pump
        sees protocol violations the SDK surfaces only as generic closes), but
        a deterministic local classification must not be displaced by it — an
        agent closing stdout right after a bad initialize response would
        otherwise flip from config-rejected to retryable.
        """
        local = classify_acp_error(exc, operation=AcpOperation.INITIALIZE, stateful=False)
        # AcpSpawnError is in the preserved set for the close-vs-spawn race: a
        # force_close landing while process creation is pending settles the
        # terminal with the retryable close classification, which must not
        # displace a deterministic launch failure such as ENOENT.
        if isinstance(local, AcpConfigError | AcpAuthRequiredError | AcpSpawnError):
            return local, exc
        terminal = self._terminal_failure
        if terminal is not None and terminal.done():
            root = terminal.result()
            return classify_acp_error(root, operation=AcpOperation.INITIALIZE, stateful=False), root
        return local, exc

    async def open_session(self) -> AcpHandshakeInfo:
        """Create exactly one fresh remote session and apply configured options."""
        # Single-use: a second session/new would race the pump announcement and
        # every session-id comparison site against the first session's id.
        if self._stateful:
            raise RuntimeError("An ACP session has already been opened for this client.")
        # A background failure that settled after connect returned must fail
        # here: the child may still be running with the connection already
        # known dead, and enqueueing session/new would create an orphaned
        # remote session no later frame can reach. Nothing stateful has been
        # sent yet, so the connect-phase classification (retryable) applies.
        terminal = self._require_terminal_failure()
        if terminal.done():
            root = terminal.result()
            raise classify_acp_error(root, operation=AcpOperation.NEW_SESSION, stateful=False) from root
        self._stateful = True
        conn = self._require_conn()
        try:
            async with asyncio.timeout_at(self._require_handshake_deadline()):
                additional = list(dict.fromkeys(self._spec.additional_directories))
                supported = (
                    self._capabilities.session_capabilities is not None
                    and self._capabilities.session_capabilities.additional_directories is not None
                )
                if additional and not supported:
                    logger.warning("ACP agent does not advertise additionalDirectories; sending cwd only")
                    additional = []
                response = await conn.new_session(
                    cwd=self._spec.cwd,
                    additional_directories=additional or None,
                    mcp_servers=[],
                )
                # The SDK schema accepts any str; an empty id would send ""
                # outbound while _active_session_id() reads it as no-session,
                # rejecting every callback on a "successfully" opened client.
                # The id is retained and echoed on every session frame, so it
                # rides the same cap as retained JSON-RPC ids.
                session_id = response.session_id
                if type(session_id) is not str or not 0 < len(session_id) <= protocol._MAX_REQUEST_ID_CHARS:
                    raise AcpConfigError("The ACP agent returned an invalid session id.")
                self._session_id = session_id
                final_options = await self._apply_session_options(response)
                terminal = self._require_terminal_failure()
                if terminal.done():
                    raise terminal.result()
        except asyncio.CancelledError:
            await self.force_close()
            raise
        except BaseException as exc:
            raise classify_acp_error(exc, operation=AcpOperation.NEW_SESSION, stateful=True) from exc

        if self._cancel_requested:
            await self.cancel()
        return AcpHandshakeInfo(
            session_id=self._session_id,
            agent_info=self._agent_info,
            auth_methods=self._auth_methods,
            capabilities=self._capabilities,
            modes=response.modes,
            models=response.models,
            config_options=final_options,
        )

    async def _apply_session_options(
        self, response: Any
    ) -> tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...]:
        conn = self._require_conn()
        session_id = self._require_session_id()
        if self._spec.session_mode:
            advertised = response.modes is not None and self._spec.session_mode in {
                mode.id for mode in response.modes.available_modes
            }
            if not advertised:
                if self._spec.best_effort_options:
                    logger.warning("Configured ACP session mode is not advertised; continuing best-effort")
                else:
                    raise AcpConfigError("The configured ACP session mode is not advertised.")
            else:
                try:
                    await conn.set_session_mode(mode_id=self._spec.session_mode, session_id=session_id)
                except RequestError as exc:
                    if self._spec.best_effort_options and is_best_effort_option_rejection(exc):
                        logger.warning("ACP agent deterministically rejected session mode; continuing best-effort")
                    else:
                        raise classify_acp_error(exc, operation=AcpOperation.SET_MODE, stateful=True) from exc
                except Exception as exc:
                    raise classify_acp_error(exc, operation=AcpOperation.SET_MODE, stateful=True) from exc
                else:
                    # The session/new snapshot predates this accepted switch;
                    # patch it so the handshake reports the applied state
                    # (config options already get this via the returned sets).
                    response.modes.current_mode_id = self._spec.session_mode

        if self._spec.model_id:
            advertised = response.models is not None and self._spec.model_id in {
                model.model_id for model in response.models.available_models
            }
            if not advertised:
                logger.warning("Configured ACP model is not advertised; continuing with the agent default")
            else:
                try:
                    await conn.set_session_model(model_id=self._spec.model_id, session_id=session_id)
                except RequestError as exc:
                    if is_best_effort_option_rejection(exc):
                        logger.warning("ACP agent rejected the configured model; continuing with the agent default")
                    else:
                        raise classify_acp_error(exc, operation=AcpOperation.SET_MODEL, stateful=True) from exc
                except Exception as exc:
                    raise classify_acp_error(exc, operation=AcpOperation.SET_MODEL, stateful=True) from exc
                else:
                    # Same snapshot-staleness rule as the mode above.
                    response.models.current_model_id = self._spec.model_id

        # Config options may depend on one another, and every set response
        # carries the complete replacement set, so each option validates
        # against the latest returned state rather than the session/new list.
        current_options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...] = tuple(
            response.config_options or ()
        )
        for option_id, value in self._spec.config_options.items():
            if option_id not in {option.id for option in current_options}:
                if self._spec.best_effort_options:
                    logger.warning("Configured ACP option %r is not advertised; continuing best-effort", option_id)
                    continue
                raise AcpConfigError("A configured ACP session option is not advertised.")
            try:
                result = await conn.set_config_option(
                    config_id=option_id,
                    session_id=session_id,
                    value=value,
                )
            except RequestError as exc:
                if self._spec.best_effort_options and is_best_effort_option_rejection(exc):
                    logger.warning("ACP agent rejected option %r; continuing best-effort", option_id)
                    continue
                raise classify_acp_error(exc, operation=AcpOperation.SET_CONFIG_OPTION, stateful=True) from exc
            except Exception as exc:
                raise classify_acp_error(exc, operation=AcpOperation.SET_CONFIG_OPTION, stateful=True) from exc
            if result is not None and result.config_options is not None:
                current_options = tuple(result.config_options)
        return current_options

    async def prompt(self, text: str) -> AcpPromptOutcome:
        """Prompt once and return only after the exact response barrier drains."""
        if type(text) is not str:
            raise AcpConfigError("ACP prompt text must be a string.")
        try:
            validate_json_scalar_tree(text)
        except ValueError as exc:
            raise AcpConfigError("ACP prompt text is not valid Unicode.", cause=exc) from exc
        # One prompt per session, claimed synchronously: a second call would
        # replace the shared completion future and reset the store's prompt
        # registration (cross-wiring barriers and ids under concurrency), and
        # ACP usage is session-cumulative, so a sequential reuse would report
        # cumulative totals as per-attempt usage.
        if self._prompt_started:
            raise RuntimeError("An ACP prompt has already been started for this client.")
        self._prompt_started = True
        conn = self._require_conn()
        store = self._require_store()
        terminal = self._require_terminal_failure()
        loop = asyncio.get_running_loop()
        self._completion_future = loop.create_future()
        self._completion_future.add_done_callback(_consume_future_exception)
        store.reset_prompt()
        self._touch_activity()
        # An already-settled terminal failure must fail before create_task:
        # conn.prompt registers and enqueues session/prompt at its first
        # yield, and cancelling the caller never removes the queued sender
        # item — the remote could execute a prompt after the client failed.
        if terminal.done():
            await self._fail_prompt_without_response(None, terminal.result())
        sdk_prompt_task = asyncio.create_task(
            conn.prompt(
                prompt=[TextContentBlock(type="text", text=text)],
                session_id=self._require_session_id(),
            ),
            name="chrys.acp.prompt",
        )
        registration_task = asyncio.create_task(store.wait_for_prompt_id(), name="chrys.acp.prompt-registration")
        watchdog = self._start_idle_watchdog()
        try:
            registered, _ = await asyncio.wait(
                {registration_task, terminal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if terminal in registered and registration_task not in registered:
                registration_task.cancel()
                await _reap_task(registration_task)
                await self._fail_prompt_without_response(sdk_prompt_task, terminal.result())
            prompt_id = await registration_task

            done, _ = await asyncio.wait(
                {sdk_prompt_task, terminal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            record = store.record_for(prompt_id)
            response_observed = record is not None and record.response_observed
            if terminal in done and not response_observed:
                await self._fail_prompt_without_response(sdk_prompt_task, terminal.result())

            usage = record.usage if record is not None else None
            response = None
            response_error: BaseException | None = None
            try:
                response = await sdk_prompt_task
            except asyncio.CancelledError as exc:
                # This await suspends when the response was observed on the
                # wire while the SDK task is still pending (e.g. a settled
                # terminal woke the wait above). A caller cancellation lands
                # here and must reach the outer handler, which owns the
                # force-close-and-reraise; only an independently cancelled
                # SDK task stays a classified transport failure.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                response_error = exc
            except BaseException as exc:
                response_error = exc

            # The final drain must stay bounded even with the idle watchdog
            # disabled (idle_timeout_seconds=0): a stuck update sink would
            # otherwise hold prompt() open forever after the response arrived.
            drain_error: BaseException | None = None
            completion = self._require_completion_future()
            drained, _ = await asyncio.wait({completion}, timeout=_FINAL_DRAIN_TIMEOUT_SECONDS)
            if completion in drained:
                try:
                    completion.result()
                except BaseException as exc:
                    drain_error = exc
            else:
                drain_error = TimeoutError("ACP update processing did not drain after the prompt response.")

            if drain_error is not None:
                error = AcpTransportError(
                    "ACP updates failed before prompt completion.", cause=drain_error, usage=usage
                )
                await self.force_close()
                raise error from drain_error
            # The response sealed the attempt — success AND error frames both
            # end the turn: outstanding inbound callbacks must not outlive it.
            await self._finish_sealed_prompt()
            if terminal.done():
                root = terminal.result()
                if not isinstance(root, _AcpDisconnectError):
                    # Response-wins is reserved for EOF after the exact
                    # response; any other settled terminal failure (malformed
                    # frame, oversized notification, idle expiry that raced
                    # the response) is a protocol failure the caller must
                    # see — it beats even an in-band error response — with
                    # the observed usage preserved.
                    classified = classify_acp_error(root, operation=AcpOperation.PROMPT, stateful=True)
                    if isinstance(classified, AcpTransportError):
                        classified.usage = usage
                    await self.force_close()
                    raise classified from root
            if response_error is not None:
                classified = classify_acp_error(
                    response_error,
                    operation=AcpOperation.PROMPT,
                    stateful=True,
                )
                if isinstance(classified, AcpTransportError):
                    classified.usage = usage
                    await self.force_close()
                elif terminal.done():
                    # Only the deferred EOF-after-response settles here (the
                    # arbitration above raised every other root). The exact
                    # error response still wins the classification, but the
                    # known-dead transport must be closed before raising:
                    # the deferred disconnect spawned no failure-close.
                    await self.force_close()
                raise classified from response_error
            if terminal.done():
                # Only the deferred EOF-after-response reaches here: the
                # response wins, and this close releases the dead transport.
                await self.force_close()
            # A missing response is possible only on the error path raised immediately above.
            prompt_response = cast(PromptResponse, response)
            return AcpPromptOutcome(
                stop_reason=prompt_response.stop_reason,
                usage=usage,
                user_message_id=prompt_response.user_message_id,
            )
        except asyncio.CancelledError:
            store.reject_all_outgoing(ConnectionError("ACP prompt was cancelled."))
            sdk_prompt_task.cancel()
            await _reap_task(sdk_prompt_task)
            completion = self._completion_future
            if completion is not None and not completion.done():
                completion.cancel()
            await self.force_close()
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await _reap_task(watchdog)
            if not registration_task.done():
                registration_task.cancel()
                await _reap_task(registration_task)

    async def _finish_sealed_prompt(self) -> None:
        """Release inbound waits once the exact prompt response is observed.

        In-flight human callbacks are cancelled and PR-2 waits torn down here
        on the success path; every failure path reaches the same releases via
        the force-close ladder instead.
        """
        if self._wait_controller is not None:
            # Run the PR-2 seam as a task: an inline await could swallow an
            # outer cancellation inside the seam and stall the caller. Unlike
            # the close ladder's _reap_task, this bounded wait must PRESERVE a
            # caller cancellation: prompt()'s CancelledError handler owns the
            # force-close-and-reraise, and suppressing here would hand a
            # successful outcome to a cancelled caller.
            waits_task = asyncio.create_task(
                self._wait_controller.cancel_pending_waits(),
                name="chrys.acp.cancel-waits",
            )
            waits_task.add_done_callback(_consume_future_exception)
            try:
                _done, pending = await asyncio.wait({waits_task}, timeout=_transport._REQUEST_DRAIN_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                waits_task.cancel()
                raise
            for stuck in pending:
                stuck.cancel()
        if self._dispatcher is not None:
            await self._dispatcher.cancel_runners()

    async def _fail_prompt_without_response(
        self,
        sdk_prompt_task: asyncio.Task[Any] | None,
        failure: BaseException,
    ) -> None:
        self._require_store().reject_all_outgoing(ConnectionError("ACP prompt failed."))
        if sdk_prompt_task is not None:
            sdk_prompt_task.cancel()
            await _reap_task(sdk_prompt_task)
        completion = self._completion_future
        if completion is not None:
            if not completion.done():
                completion.set_exception(failure)
            with contextlib.suppress(BaseException):
                await completion
        await self.force_close()
        if isinstance(failure, _AcpDisconnectError):
            raise AcpTransportError("The ACP process closed before returning a prompt response.", cause=failure)
        if isinstance(failure, AcpClientError):
            raise failure
        raise AcpTransportError("The ACP transport failed before a prompt response.", cause=failure) from failure

    async def cancel(self) -> None:
        """Fire a session-cancel notification and return without waiting for the peer."""
        self._cancel_requested = True
        if self._conn is None or self._session_id is None or self._closing:
            return
        if self._cancel_task is not None and not self._cancel_task.done():
            return

        async def send_cancel() -> None:
            with contextlib.suppress(Exception):
                await self._require_conn().cancel(session_id=self._require_session_id())

        self._cancel_task = asyncio.create_task(send_cancel(), name="chrys.acp.cancel")

    async def aclose(self) -> None:
        """Attempt a bounded session close, then always execute the force-close ladder."""
        try:
            if self._conn is not None and self._session_id is not None and not self._closing:
                try:
                    async with asyncio.timeout(_CLOSE_RPC_TIMEOUT_SECONDS):
                        await self._conn.close_session(session_id=self._session_id)
                except RequestError as exc:
                    if not (type(exc.code) is int and exc.code == -32601):
                        logger.debug("ACP session/close failed", exc_info=True)
                except Exception:
                    logger.debug("ACP session/close failed", exc_info=True)
        finally:
            await self.force_close()

    async def force_close(self) -> None:
        """Idempotently close SDK tasks and terminate the retained process tree."""
        if self._force_close_task is None:
            self._force_close_task = asyncio.create_task(self._force_close_impl(), name="chrys.acp.force-close")
        task = self._force_close_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _force_close_impl(self) -> None:
        self._closing = True
        close_failure: AcpClientError = (
            AcpTransportError("The ACP client was closed.")
            if self._stateful
            else AcpConnectError("The ACP client was closed.")
        )
        terminal = self._terminal_failure
        if terminal is not None and not terminal.done():
            terminal.set_result(close_failure)
        completion = self._completion_future
        if completion is not None and not completion.done():
            completion.set_exception(close_failure)
        pump = self._pump
        if pump is not None:
            await pump.stop()
        dispatcher = self._dispatcher
        sender = self._sender
        if dispatcher is not None:
            dispatcher.close_admission()
        if self._observer is not None:
            self._observer.clear()
        if self._wait_controller is not None:
            # Run the PR-2 seam as a task: an inline await could swallow the
            # timeout's single cancellation and stall the ladder forever.
            waits_task = asyncio.create_task(
                self._wait_controller.cancel_pending_waits(),
                name="chrys.acp.cancel-waits",
            )
            await _reap_task(waits_task, timeout_seconds=_transport._REQUEST_DRAIN_TIMEOUT_SECONDS)
        if dispatcher is not None:
            await dispatcher.drain_runners()
        if self._cancel_task is not None:
            self._cancel_task.cancel()
            await _reap_task(self._cancel_task)
        if sender is not None:
            sender.begin_close()

        close_failed = False
        if self._conn is not None:
            close_task = asyncio.create_task(self._conn.close(), name="chrys.acp.sdk-close")
            # asyncio.wait, never `asyncio.timeout: await close_task`: the
            # timeout's single cancellation would forward into close_task,
            # where conn.close()'s own suppress(CancelledError) awaits (our
            # dispatcher.stop, the SDK supervisor.shutdown) can consume it
            # while a wedged sender keeps close_task pending forever.
            close_failed = True
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done, _ = await asyncio.wait({close_task}, timeout=_transport._CLOSE_STAGE_TIMEOUT_SECONDS)
                if close_task in done:
                    close_failed = close_task.cancelled() or close_task.exception() is not None
            if close_failed:
                close_task.cancel()
                await _reap_task(close_task)
        if close_failed:
            if self._store is not None:
                self._store.reject_all_outgoing(ConnectionError("ACP connection close failed."))
            if sender is not None:
                await sender.emergency_stop()
            if dispatcher is not None:
                await dispatcher.emergency_stop()
            # The SDK only shuts its supervisor down after its sender close
            # succeeds; a wedged close would otherwise leak the receive task.
            # Run shutdown as a task: awaited inline, the SDK's own
            # suppress(CancelledError) would swallow the timeout's single
            # cancellation and the next stuck runner would hang the ladder.
            supervisor = self._supervisor
            if supervisor is not None:
                shutdown_task = asyncio.create_task(
                    supervisor.shutdown(),
                    name="chrys.acp.supervisor-shutdown",
                )
                await _reap_task(shutdown_task)

        spawn = self._spawn
        try:
            if spawn is not None:
                exited = False
                try:
                    spawn.process.close_stdin()
                    exited = await _wait_process_bounded(spawn, _transport._CLOSE_STAGE_TIMEOUT_SECONDS)
                    if not exited:
                        spawn.process.terminate_tree()
                        exited = await _wait_process_bounded(spawn, _transport._CLOSE_STAGE_TIMEOUT_SECONDS)
                except OSError:
                    # A failed OS-level wait must not abort the ladder: this
                    # close task is cached, so an escape here would leave every
                    # later force_close re-raising with resources still held.
                    pass
                finally:
                    # Descendants may survive stdin EOF or SIGTERM even when
                    # the direct child exits; the owned process group/Job stays
                    # the authority, so always finish with a kill sweep.
                    spawn.process.kill_tree()
                if not exited:
                    with contextlib.suppress(OSError):
                        await _wait_process_bounded(spawn, _transport._CLOSE_STAGE_TIMEOUT_SECONDS)
        finally:
            if self._updates is not None:
                self._updates.close()
            if self._consumer_task is not None:
                self._consumer_task.cancel()
                await _reap_task(self._consumer_task)
            if spawn is not None:
                await spawn.stderr.stop()
                spawn.process.close_transports()

    def _install_connection(self) -> None:
        spawn = self._spawn
        if spawn is None:
            raise RuntimeError("ACP process has not been spawned.")
        sdk_reader = asyncio.StreamReader(limit=ACP_STDIO_LIMIT_BYTES)
        pause_transport = _PauseTransport()
        # StreamReader uses only pause/resume hooks here, which _PauseTransport implements.
        sdk_reader.set_transport(cast(asyncio.BaseTransport, pause_transport))
        self._store = _TrackingStateStore()
        self._updates = _ObserverBuffer()
        self._observer = _FrameObserver(
            store=self._store,
            updates=self._updates,
            active_session=self._active_session_id,
            fail=self._fail,
            activity=self._touch_activity,
        )
        self._pump = _FramingPump(
            spawn.process.stdout,
            sdk_reader,
            pause_transport,
            stateful=lambda: self._stateful,
            closing=lambda: self._closing,
            preflight=self._preflight_request,
            on_response=self._observe_response_at_pump,
            active_session=self._active_session_id,
            fail=self._fail,
        )

        def sender_factory(writer: asyncio.StreamWriter, supervisor: TaskSupervisor) -> _RetainedSender:
            self._supervisor = supervisor
            self._sender = _RetainedSender(
                writer,
                supervisor,
                store=self._require_store(),
                before_response_send=self._require_observer().before_response_send,
                on_failure=self._fail,
            )
            return self._sender

        def dispatcher_factory(
            queue: MessageQueue,
            supervisor: TaskSupervisor,
            state: MessageStateStore,
            request_runner: RequestRunner,
            notification_runner: NotificationRunner,
        ) -> _BackpressureDispatcher:
            self._dispatcher = _BackpressureDispatcher(
                queue=queue,
                supervisor=supervisor,
                store=state,
                request_runner=request_runner,
                notification_runner=notification_runner,
                on_failure=self._fail,
            )
            return self._dispatcher

        # AcpAgentClient implements the SDK Client protocol; decorators obscure structural matching from ty.
        self._conn = ClientSideConnection(
            cast(Client, self),
            spawn.process.stdin,
            sdk_reader,
            queue=InMemoryMessageQueue(maxsize=256),
            state_store=self._store,
            sender_factory=sender_factory,
            dispatcher_factory=dispatcher_factory,
            observers=[self._observer],
            receive_timeout=None,
        )
        self._pump.start()
        self._consumer_task = asyncio.create_task(self._consume_updates(), name="chrys.acp.updates")

    async def _consume_updates(self) -> None:
        sequence = 0
        try:
            while True:
                item = await self._require_updates().get()
                if isinstance(item, _PromptBarrier):
                    completion = self._completion_future
                    if (
                        completion is not None
                        and not completion.done()
                        and self._store is not None
                        and self._store.prompt_id == item.request_id
                    ):
                        completion.set_result(None)
                    continue
                # Residual guard behind the observer's intake filter — a raw
                # peek, so a foreign frame is dropped before its shape can
                # fail validation and terminate the attempt.
                if item.get("sessionId") != self._active_session_id():
                    logger.warning("Dropping ACP session/update for a foreign session")
                    continue
                if not _valid_raw_usage_update(item):
                    continue
                notification = SessionNotification.model_validate(item)
                sequence += 1
                await self._update_sink.put(sequence, notification)
        except asyncio.CancelledError:
            raise
        except EOFError:
            return
        except BaseException as exc:
            failure = AcpTransportError("ACP update validation or callback failed.", cause=exc)
            completion = self._completion_future
            if completion is not None and not completion.done():
                completion.set_exception(failure)
            self._fail(failure)

    def _fail(self, failure: BaseException) -> None:
        if not self._stateful and isinstance(failure, AcpTransportError):
            if isinstance(failure, _AcpProtocolLimitError):
                # Cap and budget violations replay identically on every
                # respawn (same executable, same greeting), so the connect
                # window must surface them as deterministic incompatibility,
                # not as a retryable connect failure.
                failure = AcpConfigError(failure.detail, cause=failure.cause or failure)
            else:
                failure = AcpConnectError(failure.detail, cause=failure.cause or failure)
        terminal = self._terminal_failure
        if terminal is not None and not terminal.done():
            terminal.set_result(failure)
        completion = self._completion_future
        response_observed = False
        if self._store is not None and self._store.prompt_id is not None:
            record = self._store.record_for(self._store.prompt_id)
            response_observed = record is not None and record.response_observed
        defer_disconnect = isinstance(failure, _AcpDisconnectError) and response_observed
        if completion is not None and not completion.done() and not defer_disconnect:
            completion.set_exception(failure)
        if isinstance(failure, AcpTransportError | AcpConnectError) and not self._closing and not defer_disconnect:
            self._failure_close_task = asyncio.create_task(self.force_close(), name="chrys.acp.failure-close")

    def _active_session_id(self) -> str | None:
        """The authoritative session id, or the pump-announced one before assignment."""
        return self._session_id or self._announced_session_id

    def _observe_response_at_pump(self, message: dict[str, Any]) -> None:
        """Track a matching response synchronously, in pump frame order.

        A response coalesced with EOF in one transport read is processed by
        the pump without yielding, so the SDK observer would mark
        ``response_observed`` only after the EOF classification already ran —
        misclassifying a completed prompt, or a deterministic error response,
        as a transport failure. Observation therefore happens here; sealing
        and the update barrier stay in the SDK observer, which is the only
        point ordered against the queued updates.
        """
        store = self._store
        request_id = message.get("id")
        if store is not None and type(request_id) is int:
            try:
                store.observe_response(request_id, message)
            except AcpTransportError:
                raise
            except BaseException as exc:
                raise AcpTransportError(
                    "The ACP frame observer failed.",
                    cause=exc,
                ) from exc
        self._sniff_session_response(message)

    def _sniff_session_response(self, message: dict[str, Any]) -> None:
        """Capture the announced session id at the pump, ahead of SDK routing.

        ``_session_id`` is assigned only when ``open_session`` resumes after
        the SDK resolves ``session/new``; an agent may write a session-bound
        request back-to-back with that response, and the pump would preflight
        it before the assignment lands. The pump orders frames serially, so
        sniffing the response stream here closes the race for every session
        check downstream (preflight, observer, callbacks, update consumer).
        """
        if self._session_id is not None or self._announced_session_id is not None:
            return
        store = self._store
        request_id = message.get("id")
        if store is None or type(request_id) is not int:
            return
        record = store.record_for(request_id)
        if record is None or record.method != _NEW_SESSION_METHOD or not record.write_committed:
            return
        # First-response claim: the SDK ignores duplicate response ids, but the
        # pump sees every frame before routing — after a failed session/new, a
        # duplicate success frame with the same id must not announce.
        if self._session_response_claimed:
            return
        self._session_response_claimed = True
        result = message.get("result")
        if type(result) is not dict:
            return
        # The announced id is retained and compared on every subsequent frame
        # BEFORE the store's caps run SDK-side, so an uncapped result must
        # never announce; downstream validation still fails the open attempt.
        try:
            _validate_payload_caps(result)
        except ValueError:
            return
        # Caps bound only size and shape; open_session accepts this result
        # solely when it validates as NewSessionResponse, so announcement
        # must clear the same schema gate before exposing the id.
        try:
            NewSessionResponse.model_validate(result)
        except ValidationError:
            return
        session_id = result.get("sessionId")
        if type(session_id) is str and 0 < len(session_id) <= protocol._MAX_REQUEST_ID_CHARS:
            self._announced_session_id = session_id

    def _touch_activity(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._last_activity = asyncio.get_running_loop().time()
        if self._activity_event is not None:
            self._activity_event.set()

    def _start_idle_watchdog(self) -> asyncio.Task[None] | None:
        if self._spec.idle_timeout_seconds == 0:
            return None
        return asyncio.create_task(self._idle_watchdog(), name="chrys.acp.idle-watchdog")

    async def _idle_watchdog(self) -> None:
        timeout_seconds = self._spec.idle_timeout_seconds
        event = self._activity_event
        if event is None:
            return
        while True:
            event.clear()
            # The exact prompt response ends the agent's idle responsibility;
            # draining already-received updates through a slow local sink is
            # governed by the final-drain bound, never blamed on the agent.
            if self._prompt_response_sealed():
                return
            observer = self._observer
            if observer is not None and observer.human_wait_count:
                await event.wait()
                continue
            remaining = timeout_seconds - (asyncio.get_running_loop().time() - self._last_activity)
            if remaining <= 0:
                self._fail(AcpIdleTimeoutError("The ACP agent stopped producing protocol activity."))
                return
            try:
                async with asyncio.timeout(remaining):
                    await event.wait()
            except TimeoutError:
                # The seal and the expiry timer can land on the same wake;
                # a sealed attempt must never fail as agent-idle.
                if self._prompt_response_sealed():
                    return
                self._fail(AcpIdleTimeoutError("The ACP agent stopped producing protocol activity."))
                return

    def _prompt_response_sealed(self) -> bool:
        observer = self._observer
        if observer is not None and observer.prompt_sealed:
            return True
        # The pump marks the prompt record strictly before the SDK observer
        # runs the seal; a response already observed on the wire must never
        # lose to idle expiry inside that scheduling window.
        store = self._store
        if store is None or store.prompt_id is None:
            return False
        record = store.record_for(store.prompt_id)
        return record is not None and record.response_observed

    def _preflight_request(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            params = message.get("params")
            if type(params) is not dict:
                raise ValueError("ACP request params must be an object.")
            _validate_request_payload_caps(params)
            active = self._active_session_id()
            if active is None or type(params.get("sessionId")) is not str or params["sessionId"] != active:
                raise ValueError("ACP request is not bound to the active session.")
            # The SDK merges the metadata into the callback's arguments, read
            # under either spelling.
            for meta in sdk_field_values(params, "_meta", "field_meta"):
                if meta is not None and (type(meta) is not dict or _META_COLLISION_KEYS.intersection(meta)):
                    raise ValueError("ACP request metadata collides with callback fields.")
            method = message["method"]
            if method == protocol._PERMISSION_METHOD:
                _preflight_permission(params)
            elif method == protocol._ASK_USER_METHOD:
                _preflight_ask_user(params)
        except ValueError:
            replacement = dict(message)
            replacement["params"] = {}
            return replacement
        return message

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        if session_id != self._active_session_id():
            raise RequestError.invalid_params({"details": "Foreign ACP session."})
        request_id = _CURRENT_INBOUND_ID.get()
        observer = self._require_observer()
        if not observer.human_wait_admitted(request_id):
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        options_by_id: dict[str, PermissionOption] = {}
        for option in options:
            if option.option_id in options_by_id:
                # The wire outcome names only an option id, so duplicate ids
                # hand the agent the choice of what a selection meant — no
                # response can be transmitted faithfully.
                observer.clear_human_wait(request_id)
                raise RequestError.invalid_params({"details": "Duplicate ACP permission option ids."})
            options_by_id[option.option_id] = option
        try:
            try:
                decision = await self._callbacks.on_permission_request(tool_call, tuple(options))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(AcpTransportError("The ACP permission callback failed.", cause=exc))
                raise
        finally:
            observer.clear_human_wait(request_id)
        if decision.action == "cancelled" or decision.option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        selected = options_by_id.get(decision.option_id)
        if selected is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if selected.kind not in _DECISION_OPTION_KINDS.get(decision.action, frozenset()):
            # An id of the wrong kind must never transmute the decision —
            # deny stays deny even when the callback names an allow option.
            logger.warning("An ACP permission decision does not match its selected option kind; cancelling")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=decision.option_id),
        )

    async def session_update(self, **kwargs: Any) -> None:
        return

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = _CURRENT_INBOUND_ID.get()
        try:
            if method != "chrys/request_input":
                raise RequestError.method_not_found(method)
            active = self._active_session_id()
            if active is None:
                raise RequestError.invalid_params({"details": "Foreign ACP session."})
            _validate_ext_params(method, params, active)
            if not self._require_observer().human_wait_admitted(request_id):
                return {"cancelled": True}
            try:
                return await self._callbacks.on_ext_method(method, params)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(AcpTransportError("The ACP request-input callback failed.", cause=exc))
                raise
        finally:
            if method == "chrys/request_input":
                self._require_observer().clear_human_wait(request_id)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **kwargs: Any,
    ) -> WriteTextFileResponse | None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> ReleaseTerminalResponse | None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> KillTerminalResponse | None:
        raise RequestError.method_not_found("terminal/kill")

    def on_connect(self, conn: Any) -> None:
        return

    def _require_conn(self) -> ClientSideConnection:
        if self._conn is None:
            raise RuntimeError("ACP client is not connected.")
        return self._conn

    def _require_store(self) -> _TrackingStateStore:
        if self._store is None:
            raise RuntimeError("ACP state store is not installed.")
        return self._store

    def _require_observer(self) -> _FrameObserver:
        if self._observer is None:
            raise RuntimeError("ACP frame observer is not installed.")
        return self._observer

    def _require_updates(self) -> _ObserverBuffer:
        if self._updates is None:
            raise RuntimeError("ACP update buffer is not installed.")
        return self._updates

    def _require_terminal_failure(self) -> asyncio.Future[BaseException]:
        if self._terminal_failure is None:
            raise RuntimeError("ACP terminal failure future is not installed.")
        return self._terminal_failure

    def _require_completion_future(self) -> asyncio.Future[None]:
        if self._completion_future is None:
            raise RuntimeError("ACP completion future is not installed.")
        return self._completion_future

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("ACP session is not open.")
        return self._session_id

    def _require_handshake_deadline(self) -> float:
        if self._handshake_deadline is None:
            raise RuntimeError("ACP handshake has not started.")
        return self._handshake_deadline


def _preflight_permission(params: dict[str, Any]) -> None:
    options = params.get("options")
    tool_call = params.get("toolCall")
    if type(options) is not list or len(options) > _MAX_PERMISSION_OPTIONS or type(tool_call) is not dict:
        raise ValueError("Malformed ACP permission request.")
    for option in options:
        if type(option) is not dict:
            raise ValueError("Malformed ACP permission option.")
        if type(option.get("optionId")) is not str or type(option.get("name")) is not str:
            raise ValueError("Malformed ACP permission option.")
    for field in ("toolCallId",):
        if type(tool_call.get(field)) is not str:
            raise ValueError("Malformed ACP permission tool call.")
    if "title" in tool_call and tool_call["title"] is not None and type(tool_call["title"]) is not str:
        raise ValueError("Malformed ACP permission title.")


def _preflight_ask_user(params: dict[str, Any]) -> None:
    validate_request_input_params(params)


def _validate_ext_params(method: str, params: dict[str, Any], session_id: str) -> None:
    if type(method) is not str or type(params) is not dict:
        raise RequestError.invalid_params({"details": "Malformed ACP extension request."})
    try:
        validate_json_scalar_tree(params)
        _validate_request_payload_caps(params)
        if type(params.get("sessionId")) is not str or params["sessionId"] != session_id:
            raise ValueError
        if method == "chrys/request_input":
            _preflight_ask_user(params)
    except ValueError as exc:
        raise RequestError.invalid_params({"details": "Malformed ACP extension request."}) from exc


def _valid_raw_usage_update(params: dict[str, Any]) -> bool:
    update = params.get("update")
    if type(update) is not dict:
        return True
    if not any(kind == "usage_update" for kind in sdk_field_values(update, "sessionUpdate", "session_update")):
        return True
    return _valid_usage_int(update.get("used")) and _valid_usage_int(update.get("size"))


async def _dispose_spawn(spawn: AcpSpawnResult) -> None:
    """Release a spawn acquired after an already-completed force_close ran."""
    spawn.process.kill_tree()
    try:
        await _wait_process_bounded(spawn, _transport._CLOSE_STAGE_TIMEOUT_SECONDS)
    finally:
        await spawn.stderr.stop()
        spawn.process.close_transports()


async def _wait_process_bounded(spawn: AcpSpawnResult, timeout_seconds: float) -> bool:
    if spawn.process.returncode is not None:
        return True
    try:
        async with asyncio.timeout(timeout_seconds):
            await spawn.process.wait()
    except TimeoutError:
        return False
    return True
