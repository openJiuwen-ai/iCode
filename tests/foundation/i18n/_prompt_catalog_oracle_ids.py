# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pinned message ids for prompt shortcuts: expansion, the session-switch card, and running their commands."""

from __future__ import annotations

PROMPT_MESSAGE_IDS: frozenset[str | tuple[str, str]] = frozenset(
    {
        "tui.prompt_card.agent",
        "tui.prompt_card.agent_not_found",
        "tui.prompt_card.busy",
        "tui.prompt_card.busy_commands",
        ("tui.prompt_card.command_count", "tui.prompt_card.command_count#plural"),
        "tui.prompt_card.commands",
        "tui.prompt_card.continue_is_a_note",
        "tui.prompt_card.dont_switch",
        "tui.prompt_card.model",
        "tui.prompt_card.model_locked",
        "tui.prompt_card.model_not_found",
        "tui.prompt_card.note",
        "tui.prompt_card.remove",
        "tui.prompt_shortcuts.loading",
        "tui.prompt_shortcuts.loading_tab",
        "tui.prompt_card.title",
        "tui.prompt_shortcuts.hint.argument",
        "tui.prompt_shortcuts.hint.arguments",
        "tui.suggestions.prompt_shortcuts",
        ("tui.input.running_commands", "tui.input.running_commands#plural"),
        "coordinator.prompt_command_not_started",
        "coordinator.prompt_command_stopped",
        "coordinator.prompt_command_timed_out",
        "coordinator.prompt_shortcut_busy",
        "coordinator.prompt_shortcut_unavailable",
    }
)
