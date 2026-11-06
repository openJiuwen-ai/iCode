# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for pasted/dropped image path conversion in the chat input."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import struct
import subprocess
import tempfile
import zlib
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
from textual.app import App, ComposeResult
from textual.events import Paste
from textual.widgets.text_area import Selection

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chrome import image_paste
from chrys.app.tui.widgets.chrome.image_paste import (
    pasted_image_paths,
    save_clipboard_image_to_file,
)
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.foundation.config.settings import SESSION_ROOT_DIR_ENV_VAR
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.mentions import format_file_mention
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.engine.run.attachments import parse_image_mentions
from chrys.service.state.store import JsonFileStateStore
from tests.support.images import image_bytes
from tests.support.tui_helpers import png_bytes
from tests.support.waiting import wait_for


class _InputBarApp(App):
    def compose(self) -> ComposeResult:
        yield InputBar()


class _MainScreenPasteApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


def _write_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes((200, 40, 40)))
    return path


def _assert_labelled(input_bar: InputBar, *images: Path, before: str = "") -> None:
    """The draft shows one ``[Image #n]`` label per image, and sends each as its ``@file`` mention."""
    numbers = range(1, len(images) + 1)
    assert input_bar.value == before + " ".join(f"[Image #{number}]" for number in numbers) + " "
    references = input_bar.draft().references
    stored = [entry.content if (entry := references.get("image", number)) else None for number in numbers]
    assert all(path is not None for path in stored)
    assert [Path(path).resolve() for path in stored if path is not None] == [image.resolve() for image in images]
    mentions = " ".join(format_file_mention(path) for path in stored if path is not None)
    assert input_bar.draft().sent_text() == f"{before}{mentions}"


def _jxa_destination(script: str) -> str:
    """Return the destination path the generated JXA writes to, decoded from its string literal."""
    match = re.search(r'writeToFileAtomically\(("(?:[^"\\]|\\.)*"), false\)', script)
    assert match is not None, script
    return json.loads(match.group(1))


def _record_clipboard_retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace image_paste's own ``time`` with a shadow that records sleeps instead of taking them."""
    delays: list[float] = []
    shadow_time = ModuleType("time")
    shadow_time.sleep = delays.append
    monkeypatch.setattr(image_paste, "time", shadow_time)
    return delays


def _grab_macos_clipboard_to(monkeypatch: pytest.MonkeyPatch, grab_path: Path) -> None:
    """Run the macOS clipboard path with its pasteboard bytes written to *grab_path*, reserved empty as in production."""

    def reserve_grab_file() -> Path:
        grab_path.touch()
        return grab_path

    monkeypatch.setattr(image_paste, "get_platform", lambda: SimpleNamespace(is_macos=True, is_windows=False))
    monkeypatch.setattr(image_paste, "_reserve_macos_grab_file", reserve_grab_file)


def _serve_pasteboard(
    monkeypatch: pytest.MonkeyPatch, grab_path: Path, payloads: dict[str, bytes]
) -> list[tuple[str, ...]]:
    """Run the macOS clipboard path against a pasteboard holding *payloads* by type; return the type groups fetched."""
    fetched: list[tuple[str, ...]] = []

    def fetch(destination: Path, types: Sequence[str], *, timeout: float) -> bool:
        fetched.append(tuple(types))
        data = next((payloads[candidate] for candidate in types if candidate in payloads), None)
        if data is None:
            return False
        destination.write_bytes(data)
        return True

    _grab_macos_clipboard_to(monkeypatch, grab_path)
    monkeypatch.setattr(image_paste, "_run_macos_jxa_fetch", fetch)
    return fetched


def _shadow_subprocess(monkeypatch: pytest.MonkeyPatch, run: Callable[..., object]) -> None:
    """Replace image_paste's own ``subprocess`` with a shadow whose ``run`` is *run*."""
    shadow_subprocess = ModuleType("subprocess")
    shadow_subprocess.run = run
    shadow_subprocess.DEVNULL = subprocess.DEVNULL
    shadow_subprocess.SubprocessError = subprocess.SubprocessError
    shadow_subprocess.TimeoutExpired = subprocess.TimeoutExpired
    monkeypatch.setattr(image_paste, "subprocess", shadow_subprocess)


def _jxa_types(script: str) -> list[str]:
    """Return the pasteboard types the generated JXA looks for, in order."""
    match = re.search(r"const imageType = (\[.*?\])\.find\(", script)
    assert match is not None, script
    return json.loads(match.group(1))


def _png_with_corrupt_chunk() -> bytes:
    """Encode a PNG with a broken chunk between its two IDAT chunks; Pillow raises SyntaxError when decoding it."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width = height = 8
    pixels = zlib.compress(b"".join(b"\x00" + bytes([row]) * (width * 3) for row in range(height)))
    half = len(pixels) // 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", pixels[:half])
        + b"\x00\x00\x00\x04\x01\x02\x03\x04garbage!"
        + chunk(b"IDAT", pixels[half:])
        + chunk(b"IEND", b"")
    )


def _png_bytes_pillow_would_not_write() -> bytes:
    """Encode a PNG at a non-default compression level, so a decode-and-save changes its bytes."""
    out = BytesIO()
    Image.new("RGB", (2, 2), (80, 120, 160)).save(out, format="PNG", compress_level=1)
    payload = out.getvalue()
    assert payload != png_bytes((80, 120, 160), size=(2, 2))
    return payload


def test_clipboard_image_dir_for_session_uses_session_attachment_subdir() -> None:
    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / f"short-{session_id}"

    state_store = _FakeStateStore()

    assert image_paste.clipboard_image_dir_for_session(state_store, "abc123") == Path(
        "/sessions/short-abc123/attachments/clipboard"
    )
    assert image_paste.clipboard_image_dir_for_session(None, "abc123") is None
    assert image_paste.clipboard_image_dir_for_session(state_store, None) is None


def test_clipboard_image_dir_for_session_uses_configured_session_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(custom_root))
    state_store = JsonFileStateStore()

    assert image_paste.clipboard_image_dir_for_session(state_store, "abc123") == (
        custom_root / "sessions" / session_short_id("abc123") / "attachments" / "clipboard"
    )


def test_pasted_image_outside_cwd_resolves_to_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = _write_image(tmp_path / "outside image.png")

    paths = pasted_image_paths(str(image), cwd=workspace)

    assert paths == [image.resolve()]
    parsed = parse_image_mentions(format_file_mention(paths[0]), cwd=workspace)
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].path == image.resolve()


def test_pasted_relative_image_path_uses_active_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    image = _write_image(workspace / "shot.png")

    assert pasted_image_paths("shot.png", cwd=workspace) == [image.resolve()]


@pytest.mark.skipif(os.name != "nt", reason="Windows backslash paths require Windows path semantics")
def test_pasted_windows_backslash_image_path_is_recognized(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "Users" / "me" / "shot.png")

    assert pasted_image_paths("Users\\me\\shot.png", cwd=tmp_path) == [image.resolve()]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell escaping is not used for Windows file drops")
def test_pasted_posix_shell_escaped_image_path_is_recognized(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "screen shot.png")
    payload = str(image).replace(" ", "\\ ")

    assert pasted_image_paths(payload, cwd=tmp_path) == [image.resolve()]


def test_pasted_file_uri_image_path_is_recognized(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "screen shot.webp")

    assert pasted_image_paths(image.as_uri(), cwd=tmp_path) == [image.resolve()]


def test_pasted_unsupported_file_uri_is_not_an_image_paste(tmp_path: Path) -> None:
    text = "file://server/share/shot.png"

    assert pasted_image_paths(text, cwd=tmp_path) == []


def test_pasted_multiple_image_paths_are_recognized(tmp_path: Path) -> None:
    first = _write_image(tmp_path / "first.png")
    second = _write_image(tmp_path / "second.jpg")

    assert pasted_image_paths(f"{first}\n{second}\n", cwd=tmp_path) == [first.resolve(), second.resolve()]


def test_pasted_prose_is_not_an_image_paste(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "shot.png")
    text = f"please inspect {image}"

    assert pasted_image_paths(text, cwd=tmp_path) == []


def test_pasted_malformed_path_is_not_an_image_paste(tmp_path: Path) -> None:
    text = "a\0.png"

    assert pasted_image_paths(text, cwd=tmp_path) == []


def test_pasted_invalid_urlish_text_is_not_an_image_paste(tmp_path: Path) -> None:
    text = "// a\uff1a"

    assert pasted_image_paths(text, cwd=tmp_path) == []


def test_pasted_unknown_user_home_path_is_not_an_image_paste(tmp_path: Path) -> None:
    text = "~nosuch_chrys_user_xyz/shot.png"

    assert pasted_image_paths(text, cwd=tmp_path) == []


def test_pasted_prose_does_not_stat_each_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fail_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        calls.append(str(self))
        raise AssertionError("prose paste should fail before filesystem resolution")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    text = " ".join(f"token-{i}" for i in range(500))

    assert pasted_image_paths(text, cwd=tmp_path) == []
    assert calls == []


def test_pasted_mixed_path_group_stops_before_resolving_later_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original_resolve = Path.resolve
    image = _write_image(tmp_path / "shot.png")

    def record_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        calls.append(str(self))
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolve)

    text = f"not-an-image {image}"

    assert pasted_image_paths(text, cwd=tmp_path) == []
    assert calls == []


@pytest.mark.parametrize("os_name", ["macos", "windows"])
def test_platform_clipboard_bitmap_is_saved_to_session_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
) -> None:
    image = Image.new("RGB", (2, 2), color=(80, 120, 160))
    platform = SimpleNamespace(is_macos=os_name == "macos", is_windows=os_name == "windows")

    monkeypatch.setattr(image_paste, "get_platform", lambda: platform)
    monkeypatch.setattr(image_paste, "_grab_clipboard", lambda: image)
    directory = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    path = save_clipboard_image_to_file(directory)

    assert path is not None
    assert path.parent == directory
    assert path.suffix == ".png"
    with Image.open(path) as saved:
        assert saved.size == (2, 2)


def test_clipboard_bitmap_without_session_directory_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = SimpleNamespace(is_macos=False, is_windows=True)

    monkeypatch.setattr(image_paste, "get_platform", lambda: platform)
    monkeypatch.setattr(image_paste, "_grab_clipboard", lambda: pytest.fail("clipboard should not be read"))

    assert save_clipboard_image_to_file(None) is None


def test_windows_clipboard_grab_retries_transient_open_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    image = Image.new("RGB", (2, 2), color=(80, 120, 160))
    calls = 0

    def grabclipboard() -> Image.Image:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("clipboard temporarily unavailable")
        return image

    monkeypatch.setattr(image_paste, "get_platform", lambda: SimpleNamespace(is_macos=False, is_windows=True))
    monkeypatch.setattr("PIL.ImageGrab.grabclipboard", grabclipboard)
    delays = _record_clipboard_retry_sleeps(monkeypatch)

    assert image_paste._grab_clipboard() is image
    assert calls == 2
    assert delays == [0.02]


def test_windows_clipboard_grab_gives_up_after_five_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def busy_clipboard() -> Image.Image:
        nonlocal calls
        calls += 1
        raise OSError("failed to open clipboard")

    monkeypatch.setattr(image_paste, "get_platform", lambda: SimpleNamespace(is_macos=False, is_windows=True))
    monkeypatch.setattr("PIL.ImageGrab.grabclipboard", busy_clipboard)
    delays = _record_clipboard_retry_sleeps(monkeypatch)

    assert save_clipboard_image_to_file(tmp_path) is None
    # Each failed grab already spent Pillow's own 500 ms wait; five keep the UI freeze near 2.5 s.
    assert calls == 5
    assert delays == [0.02] * 4


def test_windows_clipboard_file_list_uses_first_supported_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = tmp_path / "notes.txt"
    doc.write_text("not an image")
    image = _write_image(tmp_path / "from file list.png")

    monkeypatch.setattr(image_paste, "get_platform", lambda: SimpleNamespace(is_macos=False, is_windows=True))
    monkeypatch.setattr(image_paste, "_grab_clipboard", lambda: [str(doc), str(image)])

    assert (
        save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR)
        == image.resolve()
    )


def test_macos_jxa_grab_moves_png_bytes_without_reencoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _png_bytes_pillow_would_not_write()
    grab_path = tmp_path / "grab.tmp"
    _serve_pasteboard(monkeypatch, grab_path, {"public.png": payload})
    directory = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    path = save_clipboard_image_to_file(directory)

    assert path is not None
    assert path.parent == directory.resolve()
    assert path.read_bytes() == payload
    assert not grab_path.exists()


def test_macos_jxa_grab_moves_png_across_volumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _png_bytes_pillow_would_not_write()
    grab_path = tmp_path / "grab.tmp"

    def refusing_the_grab_file(rename: Callable[..., None]) -> Callable[..., None]:
        def cross_device_rename(
            src: str | os.PathLike[str],
            dst: str | os.PathLike[str],
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            # The temp dir and the session dir sit on different volumes.
            if Path(src) == grab_path:
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src))
            rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        return cross_device_rename

    _serve_pasteboard(monkeypatch, grab_path, {"public.png": payload})
    monkeypatch.setattr(os, "rename", refusing_the_grab_file(os.rename))
    monkeypatch.setattr(os, "replace", refusing_the_grab_file(os.replace))

    path = save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR)

    assert path is not None
    assert path.read_bytes() == payload
    assert not grab_path.exists()


def test_macos_jxa_grab_converts_tiff_payload_to_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grab_path = tmp_path / "grab.tmp"
    fetched = _serve_pasteboard(monkeypatch, grab_path, {"public.tiff": image_bytes("TIFF")})

    path = save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR)

    assert fetched == [("public.png", "PNGf"), ("public.tiff", "TIFF")]
    assert path is not None
    assert path.suffix == ".png"
    with Image.open(path, formats=("PNG",)) as saved:
        assert saved.size == (2, 2)
    assert not grab_path.exists()


@pytest.mark.parametrize("png_payload", ["unsupported_format", "corrupt_chunk"])
def test_macos_jxa_grab_falls_back_to_tiff_when_png_cannot_be_decoded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    png_payload: str,
) -> None:
    grab_path = tmp_path / "grab.tmp"
    png = image_bytes("PCX") if png_payload == "unsupported_format" else _png_with_corrupt_chunk()
    fetched = _serve_pasteboard(monkeypatch, grab_path, {"public.png": png, "public.tiff": image_bytes("TIFF")})

    path = save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR)

    assert fetched == [("public.png", "PNGf"), ("public.tiff", "TIFF")]
    assert path is not None
    with Image.open(path, formats=("PNG",)) as saved:
        assert saved.size == (2, 2)
    assert not grab_path.exists()


@pytest.mark.parametrize("payload", ["unsupported_format", "decompression_bomb"])
def test_macos_jxa_grab_that_cannot_be_decoded_leaves_no_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    grab_path = tmp_path / "grab.tmp"
    if payload == "unsupported_format":
        payloads = {"public.png": image_bytes("PCX"), "public.tiff": image_bytes("PCX")}
    else:
        payloads = {"public.png": image_bytes("PNG"), "public.tiff": image_bytes("TIFF")}
        # Any real image against a one-pixel limit trips Pillow's decompression bomb error.
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    fetched = _serve_pasteboard(monkeypatch, grab_path, payloads)
    directory = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    assert save_clipboard_image_to_file(directory) is None
    assert len(fetched) == 2
    assert not grab_path.exists()
    assert not directory.exists()


@pytest.mark.parametrize(
    ("fetch_seconds", "expected_timeouts"),
    [(10.0, [10.0]), (3.0, [10.0, 7.0])],
    ids=["hung_fetch_stops_the_grab", "slow_fetch_leaves_the_rest"],
)
def test_macos_jxa_fetches_share_one_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_seconds: float,
    expected_timeouts: list[float],
) -> None:
    now = 100.0
    shadow_time = ModuleType("time")
    shadow_time.monotonic = lambda: now
    monkeypatch.setattr(image_paste, "time", shadow_time)
    timeouts: list[float] = []

    def osascript_without_image(
        args: list[str], *, stdin: int, capture_output: bool, check: bool, timeout: float
    ) -> object:
        nonlocal now
        timeouts.append(timeout)
        now += fetch_seconds
        if fetch_seconds >= timeout:
            raise subprocess.TimeoutExpired(args[0], timeout)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    _grab_macos_clipboard_to(monkeypatch, tmp_path / "grab.tmp")
    _shadow_subprocess(monkeypatch, osascript_without_image)

    assert save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR) is None
    assert timeouts == expected_timeouts


def test_macos_jxa_grab_converts_a_16_bit_grayscale_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = BytesIO()
    Image.new("I;16", (2, 2), 1000).save(out, format="PNG")
    grab_path = tmp_path / "grab.tmp"
    _serve_pasteboard(monkeypatch, grab_path, {"public.png": out.getvalue()})

    path = save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR)

    assert path is not None
    with Image.open(path, formats=("PNG",)) as saved:
        assert saved.mode == "RGBA"
    assert not grab_path.exists()


def test_macos_jxa_grab_removes_its_file_when_decoding_raises_past_the_grab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grab_path = tmp_path / "grab.tmp"

    def open_raising(fp: Path, mode: str = "r", formats: tuple[str, ...] | None = None) -> Image.Image:
        # Outside the grab's own suppress list; only the outer paste guard catches it.
        raise RuntimeError("decoder bug")

    _serve_pasteboard(monkeypatch, grab_path, {"public.png": image_bytes("PNG")})
    monkeypatch.setattr(Image, "open", open_raising)

    assert save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR) is None
    assert not grab_path.exists()


def test_macos_jxa_fetch_killed_after_writing_leaves_no_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grab_path = tmp_path / "grab.tmp"

    def osascript_killed_after_writing(
        args: list[str], *, stdin: int, capture_output: bool, check: bool, timeout: float
    ) -> object:
        Path(_jxa_destination(args[-1])).write_bytes(image_bytes("PNG"))
        raise subprocess.TimeoutExpired(args[0], timeout)

    _grab_macos_clipboard_to(monkeypatch, grab_path)
    _shadow_subprocess(monkeypatch, osascript_killed_after_writing)
    directory = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    assert save_clipboard_image_to_file(directory) is None
    assert not grab_path.exists()
    assert not directory.exists()


def test_macos_jxa_fetch_without_image_creates_no_attachment_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grab_path = tmp_path / "grab.tmp"
    scripts: list[str] = []
    streams: list[tuple[int, bool]] = []

    def osascript_without_image(
        args: list[str], *, stdin: int, capture_output: bool, check: bool, timeout: float
    ) -> object:
        scripts.append(args[-1])
        streams.append((stdin, capture_output))
        return SimpleNamespace(returncode=0, stdout=b"true\n", stderr=b"")

    _grab_macos_clipboard_to(monkeypatch, grab_path)
    _shadow_subprocess(monkeypatch, osascript_without_image)
    directory = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    assert save_clipboard_image_to_file(directory) is None
    assert [_jxa_types(script) for script in scripts] == [["public.png", "PNGf"], ["public.tiff", "TIFF"]]
    # osascript must neither read the TUI's stdin nor print into its terminal.
    assert streams == [(subprocess.DEVNULL, True)] * 2
    assert not grab_path.exists()
    assert not directory.exists()


def test_macos_jxa_grab_file_is_removed_when_saving_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grab_path = tmp_path / "grab.tmp"
    _serve_pasteboard(monkeypatch, grab_path, {"public.png": image_bytes("PNG")})
    session = tmp_path / "session"
    session.write_text("a file where the session folder should be", encoding="utf-8")

    assert save_clipboard_image_to_file(session / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR) is None
    assert not grab_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_macos_jxa_writes_the_pasteboard_bytes_into_an_owner_only_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes: list[int] = []

    def osascript_writing_in_place(
        args: list[str], *, stdin: int, capture_output: bool, check: bool, timeout: float
    ) -> object:
        # Like JXA's non-atomic write: open the destination for writing, creating it if missing.
        destination = Path(_jxa_destination(args[-1]))
        destination.write_bytes(image_bytes("PNG"))
        modes.append(stat.S_IMODE(destination.stat().st_mode))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(image_paste, "get_platform", lambda: SimpleNamespace(is_macos=True, is_windows=False))
    # TMPDIR may name a directory other users can list, such as /tmp.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _shadow_subprocess(monkeypatch, osascript_writing_in_place)
    previous_umask = os.umask(0o022)
    try:
        path = save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR)
    finally:
        os.umask(previous_umask)

    assert path is not None
    assert modes == [0o600]
    assert list(tmp_path.glob("chrys-clipboard-grab-*")) == []


def test_macos_jxa_script_matches_types_and_encodes_any_destination() -> None:
    destination = "/tmp/new\nline 'single' \"double\" back\\slash/grab.tmp"
    script = image_paste._macos_jxa_fetch_script(destination, ("public.png", "PNGf"))

    assert _jxa_types(script) == ["public.png", "PNGf"]
    # JXA does not bridge the pasteboard's NSString elements to JS strings;
    # only containsObject matches (verified against a real pasteboard).
    assert "pasteboard.types.containsObject(type)" in script
    assert ".js.includes" not in script
    # A raw newline would end the JavaScript string literal mid-path.
    assert "\n" not in script
    assert _jxa_destination(script) == destination


def test_clipboard_image_save_failure_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = Image.new("RGB", (2, 2), color=(80, 120, 160))

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise ValueError("cannot save")

    monkeypatch.setattr(image, "save", fail_save)

    assert image_paste._save_clipboard_image(image, directory=tmp_path / "session") is None


@pytest.mark.parametrize("payload", ["corrupt_chunk", "decompression_bomb"])
def test_windows_clipboard_image_pillow_refuses_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    def grabclipboard() -> Image.Image:
        # Like Pillow's own grab, open the clipboard bytes and leave the pixels to decode on save:
        # a broken chunk raises SyntaxError then, and an oversized image raises on open.
        data = _png_with_corrupt_chunk() if payload == "corrupt_chunk" else image_bytes("PNG")
        return Image.open(BytesIO(data), formats=("PNG",))

    if payload == "decompression_bomb":
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    monkeypatch.setattr(image_paste, "get_platform", lambda: SimpleNamespace(is_macos=False, is_windows=True))
    monkeypatch.setattr("PIL.ImageGrab.grabclipboard", grabclipboard)

    assert save_clipboard_image_to_file(tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR) is None


def test_clipboard_image_save_prunes_old_saved_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = tmp_path / "clipboard-image-old.png"
    newer = tmp_path / "clipboard-image-newer.png"
    old.write_bytes(b"old")
    newer.write_bytes(b"newer")
    os.utime(old, ns=(1, 1))
    os.utime(newer, ns=(2, 2))
    image = Image.new("RGB", (2, 2), color=(80, 120, 160))

    monkeypatch.setattr(image_paste, "_CLIPBOARD_IMAGE_KEEP_COUNT", 2)
    monkeypatch.setattr(image_paste, "_CLIPBOARD_IMAGE_KEEP_BYTES", 1024 * 1024)

    path = image_paste._save_clipboard_image(image, directory=tmp_path)

    assert path is not None
    remaining = {candidate.name for candidate in tmp_path.glob("clipboard-image-*.png")}
    assert path.name in remaining
    assert "clipboard-image-newer.png" in remaining
    assert "clipboard-image-old.png" not in remaining


@pytest.mark.asyncio
async def test_chat_input_paste_turns_image_path_into_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write_image(tmp_path / "dropped image.png")
    monkeypatch.setattr("chrys.app.tui.widgets.chrome.draft_text_area.safe_getcwd", lambda: str(tmp_path))

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        await text_area._on_paste(Paste(str(image)))
        await pilot.pause()

        _assert_labelled(input_bar, image)


@pytest.mark.asyncio
async def test_chat_input_paste_inserts_invalid_urlish_text_unchanged(tmp_path: Path) -> None:
    text = "// a\uff1a"

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        await text_area._on_paste(Paste(text))
        await pilot.pause()

        assert input_bar.value == text


@pytest.mark.asyncio
async def test_chat_input_paste_inserts_text_when_image_detection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "not an image"

    def fail_convert(_text: str, *, cwd: Path) -> list[Path]:
        raise ValueError("invalid path payload")

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.draft_text_area.pasted_image_paths", fail_convert)

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        await text_area._on_paste(Paste(text))
        await pilot.pause()

        assert input_bar.value == text


@pytest.mark.asyncio
async def test_chat_input_paste_uses_configured_workspace_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    image = _write_image(workspace / "relative.png")

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.set_paste_cwd(str(workspace))
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        await text_area._on_paste(Paste("relative.png"))
        await pilot.pause()

        _assert_labelled(input_bar, image)


@pytest.mark.asyncio
async def test_chat_input_paste_inserts_space_before_image_label_when_needed(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "shot.png")

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        input_bar.value = "look at"
        text_area.focus()
        await pilot.pause()

        await text_area._on_paste(Paste(str(image)))
        await pilot.pause()

        _assert_labelled(input_bar, image, before="look at ")


@pytest.mark.asyncio
@pytest.mark.parametrize("paste_key", [None, "shift+insert"], ids=["action", "shift-insert"])
async def test_chat_input_paste_action_and_shift_insert_convert_os_clipboard_image_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paste_key: str | None,
) -> None:
    image = _write_image(tmp_path / "clipboard.png")
    monkeypatch.setattr("chrys.app.tui.widgets.chrome.draft_text_area.safe_getcwd", lambda: str(tmp_path))

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file", lambda _dir: None
        )
        with patch("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", return_value=str(image)):
            if paste_key is None:
                text_area.action_paste()
            else:
                await pilot.press(paste_key)
        await pilot.pause()

        _assert_labelled(input_bar, image)


@pytest.mark.asyncio
async def test_chat_input_shift_insert_in_browser_uses_only_session_clipboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser shortcuts must not read the server machine's text or image clipboard."""
    clipboard_dir = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    def fail_server_clipboard_read() -> str:
        raise AssertionError("server clipboard must stay isolated from browser sessions")

    def fail_server_image_read() -> object:
        raise AssertionError("server image clipboard must stay isolated from browser sessions")

    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", fail_server_clipboard_read)
    monkeypatch.setattr("chrys.app.tui.widgets.chrome.image_paste._grab_clipboard", fail_server_image_read)

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.set_clipboard_image_dir(clipboard_dir)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        pilot.app.copy_to_clipboard("browser-session-text")
        await pilot.pause()

        await pilot.press("shift+insert")
        await pilot.pause()

        assert input_bar.value == "browser-session-text"
        assert not clipboard_dir.exists()


@pytest.mark.asyncio
async def test_chat_input_action_paste_saves_clipboard_bitmap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write_image(tmp_path / "clipboard bitmap.png")
    clipboard_dir = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR
    seen_dirs: list[Path | None] = []

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.set_clipboard_image_dir(clipboard_dir)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        pilot.app.copy_to_clipboard("stale-app-clipboard")
        await pilot.pause()

        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file",
            lambda directory: seen_dirs.append(directory) or image,
        )
        text_area.action_paste()
        await pilot.pause()

        _assert_labelled(input_bar, image)
        assert seen_dirs == [clipboard_dir]


@pytest.mark.asyncio
async def test_chat_input_empty_paste_event_saves_clipboard_bitmap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write_image(tmp_path / "empty paste bitmap.png")
    clipboard_dir = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR
    seen_dirs: list[Path | None] = []

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.set_clipboard_image_dir(clipboard_dir)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file",
            lambda directory: seen_dirs.append(directory) or image,
        )
        await text_area._on_paste(Paste(""))
        await pilot.pause()

        _assert_labelled(input_bar, image)
        assert seen_dirs == [clipboard_dir]


@pytest.mark.asyncio
async def test_chat_input_empty_paste_without_bitmap_does_not_reprobe_in_screen_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard_dir = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR
    calls: list[Path | None] = []
    app = _MainScreenPasteApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.set_clipboard_image_dir(clipboard_dir)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file",
            lambda directory: calls.append(directory) or None,
        )
        event = Paste("")
        await text_area._on_paste(event)
        app.main_screen.on_paste(event)
        await pilot.pause()

        assert input_bar.value == ""
        assert calls == [clipboard_dir]
        assert event._no_default_action is True
        assert event._stop_propagation is True


@pytest.mark.asyncio
async def test_chat_input_action_paste_falls_back_to_stale_text_when_grab_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed bitmap grab still pastes the last copied text — preserved fallback semantics."""
    clipboard_dir = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.set_clipboard_image_dir(clipboard_dir)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        pilot.app.copy_to_clipboard("stale copied text")
        await pilot.pause()

        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file", lambda _dir: None
        )
        with patch("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", return_value=""):
            text_area.action_paste()
            await wait_for(
                lambda: input_bar.value == "stale copied text",
                pilot=pilot,
                description="stale in-app clipboard text pasted after the grab failed",
            )


@pytest.mark.asyncio
async def test_input_bar_fallback_inserts_image_drop_payload(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "fallback.png")

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.value = "look at"
        await pilot.pause()

        assert input_bar.insert_image_paste_payload(str(image)) is True

        _assert_labelled(input_bar, image, before="look at ")


@pytest.mark.asyncio
async def test_input_bar_fallback_ignores_normal_prose(tmp_path: Path) -> None:
    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.value = "existing"
        await pilot.pause()

        assert input_bar.insert_image_paste_payload("please inspect /tmp/shot.png") is False

        assert input_bar.value == "existing"


@pytest.mark.asyncio
async def test_input_bar_fallback_ignores_image_detection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_convert(_text: str, *, cwd: Path) -> list[Path]:
        raise ValueError("invalid path payload")

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.draft_text_area.pasted_image_paths", fail_convert)

    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.value = "existing"
        await pilot.pause()

        assert input_bar.insert_image_paste_payload("not an image") is False
        assert input_bar.value == "existing"


@pytest.mark.asyncio
async def test_main_screen_paste_fallback_routes_image_drop_to_input(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "screen fallback.png")
    app = _MainScreenPasteApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        app.set_focus(None)
        await pilot.pause()
        assert not text_area.has_focus

        event = Paste(str(image))
        app.main_screen.on_paste(event)
        await pilot.pause()

        _assert_labelled(input_bar, image)
        assert event._no_default_action is True
        assert event._stop_propagation is True


@pytest.mark.asyncio
async def test_main_screen_empty_paste_fallback_routes_clipboard_bitmap_to_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write_image(tmp_path / "screen bitmap.png")
    clipboard_dir = tmp_path / "session" / image_paste.CLIPBOARD_IMAGE_SESSION_SUBDIR
    app = _MainScreenPasteApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.set_clipboard_image_dir(clipboard_dir)
        app.set_focus(None)
        await pilot.pause()

        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file", lambda _dir: image
        )
        event = Paste("")
        app.main_screen.on_paste(event)
        await pilot.pause()

        _assert_labelled(input_bar, image)
        assert event._no_default_action is True
        assert event._stop_propagation is True


@pytest.mark.asyncio
async def test_main_screen_paste_fallback_ignores_prose_payload(tmp_path: Path) -> None:
    app = _MainScreenPasteApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        await pilot.pause()

        event = Paste("please inspect /tmp/shot.png")
        app.main_screen.on_paste(event)
        await pilot.pause()

        assert input_bar.value == ""
        assert event._no_default_action is False
        assert event._stop_propagation is False


@pytest.mark.asyncio
async def test_main_screen_paste_fallback_skips_shell_mode(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "shell.png")
    app = _MainScreenPasteApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        app.main_screen._state.shell.active = True
        await pilot.pause()

        event = Paste(str(image))
        app.main_screen.on_paste(event)
        await pilot.pause()

        assert input_bar.value == ""
        assert event._no_default_action is False
        assert event._stop_propagation is False


@pytest.mark.asyncio
async def test_focused_chat_input_image_paste_does_not_double_insert_when_bubbled(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "focused.png")
    app = _MainScreenPasteApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await pilot.pause()

        event = Paste(str(image))
        await text_area._on_paste(event)
        app.main_screen.on_paste(event)
        await pilot.pause()

        _assert_labelled(input_bar, image)


@pytest.mark.asyncio
async def test_an_empty_paste_with_no_clipboard_image_leaves_the_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal pastes nothing for a screenshot; with no picture to take, nothing changes."""
    async with _InputBarApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        input_bar.value = "hello world"
        text_area.selection = Selection((0, 0), (0, 5))
        await pilot.pause()
        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.draft_text_area.save_clipboard_image_to_file", lambda _dir: None
        )

        await text_area._on_paste(Paste(""))

        assert input_bar.value == "hello world"
