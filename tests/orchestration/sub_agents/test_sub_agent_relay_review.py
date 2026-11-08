# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A sub-agent's relay review merges the parent build's flag with the child's own model."""

from __future__ import annotations

import pytest

from chrys.orchestration.sub_agents.tools import _sub_agent_relay_review
from chrys.service.approval.judge import ApprovalJudge
from chrys.service.profiles.models.schema import ModelProfile


@pytest.fixture
def fast_sub_agent_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make :class:`KernelSubAgentPolicy` use minimal retries + no backoff."""
    from tests.orchestration.sub_agents._pipeline import use_fast_sub_agent_policy

    use_fast_sub_agent_policy(monkeypatch)


def _profile(**overrides: object) -> ModelProfile:
    fields: dict[str, object] = {
        "id": "p",
        "name": "P",
        "model_id": "direct-model",
        "base_url": "https://direct.example.com/v1",
        "api_key": "sk-direct",
    }
    fields.update(overrides)
    return ModelProfile(**fields)  # type: ignore[arg-type]


def _relay_child() -> ModelProfile:
    return _profile(
        id="child",
        name="Child",
        model_id="relay-model",
        base_url="https://relay.example.com/v1",
        api_key="sk-relay",
        is_relay=True,
    )


def _judge(profile: ModelProfile) -> ApprovalJudge:
    return ApprovalJudge(profile)


def test_direct_parent_and_direct_child_inherit_the_parent_inputs() -> None:
    judge = _judge(_profile(id="judge", name="Judge"))

    assert _sub_agent_relay_review(False, "", _profile(id="child", name="Child"), judge) == (False, "", judge)


def test_direct_parent_and_none_profile_inherit_the_parent_inputs() -> None:
    judge = _judge(_profile(id="judge", name="Judge"))

    assert _sub_agent_relay_review(True, "parent-model", None, judge) == (True, "parent-model", judge)


def test_relay_parent_and_direct_child_keep_the_parent_inputs() -> None:
    judge = _judge(_profile(id="judge", name="Judge"))

    result = _sub_agent_relay_review(True, "parent-model", _profile(id="child", name="Child"), judge)

    assert result == (True, "parent-model", judge)


def test_direct_parent_with_relay_child_enhances_and_names_the_child_model() -> None:
    judge = _judge(_profile(id="judge", name="Judge"))

    enhanced, reviewed_model, judge_out = _sub_agent_relay_review(False, "", _relay_child(), judge)

    assert enhanced is True
    assert reviewed_model == "relay-model"
    assert judge_out is judge


def test_direct_parent_with_relay_child_and_no_judge_stays_judgeless() -> None:
    enhanced, reviewed_model, judge_out = _sub_agent_relay_review(False, "", _relay_child(), None)

    assert enhanced is True
    assert reviewed_model == "relay-model"
    assert judge_out is None


def test_relay_child_drops_a_judge_sharing_the_child_relay_host() -> None:
    judge = _judge(
        _profile(
            id="judge",
            name="Judge",
            model_id="judge-model",
            base_url="https://relay.example.com/other",
            api_key="sk-judge",
        )
    )

    enhanced, _reviewed_model, judge_out = _sub_agent_relay_review(False, "", _relay_child(), judge)

    assert enhanced is True
    assert judge_out is None


def test_relay_child_drops_a_judge_that_is_itself_a_relay() -> None:
    judge = _judge(
        _profile(
            id="judge",
            name="Judge",
            model_id="judge-model",
            base_url="https://other-relay.example.com/v1",
            api_key="sk-judge",
            is_relay=True,
        )
    )

    _enhanced, _reviewed_model, judge_out = _sub_agent_relay_review(False, "", _relay_child(), judge)

    assert judge_out is None


def test_relay_parent_and_relay_child_keep_an_independent_judge() -> None:
    # Parent relay qualified the judge at build time; the judge is independent
    # of the child's own relay too, so the child keeps auto-approval review.
    judge = _judge(_profile(id="judge", name="Judge", model_id="judge-model"))

    enhanced, reviewed_model, judge_out = _sub_agent_relay_review(True, "parent-model", _relay_child(), judge)

    assert enhanced is True
    assert reviewed_model == "relay-model"
    assert judge_out is judge


async def test_relay_sub_agent_latches_the_parent_session(tmp_path, fast_sub_agent_controller, agent_engine) -> None:
    """A relay-routed child latches relay risk into the parent session (engine-driven)."""
    from chrys.foundation.events.types import UserMessage
    from chrys.service.llm.mock import MockResponse
    from tests.orchestration.sub_agents._pipeline import _make_ctx, _sub_tool_call

    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("explore something"),
            MockResponse(text="Final summary after sub-agent."),
        ],
        sub_outcomes=[MockResponse(text="sub-agent result")],
        sub_is_relay=True,
        agent_engine=agent_engine,
    )
    try:
        assert ctx.engine.session.runtime_meta.relay_risk_seen is False

        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        # The parent itself runs direct; the relay child's run latched the
        # session fact that survives saves and later rebuilds.
        assert ctx.engine.session.runtime_meta.relay_risk_seen is True
    finally:
        await ctx.cleanup()
