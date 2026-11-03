# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for InputBar instance history: up/down browsing, persisted record loading, draft restore, and history persistence on submit."""

from __future__ import annotations

import threading

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button

from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from tests.support.tui_helpers import WidgetApp
from tests.support.waiting import wait_for


class _SubmitRecordingApp(App[None]):
    """Input bar host that records submitted text and optionally echoes it into instance history."""

    def __init__(self, *, history_session_id: str | None = None) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self._history_session_id = history_session_id

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event.text)
        if self._history_session_id is not None:
            self.query_one(InputBar).add_to_history(event.text, session_id=self._history_session_id)


async def test_input_bar_up_down_uses_instance_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal arrows should browse prompts submitted in the current TUI instance."""

    def fake_append_history(
        text: str,
        *,
        session_id: str | None = None,
        instance_id: str | None = None,
        cwd: str | None = None,
    ) -> None:
        del text, session_id, instance_id, cwd

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.input_bar.append_history", fake_append_history)
    load_calls: list[str | None] = []

    def fake_load_history(*, max_entries: int, instance_id: str | None = None) -> list[str]:
        del max_entries
        load_calls.append(instance_id)
        return ["instance old"]

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.input_bar.load_history", fake_load_history)

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        ib.add_to_history("instance old", session_id="session-1")
        ib.add_to_history("instance new", session_id="session-2")
        ib.value = "draft"
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "instance new"
        assert load_calls == []

        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "instance old"

        await pilot.press("down")
        await pilot.pause()
        assert ib.value == "instance new"

        await pilot.press("down")
        await pilot.pause()
        assert ib.value == "draft"


async def test_input_bar_enter_submit_keeps_prior_instance_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyboard Enter submits should reset browsing state without clearing instance entries."""

    monkeypatch.setattr(
        "chrys.app.tui.widgets.chrome.input_bar.append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )

    app = _SubmitRecordingApp(history_session_id="session-1")
    async with app.run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")

        ib.value = "first"
        await pilot.press("enter")
        await pilot.pause()
        ib.value = "second"
        await pilot.press("enter")
        await pilot.pause()

        assert app.submitted == ["first", "second"]

        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "second"
        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "first"


async def test_instance_history_loads_persisted_records_for_this_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instance scope should read persisted prompt history filtered by the TUI instance id.

    Up must ask storage for the current instance id (never the unfiltered global
    history), load off the event loop, and surface only that instance's records.
    """
    loop_thread = threading.current_thread()

    def fake_token_hex(nbytes: int) -> str:
        assert nbytes == 8
        return "instance-a"

    monkeypatch.setattr(
        "chrys.app.tui.widgets.chrome.input_bar.token_hex",
        fake_token_hex,
        raising=False,
    )

    calls: list[str | None] = []
    load_threads: list[threading.Thread] = []

    def fake_load_history(
        max_entries: int = 200,
        *,
        instance_id: str | None = None,
    ) -> list[str]:
        del max_entries
        calls.append(instance_id)
        load_threads.append(threading.current_thread())
        if instance_id == "instance-a":
            return ["mine old", "mine new"]
        return ["other"]

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.input_bar.load_history", fake_load_history)

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        ib.value = "draft"
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()

        assert ib.value == "mine new"
        assert calls == ["instance-a"]
        assert load_threads[0] is not loop_thread


async def test_empty_instance_history_is_loaded_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty persisted result should not trigger another disk scan on every Up key."""
    load_threads: list[threading.Thread] = []

    def fake_load_history(
        max_entries: int = 200,
        *,
        instance_id: str | None = None,
    ) -> list[str]:
        del max_entries, instance_id
        load_threads.append(threading.current_thread())
        return []

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.input_bar.load_history", fake_load_history)

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()

        assert len(load_threads) == 1


async def test_input_bar_plain_page_keys_do_not_browse_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain PageUp/PageDown should keep the TextArea default cursor-page behavior."""

    def fake_load_history(
        max_entries: int = 200,
        *,
        instance_id: str | None = None,
    ) -> list[str]:
        del max_entries, instance_id
        return ["old", "new"]

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.input_bar.load_history", fake_load_history)

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        ib.value = "draft"
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("pageup")
        await pilot.pause()
        assert ib.value == "draft"

        await pilot.press("pagedown")
        await pilot.pause()
        assert ib.value == "draft"


async def test_instance_history_keeps_latest_1000_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chrys.app.tui.widgets.chrome.input_bar.append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)

        for i in range(1001):
            ib.add_to_history(f"i-{i}")

        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        assert text_area._history.entries[0] == "i-1"
        assert len(text_area._history.entries) == 1000

        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "i-1000"


async def test_down_key_restores_draft_after_instance_browse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Down should walk forward through instance history and then restore the draft."""

    monkeypatch.setattr(
        "chrys.app.tui.widgets.chrome.input_bar.append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        ib.add_to_history("i-old", session_id="s1")
        ib.add_to_history("i-new", session_id="s1")
        ib.value = "draft"
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        # Browse instance to i-old
        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "i-new"
        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "i-old"

        await pilot.press("down")
        await pilot.pause()
        assert ib.value == "i-new"

        await pilot.press("down")
        await pilot.pause()
        assert ib.value == "draft"


async def test_send_button_submit_resets_history_browse_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting with the button should not let a later Down restore the pre-browse draft."""

    monkeypatch.setattr(
        "chrys.app.tui.widgets.chrome.input_bar.append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )
    app = _SubmitRecordingApp()
    async with app.run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        ib.add_to_history("old")
        ib.value = "draft"
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("up")
        await pilot.pause()
        assert ib.value == "old"

        ib.query_one("#send-btn", Button).press()
        await pilot.pause()
        assert app.submitted == ["old"]
        assert ib.value == ""

        await pilot.press("down")
        await pilot.pause()
        assert ib.value == ""


async def test_input_bar_add_to_history_persists_session_and_instance_id(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_thread = threading.current_thread()
    calls: list[dict[str, str | threading.Thread | None]] = []

    def fake_append_history(
        text: str,
        *,
        session_id: str | None = None,
        instance_id: str | None = None,
        cwd: str | None = None,
    ) -> None:
        calls.append(
            {
                "text": text,
                "session_id": session_id,
                "instance_id": instance_id,
                "cwd": cwd,
                "thread": threading.current_thread(),
            }
        )

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.input_bar.append_history", fake_append_history)
    monkeypatch.setattr("chrys.app.tui.widgets.chrome.draft_text_area.safe_getcwd", lambda: "/workspace")

    async with WidgetApp(InputBar).run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        text_area = ib.query_one("#chat-input", _ChatTextArea)
        ib.set_paste_cwd("/session-workspace")

        ib.add_to_history("hello", session_id="session-1")
        await text_area._history_writer.close(timeout_seconds=None)

    assert len(calls) == 1
    assert calls[0]["text"] == "hello"
    assert calls[0]["session_id"] == "session-1"
    assert isinstance(calls[0]["instance_id"], str)
    assert len(calls[0]["instance_id"]) == 16
    bytes.fromhex(calls[0]["instance_id"])
    assert calls[0]["cwd"] == "/session-workspace"
    assert calls[0]["thread"] is not loop_thread
