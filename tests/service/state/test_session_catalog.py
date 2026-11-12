# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The persisted session-listing catalog: its v2 record format, versioning and locked merges."""

from __future__ import annotations

import dataclasses
import errno
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chrys.service.state.session_catalog as catalog_module
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.platform.files import SecureFileError
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.context.providers.history import TURN_INDEX_KEY
from chrys.service.session.runtime_metadata import TOTAL_SESSION_TOKENS_KEY
from chrys.service.state._session_meta import ChatSessionMeta, WorkflowSessionMeta
from chrys.service.state.serializers import serialize_message, serialize_state
from chrys.service.state.session_catalog import (
    CATALOG_DERIVATION_VERSION,
    CatalogEntry,
    RunListing,
    RunListingKey,
    SessionCatalogFile,
    decode_entry,
    encode_entry,
    encode_meta,
    is_recordable,
)
from chrys.service.state.store import JsonFileStateStore
from tests.support.secure_files import plant_owner_only_bytes

_CREATED = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
_UPDATED = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)
_RUN_AT = datetime(2026, 9, 3, 10, 45, tzinfo=UTC)

_CHAT = ChatSessionMeta(
    session_id="chat-1",
    created_at=_CREATED,
    updated_at=_UPDATED,
    total_tokens=42,
    primary_cwd="/work",
    working_dirs=["/work", "/lib"],
    title="Fix the parser",
    custom_title="Parser",
    generated_title="Parser fix",
    size_bytes=999,
    app_version="0.27.2",
    schema_version=3,
    os_name="darwin",
    arch="arm64",
    last_surface=SessionSurface.CLI,
    agent_profile="Code",
    agent_display_name="Code",
    message_count=4,
    turn_count=2,
    agent_profile_id="code",
    agent_profile_history=["Code"],
    agent_profile_fingerprint="fp-agent",
    model_provider="openai",
    model_api_style="responses",
    model_id="gpt",
    model_profile_id="p1",
    model_base_url="https://example.invalid",
    model_profile_fingerprint="fp-model",
    service_session_id="svc",
    parent_session_id="parent",
    user_prompt_search_text="fix the parser",
)
_CHAT_RECORD = {
    "signature": [11, 22, 33],
    "meta": {
        "kind": "chat",
        "session_id": "chat-1",
        "created_at": "2026-09-01T08:00:00+00:00",
        "updated_at": "2026-09-02T09:30:00+00:00",
        "total_tokens": 42,
        "primary_cwd": "/work",
        "working_dirs": ["/work", "/lib"],
        "title": "Fix the parser",
        "custom_title": "Parser",
        "generated_title": "Parser fix",
        "app_version": "0.27.2",
        "schema_version": 3,
        "os_name": "darwin",
        "arch": "arm64",
        "last_surface": "cli",
        "agent_profile": "Code",
        "agent_display_name": "Code",
        "message_count": 4,
        "turn_count": 2,
        "agent_profile_id": "code",
        "agent_profile_history": ["Code"],
        "agent_profile_fingerprint": "fp-agent",
        "model_provider": "openai",
        "model_api_style": "responses",
        "model_id": "gpt",
        "model_profile_id": "p1",
        "model_base_url": "https://example.invalid",
        "model_profile_fingerprint": "fp-model",
        "service_session_id": "svc",
        "parent_session_id": "parent",
        "user_prompt_search_text": "fix the parser",
    },
}
_WORKFLOW = WorkflowSessionMeta(
    session_id="flow-1",
    created_at=_CREATED,
    updated_at=_UPDATED,
    title="Review",
    last_surface=None,
    workflow_id="review",
    run_count=2,
    latest_run_id="run-2",
)
_WORKFLOW_RECORD = {
    "signature": [1, 2, 3],
    "meta": {
        "kind": "workflow",
        "session_id": "flow-1",
        "created_at": "2026-09-01T08:00:00+00:00",
        "updated_at": "2026-09-02T09:30:00+00:00",
        "total_tokens": 0,
        "primary_cwd": "",
        "working_dirs": [],
        "title": "Review",
        "custom_title": "",
        "generated_title": "",
        "app_version": "",
        "schema_version": 0,
        "os_name": "",
        "arch": "",
        "last_surface": None,
        "workflow_id": "review",
        "run_count": 2,
        "latest_run_id": "run-2",
    },
    "run": {"run_id": "run-2", "header": [4, 5, 6], "events": None, "listed_at": "2026-09-03T10:45:00+00:00"},
}
_WORKFLOW_ENTRY = CatalogEntry((1, 2, 3), _WORKFLOW, RunListing(RunListingKey("run-2", (4, 5, 6), None), _RUN_AT))


def _always(*_args: object) -> bool:
    return True


def _plant(catalog: SessionCatalogFile, payload: bytes) -> None:
    """Replace the catalog file (planting appends)."""
    catalog.path.parent.mkdir(parents=True, exist_ok=True)
    catalog.path.unlink(missing_ok=True)
    plant_owner_only_bytes(catalog.path, payload)


def _write_raw(catalog: SessionCatalogFile, payload: object) -> None:
    _plant(catalog, json.dumps(payload).encode("utf-8"))


def _read_raw(catalog: SessionCatalogFile) -> dict:
    return json.loads(catalog.path.read_text(encoding="utf-8"))


def test_v2_records_are_pinned() -> None:
    """Golden v2 records: a change here must bump ``CATALOG_DERIVATION_VERSION``."""
    assert CATALOG_DERIVATION_VERSION == 2
    assert encode_entry(CatalogEntry((11, 22, 33), _CHAT)) == _CHAT_RECORD
    assert encode_entry(_WORKFLOW_ENTRY) == _WORKFLOW_RECORD
    # Size and run status are never recorded; everything else round-trips.
    assert decode_entry(_CHAT_RECORD) == CatalogEntry((11, 22, 33), dataclasses.replace(_CHAT, size_bytes=0))
    assert decode_entry(_WORKFLOW_RECORD) == _WORKFLOW_ENTRY


_LONG_PROMPT = "error " * 400 + "please fix the parser"


def _derivation_envelope() -> dict:
    def turn(index: int) -> Message:
        return Message(
            "user",
            ["turn"],
            additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, TURN_INDEX_KEY: index},
        )

    state = serialize_state(
        {
            "messages": [
                turn(3),
                Message("user", ["  Fix   the\nparser ", "second part"]),
                Message("assistant", ["done"]),
                Message("user", ["also check tests"], additional_properties={HistoryMarkerKind.INJECTED_KEY: True}),
                Message("user", ["continue"], additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True}),
                turn(4),
                Message("user", [_LONG_PROMPT]),
                Message("user", ["  Fix   the\nparser ", "second part"]),
            ],
            "compressed_msgs": [],
            "turn_counter": 2,
            TOTAL_SESSION_TOKENS_KEY: 1234,
        }
    )
    state["compressed_msgs"] = [
        {"turn_range": [1, 5], "messages": [serialize_message(Message("user", ["archived prompt"]))]}
    ]
    meta = {"session_id": "chat-1", "created_at": _CREATED.isoformat(), "updated_at": _UPDATED.isoformat()}
    return {"meta": {**meta, "message_count": 4}, "state": state}


def test_v2_derivation_is_pinned() -> None:
    """Golden v2 derived values: changing how any is computed must bump ``CATALOG_DERIVATION_VERSION``.

    Recorded entries outlive processes, so a derivation change without a bump
    would serve the old values for every unchanged session indefinitely.
    """
    assert CATALOG_DERIVATION_VERSION == 2
    collapsed = " ".join(_LONG_PROMPT.split())
    record = encode_meta(JsonFileStateStore._session_meta_from_envelope(_derivation_envelope(), size_bytes=0))

    assert (record["total_tokens"], record["turn_count"], record["message_count"]) == (1234, 5, 4)
    assert record["user_prompt_search_text"] == "\n".join(
        ["archived prompt", "Fix the parser", "second part", "also check tests", collapsed[:1000], collapsed[-1000:]]
    )


@pytest.mark.parametrize("meta_class", [ChatSessionMeta, WorkflowSessionMeta])
def test_every_listing_field_is_recorded_or_deliberately_unrecorded(meta_class: type) -> None:
    """A new SessionMeta field changes what the catalog derives: extend the golden records and bump the version."""
    golden = _CHAT_RECORD if meta_class is ChatSessionMeta else _WORKFLOW_RECORD
    fields = {field.name for field in dataclasses.fields(meta_class)}
    assert fields - {"kind", "size_bytes", "latest_run"} == set(golden["meta"]) - {"kind"}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("meta", "kind"), "other"),
        (("meta", "message_count"), "4"),
        (("meta", "message_count"), True),
        (("meta", "working_dirs"), ["/work", 3]),
        (("meta", "updated_at"), "yesterday"),
        (("meta", "title"), None),
        (("signature",), [1, 2]),
        (("signature",), None),
        (("run",), {"run_id": 3}),
        (("run",), {"run_id": "r", "listed_at": 5}),
    ],
    ids=lambda item: ".".join(item) if isinstance(item, tuple) else repr(item),
)
def test_malformed_records_are_rejected(path: tuple[str, ...], value: object) -> None:
    record = json.loads(json.dumps(_CHAT_RECORD))
    target = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises((KeyError, ValueError, TypeError)):
        decode_entry(record)


def test_an_entry_that_would_not_read_back_is_not_recordable() -> None:
    """A malformed envelope (``title: null``) is served, but recording it would be dropped by every load."""
    assert is_recordable(CatalogEntry((1, 2, 3), _CHAT))
    assert is_recordable(_WORKFLOW_ENTRY)
    assert not is_recordable(CatalogEntry((1, 2, 3), dataclasses.replace(_CHAT, title=None)))  # type: ignore[arg-type]
    assert not is_recordable(CatalogEntry((1, 2, 3), dataclasses.replace(_CHAT, schema_version="3")))  # type: ignore[arg-type]


def test_an_unknown_surface_decodes_as_unrecorded() -> None:
    record = json.loads(json.dumps(_CHAT_RECORD))
    record["meta"]["last_surface"] = "web"
    assert decode_entry(record).meta.last_surface is None


def test_load_skips_malformed_entries_and_ignores_other_versions(tmp_path: Path) -> None:
    catalog = SessionCatalogFile(tmp_path)
    assert catalog.load() == {}
    _write_raw(
        catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"chat-1": _CHAT_RECORD, "broken": {"meta": {}}}}
    )
    assert set(catalog.load()) == {"chat-1"}
    for version in (CATALOG_DERIVATION_VERSION - 1, CATALOG_DERIVATION_VERSION + 1, str(CATALOG_DERIVATION_VERSION)):
        _write_raw(catalog, {"version": version, "sessions": {"chat-1": _CHAT_RECORD}})
        assert catalog.load() == {}
    _write_raw(catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": ["chat-1"]})
    assert catalog.load() == {}
    _plant(catalog, b"{not json")
    assert catalog.load() == {}


def test_an_offset_free_timestamp_is_never_decoded(tmp_path: Path) -> None:
    """The catalog only replays aware stamps; an offset-free one is dropped, not served."""
    record = {**_CHAT_RECORD, "meta": {**_CHAT_RECORD["meta"], "updated_at": "2026-09-02T09:30:00"}}

    with pytest.raises(ValueError, match="timezone"):
        decode_entry(record)

    catalog = SessionCatalogFile(tmp_path)
    _write_raw(catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"chat-1": record}})
    assert catalog.load() == {}


def test_an_offset_free_run_listing_time_is_never_decoded(tmp_path: Path) -> None:
    """The cached run time follows the same rule as the session stamps."""
    record = {
        **_WORKFLOW_RECORD,
        "run": {"run_id": "run-2", "header": [4, 5, 6], "events": None, "listed_at": "2026-09-03T10:45:00"},
    }

    with pytest.raises(ValueError, match="timezone"):
        decode_entry(record)

    catalog = SessionCatalogFile(tmp_path)
    _write_raw(catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"flow-1": record}})
    assert catalog.load() == {}


def test_commit_writes_an_owner_only_catalog_and_round_trips_surrogates(tmp_path: Path) -> None:
    catalog = SessionCatalogFile(tmp_path)
    odd = dataclasses.replace(_CHAT, primary_cwd="/work/\udcff", title="naïve ✓")
    entry = CatalogEntry((7, 8, 9), dataclasses.replace(odd, size_bytes=0))

    outcome = catalog.commit({"chat-1": entry}, still_valid=_always, is_live=_always)

    assert outcome.settled and outcome.signature == catalog_module.file_signature(catalog.path)
    assert catalog.path.parent.name == ".cache" and catalog.lock_path.parent.name == ".locks"
    assert catalog.load() == {"chat-1": entry}
    if sys.platform != "win32":
        assert stat.S_IMODE(catalog.path.stat().st_mode) == 0o600


def test_commit_merges_with_peers_and_prunes_gone_sessions(tmp_path: Path) -> None:
    catalog = SessionCatalogFile(tmp_path)
    _write_raw(
        catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"peer": _CHAT_RECORD, "gone": _CHAT_RECORD}}
    )
    fresh = CatalogEntry((5, 5, 5), _WORKFLOW)
    stale = CatalogEntry((6, 6, 6), _WORKFLOW)

    outcome = catalog.commit(
        {"fresh": fresh, "stale": stale},
        still_valid=lambda short_id, _entry: short_id != "stale",
        is_live=lambda short_id: short_id != "gone",
    )

    assert outcome.settled
    assert set(_read_raw(catalog)["sessions"]) == {"peer", "fresh"}


def test_commit_rebuilds_an_older_catalog_and_leaves_a_newer_one_alone(tmp_path: Path) -> None:
    catalog = SessionCatalogFile(tmp_path)
    entry = CatalogEntry((5, 5, 5), _WORKFLOW)
    _write_raw(catalog, {"version": 0, "sessions": {"old": {"anything": True}}})
    assert catalog.commit({"new": entry}, still_valid=_always, is_live=_always).settled
    assert _read_raw(catalog) == {"version": CATALOG_DERIVATION_VERSION, "sessions": {"new": encode_entry(entry)}}

    newer = {"version": CATALOG_DERIVATION_VERSION + 1, "sessions": {"future": {"shape": "unknown"}}}
    _write_raw(catalog, newer)
    outcome = catalog.commit({"new": entry}, still_valid=_always, is_live=_always)
    assert outcome.settled and outcome.signature is None
    assert _read_raw(catalog) == newer
    # Deleting a session cannot rewrite a newer catalog, so it drops the file rather than leave the excerpts.
    assert catalog.remove({"future"}) is True
    assert not catalog.path.exists()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_a_catalog_unreadable_right_now_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    """A transient read failure (a peer's replace on Windows) must not read as empty and discard every entry."""
    catalog = SessionCatalogFile(tmp_path)
    _write_raw(catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"peer": _CHAT_RECORD}})
    before = catalog.path.read_bytes()

    def unreadable(_path: Path, *, max_bytes: int) -> bytes:
        raise SecureFileError(error_number, "Unable to open owner-verified file.")

    monkeypatch.setattr(catalog_module, "read_owner_verified_bounded", unreadable)

    assert catalog.load() is None
    outcome = catalog.commit({"new": CatalogEntry((5, 5, 5), _WORKFLOW)}, still_valid=_always, is_live=_always)
    assert not outcome.settled
    assert catalog.path.read_bytes() == before
    # Deleting a session still must not leave its excerpts behind.
    assert catalog.remove({"peer"}) is True
    assert not catalog.path.exists()


def test_a_missing_catalog_reads_as_empty_even_through_a_wrapped_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = SessionCatalogFile(tmp_path)

    def missing(_path: Path, *, max_bytes: int) -> bytes:
        raise SecureFileError(errno.ENOENT, "Unable to open owner-verified file.")

    monkeypatch.setattr(catalog_module, "read_owner_verified_bounded", missing)
    assert catalog.load() == {}


def test_a_merge_that_changes_nothing_does_not_rewrite(tmp_path: Path) -> None:
    catalog = SessionCatalogFile(tmp_path)
    entry = CatalogEntry((5, 5, 5), _WORKFLOW)
    assert catalog.commit({"s": entry}, still_valid=_always, is_live=_always).settled
    before = catalog.path.stat()

    outcome = catalog.commit({"s": entry}, still_valid=_always, is_live=_always)

    after = catalog.path.stat()
    assert outcome.settled and outcome.signature == catalog_module.file_signature(catalog.path)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_held_lock_defers_the_commit_and_fails_the_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_module, "SESSION_CATALOG_LOCK_TIMEOUT_SECONDS", 0.05)
    catalog = SessionCatalogFile(tmp_path)
    catalog.lock_path.parent.mkdir(parents=True)
    with FileLock(catalog.lock_path, timeout=1.0):
        outcome = catalog.commit({"new": CatalogEntry((5, 5, 5), _WORKFLOW)}, still_valid=_always, is_live=_always)
    assert not outcome.settled
    assert not catalog.path.exists()

    _write_raw(catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"gone": _CHAT_RECORD}})
    with FileLock(catalog.lock_path, timeout=1.0):
        assert catalog.remove({"gone"}) is False
    assert set(_read_raw(catalog)["sessions"]) == {"gone"}


def test_remove_drops_only_the_named_entries(tmp_path: Path) -> None:
    catalog = SessionCatalogFile(tmp_path)
    _write_raw(catalog, {"version": CATALOG_DERIVATION_VERSION, "sessions": {"a": _CHAT_RECORD, "b": _CHAT_RECORD}})
    before = catalog.path.stat().st_mtime_ns

    assert catalog.remove({"missing"}) is True
    assert catalog.path.stat().st_mtime_ns == before
    assert catalog.remove({"a"}) is True
    assert set(_read_raw(catalog)["sessions"]) == {"b"}
