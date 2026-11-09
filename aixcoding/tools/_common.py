# ruff: noqa: T201, RUF001, S603, S607, PLW1510
# (stderr notes are the observable-degradation channel (§9); Chinese prose is
#  the data; git subprocess calls are the tool's purpose)
"""Shared library for the aixcoding governance tools.

Consumed by gen_customizations.py / check_r2_diff.py / check_r3_markers.py /
check_import_boundaries.py — see aixcoding/docs/batches/devtools/design.md.

Three concerns live here so the four entry scripts stay thin:

- file classification (custom vs. upstream, per R2's broad "any change to
  upstream" reading, including ``tests/`` and build config);
- marker detection (the R3 ``AIxCoding`` single-line comment annotation,
  case-sensitive, unbracketed, module-attributed) with syntax-aware
  disambiguation between real comment markers and brand-string hits;
- git diff plumbing and design.md table parsing (sign-off state, R2 approval
  tables, custom-point registration tables).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- file classification -----------------------------------------------------

CUSTOM_PATH_PREFIXES = ("aixcoding/",)
CUSTOM_EXACT_PATHS = (".github/workflows/aixcoding-tests.yml", ".github/workflows/aixcoding-upstream-ci.yml")
# ADR-0011: custom product code lands in src/chrys/{tier}/aixcoding/ (tier = kernel/service/orchestration/app).
CUSTOM_SRC_TIER_RE = re.compile(r"^src/chrys/[^/]+/aixcoding(/|$)")


def to_posix(path: str | Path) -> str:
    """Normalize to a git-style posix relative path."""
    return str(path).replace("\\", "/")


def is_custom_src(relpath: str) -> bool:
    """True inside src/chrys/{tier}/aixcoding/ (ADR-0011 tier placement)."""
    return CUSTOM_SRC_TIER_RE.match(to_posix(relpath)) is not None


def is_custom(relpath: str) -> bool:
    """True for fork-owned assets (never requires an R2 approval to change)."""
    rel = to_posix(relpath)
    return (
        rel.startswith(CUSTOM_PATH_PREFIXES) or CUSTOM_SRC_TIER_RE.match(rel) is not None or rel in CUSTOM_EXACT_PATHS
    )


def is_upstream(relpath: str) -> bool:
    """True for anything inherited from upstream (R2 scope: any change,
    including tests/ and build/packaging config such as pyproject.toml)."""
    return not is_custom(relpath)


# --- marker detection (R3) ---------------------------------------------------

MARKER_KEYWORD = "AIxCoding"

# Text formats whose line comments ('#' or '<!--') can legally carry a marker.
HASH_COMMENT_SUFFIXES = frozenset(
    {".py", ".pyi", ".tcss", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".sh", ".ps1", ".txt", ".css", ".md"}
)
# Extension-less dotfiles whose '#' lines are real comments (Path.suffix is empty).
HASH_COMMENT_FILENAMES = frozenset({".gitattributes"})
# Structured/binary formats where a marker comment is impossible (R3: those
# custom points register an exemption below instead).
MARKER_EXEMPT_SUFFIXES = frozenset({".json", ".mo", ".exe", ".png", ".jpg", ".ico", ".woff", ".woff2"})

# R3 marker exemptions (formats that cannot carry a comment, ADR-0007): the
# single machine-readable source shared by gen_customizations.py (report view)
# and check_r3_markers.py (gate). Formerly rendered into CUSTOMIZATIONS.md and
# read back from there — retired with that file (customization-registry batch).
MARKER_EXEMPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "tests/architecture/trajectory_wait_manifest.json",
        "生成产物：wait 节点清单随模块路径刷新（`uv run python -m tests.support.trajectory_wait_inventory`）",
        "aixcoding/docs/batches/tier-migration/design.md",
    ),
    (
        "locales/chrys.pot",
        "生成产物：i18n extract 条目（借壳 key 随 msg 定义处生成，R2 表 7 号）",
        "aixcoding/docs/batches/tui/design.md",
    ),
    (
        "locales/zh-Hans/LC_MESSAGES/chrys.po",
        "登记产物：po 中文条目（msgstr=fallback 复制，ADR-0010 细则 2，R2 表 8 号）",
        "aixcoding/docs/batches/tui/design.md",
    ),
)

MODULE_PAREN_RE = re.compile(r"\(([^)]*\baixcoding[^)]*)\)")
# Keyword must be followed by a word boundary: 'AIxCoding telemetry:' matches,
# 'AIxCodingSuffix' does not (design.md marker contract).
MARKER_PREFIX_RE = re.compile(rf"{MARKER_KEYWORD}\b")


@dataclass
class MarkerHit:
    """One textual occurrence of the marker keyword, classified."""

    file: str  # posix relpath
    line: int  # 1-based
    text: str  # the full comment body (or line) carrying the keyword
    is_comment: bool  # True: real R3 comment marker; False: brand-string hit
    module: str = field(default="")  # attributed module, e.g. src/chrys/kernel/aixcoding/x


def classify_marker_body(body: str) -> str:
    """Extract the attributed module from a marker comment body.

    Prefers the parenthesized path — ``# AIxCoding telemetry: emit events
    (src/chrys/kernel/aixcoding/tool_events)``; falls back to the word before the
    colon (``telemetry`` above) for older/abbreviated forms.
    """
    m = MODULE_PAREN_RE.search(body)
    if m:
        return m.group(1).strip()
    if ":" in body:
        head = body.split(":", 1)[0].strip()
        words = head.split()
        if len(words) > 1:  # keyword + module word
            return words[1]
    return ""


def _hit(file: str, lineno: int, body: str, is_comment: bool) -> MarkerHit | None:
    """Build a hit if the keyword starts the given body (word-bounded)."""
    body = body.strip()
    if MARKER_PREFIX_RE.match(body) is None:
        return None  # must start the comment body (rejects '# [AIxCoding]', mid-sentence brand uses, 'AIxCodingSuffix')
    hit = MarkerHit(file=file, line=lineno, text=body, is_comment=is_comment)
    if is_comment:
        hit.module = classify_marker_body(body)
    return hit


def scan_python_file(relpath: str, path: Path) -> list[MarkerHit]:
    """Tokenize-aware scan: real COMMENT tokens vs. string/name hits."""
    hits: list[MarkerHit] = []
    try:
        with path.open("rb") as fh:
            tokens = list(tokenize.tokenize(fh.readline))
    except SyntaxError, tokenize.TokenError, UnicodeDecodeError, OSError:
        return scan_text_file(relpath, path)  # degraded: fall back to line scan
    for tok in tokens:
        if MARKER_KEYWORD not in tok.string:
            continue
        if tok.type == tokenize.COMMENT:
            hit = _hit(relpath, tok.start[0], tok.string.lstrip("#"), True)
        else:
            hit = MarkerHit(
                file=relpath,
                line=tok.start[0],
                text=tok.string,
                is_comment=False,
            )
        if hit is not None:
            hits.append(hit)
    return hits


def _comment_body(line: str, allow_hash: bool = True) -> tuple[int, str] | None:
    """Offset + body of the first comment opening ('#', '<!--' or '/*') in a line.

    HTML comment bodies are cut at the '-->' closer and C-style bodies at the
    '*/' closer so the body carries the marker prose only (CUSTOMIZATIONS.md
    readability). '/*' matters for ``.tcss``/``.css`` marker comments.
    """
    candidates: list[tuple[int, int, str]] = []  # (opening start, body offset, body)
    hash_idx = line.find("#") if allow_hash else -1
    if hash_idx >= 0:
        candidates.append((hash_idx, hash_idx + 1, line[hash_idx + 1 :]))
    tag_idx = line.find("<!--")
    if tag_idx >= 0:
        body = line[tag_idx + 4 :]
        end = body.find("-->")
        if end >= 0:
            body = body[:end]
        candidates.append((tag_idx, tag_idx + 4, body))
    slash_idx = line.find("/*")
    if slash_idx >= 0:
        body = line[slash_idx + 2 :]
        end = body.find("*/")
        if end >= 0:
            body = body[:end]
        candidates.append((slash_idx, slash_idx + 2, body))
    if not candidates:
        return None
    _start, offset, body = min(candidates, key=lambda cand: cand[0])
    return offset, body


def scan_text_file(relpath: str, path: Path, allow_hash: bool = True) -> list[MarkerHit]:
    """Line scan for non-Python text formats ('#' / '<!--' comments).

    ``allow_hash=False`` for Markdown, where '#' opens a heading, not a
    comment — the legal marker form there is the HTML comment (AGENTS.md
    precedent); heading lines fall through to the brand-string bucket.
    """
    hits: list[MarkerHit] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Degraded, but observable (§9): the skipped file is named on stderr.
        print(f"_common: scan skipped unreadable file {path}: {exc}", file=sys.stderr)
        return hits
    for lineno, line in enumerate(text.splitlines(), start=1):
        if MARKER_KEYWORD not in line:
            continue
        opened = _comment_body(line, allow_hash=allow_hash)
        if opened is not None:
            _, body = opened
            hit = _hit(relpath, lineno, body, True)
            if hit is not None:
                hits.append(hit)
                continue
        hits.append(MarkerHit(file=relpath, line=lineno, text=line.strip(), is_comment=False))
    return hits


def added_line_has_marker(line: str, allow_hash: bool = True) -> bool:
    """True if a diff-added line carries an R3 marker comment."""
    if MARKER_KEYWORD not in line:
        return False
    opened = _comment_body(line, allow_hash=allow_hash)
    if opened is None:
        return False
    _, body = opened
    # Same word-boundary contract as _hit: gate and scanner must agree.
    return MARKER_PREFIX_RE.match(body.strip()) is not None


def scan_file(relpath: str, path: Path) -> list[MarkerHit]:
    """Dispatch by suffix; marker-exempt formats (json/binary) return []."""
    suffix = path.suffix.lower()
    if suffix in MARKER_EXEMPT_SUFFIXES:
        return []
    if path.name in HASH_COMMENT_FILENAMES:
        return scan_text_file(relpath, path)
    if suffix in (".py", ".pyi"):
        return scan_python_file(relpath, path)
    if suffix in HASH_COMMENT_SUFFIXES:
        # Markdown: '#' is a heading, not a comment — '<!--' is the marker form.
        return scan_text_file(relpath, path, allow_hash=suffix != ".md")
    return []  # unknown suffix: not scanned (exemption table is the backstop)


# --- git plumbing ------------------------------------------------------------


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    # core.quotepath=off: keep non-ASCII paths (Chinese ADR/doc filenames) as
    # real UTF-8 — the quoted octal-escaped form defeats is_custom() routing.
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def untracked_files(repo: Path) -> list[str]:
    """Untracked (never-added) files, git-ignored ones excluded."""
    return [to_posix(ln) for ln in git(repo, "ls-files", "--others", "--exclude-standard").splitlines() if ln]


def changed_files(repo: Path, base: str) -> list[str]:
    """Files differing from `base` — tracked diff (incl. staged/unstaged
    modifications, deletions) plus untracked files, so a never-added new
    upstream file cannot slip past the R2 gate."""
    tracked = [ln for ln in git(repo, "diff", "--name-only", base).splitlines() if ln]
    seen: dict[str, None] = {}
    for rel in [*tracked, *untracked_files(repo)]:
        seen.setdefault(to_posix(rel), None)
    return list(seen)


def diff_added_lines(repo: Path, base: str, relpath: str) -> list[str]:
    """Added lines ('+' side) of one file's diff vs. base."""
    out = git(repo, "diff", "--unified=0", base, "--", relpath)
    added: list[str] = []
    for line in out.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added.append(line[1:])
    return added


# --- design.md parsing ---------------------------------------------------------

STATUS_FIELD_RE = re.compile(r"^-\s*状态[：:]\s*(.*)$", re.MULTILINE)
SIGNED_OFF = "已确认"


def signoff_status(doc_text: str) -> str | None:
    m = STATUS_FIELD_RE.search(doc_text)
    return m.group(1).strip() if m else None


def parse_table_section(doc_text: str, section_title: str) -> list[dict[str, str]]:
    """Parse the first markdown table under `## <section_title>`.

    Returns rows keyed by stripped header cell text. Missing section -> [].
    Fenced code blocks (``` … ```) are skipped: an example table inside a
    design.md fence must never parse as a live table (fail-closed).
    """
    lines = doc_text.splitlines()
    in_section = False
    in_code = False
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("## "):
            if in_section:
                break  # next section ends the table block
            in_section = line[3:].strip() == section_title
            continue
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue  # alignment row
        if header is None:
            header = cells
            continue
        if header:
            rows.append(dict(zip(header, cells, strict=False)))
    return rows


def find_design_docs(docs_root: Path) -> list[Path]:
    """All batch design.md files (aixcoding/docs/batches/<feature>/, docs-reorg batch).

    Single-level glob under batches/ by design: exact scan surface, no recursion.
    """
    return sorted(p for p in docs_root.glob("batches/*/design.md") if p.is_file())


def r2_approved_upstream_files(docs_root: Path) -> tuple[set[str], list[str]]:
    """Union of upstream files approved via signed-off design.md R2 tables.

    Returns (approved set, notes) — unsigned designs are ignored and noted,
    so an unsigned R2 table never authorizes anything (design.md sign-off is
    the R2 approval record, R10/ADR-0007).
    """
    approved: set[str] = set()
    notes: list[str] = []
    for doc in find_design_docs(docs_root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        status = signoff_status(text) or ""
        rows = parse_table_section(text, "上游改动清单（R2；无上游改动则删除本节）")
        if not rows:
            rows = parse_table_section(text, "上游改动清单（R2）")
        if not rows:
            continue
        rel = to_posix(doc.relative_to(docs_root.parents[1]))
        if status != SIGNED_OFF:
            notes.append(f"{rel}: R2 table present but sign-off status is '{status or 'missing'}' — ignored")
            continue
        cells = [(row.get("文件") or "").strip() for row in rows]
        if rows and not any(cells):
            # Header drift must be loud, not a silent empty pool (design.md assumption table).
            notes.append(
                f"{rel}: R2 table has {len(rows)} row(s) but no recognizable '文件' column — header drift? ignored"
            )
            continue
        repo_root = docs_root.parents[1]
        for cell in cells:
            # One cell may name several files as `a` + `b` + `c`: every backtick
            # span is a path, and a bare cell is a single path. A name that is
            # not a real file in the repo is a loud note, never a silent pool
            # entry (a misspelled name would otherwise masquerade as approval).
            for name in re.findall(r"`([^`]+)`", cell) or ([cell] if cell else []):
                path = to_posix(name.strip())
                if not path:
                    continue
                if not (repo_root / path).exists():
                    notes.append(f"{rel}: R2 row names {path!r} which is not a file in the repo — ignored")
                    continue
                approved.add(path)
    return approved, notes
