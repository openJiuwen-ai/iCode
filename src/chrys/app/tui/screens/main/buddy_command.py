# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stateful /buddy slash-command handling."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from chrys.app.features.buddy import actions
from chrys.app.features.buddy.commands import handle_buddy_command, pet_refusal, split_command
from chrys.app.tui.buddy_reply import PetReplyFlow
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from chrys.app.features.buddy.commands import Severity
    from chrys.app.tui.screens.main.ports import BuddyCommandView

_SUBCOMMAND_HATCH = msg("tui.buddy.subcommand.hatch", fallback="Hatch a new buddy")
_SUBCOMMAND_INFO = msg("tui.buddy.subcommand.info", fallback="Show buddy info")
_SUBCOMMAND_PET = msg("tui.buddy.subcommand.pet", fallback="Pet your buddy")
_SUBCOMMAND_MUTE = msg("tui.buddy.subcommand.mute", fallback="Toggle buddy notifications")
_SUBCOMMAND_NAME = msg("tui.buddy.subcommand.name", fallback="Rename your buddy")

_TOAST_SECONDS = 10


class BuddyCommandController:
    """Own retained async state for /buddy commands.

    A command that changes the buddy waits for the save file's lock, which another instance may
    hold for seconds, so it runs on a thread and answers when it is done. The controller's changes
    land one after another, in the order they were given: two quick ``mute`` commands cancel out.
    A pet is the exception: one at a time, and one given while the last is still being counted, or
    while any answer is still on its way, is dropped whole.
    """

    def __init__(
        self,
        view: BuddyCommandView,
        *,
        render_message: Callable[[MessageRef], str] = format_message,
    ) -> None:
        self._view = view
        self._render_message = render_message
        self._replies = PetReplyFlow(self._toast, on_answered=self._show_saved_buddy)
        self._changes: set[asyncio.Task[None]] = set()
        self._one_change_at_a_time = asyncio.Lock()
        self._count: asyncio.Task[None] | None = None

    @property
    def pet_task(self) -> asyncio.Task[None] | None:
        """Return the retained pet-response task for focused lifecycle tests."""
        return self._replies.task

    @property
    def count_task(self) -> asyncio.Task[None] | None:
        """Return the retained pet-count task for focused lifecycle tests."""
        return self._count

    def subcommands(self) -> list[tuple[str, str]]:
        """Return available /buddy subcommands."""
        if actions.current_buddy() is None:
            return [("hatch", self._render_message(_SUBCOMMAND_HATCH.bind()))]
        return [
            ("info", self._render_message(_SUBCOMMAND_INFO.bind())),
            ("pet", self._render_message(_SUBCOMMAND_PET.bind())),
            ("mute", self._render_message(_SUBCOMMAND_MUTE.bind())),
            ("name", self._render_message(_SUBCOMMAND_NAME.bind())),
        ]

    def handle(self, arg: str) -> None:
        """Handle /buddy command text."""
        if split_command(arg)[0] != "pet":
            self._change(self._carry_out(arg))
        elif not self._pet_in_hand():
            self._count = self._change(self._pet())

    @property
    def busy(self) -> bool:
        """Pending command changes or a pet reply started from either TUI surface."""
        return self._replies.answering or any(not change.done() for change in self._changes)

    def _pet_in_hand(self) -> bool:
        # Judged as the command arrives: queued behind earlier commands, a pet would otherwise be
        # answered again once the count or the answer still in hand had finished ahead of them.
        counting = self._count is not None and not self._count.done()
        return counting or self._replies.answering

    def _toast(self, message: MessageRef | str, timeout: float, *, severity: Severity = "information") -> None:
        self._view.notify_buddy(message, severity=severity, timeout=timeout)

    def _show_saved_buddy(self) -> None:
        # The buddy as it is saved now, on whichever tab the user is looking at.
        self._view.refresh_buddy_panel(focus_tab=False)

    def _change(self, work: Coroutine[None, None, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(work)
        self._changes.add(task)
        task.add_done_callback(self._changes.discard)
        return task

    async def _pet(self) -> None:
        # A save file that cannot be written, or that another instance is wedged on, costs the count, not the answer.
        with contextlib.suppress(OSError):
            # Whether the buddy can be petted is judged once the commands given before this one
            # have landed: a mute or a hatch still on its way decides the answer.
            async with self._one_change_at_a_time:
                buddy = actions.current_buddy()
                refusal = pet_refusal(buddy)
                if refusal is not None:
                    message, severity = refusal
                    self._toast(message, _TOAST_SECONDS, severity=severity)
                    return
                if buddy is None:
                    raise RuntimeError("Petting requires an existing buddy.")
                # Judged again: an answer another surface started meanwhile drops this pet whole too.
                if not self._replies.start(buddy):
                    return
                await asyncio.to_thread(actions.record_pet)
            # The answer and the count finish in either order, and each shows the panel what it did.
            self._show_saved_buddy()

    async def _carry_out(self, arg: str) -> None:
        async with self._one_change_at_a_time:
            was_muted = (buddy := actions.current_buddy()) is not None and buddy.muted
            response, severity = await asyncio.to_thread(handle_buddy_command, arg, render=self._render_message)
        # A muted buddy keeps quiet, except to confirm the very command that mutes or unmutes it.
        # A command that did not work is the user's business either way.
        if not was_muted or severity == "warning" or split_command(arg)[0] == "mute":
            self._toast(response, _TOAST_SECONDS, severity=severity)

        def do_refresh() -> None:
            self._view.refresh_buddy_panel(focus_tab=True)

        self._view.call_after_refresh(do_refresh)

    async def shutdown(self) -> None:
        """Stop waiting for /buddy work when the owning screen unmounts. A change already on its thread still lands."""
        changes = list(self._changes)
        for change in changes:
            change.cancel()
        await asyncio.gather(*changes, return_exceptions=True)
        await self._replies.shutdown()
