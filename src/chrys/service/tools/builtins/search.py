# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code search tools — grep and glob, powered by ripgrep.

Uses the ``rg`` binary for fast, correct file searching with full glob and
regex support.  The binary is bundled as package data in
``chrys/foundation/vendor/ripgrep/`` (downloaded at build time via
``scripts/fetch_rg.sh``).  A system-installed ``rg`` on PATH is also accepted
as a fallback.

When the search pattern contains non-ASCII characters, multiple encoding
passes (UTF-8, GBK, and the system ANSI codepage) are run automatically
to catch files in legacy encodings.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Annotated, Any, cast

from pathspec import GitIgnoreSpec

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.platform.output_capture import BoundedCapture, drain_process_pipes
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.platform.process import (
    MissingWorkingDirectoryError,
    decode_subprocess_output,
    managed_subprocess,
)
from chrys.foundation.vendor import find_rg
from chrys.service.tools.kinds import KIND_SEARCH, tool
from chrys.service.tools.result_metadata import record_process_result, record_process_timeout, tool_error
from chrys.service.tools.workspace_paths import missing_base_cwd_error

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment

_MAX_RESULTS = 50
_MAX_RESULTS_HARD_LIMIT = 100
_TIMEOUT = 30
_MAX_LINE_DISPLAY_CHARS = 2048
_MAX_ERROR_DIAGNOSTIC_CHARS = 512
_WINDOWS_COMMAND_LIMIT = 32_767  # UTF-16 code units, including the terminating NUL.
_WINDOWS_COMMAND_HEADROOM = 512  # Allow PATH shims to expand the executable path when forwarding argv.
_POSIX_COMMAND_LIMIT = 128 * 1024  # Leave room for the inherited environment and OS overhead.
_MAX_LISTING_BYTES = 64 * 1024 * 1024
"""File names one ``rg --files`` listing may print before the search is refused as too broad."""
_MAX_RECORD_BYTES = 1024 * 1024
"""The longest ``rg --json`` record parsed whole.

Each record carries its whole source line, and a match record lists every match
on it, so one minified line can print megabytes. A longer record keeps only
``_RECORD_HEAD_BYTES``, enough to name its file.
"""
_RECORD_HEAD_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_OVERSIZED_LINES_SHOWN = 10


@dataclass(slots=True)
class GrepEntry:
    """A single line from ripgrep output (match or context)."""

    rel_path: str
    line_num: int
    marker: str  # ">" for match, " " for context
    text: str


@dataclass(slots=True)
class LongLine:
    """A line that was truncated for exceeding ``_MAX_LINE_DISPLAY_CHARS``."""

    rel_path: str
    line_num: int
    actual_length: int


@dataclass(slots=True)
class GrepParseResult:
    """Parsed output from a single ``rg --json`` invocation."""

    entries: list[GrepEntry] = field(default_factory=list)
    match_count: int = 0
    """Matches counted toward the limit, those too long to show included."""
    long_lines: list[LongLine] = field(default_factory=list)
    oversized: set[tuple[str, int]] = field(default_factory=set)
    """Matching lines, by file and line number, whose record was longer than ``_MAX_RECORD_BYTES``."""


@dataclass(slots=True)
class _SearchErrors:
    """Bound diagnostics before they enter tool results or persisted metadata."""

    first_diagnostic: str = ""
    diagnostic_count: int = 0
    exit_code: int | None = None
    _seen_headers: set[bytes] = field(default_factory=set)

    def add(self, stderr: str, exit_code: int) -> None:
        if self.exit_code is None:
            self.exit_code = exit_code
        diagnostic = stderr.strip() or f"rg exited with code {exit_code}"
        capture_first = False
        for index, line in enumerate(diagnostic.splitlines()):
            # Continuation lines contain regex causes, source locations and hints.
            # Unprefixed stderr is one diagnostic per invocation.
            if index == 0 or line.startswith("rg:"):
                capture_first = self.diagnostic_count == 0
                digest = hashlib.blake2b(line.encode("utf-8", errors="surrogatepass"), digest_size=16).digest()
                if digest not in self._seen_headers:
                    self._seen_headers.add(digest)
                    self.diagnostic_count += 1
            if capture_first:
                separator = "\n" if index else ""
                # One extra character records truncation without retaining the tail.
                limit = _MAX_ERROR_DIAGNOSTIC_CHARS + 1
                self.first_diagnostic = (self.first_diagnostic + separator + line[:limit])[:limit]

    def summary(self) -> str:
        message = self.first_diagnostic.rstrip()
        if len(self.first_diagnostic) > _MAX_ERROR_DIAGNOSTIC_CHARS:
            marker = "... [truncated]"
            message = self.first_diagnostic[: _MAX_ERROR_DIAGNOSTIC_CHARS - len(marker)] + marker
        if self.diagnostic_count > 1:
            return f"{message} [{self.diagnostic_count - 1} additional diagnostics omitted]"
        return message


_PLATFORM = get_platform()

# Map Python codec names → WHATWG encoding labels accepted by ``rg -E``.
# Only entries where the names differ need to be listed.
_CODEC_TO_WHATWG: dict[str, str] = {
    "cp874": "windows-874",
    "cp932": "shift_jis",
    "cp949": "euc-kr",
    "cp950": "big5",
    "iso8859-1": "windows-1252",
    "latin-1": "windows-1252",
    "euc_kr": "euc-kr",
    "euc_jp": "euc-jp",
    "shift_jis": "shift_jis",
    "gb2312": "gbk",
    "gb18030": "gb18030",
}


def _find_rg() -> str:
    """Locate the ``rg`` binary: bundled vendor copy → PATH → error."""
    path = find_rg()
    if path:
        return path
    raise FileNotFoundError(
        "ripgrep (rg) not found — run scripts/fetch_rg.sh to bundle it, or install rg on your system PATH"
    )


def _to_posix(path: str) -> str:
    """Normalize a path to forward slashes for consistent output across platforms.

    ``PurePath.as_posix()`` is platform-aware: on Windows it converts
    ``\\`` separators; on POSIX it preserves any literal backslash a
    filename may legitimately contain.
    """
    return PurePath(path).as_posix()


def _codec_to_whatwg(codec_name: str) -> str:
    """Convert a Python codec name to a WHATWG encoding label for ``rg -E``."""
    normalized = codec_name.lower().replace("-", "_").replace(" ", "_")
    if normalized in _CODEC_TO_WHATWG:
        return _CODEC_TO_WHATWG[normalized]
    # Many names work directly (utf-8, gbk, big5, etc.)
    return codec_name.lower().replace("_", "-")


def _get_system_ansi_codepage() -> str | None:
    """Detect the system ANSI codepage and return its WHATWG label.

    Returns ``None`` if detection fails or the codepage is already UTF-8.
    """
    try:
        if _PLATFORM.is_windows:
            import ctypes

            ctypes_module = cast(Any, ctypes)
            acp = ctypes_module.windll.kernel32.GetACP()
            codec_name = f"cp{acp}"
        else:
            codec_name = locale.getpreferredencoding(False)

        import codecs

        info = codecs.lookup(codec_name)
        normalized = info.name.lower().replace("-", "_")

        # Skip if it's already UTF-8 or GBK (always included)
        if normalized in ("utf_8", "gbk", "cp936", "gb2312"):
            return None

        return _codec_to_whatwg(info.name)
    except Exception:
        return None


def _get_encodings() -> list[str]:
    """Return the list of WHATWG encoding labels to search with.

    Always includes UTF-8 and GBK.  Adds the system ANSI codepage if it
    differs from both.
    """
    encodings = ["utf-8", "gbk"]
    ansi = _get_system_ansi_codepage()
    if ansi and ansi not in encodings:
        encodings.append(ansi)
    return encodings


def _is_ascii_only(text: str) -> bool:
    """Return ``True`` if *text* contains only ASCII characters."""
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


class _ListingTooLarge(Exception):
    """An ``rg --files`` listing printed more than ``_MAX_LISTING_BYTES``."""


class _RgStopped(Exception):
    """Leaves ``managed_subprocess`` once rg's output is no longer needed.

    Leaving it by an exception kills rg's whole tree; leaving it normally does so
    only while the process it started runs, and a launcher rg runs under may
    already have exited.
    """


class _RgStdout:
    """rg's stdout: kept whole up to ``_MAX_LISTING_BYTES``, or handed to *consume* as it arrives."""

    def __init__(self, consume: Callable[[bytes], bool] | None) -> None:
        self._consume = consume
        self.data = bytearray()
        self.stopped = asyncio.Event()
        """Set once no more output is needed."""
        self.too_large = False

    def feed(self, chunk: bytes) -> None:
        if self.stopped.is_set():
            return
        if self._consume is not None:
            if not self._consume(chunk):
                self.stopped.set()
        elif len(self.data) + len(chunk) > _MAX_LISTING_BYTES:
            self.too_large = True
            self.stopped.set()
        else:
            self.data += chunk


async def _drain_until_stopped(proc: asyncio.subprocess.Process, stdout: _RgStdout, stderr: BoundedCapture) -> None:
    """Read rg's output until it exits, or until *stdout* needs no more of it.

    A stopped rg is left running for ``managed_subprocess`` to kill with its whole
    tree, which on Windows must happen while a launcher rg runs under lives.
    """
    drain = asyncio.ensure_future(drain_process_pipes(proc, stdout, stderr))
    stopped = asyncio.ensure_future(stdout.stopped.wait())
    try:
        await asyncio.wait((drain, stopped), return_when=asyncio.FIRST_COMPLETED)
    finally:
        drain.cancel()
        stopped.cancel()
        await asyncio.gather(drain, stopped, return_exceptions=True)
    error = None if drain.cancelled() else drain.exception()
    if error is not None:
        raise error


async def _run_rg(
    args: list[str],
    *,
    timeout: int = _TIMEOUT,
    cwd: str | None = None,
    consume: Callable[[bytes], bool] | None = None,
) -> tuple[str, str, int]:
    """Run ``rg`` asynchronously and return ``(stdout, stderr, returncode)``.

    With *consume*, stdout goes to it as rg prints it and comes back empty;
    once *consume* returns False, rg is stopped, and its exit code then says
    nothing about the search. Without, stdout comes back whole, and a listing
    longer than ``_MAX_LISTING_BYTES`` stops rg and raises ``_ListingTooLarge``.
    """
    rg = _find_rg()
    stdout = _RgStdout(consume)
    stderr = BoundedCapture(_MAX_STDERR_BYTES)
    returncode = 0
    try:
        async with managed_subprocess(
            rg,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ) as proc:
            await asyncio.wait_for(_drain_until_stopped(proc, stdout, stderr), timeout=timeout)
            if stdout.stopped.is_set():
                raise _RgStopped
            returncode = proc.returncode or 0
    except _RgStopped:
        pass
    except (FileNotFoundError, NotADirectoryError, MissingWorkingDirectoryError) as exc:
        # Process creation can fail because cwd disappeared, even while rg exists.
        if cwd is not None and not os.path.isdir(cwd):
            raise NotADirectoryError(f"search directory unavailable — {cwd}") from exc
        raise
    if stdout.too_large:
        raise _ListingTooLarge
    output = bytes(stdout.data)
    return (
        os.fsdecode(output) if "--null" in args else decode_subprocess_output(output),
        stderr.snapshot().text(),
        returncode,
    )


def _ignore_args(respect_gitignore: bool) -> list[str]:
    # User ripgrep configuration must not silently override this tool's policy.
    return ["--no-config", "--glob", "!.git/", *([] if respect_gitignore else ["--no-ignore-vcs"])]


def _search_cwd(root: str) -> str:
    """Keep CreateProcess's working directory below its Windows path limit."""
    if _PLATFORM.is_windows:
        while len(root.encode("utf-16-le", errors="surrogatepass")) // 2 >= 258:
            parent = os.path.dirname(root)
            if parent == root:
                break
            root = parent
    return root


def _literal_glob(pattern: str) -> str | None:
    """Decode a literal glob component without implementing glob matching."""
    literal: list[str] = []
    escaped = False
    for char in pattern:
        if escaped:
            literal.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in "*?[]{}":
            return None
        else:
            literal.append(char)
    return None if escaped else "".join(literal)


def _glob_scope(root: str, pattern: str | None) -> tuple[str, str | None]:
    """Pass explicitly named directory prefixes to rg as search operands.

    Keep the remaining glob anchored so src/*.py does not become a recursive
    *.py search. The caller chooses the ignore policy for the explicit target.
    """
    if not pattern or pattern.startswith(("!", "#")) or not os.path.isdir(root):
        return root, pattern
    parts = pattern.removeprefix("/").split("/")
    prefix: list[str] = []
    scoped = root
    for part in parts[:-1]:
        literal = _literal_glob(part)
        if not literal or literal in (".", "..") or os.path.splitdrive(literal)[0]:
            break
        # Filesystem lookup may accept SRC for src (or .git. for .git).
        # A glob is case-sensitive: only promote an exact directory entry.
        try:
            with os.scandir(scoped) as entries:
                if not any(entry.name == literal and entry.is_dir() for entry in entries):
                    break
        except OSError:
            break  # Let rg report an unavailable/unreadable search directory.
        if literal == ".git":
            break
        prefix.append(literal)
        scoped = os.path.join(scoped, literal)
    if not prefix:
        return root, pattern
    return scoped, "/" + ("/".join(parts[len(prefix) :]) or "**")


def _read_ignore_probe_file(path: Path) -> str | None:
    """Read small rule/config files; defer unreadable or large inputs to Git."""
    try:
        with path.open("rb") as stream:
            data = stream.read(65_537)
        # Rule/config contents can contain legacy-encoded comments on any OS.
        return data.decode("utf-8", errors="surrogateescape") if len(data) <= 65_536 else None
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def _ignore_rule_names_directory(rule: str, target: str) -> bool:
    """Recognize rules such as /log/* and **/dist/** without broadening '*' rules."""
    named = rule.removeprefix("/")
    recursive = named.startswith("**/")
    named = named.removeprefix("**/")
    if not named.endswith(("/*", "/**")):
        return False
    named = _literal_glob(named.removesuffix("/**").removesuffix("/*"))
    return bool(
        named and (target.lower() == named.lower() or (recursive and target.lower().endswith("/" + named.lower())))
    )


def _gitignore_probe_needed(root: str, repo: str, git: str) -> bool:
    """Skip Git only when local rules cannot affect the explicitly selected directory.

    This is a conservative prefilter, not the authoritative ignore decision.
    Negations never suppress a probe; Git resolves their precedence. Unusual
    configuration and linked worktrees retain the original Git-based path.
    """
    repository = Path(repo)
    if not (repository / ".git").is_dir() or any(
        key in os.environ
        for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")
    ):
        return True
    git_home = Path(os.environ.get("HOME") or Path.home())
    xdg_git = Path(os.environ.get("XDG_CONFIG_HOME") or git_home / ".config") / "git"
    configs = [repository / ".git/config"]
    configs.extend(
        [Path(os.environ["GIT_CONFIG_GLOBAL"])]
        if "GIT_CONFIG_GLOBAL" in os.environ
        else [git_home / ".gitconfig", xdg_git / "config"]
    )
    configs.extend(
        [Path(os.environ["GIT_CONFIG_SYSTEM"])]
        if "GIT_CONFIG_SYSTEM" in os.environ
        else [
            *([] if _PLATFORM.is_windows else [Path("/etc/gitconfig")]),
            Path(git).parent.parent / "etc/gitconfig",
            Path(git).resolve().parent.parent / "etc/gitconfig",
            *([Path(git).parent.parent.parent / "etc/gitconfig"] if _PLATFORM.is_windows else []),
        ]
    )
    for config in configs:
        if not config.is_absolute() and os.fspath(config) != os.devnull:
            return True  # Relative config paths may resolve against Git's -C directory.
        contents = _read_ignore_probe_file(config)
        if contents is None or re.search(r"(?i)\bexcludesfile\b|\[\s*include|\bworktreeconfig\b", contents):
            return True

    relative = _to_posix(os.path.relpath(root, repo))
    sources = [(repository / ".git/info/exclude", relative), (xdg_git / "ignore", relative)]
    directory = Path(root)
    while True:
        sources.append((directory / ".gitignore", _to_posix(os.path.relpath(root, directory))))
        if directory == repository:
            break
        directory = directory.parent
    for source, target in sources:
        contents = _read_ignore_probe_file(source)
        if contents is None:
            return True
        lines = contents.removeprefix("\ufeff").splitlines()
        # Git drops the initial UTF-8 BOM and unescaped trailing spaces. Keep
        # raw variants for escaped whitespace; extra matches only request a Git probe.
        lines += [line.rstrip() for line in lines if line.rstrip() != line]
        if target == "." and any(line in ("*", "**", "/*", "/**") for line in lines):
            return True
        if any(_ignore_rule_names_directory(line, target) for line in lines if not line.startswith("!")):
            return True
        try:
            # Case-folding can overestimate matches on a case-sensitive repo;
            # it must not miss one when Git has core.ignoreCase enabled.
            spec = GitIgnoreSpec.from_lines(line.lower() for line in lines if not line.startswith("!"))
            if spec.match_file(target.lower()) or spec.match_file(target.lower() + "/"):
                return True
        except ValueError, re.error:
            return True
    return False


async def _directory_is_gitignored(root: str) -> bool:
    """Recognize explicit ignored targets, including a directory's own '*' rule."""
    repo = root
    while not os.path.exists(os.path.join(repo, ".git")):
        parent = os.path.dirname(repo)
        if parent == repo:
            return False
        repo = parent
    # A worktree root's rules still govern ordinary project-wide discovery.
    git = shutil.which("git")
    if repo == root or git is None:
        return False
    if not await asyncio.to_thread(_gitignore_probe_needed, root, repo, git):
        return False
    relative = _to_posix(os.path.relpath(root, repo))
    operands = os.fsencode(relative + "\0" + relative + "/\0")
    try:
        async with managed_subprocess(
            git,
            "-C",
            repo,
            "check-ignore",
            "--no-index",
            "--verbose",
            "-z",
            "--stdin",
            cwd=_search_cwd(repo),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        ) as proc:
            stdout, _ = await asyncio.wait_for(proc.communicate(operands), timeout=5)
            if proc.returncode not in (0, 1):
                return False
    except OSError, TimeoutError:
        return False
    try:
        text = os.fsdecode(stdout)
    except UnicodeError:
        # Git can report raw non-UTF-8 source paths or matching patterns.
        text = stdout.decode("utf-8", errors="surrogateescape")
    fields = text.split("\0")
    for index in range(0, len(fields) - 3, 4):
        source, _, rule, operand = fields[index : index + 4]
        if rule.startswith("!"):
            continue
        if operand == relative:
            return True
        # A trailing slash also probes rules for the target's contents. An
        # inherited blanket '*' may coexist with !directory/, so only accept
        # a local blanket rule or an inherited rule naming this directory.
        if source == relative + "/.gitignore" and rule in ("*", "**", "/*", "/**"):
            return True
        origin = (
            os.path.dirname(os.path.join(repo, source))
            if not os.path.isabs(source) and os.path.basename(source) == ".gitignore"
            else repo
        )
        target = _to_posix(os.path.relpath(root, origin))
        if _ignore_rule_names_directory(rule, target):
            return True
    return False


async def _search_files(root: str, pattern: str | None, respect_gitignore: bool) -> list[str] | str:
    """Intersect ignore-aware discovery with rg's native glob matching.

    Wildcards intersect an ignore-aware listing with rg's native glob matcher.
    Literal names are explicit requests and use rg's override semantics; named
    directory prefixes similarly become explicit operands. NUL delimiters
    preserve whitespace and newlines in filenames.
    Explicit ignored directories bypass Git rules for their contents.
    """
    files: list[str] = []
    root, pattern = _glob_scope(root, pattern)
    if respect_gitignore and os.path.isdir(root) and await _directory_is_gitignored(root):
        respect_gitignore = False
    explicit_name = bool(pattern and not pattern.startswith(("!", "#")) and _literal_glob(pattern) is not None)
    # Recognize explicit dots at component/alternative starts and in positive
    # character classes. The extension dot in *.py must not enable hidden files.
    hidden = bool(
        pattern
        and not pattern.startswith(("!", "#"))
        and re.search(r"(?:^|[/,{])(?:\\?\.(?!\.?(?:/|$))|\[(?![!^])[^.\]]*\.[^\]]*\])", pattern)
    )
    cwd, target = (root, ".") if os.path.isdir(root) else os.path.split(root)
    safe_cwd = _search_cwd(cwd)
    if safe_cwd != cwd:
        prefix = _to_posix(os.path.relpath(cwd, safe_cwd))
        # Slash-containing globs are rooted at cwd. Move their anchor with the
        # operand; basename-only globs already match at any depth.
        if pattern:
            negation = "!" if pattern.startswith("!") else ""
            body = pattern.removeprefix("!")
            if body.startswith("/") or "/" in body.removesuffix("/"):
                escaped = "".join("\\" + char if char in "\\*?[]{}!#" else char for char in prefix)
                pattern = f"{negation}{escaped}/{body.removeprefix('/')}"
        target = os.path.relpath(root, safe_cwd)
        cwd = safe_cwd
    if target == "-":
        target = os.path.join(".", target)
    patterns = (pattern,) if explicit_name else ((None,) if pattern is None or pattern == "*" else (None, pattern))
    for index, filename_pattern in enumerate(patterns):
        filters = [] if filename_pattern is None else ["--glob", filename_pattern]
        try:
            stdout, stderr, code = await _run_rg(
                [
                    "--files",
                    "--null",
                    *(["--hidden"] if hidden else []),
                    *filters,
                    *_ignore_args(respect_gitignore),
                    "--",
                    target,
                ],
                cwd=cwd,
            )
        except _ListingTooLarge:
            # Searching only part of the listing would hide matches without saying so.
            return tool_error(
                "search_too_broad",
                f"too many files under {root} to list (over {_MAX_LISTING_BYTES // (1024 * 1024)} MiB of names)"
                " — search a narrower path",
            )
        if code not in (0, 1):
            errors = _SearchErrors()
            errors.add(stderr, code)
            record_process_result(code)
            return tool_error("search_process_failed", errors.summary())
        found = stdout.split("\0")
        if index == 0:
            files = [name for name in found if name]
        else:
            matches = set(found)
            files = [name for name in files if name in matches]
    return [os.path.normpath(os.path.join(cwd, name)) for name in files]


def _argument_size(argument: str) -> int:
    """Measure one appended argument, including its separator or terminator."""
    if _PLATFORM.is_windows:
        quoted = subprocess.list2cmdline([argument])
        return len(quoted.encode("utf-16-le", errors="surrogatepass")) // 2 + 1
    return len(os.fsencode(argument)) + 1 + (8 if _PLATFORM.is_linux else 0)


def _command_size(command: list[str]) -> int:
    """Measure the executable and fixed options in the platform's budget units."""
    if _PLATFORM.is_windows:
        return len(subprocess.list2cmdline(command).encode("utf-16-le", errors="surrogatepass")) // 2 + 1
    return sum(_argument_size(argument) for argument in command) + (8 if _PLATFORM.is_linux else 0)


def _file_batches(files: list[str], *, command: list[str]) -> Iterator[list[str]]:
    """Fill the remaining command budget after the executable and fixed options."""
    limit = _WINDOWS_COMMAND_LIMIT - _WINDOWS_COMMAND_HEADROOM if _PLATFORM.is_windows else _POSIX_COMMAND_LIMIT
    fixed_size = _command_size(command)
    batch: list[str] = []
    size = fixed_size
    for path in files:
        cost = _argument_size(path)
        if fixed_size + cost > limit:
            raise ValueError(f"search command and file path exceed the platform command budget ({limit})")
        if batch and size + cost > limit:
            yield batch
            batch = []
            size = fixed_size
        batch.append(path)
        size += cost
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


_CUT_RECORD_HEAD = re.compile(rb'\{"type":"(match|context)","data":\{"path":')
"""How rg starts a match or context record: its file comes first, before the line."""
_CUT_RECORD_LINE = re.compile(rb',"line_number":(\d+)[,}]')
"""How rg gives a record's line number, after the line: a JSON string holds no unescaped quote."""
_CUT_RECORD_LINE_WINDOW = 64
"""Bytes kept from one chunk to the next, so a line number split between them is still found."""


def _whole_record(record: bytes | bytearray) -> tuple[str, dict[str, Any]]:
    entry = json.loads(record)
    return entry.get("type", ""), entry.get("data", {})


def _cut_record(head: bytes | bytearray) -> tuple[str, dict[str, Any]]:
    """Read the type and file of a record from the head kept of it."""
    match = _CUT_RECORD_HEAD.match(head)
    if match is None:
        raise ValueError("not a match or context record")
    path, _ = json.JSONDecoder().raw_decode(head[match.end() :].decode("utf-8", errors="replace"))
    return match.group(1).decode("ascii"), {"path": path}


@dataclass(frozen=True, slots=True)
class _OversizedMatch:
    """A match whose record was too long to keep: its file and line number, without the line."""

    rel_path: str
    line_num: int


type _GrepItem = tuple[GrepEntry, LongLine | None] | _OversizedMatch
"""A parsed line and its long-line note, or a match too long to keep."""


class _GrepStream:
    """Parse ``rg --json`` output as it arrives, keeping only what the result can show.

    Output stops being needed once one match more than *max_results* has
    arrived; its caller stops rg there. Matches in *seen_matches* (found by an
    earlier pass) are kept without being counted. A match too long to keep is
    named in ``oversized`` by its file and line, and counts like any other.
    With *skip_binary*, a file's entries wait
    for its ``end`` record, which says whether rg found binary content, and
    only as many wait as could still fill the result.
    """

    def __init__(
        self,
        root: str,
        max_results: int,
        *,
        seen_matches: set[tuple[str, int]] | None = None,
        skip_binary: bool = False,
    ) -> None:
        self.result = GrepParseResult()
        self.full = False
        self.saw_output = False
        self._root = root
        self._max_results = max_results
        self._seen_matches = seen_matches
        self._skip_binary = skip_binary
        self._record = bytearray()
        self._record_cut = False
        self._cut_tail = bytearray()
        self._cut_line: int | None = None
        self._pending: dict[tuple[str | None, str | None], list[_GrepItem]] = {}
        self._pending_matches: dict[tuple[str | None, str | None], int] = {}

    def feed(self, chunk: bytes) -> bool:
        """Take the next chunk of output; False once the result needs no more."""
        self.saw_output = self.saw_output or bool(chunk)
        start = 0
        while not self.full:
            end = chunk.find(b"\n", start)
            piece = chunk[start : len(chunk) if end < 0 else end]
            if not self._record_cut:
                self._record += piece
                if len(self._record) > _MAX_RECORD_BYTES:
                    self._find_cut_line(self._record)
                    del self._record[_RECORD_HEAD_BYTES:]
                    self._record_cut = True
            elif self._cut_line is None:
                self._find_cut_line(piece)
            if end < 0:
                break
            self._take(self._record, cut=self._record_cut)
            self._record.clear()
            self._record_cut = False
            self._cut_tail.clear()
            self._cut_line = None
            start = end + 1
        return not self.full

    def _find_cut_line(self, data: bytes | bytearray) -> None:
        self._cut_tail += data
        found = _CUT_RECORD_LINE.search(self._cut_tail)
        if found is not None:
            self._cut_line = int(found.group(1))
        del self._cut_tail[:-_CUT_RECORD_LINE_WINDOW]

    def _take(self, record: bytearray, *, cut: bool) -> None:
        try:
            entry_type, data = _cut_record(record) if cut else _whole_record(record)
        except ValueError:
            return
        path_data = data.get("path", {})
        key = (path_data.get("text"), path_data.get("bytes"))
        if entry_type == "end":
            items = self._pending.pop(key, [])
            self._pending_matches.pop(key, None)
            if data.get("binary_offset") is None:
                for item in items:
                    self._keep(item)
                    if self.full:
                        return
            return
        if entry_type not in ("match", "context"):
            return
        waiting = self._pending_matches.get(key, 0)
        if self._skip_binary and waiting > self._max_results - self.result.match_count:
            return  # Enough of this file waits to fill the result; a dense file sends many more.
        rel_path = self._rel_path(path_data)
        if cut:
            # The line itself was not kept: a match is named by its file and line, its context dropped.
            if entry_type != "match" or self._cut_line is None:
                return
            item: _GrepItem = _OversizedMatch(rel_path, self._cut_line)
        else:
            item = self._entry(entry_type, data, rel_path)
        if not self._skip_binary:
            self._keep(item)
            return
        if self._counts(item):
            self._pending_matches[key] = waiting + 1
        self._pending.setdefault(key, []).append(item)

    def _rel_path(self, path_data: dict[str, Any]) -> str:
        file_path = (
            os.fsdecode(base64.b64decode(path_data["bytes"])) if "bytes" in path_data else path_data.get("text", "")
        )
        if file_path and not os.path.isabs(file_path):
            file_path = os.path.join(self._root, file_path)
        return _to_posix(os.path.relpath(file_path, self._root)) if file_path else ""

    def _entry(self, entry_type: str, data: dict[str, Any], rel_path: str) -> tuple[GrepEntry, LongLine | None]:
        line_num = data.get("line_number", 0)
        line_text = data.get("lines", {}).get("text", "").rstrip("\r\n")
        long_line = None
        if len(line_text) > _MAX_LINE_DISPLAY_CHARS:
            long_line = LongLine(rel_path, line_num, len(line_text))
            line_text = line_text[:_MAX_LINE_DISPLAY_CHARS] + "... [truncated]"
        return GrepEntry(rel_path, line_num, ">" if entry_type == "match" else " ", line_text), long_line

    def _counts(self, item: _GrepItem) -> bool:
        if isinstance(item, _OversizedMatch):
            key = (item.rel_path, item.line_num)
        elif item[0].marker == ">":
            key = (item[0].rel_path, item[0].line_num)
        else:
            return False
        return self._seen_matches is None or key not in self._seen_matches

    def _keep(self, item: _GrepItem) -> None:
        counts = self._counts(item)
        if counts:
            self.result.match_count += 1
            if self.result.match_count > self._max_results:
                self.full = True
                return
        if isinstance(item, _OversizedMatch):
            if counts:  # Otherwise an earlier pass showed or named it.
                self.result.oversized.add((item.rel_path, item.line_num))
            return
        entry, long_line = item
        self.result.entries.append(entry)
        if long_line is not None:
            self.result.long_lines.append(long_line)


def _oversized_note(lines: set[tuple[str, int]]) -> str:
    if not lines:
        return ""
    ordered = sorted(lines)
    shown = ", ".join(f"{rel_path}:{line_num}" for rel_path, line_num in ordered[:_MAX_OVERSIZED_LINES_SHOWN])
    more = len(ordered) - _MAX_OVERSIZED_LINES_SHOWN
    if more > 0:
        shown += f" and {more} more line(s)"
    limit = _MAX_RECORD_BYTES // (1024 * 1024)
    return f"\n\n[Matching lines too long to show (over {limit} MiB of search output each): {shown}]"


def _format_matches(entries: list[GrepEntry]) -> list[str]:
    """Group entries into context blocks and format for display."""
    matches: list[str] = []
    block: list[GrepEntry] = []

    def _flush_block() -> None:
        if not block:
            return
        first_match = next((b for b in block if b.marker == ">"), block[0])
        header = f"{first_match.rel_path}:{first_match.line_num}"
        lines = [f"  {e.marker} {e.line_num:>4} | {e.text}" for e in block]
        matches.append(header + "\n" + "\n".join(lines))

    for entry in entries:
        if block:
            prev = block[-1]
            if entry.rel_path != prev.rel_path or entry.line_num != prev.line_num + 1:
                _flush_block()
                block = []
        block.append(entry)

    _flush_block()
    return matches


async def _grep_impl(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    context_lines: int = 2,
    max_results: int = _MAX_RESULTS,
    *,
    base_cwd: str | None = None,
    respect_gitignore: bool = True,
) -> str:
    if type(max_results) is not int or max_results < 1:
        return tool_error(
            "invalid_max_results",
            "max_results must be an integer >= 1 (the maximum number of results to return).",
        )
    max_results = min(max_results, _MAX_RESULTS_HARD_LIMIT)
    missing_base = missing_base_cwd_error(path, base_cwd)
    if missing_base is not None:
        return missing_base
    root = resolve_workspace_path(path, base_cwd=base_cwd)
    if not os.path.exists(root):
        return tool_error("path_not_found", f"path not found — {root}", details={"path": path, "resolved_path": root})

    base_args = [
        "--json",
        "-e",
        pattern,
        "-C",
        str(context_lines),
        *_ignore_args(respect_gitignore),
    ]

    # Determine which encodings to search.  For pure-ASCII patterns a single
    # pass suffices since ASCII bytes are identical across all encodings.
    encodings = ["utf-8"] if _is_ascii_only(pattern) else _get_encodings()

    all_entries: dict[tuple[str, int], GrepEntry] = {}
    all_long_lines: list[LongLine] = []
    seen_long: set[tuple[str, int]] = set()
    oversized: set[tuple[str, int]] = set()
    total_matches = 0
    # Matches too long to show count toward the limit too, so a full search can show fewer.
    limited = False
    errors = _SearchErrors()

    try:
        async with asyncio.timeout(_TIMEOUT):
            files = [root]
            globbed_directory = bool(glob and glob != "*" and os.path.isdir(root))
            if (
                not globbed_directory
                and respect_gitignore
                and os.path.isdir(root)
                and await _directory_is_gitignored(root)
            ):
                base_args.append("--no-ignore-vcs")
            search_cwd = root if globbed_directory and _search_cwd(root) == root else None
            if globbed_directory:
                selected = await _search_files(root, glob, respect_gitignore)
                if isinstance(selected, str):
                    return selected
                files = []
                for name in selected:
                    if search_cwd is None:
                        files.append(name)
                        continue
                    relative = os.path.relpath(name, root)
                    # Even after --, bare '-' means stdin to rg.
                    files.append(os.path.join(".", relative) if relative == "-" else relative)
            encoding_args = {encoding: [*base_args, "-E", encoding, "--"] for encoding in encodings}
            if not files:
                # Validate with rg's own regex/flag parser against explicitly empty
                # stdin. Never let an empty candidate set fall back to cwd traversal.
                _, stderr, returncode = await _run_rg([*encoding_args["utf-8"], "-"])
                if returncode not in (0, 1):
                    errors.add(stderr, returncode)
                    record_process_result(returncode)
                    return tool_error(
                        "search_process_failed", errors.summary(), details={"path": path, "pattern": pattern}
                    )
                return surrogate_safe_text(f"No matches found for /{pattern}/ in {root}")
            rg = _find_rg()
            command = max(([rg, *args] for args in encoding_args.values()), key=_command_size)
            fatal_error = False
            # Never invoke rg with an empty argv file list: it would search cwd/stdin.
            for batch in _file_batches(files, command=command):
                utf8_searched = False
                for encoding, args in tuple(encoding_args.items()):
                    parser = _GrepStream(
                        root,
                        max_results - total_matches - len(oversized),
                        seen_matches={key for key, entry in all_entries.items() if entry.marker == ">"} | oversized,
                        # Enumerated paths are explicit to rg, which enables binary searching.
                        # Filter before counting matches; user-supplied files retain rg's defaults.
                        skip_binary=globbed_directory,
                    )
                    _, stderr, returncode = await _run_rg([*args, *batch], cwd=search_cwd, consume=parser.feed)
                    # A full result already says it is limited. The rg stopped for it was killed: its exit code
                    # is the kill's, and its stderr names only the files it reached before then.
                    if not parser.full and returncode not in (0, 1):
                        errors.add(stderr, returncode)
                        if returncode == 2 and not parser.saw_output:
                            # File-read errors still emit a JSON summary. No stdout
                            # means rg failed before searching; repeating cannot help.
                            # UTF-8 runs first. Its completed search validates the
                            # shared args; only -E changes in subsequent passes.
                            if utf8_searched:
                                del encoding_args[encoding]
                                continue
                            fatal_error = True
                            break
                        if returncode != 2:
                            continue
                    if encoding == "utf-8":
                        utf8_searched = True

                    parsed = parser.result
                    limited = limited or parser.full
                    oversized |= parsed.oversized
                    for entry in parsed.entries:
                        key = (entry.rel_path, entry.line_num)
                        previous = all_entries.get(key)
                        if previous is None or (previous.marker != ">" and entry.marker == ">"):
                            all_entries[key] = entry
                            if entry.marker == ">":
                                total_matches += 1
                                oversized.discard(key)  # Too long to show under another encoding only.
                    for ll in parsed.long_lines:
                        key = (ll.rel_path, ll.line_num)
                        if key not in seen_long:
                            seen_long.add(key)
                            all_long_lines.append(ll)
                    if limited or total_matches + len(oversized) >= max_results:
                        break
                if fatal_error or not encoding_args or limited or total_matches + len(oversized) >= max_results:
                    break
    except NotADirectoryError as exc:
        # The session directory can vanish between the check above and rg's spawn.
        missing_base = missing_base_cwd_error(path, base_cwd)
        if missing_base is not None:
            return missing_base
        return tool_error("path_not_found", str(exc), details={"path": path, "resolved_path": root})
    except FileNotFoundError:
        return tool_error("ripgrep_not_found", "ripgrep (rg) not found")
    except TimeoutError:
        record_process_timeout(_TIMEOUT)
        return tool_error("search_timeout", f"search timed out after {_TIMEOUT}s", retryable=True)
    except Exception as e:
        return tool_error("search_failed", f"search failed — {e}", details={"path": path, "pattern": pattern})

    if not total_matches and not oversized:
        if errors.exit_code is not None:
            record_process_result(errors.exit_code)
            # rg reports a session directory deleted after the check above as its own IO error.
            missing_base = missing_base_cwd_error(path, base_cwd)
            if missing_base is not None:
                return missing_base
            return tool_error("search_process_failed", errors.summary(), details={"path": path, "pattern": pattern})
        return surrogate_safe_text(f"No matches found for /{pattern}/ in {root}")

    # Sort by (file, line_number) for consistent display
    matches = _format_matches(sorted(all_entries.values(), key=lambda e: (e.rel_path, e.line_num)))
    header = (
        f"Found {total_matches} match(es) in {root}"
        if total_matches
        else f"Found matches for /{pattern}/ in {root}, but every matching line is too long to show"
    )
    if limited or total_matches + len(oversized) >= max_results:
        header += f" (limited to {max_results})"
    result = "\n\n".join([header, *matches])
    if all_long_lines:
        details = ", ".join(f"{ll.rel_path}:{ll.line_num} ({ll.actual_length} chars)" for ll in all_long_lines)
        result += f"\n\n[Long lines truncated to {_MAX_LINE_DISPLAY_CHARS} chars: {details}]"
    result += _oversized_note(oversized)
    if errors.exit_code is not None:
        record_process_result(errors.exit_code)
        result += "\n\n" + tool_error(
            "search_incomplete",
            f"search results may be incomplete — {errors.summary()}",
            details={"path": path, "pattern": pattern},
        )
    # Keep raw path identities through filtering/deduplication; escape only display text.
    return surrogate_safe_text(result)


@tool(kind=KIND_SEARCH)
async def grep(
    pattern: Annotated[str, "Regex pattern to search for in file contents."],
    path: Annotated[str, "Directory or file path to search in."] = ".",
    glob: Annotated[
        str | None, "Glob pattern to filter files (e.g. '*.py', '**/*.ts'). Omit to search all files."
    ] = None,
    context_lines: Annotated[int, "Number of context lines before and after each match."] = 2,
    max_results: Annotated[int, "Maximum number of matches to return (must be at least 1)."] = _MAX_RESULTS,
) -> str:
    """Search file contents using a regex pattern. Returns matching lines with context.

    Powered by ripgrep (rg --json -e PATTERN -C CONTEXT_LINES -E ENCODING PATH).
    The pattern is a Rust-flavor regex (PCRE-like, not Python re). Glob filtering
    uses gitignore-style rules: patterns without '/' match the filename at any
    depth (e.g. '*.py' matches 'src/foo.py'), patterns with '/' match against
    the relative path. max_results must be at least 1 and is capped at 100 regardless of the value provided.
    Directory searches respect Git ignore rules and ripgrep hidden-file rules.
    Explicit file names and directory prefixes can select hidden or ignored paths.
    Explicit ignored directories bypass Git ignore rules within that directory.
    """
    return await _grep_impl(
        pattern,
        path=path,
        glob=glob,
        context_lines=context_lines,
        max_results=max_results,
    )


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


async def _glob_impl(
    pattern: str,
    path: str = ".",
    max_results: int = _MAX_RESULTS,
    *,
    base_cwd: str | None = None,
    respect_gitignore: bool = True,
) -> str:
    if type(max_results) is not int or max_results < 1:
        return tool_error(
            "invalid_max_results",
            "max_results must be an integer >= 1 (the maximum number of results to return).",
        )
    max_results = min(max_results, _MAX_RESULTS_HARD_LIMIT)
    missing_base = missing_base_cwd_error(path, base_cwd)
    if missing_base is not None:
        return missing_base
    root = resolve_workspace_path(path, base_cwd=base_cwd)
    if not os.path.exists(root):
        return tool_error("path_not_found", f"path not found — {root}", details={"path": path, "resolved_path": root})

    try:
        async with asyncio.timeout(_TIMEOUT):
            files = await _search_files(root, pattern, respect_gitignore)
    except NotADirectoryError as exc:
        # The session directory can vanish between the check above and rg's spawn.
        missing_base = missing_base_cwd_error(path, base_cwd)
        if missing_base is not None:
            return missing_base
        return tool_error("path_not_found", str(exc), details={"path": path, "resolved_path": root})
    except FileNotFoundError:
        return tool_error("ripgrep_not_found", "ripgrep (rg) not found")
    except TimeoutError:
        record_process_timeout(_TIMEOUT)
        return tool_error("search_timeout", f"search timed out after {_TIMEOUT}s", retryable=True)
    except Exception as e:
        return tool_error("glob_failed", f"glob failed — {e}", details={"path": path, "pattern": pattern})

    if isinstance(files, str):
        return files
    if not files:
        return surrogate_safe_text(f"No files matching '{pattern}' found in {root}")

    # Convert to relative paths and limit results
    results = []
    for name in files:
        rel = _to_posix(os.path.relpath(name, root))
        results.append(rel)
        if len(results) >= max_results:
            break

    header = f"Found {len(results)} file(s) matching '{pattern}' in {root}"
    if len(results) >= max_results:
        header += f" (limited to {max_results})"
    return surrogate_safe_text(header + "\n" + "\n".join(f"  {r}" for r in results))


@tool(kind=KIND_SEARCH)
async def glob(
    pattern: Annotated[str, "Glob pattern to match file names (e.g. '*.py', 'test_*.py', '**/*.ts')."],
    path: Annotated[str, "Directory to search in."] = ".",
    max_results: Annotated[int, "Maximum number of results to return (must be at least 1)."] = _MAX_RESULTS,
) -> str:
    """Find files by name pattern. Returns relative paths of matching files.

    Powered by ripgrep (rg --files --glob PATTERN PATH). The pattern uses
    gitignore-style glob rules: patterns without '/' match the filename at any
    depth (e.g. '*.py' matches 'src/foo.py'), patterns with '/' match against
    the relative path. Searches recursively through all subdirectories.
    max_results must be at least 1 and is capped at 100 regardless of the value provided.
    Discovery respects Git ignore rules and ripgrep hidden-file rules.
    Explicit file names and directory prefixes can select hidden or ignored paths.
    Explicit ignored directories bypass Git ignore rules within that directory.
    """
    return await _glob_impl(pattern, path=path, max_results=max_results)


class SearchTools:
    """Runtime-bound search tools that resolve relative paths per session."""

    def __init__(self, runtime: SessionEnvironment, *, respect_gitignore: bool = True) -> None:
        self._runtime = runtime
        self._respect_gitignore = respect_gitignore

    def tools(self) -> list:
        """Return search tools with descriptions reflecting this runtime's ignore policy."""
        if self._respect_gitignore:
            policy = (
                "Current setting: Respect .gitignore is enabled. Directory searches respect Git ignore rules.\n"
                "Explicit ignored directories bypass Git ignore rules within that directory."
            )
        else:
            policy = (
                "Current setting: Respect .gitignore is disabled. Directory searches do not respect Git ignore rules "
                "and can include Git-ignored files and directories."
            )
        guidance = (
            f"{policy}\n"
            "Ripgrep hidden-file rules and .ignore/.rgignore rules still apply.\n"
            "Explicit file names and directory prefixes can select hidden or ignored paths."
        )
        tools = [self.grep, self.glob]
        for search_tool in tools:
            search_tool.description = f"{search_tool.description.rstrip()}\n\n{guidance}"
        return tools

    @tool(kind=KIND_SEARCH)
    async def grep(
        self,
        pattern: Annotated[str, "Regex pattern to search for in file contents."],
        path: Annotated[str, "Directory or file path to search in."] = ".",
        glob: Annotated[
            str | None, "Glob pattern to filter files (e.g. '*.py', '**/*.ts'). Omit to search all files."
        ] = None,
        context_lines: Annotated[int, "Number of context lines before and after each match."] = 2,
        max_results: Annotated[int, "Maximum number of matches to return (must be at least 1)."] = _MAX_RESULTS,
    ) -> str:
        """Search file contents using a regex pattern. Returns matching lines with context.

        Powered by ripgrep (rg --json -e PATTERN -C CONTEXT_LINES -E ENCODING PATH).
        The pattern is a Rust-flavor regex (PCRE-like, not Python re). Glob filtering
        uses gitignore-style rules: patterns without '/' match the filename at any
        depth (e.g. '*.py' matches 'src/foo.py'), patterns with '/' match against
        the relative path. max_results must be at least 1 and is capped at 100 regardless of the value provided.
        """
        return await _grep_impl(
            pattern,
            path=path,
            glob=glob,
            context_lines=context_lines,
            max_results=max_results,
            base_cwd=self._runtime.cwd,
            respect_gitignore=self._respect_gitignore,
        )

    @tool(kind=KIND_SEARCH)
    async def glob(
        self,
        pattern: Annotated[str, "Glob pattern to match file names (e.g. '*.py', 'test_*.py', '**/*.ts')."],
        path: Annotated[str, "Directory to search in."] = ".",
        max_results: Annotated[int, "Maximum number of results to return (must be at least 1)."] = _MAX_RESULTS,
    ) -> str:
        """Find files by name pattern. Returns relative paths of matching files.

        Powered by ripgrep (rg --files --glob PATTERN PATH). The pattern uses
        gitignore-style glob rules: patterns without '/' match the filename at any
        depth (e.g. '*.py' matches 'src/foo.py'), patterns with '/' match against
        the relative path. Searches recursively through all subdirectories.
        max_results must be at least 1 and is capped at 100 regardless of the value provided.
        """
        return await _glob_impl(
            pattern,
            path=path,
            max_results=max_results,
            base_cwd=self._runtime.cwd,
            respect_gitignore=self._respect_gitignore,
        )
