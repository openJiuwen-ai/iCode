# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local, per-user takeover credentials bound to one acquisition of a session lock."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from chrys.foundation.platform.files import (
    atomic_write_owner_only_text,
    secure_open_owner_only_binary,
    secure_open_regular_binary,
)
from chrys.foundation.util.session_ids import session_id_key


def read_owner_record(path: Path) -> dict[str, object]:
    try:
        with secure_open_regular_binary(path) as stream:
            raw = stream.read(4097)
        if len(raw) <= 4096:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except OSError, ValueError, RecursionError, RuntimeError:
        pass
    return {}


def credential_path(owner_path: Path, session_id: str, generation: str) -> Path:
    # A shared or copied archive contains only the public generation. Resolve
    # the physical lock directory locally; never follow a path from its JSON.
    owner = os.fsencode(os.path.normcase(owner_path.parent.resolve() / owner_path.name))
    identity = b"\0".join((owner, session_id_key(session_id).encode(), generation.encode()))
    name = hashlib.sha256(identity).hexdigest()
    # A project shell may override TMPDIR, TMP or TEMP independently of the
    # other TUI. Both processes need the same machine-local rendezvous path.
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local and Path(local).is_absolute() else Path.home() / "AppData" / "Local"
        directory = base / "Temp"
    else:
        directory = Path("/tmp")
    return directory.resolve() / f"icode-tui-{name}.key"


@dataclass(frozen=True, slots=True)
class TakeoverOwner:
    port: int
    key: bytes = field(repr=False)

    @classmethod
    def advertise(cls, port: int, path: Path, session_id: str, generation: str) -> TakeoverOwner:
        owner = cls(port, secrets.token_bytes(32))
        atomic_write_owner_only_text(credential_path(path, session_id, generation), f"{port} {owner.key.hex()}")
        return owner

    @classmethod
    def read(cls, path: Path, session_id: str, generation: str) -> TakeoverOwner | None:
        try:
            with secure_open_owner_only_binary(credential_path(path, session_id, generation)) as stream:
                port, secret = stream.read(72).split()
            if len(secret) == 64 and 0 < int(port) < 65536:
                return cls(int(port), bytes.fromhex(secret))
        except OSError, ValueError, RuntimeError:
            pass
        return None
