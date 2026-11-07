# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The buddy's answer to a pet: which model is asked, with what, and what happens when it cannot answer."""

from __future__ import annotations

import inspect
import threading
from typing import TYPE_CHECKING, Any

import pytest

from chrys.app.features.buddy.replies import STOCK_REPLIES, pet_reply, stock_reply
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.llm.route_sessions import derive_llm_route_session_id
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import default_profile
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.buddies import a_buddy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _stock_lines(name: str) -> set[str]:
    """The stock lines as their English fallback renders them, so no wording is pinned."""
    return {format_message(line.bind(name=name)) for line in STOCK_REPLIES}


def _reply_text(reply: MessageRef | str) -> str:
    """A model answer already is text; a stock reply renders through its fallback."""
    return reply if isinstance(reply, str) else format_message(reply)


class _Response:
    text = "  hoot hoot  "


class _Stream:
    def __init__(self) -> None:
        self._done = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> object:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return object()

    async def get_final_response(self) -> _Response:
        return _Response()


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def get_response(
        self, messages: list[Message], *, stream: bool = False, options: dict[str, Any] | None = None
    ) -> _Response | _Stream:
        self.calls.append({"messages": messages, "stream": stream, "options": options})
        return _Stream() if stream else _Response()

    async def aclose(self) -> None:
        self.closed = True


class _Engine:
    def __init__(
        self,
        session_dir: Path,
        *,
        profile: ModelProfile | None,
        registry: ModelProfileRegistry | None = None,
        settings: Settings | None = None,
        history: list[Message] | None = None,
    ) -> None:
        self.active_model_profile = profile
        self.session_id = "sess-buddy"
        self.session_dir = session_dir
        self.model_registry = registry
        self.settings = settings if settings is not None else Settings()
        self._history = history

    @property
    def history_messages(self) -> list[Message]:
        if self._history is None:
            raise RuntimeError("history is not bound")
        return self._history


def test_the_engine_double_offers_what_the_real_engine_offers(tmp_path: Path) -> None:
    double = _Engine(tmp_path, profile=None)
    offered = {name for name in (*vars(double), *vars(_Engine)) if not name.startswith("_")}

    assert offered == {
        "active_model_profile",
        "history_messages",
        "model_registry",
        "session_dir",
        "session_id",
        "settings",
    }
    for name in offered:
        assert isinstance(inspect.getattr_static(AgentEngine, name), property), name


def _client_factory(monkeypatch: pytest.MonkeyPatch, make: Callable[[ModelProfile], _Client]) -> None:
    """Stand in for the client factory. Its routing arguments are spelled out, so a new one fails here first."""

    async def create_client(
        profile: ModelProfile,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        session_dir: Path | None = None,
    ) -> _Client:
        return make(profile)

    monkeypatch.setattr("chrys.service.llm.clients.create_client", create_client)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    client = _Client()
    _client_factory(monkeypatch, lambda _profile: client)
    return client


def _no_engine(monkeypatch: pytest.MonkeyPatch, profile: ModelProfile) -> None:
    monkeypatch.setattr("chrys.orchestration.engine.engine.get_current_engine", lambda: None)
    monkeypatch.setattr(
        "chrys.service.profiles.models.resolver.resolve_active_profile", lambda _registry, _settings: profile
    )


def _engine(monkeypatch: pytest.MonkeyPatch, engine: _Engine) -> None:
    monkeypatch.setattr("chrys.orchestration.engine.engine.get_current_engine", lambda: engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_the_reply_follows_the_profile_stream_setting(
    stream: bool, client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = default_profile()
    profile.stream = stream
    _no_engine(monkeypatch, profile)

    assert await pet_reply(a_buddy()) == "💛 hoot hoot"
    assert [call["stream"] for call in client.calls] == [stream]


def _profiles_loaded_on(monkeypatch: pytest.MonkeyPatch, *profiles: ModelProfile) -> list[threading.Thread]:
    """Stand in for the profiles on disk: *profiles*, and a note of the thread each load ran on."""
    threads: list[threading.Thread] = []

    def load_profiles(registry: ModelProfileRegistry, directory: Path | None = None) -> int:
        threads.append(threading.current_thread())
        for profile in profiles:
            registry.register(profile)
        return len(profiles)

    monkeypatch.setattr(ModelProfileRegistry, "load_profiles", load_profiles)
    return threads


@pytest.mark.asyncio
async def test_before_any_session_the_profiles_on_disk_are_read_only_when_a_buddy_model_is_set(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHRYS_MODEL_PROFILE_BUDDY", raising=False)
    _no_engine(monkeypatch, default_profile())
    loads = _profiles_loaded_on(monkeypatch)

    await pet_reply(a_buddy())

    assert client.calls[0]["options"]["model"] == default_profile().model_id
    assert loads == []


@pytest.mark.asyncio
async def test_the_call_is_routed_under_the_current_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = default_profile()
    client = _Client()
    context: dict[str, object] = {}

    async def create_client(
        _profile: ModelProfile,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        session_dir: Path | None = None,
    ) -> _Client:
        context.update(session_id=session_id, parent_session_id=parent_session_id, session_dir=session_dir)
        return client

    _engine(monkeypatch, _Engine(tmp_path, profile=profile))
    monkeypatch.setattr("chrys.service.llm.clients.create_client", create_client)

    assert await pet_reply(a_buddy()) == "💛 hoot hoot"
    assert context == {
        "session_id": derive_llm_route_session_id("sess-buddy", route_kind="buddy-reply", model_profile=profile),
        "parent_session_id": "sess-buddy",
        "session_dir": tmp_path,
    }


@pytest.mark.asyncio
async def test_an_engine_that_has_built_no_agent_yet_asks_the_active_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Before the first build the engine has no profile of its own, but it knows the ones the active pointer names."""
    active = ModelProfile(id="active-model", name="Active", model_id="gpt-active")
    registry = ModelProfileRegistry()
    registry.register(active)
    asked: list[ModelProfile] = []
    client = _Client()

    def make(profile: ModelProfile) -> _Client:
        asked.append(profile)
        return client

    _engine(monkeypatch, _Engine(tmp_path, profile=None, registry=registry, settings=Settings(model_profile=active.id)))
    _client_factory(monkeypatch, make)

    assert await pet_reply(a_buddy()) == "💛 hoot hoot"
    assert asked == [active]
    assert client.calls[0]["options"]["model"] == "gpt-active"


_ACTIVE = ModelProfile(id="active-model", name="Active", model_id="gpt-active", base_url="https://active.example/v1")
_PET = ModelProfile(
    id="pet-profile",
    name="Pet Profile",
    provider="anthropic",
    model_id="claude-pet",
    base_url="https://pet.example",
    stream=False,
    max_output_tokens=300,
)


def _asked(monkeypatch: pytest.MonkeyPatch) -> tuple[list[ModelProfile], _Client]:
    """The profile each client was built for, and the client every call goes to."""
    asked: list[ModelProfile] = []
    client = _Client()

    def make(profile: ModelProfile) -> _Client:
        asked.append(profile)
        return client

    _client_factory(monkeypatch, make)
    return asked, client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector", [_PET.id, _PET.name, _PET.name.upper()], ids=["by-id", "by-name", "by-folded-name"]
)
async def test_a_chosen_buddy_profile_answers_over_its_own_connection(
    selector: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Like the other model roles, the buddy setting picks a whole profile: its provider, endpoint and model."""
    registry = _registry_with(_ACTIVE, _PET)
    asked: list[ModelProfile] = []
    client = _Client()
    context: dict[str, object] = {}

    async def create_client(
        profile: ModelProfile,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        session_dir: Path | None = None,
    ) -> _Client:
        asked.append(profile)
        context.update(session_id=session_id, parent_session_id=parent_session_id, session_dir=session_dir)
        return client

    _engine(
        monkeypatch,
        _Engine(tmp_path, profile=_ACTIVE, registry=registry, settings=Settings(buddy_model_profile=selector)),
    )
    monkeypatch.setattr("chrys.service.llm.clients.create_client", create_client)

    assert await pet_reply(a_buddy()) == "💛 hoot hoot"
    assert asked == [_PET]
    [call] = client.calls
    assert call["stream"] is False
    assert call["options"]["model"] == "claude-pet"
    assert call["options"]["max_tokens"] == 300
    assert context == {
        "session_id": derive_llm_route_session_id("sess-buddy", route_kind="buddy-reply", model_profile=_PET),
        "parent_session_id": "sess-buddy",
        "session_dir": tmp_path,
    }


@pytest.mark.asyncio
async def test_before_any_session_a_chosen_buddy_profile_is_found_on_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting is configuration, not session state, and a pet can come before the first session."""
    asked, client = _asked(monkeypatch)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE_BUDDY", _PET.id)
    _no_engine(monkeypatch, _ACTIVE)
    loads = _profiles_loaded_on(monkeypatch, _ACTIVE, _PET)

    await pet_reply(a_buddy())

    assert asked == [_PET]
    assert client.calls[0]["options"]["model"] == "claude-pet"
    assert len(loads) == 1 and loads[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_a_buddy_profile_that_is_gone_leaves_the_active_model_answering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A profile deleted since it was chosen names no connection, and is never sent as a model name."""
    asked, client = _asked(monkeypatch)
    registry = _registry_with(_ACTIVE, _PET)
    _engine(
        monkeypatch,
        _Engine(tmp_path, profile=_ACTIVE, registry=registry, settings=Settings(buddy_model_profile="gpt-mini")),
    )

    await pet_reply(a_buddy())

    assert asked == [_ACTIVE]
    assert client.calls[0]["options"]["model"] == "gpt-active"


@pytest.mark.asyncio
async def test_a_chosen_buddy_profile_not_filled_in_yet_leaves_the_active_model_answering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unfinished = ModelProfile(id="unfinished", name="Unfinished")
    asked, client = _asked(monkeypatch)
    registry = _registry_with(_ACTIVE, unfinished)
    _engine(
        monkeypatch,
        _Engine(tmp_path, profile=_ACTIVE, registry=registry, settings=Settings(buddy_model_profile=unfinished.id)),
    )

    await pet_reply(a_buddy())

    # Never the profile's id sent as a model name.
    assert asked == [_ACTIVE]
    assert client.calls[0]["options"]["model"] == "gpt-active"


@pytest.mark.asyncio
async def test_the_model_is_told_who_the_buddy_is_and_what_was_said_lately(
    client: _Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    history: list[Message] = []
    for index in range(12):
        # Every turn opens with a marker, which is bookkeeping and not something anybody said.
        history.append(Message("user", ["turn"], additional_properties={HistoryMarkerKind.KEY: "turn"}))
        history.append(Message("user", [f"question {index}"]))
    history.append(Message("assistant", ["the last answer"]))
    _engine(monkeypatch, _Engine(tmp_path, profile=default_profile(), history=history))
    buddy = a_buddy()

    await pet_reply(buddy)

    system, user = client.calls[0]["messages"]
    assert (system.role, user.role) == ("system", "user")
    assert all(part in system.text for part in (buddy.name, buddy.species.value, format_message(buddy.persona)))
    assert "User: question 7" in user.text
    assert "question 6" not in user.text
    assert "turn" not in user.text
    assert "Assistant: the last answer" in user.text


@pytest.mark.asyncio
async def test_a_wall_of_text_in_the_conversation_reaches_the_model_only_by_its_tail(
    client: _Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pasted = "".join(f"line {index:04d}\n" for index in range(1000))
    history = [Message("user", [pasted]), Message("assistant", ["that is a lot"])]
    _engine(monkeypatch, _Engine(tmp_path, profile=default_profile(), history=history))

    await pet_reply(a_buddy())

    user_text = client.calls[0]["messages"][1].text
    assert "User: …" in user_text
    assert "line 0999" in user_text
    assert "line 0000" not in user_text
    assert len(user_text) < 1000
    assert "Assistant: that is a lot" in user_text


@pytest.mark.asyncio
async def test_an_engine_without_bound_history_still_gets_an_answer(
    client: _Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _engine(monkeypatch, _Engine(tmp_path, profile=default_profile(), history=None))

    assert await pet_reply(a_buddy()) == "💛 hoot hoot"
    assert "conversation" not in client.calls[0]["messages"][1].text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError("offline"), TimeoutError("slow"), ValueError("bad profile")])
async def test_a_model_that_cannot_answer_is_covered_by_a_stock_line(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make(_profile: ModelProfile) -> _Client:
        raise failure

    _no_engine(monkeypatch, default_profile())
    _client_factory(monkeypatch, make)
    buddy = a_buddy()

    assert _reply_text(await pet_reply(buddy)) in _stock_lines(buddy.name)


@pytest.mark.asyncio
async def test_an_empty_answer_is_covered_by_a_stock_line(client: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_engine(monkeypatch, default_profile())
    monkeypatch.setattr(_Response, "text", "   ")
    buddy = a_buddy()

    assert _reply_text(await pet_reply(buddy)) in _stock_lines(buddy.name)


class _FailingClient(_Client):
    async def get_response(
        self, messages: list[Message], *, stream: bool = False, options: dict[str, Any] | None = None
    ) -> _Response | _Stream:
        raise ConnectionError("offline")


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [True, False])
async def test_reply_closes_its_client(answers: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client() if answers else _FailingClient()
    _client_factory(monkeypatch, lambda _profile: client)
    _no_engine(monkeypatch, default_profile())
    buddy = a_buddy()

    reply = await pet_reply(buddy)

    assert (reply == "💛 hoot hoot") is answers
    assert client.closed


def test_a_stock_reply_needs_no_model_and_names_the_buddy() -> None:
    buddy = a_buddy(name="Nori")
    assert _reply_text(stock_reply(buddy)) in _stock_lines("Nori")
    # The reply mark rides in each template now, and nothing else in the module adds it to a
    # stock line: this literal is what tells if a template loses it.
    assert all(line.startswith("💛 ") for line in _stock_lines("Nori"))


def test_every_stock_reply_renders_in_the_user_language() -> None:
    buddy = a_buddy(name="Nori")
    chinese = Localizer("zh-Hans")
    translated = set()
    for line in STOCK_REPLIES:
        reference = line.bind(name=buddy.name)
        chinese_line = chinese.render(reference)
        assert chinese_line != format_message(reference)
        assert buddy.name in chinese_line
        translated.add(chinese_line)

    # Five distinct translations, and the random pick lands on one of them.
    assert len(translated) == len(STOCK_REPLIES)
    assert chinese.render(stock_reply(buddy)) in translated


def _registry_with(*profiles: ModelProfile) -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry
