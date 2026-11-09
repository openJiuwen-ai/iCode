# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared atomic filesystem helpers."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from chrys.foundation.platform import files
from tests.support.windows_replace import briefly_held_unlink, briefly_locked_rename


def test_is_utf8_encodable_rejects_only_surrogate_bearing_text() -> None:
    assert files.is_utf8_encodable("ordinary 你好 🌍") is True
    assert files.is_utf8_encodable("damaged \udcff") is False


def test_atomic_write_text_creates_parent_and_leaves_no_success_temp(tmp_path) -> None:
    target = tmp_path / "nested" / "payload.txt"
    payload = "Chrys 你好 🌼\n"

    files.atomic_write_text(target, payload)

    assert target.read_text(encoding="utf-8") == payload
    assert list(target.parent.glob(f"{target.name}.*.tmp")) == []


def test_atomic_write_text_retries_a_briefly_locked_target_on_windows(tmp_path, monkeypatch) -> None:
    target = tmp_path / "session.json"
    target.write_text("before", encoding="utf-8")
    rename = briefly_locked_rename(monkeypatch, failures=2)

    files.atomic_write_text(target, "after")

    assert target.read_text(encoding="utf-8") == "after"
    assert len(rename.attempts) == 3
    assert rename.sleeps == [0.01, 0.02]
    assert list(tmp_path.glob("*.tmp")) == []


def test_replace_with_retry_gives_up_after_six_tries_on_windows(tmp_path, monkeypatch) -> None:
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new", encoding="utf-8")
    rename = briefly_locked_rename(monkeypatch, failures=6)

    with pytest.raises(PermissionError):
        files.replace_with_retry(source, target)

    assert len(rename.attempts) == 6
    assert rename.sleeps == [0.01, 0.02, 0.04, 0.08, 0.16]


def test_replace_with_retry_tries_once_off_windows(tmp_path, monkeypatch) -> None:
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new", encoding="utf-8")
    rename = briefly_locked_rename(monkeypatch, failures=1, windows=False)

    with pytest.raises(PermissionError):
        files.replace_with_retry(source, target)

    assert len(rename.attempts) == 1
    assert rename.sleeps == []


def test_unlink_with_retry_deletes_a_briefly_held_file_on_windows(tmp_path, monkeypatch) -> None:
    target = tmp_path / "session.json"
    target.write_text("{}", encoding="utf-8")
    held = briefly_held_unlink(monkeypatch, target, failures=2)

    files.unlink_with_retry(target)

    assert not target.exists()
    assert len(held.attempts) == 3
    assert held.sleeps == [0.01, 0.02]


def test_unlink_with_retry_gives_up_after_six_tries_on_windows(tmp_path, monkeypatch) -> None:
    target = tmp_path / "session.json"
    target.write_text("{}", encoding="utf-8")
    held = briefly_held_unlink(monkeypatch, target, failures=6)

    with pytest.raises(PermissionError):
        files.unlink_with_retry(target)

    assert target.exists()
    assert len(held.attempts) == 6
    assert held.sleeps == [0.01, 0.02, 0.04, 0.08, 0.16]


def test_unlink_with_retry_tries_once_off_windows(tmp_path, monkeypatch) -> None:
    target = tmp_path / "session.json"
    target.write_text("{}", encoding="utf-8")
    held = briefly_held_unlink(monkeypatch, target, failures=1, windows=False)

    with pytest.raises(PermissionError):
        files.unlink_with_retry(target)

    assert len(held.attempts) == 1
    assert held.sleeps == []


def test_unlink_with_retry_outlasts_a_reader_that_holds_the_file(tmp_path, monkeypatch) -> None:
    """On Windows the first delete meets the open reader for real; the reader lets go during the first wait."""
    target = tmp_path / "session.json"
    target.write_text("{}", encoding="utf-8")
    reader = target.open(encoding="utf-8")
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        reader.close()

    fake_time = ModuleType("time")
    fake_time.sleep = sleep  # type: ignore[attr-defined]
    monkeypatch.setattr(files, "time", fake_time)
    try:
        files.unlink_with_retry(target)
    finally:
        reader.close()

    assert not target.exists()
    # POSIX deletes an open file at once.
    assert sleeps == ([0.01] if sys.platform == "win32" else [])


@pytest.mark.skipif(sys.platform != "win32", reason="the owner-only Windows writer needs the Windows security APIs")
def test_atomic_write_owner_only_bytes_retries_a_briefly_locked_target(tmp_path, monkeypatch) -> None:
    target = tmp_path / "audit.json"
    files.atomic_write_owner_only_bytes(target, b"before")
    rename = briefly_locked_rename(monkeypatch, failures=2)

    files.atomic_write_owner_only_bytes(target, b"after")

    assert target.read_bytes() == b"after"
    assert len(rename.attempts) == 3
    assert rename.sleeps == [0.01, 0.02]


def test_atomic_write_owner_only_bytes_can_refuse_to_create_a_missing_parent(tmp_path) -> None:
    present = tmp_path / "present" / "payload.bin"
    present.parent.mkdir()
    files.atomic_write_owner_only_bytes(present, b"one", create_parents=False)
    assert present.read_bytes() == b"one"

    missing = tmp_path / "missing" / "payload.bin"
    with pytest.raises(OSError):
        files.atomic_write_owner_only_bytes(missing, b"two", create_parents=False)
    assert not missing.parent.exists()


@pytest.mark.parametrize(
    "reader",
    [files.secure_open_owner_only_binary, files.secure_open_owner_verified_binary, files.secure_open_regular_binary],
)
def test_secure_binary_readers_report_an_absent_file_or_parent_as_enoent(tmp_path, reader) -> None:
    """Callers treat ENOENT as "nothing there yet" on every platform, a missing parent directory included."""
    for missing in (tmp_path / "absent.bin", tmp_path / "no-such-dir" / "absent.bin"):
        with pytest.raises(OSError) as raised:
            reader(missing)
        assert raised.value.errno == errno.ENOENT


def test_atomic_create_text_creates_once_and_refuses_to_replace(tmp_path) -> None:
    target = tmp_path / "nested" / "payload.txt"
    payload = "Chrys 你好 🌼\n"

    encoded = files.atomic_create_text(target, payload)

    assert encoded == payload.encode("utf-8")
    assert target.read_text(encoding="utf-8") == payload
    assert list(target.parent.glob(f"{target.name}.*.tmp")) == []

    with pytest.raises(FileExistsError):
        files.atomic_create_text(target, "usurper")

    assert target.read_text(encoding="utf-8") == payload
    assert list(target.parent.glob(f"{target.name}.*.tmp")) == []


@pytest.mark.parametrize("link_error", [errno.ENOTSUP, errno.EPERM, errno.ENOSYS])
def test_atomic_create_text_without_hard_links_preserves_existing_file(tmp_path, monkeypatch, link_error) -> None:
    monkeypatch.setattr(files.os, "link", Mock(side_effect=OSError(link_error, "Hard links unavailable")))
    target = tmp_path / "payload.txt"

    files.atomic_create_text(target, "complete payload")
    with pytest.raises(FileExistsError):
        files.atomic_create_text(target, "must not replace")

    assert target.read_text(encoding="utf-8") == "complete payload"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["payload.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation may require Windows privileges")
def test_no_hard_links_does_not_allow_replacing_a_dangling_symlink(tmp_path, monkeypatch) -> None:
    target = tmp_path / "payload.txt"
    missing = tmp_path / "missing"
    target.symlink_to(missing)
    monkeypatch.setattr(files.os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "Hard links unavailable")))

    with pytest.raises(FileExistsError):
        files.atomic_create_text(target, "must not replace")

    assert target.is_symlink()
    assert not missing.exists()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard-link fallback")
def test_atomic_create_fails_without_publishing_when_native_rename_is_unsupported(tmp_path, monkeypatch) -> None:
    target = tmp_path / "payload.txt"
    monkeypatch.setattr(files.os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "Hard links unavailable")))
    monkeypatch.setattr(
        files, "_rename_file_no_replace", Mock(side_effect=OSError(errno.ENOTSUP, "Exclusive rename unavailable"))
    )

    with pytest.raises(OSError, match="Exclusive rename unavailable"):
        files.atomic_create_text(target, "payload")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard-link fallback")
@pytest.mark.parametrize("link_error", [errno.EACCES, errno.EIO, errno.EXDEV])
def test_atomic_create_propagates_link_errors_without_rename(tmp_path, monkeypatch, link_error) -> None:
    rename = Mock()
    monkeypatch.setattr(files, "_rename_file_no_replace", rename)
    monkeypatch.setattr(files.os, "link", Mock(side_effect=OSError(link_error, "Publication failed")))

    with pytest.raises(OSError) as raised:
        files.atomic_create_text(tmp_path / "payload.txt", "payload")

    assert raised.value.errno == link_error
    rename.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_atomic_create_removes_the_temp_name_before_the_directory_sync(tmp_path, monkeypatch) -> None:
    """A crash after the sync must not resurrect the temp entry.

    Syncing the directory while both the target and the temp name exist would
    leave the temp file durable and its removal not, so the commit has to
    consume the temp name first on both platform branches.
    """
    target = tmp_path / "payload.txt"
    listings: list[list[str]] = []
    real_fsync_dir = files.fsync_directory

    def observing_fsync_dir(directory) -> None:
        listings.append(sorted(entry.name for entry in tmp_path.iterdir()))
        real_fsync_dir(directory)

    monkeypatch.setattr(files, "fsync_directory", observing_fsync_dir)

    files.atomic_create_text(target, "payload")

    assert listings == [["payload.txt"]]


@pytest.mark.parametrize("failure_point", ["write", "fsync"])
def test_atomic_write_text_cleans_temp_when_file_write_or_fsync_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    target = tmp_path / "nested" / "payload.txt"

    if failure_point == "write":
        real_fdopen = files.os.fdopen

        class _FailingWriter:
            def __init__(self, wrapped: Any) -> None:
                self._wrapped = wrapped

            def __enter__(self) -> _FailingWriter:
                self._wrapped.__enter__()
                return self

            def __exit__(self, *args: object) -> object:
                return self._wrapped.__exit__(*args)

            def write(self, _payload: bytes) -> int:
                raise OSError("write failed")

        def failing_fdopen(*args: object, **kwargs: object) -> _FailingWriter:
            return _FailingWriter(real_fdopen(*args, **kwargs))

        monkeypatch.setattr(files.os, "fdopen", failing_fdopen)
    else:

        def failing_fsync(_fd: int) -> None:
            raise OSError("fsync failed")

        monkeypatch.setattr(files.os, "fsync", failing_fsync)

    with pytest.raises(OSError, match=f"{failure_point} failed"):
        files.atomic_write_text(target, "payload")

    assert not target.exists()
    assert list(target.parent.glob(f"{target.name}.*.tmp")) == []


def test_fsync_dir_is_noop_on_windows(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    open_mock = Mock()
    monkeypatch.setattr(files.os, "name", "nt")
    monkeypatch.setattr(files.os, "open", open_mock)

    files.fsync_directory(tmp_path)

    open_mock.assert_not_called()


def test_fsync_dir_swallows_posix_open_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files.os, "name", "posix")
    monkeypatch.setattr(files.os, "open", Mock(side_effect=OSError("unavailable")))

    files.fsync_directory(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_atomic_write_owner_only_text_sets_exact_mode(tmp_path) -> None:
    target = tmp_path / "secret.txt"

    files.atomic_write_owner_only_text(target, "sensitive")

    assert target.read_text(encoding="utf-8") == "sensitive"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with files.secure_open_owner_only_binary(target) as source:
        assert source.read() == b"sensitive"


@pytest.mark.parametrize(
    "writer",
    ["atomic_write_text", "atomic_write_owner_only_text"],
)
def test_atomic_text_writers_neutralize_lone_surrogates(tmp_path, writer: str) -> None:
    """Both shared text sinks must be TOTAL against lone surrogates.

    An ``os.fsdecode`` of an undecodable filesystem byte yields a lone surrogate
    (``\\udcXX``); carried through ``json.dumps(ensure_ascii=False)`` it lands in a
    serialized envelope as a legal ``str`` that a strict UTF-8 encode rejects.
    Every session/audit/fork write funnels through one of these two sinks, so
    encoding with ``backslashreplace`` here is what stops a surrogateescaped path
    (in ACP launch args, a repaired-history ``last_error``, a ``sub_agent_log_file``
    reference, or any main-session field) from crashing the write and leaving a
    session unsavable or a fork aborted.
    """
    target = tmp_path / "payload.json"
    import json

    payload = json.dumps({"cwd": "/work/pro\udcffject", "note": "byte /x/\udcfe"}, ensure_ascii=False)

    getattr(files, writer)(target, payload)

    # The file is strict-UTF-8 (no lone surrogate leaked into the bytes)...
    text = target.read_bytes().decode("utf-8")
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)
    # ...and it is still valid JSON that round-trips (a reconcile/fork read).
    restored = json.loads(text)
    assert restored["cwd"].endswith("ject")
    assert restored["note"].startswith("byte /x/")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_secure_open_rejects_insecure_mode_without_repair(tmp_path) -> None:
    target = tmp_path / "insecure.txt"
    target.write_text("payload", encoding="utf-8")
    target.chmod(0o644)

    with pytest.raises(files.SecureFileError, match="owner-only"):
        files.secure_open_owner_only(target, read=True)
    with pytest.raises(files.SecureFileError, match="owner-only"):
        files.secure_open_owner_only(target, write=True, truncate=True)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_text(encoding="utf-8") == "payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_secure_open_takes_an_owner_only_mode_other_than_0600(tmp_path) -> None:
    """A volume that stores no permissions (exFAT on macOS) shows every file as
    0700 and ignores chmod; owner-only means no group or other bit, not 0600."""
    target = tmp_path / "exfat-like.jsonl"
    target.write_bytes(b"first\n")
    target.chmod(0o700)

    handle = files.secure_open_owner_only_append(target)
    try:
        os.write(handle.fd, b"second\n")
    finally:
        os.close(handle.fd)
    os.close(files.secure_open_owner_only(target, read=True))

    assert target.read_bytes() == b"first\nsecond\n"
    target.chmod(0o640)
    with pytest.raises(files.SecureFileError, match="owner-only"):
        files.secure_open_owner_only_append(target)


@pytest.mark.skipif(os.name == "nt", reason="FIFOs are POSIX-only")
def test_secure_open_rejects_a_planted_fifo_without_blocking(tmp_path) -> None:
    target = tmp_path / "planted"
    os.mkfifo(target, 0o600)

    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only(target, write=True)
    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only(target, read=True)
    with pytest.raises(files.SecureFileError, match="not a regular file"):
        files.secure_open_regular_binary(target)


@pytest.mark.skipif(os.name == "nt", reason="fcntl flags and directory search permission are POSIX-only")
def test_secure_open_regular_binary_needs_only_search_permission_on_parents(tmp_path) -> None:
    import fcntl

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    ancestor = tmp_path / "enter-only"
    target = ancestor / "legacy" / "payload.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"legacy")
    ancestor.chmod(0o111)
    try:
        with pytest.raises(PermissionError):
            files.secure_open_owner_verified_binary(target)
        with files.secure_open_regular_binary(target) as handle:
            assert fcntl.fcntl(handle.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK == 0
            assert handle.read() == b"legacy"
    finally:
        ancestor.chmod(0o755)


def test_secure_open_regular_binary_reports_open_failures_as_the_system_does(tmp_path) -> None:
    """Migration shows the failure as the reason a file stayed behind, so it keeps the cause and path."""
    target = tmp_path / "held.bin"
    target.write_bytes(b"legacy")
    if os.name == "nt":
        import _winapi

        # An exclusive holder makes every other open fail: ERROR_SHARING_VIOLATION.
        holder = _winapi.CreateFile(str(target), _winapi.GENERIC_READ, 0, 0, _winapi.OPEN_EXISTING, 0, 0)
        try:
            with pytest.raises(PermissionError) as raised:
                files.secure_open_regular_binary(target)
        finally:
            _winapi.CloseHandle(holder)
        assert os.path.normcase(raised.value.filename) == os.path.normcase(str(target.parent.resolve() / target.name))
        assert raised.value.winerror == 32
    else:
        if os.geteuid() == 0:
            pytest.skip("root ignores file permissions")
        target.chmod(0)
        try:
            with pytest.raises(PermissionError) as raised:
                files.secure_open_regular_binary(target)
        finally:
            target.chmod(0o600)
        assert raised.value.filename == str(target)
        assert raised.value.strerror == os.strerror(errno.EACCES)


def _windows_wof_state(path: Path) -> tuple[bool, str]:
    """Whether WOF backs *path*, and what Windows reported about it.

    WOF hides its reparse point from applications (``FileAttributeTagInfo``
    reports no tag even through ``FILE_FLAG_OPEN_REPARSE_POINT``), so ask the
    WOF driver itself, as ``WofIsExternalFile`` does (``wofapi.dll`` is absent
    from Windows Server), with the compressed size as a second witness.
    """
    import ctypes
    from ctypes import wintypes

    windows_ctypes: Any = ctypes
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD

    # FILE_READ_ATTRIBUTES, share all, OPEN_EXISTING
    handle = kernel32.CreateFileW(str(path), 0x80, 0x7, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise windows_ctypes.WinError(windows_ctypes.get_last_error())
    try:
        # WOF_EXTERNAL_INFO (version, provider) and the provider's own info.
        info = (wintypes.ULONG * 16)()
        returned = wintypes.DWORD()
        backing_error = 0
        # FSCTL_GET_EXTERNAL_BACKING
        if not kernel32.DeviceIoControl(
            handle, 0x90310, None, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned), None
        ):
            backing_error = windows_ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(handle)
    high = wintypes.DWORD()
    low = kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    stored = (high.value << 32) | low
    size = path.stat().st_size
    backed = (not backing_error and info[1] == 2) or stored < size  # WOF_PROVIDER_FILE
    backing = f"error {backing_error}" if backing_error else f"provider {info[1]}"
    return backed, f"external backing {backing}; {stored} of {size} bytes stored"


def _wof_compress(directory: Path, payload: bytes) -> tuple[Path | None, str]:
    """Write *payload* to a WOF-compressed file in *directory*, or say why that failed."""
    from tests.support.waiting import ENGINE_TURN_TIMEOUT

    target = directory / "compressed.bin"
    target.write_bytes(payload)
    compacted = subprocess.run(
        ["compact", "/C", "/EXE:XPRESS4K", str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    backed, state = _windows_wof_state(target)
    if backed:
        return target, ""
    output = f"{compacted.stdout!r} {compacted.stderr!r}"
    return None, f"{directory}: compact exited {compacted.returncode}: {output}; {state}"


@pytest.mark.skipif(os.name != "nt", reason="WOF compression is Windows-only")
def test_secure_open_regular_binary_reads_a_compressed_file_through_its_filter(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    payload = b"legacy session data " * 16384
    target, diagnostics = _wof_compress(tmp_path, payload)
    local_temp = Path(os.environ.get("LOCALAPPDATA", ""), "Temp")
    if target is None and local_temp.is_absolute() and local_temp.is_dir():
        # CI moves TEMP to a scratch volume that cannot compress; the profile's
        # own temporary folder stays on the system volume, which can.
        fallback = Path(tempfile.mkdtemp(dir=local_temp))
        request.addfinalizer(lambda: shutil.rmtree(fallback, ignore_errors=True))
        target, fallback_diagnostics = _wof_compress(fallback, payload)
        diagnostics = f"{diagnostics}; {fallback_diagnostics}"
    if target is None:
        if os.environ.get("CI"):
            pytest.fail(f"the CI runner should WOF-compress files: {diagnostics}")
        pytest.skip(f"no volume here can WOF-compress files: {diagnostics}")

    with files.secure_open_regular_binary(target) as handle:
        assert handle.read() == payload


@pytest.mark.skipif(os.name != "nt", reason="the reopen check guards the Windows no-follow open")
def test_windows_reopen_refuses_a_name_that_now_leads_to_another_file(tmp_path) -> None:
    target = tmp_path / "legacy.json"
    target.write_bytes(b"legacy")
    decoy = tmp_path / "decoy.json"
    decoy.write_bytes(b"decoy")
    pinned = os.open(target, os.O_RDONLY)
    try:
        with pytest.raises(files.SecureFileError, match="replaced"):
            files._windows_reopen_same_file(decoy, pinned)
        with os.fdopen(files._windows_reopen_same_file(target, pinned), "rb") as handle:
            assert handle.read() == b"legacy"
    finally:
        os.close(pinned)


@pytest.mark.skipif(os.name == "nt", reason="fcntl flags are POSIX-only")
def test_secure_open_returns_blocking_descriptors(tmp_path) -> None:
    import fcntl

    fd = files.secure_open_owner_only(tmp_path / "payload.bin", write=True, create=True)
    try:
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK == 0
    finally:
        os.close(fd)


def test_secure_open_rejects_final_symlink(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("payload", encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only(link, read=True)


def test_atomic_owner_only_write_replaces_symlink_not_target(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "secret.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    files.atomic_write_owner_only_bytes(link, b"replacement")

    assert not link.is_symlink()
    assert link.read_bytes() == b"replacement"
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_atomic_owner_only_output_passes_platform_security_verifier(tmp_path) -> None:
    target = tmp_path / "verified.bin"

    files.atomic_write_owner_only_bytes(target, b"verified")

    with files.secure_open_owner_only_binary(target) as source:
        assert source.read() == b"verified"


def test_atomic_owner_only_write_omits_published_artifact_if_reverification_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "unverified.bin"
    calls = 0

    def verify_then_fail(_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise files.SecureFileError("post-publish verification failed")

    monkeypatch.setattr(files, "verify_owner_only_fd", verify_then_fail)

    with pytest.raises(files.SecureFileError, match="post-publish"):
        files.atomic_write_owner_only_bytes(target, b"sensitive")

    assert not target.exists()
    assert list(tmp_path.glob(".unverified.bin.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_secure_open_create_is_exclusive_and_existing_truncate_is_verified(tmp_path) -> None:
    target = tmp_path / "secure.txt"
    fd = files.secure_open_owner_only(target, write=True, create=True)
    os.write(fd, b"first payload")
    os.close(fd)

    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only(target, write=True, create=True)

    fd = files.secure_open_owner_only(target, write=True, truncate=True)
    os.write(fd, b"new")
    os.close(fd)
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_secure_open_and_atomic_write_reject_linked_parent(tmp_path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only(linked_parent / "secret.txt", write=True, create=True)
    with pytest.raises(files.SecureFileError):
        files.atomic_write_owner_only_text(linked_parent / "secret.txt", "secret")

    assert not (real_parent / "secret.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_atomic_owner_only_write_replaces_insecure_target_with_secure_temp(tmp_path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)

    files.atomic_write_owner_only_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor monkeypatch")
def test_atomic_owner_only_write_cleans_secure_temp_on_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"

    monkeypatch.setattr(files.os, "fsync", Mock(side_effect=OSError("fsync failed")))

    with pytest.raises(OSError, match="fsync failed"):
        files.atomic_write_owner_only_text(target, "payload")

    assert not target.exists()
    assert list(tmp_path.glob(".artifact.json.*.tmp")) == []


def test_windows_atomic_write_cleanup_deletes_only_through_verified_handles(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.bin"
    tmp_file = tmp_path / ".artifact.bin.deadbeef.tmp"
    tmp_file.write_bytes(b"")
    fd = os.open(tmp_file, os.O_WRONLY)

    monkeypatch.setattr(files, "_is_windows", lambda: True)
    monkeypatch.setattr(files, "_validate_link_free_parent", lambda _path: None)
    monkeypatch.setattr(files, "_create_windows_owner_only_temp", lambda _path: (fd, tmp_file))
    replaced: list[tuple[object, object]] = []
    monkeypatch.setattr(files.os, "replace", lambda src, dst: replaced.append((src, dst)))
    monkeypatch.setattr(
        files,
        "secure_open_owner_only",
        Mock(side_effect=files.SecureFileError("post-publish verification failed")),
    )
    expected_identity = os.fstat(fd)
    secure_unlinks: list[tuple[object, object]] = []
    monkeypatch.setattr(
        files,
        "_windows_secure_unlink",
        lambda path, identity: secure_unlinks.append((path, identity)),
    )

    with pytest.raises(files.SecureFileError, match="post-publish"):
        files.atomic_write_owner_only_bytes(target, b"payload")

    # The published artifact and the temp are handed to the verified-handle
    # deleter along with the temp fd's identity; nothing is unlinked by
    # re-traversing the parent by name.
    assert [entry[0] for entry in secure_unlinks] == [target, tmp_file]
    for _path, identity in secure_unlinks:
        assert (identity.st_dev, identity.st_ino) == (expected_identity.st_dev, expected_identity.st_ino)
    assert replaced == [(tmp_file, target)]
    assert tmp_file.exists()


def _windows_unlink_harness(monkeypatch: pytest.MonkeyPatch, opened_path: object) -> list[object]:
    """Run _windows_secure_unlink against a real fd with recorded disposition calls."""
    import ctypes
    import ctypes.wintypes

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(files, "_validate_link_free_parent", lambda _path: None)
    monkeypatch.setattr(
        files,
        "_windows_secure_open",
        lambda path, **_kwargs: os.open(path, os.O_RDONLY),
    )
    dispositions: list[object] = []

    def set_information(_handle: object, _info_class: object, _info: object, _size: object) -> bool:
        dispositions.append(opened_path)
        return True

    monkeypatch.setattr(
        files,
        "_windows_file_api",
        lambda: SimpleNamespace(
            ctypes=ctypes,
            wintypes=ctypes.wintypes,
            kernel32=SimpleNamespace(SetFileInformationByHandle=set_information),
        ),
    )
    return dispositions


def test_windows_secure_unlink_requires_exact_file_identity(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A same-user replacement passes every DACL and final-path gate, so only
    # the volume/file identity captured from the writer's own temp fd can
    # prove the opened object is the file this writer published.
    substituted = tmp_path / "artifact.bin"
    substituted.write_bytes(b"someone else's valid file")
    original = tmp_path / "original.bin"
    original.write_bytes(b"the file this writer created")

    dispositions = _windows_unlink_harness(monkeypatch, substituted)
    files._windows_secure_unlink(substituted, os.stat(original))
    assert dispositions == []
    assert substituted.exists()

    files._windows_secure_unlink(substituted, os.stat(substituted))
    assert dispositions == [substituted]

    # A writer with no captured identity never deletes.
    files._windows_secure_unlink(substituted, None)
    assert dispositions == [substituted]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended-ACL semantics")
def test_darwin_secure_open_rejects_files_with_extended_acls(tmp_path) -> None:
    target = tmp_path / "leaky.bin"
    files.atomic_write_owner_only_bytes(target, b"secret")

    # Mode bits stay 0600, but the ACL grants another principal read access.
    subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(target)], check=True)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    with pytest.raises(files.SecureFileError, match="ACL"):
        files.secure_open_owner_only(target, read=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended-ACL semantics")
def test_darwin_created_secure_files_strip_inherited_acls(tmp_path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a", "everyone allow read,file_inherit", str(shared)], check=True)

    # secure_open create path: born with inherited entries, stripped before
    # verification; the reject-on-open reopen proves none survived.
    created = shared / "log.txt"
    fd = files.secure_open_owner_only(created, write=True, create=True)
    os.close(fd)
    fd = files.secure_open_owner_only(created, read=True)
    os.close(fd)

    # atomic-write temp creation path strips too.
    published = shared / "settings.json"
    files.atomic_write_owner_only_bytes(published, b"{}")
    fd = files.secure_open_owner_only(published, read=True)
    os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended-ACL semantics")
def test_darwin_strip_inherited_acl_helpers_leave_nothing_to_inherit(tmp_path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read,list,search,file_inherit,directory_inherit", str(shared)],
        stdin=subprocess.DEVNULL,
        check=True,
    )

    def has_acl(path: Path) -> bool:
        fd = os.open(path, os.O_RDONLY)
        try:
            files._verify_darwin_no_acl(fd)
        except files.SecureFileError:
            return True
        finally:
            os.close(fd)
        return False

    stripped = shared / "stripped"
    stripped.mkdir(mode=0o700)
    assert has_acl(stripped)
    files.strip_inherited_directory_acl(stripped)
    assert not has_acl(stripped)
    (stripped / "nested").mkdir()
    (stripped / "nested" / "data.json").write_text("{}", encoding="utf-8")
    assert not has_acl(stripped / "nested")
    assert not has_acl(stripped / "nested" / "data.json")

    with open(shared / "legacy.json", "wb") as out:
        assert has_acl(shared / "legacy.json")
        files.strip_inherited_acl(out.fileno())
    assert not has_acl(shared / "legacy.json")


@pytest.mark.parametrize(
    ("entry_rc", "entry_errno", "expectation"),
    [
        (-1, 22, "clean"),  # -1/EINVAL: exhausted enumeration, the only accept
        (-1, 5, "raises"),  # EIO: an inspection failure must fail closed
        (-1, 0, "raises"),  # -1 without errno is not a proven-empty ACL either
        (1, 22, "raises"),  # undocumented rc with EINVAL residue is no proof
    ],
)
def test_darwin_acl_enumeration_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    entry_rc: int,
    entry_errno: int,
    expectation: str,
) -> None:
    import ctypes

    def acl_get_entry(_acl: object, _entry_id: object, _entry: object) -> int:
        ctypes.set_errno(entry_errno)
        return entry_rc

    monkeypatch.setattr(
        files,
        "_darwin_acl_api",
        lambda: SimpleNamespace(
            ctypes=ctypes,
            libc=SimpleNamespace(
                acl_get_fd=lambda _fd: 1,
                acl_get_entry=acl_get_entry,
                acl_free=lambda _acl: 0,
            ),
        ),
    )

    if expectation == "clean":
        files._verify_darwin_no_acl(0)
    else:
        with pytest.raises(files.SecureFileError, match="enumerate"):
            files._verify_darwin_no_acl(0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX cleanup identity check")
def test_posix_atomic_write_cleanup_leaves_substituted_targets_alone(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "substituted.bin"
    calls = 0

    def substitute_then_fail(_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            # A concurrent writer replaces the entry before the cleanup runs;
            # the cleanup must notice the identity change and leave it alone.
            target.unlink()
            target.write_bytes(b"substitute")
            raise files.SecureFileError("post-publish verification failed")

    monkeypatch.setattr(files, "verify_owner_only_fd", substitute_then_fail)

    with pytest.raises(files.SecureFileError, match="post-publish"):
        files.atomic_write_owner_only_bytes(target, b"sensitive")

    assert target.read_bytes() == b"substitute"


# --------------------------------------------------------- secure_open_owner_only_append


def test_secure_open_owner_only_append_creates_then_reopens(tmp_path) -> None:
    """Create-or-open, append semantics, owner-only mode, binary bytes (no newline translation)."""
    target = tmp_path / "events.jsonl"
    created = files.secure_open_owner_only_append(target)
    assert created.created is True
    try:
        os.write(created.fd, b'{"a":1}\n')
    finally:
        os.close(created.fd)
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    reopened = files.secure_open_owner_only_append(target)
    assert reopened.created is False
    try:
        os.write(reopened.fd, b'{"b":2}\n')
    finally:
        os.close(reopened.fd)
    assert target.read_bytes() == b'{"a":1}\n{"b":2}\n'
    assert b"\r\n" not in target.read_bytes()


def test_secure_open_owner_only_append_always_appends_even_after_seek(tmp_path) -> None:
    target = tmp_path / "events.jsonl"
    handle = files.secure_open_owner_only_append(target)
    try:
        os.write(handle.fd, b"first\n")
        os.lseek(handle.fd, 0, os.SEEK_SET)
        os.write(handle.fd, b"second\n")
    finally:
        os.close(handle.fd)
    assert target.read_bytes() == b"first\nsecond\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode verification")
def test_secure_open_owner_only_append_rejects_insecure_existing_mode(tmp_path) -> None:
    target = tmp_path / "events.jsonl"
    target.write_bytes(b"")
    target.chmod(0o644)
    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only_append(target)


def test_secure_open_owner_only_append_rejects_linked_parent_and_final_symlink(tmp_path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only_append(linked_parent / "events.jsonl")
    assert not (real_parent / "events.jsonl").exists()

    real_file = tmp_path / "real.jsonl"
    real_file.write_bytes(b"")
    if os.name != "nt":
        real_file.chmod(0o600)
    link = tmp_path / "link.jsonl"
    try:
        link.symlink_to(real_file)
    except OSError:
        pytest.skip("file symlink creation is unavailable")
    with pytest.raises((files.SecureFileError, OSError)):
        files.secure_open_owner_only_append(link)


def test_secure_open_owner_only_append_returns_blocking_descriptor(tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("O_NONBLOCK is POSIX-only")
    import fcntl

    target = tmp_path / "events.jsonl"
    handle = files.secure_open_owner_only_append(target)
    try:
        assert fcntl.fcntl(handle.fd, fcntl.F_GETFL) & os.O_NONBLOCK == 0
        assert fcntl.fcntl(handle.fd, fcntl.F_GETFL) & os.O_APPEND
    finally:
        os.close(handle.fd)


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to rename a directory with an open child")
def test_secure_open_owner_only_append_keeps_parent_renamable_while_held(tmp_path) -> None:
    """A held log does not pin its directory against the tombstone rename.

    POSIX only: names and open files are independent there, so logical
    deletion can move the whole session directory out of the way while a
    stuck writer keeps writing into it. The Windows half of that story is the
    test below.
    """
    session_dir = tmp_path / "session"
    trajectory_dir = session_dir / "trajectory"
    trajectory_dir.mkdir(parents=True)
    handle = files.secure_open_owner_only_append(trajectory_dir / "events.jsonl")
    try:
        os.write(handle.fd, b"before\n")
        tombstone = tmp_path / "tombstone"
        os.rename(session_dir, tombstone)
        os.write(handle.fd, b"after\n")
    finally:
        os.close(handle.fd)
    assert (tombstone / "trajectory" / "events.jsonl").read_bytes() == b"before\nafter\n"
    assert not session_dir.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_secure_open_owner_only_append_lets_a_held_log_be_deleted(tmp_path) -> None:
    """What ``FILE_SHARE_DELETE`` buys, and what it does not.

    The sweep removes a session whose writer never let go, so the *file* has
    to be deletable while held — CPython's default ``open()`` would refuse
    that. Its directory still cannot be renamed out from under the handle,
    which is why logical deletion records an intent and sweeps later instead.
    """
    session_dir = tmp_path / "session"
    trajectory_dir = session_dir / "trajectory"
    trajectory_dir.mkdir(parents=True)
    events = trajectory_dir / "events.jsonl"
    handle = files.secure_open_owner_only_append(events)
    try:
        os.write(handle.fd, b"before\n")
        with pytest.raises(OSError):
            os.rename(session_dir, tmp_path / "tombstone")
        os.unlink(events)
        # Deletion is pending until the handle goes, and the writer that holds
        # it keeps writing meanwhile — the same shape POSIX gives an unlinked
        # inode, which is what makes the sweep safe to run behind a live writer.
        os.write(handle.fd, b"after\n")
    finally:
        os.close(handle.fd)


def test_windows_secure_open_share_mode_includes_delete_sharing() -> None:
    assert files.WINDOWS_SECURE_OPEN_SHARE_MODE & files._WINDOWS_SHARE_DELETE
    assert files.WINDOWS_SECURE_OPEN_SHARE_MODE & files._WINDOWS_SHARE_READ
    assert files.WINDOWS_SECURE_OPEN_SHARE_MODE & files._WINDOWS_SHARE_WRITE


def test_windows_secure_open_owner_only_append_branch_maps_errors(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows branch: create → ALREADY_EXISTS → open existing, both in append mode."""
    calls: list[dict[str, Any]] = []
    sentinel_fd = 4242

    def fake_windows_secure_open(path, **kwargs):
        calls.append(dict(kwargs))
        if kwargs["create"]:
            raise files.SecureFileError(183, "exists")
        return sentinel_fd

    monkeypatch.setattr(files, "_is_windows", lambda: True)
    monkeypatch.setattr(files, "_validate_link_free_parent", lambda _path: None)
    monkeypatch.setattr(files, "_windows_secure_open", fake_windows_secure_open)

    handle = files.secure_open_owner_only_append(tmp_path / "events.jsonl")
    assert handle.fd == sentinel_fd
    assert handle.created is False
    assert [call["create"] for call in calls] == [True, False]
    assert all(call["append"] is True for call in calls)
    # Readable as well as writable: recovery scans the handle it appends through.
    assert all(call["write"] is True and call["read"] is True for call in calls)


def test_windows_secure_open_owner_only_append_retries_vanishing_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = iter(
        [
            files.SecureFileError(183, "exists"),  # create: already exists
            files.SecureFileError(2, "gone"),  # open existing: vanished
            31337,  # create again: success
        ]
    )

    def fake_windows_secure_open(path, **kwargs):
        outcome = next(sequence)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(files, "_is_windows", lambda: True)
    monkeypatch.setattr(files, "_validate_link_free_parent", lambda _path: None)
    monkeypatch.setattr(files, "_windows_secure_open", fake_windows_secure_open)

    handle = files.secure_open_owner_only_append(tmp_path / "events.jsonl")
    assert handle.fd == 31337
    assert handle.created is True


def test_windows_secure_open_owner_only_append_propagates_other_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_windows_secure_open(path, **kwargs):
        raise files.SecureFileError(5, "access denied")

    monkeypatch.setattr(files, "_is_windows", lambda: True)
    monkeypatch.setattr(files, "_validate_link_free_parent", lambda _path: None)
    monkeypatch.setattr(files, "_windows_secure_open", fake_windows_secure_open)
    with pytest.raises(files.SecureFileError):
        files.secure_open_owner_only_append(tmp_path / "events.jsonl")
