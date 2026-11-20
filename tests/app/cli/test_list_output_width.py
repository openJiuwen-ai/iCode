# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The listing tables must print something at any terminal width.

``COLUMNS`` reaches the ``rich`` console of every listing command, and a
degenerate value (0 or 1) makes the table render zero columns: the command
still exits 0, but neither stream says anything at all. A user cannot tell
that apart from "there is nothing to list".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from chrys.app.cli.profiles import _AgentRow, _ModelRow, _print_agents, _print_models
from chrys.app.cli.workflow import _print_workflows, _WorkflowRow

MODEL = _ModelRow(
    profile=SimpleNamespace(
        id="model-one",
        name="Model One",
        provider="anthropic",
        api_style="messages",
        model_id="claude-sonnet",
        max_context_tokens=200_000,
        stream=True,
        vision=False,
    ),
    is_active=True,
    flags="stream",
)

AGENT = _AgentRow(
    profile=SimpleNamespace(
        name="Code",
        display_name="Code Agent",
        id="b011c0de0001",
        description="General-purpose coding assistant",
    ),
    is_default=True,
    source="builtin",
    model="model-one",
)

WORKFLOW = _WorkflowRow(
    workflow_id="demo-workflow",
    source_kind="builtin",
    layout="file",
    path="demo-workflow.py",
    title="Demo Workflow",
)

LISTINGS = {
    "models": (_print_models, [MODEL]),
    "agents": (_print_agents, [AGENT]),
    "workflows": (_print_workflows, [WORKFLOW]),
}

DEGENERATE_WIDTHS = ["0", "1"]


@pytest.mark.parametrize("listing", list(LISTINGS))
@pytest.mark.parametrize("columns", DEGENERATE_WIDTHS)
def test_a_listing_never_goes_silent_at_a_degenerate_width(
    listing: str, columns: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A width of 0 or 1 must degrade the layout, not delete the output."""
    printer, rows = LISTINGS[listing]
    monkeypatch.setenv("COLUMNS", columns)

    printer(rows)

    captured = capsys.readouterr()
    assert captured.out.strip() or captured.err.strip(), (
        f"{listing} printed nothing on either stream at COLUMNS={columns}: "
        "the table collapsed instead of degrading"
    )


@pytest.mark.parametrize("listing", list(LISTINGS))
def test_a_listing_still_prints_at_a_normal_width(
    listing: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control case: the same call is unchanged at an ordinary width."""
    printer, rows = LISTINGS[listing]
    monkeypatch.setenv("COLUMNS", "80")

    printer(rows)

    assert capsys.readouterr().out.strip(), f"{listing} printed nothing at COLUMNS=80"
