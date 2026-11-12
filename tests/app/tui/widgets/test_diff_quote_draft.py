# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diff comments are single undo steps and never overwrite a retained draft selection."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button
from textual.widgets.text_area import Selection

from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft


class _App(App):
    def compose(self) -> ComposeResult:
        yield InputBar()
        yield Button("Keep focus", id="other")


@pytest.mark.parametrize(
    "selection",
    [
        Selection.cursor((0, 0)),
        Selection.cursor((0, 2)),
        Selection((0, 0), (0, 2)),
        Selection((0, 4), (0, 2)),
    ],
)
@pytest.mark.parametrize("comment", ["change it", "", "first\nsecond\nthird"])
@pytest.mark.asyncio
async def test_diff_defaults_to_append_without_replacing_selection_or_changing_focus(selection, comment) -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "abcd"
        area.selection = selection
        other = app.query_one("#other", Button)
        other.focus()
        await pilot.pause()
        handle = bar.add_diff_quote("snapshot", comment)
        assert handle is not None
        assert handle is bar.draft().references.get("quote", 1)
        expected = "abcd\n[Quote #1]\n" + (comment + "\n" if comment else "")
        assert area.text == expected
        assert bar.draft().sent_text() == expected.replace(handle.label, "> snapshot\n").strip()
        assert app.screen.focused is other
        assert area.cursor_location == area.document.end
        area.focus()
        await pilot.press("ctrl+z")
        assert area.text == "abcd"
        await pilot.press("ctrl+y")
        assert area.text == expected
        assert area.references.get("quote", 1).content == "snapshot"


@pytest.mark.parametrize(
    ("keys", "position"),
    [
        (("left",), (1, 3)),
        (("home",), (1, 0)),
        (("home", "end"), (1, 4)),
        (("ctrl+left",), (1, 0)),
        (("home", "ctrl+right"), (1, 4)),
        (("up",), (0, 4)),
        (("home", "shift+right"), (1, 1)),
    ],
)
@pytest.mark.asyncio
async def test_diff_respects_user_cursor_keys_and_keeps_consecutive_comments_in_order(keys, position) -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "first line\nlast"
        area.focus()
        await pilot.press(*keys)
        assert area.cursor_location == position
        index = area.document.get_index_from_location(position)
        prefix = area.text[:index] + ("\n" if position[1] else "")
        suffix = area.text[index:]
        for number in (1, 2):
            handle = bar.add_diff_quote(f"snapshot {number}", f"comment {number}")
            assert handle is not None
            prefix += f"{handle.label}\ncomment {number}\n"
            assert bar.value == prefix + suffix
            assert area.document.get_index_from_location(area.cursor_location) == len(prefix)
        await pilot.press("ctrl+z")
        assert bar.value == prefix.removesuffix("[Quote #2]\ncomment 2\n") + suffix


@pytest.mark.asyncio
async def test_mouse_placement_and_typing_preserve_the_chosen_insertion_point() -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "abcd"
        await pilot.click(area, offset=(3, 0))
        assert area.cursor_location == (0, 2)
        await pilot.press("x")
        assert area.cursor_location == (0, 3)
        assert bar.add_diff_quote("snapshot", "fix") is not None
        assert bar.value == "abx\n[Quote #1]\nfix\ncd"


@pytest.mark.parametrize("operation", ["typing", "paste", "undo", "redo", "focus-end", "preview"])
@pytest.mark.asyncio
async def test_nonplacement_cursor_changes_keep_default_append(operation) -> None:
    from textual import events

    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "abcd"
        area.focus()
        if operation == "preview":
            assert bar.add_diff_quote("snapshot", "old") is not None
            await pilot.click(area, offset=(3, 1))
            assert area.cursor_location[0] == 1
        elif operation == "focus-end":
            bar.move_draft_cursor((0, 1))
            bar.focus_input(cursor_at_end=True)
            bar.move_draft_cursor((0, 2))
        else:
            bar.move_draft_cursor((0, 2))
            if operation == "paste":
                area.post_message(events.Paste("pasted"))
                await pilot.pause()
            else:
                await pilot.press("x")
                if operation in {"undo", "redo"}:
                    await pilot.press("ctrl+z")
                if operation == "redo":
                    await pilot.press("ctrl+y")
        before = bar.value
        handle = bar.add_diff_quote("next", "new")
        assert handle is not None
        assert bar.value == before + ("" if before.endswith("\n") else "\n") + handle.label + "\nnew\n"


@pytest.mark.parametrize(
    "operation", ["send", "clear", "leave", "history", "editor", "replace", "shortcut", "load", "erase"]
)
@pytest.mark.asyncio
async def test_whole_draft_changes_forget_cursor_placement(operation) -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "abcd"
        area.focus()
        await pilot.press("home")
        if operation == "send":
            await pilot.press("enter", "ctrl+z")
        elif operation == "clear":
            area.clear()
            await pilot.press("x", "y")
        elif operation == "leave":
            area.leave_draft()
            await pilot.press("ctrl+z")
        elif operation == "history":
            area._history.entries = ["abcd"]
            area._history.storage_loaded = True
            await pilot.press("up")
        elif operation == "editor":
            bar.replace_editor_draft(ChatDraft("abcd"), cursor_location=(0, 1))
        elif operation == "replace":
            area.replace_draft(ChatDraft("abcd"))
        elif operation == "shortcut":
            bar.expand_prompt(ChatDraft("abcd", ((0, 2),)))
        elif operation == "erase":
            await pilot.press("ctrl+a", "backspace", "x", "y")
        else:
            area.load_text("abcd")
        bar.move_draft_cursor((0, 0))
        before = bar.value
        assert bar.add_diff_quote("snapshot", "fix") is not None
        assert bar.value == before + "\n[Quote #1]\nfix\n"


@pytest.mark.asyncio
async def test_default_quote_insertion_still_replaces_selection_and_focuses_input() -> None:
    app = _App()
    async with app.run_test():
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "abcd"
        area.selection = Selection((0, 1), (0, 3))
        app.query_one(Button).focus()
        assert bar.insert_reference("quote", "snapshot") == "[Quote #1]"
        assert bar.value == "a [Quote #1] d"
        assert app.screen.focused is area


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("old", "new"),
        ("", "new"),
        ("old", ""),
        ("  old  ", "  new  "),
        ("old\nsecond\n", "new\nline two\nline three"),
        ("", "first\nsecond"),
        ("old\nsecond", ""),
    ],
)
async def test_bubble_edit_preserves_surrounding_text_and_reference_and_is_one_undo(old, new) -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "before\nafter"
        area.focus()
        await pilot.press("home")
        handle = bar.add_diff_quote("snapshot", old)
        second = bar.add_diff_quote("two", "second")
        assert handle is not None
        assert second is not None
        original = bar.value
        assert bar.probe_diff_comment(handle) == "original"
        assert bar.replace_diff_comment(handle, old, new) == "success"
        edited = f"before\n{handle.label}\n" + (new + "\n" if new else "") + f"{second.label}\nsecond\nafter"
        assert bar.value == edited
        assert bar.draft().references.get("quote", 1) is handle
        area.focus()
        await pilot.press("ctrl+z")
        assert bar.value == original
        assert bar.probe_diff_comment(handle) == "original"
        assert bar.diff_comment(handle) == old
        await pilot.press("ctrl+y")
        assert bar.value == edited
        assert bar.probe_diff_comment(handle) == "original"
        assert bar.diff_comment(handle) == new


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["comment", "duplicate", "label", "equal-object", "locked"])
async def test_changed_or_ambiguous_comment_is_never_overwritten(change) -> None:
    from dataclasses import replace

    app = _App()
    async with app.run_test():
        bar = app.query_one(InputBar)
        handle = bar.add_diff_quote("snapshot", "old")
        assert handle is not None
        draft = bar.draft()
        if change == "comment":
            bar.set_draft(replace(draft, text=draft.text.replace("old", "user edit")))
        elif change == "duplicate":
            bar.set_draft(replace(draft, text=draft.text * 2))
        elif change == "label":
            bar.set_draft(replace(draft, text="old\n"))
        elif change == "equal-object":
            other = replace(handle)
            assert other == handle and other is not handle
            handle = other
        else:
            bar.locked = True
        before = bar.value
        expected = "absent" if change in {"label", "equal-object"} else "original" if change == "locked" else "changed"
        assert bar.probe_diff_comment(handle) == expected
        assert bar.replace_diff_comment(handle, "old", "new") == ("unavailable" if change == "locked" else "changed")
        assert bar.value == before


@pytest.mark.asyncio
async def test_send_clear_and_undo_restore_the_same_reference_handle() -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        handle = bar.add_diff_quote("snapshot", "old")
        assert handle is not None
        area.focus()
        await pilot.press("enter")
        assert bar.value == ""
        assert bar.probe_diff_comment(handle) == "absent"
        await pilot.press("ctrl+z")
        assert bar.probe_diff_comment(handle) == "original"
        assert bar.draft().references.get("quote", 1) is handle


@pytest.mark.parametrize("state", ["locked", "shell", "hidden"])
async def test_add_diff_quote_refuses_unavailable_input_without_mutating_draft(state) -> None:
    app = _App()
    async with app.run_test():
        bar = app.query_one(InputBar)
        bar.value = "keep"
        if state == "locked":
            bar.locked = True
        elif state == "shell":
            bar.shell_mode = True
        else:
            bar.display = False
        before = bar.draft()
        assert bar.add_diff_quote("snapshot", "comment") is None
        assert bar.draft() == before


@pytest.mark.asyncio
async def test_bubble_edit_keeps_the_cursor_the_user_placed() -> None:
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "before\nafter"
        area.focus()
        await pilot.press("home")
        first = bar.add_diff_quote("one", "first")
        second = bar.add_diff_quote("two", "second")
        assert first is not None and second is not None
        assert bar.replace_diff_comment(first, "first", "first, edited") == "success"
        third = bar.add_diff_quote("three", "third")
        assert third is not None
        assert bar.value == (
            f"before\n{first.label}\nfirst, edited\n{second.label}\nsecond\n{third.label}\nthird\nafter"
        )


@pytest.mark.asyncio
async def test_each_undo_and_redo_of_edits_leaves_the_comment_editable() -> None:
    """Undoing or redoing edits made from the Diff keeps the text the next edit replaces."""
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        handle = bar.add_diff_quote("snapshot", "original")
        assert handle is not None
        assert bar.replace_diff_comment(handle, "original", "updated") == "success"
        assert bar.replace_diff_comment(handle, "updated", "again") == "success"
        area.focus()
        for key, comment in [("ctrl+z", "updated"), ("ctrl+z", "original"), ("ctrl+y", "updated")]:
            await pilot.press(key)
            assert bar.value == f"{handle.label}\n{comment}\n"
            assert bar.probe_diff_comment(handle) == "original"
            assert bar.diff_comment(handle) == comment
        assert bar.replace_diff_comment(handle, "original", "stale") == "changed"
        assert bar.replace_diff_comment(handle, "updated", "final") == "success"
        assert bar.value == f"{handle.label}\nfinal\n"


@pytest.mark.asyncio
async def test_editing_after_an_undo_keeps_text_the_user_typed_after_the_comment() -> None:
    """The edit replaces only the comment undo restored, never lines the user typed that a later version had."""
    app = _App()
    async with app.run_test() as pilot:
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        handle = bar.add_diff_quote("snapshot", "a")
        assert handle is not None
        assert bar.replace_diff_comment(handle, "a", "a\nb") == "success"
        area.focus()
        await pilot.press("ctrl+z")
        assert bar.value == f"{handle.label}\na\n"
        area.move_cursor(area.document.end)
        await pilot.press("b", "ctrl+j")  # Enter would send the draft.
        assert bar.value == f"{handle.label}\na\nb\n"
        assert bar.diff_comment(handle) == "a"
        assert bar.probe_diff_comment(handle) == "original"
        assert bar.replace_diff_comment(handle, "a", "c") == "success"
        assert bar.value == f"{handle.label}\nc\nb\n"
