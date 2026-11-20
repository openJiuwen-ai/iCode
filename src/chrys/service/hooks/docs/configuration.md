# Chrys Hooks — Configuration Reference

Hooks are configured in two places: a global file under your Chrys
config directory, and an optional working directory file in the workspace.

```
<config_dir>/hooks/hooks.yaml       # also .yml or .json   (global)
<workspace.primary_cwd>/.chrys/hooks/hooks.yaml             (working directory, optional)
```

On macOS / Linux `<config_dir>` is typically `~/.chrys/`; on Windows it is
`%APPDATA%\chrys`. The files are optional — if neither is present, the hook
system is a complete no-op. See
[Working directory configuration](#working-directory-configuration) below for
the merge rules.

This document is the full reference for the file format. For the runtime
model see [design.md](design.md); for writing the hook scripts themselves
see [authoring.md](authoring.md).

---

## Top-level shape

```yaml
version: 1

settings:
  shutdown_grace_seconds: 5
  max_parallel_hooks: 4
  outbox_retry_age_seconds: 60
  outbox_max_retries: 3

hooks:
  - id: lint-on-write
    event: after_tool_call
    enabled: true
    description: "Run ruff after every edit_file / write_file"
    match:
      tool_name: edit_file
      args:
        path:
          regex: "\\.py$"
    run:
      type: script
      path: scripts/run_ruff.py
    execution:
      mode: async
      timeout_seconds: 30
      on_error: warn
```

Unknown top-level keys are rejected at load time. Unknown keys inside
nested sections are also rejected — typos do not silently disappear.

---

## `version`

Optional; defaults to `1`. Loading a file with a different version raises an
error.

---

## `settings`

File-level knobs. All fields optional.

| Field | Default | Meaning |
|---|---:|---|
| `shutdown_grace_seconds` | `5.0` | Max time `drain_session()` waits for in-flight async hooks before cancelling them. |
| `max_parallel_hooks` | `4` | Cap on concurrent non-detached subprocesses. Detached hooks are not counted. |
| `outbox_retry_age_seconds` | `60.0` | Pending outbox entries younger than this are skipped on startup recovery (assumed to be in-flight from another process). |
| `outbox_max_retries` | `3` | Pending entries that have already been retried this many times are moved straight to `failed/` instead of re-running. |

---

## Working directory configuration

In addition to the global `<config_dir>/hooks/hooks.yaml`, Chrys looks
for a working directory file at:

```
<workspace.primary_cwd>/.chrys/hooks/hooks.yaml      # .yml / .json also accepted
```

The working directory file uses the exact same schema as the global file. When
both are present, they are merged:

- **Working directory hooks run first** (in their YAML order), then **global
  hooks** (in their YAML order).
- **`match` clauses** still scope by `profile` / `tool_kind` /
  `tool_name` / `args`; nothing changes about matching.
- **`modify` (args rewrite) flows working directory → global**: a working directory's
  `modify` rewrites the envelope, and the global layer sees the
  rewritten args.
- **`block` short-circuits across sources**: on a gated event, a
  working directory block ends the chain — global hooks for the **same event**
  are skipped. The action is denied.
- **Observer events** (`session_*`, `before_turn`, `after_turn`,
  `approval_requested`, `approval_resolved`, `after_tool_call`, `tool_error`, `sub_agent_*`, `pre_compact`,
  `user_interrupt`) ignore block decisions; both layers run.
- **`settings`** are per-field merged: a working directory field wins, then
  the global field, then the default.
- **`id` collisions** across files are allowed; both hooks run
  (working directory first) and a `WARNING` is logged at load time. To
  suppress a global hook, set `enabled: false` in the global file.

`HookRun.path` is resolved relative to the **file's own directory**:
a `path: scripts/foo.py` in the working directory file resolves to
`<working-directory>/.chrys/hooks/scripts/foo.py`; the same in the global file
resolves to `<config_dir>/hooks/scripts/foo.py`.

Missing working directory file is silent and equivalent to no working directory hooks.
Malformed working directory file: a `WARNING` event is published and the
global file is unaffected.

Working directory hook config is loaded for the session's `primary_cwd`. Changing
that cwd rebuilds the hook manager; profile/model switches that keep the
same cwd reuse the current manager.

Trust model: a working directory hooks file ships with the repo and runs
arbitrary subprocesses without tool approval. It loads only while the
user's own `workdir.hooks_enabled` setting is on (default off; a
working directory file cannot set it). Otherwise the file is never opened, and
the settings load reports it as a `workdir_hooks_dormant` warning.

---

## `hooks`

A list of hook entries. Each entry has the fields below.

### `id` (required, string)

Stable identifier. Used in logs, outbox filenames, and the future `/hooks`
TUI screen. Duplicates within a single file are rejected at load time.

### `run` (required, mapping)

What to execute. See [the `run` section below](#run) for the full shape.
Omitting `run` is a load-time error.

### `event` (required, string)

Which lifecycle point fires this hook. See [design.md](design.md#events)
for the full list. Valid values:

```
session_start  session_restored  session_end
before_turn    after_turn        user_prompt_submit
before_tool_call  after_tool_call  tool_error
approval_requested  approval_resolved
sub_agent_start   sub_agent_end
pre_compact   user_interrupt
```

### `enabled` (optional, bool, default `true`)

Set to `false` to keep an entry around without firing it. Cheaper than
deleting + re-adding while iterating.

### `description` (optional, string)

Free-form. Surfaced in the future `/hooks` TUI screen; ignored at runtime.

---

## `match`

Filter clause. All set sub-clauses are AND-ed. Omit the whole block to fire
on every event of this type.

```yaml
match:
  profile: Code                    # exact profile name match
  profiles: [Code, QA, Explore]    # any-of profile match
  tool_kind: filesystem.write        # canonical kind (bare form)
  tool_name: edit_file             # specific tool function name
  args:
    path:
      equals: "/etc/passwd"        # exact string match
      contains: "/src/"            # substring match
      regex: "\\.py$"              # Python re.search
```

Notes:

- `tool_kind` takes the canonical kind names
  (`shell`, `filesystem.read`, `filesystem.write`, `search`, `ask_user`,
  `sleep`, `sub_agent`, `mcp`, `doc_converter`) — runtime events carry the
  same bare form. Unknown / third-party kind strings pass through unchanged.
- Tool filters require a `tool` payload. If you set `tool_kind`,
  `tool_name`, or `args` on an event without a tool (session, turn,
  compaction, interrupt), the hook will not match.
- Within a tool event, if an arg named in `match.args` is missing from
  the actual call, the match fails. There is no "wildcard-on-missing"
  form.
- `match.args` keys are tool argument names and are never kind-normalized.
- Non-string arg values are stringified via Python's `str()` before
  comparison, so `args.timeout.equals: "30"` will match an integer
  argument `30`.
- Setting both `profile` and `profiles` is allowed; both must match.
- An enabled hook whose regex does not compile is left out at load time
  (`HooksFile.skipped_hooks`); the rest of the file still loads.

---

## `run`

What to execute. Three flavours; pick one via `type`.

### `type: script`

A file on disk. The interpreter is inferred from the extension.

```yaml
run:
  type: script
  path: scripts/lint_python.py    # relative to <config_dir>/hooks/
  args: ["--format=json"]         # appended after the script path
  env:
    LINT_LEVEL: strict
  cwd: ""                         # falls back to workspace cwd
```

Supported extensions and interpreters:

| Suffix | Runner |
|---|---|
| `.py` | `uv run` if available, else `python3` / `python`, else Chrys' current Python |
| `.ps1` | `pwsh` / `powershell` |
| `.sh`, `.bash`, `.zsh` | `bash` / `sh` (or Git Bash on Windows) |
| `.js`, `.mjs` | `node` |
| `.ts` | `npx tsx` |
| `.rb` | `ruby` |
| `.pl` | `perl` |
| anything else | falls back to Python |

Relative `path` resolves against the directory containing the hooks
file. In the global file that is `<config_dir>/hooks/`; in a working directory
file it is `<working-directory>/.chrys/hooks/`.

### `type: command`

Explicit argv. No shell, so no quoting pitfalls.

```yaml
run:
  type: command
  argv: ["/usr/local/bin/notify-send", "Chrys"]
  args: ["Agent finished a turn"]   # appended to argv
  env:
    DISPLAY: ":0"
```

`argv` must be non-empty and all entries must be strings.

### `type: shell`

A single shell-syntax string executed via the user's shell. POSIX uses
`$SHELL` (fallback `/bin/sh`); Windows uses `cmd.exe /c`.

```yaml
run:
  type: shell
  shell: 'echo "$CHRYS_HOOK_EVENT" >> ~/chrys-events.log'
```

`args` is ignored for `type: shell`.

### Template variables in `cwd` and `env`

Both fields support `${var}` substitution. Available variables:

```
${workspace_cwd}   The agent's workspace cwd at dispatch time
${chrys_home}      <config_dir>
${session_id}      Current session id
${profile}         Active profile name
```

---

## `execution`

Wait / lifetime / delivery semantics. All fields optional.

```yaml
execution:
  mode: blocking          # blocking | async | fire_and_forget (default: fire_and_forget)
  detach: false           # only valid with mode: fire_and_forget
  delivery: best_effort   # best_effort | durable
  timeout_seconds: 30
  on_error: warn          # block | warn | ignore
```

> If you omit `execution.mode`, the default is `fire_and_forget` — your hook
> will run side-effect-only and **cannot block or modify** anything. Set
> `mode: blocking` explicitly when you want the hook to gate the action.

### `mode`

See [design.md — Modes](design.md#modes).

| Value | Wait? | Used for |
|---|---|---|
| `blocking` | dispatch awaits | Gating: deny / modify tool args, refuse prompts, append context |
| `async` | drained at turn end | Telemetry, linters, notifications that should finish before the next prompt |
| `fire_and_forget` | never drained | Side-effects that need not block anything |

### `detach`

Only valid with `mode: fire_and_forget`. When `true`:

- POSIX: `start_new_session=True` (new process group).
- Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`.
- Stdio is redirected to an owner-only log file under
  `<config_dir>/hooks/logs/` where the platform supports it.
- Survives `engine.shutdown()`.

Loader rejects `detach: true` with `mode: blocking` or `mode: async`.

### `delivery`

| Value | Behaviour |
|---|---|
| `best_effort` (default) | No durable state; lost on crash. |
| `durable` | Write a job file to `outbox/pending/` before spawning; move to `done/` or `failed/` on completion. On startup, stale pending entries are retried. Semantics are at-least-once — hooks must be idempotent. |

Durable applies only to non-blocking modes. A blocking + durable
combination logs a warning at load time and silently uses
`best_effort` semantics (the hook is already awaited inline).

### `timeout_seconds`

Hard timeout for `blocking`, `async`, and non-detached `fire_and_forget`
modes. Default `30.0`. Must be > 0. Ignored for detached hooks (the
detached worker owns its child after Chrys exits).

### `on_error`

Behaviour when the hook fails (non-zero exit, timeout, or launch error):

| Value | Effect |
|---|---|
| `block` | On a `mode: blocking` hook for a gated event (`before_tool_call`, `user_prompt_submit`), deny the action and surface the hook's stderr. On non-blocking hooks and non-gated events, falls back to `warn`. |
| `warn` (default) | Log a warning, allow the action. |
| `ignore` | Log at debug level only. |

When a blocking hook fails, the manager **ignores** its
`CHRYS_HOOK_RESULT` file — the file may be partial or stale. The
`on_error` policy is the only signal that matters.

---

## Validation

The loader is strict:

- Unknown top-level keys → error.
- Unknown keys inside any nested mapping → error.
- Wrong types → error (e.g. `enabled: "yes"` instead of `enabled: yes`).
- Duplicate `id` within a single file → error.
- `mode: blocking` + `detach: true` → error.
- `mode: blocking` + `delivery: durable` → warning at load time.
- `event: before_tool_call` with no `match` filters → warning at load
  time (the hook will fire for every tool call).

If global loading fails, the engine publishes a
`Warning(code="hooks_config_invalid")` event and continues with global
hooks disabled. If working directory loading fails, it publishes
`Warning(code="workdir_hooks_config_invalid")` and continues with
working directory hooks disabled. A valid source in the other layer is still used.
Each hook left out for an invalid regex gets its own
`Warning(code="hook_skipped")` naming the hook id and file.

---

## Examples

### Deny writes to a sensitive path

```yaml
- id: protect-etc
  event: before_tool_call
  match:
    tool_kind: filesystem.write
    args:
      path:
        contains: "/etc/"
  run:
    type: script
    path: scripts/deny.sh
  execution:
    mode: blocking
    on_error: block
```

### Rewrite tool args before execution

```yaml
- id: redact-secrets-in-shell
  event: before_tool_call
  match:
    tool_kind: shell          # matches zsh/bash/pwsh/powershell/cmd/etc.
  run:
    type: script
    path: scripts/redact_secrets.py
  execution:
    mode: blocking
    timeout_seconds: 5
    on_error: warn
```

### Lint after every Python edit (background)

```yaml
- id: ruff-after-edit
  event: after_tool_call
  match:
    tool_name: edit_file
    args:
      path:
        regex: "\\.py$"
  run:
    type: command
    argv: ["uv", "run", "ruff", "check", "--quiet"]
    args: []
  execution:
    mode: async
    timeout_seconds: 60
    on_error: ignore
```

### Append a working directory reminder to every prompt

```yaml
- id: working directory-reminders
  event: user_prompt_submit
  run:
    type: script
    path: scripts/inject_reminders.py
  execution:
    mode: blocking
    timeout_seconds: 2
    on_error: warn
```

### Fire-and-forget desktop notification on turn completion

```yaml
- id: notify-turn-done
  event: after_turn
  run:
    type: shell
    shell: 'osascript -e "display notification \"Turn complete\" with title \"Chrys\""'
  execution:
    mode: fire_and_forget
    on_error: ignore
```

### Durable telemetry on session end (survives Chrys exit)

```yaml
- id: telemetry-session-end
  event: session_end
  run:
    type: script
    path: scripts/post_telemetry.py
  execution:
    mode: fire_and_forget
    detach: true
    delivery: durable
    on_error: ignore
```

### Workflow lifecycle

Workflow sessions use `session_start`, `session_restored`, and `session_end` for
an execution attachment's lifetime in the host. Viewing history does not attach
it. The first admitted run creates a new attachment or restores a saved session;
subsequent runs reuse it. Shutdown or deletion ends the attachment. Reloading a
run's hook configuration does not emit another session start/end pair.

`workflow_run_start` fires after admission, before node execution, with `run_id`
and `input_text`. `workflow_run_end` fires after the final checkpoint and terminal
outcome, with `run_id`, `outcome`, and `reason`, including cancellation and failure.
Both are observers. Their common envelope contains `session_kind: workflow`,
`workflow_id`, `session_id`, `cwd`, and an empty `profile` (agents belong to nodes).

Every accepted run records its start and end, even when cancelled before any
node executes. Session and run startup hooks are dispatched in that order;
cancellation interrupts their blocking work and drains it before run-end. A
startup hook can therefore be interrupted without dropping the run's start
notification or trajectory span.

Run-scoped work is drained before resources are released; the session attachment
remains until it is ended explicitly or the host shuts down.
