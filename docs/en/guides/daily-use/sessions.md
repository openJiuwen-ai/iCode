# Manage and resume sessions

A session contains conversation history and related data, such as rollback snapshots and diagnostic logs. iCode saves sessions automatically so you can resume a conversation or look up past tasks later. This guide explains how to switch, name, find, resume, and delete sessions in the terminal user interface (TUI), and how to migrate existing sessions when changing their storage location.

## Switch to a new session

When the agent is idle, enter the appropriate command in the input field, depending on whether you want to keep the current session:

| Goal | Command | Effect on the current session |
| --- | --- | --- |
| Start a new session and keep the current session available to resume later | `/new` | Kept; you can resume it from the session list |
| Start a new session without keeping the current session | `/clear` | Opens a confirmation dialog; once confirmed, the session is permanently deleted and cannot be recovered |

Leaving a session, by either command or by switching to another session, stops the shell commands it runs in the background. See [Commands that keep running in the background](../configuration/tools.md#commands-that-keep-running-in-the-background).

## Change the session title

You can change the current session's title in any of these ways:

- Click the "Session: ..." area in the upper-left corner of the main screen to open the "Session Title" dialog, enter a title, and click "Save".
- Enter `/rename` in the input field to open the "Session Title" dialog, enter a title, and click "Save".
- Enter a title directly after `/rename`, for example, `/rename Fix the login issue`.

The top of the "Session Title" dialog also shows the session ID. Click "Copy ID" to copy it, for example to resume the session later with `icode -s <session-id>`. Click "Open folder" to open the folder that holds the session's data in your file manager. "Open folder" is not shown where iCode cannot open a file manager, for example over SSH.

After you set a title manually, the new title appears in the session list and on the main screen, and iCode stops updating that session's title automatically. If [automatic session titles](../configuration/settings.md#sessions) are enabled, clearing the "Session Title" dialog and saving restores the existing automatic title and lets iCode continue updating it automatically during subsequent tasks.

## Follow a task in the terminal tab

The terminal tab shows your latest message, or the session title once iCode updates it; a title you set yourself always stays. A mark before it shows how the current task is going, so you can check on it from another tab:

| Mark | Meaning |
| --- | --- |
| Spinning dots, such as `⠋` | The agent is working. |
| `?` | The agent is waiting for you to approve a tool call or answer a question. |
| `!` | A sub-agent failed and is waiting for you to click "Retry" or "Abort". |
| `✓` | The task is finished. |
| `✗` | The task failed. |

A task you stop shows no mark, and the mark clears when you start the next task. Some terminals do not show tab titles.

When you stop a task or it fails, the part of the reply that already appeared stays in the conversation and in the saved session, and the agent reads it when you send your next message. If you click **Retry** or **Continue** without typing anything instead, the agent writes the reply again: when it starts, the text it writes again is removed, and tool calls stay.

## Resume an existing session

When the agent is idle, enter `/resume` in the input field to resume the most recent chat session.

To find and resume another session, press **F1**, click `f1 Sessions`, or enter `/sessions` in the input field to open the "Chat Sessions" window. It lists each session's ID, title, directory, last active time, number of conversation turns, and session data size. Each page shows up to 100 sessions, most recently active first.

- **Filter by where a session was last used**: The "TUI", "CLI", and "ACP" checkboxes above the search field show sessions last used in the TUI, in [`icode run`](../running/icode-run.md), or in an editor or other client connected through [`icode acp`](../running/icode-acp.md). Only "TUI" is checked at first. Sessions from earlier iCode versions count as TUI. iCode remembers your choice until you quit it. Hover over a session to see where it was last used.
- **Switch pages**: Click "Previous" or "Next" on the right, next to the checkboxes.
- **Sort sessions**: Sessions are sorted by last active time, newest first. Click a column header to sort the current page by that column: "Last Active", "Turns", and "Size" start in descending order, and the other columns in ascending order. Click the same header again to reverse the order.
- **Search sessions**: Enter a session ID, title, directory, or a prompt you previously entered in the search field at the bottom. The search covers only the current page.
- **Resume a session**: Select a session and click "Resume". You can also double-click it, or select it with the up and down arrow keys and press Enter. iCode closes the current conversation view, loads the selected session, and switches the working directory to the session's saved primary working directory; the current session remains saved in the list. Before resuming, check the working directory that will be restored in the list's "Directory" column. If that directory no longer exists, iCode asks you to choose another folder; see [If the working directory is deleted or moved](workspaces.md#if-the-working-directory-is-deleted-or-moved).

A resumed session continues on the agent and model it was saved with. That doesn't change the agent and model a new session (`/new`) starts on: the agent you last chose with `#` (or started with) and your default model.

When you resume a long conversation, iCode shows its most recent part first so you can continue right away; earlier messages keep loading above it for a few seconds.

A resumed conversation shows the model's saved thinking folded to one line above the reply it led to: **Thought:**, followed by how the thinking begins; click it to unfold it. If a reply was interrupted or failed, the thinking shown for the request that was cut off is not saved, so it does not appear.

Successful turns also fold their intermediate activity by default. Each mid-turn user addition, and each compaction card from compacting during the turn, stays visible and starts a separate activity section. Its header counts the section's tool calls and what they were, for example `12 tool calls: 8 reads · 4 commands`, ending with how many failed or were interrupted, if any; a section without tool calls, or in compressed history, reads **Activity**. Click the header to inspect that section while keeping its inner details in their current state. A section holding only a thinking block, or only text between tool calls, folds to that block's own row instead. For keyboard access, `/fold` toggles activity sections, tool groups, thinking and text between tool calls together: it collapses them if any are open, and expands them otherwise. Thinking that is still being written and controls waiting for your input are left as they are. Copy commands still include intermediate replies and keep their original message counts. Turn off **Automatically fold completed turns** in [Settings](../configuration/settings.md#display-preferences) to keep future completed turns and subsequently opened sessions expanded. The last turn recovered from a crash stays open.

Clicking an activity header keeps focus in the input field. A restored turn waiting for sub-agents stays open. Compressed history shows only some tool details, so these sections are all labeled **Activity**.

If a resumed conversation continues on a different model service than before, some earlier reasoning may be left out; see [Switch the current model profile](../configuration/models.md#switch-the-current-model-profile).

## Delete old sessions

Open the "Chat Sessions" window and first check the session ID, directory, and last active time. If you are unsure, resume the session to check its contents, then return to the "Chat Sessions" window. Select a session you no longer need and click "Delete".

Once you confirm deletion, the session's conversation history, rollback snapshots, diagnostic data, and other session contents are permanently deleted and cannot be recovered. Deleting a session does not undo changes the agent has already made to working directory files.

Deleting the current session also starts a new session, like `/clear`. A session open in another iCode instance cannot be deleted; close that instance first. If iCode cannot remove all of a session's files, it shows an error; close any program that may have those files open, then try again.

## Find the session ID and storage location

The current session ID appears in the "Session: ..." area in the upper-left corner of the main screen. IDs for past sessions appear in the list in the "Chat Sessions" window. Click the "Session: ..." area to open the "Session Title" dialog, where you can [copy the session ID or open its folder](#change-the-session-title).

Enter `/settings sessions`, or press **F10** and select the "Sessions" tab. The "In use" text below "Session storage root" shows the actual `sessions` directory currently in use. Each session is saved in a subdirectory named after its session ID.

If you have not customized the session storage root, the `sessions` directory defaults to `~/.chrys/sessions` on macOS and Linux, and `%APPDATA%\chrys\sessions` on Windows.

The root setting names the folder that contains `sessions`. If the configuration folder, the session storage root, or `sessions` itself is a symbolic link (or a Windows directory junction), "In use" shows the folder it leads to, which can have a different name. Sessions stored there work as usual.

On macOS, these folders can also be on an exFAT drive. exFAT drives do not keep separate permissions for each user, and neither does a drive with "Ignore ownership on this volume" selected in its Finder Get Info window. On such a drive, other accounts on this Mac can read and change the configuration, sessions, and keys that iCode stores there. To keep them from other accounts, put these folders on the Mac's internal disk, or on a drive that is not exFAT and does not have that option selected.

To back up or move your configuration, copy the whole configuration folder.

## Migrate session storage

To change where sessions are stored, set a new session storage root and migrate existing sessions to it. The new location takes effect after a restart; changing the storage location alone does not migrate existing sessions automatically.

Before starting, finish the current task and close other iCode instances, then follow these steps:

1. Enter `/settings sessions`, or press **F10** and select the "Sessions" tab, then choose a new root directory under "Session storage root".
2. Click "Migrate sessions". Check that "From" is the `sessions` directory currently in use and "To" is the `sessions` directory under the new root, then click "Migrate".
3. Wait for migration to finish, then handle any sessions that were not copied according to the results:

   | Result | Meaning | Next step |
   | --- | --- | --- |
   | Copied | The session was copied to the destination directory. | No action needed. |
   | already present | The destination already contains the same session ID, so it was skipped. iCode does not compare or update the destination contents. | If you cannot confirm which version is at the destination, use an empty destination directory and migrate again, or keep the source data. |
   | active | The session is open in an iCode instance, so it was skipped. | Close other instances and retry. If it is the current session, close settings and run `/new`; do not send a message in the new session, then migrate again. |
   | busy | The session is being saved or is in use by another operation, so it was skipped. | Wait for the operation to finish, then retry. |
   | failed | The session could not be copied. | Use the paths and reasons listed in the window to resolve permission or path issues, then retry. |

4. After handling sessions marked "active", "busy", or "failed", click "Close", then click "Migrate sessions" to migrate again (the "Migrate" button is disabled after each migration). Repeat until all sessions you want to keep have been copied.
5. Close all iCode instances and restart, then press **F1** to open the "Chat Sessions" window and confirm that important sessions appear and can be resumed.

Migration keeps the original data in the source directory; it is not deleted automatically. After confirming that migration is complete, if you need to free up space, close iCode and archive or delete the old `sessions` directory shown under "From" in the migration window. Manual deletion cannot be undone.

## Check token usage and speed

While an agent replies, the status bar above the input shows how fast text is arriving, for example `92 tok/s`. It goes away when no text has arrived for a few seconds, such as while a tool runs. It is a rough figure counted from the text on screen.

To see usage across all your sessions, type `/usage`. The window has two tabs: click one, or press **Tab**, to switch. The window's bottom border lists the keys for the tab you are on, and **Esc** closes it. The numbers are from when the window opened; type `/usage` again for newer ones.

**Overview** shows one date range at a time. Click a range at the top, or press the left and right arrow keys:

- **New**: the tokens this range added: input the provider did not serve from its cache, plus output, including thinking. This is the number to watch; cached input is listed separately because it is much larger and usually costs much less.
- **Hit**: how much of the input came from the provider's cache.
- **Trend**: new tokens over the range: hour by hour for **Today**, day by day for **This week**. For a longer range, click **Daily** or **Weekly**, or press **t**. A column with only the faint pattern has no new tokens.
- **Group**: a table of totals by model, by source (the main agent, sub-agents, workflow nodes and background tasks such as session titles) or by project. Click a button above the table or press **g** to switch. The up and down arrow keys, or a click, move between rows. On a narrow window the table shows fewer columns.
- **TPS**: tokens per second, how fast a model produced output, from its first chunk to the end of the response, across all the requests that streamed. It depends on your provider and your network, so it may differ from a provider's published speed.

**Calendar** shows one month of new tokens, with stronger colors on heavier days; the line under the calendar says what each color means, or only the lowest and highest on a narrow window. When the window has room, each day also shows its tokens. Click a day or use the arrow keys, and the panel beside the calendar shows that day's numbers and top models; a very narrow window leaves the panel out. Click the arrows beside the month, or press **[** and **]**, to change the month.

Keep in mind:

- Days follow your computer's time zone, and weeks start on Monday.
- Requests that ended before the provider reported usage count as calls but add no tokens. The line at the bottom of **Overview** says how many there were in the range, and the foot of the day panel says how many on that day.
- Deleted sessions and external ACP agents are not counted.
- Sessions saved by older versions may show fewer cache reads for DeepSeek and no usage for requests that failed partway.

## Inspect session timing

Press **F12** to open the trajectory dashboard. **Overview** summarizes time and usage; **Timeline** shows individual turns and operations; **Insights** helps explain delays; **Session data** shows the saved session. Long sessions are analyzed in the background while a loading indicator is visible. The timing analysis preserves overlapping operations, so parallel work is not counted twice on a critical path.

If iCode can't record a session's trajectory, it shows a warning that trajectory recording is off for the session. You can keep working as usual; only the dashboard and the export get nothing new from that session. Workflow runs don't show this warning. Common causes are an earlier recording of the session that hasn't stopped yet (closing the iCode windows that had the session open ends it), files in the session's `trajectory` folder that other users can read or that belong to another user, a `trajectory` folder that is a link, a trajectory key (`trajectory-hmac.key` in the iCode configuration directory) that iCode can't read, and a trajectory that iCode can't safely add to, such as one written by a newer iCode version.

If a shell command's file content comparison cannot be completed because a file is unreadable, the file-change overview marks detection as incomplete. The recorded change list may omit changes; check the affected files or your Git diff before relying on it for rollback. Files deliberately excluded from content comparison by the size or binary-file limits do not by themselves trigger this warning.

## Link file changes and model requests in session data

If you build reports from a session's files, link records by the IDs below instead of by timestamps or the order of tool calls. Records from older iCode versions don't have these IDs, and some records never get one; when an ID is missing, treat the link as unknown.

**Which tool call changed a file.** Each file change recorded in `session.json` has a `tool_operation_id`. In `trajectory/events.jsonl`, the `tool.operation.started` and `tool.operation.finished` events of that tool call have the same value as their `operation_id`, including for tools run by sub-agents. For tool calls made by the main agent, the tool call entry in the reply message's `contents`, and the matching result entry in the tool message that follows, also carry the same value as `_chrys_operation_id`. The reply message itself has a different `_chrys_operation_id`, which identifies the model request (see below). One tool call can change several files.

**Which request returned a model reply.** Each model reply message in `session.json` carries `_chrys_request_attempt_id`, the ID of the HTTP request that returned it. The message's `_chrys_operation_id` is the `operation_id` of the `model.request.*` and `model.exchange.finished` events for that reply. In `trajectory/events.jsonl`:

- `model.request.prepared` marks a request iCode is about to send, and `model.request.headers_received` marks the provider's response to it. Both carry the same `request_attempt_id`. The second also records the HTTP status and, when the provider sends one, the provider's own request ID as `provider_request_id`.
- Each automatic retry or redirect is a separate request with its own ID. A request with no `model.request.headers_received` event may have failed before it reached the provider.
- A received response doesn't mean the reply finished. To see how the reply ended, check the `model.exchange.finished` event with the same `operation_id`.

iCode also sends this ID to the provider in the `Chrys-Request-Attempt-Id` header, so you can find the request in a gateway or proxy log. If you turned on raw HTTP logging, each request's `exchange_id` in that log is the same ID.

These IDs only link records. A file change detected after a shell command keeps its own confidence: the ID doesn't prove that the command made the change. The IDs also don't count changed lines.

## Export a session's turns and model requests

To investigate failed requests or analyze token usage, export the session's turns, model requests and retries to a JSON file. Replace `<id>` with your session ID:

```bash
icode trajectory export --session <id> --format turns --out turns.json
```
