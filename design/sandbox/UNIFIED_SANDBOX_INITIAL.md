# Unified sandbox configuration: executable review draft

This is an isolated implementation proposal based on PR1 `bdc92fa1`.
It adds a pure common layer and contract examples. No existing production
entrypoint imports it; the running application still uses its existing guard
configuration and behavior. Do not put the proposed YAML into live settings yet.

## 中文摘要

本轮把统一配置提案变成可运行的解析器、不可变决策和能力预检契约，供双方
评审。所有既有 PR1 生产文件和八项修复保持原样；没有接通新配置到启动、
Shell、TUI 或 ACP，也没有实现两个真实后端的统一运行时。

初稿验证配置来源、后端选择、拒绝行为及能力范围。测试中的适配器仅返回
预设能力，不执行命令，不证明 Linux/macOS 内核隔离。旧配置迁移、真实后端
生命周期、CLI/env 名称及设置面板注册等仍须评审和后续接入。

## Implemented surface

- `foundation/sandbox/configuration.py`: `resolve_configuration` consumes an
  already-read **sandbox section**, separately from extracted environment/CLI
  override fields and a workdir section. It does no filesystem or environment IO.
- `models.py`: immutable decision, per-field source, safe configuration errors,
  typed backend options, scoped capability IDs and reports.
- `options.py`: explicit `process` and `jiuwenbox` option schemas, without engine
  discovery or imports. This schema list does not mean an adapter is installed.
- `preflight.py`: injected trusted probe map, selected-backend preflight and
  mandatory requirement checking. No production registry or engine activation.
- `tests/foundation/unified_sandbox/`: two example probes and contract tests.

## Try the pure configuration API

Run through the project's `uv run python` environment:

```python
from chrys.foundation.sandbox.configuration import resolve_configuration
from chrys.foundation.sandbox.models import Capability, CapabilityReport
from chrys.foundation.sandbox.preflight import preflight

decision = resolve_configuration({
    "version": 1,
    "enabled": True,
    "profile": "isolated-shell",
    "profiles": {
        "isolated-shell": {
            "backend": "jiuwenbox",
            "require": {"shell.write_boundary": True},
            "options": {"network": "offline"},
        },
    },
})

class ExampleProbe:
    def probe(self, decision):
        capabilities = frozenset({Capability.SHELL_WRITE, Capability.NETWORK_OFFLINE})
        return CapabilityReport("jiuwenbox", capabilities, capabilities)

if decision is not None:
    report = preflight(decision, {"jiuwenbox": ExampleProbe()})
    print(report)  # available, NOT enforced; no service was contacted or started
```

Replacing `shell.write_boundary` with `file_tools.read_boundary` refuses the
Shell-only report. Calling preflight with an empty adapter map refuses the missing
selected adapter. Neither case tries a different backend or executes on the host.

## Draft behavior and deliberate boundaries

1. Inputs use nested mappings, not flattened settings keys. Existing settings
   registration/flattening needs a separate reviewed integration. No new OS env
   names or flags are installed. `environment`/`cli` are fields extracted by a
   future caller, not `os.environ` or raw argv.
2. USER < ENV < CLI for `enabled`, `profile`, `on_unavailable`. Only USER defines
   profiles. Every supplied trusted override is validated, even if a later source
   shadows it. Definitions come from USER; diagnostic provenance is retained.
3. Versioned workdir settings are ignored with a diagnostic and cannot define
   profiles, disable protection, change failure policy, or redirect a service.
   Legacy workdir-only process requests remain on the untouched legacy path.
4. `None` means no new-schema input and returns `None` for future legacy handoff.
   Explicit unversioned/legacy sections or mixed new/old fields raise an error.
   This is not an implementation of legacy migration or equivalence detection.
5. Version 1 requires a selected user-defined profile even when disabled. All
   profiles have structural/requirement validation; only the selected profile
   needs a known backend and typed options. Inactive backend options are opaque
   mappings and do not import a package or contact a service.
6. Process options are empty in this draft; existing paths, grants and security
   floors remain in the legacy implementation. jiuwenbox options are network,
   HTTP(S) service origin and token. Endpoint/token values are omitted from repr
   and error messages. This is not a general-purpose secret serializer: callers
   must not serialize the entire decision/options for logs or subprocess argv.
7. `network: offline` and `network: guarded` imply mandatory scoped capabilities.
   An adapter must verify the actual service mode; merely reading the option or
   finding the package is insufficient. `host` requests no network isolation.
   External-service negotiation is still unimplemented; don't infer enforcement
   from the client configuration, including the default guarded mode.
8. `require: false` removes that explicit requirement; it never turns a protection
   off. Unknown requirements are errors even when false. Mandatory requirements
   cannot be waived by `on_unavailable: warn`. No warn continuation is implemented.
9. `supported`, `available`, `enforced` are different sets, scoped by capability ID.
   Preflight reports must have no enforced capabilities. After a future activation,
   `check_requirements(..., activated=True)` checks a fresh owned-runtime report.
   This helper trusts the adapter's facts; it cannot prove kernel enforcement.
10. Probes in this draft may inspect facts without side effects; they cannot
    activate confinement, spawn services, or execute commands. Async remote
    negotiation and resource ownership require a separate lifecycle contract.

## Dependencies and next integration steps

```text
PR1 common decision/configuration contracts
  +--> process adapter (PR2, after PR1 merges)
  +--> jiuwenbox adapter (on top of the engine in #341 or merged main)
```

There is no import dependency on #341 in this draft. Its engine correctness fixes
can proceed independently. Once the common API is accepted, its adapter should
consume the resolved decision, negotiate service features, report actual network
and quota capabilities, and wrap its existing Shell execution/lifecycle ownership.
It must not parse a competing copy of sandbox settings. We can draft that adapter;
ownership and real-engine acceptance still need agreement with its authors.

For the process adapter, keep the existing distribution identity checks, before-
threads activation, irreversible confinement, broker and grant/floor semantics.
The common probe is not a replacement for `foundation.guard` or its `ToolCallHost`.

Before production wiring: agree schema/precedence and legacy migration; define
early process preparation vs session/Shell execution ownership; preserve startup,
serve-child, fork, cancellation, denial metadata and ACP status regressions. Then
run real backend and installed-package acceptance. Runtime backend switching and
composition remain deferred.

## Review questions

- Is the nested versioned profile schema worth its integration cost, or should
  the first public schema be smaller while retaining the same decision type?
- Should the new schema expose `warn`, or keep it legacy-only? It currently stores
  the value but never authorizes fallback.
- Are scoped capabilities and separate availability/enforcement the right common
  boundary? Which fields need quantitative detail before real quota requirements?
- Should new configuration require explicit migration when legacy settings exist?
- Which PR owns jiuwenbox adaptation after the common layer is accepted?

Validation evidence is recorded separately for the exact candidate SHA. Passing
these contract tests does not mean unified runtime or combined sandbox acceptance.
