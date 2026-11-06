# Configure tools

Built-in tools let agents access files, search content, execute shell commands, and use other iCode features. This guide explains how to enable or disable built-in tool kinds for an agent in the terminal user interface (TUI).

Tools provided by MCP servers are not built-in tools and must be managed separately in [MCP configuration](../extensions/mcp.md). Tools from [Python extensions](../extensions/python-extensions.md) are turned on in the same "Tools" tab, under "Extensions".

For the tool kind values and individual tool names used in agent configuration and hooks, see [Tool kinds and names](../../reference/tool-kinds-and-names.md).

## Open tool configuration

Enter `/agents tools` in the input field to open the current agent's "Tools" tab. To configure another agent, select it from the list on the left.

## Select built-in tools

Each option corresponds to a kind of built-in tool. Enable the kinds the agent needs for its tasks:

| Tool kind | Available operations | When to enable |
| --- | --- | --- |
| Filesystem Read | Read files and view images (PNG, JPEG, or WebP) | Enable when the agent needs to analyze project files; the agent can use the image viewing tool only if the selected model supports image input |
| Filesystem Write | Create, overwrite, and precisely edit files | Enable only when the agent needs to modify files |
| File search | Search file contents or find files by name pattern | Enable when the agent needs to locate code and files in a project |
| Web search | Find online URLs and summaries; uses Exa (exa.ai) unless the agent profile names other providers | Enable when the agent needs current external information, such as documentation or release details. Queries go to that outside service and can carry details from your conversation, so mind your data and privacy |
| Web fetch | Read the text of a page by URL | Enable when the agent needs to read pages it finds or that you point it to |
| Shell | Execute terminal commands in a subprocess | Enable when the agent needs to run builds, tests, or other commands |
| Ask User | Request additional information or ask the user to make a choice during a task | Suitable for TUI and ACP sessions; headless CLI mode (`icode run`) does not provide this tool to the agent |
| Document Converter (PDF, Office) | Convert PDF, DOCX, PPTX, XLSX, and XLS files to Markdown; if the current model supports image input and the agent has "Filesystem Read" enabled, images can also be extracted from PDF, DOCX, and PPTX files for the agent to view; XLSX and XLS conversion includes only text and tables | Enable when the agent needs to read these document formats |
| Todo List | Track tasks with multiple steps using a live checklist | Suitable for tasks with many steps that need visible progress tracking |

Every agent also has the `sleep` tool, so it has no option in this list. The agent uses it to pause a task for up to 3600 seconds, and the TUI shows a countdown you can skip.

Check the kinds to enable, uncheck those you do not need, then click "Save".

If you modify the agent used by the current session, the new tool configuration takes effect starting with subsequent requests.

Web search is off in every agent shipped with iCode, and it is separate from File search, which only searches local files. Web fetch is a separate switch. With no search provider configured, queries go to Exa's public search service. Before you turn it on, see [Configure web tools](./web-tools.md) for where requests go, approval, and network limits.

## Use the Ask User tool

The Ask User tool requests additional information or asks the user to make a choice during a task. A single request can contain 1 to 5 questions, including single-select, multi-select, and free-text questions.

By default, questions appear in an ask-user dialog. If the dialog obscures conversation content you need to review, click **Answer Inline** to show the current question in the conversation instead; this applies only to the current question. If you want future questions to appear in the conversation by default, enable **Show questions in the chat** in [Configure iCode settings](./settings.md).

In the TUI, multiple questions appear in the same question dialog. You can switch between question tabs, fill in the answers, and review all answers on the summary page before submitting them together.

The wait time for an answer is controlled by the **Ask-user timeout (seconds)** setting in [Configure iCode settings](./settings.md).

## Configure tools as needed

Tool configuration determines which capabilities an agent can use. For a specialized agent, enable only the tool kinds needed to complete its tasks.

For example:

- An agent that only analyzes code can enable "Filesystem Read" and "File search" without enabling "Filesystem Write" or Shell.
- An agent that modifies code and runs tests typically needs "Filesystem Read", "Filesystem Write", "File search", and Shell.
- When working with an untrusted project, use caution when enabling Shell and "Filesystem Write".

Enabling a tool only makes that capability available to the agent; operations that require approval are still handled according to the current approval mode. For details on approval requests and how to confirm them, see [Configure approval modes](./approval.md).

For filesystem, search, and Shell tools, relative paths are resolved against the current [working directory](../daily-use/workspaces.md), and shell commands run in the current working directory by default. Switching working directories changes the base for these relative paths and the default working directory for commands. The working directory is the default location for operations; it does not restrict tools to paths within it. Whether an operation can run also depends on its tool arguments, whether approval is granted when required, and the permissions of the system user running iCode.

When a command prints a lot, the agent sees the beginning and the end of its output, and iCode saves the output in the session directory when it can. If a command prints more than 32 MiB, iCode keeps only the first and last parts; the middle is not saved anywhere, and the result tells the agent how much was left out.

## Commands that keep running in the background

A shell command that is still running when its time limit is reached moves to the background instead of being stopped, so a development server or a file watcher keeps running while the agent goes on with its work. The agent is told where the command's output is saved and how to stop the command. A session runs at most 32 commands in the background: when 32 already run, a command that reaches its time limit is stopped, and so are programs a finished command left running. To wait for a background command, the agent uses `sleep`, which ends early when one of its background commands ends; the main agent's `sleep` also ends early when you send a new message.

A command can also move to the background before its time limit:

- When it prints nothing and uses almost no CPU for 60 seconds, for example a login command waiting for you to finish signing in with a browser. This happens only when the command's time limit is longer than 60 seconds.
- When its shell has finished but programs it started are still running, for example a server started with `&`. The agent also gets the command's exit code.

A command counts as finished only when every program it started has ended. So a command whose tool leaves a helper running, such as a build server, also moves to the background, and the helper stops when the command is stopped. On Windows, a browser or an editor that a command opened when it was not already running also closes when the command is stopped.

When background commands stop:

- In the TUI and through an ACP client, commands the main agent moved to the background keep running across turns, and the agent is reminded each turn which ones are still running. Commands a sub-agent or a workflow node moved to the background stop when it finishes.
- All of them stop when the session ends: when you start a new session, switch to another session, delete the session, or quit iCode, including by closing the terminal window. With `icode run`, they stop when the run ends.
- Interrupting the agent stops the command it is waiting on, but not the commands already in the background.
- `/rollback` stops the commands that the discarded turns started. It may not restore files that a command changed after it moved to the background.

iCode cannot stop programs that detach themselves from the command that started them, such as services that some tools start with a `-d` option; they keep running after the session ends.

If iCode ends before it can stop its background commands, for example when it crashes or an ACP client ends it outright, the commands keep running on macOS and Linux. The next time you open the TUI, iCode lists the ones still running and asks whether to **Stop** them; choose **Ignore** to leave them running. When an ACP client starts iCode again, iCode stops the ones that earlier ACP sessions left running, without asking. On Windows, the commands end together with iCode.

### See and stop background commands in the TUI

While the main agent waits on a shell command, the command's card shows "ctrl+t moves it to the background". Press **Ctrl+T** to move the main agent's running shell commands to the background right away. If the session already has 32 commands running in the background, iCode tells you, and the command keeps running as before.

The **Tasks** tab of the sidebar shows the todos at the top and the main agent's background commands under **Background Tasks** below them, with each command's elapsed background time and exit code or status; the status bar shows how many are running. Click a command to see what it runs, why it moved to the background, and its output as it grows. There you can **Stop** it, or copy its output or the path of its log file. **Clear ended** removes the commands that have ended from the list.

Before you start a new session (also with `/clear`), switch to another session, quit iCode, or roll back turns that started background commands, iCode lists the commands that would stop and asks first. Choose **Stop and continue** (**Stop and exit** when quitting) to go ahead. If you quit again while iCode is asking, it quits at once. Closing the terminal window stops them without asking.

Stop confirmations show each command in up to two left-aligned lines. The reason sits below on the left, with the runtime aligned right on the same line; when space is tight, the reason takes priority and the runtime is hidden. Long commands keep their beginning and end. Hover over a shortened command to see its full text in a tooltip. The dialog adapts to the terminal size; long lists scroll while the impact explanation and action buttons stay visible. Narrow windows stack the buttons vertically.
