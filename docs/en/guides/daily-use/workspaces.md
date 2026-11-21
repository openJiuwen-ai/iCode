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

Switching working directories does not create a new session; the current conversation history is preserved. When working on an unrelated project, first enter `/new` to create a new session, then switch working directories in that session. This avoids recording the working directory change in the original session or mixing context from different projects.

## Changing a session’s workspace

This release can open registered workspaces. Workspace management comes in a later release.

When you select a workspace for an existing session, make the session Unspecified, or reopen it with `--workspace`, iCode checks the destination, saves the membership, and then loads the agent for that directory. If the save fails, the current session stays where it was. If opening fails after the save, iCode tells you where the current run is and where the saved session will reopen.

When you open a session in a workspace, iCode uses the workspace’s registered primary folder. A membership change made in another window applies the next time you open the session. If you confirm leaving the workspace while reopening a session, iCode checks the session’s current membership and the folder you confirmed before restoring it. Starting a new session with `/new` or `/clear` keeps the current working directory and inherits the latest workspace name and reference folders. If the primary folder is missing, switch directories first. Forking copies the session and inherits its workspace membership.

If your workspace list was saved by a newer version of iCode, this version shows a warning and refuses changes to the list.

## If the working directory is deleted or moved

If the working directory is deleted or moved outside iCode, for example in a file manager or another terminal, iCode cannot keep using it:

- A task that is already running continues. Commands and file operations that need the working directory fail, and the agent tells you that the directory no longer exists.
- When the task ends, or when you send a message, iCode shows the "Working Folder Not Found" dialog. Click "Choose Folder…" and select a folder; the "Change Directory" dialog opens at the nearest folder that still exists. iCode switches to the selected folder and keeps the current conversation.
- A message you sent stays in the input field. Send it again after switching.
- If you close the dialog without choosing a folder, it appears again the next time you send a message. You can also switch with `/chdir`, or move the folder back to where it was.

When you resume a session whose working directory no longer exists, iCode asks you to choose a folder to open the session in. If you cancel, the session is not resumed; if you started iCode with `-s`, iCode starts a new session instead.

If the agent fails to load after iCode has closed the previous session, select a workspace, change directories, or switch models to start a new conversation. You can also try opening the saved session again.

If you resume the current session in a replacement folder, its running background commands stay in the task list and stop when you close the session or exit iCode.

If you start iCode from a directory that no longer exists, iCode shows an error and exits. Enter an existing directory, or pass `-C` with the full path of the working directory, such as `icode -C ~/projects/my-app`.
