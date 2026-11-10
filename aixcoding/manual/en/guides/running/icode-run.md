# Run headless tasks with icode run

`icode run` runs a task in the terminal and returns the agent's final response when it finishes. It does not open the terminal user interface (TUI). While the task runs, it shows what the agent is doing, one line at a time, making it suitable for one-off tasks, scripts, and automated workflows.

This guide explains how to specify a task, agent, model, working directory, and session for `icode run`, how to read or hide its progress, and how to obtain JSON output for use in programs.

## Before you begin

Before running a task:

- Install iCode and configure at least one working model. See [Getting started with iCode](../../start/getting-started.md) and [Configure models](../configuration/models.md).
- Choose an agent. The built-in `QA` agent appears as "Q&A Agent" in the TUI and is instructed not to modify anything, but it can run shell commands, and `icode run` does not ask for approval before running them; the `Code` agent can modify files and run commands. A custom agent's capabilities depend on its configuration. See [Configure agents](../configuration/agents.md).
- If the task might modify files, save any work that has not been written to disk and run it in a working directory you can restore, such as a Git repository with the current changes committed.

> **Note**
>
> `icode run` always bypasses tool approval. The agent does not wait for confirmation when calling the shell, writing files, or running skills. Only run trusted agents and tasks in trusted working directories.

`icode run` cannot interact with a person while running; include the task requirements and necessary context in the prompt before starting.

## Run a task

Run the command in your project directory and specify an agent with `-a` or `--agent`. Enclose task text in quotes if it contains spaces:

```shell
icode run "Summarize this project's directory structure and main modules" --agent QA
```

While the task runs, iCode shows its progress. When the task finishes, it shows a summary line and then the agent's final response. For example:

```text
• Q&A Agent ready · Example Model · session 8de5057d58ff · ~/projects/demo
I'll start with the top-level layout.
→ shell  ls
  ✓ 0.1s
→ read   README.md
  ✓ 0.0s

✓ Done · 8.2s · 2 tool calls · session 8de5057d58ff
The project has three main parts: ...
```

See [Read the progress](#read-the-progress) for what each line means. Control characters in the response that could change your terminal are shown as `�`; when you redirect the output to a file or another program, or add `--json`, the response is kept exactly as written. If the task fails, the error message is written to standard error (`stderr`), and the command exits with a nonzero status. When iCode can tell why a model request failed, the message says so, and a `detail:` line below it shows the original error text.

Use `Code` when you need the agent to modify or verify code:

```shell
icode run "Fix the login form validation error and run the relevant tests" --agent Code
```

These tasks may modify files in the working directory and execute commands. Before running one, check that the prompt, agent, and current directory are what you intend.

Shell commands that the agent moved to the background, such as a development server, stop when the run ends, and iCode prints a warning saying how many it stops. See [Commands that keep running in the background](../configuration/tools.md#commands-that-keep-running-in-the-background).

Run `icode run -h` or `icode run --help` to see the options supported by the current version.

## Read the progress

Progress lines are written to standard error (`stderr`); the final response is written to standard output (`stdout`). In a terminal you see both. When you save the response with `>` or pass it to another program, only the response goes there:

```shell
icode run "Write release notes for the latest changes" --agent QA > notes.md
```

Each line shows one step:

| Line | Meaning |
| --- | --- |
| `• Q&A Agent ready · …` | The agent has started, with its model, session and working directory. |
| Plain text | What the agent says between steps, and what it had written when the task fails or is stopped. |
| `→ read   README.md` | The agent calls a tool: here, it reads a file. |
| `✓ 0.1s` or `✗ exit 1 · 2.5s` | The tool finished, or failed with the reason, and how long it took. |
| `↳ Explore → grep   TODO` | A sub-agent's step, named after the sub-agent. |
| `↻ … Retrying in 7s (attempt 2/18)` | A model request failed for a temporary reason and will be tried again. |
| `Todo 1/3 · → Running tests` | The agent's to-do list changed. |
| `Warning: …` | Something worth knowing that does not stop the task. |
| `✓ Done · 8.2s · 2 tool calls · session …` | The task finished: its duration, how many tools the agent called, and its session. |

To see only warnings, errors and the final response, add `-q` or `--quiet`:

```shell
icode run "Summarize this project" --agent QA --quiet
```

## Specify a working directory

Use `-C` or `--workdir` to specify the working directory the agent uses to handle files and execute commands, without first changing directories in the terminal:

```shell
icode run "Review the uncommitted changes and explain the risks" --agent QA --workdir <project-directory>
```

Replace `<project-directory>` with the actual project path. You can use an absolute path or a path relative to the directory where you run the command. The path must point to an existing directory.

## Read a task from a file

For tasks that are long, need version control, or are generated by a script, put the task in a text file and read it with `-t` or `--task`:

```shell
icode run --task prompts/review.md --agent Code --workdir <project-directory>
```

`--task` does not require a particular file extension. iCode reads the file's contents and uses them as the prompt for this task. The task file path can be absolute or relative. For a relative path, iCode looks for the file in the directory specified by `--workdir`, or in the directory where you run the command if `--workdir` is omitted.

Each run accepts only one prompt source: either a prompt passed directly or a file specified with `--task`. You cannot use both together.

## Choose an agent and model

`icode run` requires an agent specified with `--agent`. Run `icode agent list` to see the available agent profiles:

```text
Default  Name  Display Name  ID            Model
   *     Code  Code Agent    b011c0de0001  active
         QA    Q&A Agent     b0119a000005  Example Model
```

Some rows and columns are omitted from this example. `Example Model` is a fictional model profile.

You can use `Name`, `Display Name`, or `ID` as the value of `--agent`. A `Model` value of `active` means the agent uses the currently active model profile; a specific name means it uses a bound model profile.

For agents that use the `active` model, you can specify a model profile for this run with `-m` or `--model`. For agents with a bound model, `--model` has no effect.

Run `icode model list` to see the available model profiles:

```text
Active  ID            Name           Provider  API   Model          Context  Flags
   *    0123456789ab  Example Model  openai    chat  example-model  200k     stream
```

The `*` in the `Active` column marks the model profile that is active by default. You can use either `ID` or `Name` as the value of `--model`.

## Get JSON output

With `--json`, successful results are written as JSON objects to standard output (`stdout`), and errors and warnings are written as JSON objects to standard error (`stderr`). No progress is shown. For example:

```shell
icode run "hello" -a QA --json
```

The following is actual output from a run:

```json
{"session_id": "8de5057d-58ff-477e-9158-2f1b8d8e9fc0", "result": "Hello! I'm iCode, a read-only Q&A assistant for this codebase. I can help you with questions about the code, architecture, APIs, conventions, and how things work — with file and line references so you can jump straight to the source.\n\nWhat would you like to know?", "duration": 9.018}
```

Here, `session_id` is the ID of the session this task belongs to, `result` is the agent's final response, and `duration` is the task's duration in seconds.

When reading a task file that does not exist:

```shell
icode run --task missing.md -a QA --json
```

Standard error output:

```json
{"error": "Task file does not exist: missing.md", "code": "task_file_not_found"}
```

`error` describes the error, and `code` identifies the error category for use in programs. This error occurs before a session is created, so it has no `session_id`. If an error occurs after the task starts, such as a failed model request, the output also includes `session_id`.

## Continue an existing session

For an ongoing conversation, use `-s` or `--session` to resume an existing session and submit another task based on the conversation so far. Without `-C` or `--workdir`, the restored session uses its saved working directory rather than the directory where you run the command. `-C` or `--workdir` uses the directory you specify instead. For automation tasks that need a known working directory, specify `-C` or `--workdir` explicitly. If the session's saved working directory no longer exists, iCode stops with an error; use `-C` or `--workdir` to continue the session in another directory.

In headless mode, get the session ID from the `session_id` field in the first turn's JSON output:

```shell
icode run "Review the current changes and list the tests that need to be added" --agent Code --json
```

Pass the returned session ID to the second turn:

```shell
icode run "Add the missing tests based on the previous turn's findings" --agent Code -s <session-id> --json
```

Replace `<session-id>` with the session ID returned by the first turn.

Sessions created or continued with `icode run` also appear in the TUI's "Chat Sessions" window once you check "CLI" there. See [Resume an existing session](../daily-use/sessions.md#resume-an-existing-session).

## Exit status

Automation scripts can use the exit status to determine the outcome:

| Status | Meaning |
| --- | --- |
| `0` | The agent completed normally and returned its final response. |
| `1` | An error occurred with the configuration, task file, agent, model, session, or task execution. The specific reason is written to standard error. |
| `2` | Invalid command-line arguments, such as a missing `--agent`, or both or neither of a prompt and `--task`. The usage message is written to standard error as plain text, even with `--json`. |
| `124` | An internal operation timed out. `icode run` has no overall time limit for a task. |
| `130` | The run was interrupted. |
