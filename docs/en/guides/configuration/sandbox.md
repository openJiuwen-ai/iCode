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
  network: guarded
```

- `sandbox.enabled`: `true` turns the sandbox on. `false` leaves it off.
- `sandbox.url`: Leave this empty to let iCode start its own sandbox on this computer. Set a URL only if you already have a sandbox service to connect to. When a URL is set, iCode connects to that service and does not start a sandbox itself.
- `sandbox.token`: Optional. Leave it empty when `sandbox.url` is empty. iCode then starts its own sandbox and sends a token it creates for that sandbox. Set `sandbox.token` only when `sandbox.url` points at a sandbox service that requires a token. iCode sends that token to the service.
- `sandbox.network`: `guarded`, `host`, or `offline`. `guarded` is the default. See [Network access](#network-access).

Write these keys in the user settings file. A project's `.chrys/settings.yaml` cannot turn the sandbox on, or change the URL, token, or network.

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

Stopping the command, or the command reaching its time limit, stops that command only. That includes a process the command started in its own session. Other commands still running in the same sandbox keep going.

## Files the command writes

The workspace is mounted writable inside the sandbox, so the command can read and change files there. Those changes are written in the workspace on your computer. Stopping the sandbox does not undo files the command wrote there.

The writable area is the workspace of this session: the directory where iCode was started. Starting iCode in your home directory makes the rest of that directory writable. These paths stay read-only when they are real files or directories inside that writable area: `.ssh`, `.bashrc`, `.bash_profile`, `.profile`, `.zshrc`, `.zprofile`, `.zshenv`, `.zlogin`, `.gitconfig`, `.git-credentials`, `.gnupg`, `.pypirc`, `.npmrc`, `.wgetrc`, `.curlrc`, `.azure`, `.config/gh`, `.config/git`, `.config/gcloud`, `.config/glab`, `.config/configstore`, `.local/share/keyrings`, `.aws`, `.kube`, `.docker`, and `.netrc`. A symbolic link with one of those names is left as it is. The sandbox's limit is where the command can write.

The sandbox limits the command's process. It is not a restore point for the workspace.

## Network access

`sandbox.network` chooses what network the command has.

- `guarded`, the default: the command is not on this computer's network. Programs that use `HTTP_PROXY` can reach public addresses, so package installs still work. The proxy refuses this computer, private networks, and the cloud metadata address `169.254.169.254`. A program that ignores the proxy has no network.
- `host`: the command uses this computer's network. It can reach services on this computer and addresses on the internet, including a cloud instance's metadata address.
- `offline`: the command has no network. It cannot reach services on this computer, the internet, or the proxy.

With `host`, this is not a read-isolated sandbox. The sandbox does not mount the rest of your home directory or `/root`. A command can still read other files this account can already read, such as files under `/usr` and `/etc`, and send what it read out over this computer's network.

## Memory and processes

When this computer lets the sandbox create a cgroup, a command is limited to 4 GiB of memory and 1024 processes. Reaching either limit stops that command. If it cannot create that cgroup, the command still runs, and those limits are not applied.

One file the command creates cannot grow past 4 GiB. If the filesystem that holds the workspace has less than 1 GiB free, the sandbox stops commands and does not start another until space is free. A write that is already in flight can still cross that line.

Commands also cannot call system calls that only change the kernel, mount filesystems, or trace other processes.

## When a change takes effect

Changing these settings takes effect the next time the agent is reloaded, not in the middle of a turn.

Save the user settings file, then quit iCode and start it again so the agent reloads. Shell commands after that reload use the new settings. A turn that is already running keeps the previous settings: iCode does not move a command into or out of the sandbox partway through that turn.

## Windows and macOS

On Windows and macOS, turning the sandbox on makes Shell commands fail. The tool returns an error that the sandbox is only supported on Linux, and iCode does not run the command.

Leaving the sandbox off changes nothing. Shell commands behave as they do today.
