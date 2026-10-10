# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scenario-driven stdio ACP agent used by service-client tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import acp
from acp import schema
from acp.exceptions import RequestError

_SESSION_ID = "stub-session"
# Each file adds 11 items to a tool call, past 4,096 in all from 372 files.
WIDE_PATCH_FILES = 600


class StubAgent:
    """Minimal ACP agent with behavior selected by ``CHRYS_ACP_STUB_SCENARIO``."""

    def __init__(self) -> None:
        self._scenario = os.environ.get("CHRYS_ACP_STUB_SCENARIO", "happy")
        self._conn: Any | None = None
        self._cancelled = asyncio.Event()

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse | dict[str, Any]:
        if self._scenario == "initialize_crash_once":
            flag_path = os.environ["CHRYS_ACP_STUB_FLAG_FILE"]
            try:
                fd = os.open(flag_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                os._exit(8)
        if self._scenario == "banner":
            os.write(1, b"not json\n")
            await asyncio.Event().wait()
        if self._scenario == "truncated":
            os.write(1, b'{"jsonrpc":"2.0"')
            os._exit(1)
        if self._scenario == "protocol_mismatch":
            return schema.InitializeResponse(protocolVersion=999)
        if self._scenario == "protocol_bool":
            return {"protocolVersion": True}
        if self._scenario == "protocol_float":
            return {"protocolVersion": 1.0}
        if self._scenario == "initialize_stall":
            await asyncio.Event().wait()
        capabilities = schema.AgentCapabilities(
            sessionCapabilities=(
                schema.SessionCapabilities(close=schema.SessionCloseCapabilities())
                if self._scenario == "no_additional_capability"
                else schema.SessionCapabilities(
                    additionalDirectories=schema.SessionAdditionalDirectoriesCapabilities(),
                    close=schema.SessionCloseCapabilities(),
                )
            )
        )
        return schema.InitializeResponse(
            protocolVersion=protocol_version,
            agentCapabilities=capabilities,
            agentInfo=schema.Implementation(name="chrys-acp-stub", version="1"),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        if self._scenario == "crash_on_new_session":
            os._exit(9)
        if self._scenario == "empty_session_id":
            return schema.NewSessionResponse(sessionId="")
        if self._scenario == "auth_required":
            raise RequestError.auth_required({"authMethods": [{"id": "login", "name": "Agent login"}]})
        if self._scenario == "no_additional_capability" and additional_directories:
            raise RequestError.invalid_params({"details": "additional directories were not gated"})
        modes = schema.SessionModeState(
            currentModeId="default",
            availableModes=[
                schema.SessionMode(id="default", name="Default"),
                schema.SessionMode(id="review", name="Review"),
            ],
        )
        models = schema.SessionModelState(
            currentModelId="stub-model",
            availableModels=[
                schema.ModelInfo(modelId="stub-model", name="Stub model"),
                schema.ModelInfo(modelId="alt-model", name="Alt model"),
            ],
        )
        options = [
            schema.SessionConfigOptionBoolean(
                id="safe",
                name="Safe",
                type="boolean",
                currentValue=True,
            )
        ]
        return schema.NewSessionResponse(
            sessionId=_SESSION_ID,
            modes=modes,
            models=models,
            configOptions=options,
        )

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> schema.SetSessionModeResponse:
        if self._scenario == "new_session_permission_update_failure":
            await self._send_text("before open_session returned")
            response = await self._require_conn().request_permission(
                session_id=_SESSION_ID,
                tool_call=schema.ToolCallUpdate(
                    toolCallId="opening-permission", title="Opening permission", kind="read", rawInput={"phase": "new"}
                ),
                options=[schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once")],
            )
            Path(os.environ["CHRYS_ACP_STUB_FLAG_FILE"]).write_text(
                json.dumps({"pid": os.getpid(), "outcome": response.model_dump(mode="json", by_alias=True)}),
                encoding="utf-8",
            )
            os._exit(9)
        if self._scenario == "mode_rejection":
            raise RequestError.invalid_params({"details": "mode rejected"})
        if self._scenario == "mode_parse_error":
            raise RequestError.parse_error({"details": "mode parse failure"})
        return schema.SetSessionModeResponse()

    async def set_session_model(
        self,
        model_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> schema.SetSessionModelResponse:
        if self._scenario == "model_rejection":
            raise RequestError.invalid_params({"details": "model rejected"})
        if self._scenario == "model_resource_error":
            raise RequestError.resource_not_found("model")
        return schema.SetSessionModelResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse:
        if self._scenario == "option_rejection":
            raise RequestError.invalid_params({"details": "option rejected"})
        if self._scenario == "option_parse_error":
            raise RequestError.parse_error({"details": "option parse failure"})
        if self._scenario == "dependent_config_options":
            # Selecting "safe" introduces "turbo": each response carries the
            # complete replacement set per the ACP config-option contract.
            def _option(option_id: str, current: str | bool) -> schema.SessionConfigOptionBoolean:
                return schema.SessionConfigOptionBoolean(
                    id=option_id,
                    name=option_id.capitalize(),
                    type="boolean",
                    currentValue=bool(current),
                )

            if config_id == "safe":
                return schema.SetSessionConfigOptionResponse(
                    configOptions=[_option("safe", value), _option("turbo", False)]
                )
            if config_id == "turbo":
                return schema.SetSessionConfigOptionResponse(
                    configOptions=[_option("safe", False), _option("turbo", value)]
                )
            raise RequestError.invalid_params({"details": f"unknown option {config_id}"})
        return schema.SetSessionConfigOptionResponse(configOptions=[])

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> schema.PromptResponse | dict[str, Any]:
        if self._scenario == "text_then_stall":
            await self._require_conn().session_update(
                session_id=_SESSION_ID, update=acp.start_tool_call("checkpoint-tool", "Read", kind="read")
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.update_tool_call("checkpoint-tool", status="completed", raw_output="done"),
            )
            await self._send_text("checkpoint progress")
            await self._cancelled.wait()
            return schema.PromptResponse(stopReason="cancelled")
        if self._scenario == "idle_stall":
            await self._cancelled.wait()
            return schema.PromptResponse(stopReason="cancelled")
        if self._scenario == "crash_mid_prompt":
            os._exit(7)
        if self._scenario in {"retry_reused_tool_usage", "timeout_reused_tool_usage"}:
            # Two real transports intentionally reuse both remote identities.
            # Persist their input/PID so the parent test can prove fresh prompt
            # execution without inferring it from the controller's counter.
            trace_path = Path(os.environ["CHRYS_ACP_STUB_FLAG_FILE"])
            first = not trace_path.exists()
            previous = "" if first else await asyncio.to_thread(trace_path.read_text, encoding="utf-8")
            entry = {
                "pid": os.getpid(),
                "session_id": session_id,
                "prompt": [item.text for item in prompt if isinstance(item, schema.TextContentBlock)],
            }
            await asyncio.to_thread(trace_path.write_text, previous + json.dumps(entry) + "\n", encoding="utf-8")
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.start_tool_call("reused", "Read", kind="read"),
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.update_tool_call("reused", status="completed", raw_output="read result"),
            )
            if first:
                await self._send_text("failed pass text")
                if self._scenario == "timeout_reused_tool_usage":
                    await self._cancelled.wait()
                    return schema.PromptResponse(
                        stopReason="cancelled", usage=schema.Usage(inputTokens=8, outputTokens=13, totalTokens=21)
                    )
                raise RequestError.internal_error({"usage": {"inputTokens": 8, "outputTokens": 13, "totalTokens": 21}})
        if self._scenario == "text_then_crash_once":
            flag_path = os.environ["CHRYS_ACP_STUB_FLAG_FILE"]
            try:
                fd = os.open(flag_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                await self._send_text("discarded partial")
                os._exit(7)
        if self._scenario == "oversized_frame":
            # asyncio puts fd 1 in non-blocking mode: one os.write would be a
            # silent short write, so push the whole flood chunk by chunk.
            view = memoryview(b"x" * (50 * 1024 * 1024 + 1) + b"\n")
            while view:
                try:
                    written = os.write(1, view[: 1024 * 1024])
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                    continue
                view = view[written:]
            await asyncio.Event().wait()
        if self._scenario == "oversized_notification":
            # A protocol-legal ext notification whose params bust the item cap,
            # far below the 50 MiB reader limit: a wide collection costs much
            # more to parse and walk than its bytes.
            frame = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "_zeta/blob",
                    "params": {"blob": list(range(5000))},
                },
                separators=(",", ":"),
            ).encode()
            view = memoryview(frame + b"\n")
            while view:
                try:
                    written = os.write(1, view[: 1024 * 1024])
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                    continue
                view = view[written:]
            await asyncio.Event().wait()
        if self._scenario == "prompt_internal_with_usage":
            raise RequestError.internal_error(
                {
                    "usage": {
                        "inputTokens": 8,
                        "outputTokens": 13,
                        "totalTokens": 21,
                    }
                }
            )
        if self._scenario == "update_then_internal_error":
            # A gateable update ahead of an error response: tests park the
            # sink on it to interleave work between barrier drain and resume.
            await self._send_text("pre")
            raise RequestError.internal_error(
                {
                    "usage": {
                        "inputTokens": 8,
                        "outputTokens": 13,
                        "totalTokens": 21,
                    }
                }
            )
        if self._scenario == "stderr_flood":
            os.write(2, b"x" * (6 * 1024 * 1024))
        if self._scenario == "malformed_prompt_response":
            return {
                "stopReason": "not-a-stop-reason",
                "usage": {"inputTokens": 13, "outputTokens": 21, "totalTokens": 34},
            }
        if self._scenario == "usage_absent":
            return {"stopReason": "end_turn"}
        if self._scenario == "usage_gauge":
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=schema.UsageUpdate(
                    sessionUpdate="usage_update",
                    used=120,
                    size=1_000,
                ),
            )
        if self._scenario in {"permission_unresolved", "permission_unresolved_error"}:
            # Fire a permission request but end the prompt while it is still
            # outstanding; the flag file is the test's signal that the
            # client-side callback is already in flight.
            pending = asyncio.ensure_future(
                self._require_conn().request_permission(
                    session_id=_SESSION_ID,
                    tool_call=schema.ToolCallUpdate(toolCallId="unresolved-1", title="Run tool"),
                    options=[
                        schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                    ],
                )
            )
            pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            flag_path = os.environ["CHRYS_ACP_STUB_FLAG_FILE"]
            # Cross-process signal: the flag file is created by the test in
            # another process, so no in-process Event can replace this poll.
            while not os.path.exists(flag_path):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            if self._scenario == "permission_unresolved_error":
                raise RequestError.invalid_params({"details": "prompt rejected at completion"})
            return schema.PromptResponse(
                stopReason="end_turn",
                usage=schema.Usage(inputTokens=3, outputTokens=5, totalTokens=8),
            )
        if self._scenario == "text_then_flag_then_done":
            # One consumed update, then hold the response until the test's
            # flag: the gap is where tests interleave work against a live,
            # still-unsealed attempt.
            await self._send_text("pre")
            flag_path = os.environ["CHRYS_ACP_STUB_FLAG_FILE"]
            # Cross-process signal: the flag file is created by the test in
            # another process, so no in-process Event can replace this poll.
            while not os.path.exists(flag_path):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            return schema.PromptResponse(
                stopReason="end_turn",
                usage=schema.Usage(inputTokens=3, outputTokens=5, totalTokens=8),
            )
        if self._scenario in {"permission", "permission_then_stall"}:
            response = await self._require_conn().request_permission(
                session_id=_SESSION_ID,
                tool_call=schema.ToolCallUpdate(toolCallId="permission-1", title="Run tool"),
                options=[
                    schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                    schema.PermissionOption(optionId="deny", name="Deny", kind="reject_once"),
                ],
            )
            selected = (
                response.outcome.option_id
                if isinstance(response.outcome, schema.AllowedOutcome)
                else response.outcome.outcome
            )
            # Stderr proof that the response bytes reached this process — the
            # close-drain test observes it after the update lane is torn down.
            sys.stderr.write(f"permission-outcome:{selected}\n")
            sys.stderr.flush()
            await self._send_text(f"permission:{selected}")
            if self._scenario == "permission_then_stall":
                await self._cancelled.wait()
        elif self._scenario == "permission_flood":

            async def _ask(index: int) -> str:
                response = await self._require_conn().request_permission(
                    session_id=_SESSION_ID,
                    tool_call=schema.ToolCallUpdate(toolCallId=f"flood-{index}", title="Flood"),
                    options=[
                        schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                        schema.PermissionOption(optionId="deny", name="Deny", kind="reject_once"),
                    ],
                )
                outcome = response.outcome
                return "allow" if isinstance(outcome, schema.AllowedOutcome) else outcome.outcome

            outcomes = await asyncio.gather(*(_ask(index) for index in range(12)))
            allowed = sum(1 for outcome in outcomes if outcome == "allow")
            cancelled = sum(1 for outcome in outcomes if outcome == "cancelled")
            await self._send_text(f"flood:{allowed},{cancelled}")
        elif self._scenario == "ask_user":
            response = await self._require_conn().ext_method(
                "chrys/request_input",
                {
                    "sessionId": _SESSION_ID,
                    "requestId": "question-1",
                    "questions": [{"question": "Continue?", "options": [{"label": "yes"}, {"label": "no"}]}],
                    "callerName": "stub",
                },
            )
            answers = response.get("answers") or [{}]
            await self._send_text("answer:" + ",".join(answers[0].get("values", [])))
        elif self._scenario == "meta_collision":
            # The SDK reads the metadata by its Python name too.
            replacement = {"tool_call": {"toolCallId": "replacement", "title": "Replacement"}}
            for key, meta in (("_meta", {"options": []}), ("_meta", replacement), ("field_meta", replacement)):
                try:
                    await self._require_conn()._conn.send_request(
                        "session/request_permission",
                        {
                            "sessionId": _SESSION_ID,
                            "toolCall": {"toolCallId": "collision", "title": "Collision"},
                            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
                            key: meta,
                        },
                    )
                except RequestError as exc:
                    await self._send_text(f"preflight:{key}:{exc.code}")
        elif self._scenario == "unknown_ext":
            try:
                await self._require_conn()._conn.send_request(
                    "_unknown/method",
                    {"sessionId": _SESSION_ID},
                )
            except RequestError as exc:
                await self._send_text(f"extension:{exc.code}")
        elif self._scenario == "ask_user_extra_field":
            try:
                await self._require_conn()._conn.send_request(
                    "_chrys/request_input",
                    {
                        "sessionId": _SESSION_ID,
                        "requestId": "question-extra",
                        "questions": [{"question": "Continue?", "options": [{"label": "yes"}]}],
                        "callerName": "stub",
                        "unexpected": "value",
                    },
                )
            except RequestError as exc:
                await self._send_text(f"ask-preflight:{exc.code}")
        elif self._scenario == "foreign_update":
            await self._require_conn()._conn.send_notification(
                "session/update",
                {
                    "sessionId": "foreign-session",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "foreign"},
                    },
                },
            )
            await self._send_text("local")
        elif self._scenario == "malformed_foreign_update":
            # Foreign traffic is inert by contract: a frame that would fail
            # schema validation must not be able to end the active attempt.
            await self._require_conn()._conn.send_notification(
                "session/update",
                {"sessionId": "foreign-session", "update": {"sessionUpdate": "bogus_kind"}},
            )
            await self._send_text("local")
        elif self._scenario == "oversized_foreign_update":
            # A foreign frame that would violate the retained-payload caps
            # must be dropped at the pump, not turned transport-fatal.
            await self._require_conn()._conn.send_notification(
                "session/update",
                {"sessionId": "foreign-session", "update": {"blob": list(range(5000))}},
            )
            await self._send_text("local")
        elif self._scenario == "large_payloads":
            # A sub-agent writing a large file: the content rides an extension
            # notification, a tool call and a permission request, each string
            # past 256 Ki characters and each payload past 1 MiB.
            content = "z" * (1024 * 1024 + 64)
            await self._require_conn()._conn.send_notification("_zeta/blob", {"blob": content})
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.start_tool_call("write-1", "Write big.txt", kind="edit", raw_input={"content": content}),
            )
            response = await self._require_conn().request_permission(
                session_id=_SESSION_ID,
                tool_call=schema.ToolCallUpdate(
                    toolCallId="write-1", title="Write big.txt", rawInput={"content": content}
                ),
                options=[
                    schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                    schema.PermissionOption(optionId="deny", name="Deny", kind="reject_once"),
                ],
            )
            selected = (
                response.outcome.option_id
                if isinstance(response.outcome, schema.AllowedOutcome)
                else response.outcome.outcome
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.update_tool_call("write-1", status="completed", raw_output=content),
            )
            await self._send_text(f"permission:{selected}")
        elif self._scenario == "wide_patch":
            # One patch over hundreds of files: a diff, a location and an
            # argument entry for each, far past 4,096 items in the tool call
            # and in the permission request.
            paths = [f"/w/src/module_{index}.py" for index in range(WIDE_PATCH_FILES)]
            content = [acp.tool_diff_content(path, "new\n", "old\n") for path in paths]
            locations = [schema.ToolCallLocation(path=path, line=1) for path in paths]
            raw_input = {"changes": {path: {"type": "update", "unified_diff": "-old\n+new\n"} for path in paths}}
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.start_tool_call(
                    "patch-1", "Apply patch", kind="edit", content=content, locations=locations, raw_input=raw_input
                ),
            )
            response = await self._require_conn().request_permission(
                session_id=_SESSION_ID,
                tool_call=schema.ToolCallUpdate(
                    toolCallId="patch-1", title="Apply patch", content=content, locations=locations, rawInput=raw_input
                ),
                options=[
                    schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                    schema.PermissionOption(optionId="deny", name="Deny", kind="reject_once"),
                ],
            )
            selected = (
                response.outcome.option_id
                if isinstance(response.outcome, schema.AllowedOutcome)
                else response.outcome.outcome
            )
            await self._send_text(f"permission:{selected}")
        elif self._scenario == "permission_duplicate_ids":
            try:
                await self._require_conn().request_permission(
                    session_id=_SESSION_ID,
                    tool_call=schema.ToolCallUpdate(toolCallId="duplicate-1", title="Run tool"),
                    options=[
                        schema.PermissionOption(optionId="allow", name="Allow", kind="allow_once"),
                        schema.PermissionOption(optionId="allow", name="Reject", kind="reject_once"),
                    ],
                )
                await self._send_text("dup:answered")
            except RequestError as exc:
                await self._send_text(f"dup:{exc.code}")
        elif self._scenario == "invalid_usage_updates":
            # The SDK reads the tag by its Python name too, and would coerce these.
            for tag in ("sessionUpdate", "session_update"):
                for value in (True, 1.5, "5", 1 << 80):
                    await self._require_conn()._conn.send_notification(
                        "session/update",
                        {
                            "sessionId": _SESSION_ID,
                            "update": {tag: "usage_update", "used": value, "size": 100},
                        },
                    )
            await self._send_text("valid after invalid usage")
        elif self._scenario == "depth":
            await self._send_text(os.environ.get("CHRYS_ACP_SUBAGENT_DEPTH", "missing"))
        elif self._scenario == "duplicate_inbound_id":
            payload = (
                b'{"jsonrpc":"2.0","id":"duplicate","method":"fs/read_text_file",'
                b'"params":{"sessionId":"stub-session","path":"x"}}\n'
            )
            os.write(1, payload + payload)
            await asyncio.Event().wait()
        elif self._scenario in {"burst", "queue_overflow"}:
            count = int(
                os.environ.get(
                    "CHRYS_ACP_STUB_BURST_COUNT",
                    "5000" if self._scenario == "queue_overflow" else "100",
                )
            )
            for index in range(count):
                await self._send_text(f"{index},")
        elif self._scenario == "message_id_boundaries":
            for message_id, text in (
                ("11111111-1111-4111-8111-111111111111", "first"),
                ("11111111-1111-4111-8111-111111111111", " continuation"),
                ("22222222-2222-4222-8222-222222222222", "second"),
            ):
                await self._require_conn().session_update(
                    session_id=_SESSION_ID,
                    update=schema.AgentMessageChunk(
                        sessionUpdate="agent_message_chunk",
                        content=schema.TextContentBlock(type="text", text=text),
                        messageId=message_id,
                    ),
                )
        elif self._scenario == "non_text_chunks":
            for content in (
                schema.ImageContentBlock(type="image", data="AA==", mimeType="image/png"),
                schema.AudioContentBlock(type="audio", data="AA==", mimeType="audio/wav"),
                schema.ResourceContentBlock(
                    type="resource_link",
                    name="artifact",
                    uri="file:///artifact.txt",
                ),
            ):
                await self._require_conn().session_update(
                    session_id=_SESSION_ID,
                    update=schema.AgentMessageChunk(
                        sessionUpdate="agent_message_chunk",
                        content=content,
                    ),
                )
        elif self._scenario == "tool_anomalies":
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.update_tool_call("tool-1", status="completed", raw_output="done"),
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.start_tool_call("tool-1", "late start", kind="execute"),
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.update_tool_call("tool-1", status="failed", raw_output="duplicate terminal"),
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.update_tool_call("tool-2", title="progress before start", raw_input={"a": 1}),
            )
            await self._require_conn().session_update(
                session_id=_SESSION_ID,
                update=acp.start_tool_call("tool-3", "terminal start", status="completed"),
            )
            await self._send_text("tool complete")
        elif self._scenario == "error_prefix":
            await self._send_text("Error: is the literal prefix requested in this example.")
        elif self._scenario != "empty":
            await self._send_text("stub response")

        stop_reason = {
            "refusal": "refusal",
            "prompt_truncated": "max_tokens",
            "prompt_cancelled": "cancelled",
        }.get(self._scenario, "end_turn")
        return schema.PromptResponse(
            stopReason=stop_reason,
            usage=schema.Usage(
                inputTokens=3,
                outputTokens=5,
                totalTokens=8,
                cachedReadTokens=1,
            ),
        )

    async def close_session(self, session_id: str, **kwargs: Any) -> schema.CloseSessionResponse:
        if self._scenario == "wedged_close":
            await asyncio.Event().wait()
        return schema.CloseSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._cancelled.set()

    async def _send_text(self, text: str) -> None:
        await self._require_conn().session_update(
            session_id=_SESSION_ID,
            update=acp.update_agent_message_text(text),
        )

    def _require_conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Stub ACP connection is not installed.")
        return self._conn


async def _main() -> None:
    scenario = os.environ.get("CHRYS_ACP_STUB_SCENARIO")
    if scenario == "response_then_eof":
        await _run_response_then_eof_agent()
        return
    if scenario == "updates_after_response":
        await _run_updates_after_response_agent()
        return
    if scenario == "update_error_response_then_eof":
        await _run_update_error_response_then_eof_agent()
        return
    if scenario == "protocol_mismatch_exit":
        await _run_protocol_mismatch_exit_agent()
        return
    if scenario == "oversized_handshake_notification":
        await _run_oversized_handshake_notification_agent()
        return
    await acp.run_agent(StubAgent(), use_unstable_protocol=True)


async def _run_protocol_mismatch_exit_agent() -> None:
    """Write a deterministically invalid initialize response and EOF together."""
    raw = await asyncio.to_thread(sys.stdin.buffer.readline)
    if not raw:
        return
    request = json.loads(raw)
    response = {"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 999}}
    os.write(1, json.dumps(response, separators=(",", ":")).encode() + b"\n")
    os._exit(0)


async def _run_oversized_handshake_notification_agent() -> None:
    """Bust the retained-payload caps inside the connect window, then EOF.

    The frame is protocol-legal JSON-RPC whose params exceed the item cap —
    a violation that replays identically on every respawn of this agent.
    """
    raw = await asyncio.to_thread(sys.stdin.buffer.readline)
    if not raw:
        return
    frame = {
        "jsonrpc": "2.0",
        "method": "_zeta/blob",
        "params": {"blob": list(range(5000))},
    }
    os.write(1, json.dumps(frame, separators=(",", ":")).encode() + b"\n")
    os._exit(0)


async def _run_updates_after_response_agent() -> None:
    """Serve the handshake, then bracket the prompt response with updates and EOF.

    The three prompt-turn frames go out in one write so their wire order is
    fixed: update("pre"), the exact response, update("post"), then EOF.
    """

    def _update(text: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": _SESSION_ID,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }

    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not raw:
            return
        request = json.loads(raw)
        method = request["method"]
        if method == "initialize":
            result: Any = {"protocolVersion": 1, "agentCapabilities": {}}
        elif method == "session/new":
            result = {"sessionId": _SESSION_ID}
        elif method == "session/prompt":
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5},
                },
            }
            frames = b"".join(
                json.dumps(frame, separators=(",", ":")).encode() + b"\n"
                for frame in (_update("pre"), response, _update("post"))
            )
            os.write(1, frames)
            os._exit(0)
        else:
            result = None
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        os.write(1, json.dumps(reply, separators=(",", ":")).encode() + b"\n")


async def _run_update_error_response_then_eof_agent() -> None:
    """Serve the handshake, then EOF right behind an update and an error response.

    The update is gateable client-side; the deterministic -32602 rejection and
    the immediate exit pin "deferred disconnect + config-classified response"
    in one wire order.
    """
    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not raw:
            return
        request = json.loads(raw)
        method = request["method"]
        if method == "initialize":
            result: Any = {"protocolVersion": 1, "agentCapabilities": {}}
        elif method == "session/new":
            result = {"sessionId": _SESSION_ID}
        elif method == "session/prompt":
            update = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": _SESSION_ID,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "pre"},
                    },
                },
            }
            error = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32602, "message": "Invalid params", "data": {"details": "rejected"}},
            }
            frames = b"".join(json.dumps(frame, separators=(",", ":")).encode() + b"\n" for frame in (update, error))
            os.write(1, frames)
            os._exit(0)
        else:
            result = None
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        os.write(1, json.dumps(reply, separators=(",", ":")).encode() + b"\n")


async def _run_response_then_eof_agent() -> None:
    """Serve the minimum handshake, then write a prompt response and EOF together."""
    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not raw:
            return
        request = json.loads(raw)
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {}}
        elif method == "session/new":
            result = {"sessionId": _SESSION_ID}
        elif method == "session/prompt":
            result = {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5},
            }
        else:
            result = None
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        os.write(1, json.dumps(response, separators=(",", ":")).encode() + b"\n")
        if method == "session/prompt":
            os._exit(0)


if __name__ == "__main__":
    asyncio.run(_main())
