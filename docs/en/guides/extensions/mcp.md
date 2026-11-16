# Connect MCP servers

Model Context Protocol (MCP) servers can provide iCode agents with external tools, prompt templates, and usage instructions. This guide explains how to connect HTTP or STDIO MCP servers in the terminal user interface (TUI), configure which tools are available, and control whether prompt templates are loaded and server instructions are included in the model context.

Before connecting an MCP server, confirm that its source is trustworthy. The server may receive tool call arguments and access local files, network services, or account data. Its prompt templates and usage instructions may also affect the agent's behavior.

To give agents tools written as a few Python functions, without running a server, see [Python extensions](./python-extensions.md).

## Add an MCP server configuration

Enter `/agents mcp` in the input field to open the current agent's "MCP" tab. Added server configurations apply only to the current agent; they are not automatically applied to other agents.

Each server has its own card. Saved servers are collapsed and show only their name; click the name to expand or collapse a card. If saving finds a problem, the card with the problem expands. To delete a server, click "✕" on its card and confirm.

Click "+ Add" to reuse an MCP configuration from another agent. When configurations are available, a picker shows their names, source agents, and the executable name or URL host. Select one to copy its complete settings into an expanded card at the top of the list. The copy is independent: later edits or deletion in either agent do not change the other. Disabled configurations stay disabled when copied. The picker uses the latest drafts in the open agent configuration window, including edits you have not saved yet.

Equivalent configurations from several agents appear once with all their sources. Configurations with the same name but different settings appear separately. Selectable configurations appear first, followed by grayed-out configurations; each group is sorted by name. Grayed-out items explain why they cannot be added: the current agent already has that name, or uses the same connection and tool name prefix with overlapping allowed tools. Disabled configurations are included in this check.

The picker does not connect to servers. If loading the agent reports a tool name conflict, follow the error guidance to adjust the affected servers' tool name prefixes or exclude overlapping tools. Testing one server successfully does not rule out conflicts with other servers.

To configure a new server, select "+ New MCP configuration…". If no valid configurations are available from other agents, "+ Add" creates a blank card directly. Invalid source configurations are omitted. Canceling the picker leaves your configuration unchanged. Both copied and new configurations take effect through the configuration page's existing Save flow.

In a new card, fill in the following fields:

1. Enter a "Server Name". This name identifies the server in the current agent's MCP configuration. Server names must be unique within an agent's MCP configuration, and are case-insensitive.
2. Optionally enter a "Description" to note the server's purpose. This description is not provided to the agent as server instructions.

HTTP header values and STDIO environment variable values can both use `{{ENV_VAR}}` to reference environment variables set before starting iCode. Variable names can contain only letters, digits, and underscores, and cannot start with a digit.

Both can also use what a command prints, with `{{!command}}`, such as `Bearer {{!tokenprovider gettoken}}`. See [Get an API key from a command](../configuration/models.md#get-an-api-key-from-a-command) for what the command has to do. An HTTP server gets a new value while iCode runs. An STDIO server keeps the value it started with, and gets a new one only when iCode starts it again. If the command fails the first time, the server fails to connect.

When editing YAML by hand, numeric header and environment variable values are read as text. Write `timeout` as a positive integer without quotes; invalid values must be corrected before saving.

Next, complete the HTTP or STDIO settings according to the server's connection method.

### Connect over HTTP

1. Set "Transport" to `HTTP`.
2. Enter the server URL. The URL must begin with `http://` or `https://`.
3. If the server requires authentication, add names and values under "Headers". Use `{{ENV_VAR}}` to reference environment variables set before starting iCode, for example:

   ```text
   Name: Authorization
   Value: Bearer {{MCP_API_TOKEN}}
   ```

4. If the current network requires this server to connect without using the system proxy, enable "Bypass proxy".
5. Keeping TLS certificate verification enabled is recommended. To connect to a self-signed server you have confirmed is trustworthy, you can enable "Skip TLS verification". When enabled, iCode does not verify the HTTPS server certificate and cannot confirm that it is connecting to the intended server.

HTTP redirects must keep the request method and stay on the same origin (scheme, host, and port); an HTTP-to-HTTPS upgrade on the same host using default ports is also allowed. For POST requests, this permits `307` and `308` redirects, such as adding a trailing slash. If a redirect changes the host or port, downgrades HTTPS to HTTP, or changes a POST into a GET, the connection fails. Configure the intended final server URL directly in that case.

### Connect over STDIO

1. Set "Transport" to `STDIO`.
2. In "Command", enter the program name or path used to start the server, along with any required arguments. The TUI splits this line and saves it into the `command` and `args` fields in the agent YAML. When editing YAML by hand, put the executable in `command` and each argument in `args` as a separate item. On Windows, if `command` is `python` or `python3`, iCode skips the placeholder that only offers Python from the Microsoft Store and runs the next program of that name on `PATH`. python.org's traditional installer adds no `python3`, and puts `python` on `PATH` only if you choose that when installing: otherwise write the full path of its `python.exe`.
3. Leave "Working Directory" blank to start the server in the [current session's working directory](../daily-use/workspaces.md). If the server needs to start in another folder to find its scripts or configuration, enter that folder here (`cwd` in the agent YAML).
4. Add any environment variables the process needs. Enter values directly or use `{{ENV_VAR}}` to reference environment variables set before starting iCode.

An STDIO connection starts the specified program on the local machine. If the startup command downloads or runs third-party packages or scripts, confirm that their sources are trustworthy and review the relevant content first.

#### Choose a working directory

iCode ignores leading and trailing whitespace in this field, including in YAML. A value containing only whitespace is treated as blank.

| What you enter | Where the server starts |
| --- | --- |
| Blank | The current session's working directory |
| `tools/mcp` | The `tools/mcp` folder inside the session's working directory |
| `~/mcp-server` | The `mcp-server` folder inside your home folder |
| An absolute path, such as `/opt/mcp-server` or `D:\mcp-server` | The specified folder |

For example, with the session's working directory at `/project`, set the server's "Working Directory" to `tools/mcp` and "Command" to `python server.py` to run `/project/tools/mcp/server.py` from `/project/tools/mcp`. That folder and script must already exist, and `python` must be available on `PATH`.

Relative paths can use `..` to point outside the session's working directory, for example `../shared-mcp`.

After you change the session's working directory with `/chdir`, a blank Working Directory setting uses the new directory, and relative paths start from it. For example, if you enter `tools/mcp` and switch from project A to project B, the server uses project B's `tools/mcp` folder. Locations specified with `~/` or an absolute path stay the same.

If the command is a relative path such as `./bin/server`, iCode looks for the program in the server's working directory. If you enter only a program name such as `python`, iCode searches `PATH`.

On Windows, include the slash after a drive letter in both Working Directory and the command: write `D:\server` and `D:\server\server.exe`, not `D:server` or `D:server\server.exe`.

Working Directory can use `{{ENV_VAR}}` to reference environment variables set before starting iCode, for example `{{MCP_HOME}}/weather`. A missing or empty variable fails that server's connection. `{{!command}}` and shell syntaxes such as `$HOME` or `%APPDATA%` are not supported; use `~/` for your home folder.

## Import servers from other applications

You can copy MCP servers you already use in Claude Code, Codex, OpenCode or Pi into the current agent. This is a one-time copy: later changes in the other application do not update iCode.

1. In `/agents mcp`, click "Import from Other Apps" next to "+ Add".
2. Select an application on the left. Click a server's name to expand its card and review its settings and warnings.
3. Tick the servers you want, then click "Import". You can select servers from several applications before importing; the left panel shows the number selected in each application.
4. Review the server list in the confirmation window. Click "Import" to confirm, or "Back" to change your selection.
5. Check the newly added cards, fill in or adjust settings as the warnings suggest, click "Test", then "Save". Importing alone does not connect to a server or save anything.

Cards you have expanded stay open when you switch applications or change the interface language, so you can continue checking them.

Some special characters appear as `�` when you preview or edit a configuration. Copying or saving keeps the original characters.

The mark after a server's name tells you what to check. Servers you cannot select have a gray name and no checkbox.

| Mark | Meaning and action |
| --- | --- |
| ⚠ with a number | The number of warnings. Expand the card to read them. |
| Already added | The current agent already has this configuration. You do not need to import it again. |
| Name in use / Connection in use | It conflicts with a configuration in the current agent. Hover over the mark for details. |
| Duplicate | It conflicts with another server you selected. Hover over the mark to see which one, then uncheck one of them. |
| Can't import | Expand the card for the reason, then follow [When a server cannot be imported](#when-a-server-cannot-be-imported). |

### Check before use

- **Warnings (⚠)**: Read the warnings in the card's "Description" first. They may ask you to set environment variables, add authentication details or choose tools again. "Original setting" tells you where to find the setting in the other application's file. Settings iCode does not recognize are not copied, so check that the features you need are still available. You can delete warnings from "Description" once you have addressed them.
- **Enabled**: Servers disabled in the other application still have "Enabled" unchecked after import. The same applies to Claude Code project servers you rejected or have not approved yet. To use one, check its settings and select "Enabled".
- **Scope**: Imported servers are available in every project that uses this agent, even if you originally used them in just one project. For STDIO servers, also check the [working directory](#check-imported-working-directories).
- **Credentials**: Tokens written directly in the other application's configuration are copied into the agent's file. Sign-in done in the other application, such as OAuth, is not copied. iCode does not yet support MCP OAuth sign-in. If the server supports token authentication, configure “Headers” as its documentation describes; header values can also [come from a command](../configuration/models.md#get-an-api-key-from-a-command). If the server only supports OAuth sign-in, it cannot currently be used in iCode. A separate warning identifies authentication headers that were not copied; re-enter them only if the server needs them.
- **Available tools**: Check "Available Tool Scope" and select only the tools you need. Settings that apply to all servers in the other application, such as OpenCode's global `tools`, are not imported. If a warning says a tool restriction could not be copied, the server is set to "No tools" until you choose again.
- **Loading**: If you see a loading warning after importing from Pi, check "Loading Strategy" and "Initially Visible Tools". See [Tool loading](#tool-loading) to choose suitable settings.
- **Timeouts**: After importing from Codex, "MCP Request Timeout" uses the original tool timeout, or iCode's default if the original value was `0`. This setting affects how long both connections and tool calls can wait. If "Test" reports a connection timeout, increase "MCP Request Timeout (seconds)" and retry. Any value you enter must be greater than `0` seconds.
- **Approval**: Approval rules from the other application do not carry over. Imported tools follow iCode's current approval mode and the agent's approval rules. Use `/approval` to choose the current mode; see [approval modes](../configuration/approval.md).

### Check imported working directories

Before saving, make sure "Working Directory" points to the folder the server needs. A blank value uses the current session folder; a relative path such as `tools/mcp` also follows that folder when it changes. To keep using the original folder, the most direct option is to enter its full path without `~` or `..`, for example `/home/me/mcp-server` or `C:\tools\mcp-server`.

After clicking "Test", check the folder and the tools you need as well as the connection result. A successful STDIO report shows "Working directory" below the connection command. On a directory error, look for the path after `Working directory:` in the connection report. Connection errors during agent loading are also available with **F6**. See [Test and save](#test-and-save).

#### Working directory compatibility details

After importing from Codex, OpenCode V1 or Pi, review the copied path in the card's "Working Directory" field. Check these cases in particular:

- **Paths starting with `~`**: Pi's `~` appears as your home folder after import. Codex and OpenCode V1 paths such as `~/server` appear as `./~/server`, pointing to an ordinary folder named `~`. If you want your home folder instead, enter the intended folder's full path.
- **OpenCode environment variables**: `{env:VAR}` appears as `{{VAR}}` after import. Set the variable before starting iCode. If its value contains `~`, or uses symbolic links and `..` on macOS or Linux, it may point to a different folder after import; use the intended folder's full path. A path such as `{env:ROOT}/../srv` cannot be imported. Add the server manually with the full path.
- **Windows paths**: Use a full path such as `C:\tools`. A path such as `C:tools` or `C:` cannot be imported. If you use an environment variable, check that its value is a full path too.

**Take extra care with relative paths imported from Codex.** For example, starting Codex in `/launch` with `codex -C /project` makes `cwd = "tools"` use `/launch/tools`. After importing into an iCode session whose working directory is `/project`, `tools` uses `/project/tools`. To keep using the original folder, enter `/launch/tools`. Check `cwd = "."` too: it changes from the folder where you started Codex to the current iCode session folder.

### Where servers are found

If a server is missing from the list, check that it is saved in one of these locations, then check the project trust settings below.

| Application | Files read |
| --- | --- |
| Claude Code | `~/.claude.json` (or the one in `CLAUDE_CONFIG_DIR`), for your servers and those added for the current project (in a Git worktree, the project is the repository's main folder, as in Claude Code); `.mcp.json` in the current folder and at the top of its Git repository |
| Codex | `~/.codex/config.toml` (or the one in `CODEX_HOME`); `.codex/config.toml` in each folder from the top of the Git repository down to the current folder that you trust in Codex |
| OpenCode | `opencode.json`, `opencode.jsonc` or `config.json` in `~/.config/opencode`; the file named by `OPENCODE_CONFIG`; `opencode.json` or `opencode.jsonc` in each folder from the top of the Git repository (outside a repository, from the top of the disk) down to the current folder, in those folders' `.opencode` folders, in `~/.opencode` and in the folder named by `OPENCODE_CONFIG_DIR` |
| Pi | `~/.pi/agent/mcp.json` (or the one in `PI_CODING_AGENT_DIR`); `.pi/mcp.json` in the current folder, if you trust it in Pi |

Project configurations also depend on your choices in the original application:

- **Codex and Pi**: Project servers appear in the import list only if you trust that folder in the original application.
- **Claude Code**: Servers from `.mcp.json` that you have not approved are imported disabled. If approval is saved in the project's own settings, you must also trust that project itself in Claude Code; trusting a parent folder is not enough.

When several files define a server with the same name, the one that applies to the current project is listed. Servers added only by cloud services, plugins or extensions are not listed; add them manually.

### When a server cannot be imported

"Can't import" names the setting that needs attention in the other application's file. Use this table to correct it and reopen import, or add the server manually.

| Cause | What to do |
| --- | --- |
| The server uses SSE or WebSocket, or runs in a remote Codex environment | If it also offers Streamable HTTP or a STDIO command on this computer, add it manually that way. |
| An environment variable in a URL, command or argument | Add the server manually with the full URL or the actual value. |
| A file reference in a setting, such as OpenCode's `{file:...}` | Add the server manually and fill in the needed content. Environment variable values and header values can also [come from a command](../configuration/models.md#get-an-api-key-from-a-command) or reference an environment variable with `{{ENV_VAR}}`. Working Directory supports only a path or an environment variable reference. |
| A value Pi gets by running a command (starting with `!`) | Add the server manually and [get the value from a command](../configuration/models.md#get-an-api-key-from-a-command) in iCode, or enter it directly. |
| A missing value, or one with spaces at either end | Fill in the value or remove the extra spaces in the original configuration file, then reopen import. |
| A tool list with empty entries, duplicate names, spaces around names, or a comma within a name | Remove empty entries and duplicates and trim spaces around names in the original configuration, then import again. Lists containing a tool name with a comma cannot currently be imported. |
| A Codex working directory written as `cwd = ""` | Remove that line to use the session folder, or fill in the folder the server needs, then import again. |
| An OpenCode Working Directory combining an environment variable and `..`, such as `{env:ROOT}/../srv` | Add the server manually with the intended folder's full path, without `~` or `..`, to avoid using a different folder. |
| A Windows working directory written as `C:tools` or `C:` | Use a full path such as `C:\tools`, with a slash after the drive letter's colon. |
| A Working Directory containing line breaks or invisible special characters | Retype the folder path in the original configuration file, then reopen import, or add the server manually and enter the path. |
| An incorrect header name, a value containing unsupported characters such as Chinese text or line breaks, or different values entered for the same header | Check the headers in the original configuration file against the server instructions, then reopen import. Keep one entry per header; names that differ only in case count as the same name. |

If a file cannot be read, the error appears above the list, and servers from the other files are still listed. Check access permissions for an unreadable file, or correct the syntax at the reported line and column, then open import again.

If import still says it couldn't search for or load MCP configurations after you reopen it, the cause is recorded in `logs/debug.log` in iCode's configuration folder: `~/.chrys/` on macOS and Linux, or `%APPDATA%\chrys\` on Windows.

## Configure instructions, tools, and request options

The following settings apply to both HTTP and STDIO servers.

### Server instructions and prompt templates

- **Include server instructions in model context (if available)**: Enabled by default for new configurations. When enabled, server instructions are included in the model context and may affect the agent's behavior.
- **Load server prompt templates as tools (if available)**: Enabled by default for new configurations. When enabled, prompt templates provided by the server are added to the model's tool list as callable tools.

### Tool access

- **Available Tool Scope**: Controls which server tools the agent may use. Choose "All server tools", "Only selected tools", or "No tools".

When selecting "Only selected tools", enter a list of tool names separated by `,`. If you do not yet know which tool names the server provides, select "All server tools" first, then follow the steps in [Test and save](#test-and-save) to view the tool names in the connection report.

### Tool loading

- **Loading Strategy**: Controls when tools become available to the agent. With "Full — load all available tools", every tool within "Available Tool Scope" is available at the start of a task. With "On demand — load tools as needed", only the tools in "Initially Visible Tools (optional)" are available at the start, and the agent can load other tools within "Available Tool Scope" as needed. If a server provides many tools but only a few are used regularly, on-demand loading can reduce the tool information sent to the model at the start of a task.
- **Initially Visible Tools (optional)**: With "On demand — load tools as needed", enter an optional list of tool names separated by `,`. These tools must be included in "Available Tool Scope".

For example:

- If a server provides only `search` and `read_file`, and both are used frequently, choose "Full — load all available tools".
- If a server provides dozens of tools but you usually need only `search` and `read_file`, choose "On demand — load tools as needed" and enter these two tools in "Initially Visible Tools (optional)".

With Claude Opus 5.5, Fable 5.1 and Sonnet 5.5, on-demand loading has a cost. These models tie the thinking they return to the conversation before it, including the tools available to the agent at that point. When the agent loads or unloads a tool, or the next task starts again with only the initially visible tools, that changes, and the model service may leave the earlier thinking out or refuse to read it back. If the service refuses, iCode sends the request once more without the earlier thinking, which costs one extra request. Whether the service leaves the thinking out or iCode resends without it, the model no longer sees that earlier reasoning. With `thinking_block_binding: error`, iCode reports the refusal instead of resending. For the settings involved, see [Claude thinking settings](../configuration/models.md#claude-thinking-settings).

### Naming and limits

- **Tool Name Prefix**: Adds a prefix to this server's tool names, joining the prefix and original name with `_`. For example, the prefix `github` displays the tool `search` as `github_search`. Use a prefix to avoid conflicts when different servers or built-in tools share the same tool name.
- **MCP Request Timeout (seconds)**: Limits how long a single MCP request can wait. Leave blank to use the default shown in the interface.

## Test and save

1. **Enable or disable the server**: To enable an MCP server, keep "Enabled" checked at the bottom left of its configuration card. To keep the configuration but temporarily disable the server, uncheck it. A disabled server does not provide tools, prompt templates, or usage instructions to the agent.
2. **Test the server connection**: Click "Test" at the bottom right of the server configuration card. iCode attempts to connect and displays the server's identity, advertised capabilities, and usage instructions in a connection report, along with the tools and prompt templates available under the current configuration.
3. **Save the configuration**: Click "Save". If you are editing the agent used by the current session, the saved MCP configuration applies to subsequent requests.

For STDIO servers, a successful report shows "Working directory" below the connection command: the folder used to start the test process, including the current session folder when the field is blank. Environment variables, `~`, and relative paths have been expanded. Check this path to confirm that the server started in the intended folder. HTTP reports do not show a working directory.

If the test fails:

- For HTTP servers, check the URL, authentication headers, environment variables, and proxy and TLS settings.
- For an STDIO working directory error, check that the path after `Working directory:` is an existing folder you can enter. If the field is blank, check the current session's working directory. Correct the path or permissions and retry.
- For other STDIO connection errors, inspect the executable, process exit code, and standard error output when shown. Check the command, arguments, and environment variables, and confirm that the command runs in the indicated working directory.

For failures while loading the agent, press **F6** to view iCode's connection error logs. iCode captures only a limited amount of the server's standard error output; for complete server logs, follow the server's own logging instructions. Remove tokens, credentials, and other sensitive information before sharing diagnostics.

## Reconnect with /reload

`/reload` tries a new connection to each enabled MCP server of the current agent and its sub-agents, while the existing connections stay open. The next request uses the tools, prompt templates, and usage instructions each server now reports. Every server has its own result, so a failure on one doesn't affect the others:

| The new connection | What happens | In the summary |
| --- | --- | --- |
| Succeeds | iCode switches to it and closes the old connection | Nothing for this server |
| Fails, the server's configuration is unchanged, and the old connection still responds | iCode keeps using the old connection | `reconnect failed, using the existing connection`, with the reason |
| Fails, and the configuration changed, for example the address, a token, or "Available Tool Scope" | The server is unavailable, even if the old connection still works, so nothing keeps the old token's or scope's access | `is unavailable`, with the reason |
| Fails, and the old connection doesn't respond or there was none | The server is unavailable | `is unavailable`, with the reason |

The reason names only the kind of failure, such as a refused connection or rejected credentials. It never repeats what the server said, because that text can hold a credential; the server's own logs have the details.

An unavailable server's tools are removed from the agent; fix the problem and run `/reload` again. A server you removed or disabled loses its tools and its connection closes, with no reconnect attempt.

Two results are normal:

- A successful reconnect starts a new session on the server, so remote state such as an open browser page or a login can be reset.
- A STDIO server that holds a port or file exclusively may not be able to start beside its old instance, so every reconnect can fail. While the configuration is unchanged and the old connection responds, the summary keeps saying that iCode is using the existing connection.

Saving in the settings or agent dialogs doesn't force a reconnect: a server whose configuration didn't change keeps its connection.

## Verify MCP tool calls

Close the agent configuration window and submit a low-risk task that needs one of the server's tools. For example, if the server provides a read-only search tool, ask the agent to search for a specific piece of data.

The conversation should show the corresponding MCP tool call card and return data from the server. If an "Approval Required" dialog appears, check the tool name and arguments before deciding whether to approve. For approval modes, see [Configure approval modes](../configuration/approval.md).

If a tool returns an image, audio clip, or file that iCode can't decode, the agent receives a short note in place of that item, along with the rest of the result. A link the tool returns reaches the agent as text (its name, address, and description).

If the connection test succeeds but tools are unavailable in the conversation, check that the server is enabled, that the tools appear in the connection report, and that they are included in "Available Tool Scope".
