# Chrys Hooks — Working Directory Configuration

Working directory hooks provide directory-local configuration that
supplements the global `<config_dir>/hooks/hooks.yaml`. The working directory
source runs first.

This document is the design for that feature. For the YAML format see
[configuration.md](configuration.md); for the script contract see
[authoring.md](authoring.md); for the overall hook system see
[design.md](design.md).

---

## Trust and reload

Enable `workdir.hooks_enabled` in the user settings to load hooks from the
working directory. The setting defaults to off, and a working directory file
cannot enable it. iCode reports `workdir_hooks_dormant` when it finds a hook
file while the switch is off. Hooks run as subprocesses with the user's
permissions.

iCode loads the hook file when it builds the session's hook manager. It
rebuilds the manager on session creation or restore, a change to
`primary_cwd`, or a settings reload that changes the switch.

## Non-goals

- Ancestor discovery. Lookup starts and ends at the session's `primary_cwd`.
- Hot reload. Edits to `hooks.yaml` take effect at the next hook-manager build.
  Profile and model switches that keep the same `primary_cwd` reuse the
  current manager.
- A `--no-workdir-hooks` CLI flag or trust tiers. Use `workdir.hooks_enabled`
  in user settings to control loading.
- Nested sources. Each session uses at most one working directory hook file.

---

## Discovery

The working directory root is **`engine.workspace.primary_cwd`**, the same value the
engine uses everywhere else for "where the session lives":

- It is set to `os.getcwd()` (or the passed `cwd` argument) on a new
  session, written into `SessionMeta.primary_cwd` on save, and restored
  from `SessionMeta` on load.
- ACP `session/new` / `session/load` requests carry their own cwd, or use
  the `chrys acp --workdir` default when one was provided. That cwd becomes
  the session's `primary_cwd`.
- For session restore across workspaces, the working directory file is loaded from
  the **session's** `primary_cwd`, not the current shell cwd. This is
  the same behavior as skills and `AGENTS.md` — workspace content is
  always read against the workspace it belongs to.

No git-root walk. No `.chrys/` ancestor walk. If the user is in a
monorepo subdirectory, the working directory file (if any) is the one in that
subdirectory, not the repo root.

---

## File location

```
<workspace.primary_cwd>/.chrys/hooks/hooks.yaml        # .yml / .json also accepted
<workspace.primary_cwd>/.chrys/hooks/scripts/...       # working directory scripts
```

The directory layout mirrors the global one 1:1. The same file-naming
precedence applies: `hooks.yaml` > `hooks.yml` > `hooks.json` (first
existing file wins).

`HookRun.path` is resolved relative to the **file's own directory**:

- A `path: scripts/foo.py` in the **global** file resolves to
  `<config_dir>/hooks/scripts/foo.py`.
- A `path: scripts/foo.py` in the **working directory** file resolves to
  `<workspace.primary_cwd>/.chrys/hooks/scripts/foo.py`.

This is a generalisation of the existing rule ("resolved relative to
`<config_dir>/hooks/` if not absolute") — the resolver uses the file's
own directory as the base, regardless of which file the entry came from.

---

## Merge rules

| Aspect | Rule |
|---|---|
| Hook ordering | Working directory hooks run in their YAML order, then global hooks in their YAML order. |
| Same physical file as global | If the working directory and global source resolve to the same file, the working directory source is dropped; the global source is used once and no collision warning is emitted. |
| `id` collisions across files | **Both run** (working directory first). A `WARNING` is logged at load time naming the shared `id` and both `source` paths. To suppress a global hook, set `enabled: false` in the **global** file. |
| `id` collisions within a file | Rejected at load time (existing rule, unchanged). |
| Blocking events | A `block` returned by any working directory hook ends the chain — the action is denied and global hooks for the **same event** are skipped. This is the "first block short-circuits" rule extended across the working directory-then-global ordering. |
| Observer events | Both layers run, in order. `block` from a working directory observer hook is ignored (existing rule). |
| `modify` (args rewrite) | Flows working directory → global. A working directory `modify` rewrites the envelope; the global layer sees the rewritten args. |
| `match` | Unchanged. Matchers still scope by `profile` / `tool_kind` / `tool_name` / `args`. |
| `settings` | Per-field merge: a field set in the working directory file wins, otherwise the global field, otherwise the dataclass default. |
| `enabled: false` | Honored per-entry, regardless of source. |
| `execution` / `run` | Copied verbatim from whichever source the entry came from. |

---

## Runtime artifacts

`HookManager(hooks_dir=...)` continues to use a single `hooks_dir` for
`logs/`, `tmp/`, and `outbox/`. With the working directory feature, that
`hooks_dir` is still `<config_dir>/hooks/` (the global one). Working directory
hooks share the same chrys-managed runtime directories.

Working directory scripts are free to write their own artifacts anywhere — most
naturally to `<working directory>/.chrys/hooks/` — but chrys does not create or
manage that path. Scripts that need working directory-local logs / state can do
so themselves; chrys-managed durability, the outbox retry policy, and
session-scoped log routing stay global to keep the recovery story
simple.

This is a deliberate trade-off: working directory hooks are treated as a
**configuration** source, not a separate runtime. If a working directory needs
isolated durable outbox behavior in the future, that is a follow-up.

---

## Security and trust

Hooks can launch arbitrary subprocesses with the user's permissions, outside
the tool approval flow and without a hook sandbox. The global
`<config_dir>/hooks/hooks.yaml` belongs to the user. A working directory hook
file can arrive with a cloned repository.

Loading that file requires opting in with `workdir.hooks_enabled` in user
settings. Treat enabling it as a decision to trust the directories you open.
The same user-controlled opt-in applies to directory settings through
`workdir.config_enabled` and to directory skills through `workdir.skills_enabled`.

---

## Error handling

Per-file isolation, symmetric with the existing global-file path:

- **Working directory file parse error** → `WARNING` is published via
  `engine._bus`, `working directory = None`, the merge falls back to
  global-only. The same `HooksConfigError` message that already
  identifies the failing path is preserved.
- **Working directory file IO error** (permission denied, etc.) → same handling.
- **Global file error** → existing behavior, unchanged.
- **Per-hook runtime error** → governed by the hook's own `on_error`
  policy (`block` / `warn` / `ignore`), unchanged.
- **Manager construction with no usable sources** → `engine._hook_manager`
  is left as `None`; the existing "no manager → no-op" path applies.

A configuration file that contains only hook settings still participates in
the merge.

---

## Durable recovery

Outbox records use the hook source and configuration path to distinguish
same-ID hooks across layers. Pending records without source fields still load.
A valid terminal record takes precedence over a leftover pending record for
the same job.
