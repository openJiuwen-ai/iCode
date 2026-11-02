# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the user settings document shape helpers."""

from __future__ import annotations

from typing import Any

from chrys.foundation.config.user_settings import SCHEMA_VERSION, apply_settings_patch, flatten_user_doc

KNOWN = frozenset({"ui.theme", "session.title.auto", "model.profile.active"})


def test_flatten_walks_nested_mappings_into_dotted_keys() -> None:
    doc = {"ui": {"theme": "dark"}, "session": {"title": {"auto": False}}}
    values, unknown = flatten_user_doc(doc, KNOWN)
    assert values == {"ui.theme": "dark", "session.title.auto": False}
    assert unknown == ()


def test_flatten_skips_the_reserved_housekeeping_keys() -> None:
    doc = {"schema_version": 1, "migrations": {"some_component": {"pending_cleanup": True}}, "ui": {"theme": "dark"}}
    values, unknown = flatten_user_doc(doc, KNOWN)
    assert values == {"ui.theme": "dark"}
    assert unknown == ()


def test_flatten_reports_unknown_leaves_without_dropping_known_siblings() -> None:
    doc = {"ui": {"theme": "dark", "them": "typo"}, "stray": 1}
    values, unknown = flatten_user_doc(doc, KNOWN)
    assert values == {"ui.theme": "dark"}
    assert unknown == ("ui.them", "stray")


def test_flatten_treats_a_known_key_as_a_leaf_even_when_it_holds_a_mapping() -> None:
    # The wrong shape is that setting's invalid value, to be rejected with its
    # own warning — not a batch of unknown grandchildren.
    doc = {"ui": {"theme": {"name": "dark"}}}
    values, unknown = flatten_user_doc(doc, KNOWN)
    assert values == {"ui.theme": {"name": "dark"}}
    assert unknown == ()


def test_patch_sets_nested_values_and_stamps_the_schema_version() -> None:
    doc: dict[str, Any] = {}
    result = apply_settings_patch(doc, {"ui.theme": "dark", "session.title.auto": True})
    assert result is doc
    assert doc == {
        "schema_version": SCHEMA_VERSION,
        "ui": {"theme": "dark"},
        "session": {"title": {"auto": True}},
    }


def test_patch_leaves_unnamed_keys_untouched() -> None:
    doc: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "ui": {"theme": "dark", "future_knob": 3}}
    apply_settings_patch(doc, {"ui.theme": "light"})
    assert doc["ui"] == {"theme": "light", "future_knob": 3}


def test_patch_replaces_a_non_mapping_obstacle_on_the_path() -> None:
    doc: dict[str, Any] = {"ui": "oops"}
    apply_settings_patch(doc, {"ui.theme": "dark"})
    assert doc["ui"] == {"theme": "dark"}


def test_patch_removal_prunes_emptied_parents_but_not_occupied_ones() -> None:
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ui": {"theme": "dark"},
        "session": {"title": {"auto": True}, "other": 1},
    }
    apply_settings_patch(doc, {}, remove=("ui.theme", "session.title.auto"))
    assert "ui" not in doc
    assert doc["session"] == {"other": 1}


def test_patch_removal_of_an_absent_key_is_a_no_op() -> None:
    doc: dict[str, Any] = {"ui": {"theme": "dark"}}
    apply_settings_patch(doc, {}, remove=("session.title.auto",))
    assert doc == {"schema_version": SCHEMA_VERSION, "ui": {"theme": "dark"}}


def test_patch_removes_a_rival_literal_spelling_of_the_patched_key() -> None:
    # The flattener reads a literal ``ui.theme`` key as the same setting, and
    # a sorted document flattens it *after* the nested shape — left behind,
    # the stale spelling would win every load over the value just patched.
    doc: dict[str, Any] = {"ui.theme": "stale", "ui": {"theme": "old"}}
    apply_settings_patch(doc, {"ui.theme": "new"})
    assert doc == {"schema_version": SCHEMA_VERSION, "ui": {"theme": "new"}}
    values, unknown = flatten_user_doc(doc, KNOWN)
    assert values == {"ui.theme": "new"}
    assert unknown == ()


def test_patch_removes_a_mixed_depth_spelling_and_prunes_its_emptied_parent() -> None:
    doc: dict[str, Any] = {"session": {"title.auto": True}}
    apply_settings_patch(doc, {"session.title.auto": False})
    assert doc == {"schema_version": SCHEMA_VERSION, "session": {"title": {"auto": False}}}


def test_patch_removes_a_merged_head_spelling() -> None:
    doc: dict[str, Any] = {"session.title": {"auto": True}}
    apply_settings_patch(doc, {"session.title.auto": False})
    assert doc == {"schema_version": SCHEMA_VERSION, "session": {"title": {"auto": False}}}


def test_patch_pruning_keeps_unrelated_siblings_of_a_rival_spelling() -> None:
    doc: dict[str, Any] = {"session.title": {"auto": True, "stray": 1}}
    apply_settings_patch(doc, {"session.title.auto": False})
    assert doc == {
        "schema_version": SCHEMA_VERSION,
        "session.title": {"stray": 1},
        "session": {"title": {"auto": False}},
    }


def test_patch_removal_also_clears_literal_spellings() -> None:
    doc: dict[str, Any] = {"ui.theme": "stale", "ui": {"theme": "old"}}
    apply_settings_patch(doc, {}, remove=("ui.theme",))
    assert doc == {"schema_version": SCHEMA_VERSION}
