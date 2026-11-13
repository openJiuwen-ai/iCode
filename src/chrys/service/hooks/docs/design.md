# Chrys Hooks — Design

Hooks let users run external commands (scripts, argv, or shell strings) at
well-defined points in the agent lifecycle: session boundaries, turn
boundaries, prompt submission, tool calls, sub-agent invocations, and context
compaction.

This document describes **how** the hook system works. For the YAML format see
[configuration.md](configuration.md); for the hook-script contract see
[authoring.md](authoring.md).

---

## Goals

- Let users observe or change agent behaviour without forking Chrys.
- Be reliable enough that "telemetry" hooks don't have to be reinvented for
  every project.
- Bound gate and shutdown waits so misbehaving hooks cannot stall critical
  shutdown paths indefinitely.
- Keep the no-hooks path zero-cost.

Non-goals:

- Replacing tool middleware. Hooks are a coarse-grained extension point;
  per-tool logic that has to share state with Chrys belongs in middleware.
- A daemon. Hooks run as ordinary subprocesses; durability is filesystem-based.

---

## Where hooks fit

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  AgentEngine                                                                 │
│                                                                              │
│   start() ─── HookManager ─── HookRunner ─── subprocess(es)                  │
│                    │                                                         │
│                    └── Outbox (filesystem: pending/done/failed)              │
│                                                                              │
│   orchestration/engine/run/coordinator.py ─► user_prompt_submit / interrupt  │
│   orchestration/engine/run/runner.py + finalizer.py ─► before_turn/after_turn│
│   orchestration/engine/run/turn_hooks.py ─► shared hook dispatch helpers     │
│   service/session/lifecycle.py ─► fires session_restored                     │
│   orchestration/engine/build/construction.py ─► fires session_start          │
│   orchestration/engine/engine.py shutdown() ─► session_end then drain        │
│                                                                              │
│   service/agent_middleware/events/tool_events.py ─► before_tool_call,        │
│   service/agent_middleware/events/sub_agent_events.py ─► after/error         │
│   orchestration/sub_agents/tools.py ──► sub_agent_start, sub_agent_end       │
│                                                                              │
│   service/context/compaction/ + orchestration/sub_agents/tools.py ─► pre     │
└──────────────────────────────────────────────────────────────────────────────┘
```

The `HookManager` is owned by `AgentEngine` and lazily built on first
`start()` only when a hooks config file exists. With no config file, Chrys does
not create hook runtime directories or an outbox. The manager survives profile
switches within a session (hooks are global, not per-profile). On
`engine.shutdown()` it fires `session_end`, drains in-flight async work, and is
then dropped so the next `start()` reloads config.

---

## Events

Names in YAML are the lowercase string values.

| Event | When it fires | Can block? | Can modify? |
|---|---|---|---|
| `session_start` | After engine fully ready, first boot / new session / reset | No | No |
| `session_restored` | After a saved session is restored | No | No |
| `session_end` | Engine shutdown, before teardown | No | No |
| `before_turn` | At the start of a new user turn or retry | No | No |
| `after_turn` | After turn completes (or fails / is interrupted) | No | No |
| `user_prompt_submit` | When the user submits a prompt (new turn or mid-turn injection) | Yes | reminders |
| `before_tool_call` | Before a tool runs (main or sub-agent) | Yes | tool args |
| `approval_requested` | A native tool approval still needs a human after automatic review, if any | No | No |
| `approval_resolved` | That human approval wait ends with acceptance or rejection | No | No |
| `after_tool_call` | After a tool returns | No (blocking hooks can append to or replace the result) | extra_context, result_override |
| `tool_error` | When a tool raises | No | extra_context, result_override |
| `sub_agent_start` | When a sub-agent invocation begins | No | No |
| `sub_agent_end` | When a sub-agent invocation finishes | No | No |
| `pre_compact` | Before each compaction phase fires | No | No |
| `user_interrupt` | After the user presses interrupt | No (dispatch is scheduled in the background) | No |

"Block" and "modify" decisions only apply where the event supports them and
the hook uses `mode: blocking` — see [Modes](#modes). Non-blocking hooks may
still return decisions, but the manager ignores them because the gated action
has already moved on by the time the subprocess finishes.

Every payload carries a base envelope:

```json
{
  "schema": 1,
  "event": "before_tool_call",
  "timestamp": "2026-05-14T15:00:00Z",
  "session_id": "…",
  "profile": "Code",
  "cwd": "/path/to/workspace"
}
```

Event-specific fields are documented per event in
[authoring.md](authoring.md).

---

## Modes

Each hook declares an `execution.mode` that controls how Chrys waits for it.

### `blocking`

The dispatch site `await`s the hook before continuing. Used to **gate**
actions:

- `before_tool_call` blocking hook → can deny the tool or rewrite its
  arguments before it runs.
- `user_prompt_submit` blocking hook → can refuse the prompt or queue
  `<system-reminder>` text for the next LLM call.
- `before_tool_call` blocking hook → can attach `extra_context` that reaches the model
  with the call's result once it runs, without changing whether it runs.
- `after_tool_call` / `tool_error` blocking hook → can append `extra_context`
  text to the tool result before it streams back to the model, or replace its
  text outright with `result_override`.

Blocking hooks within the same event run **sequentially** in YAML order. The
first `action: block` short-circuits the rest. `action: modify` updates the
envelope so later blocking hooks see the rewritten args.

### `async`

Spawned in parallel; the manager keeps the task in its `_inflight` list and
**awaits all turn-scoped async hooks at turn end** (`drain_turn`). Session-
scoped async hooks (e.g. `session_start`) are drained at shutdown.

Used for telemetry, linters, notifications — things the user wants completed
before the next prompt starts but that don't need to gate the current action.

Decisions from async hooks are discarded.

### `fire_and_forget`

Spawned in parallel and **never drained**. Useful for "best-effort,
side-effect" hooks like opening a browser tab or appending to a log file.
Tasks are still tracked internally so cancellation and outbox bookkeeping
work, but `drain_turn` / `drain_session` do not await or cancel them. Replacing
a manager while the session log stays open preserves their actual outcomes,
exit codes and durations. The recorder owns outstanding trajectory spans and
ends them with `abandoned` only when the log closes. The tasks continue,
including durable outbox updates; completion after log closure does not emit
a second terminal event. The same rule applies
to async interrupt observers dispatched outside the drained scopes. Dispatches
still awaiting confirmation of a committed start record are settled too.

Combine with `detach: true` to survive `engine.shutdown()` entirely (POSIX
`setsid`; Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`). Detached
hooks redirect stdio to a log file under `<config_dir>/hooks/logs/`. The
loader rejects `detach: true` paired with `blocking` or `async` (Chrys would
have no stdio to read from).

---

## Delivery

Orthogonal to mode:

- `best_effort` (default) — no durable state. Fast path. If Chrys crashes
  mid-spawn the hook is just lost.
- `durable` — before spawning, the manager writes
  `<config_dir>/hooks/outbox/pending/<job-id>.json` describing the
  invocation. On exit-code 0 the file moves to `done/`; on non-zero or
  timeout it moves to `failed/`. On startup, `recover_outbox()` re-runs any
  pending entries older than `outbox_retry_age_seconds`.

Semantics are **at-least-once**. If Chrys crashes after the subprocess exits
0 but before the move to `done/`, the next startup re-runs the hook. Durable
hooks must be idempotent.

Durable applies only to non-blocking modes — blocking hooks are awaited
inline and cannot be lost.

---

## Drains

```
turn boundary    ──► drain_turn()      awaits turn-scoped async hooks
                                       skips fire_and_forget, session, detached

engine.shutdown  ──► drain_session()   awaits turn + session async hooks
                                       up to shutdown_grace_seconds,
                                       cancels the rest
                                       skips fire_and_forget, detached
```

`drain_turn` has no timeout. A long-running async hook can delay the next
prompt indefinitely — author your async hooks with that in mind.

`drain_session` is bounded by `settings.shutdown_grace_seconds` (default
5.0). When the grace expires the manager cancels remaining tasks;
`managed_subprocess` kills their subprocesses and releases transports.

Detached hooks are never drained — they're already independent of Chrys.

---

## Matching

A hook's `match` clause is an AND across set sub-clauses. Empty match block →
fires on every event of that type. Tool-related sub-clauses (`tool_kind`,
`tool_name`, `args`) require a `tool` payload, so they match only tool events.

```yaml
match:
  profile: Code                      # exact profile match
  profiles: [Code, QA]               # any-of profile match
  tool_kind: filesystem.write        # canonical kind (bare form)
  tool_name: edit_file               # specific tool
  args:                              # per-arg matchers (AND across keys)
    path:
      contains: "/src/"
      regex: "\\.py$"
```

Argument matchers (`equals` / `contains` / `regex`) are AND when more than
one is set on the same arg. Non-string arg values are stringified before
comparison. A missing arg fails the match.

> Tool kinds are bare strings (`shell`, `filesystem.write`, etc.) — YAML and
> runtime share the same form. The loader strips the legacy `chrys.` prefix
> from known kinds with a warning. `match.args` keys are ordinary tool
> argument names and are not kind-normalized.

An enabled hook whose regex does not compile is left out at load time and
reported with a `hook_skipped` warning naming it and its file; the other hooks
in the file still load. The matcher still treats an uncompilable pattern as a
miss (logged once), so dispatch never raises on one.

---

## Decision aggregation

When multiple blocking hooks fire for the same event, their decisions merge
into a single `HookDecision`:

- `blocked` — first hook to return `action: block` wins; remaining hooks are
  cancelled.
- `args_override` — merged left-to-right; later hook wins on conflict.
- `system_reminders` — appended in YAML order.
- `extra_context` — appended in YAML order.

Non-blocking hooks don't contribute to the aggregated decision.

---

## Failure handling

Each hook declares `execution.on_error`:

- `block` — non-zero exit (or timeout) on a blocking hook denies a gated
  event (`before_tool_call`, `user_prompt_submit`) and surfaces the hook's
  stderr. On non-blocking hooks and non-gated events this falls back to `warn`.
- `warn` (default) — log a warning, allow the action.
- `ignore` — log at debug only.

A failed blocking hook's `CHRYS_HOOK_RESULT` file is **not** consulted — the
file may be partial or stale. The `on_error` policy is the only signal.

---

## Cancellation

If a turn is interrupted while an async hook is running, the manager **does
not** cancel the hook — turns finish their own drain. If `engine.shutdown()`
runs while async hooks are in flight, the grace timer applies and the
remaining tasks are cancelled.

Cancelled durable hooks **stay in `pending/`** for the next startup to
retry. They do not move to `failed/` on cancellation.

Background (`fire_and_forget`) and detached hooks are not cancelled by
turn drains.

---

## Performance

- `HookManager.has_hooks_for(event)` is the fast-path probe. Wire-up sites
  call it before allocating payload dicts so the no-hooks-at-all case pays
  almost nothing.
- A single `asyncio.Semaphore` caps concurrent non-detached subprocesses to
  `settings.max_parallel_hooks` (default 4). Detached hooks are not
  capped — they're handed to the OS and forgotten.
- Payload deepcopies happen once per dispatch via `_build_envelope`, so a
  hook author can mutate the payload dict it receives without affecting
  Chrys state.

---

## Files on disk

```
<config_dir>/hooks/
├── hooks.yaml                # or hooks.yml / hooks.json
├── scripts/                  # user's hook scripts (any layout)
├── logs/<session>/<hook>.<ts>.log    # detached hook stdio
├── tmp/                      # per-invocation payload/result temp files
└── outbox/
    ├── pending/<job>.json
    ├── done/<job>.json
    └── failed/<job>.json
```

Temp, detached invocation, detached log, and outbox files are written with
0600 permissions on POSIX.

---

## Boundaries and limitations

- `extra_context` is collected only from **blocking** hooks. An async
  `after_tool_call` hook that returns `extra_context` is silently ignored —
  by the time its subprocess exits, the tool result has already streamed
  back to the model.
- `args_override` is collected only from **blocking** `before_tool_call`
  hooks for the same reason.
- Cross-process coordination: if you run multiple Chrys instances against
  the same `<config_dir>`, they share the outbox. The
  `outbox_retry_age_seconds` heuristic prevents double-pickup of fresh
  pending jobs, but durable hooks should still be idempotent.
- Hot reload is not implemented. Edits to `hooks.yaml` mid-session take
  effect on the next hook-manager build: new session, restore/reset, or a
  workspace change that changes `primary_cwd`. Profile/model switches that
  keep the same `primary_cwd` reuse the current manager.

---

## Multiple sources

Hooks can be loaded from two sources:

1. The global file at `<config_dir>/hooks/hooks.yaml`.
2. The project file at `<workspace.primary_cwd>/.chrys/hooks/hooks.yaml`.

The project file is optional. When both are present, they are merged
at loader time into a single `MergedHooksFile` with project hooks
listed first. The `HookManager` iterates this combined list as a
single chain; the existing "first block short-circuits" rule applies
to the whole chain, so a project block ends the chain (including the
global layer for the same event). This is the layered semantic: both
layers apply, project just runs first.

Runtime artifacts (`logs/`, `tmp/`, `outbox/`) stay in the global
`<config_dir>/hooks/` tree. Project scripts can write their own
artifacts to `<project>/.chrys/hooks/` if they want; chrys does not
create or manage that path.

See [project-hooks.md](project-hooks.md) for the full design and
the merge / short-circuit / settings / id-collision rules.
