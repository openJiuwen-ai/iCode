# Configure the Shell sandbox

iCode can run an agent's Shell commands inside a sandbox. The sandbox limits what that command's process can do. This guide explains how to turn the sandbox on in your user settings, what you will see, and what stays the same.

This setting applies to Shell commands. Other tools are unchanged.

## Turn the sandbox on

The sandbox is off until you turn it on in your user settings file, `~/.chrys/settings.yaml`. On Windows, that file is `%APPDATA%\chrys\settings.yaml`.

Set `sandbox.enabled` to `true`:

```yaml
sandbox:
  enabled: true
  url: ""
  token: ""
```

- `sandbox.enabled`: `true` turns the sandbox on. `false` leaves it off.
- `sandbox.url`: Leave this empty to let iCode start its own sandbox on this computer. Set a URL only if you already have a sandbox service to connect to. When a URL is set, iCode connects to that service and does not start a sandbox itself.
- `sandbox.token`: Optional. Leave it empty when `sandbox.url` is empty. iCode then starts its own sandbox and sends a token it creates for that sandbox. Set `sandbox.token` only when `sandbox.url` points at a sandbox service that requires a token. iCode sends that token to the service.

Write these keys in the user settings file. A project's `.chrys/settings.yaml` cannot turn the sandbox on, or change the URL or token.

## While the sandbox is off

The default is off. While it is off, Shell commands behave as they do today. iCode runs them on this computer, shows output as the command runs, and can move a command to the background. See [Commands that keep running in the background](./tools.md#commands-that-keep-running-in-the-background).

## Linux, and the sandbox iCode starts

The sandbox runs on Linux only. The computer must be Linux, and it must have bubblewrap and python3 installed. The sandbox runs python3. iCode does not install bubblewrap or python3 for you.

iCode does not start this sandbox as root. If iCode itself is started as root, the sandbox refuses to run the command.

With the sandbox on, iCode starts the sandbox engine that ships with iCode. You do not install or deploy a separate product, and you do not start that engine yourself. Leave `sandbox.url` empty for this. If that sandbox fails to start, the error includes the sandbox engine's own error.

## If the sandbox cannot run the command

After the sandbox is on, iCode runs the Shell command in the sandbox. If the sandbox cannot run the command, the tool returns an error, and iCode does not run that command on the computer instead. You see the error on the tool result. That command is not started outside the sandbox.

## Approval still applies

Approval still applies before the command runs. If the command needs approval, iCode asks you, or follows the approval mode you are already using, and only then runs the command in the sandbox. If you decline, the command does not run. See [Configure approval modes](./approval.md).

## Output and background commands

Sandbox mode does not stream terminal output live. The tool result shows the command's output and exit code when the command has finished.

Sandbox mode does not move a command to the background. A command that reaches its time limit ends in the sandbox. It does not become a background task, **Ctrl+T** does not move it to the background, and it does not appear under **Background Tasks**.

Stopping the command, or the command reaching its time limit, stops that command only. Other commands still running in the same sandbox keep going.

A command that detaches into its own session is not stopped by that. A command that daemonizes, or that starts a new session with `setsid` and keeps running in the background, can keep running until you quit iCode. It can still write the workspace and use the network after the tool has already reported the command as finished or stopped.

## Files the command writes

The workspace is mounted writable inside the sandbox, so the command can read and change files there. Those changes are written in the workspace on your computer. Stopping the sandbox does not undo files the command wrote there.

The writable area is the workspace of this session: the directory where iCode was started. Starting iCode in your home directory makes the whole home directory writable inside the sandbox, including shell startup files and other files that live there. The sandbox's limit is where the command can write.

The sandbox limits the command's process. It is not a restore point for the workspace.

## Network access

The policy bundled with iCode allows host networking. A command in the sandbox can use this computer's network. This is not a network-disconnected sandbox.

This is not a read-isolated sandbox. A command can read files this user account can already read anywhere on the computer, and send what it read out over this computer's network.

## When a change takes effect

Changing these settings takes effect the next time the agent is reloaded, not in the middle of a turn.

Save the user settings file, then quit iCode and start it again so the agent reloads. Shell commands after that reload use the new settings. A turn that is already running keeps the previous settings: iCode does not move a command into or out of the sandbox partway through that turn.

## Windows and macOS

On Windows and macOS, turning the sandbox on makes Shell commands fail. The tool returns an error that the sandbox is only supported on Linux, and iCode does not run the command.

Leaving the sandbox off changes nothing. Shell commands behave as they do today.
