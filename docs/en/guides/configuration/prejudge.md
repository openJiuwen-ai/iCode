# Structured Shell Pre-Judge

iCode can optionally inspect Bash commands using Tree-sitter and literal YAML rules before ordinary tool approval. It is **off by default** and returns only **ALLOW** or **DEFER**. ALLOW offers an automatic-approval shortcut; DEFER continues through existing approval. Pre-Judge does not deny commands. Existing Hooks, approval policy, the read-only shortcut, DAA (the existing authorization-reuse mechanism), remembered grants, and the **pre-execution** Shell Command Filter retain their existing behavior.

Place `prejudge.yaml` in iCode's trusted user configuration directory (`~/.chrys/prejudge.yaml` on macOS/Linux). Workspace files do not provide rules.

```yaml
schema_version: 1
mode: "shadow"
policy_id: example-v1
rules:
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

Each rule uses `effect: allow`, `target: command`, and exactly **one** top-level matcher from this closed set of two literal matchers. DEFER is the default when a command cannot be allowed; it is not a configured rule effect. Regex, logical operators, CLI schemas, and other matcher names are not supported:

| Matcher | Meaning | Allowed effects |
| --- | --- | --- |
| `argv_exact: [git, status]` | Full literal argument vector equals exactly the given non-empty sequence | ALLOW |
| `argv_first: [echo]` | First token of the complete literal argument vector equals the given token | **ALLOW only** |

Matchers require **lists of non-empty literal strings**, even when there is only one token. An ALLOW rule using `argv_first: [git]` would grant *all* Git subcommands with complete static arguments, including mutating ones; prefer `argv_exact` for commands with risk-bearing arguments. Unknown keys, multiple matcher fields, empty lists, and non-string values are rejected during policy loading. The removed `effect: block` and `prefix_first` are rejected rather than silently ignored; remove those rules from older configurations. The policy schema version is `1`.

### Decision safety

A structured ALLOW requires **every executable AST node** to match an ALLOW rule, complete static argv and full supported syntax coverage. Anything unmatched, malformed, dynamic, or unsupported returns DEFER. DEFER continues through existing approval, including its read-only shortcut and sensitive-data checks. For example, `rm x` without an ALLOW rule enters ordinary approval; Pre-Judge does not reject it. Existing read-only approvals such as `ls *.py`, `ls 2>/dev/null`, and `sort f` still work, including with `rules: []`. A static prefix alone never justifies ALLOW. In `echo "rm"`, `rm` is an argument rather than another executable.

Use `mode: "off"`, `mode: "shadow"`, or `mode: "enforce"`. Unquoted `mode: off` (parsed by YAML as `False`), `mode: false`, and an empty file also disable the feature without loading Tree-sitter. `shadow` logs proposed ALLOW/DEFER decisions without acting on them; `enforce` applies eligible ALLOW shortcuts. Malformed YAML **syntax** raises a path-qualified error at ordinary approval startup, even if the intended mode was `shadow`. Parseable `shadow` YAML with invalid rules or dependencies instead warns (with the file path and rule ID where applicable) and disables observation. Invalid `enforce` rules fail with the file path and rule ID. A BYPASS run does not load or validate `prejudge.yaml`; if approval mode later changes to MANUAL/AUTO, the policy is loaded then.

The **AST matchers** support only Bash / Git Bash. Unsupported shells (zsh, sh, dash, PowerShell, cmd.exe), redirections, dynamic expansions, globs, and most control-flow constructs DEFER. The unsupported-dialect warning states that structured ALLOW is inactive and that the existing read-only shortcut still applies. For example, zsh `ls` and PowerShell `Get-ChildItem` retain their existing read-only approval. Matching uses literal command words without normalizing executable paths: a rule for `rm` does not match `/bin/rm`. Opaque wrappers such as `command`, `env` and `time` also DEFER to existing approval. Pre-Judge is an approval shortcut, not a shell security boundary.

**Dependencies:** enabled configurations require the optional Tree-sitter parser packages `tree-sitter==0.25.1` and `tree-sitter-bash==0.25.1`. The packages must be installed for the running Python/platform; the normal `off` mode does not import them. The dependency lock and Linux offline-bundle verification are separate release gates.

### Approval and execution boundaries

- **Pre-Judge only offers ALLOW or DEFER.** It does not produce a rejection or a BLOCK result. Ordinary approval and execution checks can still reject a call independently.
- **BYPASS skips structured Pre-Judge entirely, including shadow logging.** Both `icode run` and `icode workflow run` use BYPASS; `prejudge.yaml` does not change their approval behavior, and a BYPASS invocation does not validate the file.
- **Human-edited approvals are not re-evaluated.** When a person edits and approves a command, the new command is not evaluated again by Pre-Judge or LLM Judge. The edited command still goes through `before_tool_call` Hooks and the configured Shell Command Filter.
- **The Shell Command Filter checks the final command after approval and before the process starts.** BYPASS or human approval does not override it. However, this filter matches literal command names only and is **not a sandbox**: a blacklist of `[rm]` does not block every way to invoke `rm` (for example, `$'rm'`, `/bin/rm`, or `env rm`).
- **DEFER does not guarantee a human dialog.** Profile overrides, remembered authorization grants, and the AUTO LLM judge may still approve and execute the tool call.

**Policy lifetime:** Approval middleware sharing an EventBus uses one trusted policy snapshot. In the TUI, this lasts for the entire iCode process: `/new`, restoring a chat, or switching agents does not reload `prejudge.yaml`. Exit and restart iCode to apply edits. Main-agent, sub-agent, and workflow middleware use the same snapshot. If iCode begins in BYPASS, the snapshot is taken only after switching to MANUAL/AUTO.

If that first switch fails to load the policy, the requested MANUAL/AUTO mode still takes effect. The error is logged, and structured ALLOW and the read-only shortcut stay disabled for that EventBus lifetime; ordinary approval decides subsequent calls. Fix the configuration or parser installation and restart iCode to retry loading. Invalid enabled policies at ordinary startup still raise a path-qualified error.

**Shadow diagnostics:** At INFO level, Shadow logs the decision, reason, policy/rule IDs, matched spans, and a one-line representation of the command (truncated to 512 characters). Command text can include secrets; restrict access to these logs and avoid passing sensitive literal tokens.

### Compatibility and limitations

DEFER delegates to the existing read-only checker; it does not reimplement that checker as a second YAML engine. Its existing capabilities and limitations are retained, including shell-specific handling. This shortcut remains separate from the `shell_filter: read_only` preset, which has different semantics. The AST layer does not certify a glob as a fully resolved `argv_exact` vector, even when the legacy shortcut independently approves the command. Compatibility tests cover representative cases; they are not a proof of safety for every possible command.
