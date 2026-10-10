# Use the iCode ACP server

Editors and development tools that support the Agent Client Protocol (ACP) can connect to iCode to use iCode agents in the client. The ACP client provides the interface, while iCode communicates with it over standard input and output (stdio), runs agents, executes tools, and saves sessions.

Before you begin, install iCode and configure at least one model. For preparation steps, see [Getting started with iCode](../../start/getting-started.md) and [Configure models](../configuration/models.md).

## Connect to iCode

In the ACP client's "Add agent" or similar settings, enter the following:

| Field | Value |
| --- | --- |
| Executable | `icode` |
| Arguments | `acp` |

This is equivalent to the client starting:

```shell
icode acp
```

If the client provides only a single launch command field, enter the full command, `icode acp`.

After saving the configuration, verify the connection:

1. Start or connect to iCode in the client.
2. Create a session and send a message.
3. Confirm that the client displays a reply from iCode. A reply means the connection is configured successfully.

## Configure startup options

`icode acp` supports the following startup options. If the client has separate executable and arguments fields, append these options after `acp`.

| Option | Default | Purpose |
| --- | --- | --- |
| `-a`, `--agent <agent>` | `Code` | Specify the built-in or custom agent for new sessions, by name, display name, or ID. Restored sessions keep the agent they were saved with. To create a custom agent, see [Configure agents](../configuration/agents.md). |
| `--approval <mode>` | `manual` | Set the initial approval mode for every session this ACP server creates or restores: `manual`, `auto`, or `bypass`. The `bypass` approval mode skips approval and executes tool calls directly; use it with care. For the rules of each mode, see [Configure approval modes](../configuration/approval.md). |
| `-C`, `--workdir <directory>` | None | Provide a default working directory for sessions where the client does not send one. |
| `--ask-user-timeout <seconds>` | No time limit | Limit how long to wait for the client to answer an agent's question. Omit this option or set it to `0` or a negative value to wait without a time limit. |

## Working directories and sessions

When creating or restoring a session, the client can pass the project's working directory to iCode. The directory supplied by the client must be an existing absolute path. If the client does not provide a working directory, iCode uses the directory specified by `--workdir <project-directory>`. If both are provided, the client's working directory takes precedence.

iCode checks `--workdir` at startup. The specified directory must exist even if the client will later provide a working directory; otherwise, iCode cannot start.

Session data is saved in iCode's session storage directory, rather than the working directory. To find the actual location, see [Find the session ID and storage location](../daily-use/sessions.md#find-the-session-id-and-storage-location). iCode records the working directory when a session is created. Listing sessions through an ACP client shows only sessions that match the current working directory. iCode refuses to restore a session if the working directory does not match.

Sessions used through an ACP client also appear in the TUI's "Chat Sessions" window once you check "ACP" there. See [Resume an existing session](../daily-use/sessions.md#resume-an-existing-session).

## Interact through the client

iCode sends agent replies and tool call status to the client. When the client cancels a task, iCode interrupts the current task.

If the current model supports image input, a prompt can include images in PNG, JPEG, GIF, or WebP format. A prompt with an image in another format is rejected, and the client receives an error.

If the client supports session management, a closed session can still be restored in the same working directory. Deleting a session removes the session record saved by iCode, and the session can no longer be restored in iCode.

To use the question feature through an ACP client, the client must support iCode's `_chrys/request_input` extension request (shown as `chrys/request_input` in ACP SDKs that add the underscore prefix automatically). iCode uses this request to send the agent's questions to the client and receive the user's answers; a client that does not support it cannot complete tasks that require user answers. Use `--ask-user-timeout` to limit how long iCode waits for an answer.

Approval requests sent to the client wait without a time limit by default. To decline requests that stay unanswered, set a human approval timeout; see [Set the human approval timeout](../configuration/approval.md#set-the-human-approval-timeout).

If the client supports switching approval modes or models, it can change the corresponding settings for the current session. These switches, like the initial mode set with `--approval`, are not written to iCode's local configuration. If the client uses iCode's extension requests to change settings or agent and model profiles, however, those changes are written to iCode's local configuration. The next time the ACP server starts, the initial approval mode still comes only from `--approval`; other initial settings are determined by the local configuration and the startup arguments for that launch. Model profiles and available agents remain managed through iCode's local configuration.
