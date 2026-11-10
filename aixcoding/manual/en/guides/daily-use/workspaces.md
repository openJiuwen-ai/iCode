# Working directory

The working directory is the default directory iCode uses for file operations and command execution. Before asking the agent to modify files, save any unsaved content to disk and record existing changes with a Git commit. This makes it easier to distinguish the agent's changes and restore the previous state if needed.

## Choose a working directory at startup

In a terminal, enter the project directory and start iCode:

```shell
cd <project-directory>
icode
```

iCode uses the directory it was started in as the working directory.

You can also specify a working directory with `-C` or `--workdir`, without first entering the project directory:

```shell
icode -C <project-directory>
```

## Change the working directory during a session

When the agent is idle, click the current working directory path at the right end of the conversation area's bottom border, or enter `/chdir` in the input field to open the "Change Directory" dialog. Browse to and select the target directory. To create a new folder, click the folder it should go in, click "New Folder", enter a name, and press Enter; the new folder is then selected. If you have used the target working directory recently, select it from the "Recent" group on the "Favorites" tab.

If you know the target path, enter the following command in the input field to switch directly:

```text
/chdir <project-directory>
```

`<project-directory>` can be an absolute path or a path relative to the current working directory. If the target does not exist or is not a directory, iCode displays an error and keeps the current working directory.

After the switch, the new working directory path appears at the right end of the conversation area's bottom border. The agent uses the new working directory as the default location for subsequent file reads, commands, and other operations.

When switching working directories, iCode reloads the current agent and loads memory and skills from the new working directory (its skills only while “Load project skills” is on). For configuration details, see [Configure memory](../configuration/memory.md) and [Install and use skills](../extensions/skills.md).

Switching working directories does not create a new session; the current conversation history is preserved. When working on an unrelated project, first enter `/new` to create a new session, then switch working directories in that session. This avoids recording the working directory change in the original session or mixing context from different projects.

## If the working directory is deleted or moved

If the working directory is deleted or moved outside iCode, for example in a file manager or another terminal, iCode cannot keep using it:

- A task that is already running continues. Commands and file operations that need the working directory fail, and the agent tells you that the directory no longer exists.
- When the task ends, or when you send a message, iCode shows the "Working Folder Not Found" dialog. Click "Choose Folder…" and select a folder; the "Change Directory" dialog opens at the nearest folder that still exists. iCode switches to the selected folder and keeps the current conversation.
- A message you sent stays in the input field. Send it again after switching.
- If you close the dialog without choosing a folder, it appears again the next time you send a message. You can also switch with `/chdir`, or move the folder back to where it was.

When you resume a session whose working directory no longer exists, iCode asks you to choose a folder to open the session in. If you cancel, the session is not resumed; if you started iCode with `-s`, iCode starts a new session instead.

If you start iCode from a directory that no longer exists, iCode shows an error and exits. Enter an existing directory, or pass `-C` with the full path of your project directory, such as `icode -C ~/projects/my-app`.
