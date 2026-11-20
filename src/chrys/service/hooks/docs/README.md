# Chrys Hooks — Documentation

User-defined scripts that fire at well-defined points in the agent
lifecycle. Configured per-user in `<config_dir>/hooks/hooks.yaml`.

| Doc | For | What's in it |
|---|---|---|
| [design.md](design.md) | Understanding the system | Events, modes, delivery, drains, matching, decision aggregation, failure handling, files on disk. |
| [configuration.md](configuration.md) | Writing `hooks.yaml` | Full reference for every field plus copy-paste examples. |
| [authoring.md](authoring.md) | Writing the hook scripts | Process contract, payload shapes per event, decision JSON, Bash/Python examples, testing tips. |
| [workdir-hooks.md](workdir-hooks.md) | Adding working directory hooks | Working directory discovery, layered merge with the global config, cross-source short-circuit, security model, test plan. |

If you want to skim one thing first, read [design.md](design.md). To add
working directory hooks, read [workdir-hooks.md](workdir-hooks.md).
