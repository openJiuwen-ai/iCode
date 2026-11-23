# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The listing tables must print every field at any terminal width.

``COLUMNS`` reaches the ``rich`` console of every listing command, and a
degenerate value (0 or 1) makes the table render zero columns: the command
still exits 0, but neither stream says anything at all. A user cannot tell
that apart from "there is nothing to list". Even when a table does render,
too little width can drop whole columns, so these tests check the fields
rather than merely checking that a stream is non-empty.
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

DEGENERATE_WIDTHS = ["0", "1", "5", "10"]

EXPECTED_FIELDS = {
    "models": ("model-one", "Model One", "anthropic", "messages", "claude-sonnet", "200k", "stream"),
    "agents": ("Code", "Code Agent", "b011c0de0001", "model-one", "builtin", "General-purpose coding assistant"),
    # ``layout`` is carried in the row but is not a column of the workflow table.
    "workflows": ("demo-workflow", "builtin", "demo-workflow.py", "Demo Workflow"),
}

# At COLUMNS=80 long values wrap; these fields are known to stay contiguous there.
CONTROL_FIELDS = {
    "models": ("model-one", "anthropic"),
    "agents": ("Code Agent", "b011c0de0001"),
    "workflows": ("Demo Workflow", "demo-workflow.py"),
}

EMPTY_MESSAGES = {
    "models": "No model profiles found.",
    "agents": "No agent profiles found.",
    "workflows": "No workflows found.",
}


@pytest.mark.parametrize("listing", list(LISTINGS))
@pytest.mark.parametrize("columns", DEGENERATE_WIDTHS)
def test_a_listing_never_goes_silent_at_a_degenerate_width(
    listing: str, columns: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A degenerate width must degrade the layout, not delete output or columns."""
    printer, rows = LISTINGS[listing]
    monkeypatch.setenv("COLUMNS", columns)

    printer(rows)

    captured = capsys.readouterr()
    assert captured.err == "", f"{listing} wrote to stderr at COLUMNS={columns}: {captured.err!r}"
    assert captured.out.strip(), f"{listing} printed nothing at COLUMNS={columns}: the table collapsed"
    for value in EXPECTED_FIELDS[listing]:
        assert value in captured.out, f"{listing} did not print {value!r} at COLUMNS={columns}:\n{captured.out}"


@pytest.mark.parametrize("listing", list(LISTINGS))
def test_a_listing_still_prints_at_a_normal_width(
    listing: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control case: the same call is unchanged at an ordinary width."""
    printer, rows = LISTINGS[listing]
    monkeypatch.setenv("COLUMNS", "80")

    printer(rows)

    captured = capsys.readouterr()
    assert captured.err == "", f"{listing} wrote to stderr at COLUMNS=80: {captured.err!r}"
    assert captured.out.strip(), f"{listing} printed nothing at COLUMNS=80"
    for value in CONTROL_FIELDS[listing]:
        assert value in captured.out, f"{listing} did not print {value!r} at COLUMNS=80:\n{captured.out}"


@pytest.mark.parametrize("listing", list(LISTINGS))
def test_an_empty_listing_still_prints_to_stdout_at_a_degenerate_width(
    listing: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty listing reports its emptiness on stdout, never silently."""
    printer, _ = LISTINGS[listing]
    monkeypatch.setenv("COLUMNS", "0")

    printer([])

    captured = capsys.readouterr()
    assert captured.err == "", f"{listing} wrote to stderr at COLUMNS=0: {captured.err!r}"
    assert EMPTY_MESSAGES[listing] in captured.out
