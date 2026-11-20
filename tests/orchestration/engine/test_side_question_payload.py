# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A side question's ``user_prompt_submit`` carries the fields every submission's payload does.

A hook that reads ``usage`` or ``system_prompt`` per the hooks reference must not
have to ask which kind of input it was handed, and must not crash when a user
opens a side question with ``/btw``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.engine import side_questions
from chrys.service.hooks.schema import HookDecision
from chrys.service.llm.side_question import side_question_instructions
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata

_SIDE_QUESTION_KEYS = (
    "session_id",
    "profile",
    "cwd",
    "text",
    "injected",
    "side_question",
    "usage",
    "system_prompt",
)
"""What the hooks reference lists for ``user_prompt_submit``, both main-turn and side-question."""


class _CapturingManager:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def has_hooks_for(self, _event: object) -> bool:
        return True


async def _run_side_question(monkeypatch: pytest.MonkeyPatch, runtime_meta: Any) -> dict[str, Any]:
    """Drive one side question far enough to capture the payload its hooks are shown."""
    captured: dict[str, Any] = {}
    session = SimpleNamespace(
        hook_manager=_CapturingManager(),
        runtime_meta=runtime_meta,
        session_id="s1",
        shutting_down=False,
    )

    async def fake_check_operation(
        _manager: object,
        _event: object,
        _operation: object,
        payload: Any,
        *,
        scope: str = "turn",
        target_operation_id: str | None = None,
        trajectory_context: Any = None,
        origin: Any = None,
    ) -> HookDecision:
        captured.update(payload())
        return HookDecision(blocked=True, block_reason="stop here")

    monkeypatch.setattr(side_questions, "check_operation", fake_check_operation)
    side = side_questions.SideQuestions(
        bus=SimpleNamespace(),
        session=session,
        current=SimpleNamespace(),
        permits=SimpleNamespace(),
        history=SimpleNamespace(),
        recorder=SimpleNamespace(),
        state_store=None,
        lease=SimpleNamespace(),
    )

    async def fake_publish(_event: object) -> None:
        return None

    origin = InvocationOrigin(kind="side_question", session_id="s1", invocation_id="q1", parent=None)
    active = SimpleNamespace(
        emitter=SimpleNamespace(origin=origin, publish=fake_publish),
        seed=SimpleNamespace(agent_profile="Code", primary_cwd="/work"),
        operation=SimpleNamespace(invalidate=lambda _reason: None),
        cancelled=False,
        writes_cancelled=threading.Event(),
    )
    side._active = active  # the question has to be the live one for the run to start
    await side._run(
        active,
        profile=SimpleNamespace(),
        question="what does this do?",
        context="",
        session_dir=None,
        trajectory=None,
    )
    return captured


async def test_a_side_question_payload_carries_usage_and_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = SessionRuntimeMetadata()
    meta.record_usage(150, input_tokens=100, output_tokens=50)
    payload = await _run_side_question(monkeypatch, meta)

    for key in _SIDE_QUESTION_KEYS:
        assert key in payload, f"user_prompt_submit payload is missing {key!r}"
    assert payload["side_question"] is True
    assert payload["injected"] is False
    assert payload["usage"]["input_tokens"] == 100
    assert payload["usage"]["total_tokens"] == 150


async def test_a_side_question_reports_the_prompt_its_own_call_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not the main agent's instructions: this call answers with no tools, from a prompt of its own."""
    payload = await _run_side_question(monkeypatch, SessionRuntimeMetadata())

    assert payload["system_prompt"] == side_question_instructions()
    assert "no tools" in payload["system_prompt"]


def test_the_side_question_prompt_is_what_the_call_is_built_with() -> None:
    """The report and the call cannot drift apart: both come from one function."""
    assert side_question_instructions() == side_question_instructions(())
    with_reminders = side_question_instructions(["a note"])
    assert with_reminders.startswith(side_question_instructions())
    assert "<system-reminder>\na note\n</system-reminder>" in with_reminders
