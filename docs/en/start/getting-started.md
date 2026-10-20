# Getting started with iCode

This tutorial shows how to install iCode, configure a model, and use agents to complete a development task in a project.

## 1. Install iCode

Install iCode with [uv](https://docs.astral.sh/uv/getting-started/installation/). If Python 3.14 is not on your system, uv downloads it for you:

```shell
uv tool install iCode-TUI
```

To upgrade later, run `uv tool upgrade iCode-TUI`; when a newer version is out, the iCode welcome screen tells you so. To remove iCode, run `uv tool uninstall iCode-TUI`.

Windows (x64) and Linux (x86-64 or ARM64, glibc 2.27 or later) need nothing else. On a Mac with Apple silicon, first install the Xcode Command Line Tools with `xcode-select --install`; uv uses them to build one dependency during installation.

If you used an iCode offline package before, remove it first with `icode uninstall` (see [Uninstall iCode](#uninstall-icode)). If your version does not have that command, delete the folder it unpacked into: `~/Library/Application Support/pyapp/chrys` on macOS, `~/.local/share/pyapp/chrys` on Linux (or `$XDG_DATA_HOME/pyapp/chrys` if you set `XDG_DATA_HOME`), or `%LOCALAPPDATA%\pyapp\data\chrys` on Windows. Then delete `~/.local/bin/chrys` on macOS or Linux, or the `%LOCALAPPDATA%\chrys\bin` folder on Windows.

If uv reports that `icode` already exists, an earlier iCode offline install or another program with the same name is in the way. If it is an iCode offline install, remove it as described above, then run the install command again.

### Offline packages

On an Intel Mac, Windows on Arm, an older Linux, or a device without internet access, use the prebuilt offline packages from [iCode Releases](https://github.com/openJiuwen-ai/iCode/releases) instead. The packages include Python and every dependency, so you do not need to install Python separately.

The install script picks the package for your computer, checks the download and installs it. Run the same command again later to upgrade. On macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/openJiuwen-ai/iCode/main/scripts/install.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/openJiuwen-ai/iCode/main/scripts/install.ps1 | iex
```

The script downloads from GitHub, and from GitCode if GitHub fails. If GitHub is slow or unreachable where you are, get the script from GitCode and download from there:

```bash
curl -fsSL https://raw.gitcode.com/openJiuwen/iCode/raw/main/scripts/install.sh | sh -s -- --source gitcode
```

```powershell
$env:ICODE_SOURCE = "gitcode"; irm https://raw.gitcode.com/openJiuwen/iCode/raw/main/scripts/install.ps1 | iex
```

To install a particular version, end the command with `| sh -s -- --version 0.29.1` instead of `| sh`. On Windows, run `$env:ICODE_VERSION = "0.29.1"` first. A setting made with `$env:` lasts until you close that PowerShell window.

On macOS and Linux, passing an empty value to `--version` or `--source` (for example, `--version ""` or `--source=`) stops installation with an error. If you pass a shell variable to either option, make sure it contains a value.

To install by hand instead, download the package for your platform and follow the steps below. In the commands, `<version>` and `<architecture>` are placeholders. Replace them with the actual values in the downloaded filename.

#### macOS

Choose `icode-macos-aarch64-v<version>-offline.tar.gz` for Apple silicon or `icode-macos-x86_64-v<version>-offline.tar.gz` for Intel, then run the following in your download directory:

```bash
tar xzf icode-macos-<architecture>-v<version>-offline.tar.gz
chmod +x ./icode
./icode install
```

#### Linux

Choose `icode-linux-x86_64-v<version>-offline.tar.gz` or `icode-linux-aarch64-v<version>-offline.tar.gz` for your processor, then run the following in your download directory:

```bash
tar xzf icode-linux-<architecture>-v<version>-offline.tar.gz
chmod +x ./icode
./icode install
```

The Linux x86-64 package requires glibc 2.17 or later, and the ARM64 package requires glibc 2.18 or later. Distributions that provide only musl, such as Alpine Linux, are not supported by the prebuilt packages.

#### Windows

Download and extract `icode-windows-x86_64-v<version>-offline.zip`, or `icode-windows-aarch64-v<version>-offline.zip` on Windows on Arm. Open PowerShell, go to the extracted directory, and run:

```powershell
.\icode.exe install
```

The installer attempts to add iCode to the current user's `PATH`. Open a new terminal after installation.

### Verify the installation

```shell
icode --version
```

The command should print the iCode version number. If you installed with uv and your terminal cannot find `icode`, run `uv tool update-shell` and open a new terminal.

With an offline package, if your system cannot find `icode` on macOS or Linux, add `~/.local/bin` to `PATH` as the installer instructs. If the installer reported that `~/.local/bin/icode` already exists, that command starts another program; use `chrys` instead. On Windows, open a new terminal; if the installer reported that updating the user PATH failed, add `%LOCALAPPDATA%\chrys\bin` to your user `PATH` manually.

### Uninstall iCode

Run:

```shell
icode uninstall
```

If you installed an offline package, the command lists what it will remove: the `icode` and `chrys` commands and the files the package unpacked. It removes them once you confirm. On Windows, it also removes iCode from your user `PATH`, and the last files go a moment after the command exits. The package you downloaded stays where you saved it.

If you installed with uv, the command only shows what to run: `uv tool uninstall iCode-TUI`.

iCode keeps its data in `~/.chrys` (`%APPDATA%\chrys` on Windows): your settings, API keys, model profiles, agents, skills, sessions and more. Uninstalling keeps this folder. To delete it and everything in it as well, run `icode uninstall --purge`; if you save sessions in another folder, the `sessions` folder there is deleted too. If you installed with uv, do this before `uv tool uninstall`, while the `icode` command still exists. Add `--yes` to skip the confirmation.

## 2. Start iCode in a project

Start this tutorial in a project managed with Git or one that you can safely restore. Before asking an agent to modify files, save any unsaved content and record existing changes with a Git commit. This lets you distinguish the agent's changes and restore the previous state if needed.

In a terminal, go to the project directory and start iCode:

```shell
cd <project-directory>
icode
```

Replace `<project-directory>` with the actual path to your project. To switch directories after startup, see [Working directory](../guides/daily-use/workspaces.md).

## 3. Configure a model

1. Press **F4** or click `f4 Models` at the bottom of the screen to open the **Model Configuration** dialog.
2. Click **New**.
3. Enter a **Profile Name** of your choice, such as `My Model`.
4. In the **Model Options** section, select the **Provider** and **API Style**. Only OpenAI and DeepSeek (OpenAI) provide an API Style option. Before configuring it, check the model service's documentation for supported APIs and follow its recommendation when choosing **Chat Completions** or **Responses**. If the service supports only one API, select its corresponding style; if it supports both, choose according to the service's recommendation or your needs.
5. Enter the model ID in the **Model** field, and fill in the **Base URL** and **API Key** fields as required by the model service. These field names start with the selected provider, for example **OpenAI Model**.
6. Set **Max Context Window** and the **Max Output Tokens** field according to the model provider's documentation.
7. Save the profile and close the dialog. If no model profile was previously available, this profile automatically becomes the active model profile.

> **Tip**
>
> An API key entered directly is stored locally in the model profile file. To avoid writing the key to that file, [provide the API key through an environment variable](../guides/configuration/models.md#provide-an-api-key-through-an-environment-variable).

### Verify the model connection

Submit the following in the input field:

```text
Hello
```

iCode should reply with a greeting. If the model service returns an error, check the model profile again, including the model ID, base URL, and API key.

While an agent runs, the status bar above the input shows elapsed time and tool calls. Long runs include hours and days, for example `1d 4h 20m 50s`. Tool cards, background tasks, CLI progress, and the F12 timing dashboard also support days; compact displays keep only the largest time units.

## 4. Use agents

### Select an agent

iCode provides two main agents by default: Q&A Agent and Code Agent. Both can read the project, analyze code, and discuss approaches, but they have different roles and tools. Q&A Agent primarily provides read-only capabilities for understanding projects, explaining code, and analyzing problems. Code Agent can modify files and run commands, making it suitable for writing, reviewing, refactoring, debugging, and validating code.

You can switch agents when no agent task is running. Type `#` in the input field to display the agent list above it. Select an agent by clicking it, or use the up and down arrow keys and press Enter. You can also click the agent name in the status bar above the input field to switch agents in the agent selection dialog.

### Use Q&A Agent

Q&A Agent helps you understand a project, analyze code, trace implementations, and discuss approaches **without modifying the project**. It answers based on the project contents and aims to provide evidence you can trace back to its source.

After switching to **Q&A Agent**, type `/new` in the input field and press Enter to start a new session.

The following examples assume the current project is a to-do app and illustrate how to use the agents. Adapt the questions and tasks to your own project.

```text
Analyze the task list implementation and propose a plan for adding a "Clear completed tasks" feature. Explain which files need to change and the implementation steps.
```

Q&A Agent may search for files, read their contents, or run read-only commands. Tool calls appear as cards. After completing its analysis, the agent presents an implementation plan.

### Use Code Agent

Code Agent handles tasks that require **modifying files, running commands, and validating results**. It reads the relevant code first, then implements, debugs, or refactors it as requested.

After Q&A Agent provides an implementation plan, you can switch to **Code Agent** in the same session. Switching agents preserves the current session's context, so Code Agent can continue the development task using the existing plan:

```text
Implement the "Clear completed tasks" feature using the plan we just discussed. Modify the code and run the necessary tests.
```

If an approval dialog appears, review the tool call before approving it. Pay particular attention to the command, target files, and scope of its effects, and approve only when they match your expectations. For the behavior of each approval mode, see [Configure approval modes](../guides/configuration/approval.md).

When the task is complete, review the agent's final response and use `/diff` to browse the file changes recorded in the current session. Then use Git or another version control tool to inspect the complete working directory changes. Confirm that the feature works correctly, the necessary tests pass, and there are no unrelated changes. Once you have verified the changes, you can commit them.

### Ask about iCode itself

You can ask the agent about iCode in the input field, for example:

- "What features does iCode offer?"
- "How do I configure an MCP server in iCode?"
- "How does iCode's context compaction work?"

The agent looks up the answer in the user guide that comes with your installed version and tells you which pages it used. If a feature described in the guide is not turned on for the current agent, the answer says so. If the agent cannot read files, it tells you that it could not check the guide.

To read the guide yourself, type `/help` or press **F8**.

### Write multi-line messages

Press **Enter** to send a message, and **Shift+Enter**, **Ctrl+Enter** or **Ctrl+J** to start a new line. Press **Ctrl+O** to write in a larger editor.

Some terminals, including macOS's built-in Terminal and some tmux setups, can't tell Shift+Enter or Ctrl+Enter apart from Enter and send the message instead. Use Ctrl+J there.

In other multi-line fields, **Enter** and **Shift+Enter** start a new line. In the Ctrl+O editor, press **Ctrl+O** or **Ctrl+Enter** to commit.

### Read and copy replies

All agents' replies support Markdown, formulas and Mermaid diagrams. Formulas in your own messages are not rendered as math.

Lists and quotes can contain headings, tables, code and formulas.

Single-line math is written with `$...$` or `\(...\)`. Display formulas in `math` code blocks, `$$...$$` or `\[...\]` can show fractions, matrices and aligned equations. Ordinary text, prices and shell variables keep their Markdown formatting. Formulas that are unsupported or too wide are shown as complete, wrapped source.

For example, `$x^{n+1}$` displays as xⁿ⁺¹ and `$90^\circ$` as 90°.

Mermaid code blocks appear as diagrams; choose **Open full diagram** to view and scroll the full result. Some diagrams are simplified with a notice; unsupported diagrams stay as source.

Mouse selection copies what is displayed, without adding newlines for screen wrapping. Use **copy** beside the message header to copy the original Markdown and LaTeX. Reopening an older conversation uses the current rendering.

## 5. End the session

To finish using iCode, type `/exit` or press `Ctrl+Q` to exit.

## Next steps

You have installed iCode, configured a model, and learned the basics of Q&A Agent and Code Agent, including how to use them together in the same session to complete a development task.

You can continue exploring:

- [Configure agents](../guides/configuration/agents.md) to create or modify agents and set their instructions, models, tools, and sub-agents
- [Configure models](../guides/configuration/models.md) to connect to and switch between model services
- [Working directory](../guides/daily-use/workspaces.md)
- [Configure approval modes](../guides/configuration/approval.md)
- Extend iCode with [Skills](../guides/extensions/skills.md) and [MCP](../guides/extensions/mcp.md)

### Request-correlated interactive launches

Integrations can probe `icode launch capabilities`, then open `icode --launch-manifest /absolute/request.json` and query `icode launch inspect --request-id ID`. Protocol v1 requires an existing absolute working directory, a unique request ID, the selected role/model references, context text with its SHA256, and exact Skill source/file pins. `icode launch preflight --manifest FILE` checks native Skill discovery without creating a session. Existing project trust settings and profile switches still apply; this protocol does not install Skills or change roles.

A confirmed receipt means iCode acquired the native session and staged a visible composer draft. The user sends it normally. `composer-staged` does not mean submitted, read, adopted or completed, and an old receipt does not prove the draft still exists after exit. Same-request retries only inspect the saved receipt; changed content is rejected, and an unknown result requires reconciliation. A new `resume` request names the full original session ID and directory; failure exits without creating a replacement session. Ordinary manual `-s` keeps its existing behavior.

The explicit interactive handoff retains a native checkpoint even before the draft is sent, so a new resume request can reopen the same empty session. This does not insert a user message or submit the draft.
