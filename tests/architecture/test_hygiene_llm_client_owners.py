# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Src rule: every LLM client stack ``create_client`` opens has one reviewed close owner."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.architecture._hygiene_core import _meta_guard_problem, _pins_allowlist, _src_sources, _tree

# ``create_client`` returns an open stack (SDK client plus the httpx pool Chrys
# built for it) that nothing closes implicitly: the stack is not an async
# context manager and ``Agent.__aexit__`` never sees it. Each call site
# registers exactly one close owner with no ``await`` between the factory's
# return and the registration. Keys are ``<src path>::<qualified function>``;
# values name that owner. ``async with scoped_client(...)`` is compliant by
# construction and needs no entry.
_LLM_CLIENT_OWNER_ALLOWLIST = {
    "src/chrys/service/llm/clients.py::scoped_client": "the context manager's own finally",
    "src/chrys/orchestration/engine/build/builder.py::build_agent._acquire": (
        "the build's PreparedAgent (prepared.own_or_release)"
    ),
    "src/chrys/orchestration/sub_agents/tools.py::SubAgentTools._prepare._acquire": (
        "the sub-agent build's PreparedAgent (prepared.own_or_release)"
    ),
    "src/chrys/orchestration/workflows/agent_node_build.py::build_kernel_node": (
        "the node attempt's Conversation (conversation.own_or_release)"
    ),
    "src/chrys/service/approval/judge.py::ApprovalJudge._get_client": (
        "ApprovalJudge.aclose, called by the runtime or workflow session that owns the judge"
    ),
    "src/chrys/service/context/compaction/last_words.py::LastWordsGenerator._get_client": (
        "LastWordsGenerator.aclose, called by the compaction runtime that owns the generator"
    ),
    "src/chrys/app/features/session_title/updater.py::SessionTitleUpdater._leased_client": (
        "the updater's client slot (OnceClose), drained on replacement and shutdown"
    ),
}

_FACTORY = "create_client"
_SCOPED_FACTORY = "scoped_client"


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_with_scope(tree: ast.Module) -> Iterator[tuple[str, ast.Call, ast.AST | None]]:
    """Yield ``(qualified scope, call, parent)`` for every call, scopes joined with ``.``."""

    def visit(
        node: ast.AST, scope: tuple[str, ...], parent: ast.AST | None
    ) -> Iterator[tuple[str, ast.Call, ast.AST | None]]:
        if isinstance(node, ast.Call):
            yield ".".join(scope) or "<module>", node, parent
        inner = (
            (*scope, node.name) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else scope
        )
        for child in ast.iter_child_nodes(node):
            yield from visit(child, inner, node)

    yield from visit(tree, (), None)


def _factory_call_sites(path: Path, source: str) -> Iterator[tuple[str, ast.Call, ast.AST | None]]:
    for scope, call, parent in _calls_with_scope(_tree(path, source)):
        if _called_name(call) in (_FACTORY, _SCOPED_FACTORY):
            yield f"{path.as_posix()}::{scope}", call, parent


def _assert_llm_clients_have_reviewed_owners(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        for key, call, parent in _factory_call_sites(path, source):
            if _called_name(call) == _SCOPED_FACTORY:
                if not isinstance(parent, ast.withitem):
                    violations.append(
                        f"{path.as_posix()}:{call.lineno}: scoped_client(...) outside `async with` never closes "
                        "its client; use it only as `async with scoped_client(...) as client:`"
                    )
                continue
            if key not in _LLM_CLIENT_OWNER_ALLOWLIST:
                violations.append(
                    f"{path.as_posix()}:{call.lineno}: {key} opens an LLM client stack with no reviewed close "
                    "owner; register one owner right after the call (no await in between) and add a "
                    "_LLM_CLIENT_OWNER_ALLOWLIST entry naming it, or use `async with scoped_client(...)`"
                )
    assert violations == [], "\n".join(violations)


@_pins_allowlist("_LLM_CLIENT_OWNER_ALLOWLIST")
def test_llm_client_owner_allowlist_entries_are_live() -> None:
    """Each entry must still name a function that calls ``create_client``: a
    stale key would silently vouch for a new call added later under that name."""
    sources = _src_sources()
    live = {
        key
        for path, source in sources.items()
        for key, call, _parent in _factory_call_sites(path, source)
        if _called_name(call) == _FACTORY
    }
    problems = [
        _meta_guard_problem(
            "_LLM_CLIENT_OWNER_ALLOWLIST",
            f"entry {key} names no function that calls {_FACTORY}",
            "remove the stale entry or re-key it to the function that now opens the client",
        )
        for key in sorted(set(_LLM_CLIENT_OWNER_ALLOWLIST) - live)
    ]
    problems += [
        _meta_guard_problem(
            "_LLM_CLIENT_OWNER_ALLOWLIST", f"entry {key} names no owner", "name the object that closes the client"
        )
        for key, owner in sorted(_LLM_CLIENT_OWNER_ALLOWLIST.items())
        if not owner.strip()
    ]
    _tree.cache_clear()
    assert problems == [], "\n".join(problems)


_UNREVIEWED = Path("src/chrys/service/example.py")


@pytest.mark.parametrize(
    "source",
    [
        "async def build(profile):\n    client = await create_client(profile)\n",
        "async def build(profile):\n    return await clients.create_client(profile)\n",
        "import asyncio\ndef build(profile):\n    return asyncio.ensure_future(create_client(profile))\n",
        "class Owner:\n    async def open(self):\n        self.client = await create_client(self.profile)\n",
    ],
)
def test_llm_client_owner_guard_rejects_unreviewed_create_client_calls(source: str) -> None:
    with pytest.raises(AssertionError, match=r"src/chrys/service/example\.py:\d+: .* no reviewed close owner"):
        _assert_llm_clients_have_reviewed_owners({_UNREVIEWED: source})


def test_llm_client_owner_guard_rejects_scoped_client_outside_async_with() -> None:
    source = "async def build(profile):\n    manager = scoped_client(profile)\n    return await manager.__aenter__()\n"
    with pytest.raises(AssertionError, match=r"example\.py:2: scoped_client\(\.\.\.\) outside `async with`"):
        _assert_llm_clients_have_reviewed_owners({_UNREVIEWED: source})


def test_llm_client_owner_guard_accepts_reviewed_sites_and_scoped_clients() -> None:
    _assert_llm_clients_have_reviewed_owners(
        {
            _UNREVIEWED: (
                "async def reply(profile):\n"
                "    async with scoped_client(profile) as client:\n"
                "        return await client.get_response([])\n"
            ),
            Path("src/chrys/service/approval/judge.py"): (
                "class ApprovalJudge:\n"
                "    async def _get_client(self):\n"
                "        self._client = await create_client(self._profile)\n"
            ),
        }
    )


def test_llm_client_owner_guard_keys_nested_scopes_by_their_full_path() -> None:
    source = (
        "class ApprovalJudge:\n"
        "    async def _get_client(self):\n"
        "        async def nested():\n"
        "            return await create_client(self._profile)\n"
    )
    with pytest.raises(AssertionError, match=r"ApprovalJudge\._get_client\.nested opens an LLM client"):
        _assert_llm_clients_have_reviewed_owners({Path("src/chrys/service/approval/judge.py"): source})
