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
from rich.cells import cell_len
from rich.console import Console

from chrys.app.cli.display import stdout_console
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


class _WidthReadProbe:
    """A ``Console.width`` property that reports 80 first and 1 on a second read.

    Once the width has been assigned, reads fall back to Rich's real getter so
    the test can verify the stored value without polluting the read count.
    """

    def __init__(self, original: property) -> None:
        """Wrap *original*, remembering its accessors and counting no reads yet."""
        original_get = original.fget
        original_set = original.fset
        assert original_get is not None and original_set is not None
        self._get = original_get
        self._set = original_set
        self.reads: int = 0
        self._assigned: bool = False

    def get(self, console: Console) -> int:
        """Return 80 on the first read, 1 on a later read, or the stored width."""
        self.reads += 1
        if self._assigned:
            return int(self._get(console))
        return 80 if self.reads == 1 else 1

    def set(self, console: Console, width: int) -> None:
        """Store *width* through Rich's setter and switch reads to the real getter."""
        self._assigned = True
        self._set(console, width)


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


def test_a_normal_narrow_terminal_keeps_its_native_layout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """COLUMNS=78 must render at the terminal width, not the fallback width."""
    printer, rows = LISTINGS["agents"]
    monkeypatch.setenv("COLUMNS", "78")

    printer(rows)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.strip()
    for value in CONTROL_FIELDS["agents"]:
        assert value in captured.out, f"agent field {value!r} missing at COLUMNS=78:\n{captured.out}"

    line_widths = [cell_len(line) for line in captured.out.splitlines()]
    assert line_widths, "the agent table produced no lines"
    assert max(line_widths) <= 78, f"a line overflows COLUMNS=78: {line_widths}"
    # The fallback width would pad every line to 120; a native table stays
    # within the terminal budget (77 on legacy Windows, 78 elsewhere).
    assert any(61 <= width <= 78 for width in line_widths), f"not rendered at native width: {line_widths}"


def test_stdout_console_reads_width_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The width used for the floor check is the width set on the console."""
    probe = _WidthReadProbe(Console.width)
    monkeypatch.setattr(Console, "width", property(probe.get, probe.set))

    console = stdout_console()
    reads_during_call = probe.reads

    assert console.width == 80, "the console kept a width other than the one checked"
    assert reads_during_call == 1, "Console.width was read more than once"
