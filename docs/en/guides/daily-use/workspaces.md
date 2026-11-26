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

## Workspaces

A workspace groups a primary folder and reference folders under a name. A session either belongs to a workspace or is Unspecified: an Unspecified session uses only its working directory, as sessions always have. This release can open workspaces that are already registered; creating and managing workspaces comes in a later release.

To start in a workspace, pass its name or ID with `--workspace`. `icode run` accepts the same option. It cannot be combined with `-C`.

```shell
icode --workspace <workspace-name>
```

iCode opens the session in the workspace's primary folder, and the agent can also work in its reference folders. A reference folder that does not exist when the workspace opens is left out, and iCode warns once.

`/new`, `/clear` and forking keep the session in its workspace. Switching with `/chdir` to any other folder, a reference folder included, leaves the workspace. If the session already has messages, iCode asks whether to move this session to the new folder or start a new session there.

If the primary folder no longer exists when you resume a workspace session, iCode asks you to choose a replacement folder, and the session continues there as Unspecified. If you started iCode with `--workspace`, it reports the missing folder and exits.

If a folder link in a workspace now points somewhere else, reopen the workspace to use the new target.

If your workspace list was saved by a newer version of iCode, this version shows a warning and does not save workspace changes.

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
