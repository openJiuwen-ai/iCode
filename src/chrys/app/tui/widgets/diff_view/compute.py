# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Working out a diff: its hunks, and both texts as highlighted lines.

Nothing here touches a widget, so all of it can run off the event loop.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Sequence
from functools import lru_cache

from pygments.lexers import find_lexer_class_for_filename
from textual import highlight
from textual.content import Content, Span

from chrys.app.tui.util.source_text import mark_hidden_format, sanitize_source_text
from chrys.app.tui.widgets.diff_view.palette import ADDED_EMPHASIS, REMOVED_EMPHASIS
from chrys.app.tui.widgets.diff_view.rows import Hunk
from chrys.app.tui.widgets.syntax_theme import NoErrorHighlightTheme

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
"""Where `shown_code` keeps a line break: it shows every other break character as a mark."""
_EMPHASIS_SIMILARITY_CUTOFF = 0.5
"""Two lines less alike than this are different lines, not one line edited. Picking out the few
characters they happen to share would be noise on top of backgrounds that already say as much."""
_EMPHASIS_MAX_LINE_LENGTH = 500
"""Comparing characters takes time quadratic in the length of a line. Minified code is not worth it."""

_COMMON_LANGUAGE_BY_PATH_KEY = {
    ".arkts": "typescript",
    ".bash": "bash",
    ".bat": "batch",
    ".c": "c",
    ".cjs": "javascript",
    ".cfg": "ini",
    ".cmake": "cmake",
    ".cmd": "batch",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".csproj": "xml",
    ".css": "css",
    ".csv": "text",
    ".csx": "csharp",
    ".cxx": "cpp",
    ".d.ts": "typescript",
    ".dart": "dart",
    ".dockerignore": "text",
    ".editorconfig": "ini",
    ".env": "text",
    ".ets": "typescript",
    ".fish": "fish",
    ".frag": "glsl",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".fsproj": "xml",
    ".fsx": "fsharp",
    ".gemspec": "ruby",
    ".gitignore": "text",
    ".gql": "graphql",
    ".go": "go",
    ".gradle": "groovy",
    ".graphql": "graphql",
    ".groovy": "groovy",
    ".h": "c",
    ".hh": "cpp",
    ".htm": "html",
    ".hpp": "cpp",
    ".html": "html",
    ".hxx": "cpp",
    ".ini": "ini",
    ".ipynb": "json",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsonc": "javascript",
    ".jsonl": "json",
    ".jsx": "jsx",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".ksh": "bash",
    ".less": "less",
    ".lua": "lua",
    ".md": "markdown",
    ".mdx": "markdown",
    ".mjs": "javascript",
    ".mm": "objective-c++",
    ".props": "xml",
    ".proto": "protobuf",
    ".ps1": "powershell",
    ".psd1": "powershell",
    ".psm1": "powershell",
    ".php": "php",
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".rst": "restructuredtext",
    ".sass": "sass",
    ".scala": "scala",
    ".scss": "scss",
    ".sh": "bash",
    ".sln": "text",
    ".sql": "sql",
    ".svg": "xml",
    ".svelte": "html",
    ".swift": "swift",
    ".targets": "xml",
    ".tcss": "scss",
    ".tf": "terraform",
    ".tfvars": "terraform",
    ".thrift": "thrift",
    ".tsv": "text",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".txt": "text",
    ".vb": "vb.net",
    ".vbproj": "xml",
    ".vcxproj": "xml",
    ".vert": "glsl",
    ".vue": "vue",
    ".xaml": "xml",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "bash",
    "cargo.toml": "toml",
    "cmakelists.txt": "cmake",
    "dockerfile": "docker",
    "gemfile": "ruby",
    "go.mod": "text",
    "go.sum": "text",
    "makefile": "make",
    "pipfile": "toml",
    "rakefile": "ruby",
    "requirements.txt": "text",
}


def _language_cache_key(path: str) -> str:
    """Return a stable cache key for filename-based lexer detection."""
    filename = os.path.basename(path).lower()
    if not filename:
        return ""
    if filename in _COMMON_LANGUAGE_BY_PATH_KEY:
        return filename
    if filename.startswith("dockerfile."):
        return "dockerfile"
    if filename.startswith("makefile."):
        return "makefile"
    if filename.endswith(".d.ts"):
        return ".d.ts"
    if filename.endswith(".gradle.kts"):
        return ".kts"
    _root, ext = os.path.splitext(filename)
    return ext or filename


@lru_cache(maxsize=256)
def _language_from_path_key(key: str) -> str | None:
    """Resolve a Pygments language alias from a path-derived cache key."""
    if not key:
        return None
    if language := _COMMON_LANGUAGE_BY_PATH_KEY.get(key):
        return language
    sample = f"file{key}" if key.startswith(".") else key
    lexer_class = find_lexer_class_for_filename(sample)
    if lexer_class is None:
        return None
    if lexer_class.aliases:
        return lexer_class.aliases[0]
    return lexer_class.name


def _language_from_path(path: str) -> str | None:
    """Resolve a language from the file path without content guessing."""
    return _language_from_path_key(_language_cache_key(path))


def _guess_diff_language(code: str, path: str) -> str:
    """Fast path language detection for diff rendering.

    Diff inputs almost always come from concrete file paths.  Pygments'
    content-based ``guess_lexer_for_filename`` is much more expensive than
    filename matching and is usually unnecessary here, so prefer a cached
    filename/extension lookup and fall back to Textual's heuristic only for
    unknown paths.
    """
    language = _language_from_path(path)
    if language is not None:
        return language
    return highlight.guess_language(code, path)


def _highlight_lines(code: str, path: str, language: str, *, shown_end: int | None = None) -> list[Content]:
    """``code`` as one highlighted `Content` per line.

    With ``shown_end``, only the lines ahead of it are highlighted and the rest stay plain. The
    highlighted part always starts at the top of the text: a lexer carries state from line to line,
    and a string or a comment that was opened above a hunk decides what the hunk's lines are.
    """
    text_lines = code.splitlines()
    end = len(text_lines) if shown_end is None else min(shown_end, len(text_lines))
    lines: list[Content] = []
    if end:
        shown = "\n".join(text_lines[:end])
        lines = highlight.highlight(shown, language=language, path=path, theme=NoErrorHighlightTheme).split("\n")[:end]
    # Blank lines at the end of what the highlighter is given do not come back from it.
    lines.extend(Content(line) for line in text_lines[len(lines) :])
    return lines


def shown_code(code: str) -> str:
    """``code`` as a diff shows it.

    A diff can show text a model or a remote agent wrote: its control characters
    could restyle or hide lines, and bidi and zero-width characters reorder or hide
    them, so each one shows as a mark of its own. Tabs become spaces up to the next
    stop eight columns apart.
    """
    return mark_hidden_format(sanitize_source_text(code, tab_size=8))


def _compared_lines(code: str) -> list[str]:
    lines = _LINE_BREAK.split(code)
    if lines[-1] == "":
        lines.pop()
    return lines


def compute_hunks(code_before: str, code_after: str) -> list[Hunk]:
    """The changes between two texts, each with up to three unchanged lines around it.

    The texts are compared as given, since `shown_code` shows different characters alike and a
    change between two of them would go missing, in lines that are the lines it shows.
    """
    matcher = difflib.SequenceMatcher(None, _compared_lines(code_before), _compared_lines(code_after))
    return list(matcher.get_grouped_opcodes())


def emphasize_changes(removed: Sequence[Content], added: Sequence[Content]) -> tuple[list[Content], list[Content]]:
    """Mark the characters that differ between each removed line and the added line paired with it.

    A line is compared with its own counterpart only, so a mark never runs from one line into the
    next, and a pair too unlike to be one edited line is left unmarked.
    """
    pairs = [_emphasize_pair(before, after) for before, after in zip(removed, added, strict=True)]
    return [before for before, _ in pairs], [after for _, after in pairs]


def _emphasize_pair(removed: Content, added: Content) -> tuple[Content, Content]:
    if max(len(removed.plain), len(added.plain)) > _EMPHASIS_MAX_LINE_LENGTH:
        return removed, added
    matcher = difflib.SequenceMatcher(difflib.IS_CHARACTER_JUNK, removed.plain, added.plain, autojunk=False)
    # The first two are upper bounds on the third, and far cheaper.
    if any(
        ratio() < _EMPHASIS_SIMILARITY_CUTOFF
        for ratio in (matcher.real_quick_ratio, matcher.quick_ratio, matcher.ratio)
    ):
        return removed, added
    removed_spans: list[Span] = []
    added_spans: list[Span] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"delete", "replace"}:
            removed_spans.append(Span(i1, i2, REMOVED_EMPHASIS))
        if tag in {"insert", "replace"}:
            added_spans.append(Span(j1, j2, ADDED_EMPHASIS))
    return removed.add_spans(removed_spans), added.add_spans(added_spans)


def compute_highlighted_lines(
    code_before: str,
    code_after: str,
    path_before: str,
    path_after: str,
    hunks: Sequence[Hunk],
    *,
    hunks_only: bool = False,
) -> tuple[list[Content], list[Content]]:
    """Both texts as highlighted lines, with the edits inside replaced lines marked.

    With ``hunks_only``, highlighting stops below the last hunk. A diff shows nothing further
    down, and for a small change near the top of a large file that is nearly all of the work.
    """
    last_opcode = hunks[-1][-1] if hunks and hunks_only else None
    before = _highlight_lines(
        code_before,
        path_before,
        _guess_diff_language(code_before, path_before),
        shown_end=None if last_opcode is None else last_opcode[2],
    )
    after = _highlight_lines(
        code_after,
        path_after,
        _guess_diff_language(code_after, path_after),
        shown_end=None if last_opcode is None else last_opcode[4],
    )
    for hunk in hunks:
        for tag, i1, i2, j1, j2 in hunk:
            if tag == "replace":
                # The lines pair off from the top, which is also how a split diff sets them side by side.
                paired = min(i2 - i1, j2 - j1)
                removed, added = emphasize_changes(before[i1 : i1 + paired], after[j1 : j1 + paired])
                before[i1 : i1 + paired] = removed
                after[j1 : j1 + paired] = added
    return before, after
