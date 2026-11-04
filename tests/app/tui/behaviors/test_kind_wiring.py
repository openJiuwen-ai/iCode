# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for TUI sites that look up by tool ``kind``.

Kind-keyed dicts/registries must use the ``chrys.foundation.tool_kinds`` constants —
the same bare values that middleware events publish in ``tool_kind``.  A
literal that drifts from the constants makes the lookup miss silently and the
feature degrades to its fallback.  These tests pin the contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.approval import _DETAIL_KEYS, _build_detail
from chrys.app.tui.screens.main.session_handlers import SessionCallbacks, SessionHandler
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.widgets.chat import tool_renderers
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolCall, ToolCardHeader
from chrys.app.tui.widgets.chat.tool_renderers import (
    _KIND_REGISTRY,
    _REGISTRY,
    create_tool_widget,
    register_dynamic_renderer,
    register_kind_renderer,
    register_renderer,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeDetails, ProfileSwitched
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SHELL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
)
from tests.support.tui_helpers import discard_worker

# ── _DETAIL_KEYS in approval dialog ──────────────────────────────────────


def test_detail_keys_use_kind_constants() -> None:
    """``_DETAIL_KEYS`` must be keyed by the canonical kind constants."""
    assert KIND_SHELL in _DETAIL_KEYS
    assert KIND_FILESYSTEM_READ in _DETAIL_KEYS
    assert KIND_FILESYSTEM_WRITE in _DETAIL_KEYS


def test_build_detail_finds_reason_for_shell_kind() -> None:
    """``_build_detail`` extracts the reason field when called with the shell kind."""
    detail = _build_detail(KIND_SHELL, "zsh", {"command": "ls", "reason": "list files"})
    assert detail == "list files"


def test_build_detail_falls_back_to_rationale_keys_for_unknown_kind() -> None:
    """Any kind — bridged remote requests carry kind "" — falls back to the
    rationale-style keys ("reason", then "description"); without those the
    detail line stays empty."""
    detail = _build_detail("totally.unknown", "zsh", {"command": "ls", "reason": "list files"})
    assert detail == "list files"
    assert _build_detail("totally.unknown", "zsh", {"command": "ls", "description": "why"}) == "why"
    assert _build_detail("totally.unknown", "zsh", {"command": "ls"}) == ""


# ── Sub-agent renderer registration via SessionReady ─────────────────────


@pytest.fixture(autouse=True)
def _clear_kind_registry():
    """Snapshot/restore renderer registries so tests don't leak state."""
    # Built-ins register through the static loader. Snapshot the loaded baseline
    # so tests can freely mutate name and kind registries without leaking state.
    tool_renderers._ensure_loaded()
    saved_registry = dict(_REGISTRY)
    saved_kind_registry = dict(_KIND_REGISTRY)
    saved_loaded = tool_renderers._loaded
    yield
    _REGISTRY.clear()
    _REGISTRY.update(saved_registry)
    _KIND_REGISTRY.clear()
    _KIND_REGISTRY.update(saved_kind_registry)
    tool_renderers._loaded = saved_loaded


def test_session_ready_handler_registers_sub_agent_renderer_via_constant() -> None:
    """Source-level guard: the handler must call ``register_kind_renderer``
    with the ``KIND_SUB_AGENT`` constant, not a string literal.

    Regression: a previous version hardcoded the kind string while middleware
    emitted a different form, so first-session sub-agent calls fell back to
    the generic :class:`ToolCall` instead of :class:`SubAgentToolCall`.
    Pinning the constant keeps both sides coupled to ``chrys.foundation.tool_kinds``.

    A static check is more robust here than an end-to-end run of
    ``on_session_ready`` — the handler is async-deep and would require
    mocking widgets that aren't relevant to the kind-wiring contract.
    """
    from chrys.app.tui.screens.main import event_handlers

    src = inspect.getsource(event_handlers)
    assert "register_kind_renderer(KIND_SUB_AGENT" in src, (
        "BackendEventHandler must register the sub-agent renderer under "
        "the KIND_SUB_AGENT constant (string literals drift)."
    )
    literal = re.search(r'register_kind_renderer\(\s*"', src)
    assert not literal, (
        "register_kind_renderer must use kind constants from chrys.foundation.tool_kinds, not string literals."
    )


def test_create_tool_widget_routes_sub_agent_kind_to_sub_agent_widget() -> None:
    """End-to-end: after registration, the factory returns ``SubAgentToolCall``
    when the event carries the sub-agent kind."""
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    widget = create_tool_widget(
        call_id="c1",
        tool_name="explore",
        tool_kind=KIND_SUB_AGENT,
        args_summary="",
    )
    assert isinstance(widget, SubAgentToolCall)


def test_create_tool_widget_preserves_structurally_compatible_renderer() -> None:
    """Custom Widgets need not inherit BaseToolCard or expose writable tool_kind."""

    class CompatibleRenderer(Static):
        def __init__(
            self,
            call_id: str,
            tool_name: str,
            args_summary: str = "",
            args: dict[str, object] | None = None,
        ) -> None:
            super().__init__()
            self.call_id = call_id
            self.tool_name = tool_name
            self.args_summary = args_summary
            self.args = args or {}
            self.status = "running"
            self.result_text = ""

        @property
        def tool_kind(self) -> str:
            return "renderer-owned"

        def set_complete(self, result: str, duration_ms: int = 0, **kwargs: object) -> None:
            del duration_ms, kwargs
            self.status = "complete"
            self.result_text = result

        def set_error(self, error: str) -> None:
            self.status = "error"
            self.result_text = error

    register_renderer("compatible", CompatibleRenderer)

    widget = create_tool_widget("c1", "compatible", KIND_SLEEP)

    assert isinstance(widget, CompatibleRenderer)
    assert widget.tool_kind == "renderer-owned"


def test_profile_switch_registers_sub_agent_exact_tool_names() -> None:
    """ProfileSwitched must keep dynamic exact-name renderer registration."""

    class _FakeEvents:
        def format_tool_info(
            self,
            tool_names: list[str],
            skill_names: list[str],
            *,
            memory_files: list[str] | None = None,
            runtime_details: AgentRuntimeDetails | None = None,
        ) -> str:
            del tool_names, skill_names, memory_files, runtime_details
            return ""

        def get_profile_description(self, _profile: str) -> str:
            return ""

        def finish_agent_load(self, _message: str) -> None:
            return

    class _FakePanel:
        def set_profile(self, _profile: str) -> None:
            return

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            return

    class _FakeInputBar:
        pass

    class _FakeStatusBar:
        def set_profile(self, _profile: str, *, description: str = "") -> None:
            return

        def set_tool_info(self, _trail: str) -> None:
            return

        def clear_status(self) -> None:
            return

        def flash(self, _text: str, **_kwargs: object) -> None:
            return

    def query_one(cls: type) -> object:
        if cls.__name__ == "ChatPanel":
            return _FakePanel()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _events=_FakeEvents(),
        query_one=query_one,
        _update_subtitle=lambda: None,
        _debug=lambda *_args: None,
    )
    state = MainScreenState()
    state.runtime.profile = "Code"
    state.workspace_marker.current_cwd = "/repo/current"

    asyncio.run(
        SessionHandler(
            state=state,
            services=MainScreenServices(bus=EventBus()),
            view=MainScreenViewAdapter(screen, state=state),  # type: ignore[arg-type]
            callbacks=SessionCallbacks(
                set_agent_loading=lambda _value: None,
                set_has_messages=lambda value: setattr(state.run, "has_messages", value),
                set_creating_new_session=lambda value: setattr(state.session, "creating_new_session", value),
                set_restoring_session=lambda value: setattr(state.session, "restoring_session", value),
                set_profile_display=lambda value: setattr(state.runtime, "profile", value),
                set_active_model_profile_id=lambda _value: None,
                set_workspace_cwd=lambda value: setattr(state.workspace_marker, "current_cwd", value),
                update_subtitle=lambda: None,
                update_toc=lambda: None,
                clear_suggestion_file_cache=lambda: None,
                start_worker=discard_worker,
                post_gc_message=lambda _message: None,
                debug=lambda _key, _message="": None,
                refresh_model_indicator=lambda: None,
            ),
            agent_load=screen._events,
        ).on_profile_switched(
            ProfileSwitched(from_profile="Code", to_profile="Explore", sub_agent_tool_names=["explore", "bash"])
        )
    )

    widget = create_tool_widget(
        call_id="c1",
        tool_name="explore",
        tool_kind="unknown.kind",
        args_summary=json.dumps({"prompt": "map the repo"}),
    )

    assert isinstance(widget, SubAgentToolCall)

    # A sub-agent name colliding with a builtin must not clobber the builtin
    # registration for the rest of the process: after the switch (and after
    # switching away), a real shell call still renders as a shell call.
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    shell_widget = create_tool_widget(
        call_id="c2",
        tool_name="bash",
        tool_kind=KIND_SHELL,
        args_summary="",
        args={"command": "echo hi"},
    )
    assert isinstance(shell_widget, ExecuteToolCall)

    # The colliding sub-agent itself is unaffected — it routes by kind.
    sub_agent_widget = create_tool_widget(
        call_id="c3",
        tool_name="bash",
        tool_kind=KIND_SUB_AGENT,
        args_summary=json.dumps({"prompt": "run the build"}),
    )
    assert isinstance(sub_agent_widget, SubAgentToolCall)


def test_create_tool_widget_preserves_separate_name_and_kind_axes() -> None:
    """Exact tool-name registration must win before the tool-kind fallback."""

    class NameRenderer(ToolCall):
        pass

    class KindRenderer(ToolCall):
        pass

    register_renderer("exact_tool", NameRenderer)
    register_kind_renderer("sample.kind", KindRenderer)

    exact = create_tool_widget(
        call_id="c1",
        tool_name="exact_tool",
        tool_kind="sample.kind",
        args_summary="",
    )
    fallback = create_tool_widget(
        call_id="c2",
        tool_name="other_tool",
        tool_kind="sample.kind",
        args_summary="",
    )

    assert isinstance(exact, NameRenderer)
    assert isinstance(fallback, KindRenderer)


def test_static_renderer_registration_keeps_name_and_kind_baseline() -> None:
    """Built-in renderer registration is explicit and includes sub-agent kind fallback."""
    from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
    from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall, WriteFileToolCall
    from chrys.app.tui.widgets.chat.renderers.read_file import ReadFileToolCall
    from chrys.app.tui.widgets.chat.renderers.search import GlobToolCall, GrepToolCall
    from chrys.app.tui.widgets.chat.renderers.skill import SkillToolCall
    from chrys.app.tui.widgets.chat.renderers.sleep import SleepToolCall
    from chrys.app.tui.widgets.chat.renderers.todo import TodoToolCall
    from chrys.foundation.tool_kinds import KIND_TODO
    from chrys.service.tools.builtins.shell import SHELL_TOOL_NAMES

    tool_renderers._ensure_loaded()

    assert _REGISTRY["ask_user"] is AskUserToolCall
    assert _KIND_REGISTRY[KIND_ASK_USER] is AskUserToolCall
    for shell_name in SHELL_TOOL_NAMES:
        assert _REGISTRY[shell_name] is ExecuteToolCall
    assert _KIND_REGISTRY[KIND_SHELL] is ExecuteToolCall
    assert _REGISTRY["list_workspace_dirs"] is ToolCall
    assert _REGISTRY["edit_file"] is EditFileToolCall
    assert _REGISTRY["write_file"] is WriteFileToolCall
    assert _REGISTRY["read_file"] is ReadFileToolCall
    assert _REGISTRY["grep"] is GrepToolCall
    assert _REGISTRY["glob"] is GlobToolCall
    assert _REGISTRY["load_skill"] is SkillToolCall
    assert _REGISTRY["read_skill_resource"] is SkillToolCall
    assert _REGISTRY["run_skill_script"] is SkillToolCall
    assert _REGISTRY["sleep"] is SleepToolCall
    assert _KIND_REGISTRY[KIND_SLEEP] is SleepToolCall
    assert _KIND_REGISTRY[KIND_SUB_AGENT] is SubAgentToolCall
    assert _REGISTRY["todo_write"] is TodoToolCall
    assert _KIND_REGISTRY[KIND_TODO] is TodoToolCall


def test_create_tool_widget_falls_back_to_toolcall_for_unknown_kind() -> None:
    """Sanity: an unregistered kind falls back to the generic :class:`ToolCall`."""
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    widget = create_tool_widget(
        call_id="c1",
        tool_name="explore",
        tool_kind="totally.unknown",
        args_summary="",
    )
    assert type(widget) is ToolCall


def test_create_tool_widget_routes_pwsh_to_execute_renderer() -> None:
    """PowerShell 7 shell calls should render with the specialized shell execution widget."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    widget = create_tool_widget(
        call_id="c1",
        tool_name="pwsh",
        tool_kind=KIND_SHELL,
        args_summary="",
        args={"command": "$PSVersionTable.PSVersion"},
    )

    assert isinstance(widget, ExecuteToolCall)
    assert widget.tool_name == "pwsh"


def test_create_tool_widget_mcp_kind_never_resolves_builtin_names() -> None:
    """An MCP tool legitimately named after a builtin (e.g. ``bash``) must not
    pick up the builtin's renderer — the kind marks it as foreign."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
    from chrys.foundation.tool_kinds import KIND_MCP

    widget = create_tool_widget(
        call_id="c1",
        tool_name="bash",
        tool_kind=KIND_MCP,
        args_summary="",
    )

    assert not isinstance(widget, ExecuteToolCall)
    assert type(widget) is ToolCall


@pytest.mark.parametrize("tool_name", ["bash", "web_search"])
def test_create_tool_widget_extension_kind_never_resolves_builtin_names(tool_name: str) -> None:
    """An extension picks its own tool names: ``bash`` (not this platform's shell)
    or ``web_search`` (web tools off) is the extension's tool, shown as a plain call."""
    from chrys.foundation.tool_kinds import extension_kind

    widget = create_tool_widget(
        call_id="c1",
        tool_name=tool_name,
        tool_kind=extension_kind("acme_release"),
        args_summary="",
        args={"command": "echo hi", "query": "q"},
    )

    assert type(widget) is ToolCall


def test_create_tool_widget_shell_kind_covers_unlisted_shell_names() -> None:
    """A shell name outside ``SHELL_TOOL_NAMES`` (exotic ``$SHELL`` basename)
    still renders as a shell call via the kind registration."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    widget = create_tool_widget(
        call_id="c1",
        tool_name="mksh",
        tool_kind=KIND_SHELL,
        args_summary="",
        args={"command": "echo hi"},
    )

    assert isinstance(widget, ExecuteToolCall)


def test_create_tool_widget_legacy_blank_kind_still_resolves_shell_names() -> None:
    """Replayed legacy calls persisted without a kind keep name-based routing."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    widget = create_tool_widget(
        call_id="c1",
        tool_name="bash",
        tool_kind="",
        args_summary="",
        args={"command": "echo hi"},
    )

    assert isinstance(widget, ExecuteToolCall)


def test_create_tool_widget_sub_agent_kind_wins_over_builtin_name() -> None:
    """A sub-agent whose configurable tool name collides with a builtin
    (e.g. a profile exposed as ``bash``) must still get the sub-agent
    renderer — sub-agent linking requires a real ``SubAgentToolCall``."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    widget = create_tool_widget(
        call_id="c1",
        tool_name="bash",
        tool_kind=KIND_SUB_AGENT,
        args_summary=json.dumps({"prompt": "run the build"}),
    )

    assert not isinstance(widget, ExecuteToolCall)
    assert isinstance(widget, SubAgentToolCall)


def test_create_tool_widget_list_workspace_dirs_stays_generic() -> None:
    """``list_workspace_dirs`` shares ``KIND_SHELL`` but is not command
    execution — it must not pick up ExecuteToolCall's command/timeout chrome
    through the kind fallback."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    widget = create_tool_widget(
        call_id="c1",
        tool_name="list_workspace_dirs",
        tool_kind=KIND_SHELL,
        args_summary="",
    )

    assert not isinstance(widget, ExecuteToolCall)
    assert type(widget) is ToolCall


def test_register_dynamic_renderer_never_overwrites_builtins() -> None:
    """Dynamic (user-chosen name) registration is setdefault-semantics: it
    forces the static load and yields to any existing registration."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    register_dynamic_renderer("bash", SubAgentToolCall)
    assert _REGISTRY["bash"] is ExecuteToolCall

    register_dynamic_renderer("brand_new_agent", SubAgentToolCall)
    assert _REGISTRY["brand_new_agent"] is SubAgentToolCall


@pytest.mark.asyncio
async def test_execute_widget_mounts_with_replayed_string_timeout() -> None:
    """Persisted shell args may contain a string timeout; replay must still compose."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    widget = ExecuteToolCall(
        call_id="c1",
        tool_name="pwsh",
        args={
            "command": "Get-ChildItem",
            "reason": 123,
            "timeout": "120",
        },
    )

    class HostApp(App):
        def compose(self) -> ComposeResult:
            yield widget

    async with HostApp().run_test() as pilot:
        await pilot.pause()
        mounted = pilot.app.query_one(ExecuteToolCall)
        label = mounted.query_one("#exec-label", Static)
        command = mounted.query_one("#exec-cmd", Static)

        assert "timeout 2m" in label.render().plain
        assert command.render().plain == "Get-ChildItem"


@pytest.mark.asyncio
async def test_sub_agent_widget_mounts_via_runtime_kind_path() -> None:
    """End-to-end mount: the widget produced by :func:`create_tool_widget`
    for ``KIND_SUB_AGENT`` must compose without crashing.

    Regression: storing the prompt under ``self._task`` shadowed
    :class:`textual.message_pump.MessagePump`'s ``_task`` attribute, which
    the framework reassigns to the running ``asyncio.Task`` before
    :meth:`compose` runs.  By the time ``compose`` read ``self._task`` it
    found a Task object and ``Text(...)`` raised ``AttributeError`` — but
    no existing test actually mounted this widget, so the crash shipped.
    """
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    widget = create_tool_widget(
        call_id="c1",
        tool_name="explore",
        tool_kind=KIND_SUB_AGENT,
        args_summary=json.dumps({"prompt": "investigate the panel"}),
    )
    assert isinstance(widget, SubAgentToolCall)

    class HostApp(App):
        def compose(self) -> ComposeResult:
            yield widget

    async with HostApp().run_test() as pilot:
        await pilot.pause()
        mounted = pilot.app.query_one(SubAgentToolCall)
        # Prompt survived __init__ → mount → compose without being clobbered
        # by MessagePump's ``_task`` assignment.
        assert mounted._task_prompt == "investigate the panel"
        # Panel + main content widgets composed successfully.
        assert mounted.query_one("#sa-panel")
        assert mounted.query_one("#sa-latest")
        header = mounted.query_one(ToolCardHeader)
        assert header.actions_visible is True
        assert header.copy_action_visible is False
