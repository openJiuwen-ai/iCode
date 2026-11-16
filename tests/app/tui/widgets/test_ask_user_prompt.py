# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interaction and layout tests for the shared multi-question prompt."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.content import Content
from textual.geometry import Region, Size
from textual.widgets import Button, Static, TabPane

from chrys.app.tui.widgets import AskUserPrompt, AskUserSubmitted, AskUserTabbedContent, PromptDraft
from chrys.app.tui.widgets import ask_user_prompt as ask_user_prompt_module
from chrys.app.tui.widgets.ask_user_controls import (
    ASK_USER_DESCRIPTION_GLYPH,
    AskUserDraftChanged,
    AskUserOptions,
    _AskUserTextArea,
)
from chrys.app.tui.widgets.ask_user_prompt import AskUserActiveQuestionChanged, AskUserReviewPane, ask_user_pane_id
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserOption, AskUserQuestion
from tests.support.tui_helpers import rich_plain, rich_segment_lines
from tests.support.waiting import wait_for, wait_until


def _questions() -> tuple[AskUserQuestion, ...]:
    return (
        AskUserQuestion(
            "Choose [bold]scope[/]?",
            "范围[一]",
            (AskUserOption("Backend", "服务层[/]"), AskUserOption("TUI", "界面层")),
        ),
        AskUserQuestion(
            "Which targets?",
            "Targets",
            (AskUserOption("macOS"), AskUserOption("Windows"), AskUserOption("Linux")),
            multi_select=True,
        ),
        AskUserQuestion("Rollout plan?", "Rollout"),
    )


class _PromptApp(App):
    CSS = "AskUserPrompt { width: 80; height: 28; }"

    def __init__(self, questions: tuple[AskUserQuestion, ...], draft: PromptDraft | None = None) -> None:
        super().__init__()
        self.questions = questions
        self.draft = draft
        self.submissions: list[tuple[AskUserAnswer, ...]] = []

    def compose(self) -> ComposeResult:
        yield AskUserPrompt(
            "request",
            self.questions,
            inline=False,
            allow_inline=True,
            draft=self.draft,
        )

    def on_ask_user_submitted(self, event: AskUserSubmitted) -> None:
        event.stop()
        self.submissions.append(event.answers)


def _options(app: App, question_index: int) -> AskUserOptions:
    return app.query_one(f"#askuser-q{question_index}-options", AskUserOptions)


def _toggle(app: App, question_index: int, option_index: int) -> None:
    options = _options(app, question_index)
    options.toggle(options.get_option_at_index(option_index))


def _tab(app: App, index: int, question_count: int):
    return app.query_one(AskUserTabbedContent).get_tab(ask_user_pane_id(index, question_count))


@pytest.mark.asyncio
async def test_tab_switch_preserves_drafts_and_ctrl_page_navigation_from_textarea() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        input_area = pilot.app.query_one(_AskUserTextArea)
        await wait_for(lambda: _options(pilot.app, 0).has_focus, pilot=pilot)
        input_area.focus()
        await wait_for(lambda: input_area.has_focus, pilot=pilot)
        input_area.insert("first draft")
        await pilot.pause()
        await pilot.press("ctrl+pagedown")
        await wait_for(lambda: _options(pilot.app, 1).has_focus, pilot=pilot)
        assert prompt.active_index == 1
        input_area.focus()
        await wait_for(lambda: input_area.has_focus, pilot=pilot)
        input_area.insert("second draft")
        await pilot.press("ctrl+pageup")
        await wait_for(lambda: _options(pilot.app, 0).has_focus, pilot=pilot)
        assert prompt.active_index == 0
        assert input_area.text == "first draft"
        assert prompt.snapshot().drafts == ("first draft", "second draft", "")


@pytest.mark.asyncio
async def test_stale_pane_focus_after_two_switches_preserves_latest_context() -> None:
    """A queued focus from the old pane must not reactivate it or replace the draft."""
    contexts: list[int] = []

    class SwitchProbeApp(_PromptApp):
        def on_ask_user_active_question_changed(self, event: AskUserActiveQuestionChanged) -> None:
            contexts.append(event.index)

    draft = PromptDraft(drafts=("first", "second", "latest"))
    async with SwitchProbeApp(_questions(), draft).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        tabbed = pilot.app.query_one(AskUserTabbedContent)
        input_area = pilot.app.query_one(_AskUserTextArea)
        old_pane = tabbed.get_pane(ask_user_pane_id(0, 3))
        prompt._switch(1)
        prompt._switch(2)
        event = TabPane.Focused(old_pane)
        await tabbed._on_message(event)
        await wait_for(
            lambda: input_area.has_focus and len(contexts) >= 2,
            pilot=pilot,
            description="latest question receives deferred focus",
        )
        assert not await wait_until(lambda: len(contexts) > 2, timeout=0.2, pilot=pilot)
        assert contexts == [1, 2]
        assert event._no_default_action
        assert prompt.active_index == 2
        assert tabbed.active == ask_user_pane_id(2, 3)
        assert input_area.text == "latest"
        assert prompt.snapshot().drafts == ("first", "second", "latest")


@pytest.mark.asyncio
async def test_queued_question_focus_does_not_reclaim_the_tab_strip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver queued callbacks explicitly, without depending on refresh speed."""
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        first = _options(pilot.app, 0)
        await wait_for(lambda: first.has_focus, pilot=pilot)
        queued: list[Callable[..., object]] = []
        after_refresh = prompt.call_after_refresh

        def hold_focus(callback: Callable[..., object], *args: object, **kwargs: object) -> bool:
            if callback == prompt._focus_active:
                queued.append(callback)
                return True
            return after_refresh(callback, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(prompt, "call_after_refresh", hold_focus)
            prompt._switch(1)
            prompt._switch(2)
            assert queued
            queued.pop(0)()
            input_area = pilot.app.query_one(_AskUserTextArea)
            await wait_for(lambda: input_area.has_focus, pilot=pilot)
            assert prompt.active_index == 2

            strip = pilot.app.query_one(AskUserTabbedContent).query_one("ContentTabs")
            strip.focus()
            await wait_for(lambda: strip.has_focus, pilot=pilot)
            for callback in queued:
                callback()
            await pilot.pause()
            assert strip.has_focus

        # A later switch must still be able to request focus after delivery.
        prompt._switch(1)
        second = _options(pilot.app, 1)
        await wait_for(lambda: second.has_focus, pilot=pilot)


@pytest.mark.asyncio
async def test_stale_in_flight_draft_snapshot_does_not_clobber_newer_text() -> None:
    """A queue-delayed DraftChanged must not write its lagging snapshot back into the input."""
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        input_area = pilot.app.query_one(_AskUserTextArea)
        await wait_for(lambda: _options(pilot.app, 0).has_focus, pilot=pilot)
        input_area.focus()
        await wait_for(lambda: input_area.has_focus, pilot=pilot)
        input_area.insert("你好")
        assert await wait_until(lambda: prompt._drafts[0] == "你好", pilot=pilot)
        # The Windows IME interleaving: a DraftChanged posted when the input
        # held only "你" reaches the prompt after the next character landed.
        prompt.post_message(AskUserDraftChanged("request", 0, "你"))
        assert await wait_until(lambda: prompt._drafts[0] == "你", pilot=pilot)
        assert input_area.text == "你好"
        assert input_area.cursor_location == (0, 2)


@pytest.mark.asyncio
async def test_single_multi_select_hides_tabs_preserves_click_order_and_dedupes() -> None:
    question = (_questions()[1],)
    async with _PromptApp(question).run_test(size=(100, 30)) as pilot:
        tabbed = pilot.app.query_one(AskUserTabbedContent)
        assert tabbed.has_class("-single")
        assert tabbed.query_one("ContentTabs").display is False
        _toggle(pilot.app, 0, 2)
        _toggle(pilot.app, 0, 0)
        _toggle(pilot.app, 0, 0)
        _toggle(pilot.app, 0, 0)
        await pilot.pause()
        assert _options(pilot.app, 0).selection == (2, 0)
        pilot.app.query_one("#askuser-submit", Button).press()
        app = cast("_PromptApp", pilot.app)
        assert await wait_until(lambda: bool(app.submissions), pilot=pilot)
        assert app.submissions == [(AskUserAnswer(values=("Linux", "macOS")),)]


@pytest.mark.asyncio
async def test_single_select_keeps_exactly_one_box_checked_and_auto_advances() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        input_area = pilot.app.query_one(_AskUserTextArea)
        input_area.insert("only in TUI")
        _toggle(pilot.app, 0, 1)
        assert await wait_until(lambda: prompt.active_index == 1, pilot=pilot)
        assert prompt.answers()[0] == AskUserAnswer(values=("TUI",), note="only in TUI")
        assert await wait_until(lambda: pilot.app.screen.focused is not None, pilot=pilot)

        prompt._switch(0)
        await pilot.pause()
        _toggle(pilot.app, 0, 0)
        await pilot.pause()
        assert _options(pilot.app, 0).selection == (0,)
        assert prompt.answers()[0] == AskUserAnswer(values=("Backend",), note="only in TUI")


@pytest.mark.asyncio
async def test_selected_option_can_be_deselected_into_custom_answer() -> None:
    draft = PromptDraft(selected=((0,),), drafts=("custom",))
    question = (_questions()[0],)
    async with _PromptApp(question, draft).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        assert _options(pilot.app, 0).selection == (0,)
        _toggle(pilot.app, 0, 0)
        await pilot.pause()
        assert _options(pilot.app, 0).selection == ()
        assert prompt.answers() == (AskUserAnswer(values=("custom",)),)


@pytest.mark.asyncio
async def test_option_descriptions_render_on_a_gutter_indented_second_line() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        options = _options(pilot.app, 0)
        # The rendered highlight depends on the prompt's deferred focus.
        assert await wait_until(lambda: pilot.app.screen.focused is options, pilot=pilot)
        first = options.render_line(0).text
        second = options.render_line(1).text
        separator = options.render_line(2).text
        third = options.render_line(3).text
        assert first.startswith("[ ] Backend")
        assert second.startswith(f"    {ASK_USER_DESCRIPTION_GLYPH} 服务层[/]")
        assert not separator.strip()
        assert third.startswith("[ ] TUI")
        assert options.get_option_at_index(0).prompt.plain == f"Backend\n{ASK_USER_DESCRIPTION_GLYPH} 服务层[/]"
        assert "[bold]" not in options.get_option_at_index(0).prompt.plain

        # Choosing moves on to the next question and hides this one. Hidden, the list has no
        # width, and a line rendered then is cut short; it is rendered once the question is back.
        _toggle(pilot.app, 0, 0)
        prompt = pilot.app.query_one(AskUserPrompt)
        await wait_for(lambda: prompt.active_index == 1, pilot=pilot)
        prompt._switch(0)
        await wait_for(
            lambda: options.region.width > 0 and options.render_line(0).text.startswith("[*] Backend"),
            pilot=pilot,
        )


@pytest.mark.asyncio
async def test_keyboard_space_toggles_multi_select_options_in_place() -> None:
    question = (_questions()[1],)
    async with _PromptApp(question).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        options = _options(pilot.app, 0)
        assert await wait_until(lambda: pilot.app.screen.focused is options, pilot=pilot)
        await pilot.press("down", "space", "down", "space")
        await pilot.pause()
        assert options.selection == (1, 2)
        assert prompt.answers() == (AskUserAnswer(values=("Windows", "Linux")),)
        assert prompt.active_index == 0
        await pilot.press("up", "space")
        await pilot.pause()
        assert options.selection == (2,)


@pytest.mark.asyncio
async def test_keys_typed_at_once_toggle_the_options_they_were_typed_on() -> None:
    # Keys typed while the TUI lags reach the App back to back, as the driver
    # posts them: the second Down moves the highlight before the first Space's
    # toggle runs.
    question = (_questions()[1],)
    async with _PromptApp(question).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        options = _options(pilot.app, 0)
        assert await wait_until(lambda: pilot.app.screen.focused is options, pilot=pilot)
        for key in ("down", "space", "down", "space"):
            pilot.app.post_message(events.Key(key, " " if key == "space" else None))
        await wait_for(lambda: options.highlighted == 2, pilot=pilot, description="burst handled")
        await pilot.pause()
        assert prompt.answers() == (AskUserAnswer(values=("Windows", "Linux")),)
        assert options.selection == (1, 2)


@pytest.mark.asyncio
async def test_single_select_key_typed_at_once_submits_the_option_it_was_typed_on() -> None:
    question = (_questions()[0],)
    async with _PromptApp(question).run_test(size=(100, 30)) as pilot:
        app = cast(_PromptApp, pilot.app)
        options = _options(app, 0)
        assert await wait_until(lambda: app.screen.focused is options, pilot=pilot)
        assert options.highlighted == 0
        for key in ("space", "down"):
            app.post_message(events.Key(key, " " if key == "space" else None))
        await wait_for(lambda: bool(app.submissions), pilot=pilot, description="Space submitted")
        assert app.submissions == [(AskUserAnswer(values=("Backend",)),)]


@pytest.mark.asyncio
async def test_highlight_moves_scroll_the_shared_region_not_the_option_list() -> None:
    class ShortPromptApp(_PromptApp):
        CSS = "AskUserPrompt { width: 60; height: 14; } #askuser-inner { max-height: 1fr; }"

    question = (
        AskUserQuestion(
            "Pick many?",
            "Many",
            tuple(AskUserOption(f"Option {index}", f"Description {index}") for index in range(8)),
            multi_select=True,
        ),
    )
    async with ShortPromptApp(question).run_test(size=(80, 20)) as pilot:
        options = _options(pilot.app, 0)
        inner = pilot.app.query_one(AskUserTabbedContent)
        assert await wait_until(lambda: pilot.app.screen.focused is options, pilot=pilot)
        # Flush Textual's deferred focus-visibility callback, then drain whatever
        # animation it scheduled. Focusing this full-height list must not center
        # it instead of revealing its first option.
        await pilot.pause()
        await pilot.wait_for_scheduled_animations()
        # Eight two-line options separated by seven blank rows.
        assert options.region.height == 23
        assert inner.scroll_y == 0
        for _ in range(7):
            await pilot.press("down")
        assert await wait_until(lambda: inner.scroll_y > 0, pilot=pilot)
        assert options.scroll_y == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("focus_before_layout", [False, True], ids=["normal", "focus-before-layout"])
async def test_returning_to_a_pane_reveals_the_restored_option(
    monkeypatch: pytest.MonkeyPatch, focus_before_layout: bool
) -> None:
    class ShortPromptApp(_PromptApp):
        CSS = "AskUserPrompt { width: 60; height: 14; } #askuser-inner { max-height: 1fr; }"

    questions = (
        AskUserQuestion(
            "Pick many?",
            "Many",
            tuple(AskUserOption(f"Option {index}", f"Description {index}") for index in range(8)),
            multi_select=True,
        ),
        AskUserQuestion("Anything else?", "Short", (AskUserOption("Yes"),)),
    )
    async with ShortPromptApp(questions).run_test(size=(80, 20)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        options = _options(pilot.app, 0)
        inner = pilot.app.query_one(AskUserTabbedContent)
        assert await wait_until(lambda: pilot.app.screen.focused is options, pilot=pilot)
        await pilot.press("down", "down", "down", "down", "down", "down", "down", "space")
        assert await wait_until(lambda: inner.scroll_y > 0, pilot=pilot)
        assert options.highlighted == 7
        revealed = inner.scroll_y

        prompt.action_next_question()
        assert await wait_until(lambda: prompt.active_index == 1, pilot=pilot)
        second_options = _options(pilot.app, 1)
        await wait_for(
            lambda: pilot.app.screen.focused is second_options and second_options.has_focus,
            pilot=pilot,
            description="second question focused before returning to the first pane",
        )
        assert await wait_until(lambda: inner.scroll_y == 0, pilot=pilot)

        # Coming back re-highlights option 7, which is already the highlighted
        # index, so the reactive watcher stays silent. The pane switch itself has
        # to re-reveal the restored option in the shared scroll region.
        if focus_before_layout:
            # Reproduce a queued focus callback overtaking the next layout.
            # Keep Textual's real show/layout events, and deliver only this
            # callback early so a later focus cannot mask the lost reveal.
            await wait_for(lambda: _options(pilot.app, 1).has_focus, pilot=pilot)
            queued: list[Callable[..., object]] = []
            after_refresh = prompt.call_after_refresh

            def hold_focus(callback: Callable[..., object], *args: object, **kwargs: object) -> bool:
                if callback == prompt._focus_active:
                    queued.append(callback)
                    return True
                return after_refresh(callback, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(prompt, "call_after_refresh", hold_focus)
                prompt.action_previous_question()
                assert not options.region
                assert len(queued) == 1
                queued.pop()()
        else:
            prompt.action_previous_question()
        await wait_for(
            lambda: pilot.app.screen.focused is options and options.has_focus and inner.scroll_y == revealed,
            pilot=pilot,
            description="restored option focused and revealed after the pane switch",
        )
        assert options.highlighted == 7
        assert inner.scroll_y == revealed


@pytest.mark.asyncio
async def test_zero_answer_submit_requires_two_presses() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt.action_previous_question()
        await pilot.pause()
        assert prompt.active_index == len(_questions())
        submit = pilot.app.query_one("#askuser-submit", Button)
        submit.press()
        await pilot.pause()
        app = cast("_PromptApp", pilot.app)
        assert app.submissions == []
        assert prompt.snapshot().armed_empty_submit is True
        submit.press()
        assert await wait_until(lambda: bool(app.submissions), pilot=pilot)
        assert app.submissions == [(AskUserAnswer(), AskUserAnswer(), AskUserAnswer())]


@pytest.mark.asyncio
async def test_zero_answer_arm_is_disarmed_independently_by_typing_and_deletion() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        input_area = pilot.app.query_one(_AskUserTextArea)
        prompt._submit()
        assert prompt.active_index == 0
        assert prompt.snapshot().armed_empty_submit is True

        input_area.insert("x")
        assert await wait_until(lambda: not prompt.snapshot().armed_empty_submit, pilot=pilot)

    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        input_area = pilot.app.query_one(_AskUserTextArea)
        prompt._submit()
        assert prompt.active_index == 0
        assert prompt.snapshot().armed_empty_submit is True

        input_area._set_document("x", input_area.language)
        input_area.delete((0, 0), (0, 1), maintain_selection_offset=False)
        assert await wait_until(lambda: not prompt.snapshot().armed_empty_submit, pilot=pilot)


@pytest.mark.asyncio
async def test_global_submit_all_some_and_single_delivery_paths() -> None:
    questions = (
        AskUserQuestion("First?", "First", (AskUserOption("A"), AskUserOption("B"))),
        AskUserQuestion("Second?", "Second", (AskUserOption("C"), AskUserOption("D"))),
    )
    async with _PromptApp(questions).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        _toggle(pilot.app, 0, 0)
        assert await wait_until(lambda: prompt.active_index == 1, pilot=pilot)
        second_options = _options(pilot.app, 1)
        assert await wait_until(lambda: pilot.app.screen.focused is second_options, pilot=pilot)
        assert second_options.highlighted == 0
        _toggle(pilot.app, 1, 1)
        assert await wait_until(lambda: prompt.active_index == 2, pilot=pilot)
        submit = pilot.app.query_one("#askuser-submit", Button)
        assert await wait_until(lambda: pilot.app.screen.focused is submit, pilot=pilot)
        submit.press()
        submit.press()
        app = cast("_PromptApp", pilot.app)
        assert await wait_until(lambda: bool(app.submissions), pilot=pilot)
        assert app.submissions == [(AskUserAnswer(values=("A",)), AskUserAnswer(values=("D",)))]

    async with _PromptApp(questions).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        _toggle(pilot.app, 0, 0)
        assert await wait_until(lambda: prompt.active_index == 1, pilot=pilot)
        prompt._switch(2)
        await pilot.pause()
        warning = pilot.app.query_one("#askuser-review-warning", Static)
        assert warning.display is True
        rendered_warning = warning.render()
        assert isinstance(rendered_warning, (Content, Text))
        assert "1 question is still unanswered" in rendered_warning.plain
        pilot.app.query_one("#askuser-submit", Button).press()
        app = cast("_PromptApp", pilot.app)
        assert await wait_until(lambda: bool(app.submissions), pilot=pilot)
        assert app.submissions == [(AskUserAnswer(values=("A",)), AskUserAnswer())]


@pytest.mark.asyncio
async def test_zero_answer_arm_is_disarmed_by_option_toggle_without_switching_context() -> None:
    questions = (
        AskUserQuestion("First?", "First", (AskUserOption("A"),), multi_select=True),
        AskUserQuestion("Second?", "Second"),
    )
    async with _PromptApp(questions).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt._submit()
        assert prompt.active_index == 0
        assert prompt.snapshot().armed_empty_submit is True

        _toggle(pilot.app, 0, 0)
        assert await wait_until(lambda: not prompt.snapshot().armed_empty_submit, pilot=pilot)
        assert prompt.active_index == 0


@pytest.mark.asyncio
async def test_zero_answer_arm_is_disarmed_by_tab_switch() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt._submit()
        assert prompt.active_index == 0
        assert prompt.snapshot().armed_empty_submit is True

        prompt._switch(1)
        await pilot.pause()
        assert prompt.snapshot().armed_empty_submit is False


@pytest.mark.asyncio
async def test_keyboard_only_three_question_completion_keeps_focus_in_each_context() -> None:
    questions = tuple(
        AskUserQuestion(f"Question {index}?", f"Q{index}", (AskUserOption(f"A{index}"),)) for index in range(3)
    )
    async with _PromptApp(questions).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        first = _options(pilot.app, 0)
        assert await wait_until(lambda: pilot.app.screen.focused is first, pilot=pilot)
        submit = pilot.app.query_one("#askuser-submit", Button)
        for expected_index in (1, 2, 3):
            expected_focus = _options(pilot.app, expected_index) if expected_index < len(questions) else submit
            await pilot.press("enter")
            # The index changes before refresh/app callbacks focus the next
            # context. Do not send its Enter until that exact control is ready.
            await wait_for(
                lambda expected_index=expected_index, expected_focus=expected_focus: (
                    prompt.active_index == expected_index and pilot.app.screen.focused is expected_focus
                ),
                pilot=pilot,
                description=f"question context {expected_index} keyboard focus",
            )
        await pilot.press("enter")
        app = cast("_PromptApp", pilot.app)
        assert await wait_until(lambda: bool(app.submissions), pilot=pilot)
        assert app.submissions == [tuple(AskUserAnswer(values=(f"A{index}",)) for index in range(3))]


@pytest.mark.asyncio
async def test_tab_activation_switches_context_and_answered_classes_follow_state() -> None:
    questions = _questions()
    async with _PromptApp(questions).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        tabbed = pilot.app.query_one(AskUserTabbedContent)
        assert _tab(pilot.app, 0, 3).has_class("-active")
        assert _tab(pilot.app, 3, 3).has_class("-dim")
        _toggle(pilot.app, 0, 0)
        assert await wait_until(lambda: prompt.active_index == 1, pilot=pilot)
        assert _tab(pilot.app, 0, 3).has_class("-answered")
        assert _tab(pilot.app, 1, 3).has_class("-active")
        assert not _tab(pilot.app, 3, 3).has_class("-dim")
        assert _tab(pilot.app, 3, 3).has_class("-answered")

        await pilot.click(_tab(pilot.app, 2, 3))
        assert await wait_until(lambda: prompt.active_index == 2, pilot=pilot)
        assert tabbed.active == ask_user_pane_id(2, 3)
        assert await wait_until(
            lambda: pilot.app.screen.focused is pilot.app.query_one(_AskUserTextArea),
            pilot=pilot,
        )

        strip = tabbed.query_one("ContentTabs")
        strip.focus()
        assert await wait_until(lambda: strip.has_focus, pilot=pilot)
        await pilot.press("right")
        assert await wait_until(lambda: prompt.active_index == 3, pilot=pilot)
        # Every switch hands focus to the active context, so re-enter the strip.
        assert await wait_until(
            lambda: pilot.app.screen.focused is pilot.app.query_one("#askuser-submit", Button),
            pilot=pilot,
        )
        strip.focus()
        assert await wait_until(lambda: strip.has_focus, pilot=pilot)
        await pilot.press("left")
        assert await wait_until(lambda: prompt.active_index == 2, pilot=pilot)


@pytest.mark.asyncio
async def test_narrow_prompt_keeps_the_active_tab_visible_in_the_strip() -> None:
    class NarrowPromptApp(_PromptApp):
        CSS = "AskUserPrompt { width: 20; height: 28; }"

    draft = PromptDraft(
        active=len(_questions()),
        selected=((0,), (0,), ()),
        drafts=("", "", "Canary"),
    )
    async with NarrowPromptApp(_questions(), draft).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        strip = pilot.app.query_one(AskUserTabbedContent).query_one("ContentTabs")
        assert prompt.active_index == len(_questions())

        def _active_tab_visible() -> bool:
            tab = _tab(pilot.app, prompt.active_index, len(_questions()))
            return tab.region.width > 0 and strip.region.contains_region(tab.region)

        assert await wait_until(_active_tab_visible, pilot=pilot)
        prompt._switch(0)
        assert await wait_until(_active_tab_visible, pilot=pilot)
        assert _tab(pilot.app, 0, 3).region.x < _tab(pilot.app, 3, 3).region.x


@pytest.mark.asyncio
async def test_headers_are_cell_ellipsized_in_tabs_and_border_titles() -> None:
    questions = (
        AskUserQuestion("First?", "abcdefghijklm"),
        AskUserQuestion("Second?", "一二三四五六七"),
    )
    async with _PromptApp(questions).run_test(size=(100, 30)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        assert _tab(pilot.app, 0, 2).label.plain == "☐ abcdefghijk…"
        assert prompt.active_title().plain == "Question 1/2 · abcdefghijk…"

        prompt._switch(1)
        await pilot.pause()
        assert _tab(pilot.app, 1, 2).label.plain == "☐ 一二三四五…"
        assert _tab(pilot.app, 2, 2).label.plain == "✓ Submit"
        assert prompt.active_title().plain == "Question 2/2 · 一二三四五…"
        assert prompt.questions == questions


@pytest.mark.asyncio
async def test_review_pane_uses_its_review_specific_unanswered_message(monkeypatch: pytest.MonkeyPatch) -> None:
    original_render = ask_user_prompt_module.render_str

    def render_distinct(localizer: object, reference: object) -> str:
        key = reference.definition.key  # type: ignore[union-attr]
        if key == "tui.ask_user.review.unanswered":
            return "REVIEW-ONLY"
        if key == "tui.ask_user.answer.not_answered":
            return "COMPLETED-ONLY"
        return original_render(localizer, reference)  # type: ignore[arg-type]

    monkeypatch.setattr(ask_user_prompt_module, "render_str", render_distinct)
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt._switch(len(_questions()))
        await pilot.pause()
        review = rich_plain(pilot.app.query_one("#askuser-review-list", Static).content)
        assert "REVIEW-ONLY" in review
        assert "COMPLETED-ONLY" not in review


@pytest.mark.asyncio
async def test_review_pane_flags_unanswered_questions_in_the_warning_colour() -> None:
    questions = _questions()

    def unanswered_questions(app: App) -> set[str]:
        lines = rich_segment_lines(app.query_one("#askuser-review-list", Static).content)
        return {
            segment.text.strip()
            for line in lines
            for segment in line
            if segment.style is not None
            and segment.style.color is not None
            and segment.text.strip() in {q.question for q in questions}
        }

    async with _PromptApp(questions).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt._switch(len(questions))
        await pilot.pause()
        assert unanswered_questions(pilot.app) == {question.question for question in questions}

        prompt._switch(0)
        await pilot.pause()
        _toggle(pilot.app, 0, 0)
        prompt._switch(len(questions))
        await pilot.pause()
        assert unanswered_questions(pilot.app) == {question.question for question in questions[1:]}


@pytest.mark.asyncio
async def test_review_pane_mutes_answers_like_the_completed_card() -> None:
    questions = _questions()

    async with _PromptApp(questions).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt._switch(0)
        await pilot.pause()
        _toggle(pilot.app, 0, 0)
        prompt._switch(len(questions))
        await pilot.pause()

        pane = pilot.app.query_one(AskUserReviewPane)
        review_list = pilot.app.query_one("#askuser-review-list", Static)
        answer_colour = pane.get_component_rich_style("askuser-review--answer").color
        assert answer_colour is not None
        # Resolved muted text must differ from the ordinary pane foreground,
        # whether the theme supplies a concrete color or an alpha expression.
        assert answer_colour != review_list.rich_style.color
        colours = {
            segment.text.strip(): segment.style.color
            for line in rich_segment_lines(review_list.content)
            for segment in line
            if segment.style is not None and segment.style.color is not None and segment.text.strip()
        }
        # The chosen option, its glyph and the unanswered placeholders are muted;
        # the answered question keeps the pane colour and the open ones stay flagged.
        assert colours["Backend"] == answer_colour
        assert colours["└─"] == answer_colour
        assert colours["(not answered)"] == answer_colour
        assert questions[0].question not in colours
        assert colours[questions[1].question] != answer_colour


@pytest.mark.asyncio
async def test_hostile_markup_and_cjk_are_plain_in_tabs_options_and_review() -> None:
    async with _PromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        tab = _tab(pilot.app, 0, 3)
        assert "范围[一]" in tab.label.plain
        tab_renderable = tab.render()
        assert isinstance(tab_renderable, (Content, Text))
        assert "范围[一]" in tab_renderable.plain
        options = _options(pilot.app, 0)
        prompt_text = options.get_option_at_index(0).prompt.plain
        assert "[bold]" not in prompt_text
        assert "服务层[/]" in prompt_text
        _toggle(pilot.app, 0, 0)
        prompt = pilot.app.query_one(AskUserPrompt)
        prompt._switch(len(_questions()))
        await pilot.pause()
        review = rich_plain(pilot.app.query_one("#askuser-review-list", Static).content)
        assert "Choose [bold]scope[/]?" in review
        # Answers hang under their question like an option description.
        assert "\n • Choose [bold]scope[/]?\n   └─ Backend\n" in review


@pytest.mark.asyncio
async def test_control_characters_are_replaced_on_display_while_answers_keep_the_label() -> None:
    """Workflow and ACP questions come from outside the model: ESC must never reach the terminal.

    Tabs and line breaks are layout, not controls: they display as spaces and new lines, never U+FFFD.
    """
    esc = "\x1b[31m"
    questions = (
        AskUserQuestion(
            f"Pick {esc}one?\n\tindented",
            f"Hd\t{esc}",
            (AskUserOption(f"lab{esc}el\tx", f"desc{esc}\nmore"), AskUserOption("x")),
        ),
        AskUserQuestion(f"Say {esc}why?", "Why"),
    )
    async with _PromptApp(questions).run_test(size=(100, 35)) as pilot:
        from chrys.app.tui.widgets.ask_user_markdown import AskUserQuestionMarkdown

        assert _tab(pilot.app, 0, 2).label.plain == "☐ Hd �[31m"
        question_source = pilot.app.query_one("#askuser-q0-question", AskUserQuestionMarkdown).source
        assert question_source == "Pick �[31mone?\n    indented"
        option_text = _options(pilot.app, 0).get_option_at_index(0).prompt.plain
        assert "lab�[31mel  x" in option_text and "desc�[31m\nmore" in option_text
        prompt = pilot.app.query_one(AskUserPrompt)
        _toggle(pilot.app, 0, 0)
        await wait_for(lambda: prompt.active_index == 1, pilot=pilot)
        pilot.app.query_one(_AskUserTextArea).insert(f"typed{esc}")
        prompt._switch(len(questions))
        await pilot.pause()
        review = rich_plain(pilot.app.query_one("#askuser-review-list", Static).content)
        assert "\x1b" not in review and "\t" not in review
        assert "Pick �[31mone?" in review and "    indented" in review
        assert "lab�[31mel  x" in review and "typed�[31m" in review
        pilot.app.query_one("#askuser-submit", Button).press()
        await wait_for(lambda: bool(pilot.app.submissions), pilot=pilot)
    # Only display copies change: the answer names the label exactly as the question offered it.
    assert pilot.app.submissions == [
        (AskUserAnswer(values=(f"lab{esc}el\tx",)), AskUserAnswer(values=(f"typed{esc}",)))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", range(1, 11))
async def test_option_list_survives_terminals_narrower_than_its_gutter(columns: int) -> None:
    class NarrowPromptApp(_PromptApp):
        CSS = "AskUserPrompt { width: 100%; height: 100%; }"

    question = AskUserQuestion(
        "选哪个。",
        "范围",
        (AskUserOption("服务层选项", "第二行的描述文字"), AskUserOption("界面层选项", "另一行描述")),
    )
    async with NarrowPromptApp((question,)).run_test(size=(columns, 24)) as pilot:
        await pilot.pause()
        options = _options(pilot.app, 0)
        # Wrapping at a non-positive width is a crash inside Textual; the
        # measured width must floor at one cell however narrow the terminal.
        assert options.get_content_height(Size(columns, 24), Size(columns, 24), 0) >= 1
        assert options.option_count == 2
        # The marker is cropped to whatever gutter is left, so no rendered
        # line spills past a region that exists at all (below four columns the
        # prompt chrome leaves the list no region).
        region = options.scrollable_content_region
        assert (region.width >= 1) == (columns >= 4)
        for y in range(region.height if region.width else 0):
            assert options.render_line(y).cell_length <= region.width


@pytest.mark.asyncio
async def test_review_pane_hangs_wrapped_questions_and_answers_under_their_first_column() -> None:
    long_question = (
        "这个 bug 表现出来是什么。比如某个操作报错、结果不对、崩溃、卡住等。越具体越好、报错信息和现象都要。"
    )
    long_answer = "在终端里输入中文的时候光标位置不对。删除一个字符会少删半个。切换输入法后更明显。"
    long_note = " ".join(["note"] * 30)
    questions = (_questions()[0], AskUserQuestion(long_question, "bug"))
    async with _PromptApp(questions).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        input_area = pilot.app.query_one(_AskUserTextArea)
        _toggle(pilot.app, 0, 0)
        input_area.insert(long_note)
        prompt._switch(1)
        await pilot.pause()
        input_area.insert(long_answer)
        prompt._switch(len(questions))
        await pilot.pause()

        listing = pilot.app.query_one("#askuser-review-list", Static)
        strips = listing.render_lines(Region(0, 0, listing.size.width, listing.size.height))
        lines = [strip.text.rstrip() for strip in strips]
        assert lines[0] == "Review your answers"
        bullets = [index for index, line in enumerate(lines) if line.startswith(" • ")]
        glyphs = [index for index, line in enumerate(lines) if line.startswith("   └─ ")]
        assert len(bullets) == 2 and len(glyphs) == 2
        # A wrapped question continues under its own first character, a
        # wrapped answer or note under the answer text after the glyph.
        note_lines = lines[glyphs[0] + 1 : bullets[1]]
        question_lines = lines[bullets[1] + 1 : glyphs[1]]
        answer_lines = lines[glyphs[1] + 1 :]
        assert note_lines and all(line[:6] == "      " and line[6] != " " for line in note_lines)
        assert question_lines and all(line[:3] == "   " and line[3] != " " for line in question_lines)
        assert answer_lines and all(line[:6] == "      " and line[6] != " " for line in answer_lines)
        assert len(lines) == listing.size.height


@pytest.mark.asyncio
async def test_inline_prompt_uses_the_transcript_id_and_defers_input_layout() -> None:
    class InlinePromptApp(_PromptApp):
        def compose(self) -> ComposeResult:
            yield AskUserPrompt("request", self.questions, inline=True, allow_inline=False)

    async with InlinePromptApp(_questions()).run_test(size=(100, 35)) as pilot:
        prompt = pilot.app.query_one(AskUserPrompt)
        assert prompt.id == "ask-inline"
        assert prompt.inline is True
        assert pilot.app.query_one(_AskUserTextArea)._defer_layout_to_parent is True
        assert len(pilot.app.query("#askuser-inline")) == 0


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (10, (0, 16)),
        (20, (0, 16)),
        (24, (4, 16)),
        (30, (10, 16)),
    ],
)
def test_inline_allocator_has_exact_fixed_rows_and_single_content_region(
    rows: int,
    expected: tuple[int, int],
) -> None:
    assert AskUserToolCall._allocate_inline_budget(rows, has_tabs=True, live=True) == expected
