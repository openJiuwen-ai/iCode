# Python extensions

An extension is a Python module that gives agents more tools. Each tool is an `async def` function: the model sees its name, description, and parameters, and when the model calls the tool, iCode runs your function and passes what it returns back to the model.

An extension runs inside iCode, with your permissions: it can read and change anything you can, and it sees what the agent passes to its tools. Install only extensions you trust, as you would any Python package.

## Extensions, MCP servers, and skills

| Use | When |
| --- | --- |
| A Python extension | The tool is a few Python functions that should run on your computer, such as reading your project's version from `pyproject.toml` or searching your team's issue tracker with your own token. |
| An [MCP server](./mcp.md) | The tools already exist as an MCP server, are written in another language, or should also work in other applications. |
| A [skill](./skills.md) | The agent needs instructions, reference files, or scripts to use when a task calls for them, rather than a tool it calls with arguments. |

## Quick start

1. Save this file as `~/.chrys/extensions/hello.py` (on Windows, `%APPDATA%\chrys\extensions\hello.py`):

   ```python
   """Greetings for trying out extensions."""

   from typing import Annotated

   from chrys.extensions import Extension

   extension = Extension()


   @extension.tool(requires_approval=False)
   async def hello_greet(name: Annotated[str, "Who to greet"]) -> str:
       """Greet someone by name."""
       return f"Hello, {name}!"
   ```

2. Start iCode, enter `/agents tools`, check `hello` under "Extensions", and click "Save".
3. Ask the agent: "Use hello_greet to greet Ada." The tool call appears in the chat with its arguments and result.

An extension does not have to be one file: a larger one can be a folder of several modules. See [Install an extension](#install-an-extension).

## Install an extension

An extension is installed in one of two ways:

- **As a file or a folder**: copy the extension into the `extensions` folder of the iCode configuration directory, `~/.chrys/extensions/` on macOS and Linux or `%APPDATA%\chrys\extensions\` on Windows. An extension is either one file, `<extension id>.py`, or a folder, `<extension id>/`, whose `__init__.py` defines the extension and which can hold more modules beside it (see [Split an extension into several files](#split-an-extension-into-several-files)):

  ```text
  extensions/
  ├── hello.py             # the extension "hello"
  └── tracker/             # the extension "tracker"
      ├── __init__.py
      └── client.py
  ```

  Names starting with `_` or `.` are ignored. An extension installed this way can import only its own modules and the modules that iCode's own environment has.

- **As a Python package**, when its author published one. Install it into the environment iCode runs in. If you installed iCode with uv, name the extension's package with `--with`:

  ```bash
  uv tool install iCode-TUI --with icode-pyproject
  ```

  uv keeps only the packages named in the latest command, so name every extension you use each time. A package also installs the libraries the extension needs. The iCode offline packages cannot take extra packages: install extensions as files instead.

iCode looks for extensions only in these two places, not in project folders.

An extension's id is its file or folder name, or the name its package gives it. Ids use lowercase letters, digits, and `_`, start with a letter, and must not be the id of one of iCode's built-in tool categories, such as `shell`, `todo`, or `sleep`. If two places install an extension with the same id, neither copy loads: remove one.

## Turn an extension on for an agent

1. Enter `/agents tools` in the input field to open the current agent's "Tools" tab. To configure another agent, select it from the list on the left.
2. Under "Extensions", check the extensions this agent should have. Each one shows its description and, on the next line, where it is installed. Extensions installed twice are listed but cannot be checked.
3. Click "Save".

In the agent profile, the same choice is the top-level `extensions` list; see [extensions](../../reference/agent-profile.md#extensions). An extension stays in the profile while it is not installed, so you can turn it on before installing it. Each agent has its own list: a sub-agent or a workflow agent node gets an extension only when its own profile lists it.

iCode reads an extension's code the first time an agent that uses it starts, and listing extensions in the "Tools" tab runs none of it. After you change or update an extension, restart iCode.

If an extension cannot be loaded, the agent starts without it and iCode shows a warning that says why. To check what loaded, enter `/runtime` and open the "Tools" tab: each extension's tools are listed under the extension's id, and the extensions that did not load are under "Unavailable extensions". See [Troubleshoot](#troubleshoot).

## Approve extension tools

An extension's tools ask before they run, even when the agent's approval `default` is `auto`, unless the extension's author marked a tool as safe to run unasked (usually a tool that only reads). Your profile's approval rules come first. Every tool of an extension has the tool kind `extension.<extension id>`, so you can write, for example:

```yaml
approval:
  overrides:
    extension.tracker: auto
    extension.tracker.tracker_create_issue: require
```

With these rules, the tools of the `tracker` extension run without asking, except `tracker_create_issue`, which asks first.

Hooks match extension tools the same way, with `match.tool_kind: extension.tracker`. See [Configure approval modes](../configuration/approval.md) and [Tool kinds and names](../../reference/tool-kinds-and-names.md#extension-tool-names).

> `icode run` never asks for approval: every tool runs, extension tools included. See [Run headless tasks with icode run](../running/icode-run.md).

## Write an extension

An extension is a module with an `Extension` named `extension`. Each tool is an `async def` function added to it with `@extension.tool`. This extension, saved as `pyproject.py`, gives agents a tool that reads the project's version from `pyproject.toml`:

```python
"""Read details from the project's pyproject.toml."""

import tomllib
from typing import Annotated

from chrys.extensions import Extension, ToolContext, ToolError

extension = Extension()


@extension.tool(requires_approval=False)
async def pyproject_version(
    ctx: ToolContext,
    path: Annotated[str, "Path to pyproject.toml, relative to the working directory"] = "pyproject.toml",
) -> str:
    """Read the project's version from pyproject.toml."""
    try:
        data = tomllib.loads((ctx.cwd / path).read_text(encoding="utf-8"))
        return data["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise ToolError("pyproject.toml is missing or names no project version.", code="no_version") from exc
```

The first line of the module's docstring is the description the "Tools" tab shows. Write `@extension.tool` alone to keep every default, or pass the options below. For each tool:

- **Name**: the function's name, or `@extension.tool(name="...")`. The model calls the tool by this name. It uses letters, digits, `_`, and `-`, up to 64 characters, and must not repeat the name of an iCode tool, a sub-agent, a tool from one of the agent's MCP servers, or a tool of another extension the agent uses. Starting each name with your extension's id avoids clashes.
- **Description**: the function's docstring, or `@extension.tool(description="...")`. The model reads it to decide when to use the tool, so say what the tool does and when it helps.
- **Parameters**: each one needs a type. Write `Annotated[type, "description"]` to give the model a description of the value. The types are `str`, `int`, `float`, `bool`, `None`, `Any`, `Literal[...]`, `list[...]`, `dict[str, ...]`, unions such as `str | None`, and `TypedDict` classes for objects; the tool receives what the model sent, as plain Python values. A parameter with a default value is optional. Parameters must have names: `*args`, `**kwargs`, and positional-only parameters are not allowed.
- **Context**: one parameter typed `ToolContext`, under any name, tells the tool where and for whom it runs. The model never sees it.
- **Result**: return a string; `None` for no output; or a value JSON can represent, such as a `dict` or `list` of strings, numbers, booleans, and `None`, which the model gets as JSON. Convert other values, such as dates, to strings first. A result longer than 8,000 tokens is cut down, and the whole text is saved in the session folder; the model is told where. Set `@extension.tool(max_result_tokens=...)` for another limit, or `0` to turn this limit off; the limit on every tool result, [`tools.result.ceiling_tokens`](../../reference/settings.md) (64,000 tokens by default), still applies.
- **Errors**: `raise ToolError("message", code="...")` to tell the model what went wrong; the code labels the failure in the session's records. Any other exception tells the model only that the tool failed, so paths, addresses, and tokens in exception messages stay out of the conversation.
- **Approval**: tools ask before they run unless you write `@extension.tool(requires_approval=False)`; then the agent's approval settings decide, as they do for iCode's own read-only tools. Use it only for tools that change nothing.

iCode checks each tool when the extension is imported: a function that is not `async def`, a parameter without a type or with a type the model cannot send, or two tools with the same name stop the extension from loading, and the warning names the problem.

### Keep iCode responsive

- Tools run on iCode's own event loop: anything that blocks, such as a slow file operation, a network request with a synchronous library, or a long computation, freezes iCode while it runs. Run it with `await asyncio.to_thread(...)` instead.
- The model can call several tools at once, and those calls run at the same time. If calls to your tools share state, such as one file, guard it with an `asyncio.Lock`.
- When the user interrupts the agent, iCode cancels the calls still running, and the `await` in progress raises `asyncio.CancelledError`. Let it propagate, and clean up in `finally` blocks.

### What a tool can read from its context

| `ToolContext` field | Value |
| --- | --- |
| `cwd` | The current [working directory](../daily-use/workspaces.md); resolve relative paths from it |
| `workspace_dirs` | The working directory, then any additional working directories |
| `agent_name` | The name of the agent calling the tool |
| `session_id` | The session's id, or `None` |
| `data_dir` | A folder for the extension's own files, kept across sessions: `extension-data/<extension id>` in the iCode configuration directory |
| `session_data_dir` | A folder for the extension's files in this session, inside the session's folder, or `None` when the session has no folder. Forking a session copies this folder, so store paths relative to it |

iCode creates the two folders the first time a tool uses them, readable only by you. A context is valid only for the call it was given to: do not keep it for later calls. A tool changes iCode's state only through its result; anything else it changes is files and services, as with any program you run.

### Example: search your team's issue tracker

This extension, `tracker.py`, searches your team's issue tracker. It reads a token from an environment variable, calls the tracker's web API without blocking iCode, and turns failures into messages the model can act on. It returns a list of objects, which the model gets as JSON. Replace the address and the field names with your tracker's:

```python
"""Search issues in the team's issue tracker."""

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Literal

from chrys.extensions import Extension, ToolError

extension = Extension()

API = "https://tracker.example.com/api"


def _get(path: str) -> dict:
    token = os.environ.get("TRACKER_TOKEN")
    if not token:
        raise ToolError("TRACKER_TOKEN is not set; set it before starting iCode.", code="missing_token")
    request = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"The tracker answered HTTP {exc.code}.", code="http_error") from exc
    except OSError as exc:
        raise ToolError("The tracker cannot be reached.", code="unreachable") from exc


@extension.tool(requires_approval=False)
async def tracker_search(
    query: Annotated[str, "Words to look for in issue titles"],
    status: Annotated[Literal["open", "closed", "any"], "Which issues to include"] = "open",
) -> list[dict[str, str]]:
    """Search the team's issue tracker; returns each matching issue's id, title, and status."""
    data = await asyncio.to_thread(_get, "/issues?" + urllib.parse.urlencode({"q": query, "status": status}))
    return [{"id": i["id"], "title": i["title"], "status": i["status"]} for i in data["issues"]]
```

### Split an extension into several files

Install the extension as a folder, `<extension id>/`. Its `__init__.py` defines `extension` and the tools, and the first line of its docstring is the description the "Tools" tab shows. Import the folder's other modules with relative imports:

```python
# tracker/client.py
def search_issues(query: str) -> list[dict[str, str]]:
    ...  # the request to the tracker, as in the example above
```

```python
# tracker/__init__.py
"""Search issues in the team's issue tracker."""

import asyncio
from typing import Annotated

from chrys.extensions import Extension

from .client import search_issues

extension = Extension()


@extension.tool(requires_approval=False)
async def tracker_search(query: Annotated[str, "Words to look for in issue titles"]) -> list[dict[str, str]]:
    """Search the team's issue tracker; returns each matching issue's id, title, and status."""
    return await asyncio.to_thread(search_issues, query)
```

The folder is not on Python's import path, so `import client` and `from client import ...` do not find the module: write `from .client import ...`.

### Test an extension

Tools stay plain async functions, so tests call them directly with a test context. `chrys.extensions` comes with iCode; to run tests, install `iCode-TUI` in your development environment:

```python
import asyncio

from chrys.extensions import ToolContext

from icode_pyproject import pyproject_version


def test_reads_the_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.2.0"\n', encoding="utf-8")
    ctx = ToolContext.for_testing(tmp_path)
    assert asyncio.run(pyproject_version(ctx)) == "1.2.0"
```

`ToolContext.for_testing(cwd, data_dir=..., session_data_dir=..., agent_name=..., session_id=...)` gives a tool only the folders you pass to it: `ctx.data_dir` raises an error unless you pass `data_dir`, and `ctx.session_data_dir` is `None` unless you pass `session_data_dir`.

### Publish an extension as a package

Name the extension in your package's `pyproject.toml` under the `chrys.extensions` entry point group. The entry's name is the extension id, and its value names the module and its `extension`. This package keeps the extension's code in `icode_pyproject/__init__.py`, next to `pyproject.toml`:

```toml
[project]
name = "icode-pyproject"
version = "0.1.0"
description = "Read details from the project's pyproject.toml"
requires-python = ">=3.14"
dependencies = []

[project.entry-points."chrys.extensions"]
pyproject = "icode_pyproject:extension"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = ["iCode-TUI", "pytest"]
```

The package's `description` is what the "Tools" tab shows. iCode runs on Python 3.14 or later. List the libraries your extension needs under `dependencies`, but not `iCode-TUI`: your extension runs inside the iCode that is already installed, so list `iCode-TUI` only as a development dependency. `uv build` builds the package into `dist/`, ready to publish; in your own checkout, `uv sync` installs the package and its `dev` group, so tests can import it. Once the package is installed into iCode's environment, the extension is listed in the "Tools" tab.

## Troubleshoot

| What you see | What to check |
| --- | --- |
| The extension is missing from the "Tools" tab | The file is in the `extensions` folder of the configuration directory (not in a project folder), its name ends in `.py` or it is a folder with `__init__.py`, and its name does not start with `_` or `.`. A package must be installed into the environment iCode runs in. |
| "Skipped …" under "Extensions" | The id breaks the id rules above or repeats a built-in tool category. Rename the file or folder. |
| A warning that the agent starts without the extension | The warning gives the reason: the extension is not installed, is installed twice, failed to import (the message names the error), or has a tool whose name another tool already uses. |
| A call ends with "Error: Function failed." | The tool raised an exception other than `ToolError`. In the TUI, iCode records it in `logs/debug.log` in the configuration directory. Raise `ToolError` for failures the model should read. |
| A change to the extension has no effect | Restart iCode: each extension is imported only once while iCode runs. |
