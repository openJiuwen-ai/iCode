# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session mutation-detail and recorded-summary fallback tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.foundation.trajectory.envelope import measurement
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.service.analytics import ChangeVerificationState, Metric, Precision, analyze_trajectory
from tests.service.analytics._events import EventLog

_NS = 1_000_000_000
_TURN_ONE = "4" * 32
_TURN_TWO = "5" * 32
_VERIFY_CALL = "7" * 32


def test_change_verification_joins_active_32_hex_turns_to_integer_mutation_turns_and_uses_skip_aware_net_zero(
    tmp_path: Path,
) -> None:
    path = _installed_log(tmp_path)
    _write_two_turn_actions(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    },
                                    {
                                        "path": "skipped.bin",
                                        "before_skip": "binary",
                                        "after_skip": "binary",
                                        "provenance": "proven",
                                    },
                                    {
                                        "path": "foreign.py",
                                        "before_hash": "1" * 64,
                                        "after_hash": "2" * 64,
                                        "provenance": "foreign",
                                    },
                                ],
                            }
                        ]
                    },
                    "messages": [_verify_command_message()],
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is True
    assert change.files_touched.value == 2
    assert change.net_zero.value == 1
    assert {row.path for row in change.rows} == {"changed.py", "skipped.bin"}
    states = {row.path: row.state for row in change.rows}
    assert states["changed.py"] is ChangeVerificationState.VERIFIED
    assert states["skipped.bin"] is ChangeVerificationState.NET_ZERO
    assert all(row.evidence for row in change.rows)
    precisions = {row.path: row.precision for row in change.rows}
    # The verify word-list match is estimated evidence, so verified rows
    # inherit it; the fully-skipped modify never captured either side's
    # bytes, so its return to the original state is unprovable.
    assert precisions["changed.py"] is Precision.ESTIMATED
    assert precisions["skipped.bin"] is Precision.UNRESOLVED
    assert change.files_touched.precision is Precision.EXACT
    assert change.net_zero.precision is Precision.UNRESOLVED
    assert "unprovable" in (change.net_zero.reason or "")


def test_rename_to_a_new_path_folds_the_source_from_its_own_snapshot(tmp_path: Path) -> None:
    """A move's before_hash describes the destination, so the source must
    fold from its own turn snapshot — not read as never having existed."""
    path = _installed_log(tmp_path)
    _write_two_turn_actions(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "renamed.py",
                                        "old_path": "original.py",
                                        "after_hash": "c0" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ],
                        "snapshots": {
                            "original.py::1": {
                                "path": "original.py",
                                "turn_id": 1,
                                "existed": True,
                                "content_hash": "c0" * 32,
                                "size": 7,
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert {row.path for row in change.rows} == {"renamed.py", "original.py"}
    assert change.created.value == 1
    assert change.deleted.value == 1
    assert change.net_zero.value == 0


def test_same_turn_shell_mutation_is_not_marked_verified_without_an_orderable_edit(tmp_path: Path) -> None:
    """A same-turn verify can prove its order only against edit-classified
    actions; a mutation from a shell action has no such order, so the row
    must degrade instead of claiming verified."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "9" * 32,
        _NS,
        2 * _NS,
        turn_id=_TURN_ONE,
        start_payload={
            "tool_name": "Bash",
            "tool_kind": "shell",
            "call_item_id": _VERIFY_CALL,
            "argument_fingerprint": "verify-fingerprint",
        },
    )
    log.add("turn.finished", 3 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ]
                    },
                    "messages": [_verify_command_message()],
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.UNVERIFIED
    assert row.precision is Precision.UNRESOLVED


def test_same_turn_verify_overlapping_the_edit_does_not_vouch_for_it(tmp_path: Path) -> None:
    """Batched tool calls run concurrently: a verify that starts after the
    edit starts but before its terminal lands may have read the pre-edit
    state, so the row must not claim verified."""
    path = _installed_log(tmp_path)
    _write_same_turn_edit_and_verify(path, overlap=True)

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.AFTER_VERIFY
    # The state still leans on the word-list identification of the verify.
    assert row.precision is Precision.ESTIMATED


def test_same_turn_verify_starting_after_the_edit_terminal_is_verified(tmp_path: Path) -> None:
    path = _installed_log(tmp_path)
    _write_same_turn_edit_and_verify(path, overlap=False)

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.VERIFIED


@pytest.mark.parametrize(
    ("command", "mutator_call", "fingerprint"),
    [
        pytest.param("python mutate.py", "a" * 32, "mutator-fingerprint", id="shell-mutator"),
        pytest.param("ruff check --fix", "e" * 32, "fixer-fingerprint", id="verify-classified-fixer"),
    ],
)
def test_same_turn_verify_overlapping_another_mutator_does_not_vouch_for_the_edit(
    tmp_path: Path, command: str, mutator_call: str, fingerprint: str
) -> None:
    """Even a verify-classified fixer can rewrite the file after the check finishes."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "8" * 32,
        _NS,
        2 * _NS,
        turn_id=_TURN_ONE,
        start_payload={
            "tool_name": "write_file",
            "tool_kind": "filesystem.write",
            "call_item_id": "6" * 32,
            "argument_fingerprint": "edit-fingerprint",
        },
    )
    log.add(
        "tool.operation.started",
        3 * _NS,
        turn_id=_TURN_ONE,
        operation_id="b" * 32,
        payload={
            "tool_name": "Bash",
            "tool_kind": "shell",
            "call_item_id": mutator_call,
            "argument_fingerprint": fingerprint,
        },
    )
    log.span(
        "tool.operation",
        "9" * 32,
        4 * _NS,
        5 * _NS,
        turn_id=_TURN_ONE,
        start_payload={
            "tool_name": "Bash",
            "tool_kind": "shell",
            "call_item_id": _VERIFY_CALL,
            "argument_fingerprint": "verify-fingerprint",
        },
    )
    log.add(
        "tool.operation.finished",
        6 * _NS,
        turn_id=_TURN_ONE,
        operation_id="b" * 32,
        payload={"outcome": "success", "duration_ms": 3_000},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add("turn.finished", 7 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ]
                    },
                    "messages": [
                        _command_message(_VERIFY_CALL, "pytest -q"),
                        _command_message(mutator_call, command),
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.AFTER_VERIFY


def test_estimated_other_shell_action_degrades_the_unverified_row(tmp_path: Path) -> None:
    """A shell command the word list did not recognize may really have been
    verification work, so an unverified row built on that absence cannot
    claim exactness — matching the aggregate unverified count."""
    path = _installed_log(tmp_path)
    _write_two_turn_actions(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ]
                    },
                    "messages": [_command_message(_VERIFY_CALL, "python mutate.py")],
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.UNVERIFIED
    assert row.precision is Precision.ESTIMATED


def test_missing_command_carrier_leaves_the_unverified_claim_unresolved(tmp_path: Path) -> None:
    """A shell action whose command carrier is lost could have been the
    verify, so an unverified row cannot claim an exact absence."""
    path = _installed_log(tmp_path)
    _write_two_turn_actions(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.UNVERIFIED
    assert row.precision is Precision.UNRESOLVED


def test_missing_command_carrier_after_the_verify_leaves_the_after_verify_claim_unresolved(tmp_path: Path) -> None:
    """A later shell action whose command carrier is lost could have been a
    verify that would upgrade the row, so "changed after the latest verify"
    cannot claim more than the shell classification proves."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "9" * 32,
        _NS,
        2 * _NS,
        turn_id=_TURN_ONE,
        start_payload={
            "tool_name": "Bash",
            "tool_kind": "shell",
            "call_item_id": _VERIFY_CALL,
            "argument_fingerprint": "verify-fingerprint",
        },
    )
    log.add("turn.finished", 3 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.add("turn.started", 4 * _NS, turn_id=_TURN_TWO, payload={"turn_number": 2})
    log.span(
        "tool.operation",
        "8" * 32,
        5 * _NS,
        6 * _NS,
        turn_id=_TURN_TWO,
        start_payload={
            "tool_name": "write_file",
            "tool_kind": "filesystem.write",
            "call_item_id": "6" * 32,
            "argument_fingerprint": "edit-fingerprint",
        },
    )
    log.span(
        "tool.operation",
        "3" * 32,
        7 * _NS,
        8 * _NS,
        turn_id=_TURN_TWO,
        start_payload={
            "tool_name": "Bash",
            "tool_kind": "shell",
            "call_item_id": "2" * 32,
            "argument_fingerprint": "mystery-fingerprint",
        },
    )
    log.add("turn.finished", 9 * _NS, turn_id=_TURN_TWO, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 2,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ]
                    },
                    "messages": [_command_message(_VERIFY_CALL, "pytest")],
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    (row,) = change.rows
    assert row.state is ChangeVerificationState.AFTER_VERIFY
    assert row.precision is Precision.UNRESOLVED


def test_active_turn_without_a_number_distrusts_the_detailed_join(tmp_path: Path) -> None:
    """The detailed mutation state joins on turn numbers, so an active turn
    whose number is unreadable silently drops that turn's rows from the join —
    the recorded summaries must win over an exact under-count."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        turn_id=_TURN_ONE,
        payload={
            "files_touched": 1,
            "create": 0,
            "modify": 1,
            "delete": 0,
            "net_zero_count": 0,
            "source_ref": {"kind": "session_checkpoint", "id": "c" * 32, "hash": "h" * 8},
        },
    )
    log.add("turn.finished", 2 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.add("turn.started", 3 * _NS, turn_id=_TURN_TWO, payload={"turn_number": "x"})
    log.add("turn.finished", 4 * _NS, turn_id=_TURN_TWO, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "a.py",
                                        "before_hash": "1" * 64,
                                        "after_hash": "2" * 64,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ],
                        "snapshots": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is False
    assert (change.files_touched.value, change.files_touched.precision) == (1, Precision.ESTIMATED)
    assert "cannot join" in (change.files_touched.reason or "")
    assert change.rows == ()


def test_window_inferred_and_contested_mutations_cannot_pass_for_exact(tmp_path: Path) -> None:
    """An assumed row folds a window diff that may be a concurrent third
    party's write, and a contested row conflicts with a peer session, so
    neither the rows nor the counts built from them may claim exactness.

    The log holds only the edit: any shell action would degrade every row
    through the verify-absence precision and mask the provenance split."""
    path = _installed_log(tmp_path)
    _write_edit_only_action(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "guessed.py",
                                        "before_hash": "a" * 64,
                                        "after_hash": "b" * 64,
                                        "provenance": "assumed",
                                    },
                                    {
                                        "path": "shared.py",
                                        "before_hash": "c" * 64,
                                        "after_hash": "d" * 64,
                                        "provenance": "proven",
                                        "contested": True,
                                    },
                                    {
                                        "path": "solid.py",
                                        "before_hash": "e" * 64,
                                        "after_hash": "f" * 64,
                                        "provenance": "proven",
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    precisions = {row.path: row.precision for row in change.rows}
    assert precisions["guessed.py"] is Precision.ESTIMATED
    assert precisions["shared.py"] is Precision.ESTIMATED
    assert precisions["solid.py"] is Precision.EXACT
    assert change.files_touched == Metric(
        3, Precision.ESTIMATED, "counts include window-inferred or peer-contested mutations"
    )


def test_change_verification_marks_recorded_observed_detail_unresolved_when_detection_was_truncated(
    tmp_path: Path,
) -> None:
    path = _installed_log(tmp_path)
    _write_two_turn_actions(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": True,
                                "mutations": [{"path": "partial.py", "before_hash": "a" * 64, "after_hash": "b" * 64}],
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detection_truncated is True
    assert change.files_touched.precision is Precision.UNRESOLVED
    assert "recorded/observed" in (change.files_touched.reason or "")


def test_unreachable_session_json_falls_back_to_recorded_mutation_summary_without_guessing_paths(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        payload={"files_touched": 5, "create": 1, "modify": 3, "delete": 1, "net_zero_count": 2},
    )
    log.add("turn.finished", 2 * _NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is False
    assert change.files_touched.value == 5
    assert change.created.value == 1
    assert change.modified.value == 3
    assert change.deleted.value == 1
    assert change.net_zero.value == 2
    assert change.rows == ()
    assert "file detail is unavailable" in (change.files_touched.reason or "")


def test_negative_recorded_mutation_summary_is_missing_instead_of_exact(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        payload={"files_touched": -1, "create": 0, "modify": 0, "delete": 0, "net_zero_count": 0},
    )
    log.add("turn.finished", 2 * _NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert (change.files_touched.value, change.files_touched.precision) == (None, Precision.MISSING)


def test_summed_recorded_summaries_are_estimated_because_cross_turn_identity_is_unavailable(
    tmp_path: Path,
) -> None:
    """One file created in turn 1 and modified in turn 2 is one created file
    session-wide, but the identity-free summaries count it once per turn —
    the sum is real per-turn arithmetic and must not claim exactness."""
    path = tmp_path / "events.jsonl"
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        turn_id=_TURN_ONE,
        payload={"files_touched": 1, "create": 1, "modify": 0, "delete": 0, "net_zero_count": 0},
    )
    log.add("turn.finished", 2 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.add("turn.started", 3 * _NS, turn_id=_TURN_TWO, payload={"turn_number": 2})
    log.add(
        "tool.mutation_batch.summary",
        4 * _NS,
        turn_id=_TURN_TWO,
        payload={"files_touched": 1, "create": 0, "modify": 1, "delete": 0, "net_zero_count": 0},
    )
    log.add("turn.finished", 5 * _NS, turn_id=_TURN_TWO, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is False
    assert (change.files_touched.value, change.files_touched.precision) == (2, Precision.ESTIMATED)
    assert (change.created.value, change.created.precision) == (1, Precision.ESTIMATED)
    assert (change.modified.value, change.modified.precision) == (1, Precision.ESTIMATED)
    assert "fold" in (change.files_touched.reason or "")


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"messages": []}, id="mutation-detail-absent"),
        pytest.param({"chrys_mutations": "corrupt"}, id="mutation-detail-malformed"),
        pytest.param({"chrys_mutations": {"turns": "corrupt"}}, id="mutation-turns-malformed"),
        pytest.param({"chrys_mutations": {"turns": [42], "snapshots": {}}}, id="turn-entry-not-a-dict"),
        pytest.param(
            {"chrys_mutations": {"turns": [{"turn_id": "x", "mutations": []}], "snapshots": {}}},
            id="turn-id-not-an-integer",
        ),
        pytest.param({"chrys_mutations": {"turns": [{"turn_id": 1}], "snapshots": {}}}, id="mutations-key-missing"),
        pytest.param(
            {"chrys_mutations": {"turns": [{"turn_id": 1, "mutations": "corrupt"}], "snapshots": {}}},
            id="mutations-not-a-list",
        ),
        pytest.param(
            {"chrys_mutations": {"turns": [{"turn_id": 1, "mutations": [42]}], "snapshots": {}}},
            id="mutation-row-not-a-dict",
        ),
        pytest.param(
            {"chrys_mutations": {"turns": [{"turn_id": 1, "mutations": [{"path": 42}]}], "snapshots": {}}},
            id="mutation-path-not-a-string",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "a.py", "before_hash": 42, "after_hash": "af" * 32}]}
                    ],
                    "snapshots": {},
                }
            },
            id="mutation-before-hash-not-a-string",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "a.py", "after_hash": "2" * 64, "provenance": 42}]}
                    ],
                    "snapshots": {},
                }
            },
            id="mutation-provenance-not-a-string",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {
                            "turn_id": 1,
                            "detection_truncated": "true",
                            "mutations": [{"path": "a.py", "before_hash": "1" * 64, "after_hash": "2" * 64}],
                        }
                    ],
                    "snapshots": {},
                }
            },
            id="detection-truncated-not-a-bool",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {
                            "turn_id": 1,
                            "mutations": [
                                {"path": "a.py", "before_hash": "1" * 64, "after_hash": "2" * 64, "contested": "true"}
                            ],
                        }
                    ],
                    "snapshots": {},
                }
            },
            id="mutation-contested-not-a-bool",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {
                            "turn_id": 1,
                            "mutations": [
                                {"path": "a.py", "before_skip": "future_policy", "after_skip": "future_policy"}
                            ],
                        }
                    ],
                    "snapshots": {},
                }
            },
            id="mutation-skip-reason-unknown",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {
                        "a.py::1": {"path": "a.py", "turn_id": 1, "existed": True, "skip_reason": "future_policy"}
                    },
                }
            },
            id="rename-snapshot-skip-reason-unknown",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [{"turn_id": 1, "mutations": [{"path": "b.py", "old_path": 42, "after_hash": "2" * 64}]}],
                    "snapshots": {},
                }
            },
            id="mutation-old-path-not-a-string",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {},
                }
            },
            id="rename-source-snapshot-missing",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {"a.py::1": "corrupt"},
                }
            },
            id="rename-source-snapshot-not-a-dict",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {
                        "a.py::1": {"path": "a.py", "turn_id": 1, "existed": True, "content_hash": 42, "size": 7}
                    },
                }
            },
            id="rename-source-snapshot-hash-not-a-string",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {
                            "turn_id": 1,
                            "mutations": [
                                {"path": "a.py", "before_hash": "not-a-sha256", "after_hash": "not-a-sha256"}
                            ],
                        }
                    ],
                    "snapshots": {},
                }
            },
            id="mutation-hash-not-a-sha256",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {
                        "a.py::1": {
                            "path": "different.py",
                            "turn_id": 1,
                            "existed": True,
                            "content_hash": "1" * 64,
                            "size": 7,
                        }
                    },
                }
            },
            id="rename-snapshot-path-mismatch",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {
                        "a.py::1": {
                            "path": "a.py",
                            "turn_id": 999,
                            "existed": True,
                            "content_hash": "1" * 64,
                            "size": 7,
                        }
                    },
                }
            },
            id="rename-snapshot-turn-mismatch",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {
                        "a.py::1": {
                            "path": "a.py",
                            "turn_id": 1,
                            "existed": "not-a-bool",
                            "content_hash": "1" * 64,
                            "size": 7,
                        }
                    },
                }
            },
            id="rename-snapshot-existed-not-a-bool",
        ),
        pytest.param(
            {
                "chrys_mutations": {
                    "turns": [
                        {"turn_id": 1, "mutations": [{"path": "b.py", "old_path": "a.py", "after_hash": "2" * 64}]}
                    ],
                    "snapshots": {"a.py::1": {"path": "a.py", "turn_id": 1, "existed": True, "size": 7}},
                }
            },
            id="rename-snapshot-existence-inconsistent",
        ),
    ],
)
def test_readable_session_without_mutation_detail_still_uses_recorded_summaries(
    tmp_path: Path, state: dict[str, object]
) -> None:
    """A parseable session document is not evidence about mutations when its
    mutation detail is absent or malformed — anywhere in its structure, down
    to leaf values and rename-source snapshots the writer could never have
    produced; the recorded summaries must win over an exact zero, under-count,
    or misclassification read off the partial detail projection. The summary
    names its checkpoint so the detail degrades for its damage alone, not for
    staleness."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        turn_id=_TURN_ONE,
        payload={
            "files_touched": 2,
            "create": 1,
            "modify": 1,
            "delete": 0,
            "net_zero_count": 0,
            "source_ref": {"kind": "session_checkpoint", "id": "c" * 32, "hash": "h" * 8},
        },
    )
    log.add("turn.finished", 2 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(json.dumps({"state": state}), encoding="utf-8")

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is False
    assert (change.files_touched.value, change.files_touched.precision) == (2, Precision.ESTIMATED)
    assert (change.created.value, change.modified.value) == (1, 1)
    assert change.rows == ()


def test_present_empty_mutation_detail_keeps_exact_zero_counts(tmp_path: Path) -> None:
    """An empty ``chrys_mutations`` written by the tracker is a positive
    zero-mutation record, not missing detail — exactness survives."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add("turn.finished", _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps({"state": {"chrys_mutations": {"turns": [], "snapshots": {}}}}),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is True
    assert (change.files_touched.value, change.files_touched.precision) == (0, Precision.EXACT)
    assert change.rows == ()


def test_summary_without_a_checkpoint_source_distrusts_the_stale_session_detail(tmp_path: Path) -> None:
    """A failed final save drops the checkpoint but the finalizer still
    records the turn's summary; the readable document then predates those
    mutations, and its detail must not be read as an exact zero."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        turn_id=_TURN_ONE,
        payload={"files_touched": 2, "create": 1, "modify": 1, "delete": 0, "net_zero_count": 0},
    )
    log.add("turn.finished", 2 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps({"state": {"chrys_mutations": {"turns": [], "snapshots": {}}}}),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is False
    assert (change.files_touched.value, change.files_touched.precision) == (2, Precision.ESTIMATED)
    assert (change.created.value, change.modified.value) == (1, 1)


@pytest.mark.parametrize(
    "source_ref",
    [
        pytest.param({"kind": "sub_agent_log", "id": "c" * 32, "hash": "h" * 8}, id="foreign-kind"),
        pytest.param({"id": "c" * 32, "hash": "h" * 8}, id="kind-missing"),
        pytest.param({"kind": "session_checkpoint", "id": "not-a-minted-id"}, id="id-not-a-minted-shape"),
    ],
)
def test_summary_with_an_unmintable_source_ref_distrusts_the_stale_session_detail(
    tmp_path: Path, source_ref: dict[str, object]
) -> None:
    """Only a session-checkpoint ref carrying an id the save path could have
    minted proves the document covers the counted mutations; a ref of another
    kind — or one naming an id no save could produce — proves nothing about
    the document and must not vouch for its stale detail."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        turn_id=_TURN_ONE,
        payload={
            "files_touched": 2,
            "create": 1,
            "modify": 1,
            "delete": 0,
            "net_zero_count": 0,
            "source_ref": source_ref,
        },
    )
    log.add("turn.finished", 2 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps({"state": {"chrys_mutations": {"turns": [], "snapshots": {}}}}),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is False
    assert (change.files_touched.value, change.files_touched.precision) == (2, Precision.ESTIMATED)
    assert (change.created.value, change.modified.value) == (1, 1)


def test_checkpointed_summary_keeps_the_exact_session_detail(tmp_path: Path) -> None:
    """A summary that names its session checkpoint proves the document was
    saved after the mutations it counts; the detailed projection stays the
    exact source."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.add(
        "tool.mutation_batch.summary",
        _NS,
        turn_id=_TURN_ONE,
        payload={
            "files_touched": 2,
            "create": 0,
            "modify": 2,
            "delete": 0,
            "net_zero_count": 0,
            "source_ref": {"kind": "session_checkpoint", "id": "c" * 32, "hash": "h" * 8},
        },
    )
    log.add("turn.finished", 2 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "a.py",
                                        "before_hash": "1" * 64,
                                        "after_hash": "2" * 64,
                                        "provenance": "proven",
                                    },
                                    {
                                        "path": "b.py",
                                        "before_hash": "1" * 64,
                                        "after_hash": "3" * 64,
                                        "provenance": "proven",
                                    },
                                ],
                            }
                        ],
                        "snapshots": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    change = analyze_trajectory(path).change_verification

    assert change is not None
    assert change.detail_available is True
    assert (change.files_touched.value, change.files_touched.precision) == (2, Precision.EXACT)
    assert {row.path for row in change.rows} == {"a.py", "b.py"}


@pytest.mark.parametrize(("saved", "reported"), [(True, False), (False, True)])
def test_edits_that_cancelled_out_hide_the_warning_only_once_their_detail_is_saved(
    tmp_path: Path, saved: bool, reported: bool
) -> None:
    """The finalizer records a turn's end, saves, and only then writes the
    turn's mutation summary. Until the summary lands, the readable detail
    holds only earlier turns, which say nothing about the newer edit."""
    path = _installed_log(tmp_path)
    log = EventLog()
    log.coverage()
    turns = ((_TURN_ONE, 1, "8" * 32, "6" * 32), (_TURN_TWO, 2, "9" * 32, "3" * 32))
    for index, (turn_id, number, operation_id, call_item_id) in enumerate(turns):
        start = index * 5 * _NS
        log.add("turn.started", start, turn_id=turn_id, payload={"turn_number": number})
        log.span(
            "tool.operation",
            operation_id,
            start + _NS,
            start + 2 * _NS,
            turn_id=turn_id,
            start_payload={
                "tool_name": "write_file",
                "tool_kind": "filesystem.write",
                "call_item_id": call_item_id,
                "argument_fingerprint": f"edit-{number}",
            },
        )
        log.add(
            "turn.finished", start + 3 * _NS, turn_id=turn_id, payload={"end_reason": "completed", "duration_ms": 0}
        )
        if number == 1 or saved:
            log.add(
                "tool.mutation_batch.summary",
                start + 4 * _NS,
                turn_id=turn_id,
                payload={
                    "files_touched": 1,
                    "create": 0,
                    "modify": 1,
                    "delete": 0,
                    "net_zero_count": 1,
                    "source_ref": {"kind": "session_checkpoint", "id": f"{number}" * 32, "hash": "h" * 8},
                },
            )
    log.write(path)
    saved_turns = [{"turn_id": 1, "path": "a.py", "hash": "1" * 64}]
    if saved:
        saved_turns.append({"turn_id": 2, "path": "b.py", "hash": "2" * 64})
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": turn["turn_id"],
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": turn["path"],
                                        "before_hash": turn["hash"],
                                        "after_hash": turn["hash"],
                                        "provenance": "proven",
                                    }
                                ],
                            }
                            for turn in saved_turns
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    analysis = analyze_trajectory(path)

    change = analysis.change_verification
    assert change is not None
    assert {row.state for row in change.rows} == {ChangeVerificationState.NET_ZERO}
    assert all(row.precision is Precision.EXACT for row in change.rows)
    assert ("unverified-change" in {finding.rule_id for finding in analysis.findings}) is reported


def _installed_log(tmp_path: Path) -> Path:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    return path


def _write_two_turn_actions(path: Path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "8" * 32,
        _NS,
        2 * _NS,
        turn_id=_TURN_ONE,
        start_payload={
            "tool_name": "write_file",
            "tool_kind": "filesystem.write",
            "call_item_id": "6" * 32,
            "argument_fingerprint": "edit-fingerprint",
        },
    )
    log.add(
        "turn.finished",
        3 * _NS,
        turn_id=_TURN_ONE,
        payload={"end_reason": "cancelled", "duration_ms": 0},
    )
    log.add("turn.started", 4 * _NS, turn_id=_TURN_TWO, payload={"turn_number": 2})
    log.span(
        "tool.operation",
        "9" * 32,
        5 * _NS,
        6 * _NS,
        turn_id=_TURN_TWO,
        start_payload={
            "tool_name": "Bash",
            "tool_kind": "shell",
            "call_item_id": _VERIFY_CALL,
            "argument_fingerprint": "verify-fingerprint",
        },
    )
    log.add(
        "turn.finished",
        7 * _NS,
        turn_id=_TURN_TWO,
        payload={"end_reason": "cancelled", "duration_ms": 0},
    )
    log.write(path)


def _write_edit_only_action(path: Path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "8" * 32,
        _NS,
        2 * _NS,
        turn_id=_TURN_ONE,
        start_payload={
            "tool_name": "write_file",
            "tool_kind": "filesystem.write",
            "call_item_id": "6" * 32,
            "argument_fingerprint": "edit-fingerprint",
        },
    )
    log.add("turn.finished", 3 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)


def _write_same_turn_edit_and_verify(path: Path, *, overlap: bool) -> None:
    """One turn holding an edit and a verify; *overlap* starts the verify
    while the edit is still landing instead of after its terminal."""
    edit_payload = {
        "tool_name": "write_file",
        "tool_kind": "filesystem.write",
        "call_item_id": "6" * 32,
        "argument_fingerprint": "edit-fingerprint",
    }
    verify_payload = {
        "tool_name": "Bash",
        "tool_kind": "shell",
        "call_item_id": _VERIFY_CALL,
        "argument_fingerprint": "verify-fingerprint",
    }
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=_TURN_ONE, payload={"turn_number": 1})
    if overlap:
        spans = [
            ("tool.operation.started", _NS, "8" * 32, edit_payload, None),
            ("tool.operation.started", 2 * _NS, "9" * 32, verify_payload, None),
            ("tool.operation.finished", 3 * _NS, "9" * 32, None, 1_000),
            ("tool.operation.finished", 4 * _NS, "8" * 32, None, 3_000),
        ]
        for event_type, monotonic_ns, operation_id, payload, duration_ms in spans:
            log.add(
                event_type,
                monotonic_ns,
                turn_id=_TURN_ONE,
                operation_id=operation_id,
                payload=payload if payload is not None else {"outcome": "success", "duration_ms": duration_ms},
                measurements=(
                    {"/payload/duration_ms": measurement("monotonic_clock", method_version=1)}
                    if duration_ms is not None
                    else None
                ),
            )
    else:
        log.span("tool.operation", "8" * 32, _NS, 2 * _NS, turn_id=_TURN_ONE, start_payload=edit_payload)
        log.span("tool.operation", "9" * 32, 3 * _NS, 4 * _NS, turn_id=_TURN_ONE, start_payload=verify_payload)
    log.add("turn.finished", 5 * _NS, turn_id=_TURN_ONE, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "chrys_mutations": {
                        "turns": [
                            {
                                "turn_id": 1,
                                "detection_truncated": False,
                                "mutations": [
                                    {
                                        "path": "changed.py",
                                        "before_hash": "be" * 32,
                                        "after_hash": "af" * 32,
                                        "provenance": "proven",
                                    }
                                ],
                            }
                        ]
                    },
                    "messages": [_verify_command_message()],
                }
            }
        ),
        encoding="utf-8",
    )


def _verify_command_message() -> dict[str, object]:
    """Session-store carrier that classifies the shell call as a verify."""
    return _command_message(_VERIFY_CALL, "pytest -q")


def _command_message(call_item_id: str, command: str) -> dict[str, object]:
    return {
        "contents": [
            {
                "type": "function_call",
                "arguments": json.dumps({"command": command}),
                "additional_properties": {ANALYTICS_ITEM_ID_KEY: call_item_id},
            }
        ]
    }
