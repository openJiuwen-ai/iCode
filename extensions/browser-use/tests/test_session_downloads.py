# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser_use.config import BrowserConfig
from browser_use.downloads import DownloadInfo
from browser_use.errors import BadRequest
from browser_use.session import BrowserSession, _MAX_ACTIVE_DOWNLOADS
import browser_use.session as session_module


def make_session(tmp_path: Path, **overrides) -> BrowserSession:
    cfg = BrowserConfig(
        allowed_domains=("example.com",),
        download_dir=tmp_path / "downloads",
        **overrides,
    )
    return BrowserSession(cfg)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {}), session_id))
        return {}

    async def aclose(self) -> None:
        return None

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]


def attach(session: BrowserSession) -> FakeClient:
    client = FakeClient()
    session._client = client
    session._session_id = "s-main"
    return client


def downloads_of(session: BrowserSession) -> dict[str, object]:
    return {info.guid: info for info in session.list_downloads()}


def test_a_completed_download_is_renamed_inside_the_folder(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g1", "suggestedFilename": "report.txt", "url": "https://example.com/report.txt?token=secret"},
            None,
        )
        info = downloads_of(session)["g1"]
        assert info.state == "in_progress"
        assert info.path is not None and info.path.exists()
        (session.download_directory / "g1").write_bytes(b"hello")
        await session._on_download_progress(
            {
                "guid": "g1",
                "state": "completed",
                "receivedBytes": 5,
                "totalBytes": 5,
                "filePath": str(session.download_directory / "g1"),
            },
            None,
        )
        info = downloads_of(session)["g1"]
        assert info.state == "completed"
        assert info.name == "report.txt"
        assert info.bytes_received == 5
        assert info.path == tmp_path / "downloads" / "report.txt"
        assert info.path.read_bytes() == b"hello"
        assert not (session.download_directory / "g1").exists()
        assert "token" not in info.url

    asyncio.run(scenario())


def test_a_malicious_name_is_reduced_to_a_file_inside_the_folder(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g2", "suggestedFilename": "..\\..\\evil.exe", "url": "https://example.com/x"}, None
        )
        info = downloads_of(session)["g2"]
        assert info.name == "evil.exe"
        assert info.path is not None
        assert info.path.parent == tmp_path / "downloads"

    asyncio.run(scenario())


def test_the_same_name_gets_a_suffix_instead_of_being_overwritten(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)

    async def scenario() -> None:
        for guid in ("g3", "g4"):
            await session._on_download_will_begin(
                {"guid": guid, "suggestedFilename": "file.txt", "url": "https://example.com/f"}, None
            )
        names = [downloads_of(session)[guid].name for guid in ("g3", "g4")]
        assert names == ["file.txt", "file-2.txt"]

    asyncio.run(scenario())


def test_a_transfer_over_the_limit_is_canceled_and_cleaned(tmp_path: Path) -> None:
    session = make_session(tmp_path, download_max_bytes=10)
    client = attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g5", "suggestedFilename": "big.bin", "url": "https://example.com/big"}, None
        )
        (session.download_directory / "g5").write_bytes(b"0123456789abcdef")
        await session._on_download_progress(
            {"guid": "g5", "state": "inProgress", "receivedBytes": 20, "totalBytes": 100}, None
        )
        info = downloads_of(session)["g5"]
        assert info.state == "canceled"
        assert "size_limit" in info.reason
        assert ("Browser.cancelDownload", {"guid": "g5"}) == (client.calls[-1][0], client.calls[-1][1])
        assert not (session.download_directory / "g5").exists()
        assert info.path is None

    asyncio.run(scenario())


def test_a_download_that_completes_over_the_limit_is_deleted(tmp_path: Path) -> None:
    session = make_session(tmp_path, download_max_bytes=4)
    attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g6", "suggestedFilename": "late.bin", "url": "https://example.com/late"}, None
        )
        (session.download_directory / "g6").write_bytes(b"too big")
        await session._on_download_progress({"guid": "g6", "state": "completed", "receivedBytes": 7}, None)
        info = downloads_of(session)["g6"]
        assert info.state == "failed"
        assert "size_limit" in info.reason
        assert info.path is None
        assert list(session.download_directory.iterdir()) == []

    asyncio.run(scenario())


def test_a_canceled_transfer_removes_its_partial_file(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g7", "suggestedFilename": "c.bin", "url": "https://example.com/c"}, None
        )
        (session.download_directory / "g7").write_bytes(b"partial")
        await session._on_download_progress({"guid": "g7", "state": "canceled", "receivedBytes": 7}, None)
        info = downloads_of(session)["g7"]
        assert info.state == "canceled"
        assert info.path is None
        assert list(session.download_directory.iterdir()) == []

    asyncio.run(scenario())


def test_the_cancel_tool_cancels_an_in_progress_download(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    client = attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g8", "suggestedFilename": "slow.bin", "url": "https://example.com/slow"}, None
        )
        info = await session.cancel_download("g8")
        assert info.state == "canceled"
        assert info.reason == "canceled by browser_download_cancel"
        assert client.methods() == ["Browser.cancelDownload"]
        assert list(session.download_directory.iterdir()) == []

    asyncio.run(scenario())


def test_canceling_a_finished_download_sends_nothing(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    client = attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g9", "suggestedFilename": "done.bin", "url": "https://example.com/done"}, None
        )
        (session.download_directory / "g9").write_bytes(b"done")
        await session._on_download_progress(
            {
                "guid": "g9",
                "state": "completed",
                "receivedBytes": 4,
                "filePath": str(session.download_directory / "g9"),
            },
            None,
        )
        info = await session.cancel_download("g9")
        assert info.state == "completed"
        assert "Browser.cancelDownload" not in client.methods()

    asyncio.run(scenario())


def test_an_unknown_guid_is_rejected(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)
    with pytest.raises(BadRequest):
        asyncio.run(session.cancel_download("nope"))


def test_attach_style_sessions_ignore_download_events(tmp_path: Path) -> None:
    cfg = BrowserConfig(
        allowed_domains=("example.com",), download_dir=tmp_path / "downloads", cdp_url="http://127.0.0.1:9222"
    )
    session = BrowserSession(cfg)
    attach(session)
    assert session.download_directory is None

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g10", "suggestedFilename": "x.bin", "url": "https://example.com/x"}, None
        )
        await session._on_download_progress({"guid": "g10", "state": "completed"}, None)

    asyncio.run(scenario())
    assert session.list_downloads() == []


def test_browser_teardown_fails_an_in_progress_download(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g11", "suggestedFilename": "busy.bin", "url": "https://example.com/busy"}, None
        )
        await session._teardown_transport()
        info = downloads_of(session)["g11"]
        assert info.state == "failed"
        assert info.reason == "browser closed"
        assert info.path is None

    asyncio.run(scenario())


def test_cancel_does_not_rename_a_completion_that_arrives_while_canceling(tmp_path: Path) -> None:
    session = make_session(tmp_path)

    class RacingClient(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "Browser.cancelDownload":
                guid = str((params or {})["guid"])
                (session.download_directory / guid).write_bytes(b"finished")
                await session._on_download_progress({"guid": guid, "state": "completed", "receivedBytes": 8}, None)
            return await super().send(method, params, session_id=session_id, timeout=timeout)

    session._client = RacingClient()
    session._session_id = "s-main"

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g12", "suggestedFilename": "race.bin", "url": "https://example.com/race"}, None
        )
        info = await session.cancel_download("g12")
        assert info.state == "canceled"
        assert list(session.download_directory.iterdir()) == []

    asyncio.run(scenario())


def test_a_reservation_failure_cancels_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    session = make_session(tmp_path)
    client = attach(session)

    def fail(_directory, _name):
        raise OSError("no space")

    monkeypatch.setattr(session_module, "reserve_path", fail)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g13", "suggestedFilename": "nospace.bin", "url": "https://example.com/n"}, None
        )

    asyncio.run(scenario())
    info = downloads_of(session)["g13"]
    assert info.state == "failed"
    assert "could not reserve" in info.reason
    assert "Browser.cancelDownload" in client.methods()
    assert not session.download_directory.exists() or list(session.download_directory.iterdir()) == []


def test_a_received_bytes_burst_alone_can_exceed_the_limit(tmp_path: Path) -> None:
    session = make_session(tmp_path, download_max_bytes=10)
    client = attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g-recv", "suggestedFilename": "burst.bin", "url": "https://example.com/b"}, None
        )
        await session._on_download_progress(
            {"guid": "g-recv", "state": "inProgress", "receivedBytes": 20, "totalBytes": -1}, None
        )
        info = downloads_of(session)["g-recv"]
        assert info.state == "canceled"
        assert "size_limit" in info.reason
        assert ("Browser.cancelDownload", {"guid": "g-recv"}) == (client.calls[-1][0], client.calls[-1][1])

    asyncio.run(scenario())


def test_page_scoped_download_events_are_ignored(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g-page", "suggestedFilename": "page.bin", "url": "https://example.com/p"}, "s-main"
        )
        await session._on_download_progress({"guid": "g-page", "state": "completed"}, "s-main")

    asyncio.run(scenario())
    assert session.list_downloads() == []


def test_a_rename_failure_is_reported_and_cleaned(tmp_path: Path, monkeypatch) -> None:
    session = make_session(tmp_path)
    attach(session)

    def fail(_source, _target):
        raise OSError("rename failed")

    async def scenario() -> None:
        await session._on_download_will_begin(
            {"guid": "g-rename", "suggestedFilename": "rename.bin", "url": "https://example.com/r"}, None
        )
        (session.download_directory / "g-rename").write_bytes(b"data")
        monkeypatch.setattr(session_module.os, "replace", fail)
        await session._on_download_progress({"guid": "g-rename", "state": "completed", "receivedBytes": 4}, None)

    asyncio.run(scenario())
    info = downloads_of(session)["g-rename"]
    assert info.state == "failed"
    assert "could not finalise" in info.reason
    assert list(session.download_directory.iterdir()) == []


def test_too_many_active_downloads_are_refused_and_canceled(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    client = attach(session)

    async def scenario() -> None:
        for index in range(_MAX_ACTIVE_DOWNLOADS):
            await session._on_download_will_begin(
                {"guid": f"active-{index}", "suggestedFilename": f"a{index}.bin", "url": "https://example.com/a"},
                None,
            )
        await session._on_download_will_begin(
            {"guid": "active-over", "suggestedFilename": "over.bin", "url": "https://example.com/o"}, None
        )

    asyncio.run(scenario())
    info = downloads_of(session)["active-over"]
    assert info.state == "failed"
    assert "too many concurrent downloads" in info.reason
    assert ("Browser.cancelDownload", {"guid": "active-over"}) == (client.calls[-1][0], client.calls[-1][1])
    assert not any("active-over" in path.name for path in session.download_directory.iterdir())


def test_record_eviction_keeps_active_downloads(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    attach(session)
    for index in range(50):
        session._remember_download(DownloadInfo(guid=f"t{index}", name="x.bin", state="completed"))
    active = DownloadInfo(guid="still-active", name="a.bin", state="in_progress")
    session._remember_download(active)
    guids = {info.guid for info in session.list_downloads()}
    assert "still-active" in guids
    assert "t0" not in guids
    assert len(guids) <= 50
