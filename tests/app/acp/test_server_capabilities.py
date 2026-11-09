# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pin the extension surface ``initialize`` declares to what the server implements.

A client branches on ``_meta.chrys`` instead of on error codes, so a
declaration that nothing implements and a dispatcher branch that is never
declared are both defects. These tests read the server's own source and
compare the two sides as sets.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from chrys.app.acp import server as server_module
from chrys.app.acp.server import (
    ACP_EXTENSION_API_VERSION,
    ACP_EXTENSION_METHODS,
    ACP_EXTENSION_NOTIFICATIONS,
)


def _server_class_def() -> ast.ClassDef:
    tree = ast.parse(inspect.getsource(server_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ChrysAcpServer":
            return node
    raise AssertionError("ChrysAcpServer is not defined in its own module")


def _dispatched_methods() -> set[str]:
    """Every ``method == "<literal>"`` compared inside ``ChrysAcpServer._dispatch_ext_method``.

    Calls to the *client's* ``ext_method`` are skipped: those name requests the
    server sends (``chrys/request_input``), not methods it answers.
    """
    methods: set[str] = set()
    for node in ast.walk(_server_class_def()):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "_dispatch_ext_method":
            continue
        for compare in ast.walk(node):
            if not isinstance(compare, ast.Compare) or len(compare.ops) != 1:
                continue
            if not isinstance(compare.ops[0], ast.Eq):
                continue
            left, right = compare.left, compare.comparators[0]
            if (
                isinstance(left, ast.Name)
                and left.id == "method"
                and isinstance(right, ast.Constant)
                and isinstance(right.value, str)
            ):
                methods.add(right.value)
    return methods


def _dispatched_notifications() -> set[str]:
    """Every first argument of an ``ext_notification("<literal>", ...)`` call."""
    notifications: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(server_module))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "ext_notification" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            notifications.add(first.value)
    return notifications


def test_declared_methods_match_the_dispatcher() -> None:
    assert set(ACP_EXTENSION_METHODS) == _dispatched_methods()


def test_declared_notifications_match_what_the_server_sends() -> None:
    assert set(ACP_EXTENSION_NOTIFICATIONS) == _dispatched_notifications()


@pytest.mark.anyio
async def test_initialize_declares_the_extension_surface() -> None:
    server = server_module.ChrysAcpServer(_DeclaringManager(), initial_vision=False)  # type: ignore[arg-type]

    response = await server.initialize(
        protocol_version=1,
        client_capabilities=None,
        client_info=None,
    )

    chrys = response.agent_capabilities.field_meta["chrys"]
    assert chrys["apiVersion"] == ACP_EXTENSION_API_VERSION
    assert set(chrys["methods"]) == set(ACP_EXTENSION_METHODS)
    assert set(chrys["notifications"]) == set(ACP_EXTENSION_NOTIFICATIONS)
    # The request the server sends to the client is not a method it answers.
    assert "chrys/request_input" not in chrys["methods"]


class _DeclaringManager:
    """A manager that refuses any session lookup: ``initialize`` needs none."""

    def get(self, session_id: str) -> None:
        raise AssertionError("initialize must not resolve a session")
