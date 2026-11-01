# TUI slash command reference

Slash commands are shortcuts entered in the terminal user interface (TUI) input field. Use them to manage sessions, switch configurations, view information, and open feature dialogs.

## Enter slash commands

A slash command must start at the beginning of the input, with the command and its arguments on the same line.

- Valid example: `/diff` runs as a command.
- Invalid example: `Please run /diff` does not trigger a command.

After you type `/`, a command suggestion list appears above the input field. Keep typing to filter commands; press **Tab** to complete the selected command. If a command does not show argument suggestions, you can press **Enter** immediately after completing it to run it. If it accepts arguments, you can also continue typing arguments before pressing **Enter**.

The following commands show argument suggestions: `/theme`, `/language`, `/approval`, `/buddy`, `/agents`, `/settings`, and `/man`. The list opens when you complete one of these commands with **Tab**, or when you type the full command and press **Space** or **Enter**.

- Example: type `/approval` and press **Space** to see `Manual`, `Auto`, and `Bypass`.

Once the argument suggestion list appears, use the up and down arrow keys to select an item and press **Enter** to run it, or click the item with the mouse.

## View command help

Enter `/man` to view help for built-in slash commands:

- `/man`: press **Space** or **Enter** to show the list of commands with help available. Select a command to open its detailed help.
- `/man <command>`: open detailed help for a specific command. `command` can be a command name or alias, such as `/man theme`, `/man diff`, or `/man quit`.

## Command reference

The following table lists the built-in slash commands in iCode.

In this table:

- `<arg>` denotes a required argument; `[arg]` denotes an optional argument.
- `|` separates alternative values. For example, `[N|all]` accepts a positive integer or `all`.
- `<`, `>`, `[`, and `]` are argument notation and should not be typed as part of the command.

| Command and usage | Aliases | Description | Options |
| --- | --- | --- | --- |
| `/new` | — | Start a new session and keep the current session. See [Switch to a new session](../guides/daily-use/sessions.md#switch-to-a-new-session). | — |
| `/clear` | — | Delete the current session and start a new one. This cannot be undone after confirmation. See [Switch to a new session](../guides/daily-use/sessions.md#switch-to-a-new-session). | — |
| `/exit` | `/quit` | Exit iCode and return to the terminal. | — |
| `/resume` | — | Restore the most recent chat session; workflow sessions are skipped. See [Resume an existing session](../guides/daily-use/sessions.md#resume-an-existing-session). | — |
| `/fork` | — | Create an independent branch of the current session. After creation, the current window stays in the original session. You can choose to stay there or switch to the branch. | — |
| `/rename [title]` | — | Set or clear the session title. See [Change the session title](../guides/daily-use/sessions.md#change-the-session-title). | `title`: a custom title to apply immediately. Omit it or enter only whitespace to open the title editor. |
| `/sessions` | — | Open the session dialog to browse, restore, or delete sessions. See [Manage and resume sessions](../guides/daily-use/sessions.md). | — |
| `/theme [theme]` | — | Switch the interface theme. See [Appearance](../guides/configuration/settings.md#appearance). | `theme`: switch directly to the specified theme. With no argument, press **Space** or **Enter** to show available themes. |
| `/language [locale]` | — | Switch the interface language. See [Appearance](../guides/configuration/settings.md#appearance). | `locale`: switch directly to the specified language. Supported values are `system` (follow the system), `en` (English), and `zh-Hans` (Simplified Chinese). With no argument, press **Space** or **Enter** to show available values. |
| `/chdir [path]` | `/cd` | Switch the working directory for the current session. See [Switch working directories in a session](../guides/daily-use/workspaces.md#change-the-working-directory-during-a-session). | `path`: an existing target directory, specified as an absolute path or a path relative to the current working directory. Omit it to open the directory picker. |
| `/copy [N]`<br>`/copy agent [N\|all]`<br>`/copy user [N\|all]`<br>`/copy all` | — | Copy conversation content to the clipboard. | No argument: copy the most recent agent message (text the agent shows between tool calls counts as a separate message).<br>`N`: the number of recent agent messages to copy; must be a positive integer. `/copy N` is shorthand for `/copy agent N`.<br>`agent [N\|all]`: copy the most recent `N` agent messages or all agent messages. If no count is given, copy the most recent one.<br>`user [N\|all]`: copy the most recent `N` user messages or all user messages. If no count is given, copy the most recent one.<br>`all`: copy the full conversation history. |
| `/fold` | — | Toggle activity sections, tool call groups, **Thinking** blocks and text between tool calls together. If any are open, collapse them all; otherwise expand them all, including details inside activity sections. Thinking that is still being written and controls waiting for your input are left as they are. | — |
| `/diff` | — | Open the diff view to see files added, modified, and deleted during the current session line by line, so you can review the agent's changes. Files inside the current working directory appear in the directory tree; files outside it appear under **External**, including while a turn is running. | — |
| `/rollback`<br>`/rollback <N>`<br>`/rollback to <N>` | — | Roll back the conversation while keeping current file changes, or roll back the conversation and restore the file changes that iCode can recover. Rollback also stops the [background commands](../guides/configuration/tools.md#commands-that-keep-running-in-the-background) that the discarded turns started. Rollback cannot be undone. | No argument: open the rollback picker, with the most recent available target selected by default. The file restoration option in the lower-left corner of the "Rollback" dialog (its label includes the file count) controls whether file changes are also restored.<br>`N`: immediately discard the most recent `N` turns; must be a positive integer. A turn starts with a user submission and ends when that agent run finishes.<br>`to N`: keep turns 1 through `N` and discard later turns. `N` must be a nonnegative integer; `0` returns to the start of the session.<br>Both `/rollback N` and `/rollback to N` run immediately (iCode asks first only when they would stop running background commands) and restore the file changes from the discarded turns that iCode can recover. |
| `/approval [manual\|auto\|bypass]` | — | Switch the current approval mode. The chosen mode is also saved as the default for the next launch (`bypass` is saved as `auto`). See [Configure approval modes](../guides/configuration/approval.md). | `manual`: the user decides on calls that require approval.<br>`auto`: the approval judge model decides; calls it cannot confirm are safe still go to the user.<br>`bypass`: skip approval for tool calls.<br>No argument: press **Space** or **Enter** to show approval mode suggestions. |
| `/models` | — | Open the model configuration dialog. See [Configure models](../guides/configuration/models.md). | — |
| `/buddy [command]` | — | Manage your Buddy companion. Only `hatch` is available before hatching; the other options become available afterward. | No argument: press **Space** or **Enter** to show available commands.<br>`hatch`: hatch a new companion.<br>`info`: show companion information.<br>`pet`: interact with the companion and generate a response.<br>`mute`: toggle companion notifications.<br>`name <new-name>`: rename the companion to `new-name`. |
| `/agents [target]` | `/agent`<br>`/config` | Open the agent configuration dialog. See [Open the agent configuration window](../guides/configuration/agents.md#open-the-agent-configuration-window). | No argument: press **Space** or **Enter** to show available tabs.<br>`target`: open a specific tab directly. Available values are `basic`, `instructions`, `tools`, `sub-agents`, `skills`, `mcp`, `memory`, and `compaction`.<br>Argument aliases: `subagents` is equivalent to `sub-agents`; `skill` is equivalent to `skills`. |
| `/runtime` | `/details` | Open the "Runtime Details" dialog to view the current model profile and model ID, built-in tools grouped by tool kind, context management tools, sub-agent tools, MCP tools grouped by server, prompt shortcuts grouped by source folder, skills grouped by source, loaded hooks, and preconfigured memory files. | — |
| `/usage` | — | Open the token usage window, with totals, trends and speed across all your sessions. See [Check token usage and speed](../guides/daily-use/sessions.md#check-token-usage-and-speed). | — |
| `/settings [tab]` | — | Open the settings dialog. See [Configure iCode settings](../guides/configuration/settings.md#settings-dialog). | `tab`: open a specific tab directly. Available values are `general`, `models`, `safety`, `sessions`, `tools`, and `notifications`. With no argument, press **Space** or **Enter** to show available tabs. |
| `/workflow` | — | Open Workflow mode and select a workflow. See [Create and run workflows](../guides/running/workflows.md). | — |
| `/help` | — | Open the "iCode User Guide" dialog, which shows this documentation. You can also press **F8** or click `f8 Help` in the footer. | — |
| `/man [command]` | — | Show iCode command help. | No argument: press **Space** or **Enter** to show commands with help available.<br>`command`: a command name or alias. Show the specified command's name, usage, description, aliases, and options. |

## Prompt shortcuts

Prompt shortcuts are prompts you save as Markdown files in `~/.chrys/shortcuts` (Windows: `%APPDATA%\chrys\shortcuts`) or `.chrys/shortcuts` in your project. They are listed in the “Prompt Shortcuts” group after you type `/`; choosing one, or typing `/name arguments` and pressing **Tab** or **Enter**, fills its prompt into the input field for you to edit and send. To write them, see [Write and use shortcuts](../guides/extensions/shortcuts.md).

When you type a name, built-in commands come first, then skills, then prompt shortcuts.

## Skill commands

Skills loaded at runtime can be invoked using `/skill-name`. When you submit a skill command, iCode asks the agent to use that skill for the current turn's task.

You can optionally add a task description after the skill name. It is submitted along with the skill command, and the skill's instructions determine how it is handled.

For example, with a skill named `review` loaded:

- `/review`: invoke the skill.
- `/review Check the current changes for potential issues`: invoke the skill and describe the task.

Skill commands are not built-in iCode commands. If a skill has the same name as a built-in command, the built-in command takes precedence.

For instructions on installing and using skills, see [Install and use skills](../guides/extensions/skills.md#use-skills).
