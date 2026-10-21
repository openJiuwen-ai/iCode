# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Src rule: TUI binding descriptions use the canonical localized helper, with its pin and proofs."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
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


# --- TUI binding-display guard (scans src/chrys, not tests) ----------------


_TUI_BINDING_DISPLAY_MODULE = _TUI_ROOT / "binding_display.py"


_TUI_BINDING_CONSTRUCTION_ALLOWLIST = {
    # Theme controls: non-displayed focus, movement and transaction shortcuts.
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_ResetButton",
        "Binding",
        "left",
        "focus_swatch",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_ResetButton",
        "Binding",
        "up",
        "navigate_row(-1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_ResetButton",
        "Binding",
        "down",
        "navigate_row(1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_ColorSwatchButton",
        "Binding",
        "up",
        "move_focus(-1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_ColorSwatchButton",
        "Binding",
        "down",
        "move_focus(1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_ColorSwatchButton",
        "Binding",
        "right",
        "focus_reset",
        None,
        False,
    ),
    (Path("src/chrys/app/tui/screens/themes/palette.py"), "_AnsiTokenPicker", "Binding", "up", "move(-1)", None, False),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_AnsiTokenPicker",
        "Binding",
        "down",
        "move(1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_Xterm256PalettePicker",
        "Binding",
        "left",
        "move(-1, 0)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_Xterm256PalettePicker",
        "Binding",
        "right",
        "move(1, 0)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_Xterm256PalettePicker",
        "Binding",
        "up",
        "move(0, -1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_Xterm256PalettePicker",
        "Binding",
        "down",
        "move(0, 1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/palette.py"),
        "_Xterm256PalettePicker",
        "Binding",
        "space",
        "toggle_transparent",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/editor.py"),
        "ResettableThemeEditor",
        "Binding",
        "ctrl+z",
        "undo",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/editor.py"),
        "ResettableThemeEditor",
        "Binding",
        "ctrl+y",
        "redo",
        None,
        False,
    ),
    (Path("src/chrys/app/tui/screens/themes/dialogs.py"), "_PickerModal", "Binding", "escape", "close", None, False),
    (
        Path("src/chrys/app/tui/screens/themes/dialogs.py"),
        "_PickerModal",
        "Binding",
        "ctrl+enter",
        "accept",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/dialogs.py"),
        "_PickerModal",
        "Binding",
        "ctrl+z",
        "cancel_and_undo",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/themes/dialogs.py"),
        "_PickerModal",
        "Binding",
        "ctrl+y",
        "cancel_and_redo",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/agent_load.py"),
        "AgentLoadDialog",
        "Binding",
        "escape",
        "dismiss_if_allowed",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/approval/dialog.py"),
        "ApprovalDialog",
        "Binding",
        "escape",
        "noop",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/approval/dialog.py"),
        "ApprovalDialog",
        "Binding",
        "left",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/approval/dialog.py"),
        "ApprovalDialog",
        "Binding",
        "n,N",
        "decline",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/approval/dialog.py"),
        "ApprovalDialog",
        "Binding",
        "right",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/approval/dialog.py"),
        "ApprovalDialog",
        "Binding",
        "y,Y",
        "approve",
        None,
        False,
    ),
    (Path("src/chrys/app/tui/screens/dialogs/ask_user.py"), "AskUserDialog", "Binding", "escape", "noop", None, False),
    (
        Path("src/chrys/app/tui/screens/dialogs/confirm.py"),
        "ConfirmDialog",
        "Binding",
        "left",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/confirm.py"),
        "ConfirmDialog",
        "Binding",
        "right",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/connection_test.py"),
        "ConnectionTestDialog",
        "Binding",
        "escape",
        "dismiss_if_allowed",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/fork_session.py"),
        "ForkSessionDialog",
        "Binding",
        "escape",
        "dismiss_after_result",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/fork_session.py"),
        "ForkSessionDialog",
        "Binding",
        "left",
        "focus_previous",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/fork_session.py"),
        "ForkSessionDialog",
        "Binding",
        "right",
        "focus_next",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/image_compression.py"),
        "ImageCompressionDialog",
        "Binding",
        "escape",
        "ignore_escape",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/quote_manager.py"),
        "QuoteManagerDialog",
        "Binding",
        "c",
        "clear_all",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/vision_unsupported.py"),
        "VisionUnsupportedDialog",
        "Binding",
        "escape",
        "dismiss_dialog",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/ask_user_prompt.py"),
        "AskUserPrompt",
        "Binding",
        "ctrl+pagedown",
        "next_question",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/ask_user_prompt.py"),
        "AskUserPrompt",
        "Binding",
        "ctrl+pageup",
        "previous_question",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/vision_unsupported.py"),
        "VisionUnsupportedDialog",
        "Binding",
        "left",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/vision_unsupported.py"),
        "VisionUnsupportedDialog",
        "Binding",
        "right",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/prompt_cache.py"),
        "PromptCacheDialog",
        "Binding",
        "left",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/dialogs/prompt_cache.py"),
        "PromptCacheDialog",
        "Binding",
        "right",
        "switch_focus",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/diff/rollback_modal.py"),
        "RollbackProgressModal",
        "Binding",
        "escape",
        "noop",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/screens/main/screen.py"),
        "MainScreen",
        "Binding",
        "ctrl+r",
        "prompt_history",
        None,
        False,
    ),
    (Path("src/chrys/app/tui/screens/main/screen.py"), "MainScreen", "Binding", "escape", "escape", None, False),
    # Focus-local color-plane navigation has no display prose. Pin every key,
    # action and show=False site so adding a caption/tooltip requires localization.
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "left",
        "step(-1, 0, 1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "right",
        "step(1, 0, 1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "up",
        "step(0, -1, 1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "down",
        "step(0, 1, 1)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "shift+left",
        "step(-1, 0, 10)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "shift+right",
        "step(1, 0, 10)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "shift+up",
        "step(0, -1, 10)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "shift+down",
        "step(0, 1, 10)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "home",
        "edge(0)",
        None,
        False,
    ),
    (
        Path("src/chrys/app/tui/widgets/color_picker/controls.py"),
        "ColorPlane",
        "Binding",
        "end",
        "edge(1)",
        None,
        False,
    ),
}


def _binding_owner(node: ast.AST, parents: Mapping[int, ast.AST]) -> str:
    owners: list[str] = []
    parent = parents.get(id(node))
    while parent is not None:
        if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owners.append(parent.name)
        parent = parents.get(id(parent))
    return ".".join(reversed(owners)) or "<module>"


def _binding_constructor_references(tree: ast.Module) -> set[str]:
    references = {"Binding"}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "textual.binding":
            continue
        references.update(alias.asname or alias.name for alias in node.names if alias.name == "Binding")
    return references


def _display_field_literal(node: ast.expr) -> str:
    # A non-literal display argument cannot match any allowlist entry, so a
    # site that turns dynamic always re-trips the guard for a human decision.
    literal = _literal_string(node)
    return literal if literal is not None else "<dynamic>"


def _binding_site(
    path: Path,
    owner: str,
    kind: str,
    arguments: list[ast.expr],
    keywords: Sequence[ast.keyword] = (),
) -> tuple[Path, str, str, str | None, str | None, str | None, bool | str]:
    key = _literal_string(arguments[0]) if arguments else None
    action = _literal_string(arguments[1]) if len(arguments) > 1 else None
    # The display-relevant fields are part of the site identity: an
    # allowlisted invisible binding that gains a description or flips
    # ``show`` becomes a NEW site and must be re-justified or migrated.
    description = _display_field_literal(arguments[2]) if len(arguments) > 2 else None
    show: bool | str = True
    if len(arguments) > 3:
        positional_show = arguments[3]
        show = (
            positional_show.value
            if isinstance(positional_show, ast.Constant) and isinstance(positional_show.value, bool)
            else "<dynamic>"
        )
    for item in keywords:
        if item.arg == "description" and description is None:
            description = _display_field_literal(item.value)
        elif item.arg == "show":
            value = item.value
            show = value.value if isinstance(value, ast.Constant) and isinstance(value.value, bool) else "<dynamic>"
    return path, owner, kind, key, action, description, show


def _bindings_assignment_value(node: ast.AST) -> ast.expr | None:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "BINDINGS":
        return node.value
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "BINDINGS" for target in node.targets
    ):
        return node.value
    return None


def _has_display_bind_description(node: ast.Call) -> bool:
    positional = node.args[2] if len(node.args) > 2 else None
    keyword = next((item.value for item in node.keywords if item.arg == "description"), None)
    candidates = [candidate for candidate in (positional, keyword) if candidate is not None]
    return any(not (isinstance(candidate, ast.Constant) and candidate.value == "") for candidate in candidates)


def _assert_tui_binding_display_construction_is_canonical(sources: Mapping[Path, str]) -> None:
    """Keep displayed Textual bindings on the registry-populating helper."""
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        tree = _tree(path, source)
        parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        binding_references = _binding_constructor_references(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                _qualified_name(node.func) in binding_references
                or _qualified_name(node.func).rsplit(".", maxsplit=1)[-1] == "Binding"
            ):
                owner = _binding_owner(node, parents)
                if path == _TUI_BINDING_DISPLAY_MODULE and owner == "localized_binding":
                    continue
                site = _binding_site(path, owner, "Binding", node.args, node.keywords)
                if site not in _TUI_BINDING_CONSTRUCTION_ALLOWLIST:
                    violations.append(
                        f"{path}:{node.lineno}: construct displayed bindings with localized_binding; "
                        "direct Binding(...) requires a site-specific invisible-binding allowlist entry"
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "bind"
                and _has_display_bind_description(node)
            ):
                violations.append(
                    f"{path}:{node.lineno}: App.bind/BindingsMap.bind display descriptions must use localized_binding"
                )

            value = _bindings_assignment_value(node)
            if not isinstance(value, (ast.List, ast.Tuple)):
                continue
            owner = _binding_owner(node, parents)
            for item in value.elts:
                if not isinstance(item, ast.Tuple) or len(item.elts) not in {2, 3}:
                    continue
                site = _binding_site(path, owner, "tuple", item.elts)
                if site not in _TUI_BINDING_CONSTRUCTION_ALLOWLIST:
                    violations.append(
                        f"{path}:{item.lineno}: BINDINGS tuple shorthand must use localized_binding; "
                        "undisplayed tuples require a site-specific allowlist entry"
                    )
    assert violations == [], "\n".join(violations)


@_pins_allowlist("_TUI_BINDING_CONSTRUCTION_ALLOWLIST")
def test_tui_binding_display_guard_allowlist_entries_are_live_and_unambiguous() -> None:
    observed: list[tuple[Path, str, str, str | None, str | None, str | None, bool | str]] = []
    for path, source in _src_sources().items():
        if not path.is_relative_to(_TUI_ROOT):
            continue
        tree = _tree(path, source)
        parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        binding_references = _binding_constructor_references(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                _qualified_name(node.func) in binding_references
                or _qualified_name(node.func).rsplit(".", maxsplit=1)[-1] == "Binding"
            ):
                owner = _binding_owner(node, parents)
                if path != _TUI_BINDING_DISPLAY_MODULE or owner != "localized_binding":
                    observed.append(_binding_site(path, owner, "Binding", node.args, node.keywords))
            value = _bindings_assignment_value(node)
            if not isinstance(value, (ast.List, ast.Tuple)):
                continue
            owner = _binding_owner(node, parents)
            observed.extend(
                _binding_site(path, owner, "tuple", item.elts)
                for item in value.elts
                if isinstance(item, ast.Tuple) and len(item.elts) in {2, 3}
            )

    assert len(_TUI_BINDING_CONSTRUCTION_ALLOWLIST) == 54
    assert len(observed) == len(set(observed))
    assert set(observed) == _TUI_BINDING_CONSTRUCTION_ALLOWLIST


@pytest.mark.parametrize(
    "source",
    [
        "from textual.binding import Binding\nBINDINGS = [Binding('x', 'do_thing', 'Do thing')]\n",
        "class Example:\n    BINDINGS = [('x', 'do_thing', 'Do thing')]\n",
        "app.bind('x', 'do_thing', description='Do thing')\n",
    ],
    ids=["direct-binding", "tuple-shorthand", "display-bind-call"],
)
def test_tui_binding_display_guard_rejects_noncanonical_shapes(source: str) -> None:
    with pytest.raises(AssertionError, match="localized_binding"):
        _assert_tui_binding_display_construction_is_canonical({Path("src/chrys/app/tui/screens/bad.py"): source})


def test_tui_binding_display_guard_accepts_localized_binding() -> None:
    source = (
        "from chrys.app.tui.binding_display import localized_binding\n"
        "BINDINGS = [localized_binding('x', 'do_thing', DEFINITION)]\n"
    )

    _assert_tui_binding_display_construction_is_canonical({Path("src/chrys/app/tui/screens/good.py"): source})


def test_tui_binding_display_guard_accepts_allowlisted_invisible_site_verbatim() -> None:
    source = (
        "from textual.binding import Binding\n"
        "class MainScreen:\n"
        "    BINDINGS = [Binding('ctrl+r', 'prompt_history', show=False, priority=True)]\n"
    )

    _assert_tui_binding_display_construction_is_canonical({Path("src/chrys/app/tui/screens/main/screen.py"): source})


@pytest.mark.parametrize(
    "binding_source",
    [
        "Binding('ctrl+r', 'prompt_history', show=True, priority=True)",
        "Binding('ctrl+r', 'prompt_history', 'NOW VISIBLE', show=False, priority=True)",
        "Binding('ctrl+r', 'prompt_history', show=SOME_FLAG, priority=True)",
    ],
    ids=["show-flip", "description-change", "dynamic-show"],
)
def test_tui_binding_display_guard_rejects_display_field_drift_at_allowlisted_site(
    binding_source: str,
) -> None:
    source = f"from textual.binding import Binding\nclass MainScreen:\n    BINDINGS = [{binding_source}]\n"

    with pytest.raises(AssertionError, match="localized_binding"):
        _assert_tui_binding_display_construction_is_canonical(
            {Path("src/chrys/app/tui/screens/main/screen.py"): source}
        )
