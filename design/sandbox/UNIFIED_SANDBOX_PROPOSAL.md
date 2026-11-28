# Proposal: one sandbox configuration, explicit backend capabilities

Status: proposed for review through [PR #334](https://github.com/openJiuwen-ai/iCode/pull/334).
This document does not implement a backend selector, change runtime behavior, or
claim that the two implementations have passed combined acceptance. The YAML and
interfaces below are proposals, not configuration supported by this PR today.

## 中文摘要与评审请求

建议用同一个用户级 `settings.yaml` 中的 `sandbox` 配置，统一选择沙箱方案、
解释安全要求、管理生命周期并报告实际保护范围。第一版选择一个后端：
`process` 对应轻量进程保护，`jiuwenbox` 对应 #223 / #341 的 Shell 沙箱。
未来可以增加其他后端及经过验证的组合方案；本提案不默认叠加两套机制。

统一的是配置和契约，不要求不同后端提供相同保护能力，也不要求把所有引擎
打进同一个安装包。不支持的安全要求必须明确拒绝，不能忽略或自动切到宿主机。
切换后端需要重启所属 iCode 会话进程；已有进程级限制不能通过设置热更新解除。

本次仅把具体设计放入 PR1 供评审，保留已有八项修复。建议由 PR1 作者承担
公共层、轻量后端及 jiuwenbox 适配初稿；请 #341 作者复核服务生命周期和实机
行为，请维护者确认接口边界及合入顺序。这是分工建议，尚非其他贡献者的承诺。
PR2 在 PR1 合入前不发布新的公开分支。

需要评审确认：配置入口及字段、后端契约、首版切换边界、旧配置迁移、
PR 拆分与验收矩阵。以下给出可讨论的具体建议。

## 1. Evidence and scope

The reviewed integration points are pinned so later changes can be distinguished:

- PR1 [#334 at f32008e8](https://github.com/openJiuwen-ai/iCode/blob/f32008e8ae8a7ae48157db82cada7c1b9cc827e3/src/chrys/foundation/guard.py)
  provides a core facade and call sites. The actual process guard is the separate,
  not-yet-merged PR2 package; PR1 alone is not an enforcing sandbox.
- [#341 at 161a85c3](https://github.com/openJiuwen-ai/iCode/blob/161a85c3fe8d4d3e90220a2c1bb26215b1347afe/src/chrys/service/tools/builtins/shell.py#L1023)
  continues [#223](https://github.com/openJiuwen-ai/iCode/pull/223). Its Shell path
  calls the jiuwenbox backend directly. Its settings and runtime hold currently
  belong to that integration, rather than a common backend registry.
- [The #341 guide](https://github.com/openJiuwen-ai/iCode/blob/161a85c3fe8d4d3e90220a2c1bb26215b1347afe/docs/en/guides/configuration/sandbox.md)
  documents the behavior summarized below. Backend documentation is not a substitute
  for real-kernel acceptance of the eventual unified implementation.

| Property | Process backend (PR2 design) | jiuwenbox integration (#341) |
| --- | --- | --- |
| Covered operations | Core file access and launched programs, including Shell, stdio MCP and hooks | Shell commands and their descendants |
| Platforms | macOS Seatbelt; Linux Landlock plus seccomp | Linux; requires bubblewrap, Python and Landlock |
| Activation | Before threads, with an unconfined broker started before main-process confinement | Service acquired when executing Shell; engine/runtime owns cleanup |
| Files | Workspace/grant access with credential and startup-file floors | Writable workspace mounts, selected read-only covers and hidden configuration/session paths |
| Network egress | Not controlled | Bundled service: guarded, host or offline; external service decides its policy |
| Resources | No advertised memory/process quota | Memory/process caps depend on cgroup availability; file/disk limits also apply |
| Shell output/background | Preserve supported host-facing facilities under confinement; revalidate after adaptation | No streaming/PTY/background; bounded captured output |
| Existing limitations | Host services reached through sockets remain outside its boundary; platform capabilities differ | Does not confine non-Shell tools; read-only is not read-denied; guarded permits public egress |

No generic boolean named `secure` or `active` should imply all these properties.

## 2. Proposed user configuration

Use the existing user-owned settings file as the single entry point. Do not add a
second file with an independent precedence chain in the first version. Named
profiles are configuration objects, not loadable Python module paths.

```yaml
sandbox:
  version: 1
  enabled: true
  profile: local-files
  on_unavailable: refuse
  profiles:
    local-files:
      backend: process
      require:
        file_tools.read_boundary: true
        shell.write_boundary: true
      options: {}
    isolated-shell:
      backend: jiuwenbox
      require:
        shell.write_boundary: true
      options:
        network: guarded
        url: ""  # bundled service; explicit external URL otherwise
```

Selecting `isolated-shell` does not imply that file tools, MCP or hooks are
confined. Requiring `file_tools.read_boundary` with that backend must fail until
an implementation actually supplies it. Likewise, `network: offline` cannot be
silently accepted by the process backend. `guarded` means proxy-mediated public
egress, not a guarantee against data exfiltration.

Backend-specific options use a typed schema; unknown options in the selected
profile are errors. All profiles receive structural validation, but inactive
profiles must not import engines, contact services or cause missing-package
failures. Secret options remain in the trusted user configuration, never in
project-controlled profiles, diagnostic output or propagated command-line args.

## 3. Configuration authority and migration

The existing settings machinery remains authoritative. CLI, environment and user
settings must yield one typed decision with source provenance; startup and agent
reload must not parse the same values differently. Extract the minimum shared
resolution needed before threads, rather than importing service runtimes into
foundation or maintaining a second boolean parser.

- Backend/profile selection, URL, token, failure policy and profile definitions
  are user-authority settings. Workdir settings cannot supply or override them.
- Preserve the existing opt-in workdir-only process-guard request as a narrow
  legacy input during migration. It cannot weaken an enabled user request, change
  backend, redirect commands or implicitly authorize paths. A general workdir
  tightening language is deferred until its monotonicity can be validated.
- `--sandbox` enables the selected trusted profile; it does not pick a different
  backend depending on what happens to be installed. An explicit request with no
  resolvable profile is an actionable error. Legacy flags retain legacy meaning
  when the new schema is absent.
- Never auto-select a weaker backend after an unsupported-platform, missing-package,
  negotiation, startup or execution failure.

Proposed compatibility rules, to be tested against the exact branches that land:

| Input | Resolution |
| --- | --- |
| Only legacy `sandbox.files.*` / `CHRYS_FILE_SANDBOX*` | Process backend; retain effective source, grants/options and failure policy |
| Only legacy #341 `sandbox.enabled/url/token/network` / corresponding env | jiuwenbox; retain its enablement and service behavior |
| Both legacy implementations enabled | Reject ambiguity with migration instructions; do not silently combine or choose |
| Versioned unified configuration | Resolve that schema; conflicting legacy inputs are diagnosed, not ignored |
| New and legacy inputs express exactly the same effective policy | May accept with a migration notice after equivalence is defined and tested |
| Invalid selected configuration or unknown backend | Refuse activation/execution; never reinterpret as disabled |

Do not rewrite users' files on startup. Offer explicit migration guidance and
eventually a previewable migration operation if needed. The `version` discriminator
separates the old jiuwenbox `sandbox.enabled` from the proposed common key.

## 4. Capabilities and effective policy

Separate three facts: what the adapter can support, what this host/service can
currently enforce, and what this particular session actually activated. A package
being installed or `/health` returning OK establishes neither enforcement nor
coverage. Represent capability scope and limitations, not just an unscoped boolean.

The first contract should describe file read/write boundaries, credential floors,
network modes, memory/process quotas and Shell streaming/PTY/background support.
Capabilities may be unsupported, available or enforced, with reasons and scope.
For example, cgroup unavailable means memory/process quotas are not enforced;
an explicitly required quota must therefore refuse the operation.

`require` entries are mandatory and checked before side effects. Optional limits
may be best-effort only when the chosen profile explicitly defines them that way;
their absence is visible. Unknown requirements fail closed. External services
must negotiate every relied-on capability; #341's `hidden_host_paths` negotiation
is a useful starting point, not proof of network or resource policy equivalence.

## 5. Interface and ownership proposal

Names below are illustrative; do not introduce a single mandatory interface that
forces a Shell service to pretend it confines the iCode process.

| Contract | Input / output | Owner and obligations |
| --- | --- | --- |
| Resolve | Trusted configuration sources -> immutable `SandboxDecision` | Foundation/config; validated backend ID, policy, provenance and diagnostics; no engine import |
| Describe/probe | Decision + host/service facts -> `CapabilityReport` | Trusted adapter; separate static description from owned runtime probing |
| Prepare process | Decision + launch roots -> `ProcessHandle` | Early application composition; process adapter starts broker and confines before threads |
| Open session | Handle + session ID/workspace -> `SessionHandle` | Orchestration; explicit per-session owner, shared services held by leases |
| Execute Shell | Session + argv/cwd/env/timeout -> execution handle/result | Service adapter; output limits, cancellation and descendants have an explicit owner |
| Guard file/tool access | Session + operation/grant context -> allow/deny/approval offer | Process extension of the contract; preserve current `ToolCallHost` and denial provenance |
| Update grants | Session + authorized scope change -> new effective boundary | Only adapters declaring this capability; revocation semantics must be documented |
| Status/close | Owned handle -> effective status / completed cleanup | Idempotent, cancellation-safe cleanup; bounded remote cleanup failures are reported |

Common contracts and immutable policy types belong in foundation. Concrete HTTP
execution, supervisors and engine ownership stay in service/orchestration. Early
startup uses an explicitly trusted adapter entry point without reversing the
repository's layering DAG. Preserve the existing distribution identity checks;
configuration must never import an arbitrary module or choose whichever entry
point wins discovery order.

`foundation.guard` can remain a compatibility facade while decisions and shared
status move to a common contract. Existing core call sites need not all be renamed
at once. jiuwenbox's Shell adapter should receive the resolved decision and owned
session, rather than independently re-reading settings.

## 6. Activation, switching and failure

First version: one backend/profile per iCode runtime process. Multiple sessions
within that process share the choice but retain their own workspace and grants.
Changing backend or startup policy requires a fresh owning runtime process.
For `serve`, that is the browser session child, not the HTTP parent. In ACP, do not
suggest that opening another session changes irreversible process confinement.

Grant updates may happen live if supported. Do not release an old runtime until
its in-flight operations have finished or been explicitly cancelled. A process
already confined cannot be unconfined by `--no-sandbox` in a child or a settings
reload. Forks inherit the effective decision and its authority, not a lossy
enabled flag or raw credentials; do not trust an environment marker as proof of
kernel confinement.

`on_unavailable: refuse` is the recommended default. If a trusted user explicitly
chooses `warn`, any unprotected continuation must be a startup-only decision made
before confinement or command admission, with persistent inactive/requested status.
Invalid configuration, failed mandatory requirements, partial confinement and
an active backend failing a command are never grounds for host fallback. Whether
to expose warning continuation in the new schema or keep it legacy-only is an
explicit review question.

## 7. Frontends and approvals

Use one status shape across CLI, TUI, ACP and audit records: requested profile,
backend, actual state, per-scope enforcement, reasons and restart-needed state.
Keep the current ACP state enum (`active`, `degraded`, `inactive`) compatible;
add fields through a documented extension rather than changing enum meanings.
Show the coverage alongside active status: "Shell isolated" differs from
"process/file access confined". Redact secrets and sensitive option values.

Approval remains separate from enforcement. User approval does not manufacture
backend capabilities or bypass immutable credential/startup-file floors. Preserve
`sandbox_denied` and its source across tool results, main/sub-agent hooks and ACP.
Use backend capabilities to control Shell UI promises; do not disable background
tasks merely because a generic sandbox-enabled flag is true.

## 8. Delivery and proposed coordination

1. Review this proposal in #334 with #341's authors and the maintainer. Confirm
   schema, capability scope, lifecycle split, migration and merge order.
2. Implement the common foundation and contract tests in PR1 after agreement.
   Preserve its eight fixes and regression tests. Missing adapters fail explicitly;
   test adapters demonstrate the contract without shipping a fake production sandbox.
3. Adapt the process implementation in PR2 after PR1 lands. Preserve kernel,
   broker, helper, grant and security-floor implementations; remove duplicate policy
   resolution and re-run acceptance against the new integration.
4. Prepare a narrow jiuwenbox adapter against the common foundation. Suggested
   initial implementation owner: PR1 author; review/real-engine validation with
   #341's authors, subject to their agreement. Do not require them to rewrite the
   engine or freeze unrelated correctness fixes.
5. If #341 lands first, adapt the merged code in a follow-up PR. Otherwise agree
   whether its adapter belongs in #341 or a dependent follow-up. Avoid mutual PR
   dependencies. Do not change another contributor's branch without coordination.

Composition is a separate later milestone. Its proof must cover nested restriction
semantics, broker/service startup privileges, sockets, mount operations, workspace
authorization, cancellation and cleanup. Do not advertise the union of two
capability tables as enforced until the composed execution path is tested.

## 9. Acceptance matrix for the implementation

These are planned tests, not tests completed by this documentation commit.

| Area | Required evidence |
| --- | --- |
| Configuration | CLI/env/user/workdir precedence; malformed booleans; unknown profile/backend/options; both legacy modes; migration preserves denial policy |
| Trust | Project cannot redirect service, inject token, load plugin, turn protection off or weaken user requirements |
| Optional loading | Off path does not import/start engines; inactive profiles do not require packages; core-only installation reports missing selected adapter |
| Capability contract | Required file boundary rejected by Shell-only adapter; offline rejected by process adapter; mandatory quotas rejected without cgroups; old external service rejected for relied-on features |
| Early activation | Process adapter runs before threads; partial activation aborts; no invocation after probe/activation failure |
| Frontend propagation | TUI, headless, ACP, serve on either side of flags, fork, sub-agents and workflow nodes retain effective policy and correct coverage |
| Lifecycle | Cold-start cancellation, repeated cancellation, queued work, last-owner release, external service switch and shutdown all preserve resource ownership |
| Results | Exit/timeout/denial distinguished; no accidental replay or host fallback after transport loss; bounded output; secrets absent from status/errors |
| Real process backend | macOS/Linux out-of-scope reads/writes, credential/startup floors, grant/revoke, MCP/hooks and workspace restore under actual kernel enforcement |
| Real jiuwenbox | Linux workspace/write restrictions, hidden paths, each network mode, quotas when available/unavailable, cancellation/reaping and external compatibility |
| Regression | Off-path behavior; existing eight PR1 fixes; full relevant suites, architecture, formatting, typing, i18n, packaging and installed-wheel probes |

Negative controls must show that enforcement tests fail when the relevant denial,
capability check or cleanup is removed. Mocked contract tests do not establish
kernel enforcement. Record exact code/base SHAs and environment; prior standalone
backend results are not acceptance for the new integration or for composition.

## 10. Decisions requested

1. Keep one `sandbox` section in the existing user settings file, with versioned
   profiles, or prefer a different concrete schema before implementation?
2. Agree on separate process and execution capabilities under one coordinator,
   rather than treating all backends as interchangeable Shell executors?
3. Accept one backend per runtime process and restart-required switching for v1?
4. Keep warning continuation legacy-only, or expose the restricted startup-only
   form described above? Mandatory requirements always remain fail-closed.
5. Confirm ownership and PR order for the common layer, process adapter and
   jiuwenbox adapter; identify any already-started work to avoid duplication.
