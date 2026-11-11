# Structured Shell Pre-Judge

iCode can optionally inspect Bash commands using Tree-sitter and a small set of **structured YAML matchers** before ordinary tool approval. It is **off by default**. Existing Hooks, approval policy, DAA (the existing authorization-reuse mechanism), remembered grants, and the **pre-execution** Shell Command Filter retain their existing behavior. In `enforce`, the legacy read-only shortcut is represented by an equivalent built-in policy rule.

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

Each rule has exactly **one** matcher (no expressions, globbing, regex or logical operators):

| Matcher | Meaning | Allowed effects |
| --- | --- | --- |
| `argv_exact: [git, status]` | Full literal argument vector equals exactly the given non-empty sequence | ALLOW or BLOCK |
| `argv_first: [echo]` | First token of the complete literal argument vector equals the given token | **ALLOW only** |
| `prefix_first: [rm]` | First *reliably known static prefix* token equals the given token, even if later arguments are dynamic | BLOCK only |

Matcher values must be **lists of non-empty literal strings**, even when there is only one token. An ALLOW rule using `argv_first: [git]` would grant *all* Git subcommands, including mutating ones; prefer `argv_exact` for commands with risk-bearing arguments. Using `argv_first` for BLOCK is rejected because it cannot block commands with dynamic suffixes (for example, `rm "$TARGET"` or `rm *.log`); use `prefix_first` for this purpose. Unknown keys, multiple matcher fields, empty lists, and non-string values are rejected during policy loading. The policy schema version is `1`.

### Decision safety

BLOCK has priority over ALLOW. An automatic ALLOW requires **every executable AST node** to match an ALLOW rule, complete static argv and full supported syntax coverage. Otherwise the structured rules DEFER. An **implicit built-in ALLOW rule** (`__builtin_readonly__`) then invokes the existing `is_safe_readonly_command()` validator on the **whole command**, preserving its Git and other command-specific validators, glob support, and supported shell dialects. Explicit YAML BLOCK rules take precedence. If the built-in rule does not match, the original approval flow continues. No manual YAML migration of the existing read-only allowlist is required. A match on a prefix is **never sufficient for ALLOW**. A quoted dangerous word such as `echo "rm"` is not a separate executable `rm`.

The mode must be quoted: YAML may interpret unquoted `off` as a Boolean. `off` retains legacy behavior; `shadow` logs proposed decisions without enforcing them; `enforce` applies BLOCK and eligible ALLOW. Invalid enforce configuration fails during setup. A parseable configuration explicitly declaring `shadow` with invalid rules or missing dependencies logs a warning and disables observation. Malformed YAML syntax or an invalid/unknown mode fails configuration loading because the intended mode cannot be reliably established. Explicit BYPASS does **not** apply these BLOCK rules: headless `icode run` and `icode workflow run` use BYPASS.

The **new AST matchers** support only Bash / Git Bash. They do not automatically ALLOW unsupported shells (zsh, sh, dash, PowerShell, cmd.exe), redirections, dynamic expansions, globs, and most control-flow constructs. A warning is emitted for an unsupported shell dialect, while the built-in legacy read-only policy continues to auto-approve commands exactly as before. A reliably parsed inner command can still trigger BLOCK. Matching uses literal command words without normalizing executable paths. For example, `prefix_first: [rm]` does **not** match `/bin/rm -rf x`; the command DEFERs rather than being automatically allowed. Opaque command wrappers such as `command rm`, `env rm` and `time rm` are deferred: a rule for `prefix_first: [rm]` does not establish that the wrapped `rm` is a direct command node. This is not a complete Shell security boundary.

**Dependencies:** enabled configurations require the optional Tree-sitter parser packages `tree-sitter==0.25.1` and `tree-sitter-bash==0.25.1`. The packages must be installed for the running Python/platform; the normal `off` mode does not import them. The dependency lock and Linux offline-bundle verification are separate release gates.

**Approval lifecycle:** The initial Pre-Judge runs after `before_tool_call` Hooks. An explicitly human-edited and approved command runs through Hooks again, but is not sent through Pre-Judge or LLM Judge again, preserving the existing approval lifecycle. BYPASS also preserves the existing behavior: it does not apply the YAML BLOCK rules. Such BLOCK rules are approval-stage decisions, not a global mandatory deny-list.
