# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared filesystem helpers."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import ntpath
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, BinaryIO, cast

from chrys.foundation.platform.c_api import declare_functions, struct_fields

_OWNER_ONLY_MODE = 0o600
_ATOMIC_NAME_TOKEN_BYTES = 8


class SecureFileError(OSError):
    """Raised when an owner-only file cannot be opened or verified securely."""


@dataclass(frozen=True)
class _DarwinAclAPI:
    """Typed handle for the lazily loaded Darwin ACL library."""

    libc: ctypes.CDLL


@dataclass(frozen=True)
class _WindowsFileAPI:
    """Typed handles for the lazily loaded Win32 secure-file libraries."""

    advapi32: ctypes.CDLL
    kernel32: ctypes.CDLL


class _FileAttributeTagInfo(ctypes.Structure):
    """``FILE_ATTRIBUTE_TAG_INFO``, read by ``GetFileInformationByHandleEx``."""

    _fields_ = struct_fields((wintypes.DWORD, "file_attributes reparse_tag"))


class _FileDispositionInfo(ctypes.Structure):
    """``FILE_DISPOSITION_INFO``, written by ``SetFileInformationByHandle``."""

    _fields_ = struct_fields((wintypes.BOOLEAN, "delete_file"))


class _SecurityAttributes(ctypes.Structure):
    """``SECURITY_ATTRIBUTES`` carrying the descriptor a new file is created with."""

    _fields_ = struct_fields(
        (wintypes.DWORD, "length"),
        (wintypes.LPVOID, "security_descriptor"),
        (wintypes.BOOL, "inherit_handle"),
    )


class _AclSizeInformation(ctypes.Structure):
    """``ACL_SIZE_INFORMATION``, read by ``GetAclInformation``."""

    _fields_ = struct_fields((wintypes.DWORD, "ace_count acl_bytes_in_use acl_bytes_free"))


class _AceHeader(ctypes.Structure):
    """``ACE_HEADER``, the common prefix of every ACE."""

    _fields_ = struct_fields((ctypes.c_ubyte, "ace_type ace_flags"), (wintypes.WORD, "ace_size"))


class _AccessAllowedAce(ctypes.Structure):
    """``ACCESS_ALLOWED_ACE``; the trustee SID starts at ``sid_start``."""

    _fields_ = struct_fields((_AceHeader, "header"), (wintypes.DWORD, "mask sid_start"))


def _windows_stat_file_attributes(info: os.stat_result) -> int:
    return cast(Any, info).st_file_attributes


def _windows_osfhandle(fd: int) -> int:
    import msvcrt

    return cast(Any, msvcrt).get_osfhandle(fd)


def _windows_open_osfhandle(handle: int, flags: int) -> int:
    import msvcrt

    return cast(Any, msvcrt).open_osfhandle(handle, flags)


def _posix_effective_uid() -> int:
    return cast(Any, os).geteuid()


def _is_windows() -> bool:
    from chrys.foundation.platform import get_platform

    return get_platform().is_windows


def _is_macos() -> bool:
    from chrys.foundation.platform import get_platform

    return get_platform().is_macos


def fsync_directory(path: Path) -> None:
    """Best-effort fsync of a directory after an atomic rename."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


_fsync_dir = fsync_directory
"""Backward-compatible alias for callers predating the public helper."""

_HELD_FILE_MAX_ATTEMPTS = 6
_HELD_FILE_RETRY_DELAY_SECONDS = 0.01


def _retries_held_files() -> bool:
    """Whether a file another handle holds open is worth waiting for: only Windows refuses to replace or delete it."""
    return _is_windows()


def _retry_while_held(operation: Callable[[], None]) -> None:
    """Run *operation*, trying again briefly on Windows while the file it replaces or deletes is held open.

    Antivirus scanners, the search indexer, sync clients and readers in this
    process open files for a moment without ``FILE_SHARE_DELETE`` (CPython's
    ``open()`` among them), and Windows refuses the rename or delete with
    ``PermissionError`` meanwhile. Up to six tries with about 0.3 s of sleeps
    in all; elsewhere one try.
    """
    for attempt in range(_HELD_FILE_MAX_ATTEMPTS):
        try:
            operation()
            return
        except PermissionError:
            if attempt == _HELD_FILE_MAX_ATTEMPTS - 1 or not _retries_held_files():
                raise
            time.sleep(_HELD_FILE_RETRY_DELAY_SECONDS * 2**attempt)


def replace_with_retry(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """``os.replace`` that tries again briefly on Windows while the target is held open."""
    _retry_while_held(lambda: os.replace(source, target))


def unlink_with_retry(path: str | os.PathLike[str]) -> None:
    """``os.unlink`` that tries again briefly on Windows while the file is held open."""
    _retry_while_held(lambda: os.unlink(path))


def publish_file_no_replace(source: Path, target: Path) -> None:
    """Atomically publish a completed file without replacing any target entry.

    Both paths must be on the same filesystem. The caller owns flushing the
    source and syncing the parent, and must clean up the source if it remains:
    hard-link publication retains it, while native rename consumes it.
    """
    if _is_windows():
        os.rename(source, target)
        return
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as exc:
        # Some filesystems have no hard links; EPERM is also used for that, and
        # the native rename still enforces filesystem permissions itself. Not
        # every such filesystem has an exclusive rename either: macOS exFAT
        # refuses both with ENOTSUP, which reaches the caller (installation
        # keys then fall back to their directory layout, see key_files.py).
        if exc.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM}:
            raise
        _rename_file_no_replace(source, target)


def _rename_file_no_replace(source: Path, target: Path) -> None:
    """Use an exclusive native rename; never emulate it with check + replace."""
    from chrys.foundation.platform import get_platform

    platform = get_platform()
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if platform.is_macos:
            declare_functions(libc, {"renamex_np": (ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint])})
            # Darwin's RENAME_EXCL rejects even a dangling target symlink.
            result = libc.renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004)
        elif platform.is_linux:
            declare_functions(
                libc,
                {
                    "renameat2": (
                        ctypes.c_int,
                        [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint],
                    )
                },
            )
            # AT_FDCWD, RENAME_NOREPLACE; no raw syscall numbers tied to an ABI.
            result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
        else:
            raise OSError(errno.ENOTSUP, "Atomic no-replace rename is unavailable.")
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, "Atomic no-replace rename is unavailable.") from exc
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(source), None, os.fspath(target))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically write *payload* to *path* via temp-file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp_path, path)
        fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def atomic_write_text(path: Path, payload: str, *, encoding: str = "utf-8") -> bytes:
    """Atomically write text to *path* with a durable same-directory replace.

    ``errors="backslashreplace"`` makes the encode TOTAL: a lone surrogate (an
    ``os.fsdecode`` artifact of an undecodable filesystem byte, carried through
    ``json.dumps(ensure_ascii=False)``) is written as its plain-ASCII ``\\udcXX``
    escape instead of raising ``UnicodeEncodeError``. This is the single sink that
    guarantees no serialized envelope — ACP audit, main session, or fork — can
    crash the write on a surrogate, regardless of which field carries it. A lone
    surrogate reaching a strict write is always an fsdecode artifact, never data
    a caller wants to hard-fail on. See ``surrogate_safe_text`` for the read-back
    semantics (an escaped value re-parses to the surrogate; every write re-escapes).

    Returns the bytes that landed on disk, for the caller that has to digest
    what it wrote: reading the file back to find out would re-open the race the
    digest exists to close, and re-encoding the payload beside this call would
    duplicate the encoding contract above in a second place that can drift.
    """
    encoded = payload.encode(encoding, errors="backslashreplace")
    _atomic_write_bytes(path, encoded)
    return encoded


def _atomic_create_bytes(path: Path, payload: bytes) -> None:
    """Create *path* with *payload* atomically, raising ``FileExistsError``.

    Same temp-file durability as ``_atomic_write_bytes``, but the commit is
    the platform's atomic no-clobber primitive, so a file that
    appears after a caller's early existence check is never silently replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        publish_file_no_replace(tmp_path, path)
        # Drop any remaining temp link before syncing the directory, so a
        # crash after return cannot resurrect the temp entry.
        tmp_path.unlink(missing_ok=True)
        fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def atomic_create_text(path: Path, payload: str, *, encoding: str = "utf-8") -> bytes:
    """Atomically create text at *path*, refusing to replace an existing file.

    The encoding contract matches ``atomic_write_text`` (total via
    ``backslashreplace``); only the commit differs — create-if-absent, so a
    concurrent writer surfaces as ``FileExistsError`` instead of data loss.
    """
    encoded = payload.encode(encoding, errors="backslashreplace")
    _atomic_create_bytes(path, encoded)
    return encoded


def file_fingerprint(path: Path) -> str | None:
    """Digest a file's exact bytes, or ``None`` when it cannot be read.

    For "is this still the file I read a moment ago?", which is a question
    about identity, not about content: the digest is over raw bytes rather
    than over anything parsed, so it needs no grammar to be shared by two
    readers that do not have one — one may parse the file with the locale
    encoding while the other rewrites it as UTF-8, and duplicate keys,
    quoting, references between lines and line endings are all covered
    without any of them being understood.

    Any change at all reads as a change, and that asymmetry is the point
    wherever the answer gates a destructive step: an over-sensitive digest
    costs one deferred attempt at work that is idempotent and resumable,
    while an under-sensitive one destroys what the earlier read never saw.
    ``None`` — absent *or* unreadable — is the absence of evidence and must
    never be compared as though it were evidence of sameness.
    """
    try:
        return digest_bytes(path.read_bytes())
    except OSError:
        return None


def digest_bytes(raw: bytes | None) -> str | None:
    """Digest bytes already in hand the same way :func:`file_fingerprint` does.

    For the caller that reads the file itself and must compare what it read
    rather than re-reading the path: two reads are two chances for the file to
    have changed in between, and the comparison is only worth making when it
    covers the same bytes the caller is about to act on. ``None`` in, ``None``
    out — an absent file is still the absence of evidence.
    """
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _validate_link_free_parent(path: Path) -> None:
    """Reject symlink/reparse-point components in the existing parent chain."""
    resolved = path.absolute()
    current = Path(resolved.anchor)
    for part in resolved.parts[1:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            msg = f"Secure file parent is a symbolic link: {current}"
            raise SecureFileError(msg)
        if _is_windows() and _windows_stat_file_attributes(info) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            msg = f"Secure file parent is a reparse point: {current}"
            raise SecureFileError(msg)


def _darwin_acl_api() -> _DarwinAclAPI:
    """Load the small libSystem ACL surface used by owner-only verification."""
    libc = ctypes.CDLL(None, use_errno=True)
    declare_functions(
        libc,
        {
            "acl_get_fd": (ctypes.c_void_p, [ctypes.c_int]),
            "acl_get_entry": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]),
            "acl_init": (ctypes.c_void_p, [ctypes.c_int]),
            "acl_set_fd": (ctypes.c_int, [ctypes.c_int, ctypes.c_void_p]),
            "acl_free": (ctypes.c_int, [ctypes.c_void_p]),
        },
    )
    return _DarwinAclAPI(libc=libc)


def _verify_darwin_no_acl(fd: int) -> None:
    """Reject files carrying any extended ACL entry.

    macOS NFSv4-style ACLs grant access invisibly to ``st_mode`` — a 0600
    file with ``everyone allow read`` still fstats as owner-only. (Linux
    needs no analog: a POSIX.1e ACL surfaces its mask in the group bits, so
    the no-group-or-other-bits check already rejects it.)
    """
    api = _darwin_acl_api()
    ctypes.set_errno(0)
    acl = api.libc.acl_get_fd(fd)
    if not acl:
        error = ctypes.get_errno()
        # ENOENT: no ACL on the file; ENOTSUP family: the filesystem cannot
        # carry one at all — both mean no principal beyond the owner.
        if error in {errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise SecureFileError(error, "Unable to inspect the secure file ACL.")
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        got = api.libc.acl_get_entry(acl, 0, ctypes.byref(entry))
        if got == 0:
            raise SecureFileError("Secure file carries an extended ACL.")
        error = ctypes.get_errno()
        # Exhausted enumeration is exactly -1/EINVAL; any other combination is
        # an inspection failure and must fail closed, not read as "no ACL".
        if got != -1 or error != errno.EINVAL:
            raise SecureFileError(error, "Unable to enumerate the secure file ACL.")
    finally:
        api.libc.acl_free(acl)


def _strip_darwin_acl(fd: int) -> None:
    """Remove inherited ACL entries from a freshly created secure file.

    A parent directory with ``file_inherit`` entries stamps them onto every
    new file at creation, so create paths must strip before verification —
    rejection alone would make secure files impossible to create there.
    """
    api = _darwin_acl_api()
    empty = api.libc.acl_init(1)
    if not empty:
        raise SecureFileError("Unable to allocate an empty ACL.")
    try:
        ctypes.set_errno(0)
        if api.libc.acl_set_fd(fd, empty) != 0:
            error = ctypes.get_errno()
            # Filesystems without ACL support have nothing to strip; the
            # verifier's ENOENT branch accepts them.
            if error not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise SecureFileError(error, "Unable to strip the inherited file ACL.")
    finally:
        api.libc.acl_free(empty)


def _strip_created_posix_acl(fd: int) -> None:
    if _is_macos():
        _strip_darwin_acl(fd)


def strip_inherited_acl(fd: int) -> None:
    """Drop the ACL entries a file just created inherited from its directory.

    On macOS a directory's inheritable entries (``everyone allow read,
    file_inherit``) grant access beside ``st_mode``, so a private mode alone
    does not make a new file private. Linux needs nothing (its POSIX.1e mask
    follows the group bits a private mode clears), nor does Windows here or a
    filesystem without ACLs.
    """
    _strip_created_posix_acl(fd)


def strip_inherited_directory_acl(path: Path) -> None:
    """``strip_inherited_acl`` for a directory just created at *path*.

    Strip it before creating anything inside: whatever is created in a
    directory without an ACL inherits none.
    """
    if not _is_macos():
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        _strip_darwin_acl(fd)
    finally:
        os.close(fd)


def _verify_posix_owner_only(fd: int) -> None:
    _verify_posix_owner_identity(fd)
    info = os.fstat(fd)
    # No group or other bits, rather than exactly 0600: a volume that stores no
    # permissions (exFAT on macOS) reports every file as 0700 and ignores the
    # chmod, and owner-only is all that ever had to hold.
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SecureFileError("Secure file permissions are not owner-only.")
    if _is_macos():
        _verify_darwin_no_acl(fd)


def _verify_posix_owner_identity(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SecureFileError("Secure file is not a regular file.")
    if info.st_uid != _posix_effective_uid():
        raise SecureFileError("Secure file is not owned by the current user.")


def _clear_posix_nonblock(fd: int) -> None:
    import fcntl

    posix_fcntl = cast(Any, fcntl)
    posix_os = cast(Any, os)
    status = posix_fcntl.fcntl(fd, posix_fcntl.F_GETFL)
    if status & posix_os.O_NONBLOCK:
        posix_fcntl.fcntl(fd, posix_fcntl.F_SETFL, status & ~posix_os.O_NONBLOCK)


def _open_posix_parent(path: Path) -> tuple[int, str]:
    """Open each existing parent component with no-follow directory handles."""
    absolute = path.absolute()
    if not absolute.name:
        raise SecureFileError("Secure file path has no filename.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd, absolute.name


@cache
def _windows_file_api() -> _WindowsFileAPI:
    """Load the small Win32 security API surface used by secure files."""
    windows_ctypes = cast(Any, ctypes)
    advapi32 = windows_ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    out_pointer = ctypes.POINTER(wintypes.LPVOID)
    declare_functions(
        advapi32,
        {
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
                wintypes.BOOL,
                [wintypes.LPCWSTR, wintypes.DWORD, out_pointer, ctypes.POINTER(wintypes.ULONG)],
            ),
            # The five out-pointers receive the owner, group, DACL, SACL and security descriptor.
            "GetSecurityInfo": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD] + [out_pointer] * 5),
            "GetTokenInformation": (
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
            ),
            "OpenProcessToken": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]),
            "ConvertSidToStringSidW": (wintypes.BOOL, [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]),
            "EqualSid": (wintypes.BOOL, [wintypes.LPVOID, wintypes.LPVOID]),
            "GetAclInformation": (wintypes.BOOL, [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int]),
            "GetAce": (wintypes.BOOL, [wintypes.LPVOID, wintypes.DWORD, out_pointer]),
            "GetSecurityDescriptorControl": (
                wintypes.BOOL,
                [wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)],
            ),
        },
    )
    declare_functions(
        kernel32,
        {
            "GetCurrentProcess": (wintypes.HANDLE, []),
            "LocalFree": (wintypes.HLOCAL, [wintypes.HLOCAL]),
            "CreateFileW": (
                wintypes.HANDLE,
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ],
            ),
            "GetFileInformationByHandleEx": (
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            ),
            "GetFinalPathNameByHandleW": (
                wintypes.DWORD,
                [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD],
            ),
            "SetFileInformationByHandle": (
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            ),
            "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        },
    )
    return _WindowsFileAPI(advapi32=advapi32, kernel32=kernel32)


def _windows_error(api: _WindowsFileAPI, message: str) -> SecureFileError:
    error = cast(Any, ctypes).get_last_error()
    return SecureFileError(error, message)


def _windows_current_user_sid(api: _WindowsFileAPI) -> tuple[object, object, object]:
    """Return a token handle, owning buffer, and pointer to the current user SID."""
    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1

    token = wintypes.HANDLE()
    if not api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise _windows_error(api, "Unable to inspect the current Windows user token.")
    needed = wintypes.DWORD()
    api.advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
    if not needed.value:
        api.kernel32.CloseHandle(token)
        raise _windows_error(api, "Unable to size the current Windows user token.")
    buffer = ctypes.create_string_buffer(needed.value)
    if not api.advapi32.GetTokenInformation(
        token,
        TOKEN_USER_CLASS,
        buffer,
        needed,
        ctypes.byref(needed),
    ):
        api.kernel32.CloseHandle(token)
        raise _windows_error(api, "Unable to inspect the current Windows user token.")
    sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents
    return token, buffer, sid


def _windows_owner_only_descriptor(api: _WindowsFileAPI) -> object:
    """Allocate a protected owner-only descriptor for secure file creation."""
    token, _token_buffer, sid = _windows_current_user_sid(api)
    sid_text = wintypes.LPWSTR()
    descriptor = wintypes.LPVOID()
    try:
        if not api.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise _windows_error(api, "Unable to encode the current Windows user SID.")
        # The explicit O: owner is required: tokens whose default owner is a
        # group (for example Administrators) would otherwise create files that
        # our own owner==TokenUser verification immediately rejects.
        sddl = f"O:{sid_text.value}D:P(A;;FA;;;{sid_text.value})"
        if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            None,
        ):
            raise _windows_error(api, "Unable to build an owner-only Windows DACL.")
        return descriptor
    finally:
        api.kernel32.CloseHandle(token)
        if sid_text:
            api.kernel32.LocalFree(sid_text)


def _verify_windows_owner_only(fd: int) -> None:
    """Verify owner identity and reject every allow ACE for another SID."""
    api = _windows_file_api()
    token, _token_buffer, current_sid = _windows_current_user_sid(api)
    security_descriptor = wintypes.LPVOID()
    try:
        owner_sid = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        SE_FILE_OBJECT = 1
        OWNER_SECURITY_INFORMATION = 0x00000001
        DACL_SECURITY_INFORMATION = 0x00000004
        handle = wintypes.HANDLE(_windows_osfhandle(fd))

        file_info = _FileAttributeTagInfo()
        if not api.kernel32.GetFileInformationByHandleEx(
            handle,
            9,
            ctypes.byref(file_info),
            ctypes.sizeof(file_info),
        ):
            raise _windows_error(api, "Unable to inspect the secure Windows file.")
        if file_info.file_attributes & (0x00000010 | 0x00000400):
            raise SecureFileError("Secure Windows file is a directory or reparse point.")

        result = api.advapi32.GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(owner_sid),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if result:
            raise SecureFileError(result, "Unable to verify the Windows file DACL.")
        if not owner_sid.value or not api.advapi32.EqualSid(owner_sid, current_sid):
            raise SecureFileError("Secure file is not owned by the current Windows user.")
        if not dacl.value:
            raise SecureFileError("Secure file has no Windows DACL.")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not api.advapi32.GetSecurityDescriptorControl(
            security_descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise _windows_error(api, "Unable to inspect Windows DACL inheritance.")
        if not control.value & 0x1000:
            raise SecureFileError("Secure file Windows DACL is not protected from inheritance.")

        ACL_SIZE_INFORMATION_CLASS = 2
        ACCESS_ALLOWED_ACE_TYPE = 0
        OTHER_ALLOW_ACE_TYPES = frozenset({4, 5, 9, 11})
        info = _AclSizeInformation()
        if not api.advapi32.GetAclInformation(
            dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ACL_SIZE_INFORMATION_CLASS,
        ):
            raise _windows_error(api, "Unable to inspect the Windows file DACL.")

        owner_allow = False
        for index in range(info.ace_count):
            raw_ace = wintypes.LPVOID()
            if not api.advapi32.GetAce(dacl, index, ctypes.byref(raw_ace)):
                raise _windows_error(api, "Unable to inspect a Windows file DACL entry.")
            header = ctypes.cast(raw_ace, ctypes.POINTER(_AceHeader)).contents
            if header.ace_type in OTHER_ALLOW_ACE_TYPES:
                raise SecureFileError("Secure file DACL contains an unsupported allow entry.")
            if header.ace_type != ACCESS_ALLOWED_ACE_TYPE:
                continue
            sid_address = cast(int, raw_ace.value) + _AccessAllowedAce.sid_start.offset
            ace_sid = wintypes.LPVOID(sid_address)
            if not api.advapi32.EqualSid(ace_sid, current_sid):
                raise SecureFileError("Secure file DACL grants access to another Windows principal.")
            owner_allow = True
        if not owner_allow:
            raise SecureFileError("Secure file DACL does not grant access to its owner.")
    finally:
        api.kernel32.CloseHandle(token)
        if security_descriptor:
            api.kernel32.LocalFree(security_descriptor)


def verify_owner_only_fd(fd: int) -> None:
    """Verify that *fd* identifies a regular file accessible only by its owner."""
    if _is_windows():
        _verify_windows_owner_only(fd)
    else:
        _verify_posix_owner_only(fd)


def _verify_windows_owner_identity(fd: int) -> None:
    """Verify only file kind and owner identity, tolerating any DACL."""
    api = _windows_file_api()
    token, _token_buffer, current_sid = _windows_current_user_sid(api)
    security_descriptor = wintypes.LPVOID()
    try:
        owner_sid = wintypes.LPVOID()
        SE_FILE_OBJECT = 1
        OWNER_SECURITY_INFORMATION = 0x00000001
        handle = wintypes.HANDLE(_windows_osfhandle(fd))

        file_info = _FileAttributeTagInfo()
        if not api.kernel32.GetFileInformationByHandleEx(
            handle,
            9,
            ctypes.byref(file_info),
            ctypes.sizeof(file_info),
        ):
            raise _windows_error(api, "Unable to inspect the secure Windows file.")
        if file_info.file_attributes & (0x00000010 | 0x00000400):
            raise SecureFileError("Secure Windows file is a directory or reparse point.")

        result = api.advapi32.GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner_sid),
            None,
            None,
            None,
            ctypes.byref(security_descriptor),
        )
        if result:
            raise SecureFileError(result, "Unable to verify the Windows file owner.")
        if not owner_sid.value or not api.advapi32.EqualSid(owner_sid, current_sid):
            raise SecureFileError("Secure file is not owned by the current Windows user.")
    finally:
        api.kernel32.CloseHandle(token)
        if security_descriptor:
            api.kernel32.LocalFree(security_descriptor)


def verify_owner_identity_fd(fd: int) -> None:
    """Verify regular-file kind and owner identity without permission checks.

    Legacy artifacts written before the owner-only writers existed carry
    default permissions/DACLs; source reads stay no-follow and owner-checked
    but must not reject them on mode alone.
    """
    if _is_windows():
        _verify_windows_owner_identity(fd)
    else:
        _verify_posix_owner_identity(fd)


def _windows_final_path(api: _WindowsFileAPI, handle: int) -> str:
    """Resolve the opened handle's final DOS path for identity verification."""
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = api.kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
    if not length or length >= size:
        raise _windows_error(api, "Unable to resolve the secure Windows file path.")
    text = buffer.value
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def _windows_os_error(code: int, path: Path) -> OSError:
    """Build the ``OSError`` Python raises for a failed Win32 file call on *path*.

    The Win32 code sets ``winerror`` and maps to the POSIX ``errno`` (so an
    access denial is a ``PermissionError`` and a missing parent is ENOENT),
    and the message is the system's own text.
    """
    return OSError(None, cast(Any, ctypes).FormatError(code).strip(), str(path), code)


_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_REPARSE_TAG_NAME_SURROGATE = 0x20000000
_WINDOWS_SHARE_READ = 0x00000001
_WINDOWS_SHARE_WRITE = 0x00000002
_WINDOWS_SHARE_DELETE = 0x00000004
WINDOWS_SECURE_OPEN_SHARE_MODE = _WINDOWS_SHARE_READ | _WINDOWS_SHARE_WRITE | _WINDOWS_SHARE_DELETE
"""Share mode for every secure Windows open.

``FILE_SHARE_DELETE`` is load-bearing: it is what lets an append-only log be
*deleted* while a stuck writer still holds it, which is how the tombstone
sweep removes a session directory the writer has not let go of. CPython's
default ``open()`` omits it, and without it the sweep could only wait.

It does not make the ancestors renamable: Windows refuses to rename a
directory that has an open child whatever the child's share mode, which is
why logical deletion falls back to a recorded intent (see
``service/trajectory/tombstone.py``) instead of the rename POSIX allows.
"""


def _windows_secure_open(
    path: Path,
    *,
    read: bool,
    write: bool,
    create: bool,
    truncate: bool,
    delete: bool = False,
    append: bool = False,
    verify_identity_only: bool = False,
    legacy_import: bool = False,
) -> int:
    """Open a Windows file handle without traversing a final reparse point.

    ``legacy_import`` opens a regular file of any owner for
    ``secure_open_regular_binary``: open failures are raised as Windows
    reports them, and a reparse point is refused only when it names another
    file (a symbolic link, a junction), since cloud placeholders, deduplicated
    and WOF-compressed files keep their data behind one.
    """
    api = _windows_file_api()
    access = (0x80000000 if read else 0) | (0x40000000 if write else 0) | (0x00010000 if delete else 0)
    share = WINDOWS_SECURE_OPEN_SHARE_MODE
    disposition = 1 if create else 3
    flags = 0x00000080 | 0x00200000
    security_descriptor = _windows_owner_only_descriptor(api) if create else None

    security_attributes = None
    if security_descriptor is not None:
        security_attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            security_descriptor,
            False,
        )
    create_error = 0
    try:
        handle = api.kernel32.CreateFileW(
            str(path),
            access,
            share,
            ctypes.byref(security_attributes) if security_attributes is not None else None,
            disposition,
            flags,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            create_error = cast(Any, ctypes).get_last_error()
    finally:
        if security_descriptor is not None:
            api.kernel32.LocalFree(security_descriptor)
    if handle == wintypes.HANDLE(-1).value:
        if legacy_import:
            raise _windows_os_error(create_error, path)
        raise SecureFileError(create_error, "Unable to open owner-only Windows file.")

    try:
        info = _FileAttributeTagInfo()
        if not api.kernel32.GetFileInformationByHandleEx(
            handle,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise _windows_error(api, "Unable to inspect owner-only Windows file.")
        refused_reparse = info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT and (
            not legacy_import or info.reparse_tag & _WINDOWS_REPARSE_TAG_NAME_SURROGATE
        )
        if info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY or refused_reparse:
            raise SecureFileError("Secure Windows file is a directory or reparse point.")
        # The pathname-based parent walk is racy against junction swaps;
        # re-verify by handle that traversal landed on the validated path.
        final_path = _windows_final_path(api, handle)
        if ntpath.normcase(final_path) != ntpath.normcase(ntpath.abspath(str(path))):
            raise SecureFileError("Secure Windows file resolved outside its validated parents.")
        fd_flags = os.O_RDONLY if read and not write else os.O_WRONLY
        if read and write:
            fd_flags = os.O_RDWR
        fd_flags |= getattr(os, "O_NOINHERIT", 0)
        # Binary, always: the CRT would otherwise adopt the handle in text
        # mode and expand every LF to CRLF behind the caller's back, so a
        # writer that counts the bytes it handed over counts short.
        fd_flags |= getattr(os, "O_BINARY", 0)
        if append:
            # The CRT honours ``_O_APPEND`` on an adopted handle: every write
            # seeks to end first, matching POSIX ``O_APPEND`` for one writer.
            fd_flags |= os.O_APPEND
        fd = _windows_open_osfhandle(int(handle), fd_flags)
    except BaseException:
        api.kernel32.CloseHandle(handle)
        raise
    try:
        if legacy_import:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SecureFileError("Secure file is not a regular file.")
        elif verify_identity_only:
            verify_owner_identity_fd(fd)
        else:
            verify_owner_only_fd(fd)
        if truncate:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _windows_secure_unlink(path: Path, identity: os.stat_result | None) -> None:
    """Best-effort deletion through a fully verified handle, never by name.

    Cleanup after a failed atomic publish must not re-traverse the parent by
    name: a junction swapped in behind ``os.replace`` would redirect a
    name-based unlink onto an unrelated same-named file. Open with DELETE
    access under the full secure-open validation (link-free parent walk,
    reparse rejection, final-path identity, owner-only DACL), require the
    opened object to be the exact file this writer created (rename preserves
    the volume/file id *identity* captured from the temp fd — a same-user
    replacement would pass every DACL check), and only then mark the verified
    handle for disposal. If any gate fails the artifact is left in place —
    every consumer re-verifies through secure_open on each read, so
    unreachable debris is inert while a wrong deletion is unrecoverable.
    """
    if identity is None:
        return
    try:
        _validate_link_free_parent(path)
        fd = _windows_secure_open(path, read=True, write=False, create=False, truncate=False, delete=True)
    except SecureFileError, OSError:
        return
    api = _windows_file_api()

    info = _FileDispositionInfo(True)
    FILE_DISPOSITION_INFO_CLASS = 4
    handle = wintypes.HANDLE(_windows_osfhandle(fd))
    try:
        with contextlib.suppress(OSError):
            entry = os.fstat(fd)
            if entry.st_dev != identity.st_dev or entry.st_ino != identity.st_ino:
                return
            api.kernel32.SetFileInformationByHandle(
                handle,
                FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
    finally:
        os.close(fd)


def secure_unlink_owner_verified(path: Path) -> bool:
    """Best-effort deletion of an owner-verified artifact, never by bare name.

    A bare ``path.unlink()`` re-traverses every parent component by name; a
    directory swapped for a symlink/junction between an artifact's verified
    read and its deletion would redirect the unlink onto an unrelated
    same-named file. Reach the parent through the no-follow component walk
    and delete via ``dir_fd`` (POSIX) or a verified handle (Windows), after
    re-checking the target is a regular file owned by the current user.
    Owner-*verified* (not owner-only) on purpose: this deletes the same
    legacy artifacts ``secure_open_owner_verified_binary`` reads. If any
    gate fails the artifact is left in place and ``False`` is returned —
    leftover debris is re-verified by every consumer while a wrong deletion
    is unrecoverable.
    """
    return _owner_verified_unlink(path, unlink=True)


def can_unlink_owner_verified(path: Path) -> bool:
    """Whether :func:`secure_unlink_owner_verified` would delete *path*: the same gates, deleting nothing.

    Also what the deletion needs that can be told beforehand: a writable,
    searchable folder (POSIX) and no read-only attribute (Windows). For
    refusing before asking a user to confirm; the deletion still reports
    what this can't foresee (immutable flags, ACLs, a change in between).
    """
    return _owner_verified_unlink(path, unlink=False)


def _owner_verified_unlink(path: Path, *, unlink: bool) -> bool:
    if _is_windows():
        return _windows_secure_unlink_owner_verified(path, unlink=unlink)
    try:
        directory_fd, filename = _open_posix_parent(path)
    except SecureFileError, OSError:
        return False
    try:
        try:
            entry = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            return False
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != _posix_effective_uid():
            return False
        if not unlink:
            return os.access(".", os.W_OK | os.X_OK, dir_fd=directory_fd, effective_ids=True)
        try:
            os.unlink(filename, dir_fd=directory_fd)
        except OSError:
            return False
        return True
    finally:
        os.close(directory_fd)


def _windows_secure_unlink_owner_verified(path: Path, *, unlink: bool) -> bool:
    try:
        _validate_link_free_parent(path)
        fd = _windows_secure_open(
            path,
            read=True,
            write=False,
            create=False,
            truncate=False,
            delete=True,
            verify_identity_only=True,
        )
    except SecureFileError, OSError:
        return False
    if not unlink:
        try:
            # Marking a read-only file for deletion is refused.
            return not _windows_stat_file_attributes(os.fstat(fd)) & stat.FILE_ATTRIBUTE_READONLY
        finally:
            os.close(fd)
    api = _windows_file_api()

    info = _FileDispositionInfo(True)
    FILE_DISPOSITION_INFO_CLASS = 4
    handle = wintypes.HANDLE(_windows_osfhandle(fd))
    try:
        return bool(
            api.kernel32.SetFileInformationByHandle(
                handle,
                FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
    finally:
        os.close(fd)


def secure_open_owner_only(
    path: Path,
    *,
    read: bool = False,
    write: bool = False,
    create: bool = False,
    truncate: bool = False,
) -> int:
    """Open and verify an owner-only regular file without following the final link."""
    if not read and not write:
        raise ValueError("secure_open_owner_only requires read=True or write=True")
    if truncate and not write:
        raise ValueError("secure_open_owner_only requires write=True when truncate=True")
    _validate_link_free_parent(path)
    if _is_windows():
        return _windows_secure_open(path, read=read, write=write, create=create, truncate=truncate)

    flags = os.O_RDONLY if read and not write else os.O_WRONLY
    if read and write:
        flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # Never block inside open(2): a planted FIFO would otherwise wedge the
    # caller before the regular-file verification can reject it.
    flags |= getattr(os, "O_NONBLOCK", 0)
    directory_fd, filename = _open_posix_parent(path)
    try:
        fd = os.open(filename, flags, _OWNER_ONLY_MODE, dir_fd=directory_fd)
    except OSError as exc:
        raise SecureFileError(exc.errno, "Unable to open owner-only file.") from exc
    finally:
        os.close(directory_fd)
    try:
        if create:
            os.fchmod(fd, _OWNER_ONLY_MODE)
            _strip_created_posix_acl(fd)
        verify_owner_only_fd(fd)
        _clear_posix_nonblock(fd)
        if truncate:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass(frozen=True)
class OwnerOnlyAppendHandle:
    """Result of :func:`secure_open_owner_only_append`."""

    fd: int
    created: bool
    """True when this call created the file (it was absent before)."""


def secure_open_owner_only_append(path: Path) -> OwnerOnlyAppendHandle:
    """Create-or-open *path* as an owner-only regular file in append mode.

    The single sanctioned way to hold an owner-only append-only log: callers
    must not compose create/seek/reopen themselves or fall back to a bare
    ``Path.open("a")``. Contract:

    * **create-or-open** — an absent file is created exclusively with
      owner-only mode (and inherited ACLs stripped); an existing file is
      opened and verified owner-only (mode, ownership, regular file, no
      link in the parent chain, no final-symlink follow).
    * **append** — ``O_APPEND`` on POSIX; ``_O_APPEND`` on the adopted CRT fd
      on Windows, where the handle is also opened with ``FILE_SHARE_DELETE``
      so the log can still be deleted while a writer holds it.
    * **binary** — the fd is raw; callers write bytes, so ``\n`` is never
      translated on Windows.
    * **readable** — the descriptor also reads, so a writer that must inspect
      what it is about to append to (recovering a tail, say) reads the file it
      actually holds instead of reopening a pathname that may by then name a
      different file. Writes still go to the end regardless of any read.

    The create-vs-open race (file vanishing between an ``EEXIST`` and the
    follow-up open, or appearing between a failed open and the create) is
    retried a bounded number of times.
    """
    _validate_link_free_parent(path)
    for _attempt in range(16):
        try:
            fd = _open_owner_only_append_create(path)
        except FileExistsError:
            try:
                fd = _open_owner_only_append_existing(path)
            except FileNotFoundError:
                continue
            return OwnerOnlyAppendHandle(fd=fd, created=False)
        return OwnerOnlyAppendHandle(fd=fd, created=True)
    raise SecureFileError("Unable to create-or-open the owner-only append file.")


def _open_owner_only_append_create(path: Path) -> int:
    """Exclusively create *path* owner-only in append mode; ``FileExistsError`` if present."""
    if _is_windows():
        try:
            return _windows_secure_open(path, read=True, write=True, create=True, truncate=False, append=True)
        except SecureFileError as exc:
            if exc.errno in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(str(path)) from exc
            raise
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_fd, filename = _open_posix_parent(path)
    try:
        try:
            fd = os.open(filename, flags, _OWNER_ONLY_MODE, dir_fd=directory_fd)
        except FileExistsError:
            raise
        except OSError as exc:
            raise SecureFileError(exc.errno, "Unable to create owner-only append file.") from exc
    finally:
        os.close(directory_fd)
    try:
        os.fchmod(fd, _OWNER_ONLY_MODE)
        _strip_created_posix_acl(fd)
        verify_owner_only_fd(fd)
        _clear_posix_nonblock(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_owner_only_append_existing(path: Path) -> int:
    """Open an existing owner-only *path* in append mode; ``FileNotFoundError`` if absent."""
    if _is_windows():
        try:
            return _windows_secure_open(path, read=True, write=True, create=False, truncate=False, append=True)
        except SecureFileError as exc:
            if exc.errno in {2, 3}:  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
                raise FileNotFoundError(str(path)) from exc
            raise
    flags = os.O_RDWR | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_fd, filename = _open_posix_parent(path)
    try:
        try:
            fd = os.open(filename, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SecureFileError(exc.errno, "Unable to open owner-only append file.") from exc
    finally:
        os.close(directory_fd)
    try:
        verify_owner_only_fd(fd)
        _clear_posix_nonblock(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def secure_open_owner_only_binary(path: Path) -> BinaryIO:
    """Open an existing owner-only file for binary reading."""
    try:
        fd = secure_open_owner_only(path, read=True)
    except SecureFileError as exc:
        if _is_windows() and exc.errno == 3:  # ERROR_PATH_NOT_FOUND: a missing parent reads as ENOENT, as on POSIX
            raise SecureFileError(errno.ENOENT, "Unable to open owner-only file.") from exc
        raise
    return os.fdopen(fd, "rb")


def secure_open_owner_verified_binary(path: Path) -> BinaryIO:
    """Open an existing owner-verified file for binary reading.

    Same no-follow/link-free-parent/regular-file/owner gates as the owner-only
    open, but without the owner-only mode/DACL requirement: session artifacts
    written by releases that predate the owner-only writers carry default
    permissions and must remain readable by restore, reconciliation, and fork.
    """
    if _is_windows():
        try:
            fd = _windows_secure_open(
                path,
                read=True,
                write=False,
                create=False,
                truncate=False,
                verify_identity_only=True,
            )
        except SecureFileError as exc:
            if exc.errno == 3:  # ERROR_PATH_NOT_FOUND: a missing parent reads as ENOENT here, as POSIX reports it
                raise SecureFileError(errno.ENOENT, "Unable to open owner-verified file.") from exc
            raise
        return os.fdopen(fd, "rb")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # Never block inside open(2): a planted FIFO would otherwise wedge the
    # caller before the regular-file verification can reject it.
    flags |= getattr(os, "O_NONBLOCK", 0)
    _validate_link_free_parent(path)
    directory_fd, filename = _open_posix_parent(path)
    try:
        fd = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise SecureFileError(exc.errno, "Unable to open owner-verified file.") from exc
    finally:
        os.close(directory_fd)
    try:
        _verify_posix_owner_identity(fd)
        _clear_posix_nonblock(fd)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def secure_open_regular_binary(path: Path) -> BinaryIO:
    """Read a regular file without following its final link, accepting any readable owner/mode.

    For explicit imports of legacy data only: migration may import files an
    elevated older release created. Only the leaf is opened no-follow; the
    parents are traversed as by any open, so a parent alias or an ancestor the
    user may enter but not list still works (the owner-verified reader opens
    every parent and needs read access to each). Open failures are raised as
    the system reports them: migration shows them as the reason a file stayed
    behind.
    """
    if _is_windows():
        # The handle's final path must match the requested one, so compare
        # against the parents' real spelling (a mapped drive, a junction).
        target = path.parent.resolve() / path.name
        pinned = _windows_secure_open(target, read=True, write=False, create=False, truncate=False, legacy_import=True)
        try:
            fd = _windows_reopen_same_file(target, pinned)
        finally:
            os.close(pinned)
        return os.fdopen(fd, "rb")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # Never block inside open(2): a planted FIFO would otherwise wedge the
    # caller before the regular-file verification can reject it.
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SecureFileError("Secure file is not a regular file.")
        _clear_posix_nonblock(fd)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _windows_reopen_same_file(path: Path, pinned_fd: int) -> int:
    """Reopen *path* for reading as any program would, pinned to *pinned_fd*'s file.

    The no-follow handle proves the leaf is no link, but it opens a reparse
    point itself, and for a cloud placeholder (OneDrive Files On-Demand), a
    deduplicated or a WOF-compressed file that is the raw stream behind the
    filter, not the file's content. The ordinary reopen reads through the
    filter; since it resolves the name again, it must land on the same file.
    """
    api = _windows_file_api()
    handle = api.kernel32.CreateFileW(str(path), 0x80000000, WINDOWS_SECURE_OPEN_SHARE_MODE, None, 3, 0x00000080, None)
    if handle == wintypes.HANDLE(-1).value:
        raise _windows_os_error(cast(Any, ctypes).get_last_error(), path)
    try:
        fd = _windows_open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        api.kernel32.CloseHandle(handle)
        raise
    try:
        if not os.path.samestat(os.fstat(pinned_fd), os.fstat(fd)):
            raise SecureFileError("Windows file was replaced while it was being opened.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def atomic_write_owner_only_bytes(path: Path, payload: bytes, *, create_parents: bool = True) -> None:
    """Atomically replace *path* with an owner-only byte payload.

    With *create_parents* false a missing parent directory is an error rather than made: a record
    rewritten in place must not resurrect a directory deleted since the record was read.
    """
    _validate_link_free_parent(path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    if not _is_windows():
        _atomic_write_owner_only_posix(path, payload)
        return
    fd, tmp_path = _create_windows_owner_only_temp(path)
    published = False
    # os.replace preserves the file object, so the temp identity is also the
    # published target's identity for every cleanup below.
    written_identity: os.stat_result | None = None
    with contextlib.suppress(OSError):
        written_identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        replace_with_retry(tmp_path, path)
        published = True
        verify_fd = secure_open_owner_only(path, read=True)
        os.close(verify_fd)
        fsync_directory(path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        # Never unlink by name here: the parent may have been junction-swapped
        # after the replace, and a name-based unlink would follow it onto an
        # unrelated file. Deletion goes through a verified handle or not at all.
        if published:
            _windows_secure_unlink(path, written_identity)
        _windows_secure_unlink(tmp_path, written_identity)
        raise


def _create_windows_owner_only_temp(path: Path) -> tuple[int, Path]:
    for _attempt in range(128):
        temp_path = path.parent / f".{path.name}.{secrets.token_hex(_ATOMIC_NAME_TOKEN_BYTES)}.tmp"
        try:
            return secure_open_owner_only(temp_path, write=True, create=True), temp_path
        except SecureFileError as exc:
            if exc.errno not in {80, 183}:
                raise
    raise SecureFileError("Unable to allocate an owner-only Windows temporary file.")


def _unlink_verified_posix(name: str, directory_fd: int, identity: os.stat_result | None) -> None:
    """Unlink *name* only while it is still the exact file this writer created.

    POSIX has no unlink-by-handle, so the stat-to-unlink window is an accepted
    residual — deleting a concurrently substituted entry through an unverified
    name would be worse than leaving debris behind.
    """
    if not name or identity is None:
        return
    with contextlib.suppress(OSError):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if entry.st_dev == identity.st_dev and entry.st_ino == identity.st_ino:
            os.unlink(name, dir_fd=directory_fd)


def _atomic_write_owner_only_posix(path: Path, payload: bytes) -> None:
    directory_fd, filename = _open_posix_parent(path)
    temp_name = ""
    fd = -1
    published = False
    written_identity: os.stat_result | None = None
    try:
        for _attempt in range(128):
            candidate = f".{filename}.{secrets.token_hex(_ATOMIC_NAME_TOKEN_BYTES)}.tmp"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    _OWNER_ONLY_MODE,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if fd < 0:
            raise SecureFileError("Unable to allocate an owner-only temporary file.")
        # os.replace preserves the inode, so the O_EXCL-created temp identity
        # is also the published target's identity for every cleanup below.
        written_identity = os.fstat(fd)
        os.fchmod(fd, _OWNER_ONLY_MODE)
        _strip_created_posix_acl(fd)
        verify_owner_only_fd(fd)
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temp_name = ""
        published = True
        verify_fd = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        try:
            verify_owner_only_fd(verify_fd)
        finally:
            os.close(verify_fd)
        with contextlib.suppress(OSError):
            os.fsync(directory_fd)
    except BaseException:
        if published:
            _unlink_verified_posix(filename, directory_fd, written_identity)
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        _unlink_verified_posix(temp_name, directory_fd, written_identity)
        os.close(directory_fd)


def is_utf8_encodable(text: str) -> bool:
    """Return whether *text* can be encoded by strict UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def surrogate_safe_text(text: str) -> str:
    """Neutralize unpaired UTF-16 surrogates so *text* survives a strict write.

    ``os.fsdecode`` of a filesystem path that contains an undecodable byte yields
    a lone surrogate (``\\udcXX``); carried through ``json.dumps(ensure_ascii=False)``
    it lands in the serialized string as a legal ``str`` that is NOT strict-UTF-8
    encodable, so ``atomic_write_owner_only_text`` (and any strict ``.encode()``)
    would raise ``UnicodeEncodeError``. ``backslashreplace`` rewrites each lone
    surrogate as its plain-ASCII ``\\udcXX`` escape — a no-op on all valid text —
    keeping the result surrogate-free and round-trippable through a strict read.

    The returned text is NOT an equivalent filesystem path: replacing a surrogate
    changes the bytes produced by ``os.fsencode``.  Keep the raw surrogateescaped
    value for filesystem access and subprocess ``cwd``; call this helper only on a
    derived display or non-identity-bearing serialization copy.  Identity-bearing
    serialization and hash keys must encode the raw value injectively instead (for
    example, JSON with ``ensure_ascii=True`` or an encoding of ``os.fsencode``).

    The general crash-safety guarantee lives at the write SINK
    (``atomic_write_text`` / ``atomic_write_owner_only_text`` both encode with
    ``errors="backslashreplace"``), so callers do NOT need to wrap ``json.dumps``
    with this. Use it only when a SPECIFIC field must STAY surrogate-free through
    a read — apply it BEFORE ``json.dumps`` so the escape's backslash is itself
    JSON-escaped and does not re-parse to the surrogate (e.g. the HMAC-adjacent
    display command/args, whose stored form must read back surrogate-free). A
    sink-escaped value, by contrast, re-parses to the original lone surrogate on
    read-back (the ``\\udcXX`` lands as a bare JSON unicode escape) — harmless
    because every persistence path re-encodes through the same total sink.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def atomic_write_owner_only_text(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with owner-only encoded text.

    ``errors="backslashreplace"`` makes the encode TOTAL against lone surrogates,
    exactly like ``atomic_write_text`` — this is the owner-only sink for ACP audit
    and pending artifacts, which carry surrogateescaped filesystem paths.
    """
    atomic_write_owner_only_bytes(path, payload.encode(encoding, errors="backslashreplace"))


def read_owner_verified_bounded(path: Path, *, max_bytes: int) -> bytes:
    """Read at most the byte ceiling through an owner-verified descriptor."""
    with secure_open_owner_verified_binary(path) as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Artifact exceeds the {max_bytes}-byte read ceiling: {path.name}")
    return payload


def atomic_write_basename_budget(max_filename_bytes: int = 255) -> int:
    """Bytes available to a destination basename, reserving its atomic temporary name."""
    return max_filename_bytes - (2 + 2 * _ATOMIC_NAME_TOKEN_BYTES + len(".tmp"))
