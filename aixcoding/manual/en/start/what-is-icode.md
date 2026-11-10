# What is iCode?

**iCode is a platform for running and building agents.**

iCode lets you interact with agents in natural language to read and edit files, run shell commands, search codebases, and perform other tasks. Complex tasks can also be delegated to sub-agents.

The main way to interact with iCode is through its terminal user interface (TUI), which brings conversations, task execution, and tool calls together in one place. Tool calls appear as cards so you can see what the agent is doing. iCode also supports headless execution in scripts, integration with editors through the Agent Client Protocol (ACP), and running the TUI in a browser.

## Key features

* **Flexible combinations of agents and models.** Switch agents and models with a click in the interface or a quick command, and choose different combinations for different tasks within the same session.

* **Configurable agents.** Configure an agent's instructions, model, tools, skills, MCP servers, and sub-agents through the TUI, or edit its YAML profile directly, without changing the iCode source code.

## Model support

The agent determines how to work, while the model provides reasoning and generation capabilities.

Agents and models are independent in iCode, so you can combine them to suit different tasks. iCode includes built-in agents but does not include model profiles or API credentials. Before first use, create a model profile and connect it to a model service.

iCode currently supports model providers including Anthropic, OpenAI, DeepSeek, and GLM. You can also connect to other model services, including locally hosted models, through OpenAI API-compatible endpoints.

## Using iCode

iCode offers several ways to work, depending on your needs:

- **TUI**: The main iCode interface, suited to interactive tasks. View tool calls, handle approval requests, and switch agents and model profiles within a session.
- **Headless command-line interface (CLI)**: Run non-interactive tasks with `icode run`, suited to one-off tasks, scripts, and automated workflows.
- **Browser interface**: [Run the TUI in a browser](../guides/running/icode-serve.md) when you need an interactive interface outside the terminal.
- **ACP server**: Use iCode as an agent backend for [editors or other development tools that support ACP](../guides/running/icode-acp.md).

For first-time use, start with the TUI. An interactive session is a quick way to learn how agents, models, and tools work together.

When you are ready to start, read [Getting started with iCode](getting-started.md) for installation, model configuration, and your first run.
