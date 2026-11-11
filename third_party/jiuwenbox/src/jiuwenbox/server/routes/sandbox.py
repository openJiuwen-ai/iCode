# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Sandbox API routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, field_validator

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.sandbox import (
    BackgroundExecRequest,
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobRequest,
    KillBackgroundJobResult,
    PolicyMode,
    SandboxRef,
    SandboxSpec,
)
from jiuwenbox.server.sandbox_manager import (
    SandboxBackgroundExecRequest,
    SandboxExecRequest,
    SandboxListRequest,
)
from jiuwenbox.supervisor.daemon_ipc import SANDBOX_IP_ENV

router = APIRouter(tags=["sandboxes"])
configure_logging()
logger = logging.getLogger(__name__)

# How long to wait between non-blocking ``is_disconnected`` checks.
# Starlette sometimes returns immediately while the client is still
# connected; a blocking implementation waits inside the call itself.
_DISCONNECT_POLL_SECONDS = 0.05


async def _discard_task(task: asyncio.Task[object]) -> None:
    """Await ``task`` so its exception or cancellation is retrieved.

    A cancellation of this task is re-raised. A cancellation of ``task``
    is not.
    """
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


async def _cancel_exec_on_http_disconnect(
    http_request: Request,
    exec_task: asyncio.Task[ExecResult],
) -> None:
    """Cancel ``exec_task`` when the HTTP client drops.

    FastAPI does not cancel the endpoint when the client disconnects.
    ``Request.is_disconnected`` is the ASGI signal. This races that check
    against the exec so a still-connected client does not delay the response.
    """
    while not exec_task.done():
        probe = asyncio.create_task(http_request.is_disconnected())
        client_gone = False
        try:
            await asyncio.wait(
                {exec_task, probe},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if (
                not exec_task.done()
                and probe.done()
                and not probe.cancelled()
                and probe.exception() is None
                and bool(probe.result())
            ):
                client_gone = True
        finally:
            if not probe.done():
                probe.cancel()
            await _discard_task(probe)
        if exec_task.done():
            return
        if client_gone:
            exec_task.cancel()
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(exec_task),
                _DISCONNECT_POLL_SECONDS,
            )
        except TimeoutError:
            continue
        return


def _mgr():
    from jiuwenbox.server.app import get_manager
    return get_manager()


class CreateSandboxRequest(BaseModel):
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    policy: dict[str, Any] | None = None
    policy_mode: PolicyMode = PolicyMode.OVERRIDE
    sandbox_id: str | None = None
    sandbox_runtime: str | None = None

    @field_validator("env")
    @classmethod
    def reject_reserved_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if SANDBOX_IP_ENV in value:
            raise ValueError(f"{SANDBOX_IP_ENV} is reserved by the sandbox runtime")
        return value


class ExecRequest(BaseModel):
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin: str | None = None
    timeout_seconds: int | None = None

    @field_validator("env")
    @classmethod
    def reject_reserved_environment(
        cls,
        value: dict[str, str] | None,
    ) -> dict[str, str] | None:
        if value is not None and SANDBOX_IP_ENV in value:
            raise ValueError(f"{SANDBOX_IP_ENV} is reserved by the sandbox runtime")
        return value


class ListFilesQuery(BaseModel):
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True


@router.post("/sandboxes", response_model=SandboxRef, status_code=201)
async def create_sandbox(request: CreateSandboxRequest):
    if request.sandbox_id is None or request.sandbox_id.strip() == "":
        sandbox_id = None
    else:
        sandbox_id = request.sandbox_id
    spec = SandboxSpec(env=request.env, sandbox_id=sandbox_id, sandbox_runtime=request.sandbox_runtime)
    return await _mgr().create_sandbox(
        spec,
        policy_data=request.policy,
        policy_mode=request.policy_mode,
    )


@router.get("/sandboxes", response_model=list[SandboxRef])
async def list_sandboxes():
    return await _mgr().list_sandboxes()


@router.get("/sandboxes/{sandbox_id}", response_model=SandboxRef)
async def get_sandbox(sandbox_id: str):
    return await _mgr().get_sandbox(sandbox_id)


@router.delete("/sandboxes/{sandbox_id}", status_code=204)
async def delete_sandbox(sandbox_id: str):
    await _mgr().delete_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/start", response_model=SandboxRef)
async def start_sandbox(sandbox_id: str):
    return await _mgr().start_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/stop", response_model=SandboxRef)
async def stop_sandbox(sandbox_id: str):
    return await _mgr().stop_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/restart", response_model=SandboxRef)
async def restart_sandbox(sandbox_id: str):
    return await _mgr().restart_sandbox(sandbox_id)


@router.post("/sandboxes/{sandbox_id}/exec", response_model=ExecResult)
async def exec_in_sandbox(
    sandbox_id: str,
    request: ExecRequest,
    http_request: Request,
):
    stdin_data = request.stdin.encode() if request.stdin else None
    exec_task = asyncio.create_task(
        _mgr().exec_in_sandbox(
            sandbox_id=sandbox_id,
            request=SandboxExecRequest(
                command=list(request.command),
                workdir=request.workdir,
                env=request.env,
                stdin_data=stdin_data,
                timeout=request.timeout_seconds,
            ),
        )
    )
    watch_task = asyncio.create_task(_cancel_exec_on_http_disconnect(http_request, exec_task))
    try:
        await asyncio.wait(
            {exec_task, watch_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not exec_task.done():
            watch_error = None
            if watch_task.done() and not watch_task.cancelled():
                watch_error = watch_task.exception()
            exec_task.cancel()
            if watch_error is not None:
                raise watch_error
        try:
            return await exec_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # The client is gone. 499 is the conventional "client closed
            # request" status; the body is not delivered.
            return Response(status_code=499)
    finally:
        watch_task.cancel()
        if not exec_task.done():
            exec_task.cancel()
        current = asyncio.current_task()
        rearm_cancel = False
        if current is not None and current.cancelling():
            # Drop this task's cancellation count so the awaits below can
            # drain the exec task (and close its socket) before we re-raise.
            while current.uncancel():
                pass
            rearm_cancel = True
        await _discard_task(watch_task)
        if not exec_task.done():
            await _discard_task(exec_task)
        elif not exec_task.cancelled():
            # The try block already awaited a finished exec. Retrieve again
            # so a stored failure cannot be logged as never retrieved.
            exec_task.exception()
        if rearm_cancel:
            raise asyncio.CancelledError


class BackgroundJobListResponse(BaseModel):
    items: list[BackgroundJobSummary]


@router.post("/sandboxes/{sandbox_id}/exec_background", response_model=BackgroundExecResult)
async def exec_background_in_sandbox(
    sandbox_id: str,
    request: BackgroundExecRequest,
):
    stdin_data = request.stdin.encode() if request.stdin else None
    return await _mgr().exec_background_in_sandbox(
        sandbox_id=sandbox_id,
        request=SandboxBackgroundExecRequest(
            command=list(request.command),
            job_id=request.job_id,
            workdir=request.workdir,
            env=request.env,
            stdin_data=stdin_data,
        ),
    )


@router.get(
    "/sandboxes/{sandbox_id}/background",
    response_model=BackgroundJobListResponse,
)
async def list_background_jobs_in_sandbox(
    sandbox_id: str,
    running_only: bool = False,
):
    items = await _mgr().list_background_jobs_in_sandbox(
        sandbox_id,
        running_only=running_only,
    )
    return BackgroundJobListResponse(items=items)


@router.get(
    "/sandboxes/{sandbox_id}/background/{job_id}",
    response_model=BackgroundJobStatus,
)
async def get_background_job_in_sandbox(sandbox_id: str, job_id: str):
    return await _mgr().get_background_job_in_sandbox(sandbox_id, job_id)


@router.post(
    "/sandboxes/{sandbox_id}/background/{job_id}/kill",
    response_model=KillBackgroundJobResult,
)
async def kill_background_job_in_sandbox(
    sandbox_id: str,
    job_id: str,
    request: KillBackgroundJobRequest = KillBackgroundJobRequest(),
):
    return await _mgr().kill_background_job_in_sandbox(
        sandbox_id,
        job_id,
        signal=request.signal,
    )


@router.get("/sandboxes/{sandbox_id}/logs")
async def get_logs(sandbox_id: str):
    logs = await _mgr().get_logs(sandbox_id)
    return PlainTextResponse(logs)


@router.post("/sandboxes/{sandbox_id}/upload", status_code=204)
async def upload_file(
    sandbox_id: str,
    file: UploadFile = File(...),
    sandbox_path: str = Query(...),
):
    """Upload a file into the sandbox filesystem.

    In process mode, this writes to the bind-mounted host path.
    """
    content = await file.read()
    await _mgr().upload_file_to_sandbox(sandbox_id, sandbox_path, content)
    return Response(status_code=204)


@router.get("/sandboxes/{sandbox_id}/download")
async def download_file(
    sandbox_id: str,
    sandbox_path: str = Query(...),
):
    """Download a file from the sandbox filesystem."""
    try:
        content = await _mgr().download_file_from_sandbox(sandbox_id, sandbox_path)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"File not found: {sandbox_path}"})

    return Response(content=content, media_type="application/octet-stream")


@router.get("/sandboxes/{sandbox_id}/files")
async def list_files(
    sandbox_id: str,
    query: Annotated[ListFilesQuery, Query()],
):
    """List files and directories inside a sandbox path."""
    try:
        items = await _mgr().list_files_in_sandbox(
            sandbox_id=sandbox_id,
            request=SandboxListRequest(
                sandbox_path=query.sandbox_path,
                recursive=query.recursive,
                max_depth=query.max_depth,
                include_files=query.include_files,
                include_dirs=query.include_dirs,
            ),
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {query.sandbox_path}"},
        )
    return {"items": items}


@router.get("/sandboxes/{sandbox_id}/search")
async def search_files(
    sandbox_id: str,
    sandbox_path: str = Query(...),
    pattern: str = Query(...),
    exclude_patterns: list[str] | None = Query(None),
):
    """Search files under a sandbox path with shell-style glob patterns."""
    try:
        items = await _mgr().search_files_in_sandbox(
            sandbox_id=sandbox_id,
            sandbox_path=sandbox_path,
            pattern=pattern,
            exclude_patterns=exclude_patterns,
        )
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"Directory not found: {sandbox_path}"})
    return {"items": items}
