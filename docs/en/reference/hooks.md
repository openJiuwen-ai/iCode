# Hooks configuration reference

Hooks let iCode automatically run local scripts or commands at specified points. Use this page to look up the complete configuration fields, defaults, event data, and execution rules. If you are using hooks for the first time, start with [Configure and write hooks](../guides/extensions/hooks.md) to create and verify a hook using runnable examples.

> **Security notice**
>
> Hooks run directly with the current user's permissions, without tool approval or sandbox restrictions. Before enabling hooks supplied by a project, inspect their configuration files and the scripts and commands they run.
>
> Hooks may receive conversation content, tool arguments, and tool results. If a hook sends this data to an external service, check whether it contains sensitive information.

## Terminology

This page uses the following terms:

| Term | Configuration value or object | Meaning |
| --- | --- | --- |
| Turn | `turn` | iCode's handling of a user request, from when processing begins until the agent stops generating content and calling tools. A user interruption or run failure also ends the current turn; additional messages submitted during a run still belong to the current turn. |
| Event | `event` | The point that triggers a hook, such as the end of a turn or before a tool runs. |
| Blocking hook | `execution.mode: blocking` | iCode waits for the hook to finish before continuing; some events apply the decision returned by the hook. |
| Async hook | `execution.mode: async` | iCode immediately continues the current operation, but usually waits for the hook to finish before the current turn or session ends. |
| Fire-and-forget hook | `execution.mode: fire_and_forget` | iCode immediately continues and does not wait for the hook at the end of the current turn or session. This is the default mode. |
| Detached execution | `execution.detach: true` | Allows a fire-and-forget hook that has already started to keep running after iCode exits. |
| Durable delivery | `execution.delivery: durable` | Records unfinished non-blocking hook tasks so iCode can retry them later. The same task may run more than once, so scripts must be safe to run repeatedly. |
| Input file | `CHRYS_HOOK_PAYLOAD_FILE` | A JSON file that iCode generates for the event, containing event and context data. |
| Result file | `CHRYS_HOOK_RESULT` | A JSON file to which the hook writes a decision or additional information. Only eligible blocking hooks can use it to change iCode's behavior. |

## Configuration file locations

iCode can load one hook configuration file from each of the global configuration directory and the current working directory:

| Scope | macOS and Linux | Windows |
| --- | --- | --- |
| Global | `~/.chrys/hooks/hooks.yaml` | `%APPDATA%\chrys\hooks\hooks.yaml` |
| Project | `<working-directory>/.chrys/hooks/hooks.yaml` | `<working-directory>\.chrys\hooks\hooks.yaml` |

Either location can use `hooks.yml` or `hooks.json` instead. If a directory contains multiple candidates, iCode loads only the first one, in the order `hooks.yaml`, `hooks.yml`, `hooks.json`.

UTF-8 is recommended for hook configuration files. iCode also detects common legacy encodings such as GBK/GB18030 and BOM-marked UTF-16/UTF-32.

If a file cannot be decoded using the detected encoding, iCode reports a configuration warning and disables the hooks from that file. It does not substitute characters in commands or other values.

`<working-directory>` is the session's working directory. For project configuration, iCode checks only `<working-directory>/.chrys/hooks/`; it does not search parent or child directories of the working directory.

## Loading configuration files

Project hooks are not loaded by default, because they come with the repository you open. To load them, press **F10** in the terminal user interface (TUI) to open **Settings**, then turn on **Load project hooks** in the **Project trust** section of **Safety** (`project.hooks_enabled`). When the working directory has project hooks that are not loaded, iCode shows a notice. Global hooks are always loaded.

Editing a hook configuration file on disk does not take effect immediately. After editing, switch sessions or restart iCode to reload the configuration. Changes to hook scripts alone do not require a reload; the next time the hook is triggered, it uses the new script content.

Enter `/runtime` in the TUI and open the **Hooks** tab to view the currently loaded project and global hooks. In a terminal, [`icode hook list`](#icode-hook-list) shows the hooks a session started in the current directory loads, and [`icode hook validate`](#icode-hook-validate) checks the configuration files.

## Configuration file structure

A hook configuration file supports the following top-level fields:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `version` | Integer | `1` | Configuration format version. Currently, only `1` is supported. |
| `settings` | Object | The defaults for each field | Concurrency, shutdown wait, and durable delivery retry settings shared by all hooks in this configuration file. |
| `hooks` | List | `[]` | List of hook entries. |

Both `version` and `settings` can be omitted; iCode then uses the defaults listed in the table.

`hooks` defines the hooks; `settings` specifies execution parameters shared by all hooks in the current configuration file.

### Global settings

Configure `settings` when you need to adjust concurrency, shutdown wait time, or retry behavior for durable delivery:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `shutdown_grace_seconds` | Number (>= `0`) | `5.0` | Maximum time to wait for async hooks when the current session ends (see `session_end` in [Session events](#session-events)). Hooks still running after this time are canceled. |
| `max_parallel_hooks` | Integer (>= `1`) | `4` | Maximum number of hooks running concurrently. When the limit is reached, newly triggered hooks wait for running hooks to finish. Detached hooks are exempt from this limit. |
| `outbox_retry_age_seconds` | Number (>= `0`) | `60.0` | Used only for durable delivery hooks. After starting, iCode retries hook tasks that were not recorded as complete because of a previous exit or crash. This setting specifies the minimum delay in seconds before a retry, measured from the task's last execution start, or from its creation if it has never run. |
| `outbox_max_retries` | Integer (>= `0`) | `3` | Used only for durable delivery hooks. Maximum number of times a hook task may be started, including its first run. With the default `3`, an interrupted task is retried at most twice; `1` disables retries; `0` also marks tasks that never started as failed. |

When both project and global configuration exist, `settings` are merged field by field: values in the project file take precedence, followed by values in the global file, with defaults used for fields set in neither file.

### Defining hooks

Each entry in the `hooks` list defines one hook and supports the following fields:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `id` | String | Required | A stable identifier for the hook. Leading and trailing whitespace is stripped; the remaining value must be between `1` and `512` characters long. Must be unique within the file. |
| `event` | String | Required | When to run the hook. See [Events](#events) for available values. |
| `run` | Object | Required | The script, command, or shell snippet to launch. |
| `execution` | Object | See [Execution behavior](#execution-behavior) | Whether iCode waits for the hook, its time limit, and how failures are handled. |
| `match` | Object | No filters | Restricts which agents or tool calls trigger the hook. |
| `enabled` | Boolean | `true` | Set to `false` to disable the hook without deleting its configuration. |
| `description` | String | `""` | A description to help identify the hook; does not change its behavior. |

An `id` can contain Chinese characters, spaces, and special symbols, but not newlines, tabs, or other characters that cannot be displayed normally. If an `id` contains spaces or special symbols, enclose the entire value in quotes. Project and global configurations may use the same `id`; both hooks will run.

## Events

Use `event` to specify which event triggers a hook.

All events provide the [base fields](#base-fields). Some events also provide additional fields, as described below.

### Session events

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `session_start` | After the agent is ready. Triggered when iCode starts, a session is created, or the current session is rolled back to its beginning; not triggered by switching agents or models. | None |
| `session_restored` | After a saved session is restored. When a session is restored immediately after starting iCode, both `session_start` and `session_restored` may be triggered. | `restored_session_id`: ID of the saved session restored this time |
| `session_end` | When the current session ends: exiting iCode, switching sessions, starting a new session, deleting or clearing the current session, or rolling it back to its beginning. | None |

### Turn events

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `before_turn` | Before a turn starts. | `turn`: turn number, starting at `1`<br>`user_text`: user text used for the current turn<br>`is_retry`: whether the same turn is being retried |
| `after_turn` | After a turn ends, including normal completion, user interruption, and run failure. | `turn`: turn number, starting at `1`<br>`status`: `ok`, `interrupted`, or `failed`<br>`failed`: whether this turn did not complete normally; `true` for both failure and interruption |

### User action events

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `user_prompt_submit` | When the user submits a message to start a new turn, an additional message while the agent is running, or a `/btw` side question. | `text`: the submitted text; for a side question, the question without the `/btw` prefix<br>`injected`: `true` for an additional message sent to the running main turn; `false` for a new main-turn message or a side question<br>`side_question`: `true` for a side question; omitted for main-conversation messages (treat a missing value as `false`) |
| `user_interrupt` | After the user interrupts the current turn. | None |

To identify a message starting a new main turn, check both `not payload.get("injected", False)` and `not payload.get("side_question", False)`. A side question sends `injected: false` and `side_question: true`; it does not start a main turn or trigger `before_turn` / `after_turn`. Opening saved side questions with `/btw` alone does not trigger `user_prompt_submit`.

### Tool events

For the fields in the `tool` and `result` objects, see [Tool event fields](#tool-event-fields).

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `before_tool_call` | Before a tool enters the approval and execution process. If the user edits the arguments when approving the call, the hook runs again with the edited arguments before the tool runs. | `tool`: tool name, kind, call ID, and arguments |
| `after_tool_call` | After a tool call returns. Triggered whether the tool returns a normal result or a failure result, is not executed because approval was denied, or is denied by a `before_tool_call` hook. | `tool`: tool call<br>`result`: execution result |
| `tool_error` | After a tool call raises a runtime error. For the same call, `after_tool_call` is triggered first, followed by `tool_error`. | `tool`: tool call<br>`result`: execution result, with `result.error` set to `true` |

### Approval events

Approval events notify or record when tool approval is waiting for a human decision. They cover only tool approvals handled by iCode, not approvals forwarded by external ACP agents. For payload details, see [Approval event fields](#approval-event-fields).

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `approval_requested` | When tool approval enters a wait for a human decision. | `request_id`, `caller_name`, `tool` |
| `approval_resolved` | After a human approves or rejects the request, before iCode continues processing the decision. Not triggered if the wait is interrupted; does not mean the tool has executed or succeeded. | Same as `approval_requested`, plus `approved` |

In automatic approval mode (`auto`), iCode completes the automatic review first and triggers approval events only if a human decision is still needed. Neither event is triggered if automatic review approves the request, approval is bypassed, or a decision is already available before the wait begins.

Hooks for these two events can only observe approval state. They cannot approve, reject, or modify an approval decision through the result file.

### Sub-agent events

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `sub_agent_start` | When a sub-agent invocation starts. | `sub_agent`: display name `name`, tool name `tool_name`, invocation identifier `invocation_id`, and parent call identifiers `parent_call_id` and `parent_provider_call_id` |
| `sub_agent_end` | When a sub-agent invocation ends. | `sub_agent`: same as for `sub_agent_start`<br>`status`: `ok`, `failed`, or `cancelled`<br>`result_summary`: result summary of up to 500 characters |

### Context compaction events

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `pre_compact` | Immediately before context compaction. | `trigger`: compaction phase or trigger reason; values are listed below<br>`usage_pct`: context usage ratio before compaction, where `1` means 100%<br>`tokens_before`: token count before compaction<br>`sub_agent`: provided only for sub-agent compaction; contains display name `name` and tool name `tool_name` |

The values of `trigger` are:

| Value | Meaning |
| --- | --- |
| `phase1` | Reduce older tool results. |
| `phase2` | Remove older tool calls and their results. |
| `phase3` | Replace earlier completed turns with a summary. |
| `phase4` | Generate Last Words for continuing the current task and reduce the current turn's content. |
| `force` | Force compaction. |

### Workflow events

| Event | When it runs | Additional fields |
| --- | --- | --- |
| `workflow_run_start` | After a workflow run passes its startup checks, before nodes execute. | `run_id`, `input_text` |
| `workflow_run_end` | After a workflow run ends, including failure and cancellation. | `run_id`, `outcome`, `reason` |

These events fire only in workflow sessions and are for notification and recording; `action: block` is ignored. Session events also fire in workflow sessions, with different timing. For details, see [Lifecycle hooks](./workflows.md#lifecycle-hooks) in the workflow reference.

### Effects of blocking hooks

Only the following events apply decisions that blocking hooks return in the result file:

| Event | Effect |
| --- | --- |
| `user_prompt_submit` | Reject a message, [ask you to confirm it](#asking-you-to-confirm), or add a system reminder. A rejected main-conversation message stays in the input box; if you sent it while the agent was running, the agent keeps working. A rejected side question shows the reason in its card; its reminders apply only to that side question. |
| `before_tool_call` | Reject a tool call, [ask you to confirm it](#asking-you-to-confirm), or modify its arguments, but cannot approve a call or bypass approval. |
| `after_tool_call` | Append context to the tool result, but cannot undo a call that has already occurred. |
| `tool_error` | Append context to the tool error, but cannot undo a call that has already occurred. |

### When iCode waits for async hooks

Async hooks run immediately when triggered, and the current operation continues. If a hook has not finished, iCode waits at the following points:

- At the end of the current turn, it waits for async hooks triggered during that turn, except hooks triggered by `user_interrupt` or side questions.
- When the current session ends, it waits for async hooks triggered by session events or side questions. Because `session_end` is triggered during session shutdown, iCode starts waiting for it immediately.

The wait at session shutdown is limited by `shutdown_grace_seconds`. Each async hook is also subject to its own `timeout_seconds` limit.

## Match conditions

`event` determines which event a hook listens for; `match` further restricts which occurrences trigger it. Omitting `match` or setting it to `{}` adds no filters. When multiple conditions are set, the event must satisfy all of them.

| Field | Type | Applicable events | Match requirement |
| --- | --- | --- | --- |
| `profile` | String | All events | The event's agent name (the base field `profile`) equals the specified value. |
| `profiles` | List of strings | All events | The event's agent name is in the list; an empty list does not restrict the agent name. |
| `tool_kind` | String | Tool and approval events | The tool kind equals the specified value. |
| `tool_name` | String | Tool and approval events | The tool name equals the specified value. |
| `args` | Object | Tool and approval events | Tool arguments meet the specified conditions. See [Matching tool arguments](#matching-tool-arguments). |

Usually, only one of `profile` and `profiles` is needed. If both are set, both must match. For sub-agent events, and for tool, approval, and compaction events inside a sub-agent, the agent name is the sub-agent's profile name, not the main agent's.

Other events do not provide a `tool` object and cannot satisfy filters for tool kind, name, or specific arguments. For available tool kinds and their tool names, see [Tool kinds and names](./tool-kinds-and-names.md).

### Matching tool arguments

`args` uses the parameter names actually defined by the tool as keys, with comparison conditions under each parameter. For example, the following snippet requires the tool's `path` argument to contain `src/` and end with `.py`:

```yaml
match:
  args:
    path:
      contains: "src/"
      regex: '\.py$'
```

| Operator | Match requirement | Example |
| --- | --- | --- |
| `equals` | The argument's entire string equals the specified value. | `equals: "README.md"` |
| `contains` | The argument's string contains the specified substring. | `contains: "src/"` |
| `regex` | Some part of the argument's string matches the Python regular expression. | `regex: '\.py$'` |

Matching follows these rules:

- If multiple parameters or multiple operators for one parameter are specified, all conditions must match. `args: {}` adds no argument filters.
- Missing arguments and `null` values do not match. `path: {}` only checks that `path` exists and is not `null`, without comparing its value.
- Condition values must be strings. In YAML, quote numbers and booleans: an unquoted value such as `equals: 10` or `equals: true` is a configuration error that disables every hook in the file.
- Non-string arguments are converted with Python's `str()` before comparison, so JSON `true` and `false` become `True` and `False` (match them with `equals: "True"`), and lists and objects use Python's representation.
- Use YAML single-quoted strings for regular expressions where possible; backslashes must be escaped inside double quotes. A hook whose regular expression is invalid is skipped when the session starts and a warning names the hook and its file; the other hooks in the file still run.

## Run configuration

`run` specifies the script or command that the hook launches. First, choose a run type with `type`:

| `type` | Use case | Required field |
| --- | --- | --- |
| `script` | Run an existing script, with iCode choosing the runtime based on the file extension. | `path` |
| `command` | Launch an executable directly without shell interpretation. | `argv` |
| `shell` | Run a command string containing shell syntax such as redirection, pipes, or variable expansion. | `shell` |

Fill in only the `path`, `argv`, or `shell` field corresponding to the selected type. All three types support [environment variables and a working directory](#setting-environment-variables-and-the-working-directory).

### Running scripts

Use `script` to run a script file on disk. For example, run `scripts/check.py` relative to the hook configuration file's directory and pass the `--strict` argument:

```yaml
run:
  type: script
  path: scripts/check.py
  args: ["--strict"]
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | `script` | Required | Must be `script`. |
| `path` | String | Required | Script file path. Absolute paths are allowed; relative paths are resolved from the current hook configuration file's directory. |
| `args` | List of strings | `[]` | Arguments passed to the script, appended after the script path in order. |

iCode selects the runtime based on the file extension:

| Extension | Runtime |
| --- | --- |
| `.py` | `uv` or Python, selected as described below |
| `.ps1` | `pwsh` or `powershell` |
| `.sh`, `.bash`, `.zsh` | `bash` or `sh`; on Windows, iCode also looks for Git Bash |
| `.js`, `.mjs` | `node` |
| `.ts` | `npx tsx` |
| `.rb` | `ruby` |
| `.pl` | `perl` |
| Other | Run as a Python script |

For `.py` files, iCode first searches the user's `PATH` for `uv`, `python3`, and `python`, in that order. If it finds `uv`, it runs the script with `uv run`; if it finds Python, it uses that interpreter directly. If none are found, it uses `uv` or Python from iCode's bundled runtime. On Windows, iCode skips the `python` and `python3` placeholders that only offer Python from the Microsoft Store, and keeps looking.

Before running other script types, install the corresponding runtime from the table and ensure iCode can find it through `PATH`.

On Windows, shell scripts need a Windows `bash`, such as the one Git for Windows installs. iCode does not use WSL's `bash`, because it cannot open the script from its Windows path. If iCode finds no other `bash` or `sh`, the hook fails and its error says what is missing.

### Running commands

Use `command` to launch an executable directly. For example, run `git status --short`:

```yaml
run:
  type: command
  argv: ["git", "status", "--short"]
```

iCode does not interpret arguments through a shell, so redirection, pipes, variable expansion, and command substitution do not take effect.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | `command` | Required | Must be `command`. |
| `argv` | List of strings | Required | The executable to launch and its fixed arguments. The list cannot be empty; the first item is the executable. |
| `args` | List of strings | `[]` | Arguments appended to the end of `argv`. |

Write each argument as a separate list item, without shell escaping. You can put all arguments in `argv`, or put trailing arguments in `args`; both forms produce the same result. Prefer `command` when you do not need shell syntax, to avoid differences in quoting and escaping between shells.

> **Note**: On Windows, `python` and `python3` can be placeholders that only offer Python from the Microsoft Store: both are until a Python is on `PATH`, and `python3` stays one after python.org's traditional installer, which adds no `python3`. Windows itself picks the program for `command` and `shell` hooks, so iCode cannot skip the placeholders there: such a hook can fail with exit code 9009, and a `command` hook can instead run the Python iCode itself uses, without your packages. Write the full path of the Python you installed, or turn off the **App Installer** entries for `python.exe` and `python3.exe` under **App execution aliases** in Windows Settings.

### Running shell snippets

Use `shell` to run a complete shell command string. For example, this configuration appends a line to `hook-events.log` in the working directory:

```yaml
run:
  type: shell
  shell: "echo hook triggered >> hook-events.log"
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | `shell` | Required | Must be `shell`. |
| `shell` | String | Required | The complete command string for the shell to interpret. |

On macOS and Linux, iCode uses the shell specified by the `$SHELL` environment variable, or `/bin/sh` if it is unset. On Windows, it uses Command Prompt (CMD). Syntax support may differ between shells.

The shell interprets the entire command. Do not concatenate untrusted content directly into the `shell` string, as this may cause command injection. The `shell` type does not use `args`; put additional content directly in the `shell` string, or use `script` or `command` instead.

### Setting environment variables and the working directory

`env` and `cwd` apply to all three types: `script`, `command`, and `shell`.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `env` | Object mapping strings to strings | `{}` | Environment variables added to the hook process; values override those passed by iCode when names match. |
| `cwd` | String | `""` | The hook process's working directory. If omitted or empty, uses the current session's working directory. |

For example, set environment variables for a script and run it in the current session's working directory:

```yaml
run:
  type: script
  path: scripts/check.py
  env:
    CHECK_LEVEL: strict
    SESSION_NAME: "${session_id}"
  cwd: "${workspace_cwd}"
```

The script path and working directory use different base directories:

| Configuration | Base directory |
| --- | --- |
| Relative `path` | The hook configuration file's directory |
| Omitted or empty `cwd` | The current session's working directory |
| Relative `cwd` | The iCode process's current working directory, which may differ from the session working directory |

When setting `cwd`, use an absolute path or the `${workspace_cwd}` or `${chrys_home}` template. For example, `${chrys_home}/hooks` specifies the `hooks` directory under the iCode configuration directory. The target directory must exist.

Values in `env` and `cwd` support the following templates:

| Template | Value |
| --- | --- |
| `${workspace_cwd}` | The current session's working directory |
| `${chrys_home}` | The iCode configuration directory, usually `~/.chrys` or `%APPDATA%\chrys` |
| `${session_id}` | The current session ID |
| `${profile}` | The event's agent name, the same as the base field `profile` |

iCode performs literal replacement of these templates wherever they appear in `env` values and `cwd`. Other variables or expressions, such as `~`, `$VAR`, `${VAR}`, and `${VAR:-default}`, are not expanded by iCode.

## Execution behavior

`execution` controls whether iCode waits for a hook, whether the hook can keep running after iCode exits, whether unfinished tasks are retried, and how timeouts and failures are handled. All fields can be omitted. The defaults suit most notification and logging scripts: the current operation does not wait for the hook, the hook runs for at most 30 seconds, and a warning is logged if it fails.

Choose `mode` first, then set other fields as needed.

### Choosing a run mode

Choose `mode` based on whether the hook must finish before the current operation continues:

| `mode` | Waiting behavior | Use case | Can affect the current operation? |
| --- | --- | --- | --- |
| `blocking` | Waits for the hook to finish before continuing the current operation; multiple `blocking` hooks run in configuration order | Checks that must finish before an operation continues, or hooks that need to reject or modify an operation | For some events; see [Result file](#result-file) |
| `async` | The current operation continues immediately; see [When iCode waits for async hooks](#when-icode-waits-for-async-hooks) for later waits | Notifications and logging that should not delay the current operation but need to finish before the turn or current session ends | No |
| `fire_and_forget` (default) | The current operation continues immediately; iCode does not wait at the end of the turn or current session | Notifications and logging that may be interrupted by iCode exiting before they finish | No |

### Setting time limits and failure handling

| Field | Type or available values | Default | Description |
| --- | --- | --- | --- |
| `timeout_seconds` | Number greater than `0` | `30.0` | Maximum hook runtime in seconds; ignored when `detach` is enabled. |
| `on_error` | `block`, `warn`, or `ignore` | `warn` | How to handle hook failures; see the table below. |

`on_error` determines how iCode continues when a hook cannot start, times out, or returns a nonzero exit code:

| Value | Behavior |
| --- | --- |
| `block` | For blocking hooks on `user_prompt_submit` or `before_tool_call`, rejects the current operation and displays an error to the user. For other events or non-blocking modes, behaves as `warn`. |
| `warn` | Logs a warning and continues the current operation. |
| `ignore` | Logs the failure only at debug level and continues the current operation. A hook that times out or is stopped still logs a warning. |

When a blocking hook fails, iCode ignores the decision returned by the script and applies only `on_error`.

For example, to require a check before a tool runs and reject the call if the script cannot start, times out, or returns a nonzero exit code:

```yaml
execution:
  mode: blocking
  timeout_seconds: 5
  on_error: block
```

### Continuing after exit

`detach` is a boolean that defaults to `false`. Set it to `true` if a hook that has already started needs to keep running after iCode exits. It is available only with `mode: fire_and_forget`.

When `detach` is enabled, the hook is exempt from `max_parallel_hooks` and `timeout_seconds`. Standard output and standard error are saved in `~/.chrys/hooks/logs` on macOS and Linux, or `%APPDATA%\chrys\hooks\logs` on Windows.

### Retrying unfinished tasks

`delivery` controls whether iCode retries unfinished tasks on a later startup:

| Value | Behavior |
| --- | --- |
| `best_effort` (default) | Does not retry. |
| `durable` | Records pending tasks and retries eligible unfinished tasks when iCode starts. Available only for `async` and `fire_and_forget`. |

Tasks already recorded as failed, such as scripts that returned a nonzero exit code or timed out, are not automatically retried. The same task may run more than once, so scripts must be safe to run repeatedly.

Retry conditions are controlled by the [global settings](#global-settings): `outbox_retry_age_seconds` specifies the minimum wait before retrying, and `outbox_max_retries` limits how many times a task may be started. Setting `delivery: durable` for `blocking` does not enable automatic retries and produces a warning.

`detach` keeps an already-started process running after iCode exits, while `delivery` lets iCode resume unfinished tasks on a later startup. Both can be set together. For example:

```yaml
execution:
  mode: fire_and_forget
  detach: true
  delivery: durable
```

## Script input and output

Scripts locate the current hook's input and result files through environment variables:

| Environment variable | Description |
| --- | --- |
| `CHRYS_HOOK_ID` | The current entry's `id`. |
| `CHRYS_HOOK_EVENT` | The current event name. |
| `CHRYS_HOOK_PAYLOAD_FILE` | Path to the UTF-8 JSON file containing information about this event. |
| `CHRYS_HOOK_RESULT` | Path to the UTF-8 JSON result file that the hook can write; initially empty. |
| `PYTHONUTF8` | Always `1`. |
| `PYTHONIOENCODING` | Always `utf-8`. |

iCode provides both an input file and a result file every time a hook runs. Both files are deleted after the hook finishes; do not save their paths for later use.

Scripts read event information from the input file and write to the [result file](#result-file) when they need to return a decision. Standard output and standard error are not read as decisions; iCode keeps at most 256 KiB of each, from the beginning and the end. This limit does not apply to the log files of hooks with `detach` enabled.

### Base fields

Every input file contains the following fields:

| Field | Meaning |
| --- | --- |
| `schema` | Input data format version, currently `1`. |
| `event` | The name of the event that was triggered. |
| `timestamp` | Event time in UTC. |
| `session_id` | The current session's ID. |
| `profile` | The current agent's name, such as `Code`. For sub-agent events, and for tool, approval, and compaction events inside a sub-agent, this is the sub-agent's profile name. |
| `cwd` | The current session's working directory. `run.cwd` sets the hook process's working directory and does not change this field. |
| `confirmation_available` | Only for `user_prompt_submit` and `before_tool_call`. `true` when this hook can [ask you to confirm](#asking-you-to-confirm): it is a blocking hook, and someone can answer (not in `icode run` or a workflow run from the command line). Use it to decide what to do when nobody can be asked. |

For example, input before a tool call includes the following base fields:

```json
{
  "schema": 1,
  "event": "before_tool_call",
  "timestamp": "2026-05-14T15:00:00Z",
  "session_id": "abc-123",
  "profile": "Code",
  "cwd": "/path/to/working-directory"
}
```

Different events also include their own additional fields. See [Events](#events) for details.

### Tool event fields

The following object shows only the fields added by tool events:

```json
{
  "tool": {
    "name": "edit_file",
    "kind": "filesystem.write",
    "call_id": "abcdef123456",
    "args": {
      "path": "src/example.py",
      "old_string": "old",
      "new_string": "new"
    }
  },
  "result": {
    "text": "OK",
    "duration_ms": 42,
    "error": false,
    "failed": false,
    "approval_rejected": false
  }
}
```

`before_tool_call` has no `result`. `after_tool_call` and `tool_error` include `result`, whose status fields have the following meanings:

| Field | Presence and meaning |
| --- | --- |
| `error` | `true` when the tool call raises an exception; `false` when it returns a failure result normally or the call is rejected. |
| `failed` | `true` when the call is considered failed, including an exception, a recognizable failure result (such as a shell command returning a nonzero exit code), or rejection of the call. |
| `approval_rejected` | Always present; `true` when the call is rejected by the user or a hook, otherwise `false`. |
| `rejection_source` | Present only when the call is rejected; `user` means rejection by the user and `hook` means rejection by a hook. |
| `hook_denied` | Present only when the call is rejected; `true` for rejection by a hook and `false` for rejection by the user. |
| `hook_confirmation` | Present only when a hook [asked you to confirm](#asking-you-to-confirm) the call and it did not go ahead; says why: `declined` (you cancelled), `timed_out` (no answer came in time) or `unavailable` (nobody could be asked). Then `rejection_source` is `hook`. |

When `error: true`, `failed` is also `true`, but `failed: true` does not necessarily mean an exception was raised. `tool_error` is triggered only by exceptions. To check for all types of failure, listen for `after_tool_call` and inspect `result.failed`.

### Approval event fields

The following object shows the fields that `approval_resolved` provides in addition to the base fields:

```json
{
  "request_id": "approval-123",
  "caller_name": "Code",
  "tool": {
    "name": "edit_file",
    "kind": "filesystem.write",
    "call_id": "call-456",
    "args": {
      "path": "src/example.py",
      "old_string": "old",
      "new_string": "new"
    }
  },
  "approved": true
}
```

| Field | Meaning |
| --- | --- |
| `request_id` | Identifier for this approval request; the events at the start and end of the same wait use the same value. |
| `caller_name` | Display name of the agent making the tool call; falls back to the profile name if no display name is configured. |
| `tool` | Tool name, kind, call ID, and arguments when the wait for a human decision begins. Argument edits made during human approval do not update `args` here, so these may not be the final execution arguments. |
| `approved` | Present only in `approval_resolved`; `true` means approval and `false` means rejection. |

For approval events, the base field `profile` is the profile name of the agent making the call, which may differ from `caller_name`.

## Result file

To return a decision, write a UTF-8 JSON object to the file specified by `CHRYS_HOOK_RESULT`. The maximum file size is 1 MiB.

iCode applies decisions only when a blocking hook successfully returns exit code `0`, and only for the events and conditions listed below. Result files from async and fire-and-forget hooks do not change iCode's behavior. If a hook cannot start, times out, or returns a nonzero exit code, its result file is ignored and `on_error` from the [failure handling configuration](#setting-time-limits-and-failure-handling) applies.

The result file supports the following JSON fields:

| Field | Type | Applicable events and conditions | Description |
| --- | --- | --- | --- |
| `action` | `allow`, `block`, `modify`, or `ask` | `user_prompt_submit`, `before_tool_call` | Defaults to `allow`. Both events accept `block` and `ask`; only `before_tool_call` accepts `modify`. |
| `reason` | String | `user_prompt_submit` or `before_tool_call`, with `action: block` or `ask` | With `block`, the rejection reason displayed to the user and, when a tool call is rejected, provided to the model. With `ask`, what iCode shows when it asks you to confirm. |
| `args_override` | Object | `before_tool_call` with `action: modify` | Adds its top-level fields to the existing `tool.args`. Fields with the same name replace the original values; if a value is an object, that entire object is also replaced. |
| `system_reminder` | String | `user_prompt_submit` | Added as a system reminder to every model call in the current turn. |
| `extra_context` | String | `after_tool_call`, `tool_error` | Appended to the tool result text; cannot undo a call that has already occurred. |

Result files are read according to these rules:

- An empty file or no written result is equivalent to `{"action": "allow"}`.
- If the JSON is invalid, the top-level value is not an object, or the file exceeds 1 MiB, iCode logs a warning and ignores the result, treating it as no decision.
- Unknown fields are ignored.

For how decisions from multiple hooks are combined, see [Execution order and configuration merging for multiple hooks](#execution-order-and-configuration-merging-for-multiple-hooks).

### Asking you to confirm

A blocking `user_prompt_submit` or `before_tool_call` hook can leave the decision to you instead of allowing or rejecting the operation itself. It writes `action: ask` and a `reason`, then exits:

```json
{"action": "ask", "reason": "This changes the production configuration. Continue?"}
```

iCode then pauses the operation and asks you, showing the hook, its reason, and the tool call or your message. An editor connected over ACP shows it as a permission request.

- **Continue**: the remaining hooks run and the operation goes on. A tool call still needs approval where your approval settings require it.
- **Cancel**: the operation doesn't go ahead. A rejected tool call is reported to the model; a rejected message stays in the input box (an ACP editor shows `Input rejected:` and the reason, and `icode run` exits with an error).
- **No answer**: by default iCode waits until you answer or the operation ends, and so does the tool call, or the workflow run, that asked. If you set **Human approval timeout (seconds)**, the operation is rejected once that time passes unanswered. It is also rejected at once when nobody can be asked, such as in `icode run`; a hook can check `confirmation_available` to decide differently. If the operation ends first, for example because you interrupted or cancelled the workflow run, the question closes by itself.

The time you take doesn't count toward the hook's `timeout_seconds`. Your answer applies only to this operation; if the tool call's arguments change after you continued (a later hook changes them, or you edit them when approving the call), you may be asked again. To change arguments before asking, put a hook that returns `modify` before the one that returns `ask`.

## Execution order and configuration merging for multiple hooks

### Execution order

When both project and global configuration are loaded, execution mode determines the phase in which a hook runs, and configuration source determines the order within each phase. For a single event, iCode processes hooks in two phases:

1. It checks and runs matching blocking hooks one by one: project hooks first, then global hooks, preserving definition order within each configuration file.
2. If no hook rejects the operation, iCode starts matching async and fire-and-forget hooks: first in project configuration order, then in global configuration order. These non-blocking hooks may run concurrently, so they may finish in a different order from the one in which they started.

Decisions returned by blocking hooks are combined as follows:

| Decision | How results from multiple blocking hooks are combined |
| --- | --- |
| `args_override` | Modifies tool arguments immediately. Subsequent hooks use the modified arguments for matching and checks. If multiple hooks modify the same argument, the later hook overrides the earlier one. |
| `system_reminder`, `extra_context` | Accumulated in hook execution order. |
| `action: block` | For `user_prompt_submit` and `before_tool_call`, the first rejection stops all remaining hooks for the current event, including non-blocking hooks that have not started. |
| `action: ask` | iCode asks you before the next hook runs. If you continue, the remaining hooks run; otherwise it is handled like a rejection. If several hooks ask, you are asked once for each. |

For `user_prompt_submit` and `before_tool_call`, a blocking hook that fails with `on_error: block` also rejects the current operation and stops the remaining hooks for that event.

As a result, after a project hook modifies tool arguments, subsequent global hooks see the modified arguments. After a project hook rejects an operation, global hooks for that event do not run.

### Duplicate IDs do not override hooks

Project and global configurations may define the same `id`. Both hooks are retained, and iCode logs a duplicate-name warning.

Duplicate `id` values within one configuration file cause that file to fail to load. An `id` identifies a hook; it is not an override key. Defining a hook with the same name in project configuration cannot override or disable a global hook. To disable a global hook, edit the global configuration and set that hook's `enabled` to `false`, or delete its definition.

### Settings merging and load failures

`settings` in project and global configurations are merged field by field: explicitly set project values take precedence, followed by global values, with defaults used when neither file sets a field.

If either configuration file is invalid, iCode displays a warning and disables all hooks defined in that file; a valid configuration file in the other location still takes effect. No warning is issued when a configuration file is not found. To see every problem in a file, run [`icode hook validate`](#icode-hook-validate).

## Command line

### `icode hook list`

List the hooks a session started in the current directory loads, without running any of them. `icode hook` without a command does the same.

```shell
icode hook list [--json]
```

Each configuration file found is listed, the project file first, then the global one. Its first line gives its path and one of these states:

| State | Meaning |
| --- | --- |
| `loaded` | A session started here runs the hooks in this file |
| `not loaded` | Project hooks are turned off; see [Loading configuration files](#loading-configuration-files). If the file also has an error, the first one is shown too |
| `invalid, not loaded` | The file has an error, so a session runs none of its hooks. The first error is shown; run `icode hook validate` to see them all |

The file's hooks follow in order. Each starts with its ID and, in parentheses, its event and run mode, plus `turned off` when its `enabled` is `false`. A `match:` line follows with its match conditions, when it has any, then a `run:` line: `script` and the script path, `command` and the program, or `shell`. Command arguments, environment variable values and shell snippets are not shown, because they can contain passwords or tokens. The script path and the program are shown only when they can be found from the folder you run the command in: a file that exists, or a program on `PATH`. Otherwise `…` stands in for them, because what was written there may hold more, as in `"notify --token abc"`. A program found only through the hook's own `cwd` or `PATH` setting also shows `…`. `icode hook validate` reports the scripts and programs it can tell are missing. A hook left out because a regular expression in its match conditions is invalid is shown on a `skipped` line.

```text
Project hooks: ./.chrys/hooks/hooks.yaml (loaded)
  record-turn (after_turn, fire_and_forget)
    run: script scripts/record_turn.py
  protect-env (before_tool_call, blocking)
    match: tool_kind=filesystem.write, args.path matches '(^|[\\/])\.env$'
    run: script scripts/protect_env.py
```

The command exits with code 0 even when a file is invalid; to check files, use `icode hook validate`.

With `--json`, stdout holds one object: `version` (currently `1`), `files`, and `searched` (the folders looked in). Each item in `files` contains `scope` (`project` or `global`), `path`, `state` (`loaded`, `not_loaded`, or `invalid`; `not_loaded` whenever project hooks are turned off), `error` (the error that keeps a session from loading the file, in the form of a `validate` diagnostic, or `null`; other errors, such as a missing script, are reported only by `validate`), `hooks`, and `skipped` (each with `id` and `reason`). Each hook contains `id`, `event`, `enabled`, `description`, `execution`, `match`, and `run`; in `run`, `type` is `script`, `command`, or `shell`, and `path` is the script path and `program` the command's program, each `null` for the other types of hook or when the listing shows `…`.

### `icode hook validate`

Check hook configuration files the way a session loads them, and report every problem with its file and line. No hook is run.

```shell
icode hook validate [<path>] [--json]
```

Without `<path>`, the command checks the project file of the current directory and the global file. It checks the project file even when project hooks are turned off. `<path>` is a hook configuration file, which can have any name and be anywhere, or a folder; for a folder, it checks the file a session would read there.

A session stops at the first error and ignores the whole file. Validation goes on and reports every hook with a problem. It also warns about settings that are accepted but probably do not do what you meant.

Each problem is shown as `file:line: severity: message [code]`. The last line is the result:

```text
./my-hooks.yaml:2: warning: hook 'record-turn': match.tool_kind never matches on after_turn, which has no tool [match_never_fires]
./my-hooks.yaml:9: error: hook 'protect-env': execution.mode must be 'blocking' / 'async' / 'fire_and_forget', got 'block' [schema]
FAIL 1 file · 2 hooks: 1 error, 1 warning
```

When no errors are found, the result is `PASS`, such as `PASS 2 files · 5 hooks · 1 warning`. Warnings do not fail validation. The exit code is `0` for `PASS` and `1` for `FAIL`.

These problems are errors:

| Code | Meaning |
| --- | --- |
| `syntax` | The file is not valid YAML or JSON |
| `decode` | The file's text encoding cannot be read |
| `unreadable` | The file cannot be read, for example because of its permissions |
| `schema` | A field is missing, unknown, or has an invalid value, or an `id` is used twice in the file |
| `regex_invalid` | A regular expression in `match.args` is invalid. A session leaves this hook out and still loads the others. Hooks that are turned off are checked too |
| `script_missing` | The script of an enabled hook does not exist |
| `no_config` | No hook configuration file was found where the command looked |
| `not_found` | Nothing exists at `<path>` |

After a `syntax`, `decode`, `unreadable`, or `schema` error, a session loads none of the file's hooks.

These problems are warnings:

| Code | Meaning |
| --- | --- |
| `match_never_fires` | `match.tool_kind`, `match.tool_name`, or `match.args` is set for an event that has no tool call, so the hook never runs |
| `unknown_tool_kind` | `match.tool_kind` is not a known tool kind; see [Tool kinds and names](tool-kinds-and-names.md) |
| `unknown_profile` | `match.profile` or `match.profiles` names an agent that does not exist on this computer |
| `on_error_block_ignored` | `on_error: block` is set on a hook that cannot block, so its failures are handled as `warn` |
| `durable_blocking` | `delivery: durable` is set on a blocking hook, which does not use it |
| `broad_before_tool_call` | A `before_tool_call` hook has no match conditions, so it runs for every tool call |
| `duplicate_id_across_files` | The project and global files both define this `id`; both hooks run |
| `shadowed_file` | The folder holds more than one hook configuration file; only the first is read (see [Configuration file locations](#configuration-file-locations)) |
| `program_not_found` | The program that runs an enabled hook's command or script is not found on `PATH`, or on Windows is only the placeholder that offers Python from the Microsoft Store |
| `unknown_template_var` | `run.cwd` or `run.env` uses a `${...}` variable that iCode does not fill in, or `run.argv` or `run.args` uses any `${...}` variable (iCode fills in variables only in `cwd` and `env` values), so it is passed on as written |

Missing scripts and programs are checked only for enabled hooks. A hook whose `run.env` sets `PATH` is not checked for a missing program.

With `--json`, stdout holds one object. Every field is always present; unknown values are `null`.

| Field | Type and meaning |
| --- | --- |
| `version` | Integer: report format version, currently `1` |
| `status` | String: `pass` or `fail` |
| `files` | Array of objects: each file checked, with its `scope` (`project`, `global`, or `path` for a file named on the command line), `path`, `loaded` (whether a session started here loads it; `null` for `path`), and `hook_count` (the number of hook entries) |
| `searched` | Array of strings: the folders, or the path, the command looked in |
| `diagnostics` | Array of objects: each problem's `severity` (`error` or `warning`), `code`, `message`, `file`, `line` and `column` (starting at 1), `hook_id`, and `entry` (the hook's position in `hooks`, starting at 0) |
