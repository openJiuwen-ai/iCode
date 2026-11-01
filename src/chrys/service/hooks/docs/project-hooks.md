# Chrys Hooks — Project-Level Configuration

Project-level hooks let a workspace carry its own hook configuration that
supplements (and, on gated events, can override) the global
`<config_dir>/hooks/hooks.yaml`. The two sources are loaded together, the
project source runs first, and the same shape, matcher, runner, and outbox
machinery is reused without change.

This document is the design for that feature. For the YAML format see
[configuration.md](configuration.md); for the script contract see
[authoring.md](authoring.md); for the overall hook system see
[design.md](design.md).

---

## Goals

- Allow per-workspace hook configuration that ships with the repo.
- Reuse every existing hook subsystem: schema, matcher, runner, outbox,
  detached worker, events.
- Make the merge semantics explicit and predictable: **layered, project
  runs first; on a gated event a project block ends the chain (including
  the global layer).**
- Pure addition — no behavior change when no project file is present.
- Match existing per-workspace config patterns in Chrys (skills at
  `<cwd>/.agents/skills/`, workspace-relative `AGENTS.md`).

## Non-goals

- Walk-up directory discovery (no git-root walking, no project-marker
  fan-out). The workspace boundary is `primary_cwd`, full stop.
- Hot reload. Edits to `hooks.yaml` take effect on the next hook-manager
  build: new session, restore/reset, a workspace change that changes
  `primary_cwd`, or a settings reload that flips `project.hooks_enabled`.
  Profile/model switches that keep the same `primary_cwd` reuse the
  current manager.
- A `--no-project-hooks` CLI flag or a trust-tier system. These are
  deliberately deferred. The one switch that exists is the
  `project.hooks_enabled` setting (Settings → Safety → Project trust,
  default off; a project file cannot set it): off means the project file
  is not opened at all and only global hooks load. When a project file
  exists while the setting is off, the settings load reports it as a
  `project_hooks_dormant` warning (startup, reload and workspace change),
  which the TUI shows as a notice.
- Nested project sources. One project file per session.

---

## Discovery

The project root is **`engine.workspace.primary_cwd`**, the same value the
engine uses everywhere else for "where the session lives":

- It is set to `os.getcwd()` (or the passed `cwd` argument) on a new
  session, written into `SessionMeta.primary_cwd` on save, and restored
  from `SessionMeta` on load.
- ACP `session/new` / `session/load` requests carry their own cwd, or use
  the `chrys acp --workdir` default when one was provided. That cwd becomes
  the session's `primary_cwd`.
- For session restore across workspaces, the project file is loaded from
  the **session's** `primary_cwd`, not the current shell cwd. This is
  the same behavior as skills and `AGENTS.md` — workspace content is
  always read against the workspace it belongs to.

No git-root walk. No `.chrys/` ancestor walk. If the user is in a
monorepo subdirectory, the project file (if any) is the one in that
subdirectory, not the repo root.

---

## File location

```
<workspace.primary_cwd>/.chrys/hooks/hooks.yaml        # .yml / .json also accepted
<workspace.primary_cwd>/.chrys/hooks/scripts/...       # project scripts
```

The directory layout mirrors the global one 1:1. The same file-naming
precedence applies: `hooks.yaml` > `hooks.yml` > `hooks.json` (first
existing file wins).

`HookRun.path` is resolved relative to the **file's own directory**:

- A `path: scripts/foo.py` in the **global** file resolves to
  `<config_dir>/hooks/scripts/foo.py`.
- A `path: scripts/foo.py` in the **project** file resolves to
  `<workspace.primary_cwd>/.chrys/hooks/scripts/foo.py`.

This is a generalisation of the existing rule ("resolved relative to
`<config_dir>/hooks/` if not absolute") — the resolver uses the file's
own directory as the base, regardless of which file the entry came from.

---

## Loading

Two new entry points in `src/chrys/service/hooks/loader.py`:

```python
def load_hooks_project(project_root: Path) -> HooksFile | None:
    """Load <project_root>/.chrys/hooks/hooks.{yaml,yml,json} if present.

    Returns None when no project file exists.  Per-file parse errors
    raise (the caller in agent_lifecycle.py wraps with the existing
    warning-and-continue behaviour).
    """


def merge_hooks_files(
    project: HooksFile | None,
    global_: HooksFile | None,
) -> MergedHooksFile:
    """Combine two HooksFile sources into a single merged view.

    See "Merge rules" below for the exact semantics.
    """
```

The existing `load_hooks_dir(config_dir)` stays as the global loader. The
new function is its mirror for the project tree. Agent lifecycle builds
the merged manager during start/new/restore flows, and reloads it when a
workspace change moves the session to a different `primary_cwd`.

A new `MergedHooksFile` dataclass in `src/chrys/service/hooks/schema.py`
captures the combined state:

```python
@dataclass
class MergedHooksFile:
    project: HooksFile | None  # with its own .source
    global_: HooksFile | None  # with its own .source
    settings: HookSettings  # per-field merge, project wins
    hooks: list[HookConfig]  # project entries first, then global
    sources: list[str]  # for log / debug only
```

`HookManager` accepts a `MergedHooksFile` directly and still accepts a
legacy `HooksFile` by wrapping it with `MergedHooksFile.from_single(...)`.
Dispatch remains event-based; source metadata is retained for
`sources()` and durable outbox recovery.

---

## Merge rules

| Aspect | Rule |
|---|---|
| Hook ordering | Project hooks run in their YAML order, then global hooks in their YAML order. |
| Same physical file as global | If the project and global source resolve to the same file, the project source is dropped; the global source is used once and no collision warning is emitted. |
| `id` collisions across files | **Both run** (project first). A `WARNING` is logged at load time naming the shared `id` and both `source` paths. To suppress a global hook, set `enabled: false` in the **global** file. |
| `id` collisions within a file | Rejected at load time (existing rule, unchanged). |
| Blocking events | A `block` returned by any project hook ends the chain — the action is denied and global hooks for the **same event** are skipped. This is the "first block short-circuits" rule extended across the project-then-global ordering. |
| Observer events | Both layers run, in order. `block` from a project observer hook is ignored (existing rule). |
| `modify` (args rewrite) | Flows project → global. A project `modify` rewrites the envelope; the global layer sees the rewritten args. |
| `match` | Unchanged. Matchers still scope by `profile` / `tool_kind` / `tool_name` / `args`. |
| `settings` | Per-field merge: a field set in the project file wins, otherwise the global field, otherwise the dataclass default. |
| `enabled: false` | Honored per-entry, regardless of source. |
| `execution` / `run` | Copied verbatim from whichever source the entry came from. |

### Why "project block ends the chain"

The merged hook list is treated as a single ordered chain (project
first, global second). The existing "first block short-circuits" rule
(`docs/design.md:229`) applies to the chain as a whole. This gives the
project a clear "final authority on its workspace" semantic for gated
events without introducing a new aggregation rule.

The asymmetry with `modify` is intentional and matches the existing
within-source behavior: a `block` is a decision to stop; a `modify` is
a transformation that the next hook gets to see.

---

## Runtime artifacts

`HookManager(hooks_dir=...)` continues to use a single `hooks_dir` for
`logs/`, `tmp/`, and `outbox/`. With the project-level feature, that
`hooks_dir` is still `<config_dir>/hooks/` (the global one). Project
hooks share the same chrys-managed runtime directories.

Project scripts are free to write their own artifacts anywhere — most
naturally to `<project>/.chrys/hooks/` — but chrys does not create or
manage that path. Scripts that need project-local logs / state can do
so themselves; chrys-managed durability, the outbox retry policy, and
session-scoped log routing stay global to keep the recovery story
simple.

This is a deliberate trade-off: project hooks are treated as a
**configuration** source, not a separate runtime. If a project needs
isolated durable outbox behavior in the future, that is a follow-up.

---

## Security and trust

A hook is an arbitrary subprocess, executed without tool approval or a
sandbox. The global `<config_dir>/hooks/hooks.yaml` is the user's own
file; a project file comes with whatever repository was cloned. So
project hooks load only after the user turns on `project.hooks_enabled`
in their own settings — the same gate `project.config_enabled` puts on
project settings and `project.skills_enabled` on
`<cwd>/.agents/skills/`. Turning it on is a trust decision about the
repositories the user opens, not just a code-review decision.

A future `--no-project-hooks` flag on `chrys run` / `chrys acp` is a
useful escape hatch for CI and untrusted-workspace scenarios but is
**not** part of v1.

---

## Error handling

Per-file isolation, symmetric with the existing global-file path:

- **Project file parse error** → `WARNING` is published via
  `engine._bus`, `project = None`, the merge falls back to
  global-only. The same `HooksConfigError` message that already
  identifies the failing path is preserved.
- **Project file IO error** (permission denied, etc.) → same handling.
- **Global file error** → existing behavior, unchanged.
- **Per-hook runtime error** → governed by the hook's own `on_error`
  policy (`block` / `warn` / `ignore`), unchanged.
- **Manager construction with no usable sources** → `engine._hook_manager`
  is left as `None`; the existing "no manager → no-op" path applies.

The lifecycle path effectively does:

```python
config_dir = get_platform().config_dir
global_hooks = load_hooks_dir(config_dir)
if not global_hooks.source:
    global_hooks = None
project_hooks = load_hooks_project(Path(workspace.primary_cwd))
merged = merge_hooks_files(project=project_hooks, global_=global_hooks)
if merged.sources:
    engine._hook_manager = HookManager(file=merged, hooks_dir=config_dir / "hooks")
```

Both load calls are wrapped independently so `HooksConfigError` or
`OSError` in one source publishes a `Warning` event and disables only
that source. Checking `merged.sources` preserves settings-only hook files
that still need a manager.

---

## Backward compatibility

- `load_hooks_dir(config_dir)` is untouched and still works.
- The existing `HookManager(file=HooksFile, hooks_dir=...)` constructor
  variant is preserved; it normalizes the file into `MergedHooksFile`.
- Outbox job files gain optional `hook_source` and `hook_source_path`
  fields so durable recovery can distinguish project/global same-id hooks.
  Legacy pending jobs without those fields still load.
- No changes to `events.py`, `matcher.py`, `runner.py`,
  `detached_worker.py`, or any hook firing sites in `service/agent_middleware/`,
  `service/tools/`, `app/acp/`, or `app/cli/`. The merge happens at loader/manager
  construction time; dispatch callers stay agnostic.
- Existing tests continue to pass. New and expanded tests live next to
  the subsystems they exercise.
- `AGENTS.md`'s "Lifecycle hooks are global, not per-profile" bullet
  is updated to "Lifecycle hooks are global by default, with an
  optional project-level source that supplements them."

---

## Test coverage

Coverage is split by subsystem:

- `tests/service/hooks/test_loader.py`: project file discovery, extension
  precedence, parse errors, same-source suppression, project/global merge
  ordering, same-id warnings, and settings precedence.
- `tests/service/hooks/test_project_level.py`: end-to-end loader/manager smoke
  coverage for project-only, global-only, no-source, both-source,
  same-id, gated short-circuit, script path resolution, primary-cwd
  discovery, restore-style cwd handling, disabled hooks, and ordering.
- `tests/service/hooks/test_manager.py`: durable job source metadata, duplicate-id
  recovery disambiguation, stale project-source rejection, and ambiguous
  legacy recovery failure.
- `tests/service/hooks/test_runner.py`: project script path resolution.
- `tests/orchestration/engine/build/test_lifecycle_hooks.py`: startup construction,
  settings-only global files, workspace-change reload, replacement
  draining, and post-build failure handling.

---

## File-level change list

| # | File | Change |
|---|---|---|
| 1 | `src/chrys/service/hooks/loader.py` | Add `load_hooks_project(project_root) -> HooksFile | None`; add `merge_hooks_files(project, global_) -> MergedHooksFile`. |
| 2 | `src/chrys/service/hooks/schema.py` | Add `MergedHooksFile` dataclass with `project`, `global_`, `settings`, `hooks`, `sources`. |
| 3 | `src/chrys/service/hooks/manager.py` | Normalize `HooksFile` into `MergedHooksFile`; keep source/source-path metadata for `sources()` and durable recovery. |
| 4 | `src/chrys/service/hooks/outbox.py` | Persist optional source metadata on durable jobs while preserving legacy job loading. |
| 5 | `src/chrys/orchestration/engine/build/construction.py` | Build merged hook managers, isolate per-source load failures, reload on `primary_cwd` changes, and coordinate outbox recovery during manager replacement. |
| 6 | `src/chrys/service/hooks/docs/configuration.md` | New "Project-level configuration" section: discovery, file location, layered semantics, id-collision warning, relative-path resolution, settings merge. |
| 7 | `src/chrys/service/hooks/docs/design.md` | New "Multiple sources" subsection: load order, cross-source short-circuit, runtime-artifact location, and reload boundaries. |
| 8 | `src/chrys/service/hooks/docs/authoring.md` | Note that scripts in a project file resolve relative to the project file's directory. Note the security/trust implications. |
| 9 | `src/chrys/service/hooks/docs/project-hooks.md` (this file) | The design document. |
| 10 | `src/chrys/service/hooks/docs/README.md` | Add `project-hooks.md` to the table of contents. |
| 11 | `tests/service/hooks/test_project_level.py` (new) | Coverage for project/global discovery, merge ordering, settings merge, same-id warnings, and path resolution. |
| 12 | `tests/service/hooks/test_loader.py` | Unit tests for `load_hooks_project`, `merge_hooks_files`, and same-source suppression. |
| 13 | `tests/service/hooks/test_manager.py` | Durable outbox source metadata and recovery disambiguation tests. |
| 14 | `tests/service/hooks/test_runner.py` | Project script path resolution coverage. |
| 15 | `tests/orchestration/engine/build/test_lifecycle_hooks.py` | Startup and workspace-change hook-manager reload coverage. |
| 16 | `tests/orchestration/engine/test_shutdown_close.py` | Restart coverage for closed/no-hook manager state. |
| 17 | `AGENTS.md` | Update the "global, not per-profile" bullet to mention project-level supplement. |

No changes needed to: `runner.py`, `detached_worker.py`, `events.py`,
`matcher.py`, `app/acp/`, `app/cli/`, `orchestration/startup.py`, `service/profiles/`, or `service/tools/`
hook firing sites.

---

## Open questions

1. **Same-id collision behavior**: locked at "both run + WARN". The
   alternative is to reject cross-file collisions at load time, which
   is cleaner but breaks the layered intent. Decision recorded here.
2. **A `--no-project-hooks` flag on `chrys run` / `chrys acp`**: useful
   for CI and untrusted-repo scenarios; deferred from v1; trivial to
   add later as a flag on the engine start path.
3. **Per-source enable/disable**: covered by the `project.hooks_enabled`
   setting (default off; see Non-goals); a finer `disabled_sources` list in the
   global file is not planned.
4. **Project file in a sessionless invocation**: `chrys run "hi"`
   constructs a session and a workspace, so it picks up the project
   file. `chrys install` does not. No special handling needed.
5. **`${workspace_cwd}` template var** in project hooks: resolves to
   `primary_cwd`, which is the same value it would resolve to in a
   global hook for the same session. No new template var is needed.

---

## Summary

A small, additive change:

- Loader/schema changes add project discovery and a merged config view.
- Manager/outbox changes keep source metadata so duplicate IDs remain
  recoverable and legacy pending jobs remain valid.
- Lifecycle changes rebuild the hook manager at startup and when
  `primary_cwd` changes, while preserving warning-and-continue behavior.
- Docs and tests cover the new project/global layering, trust model,
  settings merge, path resolution, and recovery behavior.

The hook firing sites, matcher, runner, detached worker, and downstream
middleware/tool consumers remain unchanged.
