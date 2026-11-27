# Configure approval modes

When an agent makes a tool call, such as running a Shell command or writing a file, iCode may require approval first. This guide explains how to choose an approval mode and handle approval requests in the terminal user interface (TUI), as well as how approval settings differ across ways of running iCode.

## Understand the three approval modes

The approval mode determines how iCode handles tool calls that require approval. Which calls require approval depends on both the `approval` policy in the agent profile and iCode's safety rules. See the [approval field in the agent profile reference](../../reference/agent-profile.md#approval).

The three approval modes behave as follows:

| Mode | Behavior |
| --- | --- |
| MANUAL | Tool calls that require approval open a dialog and wait for a person to approve or decline. |
| AUTO | An approval judge model evaluates tool calls. Calls judged safe are approved automatically; suspicious calls are flagged for a person to decide. |
| BYPASS | Tool calls run without asking, even when the agent configuration or safety rules require approval. |

**Approval judge model**: In automatic mode, iCode calls the approval judge model and sends it the current time, the workspace directories, all user prompts of the current turn and the latest of them, and the tool name, tool kind, and arguments. By default, the approval judge uses the current session's model. To change it, press **F10** to open **Settings**, select the **Models & Agents** tab, and change **Approval judge model** in the **Model roles** section.

> **Tip**
>
> **Manual mode does not mean every tool call opens a dialog.** It means that calls requiring approval are decided by the user. Calls that do not require approval or qualify for automatic approval run directly. See [Understand automatic approval and safety protections](#understand-automatic-approval-and-safety-protections).

## Switch approval modes in the TUI

Use either of these methods to switch the current approval mode in the TUI:

- Type `/approval` in the input field, then press **Space** or **Enter** to open the approval mode list and choose a mode. You can also specify a mode directly, for example `/approval auto`.
- Click the approval mode label in the upper-right corner of the interface and choose a mode in the **Approval Mode** dialog.

You can switch modes while a task is running. Approval requests that are already open are unaffected; subsequent tool calls use the new mode.

### Set the default approval mode

The TUI starts in manual mode on first launch with the default settings. When you switch modes, iCode saves your choice for the next launch, including bypass. Each time the TUI starts in bypass mode, a warning reminds you that tools can run without approval.

To change only the default for the next launch without changing the current approval mode, press **F10** to open **Settings** and change **Default approval mode** on the **Safety** tab. **Settings** does not offer bypass mode as a default that can be saved.

## Choose how automatic review works

Automatic mode uses **Direct review** by default. Direct review judges the action as a whole; **Individual risk checks** use a fixed checklist and ask you to confirm any flagged risk.

| Strategy | What you can expect |
| --- | --- |
| Direct review (default) | The approval model considers your request and the proposed action together. It runs actions it judges safe and asks you about the others. |
| Individual risk checks | The model checks the risks listed below one by one. A detected risk opens an approval dialog with an explanation, so you can approve or decline. |

Both strategies ask you to confirm file deletion, including temporary files and build outputs. With Individual risk checks, the dialog identifies the matching risk from the checklist below.

Individual risk checks send the visible conversation history to the configured approval judge, including user messages, assistant text, historical tool calls, and any available compaction summary. The pending call is sent separately. Tool results are omitted except for authenticated answers from the user question dialog. A summary can provide context but does not itself authorize an action or lift a user boundary. When a Responses session continues on the provider, the review also uses the visible history saved locally by iCode. An externally hosted ACP agent may expose only the current user turn and pending call. The pending call also includes its tool kind, the workspace roots and the current time. For an in-process sub-agent, the review includes the calling conversation at delegation and any current user instructions missing from the visible history. A queued follow-up can inform approval once it is included in a model request. If Send now supersedes a pass, any pending Formal approval from that pass expires. Delegation text and workflow node inputs are identified by their source. The workflow run request supplies the user's own instructions. The review places each queued follow-up where the model received it, after any tool calls that ran before delivery. Adjacent messages alone do not establish consent or lift a user boundary. Verified answers appear at submission time with their original questions. Verified question answers are paired with the question shown, and any hook-provided text is attributed separately. The judge retains the original answer even if a hook withholds the tool result from the agent.

To choose a strategy:

1. Enter `/approval auto` to switch to automatic mode.
2. Press **F10**, open **Safety → Approval**, and find **Automatic review strategy**.
3. Choose **Direct review** or **Individual risk checks**, then close Settings. Your choice is saved automatically and takes effect when the window closes. A review already in progress finishes as before.

Individual risk checks apply three hard rules for sensitive data transfer, file or folder deletion, and sensitive file access. They also check explicit user boundaries and other risks, including changes to shared infrastructure, security controls, and Git history. A hard rule, an active user boundary, or a soft rule without the required consent opens an approval dialog.

When all checks clear, the action can run automatically. If the review cannot return a valid decision after a retry, fails, or times out, an approval dialog lets you decide what to do.

This setting applies only in automatic mode. Actions that do not need approval and [approvals you have already remembered](#remember-approvals) continue to work as before.

## Handle approval requests in the TUI

Tool calls that require approval open an **Approval Required** dialog showing the tool name and call arguments. For file edits, it also shows the planned diff so you can review changes before they are made. An argument too long to show at once, such as the whole content of a file, appears in a box with a scroll bar: scroll it with the mouse wheel, or click it and use the arrow keys, **Page Up**, **Page Down**, **Home** and **End** to read it to the end. Characters that could hide text or change the order it shows in, such as terminal control codes, appear as **�**; copying an argument still gives its original text.

- Press **Y** to approve or **N** to decline, or click the corresponding button. You cannot close the dialog with **Esc**. If you set a [human approval timeout](#set-the-human-approval-timeout), a request you have not answered when it runs out is declined.
- When declining, you can provide a reason. The reason is sent to the agent to help it adjust its next steps. Once a reason is entered, the approve button is disabled.
- In automatic mode, no dialog opens while the approval judge model evaluates a call; **Reviewing** appears, after a spinning icon, to the left of the approval mode label in the upper-right corner. Calls the model judges safe run without a dialog. If the model judges a call suspicious, the dialog opens with the title **Flagged by Auto-Review**, shows the reason, and waits for a person to decide. The cursor starts in the reason field so that a stray key press does not approve the call: type a reason and decline, or press **Tab** to move to the buttons. If the approval judge model is unavailable, or evaluation fails or times out, the dialog opens for a person to decide instead of approving the tool call automatically.
- To see each call while it is evaluated, press **F10** to open **Settings** and turn off **Show the approval dialog only when Auto-Review flags a call** on the **Safety** tab. The dialog then opens at once and shows **Evaluating**: it closes by itself if the model judges the call safe, and you can approve or decline before evaluation finishes.
- While a dialog waits for your decision, the terminal tab shows `?` before its title, so you can tell from another tab that iCode needs you. See [Follow a task in the terminal tab](../daily-use/sessions.md#follow-a-task-in-the-terminal-tab).

## Remember approvals

You can let iCode remember a Shell command or a file change you approve, so the same request does not ask you again. The approval dialog for a Shell command or a file write or edit shows a **Remember Approval** box:

1. Check **Don't ask again for this command** (for files: **Don't ask again to modify these files**).
2. Choose **This session** or **This working directory**. **This session** also applies after you restore the session; **This working directory** also applies in your other sessions using the same physical directory that are not in a workspace. In a session that belongs to a [workspace](../daily-use/workspaces.md#workspaces), the second choice is **This workspace**, which applies in the workspace's other sessions; leaving the workspace ends reuse of its approvals in the session. The original command and path constraints still apply.
3. Select **Approve**.

The next time the agent makes the same request, it runs without a dialog. Only that command, or those files, are remembered; other operations still ask. Approving without checking the box approves this one call only. A few cases are never remembered:

- A call that the approval judge model approves in automatic mode. A call that Auto-Review flags still offers the box, and you can remember it when you approve it.
- A request you edit in the dialog: the edited call runs once.
- A command for which the agent names the folder to run in, and requests that touch sensitive files such as credentials or SSH keys.
- Sub-agents, workflows, MCP tools and other tools.

If a folder or file link changes between your approval and the run, so that the call would act on a different place, the call stops with an error instead of running there.

For a simple command, **Advanced options** offers **Allow arguments added to the end of this command**. Then the remembered command also runs without asking when the agent adds more arguments to its end. Remembering `git push origin main` this way also allows `git push origin main --force`, so the dialog shows a warning. Leave this unchecked unless you trust every argument that could be added.

If iCode cannot save your choice, the call still runs once and a notice says that the approval was not remembered.

### Manage remembered approvals

Use the `icode approval` command to see and remove what you have remembered:

```bash
icode approval list                          # everything you have remembered
icode approval list --workdir /work/demo     # only one working directory
icode approval list --session SESSION_ID --json
icode approval revoke GRANT_ID               # remove one approval by its ID from the list
icode approval clear --workdir /work/demo    # remove all approvals for one working directory
icode approval clear --session SESSION_ID    # remove all approvals for one session
icode approval clear --workspace NAME       # remove all shared approvals for one workspace
icode approval clear --all                   # remove everything
```

Pass `--workdir` with `--session` to limit the selection to that session’s grants for the directory. A removed directory can still be used as a filter.

Both filters require a non-empty value. Use `clear --all` on its own to remove every grant.

Approvals for a session are deleted with that session and are not copied to a branch you create with `/fork`. Approvals for a working directory or a workspace stay until you remove them. Pass a workspace name or ID to `--workspace` to select its shared approvals; for a deleted workspace, use its full ID.

## Understand automatic approval and safety protections

The following operations usually run without an approval dialog:

- Safe, read-only Shell commands that do not access sensitive targets, such as `ls`, `cat`, and `grep`.
- File writes within the working directory's Git repository that do not access sensitive targets.

Shell commands and file reads or writes that access sensitive targets such as `.env` files, credentials, and private keys still go through approval, even for read-only operations or writes within a Git working directory. This protection applies in manual and automatic modes. Bypass mode skips these approval protections.

Skill scripts run locally with the current user's permissions, without sandbox isolation, so they request approval by default. When the session uses bypass mode, skill scripts run without asking. Before installing or running a third-party skill, review its `SKILL.md`, scripts, and related files to make sure they are trustworthy.

Web tools send requests to outside services, so the `web_search` and `web_fetch` kinds also request approval by default, even when the agent's `approval.default` is `auto`. Explicit `approval.overrides` rules for these kinds or tool names still take precedence. In automatic mode the approval judge model may approve them, and in bypass mode they run without asking. Provider-run search, such as DeepSeek's server-side `web_search`, happens on the provider's side and is not covered by these approval rules. See [Configure web tools](./web-tools.md#approve-web-tool-calls).

## Verify approval modes in the TUI

Select the built-in Code agent, then submit this request:

```text
Use the Shell tool to run icode --version
```

This command only displays the version and does not modify files, but it is not among the read-only Shell commands approved automatically. Expect the following results:

- In manual mode, the **Approval Required** dialog opens. After approval, the iCode version is displayed.
- In automatic mode, **Reviewing** appears to the left of the approval mode label while the approval judge model evaluates the command. The model will usually judge it safe, and the iCode version is displayed without an approval dialog. If the model flags it as suspicious, the dialog shows the evaluation reason and waits for a decision.
- In bypass mode, the command runs and displays the iCode version without an approval dialog.

## Approval modes in other ways of running iCode

Other ways of running iCode use the following approval modes and switching methods:

- **Headless CLI (`icode run`)**: Always bypasses approval and provides no approval-related options.
- **iCode ACP server**: Defaults to manual mode. Use `icode acp --approval manual|auto|bypass` to set the initial mode. ACP clients that support this capability can also switch the current session's mode.
- **Browser-hosted TUI (`icode serve`)**: Use the TUI operations described earlier to switch approval modes and handle approval requests.

## Set the human approval timeout

Press **F10** → **Settings** → **Safety**, then set **Human approval timeout (seconds)** in the **Approval** section. This setting applies to both the TUI and ACP clients. The default is **0 (no timeout)**; positive values set a deadline in seconds. Negative values are adjusted to **0**. Restart iCode (the TUI or ACP server) after saving.

With a positive timeout configured, the TUI timer starts when the approval dialog is shown and the call is ready for a human decision. Waiting behind another approval dialog and preparing its content (including file diffs) do not use up the timeout. In ACP, timing starts when the server sends the human approval request. If no response arrives before the timeout, iCode rejects that tool call.

Without a timeout, a request waits until you approve or decline it, it is cancelled, or the session closes. Stopping the ACP server also ends any approval that is still waiting.
