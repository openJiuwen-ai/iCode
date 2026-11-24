# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit command and file grants; presentation never participates in matching."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainValidator, TypeAdapter

from chrys.foundation.models.approval_reuse import ApprovalReuseOffer, ReuseChoice
from chrys.service.approval.command_identity import normalize_simple_command
from chrys.service.approval.grant_store import ApprovalGrantStore

LOCAL_ENVIRONMENT_ID = "local"
Scope = Literal["SESSION", "WORKDIR"]
_LITERAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./,:@%+- ")


def canonical(value: object) -> str:
    """Serialize only JSON values without coercing host objects or non-finite numbers."""
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError("Non-string JSON key")
        for item in value.values():
            canonical(item)
    elif isinstance(value, list):
        for item in value:
            canonical(item)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError("Non-JSON authorization identity")
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def workdir_path(path: str) -> str:
    """A working directory's real path, never case-folded, like the command directories it scopes."""
    return os.path.realpath(path)


def simple_argv(command: str, shell: str) -> tuple[str, ...] | None:
    """Keep the existing literal-prefix contract, including its documented risk."""
    if shell not in {"bash", "sh", "dash", "zsh", "ksh", "git_bash"}:
        return None
    if any(char not in _LITERAL_CHARS for char in command):
        return None
    tokens = normalize_simple_command(command, shell)
    return tokens if tokens is not None and len(tokens) >= 2 else None


def _raw_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


RawString = Annotated[str, PlainValidator(_raw_string)]
Tokens = Annotated[
    tuple[RawString, ...], BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value)
]


class _Key(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    environment_id: Literal["local"] = LOCAL_ENVIRONMENT_ID


class CommandKey(_Key):
    kind: Literal["command"] = "command"
    cwd: RawString
    shell: RawString
    executable: RawString
    shell_args: Tokens
    command: RawString | Tokens
    options: RawString


class FileKey(_Key):
    kind: Literal["file"] = "file"
    path: Annotated[RawString, Field(min_length=1)]


@dataclass(frozen=True)
class ReuseContext:
    session_id: str
    workdir: str

    @property
    def eligible(self) -> bool:
        return bool(self.session_id and self.workdir)


@dataclass(frozen=True)
class CommandCandidate:
    context: ReuseContext
    key: CommandKey
    prefix: tuple[str, ...] | None = None

    def offer(self) -> ApprovalReuseOffer:
        command = self.key.command
        return ApprovalReuseOffer(
            "command",
            (shlex.join(command) if isinstance(command, tuple) else command,),
            prefix=self.prefix is not None,
        )


@dataclass(frozen=True)
class FileCandidate:
    context: ReuseContext
    keys: frozenset[FileKey]

    def offer(self) -> ApprovalReuseOffer:
        return ApprovalReuseOffer("files", tuple(sorted(key.path for key in self.keys)))


Candidate = CommandCandidate | FileCandidate


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    scope: Scope
    scope_id: Annotated[RawString, Field(min_length=1)]
    workdir: RawString | None = None
    source: Literal["EXPLICIT_USER"]
    created_at: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    prefix: bool = False
    key: Annotated[CommandKey | FileKey, Field(discriminator="kind")]


_GRANT = TypeAdapter(ApprovalGrant)


class ApprovalReuseService:
    def __init__(self, store: ApprovalGrantStore, session_store: ApprovalGrantStore) -> None:
        self.store = store
        self.session_store = session_store

    def rules(self) -> list[ApprovalGrant]:
        result = []
        for store, scope in ((self.store, "WORKDIR"), (self.session_store, "SESSION")):
            for payload in store.load():
                try:
                    rule = _GRANT.validate_python(payload)
                    if rule.scope == scope and (not rule.prefix or isinstance(rule.key, CommandKey)):
                        result.append(rule)
                except ValueError, TypeError, RecursionError:
                    continue
        return result

    def match(self, candidate: Candidate | None) -> tuple[str, ...]:
        """Return the exact rule IDs covering a request, or an empty miss."""
        if candidate is None or not candidate.context.eligible:
            return ()
        available = self.rules()
        context = candidate.context
        for scope, scope_id, workdir in (
            ("SESSION", context.session_id, context.workdir),
            ("WORKDIR", context.workdir, None),
        ):
            rules = [
                rule for rule in available if (rule.scope, rule.scope_id, rule.workdir) == (scope, scope_id, workdir)
            ]
            if isinstance(candidate, FileCandidate):
                matched = {rule.key: rule.id for rule in rules if isinstance(rule.key, FileKey) and not rule.prefix}
                if candidate.keys and candidate.keys <= matched.keys():
                    return tuple(sorted(matched[key] for key in candidate.keys))
                continue
            for rule in rules:
                if not isinstance(rule.key, CommandKey):
                    continue
                if not rule.prefix and rule.key == candidate.key:
                    return (rule.id,)
                if rule.prefix and candidate.prefix is not None and isinstance(rule.key.command, tuple):
                    prefix = rule.key.command
                    if (
                        len(prefix) >= 2
                        and candidate.prefix[: len(prefix)] == prefix
                        and rule.key == candidate.key.model_copy(update={"command": prefix})
                    ):
                        return (rule.id,)
        return ()

    def remember(self, candidate: Candidate, choice: ReuseChoice) -> bool:
        if not candidate.context.eligible or choice not in {
            "EXACT_SESSION",
            "EXACT_WORKDIR",
            "PREFIX_SESSION",
            "PREFIX_WORKDIR",
        }:
            return False
        prefix = choice.startswith("PREFIX_")
        session = choice.endswith("_SESSION")
        context = candidate.context
        scope: Scope
        scope, scope_id = ("SESSION", context.session_id) if session else ("WORKDIR", context.workdir)
        if isinstance(candidate, FileCandidate):
            if prefix or not candidate.keys:
                return False
            keys = sorted(candidate.keys, key=lambda key: key.path)
        else:
            if prefix and candidate.prefix is None:
                return False
            keys = [candidate.key.model_copy(update={"command": candidate.prefix}) if prefix else candidate.key]
        payloads = [
            ApprovalGrant(
                id=uuid4().hex,
                scope=scope,
                scope_id=scope_id,
                workdir=context.workdir if session else None,
                source="EXPLICIT_USER",
                created_at=datetime.now(UTC).timestamp(),
                prefix=prefix,
                key=key,
            ).model_dump(mode="json", exclude_none=True)
            for key in keys
        ]
        return (self.session_store if session else self.store).add_many(payloads)
