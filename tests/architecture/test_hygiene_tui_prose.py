# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Src rules: TUI prose sinks stay localized and Content.from_text disables markup, with pin and proofs."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.architecture._hygiene_core import (
    _TUI_ROOT,
    _literal_string,
    _pins_allowlist,
    _qualified_name,
    _src_sources,
    _tree,
)
from tests.support.ci import CI_LINUX_ONLY

# Platform-independent source analysis: the Linux CI job covers it.
pytestmark = CI_LINUX_ONLY


_TUI_PROSE_SINK_ALLOWLIST: set[tuple[Path, str, str]] = {
    # ThinkingIndicator is dead code — zero construction sites repo-wide (verified 2026-08-09).
    (Path("src/chrys/app/tui/widgets/chat/messages.py"), "Static", "thinking"),
    # API-key format hint — data shape, not prose (settled ruling).
    (Path("src/chrys/app/tui/screens/models/screen.py"), "placeholder", "sk-..."),
    # The command token is the card title in every locale; its surrounding prose is localized.
}


_RICH_MARKUP_TAG_RE = re.compile(r"\[/?[^\[\]]*\]")


_CONTENT_SUBSTITUTION_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


_ASCII_PROSE_RE = re.compile(r"[A-Za-z]{2,}")


def _tui_callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _tui_literal_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            fragment.value
            for fragment in node.values
            if isinstance(fragment, ast.Constant) and isinstance(fragment.value, str)
        )
    return None


def _is_tui_prose_bearing_literal(node: ast.expr, *, strip_substitutions: bool = False) -> bool:
    literal = _tui_literal_text(node)
    if literal is None:
        return False
    # Requiring two consecutive ASCII letters after stripping Rich tags lets
    # glyph-only/empty strings, f-string unit fragments ("0s"/"s)"), and
    # markup-tag letters pass without hiding prose from this file-local
    # guard. Only ``from_markup`` templates additionally strip ``$name``
    # substitution tokens (so prose-free skeletons like "[b]$label[/b]"
    # pass): everywhere else ``$name`` renders literally, so it IS the
    # visible prose.
    visible_literal = _RICH_MARKUP_TAG_RE.sub("", literal)
    if strip_substitutions:
        visible_literal = _CONTENT_SUBSTITUTION_RE.sub("", visible_literal)
    return _ASCII_PROSE_RE.search(visible_literal) is not None


def _tui_prose_allowlist_entry(path: Path, sink_tag: str, value: ast.expr) -> tuple[Path, str, str] | None:
    literal = _literal_string(value)
    if literal is None:
        return None
    entry = (path, sink_tag, literal)
    return entry if entry in _TUI_PROSE_SINK_ALLOWLIST else None


def _tui_notify_prose_sites(tree: ast.Module) -> Iterator[tuple[ast.AST, str, str, ast.expr]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _tui_callee_name(node) != "notify":
            continue
        if node.args:
            yield node, "notify", "message", node.args[0]
        for keyword in node.keywords:
            if keyword.arg in {"message", "title"}:
                yield node, "notify", keyword.arg, keyword.value


def _tui_border_title_prose_sites(tree: ast.Module) -> Iterator[tuple[ast.AST, str, str, ast.expr]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr in {"border_title", "border_subtitle"}:
                yield node, target.attr, "assignment", node.value


_TUI_WIDGET_LABEL_CALLEES = {"Label", "Button", "Checkbox", "TabPane", "Static"}


def _tui_widget_label_prose_sites(tree: ast.Module) -> Iterator[tuple[ast.AST, str, str, ast.expr]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee_name = _tui_callee_name(node)
        if callee_name in _TUI_WIDGET_LABEL_CALLEES:
            yield node, callee_name, "first positional argument", node.args[0]


def _tui_placeholder_tooltip_prose_sites(tree: ast.Module) -> Iterator[tuple[ast.AST, str, str, ast.expr]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in {"placeholder", "tooltip"}:
                    yield node, keyword.arg, "keyword argument", keyword.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in {"placeholder", "tooltip"}:
                    yield node, target.attr, "assignment", node.value


def _tui_content_markup_prose_sites(tree: ast.Module) -> Iterator[tuple[ast.AST, str, str, ast.expr]]:
    # Localized text belongs in ``$name`` substitutions (kept literal by
    # Content/Text); the markup template itself must stay prose-free. The
    # template parameter is named ``markup`` on Content and ``text`` on
    # rich.Text, so cover both keyword spellings alongside the positional
    # form.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _tui_callee_name(node) != "from_markup":
            continue
        if node.args:
            yield node, "from_markup", "first positional argument", node.args[0]
        for keyword in node.keywords:
            if keyword.arg in {"markup", "text"}:
                yield node, "from_markup", keyword.arg, keyword.value


_TUI_PROSE_SITE_FINDERS = (
    _tui_notify_prose_sites,
    _tui_border_title_prose_sites,
    _tui_widget_label_prose_sites,
    _tui_placeholder_tooltip_prose_sites,
    _tui_content_markup_prose_sites,
)


def _assert_tui_notify_prose_is_localized(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        for node, sink_tag, field, value in _tui_notify_prose_sites(_tree(path, source)):
            if _is_tui_prose_bearing_literal(value) and _tui_prose_allowlist_entry(path, sink_tag, value) is None:
                violations.append(f"{path}:{node.lineno}: localize raw prose in notify {field}")
    assert violations == [], "\n".join(violations)


def _assert_tui_border_titles_are_localized(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        for node, sink_tag, _field, value in _tui_border_title_prose_sites(_tree(path, source)):
            if _is_tui_prose_bearing_literal(value) and _tui_prose_allowlist_entry(path, sink_tag, value) is None:
                violations.append(f"{path}:{node.lineno}: localize raw prose assigned to {sink_tag}")
    assert violations == [], "\n".join(violations)


def _assert_tui_widget_label_prose_is_localized(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        for node, sink_tag, field, value in _tui_widget_label_prose_sites(_tree(path, source)):
            if _is_tui_prose_bearing_literal(value) and _tui_prose_allowlist_entry(path, sink_tag, value) is None:
                violations.append(f"{path}:{node.lineno}: localize raw prose in {sink_tag} {field}")
    assert violations == [], "\n".join(violations)


def _assert_tui_placeholder_tooltip_prose_is_localized(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        for node, sink_tag, field, value in _tui_placeholder_tooltip_prose_sites(_tree(path, source)):
            if _is_tui_prose_bearing_literal(value) and _tui_prose_allowlist_entry(path, sink_tag, value) is None:
                violations.append(f"{path}:{node.lineno}: localize raw prose in {sink_tag} {field}")
    assert violations == [], "\n".join(violations)


def _assert_tui_content_markup_prose_is_localized(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        for node, sink_tag, field, value in _tui_content_markup_prose_sites(_tree(path, source)):
            if (
                _is_tui_prose_bearing_literal(value, strip_substitutions=True)
                and _tui_prose_allowlist_entry(path, sink_tag, value) is None
            ):
                violations.append(f"{path}:{node.lineno}: localize raw prose in {sink_tag} {field}")
    assert violations == [], "\n".join(violations)


def _textual_content_references(tree: ast.Module) -> set[str]:
    """Return import spellings that name :class:`textual.content.Content`."""
    references: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "textual.content":
            references.update(alias.asname or alias.name for alias in node.names if alias.name == "Content")
        elif isinstance(node, ast.ImportFrom) and node.module == "textual":
            references.update(
                f"{alias.asname or alias.name}.Content" for alias in node.names if alias.name == "content"
            )
        elif isinstance(node, ast.Import):
            references.update(
                f"{alias.asname}.Content" if alias.asname else "textual.content.Content"
                for alias in node.names
                if alias.name == "textual.content"
            )
    return references


def _assert_tui_content_from_text_disables_markup(sources: Mapping[Path, str]) -> None:
    """Keep dynamic display strings out of Textual's implicit markup parser."""
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        tree = _tree(path, source)
        callees = {f"{reference}.from_text" for reference in _textual_content_references(tree)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _qualified_name(node.func) not in callees:
                continue
            markup = next((keyword.value for keyword in node.keywords if keyword.arg == "markup"), None)
            if not (isinstance(markup, ast.Constant) and markup.value is False):
                violations.append(
                    f"{path}:{node.lineno}: Textual Content.from_text must pass markup=False; "
                    "use Content.from_markup for intentional markup"
                )
    assert violations == [], "\n".join(violations)


@_pins_allowlist("_TUI_PROSE_SINK_ALLOWLIST")
def test_tui_prose_sink_allowlist_entries_are_live() -> None:
    sources = _src_sources()
    consumed: set[tuple[Path, str, str]] = set()
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        tree = _tree(path, source)
        for find_sites in _TUI_PROSE_SITE_FINDERS:
            for _node, sink_tag, _field, value in find_sites(tree):
                if not _is_tui_prose_bearing_literal(value, strip_substitutions=sink_tag == "from_markup"):
                    continue
                entry = _tui_prose_allowlist_entry(path, sink_tag, value)
                if entry is not None:
                    consumed.add(entry)

    for rule in (
        _assert_tui_notify_prose_is_localized,
        _assert_tui_border_titles_are_localized,
        _assert_tui_widget_label_prose_is_localized,
        _assert_tui_placeholder_tooltip_prose_is_localized,
    ):
        rule(sources)
    _tree.cache_clear()

    assert consumed == _TUI_PROSE_SINK_ALLOWLIST


@pytest.mark.parametrize(
    "source",
    [
        'self.notify("Connection failed")\n',
        'notify(message="Connection failed")\n',
        'notify(f"Loading {name}")\n',
        'self.notify(MESSAGE, title="Connection failed")\n',
        'self.notify("$Unlocalized")\n',
    ],
    ids=["positional-message", "keyword-message", "formatted-message", "title", "dollar-prose"],
)
def test_tui_notify_prose_guard_rejects_literals(source: str) -> None:
    with pytest.raises(AssertionError, match="localize raw prose"):
        _assert_tui_notify_prose_is_localized({Path("src/chrys/app/tui/screens/bad.py"): source})


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (Path("src/chrys/app/tui/screens/good.py"), 'notify(f"[red]*[/red] {value}")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'notify("✕")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), "notify(render_str(MESSAGE))\n"),
        (Path("src/chrys/app/cli/good.py"), 'notify("Raw English")\n'),
        (Path("src/chrys/app/tui/widgets/chat/messages.py"), 'Static("thinking")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'notify("")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'notify(MESSAGE, severity="warning")\n'),
    ],
    ids=["markup-fstring", "glyph", "render-call", "outside-tui", "allowlisted-site", "empty", "severity"],
)
def test_tui_notify_prose_guard_accepts_nonprose_and_nonsinks(path: Path, source: str) -> None:
    _assert_tui_notify_prose_is_localized({path: source})


@pytest.mark.parametrize(
    "source",
    [
        'Widget.border_title = "Connection details"\n',
        'widget.border_subtitle = f"Loading {name}"\n',
    ],
    ids=["title", "formatted-subtitle"],
)
def test_tui_border_title_prose_guard_rejects_literals(source: str) -> None:
    with pytest.raises(AssertionError, match="localize raw prose"):
        _assert_tui_border_titles_are_localized({Path("src/chrys/app/tui/screens/bad.py"): source})


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (Path("src/chrys/app/tui/screens/good.py"), 'widget.border_title = f"[red]*[/red] {value}"\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'widget.border_subtitle = "✕"\n'),
        (Path("src/chrys/app/tui/screens/good.py"), "widget.border_title = render_str(MESSAGE)\n"),
        (Path("src/chrys/app/cli/good.py"), 'widget.border_title = "Raw English"\n'),
        (Path("src/chrys/app/tui/widgets/chat/messages.py"), 'Static("thinking")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'widget.border_title = ""\n'),
    ],
    ids=["markup-fstring", "glyph", "render-call", "outside-tui", "allowlisted-site", "empty"],
)
def test_tui_border_title_prose_guard_accepts_nonprose_and_nonsinks(path: Path, source: str) -> None:
    _assert_tui_border_titles_are_localized({path: source})


@pytest.mark.parametrize(
    "source",
    [
        'Label("Connection details")\n',
        'ui.Button(f"Loading {name}")\n',
        'Checkbox("Remember choice")\n',
        'TabPane("Session details")\n',
        'Static("Event stream")\n',
    ],
    ids=["label", "formatted-button", "checkbox", "tab-pane", "static"],
)
def test_tui_widget_label_prose_guard_rejects_literals(source: str) -> None:
    with pytest.raises(AssertionError, match="localize raw prose"):
        _assert_tui_widget_label_prose_is_localized({Path("src/chrys/app/tui/screens/bad.py"): source})


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (Path("src/chrys/app/tui/screens/good.py"), 'Label(f"[red]*[/red] {value}")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'Button("✕")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), "Checkbox(render_str(MESSAGE))\n"),
        (Path("src/chrys/app/cli/good.py"), 'Static("Raw English")\n'),
        (Path("src/chrys/app/tui/widgets/chat/messages.py"), 'Static("thinking")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'TabPane("")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'Label(text="Raw English")\n'),
    ],
    ids=[
        "markup-fstring",
        "glyph",
        "render-call",
        "outside-tui",
        "allowlisted-site",
        "empty",
        "keyword-label",
    ],
)
def test_tui_widget_label_prose_guard_accepts_nonprose_and_nonsinks(path: Path, source: str) -> None:
    _assert_tui_widget_label_prose_is_localized({path: source})


@pytest.mark.parametrize(
    "source",
    [
        'Input(placeholder="Enter a name")\n',
        'Button("Details", tooltip="Open details")\n',
        'widget.placeholder = "Enter a name"\n',
        'widget.tooltip = f"Loading {name}"\n',
    ],
    ids=["placeholder-keyword", "tooltip-keyword", "placeholder-assignment", "formatted-tooltip-assignment"],
)
def test_tui_placeholder_tooltip_prose_guard_rejects_literals(source: str) -> None:
    with pytest.raises(AssertionError, match="localize raw prose"):
        _assert_tui_placeholder_tooltip_prose_is_localized({Path("src/chrys/app/tui/screens/bad.py"): source})


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (Path("src/chrys/app/tui/screens/good.py"), 'Input(placeholder=f"[red]*[/red] {value}")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'widget.tooltip = "✕"\n'),
        (Path("src/chrys/app/tui/screens/good.py"), "Input(tooltip=render_str(MESSAGE))\n"),
        (Path("src/chrys/app/cli/good.py"), 'Input(placeholder="Raw English")\n'),
        (Path("src/chrys/app/tui/screens/models/screen.py"), 'Input(placeholder="sk-...")\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'widget.placeholder = ""\n'),
        (Path("src/chrys/app/tui/screens/good.py"), 'placeholder = "Raw English"\n'),
    ],
    ids=[
        "markup-fstring",
        "glyph",
        "render-call",
        "outside-tui",
        "allowlisted-site",
        "empty",
        "plain-name-assignment",
    ],
)
def test_tui_placeholder_tooltip_prose_guard_accepts_nonprose_and_nonsinks(path: Path, source: str) -> None:
    _assert_tui_placeholder_tooltip_prose_is_localized({path: source})


@pytest.mark.parametrize(
    "source",
    [
        'Content.from_markup("[b]Compacting conversation...[/b]")\n',
        'Content.from_markup(f"[$error]✗ Compaction failed[/]{elapsed}")\n',
        'Text.from_markup("[red]Connection failed[/red]")\n',
        'Content.from_markup(markup="[b]Connection failed[/b]")\n',
        'Text.from_markup(text="Connection failed")\n',
    ],
    ids=["constant-prose", "fstring-prose", "text-prose", "markup-keyword", "text-keyword"],
)
def test_tui_content_markup_prose_guard_rejects_literals(source: str) -> None:
    with pytest.raises(AssertionError, match="localize raw prose"):
        _assert_tui_content_markup_prose_is_localized({Path("src/chrys/app/tui/widgets/bad.py"): source})


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (Path("src/chrys/app/tui/widgets/good.py"), 'Content.from_markup("[b]$label[/b]", label=label)\n'),
        (
            Path("src/chrys/app/tui/widgets/good.py"),
            'Content.from_markup(f"[b]{prefix}$label[/b] [$success]✓[/]{elapsed}", label=label)\n',
        ),
        (
            Path("src/chrys/app/tui/widgets/good.py"),
            'Content.from_markup("[$text-success][b]+$additions[/b][/]", additions=additions)\n',
        ),
        (Path("src/chrys/app/cli/good.py"), 'Content.from_markup("[b]Raw English[/b]")\n'),
        (Path("src/chrys/app/tui/widgets/good.py"), "Content.from_markup(template)\n"),
        (Path("src/chrys/app/tui/widgets/good.py"), 'Content.from_markup("[b]$text[/b]", text=value)\n'),
    ],
    ids=[
        "substitution-skeleton",
        "fstring-skeleton",
        "numeric-skeleton",
        "outside-tui",
        "nonliteral",
        "substitution-keyword-variable",
    ],
)
def test_tui_content_markup_prose_guard_accepts_skeletons_and_nonsinks(path: Path, source: str) -> None:
    _assert_tui_content_markup_prose_is_localized({path: source})


@pytest.mark.parametrize(
    "source",
    [
        "from textual.content import Content\nContent.from_text(value)\n",
        "from textual.content import Content as TContent\nTContent.from_text(value, markup=True)\n",
        "import textual.content as tc\ntc.Content.from_text(value, markup=allow_markup)\n",
    ],
    ids=["implicit-markup", "explicit-markup", "dynamic-markup"],
)
def test_tui_content_from_text_guard_rejects_markup_parsing(source: str) -> None:
    with pytest.raises(AssertionError, match="must pass markup=False"):
        _assert_tui_content_from_text_disables_markup({Path("src/chrys/app/tui/widgets/bad.py"): source})


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (
            Path("src/chrys/app/tui/widgets/good.py"),
            "from textual.content import Content\nContent.from_text(value, markup=False)\n",
        ),
        (
            Path("src/chrys/app/tui/widgets/good.py"),
            "from textual.content import Content\nContent.from_markup(template)\n",
        ),
        (
            Path("src/chrys/app/cli/good.py"),
            "from textual.content import Content\nContent.from_text(value)\n",
        ),
    ],
    ids=["literal-text", "intentional-markup", "outside-tui"],
)
def test_tui_content_from_text_guard_accepts_explicit_literal_text_and_nonsinks(path: Path, source: str) -> None:
    _assert_tui_content_from_text_disables_markup({path: source})


def test_tui_prose_sink_allowlist_rejects_literal_drift() -> None:
    source = 'Static("pondering")\n'
    with pytest.raises(AssertionError, match="Static"):
        _assert_tui_widget_label_prose_is_localized({Path("src/chrys/app/tui/widgets/chat/messages.py"): source})
