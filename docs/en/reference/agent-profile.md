# Agent profile reference

An agent profile is a local YAML file that defines an agent's name, instructions, model, tools, approval policy, skills, memory, context compaction, and sub-agents. This page describes how YAML files are loaded and modified, along with the available fields, defaults, and constraints.

To configure an agent through the terminal user interface (TUI), see [Configure agents](../guides/configuration/agents.md). Use this page as the reference when editing YAML manually.

## User agent profile directory

User agent profiles are stored in the following directories:

| Platform | Directory |
| --- | --- |
| macOS, Linux | `~/.chrys/agents/` |
| Windows | `%APPDATA%\chrys\agents\` |

iCode loads only non-hidden files with the `.yaml` or `.yml` extension in this directory. Other files are ignored. Invalid profiles do not prevent other profiles from loading, but are skipped with a warning in the startup log.

UTF-8 is recommended. iCode also detects common legacy encodings such as GBK/GB18030 and BOM-marked UTF-16/UTF-32. Files that cannot be decoded using the detected encoding are treated as invalid, without replacing bytes with `�`. Profiles saved or rewritten by iCode use UTF-8.

iCode loads these files at startup. After manually adding, editing, or deleting a profile, restart iCode for the change to take effect.

## Profile naming and normalization

Every profile must include `name`. Because `name` is used as the filename, it must be a valid cross-platform filename: it cannot contain path separators, colons, control characters, or any of `* ? " < > |`, and cannot be `.`, `..`, or a reserved Windows device name. Leading and trailing whitespace is removed from `name` during loading.

When loading user profiles, iCode normalizes the files based on their contents:

- If the filename is not `<name>.yaml`, iCode renames it to `<name>.yaml`. This also changes the `.yml` extension to `.yaml`.
- If the profile has no `id`, iCode assigns a stable ID and rewrites the entire file. The rewrite does not preserve existing comments, field order, or formatting, and it removes unrecognized keys and fields set to their default values (except `approval`, which is always written).
- If `sub_agents.agents` references a built-in agent that has been removed from iCode, iCode deletes those entries and rewrites the file in the same way. The references are kept if you have your own profile with that name, and the change waits while any profile file in the directory fails to load.
- If another profile already occupies the target filename, the profile is not loaded. Where possible, iCode renames the conflicting file with a `.conflict` marker.

Normalization can rename or rewrite files. Back up the original files before loading them if you need to preserve them.

If a conflict occurs, inspect `<name>.yaml` and the file marked with `.conflict`, and keep the profile you need. To retain both, turn one file into a separate profile: change its `name`, remove its existing `id`, and restore the `.yaml` extension. iCode assigns the profile a new ID after a restart.

Filesystem restrictions may prevent iCode from adding the `.conflict` marker. In that case, the conflicting file retains its original name but is still not loaded.

## Override built-in agents

The original profiles for built-in agents are included with the iCode installation. When you edit and save a built-in agent through the TUI, iCode creates a profile with the same name in the [user agent profile directory](#user-agent-profile-directory). For example, editing and saving the built-in agent named `Code` creates `Code.yaml`.

The built-in agent names are `Code`, `QA`, `Explore`, and `General`. A profile manually created in the user agent profile directory with any of these `name` values also overrides the corresponding built-in agent. An override replaces the entire built-in profile; it does not inherit or merge any other fields from it. Delete the corresponding user profile and restart iCode to restore the built-in profile.

## Basic profile example

A profile must include the `name` field to identify it. Providing `display_name`, `description`, and `instructions` is also recommended to make the profile recognizable and specify the agent's behavior. The following example defines a code review agent:

```yaml
name: Reviewer
display_name: Code review
description: Check code quality and potential defects
instructions: |
  Read the relevant code and tests.
  Prioritize defects that affect users, and include file locations.
tools:
  builtins:
    - filesystem.read
    - search
```

This example omits `id`. iCode assigns a stable ID and writes it back to the file when it first loads the profile.

Save the profile as `Reviewer.yaml` in the [user agent profile directory](#user-agent-profile-directory). Run the following command to confirm that it loads:

```bash
icode agents
```

If "Code review" appears in the list, the profile has loaded. If it does not appear, check the YAML parsing or field validation warnings shown at startup, and confirm that the file is in the user agent profile directory.

## Top-level fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | String | Required | Profile name, also used as the base of the normalized filename. Sub-agent references use this value. |
| `id` | String | Assigned automatically | Stable identifier. If omitted or empty, iCode automatically assigns a 12-character hexadecimal ID: it reuses the built-in agent's ID when the names match, or generates a new ID otherwise. You can also specify a unique ID manually; quote an ID made only of digits, such as `id: "123"`, because an unquoted one is read as a number and the profile fails to load. When copying and modifying an existing profile to create a new one, remove the `id` field so iCode can assign a new ID. |
| `display_name` | String | Empty | Display name. |
| `description` | String | Empty | Describes the agent's purpose. Also used as the default tool description for a sub-agent when none is specified. |
| `sub_agent_only` | Boolean | `false` | If `true`, the agent cannot be selected as the main agent and can only be called by other agents. Forced to `true` for external ACP agents. |
| `instructions` | String or list of strings | Empty | Main behavioral instructions for agents of the built-in type. A list is joined into one text, one item per line. External ACP profiles ignore this field. |
| `model` | Object | `{}` | Binds a model profile. See [model](#model). |
| `tools` | Object | `{}` | Configures built-in tools, MCP, and shell filtering. See [tools](#tools). |
| `approval` | Object | See below | Configures which tool calls require approval. See [approval](#approval). |
| `skills` | Object | See below | Configures skill sources and inline skills. See [skills](#skills). |
| `memory` | Object | `{}` | Configures reference files to load automatically. See [memory](#memory). |
| `compaction` | Object | See below | Configures context compaction. See [compaction](#compaction). |
| `sub_agents` | Object | See below | Configures callable sub-agents. See [sub_agents](#sub_agents). |
| `acp` | Object | Disabled | The presence of this field makes the profile an external Agent Client Protocol (ACP) agent. See [acp](#acp). An empty object (`acp: {}`) or an empty value (`acp:` or `acp: null`) also enables this type, but the agent cannot start without `command`. |

## model

Use `model` to bind an agent to a specific model profile.

```yaml
model:
  profile_id: 0123456789ab
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `profile_id` | String | Empty | ID of the model profile to bind. |

If `profile_id` is omitted, empty, or not found among the loaded model profiles, the main agent uses the model profile currently active in the session, and a sub-agent inherits the model profile actually used by its parent. iCode also logs a warning when the ID cannot be found.

Run `icode models` to find stable model profile IDs in the `ID` column. For model configuration instructions, see [Configure models](../guides/configuration/models.md).

## tools

```yaml
tools:
  builtins:
    - filesystem.read
    - search
    - shell
  mcp: []
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `builtins` | List of strings | `[]` | Built-in tool kinds to enable. For available values, see the rows marked "Yes" in the "Built-in" column of the [kinds and names overview](./tool-kinds-and-names.md#overview-of-kinds-and-names). No built-in agent includes `web_search` or `web_fetch`. |
| `mcp` | List of objects | `[]` | Model Context Protocol (MCP) server configurations. See [tools.mcp](#toolsmcp). |
| `shell_filter` | String or object | Not set (no shell filtering) | Restricts the commands that shell tools can execute. See [tools.shell_filter](#toolsshell_filter). |
| `web_search` | Object | Not set (uses user settings) | Mode and search providers for the `web_search` tool. See [tools.web_search](#toolsweb_search). |
| `web_fetch` | Object | Not set (uses user settings) | Mode and limits for the `web_fetch` tool. See [tools.web_fetch](#toolsweb_fetch). |

### tools.mcp

Each list entry configures one server. `name` and `transport` are required. Server names must be unique within an agent profile, ignoring case. HTTP connections require `url`; STDIO connections require `command`.

STDIO example:

```yaml
tools:
  mcp:
    - name: local-server
      transport: stdio
      command: python
      args: ["-m", "example_mcp_server"]
      env:
        API_KEY: "{{MCP_API_KEY}}"
        LOG_LEVEL: info
```

HTTP example:

```yaml
tools:
  mcp:
    - name: remote-server
      transport: http
      url: https://example.com/mcp
      headers:
        Authorization: "Bearer {{MCP_API_TOKEN}}"
```

Both `env` and `headers` are key-value mappings: enter the variable or header name on the left and its value on the right. You can specify values directly or use `{{ENV_VAR}}` to reference an environment variable set before starting iCode. If a variable referenced in `env` is missing or empty, the server connection fails. For `headers`, variables are resolved and this rule applies only when `resolve_header_templates` is `true`.

| Field | Type | Default | Applies to | Description |
| --- | --- | --- | --- | --- |
| `name` | String | Required | All | Server name within this profile. |
| `transport` | `http` or `stdio` | Required | All | Connection transport. |
| `enabled` | Boolean | `true` | All | Whether to connect to the server and provide its capabilities. |
| `description` | String | Empty | All | A note to help users identify the configuration. It is not sent to the agent as a server description. |
| `tool_name_prefix` | String | Empty | All | Adds `<prefix>_` to MCP tool names. The prefix can contain only letters, digits, underscores, and hyphens. It must begin and end with a letter or digit and cannot have leading or trailing whitespace. The full tool name with the prefix cannot exceed 64 characters. |
| `allowed_tools` | List of strings or `null` | `null` | All | `null` allows all server tools; `[]` allows none; a list allows only the specified tools. |
| `use_progressive_disclosure` | Boolean | `false` | All | If `true`, loads allowed tools on demand. Cannot be `true` when `allowed_tools` is `[]`. |
| `always_load` | List of strings | `[]` | All | Tools initially visible when loading on demand. If `allowed_tools` is a list, these names must also appear in that list. Must be empty when loading on demand is disabled. |
| `load_prompts` | Boolean | `true` | All | Whether to load the server's prompt templates as tools. |
| `expose_instructions` | Boolean | `true` | All | Whether to add the server's usage instructions to the model context. |
| `timeout` | Positive integer or `null` | `null` | All | Timeout in seconds for a single MCP request, including tool calls. `null` uses the default of 30 seconds. Must be greater than `0`. |
| `max_tool_result_tokens` | Integer or `null` | `null` | All | Text result limit for a single remote tool call. `null` uses the default limit of `8000` tokens; `0` disables only this limit; a positive integer must be at least `100`. Other values cause the entire agent profile to be skipped. |
| `command` | String | Empty | STDIO | Executable name or path used to start the server. Do not append arguments to this YAML field. |
| `args` | List of strings | `[]` | STDIO | Arguments passed to the server process. |
| `env` | String-valued object | `{}` | STDIO | Environment variables passed to the server process. Ignored for HTTP configurations. |
| `encoding` | String or `null` | `null` | STDIO | Encoding for the server's standard input and output. Usually left empty. |
| `url` | String | Empty | HTTP | Server URL beginning with `http://` or `https://`. |
| `headers` | String-valued object | `{}` | HTTP | HTTP request headers. |
| `resolve_header_templates` | Boolean | `true` | HTTP | Whether to resolve `{{ENV_VAR}}` in header values. If `false`, sends values literally. |
| `verify_ssl` | Boolean | `true` | HTTP | Whether to verify HTTPS certificates. Disabling this reduces connection security. |
| `bypass_proxy` | Boolean | `false` | HTTP | Whether to bypass HTTP/HTTPS proxies configured through environment variables and connect directly to the server. |
| `terminate_on_close` | Boolean or `null` | `null` | HTTP | Whether to request termination of the remote session when closing the connection. `null` uses the default of `true`. |

Use the server's original tool names in `allowed_tools` and `always_load`. `tool_name_prefix` changes the name the agent uses to call a tool; `approval.overrides` uses that same name. See [MCP tool names](./tool-kinds-and-names.md#mcp-tool-names) for naming rules. If a name you list could mean two tools — for example, with prefix `gh`, a server tool `search` becomes `gh_search`, the original name of another tool — the agent fails to load. The error names that entry and, for each tool it matches, a name that selects only that tool: here `search` for the server's `search` and `gh_gh_search` for the server's `gh_search`. Replace the entry with the names of only the tools you want the agent to use. If the error says no name selects a tool by itself, choose a different `tool_name_prefix`.

When loading on demand is enabled, iCode also adds control tools for listing, loading, and unloading MCP tools. Their naming rules are described in [MCP tool names](./tool-kinds-and-names.md#mcp-tool-names). In this case, `tool_name_prefix` cannot exceed 49 characters.

For configuration instructions, connection tests, and verification of tool calls, see [Connect MCP servers](../guides/extensions/mcp.md).

### tools.shell_filter

`shell_filter` applies only to tools in the `shell` kind. If this field is omitted or set to `unrestricted`, iCode does not filter shell commands.

Shell tool calls go through [approval](#approval) first. Once approved, `shell_filter` checks the actual command. The command runs only after both checks pass. If the filter blocks a command, iCode returns a tool error indicating that the command was blocked, without starting a shell process.

You can use a preset or define your own allowed or blocked commands. To use a preset, specify its name directly or select it with `preset` in an object. These two forms are equivalent:

```yaml
tools:
  shell_filter: read_only
```

```yaml
tools:
  shell_filter:
    preset: read_only
```

Available presets:

| Value | Behavior |
| --- | --- |
| `read_only` | Allows only commands in the built-in command list and blocks unquoted redirection and command substitution. Examples include `ls`, `cat`, `rg`, `git`, `python`, `curl`, and PowerShell's `Get-Content`. It does not inspect arguments, subcommands, or script contents, so it does not guarantee read-only execution and is not a security sandbox. |
| `unrestricted` | Does not filter shell commands. Usually, you can simply omit `shell_filter`. |

Only the names in this table enable presets. Unrecognized presets do not take effect: the scalar form performs no shell filtering; for the object form, iCode attempts to use the custom rules in the same object. If `commands` is empty, no shell filtering occurs in that case either.

To specify your own allowed or blocked commands, use the object form. For example:

```yaml
tools:
  shell_filter:
    mode: whitelist
    commands: [git, rg]
    allow_redirections: false
    allow_subshells: false
```

The object form supports these fields:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `preset` | `read_only`, `unrestricted`, or empty | Empty | Selects a preset. A recognized preset takes precedence over all custom filtering fields. |
| `mode` | `whitelist` or `blacklist` | `whitelist` | `whitelist` allows only commands in `commands`; `blacklist` blocks commands in `commands` and allows others. |
| `commands` | List of strings | `[]` | Command names to allow in `whitelist` mode or block in `blacklist` mode. Must contain at least one command for custom filtering to take effect. An empty list means no shell filtering. |
| `allow_redirections` | Boolean | `true` | Whether to allow unquoted `>`, `>>`, and `<`. |
| `allow_subshells` | Boolean | `true` | Whether to allow unquoted `$()` and backtick command substitution. |

In custom rules, use executable names in `commands`, such as `[git, rg]`. Names are case-sensitive; when using an absolute path, specify the full path. For commands joined by `|`, `&&`, `||`, `;`, or similar operators, iCode checks each segment separately. Execution is allowed only if every command name satisfies the rules. The example above allows `rg foo .`; it blocks `rg foo . | less` because `less`, on the right side of the pipe, is not in the allowlist.

### tools.web_search

`web_search` configures the `web_search` tool and takes effect only when `builtins` includes `web_search`. Omitted fields use the [web tool user settings](./settings.md#web-tools). For setup steps and how providers behave, see [Configure web tools](../guides/configuration/web-tools.md).

```yaml
tools:
  builtins: [filesystem.read, search, web_search]
  web_search:
    mode: auto
    fallback_chain: [primary, backup]
    num_results: 8
    timeout_seconds: 30
    providers:
      primary:
        type: tavily
        api_key_env: TAVILY_API_KEY
      backup:
        type: duckduckgo_html
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `mode` | `auto`, `provider`, `off`, or Boolean | The `tools.web_search.mode` user setting (`auto`) | `auto` uses Exa's public endpoint when `provider`, `fallback_chain`, and `providers` are all omitted; with a `providers` map it requires `fallback_chain`. `provider` uses exactly one provider, named in `provider`, and does not allow `fallback_chain`. `off` turns web search off. `true` means `auto`; `false` means `off`. Any other value causes the entire agent profile to be skipped. |
| `provider` | String | Not set | ID of the provider in `providers` to use in `provider` mode. Cannot be combined with `fallback_chain`. |
| `fallback_chain` | List of strings | Not set | IDs of providers in `providers` to try in order in `auto` mode. At most 5, without duplicates. |
| `providers` | Object | Not set | Provider configurations keyed by an ID of your choice. An ID starts with a lowercase letter and contains only lowercase letters, digits, `_`, and `-`, up to 64 characters. |
| `num_results` | Integer | The `tools.web_search.num_results` user setting (`8`) | Results a search returns when the agent does not ask for a number, from `1` to `20`. |
| `timeout_seconds` | Integer | The `tools.web_search.timeout_seconds` user setting (`30`) | Deadline in seconds for a whole search call, from `1` to `120`. |

An out-of-range number or a malformed `providers` entry also causes the profile to be skipped. A combination that does not fit the mode, such as a `provider` mode without `provider` or a selected provider missing from `providers`, instead leaves the agent without its web tools, and iCode shows a warning with the reason.

Each entry in `providers` supports these fields:

| Field | Applies to | Description |
| --- | --- | --- |
| `type` | All | Required. `exa_mcp`, `bing_html`, or `duckduckgo_html` for services that need no key; `tavily`, `brave`, or `exa` for search APIs with a key; `custom_http` for your own endpoint. |
| `api_key_env` | `tavily`, `brave`, `exa` | Required. Name of the environment variable that holds the API key. These types accept no other field. |
| `preset` | `custom_http` | `searxng` or `serpapi`. Fills in the request and response mapping for that service; `serpapi` also sets `endpoint` and `auth`. Fields you set yourself take precedence. |
| `endpoint` | `custom_http` | URL of the search endpoint. It must also be granted in the `tools.web_search.custom_endpoints` user setting, together with every environment variable the provider reads. |
| `method` | `custom_http` | `GET` (default) or `POST`. |
| `auth` | `custom_http` | Object with `location` (`header`, `query`, or `none`, the default), `name` (the header or query parameter name), `key_env` (the environment variable holding the key), and an optional `prefix` such as `"Bearer "`. For `header`, `name` must be `Authorization`, `X-API-Key`, or `X-Subscription-Token`. |
| `headers` | `custom_http` | Only `Accept` and `Content-Type`; the content type must be `application/json`. Send keys through `auth`, not headers. |
| `request` | `custom_http` | `query_params` and, for `POST`, `json`. A value written as `{$input: query}`, `{$input: limit}`, `{$input: allowed_domains}`, or `{$input: blocked_domains}` is replaced by that search input, and `{$env: NAME}` by an environment variable. |
| `response` | `custom_http` | JSON Pointers into the response: `results_pointer` and `url_pointer` (required unless a preset provides them), plus optional `title_pointer` and `snippet_pointer`. |

`exa_mcp`, `bing_html`, and `duckduckgo_html` accept only `type`.

### tools.web_fetch

`web_fetch` configures the `web_fetch` tool and takes effect only when `builtins` includes `web_fetch`. Omitted fields use the [web tool user settings](./settings.md#web-tools).

```yaml
tools:
  builtins: [filesystem.read, search, web_search, web_fetch]
  web_fetch:
    mode: on
    max_tokens: 16000
    timeout_seconds: 60
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `mode` | `on`, `off`, or Boolean | The `tools.web_fetch.mode` user setting (`on`) | `on` makes `web_fetch` available; `off` leaves it out. `true` means `on`; `false` means `off`. |
| `max_tokens` | Integer | The `tools.web_fetch.max_tokens` user setting (`16000`) | Most tokens a fetched page returns, from `1` to `64000`. The agent can ask for less, but not more. |
| `timeout_seconds` | Integer | The `tools.web_fetch.timeout_seconds` user setting (`60`) | Deadline in seconds for a whole fetch call, from `1` to `120`. |

Network access (the proxy, origin lists, and custom endpoint grants) can be set only in user settings, not in agent profiles. See [Web tools](./settings.md#web-tools) in the settings reference.

## approval

`approval` sets the **initial approval level** for tool calls. This differs from the session's **approval mode**: the approval level determines whether a tool call enters the approval process, while the approval mode determines how it is handled once it enters that process.

| Field | Type | Description |
| --- | --- | --- |
| `default` | `auto`, `require`, or `skip` | Approval level used when no override rule matches. |
| `overrides` | Object | Approval levels set by tool kind or tool name. |

If `approval` is not configured, iCode uses these defaults:

```yaml
approval:
  default: auto
  overrides:
    shell: require
    filesystem.write: require
    todo: skip
```

### Approval levels

Available approval levels:

| Value | Behavior |
| --- | --- |
| `require` | The tool call initially requires approval. |
| `auto`, `skip` | The tool call initially does not require approval. These values behave identically in the current version. |

A tool call is processed as follows:

1. iCode determines the initial approval level from `default` and `overrides`.
2. iCode adjusts the result using safety rules:

   * Shell commands and file reads or writes that access sensitive targets are changed to require approval.
   * Operations such as known-safe read-only shell commands and writes to non-sensitive files in the working directory's Git repository can run directly.
3. If the call still requires approval, the current session's approval mode determines how to handle it: user confirmation, a decision by the approval judge model, or bypass approval. For details, see [Configure approval modes](../guides/configuration/approval.md).

### Override rules

For the distinction between tool kinds and names, available values, and dynamic naming rules, see [Tool kinds and names](./tool-kinds-and-names.md).

Keys in `overrides` can be:

* A tool kind, such as `shell`.
* A tool name, such as `write_file`.
* A tool kind and tool name together, such as `filesystem.write.write_file`.

A key equal to a tool kind always refers to that kind. To target a tool whose name matches a kind, such as an MCP tool named `search`, use the kind and tool name together, for example `mcp.search`. `web_search` and `web_fetch` are the exception: these keys also match any tool with that name, such as an MCP tool named `web_search`, so an override written for such a tool keeps applying.

When multiple rules match a tool call, iCode selects one in this order of precedence:

1. Tool kind and tool name.
2. Tool name.
3. Tool kind.
4. `default`.

The order of rules in YAML does not affect matching. For example:

```yaml
approval:
  default: auto
  overrides:
    filesystem.write: require
    filesystem.write.write_file: auto
```

In this example:

* `write_file` matches `filesystem.write.write_file` and uses the approval level `auto`.
* Other file-writing tools match `filesystem.write` and use the approval level `require`.
* Other tools use the `default` value of `auto`.

If `overrides` is omitted, iCode uses the default overrides: `shell: require`, `filesystem.write: require`, and `todo: skip`. Explicitly setting `overrides: {}` clears these defaults, so tools that match no other rule use `default`.

Skill scripts are an exception: `run_skill_script` requires approval by default. To change this behavior, explicitly set an approval level for `skill`, `run_skill_script`, or `skill.run_skill_script` in `overrides`.

Web tools are also an exception: tools of the `web_search` and `web_fetch` kinds require approval by default, even when `default` is `auto`. This rule applies after the rules in `overrides` and before `default`. To change it, explicitly set an approval level for the kind or tool name in `overrides`, for example `web_search: auto`.

## skills

```yaml
skills:
  paths:
    - ./team-skills
  script_timeout: 300
  script_extensions: [.py, .sh, .ps1]
  auto_load_user_agents_skills: true
  auto_load_cwd_agents_skills: true
  inline:
    - name: release-notes
      description: Draft user-facing release notes from commits
      instructions: |
        Read the changes and group them into additions, fixes, and compatibility changes.
      resources:
        - name: style-guide
          description: Release note writing guidelines
          content: |
            Use short headings and describe only changes users can observe.
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `paths` | List of strings | `[]` | Additional skill search roots. iCode searches each directory and up to two levels of subdirectories for `SKILL.md`. Once found, it does not search inside that skill directory. Relative paths are resolved against the current [working directory](../guides/daily-use/workspaces.md). iCode expands a leading `~` to the current user's home directory. |
| `inline` | List of objects | `[]` | Skills written directly in the profile. See below. |
| `script_timeout` | Positive integer | `300` | Maximum runtime for a skill script, in seconds. |
| `script_extensions` | List of strings | `[.py, .sh, .ps1]` | Extensions allowed for skill scripts. The required interpreters must already be installed. |
| `auto_load_user_agents_skills` | Boolean | `true` | Whether to load the user-level shared Agent Skills directory. |
| `auto_load_cwd_agents_skills` | Boolean | `true` | Whether to load `.agents/skills` in the current working directory; “Load project skills” (`project.skills_enabled`) must also be on in settings. Reloaded when switching working directories. |

The iCode user skills directory is always loaded. The user-level shared Agent Skills directory is loaded by default; the current working directory's skills directory loads once “Load project skills” is on in settings. Each can be disabled with its corresponding `auto_load_*` field. For user-level directory paths, see [Skill installation locations](../guides/extensions/skills.md#skill-installation-locations).

When multiple sources contain skills with the same name, precedence from highest to lowest is: earlier directories in `paths`, the iCode user skills directory, the user-level shared Agent Skills directory, the current working directory's skills directory, and earlier definitions in `inline`. If one search root contains multiple skills with the same name, discovery order is unspecified. Keep only one to ensure that the intended version loads.

> **Note**: Skill scripts run as local processes without security sandbox isolation. Before adding a directory to `paths` or enabling an automatically loaded source, inspect its `SKILL.md`, scripts, and other relevant files. Load only trusted skills. Allowing a script extension does not install its interpreter.

### skills.inline

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | String | Required | 1-64 characters, using lowercase letters, digits, and hyphens. Hyphens cannot be consecutive or appear at the start or end. |
| `description` | String | Required | Non-empty, up to 1024 characters. Helps the agent decide when to load the skill. |
| `instructions` | String or list of strings | Empty | Instructions provided to the agent when the skill is loaded. A list is joined into one text, one item per line. |
| `resources` | List of objects | `[]` | Inline text resources loaded with the skill. See the following table. |

| `resources` field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | String | Required | Resource identifier, not a file path. Avoid duplicate names within a skill that differ only in case. |
| `description` | String | Empty | Describes the resource's purpose. |
| `content` | String | Empty | Text resource content. Resources with empty content are ignored. |

Inline skills do not support file resources or scripts. To reference files or run scripts, create a directory skill containing `SKILL.md` and load it through `skills.paths` or one of the automatically loaded skill directories described above.

For installation steps, directory conventions, loading verification, and usage, see [Install and use skills](../guides/extensions/skills.md).

## memory

```yaml
memory:
  files:
    - AGENTS.md
    - docs/project-context.md
  folders:
    - docs/reference
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `files` | List of strings | `[]` | Individual `.md` or `.txt` files. |
| `folders` | List of strings | `[]` | Directories to scan for `.md` and `.txt` files. |

Paths can be absolute or relative to the current working directory; relative paths cannot point outside the working directory. Memory is loaded by the main agent and by workflow agent nodes; sub-agents load neither their own memory nor their parent's memory. For directory scan depth, file count, and total token limits, see [Configure memory](../guides/configuration/memory.md).

## compaction

`compaction` configures the continuation information (Last Words) that iCode generates when compacting the current task. These settings do not affect compaction summaries of earlier tool results or completed conversation turns.

```yaml
compaction:
  last_words_template: |
    Prioritize reproduction steps, key logs, and hypotheses that still need verification.
  last_words_max_output_tokens: 20000
  phase4_side_call_token_budget: -1
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `last_words_template` | String | Empty | Additional instructions appended to iCode's fixed Last Words requirements, emphasizing information to preserve from the current task. Does not replace the fixed format. |
| `last_words_max_output_tokens` | Integer | `20000` | Maximum output tokens for Last Words. At runtime, this cannot exceed the model profile's output limit. |
| `phase4_side_call_token_budget` | Integer or `null` | `-1` | Total estimated input token budget for additional model requests that generate Last Words, from the user's message submission until the agent completes its reply. `-1` or `null` means unlimited; `0` disables Last Words requests; a positive integer sets the cumulative limit. Cannot be less than `-1`. |

`last_words_max_output_tokens` limits output, while `phase4_side_call_token_budget` limits cumulative input. The latter is mainly intended for strict control of additional model usage and can usually be left at its default. If the budget is insufficient, iCode preserves the current task's content. If the context is still too large, the task may be unable to continue.

`compaction` does not provide a setting for the compaction trigger threshold. iCode automatically determines when to compact based on the current model's context window, maximum output tokens, and safety margin.

For the compaction process and visible behavior, see [Configure context compaction](../guides/configuration/compaction.md).

## sub_agents

```yaml
sub_agents:
  max_total_concurrency: 3
  agents:
    - profile: Explore
      tool_name: explore
      tool_description: Search and analyze relevant code
      max_concurrency: 3
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `max_total_concurrency` | Positive integer | `3` | Maximum total number of concurrent sub-agent calls. |
| `agents` | List of objects | `[]` | Profiles the current agent can call. |
| `agents[].profile` | String | Required | `name` of a loaded agent. Cannot reference itself or reference the same profile more than once. Entries without `profile` are ignored. |
| `agents[].tool_name` | String | Sub-agent's `name` | Tool name exposed to the parent agent. An explicitly set value must begin with a letter or underscore, followed only by letters, digits, or underscores. If omitted, the sub-agent's `name` is used as is. The effective name must be unique within the parent agent. |
| `agents[].tool_description` | String | Sub-agent's `description` | Helps the parent agent decide when to call this sub-agent. |
| `agents[].max_concurrency` | Positive integer | `3` | Maximum concurrent calls to this sub-agent. |

The **Agent Configuration** window in the TUI checks the concurrency values and the `tool_name` format when saving; iCode does not check them when loading a YAML file you edited by hand. A concurrency value of `0` or less makes every call to the affected sub-agents fail.

Tool calls within non-ACP sub-agents use the parent agent's `approval` configuration. The sub-agent's own `approval` configuration does not affect these calls.

A non-ACP sub-agent returns its final reply to the parent agent, without the text it wrote between tool calls. If the final reply has no text, it returns all the text it wrote, in order. An ACP sub-agent returns what [`result_mode`](#acp) selects.

For configuration instructions and call verification, see [Configure agents](../guides/configuration/agents.md#configure-sub-agents-for-an-agent).

## acp

When `acp` appears at the top level, the profile becomes an external ACP agent and is forced to be available only as a sub-agent. The external process determines its task instructions, model, and tools.

External ACP agents do not use `instructions`, `model.profile_id`, `tools`, `skills`, `memory`, `compaction`, `approval`, or `sub_agents` from the current profile. These settings do not affect the external agent's behavior.

```yaml
name: ExternalReviewer
display_name: External review agent
description: Call an external review program through ACP
acp:
  command: example-agent
  args: [acp]
  env:
    API_KEY: "{{EXAMPLE_API_KEY}}"
  result_mode: last_segment
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `command` | String | Empty | Executable name or path. Required to start the external agent. An empty value can be loaded but cannot be started. Cannot contain NUL. Do not append arguments to this field. |
| `args` | List of strings | `[]` | Arguments passed individually to the process. Cannot contain NUL. |
| `env` | String-valued object | `{}` | Environment variables passed to the process. Names must follow environment variable naming rules and be unique ignoring case. `CHRYS_ACP_SUBAGENT_DEPTH` is reserved for iCode. |
| `cwd` | String | Empty | The external agent's working directory. If empty, uses the current iCode session's working directory. Relative paths are resolved against that session directory. |
| `allow_external_cwd` | Boolean | `false` | Whether to allow the external agent to run outside the current session's working directory and its subdirectories. Defaults to `false`: a `cwd` outside the allowed directory scope causes an error and prevents startup; iCode does not fall back to the default directory. This option only controls startup directory validation, not the external agent's file access permissions. |
| `session_mode` | String | Empty | Session mode ID to request from the external agent. |
| `model_id` | String | Empty | Model ID to request from the external agent on a best-effort basis. |
| `config_options` | Object | `{}` | External agent configuration options. Keys must be non-empty strings; values can only be strings or booleans. |
| `best_effort_options` | Boolean | `false` | Whether to ignore session modes and configuration options that the external agent does not support and continue connecting. |
| `result_mode` | `last_segment` or `transcript` | `last_segment` | Returns the last message segment or the full transcript to the parent agent. |
| `handshake_timeout_seconds` | Number | `30.0` | Timeout in seconds for starting, initializing, and opening a session. Must be greater than `0`. Invalid YAML values fall back to the default with a warning. |
| `idle_timeout_seconds` | Number | `600.0` | Timeout in seconds for inactivity during a call. `0` means no timeout; cannot be negative. Invalid YAML values fall back to the default with a warning. |

**Directory scope for `allow_external_cwd`**: If an ACP client creates the iCode session and supplies additional working directories, the external agent can run in those directories and their subdirectories even when `allow_external_cwd` is `false`.

`command`, `args`, values in `env`, and `cwd` can use `{{ENV_VAR}}` to reference environment variables from the iCode process. Missing or empty variables cause startup to fail. For connection tests, working directory security boundaries, and how configuration options are applied, see [Configure external ACP agents](../guides/extensions/external-acp-agents.md).
