# Install and use skills

A skill is a reusable set of instructions that can include reference materials and scripts to extend an agent's ability to handle specific tasks. This guide explains how to install skills for agents of the "Built-in" type, confirm that they have loaded, and use them. Support for skills and their configuration in external ACP agents is determined by the external agent.

## Skill installation locations

Choose an installation location based on where the skill should be available and whether it needs to be shared:

| Location | When to use it | How iCode loads it |
| --- | --- | --- |
| iCode user skills directory | Make skills available to all agents of the "Built-in" type in iCode | Always loaded automatically |
| Agent Skills shared directory | Share skills between iCode and other tools that support Agent Skills | Loaded automatically by default; can be disabled per agent |
| `<working-directory>/.agents/skills` | Make skills available only in the current working directory | Loaded only after you turn on “Load project skills”; can also be disabled per agent |
| Custom directory | Load skills from a specific location | Must be added manually to the corresponding agent's configuration |

Here, `<working-directory>` is the current working directory. Skills in a working directory come with the repository, so iCode does not load them until you turn on “Load project skills” under “Settings → Security → Project trust” (`project.skills_enabled`). When the working directory has skills that are not loaded, iCode shows a notice. When you switch working directories, iCode reloads the skills from the new working directory.

The two user-level skills directories have the following paths on each platform:

| Directory | macOS and Linux | Windows |
| --- | --- | --- |
| iCode user skills directory | `~/.chrys/skills` | `%APPDATA%\chrys\skills` |
| Agent Skills shared directory | `~/.agents/skills` | `%USERPROFILE%\.agents\skills` |

For the first three installation locations, copy or move the skill directory to the corresponding location. For a custom directory, there is no need to move the skill; add its directory to the corresponding agent's configuration.

iCode searches for `SKILL.md` files in configured skills directories and up to two levels of subdirectories. Once it finds a `SKILL.md`, it recognizes the containing directory as a skill directory and stops searching for nested skills within it.

## Install and enable skills

The iCode user skills directory is always loaded automatically. The Agent Skills shared directory is also loaded automatically by default. The current working directory's skills directory loads once “Load project skills” is on. If a source has been disabled in the agent's configuration, enable it again. For a custom directory, add it to the corresponding agent's configuration.

To define an inline skill without files directly in an agent's YAML, see [Agent profile reference](../../reference/agent-profile.md#skillsinline).

To check or adjust an agent's skill configuration:

1. Enter `/agents skills` in the input field to open the current agent's "Skills" tab. To configure another agent, select it in the list on the left.
2. Confirm that the source corresponding to the skill's location is enabled:

   * "Load skills from user folder (if present)" corresponds to the Agent Skills shared directory.
   * "Load skills from working folder (if present)" corresponds to `<working-directory>/.agents/skills`. It takes effect only while “Load project skills” is on in “Settings → Security → Project trust”.

3. If the skill is in another directory, click "+ Add" and enter the directory containing the skill:

   * For a path relative to the working directory, select "Workspace relative".
   * For an absolute path, clear "Workspace relative".

4. If the skill includes scripts, confirm that their extensions are listed in "Allowed Script Extensions". The defaults are `.py`, `.sh`, and `.ps1`; other available extensions include `.ts`, `.js`, and `.rb`. The interpreters required to run the scripts must already be installed.
5. To adjust the maximum runtime for skill scripts, enter a positive integer in "Script Execution Timeout (seconds)".
6. Click "Save".

> **Note**: Skill scripts run as local processes without sandbox isolation. Before installing a third-party skill, inspect its `SKILL.md`, scripts, and other related files, and install skills only from trusted sources. iCode runs skill scripts through the `run_skill_script` tool. Whether this tool requires confirmation depends on the agent's approval rules and the [current approval mode](../configuration/approval.md).

When a skill script prints more than 32 MiB to standard output or standard error, iCode keeps only the beginning and end of that output; the middle is not saved, and the result tells the agent how much was left out.

## Confirm that skills have loaded

After you save the agent's configuration and return to the session, iCode reloads its skills. If you add skill directories or change their files directly on disk, iCode does not monitor those changes in real time. The next time you send a message, iCode rescans the current agent's enabled skills directories before processing the message.

Use either of the following methods to confirm that a skill has loaded:

* Enter `/` in the input field, then start typing the skill name (for example, `/release-`). The autocomplete list filters matches as you type. If the skill name appears in the "Loaded Skills" group, it has loaded.
* Enter `/runtime`, or click the skill count at the right end of the status bar above the input field, to open "Runtime Details". Check the skill name and source in the "Skills" tab.

If the skill does not appear, check the following in order:

* For the Agent Skills shared directory or working directory skills directory, confirm that the corresponding option is enabled (the working directory skills directory also needs “Load project skills”). For a custom directory, confirm that it has been added to the current agent.
* Confirm that the skill directory is at the selected location or within at most two levels of subdirectories.
* Confirm that the skill meets the [basic requirements for creating skills](#basic-requirements-for-creating-skills).

## Use skills

Skills usually do not need to be invoked manually. When a task matches a skill's description, the agent automatically loads its full instructions and follows them to complete the task. For example:

```text
Draft release notes based on the changes in the current repository since the previous release.
```

You can also explicitly invoke a loaded skill by starting a message with `/skill-name`. For example:

```text
/release-notes Draft release notes based on the changes in the current repository since the previous release.
```

If the skill name matches a built-in iCode slash command, you cannot invoke it explicitly with `/skill-name`. In that case, name the skill directly in your message, for example:

```text
Use the release-notes skill to draft release notes based on the changes in the current repository since the previous release.
```

The first time the skill loads in the current session, a `load_skill` tool call card appears with the skill name `release-notes`. The agent then follows the skill's instructions to complete the task.

## Priority for skills with the same name

Skills loaded from the [installation locations](#skill-installation-locations) listed earlier are directory-based skills, each containing a `SKILL.md`. iCode also supports inline skills defined directly in an agent profile. These do not require a skill directory or `SKILL.md`.

If multiple skills have the same name, iCode loads only the one with the highest priority. Priority, from highest to lowest, is:

1. Directories added manually in the agent's "Skills" tab, with those higher in the list taking priority (new directories are added at the top).
2. The iCode user skills directory.
3. The Agent Skills shared directory.
4. Skills in the current working directory.
5. Inline skills in the agent profile, in their definition order.

If multiple skills with the same name exist within one installation location or custom directory, their discovery order is unspecified. To ensure that a particular version loads, keep only that version.

## Appendix

### Basic requirements for creating skills

Each skill uses its own directory with a `SKILL.md` at the directory root. The following is a minimal skill that iCode can load:

```markdown
---
name: release-notes
description: Draft user-facing release notes from commit history
---

# Draft release notes

1. Read the commit history since the previous release.
2. Group changes into additions, fixes, and compatibility changes.
3. Include only changes observable to users.
```

#### Write `SKILL.md`

UTF-8 is recommended for `SKILL.md`. iCode also detects common legacy encodings such as GBK/GB18030 and BOM-marked UTF-16/UTF-32. The file starts with a YAML configuration block enclosed by `---`, also called frontmatter, followed by instructions for the agent. The frontmatter should be a valid YAML mapping; if it is not valid YAML, iCode tries to read it line by line and logs a warning. iCode validates the file as follows:

| Content | Loading requirements |
| --- | --- |
| frontmatter | Required; the top-level content must be a YAML mapping, without anchors (`&`) or aliases (`*`), at most 16,384 characters long and nested at most 64 levels deep |
| `name` | Required; must exactly match the skill directory name; 1-64 characters long; may contain only lowercase letters, digits, and hyphens (`-`). Uppercase letters, underscores (`_`), and other characters are not allowed. Must not start or end with a hyphen or contain consecutive hyphens |
| `description` | Required; text of at most 1,024 characters. Describe both the skill's purpose and when to use it to help the agent decide when to load it |
| `compatibility` | Optional; text of at most 500 characters |
| `license`, `allowed-tools` | Optional; iCode reads their text without further validation |
| `metadata` | Optional; must be a YAML mapping whose values are plain text or numbers, otherwise iCode does not retain this field |
| Body | iCode imposes no additional length or structure restrictions |

Use valid YAML for the frontmatter to ensure that the skill loads reliably. iCode does not load a skill if its file cannot be decoded using the detected encoding, if its frontmatter, `name`, or `description` is missing, or if field values fail the validation rules above. An optional field given as a list or mapping is ignored.

The optional fields serve the following purposes:

* `compatibility`: Describes the runtime environment the skill depends on, such as supported products, required system packages, or network access requirements.
* `license`: Declares the skill's license. This can be a license name or the name of a license file in the skill directory.
* `allowed-tools`: Declares tools that may be approved in advance under the Agent Skills specification. This field is still experimental. iCode currently only reads and retains its value; it does not automatically grant tool permissions based on it.

iCode skill directories and the `SKILL.md` format follow the common Agent Skills specification. For details on these fields and the complete skill specification, see the [official Agent Skills specification](https://agentskills.io/specification).

#### Add scripts and resources

A skill can include scripts, reference materials, and other text resources as needed.

For example:

```text
release-notes/
├── SKILL.md
├── scripts/
│   └── collect.py
├── references/
│   └── style-guide.md
└── assets/
    └── template.html
```

iCode recognizes text files as skill resources only if they have one of these extensions: `.md`, `.txt`, `.rst`, `.html`, `.htm`, `.xml`, `.svg`, `.json`, `.jsonl`, `.yaml`, `.yml`, `.toml`, `.csv`, `.tsv`, `.ini`, `.cfg`, `.css`.

Text resources use the same automatic encoding detection as `SKILL.md`; UTF-8 is recommended. In addition:

* Script extensions must be listed in the agent configuration's "Allowed Script Extensions".
* iCode reads only files at the skill directory root and in its immediate subdirectories. For example, it discovers `guide.md` and `references/guide.md`, but ignores `references/api/guide.md`.
* Within a skill directory, iCode skips files and directories accessed through symbolic links or Windows directory junctions. Both point to other locations on disk. To use their contents, place the actual files in the skill directory.

Files that do not meet the extension, directory depth, or link rules do not appear in the skill's resource or script list. A missing, inaccessible, or undecodable resource fails when read. These issues do not prevent an otherwise valid `SKILL.md` from loading.
