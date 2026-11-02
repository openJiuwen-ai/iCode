# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Slash command definitions for the main screen."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from textual.content import Content

from chrys.app.tui.screens.dialogs.approval.mode import MODE_AUTO_DESCRIPTION, MODE_BYPASS_DESCRIPTION
from chrys.app.tui.screens.main.buddy_command import BuddyCommandController
from chrys.app.tui.widgets.chrome.commands import (
    ManPageHeading,
    ManPageProseBlock,
    ManPageRows,
    ManPageSegment,
    ManPageSpec,
    ManPageVerbatimBlock,
    SlashCommandDef,
)
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

INVALID_COMMAND_TITLE = msg("tui.commands.title.invalid", fallback="Invalid Command")
INVALID_COMMAND_TITLE_REF = INVALID_COMMAND_TITLE.bind()
_MAN_PAGE_TITLE = msg("tui.commands.title.man_page", fallback="Man Page")
_UNKNOWN_AGENTS_TARGET = msg(
    "tui.commands.unknown_agents_target",
    fallback="Unknown /agents target: {value}",
)
_UNKNOWN_SETTINGS_TAB = msg(
    "tui.commands.unknown_settings_tab",
    fallback="Unknown /settings tab: {value}",
)
_UNKNOWN_COMMAND = msg("tui.commands.unknown_command", fallback="Unknown command: /{cmd_name}")

_DESCRIPTION_NEW = msg("tui.commands.description.new", fallback="Start a new session")
_DESCRIPTION_CLEAR = msg(
    "tui.commands.description.clear",
    fallback="Delete the current session and start a new one",
)
_DESCRIPTION_EXIT = msg("tui.commands.description.exit", fallback="Exit {app}")
_DESCRIPTION_RESUME = msg("tui.commands.description.resume", fallback="Resume the most recent session in this mode")
_DESCRIPTION_FORK = msg("tui.commands.description.fork", fallback="Fork the current session")
_DESCRIPTION_BTW = msg("tui.commands.description.btw", fallback="Ask a side question without changing the main task")
_MAN_BTW_BODY = msg(
    "tui.man.btw.body",
    fallback=(
        "Use /btw <question> to ask using the conversation's existing text and\n"
        "tool results. Use /btw alone to reopen this session's latest saved answer.\n\n"
        "The main task continues separately. The side question cannot use tools\n"
        "and does not enter the main conversation. Its card appears below the\n"
        "chat, leaving the sidebar at full height.\n\n"
        "The Answer tab opens by default, showing a thinking animation until\n"
        "the complete answer is ready. The Question tab keeps your full question.\n"
        "Long content scrolls within each tab. Previous/next buttons browse saved\n"
        "answers without changing your selected tab, and are disabled while generating.\n\n"
        "Completed answers are kept with the session. Clearing, deleting, or rolling\n"
        "back to the starting screen removes them. Prompt-submission hooks also check\n"
        "side questions. Failed or cancelled questions are not saved. Escape or the\n"
        "close icon cancels a running question or hides the card, keeping saved\n"
        "answers. Only one side question can run at a time."
    ),
    multiline=True,
)
_DESCRIPTION_RENAME = msg(
    "tui.commands.description.rename",
    fallback="Set or clear a custom session title",
)
_DESCRIPTION_SESSIONS = msg("tui.commands.description.sessions", fallback="Browse saved sessions")
_DESCRIPTION_THEME = msg("tui.commands.description.theme", fallback="Set color theme")
_DESCRIPTION_LANGUAGE = msg("tui.commands.description.language", fallback="Set display language")
_DESCRIPTION_CHDIR = msg("tui.commands.description.chdir", fallback="Change working directory")
_DESCRIPTION_COPY = msg(
    "tui.commands.description.copy",
    fallback="Copy agent, user, or all turns to clipboard",
)
_DESCRIPTION_FOLD = msg(
    "tui.commands.description.fold", fallback="Toggle collapse on activity, tool groups and thinking"
)
_DESCRIPTION_DIFF = msg(
    "tui.commands.description.diff",
    fallback="View file changes for the current session",
)
_DESCRIPTION_ROLLBACK = msg(
    "tui.commands.description.rollback",
    fallback="Discard recent turns or return to a specific turn",
)
_DESCRIPTION_APPROVAL = msg(
    "tui.commands.description.approval",
    fallback="Switch approval mode: manual → auto → bypass",
)
_DESCRIPTION_MODELS = msg(
    "tui.commands.description.models",
    fallback="Configure model provider and settings",
)
_DESCRIPTION_BUDDY = msg("tui.commands.description.buddy", fallback="Hatch, pet and look after your buddy")
_DESCRIPTION_AGENTS = msg("tui.commands.description.agents", fallback="Manage agent configs")
_DESCRIPTION_RUNTIME = msg(
    "tui.commands.description.runtime",
    fallback="Show active model, tools, skills, and files",
)
_DESCRIPTION_USAGE = msg(
    "tui.commands.description.usage",
    fallback="Show token usage and speed across all sessions",
)
_DESCRIPTION_SETTINGS = msg("tui.commands.description.settings", fallback="Open the Settings panel")
_DESCRIPTION_MAN = msg("tui.commands.description.man", fallback="Show manual page for a command")

_APPROVAL_MANUAL = msg(
    "tui.commands.approval.manual",
    fallback="Approve each call that needs approval",
)

_AGENTS_TARGET_BASIC = msg("tui.commands.agents_target.basic", fallback="Open Basic settings")
_AGENTS_TARGET_INSTRUCTIONS = msg(
    "tui.commands.agents_target.instructions",
    fallback="Open Instructions editor",
)
_AGENTS_TARGET_TOOLS = msg("tui.commands.agents_target.tools", fallback="Open Tools settings")
_AGENTS_TARGET_SUB_AGENTS = msg(
    "tui.commands.agents_target.sub_agents",
    fallback="Open Sub-Agents settings",
)
_AGENTS_TARGET_SKILLS = msg("tui.commands.agents_target.skills", fallback="Open Skills settings")
_AGENTS_TARGET_MCP = msg("tui.commands.agents_target.mcp", fallback="Open MCP server settings")
_AGENTS_TARGET_MEMORY = msg("tui.commands.agents_target.memory", fallback="Open Memory settings")
_AGENTS_TARGET_COMPACTION = msg(
    "tui.commands.agents_target.compaction",
    fallback="Open Compaction settings",
)

_MAN_SUBCOMMAND = msg("tui.commands.man.command", fallback="Show help for /{name}")

_MAN_HEADING_NAME = msg("tui.man.heading.name", fallback="NAME")
_MAN_HEADING_SYNOPSIS = msg("tui.man.heading.synopsis", fallback="SYNOPSIS")
_MAN_HEADING_DESCRIPTION = msg("tui.man.heading.description", fallback="DESCRIPTION")
_MAN_HEADING_ALIASES = msg("tui.man.heading.aliases", fallback="ALIASES")
_MAN_HEADING_OPTIONS = msg("tui.man.heading.options", fallback="OPTIONS")
_MAN_HEADING_AVAILABLE_COMMANDS = msg(
    "tui.man.heading.available_commands",
    fallback="AVAILABLE COMMANDS",
)
_MAN_HEADING_SEE_ALSO = msg("tui.man.heading.see_also", fallback="SEE ALSO")
_MAN_EXAMPLES_LABEL = msg("tui.man.examples_label", fallback="Examples:")
_MAN_EXAMPLE_SHOW_ALL = msg("tui.man.example.show_all", fallback="Show all commands")
_MAN_EXAMPLE_SHOW_THEME = msg("tui.man.example.show_theme", fallback="Show help for /theme")
_MAN_EXAMPLE_SHOW_DIFF = msg("tui.man.example.show_diff", fallback="Show help for /diff")
_MAN_ALIASES_NONE = msg("tui.man.aliases_none", fallback="none")
_MAN_SUPPORTS_SUBCOMMANDS = msg(
    "tui.man.options.supports_subcommands",
    fallback="This command supports subcommands.",
)
_MAN_NO_ADDITIONAL_OPTIONS = msg(
    "tui.man.options.no_additional_options",
    fallback="This command does not take additional options.",
)
_MAN_INDEX_NAME = msg(
    "tui.man.index.name",
    fallback="{app} - AI-powered code assistant",
)
_MAN_INDEX_DESCRIPTION = msg(
    "tui.man.index.description",
    fallback=("{app} is a terminal-based AI assistant for code exploration,\nanalysis, and understanding."),
    multiline=True,
)
_MAN_INDEX_SEE_ALSO = msg(
    "tui.man.index.see_also",
    fallback="Show detailed help for a specific command",
)

_MAN_OPTION_COPY_AGENT = msg(
    "tui.man.option.copy_agent",
    fallback="Copy agent turns. /copy N is shorthand for /copy agent N.",
)
_MAN_OPTION_COPY_USER = msg("tui.man.option.copy_user", fallback="Copy user turns.")
_MAN_OPTION_COPY_ALL = msg(
    "tui.man.option.copy_all",
    fallback="Copy the full conversation transcript.",
)
_MAN_OPTION_COPY_COUNT = msg(
    "tui.man.option.copy_count",
    fallback="Positive integer count of recent turns to copy.",
)
_MAN_OPTION_ROLLBACK_COUNT = msg(
    "tui.man.option.rollback_count",
    fallback="Positive number of most recent turns to discard.",
)
_MAN_OPTION_ROLLBACK_TARGET = msg(
    "tui.man.option.rollback_target",
    fallback="Keep turns 1 through N and discard every later turn. N may be 0.",
)

_MAN_NEW_BODY = msg(
    "tui.man.new.body",
    fallback=(
        "Start a completely new {app} session.\n\n"
        "This clears the current conversation context and begins fresh.\n"
        "Use this when you want to work on a new task without\n"
        "carrying over previous context."
    ),
    multiline=True,
)
_MAN_CLEAR_BODY = msg(
    "tui.man.clear.body",
    fallback=(
        "Delete the current {app} session and start a new one.\n\n"
        "Unlike /new, which keeps the current session so it can be\n"
        "resumed later, /clear permanently removes it from disk.\n"
        "A confirmation dialog opens first; nothing is deleted\n"
        "until you confirm, and the deletion cannot be undone."
    ),
    multiline=True,
)
_MAN_EXIT_BODY = msg(
    "tui.man.exit.body",
    fallback=(
        "Exit {app} and return to the terminal.\n\n"
        "If an agent is currently running, you will be prompted\n"
        "to confirm before exiting."
    ),
    multiline=True,
)
_MAN_RESUME_BODY = msg(
    "tui.man.resume.body",
    fallback=("Resume the most recent {app} chat conversation."),
    multiline=True,
)
_MAN_FORK_BODY = msg(
    "tui.man.fork.body",
    fallback=(
        "Create an independent branch of the current session.\n\n"
        "The current window stays on the existing session. After\n"
        "the fork is created, choose whether to stay here or switch\n"
        "this window to the forked session."
    ),
    multiline=True,
)
_MAN_RENAME_BODY = msg(
    "tui.man.rename.body",
    fallback=(
        "With a title argument, apply it directly as the custom\n"
        "session title. Without one (or with only whitespace),\n"
        "open the session title editor — the same dialog as\n"
        "clicking the session title on the chat border.\n\n"
        "A custom title becomes the permanent title for this\n"
        "session (chat border, terminal tab, session list) and\n"
        "disables auto-generated titles. Clearing it in the\n"
        "editor falls back to the automatic title."
    ),
    multiline=True,
)
_MAN_SESSIONS_BODY = msg(
    "tui.man.sessions.body",
    fallback=(
        "Open the session browser to view and resume previous sessions.\n\n"
        "From the browser you can:\n"
        "    - Select a session to resume\n"
        "    - Delete old sessions\n"
        "    - View session metadata (date, turns, etc.)"
    ),
    multiline=True,
)
_MAN_THEME_BODY = msg(
    "tui.man.theme.body",
    fallback=(
        "Change the color theme of the {app} interface.\n\n"
        "Without arguments, opens the theme picker dialog.\n"
        "With a theme name, directly switches to that theme.\n\n"
        "Available themes can be listed using tab completion after /theme."
    ),
    multiline=True,
)
_MAN_LANGUAGE_BODY = msg(
    "tui.man.language.body",
    fallback=(
        "Change the display language of the {app} interface.\n\n"
        "Without arguments, opens the language picker dialog.\n"
        "With a supported locale, directly confirms that language.\n\n"
        "Available locales can be listed using tab completion after /language."
    ),
    multiline=True,
)
_MAN_CHDIR_BODY = msg(
    "tui.man.chdir.body",
    fallback=(
        "Change the current working directory for the {app} session.\n\n"
        "Subsequent file operations and AI context will use\n"
        "the new directory as the base path."
    ),
    multiline=True,
)
_MAN_COPY_BODY = msg(
    "tui.man.copy.body",
    fallback=(
        "Copy conversation turns to the clipboard.\n\n"
        "Without arguments, copies the most recent agent turn.\n"
        "With a number N, copies the last N agent turns.\n"
        '"/copy agent N" is equivalent to "/copy N".\n'
        '"/copy user N" copies the last N user turns.\n'
        'Use "all" to copy all turns for a role, or the full transcript.'
    ),
    multiline=True,
)
_MAN_COPY_USEFUL_FOR = msg(
    "tui.man.copy.useful_for",
    fallback=(
        "Useful for:\n"
        "    - Sharing conversation snippets\n"
        "    - Archiving important AI outputs\n"
        "    - Creating documentation from AI-generated content"
    ),
    multiline=True,
)
_MAN_FOLD_BODY = msg(
    "tui.man.fold.body",
    fallback=(
        "Toggle collapse state on activity sections, tool groups\n"
        "and thinking in the current conversation view.\n\n"
        "If any are open, collapse them all; otherwise expand them all.\n"
        "Thinking that is still being written and controls waiting\n"
        "for your input stay open.\n"
        "Click an activity header to toggle just that section.\n"
        "Useful for decluttering the view when reviewing long sessions."
    ),
    multiline=True,
)
_MAN_DIFF_BODY = msg(
    "tui.man.diff.body",
    fallback=(
        "Display a diff view of all file changes made in the current session.\n\n"
        "Shows added, modified, and deleted files with line-by-line changes.\n"
        "Useful for reviewing what the agent has done before committing."
    ),
    multiline=True,
)
_MAN_ROLLBACK_BODY = msg(
    "tui.man.rollback.body",
    fallback=(
        "Roll back recent conversation turns.\n\n"
        "Without arguments, opens the rollback picker at the most\n"
        "recent available target. Use this picker when you want to\n"
        "discard conversation without restoring file changes.\n\n"
        'A bare number is a relative count. "/rollback 1" discards\n'
        'the last turn; "/rollback 3" discards the last three turns.\n'
        "A valid explicit count executes immediately.\n\n"
        'The "to" form is an absolute target. "/rollback to 1"\n'
        "keeps Turn 1 and discards Turns 2 through the current turn.\n"
        'Use "/rollback to 0" to return to the session start. A valid\n'
        "explicit target executes immediately.\n\n"
        "Both explicit forms restore eligible file changes by\n"
        "default. The picker also selects file rollback by default;\n"
        "explicitly turn it off there to discard conversation only.\n\n"
        "Explicit commands immediately show a non-dismissible loading\n"
        "modal. Picker actions show it after confirmation. It blocks\n"
        "interaction until rollback has completely finished.\n\n"
        "Use with caution: this action cannot be undone."
    ),
    multiline=True,
)
_MAN_APPROVAL_BODY = msg(
    "tui.man.approval.body",
    fallback="Control how tool calls are approved during agent execution.\n\n    Modes:",
    multiline=True,
)
_MAN_APPROVAL_MODE_HINT = msg(
    "tui.man.approval.mode_hint",
    fallback="Use tab completion to switch directly to a specific mode.",
)
_MAN_MODELS_BODY = msg(
    "tui.man.models.body",
    fallback=(
        "Open the model configuration dialog.\n\n"
        "Configure:\n"
        "    - Model provider (OpenAI, Anthropic, local, etc.)\n"
        "    - Model name and version\n"
        "    - Temperature and other generation parameters\n"
        "    - API keys and endpoints"
    ),
    multiline=True,
)
_MAN_BUDDY_BODY = msg(
    "tui.man.buddy.body",
    fallback=(
        "Look after the buddy that lives in the sidebar.\n\n"
        "    Subcommands:\n"
        "        hatch   - Hatch your buddy (there is only ever one)\n"
        "        info    - Show how your buddy is doing\n"
        "        pet     - Pet your buddy and hear what it says\n"
        "        mute    - Keep your buddy quiet, or let it speak again\n"
        "        name    - Give your buddy a new name"
    ),
    multiline=True,
)
_MAN_AGENTS_BODY = msg(
    "tui.man.agents.body",
    fallback=(
        "Open the agent configuration panel.\n\n"
        "    Without arguments, opens the main agent settings dialog.\n"
        "    With a target name, opens directly to that settings tab.\n\n"
        "    Targets:\n"
        "        basic        - Basic agent settings\n"
        "        instructions - Edit system instructions\n"
        "        tools        - Configure available tools\n"
        "        sub-agents   - Manage sub-agent configurations\n"
        "        skills       - Configure agent skills\n"
        "        mcp          - MCP server settings\n"
        "        memory       - Configure memory files and folders\n"
        "        compaction   - Context compaction settings"
    ),
    multiline=True,
)
_MAN_RUNTIME_BODY = msg(
    "tui.man.runtime.body",
    fallback=(
        "Open the active runtime details dialog.\n\n"
        "    Shows:\n"
        "        - The active model profile and model ID\n"
        "        - Built-in tools grouped by category\n"
        "        - MCP tools grouped by server\n"
        "        - Prompt shortcuts grouped by source folder\n"
        "        - Skills grouped by source directory\n"
        "        - Preconfigured memory files grouped by source"
    ),
    multiline=True,
)
_MAN_USAGE_BODY = msg(
    "tui.man.usage.body",
    fallback=(
        "Open the token usage dialog, read from every saved session.\n\n"
        "    Overview: new tokens by date range, their trend over time,\n"
        "    and totals by model, source or project, with each model's speed.\n"
        "    Calendar: new tokens day by day, and one day's details.\n\n"
        "    New tokens are the input not served from the provider's cache,\n"
        "    plus the output, thinking included. Deleted sessions and external\n"
        "    ACP agents are not counted."
    ),
    multiline=True,
)
_MAN_SETTINGS_BODY = msg(
    "tui.man.settings.body",
    fallback=(
        "Open the Settings panel (F10), optionally on a tab.\n\n"
        "    Tabs: general, models, safety, sessions, tools, notifications.\n"
        "    Changes are saved as you make them; the panel says when each\n"
        "    one takes effect (immediately, on close, or after a restart)."
    ),
    multiline=True,
)
_MAN_MAN_BODY = msg(
    "tui.man.man.body",
    fallback=(
        "Display detailed documentation for {app} commands.\n\n"
        "    Without arguments, shows a list of all available commands.\n"
        "    With a command name, shows detailed help for that command."
    ),
    multiline=True,
)


_DESCRIPTION_HELP = msg("tui.commands.description.help", fallback="Open the user guide")


class SlashCommandActionPort(Protocol):
    """Semantic actions used by slash command definitions."""

    def available_themes(self) -> list[str]: ...
    def current_theme(self) -> str: ...
    def set_theme(self, name: str) -> bool: ...
    def open_theme_picker(self) -> None: ...
    def available_languages(self) -> list[tuple[str, Content]]: ...
    def current_language(self) -> str: ...
    def set_language(self, requested_locale: str) -> None: ...
    def open_language_picker(self) -> None: ...
    def unknown_language_warning(self, requested_locale: str) -> str: ...
    def debug(self, key: str, message: str = "") -> None: ...
    def create_new_session(self) -> None: ...
    def clear_current_session(self) -> None: ...
    def quit(self) -> None: ...
    def resume_last_session(self) -> None: ...
    def fork_current_session(self) -> None: ...
    def ask_side_question(self, question: str, /) -> None: ...
    def browse_sessions(self) -> None: ...
    def open_workflows(self) -> None: ...
    def open_guide(self) -> None: ...
    def rename_session(self, arg: str = "") -> None: ...
    def chdir(self, arg: str) -> None: ...
    def copy_agent_responses(self, arg: str) -> None: ...
    def toggle_fold(self) -> None: ...
    def show_diff(self) -> None: ...
    def show_rollback(self, arg: str = "") -> None: ...
    def current_approval_mode(self) -> str: ...
    def set_approval_mode(self, arg: str) -> None: ...
    def open_model_config(self) -> None: ...
    def open_agent_config(self) -> None: ...
    def open_agent_config_tab(self, tab: str) -> None: ...
    def runtime_details(self) -> None: ...
    def usage(self) -> None: ...
    def open_settings(self, tab: str) -> None: ...
    def show_man_pages(self, pages: list[ManPageSpec], *, start_index: int = 0) -> None: ...
    def notify_warning(
        self,
        message: MessageRef | str,
        *,
        title: MessageRef | str = INVALID_COMMAND_TITLE_REF,
        timeout: float | None = 3,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SlashCommandActions:
    """Callable-backed slash-command actions for the main screen."""

    list_themes: Callable[[], list[str]]
    get_theme: Callable[[], str]
    apply_theme: Callable[[str], bool]
    pick_theme: Callable[[], None]
    list_languages: Callable[[], list[tuple[str, Content]]]
    get_language: Callable[[], str]
    apply_language: Callable[[str], None]
    pick_language: Callable[[], None]
    render_unknown_language_warning: Callable[[str], str]
    debug_event: Callable[[str, str], None]
    new_session: Callable[[], object]
    clear_session: Callable[[], None]
    quit_app: Callable[[], None]
    resume_session: Callable[[], object]
    fork_session: Callable[[], object]
    ask_side_question: Callable[[str], None]
    browse_session_list: Callable[[], None]
    workflow_selection: Callable[[], None]
    open_guide: Callable[[], None]
    edit_session_title: Callable[[], None]
    apply_session_title: Callable[[str], None]
    change_directory: Callable[[str], object]
    copy_conversation: Callable[[str], None]
    fold_tools: Callable[[], None]
    open_diff: Callable[[], None]
    open_rollback: Callable[[str], None]
    get_approval_mode: Callable[[], str]
    change_approval_mode: Callable[[str], object]
    configure_model: Callable[[], None]
    configure_agent: Callable[[], None]
    configure_agent_tab: Callable[[str], None]
    show_runtime_details: Callable[[], None]
    show_usage: Callable[[], None]
    configure_settings: Callable[[str], None]
    show_manual_pages: Callable[[list[ManPageSpec], int], None]
    warn: Callable[[MessageRef | str, MessageRef | str, float | None], None]

    def open_workflows(self) -> None:
        self.workflow_selection()

    def available_themes(self) -> list[str]:
        return self.list_themes()

    def current_theme(self) -> str:
        return self.get_theme()

    def set_theme(self, name: str) -> bool:
        return self.apply_theme(name)

    def open_theme_picker(self) -> None:
        self.pick_theme()

    def available_languages(self) -> list[tuple[str, Content]]:
        return self.list_languages()

    def current_language(self) -> str:
        return self.get_language()

    def set_language(self, requested_locale: str) -> None:
        self.apply_language(requested_locale)

    def open_language_picker(self) -> None:
        self.pick_language()

    def unknown_language_warning(self, requested_locale: str) -> str:
        return self.render_unknown_language_warning(requested_locale)

    def debug(self, key: str, message: str = "") -> None:
        self.debug_event(key, message)

    def create_new_session(self) -> None:
        self.new_session()

    def clear_current_session(self) -> None:
        self.clear_session()

    def quit(self) -> None:
        self.quit_app()

    def resume_last_session(self) -> None:
        self.resume_session()

    def fork_current_session(self) -> None:
        self.fork_session()

    def browse_sessions(self) -> None:
        self.browse_session_list()

    def rename_session(self, arg: str = "") -> None:
        title = arg.strip()
        if title:
            self.apply_session_title(title)
        else:
            self.edit_session_title()

    def chdir(self, arg: str) -> None:
        self.change_directory(arg)

    def copy_agent_responses(self, arg: str) -> None:
        self.copy_conversation(arg)

    def toggle_fold(self) -> None:
        self.fold_tools()

    def show_diff(self) -> None:
        self.open_diff()

    def show_rollback(self, arg: str = "") -> None:
        self.open_rollback(arg)

    def current_approval_mode(self) -> str:
        return self.get_approval_mode()

    def set_approval_mode(self, arg: str) -> None:
        self.change_approval_mode(arg)

    def open_model_config(self) -> None:
        self.configure_model()

    def open_agent_config(self) -> None:
        self.configure_agent()

    def open_agent_config_tab(self, tab: str) -> None:
        self.configure_agent_tab(tab)

    def runtime_details(self) -> None:
        self.show_runtime_details()

    def usage(self) -> None:
        self.show_usage()

    def open_settings(self, tab: str) -> None:
        self.configure_settings(tab)

    def show_man_pages(self, pages: list[ManPageSpec], *, start_index: int = 0) -> None:
        self.show_manual_pages(pages, start_index)

    def notify_warning(
        self,
        message: MessageRef | str,
        *,
        title: MessageRef | str = INVALID_COMMAND_TITLE_REF,
        timeout: float | None = 3,
    ) -> None:
        self.warn(message, title, timeout)


class MainSlashCommandRegistry:
    """Build slash command definitions from semantic action ports."""

    def __init__(
        self,
        *,
        actions: SlashCommandActionPort,
        buddy: BuddyCommandController,
        render_message: Callable[[MessageRef], str] = format_message,
    ) -> None:
        self._actions = actions
        self._buddy = buddy
        self._render_message = render_message
        self._slash_commands: list[SlashCommandDef] = []

    @property
    def commands(self) -> list[SlashCommandDef]:
        return self._slash_commands

    def build(self) -> list[SlashCommandDef]:
        """Build slash command list with bound actions."""
        actions = self._actions

        def _available_themes() -> list[tuple[str, str]]:
            current = actions.current_theme()
            return [(theme, f"{'● ' if theme == current else '  '}{theme}") for theme in actions.available_themes()]

        def _set_theme(arg: str) -> None:
            name = arg.strip()
            if not name:
                actions.open_theme_picker()
                return
            if name in actions.available_themes() and actions.set_theme(name):
                actions.debug("ThemeChanged", name)

        def _available_languages() -> list[tuple[str, Content]]:
            current = actions.current_language()
            return [
                (requested_locale, Content.assemble("● " if requested_locale == current else "  ", label))
                for requested_locale, label in actions.available_languages()
            ]

        def _set_language(arg: str) -> None:
            requested_locale = arg.strip()
            if not requested_locale:
                actions.open_language_picker()
                return
            if requested_locale in {value for value, _label in actions.available_languages()}:
                actions.set_language(requested_locale)
                return
            actions.notify_warning(actions.unknown_language_warning(requested_locale))

        def _approval_subcommands() -> list[tuple[str, Content]]:
            current = actions.current_approval_mode()
            modes = [
                ("manual", _APPROVAL_MANUAL.bind()),
                ("auto", MODE_AUTO_DESCRIPTION.bind()),
                ("bypass", MODE_BYPASS_DESCRIPTION.bind()),
            ]
            return [
                (
                    value,
                    Content.assemble(
                        f"{'● ' if value == current else '  '}{value.capitalize()}  ",
                        (self._render_message(reference), "dim"),
                    ),
                )
                for value, reference in modes
            ]

        def _agents_subcommands() -> list[tuple[str, str]]:
            return [
                ("basic", self._render_message(_AGENTS_TARGET_BASIC.bind())),
                ("instructions", self._render_message(_AGENTS_TARGET_INSTRUCTIONS.bind())),
                ("tools", self._render_message(_AGENTS_TARGET_TOOLS.bind())),
                ("sub-agents", self._render_message(_AGENTS_TARGET_SUB_AGENTS.bind())),
                ("skills", self._render_message(_AGENTS_TARGET_SKILLS.bind())),
                ("mcp", self._render_message(_AGENTS_TARGET_MCP.bind())),
                ("memory", self._render_message(_AGENTS_TARGET_MEMORY.bind())),
                ("compaction", self._render_message(_AGENTS_TARGET_COMPACTION.bind())),
            ]

        def _open_agents(arg: str) -> None:
            value = arg.strip().lower()
            if not value:
                actions.open_agent_config()
                return
            tab_map = {
                "basic": "basic",
                "instructions": "instructions",
                "tools": "tools",
                "sub-agents": "sub-agents",
                "subagents": "sub-agents",
                "skill": "skills",
                "skills": "skills",
                "mcp": "mcp",
                "memory": "memory",
                "compaction": "compaction",
            }
            tab = tab_map.get(value)
            if tab:
                actions.open_agent_config_tab(tab)
                return
            actions.notify_warning(_UNKNOWN_AGENTS_TARGET.bind(value=value))

        def _settings_subcommands() -> list[tuple[str, str]]:
            from chrys.app.tui.screens.settings import TABS

            return [(tab.id, self._render_message(tab.title.bind())) for tab in TABS]

        def _open_settings(arg: str) -> None:
            from chrys.app.tui.screens.settings import GENERAL_TAB_ID, TAB_IDS

            value = arg.strip().lower()
            if not value:
                actions.open_settings(GENERAL_TAB_ID)
                return
            if value in TAB_IDS:
                actions.open_settings(value)
                return
            actions.notify_warning(_UNKNOWN_SETTINGS_TAB.bind(value=value))

        def _show_man_page(arg: str) -> None:
            cmd_name = arg.strip().lower()
            if not cmd_name:
                available = tuple(
                    (f"  /{cmd.name:12} - ", cmd.description) for cmd in self._slash_commands if not cmd.hidden
                )
                actions.show_man_pages(
                    [
                        ManPageSpec(
                            "man",
                            (
                                ManPageHeading(_MAN_HEADING_NAME.bind()),
                                ManPageProseBlock(_MAN_INDEX_NAME.bind(app=APP_DISPLAY_NAME)),
                                ManPageVerbatimBlock(""),
                                ManPageHeading(_MAN_HEADING_DESCRIPTION.bind()),
                                ManPageProseBlock(_MAN_INDEX_DESCRIPTION.bind(app=APP_DISPLAY_NAME)),
                                ManPageVerbatimBlock(""),
                                ManPageHeading(_MAN_HEADING_AVAILABLE_COMMANDS.bind()),
                                ManPageRows(available),
                                ManPageVerbatimBlock(""),
                                ManPageHeading(_MAN_HEADING_SEE_ALSO.bind()),
                                ManPageRows((("    /man <command>  ", _MAN_INDEX_SEE_ALSO.bind()),)),
                                ManPageVerbatimBlock(""),
                            ),
                        )
                    ]
                )
                return

            target_cmd = None
            for cmd in self._slash_commands:
                if cmd.name == cmd_name or cmd_name in cmd.aliases:
                    target_cmd = cmd
                    break

            if not target_cmd:
                actions.notify_warning(_UNKNOWN_COMMAND.bind(cmd_name=cmd_name), title=_MAN_PAGE_TITLE.bind())
                return

            def _build_page(cmd: SlashCommandDef) -> ManPageSpec:
                body = cmd.man_page
                if body is None:
                    body_segments: tuple[ManPageSegment, ...] = (ManPageProseBlock(cmd.description),)
                elif isinstance(body, MessageRef):
                    body_segments = (ManPageProseBlock(body),)
                else:
                    body_segments = body
                synopsis = cmd.synopsis or f"/{cmd.name}{' [args]' if cmd.subcommands else ''}"
                aliases: ManPageSegment = (
                    ManPageVerbatimBlock(", ".join(f"/{alias}" for alias in cmd.aliases))
                    if cmd.aliases
                    else ManPageRows((("    ", _MAN_ALIASES_NONE.bind()),))
                )
                options = (
                    ManPageRows(cmd.options_help, indent=4)
                    if cmd.options_help
                    else ManPageRows(
                        (
                            (
                                "    ",
                                (
                                    _MAN_SUPPORTS_SUBCOMMANDS.bind()
                                    if cmd.subcommands
                                    else _MAN_NO_ADDITIONAL_OPTIONS.bind()
                                ),
                            ),
                        )
                    )
                )
                return ManPageSpec(
                    cmd.name,
                    (
                        ManPageHeading(_MAN_HEADING_NAME.bind()),
                        ManPageRows(((f"    /{cmd.name} - ", cmd.description),)),
                        ManPageVerbatimBlock(""),
                        ManPageHeading(_MAN_HEADING_SYNOPSIS.bind()),
                        ManPageVerbatimBlock(synopsis),
                        ManPageVerbatimBlock(""),
                        ManPageHeading(_MAN_HEADING_DESCRIPTION.bind()),
                        *body_segments,
                        ManPageVerbatimBlock(""),
                        ManPageHeading(_MAN_HEADING_ALIASES.bind()),
                        aliases,
                        ManPageVerbatimBlock(""),
                        ManPageHeading(_MAN_HEADING_OPTIONS.bind()),
                        options,
                        ManPageVerbatimBlock(""),
                    ),
                )

            visible = [cmd for cmd in self._slash_commands if not cmd.hidden]
            pages = [_build_page(cmd) for cmd in visible]
            start_index = next((index for index, cmd in enumerate(visible) if cmd is target_cmd), 0)
            actions.show_man_pages(pages, start_index=start_index)

        def _man_subcommands() -> list[tuple[str, str]]:
            return [
                (cmd.name, self._render_message(_MAN_SUBCOMMAND.bind(name=cmd.name)))
                for cmd in self._slash_commands
                if not cmd.hidden
            ]

        from chrys.app.tui.widgets.workflow.text import COMMAND

        self._slash_commands = [
            SlashCommandDef(
                "new",
                _DESCRIPTION_NEW.bind(),
                action=lambda _: actions.create_new_session(),
                man_page=_MAN_NEW_BODY.bind(app=APP_DISPLAY_NAME),
            ),
            SlashCommandDef(
                "clear",
                _DESCRIPTION_CLEAR.bind(),
                action=lambda _: actions.clear_current_session(),
                man_page=_MAN_CLEAR_BODY.bind(app=APP_DISPLAY_NAME),
            ),
            SlashCommandDef(
                "exit",
                _DESCRIPTION_EXIT.bind(app=APP_DISPLAY_NAME),
                action=lambda _: actions.quit(),
                aliases=["quit"],
                man_page=_MAN_EXIT_BODY.bind(app=APP_DISPLAY_NAME),
            ),
            SlashCommandDef(
                "resume",
                _DESCRIPTION_RESUME.bind(),
                action=lambda _: actions.resume_last_session(),
                man_page=_MAN_RESUME_BODY.bind(app=APP_DISPLAY_NAME),
            ),
            SlashCommandDef(
                "fork",
                _DESCRIPTION_FORK.bind(),
                action=lambda _: actions.fork_current_session(),
                man_page=_MAN_FORK_BODY.bind(),
            ),
            SlashCommandDef(
                "btw",
                _DESCRIPTION_BTW.bind(),
                action=actions.ask_side_question,
                allow_while_running=True,
                synopsis="/btw [question]",
                man_page=_MAN_BTW_BODY.bind(),
            ),
            SlashCommandDef(
                "rename",
                _DESCRIPTION_RENAME.bind(),
                action=actions.rename_session,
                synopsis="/rename [title]",
                man_page=(
                    ManPageProseBlock(_MAN_RENAME_BODY.bind()),
                    ManPageVerbatimBlock(""),
                    ManPageProseBlock(_MAN_EXAMPLES_LABEL.bind()),
                    ManPageVerbatimBlock("/rename\n/rename Login bug fix", indent=8),
                ),
            ),
            SlashCommandDef(
                "sessions",
                _DESCRIPTION_SESSIONS.bind(),
                action=lambda _: actions.browse_sessions(),
                man_page=_MAN_SESSIONS_BODY.bind(),
            ),
            SlashCommandDef(
                "theme",
                _DESCRIPTION_THEME.bind(),
                action=_set_theme,
                subcommands=_available_themes,
                initial=actions.current_theme,
                man_page=_MAN_THEME_BODY.bind(app=APP_DISPLAY_NAME),
            ),
            SlashCommandDef(
                "language",
                _DESCRIPTION_LANGUAGE.bind(),
                action=_set_language,
                subcommands=_available_languages,
                initial=actions.current_language,
                synopsis="/language [locale]",
                man_page=_MAN_LANGUAGE_BODY.bind(app=APP_DISPLAY_NAME),
            ),
            SlashCommandDef(
                "chdir",
                _DESCRIPTION_CHDIR.bind(),
                action=actions.chdir,
                aliases=["cd"],
                man_page=(
                    ManPageProseBlock(_MAN_CHDIR_BODY.bind(app=APP_DISPLAY_NAME)),
                    ManPageVerbatimBlock(""),
                    ManPageProseBlock(_MAN_EXAMPLES_LABEL.bind()),
                    ManPageVerbatimBlock("/chdir /path/to/project\n/cd ~/Documents", indent=8),
                ),
            ),
            SlashCommandDef(
                "copy",
                _DESCRIPTION_COPY.bind(),
                action=actions.copy_agent_responses,
                allow_while_running=True,
                synopsis=textwrap.dedent("""\
                    /copy [N]
                    /copy agent [N|all]
                    /copy user [N|all]
                    /copy all
                """),
                options_help=(
                    ("agent [N|all]  ", _MAN_OPTION_COPY_AGENT.bind()),
                    ("user [N|all]   ", _MAN_OPTION_COPY_USER.bind()),
                    ("all            ", _MAN_OPTION_COPY_ALL.bind()),
                    ("N              ", _MAN_OPTION_COPY_COUNT.bind()),
                ),
                man_page=(
                    ManPageProseBlock(_MAN_COPY_BODY.bind()),
                    ManPageVerbatimBlock(""),
                    ManPageProseBlock(_MAN_EXAMPLES_LABEL.bind()),
                    ManPageVerbatimBlock(
                        "/copy\n/copy 3\n/copy agent 3\n/copy user 3\n/copy agent all\n/copy user all\n/copy all",
                        indent=8,
                    ),
                    ManPageVerbatimBlock(""),
                    ManPageProseBlock(_MAN_COPY_USEFUL_FOR.bind()),
                ),
            ),
            SlashCommandDef(
                "fold",
                _DESCRIPTION_FOLD.bind(),
                action=lambda _: actions.toggle_fold(),
                man_page=_MAN_FOLD_BODY.bind(),
            ),
            SlashCommandDef(
                "diff",
                _DESCRIPTION_DIFF.bind(),
                action=lambda _: actions.show_diff(),
                allow_while_running=True,
                man_page=_MAN_DIFF_BODY.bind(),
            ),
            SlashCommandDef(
                "rollback",
                _DESCRIPTION_ROLLBACK.bind(),
                action=actions.show_rollback,
                synopsis=textwrap.dedent("""\
                    /rollback
                    /rollback N
                    /rollback to N
                """),
                options_help=(
                    ("N       ", _MAN_OPTION_ROLLBACK_COUNT.bind()),
                    ("to N    ", _MAN_OPTION_ROLLBACK_TARGET.bind()),
                ),
                man_page=_MAN_ROLLBACK_BODY.bind(),
            ),
            SlashCommandDef(
                "approval",
                _DESCRIPTION_APPROVAL.bind(),
                action=actions.set_approval_mode,
                subcommands=_approval_subcommands,
                initial=actions.current_approval_mode,
                allow_while_running=True,
                man_page=(
                    ManPageProseBlock(_MAN_APPROVAL_BODY.bind()),
                    ManPageRows(
                        (
                            ("    manual  - ", _APPROVAL_MANUAL.bind()),
                            ("    auto    - ", MODE_AUTO_DESCRIPTION.bind()),
                            ("    bypass  - ", MODE_BYPASS_DESCRIPTION.bind()),
                        ),
                        indent=8,
                    ),
                    ManPageVerbatimBlock(""),
                    ManPageRows((("", _MAN_APPROVAL_MODE_HINT.bind()),), indent=8),
                ),
            ),
            SlashCommandDef(
                "models",
                _DESCRIPTION_MODELS.bind(),
                action=lambda _: actions.open_model_config(),
                allow_while_running=True,
                man_page=_MAN_MODELS_BODY.bind(),
            ),
            SlashCommandDef(
                "buddy",
                _DESCRIPTION_BUDDY.bind(),
                action=self._buddy.handle,
                subcommands=self._buddy.subcommands,
                man_page=_MAN_BUDDY_BODY.bind(),
            ),
            SlashCommandDef(
                "agents",
                _DESCRIPTION_AGENTS.bind(),
                action=_open_agents,
                subcommands=_agents_subcommands,
                aliases=["config", "agent"],
                allow_while_running=True,
                man_page=_MAN_AGENTS_BODY.bind(),
            ),
            SlashCommandDef(
                "runtime",
                _DESCRIPTION_RUNTIME.bind(),
                action=lambda _: actions.runtime_details(),
                aliases=["details"],
                allow_while_running=True,
                man_page=_MAN_RUNTIME_BODY.bind(),
            ),
            SlashCommandDef(
                "usage",
                _DESCRIPTION_USAGE.bind(),
                action=lambda _: actions.usage(),
                allow_while_running=True,
                man_page=_MAN_USAGE_BODY.bind(),
            ),
            SlashCommandDef(
                "settings",
                _DESCRIPTION_SETTINGS.bind(),
                action=_open_settings,
                subcommands=_settings_subcommands,
                man_page=_MAN_SETTINGS_BODY.bind(),
            ),
            SlashCommandDef(
                "workflow",
                COMMAND.bind(),
                action=lambda _arg: actions.open_workflows(),
                allow_while_running=False,
                synopsis="/workflow",
            ),
            SlashCommandDef(
                "help",
                _DESCRIPTION_HELP.bind(),
                action=lambda _: actions.open_guide(),
                allow_while_running=True,
                synopsis="/help",
            ),
            SlashCommandDef(
                "man",
                _DESCRIPTION_MAN.bind(),
                action=_show_man_page,
                subcommands=_man_subcommands,
                allow_while_running=True,
                man_page=(
                    ManPageProseBlock(_MAN_MAN_BODY.bind(app=APP_DISPLAY_NAME)),
                    ManPageVerbatimBlock(""),
                    ManPageRows((("", _MAN_EXAMPLES_LABEL.bind()),), indent=8),
                    # Command syntax stays untranslated in the prefixes (with
                    # their layout padding); only the explanations localize.
                    ManPageRows(
                        (
                            ("/man              ", _MAN_EXAMPLE_SHOW_ALL.bind()),
                            ("/man theme        ", _MAN_EXAMPLE_SHOW_THEME.bind()),
                            ("/man diff         ", _MAN_EXAMPLE_SHOW_DIFF.bind()),
                        ),
                        indent=12,
                    ),
                ),
            ),
        ]
        return self._slash_commands
