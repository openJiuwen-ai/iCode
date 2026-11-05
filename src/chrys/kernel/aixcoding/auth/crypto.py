# ruff: noqa: RUF001
# (Chinese log messages keep full-width punctuation by design,
#  aixcoding/docs/batches/logging-routing-fixes/design.md signed off 2026-11-05)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OS-level encryption backends for the stored credential -- zero new deps.

The design mirrors the reference app's Electron ``safeStorage`` behaviour:

* Windows: DPAPI via ``crypt32.dll`` (``CryptProtectData`` / ``CryptUnprotectData``)
* macOS: the ``/usr/bin/security`` CLI (generic-password items)
* Linux: explicit plaintext, flagged ``degraded`` (matches the reference's
  ``setUsePlainTextEncryption(true)``)
* anywhere else, or when a backend probe fails: an in-process
  :class:`MemoryBackend` that never touches disk

Nothing here ever raises on an unavailable backend -- :func:`get_backend`
probes with a round trip and degrades to memory instead, so a headless RDP
session or a locked keychain costs a re-login, not a crash.

The ctypes style matches the host package's ``_win32_clipboard_api``:
``WinDLL(..., use_last_error=True)`` plus explicit ``argtypes``/``restype``.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys
from typing import Protocol, runtime_checkable

# AIxCoding logging (batches/logging-routing-fixes, A2): literal-name loggers on
# the aixcoding subtree — the former __name__ logger sat outside it, its DEBUG
# records silenced by root's INFO threshold; zero-dependency style as session.py.
_log = logging.getLogger("aixcoding.auth")
_detail_log = logging.getLogger("aixcoding.detail.auth")

#: AIxCoding detail logging: mirrors DETAIL_LEVEL in diagnostics.logging_support
#: (ADR-0017); duplicated locally — kernel cross-subpackage imports are
#: forbidden (test_layering).
_DETAIL_LEVEL = 5

#: Keychain service name on macOS, and the DPAPI entropy label everywhere else.
LABEL = "aixcoding-account-auth"

#: `CRYPTPROTECT_UI_FORBIDDEN`: never allow a credential prompt to pop UI.
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


@runtime_checkable
class ProtectBackend(Protocol):
    """Anything that can round-trip the credential plaintext."""

    @property
    def degraded(self) -> bool:
        """Whether this backend persists ciphertext in a recoverable way."""
        ...

    def protect(self, plaintext: bytes, label: str = LABEL) -> bytes:
        """Encrypt ``plaintext``; return opaque ciphertext bytes."""
        ...

    def unprotect(self, ciphertext: bytes, label: str = LABEL) -> bytes:
        """Reverse :meth:`protect`; raise ``AuthError``-adjacent failures."""
        ...


class MemoryBackend:
    """Last-resort backend: an in-process dict, never written to disk."""

    def __init__(self) -> None:
        self._vault: dict[tuple[bytes, str], bytes] = {}

    @property
    def degraded(self) -> bool:
        """Always true: credentials die with the process."""
        return True

    def protect(self, plaintext: bytes, label: str = LABEL) -> bytes:
        token = os.urandom(16)
        self._vault[(token, label)] = plaintext
        return token

    def unprotect(self, ciphertext: bytes, label: str = LABEL) -> bytes:
        try:
            return self._vault.pop((ciphertext, label))
        except KeyError:
            raise ValueError("memory backend token not found") from None


class PlaintextBackend:
    """Identity "encryption" -- Linux downgrade, mirroring safeStorage.

    The envelope still base64s these bytes, so the on-disk form is the
    reversible plaintext; file permissions (0600) are the only guard, exactly
    like the reference's ``setUsePlainTextEncryption(true)``.
    """

    @property
    def degraded(self) -> bool:
        """True: the on-disk form is trivially reversible."""
        return True

    def protect(self, plaintext: bytes, label: str = LABEL) -> bytes:
        """Return ``plaintext`` unchanged."""
        return plaintext

    def unprotect(self, ciphertext: bytes, label: str = LABEL) -> bytes:
        """Return ``ciphertext`` unchanged."""
        return ciphertext


class WindowsDPAPI:
    """DPAPI via ctypes: per-user encryption under ``%APPDATA%`` isolation."""

    def __init__(self) -> None:
        import ctypes.wintypes as wintypes

        class _DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(wintypes.BYTE)),
            ]

        self._blob_type = _DATA_BLOB
        # WinDLL is a win32-only ctypes member; access it dynamically so
        # non-Windows type checks resolve it without per-platform ignores.
        windll = getattr(ctypes, "WinDLL")  # noqa: B009 — dynamic on purpose, see comment above
        crypt32 = windll("crypt32", use_last_error=True)
        kernel32 = windll("kernel32", use_last_error=True)

        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),  # pDataIn
            wintypes.LPCWSTR,  # szDataDescr
            ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            wintypes.DWORD,  # dwFlags
            ctypes.POINTER(_DATA_BLOB),  # pDataOut
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),  # pDataIn
            ctypes.POINTER(wintypes.LPCWSTR),  # ppszDataDescr
            ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            wintypes.DWORD,  # dwFlags
            ctypes.POINTER(_DATA_BLOB),  # pDataOut
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        self._crypt32 = crypt32
        self._kernel32 = kernel32
        self._wintypes = wintypes

    @property
    def degraded(self) -> bool:
        """False: DPAPI ciphertext is only reversible by this user."""
        return False

    def _into_blob(self, data: bytes) -> object:
        buffer = ctypes.create_string_buffer(data, len(data))
        byte_ptr = ctypes.cast(buffer, ctypes.POINTER(self._wintypes.BYTE))
        return self._blob_type(cbData=len(data), pbData=byte_ptr)

    def _from_blob(self, blob: object) -> bytes:
        raw = ctypes.string_at(blob.pbData, blob.cbData)  # ty: ignore[unresolved-attribute]
        self._kernel32.LocalFree(blob.pbData)  # ty: ignore[unresolved-attribute]
        return raw

    def _run(self, data: bytes, label: str, *, unprotect: bool) -> bytes:
        in_blob = self._into_blob(data)
        entropy = self._into_blob(label.encode("utf-8"))
        out_blob = self._blob_type()
        func = self._crypt32.CryptUnprotectData if unprotect else self._crypt32.CryptProtectData
        ok = func(
            ctypes.byref(in_blob),  # ty: ignore[invalid-argument-type]
            None,
            ctypes.byref(entropy),  # ty: ignore[invalid-argument-type]
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(f"DPAPI call failed: win32 error {getattr(ctypes, 'get_last_error')()}")  # noqa: B009 — win32-only member, dynamic for cross-platform typecheck
        return self._from_blob(out_blob)

    def protect(self, plaintext: bytes, label: str = LABEL) -> bytes:
        """Encrypt with per-user DPAPI scope."""
        return self._run(plaintext, label, unprotect=False)

    def unprotect(self, ciphertext: bytes, label: str = LABEL) -> bytes:
        """Decrypt; a wrong user or tampered blob raises ``OSError``."""
        return self._run(ciphertext, label, unprotect=True)


class MacOSSecurity:
    """macOS Keychain generic passwords via the ``security`` CLI."""

    _TIMEOUT_SECONDS = 2

    @property
    def degraded(self) -> bool:
        """False: the ciphertext lives in the user's keychain."""
        return False

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed binary, fixed args
            ["/usr/bin/security", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=self._TIMEOUT_SECONDS,
            check=check,
        )

    def protect(self, plaintext: bytes, label: str = LABEL) -> bytes:
        """Store ``plaintext`` under a fresh random account, return the key."""
        account = os.urandom(12).hex()
        proc = self._run(
            [
                "add-generic-password",
                "-a",
                account,
                "-s",
                label,
                "-w",
                base64.b64encode(plaintext).decode("ascii"),
                "-U",
            ],
            check=False,
        )
        if proc.returncode != 0:
            raise OSError(f"security add-generic-password failed: {proc.stderr.strip()}")
        return account.encode("ascii")

    def unprotect(self, ciphertext: bytes, label: str = LABEL) -> bytes:
        """Fetch by account; a locked keychain or miss raises ``OSError``."""
        account = ciphertext.decode("ascii", errors="strict")
        try:
            proc = self._run(["find-generic-password", "-a", account, "-s", label, "-w"])
        except (subprocess.SubprocessError, OSError) as exc:
            raise OSError(f"security find-generic-password failed: {exc}") from exc
        return base64.b64decode(proc.stdout.strip(), validate=True)


def get_backend() -> ProtectBackend:
    """Probe the platform backend and degrade to memory when it fails.

    A round trip on throwaway data decides: any exception (missing DLL,
    headless DPAPI failure, locked keychain, CLI absent) selects
    :class:`MemoryBackend`, which the status surface can report as
    "log in again after restart" instead of crashing the app.
    """
    for factory in _platform_factories():
        try:
            candidate = factory()
            probe = b"aixcoding-backend-probe"
            if candidate.unprotect(candidate.protect(probe)) == probe:
                return candidate
            # AIxCoding logging (batches/logging-routing-fixes, A2): the silent
            # round-trip mismatch path previously carried no record at all.
            _detail_log.log(_DETAIL_LEVEL, "加密后端 %s 往返校验不一致。", getattr(factory, "__name__", factory))
        except Exception as exc:
            # AIxCoding logging (batches/logging-routing-fixes, A2): technical
            # reason on the detail volume only, never the panel/main volume.
            _detail_log.log(_DETAIL_LEVEL, "加密后端 %s 探测失败：%s", getattr(factory, "__name__", factory), exc)
            continue
    # AIxCoding logging (batches/logging-routing-fixes, A2): the fallback is a
    # user-facing state change (credentials die with the process) — a public
    # WARNING double-write per DEVELOPMENT.md §9; detail keeps the full picture.
    _log.warning("系统凭据库不可用，登录凭证将仅保存在内存中，重启后需重新登录。")
    _detail_log.log(_DETAIL_LEVEL, "系统凭据库不可用，登录凭证将仅保存在内存中，重启后需重新登录。")
    return MemoryBackend()


def _platform_factories() -> list:
    """Backend candidates for this OS, best first."""
    if sys.platform == "win32":
        return [WindowsDPAPI]
    if sys.platform == "darwin":
        return [MacOSSecurity]
    return [PlaintextBackend]
