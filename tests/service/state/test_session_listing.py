# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Paged session listing: a fixed newest-first snapshot, surface filters, and the persisted catalog behind it."""

from __future__ import annotations

import asyncio
import errno
import json
import shutil
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import chrys.service.state.session_catalog as catalog_module
import chrys.service.state.store as store_module
import chrys.service.trajectory.tombstone as tombstone_module
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.state.session_catalog import CATALOG_DERIVATION_VERSION
from chrys.service.state.session_listing import SessionListing, SessionListingEntry, page_slice
from chrys.service.state.store import ChatSessionMeta, JsonFileStateStore, SessionMeta
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.trajectory.tombstone import INTENT_SUFFIX, DeleteOutcome, DeleteResult, tombstones_dir
from chrys.service.workflows import history as history_module
from chrys.service.workflows.history import WorkflowRunMeta, WorkflowRunRead
from tests.service.state._store_helpers import write_legacy_envelope
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.workflow_history import record_workflow_run, workflow_state

ALL = frozenset(SessionSurface)
TUI = frozenset({SessionSurface.TUI})


def _at(hour: int) -> str:
    return datetime(2026, 9, 1, hour, tzinfo=UTC).isoformat()


def _entry(session_id: str, hour: int, surface: SessionSurface = SessionSurface.TUI) -> SessionListingEntry:
    at = datetime(2026, 9, 1, hour, tzinfo=UTC)
    meta = ChatSessionMeta(
        session_id=session_id,
        created_at=at,
        updated_at=at,
        agent_profile="Code",
        agent_display_name="Code",
        message_count=1,
    )
    return SessionListingEntry(session_id, at, surface, Path(session_id), meta=meta)


def _ids(listing: SessionListing, surfaces: frozenset[SessionSurface] = ALL) -> list[str]:
    return [entry.session_id for entry in listing.filtered(surfaces)]


def _catalog_ids(root: Path) -> set[str]:
    path = root / ".cache" / "session_catalog.json"
    return set(json.loads(path.read_text(encoding="utf-8"))["sessions"]) if path.exists() else set()


def _count_parses(monkeypatch: pytest.MonkeyPatch, store: JsonFileStateStore) -> list[str]:
    parsed: list[str] = []
    parse = store._session_meta_from_envelope

    def counting_parse(envelope: dict[str, Any], *, size_bytes: int) -> SessionMeta:
        parsed.append(envelope["meta"]["session_id"])
        return parse(envelope, size_bytes=size_bytes)

    monkeypatch.setattr(store, "_session_meta_from_envelope", counting_parse)
    return parsed


def _count_run_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Reads of a run's files for its listing time (cached in the catalog under the run files' signatures)."""
    reads: list[Path] = []
    read_workflow_run = store_module.read_workflow_run

    def counting_read(directory: Path, *, active: bool) -> WorkflowRunRead:
        reads.append(directory)
        return read_workflow_run(directory, active=active)

    monkeypatch.setattr(store_module, "read_workflow_run", counting_read)
    return reads


def _count_summary_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Live reads of a run's summary (title, input, status), never cached: the status depends on the active lock."""
    reads: list[Path] = []
    read_workflow_meta = store_module.read_workflow_meta

    def counting_read(directory: Path, *, active: bool) -> WorkflowRunMeta | None:
        reads.append(directory)
        return read_workflow_meta(directory, active=active)

    monkeypatch.setattr(store_module, "read_workflow_meta", counting_read)
    return reads


def _latest_run_id(store: JsonFileStateStore, session_id: str) -> str:
    (run_id,) = (path.name for path in (store.session_dir(session_id) / "workflows").iterdir())
    return run_id


def _pinned_delete(session_dir: Path, *, sessions_root: Path, path_reused: bool = False) -> DeleteResult:
    """A delete whose folder is pinned: it records an intent and leaves ``session.json`` behind."""
    graveyard = tombstones_dir(sessions_root)
    graveyard.mkdir(exist_ok=True)
    plant_owner_only_bytes(graveyard / f"{session_dir.name}{INTENT_SUFFIX}", session_dir.name.encode())
    return DeleteResult(DeleteOutcome.INTENT_RECORDED)


async def _chat(store: JsonFileStateStore, session_id: str, *texts: str, surface: SessionSurface | None = None) -> None:
    await store.save_session(
        session_id,
        {"messages": [Message("user", [text]) for text in texts or ("hi",)], "compressed_msgs": []},
        last_surface=surface,
    )


async def _workflow(store: JsonFileStateStore, tmp_path: Path, *, runs: int = 1) -> str:
    """A workflow session (run records need a UUID session id) with *runs* runs; returns its id."""
    session_id = str(uuid4())
    run_id = uuid4().hex if runs else ""
    state = workflow_state(tmp_path, run_count=runs, latest_run_id=run_id)
    await store.save_workflow_session(session_id, WorkflowSessionState.decode(state))
    if runs:
        directory = store.session_dir(session_id) / "workflows" / run_id
        await record_workflow_run(directory, session_id=session_id, title="Review", outcome="completed")
    return session_id


# ---------------------------------------------------------------------------
# The snapshot itself
# ---------------------------------------------------------------------------


def test_pages_are_clamped_and_counted_over_the_filtered_snapshot() -> None:
    listing = SessionListing(
        "chat",
        tuple(_entry(f"s{hour}", hour, SessionSurface.CLI if hour % 2 else SessionSurface.TUI) for hour in range(7)),
    )

    assert listing.page_count(ALL, page_size=3) == 3
    assert listing.page_count(TUI, page_size=3) == 2
    assert listing.page_count(frozenset(), page_size=3) == 1
    entries, page, count, total = page_slice(listing, TUI, 9, page_size=3)
    assert ([entry.session_id for entry in entries], page, count, total) == (["s6"], 2, 2, 4)
    entries, page, _count, _total = page_slice(listing, TUI, 0, page_size=3)
    assert ([entry.session_id for entry in entries], page) == (["s0", "s2", "s4"], 1)
    assert page_slice(listing, frozenset(), 1, page_size=3) == ((), 1, 1, 0)

    assert _ids(listing.without("s2"), TUI) == ["s0", "s4", "s6"]
    with pytest.raises(ValueError, match="page_size"):
        listing.page_count(ALL, page_size=0)


async def test_listing_is_newest_first_with_id_tie_break_and_filters_by_surface(tmp_path: Path) -> None:
    for session_id, hour, surface in (("a", 10, "cli"), ("b", 11, None), ("c", 11, "acp"), ("d", 9, "tui")):
        meta: dict[str, object] = {"updated_at": _at(hour)}
        if surface is not None:
            meta["last_surface"] = surface
        write_legacy_envelope(tmp_path / session_id / "session.json", session_id, **meta)
    store = JsonFileStateStore(tmp_path)

    listing = await store.open_session_listing(kind="chat")

    assert _ids(listing) == ["c", "b", "a", "d"]
    # A session saved before surfaces were recorded counts as TUI.
    assert _ids(listing, TUI) == ["b", "d"]
    assert _ids(listing, frozenset({SessionSurface.CLI, SessionSurface.ACP})) == ["c", "a"]
    assert await store.open_session_listing(kind="workflow") == SessionListing("workflow", ())


async def test_workflow_sessions_list_by_their_latest_run_and_only_with_one(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    saved_first = await _workflow(store, tmp_path)
    await _workflow(store, tmp_path, runs=0)
    await _chat(store, "chat")
    run_last = await _workflow(store, tmp_path)
    # Re-saving the session state moves its ``updated_at`` but not its run.
    await store.save_workflow_session(saved_first, await store.load_workflow_session(saved_first))

    listing = await store.open_session_listing(kind="workflow")
    page = await store.load_session_page(listing, surfaces=ALL)

    assert _ids(listing) == [run_last, saved_first]
    assert [meta.session_id for meta in page.metas] == [run_last, saved_first]
    assert all(meta.latest_run is not None and meta.latest_run.status == "completed" for meta in page.metas)
    assert listing.entries[0].listed_at == page.metas[0].latest_run.updated_at
    # The snapshot already knows what each row shows of its run, so the browser can search every session by it.
    assert [entry.meta.latest_run for entry in listing.entries] == [meta.latest_run for meta in page.metas]
    assert listing.entries[0].meta.latest_run.title == "Review"
    assert listing.entries[0].meta.latest_run.input_text == "workflow input only"


async def test_pages_measure_folder_size_only_for_their_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonFileStateStore(tmp_path)
    for index in range(3):
        await _chat(store, f"s{index}", surface=SessionSurface.TUI)
    measured: list[str] = []
    dir_size = store_module._dir_size

    def counting_dir_size(path: Path) -> int:
        measured.append(path.name)
        return dir_size(path)

    monkeypatch.setattr(store_module, "_dir_size", counting_dir_size)

    listing = await store.open_session_listing(kind="chat")
    assert measured == []
    first = await store.load_session_page(listing, surfaces=TUI, page_size=2)
    second = await store.load_session_page(listing, surfaces=TUI, page=2, page_size=2)

    assert (first.page, first.page_count, first.total, second.page) == (1, 2, 3, 2)
    assert [meta.session_id for meta in first.metas + second.metas] == ["s2", "s1", "s0"]
    assert sorted(measured) == ["s0", "s1", "s2"]
    assert all(meta.size_bytes > 0 for meta in first.metas + second.metas)


async def test_a_page_shows_current_contents_in_snapshot_order(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    for session_id in ("old", "mid", "new"):
        await _chat(store, session_id)
    listing = await store.open_session_listing(kind="chat")
    await _chat(store, "old", "hi", "now the newest", surface=SessionSurface.CLI)
    await store.delete_session("mid")

    page = await store.load_session_page(listing, surfaces=ALL)

    assert [meta.session_id for meta in page.metas] == ["new", "old"]
    assert page.metas[1].message_count == 2 and page.metas[1].last_surface is SessionSurface.CLI
    # The entry keeps what the snapshot read, prompts included.
    old_entry = next(entry for entry in listing.entries if entry.session_id == "old")
    assert (old_entry.meta.message_count, old_entry.meta.user_prompt_search_text) == (1, "hi")
    assert "now the newest" in page.metas[1].user_prompt_search_text
    assert page.total == 3
    assert (await store.load_session_page(listing.without("mid"), surfaces=ALL)).total == 2


async def test_legacy_flat_file_sessions_are_listed_and_paged(tmp_path: Path) -> None:
    write_legacy_envelope(tmp_path / "flat.json", "legacy-id", updated_at=_at(8), message_count=1)
    store = JsonFileStateStore(tmp_path)
    await _chat(store, "folder")

    listing = await store.open_session_listing(kind="chat")
    page = await store.load_session_page(listing, surfaces=TUI)

    assert _ids(listing) == ["folder", "legacy-id"]
    assert listing.entries[1].legacy and listing.entries[1].source == tmp_path / "flat.json"
    assert listing.entries[1].meta.session_id == "legacy-id" and listing.entries[1].meta.message_count == 1
    assert [meta.session_id for meta in page.metas] == ["folder", "legacy-id"]
    assert "legacy-id" not in _catalog_ids(tmp_path)


async def test_a_caller_editing_a_listed_meta_never_changes_later_listings(tmp_path: Path) -> None:
    """The listing caches hand out copies — to snapshot entries and pages alike —
    nested lists included, for folder, legacy and workflow sessions."""
    write_legacy_envelope(tmp_path / "flat.json", "legacy-id", updated_at=_at(8), message_count=1)
    store = JsonFileStateStore(tmp_path)
    await _chat(store, "folder")
    workflow = await _workflow(store, tmp_path)

    def edit(metas: Collection[SessionMeta]) -> None:
        for meta in metas:
            meta.title = "edited"
            meta.working_dirs.append("edited")
            if isinstance(meta, ChatSessionMeta):
                meta.agent_profile_history.append("edited")

    # The first round fills the caches; the second edits what they hand out.
    for _ in range(2):
        edit(await store.list_sessions())
        for kind in ("chat", "workflow"):
            listing = await store.open_session_listing(kind=kind)
            edit([entry.meta for entry in listing.entries])
            edit((await store.load_session_page(listing, surfaces=ALL)).metas)

    metas = await store.list_sessions()
    assert sorted(meta.session_id for meta in metas) == sorted(["folder", "legacy-id", workflow])
    for meta in metas:
        assert meta.title != "edited"
        assert "edited" not in meta.working_dirs
        if isinstance(meta, ChatSessionMeta):
            assert "edited" not in meta.agent_profile_history


async def test_a_legacy_session_migrated_since_the_snapshot_keeps_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading a legacy session moves its flat file into its folder; the row follows it, deletes included."""
    store = JsonFileStateStore(tmp_path)
    write_legacy_envelope(store._legacy_path("legacy-id"), "legacy-id", updated_at=_at(8), message_count=1)
    await _chat(store, "folder")
    listing = await store.open_session_listing(kind="chat")
    assert listing.entries[1].legacy

    assert await store.load_session("legacy-id") is not None
    assert not store._legacy_path("legacy-id").exists()
    page = await store.load_session_page(listing, surfaces=TUI)

    assert [meta.session_id for meta in page.metas] == ["folder", "legacy-id"]
    assert page.metas[1].size_bytes == store_module._dir_size(store.session_dir("legacy-id")) > 0
    monkeypatch.setattr(tombstone_module, "delete_session_directory", _pinned_delete)
    await store.delete_session("legacy-id")
    assert (store.session_dir("legacy-id") / "session.json").exists()
    page = await store.load_session_page(listing, surfaces=TUI)
    assert [meta.session_id for meta in page.metas] == ["folder"]


# ---------------------------------------------------------------------------
# The persisted catalog behind it
# ---------------------------------------------------------------------------


async def test_another_process_lists_from_the_catalog_and_reparses_only_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every listing merges, however few its new entries.
    monkeypatch.setattr(store_module, "SESSION_CATALOG_COMMIT_INTERVAL_SECONDS", 0.0)
    writer = JsonFileStateStore(tmp_path)
    for session_id in ("a", "b", "c"):
        await _chat(writer, session_id, surface=SessionSurface.CLI)
    workflow = await _workflow(writer, tmp_path)
    first = await writer.open_session_listing(kind="chat")
    await writer.open_session_listing(kind="workflow")
    assert _catalog_ids(tmp_path) == {"a", "b", "c", writer.session_dir(workflow).name}

    reader = JsonFileStateStore(tmp_path)
    parsed = _count_parses(monkeypatch, reader)
    run_reads = _count_run_reads(monkeypatch)
    summary_reads = _count_summary_reads(monkeypatch)

    assert await reader.open_session_listing(kind="chat") == first
    await reader.open_session_listing(kind="workflow")
    assert (parsed, run_reads) == ([], [])
    # Only the summary each workflow row shows is read: once per workflow session, never for a chat listing.
    assert summary_reads == [writer.session_dir(workflow) / "workflows" / _latest_run_id(writer, workflow)]

    await _chat(writer, "b", "hi", "changed")
    listing = await reader.open_session_listing(kind="chat")
    assert parsed == ["b"]
    assert _ids(listing)[0] == "b"


async def test_sessions_read_from_a_sidecar_or_backup_are_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _chat(store, "crashed", surface=SessionSurface.CLI)
    store.save_recovery_session(
        "crashed",
        {"messages": [Message("user", ["hi"]), Message("user", ["cut short"])], "compressed_msgs": []},
        last_surface=SessionSurface.ACP,
    )
    await _chat(store, "healed")
    await _chat(store, "healed", "hi", "again")
    (store.session_dir("healed") / "session.json").write_text("{", encoding="utf-8")

    listing = await store.open_session_listing(kind="chat")

    assert {entry.session_id: entry.surface for entry in listing.entries} == {
        "crashed": SessionSurface.ACP,
        "healed": SessionSurface.TUI,
    }
    assert _catalog_ids(tmp_path) == set()
    parsed = _count_parses(monkeypatch, store)
    await store.open_session_listing(kind="chat")
    # The sidecar is parsed every time; the healed primary is now cacheable.
    assert sorted(parsed) == ["crashed", "healed"]
    assert _catalog_ids(tmp_path) == {"healed"}


async def test_a_checkpoint_written_during_the_scan_is_not_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar that appears after the listing looked for one is a live turn's checkpoint."""
    writer, reader = JsonFileStateStore(tmp_path), JsonFileStateStore(tmp_path)
    await _chat(writer, "live", surface=SessionSurface.CLI)
    resolve = reader._resolve_effective_envelope_with_source
    checkpoints: list[str] = []

    def checkpoint_first(session_id: str, *, prefer_recovery: bool = False) -> tuple[dict[str, Any] | None, str]:
        if not checkpoints:
            checkpoints.append(session_id)
            writer.save_recovery_session(
                "live",
                {"messages": [Message("user", ["hi"]), Message("user", ["in flight"])], "compressed_msgs": []},
                last_surface=SessionSurface.ACP,
            )
        return resolve(session_id, prefer_recovery=prefer_recovery)

    monkeypatch.setattr(reader, "_resolve_effective_envelope_with_source", checkpoint_first)
    with FileLock(writer.active_lock_path("live"), timeout=1.0):
        listing = await reader.open_session_listing(kind="chat")

    assert checkpoints == ["live"]
    assert [(entry.session_id, entry.surface) for entry in listing.entries] == [("live", SessionSurface.CLI)]
    assert _catalog_ids(tmp_path) == set()


async def test_deleted_vanished_and_reset_sessions_leave_the_catalog(tmp_path: Path) -> None:
    """Entries hold prompt excerpts: they go with the session, and pruning never waits for the commit interval."""
    store = JsonFileStateStore(tmp_path)
    for session_id in ("keep", "delete", "vanish", "reset"):
        await _chat(store, session_id, f"private prompt of {session_id}")
    await store.open_session_listing(kind="chat")
    assert _catalog_ids(tmp_path) == {"keep", "delete", "vanish", "reset"}

    await store.delete_session("delete")
    assert _catalog_ids(tmp_path) == {"keep", "vanish", "reset"}
    shutil.rmtree(store.session_dir("vanish"))
    # What resetting a session with a recorded trajectory leaves: the folder without its session files.
    for name in ("session.json", "session.json.bak"):
        (store.session_dir("reset") / name).unlink(missing_ok=True)
    listing = await store.open_session_listing(kind="chat")

    assert _ids(listing) == ["keep"]
    assert _catalog_ids(tmp_path) == {"keep"}
    assert set(store._meta_cache) == {"keep"}
    persisted = (tmp_path / ".cache" / "session_catalog.json").read_text(encoding="utf-8")
    assert "private prompt of keep" in persisted
    assert not any(f"private prompt of {session_id}" in persisted for session_id in ("delete", "vanish", "reset"))


async def test_a_logically_deleted_session_never_returns_to_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete whose folder is pinned records an intent and can leave ``session.json`` behind."""
    writer, reader = JsonFileStateStore(tmp_path), JsonFileStateStore(tmp_path)
    for session_id in ("keep", "gone"):
        await _chat(writer, session_id, f"private prompt of {session_id}")
    await reader.open_session_listing(kind="chat")
    await _chat(writer, "gone", "private prompt of gone", "changed")
    # Within the commit interval: the reader keeps its fresher entry pending.
    snapshot = await reader.open_session_listing(kind="chat")
    assert reader._catalog_pending == {"gone"}

    monkeypatch.setattr(tombstone_module, "delete_session_directory", _pinned_delete)
    await writer.delete_session("gone")
    assert (writer.session_dir("gone") / "session.json").exists()
    assert _catalog_ids(tmp_path) == {"keep"}

    page = await reader.load_session_page(snapshot, surfaces=ALL)
    assert [meta.session_id for meta in page.metas] == ["keep"]
    monkeypatch.setattr(store_module, "SESSION_CATALOG_COMMIT_INTERVAL_SECONDS", 0.0)
    # A commit that took its updates before the delete still drops the entry.
    await asyncio.to_thread(reader._commit_catalog)
    assert _catalog_ids(tmp_path) == {"keep"}
    assert _ids(await reader.open_session_listing(kind="chat")) == ["keep"]
    assert set(reader._meta_cache) == {"keep"}
    assert "private prompt of gone" not in (tmp_path / ".cache" / "session_catalog.json").read_text(encoding="utf-8")


async def test_a_catalog_removal_that_failed_is_retried_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    for session_id in ("keep", "gone"):
        await _chat(store, session_id, f"private prompt of {session_id}")
    await store.open_session_listing(kind="chat")
    remove = store._catalog.remove
    attempts: list[set[str]] = []

    def locked_once(short_ids: Collection[str]) -> bool:
        attempts.append(set(short_ids))
        return False if len(attempts) == 1 else remove(short_ids)

    monkeypatch.setattr(store._catalog, "remove", locked_once)
    await store.delete_session("gone")
    assert _catalog_ids(tmp_path) == {"keep", "gone"}

    assert _ids(await store.open_session_listing(kind="chat")) == ["keep"]
    assert _catalog_ids(tmp_path) == {"keep"}
    await store.open_session_listing(kind="chat")
    assert attempts == [{"gone"}, {"gone"}]


async def test_a_scan_eviction_the_catalog_lock_stopped_is_retried_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once evicted from memory, a gone session is found by no later scan: its removal must stay owed."""
    monkeypatch.setattr(catalog_module, "SESSION_CATALOG_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    for session_id in ("keep", "reset"):
        await _chat(store, session_id, f"private prompt of {session_id}")
    await store.open_session_listing(kind="chat")
    for name in ("session.json", "session.json.bak"):
        (store.session_dir("reset") / name).unlink(missing_ok=True)

    with FileLock(store._catalog.lock_path, timeout=1.0):
        assert _ids(await store.open_session_listing(kind="chat")) == ["keep"]
    assert _catalog_ids(tmp_path) == {"keep", "reset"}
    assert store._catalog_removals_owed == {"reset"}

    assert _ids(await store.open_session_listing(kind="chat")) == ["keep"]
    assert _catalog_ids(tmp_path) == {"keep"}
    assert store._catalog_removals_owed == set()
    assert "private prompt of reset" not in (tmp_path / ".cache" / "session_catalog.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("unreadable", ["read_run_header", "read_run_terminal"])
async def test_a_run_read_an_io_error_shaped_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    """Kept, it would hide the session (or date it by its start) until its run files change."""
    store = JsonFileStateStore(tmp_path)
    session_id = await _workflow(store, tmp_path)
    reads = _count_run_reads(monkeypatch)

    def failing(_directory: Path) -> object:
        raise OSError(errno.EIO, "I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(history_module, unreadable, failing)
        listed = _ids(await store.open_session_listing(kind="workflow"))
    assert listed == ([] if unreadable == "read_run_header" else [session_id])
    for _ in range(2):
        assert _ids(await store.open_session_listing(kind="workflow")) == [session_id]
    assert len(reads) == 2
    assert _ids(await JsonFileStateStore(tmp_path).open_session_listing(kind="workflow")) == [session_id]


async def test_a_trickle_of_new_entries_waits_for_the_commit_interval_but_a_batch_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog is rewritten whole, so the session each turn changes must not rewrite it on every listing."""
    monkeypatch.setattr(store_module, "SESSION_CATALOG_EAGER_COMMIT_ENTRIES", 3)
    store = JsonFileStateStore(tmp_path)
    for session_id in ("a", "b"):
        await _chat(store, session_id)
    await store.open_session_listing(kind="chat")
    catalog = tmp_path / ".cache" / "session_catalog.json"
    first = catalog.read_bytes()

    await _chat(store, "a", "hi", "changed")
    await _chat(store, "c")
    await store.open_session_listing(kind="chat")
    assert catalog.read_bytes() == first

    await _chat(store, "d")
    await store.open_session_listing(kind="chat")
    assert _catalog_ids(tmp_path) == {"a", "b", "c", "d"}
    assert JsonFileStateStore(tmp_path)._catalog.load() == store._meta_cache


@pytest.mark.parametrize("kind", ["chat", "workflow"])
async def test_a_malformed_envelope_is_listed_but_never_cataloged(tmp_path: Path, kind: str) -> None:
    """Its entry would be dropped by every load, so recording it would only rewrite the catalog in each process.

    A workflow session's entry is queued again once its run's listing time is known.
    """
    store = JsonFileStateStore(tmp_path)
    if kind == "chat":
        odd, fine = "odd", "fine"
        for session_id in (odd, fine):
            await _chat(store, session_id)
    else:
        odd, fine = await _workflow(store, tmp_path), await _workflow(store, tmp_path)
    primary = store.session_dir(odd) / "session.json"
    envelope = json.loads(primary.read_text(encoding="utf-8"))
    envelope["meta"]["title"] = None
    primary.write_text(json.dumps(envelope), encoding="utf-8")

    assert set(_ids(await store.open_session_listing(kind=kind))) == {odd, fine}
    assert _catalog_ids(tmp_path) == {store.session_dir(fine).name}
    assert store._catalog_pending == set()
    if kind == "workflow":
        assert store._meta_cache[store.session_dir(odd).name].run is not None


async def test_a_catalog_that_could_not_be_read_is_read_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = JsonFileStateStore(tmp_path)
    await _chat(writer, "a")
    await writer.open_session_listing(kind="chat")
    reader = JsonFileStateStore(tmp_path)
    load = reader._catalog.load
    loads: list[bool] = []

    def flaky_load():
        loads.append(True)
        return None if len(loads) == 1 else load()

    monkeypatch.setattr(reader._catalog, "load", flaky_load)
    await asyncio.to_thread(reader._refresh_catalog)
    assert reader._meta_cache == {}
    await asyncio.to_thread(reader._refresh_catalog)

    assert len(loads) == 2
    assert set(reader._meta_cache) == {"a"}


async def test_a_corrupt_catalog_is_rebuilt_and_a_newer_one_left_alone(tmp_path: Path) -> None:
    catalog = tmp_path / ".cache" / "session_catalog.json"
    catalog.parent.mkdir(parents=True)
    plant_owner_only_bytes(catalog, b"{corrupt")
    store = JsonFileStateStore(tmp_path)
    await _chat(store, "s")

    assert _ids(await store.open_session_listing(kind="chat")) == ["s"]
    assert _catalog_ids(tmp_path) == {"s"}

    catalog.unlink()
    newer = json.dumps({"version": CATALOG_DERIVATION_VERSION + 1, "sessions": {}}).encode("utf-8")
    plant_owner_only_bytes(catalog, newer)
    other = JsonFileStateStore(tmp_path)
    await _chat(other, "t")

    assert _ids(await other.open_session_listing(kind="chat")) == ["t", "s"]
    assert catalog.read_bytes() == newer


async def test_listing_the_legacy_way_still_reports_sizes_and_feeds_the_catalog(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await _chat(store, "s", surface=SessionSurface.ACP)

    listed = await store.list_sessions()

    assert listed[0].size_bytes == store_module._dir_size(store.session_dir("s")) > 0
    assert listed[0].last_surface is SessionSurface.ACP
    assert _catalog_ids(tmp_path) == {"s"}
