# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase-4 state SystemReminderMiddleware carries across turns, retries and session restore."""

from __future__ import annotations

import asyncio
import json
import logging
from contextvars import Context
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Content, Message
from chrys.kernel.middleware import ChatContext
from chrys.orchestration.invoker.runtime import restore_phase4_state
from chrys.service.agent_middleware.reminders.archive_pointer import (
    CATALOG_POINTER_RECORD_COUNT_STATE_KEY,
    _catalog_pointer_text,
)
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.agent_middleware.system_reminder import (
    wrap_system_reminder as _wrap,
)
from chrys.service.context.compaction import last_words_state as last_words_mod
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState, ManifestEntry
from chrys.service.context.compaction.spill import (
    CATALOG_RELATIVE_PATH,
    NOTE_RECORD_GROUP_ID,
    NOTE_RECORD_TOOL_NAME,
    SpillQuota,
)
from tests.support.reminder_calls import enrich_call
from tests.support.reminder_stack import reminder_pair


def _user(text: str) -> Message:
    return Message(role="user", contents=[Content.from_text(text)])


def _manifest_entry(
    sequence: int,
    *,
    available: bool = True,
    no_record_reason: str = "",
    assistant_text: bool = False,
) -> ManifestEntry:
    record_id = f"record-{sequence}" if not no_record_reason else ""
    relative_path = f"compactions/dropped/turn012/{sequence:03d}_read_file_{sequence:032x}.md"
    if no_record_reason:
        relative_path = ""
    return ManifestEntry(
        record_id=record_id,
        group_id=f"group-{sequence}",
        record_dir="compactions/dropped/turn012",
        relative_path=relative_path,
        turn=12,
        round=2,
        sequence=sequence,
        tool="assistant" if assistant_text else "read_file",
        display_argument="" if assistant_text else f'path="file-{sequence}.txt"',
        outcome="" if assistant_text else "ok",
        size_chars=sequence * 100,
        assistant_text=assistant_text,
        available=available,
        no_record_reason=no_record_reason,
    )


class TestLastWordsRoundTrip:
    def test_set_and_get(self) -> None:
        _, lw = reminder_pair()
        assert lw.get_last_words() is None
        lw.set_last_words("[note]")
        assert lw.get_last_words() == "[note]"

    def test_set_empty_clears(self) -> None:
        """Empty/None text clears the note — prevents falsey strings
        slipping through as valid notes."""
        _, lw = reminder_pair()
        lw.set_last_words("[note]")
        lw.set_last_words("")
        assert lw.get_last_words() is None
        lw.set_last_words("[note2]")
        lw.set_last_words(None)
        assert lw.get_last_words() is None

    def test_prepare_turn_clears_last_words(self) -> None:
        """A new user turn must not inherit the previous turn's LAST_WORDS.

        If we didn't clear here, the note from turn N would silently leak
        into turn N+1's user message — confusing the model with stale state.
        """
        mw, lw = reminder_pair()
        lw.set_last_words("[turn N note]")
        mw.prepare_turn(usage={})
        assert lw.get_last_words() is None

    async def test_last_words_child_task_update_survives_for_retry(self) -> None:
        """Phase 4 writes LAST_WORDS inside the agent task; retry runs in another task."""
        mw, lw = reminder_pair()
        mw.prepare_turn(usage={})

        async def _phase4_child_task() -> None:
            lw.set_last_words("[progress from child task]")

        await asyncio.create_task(_phase4_child_task())
        assert lw.get_last_words() == "[progress from child task]"

        async def _retry_task() -> None:
            mw.prepare_turn(usage={}, preserve_last_words=True)
            assert lw.get_last_words() == "[progress from child task]"
            appended = lw.render()
            assert len(appended) == 1
            assert "[progress from child task]" in appended[0]

        await Context().run(asyncio.create_task, _retry_task())


class TestLastWordsRestore:
    """Session-restore persistence: a note restored from ``session.json`` must
    behave exactly like an in-process note — re-injected when the interrupted
    turn is resumed, discarded when a fresh turn starts instead."""

    def test_preserving_prepare_turn_consumes_restored_note(self) -> None:
        """Post-restart Continue (run_retry) re-injects the persisted note."""
        mw, lw = reminder_pair()
        lw.restore_last_words("[persisted note]")
        mw.prepare_turn(usage={}, preserve_last_words=True)
        assert lw.get_last_words() == "[persisted note]"
        appended = lw.render()
        assert len(appended) == 1
        assert "[persisted note]" in appended[0]

    def test_fresh_turn_discards_restored_note(self) -> None:
        """A new user turn after restore drops the note — same as in-process —
        and a later retry must not resurrect it."""
        mw, lw = reminder_pair()
        lw.restore_last_words("[persisted note]")
        mw.prepare_turn(usage={})
        assert lw.get_last_words() is None
        mw.prepare_turn(usage={}, preserve_last_words=True)
        assert lw.get_last_words() is None

    def test_get_last_words_falls_back_to_restored_note_before_any_turn(self) -> None:
        """Saving a restored session that was never resumed must still see the
        note — otherwise restore → quit would erase it from disk."""
        _, lw = reminder_pair()
        lw.restore_last_words("[persisted note]")
        assert lw.get_last_words() == "[persisted note]"

    def test_live_turn_note_wins_over_restored_note(self) -> None:
        _, lw = reminder_pair()
        lw.restore_last_words("[stale persisted note]")
        lw.set_last_words("[fresh phase4 note]")
        assert lw.get_last_words() == "[fresh phase4 note]"

    def test_restore_empty_clears_pending_note(self) -> None:
        mw, lw = reminder_pair()
        lw.restore_last_words("[persisted note]")
        lw.restore_last_words("")
        assert lw.get_last_words() is None
        mw.prepare_turn(usage={}, preserve_last_words=True)
        assert lw.get_last_words() is None

    def test_in_process_note_preferred_over_restored_on_retry(self) -> None:
        """When a live turn already carries a note, a preserving prepare_turn
        keeps carrying it; the restored stash never overrides it."""
        mw, lw = reminder_pair()
        mw.prepare_turn(usage={})
        lw.set_last_words("[in-process note]")
        lw.restore_last_words("[persisted note]")
        mw.prepare_turn(usage={}, preserve_last_words=True)
        assert lw.get_last_words() == "[in-process note]"


class TestRefreshLastWordsReminder:
    """Phase 4 sets the note *below* the middleware's enrichment, so the
    compacting call's user message was rendered before the note existed.
    ``refresh_last_words_reminder`` rewrites it in the per-call list so that
    very request already carries the fresh note, byte-identical to what the
    next call's enrichment will produce."""

    def test_appends_note_and_replaces_list_entry_without_mutating_original(self) -> None:
        mw, lw = reminder_pair()
        lw.set_last_words("[fresh note]")
        original = _user("please do X")
        assistant = Message(role="assistant", contents=[Content.from_text("ok")])
        messages: list[Message] = [original, assistant]

        assert mw.refresh_last_words_reminder(messages) == 0

        refreshed = messages[0]
        assert refreshed is not original
        assert [c.text for c in original.contents] == ["please do X"]
        # Write-through for exclusion marks etc. must survive the swap.
        assert refreshed.additional_properties is original.additional_properties
        texts = [c.text or "" for c in refreshed.contents if c.type == "text"]
        assert texts[0] == "please do X"
        assert texts[-1].startswith("<system-reminder>\n[LAST_WORDS] ")
        assert "[fresh note]" in texts[-1]

    def test_replaces_stale_note_block_keeping_turn_reminders(self) -> None:
        mw, lw = reminder_pair()
        lw.set_last_words("[old note]")
        enriched = SystemReminderMiddleware._create_enriched(
            _user("please do X"),
            ["[runtime]"],
            lw.render(),
        )
        messages: list[Message] = [enriched]

        lw.set_last_words("[new note]")
        assert mw.refresh_last_words_reminder(messages) == 0

        texts = [c.text or "" for c in messages[0].contents if c.type == "text"]
        assert sum("LAST_WORDS" in t for t in texts) == 1
        assert "[new note]" in texts[-1]
        assert all("[old note]" not in t for t in texts)
        assert any("[runtime]" in t for t in texts)

    def test_noop_without_note_or_existing_block(self) -> None:
        mw = SystemReminderMiddleware()
        original = _user("hello")
        messages: list[Message] = [original]
        assert mw.refresh_last_words_reminder(messages) is None
        assert messages[0] is original

    def test_noop_without_user_message(self) -> None:
        mw, lw = reminder_pair()
        lw.set_last_words("[note]")
        messages: list[Message] = [Message(role="assistant", contents=[Content.from_text("hi")])]
        assert mw.refresh_last_words_reminder(messages) is None

    def test_targets_last_user_message(self) -> None:
        mw, lw = reminder_pair()
        lw.set_last_words("[note]")
        first = _user("first")
        last = _user("last")
        messages: list[Message] = [first, Message(role="assistant", contents=[Content.from_text("ok")]), last]

        assert mw.refresh_last_words_reminder(messages) == 2
        assert messages[0] is first
        assert "LAST_WORDS" in (messages[2].contents[-1].text or "")

    async def test_rendering_matches_next_call_enrichment(self) -> None:
        """Byte-stability: the refreshed message must equal what the next
        call sends for the same original — otherwise the user-message tail
        changes between consecutive requests and the provider prefix cache
        re-misses on it."""
        mw, lw = reminder_pair()
        mw.prepare_turn(usage={})
        original = _user("please do X")
        compacting = ChatContext(client=None, messages=[original], options=None)

        async def _deliver_compact_and_send() -> None:
            # The compacting call was enriched and delivered before the note
            # existed; Phase 4 then sets it and refreshes the outgoing message
            # before the provider request delivers it again.
            for observer in compacting.request_message_observers:
                observer(compacting.messages)
            lw.set_last_words("[phase4 note]")
            assert mw.refresh_last_words_reminder(compacting.messages) == 0
            for observer in compacting.request_message_observers:
                observer(compacting.messages)

        await mw.process(compacting, _deliver_compact_and_send)
        next_call = ChatContext(client=None, messages=[original], options=None)

        async def _send() -> None:
            for observer in next_call.request_message_observers:
                observer(next_call.messages)

        await mw.process(next_call, _send)
        refreshed = compacting.messages[0]
        sent_next = next_call.messages[0]
        assert "[phase4 note]" in (refreshed.contents[-1].text or "")
        assert [c.text for c in refreshed.contents] == [c.text for c in sent_next.contents]


class TestAppendReminders:
    """LAST_WORDS rendering: the pending note as an injected ``<system-reminder>`` block."""

    def test_render_is_empty_by_default(self) -> None:
        _, lw = reminder_pair()
        assert lw.render() == []

    def test_render_contains_note(self) -> None:
        _, lw = reminder_pair()
        lw.set_last_words("[progress]")
        appended = lw.render()
        assert len(appended) == 1
        # Note body must appear in the appended reminder text.
        assert "[progress]" in appended[0]
        # Must include the LAST_WORDS label so the model knows what it is.
        assert "LAST_WORDS" in appended[0]

    def test_render_last_words_reminder_text_matches_injected_blocks(self) -> None:
        from chrys.service.agent_middleware.system_reminder import REMINDER_TAG_OPEN
        from chrys.service.agent_middleware.system_reminder import wrap_system_reminder as _wrap

        _, lw = reminder_pair()
        assert lw.render_last_words_reminder_text() is None
        lw.set_last_words("[progress] with a literal <system-reminder> tag")
        rendered = lw.render_last_words_reminder_text()
        # Wire fidelity: same envelope + tag-escaping as the enrichment path.
        assert rendered == "\n\n".join(_wrap(r) for r in lw.render())
        assert "[progress]" in rendered
        assert rendered.startswith("<system-reminder>\n")
        assert rendered.endswith("\n</system-reminder>")
        assert "&lt;system-reminder&gt; tag" in rendered
        assert rendered.count(REMINDER_TAG_OPEN) == 1


class TestDroppedRecordManifestState:
    def test_manifest_entry_state_round_trip_and_malformed_drop(self) -> None:
        entry = _manifest_entry(7, available=False)

        assert ManifestEntry.from_state(entry.to_state()) == entry
        assert ManifestEntry.from_state({**entry.to_state(), "turn": True}) is None
        assert ManifestEntry.from_state({**entry.to_state(), "relative_path": "../escape.md"}) is None
        assert ManifestEntry.from_state({**entry.to_state(), "record_dir": "."}) is None
        assert (
            ManifestEntry.from_state(
                {
                    **entry.to_state(),
                    "record_dir": "compactions/dropped/turn999",
                }
            )
            is None
        )
        assert ManifestEntry.from_state({**entry.to_state(), "tool": "x" * 4_097}) is None
        overlong_argument = "x" * (last_words_mod.MANIFEST_DISPLAY_ARGUMENT_MAX_CHARS + 1)
        assert ManifestEntry.from_state({**entry.to_state(), "display_argument": overlong_argument}) is None
        assert ManifestEntry.from_state({**entry.to_state(), "size_chars": 1 << 63}) is None
        assert DropRoundBreakerState.from_state({**DropRoundBreakerState().to_state(), "version": True}) is None
        assert (
            DropRoundBreakerState.from_state({**DropRoundBreakerState().to_state(), "side_call_tokens": 1 << 63})
            is None
        )

    def test_no_saved_breaker_state_is_not_reported_as_malformed(self, caplog: pytest.LogCaptureFixture) -> None:
        """Every session that never dropped anything restores without one."""
        with caplog.at_level(logging.DEBUG, logger=last_words_mod.__name__):
            assert DropRoundBreakerState.from_state(None) is None
            assert caplog.records == []
            # The control: data that is there and wrong is still reported.
            assert DropRoundBreakerState.from_state({"version": "1"}) is None
            assert [record.getMessage() for record in caplog.records] == [
                "Dropping malformed or unsupported Phase-4 breaker state"
            ]

    def test_manifest_state_is_strict_utf8_even_with_unpaired_surrogate(self) -> None:
        entry = replace(_manifest_entry(7), display_argument='path="bad-\udc80.txt"')

        state = entry.to_state()

        assert state["display_argument"] == 'path="bad-\\udc80.txt"'
        assert ManifestEntry.from_state(state) is not None
        assert ManifestEntry.from_state({**state, "display_argument": "\udc80"}) is None
        json.dumps(state, ensure_ascii=False).encode("utf-8")

    def test_manifest_and_full_breaker_restore_on_preserving_retry(self, tmp_path: Path) -> None:
        entry = _manifest_entry(1)
        breaker = DropRoundBreakerState(
            attempts=4,
            consecutive_no_progress=1,
            tail_override=True,
            disabled=False,
            side_call_tokens=1_234,
        )
        record = tmp_path / entry.relative_path
        record.parent.mkdir(parents=True)
        record.write_text("record", encoding="utf-8")
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=True)
        lw.restore_last_words_manifest([entry.to_state()])
        lw.restore_last_words_breaker(breaker.to_state())

        mw.prepare_turn(usage={}, preserve_last_words=True)

        assert lw.get_last_words_manifest() == [entry.to_state()]
        assert lw.get_drop_round_breaker() == breaker

    def test_restore_availability_sweep_marks_missing_without_render_io(self, tmp_path: Path) -> None:
        entry = _manifest_entry(1)
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=True)
        lw.restore_last_words_manifest([entry.to_state()], available_relative_paths=frozenset())
        mw.prepare_turn(usage={}, preserve_last_words=True)

        with patch.object(Path, "is_file", side_effect=AssertionError("render touched filesystem")):
            rendered = lw.render()[0]

        assert "(record missing)" in rendered
        assert "read the listed file with read_file" not in rendered
        assert "full input/output" not in rendered
        assert "middle-truncation marker" not in rendered

    def test_restore_availability_rejects_symlinked_record_directory(self, tmp_path: Path) -> None:
        entry = _manifest_entry(1)
        redirected = tmp_path / "redirected"
        redirected.mkdir()
        (redirected / Path(entry.relative_path).name).write_text("redirected", encoding="utf-8")
        dropped = tmp_path / "compactions" / "dropped"
        dropped.mkdir(parents=True)
        try:
            (dropped / "turn012").symlink_to(redirected, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=True)

        lw.restore_last_words_manifest([entry.to_state()])
        mw.prepare_turn(usage={}, preserve_last_words=True)

        assert lw.get_last_words_manifest()[0]["available"] is False
        assert "read the listed file with read_file" not in lw.render()[0]

    def test_quota_eviction_marks_existing_manifest_record_unavailable(self, tmp_path: Path) -> None:
        entry = _manifest_entry(1)
        quota = SpillQuota()
        quota.initialize(10, {entry.relative_path})
        mw, lw = reminder_pair(
            session_root=tmp_path,
            file_read_available=True,
            spill_quota=quota,
        )
        mw.prepare_turn()
        lw.append_manifest([entry])

        quota.reclaim(10, relative_path=entry.relative_path)

        assert lw.get_last_words_manifest()[0]["available"] is False
        assert "(record missing)" in lw.render()[0]

    def test_manifest_rendering_has_call_assistant_policy_and_no_read_affordance(self, tmp_path: Path) -> None:
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=False)
        mw.prepare_turn()
        lw.append_manifest(
            [
                _manifest_entry(1),
                _manifest_entry(2, assistant_text=True),
                _manifest_entry(3, no_record_reason="round cap"),
            ]
        )

        rendered = lw.render()[0]

        expected_dir = (tmp_path / "compactions/dropped/turn012").resolve().as_posix()
        assert f"--- Dropped this turn (records under {expected_dir}/) ---" in rendered
        assert 'r2 001 read_file(path="file-1.txt") → ok, 100 chars' in rendered
        assert "r2 002 assistant text, 200 chars" in rendered
        assert "(no record: round cap)" in rendered
        assert "read the listed file with read_file" not in rendered

    def test_manifest_renders_superseded_note_entry_through_real_state(self, tmp_path: Path) -> None:
        """A note entry shaped exactly like the spill layer's must survive the
        REAL from_state round-trip — an empty group_id was dropped silently."""
        note_entry = ManifestEntry(
            record_id="ab12cd34",
            group_id=NOTE_RECORD_GROUP_ID,
            record_dir="compactions/dropped/turn012",
            relative_path="compactions/dropped/turn012/004_last_words_ab12cd34.md",
            turn=12,
            round=2,
            sequence=4,
            tool=NOTE_RECORD_TOOL_NAME,
            display_argument="superseded by this round's note",
            outcome="merged",
            size_chars=1234,
        )
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=True)
        mw.prepare_turn()
        lw.append_manifest([_manifest_entry(1), note_entry])

        rendered = lw.render()[0]

        assert "r2 004 last_words(superseded by this round's note) → merged" in rendered
        assert "004_last_words_ab12cd34.md" in rendered

    def test_restore_normalizes_legacy_note_entry_with_empty_group_id(self, tmp_path: Path) -> None:
        """Sessions persisted by the first note-records build carry note rows
        with ``group_id=""``; restore must repair them, not drop them."""
        legacy_row = {
            "record_id": "5437c317",
            "group_id": "",
            "record_dir": "compactions/dropped/turn001",
            "relative_path": "compactions/dropped/turn001/006_last_words_5437c317.md",
            "turn": 1,
            "round": 2,
            "sequence": 6,
            "tool": "last_words",
            "display_argument": "superseded by this round's note",
            "outcome": "merged",
            "size_chars": 13004,
            "assistant_text": False,
            "available": True,
            "no_record_reason": "",
        }
        _, lw = reminder_pair(session_root=tmp_path, file_read_available=True)
        lw.restore_last_words_manifest(
            [legacy_row],
            available_relative_paths={legacy_row["relative_path"]},
        )

        restored = lw.get_last_words_manifest()
        assert len(restored) == 1
        assert restored[0]["group_id"] == NOTE_RECORD_GROUP_ID
        rendered = lw.render()[0]
        assert "r2 006 last_words(superseded by this round's note) → merged" in rendered
        assert "006_last_words_5437c317.md" in rendered

    def test_empty_group_id_still_rejected_for_non_note_tools(self) -> None:
        entry_state = _manifest_entry(1).to_state()
        entry_state["group_id"] = ""

        assert last_words_mod.ManifestEntry.from_state(entry_state) is None

    def test_manifest_persisted_and_render_budgets_keep_most_recent(self) -> None:
        mw, lw = reminder_pair(file_read_available=True)
        mw.prepare_turn()
        lw.append_manifest([_manifest_entry(index) for index in range(1, 551)])

        state = lw.get_last_words_manifest()
        rendered = lw.render()[0]
        manifest = lw._render_manifest(lw._current_manifest_entries())

        assert len(state) == last_words_mod._MANIFEST_MAX_PERSISTED == 500
        assert state[0]["sequence"] == 51
        assert len(manifest.splitlines()) <= last_words_mod._MANIFEST_MAX_LINES
        assert len(rendered.split("--- Dropped this turn", 1)[-1]) <= last_words_mod._MANIFEST_MAX_CHARS
        assert "… and 453 earlier records — see manifest.md" in rendered
        assert "file-550.txt" in rendered
        assert "file-51.txt" not in rendered

    def test_breaker_writes_without_note_and_resets_at_real_turn_boundary(self) -> None:
        mw, lw = reminder_pair()
        mw.prepare_turn()
        breaker = DropRoundBreakerState(attempts=1, consecutive_no_progress=1, tail_override=True, side_call_tokens=55)

        lw.set_drop_round_breaker(breaker)

        assert lw.get_last_words() is None
        assert lw.get_last_words_breaker_state() == breaker.to_state()
        mw.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)
        assert lw.get_drop_round_breaker() == breaker
        mw.prepare_turn()
        assert lw.get_drop_round_breaker() == DropRoundBreakerState()

    def test_context_pressure_notification_is_preserved_only_across_retry(self) -> None:
        mw, lw = reminder_pair()
        mw.prepare_turn()

        assert lw.claim_context_pressure_notification()
        assert not lw.claim_context_pressure_notification()
        mw.prepare_turn(preserve_last_words=True)
        assert not lw.claim_context_pressure_notification()
        mw.prepare_turn()
        assert lw.claim_context_pressure_notification()


class TestDroppedRecordCatalogPointer:
    @staticmethod
    def _write_catalog(root: Path, count: int) -> SpillQuota:
        catalog = root / CATALOG_RELATIVE_PATH
        catalog.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "record_id": f"r{index}",
                "relative_path": f"compactions/dropped/turn001/{index:03d}_tool_{index:032x}.md",
                "turn": 1,
                "round": 1,
                "tool": "tool",
                "bytes": 10,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for index in range(count)
        ]
        catalog.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        return TestDroppedRecordCatalogPointer._quota_for_records(records)

    @staticmethod
    def _quota_for_records(records: list[dict[str, object]]) -> SpillQuota:
        quota = SpillQuota()
        quota.initialize(0, live_relative_paths=(str(record["relative_path"]) for record in records))
        return quota

    def test_pointer_present_iff_snapshot_and_read_tool_and_stable_within_turn(self, tmp_path: Path) -> None:
        quota = self._write_catalog(tmp_path, 2)
        mw = SystemReminderMiddleware(session_root=tmp_path, file_read_available=True, spill_quota=quota)
        with patch("chrys.service.context.compaction.spill._read_live_catalog", side_effect=AssertionError):
            mw.prepare_turn()
        first = mw._build_reminders()
        (tmp_path / CATALOG_RELATIVE_PATH).unlink()

        assert any("archived 2 records" in reminder for reminder in first)
        assert any((tmp_path / CATALOG_RELATIVE_PATH).resolve().as_posix() in reminder for reminder in first)
        assert all("tool calls" not in reminder and "per-turn manifest.md" not in reminder for reminder in first)
        assert mw._build_reminders() == first
        mw.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)
        assert mw._build_reminders() == first

        without_reader = SystemReminderMiddleware(session_root=tmp_path, file_read_available=False)
        self._write_catalog(tmp_path, 1)
        without_reader.prepare_turn()
        assert not any("Earlier context compaction" in reminder for reminder in without_reader._build_reminders())

    def test_pointer_rejects_symlinked_catalog_parent_after_reconciliation(self, tmp_path: Path) -> None:
        redirected = tmp_path / "redirected"
        redirected.mkdir()
        (redirected / CATALOG_RELATIVE_PATH.name).write_text("{}\n", encoding="utf-8")
        (tmp_path / CATALOG_RELATIVE_PATH.parent.parent).mkdir()
        dropped = tmp_path / CATALOG_RELATIVE_PATH.parent
        try:
            dropped.symlink_to(redirected, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        quota = SpillQuota()
        quota.initialize(0, live_relative_paths=("compactions/dropped/turn001/001_tool_a.md",))
        mw = SystemReminderMiddleware(session_root=tmp_path, file_read_available=True, spill_quota=quota)

        mw.prepare_turn()

        assert mw.sources.archive_pointer.record_count_state() == 1
        assert not any("Earlier context compaction" in reminder for reminder in mw._build_reminders())

    async def test_pointer_coexists_with_live_manifest_and_refresh_keeps_pointer(self, tmp_path: Path) -> None:
        quota = self._write_catalog(tmp_path, 1)
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=True, spill_quota=quota)
        mw.prepare_turn()
        context = ChatContext(client=None, messages=[_user("continue")], options=None)

        async def _deliver_and_refresh() -> None:
            for observer in context.request_message_observers:
                observer(context.messages)
            lw.set_last_words("[turn two note]")
            lw.append_manifest([_manifest_entry(2)])
            assert mw.refresh_last_words_reminder(cast("list[Message]", context.messages)) == 0

        await mw.process(context, _deliver_and_refresh)

        rendered = "\n".join(content.text or "" for content in context.messages[0].contents)
        assert "Earlier context compaction archived 1 record" in rendered
        assert "--- Dropped this turn" in rendered
        assert "[turn two note]" in rendered

    def test_restored_retry_excludes_active_manifest_records_from_cross_turn_pointer(self, tmp_path: Path) -> None:
        active_entry = _manifest_entry(2)
        catalog = tmp_path / CATALOG_RELATIVE_PATH
        catalog.parent.mkdir(parents=True)
        records = [
            {
                "record_id": "previous",
                "relative_path": "compactions/dropped/turn011/001_read_file_a.md",
                "turn": 11,
                "round": 1,
                "tool": "read_file",
                "bytes": 10,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "record_id": active_entry.record_id,
                "relative_path": active_entry.relative_path,
                "turn": active_entry.turn,
                "round": active_entry.round,
                "tool": active_entry.tool,
                "bytes": 10,
                "created_at": "2026-01-02T00:00:00+00:00",
            },
        ]
        catalog.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        quota = self._quota_for_records(records)
        mw, lw = reminder_pair(session_root=tmp_path, file_read_available=True, spill_quota=quota)
        restore_phase4_state(
            mw,
            lw,
            {
                "last_words": "[active note]",
                "last_words_manifest": [active_entry.to_state()],
            },
            available_relative_paths={active_entry.relative_path},
        )

        mw.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)

        pointer = next(item for item in mw._build_reminders() if "Earlier context compaction" in item)
        assert "archived 1 record from previous turns" in pointer
        last_words = "\n".join(lw.render())
        assert active_entry.relative_path.rsplit("/", 1)[-1] in last_words

    def test_restored_retry_reuses_persisted_pointer_before_current_turn_records(self, tmp_path: Path) -> None:
        active_entry = _manifest_entry(2)
        records = [
            {
                "record_id": "previous",
                "relative_path": "compactions/dropped/turn011/001_read_file_a.md",
                "turn": 11,
                "round": 1,
                "tool": "read_file",
                "bytes": 10,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "record_id": active_entry.record_id,
                "relative_path": active_entry.relative_path,
                "turn": active_entry.turn,
                "round": active_entry.round,
                "tool": active_entry.tool,
                "bytes": 10,
                "created_at": "2026-01-02T00:00:00+00:00",
            },
            {
                "record_id": "current-subagent",
                "relative_path": (
                    "compactions/sub_agents/explore/invocation-1/dropped/turn012/001_read_file_subagent.md"
                ),
                "turn": 12,
                "round": 1,
                "tool": "read_file",
                "bytes": 10,
                "created_at": "2026-01-02T00:00:01+00:00",
            },
        ]
        catalog = tmp_path / CATALOG_RELATIVE_PATH
        catalog.parent.mkdir(parents=True)
        catalog.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        mw, lw = reminder_pair(
            session_root=tmp_path,
            file_read_available=True,
            spill_quota=self._quota_for_records(records),
        )
        restore_phase4_state(
            mw,
            lw,
            {
                "last_words": "[active note]",
                "last_words_manifest": [active_entry.to_state()],
                CATALOG_POINTER_RECORD_COUNT_STATE_KEY: 1,
            },
            available_relative_paths={active_entry.relative_path},
        )

        mw.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)

        pointer = next(item for item in mw._build_reminders() if "Earlier context compaction" in item)
        assert "archived 1 record from previous turns" in pointer
        assert mw.sources.archive_pointer.record_count_state() == 1

    def test_restored_retry_suppresses_pointer_when_spill_storage_is_unavailable(self, tmp_path: Path) -> None:
        quota = self._write_catalog(tmp_path, 1)
        quota.disable_storage()
        mw, lw = reminder_pair(
            session_root=tmp_path,
            file_read_available=True,
            spill_quota=quota,
        )
        restore_phase4_state(mw, lw, {CATALOG_POINTER_RECORD_COUNT_STATE_KEY: 1})

        mw.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)

        assert mw.sources.archive_pointer.record_count_state() == 1
        assert not any("Earlier context compaction" in item for item in mw._build_reminders())

    def test_pointer_count_and_catalog_cover_main_assistant_and_subagent_records(self, tmp_path: Path) -> None:
        catalog = tmp_path / CATALOG_RELATIVE_PATH
        catalog.parent.mkdir(parents=True)
        records = [
            {
                "record_id": "assistant",
                "relative_path": "compactions/dropped/turn001/001_assistant_a.md",
                "turn": 1,
                "round": 1,
                "tool": "assistant",
                "bytes": 10,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "record_id": "subagent",
                "relative_path": "compactions/sub_agents/Explore/invocation/dropped/turn001/001_read_file_b.md",
                "turn": 1,
                "round": 1,
                "tool": "read_file",
                "bytes": 10,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        ]
        catalog.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        quota = self._quota_for_records(records)
        mw = SystemReminderMiddleware(session_root=tmp_path, file_read_available=True, spill_quota=quota)

        mw.prepare_turn()

        reminder = next(item for item in mw._build_reminders() if "Earlier context compaction" in item)
        assert "archived 2 records" in reminder
        assert catalog.resolve().as_posix() in reminder
        assert "tool calls" not in reminder

    async def test_pointer_is_resent_only_when_its_count_changes(self, tmp_path: Path) -> None:
        """The pointer is standing context: a moved session renders it at its own catalog without resending it."""
        root = tmp_path / "session"
        mw = SystemReminderMiddleware(
            session_root=root, file_read_available=True, spill_quota=self._write_catalog(root, 2)
        )
        mw.prepare_turn()
        opener = _user("first")
        first = await enrich_call(mw, [opener])

        pointer = _catalog_pointer_text(2, (root / CATALOG_RELATIVE_PATH).resolve().as_posix())
        assert [content.text for content in first[0].contents] == ["first", _wrap(pointer)]
        assert opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY] == [
            {"kind": "catalog", "text": pointer, "name": "archive"}
        ]

        moved_root = tmp_path / "moved"
        moved = SystemReminderMiddleware(
            session_root=moved_root, file_read_available=True, spill_quota=self._write_catalog(moved_root, 2)
        )
        moved.prepare_turn()
        history = [opener, Message(role="assistant", contents=["a"]), _user("second")]
        same_count = await enrich_call(moved, history)

        moved_catalog = (moved_root / CATALOG_RELATIVE_PATH).resolve().as_posix()
        assert [content.text for content in same_count[0].contents] == [
            "first",
            _wrap(_catalog_pointer_text(2, moved_catalog)),
        ]
        assert [content.text for content in same_count[2].contents] == ["second"]

        grown = SystemReminderMiddleware(
            session_root=moved_root, file_read_available=True, spill_quota=self._write_catalog(moved_root, 3)
        )
        grown.prepare_turn()
        more = await enrich_call(grown, [*history, Message(role="assistant", contents=["b"]), _user("third")])

        assert [content.text for content in more[-1].contents] == [
            "third",
            _wrap(_catalog_pointer_text(3, moved_catalog)),
        ]
