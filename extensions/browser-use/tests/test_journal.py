# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Action journal storage: phases, isolation, retention and failure tolerance (W04)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from icode_browser_use.journal import DISPATCHED, PREPARED, RESULT, Journal, describe


def journal(tmp_path: Path, **overrides: object) -> Journal:
    settings: dict[str, object] = {"owner": "owner-a", "logical_session": "slot-0", "generation": "gen-1"}
    settings.update(overrides)
    return Journal(tmp_path / "journal.sqlite3", **settings)


def record(j: Journal, action_id: str = "a1", phase: str = RESULT, status: str = "succeeded", **extra: object) -> None:
    j.record(
        action_id=action_id,
        tool="browser_click",
        phase=phase,
        status=status,
        evidence=f"action_id={action_id}",
        document_epoch=int(extra.pop("document_epoch", 7)),  # type: ignore[arg-type]
    )


def test_result_row_round_trips(tmp_path: Path) -> None:
    j = journal(tmp_path)
    record(j)
    row = j.lookup("a1")
    assert row is not None
    assert row["phase"] == RESULT
    assert row["status"] == "succeeded"
    assert row["tool"] == "browser_click"
    assert row["document_epoch"] == 7
    assert row["schema"] == 1
    assert row["generation"] == "gen-1"
    assert isinstance(row["at"], float)


def test_each_phase_overwrites_the_previous_one(tmp_path: Path) -> None:
    j = journal(tmp_path)
    record(j, phase=PREPARED, status="prepared")
    assert j.lookup("a1")["phase"] == PREPARED  # type: ignore[index]
    record(j, phase=DISPATCHED, status="dispatched")
    assert j.lookup("a1")["phase"] == DISPATCHED  # type: ignore[index]
    assert j.count() == 1


def test_rows_are_scoped_by_owner_and_session(tmp_path: Path) -> None:
    shared = tmp_path / "journal.sqlite3"
    first = Journal(shared, owner="owner-a", logical_session="slot-0")
    second = Journal(shared, owner="owner-a", logical_session="slot-1")
    third = Journal(shared, owner="owner-b", logical_session="slot-0")
    record(first)
    assert first.lookup("a1") is not None
    assert second.lookup("a1") is None
    assert third.lookup("a1") is None
    second.record(action_id="a2", tool="browser_fill", phase=RESULT, status="succeeded", evidence="x")
    assert second.count() == 1
    assert first.count() == 1


def test_retention_keeps_the_newest_rows(tmp_path: Path) -> None:
    j = journal(tmp_path, limit=5)
    for index in range(9):
        record(j, action_id=f"a{index}")
    assert j.count() == 5
    assert j.lookup("a0") is None
    assert j.lookup("a8") is not None


def test_unavailable_storage_never_raises(tmp_path: Path) -> None:
    j = Journal(tmp_path)  # a directory cannot be opened as a database
    record(j)
    assert j.lookup("a1") is None
    assert j.count() == 0
    j.close()


def test_in_memory_journal_supports_test_doubles() -> None:
    j = Journal(None)
    record(j, phase=DISPATCHED, status="dispatched")
    assert j.lookup("a1")["phase"] == DISPATCHED  # type: ignore[index]
    j.close()


def test_describe_downgrades_incomplete_phases_to_uncertain(tmp_path: Path) -> None:
    j = journal(tmp_path)
    record(j, phase=PREPARED, status="prepared")
    payload = json.loads(describe(j.lookup("a1")))  # type: ignore[arg-type]
    assert payload["phase"] == PREPARED
    assert payload["status"] == "uncertain"
    assert payload["retryable"] is False
    assert "may or may not have been sent" in payload["reason"]


def test_describe_keeps_a_recorded_result(tmp_path: Path) -> None:
    j = journal(tmp_path)
    record(j, status="blocked")
    payload = json.loads(describe(j.lookup("a1")))  # type: ignore[arg-type]
    assert payload["status"] == "blocked"
    assert payload["retryable"] is False
    assert "reason" not in payload


def test_unknown_action_id_is_absent(tmp_path: Path) -> None:
    j = journal(tmp_path)
    assert j.lookup("missing") is None


def test_rows_for_includes_in_flight_rows_of_the_tool_and_epoch_newest_first(tmp_path: Path) -> None:
    j = journal(tmp_path)
    record(j, action_id="old", status="succeeded", document_epoch=7)
    j.record(
        action_id="other-tool", tool="browser_fill", phase=RESULT, status="uncertain", evidence="x", document_epoch=7
    )
    record(j, action_id="other-epoch", status="uncertain", document_epoch=8)
    record(j, action_id="still-prepared", phase=PREPARED, status="prepared", document_epoch=7)
    record(j, action_id="still-dispatched", phase=DISPATCHED, status="dispatched", document_epoch=7)
    record(j, action_id="new", status="uncertain", document_epoch=7)

    rows = j.rows_for("browser_click", 7)
    assert [row["action_id"] for row in rows] == ["new", "still-dispatched", "still-prepared", "old"]
    assert [row["phase"] for row in rows] == [RESULT, DISPATCHED, PREPARED, RESULT]
    assert all(row["tool"] == "browser_click" and row["document_epoch"] == 7 for row in rows)
    assert rows[0]["evidence"] == "action_id=new"
    assert [row["action_id"] for row in j.rows_for("browser_click", 8)] == ["other-epoch"]
    assert [row["action_id"] for row in j.rows_for("browser_fill", 7)] == ["other-tool"]
    assert j.rows_for("browser_navigate", 7) == []


def test_rows_for_is_scoped_to_the_owner_and_session(tmp_path: Path) -> None:
    shared = tmp_path / "journal.sqlite3"
    first = Journal(shared, owner="owner-a", logical_session="slot-0")
    second = Journal(shared, owner="owner-a", logical_session="slot-1")
    record(first, action_id="a1")
    assert [row["action_id"] for row in first.rows_for("browser_click", 7)] == ["a1"]
    assert second.rows_for("browser_click", 7) == []


def test_rows_for_on_unavailable_storage_is_empty(tmp_path: Path) -> None:
    j = Journal(tmp_path)  # a directory cannot be opened as a database
    record(j)
    assert j.rows_for("browser_click", 7) == []
    j.close()


def test_reopening_the_file_finds_earlier_rows(tmp_path: Path) -> None:
    first = journal(tmp_path)
    record(first)
    first.close()
    second = journal(tmp_path)
    assert second.lookup("a1") is not None
    second.close()


def test_journal_rows_survive_a_session_restart(tmp_path: Path) -> None:
    from icode_browser_use.actions.actionability import lookup_action, record_action
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.session import BrowserSession

    cfg = BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state")
    first = BrowserSession(cfg, journal_owner="owner-a", journal_session="slot-0")
    record_action(first, "abc123", "browser_click", "succeeded", "effect=dom")
    asyncio.run(first.aclose())

    second = BrowserSession(cfg, journal_owner="owner-a", journal_session="slot-0")
    row = lookup_action(second, "abc123")
    assert row is not None
    assert row["status"] == "succeeded"
    assert lookup_action(second, "other") is None
    asyncio.run(second.aclose())


def test_another_session_object_cannot_read_the_journal(tmp_path: Path) -> None:
    from icode_browser_use.actions.actionability import lookup_action, record_action
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.session import BrowserSession

    cfg = BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state")
    first = BrowserSession(cfg, journal_owner="owner-a", journal_session="slot-0")
    record_action(first, "abc123", "browser_click", "succeeded", "effect=dom")
    second = BrowserSession(cfg, journal_owner="owner-b", journal_session="slot-0")
    assert lookup_action(second, "abc123") is None
    asyncio.run(first.aclose())
    asyncio.run(second.aclose())


def test_dom_click_allowed_uses_structured_fields_not_evidence_prose(tmp_path: Path) -> None:
    import sqlite3
    from types import SimpleNamespace

    from icode_browser_use.actions.click import _dom_click_allowed
    from icode_browser_use.journal import RESULT

    j = journal(tmp_path)
    entry = SimpleNamespace(token="@e5#7")
    session = SimpleNamespace(journal=j, page_epoch=7)
    facts = {"tag": "button", "role": "button"}

    # Completely arbitrary prose in evidence still allows dom click when structured fields match.
    j.record(
        action_id="c1",
        tool="browser_click",
        phase=RESULT,
        status="uncertain",
        evidence="completely rewritten english prose without any magic markers",
        document_epoch=7,
        target_ref="@e5#7",
        dispatch="mouse",
        effect="none",
    )
    assert _dom_click_allowed(session, entry, facts, submit_like=False) is None
    assert _dom_click_allowed(session, entry, facts, submit_like=True) is not None

    # A second dom click is refused once dispatch="dom" was recorded, regardless of prose.
    j.record(
        action_id="c2",
        tool="browser_click",
        phase=RESULT,
        status="succeeded",
        evidence="rewritten prose",
        document_epoch=7,
        target_ref="@e5#7",
        dispatch="dom",
        effect="dom",
    )
    assert _dom_click_allowed(session, entry, facts, submit_like=False) is not None
    j.close()

    # Legacy SQLite DB missing the new columns migrates cleanly and treats old rows conservatively.
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.execute(
        "CREATE TABLE actions ("
        "action_id TEXT PRIMARY KEY, schema INTEGER NOT NULL, owner TEXT NOT NULL, "
        "logical_session TEXT NOT NULL, generation TEXT NOT NULL, tool TEXT NOT NULL, "
        "phase TEXT NOT NULL, status TEXT NOT NULL, document_epoch INTEGER NOT NULL, "
        "evidence TEXT NOT NULL, at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "old1",
            1,
            "owner-a",
            "slot-0",
            "g1",
            "browser_click",
            RESULT,
            "uncertain",
            7,
            "old @e5#7\neffect=none\n",
            1.0,
        ),
    )
    conn.commit()
    conn.close()

    migrated = Journal(legacy_path, owner="owner-a", logical_session="slot-0", generation="g1")
    legacy_session = SimpleNamespace(journal=migrated, page_epoch=7)
    assert _dom_click_allowed(legacy_session, entry, facts, submit_like=False) is not None
    migrated.close()


def test_journal_connect_closes_on_error_and_record_rolls_back(tmp_path: Path, monkeypatch) -> None:
    """Wave 3 P2-6: _connect closes SQLite handle on init error and record() rolls back on sqlite3.Error."""
    import sqlite3

    from icode_browser_use import journal as journal_mod

    events: list[str] = []

    class FakeConn:
        row_factory = None

        def execute(self, _sql: str, *_args):
            raise sqlite3.OperationalError("simulated pragma error")

        def executescript(self, _sql: str):
            raise sqlite3.OperationalError("simulated schema error")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(journal_mod.sqlite3, "connect", lambda *_a, **_kw: FakeConn())
    j1 = Journal(tmp_path / "broken.sqlite3", owner="o", logical_session="s")
    assert j1._connect(tmp_path / "broken.sqlite3") is None
    assert "close" in events

    # Restore real connect and verify record() rolls back on sqlite3.Error.
    monkeypatch.undo()
    j2 = journal(tmp_path)
    conn = j2._connect(tmp_path / "journal.sqlite3")
    assert conn is not None

    class FailingWriteConn:
        def __init__(self, inner) -> None:
            self._inner = inner

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            events.append("record_rollback")

        def close(self) -> None:
            self._inner.close()

    j2._connection = FailingWriteConn(conn)  # type: ignore[assignment]
    record(j2, action_id="fail-1")
    assert "record_rollback" in events
    j2.close()
