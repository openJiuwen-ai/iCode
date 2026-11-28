# Working directory

The working directory is the default directory iCode uses for file operations and command execution. Before asking the agent to modify files, save any unsaved content to disk and record existing changes with a Git commit. This makes it easier to distinguish the agent's changes and restore the previous state if needed.

## Choose a working directory at startup

In a terminal, enter the working directory and start iCode:

```shell
cd <working-directory>
icode
```

iCode uses the directory it was started in as the working directory.

You can also specify a working directory with `-C` or `--workdir`, without first entering the working directory:

```shell
icode -C <working-directory>
```

## Change the working directory during a session

When the agent is idle, click the current working directory path at the right end of the conversation area's bottom border, or enter `/chdir` in the input field to open the "Change Directory" dialog. Browse to and select the target directory. To create a new folder, click the folder it should go in, click "New Folder", enter a name, and press Enter; the new folder is then selected. If you have used the target working directory recently, select it from the "Recent" group on the "Favorites" tab.

If you know the target path, enter the following command in the input field to switch directly:

```text
/chdir <working-directory>
```

`<working-directory>` can be an absolute path or a path relative to the current working directory. If the target does not exist or is not a directory, iCode displays an error and keeps the current working directory.

After the switch, the new working directory path appears at the right end of the conversation area's bottom border. The agent uses the new working directory as the default location for subsequent file reads, commands, and other operations.

When switching working directories, iCode reloads the current agent and loads memory and skills from the new working directory (its skills only while “Load working directory skills” is on). For configuration details, see [Configure memory](../configuration/memory.md) and [Install and use skills](../extensions/skills.md).

In an Unspecified session, changing the working directory keeps the conversation and its history. When working on an unrelated project, first enter `/new` to create a new session, then switch working directories in that session. This avoids recording the working directory change in the original session or mixing context from different projects.

## Workspaces

A workspace groups a primary folder and optional reference folders. A session can belong to a workspace or be Unspecified.

1. Click the workspace label in the bottom status bar to open the picker. From the keyboard, type `/workspace switch`, press Esc if suggestions are open, then press Enter.
2. Choose **New workspace…**, enter a name, and select the primary folder. Use **Add folder…** to include reference folders.
3. Click **Create**. iCode saves the workspace. If the conversation has messages, the confirmation shows how moving will affect the conversation and working directory. You can move the conversation, start a new session in the workspace, or cancel the switch.

After you select a folder, you return to the workspace form. iCode adds a reference folder only if it is available and neither matches, contains, nor is contained by the primary folder or another reference folder. When you create or save a workspace, iCode checks for a duplicate name or an identical set of folders. If it finds a duplicate, the form keeps your draft so you can change the name or folders.

If saving fails, the form displays the reason and keeps your draft so you can retry.

Workspaces containing the current directory come first, followed by the most recently used workspaces. The picker and command suggestions use this order. The picker opens with your current workspace or **Unspecified** highlighted. Choose a workspace in the picker or enter `/workspace switch <name>` to switch directly. Choose **Unspecified** or enter `/workspace switch Unspecified` to switch to Unspecified in the current working directory. If the conversation has messages, choose **Move this session**, **New session**, or **Cancel**. **New session** keeps the original conversation and starts another under the selected item. Selecting the current item closes the picker. If the selected workspace's primary folder is missing, iCode shows the workspace name and folder path.

Use **Edit** at the end of a row to change the name and reference folders, including when the primary folder is missing. The picker marks a missing primary folder, and the editor and status-bar tooltip mark the missing folders they list, in the theme's warning color. Restore a missing primary folder before entering the workspace, or create a workspace to use a different primary folder. Click **✕** to remove a reference folder.

Use **Delete** to remove a workspace. Its folders and conversations are kept, and its conversations appear under Unspecified. You can delete the current workspace even if its primary folder is missing. The current session stays in its working directory; if that folder is missing, iCode asks you to choose a replacement when you next send a message.

If someone edits or deletes the same workspace while you have it open, the form keeps your draft. You can copy it before closing the form. If the workspace still exists, reopen it to make changes.

The bottom status bar shows the current workspace or Unspecified. On narrower terminals, it shortens the workspace, agent, and model names and may hide their labels.

When the primary folder is available and the workspace has reference folders, the picker shows their count next to the primary folder path. The status-bar tooltip names the primary folder and lists reference folders, with a count for any remaining reference folders.

To start in a workspace, pass its name or ID with `--workspace`. `icode run` accepts the same option. It cannot be combined with `-C`.

```shell
icode --workspace <workspace-name>
```

Manage workspaces from the command line without opening a session. `new` uses the current directory as the primary folder. `<workspace>` accepts the same selectors as `--workspace`: a full ID, a case-insensitive name, or a unique ID prefix. Sessions that are already open see these changes the next time they open.

```shell
icode workspace list
icode workspace list --json
icode workspace new <name>
icode workspace rename <workspace> <new-name>
icode workspace add-folder <workspace> <path>
icode workspace remove-folder <workspace> <path>
icode workspace delete <workspace>
```

iCode opens the session in the workspace's primary folder, and the agent can also work in its reference folders. If iCode cannot find or access reference folders, it opens the workspace without them and shows one warning listing their paths. Restore access to those folders and reopen the session, or remove them from the workspace.

`/new`, `/clear` and forking keep the session in its workspace. Before `/chdir` or **Change Directory** moves you away from the primary folder, iCode asks you to confirm. It tells you if the current session can already use the target folder without changing its working directory. Moving the session makes it Unspecified. If it has messages, you can also start a new Unspecified session in the destination and keep the original session in its workspace.

If the primary folder no longer exists when you resume a workspace session, iCode asks you to choose a replacement working directory. The session opens in that directory as Unspecified. If you started iCode with `--workspace`, it reports the missing folder and exits.

If a folder link in a workspace now points somewhere else, reopen the workspace to use the new target.

If your workspace list was saved by a newer version of iCode, this version shows a warning and does not save workspace changes.

### Workspace commands in the terminal and editor

In the terminal, type a space after `/workspace` for action and name suggestions, with the current workspace marked **◦**. See [Slash commands](../../reference/tui-slash-commands.md) for how Enter treats a highlighted suggestion.

| Command | Effect |
| --- | --- |
| `/workspace` | Show usage and available actions. |
| `/workspace switch` | Open the picker in the terminal. In an ACP editor, list workspaces with their primary folders and include Unspecified. |
| `/workspace switch <name>` | Switch to the named workspace. In an ACP editor, the workspace's primary folder must be the current directory, and selecting the current workspace confirms that the session is already there. |
| `/workspace switch Unspecified` | Switch to Unspecified and keep the working directory. The terminal also accepts the label shown in the current display language. |
| `/workspace new <name>` | Create a workspace with the current directory as its primary folder. After entering the workspace, use `/workspace add-folder <path>` to add reference folders. |
| `/workspace rename <new name>` | Rename the current workspace. |
| `/workspace add-folder <path>` | Add a reference folder to the current workspace. |
| `/workspace remove-folder <path>` | Remove a reference folder from the current workspace. |
| `/workspace delete <name>` | Delete the named workspace immediately. Its folders and conversations are kept. |

**Unspecified** and its translated labels are reserved workspace names. If an existing workspace already uses one of these names, rename it with `icode workspace rename <id> <new-name>`, using the full ID from `icode workspace list --json`. Workspace names must be unique, ignoring case; if an edited workspace list contains the same name twice, iCode cannot read the list until one of them is renamed in the file.

To switch to a workspace with a different primary folder in an ACP editor, open that folder, start a new conversation, and run `/workspace switch <name>` there. An ACP editor moves the current session into a newly created workspace. In the terminal, if the conversation has messages, iCode offers to move it, start a new session, or cancel the switch. If you cancel, you can enter the saved workspace later from the workspace picker.

Folders supplied by an ACP editor apply when it creates or loads a session. An active session keeps its current folders when reopened. When loading a closed session, omitting the folders uses the workspace definition for a workspace session or the saved folders for an Unspecified session. Switching to Unspecified keeps only the current folder.

Type names and paths with spaces directly, without quotes. Relative paths start at the session's current directory. You can also enter an absolute path or use `~` for your home directory. Workspace names are matched without regard to case. Unspecified is a reserved item and cannot be deleted. Edits to the current workspace apply immediately when the session is in its primary folder. If the session uses another folder, edits apply the next time you open the session. To use a different primary folder, create a workspace.

## If the working directory is deleted or moved

If the working directory is deleted or moved outside iCode, for example in a file manager or another terminal, iCode cannot keep using it:

- A task that is already running continues. Commands and file operations that need the working directory fail, and the agent tells you that the directory no longer exists.
- When the task ends, or when you send a message, iCode shows the "Working Folder Not Found" dialog. Click "Choose Folder…" and select a folder; the "Change Directory" dialog opens at the nearest folder that still exists. iCode switches to the selected folder and keeps the current conversation.
- A message you sent stays in the input field. Send it again after switching.
- If you close the dialog without choosing a folder, it appears again the next time you send a message. You can also switch with `/chdir`, or move the folder back to where it was.

When you resume a session whose working directory no longer exists, iCode asks you to choose a folder to open the session in. If you cancel during startup with `-s`, you can retry or choose **Start new session**.

If the agent fails to load after iCode has closed the previous session, change directories or switch models to start a new conversation, or try opening the saved session again. The new conversation is Unspecified and starts in the previous folder, or in the startup folder if the previous folder no longer exists.

If you resume the current session in a replacement folder, its running background commands stay in the task list and stop when you close the session or exit iCode.

If you start iCode from a directory that no longer exists, iCode shows an error and exits. Enter an existing directory, or pass `-C` with the full path of the working directory, such as `icode -C ~/projects/my-app`.
