# Configure external ACP agents

This guide explains how to connect an external agent that supports the Agent Client Protocol (ACP) in the terminal user interface (TUI) and add it as a sub-agent of the main agent.

**External ACP agents can be used as sub-agents or workflow nodes, but not as the main chat agent**. Each time an external ACP agent is called, iCode starts an external agent process for that invocation and communicates with it over ACP. The external agent handles the task and tool calls; approval requests, execution status, progress updates, and the final result are passed over the ACP connection. When the invocation ends, iCode closes the connection and terminates the process.

## Prepare the external agent

Before configuring an external agent, confirm that it:

- Is installed on the device running iCode.
- Supports running an ACP server over standard input and output (stdio).
- Has completed any required login or authentication, or has the environment variables needed for authentication ready.
- Can start in ACP mode using an executable and arguments.

You do not need to start the ACP server in advance. When testing the connection or calling the sub-agent, iCode automatically starts the external agent process using the configuration and establishes an ACP connection over stdio.

Before the initial setup, consult the external agent's documentation and prepare the following information:

- The executable and arguments required to start ACP mode.
- The required authentication information.
- Optional session modes, models, and other configuration options.

You can specify the session mode, model, and other configuration options during the initial setup, or fill them in using the test results after a successful connection.

## Create a profile and test the connection

### Create an external ACP agent profile

1. Open the "Agent Configuration" window.
2. Click "New" and enter a name, display name, and description.
3. Change "Agent Type" to "External ACP". iCode automatically enables "Sub-Agent only".
4. Open the "ACP Settings" tab, enter the program name or path in "Executable", and add each startup argument separately.
5. Add any environment variables required for authentication. The external agent does not inherit iCode's environment, so add them here; see [Use environment variables](#use-environment-variables).
6. You can leave "Session Mode", "Model ID", and "Config Options" blank for now. Click "Test" after completing the basic setup.

Fill in "Executable" and "Arguments" separately. For example, if the startup command is:

```text
example-agent acp --model example-model
```

Enter `example-agent` in "Executable", then add `acp`, `--model`, and `example-model` as separate startup arguments. Do not enter the entire command in "Executable".

> **Note**
>
> Changing a custom agent's "Agent Type" from "Built-in" to "External ACP" clears its existing configuration except for basic information such as its name, display name, and description. To keep the original configuration, create a new external ACP agent.

### Test the connection

When testing a connection, iCode starts a temporary external agent process and checks whether it can establish an ACP session, without sending a task. After a successful connection, the report shows sections for identity, capabilities, modes, models, configuration options, and authentication methods, listing the information provided by the external agent. Information the agent does not provide may appear as "Not advertised.", "The agent did not report its identity.", or a blank entry. This does not indicate a connection failure.

When the test ends, iCode closes the temporary process.

If the connection fails, check the following in order:

- Whether the error message and the end of the standard error output indicate missing dependencies, incorrect arguments, or authentication failure.
- Whether the executable and startup arguments are correct.
- Whether the authentication information or environment variables are valid.
- Whether the configured working directory exists and is accessible.

### Example: Configure iCode as an external ACP agent

iCode itself can also provide an ACP server over stdio. Use the following configuration:

- "Executable": `icode`
- "Arguments": add `acp`

This configuration corresponds to the startup command:

```bash
icode acp
```

This command starts the iCode ACP server, offering the `Code` agent by default and using `manual` approval mode. For more configuration and usage instructions for the iCode ACP server, see [Use the iCode ACP server](../running/icode-acp.md).

## Use environment variables

The external agent does not inherit the environment iCode runs in. It starts with only a basic set of variables — `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, and `USER` on macOS and Linux; `APPDATA`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, `PATH`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`, `SYSTEMDRIVE`, `SYSTEMROOT`, `TEMP`, `USERNAME`, and `USERPROFILE` on Windows — plus the environment variables set in the agent profile. Add anything else the agent needs, such as API keys or proxy settings like `HTTPS_PROXY`, as environment variables in the profile.

In the executable, arguments, environment variable values, and working directory, you can use `{{ENV_VAR}}` to reference environment variables in the environment where iCode runs. This lets you reuse existing configuration values without entering fixed values or sensitive information directly in the agent profile. Environment variable names can contain only letters, digits, and underscores, and cannot start with a digit.

For example, if an API key is stored in the `EXTERNAL_AGENT_API_KEY` environment variable, set the value of the environment variable required by the external agent to:

```text
{{EXTERNAL_AGENT_API_KEY}}
```

iCode resolves these references when you save the profile, when you test the connection, and each time it starts the external agent. If a referenced variable does not exist or has an empty value, the profile cannot be saved, and the connection test or invocation fails.

Arguments and environment variable values can also use what a command prints, with `{{!command}}`, such as `{{!tokenprovider gettoken}}`. See [Get an API key from a command](../configuration/models.md#get-an-api-key-from-a-command) for what the command has to do. The command runs each time iCode starts the external agent, and a running agent keeps the value it started with. If the command fails the first time, the external agent does not start. The executable and working directory cannot use a command.

While the agent runs, other users of this computer may be able to see its arguments. If the agent can read a secret from an environment variable, put it there.

## Set the working directory

When "Working Directory" is blank, the external agent uses the current iCode session's working directory.

"Working Directory" accepts a relative or absolute path. Relative paths are resolved against the current session's working directory.

By default, the external agent's working directory must be within the current session's primary working directory or any additional working directory. To use a directory outside all of these locations, select "Allow cwd outside workspace". This option only relaxes directory validation; it does not change the external agent's file permissions. Before enabling it, confirm that the external agent program is trusted and check whether the target directory contains sensitive files.

## Adjust the external agent's configuration

After the first successful connection test, use the test report to refine the external agent's configuration:

- **Session Mode**: Set the external agent's session mode. Enter a mode ID listed in the test report.
- **Model ID**: Specify the model the external agent uses. Enter a model ID listed in the test report. This setting does not affect the model used by the main agent.
- **Config Options**: Set other configuration options provided by the external agent. Enter the corresponding option IDs and values from the test report.

All of these settings can be left blank. When they are blank, iCode uses the external agent's defaults. Test the connection again after changing the configuration.

By default, the connection test fails if the external agent does not support the specified session mode or configuration options. To ignore unsupported settings and continue connecting, enable "Best effort mode and config options".

"Model ID" is applied on a best-effort basis. If the external agent does not list the specified model, or answers the switch with an unsupported-method or invalid-parameters error, iCode continues using the external agent's default model. Any other error while switching the model fails the connection.

## Set the result and timeouts

"Result" determines what the main agent receives:

- **Last message segment**: Returns only the external agent's final reply segment. Suitable for most tasks.
- **Full transcript**: Returns all message segments the external agent sends during the invocation. Suitable for tasks where the complete response sequence is needed.

"Handshake Timeout (seconds)" limits how long iCode waits to start the process, complete ACP initialization, and open a session. "Idle Timeout (seconds; 0 disables)" limits how long iCode waits without receiving any activity during an invocation. Set it to `0` to allow an unlimited idle wait. The **Test** button does not use "Handshake Timeout (seconds)": it always waits at most 15 seconds, so an agent that starts more slowly can fail the test yet still work when invoked.

## Add the external ACP agent to the main agent

After saving the external agent profile, follow the steps in [Configure sub-agents for an agent](../configuration/agents.md#configure-sub-agents-for-an-agent) to add it to the main agent.

In the "Profile" dropdown, external ACP agent names have an `(ACP)` marker. Select the corresponding profile and save.
