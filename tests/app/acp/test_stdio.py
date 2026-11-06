# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP stdio wire discipline."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import suppress
from typing import BinaryIO

_INITIALIZE_RESPONSE_TIMEOUT_SECONDS = 45


def _drain_lines(stream: BinaryIO, sink: queue.Queue[bytes]) -> None:
    try:
        for line in stream:
            sink.put(line)
    finally:
        sink.put(b"")


def test_acp_stdio_stdout_contains_only_json_rpc(tmp_path) -> None:
    home = tmp_path / "home"
    env = os.environ.copy()
    env["HOME"] = os.fspath(home)
    # The child is a fresh process, so the in-process config-dir guard cannot
    # reach it and ``HOME`` is all it has to go on — except on Windows, which
    # derives the config directory from these two instead. Without them the
    # child boots against the developer's real ``%APPDATA%\chrys`` and runs its
    # startup migrations there.
    env["USERPROFILE"] = os.fspath(home)
    env["APPDATA"] = os.fspath(tmp_path / "appdata")
    env["NO_PROXY"] = "[::1]"
    # Keep user configuration and the session workspace private. Exercise the
    # installed package without reading repository configuration.
    home.mkdir()
    stderr_path = tmp_path / "acp-stderr.log"
    startup_failure: str | None = None
    line = b""
    with stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "chrys.app.cli.app", "acp"],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        # Read protocol bytes, independent of Windows' ambient text code page.
        # A file sink preserves stderr even on startup crashes and cannot block
        # the child behind a full stderr pipe or a failed decoder thread.
        stdout_lines: queue.Queue[bytes] = queue.Queue()
        reader = threading.Thread(target=_drain_lines, args=(proc.stdout, stdout_lines), daemon=True)
        reader.start()
        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
            try:
                proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                proc.stdin.flush()
                # A cold interpreter imports the full ACP stack before reading
                # stdin. Preserve a bounded startup budget under xdist load.
                line = stdout_lines.get(timeout=_INITIALIZE_RESPONSE_TIMEOUT_SECONDS)
                if not line:
                    startup_failure = "ACP server closed stdout before sending initialize response"
            except queue.Empty:
                startup_failure = (
                    f"ACP server did not write an initialize response within {_INITIALIZE_RESPONSE_TIMEOUT_SECONDS}s"
                )
            except BrokenPipeError:
                startup_failure = "ACP server closed stdin before receiving initialize request"
            with suppress(BrokenPipeError):
                proc.stdin.close()
            if startup_failure is None:
                proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            reader.join(timeout=5)
            proc.stdout.close()

    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    diagnostics = f"returncode={proc.returncode}\nstderr:\n{stderr}"
    assert startup_failure is None, f"{startup_failure}\n{diagnostics}"
    response = json.loads(line)
    assert response["jsonrpc"] == "2.0", diagnostics
    assert response["id"] == 1, diagnostics
    assert "result" in response, diagnostics
    assert proc.returncode == 0, diagnostics
    remaining: list[bytes] = []
    while True:
        try:
            extra = stdout_lines.get_nowait()
        except queue.Empty:
            break
        if extra and extra.strip():
            remaining.append(extra)
    assert remaining == []
    assert "NO_PROXY is invalid" in stderr, diagnostics
