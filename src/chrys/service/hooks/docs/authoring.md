# Chrys Hooks — Authoring Guide

This document is for people writing the actual hook scripts. The runtime
contract is the same regardless of language: read the payload from a file,
optionally write a decision to another file, exit with a status code.

For the runtime model see [design.md](design.md); for the YAML config see
[configuration.md](configuration.md).

---

## The process contract

When Chrys runs your hook it spawns a subprocess with:

| Env var | What it points at |
|---|---|
| `CHRYS_HOOK_ID` | The hook's `id` from `hooks.yaml`. |
| `CHRYS_HOOK_EVENT` | The event name (e.g. `before_tool_call`). |
| `CHRYS_HOOK_PAYLOAD_FILE` | Path to a JSON file containing the event payload. |
| `CHRYS_HOOK_RESULT` | Path to a JSON file you may write your decision to (file exists, empty). |
| `PYTHONUTF8`, `PYTHONIOENCODING` | Set to `1` / `utf-8` for predictable encoding. |

Plus any `run.env` you declared in YAML (`${var}` templates expanded).

Your hook should:

1. Read JSON from `$CHRYS_HOOK_PAYLOAD_FILE`.
2. Do its work.
3. Optionally write a JSON decision to `$CHRYS_HOOK_RESULT`.
4. Exit 0 for success; non-zero for failure.

stdout and stderr are captured but **not used for the decision** — write to
`$CHRYS_HOOK_RESULT` for that. stderr is surfaced to the user on failure
(per `on_error`).

Payload and result files are 0600 on POSIX and Chrys deletes them after the
hook returns. Detached logs and durable outbox files are also owner-only where
the platform supports it. Don't keep references to temp files.

---

## The payload

Every event payload includes a base envelope:

```json
{
  "schema": 1,
  "event": "before_tool_call",
  "timestamp": "2026-05-14T15:00:00Z",
  "session_id": "abc-123",
  "profile": "Code",
  "cwd": "/path/to/workspace"
}
```

Event-specific fields are layered on top.

### `session_start` / `session_end`

Base envelope only.

### `session_restored`

```json
{ ..., "restored_session_id": "…" }
```

Distinct from `session_start` so you can tell a fresh boot apart from a
resume. Both can fire in the same startup (boot + immediate restore).

### `before_turn` / `after_turn`

```json
{ ..., "turn": 1, "user_text": "...", "is_retry": false }                # before_turn
{ ..., "turn": 1, "status": "ok|interrupted|failed", "failed": false }   # after_turn
```

`is_retry` is `true` when the turn is resuming an existing logical turn
(user clicked Retry) rather than starting a fresh one.

### `user_prompt_submit`

```json
{ ..., "text": "the user's prompt", "injected": false }
```

`injected` is `true` when the prompt is a mid-turn injection into the main
conversation. This includes direct user injections and retry/resume
submissions that carry additional user text.

A `/btw <question>` submission carries `injected: false` and the optional
`side_question: true` field; `text` contains the question without `/btw`:

```json
{ ..., "text": "the user's side question", "injected": false, "side_question": true }
```

Main-conversation submissions omit `side_question`; treat a missing value as
`false`. To identify a new main-turn submission, both `injected` and
`side_question` must be false. A side question does not start a main turn or
trigger `before_turn` / `after_turn`. Opening saved questions with `/btw`
alone does not trigger `user_prompt_submit`.

### `before_tool_call` / `after_tool_call` / `tool_error`

```json
{
  ...,
  "tool": {
    "name": "edit_file",
    "kind": "filesystem.write",
    "call_id": "abcdef123456",
    "args": { "path": "src/foo.py", "old_string": "...", "new_string": "..." }
  },
  "result": {                       // after_tool_call / tool_error only
    "text": "OK",
    "duration_ms": 42,
    "error": false,
    "failed": false,
    "approval_rejected": false
  }
}
```

The `args` keys are whatever the tool function defines — e.g. `edit_file`
takes `path` / `old_string` / `new_string` / `replace_all`; `write_file`
takes `path` / `content` / `overwrite`; shell tools take
`command` / `reason` / `timeout` / `working_dir` / `max_tokens`. MCP and
skill tools have their own arg schemas.

`tool.kind` is the canonical chrys kind value in its bare form
(e.g. `shell`, `filesystem.write`) — the same form used in YAML matchers.

`result.error` is reserved for hard/runtime errors, and `tool_error` fires
only for that path. `result.failed` is the structured tool-result failure
status, so normal tool returns such as validation failures or rejected calls
can report `failed: true` while keeping `error: false`.

`result.approval_rejected` remains a compatibility flag for calls blocked
before execution by either user rejection or a hook. Use
`result.rejection_source` (`user` or `hook`) or `result.hook_denied` when
you need to distinguish those cases.

### `approval_requested` / `approval_resolved`

```json
{
  ...,
  "request_id": "…",
  "caller_name": "Code",
  "tool": { "name": "edit_file", "kind": "filesystem.write", "call_id": "…", "args": {} },
  "approved": true                  // approval_resolved only
}
```

Native tool approval middleware emits `approval_requested` only while a
human decision is still needed. In AUTO mode, automatic review finishes
first; automatically accepted or synchronously answered requests emit neither
event. `approval_resolved` fires before continuing with the accepted or
rejected result. An interrupted wait instead ends through turn lifecycle
events. External ACP agents' approval forwarding does not emit these events.

These are observer events: returned decisions cannot grant, reject, or rewrite
an approval. A blocking observer can delay progress until its timeout, so
keep status notifications short and use `on_error: ignore` where appropriate.

### `sub_agent_start` / `sub_agent_end`

```json
{
  ...,
  "sub_agent": {
    "name": "Explore",
    "tool_name": "explore",
    "invocation_id": "…",
    "parent_call_id": "…"
  },
  "status": "ok|failed|cancelled",  // sub_agent_end only
  "result_summary": "…"             // sub_agent_end only, max 500 chars
}
```

### `pre_compact`

```json
{
  ...,
  "trigger": "phase1|phase2|phase3|phase4|force",
  "usage_pct": 0.82,
  "tokens_before": 174000,
  "sub_agent": { ... }              // present only for sub-agent compactions
}
```

### `user_interrupt`

Base envelope only. Dispatch is scheduled in the background after the
interrupt has been delivered, so a hook here cannot prevent or delay the
interrupt. The hook entry still uses its configured `execution.mode` inside
that background dispatch.

---

## The decision file

If your hook wants to influence Chrys (block, modify args, inject reminders,
append context to a tool result), write JSON to `$CHRYS_HOOK_RESULT`:

```json
{
  "action": "block",
  "reason": "edit_file path is on the deny-list"
}
```

Recognized top-level keys:

| Key | Type | Honoured when | Meaning |
|---|---|---|---|
| `action` | `"block"` / `"modify"` / `"allow"` (default) | Blocking hooks on gated events only | Gate the action. |
| `reason` | string | `action: block` | Surfaced to the user (and the model for tool blocks). |
| `args_override` | object | Blocking `before_tool_call` only | Merged over `tool.args` before the tool runs. |
| `system_reminder` | string | Blocking `user_prompt_submit` only | Wrapped in `<system-reminder>` and appended to the next LLM call. |
| `extra_context` | string | Blocking `after_tool_call` / `tool_error` only | Appended to the tool's result text. |

Notes:

- An empty or missing result file means "no opinion" — equivalent to
  `{ "action": "allow" }`.
- Invalid JSON logs a warning and is treated as "no opinion".
- Unknown keys are ignored. Future Chrys versions may add new keys.
- Async and `fire_and_forget` hooks may still write the file, but their
  decisions are **discarded** — by the time the subprocess exits, the gated
  action has already moved on.
- A blocking hook that exits non-zero has its decision file **ignored** —
  the `on_error` policy is the only signal that matters.

---

## Exit codes

| Exit code | Meaning |
|---|---|
| `0` | Success. Decision file (if any) is honoured. |
| non-zero | Failure. `on_error` applies; decision file ignored. |

Timeouts and launch errors are treated as failures regardless of exit code.

---

## Worked examples

### Bash: block writes to `/etc/`

```bash
#!/usr/bin/env bash
set -euo pipefail

payload="$(cat "$CHRYS_HOOK_PAYLOAD_FILE")"
path="$(jq -r '.tool.args.path // empty' <<<"$payload")"

if [[ "$path" == /etc/* ]]; then
  cat >"$CHRYS_HOOK_RESULT" <<EOF
{ "action": "block", "reason": "writes to /etc are not allowed" }
EOF
fi

exit 0
```

YAML:

```yaml
- id: protect-etc
  event: before_tool_call
  match:
    tool_kind: filesystem.write
  run:
    type: script
    path: scripts/protect_etc.sh
  execution:
    mode: blocking
    timeout_seconds: 5
    on_error: block
```

### Python: rewrite shell commands to redact secrets

```python
#!/usr/bin/env python3
"""Replace AWS secret keys with placeholders before shell tools run."""

import json
import os
import re

PATTERN = re.compile(r"AKIA[0-9A-Z]{16}")

with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
    payload = json.load(f)

cmd = payload.get("tool", {}).get("args", {}).get("command", "")
redacted = PATTERN.sub("AKIA****REDACTED****", cmd)

if redacted != cmd:
    with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
        json.dump(
            {"action": "modify", "args_override": {"command": redacted}},
            f,
        )
```

YAML:

```yaml
- id: redact-shell-secrets
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

### Python: inject project context on new main turns

```python
#!/usr/bin/env python3
"""Append a project-specific <system-reminder> to new main-turn prompts."""

import json
import os

REMINDER = (
    "[Project: customer-portal] Follow the conventions in docs/style.md. "
    "Run `pnpm test` before claiming any task is done."
)

with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
    payload = json.load(f)

# Apply only to new main turns; skip injections and side questions.
if payload.get("injected", False) or payload.get("side_question", False):
    raise SystemExit(0)

with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
    json.dump({"system_reminder": REMINDER}, f)
```

YAML:

```yaml
- id: project-reminders
  event: user_prompt_submit
  run:
    type: script
    path: scripts/project_reminders.py
  execution:
    mode: blocking
    timeout_seconds: 2
    on_error: warn
```

### Python: append lint summary to tool result

```python
#!/usr/bin/env python3
"""After edit_file, run ruff and append its findings to the tool result."""

import json
import os
import subprocess

with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
    payload = json.load(f)

path = payload.get("tool", {}).get("args", {}).get("path")
if not path:
    raise SystemExit(0)

proc = subprocess.run(
    ["uv", "run", "ruff", "check", "--quiet", path],
    capture_output=True,
    check=False,
    text=True,
)
if proc.stdout.strip():
    with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
        json.dump(
            {"extra_context": f"[ruff]\n{proc.stdout.strip()}"},
            f,
        )
```

YAML:

```yaml
- id: ruff-after-edit
  event: after_tool_call
  match:
    tool_name: edit_file
    args:
      path:
        regex: "\\.py$"
  run:
    type: script
    path: scripts/ruff_summary.py
  execution:
    mode: blocking          # extra_context only flows from blocking hooks
    timeout_seconds: 60
    on_error: warn
```

### Python: durable telemetry on session end

```python
#!/usr/bin/env python3
"""Post session summary to a telemetry endpoint. Idempotent — retries are safe."""

import json
import os
import urllib.request

with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
    payload = json.load(f)

req = urllib.request.Request(
    "https://telemetry.example.com/chrys-sessions",
    data=json.dumps(
        {
            "session_id": payload["session_id"],
            "profile": payload["profile"],
            "ended_at": payload["timestamp"],
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=10).read()
```

YAML:

```yaml
- id: telemetry-session-end
  event: session_end
  run:
    type: script
    path: scripts/telemetry.py
  execution:
    mode: fire_and_forget
    detach: true
    delivery: durable
    on_error: ignore
```

### Bash: webhook telemetry on every turn

Telemetry from a plain shell script — useful when you don't want a Python
dependency. Idempotent because the server keys on `session_id` + `turn`.

```bash
#!/usr/bin/env bash
set -euo pipefail

payload="$(cat "$CHRYS_HOOK_PAYLOAD_FILE")"

session_id="$(jq -r '.session_id' <<<"$payload")"
turn="$(jq -r '.turn' <<<"$payload")"
status="$(jq -r '.status' <<<"$payload")"

curl --silent --show-error --fail \
  --max-time 10 \
  --header 'Content-Type: application/json' \
  --data "{\"session\":\"$session_id\",\"turn\":$turn,\"status\":\"$status\"}" \
  https://telemetry.example.com/chrys-turns \
  >/dev/null
```

YAML:

```yaml
- id: webhook-turn-telemetry
  event: after_turn
  run:
    type: script
    path: scripts/post_turn.sh
  execution:
    mode: async               # don't block the next prompt on the network
    delivery: durable         # retry on next startup if Chrys crashes mid-flight
    timeout_seconds: 15
    on_error: warn
```

### Bash: desktop notification on long-running turn

A simpler shell-script case — no JSON output, just a side effect. Uses
`mode: fire_and_forget` since there's nothing for Chrys to act on.

```bash
#!/usr/bin/env bash
set -euo pipefail

payload="$(cat "$CHRYS_HOOK_PAYLOAD_FILE")"
status="$(jq -r '.status' <<<"$payload")"

case "$(uname -s)" in
  Darwin)
    osascript -e "display notification \"Turn finished ($status)\" with title \"Chrys\""
    ;;
  Linux)
    notify-send "Chrys" "Turn finished ($status)"
    ;;
esac
```

YAML:

```yaml
- id: notify-turn-done
  event: after_turn
  run:
    type: script
    path: scripts/notify.sh
  execution:
    mode: fire_and_forget
    on_error: ignore
```

### PowerShell: telemetry + toast on session end (Windows)

The interpreter is auto-detected from `.ps1`; Chrys uses `pwsh` if available
and falls back to `powershell`. Run with `-NoProfile` so the user's profile
script doesn't slow startup.

```powershell
#Requires -Version 5.1

$ErrorActionPreference = "Stop"

$payload = Get-Content -Raw -LiteralPath $env:CHRYS_HOOK_PAYLOAD_FILE |
           ConvertFrom-Json

# 1. POST a session summary to the telemetry endpoint.
$body = @{
    session_id = $payload.session_id
    profile    = $payload.profile
    ended_at   = $payload.timestamp
} | ConvertTo-Json -Compress

try {
    Invoke-RestMethod `
        -Method Post `
        -Uri "https://telemetry.example.com/chrys-sessions" `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 10 | Out-Null
} catch {
    # on_error: ignore on the YAML side means this still counts as a failure
    # for outbox bookkeeping; rethrow so the exit code reflects reality.
    throw
}

# 2. Pop a Windows toast so the user knows the session closed cleanly.
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02
)
$xml = $template.GetXml()
$texts = $template.GetElementsByTagName("text")
$texts[0].AppendChild($template.CreateTextNode("Chrys")) | Out-Null
$texts[1].AppendChild($template.CreateTextNode("Session ended ($($payload.profile))")) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Chrys").Show($toast)
```

YAML:

```yaml
- id: telemetry-and-toast-session-end
  event: session_end
  run:
    type: script
    path: scripts/session_end.ps1
  execution:
    mode: fire_and_forget
    detach: true              # survive engine shutdown
    delivery: durable         # at-least-once delivery to the telemetry endpoint
    on_error: ignore
```

> **Cross-platform tip.** If you want a hook that works on both Unix and
> Windows from the same `hooks.yaml`, you have two options:
> 1. Two separate hook entries gated by what you can detect at hook time
>    (e.g. presence of `notify-send` vs PowerShell) — but the entries fire
>    on every platform, so the off-platform copy will be a no-op.
> 2. A single `type: command` entry that invokes a wrapper which dispatches
>    by `uname` / `$env:OS`.
>
> Option 2 is usually cleaner for telemetry; option 1 for desktop UI.

---

## Testing your hook

A quick way to dry-run a hook without booting the engine:

```bash
# Create a fake payload
cat >/tmp/payload.json <<'EOF'
{
  "schema": 1,
  "event": "before_tool_call",
  "timestamp": "2026-05-14T15:00:00Z",
  "session_id": "test",
  "profile": "Code",
  "cwd": "/tmp",
  "tool": {
    "name": "edit_file",
    "kind": "filesystem.write",
    "call_id": "xxx",
    "args": {"path": "/etc/hosts", "old_string": "", "new_string": ""}
  }
}
EOF

# Run the script directly
CHRYS_HOOK_ID=protect-etc \
CHRYS_HOOK_EVENT=before_tool_call \
CHRYS_HOOK_PAYLOAD_FILE=/tmp/payload.json \
CHRYS_HOOK_RESULT=/tmp/result.json \
  ~/.chrys/hooks/scripts/protect_etc.sh

# Inspect the decision
cat /tmp/result.json
```

If the hook is supposed to be idempotent (`delivery: durable`), test that
running it twice over the same payload produces the same observable
behaviour the second time.

---

## Gotchas

- **`PATH` is your responsibility.** Chrys inherits most of the parent process
  env, while stripping inherited `PYTHONHOME` / `PYTHONPATH` so Python-based
  hook commands are not forced onto Chrys's parent interpreter paths. If your
  hook calls `git` or `curl`, make sure the launching environment finds them
  — or use absolute paths in `run.argv` / `run.shell`.
- **Don't print to stdout expecting Chrys to read it.** Decisions go through
  `$CHRYS_HOOK_RESULT`. Anything you write to stdout is captured into the
  `HookResult` for logging but otherwise ignored.
- **Sequencing within an event.** Blocking hooks for the same event run in
  YAML order. If a hook depends on `args_override` from another, put it
  later in the file.
- **Decisions from non-blocking hooks are discarded.** If you want to block
  or modify, use `mode: blocking`. If your hook is slow, accept that the
  turn waits for it; an async hook can't gate anything.
- **Detached hooks have no Chrys timeout.** Once spawned they own their own
  lifetime. Use OS-level mechanisms (`timeout(1)`, signal handlers) if you
  need a watchdog.
- **Working directory.** Unless you set `run.cwd`, hooks inherit the agent's
  workspace cwd. If your script needs the chrys home, use the
  `${chrys_home}` template or read `<config_dir>` from a known location.

---

## Project-level scripts

A `path:` in a project-level `hooks.yaml` resolves relative to the
project file's own directory, not the global one. That is,
`path: scripts/foo.py` in `<workspace>/.chrys/hooks/hooks.yaml`
resolves to `<workspace>/.chrys/hooks/scripts/foo.py`. The same
`path:` in the global file resolves to
`<config_dir>/hooks/scripts/foo.py`. The interpreter is still
inferred from the extension (`.py` → `python3`, `.sh` → `sh`, etc.);
see [configuration.md](configuration.md#type-script).

---

## Trust model

Hooks are arbitrary subprocesses and run without an in-process
approval prompt. This applies equally to the global file and any
project file. A project file loads only while the user's own
`project.hooks_enabled` setting is on (default off; a project file
cannot set it). Once it is on, treat the project file as executable
code committed to the repo, the same way you would treat a
`Makefile` or a `.github/workflows/*.yml`. See
[project-hooks.md](project-hooks.md) for the layered merge rules.
