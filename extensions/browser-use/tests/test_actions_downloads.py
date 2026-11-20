# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_use.actions import download_cancel, downloads
from browser_use.downloads import DownloadInfo
from browser_use.errors import BadRequest
from browser_use.journal import Journal
from browser_use.validation import validate


class FakeSession:
    page_epoch = 3

    def __init__(self, directory: Path | None, records: list[DownloadInfo] | None = None) -> None:
        self.journal = Journal(None)
        self.cfg = SimpleNamespace(download_max_bytes=50 * 1024 * 1024)
        self._directory = directory
        self.records = list(records or [])
        self.cancelled: list[str] = []

    @property
    def download_directory(self) -> Path | None:
        return self._directory

    def list_downloads(self) -> list[DownloadInfo]:
        return list(self.records)

    async def cancel_download(self, guid: str) -> DownloadInfo:
        info = next((item for item in self.records if item.guid == guid), None)
        if info is None:
            raise BadRequest(f"未知下载：{guid}")
        self.cancelled.append(guid)
        if info.state == "in_progress":
            info.state = "canceled"
            info.reason = "canceled by browser_download_cancel"
            info.path = None
        return info


def fixed_id(monkeypatch) -> None:
    monkeypatch.setattr(download_cancel, "new_action_id", lambda: "fixed")


def test_disabled_downloads_explain_how_to_enable_them() -> None:
    session = FakeSession(None)
    blocks = asyncio.run(downloads.run(session, {}))
    text = blocks[0]["text"]
    assert "下载未启用" in text
    assert "ICODE_BROWSER_DOWNLOADS=allow" in text
    assert "Attach/Bridge/Broker" in text


def test_a_completed_download_reports_state_name_size_and_path(tmp_path: Path) -> None:
    info = DownloadInfo(
        guid="g1",
        name="report.txt",
        state="completed",
        bytes_received=5,
        total_bytes=5,
        path=tmp_path / "report.txt",
        url="https://example.com/report.txt",
    )
    session = FakeSession(tmp_path, [info])
    text = asyncio.run(downloads.run(session, {}))[0]["text"]
    assert "下载 1 个" in text
    assert "guid=g1" in text
    assert "state=completed" in text
    assert 'name="report.txt"' in text
    assert "bytes=5/5" in text
    assert f"path={json.dumps(str(tmp_path / 'report.txt'))}" in text
    assert f"url={json.dumps('https://example.com/report.txt')}" in text


def test_a_failed_download_reports_its_reason(tmp_path: Path) -> None:
    info = DownloadInfo(
        guid="g2",
        name="big.bin",
        state="failed",
        bytes_received=20,
        reason="size_limit: 20 > 4 bytes",
    )
    session = FakeSession(tmp_path, [info])
    text = asyncio.run(downloads.run(session, {}))[0]["text"]
    assert "state=failed" in text
    assert 'reason="size_limit: 20 > 4 bytes"' in text
    assert "path=" not in text


def test_cancel_requires_an_in_progress_download(tmp_path: Path, monkeypatch) -> None:
    fixed_id(monkeypatch)
    info = DownloadInfo(guid="g3", name="done.bin", state="completed", path=tmp_path / "done.bin")
    session = FakeSession(tmp_path, [info])
    blocks = asyncio.run(download_cancel.run(session, {"guid": "g3"}))
    text = blocks[0]["text"]
    assert "未取消" in text and "completed" in text and "status=blocked" in text
    assert session.cancelled == []
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"


def test_an_unknown_guid_is_blocked(tmp_path: Path, monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(tmp_path)
    text = asyncio.run(download_cancel.run(session, {"guid": "missing"}))[0]["text"]
    assert "未知下载" in text and "status=blocked" in text
    assert session.cancelled == []


def test_cancel_reports_success_and_cleans_the_record(tmp_path: Path, monkeypatch) -> None:
    fixed_id(monkeypatch)
    info = DownloadInfo(guid="g4", name="slow.bin", state="in_progress", bytes_received=99, path=tmp_path / "slow.bin")
    session = FakeSession(tmp_path, [info])
    text = asyncio.run(download_cancel.run(session, {"guid": "g4"}))[0]["text"]
    assert "已取消下载 slow.bin" in text
    assert "status=succeeded" in text
    assert session.cancelled == ["g4"]
    row = session.journal.lookup("fixed")
    assert row is not None
    assert (row["tool"], row["phase"], row["status"]) == ("browser_download_cancel", "result", "succeeded")


def test_cancel_refuses_a_download_that_finished_while_canceling(tmp_path: Path, monkeypatch) -> None:
    fixed_id(monkeypatch)

    class Racing(FakeSession):
        async def cancel_download(self, guid: str) -> DownloadInfo:
            info = next(item for item in self.records if item.guid == guid)
            info.state = "completed"
            return info

    info = DownloadInfo(guid="g5", name="race.bin", state="in_progress", path=tmp_path / "race.bin")
    session = Racing(tmp_path, [info])
    text = asyncio.run(download_cancel.run(session, {"guid": "g5"}))[0]["text"]
    assert "完成于取消之前" in text
    assert "status=blocked" in text
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"


def test_cancel_does_nothing_when_downloads_are_disabled(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(None)
    text = asyncio.run(download_cancel.run(session, {"guid": "g6"}))[0]["text"]
    assert "下载未启用" in text and "status=blocked" in text
    assert session.cancelled == []


def test_cancel_journal_records_the_action_id(tmp_path: Path, monkeypatch) -> None:
    fixed_id(monkeypatch)
    info = DownloadInfo(guid="g7", name="x.bin", state="in_progress")
    session = FakeSession(tmp_path, [info])
    text = asyncio.run(download_cancel.run(session, {"guid": "g7"}))[0]["text"]
    assert "action_id=fixed" in text
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result"


def test_parameters_are_declared() -> None:
    assert downloads.PARAMS == {}
    assert download_cancel.PARAMS == {"guid": {"type": "string", "required": True}}
    with pytest.raises(BadRequest):
        validate(download_cancel.PARAMS, {})
