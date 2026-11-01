# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests: sending a prompt shortcut's draft in a real app on a real engine.

The engine runs the draft's ``!`command``` placeholders while it prepares the
message, after switching the session's agent and model when the shortcut asks
for that, and says when the commands start and end. The TUI shows them running,
lets Esc withdraw the message, hands a refused draft back as it was and keeps
every control a running turn disables disabled meanwhile.

Written from ``.plan/prompt-shortcuts-engine.md`` (rules R1-R11, section 4 and
items 17-22, 26 and 30 of section 7), not from the implementation. The harness
at the top is shared with ``test_models_dialog_deletion.py`` and
``test_prompt_card_layout.py``.

The new event types are read from the events module when a test runs, never at
import: the module collects before the engine defines them.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest
from textual.widgets import Static
from textual.widgets._footer import FooterKey

import chrys.orchestration.engine.build.builder as builder_module
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.agents.config import AgentsConfigScreen
from chrys.app.tui.screens.agents.picker import AgentsScreen
from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.app_mode import AppModeDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.guides.screen import GuideDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.app.tui.widgets.chat.messages import ErrorMessage, SystemMessage
from chrys.app.tui.widgets.chat.messages import UserMessage as UserBubble
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_card import PromptCard
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as ev
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.orchestration.engine.state.lifecycle_permits import RebuildPermitDenied
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    ModelConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.env_bridge import set_global_default_profile_id
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile
from chrys.service.prompt_shortcuts import load_prompt_shortcuts, prompt_shortcut_folders
from chrys.service.state.store import JsonFileStateStore
from tests.support.scripted_clients import FrameworkBoom
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    MAIN_SCREEN_BUILD_TIMEOUT,
    await_run_task_chain,
    wait_for,
    wait_until,
    with_wait_deadline,
)

if TYPE_CHECKING:
    from textual.pilot import Pilot
    from textual.screen import Screen

    from chrys.orchestration.engine.engine import AgentEngine
    from tests.support.engines import AgentEngineFactory

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shortcuts' commands are POSIX shell (a loop on a marker file, a redirect)"
)

# ---------------------------------------------------------------------------
# Harness: a real ChrysApp on a real AgentEngine, its model a scripted mock
# ---------------------------------------------------------------------------

ALPHA = "Alpha"
"""The agent the app starts on: the default agent."""
BETA = "Beta"
"""Another main agent a shortcut can switch to."""

MAIN_MODEL = "mock-main"
"""The settings' model: the default model."""
FAST_MODEL = "mock-fast"
BROKEN_MODEL = "mock-broken"
"""A model whose builds always fail (see :attr:`Builds.failing`)."""

MODEL_NAMES = {MAIN_MODEL: "Main", FAST_MODEL: "Fast", BROKEN_MODEL: "Broken"}

SECTION_SHORTCUTS = "Prompt Shortcuts"
SECTION_HEADERS = ("System Commands", SECTION_SHORTCUTS, "Loaded Skills")
NOT_FILLED_IN = "not filled in"
"""What the unfilled-fields warning says, whatever the count."""


def make_agent(name: str, *, model: str = "") -> AgentProfile:
    """A main agent with no tools of its own; *model* binds it to a model profile."""
    return AgentProfile(
        name=name,
        description=f"The {name} test agent",
        instructions="You are a test assistant.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="require", overrides={}),
        compaction=CompactionConfig(enabled=False),
        model=ModelConfig(profile_id=model),
    )


def mock_model(profile_id: str, name: str) -> ModelProfile:
    return ModelProfile(id=profile_id, name=name, provider="mock", model_id="mock", stream=False)


def mock_models(names: dict[str, str] | None = None) -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    for profile_id, name in (names or MODEL_NAMES).items():
        registry.register(mock_model(profile_id, name))
    return registry


@dataclass
class Hold:
    """A point the test keeps shut until it releases it."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def pass_through(self) -> None:
        self.entered.set()
        await self.released.wait()

    def release(self) -> None:
        self.released.set()


class ScriptedModel(MockChatClient):
    """The one model every build of the session gets.

    It answers ``Done.`` to every request, unless the test queued a failure
    (the turn fails, and the composer enters retry mode) or a hold (the request
    waits, so a turn is running).
    """

    def __init__(self) -> None:
        super().__init__(responses=[])
        self.failures: list[BaseException] = []
        self.hold: Hold | None = None

    def _next_response(self) -> MockResponse:
        self._call_index += 1
        return MockResponse(text="Done.")

    def _inner_get_response(self, *, messages: Any, stream: bool, options: Any, **kwargs: Any) -> Any:
        if self.failures:
            failure = self.failures.pop(0)
            self._call_history.append((list(messages), dict(options)))
            self._call_index += 1

            async def fail() -> Any:
                raise failure

            return fail()
        result = super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)
        hold, self.hold = self.hold, None
        if hold is None:
            return result

        async def held() -> Any:
            try:
                await hold.pass_through()
            except BaseException:
                if inspect.iscoroutine(result):
                    result.close()
                raise
            return await result

        return held()

    def last_user_text(self) -> str:
        """The text of the last user message the model was sent."""
        messages, _options = self.call_history[-1]
        return [message for message in messages if message.role == "user"][-1].text


class Builds:
    """Stands in for ``create_client``: hands every build the session's model, recording which profile it was for."""

    def __init__(self, model: ScriptedModel) -> None:
        self.model = model
        self.profiles: list[str] = []
        self.hold: Hold | None = None
        """Holds the next build until released."""
        self.failing: set[str] = set()
        """Model profile ids whose builds fail."""

    async def create_client(
        self,
        profile: ModelProfile,
        on_intermediate_text: Callable[[str], None] | None = None,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        use_route_session_context: bool = False,
        session_dir: Path | None = None,
        tool_result_ceiling_tokens: int | None = None,
    ) -> ScriptedModel:
        self.profiles.append(profile.id)
        hold, self.hold = self.hold, None
        if hold is not None:
            await hold.pass_through()
        if profile.id in self.failing:
            raise RuntimeError(f"cannot reach the {profile.name} model")
        self.model._on_intermediate_text = on_intermediate_text
        return self.model


def recorded_event_types() -> tuple[type[ev.Event], ...]:
    """What :class:`Seen` records; read at run time, since some of these are new."""
    return (
        ev.UserMessage,
        ev.UserRetry,
        ev.UserInject,
        ev.UserInjectCancel,
        ev.UserInterrupt,
        ev.SessionOnlySwitch,
        ev.PromptCommandsRunning,
        ev.PromptCommandsEnded,
        ev.ExecutionChanged,
        ev.Error,
        ev.ProfileSwitched,
        ev.ModelProfileSwitched,
        ev.SettingsReload,
        ev.SettingsReloaded,
        ev.SessionNew,
        ev.SessionReady,
        ev.TurnStarted,
        ev.AgentLoadStarted,
        ev.AgentLoadFailed,
    )


class Seen:
    """Every recorded event, in the order its publish began.

    Subscribed before the engine and the screen subscribe, so a ``UserMessage``
    is seen when it is published, not once the engine has prepared it; the
    objects are the published ones, so fields the engine sets later
    (``started_turn``, ``applied``) are read from them afterwards.
    """

    def __init__(self) -> None:
        self.events: list[ev.Event] = []

    async def __call__(self, event: ev.Event) -> None:
        self.events.append(event)

    def of[E](self, kind: type[E]) -> list[E]:
        return [event for event in self.events if isinstance(event, kind)]

    def position(self, event: ev.Event) -> int:
        return next(index for index, seen in enumerate(self.events) if seen is event)


async def hold_next(
    bus: EventBus, kind: type[ev.Event], hold: Hold, *, when: Callable[[Any], bool] | None = None
) -> None:
    """Hold the next publish of *kind* (that *when* accepts) inside one handler until *hold* is released.

    ``publish`` awaits its handlers in turn, so whatever published the event
    waits too: subscribed after the engine, the engine's own handler has run.
    """
    taken = False

    async def handler(event: Any) -> None:
        nonlocal taken
        if taken or (when is not None and not when(event)):
            return
        taken = True
        await hold.pass_through()

    await bus.subscribe(kind, handler)


@dataclass
class Session:
    app: ChrysApp
    pilot: Pilot
    main: MainScreen
    bus: EventBus
    engine: AgentEngine
    model: ScriptedModel
    builds: Builds
    seen: Seen
    holds: list[Hold]

    @property
    def bar(self) -> InputBar:
        return self.main.query_one(InputBar)

    @property
    def chat(self) -> _ChatTextArea:
        return self.main.query_one("#chat-input", _ChatTextArea)

    def hold(self) -> Hold:
        """A new hold, released when the session ends whatever the test did."""
        hold = Hold()
        self.holds.append(hold)
        return hold

    def session_id(self) -> str:
        ready = self.seen.of(ev.SessionReady)
        assert ready, "no session started"
        session_id = ready[-1].session_id
        assert session_id
        return session_id


@contextlib.asynccontextmanager
async def running_session(
    agent_engine: AgentEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agents: Sequence[AgentProfile] = (),
    models: ModelProfileRegistry | None = None,
    settings: Settings | None = None,
    startup_agent: str = ALPHA,
    hold_startup_load: bool = False,
    size: tuple[int, int] = (120, 40),
) -> AsyncIterator[Session]:
    """Run the app on a real engine until the session is ready, with the chat input focused.

    With *hold_startup_load* the startup's ``AgentLoadStarted`` is held before
    the screen sees it (the first hold in ``holds``): the agent is loading and
    nothing is waited for.
    """
    bus = EventBus()
    seen = Seen()
    for kind in recorded_event_types():
        await bus.subscribe(kind, seen)
    holds: list[Hold] = []
    if hold_startup_load:
        startup = Hold()
        holds.append(startup)
        await hold_next(bus, ev.AgentLoadStarted, startup)
    model = ScriptedModel()
    builds = Builds(model)
    monkeypatch.setattr(builder_module, "create_client", builds.create_client)
    registry = AgentProfileRegistry()
    for profile in agents or (make_agent(ALPHA), make_agent(BETA)):
        registry.register(profile)
    models = models if models is not None else mock_models()
    engine = agent_engine(
        bus,
        settings=settings or Settings(model_profile=MAIN_MODEL, workspace_change_notice=False),
        agent_registry=registry,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        initial_workspace=Workspace.from_cwd(str(Path.cwd())),
        surface=SessionSurface.TUI,
    )
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=engine.settings_handle.settings,
        settings_handle=engine.settings_handle,
        engine=engine,
        event_bus=bus,
        agent_registry=registry,
        model_registry=models,
        profile_name=startup_agent,
    )
    try:
        async with app.run_test(size=size, notifications=True) as pilot:
            await wait_for(
                lambda: app._main_screen is not None and app._main_screen.is_mounted,
                pilot=pilot,
                timeout=MAIN_SCREEN_BUILD_TIMEOUT,
                description="main screen mounted",
            )
            main = app._main_screen
            assert main is not None
            session = Session(app, pilot, main, bus, engine, model, builds, seen, holds)
            try:
                if not hold_startup_load:
                    await wait_ready(session, startup_agent)
                yield session
            finally:
                # Nothing the test held may outlive it: the app's exit shuts the engine down.
                for hold in holds:
                    hold.release()
    finally:
        for hold in holds:
            hold.release()


async def wait_ready(session: Session, agent: str = ALPHA) -> None:
    """The session is ready on *agent*, no load on screen, and the chat input has the focus."""
    main, pilot = session.main, session.pilot
    await wait_for(
        lambda: (
            session.app.screen is main
            and not session.bar.agent_loading
            and bool(session.seen.of(ev.SessionReady))
            and profile_tag(main) == agent
        ),
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description=f"session ready on {agent}",
    )
    session.bar.focus_input()
    await wait_for(lambda: session.chat.has_focus, pilot=pilot, description="chat input focused")


# -- what the user sees -------------------------------------------------------


def status_line(main: MainScreen) -> str:
    """The status bar's text as shown: the run label and its trail, or the flash."""
    bar = main.query_one(StatusBar)
    if not bar.visible:
        return ""
    parts = [bar.query_one(selector, Static) for selector in ("#status-text", "#status-trail", "#status-flash")]
    return " ".join(str(part.content) for part in parts if part.display and part.visible).strip()


def profile_tag(main: MainScreen) -> str:
    return str(main.query_one(StatusBar).query_one("#profile-tag", Static).content)


def model_tag(main: MainScreen) -> str:
    return str(main.query_one(StatusBar).query_one("#model-tag", Static).content)


def footer_actions(main: MainScreen) -> set[str]:
    return {key.action for key in main.query_one(ChrysFooter).query(FooterKey)}


def notices(app: ChrysApp) -> list[tuple[str, str]]:
    """Every toast on screen, as ``(severity, message)``."""
    return [(str(notification.severity), str(notification.message)) for notification in app._notifications]


def user_bubbles(main: MainScreen) -> list[str]:
    return [bubble.text for bubble in main.query_one(ChatPanel).query(UserBubble)]


def error_cards(main: MainScreen) -> list[str]:
    """The transcript's error cards, one still composing its body (mounted, not yet composed) read as ""."""
    return [
        "".join(str(body.content) for body in card.query(".status-body").results(Static))
        for card in main.query_one(ChatPanel).query(ErrorMessage)
    ]


def everything_shown(session: Session) -> str:
    """Toasts, transcript error cards and the status line, joined: wherever a message could have been shown."""
    return "\n".join(
        [
            *(message for _severity, message in notices(session.app)),
            *error_cards(session.main),
            status_line(session.main),
        ]
    )


def card_state(main: MainScreen) -> Any:
    return main.query_one(PromptCard).state


def suggestions_open(main: MainScreen) -> bool:
    suggestions = main.query_one(SuggestionList)
    return suggestions.is_visible and not suggestions.is_loading


# -- shortcuts and their commands ---------------------------------------------


def write_shortcut(name: str, text: str) -> Path:
    path = get_platform().config_dir / "shortcuts" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def loaded_shortcut_path(name: str) -> str:
    """The shortcut's path as the loader returns it, which is what a message's ``PromptShortcutRef`` names."""
    loaded = load_prompt_shortcuts(**prompt_shortcut_folders(str(Path.cwd())))
    return next(shortcut.path for shortcut in loaded if shortcut.name == name)


def held_command(gate: str, output: str) -> str:
    """A placeholder whose command prints *output* once the test creates the file *gate* in the working folder."""
    return f"!`while [ ! -f {gate} ]; do sleep 0.05; done; echo {output}`"


def release_command(gate: str) -> None:
    Path(gate).touch()


def break_the_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The user's shell is gone: every command a send runs fails to start (``$SHELL`` is read per command)."""
    monkeypatch.setenv("SHELL", str(tmp_path / "no-such-shell"))


async def type_text(pilot: Pilot, text: str) -> None:
    await pilot.press(*["space" if char == " " else char for char in text])


def _suggestion_rows(main: MainScreen) -> list[str]:
    return [content.plain for content in main.query_one(SuggestionList)._contents]


def _shortcut_row(main: MainScreen, name: str) -> int | None:
    rows = _suggestion_rows(main)
    if SECTION_SHORTCUTS not in rows:
        return None
    for index in range(rows.index(SECTION_SHORTCUTS) + 1, len(rows)):
        if rows[index] in SECTION_HEADERS:
            return None
        if rows[index].split()[:1] == [f"/{name}"]:
            return index
    return None


async def pick_shortcut(session: Session, name: str) -> None:
    """Type ``/name`` into an empty draft and pick its row from the ``/`` list: it expands into the draft."""
    pilot, main = session.pilot, session.main
    suggestions = main.query_one(SuggestionList)
    await type_text(pilot, f"/{name}")
    await wait_for(
        lambda: suggestions_open(main) and _shortcut_row(main, name) is not None,
        pilot=pilot,
        description=f"/{name} listed",
    )
    target = _shortcut_row(main, name)
    for _ in _suggestion_rows(main):
        if suggestions._highlighted == target:
            break
        await pilot.press("down")
    assert suggestions._highlighted == target, f"could not highlight /{name}"
    await pilot.press("tab")
    await wait_for(
        lambda: session.bar.draft_owner is not None and session.chat.text != f"/{name}",
        pilot=pilot,
        description=f"/{name} expanded into the draft",
    )


async def expand_shortcut(session: Session, name: str) -> ChatDraft:
    """Expand the shortcut *name* into the draft and return the draft, the list closed."""
    await pick_shortcut(session, name)
    await wait_for(lambda: not suggestions_open(session.main), pilot=session.pilot, description="the / list closed")
    return session.bar.draft()


# -- the engine's side ----------------------------------------------------------


def shortcut_messages(session: Session) -> list[ev.UserMessage]:
    return [message for message in session.seen.of(ev.UserMessage) if message.prompt_shortcut is not None]


async def await_commands_running(session: Session) -> tuple[ev.UserMessage, Any]:
    """The last shortcut message published and the engine's word that its commands are running."""

    def found() -> tuple[ev.UserMessage, Any] | None:
        for message in reversed(shortcut_messages(session)):
            for running in session.seen.of(ev.PromptCommandsRunning):
                if running.injection_id == message.injection_id:
                    return message, running
        return None

    await wait_for(
        found, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="the message's commands running"
    )
    result = found()
    assert result is not None
    return result


async def await_commands_ended(session: Session, message: ev.UserMessage) -> Any:
    def found() -> Any:
        return next(
            (ended for ended in session.seen.of(ev.PromptCommandsEnded) if ended.injection_id == message.injection_id),
            None,
        )

    await wait_for(found, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="the message's commands ended")
    return found()


async def await_error(session: Session, code: str) -> ev.Error:
    def found() -> ev.Error | None:
        return next((error for error in session.seen.of(ev.Error) if error.code == code), None)

    await wait_for(found, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description=f"Error {code}")
    error = found()
    assert error is not None
    return error


async def send_plain(session: Session, text: str) -> ev.UserMessage:
    """Type *text* and press Enter; return the message once its turn has started."""
    before = len(session.seen.of(ev.UserMessage))
    await type_text(session.pilot, text)
    await session.pilot.press("enter")
    await wait_for(
        lambda: len(session.seen.of(ev.UserMessage)) > before and session.seen.of(ev.UserMessage)[-1].started_turn,
        pilot=session.pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description=f"{text!r} opened a turn",
    )
    return session.seen.of(ev.UserMessage)[-1]


async def turn_finished(session: Session) -> None:
    """The turn's run, unwinding and save are done, and the screen shows it ended."""
    await await_run_task_chain(session.engine, turn_state=session.engine.turns.turn_state)
    await wait_for(lambda: not session.bar.agent_running, pilot=session.pilot, description="the turn's end shown")


async def gives_back(session: Session, draft: ChatDraft) -> None:
    """The draft is in the composer again, whole: its text, its unfilled fields and the shortcut it belongs to."""
    await wait_for(
        lambda: session.bar.draft() == draft,
        pilot=session.pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description="the draft given back whole",
    )


def no_turn(session: Session) -> bool:
    """No turn started in this session's app and the model was never asked."""
    return session.seen.of(ev.TurnStarted) == [] and session.model.call_count == 0


# ---------------------------------------------------------------------------
# 17. Enter sends the draft with its shortcut; a refusal gives it back
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_enter_sends_the_draft_with_its_shortcut_and_shows_its_commands_running(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = held_command("go", "clean")
    write_shortcut("zzstatus", f"Status: {command}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, chat = session.main, session.pilot, session.chat
        draft = await expand_shortcut(session, "zzstatus")
        assert draft.text == f"Status: {command}"

        # Another message's commands are not this composer's: their events show nothing here.
        foreign = "another-message"
        await session.bus.publish(
            ev.PromptCommandsRunning(session_id=session.session_id(), count=3, injection_id=foreign)
        )
        assert not await wait_until(lambda: "Running 3" in status_line(main), timeout=0.3, pilot=pilot)
        await session.bus.publish(
            ev.PromptCommandsEnded(session_id=session.session_id(), outcome="completed", injection_id=foreign)
        )

        await pilot.press("enter")

        sent, running = await await_commands_running(session)
        assert sent.text == draft.text
        assert sent.prompt_shortcut == ev.PromptShortcutRef(name="zzstatus", path=loaded_shortcut_path("zzstatus"))
        assert sent.injection_id
        assert running.count == 1
        await wait_for(
            lambda: chat.text == "" and "Running 1 command" in status_line(main) and "Esc" in status_line(main),
            pilot=pilot,
            description="composer cleared, the commands shown running",
        )

        release_command("go")

        ended = await await_commands_ended(session, sent)
        assert ended.outcome == "completed"
        await turn_finished(session)
        assert sent.started_turn
        assert "Running 1 command" not in status_line(main)
        # The model, the transcript and history read what was sent: the typed text with the command's output.
        assert "Status: clean" in session.model.last_user_text()
        assert "!`" not in session.model.last_user_text()
        assert user_bubbles(main)[-1] == "Status: clean"
        # Input history keeps the text as typed.
        await pilot.press("up")
        await wait_for(lambda: chat.text == draft.text, pilot=pilot, description="history holds the typed text")


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_refused_message_comes_back_whole_and_only_once(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzreview", "Review $1 with !`echo checked`")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, chat = session.main, session.pilot, session.chat
        draft = await expand_shortcut(session, "zzreview")
        assert draft.fields
        assert draft.owner is not None
        break_the_shell(monkeypatch, tmp_path)

        # The unfilled field is asked about first; the second Enter sends the draft as it is.
        await pilot.press("enter")
        await wait_for(
            lambda: any(NOT_FILLED_IN in message for _severity, message in notices(session.app)),
            pilot=pilot,
            description="the unfilled field asked about",
        )
        assert session.seen.of(ev.UserMessage) == []
        await pilot.press("enter")

        await await_error(session, "prompt_shortcut_command_failed")
        await gives_back(session, draft)
        sent = shortcut_messages(session)[-1]
        assert (await await_commands_ended(session, sent)).outcome == "command_failed"
        # The end of the commands clears their status before the failure shows, so the failure stays.
        assert "echo checked" in status_line(main)
        assert no_turn(session)
        assert card_state(main) is not None
        assert card_state(main).commands == 1
        # The failure names the command; the composer is not in retry mode, since no turn ran.
        # Its card mounts after the draft comes back, so it is waited for.
        await wait_for(
            lambda: any("echo checked" in card for card in error_cards(main)),
            pilot=pilot,
            description="the failure's card naming the command",
        )
        assert not session.bar.retry_mode

        # Given back once: a composer the user empties stays empty.
        chat.select_all()
        await pilot.press("backspace")
        await wait_for(lambda: chat.text == "", pilot=pilot, description="draft deleted")
        assert not await wait_until(lambda: chat.text != "", timeout=0.5, pilot=pilot)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_an_unavailable_shortcut_comes_back_without_its_owner_and_then_sends_as_typed(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_shortcut("zzgone", "Note !`echo ran > ran.txt`")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, chat = session.main, session.pilot, session.chat
        draft = await expand_shortcut(session, "zzgone")
        path.unlink()

        await pilot.press("enter")

        await await_error(session, "prompt_shortcut_unavailable")
        await wait_for(
            lambda: chat.text == draft.text and session.bar.draft_owner is None,
            pilot=pilot,
            description="the draft given back as plain text",
        )
        assert card_state(main) is None
        assert session.seen.of(ev.PromptCommandsRunning) == []
        assert not Path("ran.txt").exists()
        assert no_turn(session)

        # No longer that shortcut's: a second Enter sends it as typed, and nothing runs.
        await pilot.press("enter")
        # Seen as it is published; its turn starts once the engine has admitted it.
        await wait_for(
            lambda: len(session.seen.of(ev.UserMessage)) == 2 and session.seen.of(ev.UserMessage)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="sent again",
        )
        again = session.seen.of(ev.UserMessage)[-1]
        assert again.text == draft.text
        assert again.prompt_shortcut is None
        await turn_finished(session)
        assert "!`echo ran > ran.txt`" in session.model.last_user_text()
        assert not Path("ran.txt").exists()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_session_change_while_the_commands_run_ends_them_and_gives_the_text_back(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: a session transition ends the commands, and the message, never sent, comes back.

    Ambiguity: no TUI control can change the session while the commands run (New, restore and a
    working-folder change are refused then, item 20), so the transition comes from the bus, as the
    engine would get it from any driver. The engine ends the preparation before the session closes
    (the transition's rebuild permit waits for the admission), so the draft comes back while the
    claim's session is still current and nothing here says whether it keeps its owner: only the text
    is pinned. A return that lands in another session, which drops the owner, is pinned by
    ``test_a_session_change_between_the_switch_and_the_message_gives_the_draft_back``."""
    command = held_command("go", "ok")
    write_shortcut("zzwait", f"Wait {command}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        pilot, chat = session.pilot, session.chat
        draft = await expand_shortcut(session, "zzwait")
        first_session = session.session_id()
        await pilot.press("enter")
        sent, _running = await await_commands_running(session)

        transition = asyncio.create_task(session.bus.publish(ev.SessionNew()))
        try:
            assert (await await_commands_ended(session, sent)).outcome == "cancelled"
            await wait_for(transition.done, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="new session")
        finally:
            release_command("go")
            await transition
        await wait_ready(session)
        assert session.session_id() != first_session

        await wait_for(lambda: chat.text == draft.text, pilot=pilot, description="the draft's text given back")
        assert not sent.started_turn
        assert no_turn(session)


# ---------------------------------------------------------------------------
# 18. Esc withdraws a message being prepared; Esc during the switch does nothing
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_esc_while_the_commands_run_withdraws_the_message_and_gives_the_draft_back(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app = session.main, session.pilot, session.app
        draft = await expand_shortcut(session, "zzwait")
        await pilot.press("enter")
        sent, _running = await await_commands_running(session)
        notices_before = len(notices(app))

        await pilot.press("escape")

        await wait_for(
            lambda: [cancel.injection_id for cancel in session.seen.of(ev.UserInjectCancel)] == [sent.injection_id],
            pilot=pilot,
            description="the message withdrawn by its id",
        )
        assert (await await_commands_ended(session, sent)).outcome == "cancelled"
        await gives_back(session, draft)
        assert card_state(main) is not None
        assert not sent.started_turn
        assert no_turn(session)
        assert session.seen.of(ev.UserInterrupt) == []
        assert app.screen is main
        assert "Running 1 command" not in status_line(main)
        # Silent by design, as Esc on a queued mid-turn note: the draft coming back is the answer.
        assert len(notices(app)) == notices_before


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_esc_during_the_switch_does_nothing_and_the_message_goes_out_after_it(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzbeta", f"---\nagent: {BETA}\n---\nReview this")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app = session.main, session.pilot, session.app
        await expand_shortcut(session, "zzbeta")
        build = session.hold()
        session.builds.hold = build

        await pilot.press("enter")
        await wait_for(build.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="switch building")
        await wait_for(lambda: isinstance(app.screen, AgentLoadDialog), pilot=pilot, description="agent-load modal")

        await pilot.press("escape")
        assert not await wait_until(
            lambda: (
                bool(session.seen.of(ev.UserInjectCancel) or session.seen.of(ev.UserInterrupt))
                or not isinstance(app.screen, AgentLoadDialog)
            ),
            timeout=0.5,
            pilot=pilot,
        )
        assert session.seen.of(ev.UserMessage) == []

        build.release()
        # The message is seen as it is published, before the engine admits it: wait for its turn.
        await wait_for(
            lambda: bool(session.seen.of(ev.UserMessage)) and session.seen.of(ev.UserMessage)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the message sent after the switch",
        )
        await turn_finished(session)
        switch = session.seen.of(ev.SessionOnlySwitch)[-1]
        assert switch.applied is True
        assert session.seen.of(ev.UserInjectCancel) == []
        await wait_for(lambda: profile_tag(main) == BETA, pilot=pilot, description="the session runs on Beta")


# ---------------------------------------------------------------------------
# 19. Retry mode: a draft with work is a new message, never a Continue note (R11)
# ---------------------------------------------------------------------------


async def _failed_turn(session: Session) -> None:
    """A first turn that fails, so the composer is in retry mode."""
    session.model.failures.append(FrameworkBoom("the provider failed"))
    await send_plain(session, "hello")
    await turn_finished(session)
    await wait_for(lambda: session.bar.retry_mode, pilot=session.pilot, description="composer in retry mode")


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_enter_in_retry_mode_sends_a_draft_with_work_as_a_new_message(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzstatus", "Status: !`echo clean`")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        pilot = session.pilot
        await _failed_turn(session)
        turns_before = len(session.seen.of(ev.TurnStarted))
        draft = await expand_shortcut(session, "zzstatus")

        await pilot.press("enter")

        await wait_for(
            lambda: bool(shortcut_messages(session)) and shortcut_messages(session)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="sent as a new message",
        )
        await turn_finished(session)
        sent = shortcut_messages(session)[-1]
        assert sent.text == draft.text
        assert session.seen.of(ev.UserRetry) == []
        new_turns = session.seen.of(ev.TurnStarted)[turns_before:]
        assert [turn.is_retry for turn in new_turns] == [False]
        assert "Status: clean" in session.model.last_user_text()
        # The failed turn got no answer, so the new message replaced it: the model is sent only the new one,
        # and the transcript shows what it was sent, with no failed prompt or error card left above it.
        messages, _options = session.model.call_history[-1]
        assert len([message for message in messages if message.role == "user"]) == 1
        assert user_bubbles(session.main) == ["Status: clean"]
        assert error_cards(session.main) == []


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_the_continue_button_keeps_a_draft_with_work_and_says_to_press_enter(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Continue/Retry button sends the composer's text as a note to the failed turn; a shortcut's
    prompt opens a turn of its own, so the button leaves it and says to send it with Enter."""
    write_shortcut("zzstatus", "Status: !`echo clean`")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        pilot, app = session.pilot, session.app
        await _failed_turn(session)
        draft = await expand_shortcut(session, "zzstatus")
        messages_before = len(session.seen.of(ev.UserMessage))
        notices_before = len(notices(app))

        await click_when_settled(pilot, "#send-btn")

        await wait_for(lambda: len(notices(app)) > notices_before, pilot=pilot, description="a notice about the button")
        assert "Enter" in notices(app)[-1][1]
        assert not await wait_until(
            lambda: bool(session.seen.of(ev.UserRetry)) or len(session.seen.of(ev.UserMessage)) > messages_before,
            timeout=0.5,
            pilot=pilot,
        )
        assert session.bar.draft() == draft
        assert session.bar.retry_mode


# ---------------------------------------------------------------------------
# 20. Busy and loading rules, refused controls, the list fallback, no reparse
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_draft_with_work_stays_while_a_turn_runs(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: a message carrying a shortcut never joins a running turn; the draft stays, with a notice."""
    write_shortcut("zzstatus", "Status: !`echo clean > ran.txt`")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        pilot, app = session.pilot, session.app
        answer = session.hold()
        session.model.hold = answer
        await send_plain(session, "hello")
        await wait_for(answer.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="turn running")
        draft = await expand_shortcut(session, "zzstatus")
        notices_before = len(notices(app))

        await pilot.press("enter")

        await wait_for(lambda: len(notices(app)) > notices_before, pilot=pilot, description="a notice")
        assert not await wait_until(
            lambda: len(session.seen.of(ev.UserMessage)) > 1 or bool(session.seen.of(ev.UserInject)),
            timeout=0.5,
            pilot=pilot,
        )
        assert session.bar.draft() == draft
        assert not Path("ran.txt").exists()

        # Kept for later: once the turn has ended, Enter sends it.
        answer.release()
        await turn_finished(session)
        await pilot.press("enter")
        await wait_for(
            lambda: bool(shortcut_messages(session)) and shortcut_messages(session)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="sent once the turn ended",
        )
        await turn_finished(session)
        assert Path("ran.txt").exists()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_draft_with_work_whose_turn_started_after_its_keypress_comes_back_and_never_joins_it(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 between the keypress and the screen: the router sent the draft as a new message while no turn ran, and a
    turn started before the screen took it. It comes back with the busy notice, never as a note to that turn."""
    write_shortcut("zzstatus", "Status: !`echo clean > ran.txt`")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        pilot, app = session.pilot, session.app
        answer = session.hold()
        session.model.hold = answer
        await send_plain(session, "hello")
        await wait_for(answer.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="turn running")
        draft = await expand_shortcut(session, "zzstatus")
        notices_before = len(notices(app))

        # What the input bar did at a keypress its router answered before the turn started.
        session.chat.clear()
        session.bar.post_message(InputBar.UserSubmitted(draft.text.strip(), draft))

        await wait_for(lambda: session.bar.draft() == draft, pilot=pilot, description="the draft back")
        assert len(notices(app)) > notices_before
        assert not await wait_until(
            lambda: len(session.seen.of(ev.UserMessage)) > 1 or bool(session.seen.of(ev.UserInject)),
            timeout=0.5,
            pilot=pilot,
        )
        assert not Path("ran.txt").exists()
        answer.release()
        await turn_finished(session)


@pytest.mark.parametrize("text", ["hello there", "/help"], ids=["message", "command"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_draft_whose_load_started_after_its_keypress_comes_back(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """The input bar gave the draft up at a keypress no load stopped; one began before the screen took it."""
    async with running_session(agent_engine, tmp_path, monkeypatch, hold_startup_load=True) as session:
        pilot = session.pilot
        startup = session.holds[0]
        await wait_for(startup.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="agent loading")
        draft = ChatDraft(text)

        # What the input bar did at a keypress its router answered before the load started.
        session.bar.post_message(InputBar.UserSubmitted(text, draft))

        await wait_for(lambda: session.bar.draft() == draft, pilot=pilot, description="the draft back")
        assert not await wait_until(lambda: bool(session.seen.of(ev.UserMessage)), timeout=0.3, pilot=pilot)
        startup.release()
        await wait_ready(session)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_draft_with_work_stays_while_the_agent_loads(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held at the startup load's first event, before the load's modal opens: the agent is loading."""
    write_shortcut("zzstatus", "Status: !`echo clean > ran.txt`")
    async with running_session(agent_engine, tmp_path, monkeypatch, hold_startup_load=True) as session:
        pilot = session.pilot
        startup = session.holds[0]
        await wait_for(startup.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="agent loading")
        assert session.bar.agent_loading
        session.bar.focus_input()
        await wait_for(lambda: session.chat.has_focus, pilot=pilot, description="chat input focused")
        draft = await expand_shortcut(session, "zzstatus")

        await pilot.press("enter")

        assert not await wait_until(lambda: bool(session.seen.of(ev.UserMessage)), timeout=0.5, pilot=pilot)
        assert session.bar.draft() == draft
        assert not Path("ran.txt").exists()

        startup.release()
        await wait_ready(session)
        assert session.bar.draft() == draft
        await pilot.press("enter")
        await wait_for(
            lambda: bool(shortcut_messages(session)) and shortcut_messages(session)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="sent once loaded",
        )
        await turn_finished(session)
        assert Path("ran.txt").exists()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_agent_model_and_new_session_are_refused_while_commands_run(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6: while the commands run the execution is busy, so ``#``, ``$`` and New wait as for a running turn."""
    write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, chat = session.main, session.pilot, session.chat
        await send_plain(session, "hello")
        await turn_finished(session)
        first_session = session.session_id()
        await expand_shortcut(session, "zzwait")
        await pilot.press("enter")
        sent, _running = await await_commands_running(session)
        await wait_for(lambda: chat.text == "", pilot=pilot, description="composer cleared")
        try:
            for trigger in ("#", "$"):
                await pilot.press(trigger)
                await wait_for(lambda t=trigger: chat.text == t, pilot=pilot, description=f"{trigger} typed")
                assert not await wait_until(lambda: suggestions_open(main), timeout=0.3, pilot=pilot), trigger
                await pilot.press("backspace")
                await wait_for(lambda: chat.text == "", pilot=pilot, description=f"{trigger} deleted")
            await type_text(pilot, "/new")
            await pilot.press("enter")
            assert not await wait_until(lambda: bool(session.seen.of(ev.SessionNew)), timeout=0.5, pilot=pilot)
            chat.select_all()
            await pilot.press("backspace")
        finally:
            release_command("go")

        assert (await await_commands_ended(session, sent)).outcome == "completed"
        await turn_finished(session)
        assert session.seen.of(ev.SessionNew) == []
        assert session.session_id() == first_session

        # Positive control: the same key opens the agent list once nothing runs.
        await wait_for(lambda: chat.text == "", pilot=pilot, description="composer empty")
        await pilot.press("#")
        await wait_for(lambda: suggestions_open(main), pilot=pilot, description="agent list open when idle")
        await pilot.press("escape")


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_quitting_while_the_commands_run_ends_them_and_the_send_without_an_error(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app exits with a send in flight: its screen's widgets are pruned while the send waits on the engine, and
    the send and the engine's end of the commands, arriving after that, find nothing to update and fail nothing."""
    write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await expand_shortcut(session, "zzwait")
        await session.pilot.press("enter")
        sent, _running = await await_commands_running(session)
        # Leaving the block exits the app, the command still running: ``run_test`` re-raises a worker's failure.

    ended = [event for event in session.seen.of(ev.PromptCommandsEnded) if event.injection_id == sent.injection_id]
    assert [event.outcome for event in ended] == ["cancelled"]
    assert sent.started_turn is False


@pytest.mark.parametrize(
    ("key", "dialog", "notice"),
    [("f4", ModelConfigScreen, "#mc-read-only-notice"), ("f2", AgentsConfigScreen, "#ac-read-only-notice")],
    ids=["models", "agents"],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_config_dialog_opened_while_a_send_is_in_flight_is_read_only(
    agent_engine: AgentEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    dialog: type[Screen[str]],
    notice: str,
) -> None:
    """A dialog reads the session once, as it opens, and the send then starts its turn under it: left editable,
    the model the session runs on could be deleted with no reload."""
    write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    # A provider the Models dialog can show (it has no form for ``mock``); every build gets the scripted model anyway.
    for profile_id, name in MODEL_NAMES.items():
        save_profile(ModelProfile(id=profile_id, name=name, provider="openai", model_id="gpt-test", stream=False))
    set_global_default_profile_id(MAIN_MODEL)
    models = ModelProfileRegistry()
    models.load_all()
    async with running_session(agent_engine, tmp_path, monkeypatch, models=models) as session:
        main, pilot, app, chat = session.main, session.pilot, session.app, session.chat

        async def opens_read_only() -> bool:
            await pilot.press(key)
            await wait_for(
                lambda: isinstance(app.screen, dialog) and bool(app.screen.query(notice)),
                pilot=pilot,
                description="dialog open",
            )
            shown = app.screen.query_one(notice).display
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot, description="dialog closed")
            return shown

        # Nothing in flight: the same dialog is editable.
        assert not await opens_read_only()

        await expand_shortcut(session, "zzwait")
        await pilot.press("enter")
        sent, _running = await await_commands_running(session)
        try:
            assert await opens_read_only()
        finally:
            release_command("go")
        assert (await await_commands_ended(session, sent)).outcome == "completed"
        await turn_finished(session)
        await wait_for(lambda: chat.text == "", pilot=pilot, description="composer empty")


@pytest.mark.parametrize("entry", ["list_fallback", "send_button"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_every_submit_route_asks_about_unfilled_fields_first(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The unfilled-fields question lives in the input bar's submit (4.4), which every route reaches.

    Ambiguity: no list opens over a shortcut's draft (typing ``/``, ``#`` or a ``/name`` into it opens
    none: an owned draft is never read as a typed name), so the ``/`` list's Enter fallback cannot be
    reached with fields by keys. ``list_fallback`` takes the route the fallback takes once the list
    has let Enter through, the input bar's ``submit_input``; ``send_button`` is the Send button."""
    write_shortcut("zzfields", "Review $1 and $2")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app = session.main, session.pilot, session.app
        draft = await expand_shortcut(session, "zzfields")
        assert len(draft.fields) == 2
        button = main.query_one("#send-btn")

        async def submit() -> None:
            if entry == "list_fallback":
                session.bar.submit_input()
                return
            # A Button ignores clicks during its short press effect.
            await wait_for(lambda: not button.has_class("-active"), pilot=pilot, description="press effect over")
            await click_when_settled(pilot, button)

        await submit()

        await wait_for(
            lambda: any(NOT_FILLED_IN in message for _severity, message in notices(app)),
            pilot=pilot,
            description="the unfilled fields asked about",
        )
        assert not await wait_until(lambda: bool(session.seen.of(ev.UserMessage)), timeout=0.3, pilot=pilot)
        assert session.bar.draft() == draft

        await submit()

        # Seen as it is published; its turn starts once the engine has admitted it.
        await wait_for(
            lambda: bool(session.seen.of(ev.UserMessage)) and session.seen.of(ev.UserMessage)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="sent as it is",
        )
        sent = session.seen.of(ev.UserMessage)[-1]
        assert sent.text == draft.text
        assert sent.prompt_shortcut is None
        await turn_finished(session)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_shortcuts_draft_that_names_a_command_runs_it_as_typed_text_would(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft naming a command runs it, a shortcut's too (4.4): ``/help please`` opens the guide, nothing is sent."""
    write_shortcut("zzhelp", "/help please")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app = session.main, session.pilot, session.app
        draft = await expand_shortcut(session, "zzhelp")
        assert draft.text == "/help please"

        await pilot.press("enter")

        await wait_for(lambda: isinstance(app.screen, GuideDialog), pilot=pilot, description="the guide opened")
        assert session.seen.of(ev.UserMessage) == []
        assert main.query_one(InputBar).draft_owner is None


# ---------------------------------------------------------------------------
# 21. The switch, then the message
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_the_switch_goes_live_before_the_message_and_its_marker_sits_above_it(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzfast", f"---\nagent: {BETA}\nmodel: {FAST_MODEL}\n---\nReview this")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, seen = session.main, session.pilot, session.seen
        await send_plain(session, "hello")
        await turn_finished(session)
        await expand_shortcut(session, "zzfast")
        loads_before = len(seen.of(ev.AgentLoadStarted))

        await pilot.press("enter")

        await wait_for(
            lambda: len(seen.of(ev.UserMessage)) == 2 and seen.of(ev.UserMessage)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the shortcut's message sent",
        )
        await turn_finished(session)
        switch = seen.of(ev.SessionOnlySwitch)[-1]
        sent = seen.of(ev.UserMessage)[-1]
        assert (switch.agent, switch.model_profile, switch.applied) == (BETA, FAST_MODEL, True)
        # A shortcut with no commands sends a plain message.
        assert sent.prompt_shortcut is None
        assert sent.injection_id is None
        # One rebuild for both halves, done before the message is published.
        switched = [event for event in seen.of(ev.ProfileSwitched) if event.to_profile == BETA][-1]
        model_switched = [event for event in seen.of(ev.ModelProfileSwitched) if event.model_profile_id == FAST_MODEL]
        assert seen.position(switch) < seen.position(switched) < seen.position(sent)
        assert model_switched
        assert seen.position(model_switched[-1]) < seen.position(sent)
        loads = [
            event for event in seen.of(ev.AgentLoadStarted)[loads_before:] if seen.position(event) < seen.position(sent)
        ]
        assert len(loads) == 1
        assert session.builds.profiles[-1] == FAST_MODEL
        await wait_for(
            lambda: profile_tag(main) == BETA and model_tag(main) == MODEL_NAMES[FAST_MODEL],
            pilot=pilot,
            description="the session runs on Beta and Fast",
        )
        # The switch's marker lands above the shortcut's message.
        order = list(main.query_one(ChatPanel).walk_children())
        markers = [widget for widget in order if isinstance(widget, SystemMessage) and BETA in str(widget.content)]
        bubbles = [widget for widget in order if isinstance(widget, UserBubble) and widget.text == "Review this"]
        assert markers
        assert bubbles
        assert order.index(markers[-1]) < order.index(bubbles[-1])


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_failed_switch_sends_nothing_and_gives_the_draft_back(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzbroken", f"---\nmodel: {BROKEN_MODEL}\n---\nReview this")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app = session.main, session.pilot, session.app
        draft = await expand_shortcut(session, "zzbroken")
        session.builds.failing.add(BROKEN_MODEL)

        await pilot.press("enter")

        await wait_for(
            lambda: isinstance(app.screen, AgentLoadDialog) and app.screen.has_class("-error"),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the load failure shown",
        )
        await click_when_settled(pilot, "#agent-load-ok")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="failure acknowledged")
        await gives_back(session, draft)
        assert session.seen.of(ev.UserMessage) == []
        assert session.seen.of(ev.SessionOnlySwitch)[-1].applied is False
        assert no_turn(session)
        assert profile_tag(main) == ALPHA
        assert model_tag(main) == MODEL_NAMES[MAIN_MODEL]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_switch_the_engine_may_not_make_now_refuses_the_send_and_is_no_failed_turn(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_shortcut("zzfast", f"---\nmodel: {FAST_MODEL}\n---\nReview this")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot = session.main, session.pilot
        await send_plain(session, "hello")
        await turn_finished(session)
        draft = await expand_shortcut(session, "zzfast")
        # A rebuild the engine may not make now (another load still active, say) is refused at its permit.
        permits = session.engine.permits

        async def refuse(*_args: Any, **_kwargs: Any) -> RebuildPermitDenied:
            return RebuildPermitDenied(
                reason="load_active",
                code="runtime_mutation_load_active",
                message="Cannot rebuild while an agent load is still active.",
            )

        monkeypatch.setattr(
            permits,
            "acquire_rebuild_permit",
            create_autospec(permits.acquire_rebuild_permit, side_effect=refuse),
        )

        await pilot.press("enter")

        await await_error(session, "runtime_mutation_load_active")
        await gives_back(session, draft)
        assert session.seen.of(ev.SessionOnlySwitch)[-1].applied is False
        assert [message.text for message in session.seen.of(ev.UserMessage)] == ["hello"]
        # A refused send, not a failed turn: Retry would run the finished turn again.
        assert not session.bar.retry_mode
        assert not await wait_until(lambda: session.bar.retry_mode, timeout=0.3, pilot=pilot)
        assert model_tag(main) == MODEL_NAMES[MAIN_MODEL]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_session_change_between_the_switch_and_the_message_gives_the_draft_back(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held after the engine applied the switch, a new session starts before the message goes out:
    nothing is sent, the draft comes back without its owner, since it lands in another session
    (item 17's return into another session), and the new session runs on the default agent, the
    switch having ended with the session it was for (R10a).

    The new session comes from the bus, as from any driver: the screen refuses New meanwhile."""
    write_shortcut("zzbeta", f"---\nagent: {BETA}\n---\nReview this")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, chat = session.main, session.pilot, session.chat
        draft = await expand_shortcut(session, "zzbeta")
        first_session = session.session_id()
        between = session.hold()
        await hold_next(session.bus, ev.SessionOnlySwitch, between)

        await pilot.press("enter")
        await wait_for(between.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="switch applied")
        assert session.seen.of(ev.SessionOnlySwitch)[-1].applied is True

        await session.bus.publish(ev.SessionNew())
        assert session.session_id() != first_session
        between.release()

        await wait_ready(session)
        await wait_for(
            lambda: chat.text == draft.text and session.bar.draft_owner is None,
            pilot=pilot,
            description="the draft given back without its owner",
        )
        assert not await wait_until(lambda: bool(session.seen.of(ev.UserMessage)), timeout=0.3, pilot=pilot)
        assert no_turn(session)
        assert profile_tag(main) == ALPHA


# ---------------------------------------------------------------------------
# 22 and 30. An unrelated error published during a refused preparation
# ---------------------------------------------------------------------------

_UNRELATED = "The workspace index could not be refreshed"


async def _refused_with_an_unrelated_error(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """Send a shortcut's draft, publish an unrelated ``Error`` while the engine prepares it, and have it refused.

    *refusal* is ``withdrawn`` (Esc while the commands run) or ``command_failed``
    (the command cannot start; the error goes out while the commands' start is held).
    """
    pilot = session.pilot
    if refusal == "withdrawn":
        write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    else:
        write_shortcut("zzwait", "Wait !`echo ok`")
    draft = await expand_shortcut(session, "zzwait")
    unrelated = ev.Error(code="zz_index_refresh_failed", message=_UNRELATED, session_id=session.session_id())
    if refusal == "withdrawn":
        await pilot.press("enter")
        sent, _running = await await_commands_running(session)
        await session.bus.publish(unrelated)
        await pilot.press("escape")
    else:
        break_the_shell(monkeypatch, tmp_path)
        starting = session.hold()
        await hold_next(session.bus, ev.PromptCommandsRunning, starting)
        await pilot.press("enter")
        await wait_for(starting.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="commands start")
        await session.bus.publish(unrelated)
        starting.release()
        sent = shortcut_messages(session)[-1]
    expected = "cancelled" if refusal == "withdrawn" else "command_failed"
    assert (await await_commands_ended(session, sent)).outcome == expected
    await gives_back(session, draft)
    assert no_turn(session)


@pytest.mark.parametrize("refusal", ["withdrawn", "command_failed"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_an_unrelated_error_during_a_refused_preparation_is_shown(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """Item 22: the screen holds back errors while it waits for the message's turn; when no turn
    comes, the errors it held are still shown (4.3), not dropped with the turn's content."""
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await _refused_with_an_unrelated_error(session, tmp_path, monkeypatch, refusal)
        await wait_for(
            lambda: _UNRELATED in everything_shown(session), pilot=session.pilot, description="the error shown"
        )


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_kept_error_shows_as_a_notice_and_the_composer_does_not_enter_retry_mode(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 30: a held error is shown as a warning toast, not on the turn-failure surface, which would
    add an error card and put the composer in retry mode while the engine is idle."""
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot = session.main, session.pilot
        await _refused_with_an_unrelated_error(session, tmp_path, monkeypatch, "withdrawn")
        await wait_for(
            lambda: any(_UNRELATED in message for _severity, message in notices(session.app)),
            pilot=pilot,
            description="the error shown as a notice",
        )
        assert [severity for severity, message in notices(session.app) if _UNRELATED in message] == ["warning"]
        assert not any(_UNRELATED in card for card in error_cards(main))
        assert not session.bar.retry_mode
        assert not await wait_until(lambda: session.bar.retry_mode, timeout=0.3, pilot=pilot)


# ---------------------------------------------------------------------------
# 26 (TUI). The screen follows the busy state the commands hold
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_the_footer_status_bar_and_workflow_entry_follow_the_commands_and_a_refusal_ends_it(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambiguity: the workflow Start button lives in workflow mode, and the mode is pinned while a
    chat submit is in flight, so a workflow cannot be reached, let alone started, during the
    commands; the mode badge is the workflow entry the user can press, and it says why it waits.

    A withdraw is the refusal here: it is the one a test can time after the busy checks."""
    write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app, seen = session.main, session.pilot, session.app, session.seen
        await wait_for(lambda: "sessions" in footer_actions(main), pilot=pilot, description="idle footer")
        draft = await expand_shortcut(session, "zzwait")

        await pilot.press("enter")
        sent, running = await await_commands_running(session)

        # While the commands run the execution reads as a turn that cannot be interrupted.
        await wait_for(
            lambda: any(
                change.snapshot.kind == "turn"
                and not change.snapshot.cancellable
                and seen.position(change) > seen.position(sent)
                for change in seen.of(ev.ExecutionChanged)
            ),
            pilot=pilot,
            description="the execution busy with the commands",
        )
        await wait_for(lambda: "sessions" not in footer_actions(main), pilot=pilot, description="busy footer")
        await click_when_settled(pilot, "#profile-tag")
        await wait_for(
            lambda: any("Cannot switch agents" in message for _severity, message in notices(app)),
            pilot=pilot,
            description="the agent selector waits",
        )
        assert not isinstance(app.screen, AgentsScreen)
        await click_when_settled(pilot, "#mode-badge")
        await wait_for(
            lambda: any("Cannot switch app mode" in message for _severity, message in notices(app)),
            pilot=pilot,
            description="the workflow mode waits",
        )
        assert not isinstance(app.screen, AppModeDialog)

        await pilot.press("escape")

        ended = await await_commands_ended(session, sent)
        assert ended.outcome == "cancelled"
        await wait_for(
            lambda: any(
                change.snapshot.kind == "idle" and seen.position(change) > seen.position(ended)
                for change in seen.of(ev.ExecutionChanged)
            ),
            pilot=pilot,
            description="the execution idle again",
        )
        await wait_for(lambda: "sessions" in footer_actions(main), pilot=pilot, description="footer back")
        assert "Running 1 command" not in status_line(main)
        # The draft came back with its card, which sits over the status bar: the user deletes it first.
        await gives_back(session, draft)
        session.chat.select_all()
        await pilot.press("backspace")
        await wait_for(lambda: card_state(main) is None, pilot=pilot, description="draft deleted, card gone")
        await click_when_settled(pilot, "#profile-tag")
        await wait_for(lambda: isinstance(app.screen, AgentsScreen), pilot=pilot, description="agent picker opens")
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="agent picker closed")
        assert running.count == 1


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_second_esc_while_the_withdraw_settles_opens_no_confirm(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The withdraw owns Esc for as long as the message is being published (4.5); the publish is kept
    open here by holding the engine's word that the commands ended."""
    write_shortcut("zzwait", f"Wait {held_command('go', 'ok')}")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main, pilot, app, seen = session.main, session.pilot, session.app, session.seen
        draft = await expand_shortcut(session, "zzwait")
        settling = session.hold()
        await hold_next(session.bus, ev.PromptCommandsEnded, settling)
        await pilot.press("enter")
        sent, _running = await await_commands_running(session)

        await pilot.press("escape")
        await wait_for(settling.entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="withdrawing")
        assert len(seen.of(ev.UserInjectCancel)) == 1

        await pilot.press("escape")
        assert not await wait_until(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                or len(seen.of(ev.UserInjectCancel)) > 1
                or bool(seen.of(ev.UserInterrupt))
            ),
            timeout=0.5,
            pilot=pilot,
        )
        assert app.screen is main

        settling.release()
        await gives_back(session, draft)
        assert not sent.started_turn
        assert no_turn(session)
        assert app.screen is main
