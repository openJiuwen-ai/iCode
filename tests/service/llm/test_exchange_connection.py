# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Every client names the model profile it sends through on both exchange markers.

The main agent's exchanges always did, from the turn's own facts; a
sub-agent's, a workflow node's and a side call's did not, so their usage could
not be told apart by profile. The client knows its profile whoever asks.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from chrys.foundation.trajectory.context import side_call_scope, trajectory_scope
from chrys.foundation.trajectory.envelope import ActorRole
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream, internal_side_call_scope
from chrys.service.llm.clients import scoped_client
from chrys.service.llm.observer import WireCallObserver, exchange_connection_facts
from chrys.service.llm.wire_client import WireClient
from chrys.service.profiles.models.schema import ModelProfile
from tests.service.trajectory._fakes import FakeSink, make_context

_PROFILE_ID = "0123456789abcdef0123456789abcdef"
# What the user writes into a profile, none of which may reach the log.
_NAME = "SENTINEL-My Second Subscription"
_HOST = "sentinel-relay.example"
_API_KEY = "sk-SENTINEL-never-logged"


def _profile(provider: str = "openai", api_style: str = "chat_completions", **overrides: Any) -> ModelProfile:
    values: dict[str, Any] = {
        "id": _PROFILE_ID,
        "name": _NAME,
        "provider": provider,
        "api_style": api_style,
        "model_id": "glm-4.6",
        "api_key": _API_KEY,
        "base_url": f"https://{_HOST}/v1",
    }
    return ModelProfile(**{**values, **overrides})


def _assert_nothing_the_user_wrote(sink: FakeSink) -> None:
    recorded = json.dumps([draft.payload for draft in sink.drafts])
    for secret in (_NAME, _HOST, _API_KEY):
        assert secret not in recorded


class _StreamWireClient(WireClient):
    """A wire client whose one stream ends in *fail_with*, or normally."""

    def __init__(self, observer: WireCallObserver, fail_with: type[BaseException] | None = None) -> None:
        super().__init__(observer=observer)
        self.fail_with = fail_with

    def _send(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("this double only streams")

    def _open_stream(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        del messages, options, kwargs
        fail_with = self.fail_with

        async def _updates() -> Any:
            yield ChatResponseUpdate(contents=[Content.from_text("partial")], role="assistant")
            if fail_with is not None:
                raise fail_with()

        return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)


def test_the_facts_name_the_profile_by_its_id_and_dialect_alone() -> None:
    facts = exchange_connection_facts(_profile(provider="deepseek-openai", api_style="responses"))

    assert facts == {"model_profile_id": _PROFILE_ID, "provider": "deepseek-openai", "api_style": "responses"}


@pytest.mark.parametrize("profile_id", ["", "x" * 10_000, "bad\nid"], ids=["empty", "too-long", "unprintable"])
def test_an_id_the_log_would_refuse_is_left_out(profile_id: str) -> None:
    """A refused id would cost the whole exchange marker, and with it the usage it carries."""
    assert exchange_connection_facts(_profile(id=profile_id)) == {"provider": "openai", "api_style": "chat_completions"}


@pytest.mark.parametrize(
    ("provider", "api_style"),
    [
        ("openai", "chat_completions"),
        ("openai", "responses"),
        ("anthropic", "chat_completions"),
        ("deepseek-openai", "chat_completions"),
        ("deepseek-openai", "responses"),
        ("glm-openai", "chat_completions"),
    ],
)
async def test_every_wire_client_reports_the_profile_it_was_built_from(provider: str, api_style: str) -> None:
    async with scoped_client(_profile(provider=provider, api_style=api_style)) as client:
        observer = client.inner.inner._observer

    assert observer._connection == {"model_profile_id": _PROFILE_ID, "provider": provider, "api_style": api_style}


@pytest.mark.parametrize("fail_with", [None, RuntimeError], ids=["finished", "failed"])
async def test_a_side_calls_exchange_names_its_profile_on_both_markers(fail_with: type[BaseException] | None) -> None:
    """The side call's scope drops the turn's facts; its client puts its own profile back."""
    sink = FakeSink()
    client = _StreamWireClient(WireCallObserver(connection=exchange_connection_facts(_profile())), fail_with)
    # The turn's own facts name the main agent's profile; the side call runs on another.
    context = make_context(sink).with_exchange_facts({"model_profile_id": "f" * 32, "provider": "anthropic"})

    with trajectory_scope(context), internal_side_call_scope(), side_call_scope(ActorRole.TITLE_GEN):
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        if fail_with is None:
            await stream.get_final_response()
        else:
            with pytest.raises(fail_with):
                await stream.get_final_response()

    started = sink.only(EventType.MODEL_EXCHANGE_STARTED)
    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == (ExchangeOutcome.SUCCESS if fail_with is None else ExchangeOutcome.ERROR)
    for marker in (started, finished):
        assert marker.payload["model_profile_id"] == _PROFILE_ID
        assert marker.payload["provider"] == "openai"
        assert marker.payload["api_style"] == "chat_completions"
    _assert_nothing_the_user_wrote(sink)


async def test_a_mock_profiles_client_reports_its_profile_as_a_real_one_does() -> None:
    sink = FakeSink()
    profile = _profile(provider="mock")

    async with scoped_client(profile) as client:
        with trajectory_scope(make_context(sink)), side_call_scope(ActorRole.TITLE_GEN):
            await client.get_response([Message("user", ["hi"])])

    for marker in (sink.only(EventType.MODEL_EXCHANGE_STARTED), sink.only(EventType.MODEL_EXCHANGE_FINISHED)):
        assert marker.payload["model_profile_id"] == _PROFILE_ID
        assert marker.payload["provider"] == "mock"
    _assert_nothing_the_user_wrote(sink)
