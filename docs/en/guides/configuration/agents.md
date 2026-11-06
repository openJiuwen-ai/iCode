# Configure agents

iCode uses agent profiles to define an agent's name, instructions, model, available tools, sub-agents, and other settings. This guide explains how to create, clone, edit, reset, switch, and delete agent profiles in the terminal user interface (TUI), and how to configure models and sub-agents for an agent.

## Open the agent configuration window

Open the **Agent Configuration** window in any of these ways:

- Press **F2**.
- Click `f2 Agents` at the bottom of the interface.
- Enter `/agents` in the input field, press **Space** or **Enter** to display the list of tabs, then select the tab to open.

The **Agent Configuration** window lists existing agent profiles on the left. Tabs on the right show the settings for the selected agent.

Add a tab name after `/agents` to open that tab directly:

| Command | Tab opened |
| --- | --- |
| `/agents basic` | Basic |
| `/agents instructions` | Instructions |
| `/agents tools` | Tools |
| `/agents sub-agents` | Sub-Agents |
| `/agents skills` | Skills |
| `/agents mcp` | MCP |
| `/agents memory` | Memory |
| `/agents compaction` | Compaction |

While a task is running, the **Agent Configuration** window opens in read-only mode. You cannot edit or save profiles. Wait for the task to finish before changing the configuration.

## Manage agent profiles

### Create an agent profile

The following steps create an agent of the **Built-in** type. To connect an external program as a sub-agent, see [Configure external ACP agents](../extensions/external-acp-agents.md).

1. Click **New**.

2. Configure the agent's basic information on the **Basic** tab:
    - Enter a name, display name, and description. All three fields are required. **Name** identifies the agent profile; **Display Name** appears in agent selection lists and the main interface. **Name** must start with a letter or digit and contain only English letters, digits, hyphens (`-`), and underscores (`_`); it must be unique, ignoring case.
    - Select **Sub-Agent only** if appropriate for your use case. For the distinction between main agents and sub-agents, see [Main agents and sub-agents](#main-agents-and-sub-agents).
    - Set the model the agent uses as needed. By default, it uses the active model profile. To bind a specific model profile, see [Set the model an agent uses](#set-the-model-an-agent-uses).

3. On the **Instructions** tab, enter system instructions to define the agent's behavior. System instructions cannot be empty.

4. Configure the other tabs as needed:
    - **Tools**: Select the built-in tools and the [Python extensions](../extensions/python-extensions.md) the agent can call. See [Configure tools](./tools.md).
    - **Sub-Agents**: Add sub-agents this agent can call. See [Configure sub-agents for an agent](#configure-sub-agents-for-an-agent).
    - **Skills**: Configure where the agent loads skills from. See [Install and use skills](../extensions/skills.md).
    - **MCP**: Connect MCP servers and configure the MCP tools the agent can use. See [Connect MCP servers](../extensions/mcp.md).
    - **Memory**: Add files or folders to load into context automatically. See [Configure memory](./memory.md).
    - **Compaction**: Adjust how context is compacted as the conversation grows. New agent profiles use the default context compaction settings, which usually do not need changing. To change them, see [Configure context compaction](./compaction.md).

5. Click **Save**.

### Clone an agent profile

Use cloning to create a new profile based on an existing agent. Cloning creates an independent copy without changing the original profile. When you clone a built-in agent, iCode creates a custom agent copy that you can edit independently.

1. Select the agent to clone in the list on the left.
2. Click **Clone**.
3. iCode copies the agent's configuration, generates unique values for its name and display name, and selects the new copy.
4. Review and adjust the copy's name, display name, and other settings as needed.
5. Click **Save** to finish creating it.

**Notes:**

- Clicking **Clone** creates a temporary copy for editing in the current configuration window. The profile is only created and retained when you click **Save**.
- After editing a profile, click **Save** to apply the changes. Changes left unsaved when you close the agent configuration window do not take effect.

### Edit an agent profile

1. Select the agent to edit in the list on the left.
2. Adjust its settings on the relevant tabs.
3. Click **Save** to apply the changes.

**Notes:**

- You cannot change **Name** or **Agent Type** for the built-in agents included with iCode, such as Code Agent and Q&A Agent. Other settings can be adjusted as needed.
- After editing a profile, click **Save** to apply the changes. Changes left unsaved when you close the agent configuration window do not take effect.
- If you edit the agent used by the current session, the saved configuration takes effect for subsequent requests.

### Delete a custom agent profile

Deleting a custom agent also removes its configuration stored on your machine. This cannot be undone in iCode. Built-in agents cannot be deleted.

Before deleting a custom agent, ensure that:

- At least one usable main agent remains.
- Any other agents that reference this agent as a sub-agent have had those references removed.

To delete a custom agent:

1. Select the custom agent to delete in the list on the left.
2. Click **Delete**.
3. Check the agent name in the confirmation window, then confirm deletion.

If you delete the agent currently in use, iCode automatically switches to another available main agent when you close the configuration window.

### Reset a built-in agent profile

The built-in agents included with iCode cannot be deleted.

When you edit and save a built-in agent, iCode creates a user profile file with the same name, such as `Code.yaml`, in the [user agent profile directory](../../reference/agent-profile.md#user-agent-profile-directory). This file stores both your changes and the other settings in effect at that time. For example, if you only add an MCP server, the saved profile contains that server and retains the current values of the other settings.

At startup, iCode gives precedence to the user profile file with the same name, instead of merging user and built-in settings field by field. As a result, after an iCode upgrade, updates to built-in defaults such as agent instructions are not automatically applied to that agent.

To adopt the built-in defaults from the current version, reset the built-in agent. Resetting preserves only skills, MCP server, memory, and [extension](../extensions/python-extensions.md) settings. Instructions, compaction, and all other settings return to the current version's built-in defaults.

To reset an agent:

1. Select the built-in agent to reset in the list on the left.
2. Click **Reset**, then click **Reset** again in the confirmation window.
3. If **Save** is enabled, click it to apply the reset. If it is disabled, the current configuration already matches the reset result described above, so there is nothing to save.

Reset changes remain pending in the configuration window until saved. Closing the window discards unsaved changes.

After a reset, if the user profile file with the same name no longer exists, future upgrades use the new built-in configuration directly. If the file remains because it preserves custom skills, MCP server, memory, or extension settings, you still need to reset the agent again after future upgrades to adopt updated built-in defaults.

## Configure models and sub-agents

### Set the model an agent uses

New agents use the active model profile by default. When you switch the active model, the agent's model changes with it.

To make an agent always use a specific model profile:

1. Select the agent in the list on the left, then open the **Basic** tab.
2. Clear **Use active model profile** (the label also shows the current profile).
3. Select the model profile to bind from the dropdown. To add or edit a model profile, see [Configure models](./models.md).
4. Click **Save**, then close the configuration window.

If the current main agent is bound to a specific model profile, the model profile name in the status bar is grayed out. Clicking it does not switch models; instead, a notice tells you which model the agent is bound to.

To switch models through the status bar again, select **Use active model profile** on the agent's **Basic** tab, click **Save**, then close the configuration window.

### Main agents and sub-agents

A main agent interacts directly with the user. A sub-agent is called by another agent to handle delegated tasks.

An agent profile can be used both as a main agent and as a sub-agent called by other agents. Selecting **Sub-Agent only** prevents the agent from being selected as a main agent; it can only be called by other agents.

The Code Agent and Q&A Agent included with iCode can serve as main agents. Explore and General are sub-agents only. External ACP agents are also sub-agents only. To configure them, see [Configure external ACP agents](../extensions/external-acp-agents.md).

### Configure sub-agents for an agent

After you add sub-agents to an agent, it can call them as needed to handle parts of a task.

In the agent configuration window:

1. Select the agent to configure in the list on the left.
2. Open the **Sub-Agents** tab.
3. Click **+ Add** and select the sub-agent to add.
4. Set the sub-agent's **Tool Name**, **Tool Description**, and **Max Concurrency**.
5. Adjust **Max Total Concurrency** at the top of the tab as needed.
6. Click **Save**.

These settings work as follows:

- **Tool Name** and **Tool Description** help the agent identify and use a sub-agent. When left blank, they default to the sub-agent's own profile name and description, respectively.
- **Max Concurrency** limits simultaneous calls to a single sub-agent. **Max Total Concurrency** limits simultaneous calls across all sub-agents.

During a task, the agent uses each sub-agent's tool name and description to decide whether to delegate work to it. You can also request a specific sub-agent in the input field, for example: `Call <tool name> to complete <task>`. A successful result from the corresponding sub-agent tool confirms that the configuration has taken effect.

A sub-agent returns its final reply to the agent that called it. Text it writes between tool calls appears on its card but is not returned. The card shows each piece of text once the sub-agent has finished writing it. A model request that fails before any of its text has appeared on the card is retried as in the main conversation; once some has appeared, the card keeps it and shows the error. In `icode run`, ACP clients and workflows, a sub-agent's text is not shown as it is written, so text it has already written does not by itself stop such a retry. If the final reply has no text, all the text the sub-agent wrote is returned, in order. If the sub-agent fails, its card shows the reason. An external ACP agent returns what its **Result** setting selects; see [Set the result and timeouts](../extensions/external-acp-agents.md#set-the-result-and-timeouts). Click **View details** on the card to see everything the sub-agent did: the view opens at its newest content and keeps showing new tool calls and text as they arrive while you stay at the bottom; scroll up to read earlier content and it stays where you are until you scroll back to the bottom.

**Notes:**

- After adding, removing, or editing sub-agent settings, you must click **Save** for the changes to take effect. Closing the agent configuration window without saving discards the changes.
- An agent cannot list itself as a sub-agent, and the same sub-agent cannot be added more than once to a profile.

## Switch the current agent

You can switch the current agent through the input field or the status bar:

- **Through the input field**: Enter `#` to display a list of available agents above the input field. Click an agent, or select one with the up and down arrow keys and press **Enter** to confirm.
- **Through the status bar**: Click the agent name in the status bar above the input field to open the agent picker, then select the agent to use.

After switching, iCode immediately uses the selected agent for subsequent requests. The current agent is grayed out in the list, with a hollow circle before its name. The list shows only profiles that can serve as main agents.
