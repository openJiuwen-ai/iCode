# Configure iCode settings

iCode settings control the interface appearance, default agent, sessions, tools, notifications, and some security options. This guide explains how to configure these basics in the terminal user interface (TUI) and check when changes take effect.

To edit `settings.yaml` directly or configure settings through environment variables, see the [iCode settings file reference](../../reference/settings.md) for file locations, settings keys, and source precedence.

## Settings dialog

### Open the Settings dialog

The Settings dialog can only be opened while the agent is idle.

Open **Settings** in any of these ways:

- Press **F10**.
- Click `f10 Settings` at the bottom of the interface.
- Type `/settings` in the input field, press **Space** or **Enter** to show the tab list, then select a tab.

You can also open a tab directly with `/settings <tab>`:

| Tab | `<tab>` | Main settings |
| --- | --- | --- |
| General | `general` | Appearance, input and updates |
| Models & Agents | `models` | Default agent, model roles, and request retries |
| Security | `security` | Approval, project trust, diagnostics, and telemetry |
| Sessions | `sessions` | Session titles, storage, and rollback |
| Tools | `tools` | File search, images, web tools, agent questions, tool detail display, and workspace notices |
| Notifications | `notifications` | Notification toggles, delivery methods, and events |

For example, enter `/settings security` to open the **Security** tab directly.

### Save changes and check when they take effect

Changes are saved automatically as you make them. Press **Enter** to submit a change in a text field; it is also submitted when you move to another setting or close the dialog. Closing the dialog with **Esc** does not undo changes already submitted.

The status message at the bottom right of the Settings dialog explains when changes take effect. If changes have different pending application times, the corresponding messages appear together.

| Status message | When changes take effect |
| --- | --- |
| "Changes are saved as you make them" | Changes are saved, with no changes waiting for the dialog to close or a restart |
| "Changes apply on close (reload)" | After you press **Esc** to close **Settings** |
| "Changes apply when the current turn ends" | Replaces "Changes apply on close (reload)" while a turn or workflow is running; changes apply after you close **Settings** and the run ends |
| A message showing how many changes apply after restart | Changes are saved, but you must quit and restart iCode for them to take effect |

The "Takes effect" column in the following tables describes each setting's usual behavior.

## Settings

### General

#### Appearance

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Theme | Changes the TUI theme. To adjust colors or create a custom theme, see [Customize themes](themes.md). | Immediately |
| UI Language | Changes only iCode interface text, not conversation content, the model's output language, or existing records. You can also switch the interface language with `/language`. | Immediately |

#### Display preferences

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Expand thinking by default | Disabled by default (`ui.chat.show_thinking`). When enabled, new thinking is expanded above the reply while the model writes it, when the model service returns it as readable text. When disabled, it stays on one line, which shows the newest words as they arrive. Either way, it folds to one line showing how the thinking begins once the model moves on to a tool call or its reply. You can click it at any time to read it in full. Thinking the service returns only in encrypted form is never shown. Thinking already in the conversation keeps its state when you change this. | Immediately |
| Expand tool details by default | Disabled by default (`ui.chat.tool_groups_expanded`). When enabled, new tool groups are expanded by default. When disabled, they are collapsed and show only a summary and the current operation. You can click a tool group at any time to expand its details. | Immediately |
| Automatically fold completed turns | Enabled by default (`ui.chat.fold_finished_turns`). After a successful reply, folds thinking, tool calls and intermediate replies under a header that counts the tool calls by kind, such as `12 tool calls: 8 reads · 4 commands`, or **Activity** when there were no tools or the turn is in compressed history. User messages, including mid-turn additions, and the final reply remain visible. Click the header to toggle one section, or type `/fold` to collapse or expand activity sections, tool groups, thinking and text between tool calls with the keyboard. Errors, interruptions and the last turn recovered after a crash stay visible; a turn without a visible final reply is not automatically folded. Existing folds keep your choice. | Future completed turns and sessions you open afterwards |
| Full-color images | Enabled by default (`ui.chat.image_graphics`). In Ghostty, kitty and iTerm2 3.7.5 or later, images in the conversation, such as attached screenshots and images that tools return, are shown in full color at the terminal's own resolution; in WezTerm, VS Code and Windows Terminal 1.22 or later, at 10 by 20 pixels to a character cell. VS Code shows them only with `terminal.integrated.enableImages` on and GPU acceleration (`terminal.integrated.gpuAcceleration`) not turned off. Over SSH, Ghostty, kitty and iTerm2 show them in full color too when `COLORTERM=truecolor` is set on the remote host, for example in its shell profile. To tell which terminal it runs in, iCode asks the terminal as it starts; a terminal that never answers delays the start by up to a second and a half. In other terminals, inside tmux, screen or Zellij, or with this setting off, images are drawn with colored blocks, two pixels to a character cell. In WezTerm, VS Code and Windows Terminal, an image shows as colored blocks while the conversation scrolls and sharpens once it stops. If images still show as blocks in Windows Terminal, start iCode from a Windows Terminal tab rather than from a console window that Windows opened in Windows Terminal. | After restart |

#### Input

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Save prompt history | Saves submitted input across sessions for reuse. When no other dialog covers the main screen, press **Ctrl+R** to open the prompt history list above the input field. Type to search it; the most relevant prompts are listed first. Press **Ctrl+R** again, the up and down arrow keys, or **Page Up** and **Page Down** to move through the list. Click an entry, or select it with the up and down arrow keys and press **Enter**, to copy the prompt into the input field. The history file, `prompt_history.jsonl`, is stored in `~/.chrys/` on macOS and Linux, or `%APPDATA%\chrys\` on Windows. Turning this setting off does not delete the file; iCode stops reading it, and **Ctrl+R** says that prompt history is off. Re-enable the setting and restart iCode to read the file's entries again. To clear the history, delete the history file; deletion cannot be undone. | After restart |

When no suggestion list is open, **Up** and **Down** move through the rows of the prompt, including automatically wrapped rows. **Up** recalls an older prompt only from the first row. While browsing history, **Down** moves to a newer prompt only from the last row; moving past the newest entry restores your draft.

#### Updates

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Check for updates | Once a day, when iCode starts, looks up the newest version. When a newer one is out, the welcome screen shows it with the command that upgrades iCode; click the command to copy it. The check sends none of your personal information or data. | After restart |

### Models & Agents

This tab selects existing agent or model profiles; it does not edit them. For those operations, see [Configure agents](./agents.md) and [Configure models](./models.md).

#### Agent

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Default agent | Determines only the default agent for the next TUI launch; it does not affect the current session. | Saved immediately; applies at the next launch |

#### Model roles

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Session title model | Defaults to "Use active model". Used only when [Auto-generate session titles](#sessions) is enabled. | On close |
| Approval judge model | Defaults to "Use active model". Used only in [automatic approval mode](./approval.md#understand-the-three-approval-modes). | On close |
| Buddy model | Defaults to "Use active model". Used for the Buddy companion feature. | On close |

#### LLM

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Max transient retries | Sets the maximum number of automatic retries for errors that may resolve shortly, such as temporary network interruptions, request timeouts, temporary model service unavailability, or server rate limits. It does not retry problems that waiting cannot resolve, such as invalid model configuration or request parameters. A higher limit gives temporary failures more chances to recover; if retries keep failing, it also increases the wait and the number of model requests before the final failure. Leave blank to use the TUI default; set to `0` to disable automatic retries. This setting does not cover the one resend after iCode compacts a full context window, or the one resend of a reply that came back with no answer. | On close |
| Credential command refresh interval (seconds) | How often iCode runs a `{{!command}}` value again, at least 60 seconds. The default is 3600 (one hour). A key the service refuses is replaced at once, whatever this says. See [Get an API key from a command](./models.md#get-an-api-key-from-a-command). | After restart |

### Security

#### Approval

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Default approval mode | Sets the default approval mode for the next iCode launch; it does not affect the current session. Only `manual` and `auto` are offered; to bypass approval in the current session, use `/approval bypass`. For switching the current session's mode and the differences between modes, see [Configure approval modes](./approval.md). | Saved immediately; applies at the next launch |
| Automatic review strategy | **Direct review** (default) lets the model judge the action as a whole. **Individual risk checks** ask you to confirm detected risks such as file deletion or code publication. Used only in automatic mode. See [Choose how automatic review works](./approval.md#choose-how-automatic-review-works). | After closing Settings |
| Human approval timeout (seconds) | Defaults to `0` (no timeout); positive values set a deadline in seconds. Negative values are adjusted to `0`. Applies to TUI and ACP human approvals. Timeout rejects the call. | After restarting iCode |
| Show the approval dialog only when Auto-Review flags a call | On by default. In automatic mode, no dialog opens while the approval judge model evaluates a call, and calls it judges safe run without one; the dialog opens only for flagged calls or when evaluation fails. When disabled, the dialog opens at once and shows **Evaluating**, so you can decide before evaluation finishes. Requests already waiting keep the behavior they started with. See [Handle approval requests in the TUI](./approval.md#handle-approval-requests-in-the-tui). | Immediately |

#### Project trust

Project settings, hooks, and skills come with the repository you open, so all three are off by default. When the working directory has any of them that are not loaded, iCode shows a notice naming the setting to turn on.

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Load project settings | Reads `.chrys/settings.yaml` in the current working directory and applies its project settings, such as request retries, automatic session titles, and working directory notices. See [Project-level settings](../../reference/settings.md#project-level-settings) for allowed keys and override restrictions. | On close |
| Load project hooks | Controls whether iCode loads hooks from `.chrys/hooks` in the current working directory; it does not affect user-level hooks. To create and validate hooks, see [Configure and write hooks](../extensions/hooks.md). For the complete configuration fields, see the [Hooks configuration reference](../../reference/hooks.md). | On close |
| Load project skills | Controls whether iCode loads skills from `.agents/skills` in the current working directory; it does not affect your own skills directories. See [Install and use skills](../extensions/skills.md). | On close |

#### Diagnostics

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Capture raw model HTTP requests | Records model requests and responses for troubleshooting. **Logs may contain API keys, complete prompts, and model responses in plain text.** Enable this only while troubleshooting and protect the log files. When finished, turn it off promptly and restart iCode. The log file, `llm_raw_http.jsonl`, is stored in the current session folder; see [Find the session ID and storage location](../daily-use/sessions.md#find-the-session-id-and-storage-location). | After restart |

After enabling raw capture, fully quit and restart iCode, resume the affected session, and send a request. The log is created when that request is sent; enabling the checkbox does not capture requests from the current process.

iCode uses structured error details to distinguish a rejected request from a temporary service failure, even when a gateway rewrites the HTTP status. The chat error retains the provider's explanation. Known content-filter and account-quota rejections stop iCode's automatic retry loop; the provider SDK may already have retried the HTTP request before returning the error.

| Service or gateway | Recognized details | Handling |
| --- | --- | --- |
| [New API](https://github.com/QuantumNous/new-api) and compatible relays | `sensitive_words_detected`, `prompt_blocked`; the `new_api_error` message form used by Anthropic-compatible endpoints | Content-filter rejection, including when wrapped in HTTP 500 |
| [Alibaba Cloud Model Studio / DashScope](https://www.alibabacloud.com/help/en/model-studio/error-code) | `DataInspectionFailed` / `data_inspection_failed`; `Arrearage` | Content-filter rejection; account quota exhausted |
| [Volcengine Ark](https://console.volcengine.com/ark/region%3Acn-beijing/docs/error-codes) | `SensitiveContentDetected` and documented input/output content-policy codes | Content-filter rejection |
| [OpenRouter](https://openrouter.ai/docs/api_reference/errors-and-debugging) | Canonical `error_type`, moderation metadata, and budget `limit_source` | Preserves the specific rejection, quota, throttling, or service-failure category across Chat Completions, Anthropic Messages, and Responses error formats |

For non-streaming Chat Completions, an error in `choices[].error` fails the whole response even under HTTP 200: partial answers are not accepted and tool calls are not executed. Reported token usage is retained. A bare `finish_reason: "error"` without details is treated as a retryable service failure.

When an in-band error omits `code`, iCode uses its native `type`: for example, `billing_error` and `authentication_error` stop automatic retries, while `overloaded_error` remains retryable. A recognized canonical `error_type` still takes precedence.

Ordinary 500/502/503 errors remain retryable. A 403 without content-filter evidence remains an authorization failure. OpenRouter's temporary `openrouter_in_flight_budget` response is retryable when accompanied by a positive `Retry-After`; exhausted account or workspace budgets are not. If ordinary task content is blocked, contact the provider with the request ID: the response may omit the exact matched text, and a client cannot infer it from the status alone.

#### Telemetry

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| OpenTelemetry export | Enables telemetry to record traces, logs, and metrics. When disabled, the other two settings do not cause iCode to save or send telemetry. | After restart |
| Telemetry endpoint | Sets the address of the collector that receives telemetry. With no endpoints configured through settings or environment variables, only traces and logs are saved locally. Configuring any endpoint stops local storage, with no local fallback if export fails. | After restart |
| Include sensitive data in telemetry | Additionally records prompts, model responses, tool arguments, and tool results in both local storage and remote export. Tool duration metrics may also contain tool arguments. Before enabling this, confirm where the data will go and who can access it. | After restart |

For telemetry data types, storage locations, endpoint precedence, and collector connections, see the [OpenTelemetry reference](../../reference/opentelemetry.md).

### Sessions

#### Titles

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Auto-generate session titles | Makes additional model requests to generate titles based on the conversation. **Once you set a session title manually, iCode stops updating that session's title automatically.** To set or clear a manual title, see [Change a session title](../daily-use/sessions.md#change-the-session-title). | On close |

#### Location

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Session storage root | Determines where iCode saves sessions. Leave blank to use the default location; sessions are stored in the root's `sessions` subdirectory. "In use" shows the `sessions` directory used by the current launch. After changing the root, restart iCode to use the new location; until then, sessions continue to be saved in the directory shown under "In use". **After restart, new sessions are saved in the new directory, but existing session files are not moved automatically.** To continue using existing sessions at the new location, see [Migrate session storage](../daily-use/sessions.md#migrate-session-storage). | After restart |

#### Rollback

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Rollback snapshots to keep | Sets the maximum number of rollback snapshots kept per session. iCode creates a snapshot before it starts processing each newly submitted prompt; older snapshots are deleted automatically when the limit is exceeded. Snapshots are stored in the session folder's `snapshots` subdirectory. Keeping more snapshots lets `/rollback` return to more earlier session states, but uses more disk space. | On close |

### Tools

#### File search

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Respect .gitignore | Applies Git ignore rules to grep and glob searches. When off, hidden paths are still skipped, and other ignore files such as `.ignore` still apply. | On close |

#### Images

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Image size limit (KB) | The largest an image's file can be when it is sent to the model, from 32 to 3072 KB. The default is 256. In the request an image takes about a third more than its file, since it is sent as text. A larger image that you attach, that the agent views, or that an ACP client sends is compressed to a JPEG within the limit; the image file itself is not changed. Each new turn and each retry also compresses the images sent earlier in the session that are larger than the limit, and the session keeps the compressed copies, so an image that an MCP tool returns is compressed from the next turn or retry on. If a request with images fails because it is too large, for example with an HTTP 413 error from a gateway, lower the limit and retry. With a lower limit the model sees less detail in each image. | On close |

#### Web search

These settings apply to agents that include the web tools, which are off by default. See [Configure web tools](./web-tools.md). An agent profile that sets its own mode, result count, or timeout overrides these values for that agent. Project settings cannot change them.

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Web search mode | `auto` (default) lets agents that include web search use it. An agent that configures no search provider sends its queries to Exa's public search endpoint, a third-party service; see [Where web requests go](./web-tools.md#where-web-requests-go). `off` turns web search off. An agent profile that sets its own mode uses that mode instead. | On close |
| Results per web search | Number of results a search returns when the agent does not ask for a number, from 1 to 20. The default is 8. | On close |
| Web fetch mode | `on` (default) lets agents that include web fetch read pages by URL. `off` turns web fetch off. An agent profile that sets its own mode uses that mode instead. | On close |
| Web proxy URL | An `http://` or `https://` proxy used only by web tools, without credentials or a path. Leave blank to connect directly. Web tools ignore proxy environment variables such as `HTTP_PROXY`. | On close |
| Web proxy DNS | Where host names are resolved when a proxy is set. `local` (default) resolves them on this device and checks every address before connecting. `remote` sends the host name to the proxy, as fake-IP proxies require, and leaves blocking private addresses to the proxy; see [Use a fake-IP proxy](./web-tools.md#use-a-fake-ip-proxy). It has no effect without a web proxy URL. | On close |
| Custom search endpoints | A JSON array of grants for custom search endpoints used in agent profiles, each written as `{"url": "...", "credential_env_names": ["..."]}`. | On close |
| Web search private-network origins | A JSON array of origins that web search may reach even though they resolve to private addresses, such as `["http://127.0.0.1:8080"]` for a local SearXNG. | On close |
| Web search plain-HTTP origins | A JSON array of origins that web search may reach over plain `http://`. | On close |

Values are checked with the same rules the web tools use before they are saved, and a rejected value says what is wrong. Clear a list field to empty the list. The web search timeout, the web fetch timeout and token budget, and the web fetch origin lists are not shown in the dialog; set them in `settings.yaml` (see [Web tools](../../reference/settings.md#web-tools)).

#### Ask user

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Show questions in the chat | When enabled, the agent's questions appear directly in the conversation. When disabled, they appear in a popup. | On close |
| Ask-user timeout (seconds) | Sets how long iCode waits for an answer. When left blank, it uses the default value shown in the TUI. If no answer arrives before the timeout, iCode returns a timeout result to the agent. Set to `0` to wait until an answer arrives or the task is interrupted. When using an ACP client such as an IDE plugin, `ask_user` has no server-side time limit by default; to set one, use `--ask-user-timeout` when starting the ACP server. | On close |

#### Workspace notice

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Workspace change notices | When processing a newly submitted prompt, iCode gives the agent a summary of working directory file changes since the previous prompt was submitted. This includes changes made by the current session's agent and its sub-agents, as well as changes made manually, by other programs, or by other iCode sessions. | On close |

### Notifications

#### General

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Enable Notifications | The master switch for event notifications. When disabled, no desktop popups or sounds are sent, but delivery and event selections are preserved and still apply when notifications are re-enabled. | Immediately |

#### Delivery

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Desktop popup, Sound | Can be enabled separately. If both are disabled, no notifications are delivered, even when the master switch and event switches are enabled. | Immediately |
| Suppress while iCode is focused | Prevents desktop popups and sounds while iCode is in the foreground. | Immediately |
| Test | Sends a test notification using the current delivery settings, regardless of the master switch, event switches, or focus state. It only checks whether desktop popups and sound work; enable at least one of "Desktop popup" or "Sound" before testing. | Immediately |

#### Events

| Setting | Effect and notes | Takes effect |
| --- | --- | --- |
| Approval required | Controls whether a notification is triggered when approval is required. | Immediately |
| Agent needs input | Controls whether a notification is triggered when the agent needs input. | Immediately |
| Agent finished | Controls whether a notification is triggered when the agent finishes a task. | Immediately |
| Agent error | Controls whether a notification is triggered when the agent encounters an error. | Immediately |
