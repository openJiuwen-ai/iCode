# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted builtin adapters; local path/command keys are independent of identity."""

from __future__ import annotations

import inspect
import os
from dataclasses import asdict
from typing import TYPE_CHECKING

from chrys import __version__
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE, KIND_SHELL, get_tool_kind
from chrys.kernel import FunctionTool
from chrys.service.approval.daa import Candidate, DAAService, ReuseContext, canonical, digest
from chrys.service.approval.daa_store import DAAStore
from chrys.service.tools.builtins.filesystem import FilesystemTools
from chrys.service.tools.builtins.shell import ShellTools

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.kernel.middleware import FunctionInvocationContext
    from chrys.service.tools.file_approval import FileWriteTarget


class DAABinding:
    """One build's authoritative tool objects and execution context, never model input."""

    def __init__(
        self, runtime: SessionEnvironment, tools: list, profile_fingerprint: str, *, session_id: str | None = None
    ) -> None:
        self.runtime = runtime
        self.session_id = session_id if session_id is not None else runtime.session_id
        self.tools = {tool.name: tool for tool in tools if isinstance(tool, FunctionTool)}
        self.profile_fingerprint = profile_fingerprint
        self.service = DAAService(DAAStore(runtime.platform.config_dir / "daa-minimal-v1.sqlite3"))

    def file_targets(self, context: FunctionInvocationContext) -> tuple[FileWriteTarget, ...] | None:
        """Resolve only authoritative builtin writes, also for one-time confirmation."""
        tool = context.function
        owner = tool.bound_instance
        if (
            self.tools.get(tool.name) is not tool
            or not isinstance(owner, FilesystemTools)
            or tool.func not in (FilesystemTools.write_file.func, FilesystemTools.edit_file.func)
            or not isinstance(context.arguments, dict)
        ):
            return None
        return owner.approval_targets(context.arguments)

    def candidate(
        self,
        context: FunctionInvocationContext,
        *,
        non_reusable: bool = False,
    ) -> Candidate | None:
        tool = context.function
        kind = get_tool_kind(tool)
        func = tool.func
        if (
            non_reusable
            or self.tools.get(tool.name) is not tool
            or kind not in {KIND_SHELL, KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE}
            or not (inspect.isfunction(func) or inspect.ismethod(func))
            or not func.__module__.startswith("chrys.service.tools.builtins.")
            or context.kwargs.get("session", context.session) is not context.session
            or not isinstance(context.arguments, dict)
        ):
            return None
        runtime = self.runtime
        reuse = ReuseContext(self.session_id, runtime.cwd)
        owner = tool.bound_instance
        if kind == KIND_FILESYSTEM_WRITE:
            if not isinstance(owner, FilesystemTools) or func not in (
                FilesystemTools.write_file.func,
                FilesystemTools.edit_file.func,
            ):
                return None
            return self.service.file_candidate(owner.affected_paths(context.arguments), reuse)
        if kind == KIND_SHELL:
            if not isinstance(owner, ShellTools) or func is not ShellTools.execute.func:
                return None
            shell = owner.shell
            return self.service.candidates(
                [shell.name, shell.path, shell.args], context.arguments, reuse, shell=shell.name
            )
        try:
            # Only read EXACT retains the full request and runtime fingerprint.
            execution = canonical(
                {
                    "cwd": runtime.cwd,
                    "working_dirs": [asdict(directory) for directory in runtime.working_dirs],
                    "platform": [runtime.platform.os_name, runtime.platform.os_version, runtime.platform.arch],
                    "shells": [asdict(runtime.platform.shell), *[asdict(s) for s in runtime.platform.extra_shells]],
                    "environment_digest": digest(dict(os.environ)),
                    "profile": self.profile_fingerprint,
                    "kwargs": {key: value for key, value in context.kwargs.items() if key != "session"},
                    "chrys_version": __version__,
                }
            )
            identity = [tool.name, kind, func.__module__, func.__qualname__, tool.parameters()]
            return self.service.candidates(
                identity,
                context.arguments,
                ReuseContext(self.session_id, runtime.cwd, execution),
            )
        except ValueError, TypeError, RecursionError:
            return None
