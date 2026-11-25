# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Memory loader — reads ``MemoryConfig`` files/folders into a renderable system-prompt block.

Resolves relative user-configured paths against the workspace cwd, accepts
absolute paths for files and folders, filters to ``.md`` and ``.txt`` files,
scans folders breadth-first with hard bounds (``FOLDER_MAX_DEPTH``
subdirectory levels, ``FOLDER_MAX_FILES`` files per folder, symlink cycle
detection), decodes via
:func:`chrys.foundation.text.encoding.decode_bytes`, and renders the combined content
into a markdown block ready for injection by
:class:`chrys.service.context.providers.memory.MemoryProvider`.  Each
physical file loads at most once — explicit file entries win over
folder-discovered duplicates.

Loading is capped at a token budget (default 35,000).  If the cap is hit,
the remaining files are reported as ``skipped_files`` and ``truncated`` is
set so callers (``agent_builder``) can surface a ``Warning`` event.
"""

from __future__ import annotations

import logging
import os
import posixpath
from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import msg
from chrys.foundation.models.workspace import path_identity
from chrys.foundation.platform.paths import is_absolute_path as _is_absolute_path
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.service.profiles.agents.schema import MemoryConfig

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_CAP = 35_000

_MEMORY_TRUNCATED = msg(
    "builder.memory_truncated",
    fallback="Auto-loaded memory truncated at the token cap. Loaded {loaded_count} file(s); skipped {skipped_count}.",
)

_ALLOWED_SUFFIXES = (".md", ".txt")


@dataclass
class MemoryContent:
    """Result of :func:`load_memory_content`.

    ``text`` is the rendered markdown block (empty when nothing was loaded).
    ``loaded_files`` and ``skipped_files`` are workspace-cwd-relative paths
    for workspace-contained files, or absolute paths for files loaded via
    absolute file/folder entries outside the workspace.
    ``loaded_files_by_source`` maps each configured file/folder location
    to the display paths loaded from it.
    ``missing_paths`` lists configured entries (files or folders) that did
    not exist on disk.
    """

    text: str = ""
    loaded_files: list[str] = field(default_factory=list)
    loaded_files_by_source: dict[str, list[str]] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    token_count: int = 0
    truncated: bool = False


def memory_truncated_warning(content: MemoryContent, *, session_id: str | None) -> Warning:
    """The warning every agent build publishes when *content* stopped at the token cap.

    Truncation is surfaced because the agent's reference context is incomplete;
    missing paths are silently skipped, since built-in profiles configure optional
    files such as ``AGENTS.md`` that need not exist in every workspace.
    """
    loaded = len(content.loaded_files)
    skipped = len(content.skipped_files)
    return Warning(
        code="memory_truncated",
        message=f"Auto-loaded memory truncated at the token cap. Loaded {loaded} file(s); skipped {skipped}.",
        display_message=_MEMORY_TRUNCATED.bind(loaded_count=loaded, skipped_count=skipped),
        session_id=session_id,
    )


@dataclass
class _Candidate:
    """A single file selected for loading, with display metadata.

    ``rel_path`` is the display path used in the rendered ``## File:`` header.
    It is workspace-relative for workspace-contained files and absolute for
    absolute files outside the workspace.  ``folder_root`` is non-empty only
    for files discovered via the bounded folder scan — used to group them
    under a ``## Folder:`` header.
    """

    abs_path: Path
    rel_path: str
    source: str = ""
    folder_root: str = ""


def load_memory_content(
    memory: MemoryConfig,
    workspace_cwd: str | None,
    *,
    token_cap: int = DEFAULT_TOKEN_CAP,
) -> MemoryContent:
    """Load and render auto-load memory files for the given workspace.

    Returns a zero-state :class:`MemoryContent` when there is nothing to
    load (no workspace, no configured paths).  Otherwise builds the
    markdown block in files-first / folder-grouped order, capping at
    ``token_cap`` total tokens.
    """
    if not workspace_cwd or (not memory.files and not memory.folders):
        return MemoryContent()

    try:
        base = Path(workspace_cwd).resolve()
    except OSError:
        return MemoryContent()
    missing: list[str] = []
    file_candidates: list[_Candidate] = []
    folder_candidates: dict[str, list[_Candidate]] = {}
    # Each physical file loads once: explicit files win over
    # folder-discovered duplicates, earlier folders over later ones.
    # Identity is filesystem-aware (see path_identity) so case aliases on
    # case-insensitive filesystems and symlinked spellings collapse.
    seen_files: set[object] = set()

    # Files: each configured path resolves to a single file.  Relative files
    # stay workspace-contained; absolute files are intentionally allowed.
    for raw in memory.files:
        path = memory_file_path(raw, base)
        if path is None:
            missing.append(raw)
            continue
        identity = path_identity(path)
        if identity in seen_files:
            continue
        seen_files.add(identity)
        rel_path = _rel(path, base)
        file_candidates.append(_Candidate(abs_path=path, rel_path=rel_path, source=rel_path))

    # Folders: bounded breadth-first scan (see _walk_folder) with symlink
    # cycle detection + containment.  Relative folders must stay inside the
    # workspace; absolute folders are intentionally allowed anywhere
    # (matching absolute files) but their scan is contained to the folder
    # itself so a symlink inside cannot surface files from elsewhere.
    seen_folders: set[object] = set()
    for raw in memory.folders:
        folder = _safe_resolve_folder(raw, base)
        if folder is None or not folder.is_dir():
            missing.append(raw)
            continue
        folder_identity = path_identity(folder)
        if folder_identity in seen_folders:
            # Alias (symlink / case spelling) of an already-scanned folder —
            # its files are all loaded there; re-registering would overwrite
            # that group with an empty deduped list.
            continue
        seen_folders.add(folder_identity)
        # Workspace-contained folders keep the workspace as the containment
        # root (a symlink may legitimately target elsewhere in the
        # workspace); folders outside it are contained to themselves.
        containment_root = base if folder.is_relative_to(base) else folder
        # The walk itself filters through seen_files so duplicates of
        # already-loaded files never consume the per-folder cap.
        discovered = _walk_folder(folder, containment_root, seen_files)
        folder_root = _rel(folder, base)
        # Normalize folder header to always end with `/` so the header is
        # visually distinct from a file path.
        if not folder_root.endswith("/"):
            folder_root += "/"
        folder_candidates[folder_root] = [
            _Candidate(abs_path=p, rel_path=_rel(p, base), source=folder_root, folder_root=folder_root)
            for p in discovered
        ]

    # Render header + sections, counting tokens incrementally.  We hold the
    # rendered pieces in a list and join at the end to avoid quadratic
    # string concatenation, but token-count each piece as it's added.
    tokenizer = MixedLanguageTokenizer()
    pieces: list[str] = [_HEADER]
    token_count = tokenizer.count_tokens(_HEADER)
    loaded: list[str] = []
    loaded_by_source: dict[str, list[str]] = {}
    skipped: list[str] = []
    truncated = False

    def _try_add(section: str, rel_path: str, source: str) -> bool:
        """Add a section if it fits the cap.  Return False on overflow."""
        nonlocal token_count, truncated
        section_tokens = tokenizer.count_tokens(section)
        if token_count + section_tokens > token_cap:
            truncated = True
            skipped.append(rel_path)
            return False
        pieces.append(section)
        token_count += section_tokens
        loaded.append(rel_path)
        loaded_by_source.setdefault(source or rel_path, []).append(rel_path)
        return True

    # Files first.
    for cand in file_candidates:
        if truncated:
            skipped.append(cand.rel_path)
            continue
        section = _render_file_section(cand, level=2)
        if section is None:
            missing.append(cand.rel_path)
            continue
        _try_add(section, cand.rel_path, cand.source)

    # Then folders, grouped under a ``## Folder:`` header.
    for folder_root, cands in folder_candidates.items():
        if not cands:
            continue
        if truncated:
            skipped.extend(c.rel_path for c in cands)
            continue
        # The folder header itself is cheap; include it eagerly even if
        # subsequent files overflow — the agent still sees a clear
        # "we tried to load this folder" anchor.
        folder_header = f"\n## Folder: {folder_root}\n"
        header_tokens = tokenizer.count_tokens(folder_header)
        if token_count + header_tokens > token_cap:
            truncated = True
            skipped.extend(c.rel_path for c in cands)
            continue
        pieces.append(folder_header)
        token_count += header_tokens
        for cand in cands:
            if truncated:
                skipped.append(cand.rel_path)
                continue
            section = _render_file_section(cand, level=3)
            if section is None:
                missing.append(cand.rel_path)
                continue
            _try_add(section, cand.rel_path, cand.source)

    if not loaded:
        # Nothing actually got rendered; collapse to empty content so the
        # provider stays a no-op.  We still surface ``missing_paths`` /
        # ``skipped_files`` so the caller can publish warnings.
        return MemoryContent(
            text="",
            loaded_files=[],
            loaded_files_by_source={},
            skipped_files=skipped,
            missing_paths=missing,
            token_count=0,
            truncated=truncated,
        )

    # Recompute the final count over the joined text — tokenizing piece
    # by piece during accumulation can drift by a token or two vs. the
    # whole-text count (heuristic tokenizer boundaries shift across
    # joins, esp. with mixed line endings on Windows).  The in-loop sum
    # is fine as a conservative budget gate; what we *report* should
    # equal what consumers see.
    text = "".join(pieces)
    return MemoryContent(
        text=text,
        loaded_files=loaded,
        loaded_files_by_source=loaded_by_source,
        skipped_files=skipped,
        missing_paths=missing,
        token_count=tokenizer.count_tokens(text),
        truncated=truncated,
    )


_HEADER = (
    "# Auto-loaded memory\n\n"
    "The following files were auto-loaded as reference context for the current task.\n"
    "Relative paths are resolved from the workspace working directory.\n"
)


def validate_memory_config(memory: MemoryConfig, *, workspace_cwd: str | None = None) -> list[str]:
    """Validate that a :class:`MemoryConfig` is internally consistent.

    Errors checked:

    * Files must end in ``.md`` or ``.txt``.
    * No duplicate files (compared case-insensitively after path
      normalization, so ``AGENTS.md`` and ``agents.md`` collide and
      ``docs/spec.md`` collides with ``./docs/spec.md``).
    * No duplicate folders, same comparison rules.
    * No filesystem root as a folder ("/", "C:\\") — even the bounded
      folder scan has no business starting at one; the loader refuses
      them too.

    A file inside a configured folder is deliberately NOT an error: the
    bounded folder scan (depth / file-count caps) does not guarantee the
    folder reaches it, so the explicit entry is how users pin a deep or
    over-cap file.  The loader deduplicates when both do load it.

    Indices in error messages are **1-based positions in the original
    list** so they line up with the per-card display in the UI.  Empty /
    whitespace-only entries are skipped without error (the panel filters
    them on save) but still consume an index slot to keep this alignment.
    When ``workspace_cwd`` is provided, relative paths are normalized through
    that cwd before duplicate checks.
    Absolute file and folder entries are accepted even when they target
    another host; incompatible host paths are reported as missing at load
    time.
    """
    errors: list[str] = []

    # Per-entry checks, plus collect normalized values for cross-checks.
    # ``""`` placeholders preserve list-position indexing for blanks and
    # entries that already failed a per-entry check.
    file_norms: list[str] = []
    folder_norms: list[str] = []

    for i, raw in enumerate(memory.files, 1):
        p = raw.strip()
        if not p:
            file_norms.append("")
            continue
        expanded = os.path.expanduser(p)
        if not expanded.lower().endswith(_ALLOWED_SUFFIXES):
            errors.append(f"Memory File {i}: '{p}' must end with .md or .txt.")
            # Still include in cross-checks — a wrong-extension file can
            # still duplicate or sit under a folder, and reporting both
            # surfaces in one save round-trip is friendlier.
        file_norms.append(_normalize_for_compare(expanded, workspace_cwd=workspace_cwd))

    for i, raw in enumerate(memory.folders, 1):
        p = raw.strip()
        if not p:
            folder_norms.append("")
            continue
        expanded = os.path.expanduser(p)
        norm = _normalize_for_compare(expanded, workspace_cwd=workspace_cwd)
        # Check the raw form too: a bare drive ("C:") is drive-relative
        # (``ntpath.isabs`` is false), so it rebases onto ``workspace_cwd``
        # before the root check would ever see "c:".
        if _is_root_norm(norm) or _is_root_norm(_normalize_for_compare(expanded)):
            errors.append(
                f"Memory Folder {i}: '{p}' is a filesystem root; walking it would scan the "
                f"entire volume. Configure a specific folder instead."
            )
            # Excluded from duplicate cross-checks — the entry is already
            # rejected outright.
            folder_norms.append("")
            continue
        folder_norms.append(norm)

    # Duplicate detection — flag the *later* occurrence so the first one
    # acts as the canonical entry.
    _flag_duplicates(errors, memory.files, file_norms, "File")
    _flag_duplicates(errors, memory.folders, folder_norms, "Folder")

    return errors


def _normalize_for_compare(p: str, *, workspace_cwd: str | None = None) -> str:
    """Normalize a path for case-insensitive comparison.

    Uses :mod:`posixpath` so the result always uses forward slashes,
    making comparisons stable across YAML authored on Windows vs POSIX.
    Collapses ``./``, redundant separators, and trailing slashes; then
    lower-cases so ``AGENTS.md`` and ``agents.md`` compare equal.

    Drive-absolute Windows paths normalize their tail against the drive
    anchor, mirroring Windows semantics where ".." clamps at the drive
    root: "C:\\.." → "c:".  Fed whole to :func:`posixpath.normpath`,
    "c:/.." would instead treat "c:" as an ordinary consumable component
    and reduce to "." — hiding a drive root from the validator's root
    check.  UNC paths likewise anchor at host+share (mirroring
    :func:`ntpath.splitdrive`): "\\\\server\\share\\..\\docs" →
    "//server/share/docs", where posixpath alone would let ".." consume
    the share and produce "//server/docs" — misread as a share root.
    Redirector-prefixed UNC forms preserve their leading ";" resolver
    specifiers but locate host+share after those directives.
    """
    cleaned = p.strip()
    if workspace_cwd and not _is_absolute_path(cleaned):
        cleaned = str(Path(workspace_cwd) / cleaned)
    cleaned = cleaned.replace("\\", "/")
    if len(cleaned) >= 3 and cleaned[0].isalpha() and cleaned[1:3] == ":/":
        # Pin the tail to exactly one leading slash first: POSIX gives
        # "//" special root status and preserves it ("//docs", "//.." →
        # "//"), but after a drive anchor extra separators are noise on
        # Windows — without the pin, "C://docs" would not normalize
        # together with "C:/docs", so separator variants dodge duplicate
        # detection ("C://.."-style roots would still be caught, but only
        # by the trailing-slash catch-all, as "c://" instead of the
        # canonical bare-drive "c:").
        tail = posixpath.normpath("/" + cleaned[2:].lstrip("/"))
        return (cleaned[:2] + ("" if tail == "/" else tail)).lower()
    if cleaned.startswith("//"):
        parts = cleaned[2:].split("/")
        anchor_parts = _strip_redirector_specifiers(parts)
        specifier_count = len(parts) - len(anchor_parts)
        if (
            len(anchor_parts) >= 2
            and anchor_parts[0] not in ("", "?", ".", "..")
            and anchor_parts[1] not in ("", ".", "..")
        ):
            # UNC paths anchor at host+share (ntpath.splitdrive's drive),
            # so ".." clamps at the share instead of consuming it.  The
            # leading ";" components in redirector forms are resolver
            # directives, not host/share; retain them in the spelling but
            # locate the anchor after all of them.
            # Extended/device forms ("//?/", "//./") keep the whole-path
            # treatment (Windows disables normalization after "\\?\"
            # anyway), as do paths with an empty or dot host/share slot —
            # there is no meaningful anchor to preserve.
            anchor = "//" + "/".join([*parts[:specifier_count], *anchor_parts[:2]])
            rest = "/".join(anchor_parts[2:])
            tail = posixpath.normpath("/" + rest.lstrip("/"))
            return (anchor + ("" if tail == "/" else tail)).lower()
    return posixpath.normpath(cleaned).lower()


def _is_bare_drive_norm(norm: str) -> bool:
    """Whether a normalized path is a bare Windows drive ("C:\\" → "c:")."""
    return len(norm) == 2 and norm[1] == ":" and norm[0].isalpha()


def _strip_redirector_specifiers(segments: list[str]) -> list[str]:
    """Drop leading ";"-prefixed redirector/session specifiers.

    Windows accepts both the fused form (";CSC;LanmanRedirector") and a
    chain of *separate* ";" components (";LanmanRedirector/;a" — the
    CVE-2019-1220 syntax "\\\\;LanmanRedirector\\;xxxx\\server\\share").
    Every leading component starting with ";" is a resolver directive,
    not share content, so all of them must be consumed before the
    server/share depth rule is applied — stripping only the first would
    let a chained share root masquerade as a "subfolder".  A genuine
    directory named ";x" can only appear *after* the server component,
    which never starts with ";", so real children are never stripped.
    """
    index = 0
    while index < len(segments) and segments[index].startswith(";"):
        index += 1
    return segments[index:]


def _device_namespace_root(body: str) -> bool | None:
    """Classify a Windows device-namespace path body as root / not-root.

    *body* is the normalized remainder after the leading slashes and any
    "?/" / "./" prefix: "c:/docs", "globalroot/device/harddiskvolume1",
    "volume{guid}/docs".  Returns None when the body is not a recognized
    device form (a real UNC hostname cannot contain ":", "{", or a
    leading ";", and "globalroot" is a reserved object-manager name, so
    misclassifying a genuine server by these heads is not a practical
    concern).
    """
    head, _, rest = body.partition("/")
    if head.startswith(";"):
        # "\\\\;LanmanRedirector\\server\\share" (optionally chained, either
        # fused "\\\\;CSC;LanmanRedirector\\..." or as separate components
        # "\\\\;LanmanRedirector\\;a\\...") explicitly routes UNC resolution
        # through a named redirector; the remainder is ordinary
        # server/share space, so the share-root rule applies to it —
        # without this, the component-count fallback would count the
        # redirector specifiers as path segments and wave a share root
        # through as a "subfolder".
        return len(_strip_redirector_specifiers(body.split("/"))) <= 2
    if _is_bare_drive_norm(head):
        # "c:" addresses the whole drive; "c:/docs" is an ordinary folder.
        return not rest
    if head == "unc":
        # "unc/server" or "unc/server/share" is a host/share root — both the
        # "\\\\?\\UNC\\..." extended and "\\\\.\\UNC\\..." device spellings land
        # here (the latter with its "./" collapsed by normalization, so a
        # genuine server literally named "unc" is conservatively treated as
        # this form too).  "\\\\?\\UNC\\;LanmanRedirector\\..." routes through a
        # redirector just like the "\\\\;..." spelling, so specifiers are
        # stripped before counting.
        return len(_strip_redirector_specifiers(body.split("/")[1:])) <= 2
    if head == "globalroot":
        # GLOBALROOT exposes the NT object namespace.  "??"-style arcs
        # alias the regular Win32 namespace, so they recurse.  Device paths
        # split two ways: volume devices address a whole volume themselves
        # ("globalroot/device/harddiskvolumeshadowcopy1" — the one form not
        # reachable by drive letter, so its subfolders stay valid), while
        # network-provider devices (mup, lanmanredirector, p9rdr, webdav…)
        # re-enter share space two levels deeper, optionally behind the
        # same ";" specifier chain ("device/mup/;lanmanredirector/…").
        # Providers are an open set, so every device that is not a
        # recognized volume gets the conservative share-depth rule.
        segments = body.split("/")
        if len(segments) >= 2 and segments[1] in ("??", "global??"):
            return _is_root_norm("//?/" + "/".join(segments[2:]))
        if len(segments) >= 3 and segments[1] == "device":
            if segments[2].startswith(("harddiskvolume", "cdrom")):
                return len(segments) <= 3
            return len(_strip_redirector_specifiers(segments[3:])) <= 2
        return body.count("/") <= 2
    if head.startswith("volume{"):
        # "volume{guid}" addresses a whole volume by GUID.
        return not rest
    return None


def _is_root_norm(norm: str) -> bool:
    """Whether a normalized path is a filesystem root.

    Roots keep their trailing slash through :func:`_normalize_for_compare`
    ("/" and the POSIX-special "//"); Windows drive roots normalize to a
    bare drive letter ("C:\\" → "c:"); UNC hosts and share roots normalize
    to "//server" / "//server/share" — pathlib treats a share root as its
    own parent, so the loader could never walk one anyway.  Extended-length
    forms alias the plain ones and are unwrapped first: "//?/c:" ≙ "c:",
    "//?/unc/server/share" ≙ "//server/share"; device-namespace volume
    addresses ("//?/globalroot/device/<volume>", "//?/volume{guid}") are
    volume roots too.  Redirector-prefixed UNC forms
    ("\\\\;LanmanRedirector\\server\\share", chained "\\\\;a;b\\...") strip
    the ";" specifier before the share-root rule.  Note :func:`_normalize_for_compare` collapses the
    "\\\\.\\" device prefix outright ("\\\\.\\C:\\docs" → "//c:/docs"), so the
    plain-"//" branch must also recognize device heads rather than treat
    them as UNC hostnames.  The final UNC test is deliberately
    conservative: a POSIX "//tmp"-style doubled-slash path is also flagged
    even though it aliases "/tmp" — configure it without the doubled slash
    instead.
    """
    if norm.endswith("/"):
        return True
    if _is_bare_drive_norm(norm):
        return True
    if not norm.startswith("//"):
        return False
    body = norm[2:]
    if body.startswith(("?/", "./")):
        # Extended-length ("\\\\?\\...") / device ("\\\\.\\...") prefix.
        body = body[2:]
        device = _device_namespace_root(body)
        if device is not None:
            return device
        return _is_root_norm(body)
    device = _device_namespace_root(body)
    if device is not None:
        return device
    # "//server" or "//server/share" is a host/share root.
    return norm.count("/") <= 3


def _flag_duplicates(
    errors: list[str],
    raw_values: list[str],
    norms: list[str],
    kind: str,
) -> None:
    """Append duplicate-entry errors to *errors* in 1-based list order."""
    seen: dict[str, int] = {}
    for i, norm in enumerate(norms, 1):
        if not norm:
            continue
        if norm in seen:
            first = seen[norm]
            errors.append(
                f"Memory {kind} {i}: '{raw_values[i - 1].strip()}' duplicates {kind} {first} "
                f"(paths are matched case-insensitively after normalization)."
            )
        else:
            seen[norm] = i


def memory_file_path(raw: str, base: Path) -> Path | None:
    """An existing eligible reference file, shared by full loading and workspace indexes."""
    path = _safe_resolve_file(raw, base)
    if path is None or not path.is_file() or path.suffix.lower() not in _ALLOWED_SUFFIXES:
        return None
    return path


def _safe_resolve_file(raw: str, base: Path) -> Path | None:
    """Resolve a configured file path.

    Relative file paths resolve under *base* and must stay inside it.
    Native absolute file paths are allowed.  Foreign absolute paths (for
    example ``C:\\foo.md`` on POSIX) cannot be loaded on this host and return
    ``None`` so callers can report the configured path as missing.
    """
    expanded = os.path.expanduser(raw.strip())
    if _is_absolute_path(expanded):
        path = Path(expanded)
        if not path.is_absolute():
            return None
        try:
            return path.resolve()
        except OSError:
            return path
    try:
        resolved = (base / expanded).resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(base):
        return None
    return resolved


def _safe_resolve_folder(raw: str, base: Path) -> Path | None:
    """Resolve a configured folder path.

    Relative folder paths resolve under *base* and must stay inside it
    (``..`` traversal or symlink targets escaping *base* return ``None``).
    Native absolute folder paths are allowed, mirroring absolute files.
    Foreign absolute paths (for example ``C:\\docs`` on POSIX) cannot be
    loaded on this host and return ``None`` so callers can report the
    configured path as missing.  Filesystem roots return ``None`` — even
    the bounded scan has no business starting at one.  *base* is expected
    to be already resolved.
    """
    expanded = os.path.expanduser(raw.strip())
    if _is_root_norm(_normalize_for_compare(expanded)):
        # Bare drives ("C:") rebase onto *base* and UNC share roots resolve
        # to their own parent — neither would reach the parent==self check
        # below in its configured form, so reject that form directly.
        return None
    if _is_absolute_path(expanded):
        path = Path(expanded)
        if not path.is_absolute():
            return None
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
    else:
        try:
            resolved = (base / expanded).resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(base):
            return None
    if resolved.parent == resolved:
        # A root is never a curated memory folder.
        return None
    return resolved


def _rel(path: Path, base: Path) -> str:
    """Return *path* relative to *base* in POSIX form.

    Always emits ``/`` separators so headers, ``loaded_files``, and
    ``skipped_files`` stay consistent across platforms.
    """
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


FOLDER_MAX_FILES = 100
"""Per-folder cap on collected ``.md``/``.txt`` files."""

FOLDER_MAX_DEPTH = 2
"""How many subdirectory levels below a configured folder are scanned."""

FOLDER_SCAN_MAX_ENTRIES = 10_000
"""Work bound: total directory entries examined per configured folder.

The file/depth caps bound what is *collected*, not what is *scanned* — a
huge or file-less tree (thousands of subdirectories, a directory with a
million entries) would still stall agent construction without a bound on
the traversal itself.
"""


def _walk_folder(folder: Path, base: Path, seen_files: set[object]) -> list[Path]:
    """Collect ``.md``/``.txt`` files under *folder*, breadth-first, bounded.

    The scan is deliberately bounded so a huge tree (or an accidentally
    configured volume/share) cannot stall agent construction: the folder's
    own files are collected first, then subdirectories up to
    ``FOLDER_MAX_DEPTH`` levels deep, stopping once ``FOLDER_MAX_FILES``
    files are found — breadth-first, so shallow (typically curated) files
    win over deep ones when the cap bites.

    Only files whose physical identity (see ``path_identity``) is not
    already in *seen_files* are collected — and only those count toward
    ``FOLDER_MAX_FILES``, so aliases of an already-loaded file (explicit,
    earlier-folder, or within this folder) cannot crowd unique files out
    of the cap.  Collected identities are registered in *seen_files* as
    the scan goes.

    The traversal itself is work-bounded: at most
    ``FOLDER_SCAN_MAX_ENTRIES`` directory entries are examined in total
    (a directory wider than the remaining budget is truncated in
    filesystem order), so the file/depth caps cannot be defeated by huge
    or file-less trees.  Since every queued subdirectory was itself an
    entry of its parent, the budget also bounds the number of ``scandir``
    calls.

    Follows symlinks but tracks visited directory ``realpath``s to break
    cycles **and** prunes any directory or single-file symlink whose
    realpath escapes *base* (the workspace for workspace-contained folders,
    the folder itself for absolute folders outside it), so a configured
    folder cannot be tricked into surfacing files from elsewhere.  Returned
    paths keep the scan order — the folder's own files first, then
    subdirectory files level by level, name-sorted within each directory.
    Order is load-bearing: the token cap downstream consumes candidates in
    list order, so shallow files must come first to win when the budget
    bites.
    """
    results: list[Path] = []
    visited: set[str] = set()
    queue: deque[tuple[Path, int]] = deque([(folder, 0)])
    entry_budget = FOLDER_SCAN_MAX_ENTRIES

    while queue and len(results) < FOLDER_MAX_FILES and entry_budget > 0:
        dirpath, depth = queue.popleft()
        try:
            real = Path(os.path.realpath(dirpath))
        except OSError:
            continue
        if not real.is_relative_to(base):
            # Symlinked into a non-workspace location — prune entirely.
            continue
        if str(real) in visited:
            # Cycle node already scanned via another route.
            continue
        visited.add(str(real))
        try:
            with os.scandir(dirpath) as scan:
                entries = list(islice(scan, entry_budget))
        except OSError:
            continue
        entry_budget -= len(entries)
        entries.sort(key=lambda e: e.name)
        subdirs: list[Path] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=True)
            except OSError:
                continue
            if is_dir:
                subdirs.append(Path(entry.path))
                continue
            if not entry.name.lower().endswith(_ALLOWED_SUFFIXES):
                continue
            file_path = Path(entry.path)
            try:
                file_real = Path(os.path.realpath(file_path))
            except OSError:
                continue
            if not file_real.is_relative_to(base):
                # Single-file symlink pointing outside the workspace.
                continue
            identity = path_identity(file_path)
            if identity in seen_files:
                continue
            seen_files.add(identity)
            results.append(file_path)
            if len(results) >= FOLDER_MAX_FILES:
                break
        if depth < FOLDER_MAX_DEPTH:
            queue.extend((subdir, depth + 1) for subdir in subdirs)

    return results


def _render_file_section(cand: _Candidate, *, level: int) -> str | None:
    """Render a single file as ``## File: <path>\\n\\n<content>\\n``.

    Returns ``None`` if the file became unreadable between discovery and
    rendering (e.g. deleted mid-build) so the caller can record it as
    missing rather than emitting an empty section.
    """
    try:
        content = decode_bytes(cand.abs_path.read_bytes())
    except OSError as e:
        logger.warning("Memory: failed to read %s: %s", cand.abs_path, e)
        return None
    prefix = "#" * level
    return f"\n{prefix} File: {cand.rel_path}\n\n{content}\n"
