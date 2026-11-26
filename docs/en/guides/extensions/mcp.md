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
3. Leave "Working Directory" blank to start the server in the [current session's working directory](../daily-use/workspaces.md), which is the primary folder in a named workspace. If the server needs to start in another folder to find its scripts or configuration, enter that folder here (`cwd` in the agent YAML).
4. Add any environment variables the process needs. Enter values directly or use `{{ENV_VAR}}` to reference environment variables set before starting iCode.

An STDIO connection starts the specified program on the local machine. If the startup command downloads or runs third-party packages or scripts, confirm that their sources are trustworthy and review the relevant content first.

The program also receives most of the environment variables iCode was started with, such as `PATH`, proxy settings and any tokens set in that terminal. If a server must not see a credential, start iCode without that variable.

#### Choose a working directory

iCode ignores leading and trailing whitespace in this field, including in YAML. A value containing only whitespace is treated as blank.

| What you enter | Where the server starts |
| --- | --- |
| Blank | The current session's working directory |
| `tools/mcp` | The `tools/mcp` folder inside the session's working directory |
| `~/mcp-server` | The `mcp-server` folder inside your home folder |
| An absolute path, such as `/opt/mcp-server` or `D:\mcp-server` | The specified folder |

For example, with the session's working directory at `/work/demo`, set the server's "Working Directory" to `tools/mcp` and "Command" to `python server.py` to run `/work/demo/tools/mcp/server.py` from `/work/demo/tools/mcp`. That folder and script must already exist, and `python` must be available on `PATH`.

Relative paths can use `..` to point outside the session's working directory, for example `../shared-mcp`.

After you change the session's working directory with `/chdir`, a blank Working Directory setting uses the new directory, and relative paths start from it. For example, if you enter `tools/mcp` and switch from working directory A to working directory B, the server uses working directory B's `tools/mcp` folder. Locations specified with `~/` or an absolute path stay the same.

If the command is a relative path such as `./bin/server`, iCode looks for the program in the server's working directory. If you enter only a program name such as `python`, iCode searches `PATH`.

On Windows, include the slash after a drive letter in both Working Directory and the command: write `D:\server` and `D:\server\server.exe`, not `D:server` or `D:server\server.exe`.

Working Directory can use `{{ENV_VAR}}` to reference environment variables set before starting iCode, for example `{{MCP_HOME}}/weather`. A missing or empty variable fails that server's connection. `{{!command}}` and shell syntaxes such as `$HOME` or `%APPDATA%` are not supported; use `~/` for your home folder.

## Import servers from other applications

You can copy MCP server configurations from Claude Code, ChatGPT, OpenCode or Pi to the current agent. Import is a one-time copy. Later changes to the source configuration do not sync to iCode.

The "ChatGPT" entry reads the local MCP configuration shared by the ChatGPT desktop app, Codex CLI and the Codex IDE extension. Connections configured in ChatGPT on the web are not included.

1. In `/agents mcp`, click "Import from Other Apps" next to "+ Add".
2. Select an application on the left, then click a server's name to expand its card and review its settings and warnings.
3. Tick the servers to import, then click "Import". Servers from several applications can be ticked and imported together; the left panel shows how many are ticked in each application.
4. Review the server list in the confirmation window, then click "Import" to confirm or "Back" to change the selection. Confirming adds the cards as unsaved changes.
5. The new cards appear in the "MCP" tab. Adjust any settings flagged in the warnings, then follow the steps in [Test and save](#test-and-save).

The mark after a server's name shows its status. Servers that cannot be ticked have no checkbox, but clicking the name still expands the card with the details.

| Mark | Meaning and action |
| --- | --- |
| ⚠ with a number | That many warnings. Expand the card to read them. |
| Already added | The current agent already has this configuration, so there is nothing to import. |
| Duplicate | The server has the same name as a ticked server or meets the connection conflict conditions below. Hover over the mark to see which server conflicts. To pick this one, untick the other one first. |
| Can't import | Expand the card for the reason, and see [When a server cannot be imported](#when-a-server-cannot-be-imported). |
| Name in use | The current agent has a configuration with the same name but different settings. Servers cannot be renamed in the import window. To keep both, change "Server Name" on the existing card in the "MCP" tab, or rename the server in the original application's configuration, then reopen the import window. |
| Connection in use | A configuration in the current agent uses the same connection, so the tools may overlap. Hover over the mark to see that configuration's name, then compare the two as described below. |

"Connection in use" appears when two configurations have the same HTTP URL, or the same STDIO command and arguments, as well as the same "Tool Name Prefix" and overlapping "Available Tool Scope" values. A configuration set to "No tools" overlaps with none. Working directories, environment variables and headers are not compared, so two configurations with this mark may still connect to different servers: compare with the existing card. To keep two configurations for the same connection, give them different server names and use either different tool name prefixes or non-overlapping tool scopes.

### Check before use

The import window flags settings that iCode may handle differently. After import, the card's "Description" shows the warnings and the source file path. Test the features you need even if there are no warnings. You can remove the warnings from "Description" after addressing them.

Codex administrator policies, OpenCode runtime overrides (`OPENCODE_CONFIG_CONTENT` and `OPENCODE_PERMISSION`), and permissions specific to an OpenCode agent apply only in the source application. If you rely on them, check which servers are enabled and which tools are available before testing or saving.

- **Enabled**: Servers disabled in the imported configuration files, and Claude Code project servers that were rejected or not yet approved, still have "Enabled" unchecked after import. Review the settings, then select "Enabled" and save to use the server.
- **Credentials**: Tokens written directly in the original configuration are copied into the agent's file. Sign-ins made through the other application are not copied, and for servers from Claude Code, neither are headers that reference Claude Code's authentication variables. If the server accepts a token, set "Headers" as the server's documentation describes; header values can also [come from a command](../configuration/models.md#get-an-api-key-from-a-command). A public HTTP server that allows anonymous access needs no "Headers". Servers that support only OAuth sign-in cannot currently be used in iCode.
- **Environment variables**: Imported `{{ENV_VAR}}` references are read when the server is tested or loaded, and defaults set for them in the original configuration are not kept: if a variable is unset or empty, the connection fails and names the variable. Set the variables and restart iCode, or enter fixed values in the fields that reference them. A configuration that references `CLAUDE_PROJECT_DIR`, which Claude Code sets automatically, also needs that variable set. A local server imported from ChatGPT can receive more environment variables in iCode than in Codex, including credentials. Remove any credentials the server should not receive before starting iCode and testing the connection. `PYTHONPATH` and `PYTHONHOME` are the exception: iCode does not pass them to local servers, so if a ChatGPT server is set to pass either on from your environment, import does not copy it and shows a warning. If the server needs one, add it under "Environment Variables" on the card, with a fixed value or a reference such as `{{PYTHONPATH}}`. See [Connect over STDIO](#connect-over-stdio).
- **Tools**: Check "Available Tool Scope" and keep only the tools needed. If a tool restriction could not be carried over, the server is set to "No tools" until tools are chosen again as described in [Tool access](#tool-access). For example, a Claude Code rule that denies one MCP tool can leave the server with "No tools" after import. Import usually fills in "Tool Name Prefix" from the server name. For example, the `search` tool from the `github` server appears as `github_search`. If a selected tool name is too long to fit with a prefix, the prefix may be left blank. You can edit the prefix as needed. See [Naming and limits](#naming-and-limits). If a server imported from Pi warns that tool loading rules were not copied, check "Loading Strategy" and "Initially Visible Tools (optional)" as described in [Tool loading](#tool-loading).
- **Timeouts**: For servers imported from ChatGPT, "MCP Request Timeout (seconds)" uses the original tool timeout. If omitted, it is set to 300 seconds. A source value of `0` uses iCode's default. If "Test" reports a connection timeout, increase this setting and try again.
- **Approval**: Approval rules from the other application are not imported. Imported tools follow iCode's current approval mode and the agent's approval rules. Use `/approval` to change the mode; see [approval modes](../configuration/approval.md).

### Check imported working directories

Once enabled and saved, imported servers are available in every session that uses the current agent. With a blank or relative "Working Directory", an STDIO server's start folder changes with the session folder. To keep the same start folder across sessions, enter an absolute path or one starting with `~/`. See [Choose a working directory](#choose-a-working-directory).

If the path on the card differs from the original, or the test shows an unexpected start folder, check the following cases.

- **`~` paths from ChatGPT or OpenCode**: Codex and OpenCode do not expand `~` in a working directory to the home folder; they treat it as a folder named `~`. iCode does expand a leading `~`, so import writes `~/server` as `./~/server` to keep the original meaning. If the intent was a folder in the home folder, change it to `~/server` or a full path.
- **Relative working directories from ChatGPT**: Codex resolves an explicit relative working directory from the folder where Codex was launched. iCode resolves it from the session folder. Use an absolute path to keep the same start folder.
- **OpenCode paths that reference environment variables**: `{env:VAR}` becomes `{{VAR}}`, whose value is read when the server is tested or loaded. iCode may interpret forms such as `~` in the value differently from OpenCode, and import shows a warning for this. Set the variable before starting iCode, and make sure its value is a full path.

When testing an STDIO server, check "Working directory" in the connection report.

### Where servers are found

If a server is missing from the list, check that it is saved in one of the locations below, then check the trust settings after the table. The search for project configuration starts from the session's working directory. A session in a [named workspace](../daily-use/workspaces.md#workspaces) starts from the workspace's primary folder, and reference folders are not searched. To import project configuration from a reference folder, open a session with that folder as its working directory (for example `icode -C <folder>`), or switch to it in the session with `/chdir` (which leaves the workspace), then import.

| Application | Files read |
| --- | --- |
| Claude Code | `~/.claude.json` (or the one in `CLAUDE_CONFIG_DIR`), for user servers and servers added for the current project (when you work in a Git worktree, the servers added for the repository's main folder); `.mcp.json` in the session folder and at the top of its Git repository |
| ChatGPT | `~/.codex/config.toml` (or the one in `CODEX_HOME`); `.codex/config.toml` in each trusted folder from the project root (by default, the top of the Git repository) down to the session folder |
| OpenCode | `opencode.json`, `opencode.jsonc` or `config.json` in `~/.config/opencode` (or `XDG_CONFIG_HOME/opencode`); the file named by `OPENCODE_CONFIG`; `opencode.json` or `opencode.jsonc` in each folder from the top of the Git repository (outside a repository, from the top of the disk) down to the session folder, in those folders' `.opencode` subfolders, in `~/.opencode` and in the folder named by `OPENCODE_CONFIG_DIR` |
| Pi | `~/.pi/agent/mcp.json` (or the one in `PI_CODING_AGENT_DIR`); `.pi/mcp.json` in the session folder, if that folder is trusted in Pi |

Whether project servers are listed also depends on settings in the original application:

- If **ChatGPT** is missing from the left panel, check that the user-level `config.toml` exists. If only project servers are missing, check in Codex that the project folder (in a Git worktree, the repository's main folder) is trusted.
- If **Pi** project servers are missing, check trust for the session folder or its parents. If no trust decision applies, Pi project configuration is read only when `defaultProjectTrust` is set to `"always"`.
- If **OpenCode** project servers are missing, check whether OpenCode is configured to skip project files. If a server name contains a variable, enter the actual name in the OpenCode configuration and reopen the import window.
- **Claude Code**: `.mcp.json` servers that have not been approved are listed and imported disabled. If the approval is stored in the project's own settings, the project itself must also be trusted in Claude Code; trusting a parent folder is not enough.

When several configuration files of one application define a server with the same name, it is listed once, with the settings that apply to the current project. The file named in "Description" may not be the only one that sets it: if a warning names a setting you cannot find there, check that application's other configuration files. Servers with the same name in different applications are still listed separately.

Servers supplied dynamically by cloud services, plugins or extensions, and not saved in the files above, are not listed. If a Streamable HTTP URL or a local startup command is available, add the server manually with "+ Add" in `/agents mcp`; see [Add an MCP server configuration](#add-an-mcp-server-configuration).

### When a server cannot be imported

Expand a server marked "Can't import" and check "Notes" for the reason and what to do. Here are some common cases.

- **Unsupported connection**: SSE, WebSocket, or access through a remote environment. If a Streamable HTTP URL reachable from this computer, or an STDIO command that runs here, is available, [add the server manually](#add-an-mcp-server-configuration).
- **Variables in a URL, command or argument**: iCode does not expand variables in these fields. When adding the server manually, replace each reference with the variable's value. For example, if `MCP_HOST` is `https://example.com`, write `{env:MCP_HOST}/mcp` as `https://example.com/mcp`.
- **Values read from a file or a command**: such as OpenCode's `{file:...}` or a Pi value starting with `!`. Add the server manually and fill in the content. Environment variable values and header values can also reference an environment variable with `{{ENV_VAR}}` or [come from a command](../configuration/models.md#get-an-api-key-from-a-command).
- **Values iCode cannot keep as written**: for example spaces at the start or end, invisible control characters, unsupported characters in a header name or value, or a Windows path written as `C:tools`. Follow "Notes": most cases are fixed in the original configuration file before reopening the import window, and a few require adding the server manually.

If a server is missing from the list, also check the required fields in the source configuration. For example, a ChatGPT server needs exactly one of `command` and `url`, and a Claude Code HTTP server needs a valid `url`.

If the list shows a file error, check the file's access permissions or fix the reported setting or syntax, then reopen the import window. An error in a project configuration can keep all servers from that application out of the list, including servers you configured globally.

If the import window still says it couldn't search for or load MCP configurations after reopening, the cause is recorded in iCode's log: `~/.chrys/logs/debug.log` on macOS and Linux, or `%APPDATA%\chrys\logs\debug.log` on Windows.

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

For STDIO servers, a successful report shows "Working directory" below the connection command: the start folder the test passed to the server process. With a blank "Working Directory" field, this is the current session's working directory. Environment variables and `~` are already expanded and relative paths are joined to the session's working directory, so the path can be compared directly with the intended folder; symbolic links are not resolved, and `..` may remain. HTTP reports show no working directory.

If the test fails:

- For HTTP servers, check the URL, authentication headers, environment variables, and proxy and TLS settings.
- For an STDIO working directory error, check that the path after `Working directory:` is an existing folder you can enter. If the field is blank, check the current session's working directory. Correct the path or permissions and retry.
- For other STDIO connection errors, inspect the executable, process exit code, and standard error output when shown. Check the command, arguments, and environment variables, and confirm that the command runs in the indicated working directory.

For failures while loading the agent, press **F6** to view iCode's connection error logs. iCode captures only a limited amount of the server's standard error output; for complete server logs, follow the server's own logging instructions. Remove tokens, credentials, and other sensitive information before sharing diagnostics.

## Reconnect with /reload

While an agent and its local sub-agents load, matching MCP configurations share one connection attempt, including a failed attempt. The loading dialog names the sub-agent and MCP server being prepared. A failed server is not retried for each sub-agent during that load; fix the server and use `/reload` to try again. Server names and connection settings must match to share an attempt; the same address alone is not sufficient.

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
