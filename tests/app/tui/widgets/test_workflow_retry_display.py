# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workflow node's retry notice shows what the failure means, with its hint, as the chat does."""

from __future__ import annotations

from textual.widgets import Static

from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptJournal,
    AgentTranscriptSurface,
    TranscriptRetryOp,
)
from chrys.app.tui.widgets.chat.messages import RetryMessage
from chrys.app.tui.widgets.workflow.projector import transcript_operation
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.errors.display import _DNS_FAILED, _MAYBE_OFFLINE
from chrys.foundation.events.types import InvocationOrigin, InvocationRetryAttempt
from chrys.foundation.i18n import Localizer
from tests.support.tui_helpers import LocalizedWidgetApp
from tests.support.waiting import wait_for

_NODE = InvocationOrigin("workflow_node", "", "review", None)
_CHILD = InvocationOrigin("sub_agent", "Explore", "inv-1", _NODE)
_DNS = _DNS_FAILED.bind(host="api.example.com")
_HINT = _MAYBE_OFFLINE.bind(app=APP_DISPLAY_NAME)


def _retry(**fields: object) -> InvocationRetryAttempt:
    return InvocationRetryAttempt(
        origin=_CHILD, message="Connection error.", attempt=1, max_attempts=7, delay_seconds=3, **fields
    )


async def test_a_retry_notice_shows_the_display_and_its_hint() -> None:
    operation = transcript_operation(_retry(display_message=_DNS, display_hint=_HINT))
    assert isinstance(operation, TranscriptRetryOp)
    journal = AgentTranscriptJournal()
    surface = AgentTranscriptSurface(journal)
    async with LocalizedWidgetApp(lambda: surface).run_test() as pilot:
        journal.record(operation)
        await wait_for(lambda: bool(surface.query(RetryMessage)), pilot=pilot)

        text = surface.query_one(RetryMessage).query_one(".status-body", Static).render().plain
        localizer = Localizer("en")
        assert f"{localizer.render(_DNS)} {localizer.render(_HINT)}" in text
        assert "Connection error." not in text


def test_a_hint_never_shows_without_its_display() -> None:
    operation = transcript_operation(_retry(display_hint=_HINT))

    assert isinstance(operation, TranscriptRetryOp)
    assert (operation.message, operation.hint) == ("Connection error.", None)
