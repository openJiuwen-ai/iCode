# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structural invariants of the settings panel layout."""

from __future__ import annotations

from collections import Counter

import pytest

from chrys.app.tui.screens.settings import DEFERRED_KEYS, TAB_IDS, TABS, rendered_keys
from chrys.app.tui.screens.settings.layout import (
    GENERAL_TAB_ID,
    NOTIFICATIONS_TAB_ID,
    HintArgs,
    RowKind,
    rendered_row_specs,
    tab_by_id,
)
from chrys.app.tui.screens.settings.rows import (
    BoolRow,
    InputRow,
    SelectRow,
    row_class_for,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import Kind, specs_by_key
from chrys.foundation.i18n.formatting import parse_placeholder_names


def test_every_persisted_key_is_rendered_or_deliberately_deferred() -> None:
    """The panel is a renderer over the spec metadata: a new persisted field
    must land in a tab or be listed as deferred, never silently missing."""
    persisted = {key for key, spec in specs_by_key(Settings).items() if spec.persist}

    assert rendered_keys() & DEFERRED_KEYS == frozenset()
    assert rendered_keys() | DEFERRED_KEYS == persisted


def test_layout_lists_each_key_once_and_tab_ids_are_unique() -> None:
    keys = Counter(row.key for row in rendered_row_specs())
    assert [key for key, count in keys.items() if count > 1] == []
    assert len(set(TAB_IDS)) == len(TAB_IDS)
    assert TAB_IDS[0] == GENERAL_TAB_ID
    assert TAB_IDS[-1] == NOTIFICATIONS_TAB_ID
    assert tab_by_id("nope") is None
    assert tab_by_id(NOTIFICATIONS_TAB_ID) is not None and tab_by_id(NOTIFICATIONS_TAB_ID).custom_pane is True


def test_the_chat_display_preferences_sit_together_right_after_the_appearance_rows() -> None:
    general = tab_by_id(GENERAL_TAB_ID)
    tools = tab_by_id("tools")

    assert general is not None and tools is not None
    titles = [section.title.fallback for section in general.sections]
    appearance = titles.index("Appearance")
    assert titles[appearance + 1] == "Display preferences"
    assert [row.key for row in general.sections[appearance].rows] == ["ui.theme", "ui.locale"]
    assert [row.key for row in general.sections[appearance + 1].rows] == [
        "ui.chat.show_thinking",
        "ui.chat.tool_groups_expanded",
        "ui.chat.fold_finished_turns",
        "ui.chat.image_graphics",
    ]
    assert "ui.chat.tool_groups_expanded" not in {row.key for section in tools.sections for row in section.rows}


def test_tools_tab_lists_each_tool_category_as_its_own_section() -> None:
    tools = tab_by_id("tools")

    assert tools is not None
    assert [(section.title.fallback, [row.key for row in section.rows]) for section in tools.sections] == [
        ("File search", ["tools.search.respect_gitignore"]),
        ("Images", ["tools.image.send_limit_kb"]),
        (
            "Web search",
            [
                "tools.web_search.mode",
                "tools.web_search.num_results",
                "tools.web_fetch.mode",
                "tools.web_egress.proxy_url",
                "tools.web_egress.proxy_dns",
                "tools.web_search.custom_endpoints",
                "tools.web_search.private_origins",
                "tools.web_search.http_origins",
            ],
        ),
        ("Ask user", ["tools.ask_user.inline", "tools.ask_user.timeout_seconds"]),
        ("Workspace notice", ["workspace.change_notice.enabled"]),
    ]


def test_only_the_notifications_tab_is_a_custom_pane_and_its_keys_are_all_bool() -> None:
    specs = specs_by_key(Settings)
    for tab in TABS:
        if tab.id == NOTIFICATIONS_TAB_ID:
            assert tab.custom_pane is True
            for section in tab.sections:
                for row in section.rows:
                    assert specs[row.key].kind is Kind.BOOL
        else:
            assert tab.custom_pane is False


def test_path_keys_render_only_as_the_session_root_row() -> None:
    specs = specs_by_key(Settings)
    for row in rendered_row_specs():
        if specs[row.key].kind is Kind.PATH:
            assert row.special is RowKind.SESSION_ROOT
            assert row.hint_args is HintArgs.SESSION_ROOT_DEFAULT
        else:
            assert row.special is not RowKind.SESSION_ROOT
    session_root = next(row for row in rendered_row_specs() if row.special is RowKind.SESSION_ROOT)
    with pytest.raises(ValueError, match="sessions pane"):
        row_class_for(specs[session_root.key], session_root)


def test_row_classes_follow_the_spec_kind_and_layout_variant() -> None:
    specs = specs_by_key(Settings)
    classes = {
        row.key: row_class_for(specs[row.key], row)
        for row in rendered_row_specs()
        if row.special is not RowKind.SESSION_ROOT
    }
    notification_keys = {row.key for row in tab_by_id(NOTIFICATIONS_TAB_ID).sections[0].rows}
    for key, cls in classes.items():
        spec = specs[key]
        row = next(candidate for candidate in rendered_row_specs() if candidate.key == key)
        if spec.kind is Kind.BOOL:
            assert cls is BoolRow
        elif spec.kind is Kind.ENUM or row.suggestions is not None:
            assert cls is SelectRow
        else:
            assert cls is InputRow
    assert classes["agent.default_profile"] is SelectRow
    assert classes["model.role.buddy_model_id"] is SelectRow
    assert classes["ui.theme"] is SelectRow
    assert classes["rollback.snapshots_keep"] is InputRow
    assert notification_keys <= {key for key, cls in classes.items() if cls is BoolRow}


def test_trajectory_verify_commands_is_deliberately_hidden_from_the_panel() -> None:
    assert "trajectory.verify_commands" not in rendered_keys()
    assert "trajectory.verify_commands" in DEFERRED_KEYS


def test_web_tuning_keys_are_deliberately_hidden_from_the_panel() -> None:
    """Web-tool tuning knobs stay config-file-only; the panel keeps the common controls."""
    hidden = {
        "tools.web_search.timeout_seconds",
        "tools.web_fetch.max_tokens",
        "tools.web_fetch.timeout_seconds",
        "tools.web_fetch.private_origins",
        "tools.web_fetch.http_origins",
        "tools.web_fetch.allowed_origins",
        "tools.web_fetch.denied_origins",
    }

    assert hidden <= DEFERRED_KEYS
    assert rendered_keys() & hidden == frozenset()
    assert {"tools.web_search.http_origins", "tools.web_fetch.mode"} <= rendered_keys()


def test_hint_arguments_match_what_the_row_can_bind() -> None:
    """A hint that names ``{default}`` must ask for it, or rendering raises."""
    for row in rendered_row_specs():
        if row.hint is None:
            assert row.hint_args is HintArgs.NONE
            continue
        placeholders = set(parse_placeholder_names(row.hint.fallback))
        if row.hint_args is HintArgs.NONE:
            assert placeholders == set()
        elif row.hint_args is HintArgs.RETRY_DEFAULTS:
            assert placeholders == {"default", "maximum"}
        else:
            assert placeholders == {"default"}
