# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable browser manifest: identity, redaction and atomic replacement (W03)."""

from __future__ import annotations

import json
from pathlib import Path

from chrys.extensions import ToolContext

from icode_browser_use import registry


def ctx(tmp_path: Path, *, session: str | None = "s1", agent: str = "A", with_session_dir: bool = True):
    return ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=(tmp_path / "session") if with_session_dir else None,
        agent_name=agent,
        session_id=session,
    )


def record(resume_id: str = "s0-abc", **overrides: object) -> registry.ManifestRecord:
    values: dict[str, object] = {
        "schema": registry.SCHEMA_VERSION,
        "browser_id": "b1",
        "resume_id": resume_id,
        "session_id": "s1",
        "agent_name": "A",
        "owner_key": "owner",
        "backend": "owned",
        "generation": "abc",
        "slot": 0,
        "origin": "https://example.com:443",
        "updated_at": 0.0,
    }
    values.update(overrides)
    return registry.ManifestRecord(**values)  # type: ignore[arg-type]


def test_manifest_path_prefers_the_session_folder(tmp_path: Path) -> None:
    in_session = registry.manifest_path(ctx(tmp_path))
    assert in_session == tmp_path / "session" / "browsers.json"
    without = registry.manifest_path(ctx(tmp_path, with_session_dir=False))
    assert without == tmp_path / "data" / "browsers.json"


def test_save_replaces_atomically_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = ctx(tmp_path).session_data_dir / "browsers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    registry.save(path, [record()])
    assert registry.load(path)[0] == [record()]
    assert sorted(item.name for item in path.parent.iterdir()) == ["browsers.json"]


def test_load_reports_missing_and_corrupt_manifests(tmp_path: Path) -> None:
    path = ctx(tmp_path).session_data_dir / "browsers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert registry.load(path) == ([], None)
    path.write_text("{broken", encoding="utf-8")
    records, problem = registry.load(path)
    assert records == []
    assert problem == "unreadable"
    path.write_text(json.dumps({"schema": registry.SCHEMA_VERSION}), encoding="utf-8")
    assert registry.load(path) == ([], "unexpected shape")


def test_load_ignores_records_from_an_older_schema(tmp_path: Path) -> None:
    path = ctx(tmp_path).session_data_dir / "browsers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": 1, "records": [{**record().to_dict(), "schema": 0}, record("s0-new").to_dict()]}),
        encoding="utf-8",
    )
    records, problem = registry.load(path)
    assert problem is None
    assert [item.resume_id for item in records] == ["s0-new"]


def test_load_rejects_records_with_invalid_field_types(tmp_path: Path) -> None:
    path = ctx(tmp_path).session_data_dir / "browsers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    bad_slot = {**record("s0-bad").to_dict(), "slot": "not-an-int"}
    bool_slot = {**record("s0-bool").to_dict(), "slot": True}
    good = record("s0-good").to_dict()
    path.write_text(json.dumps({"schema": 1, "records": [bad_slot, bool_slot, good]}), encoding="utf-8")
    records, problem = registry.load(path)
    assert problem == "2 invalid record(s) skipped"
    assert [item.resume_id for item in records] == ["s0-good"]
    assert isinstance(records[0].slot, int)


def test_upsert_replaces_the_same_resume_id_only() -> None:
    first = record("s0-a", origin="https://a.example:443")
    second = record("s0-b")
    updated = record("s0-a", origin="https://b.example:443")
    records = registry.upsert([first, second], updated)
    assert records == [updated, second]


def test_supersede_drops_older_generations_of_the_same_slot() -> None:
    older = record("s0-old", slot=0, generation="old")
    other_slot = record("s1-keep", slot=1, generation="keep")
    other_owner = record("s0-other", slot=0, owner_key="someone-else")
    newest = record("s0-new", slot=0, generation="new")
    records = registry.supersede([older, other_slot, other_owner], newest)
    assert [item.resume_id for item in records] == ["s1-keep", "s0-other", "s0-new"]


def test_drop_removes_only_the_named_record() -> None:
    records = [record("s0-a"), record("s0-b")]
    assert [item.resume_id for item in registry.drop(records, "s0-a")] == ["s0-b"]
    assert registry.drop(records, "missing") == records


def test_stored_origin_never_keeps_query_credentials_or_paths() -> None:
    url = "https://user:secret@example.com:8443/app?token=SENTINEL#fragment"
    assert registry.redacted_origin(url) == "https://example.com:8443"
    assert registry.redacted_origin("chrome-error://chromewebdata/") == "(not navigated yet)"
    stored = registry.new_record(
        browser_id="b1",
        resume_id="s0-abc",
        session_id="s1",
        agent_name="A",
        owner_key="owner",
        backend="owned",
        generation="abc",
        slot=0,
        origin_url=url,
    )
    assert "SENTINEL" not in json.dumps(stored.to_dict())
    assert "secret" not in json.dumps(stored.to_dict())
    assert stored.origin == "https://example.com:8443"
