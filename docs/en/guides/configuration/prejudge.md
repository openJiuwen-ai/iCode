# Structured Shell Pre-Judge

iCode can optionally inspect Bash commands using Tree-sitter and a small set of **structured YAML matchers** before ordinary tool approval. It is **off by default**. Existing Hooks, approval policy, DAA (the existing authorization-reuse mechanism), remembered grants, and the **pre-execution** Shell Command Filter retain their existing behavior. In `enforce`, the previous read-only shortcut is re-expressed as shipped YAML policy data evaluated by the new AST parser and policy engine, rather than calling the old validator.

Place `prejudge.yaml` in iCode's trusted user configuration directory (`~/.chrys/prejudge.yaml` on macOS/Linux). Workspace files do not provide rules.

```yaml
schema_version: 1
mode: "shadow"
policy_id: example-v1
rules:
  - id: block-rm
    effect: block
    target: command
    match:
      prefix_first: [rm]
  - id: allow-git-status
    effect: allow
    target: command
    match:
      argv_exact: [git, status]
  - id: allow-echo
    effect: allow
    target: command
    match:
      argv_first: [echo]
```

### Matcher reference

Each rule has exactly **one** top-level matcher. Literal matchers remain available; a small, fixed vocabulary of structural matchers supports declarative policy conditions (not arbitrary code or a general-purpose expression language):

| Matcher | Meaning | Allowed effects |
| --- | --- | --- |
| `argv_exact: [git, status]` | Full literal argument vector equals exactly the given non-empty sequence | ALLOW or BLOCK |
| `argv_first: [echo]` | First token of the complete literal argument vector equals the given token | **ALLOW only** |
| `prefix_first: [rm]` | First *reliably known static prefix* token equals the given token, even if later arguments are dynamic | BLOCK only |
| `command_in: [ls, cat]` | Executable name is in a fixed set | ALLOW only |
| `args_none_of: [--delete]` | No argument equals a forbidden literal | ALLOW only |
| `all: [{command_in: [ls]}, {args_none_of: [--delete]}]` | All declared matchers agree | ALLOW only |
| `cli: {command: git, subcommand: status, ...}` | Declaratively validates subcommands, options and mandatory flags | ALLOW only |

Literal token-list matchers require **lists of non-empty literal strings**, even when there is only one token. The structural fields are strictly validated at load time; nested `all`/`any` conditions are bounded and are only eligible for ALLOW when the entire argv is statically known. An ALLOW rule using `argv_first: [git]` would grant *all* Git subcommands, including mutating ones; prefer `argv_exact` for commands with risk-bearing arguments. Using `argv_first` for BLOCK is rejected because it cannot block commands with dynamic suffixes (for example, `rm "$TARGET"` or `rm *.log`); use `prefix_first` for this purpose. Unknown keys, multiple matcher fields, empty lists, and non-string values are rejected during policy loading. The policy schema version is `1`.

### Decision safety

BLOCK has priority over ALLOW. An automatic ALLOW requires **every executable AST node** to match an ALLOW rule, complete static argv and full supported syntax coverage. Otherwise the structured rules DEFER. An implicit collection of **built-in YAML ALLOW rules** is loaded from `prejudge_v2/readonly_policy.yaml` and evaluated by the **new AST parser and structured matcher engine**. No production path calls the old `is_safe_readonly_command()` implementation. The rules cover common read-only utilities, a dangerous-token guard, and selected Git, pip, ruff and other command constraints. Explicit user YAML BLOCK rules take precedence. Unsupported or incompletely modeled constructs DEFER to the existing approval flow. Users need not copy the built-in policy into their configuration. A match on a prefix is **never sufficient for ALLOW**. A quoted dangerous word such as `echo "rm"` is not a separate executable `rm`.

The mode must be quoted: YAML may interpret unquoted `off` as a Boolean. `off` retains legacy behavior; `shadow` logs proposed decisions without enforcing them; `enforce` applies BLOCK and eligible ALLOW. Invalid enforce configuration fails during setup. A parseable configuration explicitly declaring `shadow` with invalid rules or missing dependencies logs a warning and disables observation. Malformed YAML syntax or an invalid/unknown mode fails configuration loading because the intended mode cannot be reliably established. Explicit BYPASS does **not** apply these BLOCK rules: headless `icode run` and `icode workflow run` use BYPASS.

The **new AST matchers** support only Bash / Git Bash. They do not automatically ALLOW unsupported shells (zsh, sh, dash, PowerShell, cmd.exe), redirections, dynamic expansions, globs, and most control-flow constructs. A warning is emitted for an unsupported shell dialect and the new AST policy DEFERs; the prior read-only checker is **not** used as a fallback. A reliably parsed inner command can still trigger BLOCK. Matching uses literal command words without normalizing executable paths. For example, `prefix_first: [rm]` does **not** match `/bin/rm -rf x`; the command DEFERs rather than being automatically allowed. Opaque command wrappers such as `command rm`, `env rm` and `time rm` are deferred: a rule for `prefix_first: [rm]` does not establish that the wrapped `rm` is a direct command node. This is not a complete Shell security boundary.

**Dependencies:** enabled configurations require the optional Tree-sitter parser packages `tree-sitter==0.25.1` and `tree-sitter-bash==0.25.1`. The packages must be installed for the running Python/platform; the normal `off` mode does not import them. The dependency lock and Linux offline-bundle verification are separate release gates.

**Approval lifecycle:** The initial Pre-Judge runs after `before_tool_call` Hooks. An explicitly human-edited and approved command runs through Hooks again, but is not sent through Pre-Judge or LLM Judge again, preserving the existing approval lifecycle. BYPASS also preserves the existing behavior: it does not apply the YAML BLOCK rules. Such BLOCK rules are approval-stage decisions, not a global mandatory deny-list.

### Compatibility and limitations

The built-in policy **reimplements** common legacy read-only decisions; it does not yet achieve full behavioral parity. In particular, safe file-descriptor redirections, certain environment-assignment prefixes, complex `find -exec`/`xargs`/`sed`/`awk` cases, POSIX `for` loops, and non-Bash dialects may now DEFER instead of auto-approving. These conservatively preserve the ordinary approval path rather than granting an unproven ALLOW. The old checker remains an independent regression oracle in tests only; do not describe this as full parity until the complete historical corpus passes. A static `argv_exact` rule never treats a glob as a fully resolved argument vector; built-in read-only rules may inspect limited literal glob patterns symbolically.
