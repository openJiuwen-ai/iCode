# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bind approval reuse only to trusted built-in command and file-write tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.kernel import FunctionTool
from chrys.service.approval.command_identity import normalize_simple_command
from chrys.service.approval.grant_store import GRANTS_FILE, ApprovalGrantStore, session_grants_path
from chrys.service.approval.reuse import (
    ApprovalReuseService,
    Candidate,
    CommandCandidate,
    CommandKey,
    FileCandidate,
    FileKey,
    ReuseContext,
    canonical,
    project_path,
    simple_argv,
)
from chrys.service.approval.safety_classifier import target_may_access_sensitive_data
from chrys.service.tools.approval_targets import ApprovedTargets
from chrys.service.tools.builtins.filesystem import FilesystemTools
from chrys.service.tools.builtins.shell import ShellTools

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.kernel.middleware import FunctionInvocationContext

# Arguments that change neither what a command does nor where it runs. A
# remembered call never names a workdir (an empty one means the session's).
_UNBOUND_SHELL_ARGS = frozenset({"command", "reason", "timeout", "max_output_tokens", "workdir"})


@dataclass(frozen=True)
class PreparedReuse:
    """One request's confirmed targets, its grant candidate and the grants covering it."""

    targets: ApprovedTargets
    candidate: Candidate | None = None
    grant_ids: tuple[str, ...] = ()


class ApprovalReuseBinding:
    """Derive grant identities from the agent's own shell and file tools.

    ``prepare`` and ``targets`` resolve paths and read grant files: run them
    off the event loop.
    """

    def __init__(self, runtime: SessionEnvironment, tools: list, *, session_id: str | None = None) -> None:
        self.runtime = runtime
        self.session_id = session_id or runtime.session_id
        self._tools = {tool.name: tool for tool in tools if isinstance(tool, FunctionTool)}
        config_dir = runtime.platform.config_dir
        self.service = ApprovalReuseService(
            ApprovalGrantStore(config_dir / GRANTS_FILE),
            ApprovalGrantStore(session_grants_path(config_dir, self.session_id)),
        )

    def supports(self, context: FunctionInvocationContext) -> bool:
        """Only this agent's built-in shell, write and edit tools take part."""
        tool = context.function
        owner = tool.bound_instance
        return (
            self._tools.get(tool.name) is tool
            and isinstance(context.arguments, dict)
            and (
                (isinstance(owner, ShellTools) and tool.func is ShellTools.execute.func)
                or (
                    isinstance(owner, FilesystemTools)
                    and tool.func in (FilesystemTools.write_file.func, FilesystemTools.edit_file.func)
                )
            )
        )

    def targets(self, context: FunctionInvocationContext) -> ApprovedTargets:
        """Where a supported call will act, as it resolves right now."""
        owner = context.function.bound_instance
        if not isinstance(owner, ShellTools | FilesystemTools) or not isinstance(context.arguments, dict):
            return ApprovedTargets()
        return owner.approval_targets(context.arguments)

    def prepare(self, context: FunctionInvocationContext, *, reusable: bool) -> PreparedReuse:
        """Resolve a supported call's targets and, when *reusable*, its covering grants."""
        targets = self.targets(context)
        candidate = self._candidate(context, targets) if reusable else None
        if candidate is None:
            return PreparedReuse(targets)
        return PreparedReuse(targets, candidate, self.service.match(candidate))

    def _candidate(self, context: FunctionInvocationContext, targets: ApprovedTargets) -> Candidate | None:
        args = context.arguments
        reuse = ReuseContext(self.session_id, project_path(self.runtime.cwd))
        if not isinstance(args, dict) or not reuse.eligible:
            return None
        if targets.files is not None:
            # A final-component symlink both reads its referent and replaces
            # itself, and a credential file must always be confirmed by a person.
            if not targets.files or any(
                target.link_target is not None or target_may_access_sensitive_data(target.path)
                for target in targets.files
            ):
                return None
            return FileCandidate(reuse, frozenset(FileKey(path=target.path) for target in targets.files))
        owner = context.function.bound_instance
        command = args.get("command")
        if (
            not isinstance(owner, ShellTools)
            or targets.cwd is None
            or args.get("workdir")
            or not isinstance(command, str)
        ):
            return None
        shell = owner.shell
        try:
            options = canonical({key: value for key, value in args.items() if key not in _UNBOUND_SHELL_ARGS})
            tokens = normalize_simple_command(command, shell.name)
            key = CommandKey(
                cwd=targets.cwd,
                shell=shell.name,
                executable=shell.path,
                shell_args=tuple(shell.args),
                command=tokens if tokens is not None else command,
                options=options,
            )
        except ValueError, TypeError, RecursionError:
            return None
        return CommandCandidate(reuse, key, simple_argv(command, shell.name))
