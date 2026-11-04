# Write and use shortcuts

A shortcut is a prompt you save once as a Markdown file and reuse by typing `/` and its name. Choosing it fills the prompt into the input field, where you can edit it before you send it. A shortcut can also take arguments, switch the agent or model for the session, and fill in the output of shell commands when it is sent. Shortcuts work in the iCode window; `icode run` and editors connected through `icode acp` send `/name` as plain text.

## Where shortcut files go

iCode reads shortcuts from two places:

| Folder | macOS and Linux | Windows |
| --- | --- | --- |
| Your shortcuts | `~/.chrys/shortcuts` | `%APPDATA%\chrys\shortcuts` |
| The project's shortcuts | `.chrys/shortcuts` in the working directory and its parent directories up to the repository root | Same |

New and changed files show the next time you type `/`.

A project's `.chrys/shortcuts` must stay inside the repository: if it is a link to a folder outside it, iCode skips it.

The file name is the shortcut's name: `review.md` is `/review`. A file in a subfolder keeps the folder in its name: `git/commit-msg.md` is `/git/commit-msg`. When two folders hold a shortcut with the same name, the first one read is used: your own shortcuts, then the nearest project folder.

Some files are not loaded: a name with spaces (it could not be typed after `/`), a file or folder whose name starts with `.`, and a subfolder that is a link. If a shortcut you expect is missing, check its file name.

To see which shortcuts are loaded and where each comes from, enter `/runtime` and open the **Shortcuts** tab.

## Write a shortcut

A shortcut file is Markdown with optional settings at the top, between `---` lines:

```text
---
description: Review my uncommitted changes
agent: QA
model: my-provider/my-model
---

Review these changes and point out bugs first:

!`git diff --stat`

Focus on $ARGUMENTS.
```

| Setting | Effect |
| --- | --- |
| `description` | Shown next to the name in the `/` list. |
| `agent` | The agent to switch to when the prompt is sent. |
| `model` | The model to switch to when the prompt is sent: a model profile's name, or `provider/model`. |

Everything after the settings is the prompt.

In the prompt you can use:

- `$ARGUMENTS`: everything typed after the name.
- `$1`, `$2`, …: one argument each, split at spaces; quote an argument that contains spaces (`"the meeting moved"`). The highest-numbered one takes all the remaining arguments.
- No placeholder at all: what you type after the name is added at the end of the prompt, after a blank line.
- `` !`command` ``: runs `command` in the session's working directory when the prompt is sent, and puts its output in its place.

## Use a shortcut

1. Type `/` in the input field. Shortcuts are listed in the **Prompt Shortcuts** group.
2. Choose one, or type its name, with or without arguments (`/review error handling`), and press **Tab** or **Enter**.
3. The prompt fills the input field. Arguments you did not give are highlighted: press **Tab** to move between them and type to fill them in. If you press **Enter** while some are still empty, iCode asks once before sending. Stepping through your prompt history with **↑** and back with **↓** brings the prompt back as it was, highlights included; so does closing the full-screen editor without changing the text. If you change the text in the editor, the highlights go, but the prompt still belongs to its shortcut.
4. Edit the prompt if you like, then press **Enter** to send it.

### Commands in the prompt

When you send the prompt, your [prompt hooks](hooks.md) check it first, as you typed it, commands included. Then each `` !`command` `` in it runs in the session's working directory, and its output replaces it in the message the model receives; the conversation shows the message with the output. A command may run for up to 10 seconds, and up to 16 KB of its output is kept. While the commands run, the status bar says so and other actions, such as switching the agent or starting a new session, wait; press **Esc** to cancel. If a hook blocks the prompt, a command fails to start, runs out of time or is cancelled, or the prompt cannot be sent for another reason, nothing is sent and the prompt comes back to the input field as it was, highlights included, unless you have typed something else there meanwhile.

The commands run only if the shortcut is still loaded from the same file for the session's working directory. If you have since changed the working directory, or moved or deleted the shortcut's file, they do not run: the prompt comes back as plain text, and sending it again sends it as typed.

Whenever sending will run commands, a card above the input field says how many, such as `2 commands in the input run when sent`.

While a task is running, a prompt that runs commands or switches the agent or model is not sent: it stays in the input field, and you can send it when the task ends. A prompt that only switches can be added to the running task as it is: click **✕ Don't switch** first.

Such a prompt always starts a new turn. After a task stopped or failed, press **Enter** to send it; the **Continue** button sends what is in the input field as a note, as typed, so it leaves such a prompt where it is.

Once you choose a shortcut, the input field holds that shortcut's prompt until you clear it or replace it, however much you edit it. Every `` !`…` `` you can see in it when you press **Enter** runs, including any you typed or pasted. A message you type yourself without choosing a shortcut never runs commands.

### Agent and model

If the shortcut sets an `agent` or `model` that differs from the one in use, a card above the input field shows the change, such as `Code → QA`. When you send the prompt, the session switches to it first and stays on it for the rest of the session, also if the prompt itself is then not sent. A new session (`/new` or `/clear`) starts on the agent and model new sessions use: the agent you last chose with `#` (or started with) and your default model.

To send the prompt without switching, click **✕ Don't switch** on the card. Choosing an agent with `#` or a model with `$` yourself also replaces the card's switch. If the agent or model named in the file is not found, the card says so and the prompt is sent with the current one. An agent that is set to its own model always runs that model: if the shortcut names a different one, a notice says it was not applied and the prompt is sent on the agent's model, and the session does not keep the shortcut's model for later.
