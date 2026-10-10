# Chrys ACP Frontend API

Reference for the ACP surface that non-TUI clients (e.g. the VS Code extension)
use to reach parity with the Textual TUI. Source of truth is the code — keep this
in sync with:

- `src/chrys/app/acp/server.py` — request routing, standard methods, notifications
- `src/chrys/app/acp/session_manager.py` — session/profile/config backend behavior

Related issue: #355.

## Design model

Chrys has one backend runtime (`AgentEngine` + `ChrysSessionHost`) driven by an
internal `EventBus`. Two frontends consume it:

- **TUI** — subscribes to `EventBus` events directly and calls engine/session
  helpers in-process.
- **ACP clients** — speak standard ACP plus a set of Chrys-private extensions.

Goal: a *symmetric frontend contract* — anything the TUI can do, an ACP client can
do through ACP, without reaching into Chrys internals.

Direction principles:

1. ACP clients never call Chrys internals. Missing capability becomes an ACP
   method/event, not a private import.
2. Prefer standard ACP. Only fall back to an extension when ACP has no concept for
   the capability (see *Standard vs. extension* below).
3. Events describe runtime *facts*; each frontend renders them its own way — no
   frontend-specific payload shapes.
4. Some UI stays frontend-native even with a shared backend: editor diff rendering,
   terminal integration, file pickers, notifications, settings UI.
5. Extensions use the `chrys/*`, `session/*`, `sub_agent/*`, `profiles/*`,
   `settings/*`, or `mcp/*` namespaces and are documented here.

## Transport

Standard ACP methods are dispatched by name. Everything Chrys-specific rides on two
ACP escape hatches:

- **`ext_method(method, params)`** — request/response. Client → backend for
  actions/queries; backend → client for `chrys/request_input` (the `ask_user`
  callback). Dispatched by the `method` string in `ChrysAcpServer.ext_method`.
- **`ext_notification(method, params)`** — one-way backend → client push, bridged
  from `EventBus` events in `_handle_chrys_extension_event`.

Param keys are camelCase; most request handlers also accept the legacy
`session_id` spelling. Unknown methods raise JSON-RPC `method_not_found`.

## Standard ACP

Implemented standard methods:

- Lifecycle: `initialize`, `session/new`, `session/load`, `session/list`,
  `session/prompt`, `session/cancel`, `session/close`, and the top-level
  `delete_session`.
- Server → client: `session/request_permission` (approvals), `session/update`
  (agent message/thought chunks, tool calls, `usage_update`, `session_info_update`,
  `current_mode_update`, `plan`). The `plan` update mirrors the builtin todo
  list: streamed live from `TodoListUpdated` events during a turn, and re-seeded
  unconditionally (empty entries = clear) on `session/new`, `session/load`, and
  after `session/rollback` — both to a turn and to welcome.

`session/prompt` takes text, resource links and embedded text resources (sent as
text), and, when the model profile has vision, images. An image needs an `image/*`
`mimeType`, but its bytes name its type; anything but PNG, JPEG, GIF or WebP fails
the whole prompt with `invalid_params`. Audio and embedded blob resources fail the same way.

`session/new` and `session/load` accept `additionalDirectories` (advertised via
`SessionCapabilities.additionalDirectories` in `initialize` so clients surface the
option); the manager builds a multi-dir `Workspace` and passes it to
`ChrysSessionHost(workspace=...)`. All `cwd` / `additionalDirectories` paths must be
**absolute** (ACP contract) — a relative path is rejected rather than resolved against
the ACP server's process cwd, which would bind the session to the wrong workspace.
On `session/load` of an inactive session, supplied `additionalDirectories` are
**authoritative** and replace the saved additional roots, with the primary cwd kept
first so an unchanged scope still reuses an OpenAI Responses service session. A
non-empty list narrows/swaps scope; an explicit empty list `[]` clears the saved roots
entirely; omitting the field (`null`/absent) keeps the saved roots. A moved cwd also
drops them. Reloading an already-active session with `additionalDirectories` at all
(any list, including `[]`, or MCP overlays) is rejected — roots can't be rebuilt on a
running host, so the session must be closed first rather than silently keeping the old
scope. `session/list` treats a non-empty `additionalDirectories` list as an exact,
ordered additional-root filter (roots validated absolute like new/load): only
sessions whose saved extra roots match exactly are returned. When omitted or empty,
all sessions for the `cwd` are returned (each session reports its own roots via
`additionalDirectories` in the response).

### Session modes — approval mode (standard)

Approval mode uses ACP's native session-mode mechanism, not an extension:

- `session/new` / `session/load` advertise `modes` (`SessionModeState`):
  available `manual` / `auto` / `bypass` + the current mode.
- `set_session_mode(mode_id, session_id)` switches it.
- Changes are pushed as the standard `current_mode_update` (`session/update`).

The switch is **per-session**: it updates the live approval middleware and does
not write the global settings-document default. Persisting a default approval
mode for future sessions is handled by `session/set_config_option`.

### Session models — model switch (standard, per-session)

Model selection uses ACP's native model mechanism:

- `session/new` / `session/load` advertise `models` (`SessionModelState`):
  available model profiles + the current model id.
- `set_session_model(model_id, session_id)` switches it.

The switch is **per-session**: it swaps `settings.model_profile` in-memory and
soft-restarts the agent (`SetModelProfile` → `on_set_model_profile`) — **no global
settings write**. Credentials come from the `ModelProfile` (read by `create_client`),
not `os.environ`, so two sessions in one process can run different providers
without colliding. `on_settings_reload` preserves the per-session override.
Persisting a *default* model for future sessions is a separate concern handled by
the config surface (`session/set_config_option`).

## Extension RPCs (client → backend)

All routed through `ext_method`.

### Session control

| Method | Params | Effect |
|---|---|---|
| `session/delete` | `sessionId`, `cwd?` | Delete a saved session scoped to cwd. |
| `session/inject` | `sessionId`, `text` | Inject a prompt into the active turn. Rejected when no turn is running (use `session/prompt` to start one). |
| `session/rollback` | `sessionId`, `targetTurn`, `revertChanges?`, `selectedPaths?` | Roll back; replays history + emits `chrys/rollback_result`. Response/notification carry `rolledBackUserText` (first discarded user prompt, for restoring the composer), `exclusions` (`[{path, reason}]`, reason = `RollbackExclusionReason` value like `"unrestorable"` / `"move_poisoned"`) for files the plan dropped, and advisory `warnings` (strings). |
| `session/switch_agent` | `sessionId`, `agentProfile` | Switch active agent profile in-session. Unknown profile → immediate error; switching to the active profile is a no-op (no 60s wait) whose response still carries the live runtime (model/tools/skills), so it won't blank client state. |
| `session/set_workspace` | `sessionId`, `primaryCwd` | Change primary workspace cwd. `primaryCwd` is validated strictly (must exist + be a directory, resolved absolutely) like `new`/`load`. Soft-restarts the agent, then pushes `chrys/runtime_update` (skills/MCP/memory/context may change). |
| `session/skip_sleep` | `sessionId`, `callId` | Finish an active `sleep` tool call early. |
| `session/set_config_option` | `sessionId`, `key`, `value` | Persist a supported config key to the user settings document + awaited reload, then push `chrys/runtime_update` (global default; see below). `key` accepts the logical name or its legacy `CHRYS_*` spelling; the response carries all three names (`key` echoed, `envKey`, `settingKey`). The session must be active — it is validated *before* the document write, so a failed request never leaves persisted config mutated. |

Approval mode and model switching are **not** here — they use standard ACP
`set_session_mode` / `set_session_model` (see *Standard ACP*).

### Read-only queries

| Method | Params | Returns |
|---|---|---|
| `chrys/session_runtime` | `sessionId` | Runtime snapshot: profile, model, tokens/usage, tools, MCP/skills details. |
| `session/mutations` | `sessionId` | Per-turn mutation summary + rollback turns + file summary. Mutations carry `beforeSkip`/`afterSkip` (`"too_large"` / `"binary"` / `null`) when a side's content backup was withheld by snapshot policy; such files are excluded from the `files` summary. Each mutation also carries `provenance` (`"proven"` / `"assumed"` / `"foreign"`) + `contested` (bool); `turns[].mutations` and `mutationCount` are the **raw event log** (foreign rows included — clients filter/badge themselves), while the net-level `files` list excludes foreign changes and carries folded `contested`/`inferred` badges. |
| `session/diff` | `sessionId`, `path?`, `turn?` | Before/after text diff entries (binary-aware). Entries whose content backup was withheld by snapshot policy (oversized/binary files) are omitted. Each entry carries the same folded `contested`/`inferred` badges as the `session/mutations` `files` list, so clients can badge diffs without joining the two endpoints. |
| `session/history` | `sessionId`, `cwd?` | Raw persisted messages, without the messages compaction excluded (tool calls it summarized or removed, a dropped current turn's in-between text, folded turns). Scoped to the requesting workspace cwd (like `load`/`delete`); a session from another workspace is rejected. An assistant message whose `additional_properties._group` has `_summary_of_group_ids` is compaction's summary of excluded tool calls, kept for the model: the `session/load` and `session/rollback` replays show neither the summary nor the messages it replaced, and a client rendering this list should skip it too. |
| `settings/options` | `sessionId?` | Supported config keys under all three names (`key`, `envKey`, `settingKey`). Each entry carries the durable document `value` plus `baseValue`/`baseSource` — the manager's base settings, deliberately not called any session's view. With `sessionId`, entries add `sessionValue`/`sessionSource` read from that session's own loaded settings. |
| `mcp/list` | `sessionId` | MCP tools + failures from runtime details. |
| `skills/list` | `sessionId` | Skill sources + details from runtime details. |

### Sub-agent control

| Method | Params | Effect |
|---|---|---|
| `sub_agent/retry` | `sessionId`, `invocationId` | Retry a paused sub-agent invocation. |
| `sub_agent/abort` | `sessionId`, `invocationId` | Abort a paused sub-agent invocation. |

### Profiles / config / MCP

| Method | Params | Notes |
|---|---|---|
| `profiles/agents/list` | — | Agent profile summaries, including a `builtin` flag. |
| `profiles/agents/read` | `name` | Full agent profile. **MCP `headers`/`env` values masked as `"***"`.** |
| `profiles/agents/write` | `profile` | Persist. Masked `"***"` MCP secrets restored from the stored profile. |
| `profiles/agents/delete` | `name` | Delete a user agent profile. Built-in profiles are rejected; clients should migrate the previous "delete shadow to restore built-in" behavior to `profiles/agents/reset`. |
| `profiles/agents/reset` | `name` | Reset a built-in profile while retaining Skills, MCP, and Memory settings. Returns `changed: false` when no shadow or resettable changes exist. |
| `profiles/models/list` | — | Model profile summaries (no secrets). |
| `profiles/models/read` | `id` | Full model profile. **`api_key` masked as `"***"`.** |
| `profiles/models/write` | `profile` | Persist. `api_key` of `""`/`"***"` preserves the stored key. Omitting `stream` or sending `null` streams for a new profile and keeps the stored value on an update; `false` (or another false spelling such as `0` or `""`) turns streaming off. |
| `profiles/models/delete` | `id` | Delete a model profile. |
| `settings/reload` | `sessionId` | Reload registries + soft-restart. Awaits the rebuild: a failed reload returns an error, success pushes `chrys/runtime_update`. |
| `mcp/test` | `server` | One-shot HTTP MCP connection test. Client-supplied stdio is rejected. |

### Backend → client callback

`chrys/request_input` carries the whole `ask_user` batch in one request:
`sessionId`, `requestId`, `callerName`, and `questions`:

```json
{
  "sessionId": "…",
  "requestId": "…",
  "callerName": "Code",
  "questions": [
    {
      "question": "Which library?",
      "header": "Library",
      "multiSelect": false,
      "options": [{"label": "tenacity", "description": "Use the existing dependency."}]
    },
    {
      "question": "Anything else the retry layer must respect?",
      "header": "Constraints",
      "multiSelect": false,
      "options": []
    }
  ]
}
```

A receiver rejects unknown fields, wrong nested types, over-cap payloads, and
malformed metadata. There is no capability negotiation and no single-question
projection: every peer speaks this shape.

The client returns a fixed-length positional `answers` array:

```json
{
  "answers": [
    {"values": ["tenacity"], "note": "only for the retry layer"},
    {"values": ["Keep the retry layer transport-agnostic."], "note": ""}
  ],
  "cancelled": false
}
```

Free text is a value, not a note: a question without options (or one the user
answered by typing instead of choosing) carries the typed text as the single
entry of `values`. `note` is only accepted alongside selected option labels, so
a note-only answer invalidates the whole response. An empty `values` means the
question was left unanswered; the model sees it as `unanswered: true`.

Structured responses are accepted atomically. The answer count must match the
question count; values must be distinct and non-blank; a non-multi-select
question accepts at most one value; multiple values must all be offered option
labels; and a note is valid only with canonical selected options. Every string
in the request and the response must be valid Unicode (no unpaired surrogate
escapes), matching what the JSON transport accepts. A malformed `answers`
array, `cancelled: true`, a response without `answers`, or a failed callback
all mean no response; the model receives the retryable `ask_user_no_response`
tool error. There is no fabricated answer-text sentinel.

`ask_user` waits for the client reply with **no backend timeout** by default under
ACP (`chrys acp` sets `ask_user_timeout_seconds=None`), so the client owns the
interaction lifetime — keep the prompt open until the user answers, or send
`session/cancel` to abandon the turn. Pass `chrys acp --ask-user-timeout SECONDS`
to opt into a backend bound instead; on expiry the model receives a "user did not
respond" result and the question is considered closed. The timeout is a
per-session `Settings.ask_user_timeout_seconds` value (TUI/CLI default to a bounded
15 minutes via `CHRYS_ASK_USER_TIMEOUT_SECONDS`). Under ACP this value is *pinned*:
`settings/reload` keeps the client-owned timeout rather than reverting to the env
default (TUI/CLI sessions are unpinned, so they pick up an edited
`CHRYS_ASK_USER_TIMEOUT_SECONDS` on reload).

Standard `session/request_permission` callbacks use `approval.timeout_seconds`
(`CHRYS_APPROVAL_TIMEOUT_SECONDS`), shared with TUI human approvals. The default
`0` means no timeout; positive values set a deadline in seconds, after which the
server rejects the tool call. Restart the server after changing this setting.
Transport shutdown cancels and drains outstanding SDK requests before closing
sessions, so unanswered callbacks do not block exit. Unsent output is discarded
on shutdown; cleanup does not wait for the client to read stdout and continues
after a broken pipe. On Windows the pinned SDK writes stdout synchronously, so a
client that keeps stdout open without reading it can still block the server.
`session/cancel`, `session/close`, and
`session/delete` interrupt the engine first and then release pending input and
permission callbacks; close and delete additionally stop prompt admission
before releasing waits, so a prompt already queued on the session is rejected
rather than started as a fresh turn the teardown would wait behind. Late
permission replies are ignored, and unrelated sessions remain independently
promptable while one client callback is pending.

## Extension notifications (backend → client)

One-way `ext_notification` pushes, bridged from `EventBus`:

- Runtime / session: `chrys/runtime_update`, `chrys/session_restored`,
  `chrys/usage_update`, `chrys/profile_switched`, `chrys/workspace_updated`,
  `chrys/approval_reviewed`.
- Agent load lifecycle: `chrys/agent_load_started`, `chrys/agent_load_progress`,
  `chrys/agent_load_finished`, `chrys/agent_load_failed`.
- Diagnostics: `chrys/error`, `chrys/warning`, `chrys/context_compressed`,
  `chrys/context_pressure`, `chrys/tool_compacted`,
  `chrys/compaction_started`, `chrys/compaction_finished`.
- Edits / turns: `chrys/rollback_result`, `chrys/user_inject_result`.
- Sub-agent lifecycle: `chrys/sub_agent_invocation_start`,
  `chrys/sub_agent_tool_call_start`, `chrys/sub_agent_tool_call_result`,
  `chrys/sub_agent_progress`, `chrys/sub_agent_retry_attempt`,
  `chrys/sub_agent_compaction_started`, `chrys/sub_agent_compaction_finished`,
  `chrys/sub_agent_compaction_committed`,
  `chrys/sub_agent_paused`, `chrys/sub_agent_resumed`,
  `chrys/sub_agent_aborted`, `chrys/sub_agent_cascade_aborted`.

Notes:
- `chrys/runtime_update` always uses the envelope `{ sessionId, runtime: {...} }`
  for every sender (`session/new`/`load`, model switch, settings reload,
  `set_config_option`, `set_workspace`, rollback, `SessionReady`,
  `AgentRuntimeUpdated` — including mid-turn runtime-skill refreshes). The
  `runtime` object may be partial. The
  `chrys/session_runtime` *request* returns that same runtime object flat (a
  separate request/response contract).
- `chrys/usage_update` is dual-path for parent-agent session-window updates —
  emitted as a notification *and* projected through the standard
  `session/update` bridge. Sub-agent usage updates are extension-only so
  standard ACP clients do not replace the parent session-window gauge with a
  sub-agent window.
  Its extension payload includes `agentProfile` for display and `usageSourceId`
  for identity, so clients can distinguish parent-agent usage from concurrent
  same-profile sub-agent usage.
- Sub-agent events are dual-path too: each `chrys/sub_agent_*` notification is
  emitted *and* the bridge projects standard `session/update` tool-call progress
  onto the parent sub-agent tool call, so standard-only clients still see progress.
- Free-form values are sent as plain JSON: tool arguments (`rawInput` on tool
  calls and permission requests, `args` on `chrys/sub_agent_tool_call_start`),
  `metadata` on `chrys/sub_agent_tool_call_result`, and the profile returned by
  `profiles/agents/read`. Structured values become objects: iCode's own
  records are keyed by their snake_case field names (e.g.
  `metadata.file_mutation_hashes` is
  `{ before, after, before_skip, after_skip, contested, inferred }`), and a
  structured tool argument keeps the keys of the tool's argument schema. Enum
  members become their value, dates ISO 8601 strings, sets and tuples arrays,
  and non-finite numbers `null`. Their text never carries an unpaired
  surrogate: an undecodable byte in a path (e.g. in
  `metadata.tool_error_details`) reads as its `\udcXX` escape.
- `metadata` on `chrys/sub_agent_tool_call_result` never carries file contents,
  so a large edit or a command that changes many files stays within a client's
  message limits. The edited file's before/after text (`file_snapshot`) is
  left out, and `shell_file_snapshots` lists the files a command changed as
  `{ path, operation, bytes_changed, source, before_hash, after_hash,
  before_skip, after_skip, provenance, contested }`. `path` is display text
  (an undecodable byte reads as its `\udcXX` escape), so two entries can
  share it. The list stops at 100 files or 256 KiB of JSON, whichever comes
  first; `shell_file_snapshots_omitted` counts the rest. Read the text through
  `session/diff`.
- `chrys/compaction_finished` and `chrys/sub_agent_compaction_finished`
  payloads include `formatViolation`. It is empty unless a malformed
  LAST_WORDS note was accepted, in which case it carries the bounded,
  single-line violation left when the corrective retry process stops. The
  parent event also carries a successful generated note in `lastWords`;
  sub-agent notes remain private to their invocation.
- Both `*_compaction_finished` payloads also include `failureReason` — a
  short human-readable cause set on `failed` outcomes tripped by a known
  safety limit (per-turn compaction round limit, side-call spend budget).
  Frontends should show it in place of the duration on the failure state;
  it is empty for ordinary generation failures.
- `chrys/sub_agent_compaction_committed` (payload `{ sessionId, agentName,
  invocationId, compactionId, phase }`) trails a `finished` `ok` outcome for
  the same `compactionId` once the round is durably applied — spill written,
  note set, messages excluded. A `finished(ok)` round without it was
  abandoned (its spill write failed), so clients that count or persist real
  compactions must key on this notification, not on `finished`. It is
  extension-only: the standard bridge stays silent because the finished
  update already narrated the outcome.
- Approval-mode changes use the standard `current_mode_update` `session/update`,
  not an extension.

## Standard vs. extension

Capabilities that map cleanly to ACP semantics use standard methods; the rest are
extensions because ACP has no equivalent concept.

**Standard (already migrated):** approval mode (`set_session_mode` + session
modes), model switch (`set_session_model` + session models).

**Extension by necessity (no ACP concept):** rollback, mutations/diff, sub-agent
hierarchy + retry/abort, runtime details (tools/MCP/skills introspection), agent
profile CRUD, `mcp/test`, mid-run injection, agent-load progress, context/tool
compaction, structured errors/warnings.

### Why config options stay an extension

ACP has a native config mechanism (`config_options` on `session/new`,
`set_config_option`, `current_config_option_update`). Chrys deliberately does
**not** use it for its config keys, because the semantics don't match:

- **Scope.** ACP `set_config_option` is *per-session*; Chrys's keys (the
  `AcpConfigOption` descriptors in `_SUPPORTED_CONFIG_OPTIONS`) write the global
  user settings document. A per-session advertisement of a global value would lie
  about scope and bleed across sessions.
- **Lifecycle.** The settings-document write persists to disk and is shared with
  the TUI and future processes. ACP config options have no "global application
  setting" concept.
- **Defaults vs. live state.** Keys like `default_agent` / `default_approval_mode`
  are defaults for the *next* session, not live controls for the current one.
  `default_approval_mode=bypass` is downgraded to `auto` by the settings store's
  own `persist` so a later launch never starts in unattended approval.
- **Type.** ACP config options are `select` / `boolean` only;
  `rollback_snapshots_keep` is an integer and several keys are free identifiers.
  Values are judged by the same coercer that will read them back, *before* the
  document write (a non-integer `rollback_snapshots_keep` is rejected), so an
  unparseable value can't persist and break a later launch/reload.

So the config extension is intentionally the home for *global, persisted
application defaults*.

## Security notes

- **No config-key injection.** Config values land in the YAML settings document,
  which stores them verbatim — there is no line-oriented format for a newline to
  smuggle a second key into. With the `_SUPPORTED_CONFIG_OPTIONS` allowlist, a
  client cannot write arbitrary settings keys.
- **Secret masking.** `read_model_profile` masks `api_key`; `read_agent_profile`
  masks MCP `headers`/`env` values. Write paths restore masked sentinels from the
  stored profile so a read → edit → write round-trip never destroys secrets, and
  reject unresolved masked MCP values after server rename/add.
- **Profile path safety.** Agent profile names and model profile ids accepted over
  ACP must be filename-safe stems, not absolute or relative paths.
- **No client stdio execution.** `mcp/test` accepts HTTP MCP configs only and keeps
  client-supplied HTTP headers literal; stdio MCP execution remains limited to
  trusted profile configuration.

## Gaps vs. the full ACP protocol

Standard ACP methods Chrys does **not** implement:

- `authenticate` — Chrys advertises `auth=None`.
- `fork_session` / `resume_session` — defer or implement as standard (not ext).
- `set_config_option` (standard typed config) — intentionally replaced by the
  global config extension (see *Why config options stay an extension*).

Standard client capabilities Chrys does **not** use (server → client):

- `read_text_file` / `write_text_file` — editor-mediated file I/O. Chrys writes
  files directly and surfaces changes via `session/diff` + `session/mutations`.
- Terminal suite (`create_terminal`, `terminal_output`, `release_terminal`,
  `wait_for_terminal_exit`, `kill_terminal`) — no shell/terminal delegation yet.

Standard `session/update` variants Chrys does **not** emit:
`available_commands_update`, `current_config_option_update`.

## Remaining feature gaps / TODO

Priority: P1 = high-value VS Code UX, P2 = can stay TUI-only for now.

- **[P1] Session-level retry/continue.** `UserRetry` / `RetryAttempt` not exposed
  (only sub-agent retry is). Needs a `session/retry` RPC + `chrys/retry_attempt`.
- **[P1] Approval-judge detail depth.** `chrys/approval_reviewed` carries the final
  decision; pending/intermediate judge metadata is not surfaced.
- **[P1] Richer CRUD validation.** Profile/MCP write/delete is functional, but
  field-level validation feedback and a config-dir MCP overlay beyond
  session-create are still thin.
- **[P2] Image attachment compression events**
  (`ImageAttachmentCompressionStarted/Finished`).
- **[P2] Shell mode / terminal delegation** — use ACP's standard terminal client
  methods rather than a new extension.
- **[P2] Queryable backend logs** (`session/logs` / `runtime/logs`).
- **[P2] Session fork/resume** — use standard `fork_session` / `resume_session`.
- **[P2] Editor file mediation** — adopt standard `read_text_file` /
  `write_text_file` so VS Code can mediate edits (unsaved buffers).
- **[P2] Session raw/debug read**, gated to local clients.
- **Out of scope by design:** notifications config, theme, and the Buddy
  surface remain frontend-local cosmetic UX, not shared backend capabilities.

### Capability discovery (known limitation)

Extension methods are not advertised in `initialize` capabilities or versioned —
clients must know the `chrys/*` / `session/*` method names by convention; unknown
methods raise `method_not_found`. A future improvement is to advertise the
extension surface (and its version) during `initialize`.

## Suggested implementation order

1. Session-level retry/continue (matches TUI interaction model).
2. Approval-judge detail surfacing (trust/diagnosis).
3. CRUD validation + MCP overlay hardening.
4. Capability discovery during `initialize`.
5. Lower-priority P2 items as VS Code UX demands.
