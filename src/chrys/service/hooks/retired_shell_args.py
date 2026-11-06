# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DEPRECATED — the built-in shell tool's retired argument names, still honored at the hook boundary.

The shell tool renamed ``working_dir`` to ``workdir`` and ``max_tokens`` to
``max_output_tokens``, the names other agent harnesses use. A hook written
against the old names keeps doing what it did, and one written against the new
names still reads a call recorded before the rename:

* ``match.args`` on a retired name reads the new one (:func:`matched_arg_name`),
  so a blocking hook guarding a directory still fires.
* The payload a hook script reads also carries each retired name beside its
  new one (:func:`with_retired_names`); the envelope the manager keeps, whose
  arguments become the tool's, never does.
* A retired name in a blocking hook's ``args_override`` sets the new one
  (:func:`renamed_override`), so it neither reaches the tool nor is lost.
* A durable job left pending across the upgrade recorded its shell call under
  the retired names: when recovery runs it, the script's payload also carries
  each new name beside its retired one (:func:`with_new_names`).

Each applies only to a tool of the ``shell`` kind. Matching and the live
script payload need the call's arguments to hold the new name and not the old
one; an override needs only the old one absent, so a hook may still set a new
name the call left out. A tool that really takes ``working_dir`` (another
host's shell) is thus left as it is. A recovered job goes the other way round,
and safely: only the copy of its payload handed to the retry script gains the
new names; the tool call is neither changed nor replayed.
Session start and ``icode hook validate`` warn about a ``match.args`` that
still uses a retired name (:func:`retired_arg_names_warning`), telling it to
rename only under ``tool_kind: shell``: a tool name cannot tell (an MCP tool
may be called ``bash``, a shell ``mksh``).

TODO(retired-shell-args): delete this module, its callers (``matcher``,
``manager``, ``runner``, ``loader``, ``validation``), the
``retired_shell_arg`` check code in the hooks reference (en and zh-Hans) and
their tests once hook files have had a release or two to move to the new names.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.service.hooks.operation import args_key

if TYPE_CHECKING:
    from chrys.service.hooks.schema import HookConfig

RETIRED_SHELL_ARGS: Final[Mapping[str, str]] = MappingProxyType(
    {"working_dir": "workdir", "max_tokens": "max_output_tokens"}
)
"""Retired name → the name the shell tool takes now."""

_TAKEN_BY_OTHER_TOOLS: Final = frozenset({"max_tokens"})
"""Retired names other built-in tools still take (``read_file``, ``web_fetch``): a hook not
limited to ``tool_kind: shell`` most likely means those, so it is not warned."""


def _shell_args(tool: object) -> Mapping[str, Any] | None:
    """*tool*'s arguments when it is a payload ``tool`` of the shell kind."""
    if not isinstance(tool, Mapping) or tool.get("kind") != KIND_SHELL:
        return None
    args = tool.get("args")
    return args if isinstance(args, Mapping) else None


def matched_arg_name(tool: object, name: str) -> str:
    """The key a ``match.args.<name>`` clause reads in *tool*'s arguments."""
    args = _shell_args(tool)
    new = RETIRED_SHELL_ARGS.get(name)
    if args is None or new is None or name in args or new not in args:
        return name
    return new


def with_retired_names(payload: Any) -> Any:
    """*payload* as a hook script reads it: a shell tool's arguments also under their retired names.

    *payload* itself is not changed; a payload with nothing to add is returned as it is.
    """
    return _also_under(payload, [(new, old) for old, new in RETIRED_SHELL_ARGS.items()])


def with_new_names(payload: Any) -> Any:
    """A recovered job's *payload*, recorded before the rename: a shell tool's arguments also under their new names.

    Only outbox recovery calls this: a pending job reaches its script again
    without a new call to match. *payload* itself is not changed; a payload
    with nothing to add is returned as it is.
    """
    return _also_under(payload, list(RETIRED_SHELL_ARGS.items()))


def _also_under(payload: Any, names: list[tuple[str, str]]) -> Any:
    """*payload* with each shell argument ``name`` of the ``(name, twin)`` pairs also under ``twin``, if absent."""
    if not isinstance(payload, Mapping):
        return payload
    tool = payload.get("tool")
    args = _shell_args(tool)
    if args is None:
        return payload
    added = {twin: args[name] for name, twin in names if name in args and twin not in args}
    if not added:
        return payload
    return {**payload, "tool": {**tool, "args": {**args, **added}}}


def renamed_override(tool: object, override: dict[str, Any]) -> dict[str, Any]:
    """A blocking hook's *override* with retired shell names moved to the new ones.

    A script that copied the payload's arguments returns both names: the one it
    changed wins, judged against *tool*'s current arguments, and the new name
    wins when both changed. A new name the call did not have counts as changed,
    ``null`` included: for the shell, an explicit ``workdir: null`` is the
    session's folder.
    """
    args = _shell_args(tool)
    if args is None or not any(old in override for old in RETIRED_SHELL_ARGS):
        return override
    renamed = dict(override)
    for old, new in RETIRED_SHELL_ARGS.items():
        if old not in renamed or old in args:
            continue
        value = renamed.pop(old)
        if new not in renamed or _unchanged(args, new, renamed[new]):
            renamed[new] = value
    return renamed


def _unchanged(args: Mapping[str, Any], name: str, value: Any) -> bool:
    """Whether *value* is what *args* already gives *name*, compared as the tool gets it (as JSON).

    An absent *name* is not ``None``, and 1 is not ``true``; a value with no JSON
    form counts as changed.
    """
    if name not in args:
        return False
    before = args_key({name: args[name]})
    return before is not None and before == args_key({name: value})


def retired_arg_names_warning(hook: HookConfig) -> str | None:
    """Why *hook*'s ``match.args`` should move to the shell tool's new argument names.

    Only ``tool_kind: shell`` makes a hook certainly the shell's, so only such a
    hook is told to rename. Any other hook without a tool kind may guard a tool
    that takes the old name itself (an MCP tool may even be called ``bash``),
    so it is told to rename only if it is for the shell.
    """
    match = hook.match
    if not match.args or match.tool_kind not in (None, KIND_SHELL):
        return None
    retired = [name for name in match.args if name in RETIRED_SHELL_ARGS]
    if match.tool_kind is None:
        retired = [name for name in retired if name not in _TAKEN_BY_OTHER_TOOLS]
    if not retired:
        return None
    renames = ", ".join(f"{name!r} is now {RETIRED_SHELL_ARGS[name]!r}" for name in retired)
    if match.tool_kind == KIND_SHELL:
        return (
            f"match.args uses the shell tool's retired argument names ({renames}); it still matches shell calls "
            "for now, but rename it before support for the old names ends"
        )
    return (
        f"match.args uses a name the built-in shell tool retired ({renames}); it still matches shell calls by "
        "the old name for now. If the hook is for the shell, add tool_kind: shell and use the new name; a tool "
        "that takes the old name itself (such as an MCP tool) keeps it, so a hook for both needs one hook per tool"
    )
