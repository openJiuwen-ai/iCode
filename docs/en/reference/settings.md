# iCode settings file reference

`settings.yaml` stores iCode interface preferences, the default agent, and session and tool settings. This page lists common settings by purpose and explains file locations and format, how to write values, precedence when multiple sources are present, and which settings project files can override.

For steps to change settings in the terminal user interface (TUI) and when those changes take effect, see [Configure iCode settings](../guides/configuration/settings.md).

## File locations and format

| Configuration source | macOS / Linux | Windows | Purpose |
| --- | --- | --- | --- |
| User settings | `~/.chrys/settings.yaml` | `%APPDATA%\chrys\settings.yaml` | Store personal settings; the TUI Settings dialog writes to this file |
| Project settings | `.chrys/settings.yaml` in the working directory | Same as macOS / Linux | Override some settings for that working directory; not loaded by default |

iCode looks for `.chrys/settings.yaml` only in the current session's working directory. It does not search parent directories or subdirectories.

The file uses YAML. Dots in key names represent nesting. For example, write `llm.retry.max_transient` as:

```yaml
llm:
  retry:
    max_transient: 3
```

After editing the settings file manually, save it and restart iCode for the changes to take effect.

## Settings keys

In the tables, “None” means there is no corresponding environment variable, and “Unset” means the key has no saved value (it is omitted or left empty). For general rules on writing values and handling invalid values, see [Value syntax and invalid values](#value-syntax-and-invalid-values).

### Interface and input

| YAML key | Environment variable | Default | Type, values, and effect |
| --- | --- | --- | --- |
| `ui.theme` | `CHRYS_THEME` | `chrys` | String; use a theme name from the TUI theme list (press **F9** to open it). The retired name `chrys-dark` is read as `chrys`; other unknown names fall back to the default theme |
| `ui.locale` | `CHRYS_LOCALE` | `system` | String; `system` follows the system language. You can also set `en` or `zh-Hans`. Unsupported languages fall back to English; this does not change the language of model responses |
| `ui.editor.keymap` | `CHRYS_EDITOR_KEYMAP` | `standard` | String; `standard`, `emacs`, or `vim` selects the input editor's key bindings |
| `ui.chat.tool_groups_expanded` | None | `false` | Boolean; whether newly displayed tool groups expand their details by default |
| `ui.chat.file_snapshot_inline_chars` | `CHRYS_TUI_FILE_SNAPSHOT_INLINE_CHARS` | `131072` | Integer; limits the before-and-after file content loaded directly with a file edit card, measured by their combined byte size. Within the limit, content loads with the card and the header shows the exact numbers of added and deleted lines. Above the limit, if snapshot references are available, content is read only when the diff is expanded, and the header does not show exact line counts. `0` or a negative value sets the limit to zero; inline content may still be retained when no snapshot references are available. This setting controls only how the card loads content; `mutations.snapshot.max_file_mb` and `mutations.snapshot.skip_binary` determine whether file content is backed up |
| `history.prompt.enabled` | `CHRYS_HISTORY_DISABLE` | `true` | Boolean; save and read input history across sessions. Disabling it does not delete existing records. The environment variable has the opposite meaning: only the exact value `1` disables history; other values do not |
| `app.update_check` | `CHRYS_UPDATE_CHECK` | `true` | Boolean; once a day, when the TUI starts, look up the newest iCode version and, when it is newer, show it on the welcome screen with the command that upgrades iCode. The request carries none of your personal information or data. `icode run`, `icode acp` and `icode serve` never check |
| `workspace.mru_max_entries` | `CHRYS_WORKSPACE_MRU_MAX_ENTRIES` | `20` | Integer; maximum number of recently used working directories to retain, capped at `100`. `0` or a negative value disables recording |

### Agents, models, and requests

Model role keys take the ID or unique name of an existing model profile. Buddy does not select a model profile: its key takes the model name used by the model service (`model_id`).

| YAML key | Environment variable | Default | Type, values, and effect |
| --- | --- | --- | --- |
| `agent.default_profile` | `CHRYS_DEFAULT_AGENT` | `Code` | String; the default agent the next time the TUI starts. Does not switch the current session's agent; an explicit `--agent` takes precedence |
| `model.profile.active` | `CHRYS_MODEL_PROFILE` | Unset | String; select an existing model profile as the fallback when the agent has no valid `model.profile_id`. An explicit `--model` takes precedence over this key but still serves as a fallback; explicitly switching models in an ACP session takes precedence over the model specified by the agent |
| `model.role.approval_judge` | `CHRYS_MODEL_PROFILE_APPROVAL_JUDGE` | Unset | String; the model profile used for automatic approval. Uses the current model when unset |
| `model.role.session_title` | `CHRYS_MODEL_PROFILE_SESSION_TITLE` | Unset | String; the model profile used to generate session titles automatically. Uses the current model when unset |
| `model.role.buddy_model_id` | `CHRYS_PET_MODEL` | Unset | String; the model used by Buddy, using the current model profile's connection. Uses the current model when unset |
| `llm.retry.max_transient` | `CHRYS_MAX_TRANSIENT_RETRIES` | `null` | Integer or `null`; `null` uses the default retry count for the launch command: `10` for the TUI (including `icode serve`) and `icode acp`, or `18` for `icode run` and `icode workflow run`. The wait before each retry grows, up to 10 minutes. `0` disables automatic retries for transient errors; negative values are invalid; capped at `50` |

Transient error retries handle recoverable errors such as temporary network failures, request timeouts, and rate limits. Raising the count also increases the wait and number of requests before a final failure. This number is not the total number of model requests for the entire task.

### Approval and project trust

The `approval` keys control approval behavior when an agent requests an operation. For the differences between the three modes and configuration steps, see [Configure approval modes](../guides/configuration/approval.md).

| YAML key | Environment variable | Default | Type, values, and effect |
| --- | --- | --- | --- |
| `approval.default_mode` | `CHRYS_DEFAULT_APPROVAL_MODE` | `manual` | String; `manual` for manual approval, `auto` for automatic approval, or `bypass` to bypass approval. Setting the default mode does not switch the current session's mode |
| `approval.timeout_seconds` | `CHRYS_APPROVAL_TIMEOUT_SECONDS` | `0` | Integer; seconds to wait for human approval in TUI and ACP. `0` disables the timeout; positive values set a deadline. Negative values are raised to `0`. Timeout rejects the tool call. Restart iCode to apply |
| `ui.approval.defer_while_judging` | None | `true` | Boolean; in automatic mode, whether the TUI opens the approval dialog only when the approval judge model flags a call or evaluation fails. `false` opens it at once while the call is evaluated. The ACP server and `icode run` ignore it |
| `project.config_enabled` | None | `false` | Boolean; whether to load project settings for each working directory. Must be enabled in user settings |
| `project.hooks_enabled` | None | `false` | Boolean; whether to load project hooks from `.chrys/hooks` in the working directory. Must be enabled in user settings. Does not affect user-level hooks |
| `project.skills_enabled` | None | `false` | Boolean; whether to load project skills from `.agents/skills` in the working directory. Must be enabled in user settings. The agent's own “Load skills from working folder” option must also stay on |

If you manually set the default approval mode to `bypass` in YAML or an environment variable, approval is bypassed the next time iCode starts with that default. When you switch to `bypass` with `/approval` in the TUI, iCode saves `auto` as the default mode; the TUI Settings dialog does not offer `bypass`.

Project settings, project hooks, and project skills come from the repository you open, so all three are off by default and each is turned on separately: enabling one does not enable the others. When the working directory has any of them that are off, iCode shows a notice naming the setting to turn on. Hooks are external commands that iCode runs on specific events; for authoring and configuration, see [Configure and write hooks](../guides/extensions/hooks.md). For skills, see [Install and use skills](../guides/extensions/skills.md).

### Sessions and file recovery

| YAML key | Environment variable | Default | Type, values, and effect |
| --- | --- | --- | --- |
| `session.title.auto` | `CHRYS_SESSION_TITLE_AUTO` | `true` | Boolean; automatically generate session titles through additional model requests. Once you set a title manually, that session's title is no longer updated automatically |
| `storage.session_root_dir` | `CHRYS_SESSION_ROOT_DIR` | Unset | Path string; defaults to the user configuration directory. Sessions are stored in a `sessions` subdirectory under this root. Supports `~`; an absolute path is recommended |
| `rollback.snapshots_keep` | `CHRYS_ROLLBACK_SNAPSHOTS_KEEP` | `20` | Integer; number of rollback snapshots to retain per session, with a minimum of `1`. Older snapshots are deleted when the limit is exceeded |
| `mutations.snapshot.max_file_mb` | `CHRYS_MUTATION_SNAPSHOT_MAX_FILE_MB` | `50` | Integer; maximum size of an individual file backup, in MiB. `0` or a negative value removes the size limit. Files above the limit are still recorded as changed, but their content is not backed up, so that record cannot be used to display a diff or restore the file |
| `mutations.snapshot.skip_binary` | `CHRYS_MUTATION_SNAPSHOT_SKIP_BINARY` | `true` | Boolean; whether to skip backing up binary file content. When set to `false`, the per-file size limit still applies |

Changing the session storage root does not automatically move existing sessions. If the specified directory is unavailable, iCode reports it and falls back to the default location. For migration steps, see [Move session storage](../guides/daily-use/sessions.md#migrate-session-storage).

### Tools and working directory changes

| YAML key | Environment variable | Default | Type, values, and effect |
| --- | --- | --- | --- |
| `tools.search.respect_gitignore` | None | `true` | Boolean; apply `.gitignore` rules to grep and glob searches. When `false`, hidden paths are still skipped and other ignore files such as `.ignore` still apply |
| `tools.ask_user.inline` | `CHRYS_ASK_USER_INLINE` | `false` | Boolean; show agent questions in the TUI conversation instead of a dialog |
| `tools.ask_user.timeout_seconds` | `CHRYS_ASK_USER_TIMEOUT_SECONDS` | `900` | Integer or `null`; seconds to wait for an answer. On timeout, a timeout result is returned to the agent. `0`, a negative value, or `null` in user YAML means wait indefinitely |
| `tools.result.ceiling_tokens` | `CHRYS_TOOL_RESULT_CEILING_TOKENS` | `64000` | Integer; final length limit for a single tool result before it is passed to the model, in tokens. `0` disables this limit; positive values have a minimum of `2000`; negative values are invalid |
| `workspace.change_notice.enabled` | `CHRYS_WORKSPACE_CHANGE_NOTICE` | `true` | Boolean; when processing a new prompt, give the agent a summary of working directory file changes since the previous prompt was submitted, including changes made by the agent and external changes |
| `workspace.change_notice.max_entries` | `CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES` | `50` | Integer; maximum number of entries in the change summary, from `1` to `100` |
| `mutations.parallel_implicit_tools` | `CHRYS_PARALLEL_IMPLICIT_TOOLS` | `true` | Boolean; allow tools that may modify files, such as shell commands and skill scripts, to run in parallel within the same session. Setting this to `false` makes it easier to identify which tool call caused a file change |
| `mutations.coordination.enabled` | `CHRYS_MUTATION_COORDINATION` | `true` | Boolean; help distinguish file changes made by different iCode sessions sharing a working directory |

Tools may also have their own output limits, such as limits on MCP results and skill resources. Setting `tools.result.ceiling_tokens` to `0` does not disable those limits or recover content that a tool has already truncated. Separately from these token limits, shell commands and skill scripts keep at most 32 MiB of each output stream.

### Web tools

These keys configure the `web_search` and `web_fetch` tools, which are off by default and must also be added to an agent. They have no corresponding environment variables and can be set only in user settings; project settings cannot set them. The TUI **Settings** dialog shows the common keys on the **Tools** tab; set the others in the user `settings.yaml`. For how the tools use these values, see [Configure web tools](../guides/configuration/web-tools.md).

Origin lists take exact origins (scheme, host, and optional port, such as `https://docs.example.com`) without paths, queries, credentials, or wildcards. Write a list as a YAML list or as a JSON array string; the TUI saves lists as JSON strings. An empty string is an empty list. In the two mode keys, `true` or an unquoted `on` means `auto` for search and `on` for fetch, and `false` or an unquoted `off` means `off`. Values are checked with the same rules the tools use, and an invalid value is reported, with what is wrong, and ignored.

| YAML key | Default | Type, values, and effect |
| --- | --- | --- |
| `tools.web_search.mode` | `auto` | String; `auto` or `off`. With `auto`, an agent that includes `web_search` and configures no search provider sends queries to Exa's public endpoint `https://mcp.exa.ai/mcp`. `off` turns web search off. The agent profile's `tools.web_search.mode` takes precedence |
| `tools.web_search.num_results` | `8` | Integer from `1` to `20`; number of results a search returns when the agent does not ask for a number. The agent profile's value takes precedence |
| `tools.web_search.timeout_seconds` | `30` | Integer from `1` to `120`; deadline in seconds for a whole search call, including time waiting for a free connection. The agent profile's value takes precedence |
| `tools.web_search.custom_endpoints` | `[]` | List of objects of the form `{"url": "...", "credential_env_names": ["..."]}`; authorizes a `custom_http` search endpoint and the environment variables it may read. Each URL may appear only once |
| `tools.web_search.private_origins` | `[]` | Origin list; search endpoints that may be reached even though they resolve to private or other non-public addresses |
| `tools.web_search.http_origins` | `[]` | Origin list; search endpoints that may be reached over plain `http://` |
| `tools.web_fetch.mode` | `on` | String; `on` or `off`. In `on` mode, `web_fetch` is available to agents that include it; in `off` mode it is not. The agent profile's `tools.web_fetch.mode` takes precedence |
| `tools.web_fetch.max_tokens` | `16000` | Integer from `1` to `64000`; most tokens a fetched page returns. The agent can ask for less, but not more. The agent profile's value takes precedence |
| `tools.web_fetch.timeout_seconds` | `60` | Integer from `1` to `120`; deadline in seconds for a whole fetch call. The agent profile's value takes precedence |
| `tools.web_fetch.private_origins` | `[]` | Origin list; origins `web_fetch` may read even though they resolve to private or other non-public addresses |
| `tools.web_fetch.http_origins` | `[]` | Origin list; origins `web_fetch` may read over plain `http://` |
| `tools.web_fetch.allowed_origins` | `[]` | Origin list; when not empty, `web_fetch` reads only from these origins |
| `tools.web_fetch.denied_origins` | `[]` | Origin list; origins `web_fetch` never reads. Takes precedence over every other list |
| `tools.web_egress.proxy_url` | Unset | String; an `http://` or `https://` proxy used only by web tools, written as an origin without credentials or a path, such as `http://proxy.example.com:8080`. Web tools ignore `HTTP_PROXY`, `HTTPS_PROXY`, and similar variables |
| `tools.web_egress.proxy_dns` | `local` | String; `local` or `remote`. With a proxy set, `local` resolves host names on this device and connects only to checked public addresses; `remote` sends the host name to the proxy, which then decides the final address. Has no effect without `proxy_url`. See [Use a fake-IP proxy](../guides/configuration/web-tools.md#use-a-fake-ip-proxy) |

Host names in these values may contain underscores.

### Context and trajectory analysis

The following settings have no corresponding environment variables.

| YAML key | Default | Type, values, and effect |
| --- | --- | --- |
| `context.warn_threshold_pct` | `0.5` | Number; from `0` to `1`, representing the fraction of the context window in use. When usage reaches the threshold, the agent is told once that context usage is high, and again only after usage has dropped below the threshold and reached it again. The default threshold is 50%; this does not change when automatic compaction is triggered |
| `trajectory.verify_commands` | Built-in list of common test and check commands | String; comma-separated command terms that trajectory analysis uses to identify verification operations, such as `"pytest,ruff,npm test"`. A custom value replaces the entire list. This affects only analysis categories; it does not run the commands |

### Notifications

The following keys are all booleans, default to `true`, and have no corresponding environment variables. They control TUI event notifications.

| YAML key | Effect |
| --- | --- |
| `notifications.enabled` | Master switch for event notifications |
| `notifications.delivery.desktop` | Show desktop notifications |
| `notifications.delivery.sound` | Play a sound |
| `notifications.suppress_when_focused` | Suppress notifications while iCode has focus |
| `notifications.events.approval_required` | Notify when approval is required |
| `notifications.events.ask_user` | Notify when the agent needs input |
| `notifications.events.turn_complete` | Notify when the agent completes a task |
| `notifications.events.turn_error` | Notify when the agent encounters an error |

To send a notification, the master switch, the corresponding event switch, and at least one delivery method must be enabled, and notifications must not be suppressed because iCode has focus. Turning off the master switch does not clear the other choices.

### Diagnostics and telemetry

**Raw HTTP logs may contain plaintext API keys, full prompts, and model responses.** Enable this only after confirming where the data will go and who can access it. After troubleshooting, turn off diagnostic options you no longer need and restart iCode.

| YAML key | Environment variable | Default | Type, values, and effect |
| --- | --- | --- | --- |
| `log.raw_http_capture` | `CHRYS_DEBUG_LLM_RAW_HTTP_LOG` | `false` | Boolean; write raw model HTTP requests and responses to `llm_raw_http.jsonl` in the current session directory |
| `otel.enabled` | `CHRYS_OTEL` | `false` | Boolean; enable OpenTelemetry export |
| `otel.endpoint` | `CHRYS_OTEL_ENDPOINT` | Unset | String; telemetry collector endpoint. With no endpoints configured, only traces and logs are saved locally. Standard OTLP endpoint environment variables take precedence over this setting. Configuring an endpoint stops local storage, with no local fallback if export fails |
| `otel.sensitive_data` | `CHRYS_OTEL_SENSITIVE_DATA` | `false` | Boolean; additionally record prompts, model responses, tool arguments, and tool results, which may contain private information. Enable this only after confirming where the data will go and who can access it |

For data storage locations, collector connections, authentication, and metric queries, see the [OpenTelemetry reference](./opentelemetry.md).

## Source precedence

Each key is resolved independently in the following order, using the first valid value. Project settings must also meet the restrictions under [Project-level settings](#project-level-settings).

| Priority | Source |
| --- | --- |
| 1 | Explicit choices in the current session, command-line arguments, or behavior fixed by the launch command |
| 2 | Environment variables already present when the process starts |
| 3 | Settings not yet migrated from the legacy user `.env` file (macOS / Linux: `~/.chrys/.env`; Windows: `%APPDATA%\chrys\.env`) |
| 4 | Project settings that are enabled and permitted to apply |
| 5 | User settings |
| 6 | Built-in defaults |

At startup, iCode attempts to migrate iCode settings from the legacy user `.env` file into the user `settings.yaml` and remove the migrated lines. During the merge, existing keys in `settings.yaml` take precedence and are not overwritten. Settings that have not yet been migrated continue to apply at priority 3 in the table. If migration fails, iCode reports it and retries on the next startup. Environment variables such as model service API keys remain in `.env`.

iCode settings in the working directory's `.env` file do not take effect. Write project settings in `.chrys/settings.yaml`.

## Value syntax and invalid values

These are the general rules for values in settings files and environment variables. For per-key constraints such as limits and the meaning of `null`, see the “Type, values, and effect” column under [Settings keys](#settings-keys).

- Write booleans as `true` or `false` in YAML. Corresponding environment variables accept `1`, `true`, `yes`, `on` and `0`, `false`, `no`, `off`, case-insensitively. Exceptions where an environment variable has the opposite meaning or accepts only a specific spelling are noted in the relevant row.
- Write integers as numbers. For keys with stated limits, out-of-range values are adjusted to the boundary; keys that explicitly prohibit negative values reject them. Special meanings of `0` or negative values, such as disabling a feature or removing a limit, follow the relevant row.
- Omitted keys and empty strings generally do not provide an override. To remove a setting from a file, delete the key. A value from another source may still apply afterward (see [Source precedence](#source-precedence)).
- Keys whose type is “Integer or `null`” accept `null`, with the meaning described in the relevant row. User settings and project files handle `null` differently; see [Project-level settings](#project-level-settings).
- Unknown keys are reported and do not take effect. Most invalid values are ignored and reported, and resolution continues with lower-priority sources. For example, if an environment variable is invalid but user settings contain a valid value, the user setting is used. Five keys behave differently: `approval.default_mode`, `tools.result.ceiling_tokens`, `log.raw_http_capture`, `otel.sensitive_data`, and `mutations.trace.fsatrace_path`. Once resolution encounters an invalid value for one of these keys, it immediately uses the built-in default and stops, without falling back to lower-priority sources. For example, an unrecognized approval mode uses the default `manual`.

## Project-level settings

Project settings are not loaded by default. First enable them in the user `settings.yaml`, or turn on “Load project settings” under “Settings → Security” in the TUI:

```yaml
project:
  config_enabled: true
```

Once enabled, project settings apply to the working directories you open. Project files can configure only the keys in the following table.

For keys with restrictions on the direction of an override, project values are accepted only in the direction listed in the table. The comparison uses the value in user YAML, or the built-in default if the user has not set one. For `llm.retry.max_transient`, an unset or `null` user value uses the default for the launch command as the comparison baseline. Keys without a direction restriction use the project value directly. Environment variables are not part of this comparison, but still take precedence over project settings. A `null` value in a project file does not provide an override.

| Key allowed in project settings | Allowed override direction |
| --- | --- |
| `llm.retry.max_transient` | Can only reduce or maintain the retry count |
| `tools.result.ceiling_tokens` | Can only lower or maintain the result length limit. `0` means unlimited and is the least restrictive value; a project can use `0` only when the user setting is already `0` |
| `session.title.auto` | Can disable automatic titles; cannot re-enable them if the user has disabled them |
| `mutations.parallel_implicit_tools` | Can disable parallel execution; cannot re-enable it if the user has disabled it |
| `mutations.coordination.enabled` | Can enable distinguishing changes across sessions; cannot disable it if the user has enabled it |
| `workspace.change_notice.enabled` | Can enable change notices; cannot disable them if the user has enabled them |
| `workspace.change_notice.max_entries` | Can override within the range `1`–`100` |
| `context.warn_threshold_pct` | Can override within the range `0`–`1` |
| `trajectory.verify_commands` | Can replace the verification command list |

Web tool keys (`tools.web_search.*`, `tools.web_fetch.*`, and `tools.web_egress.*`) are not in this table, so a project cannot turn on web search or widen where web tools may connect.

For example, write the following in `.chrys/settings.yaml` in the working directory:

```yaml
llm:
  retry:
    max_transient: 3
```

After enabling project settings and restarting, if the user has not set a retry count, the project's `3` retries apply. If user YAML already sets `1`, the project's `3` is rejected and `1` still applies. A valid value in the startup environment always takes precedence.

iCode reports and ignores disallowed project keys, values that exceed override restrictions, and unknown keys, while continuing to apply other valid settings.

## Differences between launch commands

| Launch command | Behavior related to settings files |
| --- | --- |
| `icode` (TUI) | Uses interface, input, and notification settings; the default agent comes from `agent.default_profile` |
| `icode run` | Always bypasses approval and does not wait for user answers; the default approval mode and question timeout settings do not change these behaviors |
| `icode acp` | Agent Client Protocol (ACP) server; the initial agent and approval mode are determined by `--agent` and `--approval`, defaulting to `Code` and `manual`, respectively. Questions wait indefinitely by default; set a timeout with `--ask-user-timeout` |
| `icode serve` | Hosts the TUI in a browser; configuration files and environment variables come from the machine running the iCode service |

## When a setting does not take effect

1. Confirm that the settings file is in the correct location and that you have saved it and restarted iCode.
2. Check for a higher-priority source. Deleting a key from YAML does not clear values from other sources.
3. For project settings, confirm that loading is enabled in user settings and that the key and override direction meet the restrictions.
4. Check the messages iCode reports at startup. For how invalid values and unknown keys are handled, see [Value syntax and invalid values](#value-syntax-and-invalid-values); for directory-related messages, check [File locations and format](#file-locations-and-format).
