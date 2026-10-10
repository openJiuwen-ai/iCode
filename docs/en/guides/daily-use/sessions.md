# Manage and resume sessions

A session contains conversation history and related data, such as rollback snapshots and diagnostic logs. iCode saves sessions automatically so you can resume a conversation or look up past tasks later. This guide explains how to switch, name, find, resume, and delete sessions in the terminal user interface (TUI), and how to migrate existing sessions when changing their storage location.

## Switch to a new session

When the agent is idle, enter the appropriate command in the input field, depending on whether you want to keep the current session:

| Goal | Command | Effect on the current session |
| --- | --- | --- |
| Start a new session and keep the current session available to resume later | `/new` | Kept; you can resume it from the session list |
| Start a new session without keeping the current session | `/clear` | Opens a confirmation dialog; once confirmed, the session is permanently deleted and cannot be recovered |

## Change the session title

You can change the current session's title in any of these ways:

- Click the "Session: ..." area in the upper-left corner of the main screen to open the "Session Title" dialog, enter a title, and click "Save".
- Enter `/rename` in the input field to open the "Session Title" dialog, enter a title, and click "Save".
- Enter a title directly after `/rename`, for example, `/rename Fix the login issue`.

The top of the "Session Title" dialog also shows the session ID. Click "Copy ID" to copy it, for example to resume the session later with `icode -s <session-id>`. Click "Open folder" to open the folder that holds the session's data in your file manager. "Open folder" is not shown where iCode cannot open a file manager, for example over SSH.

After you set a title manually, the new title appears in the session list and on the main screen, and iCode stops updating that session's title automatically. If [automatic session titles](../configuration/settings.md#sessions) are enabled, clearing the "Session Title" dialog and saving restores the existing automatic title and lets iCode continue updating it automatically during subsequent tasks.

## Resume an existing session

When the agent is idle, enter `/resume` in the input field to resume the most recent chat session.

To find and resume another session, press **F1**, click `f1 Sessions`, or enter `/sessions` in the input field to open the "Chat Sessions" window. It lists each session's ID, title, directory, last active time, number of conversation turns, and session data size. Each page shows up to 100 sessions, most recently active first.

- **Filter by where a session was last used**: The "TUI", "CLI", and "ACP" checkboxes above the search field show sessions last used in the TUI, in [`icode run`](../running/icode-run.md), or in an editor or other client connected through [`icode acp`](../running/icode-acp.md). Only "TUI" is checked at first. Sessions from earlier iCode versions count as TUI. iCode remembers your choice until you quit it. Hover over a session to see where it was last used.
- **Switch pages**: Click "Previous" or "Next" on the right, next to the checkboxes.
- **Sort sessions**: Sessions are sorted by last active time, newest first. Click a column header to sort the current page by that column: "Last Active", "Turns", and "Size" start in descending order, and the other columns in ascending order. Click the same header again to reverse the order.
- **Search sessions**: Enter a session ID, title, directory, or a prompt you previously entered in the search field at the bottom. The search covers only the current page.
- **Resume a session**: Select a session and click "Resume". You can also double-click it, or select it with the up and down arrow keys and press Enter. iCode closes the current conversation view, loads the selected session, and switches the working directory to the session's saved primary working directory; the current session remains saved in the list. Before resuming, check the working directory that will be restored in the list's "Directory" column. If that directory no longer exists, iCode asks you to choose another folder; see [If the working directory is deleted or moved](workspaces.md#if-the-working-directory-is-deleted-or-moved).

When you resume a long conversation, iCode shows its most recent part first so you can continue right away; earlier messages keep loading above it for a few seconds.

If a resumed conversation continues on a different model service than before, some earlier reasoning may be left out; see [Switch the current model profile](../configuration/models.md#switch-the-current-model-profile).

## Delete old sessions

Open the "Chat Sessions" window and first check the session ID, directory, and last active time. If you are unsure, resume the session to check its contents, then return to the "Chat Sessions" window. Select a session you no longer need and click "Delete".

Once you confirm deletion, the session's conversation history, rollback snapshots, diagnostic data, and other session contents are permanently deleted and cannot be recovered. Deleting a session does not undo changes the agent has already made to working directory files.

Deleting the current session also starts a new session, like `/clear`. A session open in another iCode instance cannot be deleted; close that instance first.

## Find the session ID and storage location

The current session ID appears in the "Session: ..." area in the upper-left corner of the main screen. IDs for past sessions appear in the list in the "Chat Sessions" window. Click the "Session: ..." area to open the "Session Title" dialog, where you can [copy the session ID or open its folder](#change-the-session-title).

Enter `/settings sessions`, or press **F10** and select the "Sessions" tab. The "In use" text below "Session storage root" shows the actual `sessions` directory currently in use. Each session is saved in a subdirectory named after its session ID.

If you have not customized the session storage root, the `sessions` directory defaults to `~/.chrys/sessions` on macOS and Linux, and `%APPDATA%\chrys\sessions` on Windows.

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

## Link file changes and model requests in session data

If you build reports from a session's files, link records by the IDs below instead of by timestamps or the order of tool calls. Records from older iCode versions don't have these IDs, and some records never get one; when an ID is missing, treat the link as unknown.

**Which tool call changed a file.** Each file change recorded in `session.json` has a `tool_operation_id`. In `trajectory/events.jsonl`, the `tool.operation.started` and `tool.operation.finished` events of that tool call have the same value as their `operation_id`, including for tools run by sub-agents. For tool calls made by the main agent, the tool call entry in the reply message's `contents`, and the matching result entry in the tool message that follows, also carry the same value as `_chrys_operation_id`. The reply message itself has a different `_chrys_operation_id`, which identifies the model request (see below). One tool call can change several files.

**Which request returned a model reply.** Each model reply message in `session.json` carries `_chrys_request_attempt_id`, the ID of the HTTP request that returned it. The message's `_chrys_operation_id` is the `operation_id` of the `model.request.*` and `model.exchange.finished` events for that reply. In `trajectory/events.jsonl`:

- `model.request.prepared` marks a request iCode is about to send, and `model.request.headers_received` marks the provider's response to it. Both carry the same `request_attempt_id`. The second also records the HTTP status and, when the provider sends one, the provider's own request ID as `provider_request_id`.
- Each automatic retry or redirect is a separate request with its own ID. A request with no `model.request.headers_received` event may have failed before it reached the provider.
- A received response doesn't mean the reply finished. To see how the reply ended, check the `model.exchange.finished` event with the same `operation_id`.

iCode also sends this ID to the provider in the `Chrys-Request-Attempt-Id` header, so you can find the request in a gateway or proxy log. If you turned on raw HTTP logging, each request's `exchange_id` in that log is the same ID.

These IDs only link records. A file change detected after a shell command keeps its own confidence: the ID doesn't prove that the command made the change. The IDs also don't count changed lines.
