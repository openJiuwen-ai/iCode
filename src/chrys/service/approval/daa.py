# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Minimal allow-only reuse of explicit human approval; no semantic equivalence."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainValidator, TypeAdapter

from chrys.service.approval.daa_command import normalize_simple_command
from chrys.service.approval.daa_store import DAAStore

LOCAL_ENVIRONMENT_ID = "local"

Scope = Literal["SESSION", "PROJECT"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Restricted literal shell language: deliberately no quotes, escapes or expansion.
_LITERAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./,:@%+- ")
_RESERVED_HEADS = frozenset(
    [
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "in",
        "function",
        "time",
        "coproc",
        "select",
    ]
)
_POSIX_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "git_bash"})


def canonical(value: object) -> str:
    """Injective JSON identity on the supported JSON value domain."""
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


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def simple_argv(command: object, shell: str) -> tuple[str, ...] | None:
    """Recognize only literal argv; never try to repair ambiguous shell syntax."""
    if shell not in _POSIX_SHELLS or not isinstance(command, str) or not command:
        return None
    if any(char not in _LITERAL_CHARS for char in command):
        return None
    argv = tuple(shlex.split(command, posix=True))
    if len(argv) < 2 or argv[0] in _RESERVED_HEADS:
        return None
    return argv


@dataclass(frozen=True)
class ReuseContext:
    session_id: str
    project_id: str
    execution: str = ""

    @property
    def eligible(self) -> bool:
        return bool(self.session_id and self.project_id)


def _raw_string(value: object) -> str:
    # Pydantic's Unicode decoder rejects valid POSIX surrogateescaped paths.
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


def _json_tokens(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


RawString = Annotated[str, PlainValidator(_raw_string)]
Tokens = Annotated[tuple[RawString, ...], BeforeValidator(_json_tokens)]


class _Key(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    environment_id: Literal["local"] = LOCAL_ENVIRONMENT_ID


class FileApprovalKey(_Key):
    path: Annotated[RawString, Field(min_length=1)]


class CommandSessionKey(_Key):
    shell: Annotated[RawString, Field(min_length=1)]
    command: Annotated[Tokens, Field(min_length=1)]


@dataclass(frozen=True)
class Candidate:
    context: ReuseContext
    display: str
    # None means this is not a file candidate; an empty set never authorizes.
    file_keys: frozenset[FileApprovalKey] | None = None
    command_key: CommandSessionKey | None = None
    shell: str = ""
    legacy_display: str = ""
    prefix: tuple[str, ...] | None = None

    @property
    def exact_key(self) -> str:
        # Command PROJECT uses execution identity; reads retain full requests.
        return digest(self.legacy_display)

    @property
    def prefix_base(self) -> str:
        if not self.prefix:
            return ""
        legacy = json.loads(self.legacy_display)
        return digest(
            [
                legacy["tool"],
                {key: value for key, value in legacy["arguments"].items() if key != "command"},
                legacy["execution"],
                legacy["project"],
            ]
        )


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    scope: Scope
    scope_id: Annotated[RawString, Field(min_length=1)]
    source: Literal["EXPLICIT_USER_DAA"]
    created_at: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ExactRule(_Rule):
    kind: Literal["EXACT"]
    exact_key: Digest


class PrefixRule(_Rule):
    kind: Literal["PREFIX"]
    prefix_base: Digest
    prefix: Annotated[Tokens, Field(min_length=2)]


class FilePathRule(_Rule):
    kind: Literal["FILE_PATH"] = "FILE_PATH"
    # Required on disk: pre-fix lexical grants cannot be reinterpreted as
    # physical grants. Old rows are ignored without rewriting stored data.
    path_resolution: Literal["physical_v1"]
    key: FileApprovalKey


class CommandSessionRule(_Rule):
    kind: Literal["COMMAND_SESSION"] = "COMMAND_SESSION"
    scope: Literal["SESSION"] = "SESSION"
    key: CommandSessionKey


Rule = Annotated[ExactRule | PrefixRule | FilePathRule | CommandSessionRule, Field(discriminator="kind")]
_RULE = TypeAdapter(Rule)


class DAAService:
    def __init__(self, store: DAAStore) -> None:
        self.store = store

    def candidates(
        self, tool_identity: object, arguments: dict[str, object], context: ReuseContext, *, shell: str = ""
    ) -> Candidate | None:
        if not context.eligible:
            return None
        candidate = self.command_candidate(arguments, context, shell=shell) if shell else None
        if shell and candidate is None:
            return None
        # Commands bind their shell identity and default cwd, not presentation
        # or resource limits. Retain unknown arguments so future execution
        # options cannot silently inherit an existing grant.
        reuse_arguments = (
            {key: value for key, value in arguments.items() if key not in {"reason", "timeout", "max_tokens"}}
            if shell
            else arguments
        )
        try:
            display = canonical(
                {
                    "tool": tool_identity,
                    "arguments": reuse_arguments,
                    "execution": {"shell": shell, "cwd": context.project_id} if shell else context.execution,
                    "project": context.project_id,
                }
            )
        except ValueError, TypeError, RecursionError:
            return candidate if candidate is not None and candidate.command_key is not None else None
        if candidate is None:
            return Candidate(context, display, legacy_display=display)
        preview = json.loads(candidate.display)
        preview["project_exact"] = True
        return replace(
            candidate,
            display=canonical(preview),
            legacy_display=display,
            prefix=simple_argv(arguments.get("command"), shell),
        )

    @staticmethod
    def command_candidate(arguments: dict[str, object], context: ReuseContext, *, shell: str) -> Candidate | None:
        """Build the session key independently of legacy request serialization."""
        # The session key does not bind a per-call directory override.
        # Keep explicit working_dir requests on ordinary approval, including
        # legacy serialization fallbacks and attempts to mint new grants.
        if not context.eligible or "working_dir" in arguments:
            return None
        argv = normalize_simple_command(arguments.get("command"), shell)
        return Candidate(
            context=context,
            display=canonical({"command_session": list(argv) if argv is not None else None, "project_exact": False}),
            command_key=CommandSessionKey(shell=shell, command=argv) if argv is not None else None,
            shell=shell,
        )

    @staticmethod
    def file_candidate(paths: tuple[str, ...] | None, context: ReuseContext) -> Candidate | None:
        """Paths are the complete, resolved set supplied by a trusted adapter."""
        if not paths or not context.eligible:
            return None
        keys = frozenset(FileApprovalKey(path=path) for path in paths)
        return Candidate(context, canonical({"file_paths": sorted(key.path for key in keys)}), file_keys=keys)

    def rules(self) -> list[Rule]:
        result = []
        for rule_id, payload in self.store.load():
            try:
                # stdlib JSON preserves escaped filesystem bytes injectively.
                rule = _RULE.validate_python(json.loads(payload))
                if rule.id == rule_id:
                    result.append(rule)
            except ValueError, TypeError, RecursionError:
                continue
        return result

    def match(self, candidate: Candidate | None) -> Literal["HIT_ALLOW", "MISS"]:
        if candidate is None or not candidate.context.eligible:
            return "MISS"
        rules = self.rules()
        if candidate.file_keys is not None:
            # Every path must be covered within one applicable scope. Never all([]).
            for scope in ("SESSION", "PROJECT"):
                approved = {
                    rule.key
                    for rule in rules
                    if isinstance(rule, FilePathRule)
                    and rule.scope == scope
                    and self._scope_matches(rule, candidate.context)
                }
                if candidate.file_keys and candidate.file_keys <= approved:
                    return "HIT_ALLOW"
            return "MISS"
        for rule in rules:
            if (
                isinstance(rule, CommandSessionRule)
                and self._scope_matches(rule, candidate.context)
                and rule.key == candidate.command_key
            ):
                return "HIT_ALLOW"
        for rule in rules:
            if (
                isinstance(rule, ExactRule)
                and candidate.legacy_display
                and (not candidate.shell or rule.scope == "PROJECT")
                and self._scope_matches(rule, candidate.context)
                and rule.exact_key == candidate.exact_key
            ):
                return "HIT_ALLOW"
        for rule in rules:
            if (
                isinstance(rule, PrefixRule)
                and self._scope_matches(rule, candidate.context)
                and candidate.prefix is not None
                and rule.prefix_base == candidate.prefix_base
                and candidate.prefix[: len(rule.prefix)] == rule.prefix
            ):
                return "HIT_ALLOW"
        return "MISS"

    @staticmethod
    def _scope_matches(rule: Rule, context: ReuseContext) -> bool:
        current = context.session_id if rule.scope == "SESSION" else context.project_id
        return bool(current) and current == rule.scope_id

    def remember(self, candidate: Candidate, choice: str) -> bool:
        """The trusted human-response path supplies an immutable displayed candidate."""
        if not candidate.context.eligible or choice not in {
            "EXACT_SESSION",
            "EXACT_PROJECT",
            "PREFIX_SESSION",
            "PREFIX_PROJECT",
        }:
            return False
        kind, scope = choice.split("_")
        common = {
            "id": uuid4().hex,
            "scope": scope,
            "scope_id": candidate.context.session_id if scope == "SESSION" else candidate.context.project_id,
            "source": "EXPLICIT_USER_DAA",
            "created_at": datetime.now(UTC).timestamp(),
        }
        if kind == "EXACT" and candidate.file_keys is not None:
            if not candidate.file_keys:
                return False
            rules = [
                FilePathRule.model_validate({**common, "id": uuid4().hex, "path_resolution": "physical_v1", "key": key})
                for key in candidate.file_keys
            ]
        elif kind == "EXACT" and candidate.shell and scope == "SESSION":
            if candidate.command_key is None:
                return False
            rules = [CommandSessionRule.model_validate({**common, "key": candidate.command_key})]
        elif kind == "EXACT" and candidate.legacy_display:
            rules = [ExactRule.model_validate({"kind": "EXACT", "exact_key": candidate.exact_key, **common})]
        elif kind == "PREFIX" and candidate.prefix is not None:
            rules = [
                PrefixRule.model_validate(
                    {"kind": "PREFIX", "prefix_base": candidate.prefix_base, "prefix": candidate.prefix, **common}
                )
            ]
        else:
            return False
        return self.store.add_many([(rule.id, canonical(rule.model_dump(mode="json"))) for rule in rules])

    def revoke(self, rule_id: str) -> bool:
        return self.store.revoke(rule_id)
