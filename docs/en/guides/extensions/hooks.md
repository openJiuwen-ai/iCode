# Configure and write hooks

Hooks automatically run local scripts or commands when specified events occur. For example, a hook can record results after a turn ends, check arguments before a tool runs, or add instructions for the model when a user submits a message.

This guide uses a simple example to introduce writing and configuring hooks, followed by several common examples you can adapt. For the full list of events, configuration fields, defaults, and script input and output formats, see the [Hooks configuration reference](../../reference/hooks.md).

> **Security notice**
>
> Hooks run directly with the current user's permissions, without tool approval or sandbox restrictions. Before enabling hooks included in a project, inspect their configuration files and the scripts and commands they run.
>
> Hooks may receive conversation content, tool arguments, and tool results. If a hook sends this data to an external service, check whether it contains sensitive information.

## Create your first hook

The following steps create a project hook in the working directory of the current iCode session. It applies only to this working directory and appends the turn status to `hook-events.log` in that directory after each turn ends.

### 1. Create the configuration

Create `.chrys/hooks/hooks.yaml`:

```yaml
hooks:
  - id: record-turn
    event: after_turn
    run:
      type: script
      path: scripts/record_turn.py
```

`id` is the hook's stable identifier and must be unique within a configuration file. This example uses `record-turn`.

This configuration runs `scripts/record_turn.py` when the `after_turn` event occurs. Relative script paths are resolved from the directory containing the hook configuration file, so this points to `.chrys/hooks/scripts/record_turn.py`, which you create in the next step.

### 2. Create the script

Create `.chrys/hooks/scripts/record_turn.py`:

```python
import json
import os
from pathlib import Path

with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as payload_file:
    payload = json.load(payload_file)

with Path("hook-events.log").open("a", encoding="utf-8") as log_file:
    log_file.write(f"turn={payload['turn']} status={payload['status']}\n")
```

iCode provides the event's JSON input file through the `CHRYS_HOOK_PAYLOAD_FILE` environment variable. The input for `after_turn` includes `turn` and `status`, which contain the turn number and ending status. By default, the script runs in the current iCode session's working directory, so `hook-events.log` is also written there.

### 3. Load and verify

To check the configuration, run this in a terminal in the working directory:

```shell
icode hook validate
```

It checks the project and global hook files without running any hook. It lists each problem with its file and line, then ends with `PASS` when no errors are found, or `FAIL` and the number of errors. Fix what it reports and run it again until it prints a line like this:

```text
PASS 1 file · 1 hook
```

Project hooks are not loaded by default. In the terminal user interface (TUI), press **F10** to open **Settings**, then turn on **Load project hooks** in the **Project trust** section of **Safety**. If it is already on, switch sessions or restart iCode to apply the configuration on disk.

Run `icode hook list` to see the hooks a session started in this directory loads. The `Project hooks` line should end with `(loaded)`, with `record-turn` listed below it.

In the TUI, enter `/runtime` and open the **Hooks** tab. You should see `record-turn`. Submit a message and wait for the current turn to end, then check `hook-events.log` in the working directory. It should contain a line like this:

```text
turn=1 status=ok
```

## Example: block an operation based on tool arguments

To check or reject an operation before a tool runs, use the `before_tool_call` event with blocking execution. This example prevents file-writing tools from modifying files named `.env`.

First, add this entry to the `hooks` list in `.chrys/hooks/hooks.yaml`:

```yaml
  - id: protect-env
    event: before_tool_call
    match:
      tool_kind: filesystem.write
      args:
        path:
          regex: "(^|[\\\\/])\\.env$"
    run:
      type: script
      path: scripts/protect_env.py
    execution:
      mode: blocking
      timeout_seconds: 5
      on_error: block
```

This configuration matches only `filesystem.write` tool calls that have a `path` argument whose path ends in `.env`. Blocking hooks finish before iCode continues processing the call.

Then create `.chrys/hooks/scripts/protect_env.py`:

```python
import json
import os

with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as result_file:
    json.dump(
        {"action": "block", "reason": "Modifying .env with file-writing tools is not allowed"},
        result_file,
        ensure_ascii=False,
    )
```

The script returns `action: block` and the rejection reason through the result file specified by `CHRYS_HOOK_RESULT`. What a script prints to standard output or standard error is not read as a decision, and iCode keeps at most 256 KiB of each, from the beginning and the end.

`on_error: block` also rejects the tool call if the script fails to start, times out, or returns a nonzero exit code. Use it for checks that must keep the restriction in place when they fail. This hook blocks only matching `filesystem.write` calls; shell commands are unaffected.

After switching sessions or restarting iCode, create a dedicated test file, `hook-demo/.env`, with known content such as `HOOK_TEST=unchanged`. Ask the agent to attempt one modification using only `write_file` or `edit_file`, then stop after rejection without using the shell or another method. Check that the tool call is rejected before execution and displays the reason provided by the script. Then confirm that the file content is unchanged. You can delete `hook-demo/.env` after verification.

## Example: ask before an operation

Instead of rejecting an operation itself, a hook can leave the decision to you. This example asks before any shell command that mentions `prod`.

Add this entry to the `hooks` list in `.chrys/hooks/hooks.yaml`:

```yaml
  - id: confirm-prod
    event: before_tool_call
    match:
      tool_kind: shell
      args:
        command:
          contains: prod
    run:
      type: script
      path: scripts/confirm_prod.py
    execution:
      mode: blocking
      timeout_seconds: 5
```

Then create `.chrys/hooks/scripts/confirm_prod.py`:

```python
import json
import os

with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as result_file:
    json.dump(
        {"action": "ask", "reason": "This command mentions prod. Run it?"},
        result_file,
        ensure_ascii=False,
    )
```

When the agent runs a matching command, iCode asks you with the hook's reason and the command. **Continue** lets it run; **Cancel** rejects it. The time you take doesn't count toward `timeout_seconds`. For the full rules, see [Asking you to confirm](../../reference/hooks.md#asking-you-to-confirm).

## Example: add a system reminder for the model

A `user_prompt_submit` hook can add instructions or hints for the model when a user submits a message. This example applies only to the Code agent and reminds it to run project tests after modifying Python files.

First, add this entry to the `hooks` list in `.chrys/hooks/hooks.yaml`:

```yaml
  - id: remind-python-tests
    event: user_prompt_submit
    match:
      profile: Code
    run:
      type: script
      path: scripts/remind_python_tests.py
    execution:
      mode: blocking
      timeout_seconds: 5
```

Then create `.chrys/hooks/scripts/remind_python_tests.py`:

```python
import json
import os

with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as result_file:
    json.dump(
        {"system_reminder": "After modifying Python files, run the tests relevant to the changes."},
        result_file,
        ensure_ascii=False,
    )
```

Switch sessions or restart iCode to apply the configuration. The system reminder takes effect only after the blocking hook finishes successfully. iCode wraps the reminder in `<system-reminder>` tags and sends it to the model along with the user's submitted message. The reminder does not appear in the TUI conversation and is not saved in session data.

To confirm that a `user_prompt_submit` hook is working, or to inspect the actual system reminder sent to the model, check the raw HTTP log of model requests. This log contains unredacted API keys and full conversation content, so enable it only temporarily for troubleshooting. In the TUI, press **F10**, go to **Safety** > **Diagnostics**, enable **Capture raw model HTTP requests**, and restart iCode. Submit a message using the Code agent, then search for the reminder text "After modifying Python files" in `llm_raw_http.jsonl` in the current session directory. For its location, see [Find the session ID and storage location](../daily-use/sessions.md#find-the-session-id-and-storage-location). After verification, disable **Capture raw model HTTP requests** and restart iCode again.

To add a reminder only for certain tasks, have the script read `text` (the message the user just submitted) from `CHRYS_HOOK_PAYLOAD_FILE` and write `system_reminder` to `CHRYS_HOOK_RESULT` only when the message meets your conditions.

## Choose an execution mode

For most hooks, choose an execution mode based on whether the current operation must wait for the result:

| Need | Recommended mode | Behavior |
| --- | --- | --- |
| Check, reject, or modify an operation before it continues | `blocking` | iCode waits for the hook to finish; some events apply the action decision from the result file |
| Continue the current operation immediately, but let iCode wait for a limited time when the current turn or session ends | `async` | iCode continues immediately, then waits for the hook to finish at applicable events, within the wait limit |
| Send ordinary notifications or record information in a task that can be interrupted by exit | `fire_and_forget` | iCode continues immediately and does not wait when the current turn or session ends; this is the default mode |

An asynchronous hook (`async`) does not delay the event that triggered it or the current operation. When the current turn or session ends, if an applicable asynchronous hook is still running, iCode waits for it to finish before ending the turn or session. iCode does not wait for asynchronous hooks triggered by `user_interrupt`. The wait limit at session end is configurable in [Global settings](../../reference/hooks.md#global-settings). To retry unfinished tasks after iCode exits, also configure `delivery: durable`.

## See which hooks ran

In a chat, the bottom-right corner shows the hook iCode is waiting for. Select it to open the **Hooks** tab of the sidebar (**Ctrl+G** shows or hides the sidebar) and, when a hook is shown there, that hook's details over it; the list scrolls to that hook's row and marks it.

The **Hooks** tab lists the hooks that ran since iCode started, in every chat session (hooks that workflows run aren't listed), numbered in the order they started, the newest at the bottom: those still running or waiting for their turn, and the 100 that ended last. Each row shows its number, an icon for how it went (for example `✗` for a hook that failed, `⊘` for one that stopped the operation, `?` for one waiting for your answer), the hook's name, and at the right end how long it ran. Its details say what triggered it and what came of it. **Clear ended** removes the runs that ended from the list; running ones stay. Cleared runs can't be shown again, and new runs keep counting from the last number. Once more than 100 runs have ended, the top of the list says that only the latest 100 are kept.

Click a run to see its details. **Input** shows the payload iCode gave the hook and how it was run; **Output** shows its result file, what iCode took from it (or why it took nothing), and what it printed to stdout and stderr. Click a box to copy its text, or press **c** to copy the whole tab; very long text shows only in part, but a copy still takes all of it. The list starts empty each time iCode starts; to see the hooks iCode loaded, enter `/runtime` and open the **Hooks** tab.

## Use global hooks

The examples above all live in `.chrys/hooks` in the current working directory and apply only to that working directory. To use the same hook across all working directories, use global configuration:

| Platform | Global configuration file |
| --- | --- |
| macOS and Linux | `~/.chrys/hooks/hooks.yaml` |
| Windows | `%APPDATA%\chrys\hooks\hooks.yaml` |

Project and global configurations can be loaded together. For execution order across both locations, duplicate IDs, and `settings` merge rules, see [Execution order and configuration merging for multiple hooks](../../reference/hooks.md#execution-order-and-configuration-merging-for-multiple-hooks).
