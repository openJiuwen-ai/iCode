# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the stateful /buddy command controller."""

from __future__ import annotations

from random import Random
from typing import TYPE_CHECKING

import pytest

from chrys.app.features.buddy import actions, replies
from chrys.app.features.buddy.replies import reply_gate
from chrys.app.tui.screens.main.buddy_command import BuddyCommandController
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from tests.support.buddies import HeldPetReply, HeldSaveFile, wedge_the_save_file
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.app.features.buddy.model import Buddy


class _BuddyView:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str, float]] = []
        self.refreshes: list[bool] = []

    def notify_buddy(
        self,
        message: MessageRef | str,
        *,
        severity: str = "information",
        timeout: float = 10,
    ) -> None:
        self.notifications.append((message if isinstance(message, str) else format_message(message), severity, timeout))

    def refresh_buddy_panel(self, *, focus_tab: bool) -> None:
        self.refreshes.append(focus_tab)

    def call_after_refresh(self, callback: Callable[[], None]) -> None:
        callback()


class _RawBuddyView(_BuddyView):
    """Also keeps each message as it arrived, so a test can render it in another locale itself."""

    def __init__(self) -> None:
        super().__init__()
        self.raw: list[MessageRef | str] = []

    def notify_buddy(
        self,
        message: MessageRef | str,
        *,
        severity: str = "information",
        timeout: float = 10,
    ) -> None:
        self.raw.append(message)
        super().notify_buddy(message, severity=severity, timeout=timeout)


pytestmark = pytest.mark.usefixtures("buddy_reply_gate_left_open")


def _saved_buddy() -> Buddy:
    buddy = actions.current_buddy()
    assert buddy is not None
    return buddy


def _pets() -> int:
    return _saved_buddy().record.pets


def test_the_offered_subcommands_depend_on_whether_a_buddy_has_hatched() -> None:
    controller = BuddyCommandController(_BuddyView())

    assert [name for name, _description in controller.subcommands()] == ["hatch"]
    actions.hatch(Random(1))
    assert [name for name, _description in controller.subcommands()] == ["info", "pet", "mute", "name"]


@pytest.mark.asyncio
async def test_pet_without_a_buddy_warns_and_asks_nobody() -> None:
    view = _BuddyView()
    controller = BuddyCommandController(view)

    controller.handle("pet")

    await wait_for(lambda: len(view.notifications) == 1, description="the pet is refused")
    assert controller.pet_task is None
    assert view.notifications == [("There is nobody to pet yet. Hatch a buddy with /buddy hatch.", "warning", 10)]
    assert view.refreshes == []


@pytest.mark.asyncio
async def test_pet_of_a_muted_buddy_warns_asks_nobody_and_counts_nothing() -> None:
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    actions.rename("Nori")
    actions.set_muted(True)

    controller.handle(" PET ")

    await wait_for(lambda: len(view.notifications) == 1, description="the pet is refused")
    assert controller.pet_task is None
    assert view.notifications == [
        ("Nori is muted and stays quiet. Use /buddy mute to hear from it again.", "warning", 10)
    ]
    assert _pets() == 0


@pytest.mark.asyncio
async def test_pet_is_judged_once_the_commands_given_before_it_have_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mute still on its way to the save file decides the pet typed right after it."""
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    actions.rename("Nori")
    hold = HeldSaveFile(monkeypatch)

    controller.handle("mute")
    controller.handle("pet")

    await wait_for(hold.entered.is_set, description="the mute has reached the save file")
    assert (model.calls, controller.pet_task, view.notifications) == (0, None, [])

    hold.release()
    await wait_for(lambda: len(view.notifications) == 2, description="the mute and then the pet are answered")
    assert [text for text, _severity, _timeout in view.notifications] == [
        "Your buddy will keep quiet from now on. It is still around.",
        "Nori is muted and stays quiet. Use /buddy mute to hear from it again.",
    ]
    assert (model.calls, controller.pet_task, _pets()) == (0, None, 0)


@pytest.mark.asyncio
async def test_pet_counts_the_pet_shows_the_answer_and_refreshes_the_panel_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))

    controller.handle("pet")
    controller.handle("pet")  # one answer at a time: the second pet is dropped whole

    await wait_for(lambda: _pets() == 1, description="the pet is counted")
    await wait_for(lambda: view.refreshes == [False], description="the count showed itself, without taking the tab")
    assert len(view.notifications) == 1
    task = controller.pet_task
    assert task is not None
    model.go.set()
    await task

    assert view.notifications[-1] == ("💛 hoot", "information", 10)
    # The answer showed itself too, and the tab stayed where it was.
    assert view.refreshes == [False, False]
    assert controller.pet_task is None


@pytest.mark.asyncio
async def test_a_stock_answer_reaches_the_toast_as_a_message_the_ui_can_localize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def offline(_buddy: Buddy) -> str:
        raise ConnectionError("offline")

    monkeypatch.setattr(replies, "_ask_model", offline)
    view = _RawBuddyView()
    controller = BuddyCommandController(view)
    buddy = actions.hatch(Random(1))

    controller.handle("pet")
    await wait_for(lambda: len(view.raw) == 2, description="the thinking line, then the answer")
    if controller.pet_task is not None:
        await controller.pet_task

    answer = view.raw[-1]
    assert isinstance(answer, MessageRef)
    chinese = Localizer("zh-Hans").render(answer)
    assert buddy.name in chinese
    assert chinese != format_message(answer)


@pytest.mark.asyncio
async def test_a_count_that_lands_after_the_answer_still_reaches_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    hold = HeldSaveFile(monkeypatch)

    controller.handle("pet")
    await wait_for(hold.entered.is_set, description="the count has reached the save file")
    model.go.set()
    await wait_for(lambda: view.notifications[-1:] == [("💛 hoot", "information", 10)], description="answered")
    assert view.refreshes == [False]
    assert _pets() == 0

    hold.release()
    await wait_for(lambda: view.refreshes == [False, False], description="the count shows itself once it lands")
    assert _pets() == 1


@pytest.mark.asyncio
async def test_a_second_pet_is_dropped_whole_even_when_the_first_answer_lands_before_its_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count can sit on a save file another instance holds for seconds; the answer does not wait for it."""
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    hold = HeldSaveFile(monkeypatch)

    controller.handle("pet")
    count = controller.count_task
    assert count is not None
    await wait_for(hold.entered.is_set, description="the first pet's count has reached the save file")
    model.go.set()
    await wait_for(lambda: controller.pet_task is None, description="the first answer has landed")

    controller.handle("pet")  # given while the first is still being counted, its answer already shown

    assert controller.count_task is count  # dropped: no second count was queued
    hold.release()
    await count
    # One pet's worth of toasts: that it is thinking, and the answer.
    assert (model.calls, _pets(), len(view.notifications)) == (1, 1, 2)


@pytest.mark.asyncio
async def test_a_pet_queued_behind_a_slow_command_is_dropped_while_the_first_answer_is_on_its_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted already, the first pet is still in hand until its answer lands; the queue is no way past that."""
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))

    controller.handle("pet")
    count = controller.count_task
    assert count is not None
    await count
    assert (_pets(), controller.pet_task is not None) == (1, True)
    hold = HeldSaveFile(monkeypatch)
    controller.handle("name Mochi")
    await wait_for(hold.entered.is_set, description="the rename has reached the save file")

    controller.handle("pet")  # given while the first answer is still on its way

    assert controller.count_task is count  # dropped: nothing was queued behind the rename
    model.go.set()
    await wait_for(lambda: controller.pet_task is None, description="the first answer has landed")
    hold.release()
    await wait_for(lambda: _saved_buddy().name == "Mochi", description="the rename has landed")
    assert (model.calls, _pets()) == (1, 1)


@pytest.mark.asyncio
async def test_a_pet_given_while_another_surface_is_answering_is_dropped_whole() -> None:
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    assert reply_gate.acquire(blocking=False)
    try:
        controller.handle("pet")
    finally:
        reply_gate.release()

    assert controller.count_task is None
    assert (view.notifications, _pets()) == ([], 0)


@pytest.mark.asyncio
async def test_a_pet_given_after_the_previous_one_is_over_is_answered_again(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))

    controller.handle("pet")
    count = controller.count_task
    assert count is not None
    await model.asked.wait()
    model.go.set()
    await count
    await wait_for(lambda: controller.pet_task is None, description="the first answer has landed")

    controller.handle("pet")

    assert controller.count_task is not count
    await wait_for(lambda: _pets() == 2, description="the second pet is counted")
    assert model.calls == 2


@pytest.mark.asyncio
async def test_pet_still_answers_when_the_save_file_is_wedged(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    wedge_the_save_file(monkeypatch)

    controller.handle("pet")
    await model.asked.wait()
    task = controller.pet_task
    assert task is not None
    model.go.set()
    await task

    assert view.notifications[-1][0] == "💛 hoot"
    await controller.shutdown()
    assert _pets() == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_the_answer_being_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch, "💛 hoot")
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))

    controller.handle("pet")
    await model.asked.wait()
    task = controller.pet_task
    assert task is not None

    await controller.shutdown()

    assert task.cancelled()
    assert controller.pet_task is None
    assert len(view.notifications) == 1


@pytest.mark.asyncio
async def test_shutdown_leaves_an_answer_another_surface_owns_alone() -> None:
    assert reply_gate.acquire(blocking=False)
    try:
        await BuddyCommandController(_BuddyView()).shutdown()
        assert not reply_gate.acquire(blocking=False)
    finally:
        reply_gate.release()


@pytest.mark.asyncio
async def test_other_commands_answer_with_a_toast_and_bring_the_buddy_tab_forward() -> None:
    view = _BuddyView()
    controller = BuddyCommandController(view)

    controller.handle("hatch")

    await wait_for(lambda: view.refreshes == [True], description="the command is answered")
    buddy = actions.current_buddy()
    assert buddy is not None
    assert len(view.notifications) == 1
    assert buddy.name in view.notifications[0][0]
    assert view.notifications[0][1:] == ("information", 10)


@pytest.mark.asyncio
async def test_a_command_waits_for_the_save_file_on_a_thread_while_the_loop_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions.hatch(Random(1))
    view = _BuddyView()
    controller = BuddyCommandController(view)
    hold = HeldSaveFile(monkeypatch)

    controller.handle("name Mochi")

    # Seen from the loop while the change sits on the save file: the loop is not the one sitting there.
    await wait_for(hold.entered.is_set, description="the rename has reached the save file")
    assert view.notifications == []
    assert _saved_buddy().name != "Mochi"

    hold.release()
    await wait_for(lambda: view.refreshes == [True], description="the rename is answered")
    assert view.notifications == [("Your buddy answers to Mochi now.", "information", 10)]
    assert _saved_buddy().name == "Mochi"


@pytest.mark.asyncio
async def test_commands_land_in_the_order_they_were_given(monkeypatch: pytest.MonkeyPatch) -> None:
    actions.hatch(Random(1))
    view = _BuddyView()
    controller = BuddyCommandController(view)
    hold = HeldSaveFile(monkeypatch)

    controller.handle("mute")
    controller.handle("mute")
    controller.handle("name Mochi")
    await wait_for(hold.entered.is_set, description="the first command has reached the save file")
    hold.release()

    await wait_for(lambda: len(view.refreshes) == 3, description="all three commands are answered")
    assert [text for text, _severity, _timeout in view.notifications] == [
        "Your buddy will keep quiet from now on. It is still around.",
        "Your buddy can speak up again.",
        "Your buddy answers to Mochi now.",
    ]
    assert (_saved_buddy().muted, _saved_buddy().name) == (False, "Mochi")


@pytest.mark.asyncio
async def test_shutdown_stops_waiting_for_a_command_and_the_command_still_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions.hatch(Random(1))
    view = _BuddyView()
    controller = BuddyCommandController(view)
    hold = HeldSaveFile(monkeypatch)

    controller.handle("name Mochi")
    await wait_for(hold.entered.is_set, description="the rename has reached the save file")

    await controller.shutdown()
    hold.release()

    await wait_for(lambda: _saved_buddy().name == "Mochi", description="the rename lands on its thread")
    assert view.notifications == []
    assert view.refreshes == []


@pytest.mark.asyncio
async def test_a_muted_buddy_only_speaks_up_to_confirm_the_mute_command_itself() -> None:
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))

    controller.handle("mute")
    await wait_for(lambda: view.refreshes == [True], description="the mute is answered")
    assert len(view.notifications) == 1

    controller.handle("info")
    controller.handle("name Nori")
    await wait_for(lambda: len(view.refreshes) == 3, description="info and the rename are dealt with")
    assert len(view.notifications) == 1
    assert _saved_buddy().name == "Nori"

    controller.handle("Mute")
    await wait_for(lambda: len(view.refreshes) == 4, description="the unmute is answered")
    assert len(view.notifications) == 2
    assert not _saved_buddy().muted
    assert view.refreshes == [True, True, True, True]


@pytest.mark.asyncio
async def test_a_muted_buddy_still_hears_about_a_command_that_did_not_work(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _BuddyView()
    controller = BuddyCommandController(view)
    actions.hatch(Random(1))
    actions.rename("Nori")
    actions.set_muted(True)

    controller.handle("name")
    controller.handle("dance")
    await wait_for(lambda: len(view.refreshes) == 2, description="both refusals are answered")
    assert [severity for _text, severity, _timeout in view.notifications] == ["warning", "warning"]

    wedge_the_save_file(monkeypatch)
    controller.handle("name Mochi")
    await wait_for(lambda: len(view.refreshes) == 3, description="the failed rename is answered")

    text, severity, _timeout = view.notifications[-1]
    assert severity == "warning"
    assert text.startswith("The buddy save file could not be updated")
    assert len(view.notifications) == 3
    assert _saved_buddy().name == "Nori"
