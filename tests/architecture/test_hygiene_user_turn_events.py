# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Src rule: in the TUI only the input flow builds the events that send text to the model.

``chrys.foundation.events.types.UserMessage`` and ``UserRetry`` reach the model,
and ``SessionOnlySwitch`` is the first step of sending a prompt shortcut's
draft. Every send in the TUI claims the one submit in flight in
``app/tui/screens/main/input_flow.py`` and publishes under that claim, so a
send whose switch or commands are still on their way refuses every other way
of sending and hands its draft back once. A second place that builds one of
these events (and so publishes it) would bypass the claim. This is an
anti-bypass guard; the behaviour itself is pinned by the TUI send tests in
``tests/app/tui/screens/test_prompt_shortcut_send.py``.

The chat widgets' ``UserMessage`` (``app/tui/widgets/chat/messages.py``) is a
different class; only names bound to the events module count.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePath

import pytest

from chrys.app.tui.screens.main import input_flow
from tests.architecture._hygiene_core import _meta_guard_problem, _tree
from tests.support.ci import CI_LINUX_ONLY
from tests.support.paths import REPO_ROOT

# Platform-independent source analysis: the Linux CI job covers it.
pytestmark = CI_LINUX_ONLY

_TUI_DIR = "src/chrys/app/tui/"
# The one TUI module that builds and publishes the model-bound user events.
_USER_TURN_EVENT_OWNER = PurePath("src/chrys/app/tui/screens/main/input_flow.py")
_EVENT_MODULES = frozenset({"chrys.foundation.events.types", "chrys.foundation.events"})
_EVENT_CLASSES = frozenset({"UserMessage", "UserRetry", "SessionOnlySwitch"})
_PARTIAL_NAMES = frozenset({"partial", "functools.partial"})


def _event_bindings(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Names bound to the event classes (name → class) and to the events modules, anywhere in *tree*."""
    classes: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in _EVENT_MODULES:
                for alias in node.names:
                    if alias.name in _EVENT_CLASSES:
                        classes[alias.asname or alias.name] = alias.name
            if node.module == "chrys.foundation.events":
                modules.update(alias.asname or alias.name for alias in node.names if alias.name == "types")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _EVENT_MODULES:
                    modules.add(alias.asname or alias.name)
    return classes, modules


def _event_class(node: ast.expr, classes: Mapping[str, str], modules: set[str]) -> str | None:
    if isinstance(node, ast.Name):
        return classes.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr in _EVENT_CLASSES:
        owner = node.value
        dotted: list[str] = []
        while isinstance(owner, ast.Attribute):
            dotted.append(owner.attr)
            owner = owner.value
        if isinstance(owner, ast.Name):
            dotted.append(owner.id)
            if ".".join(reversed(dotted)) in modules:
                return node.attr
    return None


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _constructions(path: PurePath, source: str) -> Iterator[tuple[int, str]]:
    """``(line, class)`` for each event built in *source*: a call, or a ``partial`` that calls it later."""
    tree = _tree(path, source)
    classes, modules = _event_bindings(tree)
    if not classes and not modules:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        built = _event_class(node.func, classes, modules)
        if built is None and _dotted(node.func) in _PARTIAL_NAMES and node.args:
            built = _event_class(node.args[0], classes, modules)
        if built is not None:
            yield node.lineno, built


def _outside_constructions(sources: Mapping[Path, str]) -> Iterator[tuple[PurePath, int, str]]:
    for path, source in sources.items():
        posix = PurePath(path).as_posix()
        if not posix.startswith(_TUI_DIR) or PurePath(posix) == _USER_TURN_EVENT_OWNER:
            continue
        for line, built in _constructions(PurePath(posix), source):
            yield PurePath(posix), line, built


def _assert_user_turn_events_are_built_only_by_the_input_flow(sources: Mapping[Path, str]) -> None:
    violations = [
        f"{path.as_posix()}:{line}: builds chrys.foundation.events.types.{built} outside "
        f"{_USER_TURN_EVENT_OWNER.as_posix()}; a send published here bypasses the one submit claim "
        "(see tests/architecture/test_hygiene_user_turn_events.py): route it through InputFlowController "
        "(submit_user_text or request_retry)"
        for path, line, built in _outside_constructions(sources)
    ]
    assert violations == [], "\n".join(violations)


def test_user_turn_event_owner_still_builds_both_events() -> None:
    """The exempt module must be the real publisher, or the guard exempts nothing that matters."""
    owner = Path(input_flow.__file__).resolve().relative_to(REPO_ROOT)
    problems: list[str] = []
    if PurePath(owner.as_posix()) != _USER_TURN_EVENT_OWNER:
        problems.append(
            _meta_guard_problem(
                "_USER_TURN_EVENT_OWNER",
                f"InputFlowController lives in {owner.as_posix()}, not {_USER_TURN_EVENT_OWNER.as_posix()}",
                "point _USER_TURN_EVENT_OWNER at the module that now owns the submit claim",
            )
        )
    built = {
        name for _line, name in _constructions(_USER_TURN_EVENT_OWNER, (REPO_ROOT / owner).read_text(encoding="utf-8"))
    }
    if built != _EVENT_CLASSES:
        problems.append(
            _meta_guard_problem(
                "_USER_TURN_EVENT_OWNER",
                f"{_USER_TURN_EVENT_OWNER.as_posix()} builds {sorted(built)}, not every one of {sorted(_EVENT_CLASSES)}",
                "keep the guard's owner and event set in step with the module that publishes user sends",
            )
        )
    _tree.cache_clear()
    assert problems == [], "\n".join(problems)


_ROGUE = Path("src/chrys/app/tui/screens/main/rogue.py")


@pytest.mark.parametrize(
    "source",
    [
        (
            "from chrys.foundation.events.types import UserMessage\n"
            "async def f(bus):\n    await bus.publish(UserMessage(text='hi'))\n"
        ),
        (
            "from chrys.foundation.events.types import UserRetry as Retry\n"
            "async def f(bus):\n    await bus.publish(Retry(text=''))\n"
        ),
        "from chrys.foundation.events import UserMessage\ndef f():\n    return UserMessage(text='x')\n",
        "from chrys.foundation.events import types\ndef f():\n    return types.UserRetry(text='x')\n",
        "import chrys.foundation.events.types as ev\ndef f():\n    return ev.UserMessage(text='x')\n",
        "import chrys.foundation.events.types\ndef f():\n    return chrys.foundation.events.types.UserRetry()\n",
        # Function-scoped (lazy) imports count too.
        (
            "async def f(bus):\n    from chrys.foundation.events.types import UserMessage\n"
            "    await bus.publish(UserMessage(text='x'))\n"
        ),
        (
            "from functools import partial\nfrom chrys.foundation.events.types import UserMessage\n"
            "def f():\n    return partial(UserMessage, text='x')\n"
        ),
    ],
    ids=["direct", "aliased", "package-reexport", "types-module", "module-alias", "qualified", "lazy", "partial"],
)
def test_user_turn_event_guard_rejects_a_second_publisher(source: str) -> None:
    with pytest.raises(AssertionError, match=r"rogue\.py:\d+: builds chrys\.foundation\.events\.types\.User"):
        _assert_user_turn_events_are_built_only_by_the_input_flow({_ROGUE: source})


def test_user_turn_event_guard_accepts_the_owner_reads_and_the_chat_widget_class() -> None:
    _assert_user_turn_events_are_built_only_by_the_input_flow(
        {
            Path(_USER_TURN_EVENT_OWNER.as_posix()): (
                "from chrys.foundation.events.types import UserMessage, UserRetry\n"
                "def f():\n    return UserMessage(text='x'), UserRetry(text='')\n"
            ),
            # Reading, matching and subscribing are not sending.
            _ROGUE: (
                "from chrys.foundation.events.types import UserMessage, UserRetry\n"
                "def f(event: UserMessage | UserRetry, bus) -> bool:\n"
                "    bus.subscribe(UserMessage, print)\n"
                "    return isinstance(event, UserRetry)\n"
            ),
            # The chat widget of the same name is no event.
            Path("src/chrys/app/tui/widgets/chat/example.py"): (
                "from chrys.app.tui.widgets.chat.messages import UserMessage\ndef f():\n    return UserMessage('hi')\n"
            ),
            # Outside the TUI the rule does not apply.
            Path("src/chrys/app/acp/example.py"): (
                "from chrys.foundation.events.types import UserMessage\ndef f():\n    return UserMessage(text='x')\n"
            ),
        }
    )
