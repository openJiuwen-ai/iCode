# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main-screen fakes share MainScreen's state containers and refuse the fields those replaced."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.foundation.events.bus import EventBus
from tests.support.tui_helpers import (
    _REMOVED_MAIN_SCREEN_FIELDS,
    ScreenSetters,
    SuggestionScreen,
    main_screen_parts,
    make_backend_handler,
    make_session_handler,
)


def _main_screen_instance_fields() -> set[str]:
    """Every ``self.<name>`` MainScreen and its Chrys bases assign, plus their class attributes."""
    fields: set[str] = set()
    for cls in MainScreen.__mro__:
        fields.update(vars(cls))
        if not cls.__module__.startswith("chrys."):
            continue
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(cls)))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                fields.add(node.attr)
    return fields


def test_removed_fields_are_fields_main_screen_no_longer_has() -> None:
    fields = _main_screen_instance_fields()

    assert {"_state", "_services", "_live_diff"} <= fields
    assert _REMOVED_MAIN_SCREEN_FIELDS.isdisjoint(fields)


def _fake_with_class_field() -> object:
    class _Screen:
        _agent_running = True

    return _Screen()


@pytest.mark.parametrize(
    "build",
    [main_screen_parts, make_backend_handler, make_session_handler],
    ids=["parts", "backend", "session"],
)
@pytest.mark.parametrize(
    ("make_fake", "field"),
    [
        (lambda: SimpleNamespace(_profile="Code"), "_profile"),
        (_fake_with_class_field, "_agent_running"),
    ],
    ids=["instance", "class"],
)
def test_fake_carrying_a_removed_field_is_refused(
    build: Callable[[object], object], make_fake: Callable[[], object], field: str
) -> None:
    with pytest.raises(TypeError, match=field):
        build(make_fake())


def test_fake_part_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(TypeError, match="_state is object, not MainScreenState"):
        main_screen_parts(SimpleNamespace(_state=object()))


def test_handlers_built_on_one_fake_share_its_parts() -> None:
    state = MainScreenState()
    screen = SimpleNamespace(_state=state)

    backend = make_backend_handler(screen)
    services = screen._services
    live_diff = screen._live_diff
    session = make_session_handler(screen)

    assert backend._state is state
    assert session._state is state
    assert backend._services is services
    assert session._services is services
    assert backend._live_diff is live_diff
    assert screen._state is state
    assert screen._services is services
    assert screen._live_diff is live_diff

    backend.restoring_session = True
    assert session.restoring_session is True


_Read = Callable[[MainScreenState, MainScreenServices], object]

_SETTER_CASES: list[tuple[str, object, _Read, object]] = [
    ("set_agent_running", True, lambda state, _services: state.run.agent_running, True),
    ("set_agent_loading", True, lambda state, _services: state.run.agent_loading, True),
    ("set_has_messages", True, lambda state, _services: state.run.has_messages, True),
    ("set_profile_display", "Code", lambda state, _services: state.runtime.profile, "Code"),
    ("set_active_model_profile_id", "model-a", lambda _state, services: services.active_model_profile_id, "model-a"),
    ("set_creating_new_session", True, lambda state, _services: state.session.creating_new_session, True),
    ("set_restoring_session", True, lambda state, _services: state.session.restoring_session, True),
    (
        "set_workspace_cwd",
        "/repo",
        lambda state, _services: (state.workspace.current_cwd, state.workspace_marker.current_cwd),
        ("/repo", "/repo"),
    ),
]


@pytest.mark.parametrize(
    ("name", "value", "read", "expected"),
    [pytest.param(*case, id=case[0]) for case in _SETTER_CASES],
)
def test_screen_setter_writes_the_state_then_calls_the_fakes_setter(
    name: str, value: object, read: _Read, expected: object
) -> None:
    state = MainScreenState()
    services = MainScreenServices(bus=EventBus())
    seen: list[tuple[object, object]] = []
    screen = SimpleNamespace(**{f"_{name}": lambda received: seen.append((received, read(state, services)))})

    getattr(ScreenSetters(screen, state, services), name)(value)

    assert seen == [(value, expected)]


def test_handler_setters_call_the_fakes_setter() -> None:
    seen: list[tuple[str, object, object]] = []
    state = MainScreenState()

    def recorder(name: str, read: Callable[[], object]) -> Callable[[object], None]:
        return lambda value: seen.append((name, value, read()))

    screen = SimpleNamespace(
        _state=state,
        _set_restoring_session=recorder("restoring", lambda: state.session.restoring_session),
        _set_creating_new_session=recorder("creating", lambda: state.session.creating_new_session),
        _set_workspace_cwd=recorder("cwd", lambda: state.workspace_marker.current_cwd),
        _set_profile_display=recorder("profile", lambda: state.runtime.profile),
    )
    backend = make_backend_handler(screen)
    session = make_session_handler(screen)

    backend.restoring_session = True
    session.creating_new_session = True
    backend.chdir_current_cwd = "/repo"
    session.profile = "Code"

    assert seen == [
        ("restoring", True, True),
        ("creating", True, True),
        ("cwd", "/repo", "/repo"),
        ("profile", "Code", "Code"),
    ]
    assert state.workspace.current_cwd == "/repo"


def test_suggestion_screen_refuses_a_removed_field() -> None:
    screen = SuggestionScreen()
    screen.state.run.agent_running = True

    with pytest.raises(TypeError, match="_agent_running"):
        screen._agent_running = True  # type: ignore[attr-defined]
    assert screen.state.run.agent_running is True


async def test_workspace_read_settler_waits_for_a_read_requested_during_one(tmp_path, monkeypatch):
    """A refresh asked for while a read runs becomes one more read; the settler waits for it too."""
    import asyncio

    from chrys.app.tui.theme import TuiVariableDefaultsMixin
    from chrys.app.tui.widgets.sidebar.workspaces import WorkspacePanel
    from chrys.service.state.store import JsonFileStateStore
    from chrys.service.workspaces.registry import WorkspaceRegistry
    from tests.support.async_tasks import running_task
    from tests.support.tui_helpers import WidgetApp, settle_workspace_page_reads
    from tests.support.waiting import wait_for

    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    store = JsonFileStateStore(tmp_path / "sessions", workspace_registry=registry)
    original = store.open_session_listing
    started: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    releases: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    reads = 0

    async def gated(*, kind):
        nonlocal reads
        index = min(reads, 1)
        reads += 1
        started[index].set()
        await releases[index].wait()
        return await original(kind=kind)

    monkeypatch.setattr(store, "open_session_listing", gated)
    panel = WorkspacePanel()

    class WorkspaceReadApp(TuiVariableDefaultsMixin, WidgetApp):
        pass

    async with WorkspaceReadApp(lambda: panel).run_test() as pilot:
        panel.set_session(None, store, "current", "", registry=registry)
        await wait_for(started[0].is_set, pilot=pilot)
        # Refreshes asked for during a read become one more read after it.
        panel.refresh_sessions()
        panel.refresh_sessions()
        panel.refresh_sessions()
        async with running_task(settle_workspace_page_reads(panel)) as task:
            try:
                releases[0].set()
                await wait_for(started[1].is_set, pilot=pilot)
                assert not task.done()
            finally:
                releases[0].set()
                releases[1].set()
            await task
        assert reads == 2
