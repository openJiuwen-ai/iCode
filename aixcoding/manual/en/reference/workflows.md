# Workflow reference

This page provides workflow authors with the Python API, data and scheduling rules, execution environments, interaction behavior, and run records.

## Workflow files

### File definition and validation

Workflows are defined in `.py` files. This is a minimal complete example that returns its input unchanged:

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Echo")


def echo(value: WorkflowValue) -> WorkflowValue:
    return value


node = wf.python("echo", echo)  # Register the node
wf.start(node)                 # Declare the single top-level start
wf.output(node)                # Declare at least one output; it may also be the start
workflow = wf.build()          # Validate, build, and assign to the module-level workflow variable
```

The module must define a top-level variable named `workflow` containing the `Workflow` object returned by calling `build()` on a `WorkflowBuilder`, as in `workflow = wf.build()` above. You can choose a different name for the builder variable `wf`, but the entry variable must be named `workflow`. Do not place the `workflow` assignment inside a function or an `if __name__ == "__main__":` block.

Syntax errors, exceptions in top-level code, build validation failures, or a missing valid `workflow` object prevent loading. For structural validation rules, see [`WorkflowBuilder.build()`](#workflowbuilderbuild).

To check a workflow before running it, use [`icode workflow validate`](#icode-workflow-validate): it reports each problem with its file and line.

### File discovery

A workflow is either a single `.py` file or a workflow folder: a folder holding an entry file with exactly the folder's name, such as `code-review/code-review.py`. A workflow's ID is its file name without `.py`, or its folder name, independent of the `WorkflowBuilder` title. iCode looks for workflows in these locations:

| Location | Source |
| --- | --- |
| `.chrys/workflows/` in the current working directory | `project` |
| Under the iCode configuration directory: `%APPDATA%\chrys\workflows\` on Windows; `~/.chrys/workflows/` on Linux and macOS | `global` |
| Built-in examples shipped with iCode | `builtin` |

When several workflows have the same ID, the first one that can be read wins, in this order:

| Priority | Workflow |
| --- | --- |
| 1 | Project workflow folder |
| 2 | Project `.py` file |
| 3 | Global workflow folder |
| 4 | Global `.py` file |
| 5 | Built-in workflow |

Project workflows are discovered only in `.chrys/workflows/` under the current working directory; parent directories are not searched. Each location is scanned only for the `.py` files and workflow folders directly inside it. Names starting with `.` or `_` are ignored. A folder without an entry file of exactly its name, such as `data/` or `venv/`, is not a workflow and is ignored, and so is any folder inside a workflow folder. Built-in workflows are single files only. The global location reserves the folder name `sdk` for iCode's own files.

User workflow files cannot be symbolic links, must pass ownership checks, and have a source size limit of 4 MiB. A workflow folder is skipped when the folder or anything in it is a symbolic link or a Windows directory junction, when it holds something other than regular files and folders (such as a named pipe), or when it holds more than 1000 files and folders, more than 512 MiB in total, or is nested more than 16 levels deep. Names starting with `.` (such as `.venv` and `.git`) and the compiled copies Python keeps in `__pycache__` folders (such as `helpers.cpython-312.pyc`) are not checked and do not count toward these limits; every other file does, binaries and data files included. Workflows that cannot be read are skipped with a reported reason, and the next workflow with the same ID is used instead.

### Workflow folders

Split a workflow into several files by putting it in a folder:

```text
.chrys/workflows/
  code-review/
    code-review.py        # Entry file: same name as the folder
    steps.py              # Imported with `from steps import ...`
    prompts/summary.md    # Read relative to __file__
```

- The folder comes first on `sys.path`, as a single file's directory does: `import steps`, `from steps import check`, and modules in subfolders (`from helpers.git import diff`, with or without `__init__.py`) all work. Relative imports such as `from .steps import check` do not, because the entry file is not part of a package.
- Read other files relative to `__file__`, for example `Path(__file__).parent / "prompts" / "summary.md"`. Relative paths such as `open("summary.md")` are relative to the workspace, not to the folder.
- Do not name the entry file or another file in the folder after a standard library or installed module, such as `json.py` or `pkgutil.py`. Python may then load the module instead of your file, or your file instead of the module, and processes the workflow starts with `multiprocessing` can fail to start.
- Do not import the entry file from another file by its name: that runs the entry file again as a separate module.
- iCode keeps the workflow's compiled Python files in its own folder under the configuration directory, so a workflow run creates no `__pycache__` folders next to your files and does not use the compiled copies of your files found in them. This also holds for Python processes the workflow starts, unless it starts them without its environment variables or with `-E` or `-I`: those use `__pycache__` folders as usual, including compiled copies that confirmation does not check.

Moving `review.py` to `review/review.py` changes the workflow's path, so it must be confirmed again, and workflow sessions created for the old file cannot run it; start a new session.

### Trust confirmation

Loading a custom workflow for the first time requires trust confirmation:

- **TUI**: Selecting a workflow opens the “Trust workflow” dialog. Review the source and declared execution environment, then click “Trust” to continue or “Cancel” to leave it unloaded.
- **CLI**: Add `--trust` to `icode workflow run`. For example, run `.chrys/workflows/echo.py` from the project root with:

```shell
icode workflow run echo --trust --input "Hello"
```

The confirmation is saved, so later runs can omit `--trust`. Confirmation is required again if the workflow's source, the built workflow definition, or environment information changes, including the selected interpreter's path, version, platform, or the workflow SDK supplied by iCode. For a single `.py` file, the source is that file; other Python files it imports are not checked. For a workflow folder, the source is every file in the folder except names starting with `.` and the compiled copies Python keeps in `__pycache__` folders, so adding, changing, or removing any of them requires confirming again. The check does not cover changes to installed dependencies.

Files are checked when the workflow is previewed and when a run starts, and Python reads them again as it loads them. A change made in between, or to a file the workflow reads only while it runs, is not caught. After changing files in a workflow folder, reopen the workflow to preview and confirm it.

Review the source before confirming trust. Workflow code can read and write files or start programs with your user permissions. Previewing a trusted workflow executes its top-level code, and running it loads the module again. Top-level operations such as file writes or network requests can therefore occur before any node starts and can occur more than once. Put task operations inside node functions, leaving only imports, function definitions, and workflow construction at the top level.

## Python API

`chrys.workflows` provides these APIs:

| Name | Purpose |
| --- | --- |
| `WorkflowBuilder` | Create top-level nodes, edges, loops, the start node, and outputs |
| `BuilderScope` | Create nodes and edges inside a loop body |
| `NodeHandle` | A node reference returned by builder methods; `node_id` is the node name |
| `Workflow` | The result of `build()`, assigned to the top-level `workflow` variable |
| `WorkflowValue` | Text and structured data passed between nodes |
| `SourceValue` | A source and its result, received by a combine function |
| `NodeContext` | Progress reporting and user questions for Python nodes |
| `Question` | A question for `NodeContext.ask`, with optional single- or multi-select options |
| `Option` | One option of a `Question` |
| `Answer` | The user's answer to one `Question` |
| `Retry` | Node retry count and interval |

### `WorkflowBuilder`

Create a top-level workflow:

```python
WorkflowBuilder(title: str, *, description: str | None = None)
```

`title` must be a nonempty string; `description` is optional. Neither is passed as node input or agent instructions.

`WorkflowBuilder` inherits from `BuilderScope` and can directly call its [`python()`](#builderscopepython), [`agent()`](#builderscopeagent), [`edge()`](#builderscopeedge), [`chain()`](#builderscopechain), [`switch()`](#builderscopeswitch), and [`join()`](#builderscopejoin) methods.

#### `WorkflowBuilder.start`

```python
start(node: NodeHandle) -> None
```

Declare the single top-level start node. This can be called only once. `node` must be a top-level node handle created by this builder.

#### `WorkflowBuilder.output`

```python
output(node: NodeHandle) -> None
```

Collect the result of `node` as a workflow output. `node` must be a top-level node handle created by this builder. Call this method multiple times to collect results from different nodes; the same node cannot be declared twice.

Declaring a node as an output does not stop further execution. Its downstream nodes and other runnable branches continue, and the workflow waits for those paths to finish. For collection order and skipped nodes, see [Input scheduling and output collection](#input-scheduling-and-output-collection).

#### `WorkflowBuilder.build`

```python
build() -> Workflow
```

Validate and freeze the workflow, returning a `Workflow`. A builder can be successfully built only once.

Declare one top-level start node with `start()` and at least one output node with `output()`. Every top-level node must be reachable from the top-level start along the edges. Every node inside a loop body must likewise be reachable from that body's entry node.

An edge from the same source to the same destination cannot be declared twice. A node cannot connect to itself, and edges cannot directly connect nodes inside a loop body to nodes outside it. Edges cannot form cycles such as `A → B → A`. To repeat a group of nodes, define a loop with `loop()` instead of connecting a downstream node back to an upstream node.

#### `WorkflowBuilder.loop`

Wrap a group of nodes in a loop that repeats until a stop condition is met or the iteration limit is reached. The returned `NodeHandle` represents the whole loop node and connects it to upstream and downstream nodes at the top level.

```python
loop(name, body, until, max_iterations, on_exhausted="continue") -> NodeHandle
```

| Parameter | Rules |
| --- | --- |
| `name` | Loop node name, unique across the entire workflow; cannot be empty, start with `join:`, or contain `->` |
| `body` | Function that builds the loop body, with signature `(scope: BuilderScope) -> tuple[NodeHandle, NodeHandle]`. The builder supplies `scope` to create nodes and edges inside the body. Return `(entry, exit)`, the entry and exit node handles |
| `until` | Synchronous stop condition with signature `(value: WorkflowValue) -> bool`. `value` is the current iteration's exit output; `True` ends the loop and `False` means the stop condition has not been met |
| `max_iterations` | Required maximum iteration count; an integer of at least 1 |
| `on_exhausted` | What to do when the iteration limit is reached without satisfying the stop condition: `"continue"` (default) or `"fail"`, as described below |

`body` and `until` each take one positional argument. The names `scope` and `value` above are illustrative; you can name them differently.

**Building the loop body**

`body` runs once when `loop()` is called. The returned entry and exit must belong to that loop body and may be the same node.

`body` creates nodes and edges through the supplied [`BuilderScope`](#builderscope) instance. A loop body cannot contain another loop.

**Execution and stopping rules**

At runtime, each iteration executes the previously built body:

1. The first iteration passes the loop node's input to `entry`. Later iterations pass the previous `exit` output to `entry`.
2. Wait for the iteration's body to finish and obtain the `exit` output. If no branch passes a result to the exit in this iteration—for example, if the only edge to it has a `when` condition that returns `False`—the exit node is skipped and the loop fails with `loop_no_value`.
3. Pass the exit value to `until`. If it returns `True`, end the loop with this value as the loop node's output. If it returns `False` and the iteration limit has not been reached, start the next iteration.

`until` is checked after each iteration, so the loop always runs at least once. If `max_iterations` is reached and `until` still returns `False`, `on_exhausted` determines the result:

| Value | Behavior |
| --- | --- |
| `"continue"` | End the loop, use the last iteration's exit value as its output, and continue downstream |
| `"fail"` | End the workflow run with `loop_exhausted` |

### `BuilderScope`

`BuilderScope` provides instance methods for creating nodes and edges, inherited by `WorkflowBuilder`. At the top level, use the `WorkflowBuilder` instance. Inside a loop body, use the `BuilderScope` instance supplied to the `body` callback of `loop()`; you do not need to construct one yourself.

**Node naming rules**: Names must be unique across the entire workflow, cannot be empty, cannot start with `join:`, and cannot contain `->`.

#### `BuilderScope.python`

Define a node that executes a Python function.

```python
python(name, fn, *, timeout=300.0, retry=None) -> NodeHandle
```

| Parameter | Meaning |
| --- | --- |
| `name` | Node name |
| `fn` | Python function to execute; see signature and return value requirements below |
| `timeout` | Maximum seconds per attempt, default 300; a finite positive number or `None`, where `None` means no deadline |
| `retry` | A [`Retry`](#retry) instance; when omitted, the node runs only once |

`fn` can be synchronous or asynchronous, defined with `async def`.

`fn` takes one or two positional arguments. The first is the node input, a [`WorkflowValue`](#workflowvalue). The optional second argument is a [`NodeContext`](#nodecontext), used to report progress or ask the user a question. Examples of both signatures:

```python
def transform(value: WorkflowValue) -> str:
    return value.text.strip()


def report(value: WorkflowValue, ctx: NodeContext) -> WorkflowValue:
    ctx.emit("Preparing result")
    return value
```

The positional parameters of `fn` cannot have default values. `fn` also cannot declare `*args`, `**kwargs`, or required keyword-only parameters.

`fn` must return a string or a `WorkflowValue`. iCode converts a returned string to `WorkflowValue(text=returned_string, data=None)`.

Relative file paths in Python nodes are resolved against the workflow session's working directory.

#### `BuilderScope.agent`

Define a node that executes an agent task.

```python
agent(
    name,
    *,
    profile,
    model=None,
    instructions_suffix=None,
    timeout=None,
    retry=None,
) -> NodeHandle
```

| Parameter | Meaning |
| --- | --- |
| `name` | Node name |
| `profile` | Required ID, name, or display name of an existing agent profile; using the ID is recommended. List profiles with `icode agent list` |
| `model` | Optional iCode model profile ID or name, listed by `icode model list`; when omitted, the model selection rules below apply |
| `instructions_suffix` | Optional string with additional instructions for this node |
| `timeout` | Maximum seconds per attempt, unlimited by default; a finite positive number or `None` |
| `retry` | When omitted, at most three attempts with zero backoff; not all errors are retried automatically |

##### Agent profile matching

`profile` is matched in this order:

1. ID.
2. Exact name.
3. Exact display name.
4. Case-insensitive name.
5. Case-insensitive display name.

A unique match at any step selects that profile. No match proceeds to the next step; multiple matches cause an immediate error. If all steps fail to match, an error is also reported.

##### Model selection

`model` is matched against iCode model profiles in this order:

1. Exact ID.
2. Exact name.
3. Case-insensitive name.

A unique match at any step selects that profile. No match proceeds to the next step; multiple matches cause an immediate error. If all steps fail to match, an error is also reported.

Regular iCode agents select a model in this priority order:

1. The `model` argument to `agent()`.
2. The model bound to the agent profile.
3. The workflow's default model. In the TUI, select it under “Run Settings”. In the CLI, loading an existing workflow session with `icode workflow run <workflow-id> --session <session-id>` retains that session's saved model selection. If no workflow default has been selected, the [default model configured in iCode](settings.md#agents-models-and-requests) is used.

An explicit `model` that does not exist or is unavailable causes an error. If the agent's bound model does not exist, selection falls back to the workflow default. The workflow cannot start without an available model.

##### Input and output

The agent uses the upstream `WorkflowValue.text` as its task input. `WorkflowValue.data` is not automatically added to the prompt.

The node returns a `WorkflowValue` with the agent's response text in `text` and `None` in `data`. Regular iCode agents prefer the final response text. If the final response has no text, the text emitted during this execution is concatenated in order as the node output, which may include commentary around tool calls.

##### Agent profile support

Regular iCode agent nodes support profile settings as follows (✓ supported, ✗ unsupported). For field formats and configuration rules, see the [Agent profile reference](agent-profile.md).

| Setting | Supported | Behavior in a node |
| --- | :---: | --- |
| `instructions` | ✓ | Uses the profile's instructions, with the `instructions_suffix` argument to `agent()` appended |
| `model` | ✓ | Participates in model selection |
| `tools.builtins` | ✓ | Loads built-in tools; CLI runs do not provide the user-question tool |
| `tools.shell_filter` | ✓ | Applies Shell command filtering configuration |
| `tools.mcp` | ✓ | Loads MCP tools and applies allowed-tool scope, progressive disclosure, and result limits |
| `approval` | ✓ | Uses the same [tool approval rules](../guides/configuration/approval.md) as Chat mode |
| `compaction` | ✓ | Applies context compaction configuration |
| `sub_agents` | ✓ | Registers sub-agent tools. A failed sub-agent ends at once and returns its error to the node's model, without offering “Retry” or “Abort” |
| `skills` | ✓ | Discovers and loads skills as in Chat mode, and provides `load_skill` |
| `memory` | ✓ | Loads the configured memory files and directories |

Each agent node has its own context. Edges pass only node outputs, not the full conversation history or tool-call records.

##### External ACP agents

`profile` can directly reference a configured ACP agent, even one marked as sub-agent-only. [Configure an external ACP agent in the TUI](../guides/extensions/external-acp-agents.md), or [create an agent YAML file containing an `acp` configuration](agent-profile.md#acp).

ACP nodes differ in these ways:

- Omitting `model` uses the remote model settings in the ACP profile, not the workflow default.
- An explicit `model` takes an iCode model profile ID or name. iCode uses only its `model_id` to request a remote model switch over ACP; the profile's other fields do not apply. If the remote agent does not list the model, or rejects the switch because the method is unsupported or the parameters are invalid, execution continues with the remote default model.
- `instructions_suffix` is appended to the user prompt sent to the external agent.
- The node output is determined by “Result” in the external agent profile ([`acp.result_mode`](agent-profile.md#acp)), with options “Last message segment” and “Full transcript”. See [Set the result and timeouts](../guides/extensions/external-acp-agents.md#set-the-result-and-timeouts).
- The external agent executes its own tools. Only operations for which it sends ACP permission requests enter iCode's approval process.

#### `BuilderScope.edge`

Connect a source node to a destination, optionally with a condition for passing the result.

```python
edge(src: NodeHandle, dst: NodeHandle, *, when=None) -> None
```

`src` is the source node and `dst` is the destination. Once the source finishes, its output is passed along the edge to the destination.

Omitting `when` passes the output unconditionally. If supplied, `when` is a synchronous function that receives the source output as a `WorkflowValue`: `True` passes it on, while `False` does not. A source can connect to multiple destinations. Edge conditions are evaluated independently, so multiple true conditions pass the result to multiple destinations.

A condition function must take exactly one positional argument with no default value. Prefer conditions based only on the input, without modifying external state. An exception during condition evaluation prevents the source node from completing normally; it is not treated as a false condition.

`src` and `dst` must be created by the instance on which `edge()` is called. For a `WorkflowBuilder`, both must be top-level nodes in that workflow; for a loop body's `BuilderScope`, both must belong to that body.

#### `BuilderScope.chain`

Connect multiple nodes in order.

```python
chain(*nodes: NodeHandle) -> None
```

Pass at least two nodes. `chain(a, b, c)` is equivalent to `edge(a, b)` followed by `edge(b, c)`.

Every node in `nodes` must be created by the instance on which `chain()` is called.

#### `BuilderScope.switch`

Select one downstream branch from a source node based on conditions.

```python
switch(
    src: NodeHandle,
    cases: Sequence[tuple[Callable[[WorkflowValue], bool], NodeHandle]],
    default: NodeHandle,
) -> None
```

`src` is the source node. `cases` is an ordered sequence whose entries must be `(condition_function, destination_node)` pairs, with each destination a `NodeHandle`. `default` is the required fallback destination.

Conditions are evaluated in `cases` order. The first condition that returns `True` selects its branch; later conditions are not evaluated. If none match, the default branch is selected.

Each condition must be a synchronous function taking one positional argument with no default value. It receives the source output as a `WorkflowValue` and returns a Boolean. Prefer conditions based only on the input, without modifying external state. An exception during condition evaluation prevents the source node from completing normally; it is not treated as a false condition.

A source node can have only one `switch()`. A `switch()` and ordinary `edge()` calls can coexist on the same source.

`src`, every destination in `cases`, and `default` must be created by the instance on which `switch()` is called.

#### `BuilderScope.join`

Combine results from multiple source nodes and pass them to a destination.

```python
join(
    sources: Sequence[NodeHandle],
    dst: NodeHandle,
    *,
    combine=None,
) -> None
```

`sources` is a nonempty sequence of `NodeHandle` objects, and `dst` is the destination's `NodeHandle`. `join()` creates a join node named `join:<destination-name>` and connects it to `dst`. `dst` cannot have any other direct incoming edges.

Every node in `sources` and `dst` must be created by the instance on which `join()` is called.

Without `combine`, the [default merge rules](#input-scheduling-and-output-collection) apply.

If supplied, `combine` must be a synchronous single-argument function taking `list[SourceValue]` and returning a string or `WorkflowValue`. The list follows the declaration order of `sources`, not completion order. Skipped sources or sources not selected by a condition do not appear in it.

The supplied `combine` is called whenever at least one source provides input, including when there is only one input. If no source provides input, both the join node and the destination are skipped, and `combine` is not called.

### `NodeHandle`

```python
class NodeHandle:
    node_id: str
```

A node reference used to declare the start, outputs, and edges. Use handles returned by `python()`, `agent()`, or `loop()`. Instances created directly with `NodeHandle("name")` cannot be used to build a workflow.

The sole public attribute, `node_id`, is the node name and cannot be modified.

### `Workflow`

```python
class Workflow:
    definition: WorkflowDefinition  # Read-only property
```

A built workflow returned by `WorkflowBuilder.build()`. Assign it to the workflow file's top-level `workflow` variable for iCode to load and run.

Its `definition` attribute holds the built workflow definition, including the title, description, start, output nodes, nodes, and edges. This `WorkflowDefinition` instance is created automatically by `build()`; you do not need to define it yourself.

### `WorkflowValue`

```python
class WorkflowValue:
    text: str
    data: dict | list | str | int | float | bool | None = None
```

A value passed between nodes. `text` holds text content; `data` holds structured data, defaults to `None`, and supports only these JSON values:

- Strings (`str`), integers (`int`), finite floating-point numbers (`float`), Booleans (`bool`), and `None`.
- Lists (`list`) and dictionaries (`dict`) with string keys. Nesting is allowed, but list elements and dictionary values must also meet these type requirements.

Non-JSON types such as sets (`set`, `frozenset`), tuples (`tuple`), and bytes (`bytes`, `bytearray`) are unsupported, as are ordinary custom objects, non-string dictionary keys, `NaN`, and positive or negative infinity. For size limits, see [Data and output limits](#data-and-output-limits).

The framework supplies each node's input. When a Python node or `combine` returns a string, it is automatically converted to a `WorkflowValue`. To return structured data, construct `WorkflowValue(text="...", data=...)` yourself; `text` is required.

### `SourceValue`

```python
class SourceValue:
    node_id: str
    activation_id: str
    value: WorkflowValue
```

A source item received by the `combine` function. The framework creates it; you do not need to construct it yourself.

- `node_id`: The source node's name.
- `activation_id`: The identifier for this execution of the source node within the workflow run. A loop-body node has a different identifier in each iteration.
- `value`: The `WorkflowValue` produced by that execution.

### `NodeContext`

A Python node function can receive a `NodeContext` as its second positional argument to send text messages or ask the user questions.

A new `NodeContext` is created for every function invocation, including each loop iteration and retry after failure. Do not store task data that needs to persist across invocations in `NodeContext`; pass data between nodes through return values.

#### `NodeContext.emit`

```python
emit(text: str) -> None
```

Send any text message, such as progress or a notice. The TUI shows it on the “Output” tab; the CLI writes it to stderr in text mode and suppresses it in `--json` mode. Messages do not affect the node output returned by the function.

#### `NodeContext.ask`

```python
async ask(prompt: str) -> str
async ask(prompt: Question) -> Answer
async ask(prompt: list[Question] | tuple[Question, ...]) -> tuple[Answer, ...]
```

Ask the user and wait for the answer. Call it only from an asynchronous node function, using `await ctx.ask(...)`. The return value follows the argument:

- A string, which must not be blank, asks one open question and returns the typed answer as text.
- A [`Question`](#question) can offer options to pick one or several from, and returns one [`Answer`](#answer).
- A list or tuple of 1 to 5 questions shows them together in one dialog, one tab per question, and returns a tuple of answers in question order. A one-element list also returns a tuple.

Any other argument raises `TypeError`; an empty list or more than five questions raise `ValueError`.

The TUI displays the questions in the same dialog as the agent question tool. The CLI does not support interaction; the call fails with `ask_unavailable`.

Time spent waiting for an answer counts toward the node timeout. `ask()` does not use the agent question tool's timeout setting. To wait indefinitely for the user, set `timeout=None` when registering the Python node to remove its deadline.

The run's `workflows/<run_id>/events.jsonl` under the [session directory](../guides/daily-use/sessions.md#find-the-session-id-and-storage-location) keeps a text summary of the questions and the answers, cut to 512 characters, rather than the full questions.

### `Question`

```python
Question(
    question: str,
    header: str = "",
    options: list[Option | str] | tuple[Option | str, ...] = (),
    multi_select: bool = False,
)
```

One question for `ctx.ask()`. The field names match the agent question tool.

- `question`: The question text, rendered as Markdown. It must not be blank. Apart from the message size limit it has no length limit, so a question can carry a whole draft for review.
- `header`: A short label for the question's tab, at most 64 characters. Without one, the tab shows the question number. Tabs, and therefore headers, show only when several questions are asked together.
- `options`: Up to 8 options. A string is shorthand for `Option(label=...)`. Without options, the question takes a typed answer only.
- `multi_select`: `True` lets the user pick several options, and needs at least one option.

Option labels have surrounding whitespace removed and must be unique within a question. Every string must be valid Unicode, without unpaired surrogates. Wrong types raise `TypeError` and other violations raise `ValueError`, both when the `Question` or `Option` is created, so the traceback points at your code.

Whether or not a question has options, the user can type an answer of their own, or add a note to a selection.

### `Option`

```python
Option(label: str, description: str = "")
```

- `label`: What the user picks, and what comes back in `Answer.selected`. At most 200 characters.
- `description`: One line shown under the label. At most 500 characters.

### `Answer`

```python
class Answer:
    selected: tuple[str, ...] = ()
    text: str = ""
    answered: bool       # read-only property
    choice: str | None   # read-only property
```

The user's answer to one `Question`.

- `selected`: The offered labels the answer names, in option order. Clicking an option and typing its label exactly (case-sensitive) give the same answer.
- `text`: Everything else the user typed: an answer of their own that matches no label, or a note given with a selection.
- `answered`: `False` only when the user skipped the question.
- `choice`: The one selected label, or `None` when nothing is selected. It raises `ValueError` when several are selected; read `selected` for multi-select questions.

Every answer has one of three shapes:

| `selected` | `text` | Meaning |
| --- | --- | --- |
| Not empty | A note, or `""` | Offered labels picked or typed; at most one for a single-select question |
| `()` | Not empty | An answer of the user's own that matches no label |
| `()` | `""` | Skipped |

How the dialog behaves:

- With one single-select question, clicking an option submits at once, so type any note before clicking. Text submitted without clicking an option is the whole answer.
- With several questions, clicking an option of a single-select question moves on to the next unanswered question, or to the “Submit” tab. Multi-select and typed answers move on with “Answer & Next” (“Answer & Review” on the last question). The “Submit” tab lists all answers and sends them with “Submit answers”, or with “Submit anyway” while some questions are unanswered. Submitting with nothing answered needs a second, confirming press.
- The dialog cannot be dismissed with Esc. The question waits until it is answered, or until its attempt or run ends.

`Answer` is a local result, not a node output: return a string or a `WorkflowValue`. To pass a selection downstream, put it in both `text`, which agent nodes and the default join read, and `data`, for Python nodes:

```python
async def pick_areas(value: WorkflowValue, ctx: NodeContext) -> WorkflowValue:
    answer = await ctx.ask(Question("Which areas?", options=["API", "Storage", "UI"], multi_select=True))
    text = "\n".join(part for part in (", ".join(answer.selected), answer.text) if part)
    return WorkflowValue(text=text, data={"selected": list(answer.selected), "text": answer.text})
```

### `Retry`

```python
Retry(max_attempts: int, backoff: float = 0.0)
```

`max_attempts` is the total number of attempts including the first, and must be at least 1. `backoff` is a fixed, finite, nonnegative interval in seconds.

Pass it through the `retry` argument of `python()` or `agent()`. Whether an error is retried also depends on its type; see [Timeouts and retries](#timeouts-and-retries).

## Data and scheduling rules

### Data and output limits

Workflows are intended to pass task text and bounded JSON data. Exceeding the limits below causes failure; values are not automatically truncated before being passed downstream. Depth, value count, and string length limits apply to Python node and custom `combine` return values. Return values that cannot be serialized also cause failure.

| Limit | Maximum | Scope |
| --- | --- | --- |
| One message sent between processes | 16 MiB | The entire UTF-8-encoded message, including the value and information added by the framework |
| Nesting depth of `data` | 64 levels | `data` itself is level 0; each level of list elements or dictionary values adds 1 |
| Total values in `data` | 200,000 | Includes all nested content; each list and dictionary itself counts as 1. Dictionary keys do not count. See the example below |
| One string value in `text` or `data` | 4,194,304 characters | Measured in characters; the full transport message must still meet the byte limit |
| All source data passed to a custom `combine` | 12 MiB total | All sources before merging, including each source's `text`, `data`, and identifiers, measured as UTF-8-encoded JSON |

This `data` contains 7 values:

```python
data = {                          # Outer dictionary: 1
    "names": ["Alice", "Bob"],     # List itself + two strings: 3
    "scores": [90, 80],            # List itself + two numbers: 3
}
```

The keys `"names"` and `"scores"` do not count.

Progress messages and diagnostic output have separate limits. An “attempt” below is one execution of a node; counts reset on retry.

| Output method | Limit per attempt | Effect of exceeding the limit |
| --- | --- | --- |
| Messages sent by `ctx.emit()` | At most 200 in any rolling 1-second window; at most 10,000 total; at most 8 MiB of UTF-8-encoded message text in total. All three limits apply | The attempt fails |
| Direct writes to `sys.stdout` / `sys.stderr` in Python nodes, such as `print()` output, excluding `ctx.emit()` | Only the first 64 KiB combined, measured as UTF-8, is retained | Excess output is truncated; this alone does not fail the node or affect its return value |

CLI text mode displays received `ctx.emit()` messages on stderr, but these still count toward the separate `ctx.emit()` limits, not the node's 64 KiB output allowance.

### Input scheduling and output collection

Multiple `wf.edge()` calls can connect multiple upstream nodes to the same node. The destination waits until every incoming edge has determined whether it will pass a result, then runs or skips based on the results actually received.

All node types use these default input merge rules, except join nodes with a custom `combine`:

| Number of sources that actually provide results | Behavior |
| --- | --- |
| Zero | Skip the node; successors that depend only on it are also skipped |
| One | Pass through that source's `WorkflowValue` unchanged, retaining `data` |
| Multiple | Merge their `text` in incoming-edge declaration order as the node input, with `data=None` |

When merging multiple sources by default, each source node's name becomes a heading:

```text
## first_node
First output

## second_node
Second output
```

Ready branches can be scheduled in parallel. Each run currently permits at most four concurrent agent attempts; further agent nodes wait for a slot.

The workflow collects output nodes in `output()` declaration order, including only completed nodes that have a result. Skipped outputs do not produce empty placeholders, so a workflow can complete without any final outputs.

### Timeouts and retries

For Python and agent nodes, `timeout` limits each attempt's duration, including time spent waiting for questions or approvals. The timer resets for each retry.

The entire batch of outgoing-edge condition evaluations for a node, including `switch` checks, shares a 30-second limit. Each loop stop-condition evaluation and custom `combine` call has its own 30-second limit. These calculations are independent of the node's `timeout`. Loading and building the workflow source has a separate combined limit of 60 seconds.

Automatic retry after a node failure depends on the error type and the node's `Retry` configuration:

| Situation | Automatic retry condition |
| --- | --- |
| A Python node's function raises an exception or times out | `Retry.max_attempts` has not been reached |
| A non-ACP agent node's model request hits a temporary error, such as a dropped connection, a rate limit or a request timeout | The request is first retried within the attempt, as in chat. If it still fails, the node is retried while `Retry.max_attempts` has not been reached |
| A non-ACP agent node's model request is too long for the model's context window | As in chat, with context compaction on, the context is compacted and the request is sent once more within the attempt, unless the error names a smaller limit than the model profile's. If it still fails, the node is not retried automatically |
| A non-ACP agent node reaches its `timeout` | `Retry.max_attempts` has not been reached |
| An external ACP agent node loses its connection, stops responding, reports an error, or reaches its `timeout` | `Retry.max_attempts` has not been reached. iCode cannot tell which errors an external agent reports are permanent, so it retries them all. It does not retry an agent that cannot be started, one that still cannot be reached after the attempt retried the connection several times, a rejected configuration, or an answer the agent refuses, cuts short or leaves empty |
| Errors that retrying cannot fix, such as an exhausted quota or plan, or an external agent that needs you to log in; also failed condition or combine calculations, unserializable or oversized return values, user questions in an environment that does not support them, and similar errors | Not retried automatically |

If retrying could make the model provider run one of its own tools a second time, such as an MCP server or shell the provider runs, the node is not retried automatically. Provider-run search and code execution are safe to repeat and don't stop retries; neither do iCode's own file, Shell or MCP tools.

Once an agent node's model has started writing its reply, a request that fails is not sent again within the same attempt: the attempt fails, and the node is retried as described in this section.

When a non-ACP agent's attempt fails or times out, the retry continues from its conversation so far: tool calls that already finished do not run again, and node details show the new attempt's transcript continuing the previous one. A reply the failure cut off is written again, and the new attempt's transcript shows only the new reply. An external ACP agent starts a new session with the same input, and the new attempt's transcript shows only that session. If the agent finished and a later step of the node failed, such as an outgoing condition, a retry runs the agent again from its input.

`Retry(max_attempts=1)` turns off node retries only: a failed model request is still retried within the attempt, unless the model had already started writing its reply.

In the TUI, a failed node that is not retried automatically, either because retries are exhausted or because they do not apply, enters `awaiting_retry`. The user can inspect diagnostics and retry, or cancel the entire run. Manual retry applies only to nodes in this state in the current run.

The CLI does not support manual retry. If a node fails and cannot retry automatically, the entire run ends. The workflow is considered failed even if some outputs already exist.

Retrying a Python node reruns the entire function, including when it failed while evaluating outgoing conditions after the function had completed. If the loop node itself fails while evaluating its stop condition or outgoing conditions, manual retry reevaluates only the failed condition, retaining completed loop progress.

Retries can repeat file writes, external requests, and other operations, so ensure these operations can safely be repeated. A timeout or cancellation does not undo operations that have already occurred, nor does it guarantee that a running synchronous Python function stops immediately.

## Execution environment

By default, workflows use the Python interpreter running iCode. You can instead specify an existing virtual environment or Python executable in the workflow file. Python 3.9 and later are supported.

This example uses an existing `.venv` in the project root, with the workflow file in the project's `.chrys/workflows/` directory:

```python
# /// script
# [tool.chrys]
# python = "../../.venv"
# ///
```

Keep the `#` prefixes and `# ///` markers: iCode reads the configuration from these comments. `[tool.chrys]` is the iCode-specific configuration section. Relative `python` paths are resolved against the directory containing the workflow file. For a workflow folder, that is the folder itself: a `.venv` in the project root is `../../../.venv`, and one inside the folder is `.venv`.

`python` can also point directly to a Python executable, such as `"/opt/homebrew/bin/python3.12"`. iCode does not search `PATH` for commands. For an environment created with uv, point to its `.venv`.

iCode starts the interpreter with the `PYTHONPYCACHEPREFIX` environment variable set to its own folder for compiled files. A wrapper script that runs Python with `-E` or `-I` ignores it, and the workflow then fails to load.

iCode does not install dependencies automatically. Install the workflow's third-party packages in the selected environment beforehand. iCode supplies `chrys.workflows` at runtime, so it needs no separate installation.

Script metadata also supports these two top-level fields, placed before `[tool.chrys]`:

| Field | Rules |
| --- | --- |
| `requires-python` | Optional Python version constraint string, such as `">=3.10,<3.13"`. iCode checks the selected interpreter against it and refuses to load if it does not match; it does not find or install another version automatically. Only comma-separated `~=`, `==`, `!=`, `<=`, `>=`, `<`, and `>` clauses with numeric release versions are supported: `.*` works only with `==` and `!=`, and `~=` needs at least two version components. Any other clause also refuses loading |
| `dependencies` | Optional array of dependency strings. A nonempty array requires `[tool.chrys] python`, otherwise loading is refused even if the default environment already has those packages. With a user-supplied interpreter, iCode neither installs these dependencies nor verifies that installed versions match the declaration |

For example, this declaration requires a project virtual environment using Python 3.10 through 3.12, with `requests` installed beforehand by the author:

```python
# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["requests"]
# [tool.chrys]
# python = "../../.venv"
# ///
```

## Sessions, runs, and records

Workflow sessions are separate from chat sessions and save multiple runs of the same workflow in the same workspace. Each start creates a new `run_id` using the input supplied for that run; previous conversations are not automatically included.

### Session settings

The TUI's “Start Workflow” dialog provides “Run Settings”. Whether the working directory can change depends on whether the session has any runs and on the workflow's source:

| Session and workflow source | Working directory rules |
| --- | --- |
| New session with no runs, using a built-in or global workflow | The working directory can be changed in Run Settings |
| New session with no runs, using a project workflow | Fixed to the project directory where the workflow was discovered: the directory containing `.chrys/workflows/` |
| Session with existing runs | The workspace is fixed; create a new session to change directories or workflows |

Run Settings also offers a default model selector when the workflow contains iCode agent nodes.

Workflow sessions do not save their own approval mode. They share the TUI's current approval mode with Chat mode, including sessions reopened later. CLI runs always use `bypass`.

### Run outcomes and recovery

Reopening a session lets you view history or start a new run, but does not resume from the last interrupted node. Cancelled or interrupted runs must start over. For nodes awaiting manual retry in the current run, see [Timeouts and retries](#timeouts-and-retries).

In the result object from `icode workflow run --json`, `outcome` indicates the run outcome. Local records also store it in the run-end event in `workflows/<run_id>/events.jsonl` under the [session directory](../guides/daily-use/sessions.md#find-the-session-id-and-storage-location).

If the process terminates abnormally before finalizing a run, restoring the session records `outcome=orphaned` for that run. The workflow run view displays “orphaned”.

| Run `outcome` | Meaning |
| --- | --- |
| `completed` | Run completed |
| `node_failed` | A node failed |
| `loop_exhausted` | A loop exhausted its iterations with `on_exhausted="fail"` |
| `cancelled` | Run cancelled; `reason=deadline_exceeded` indicates the overall run timeout |
| `worker_lost` | The process executing the workflow was lost |
| `storage_failed` | Run records could not be written |
| `orphaned` | Recovery found that the original process had terminated without properly finalizing the run |

File changes made by tool calls are recorded against the run, but do not assume that all side effects of arbitrary Python code or external programs can be tracked or undone. Cancellation, node retries, and new runs do not automatically roll back files.

### View and export records

Use the run tabs in the TUI to switch between records within a session; after quitting, switch to Workflow mode and reopen it from the session list. Node details retain separate loop iterations and retry attempts. Viewing history does not execute workflow source code.

The `workflows/<run_id>/` directory under the [session directory](../guides/daily-use/sessions.md#find-the-session-id-and-storage-location) stores the source snapshot, graph definition, input, final outputs, run events, and node records. Agent transcripts also belong to that run. These files preserve records; they do not support resuming a workflow run.

Run and node usage and elapsed time help assess costs; ACP usage depends on what the remote agent reports. To export analysis data, use:

```shell
icode trajectory export --session <session-id> --format json --out workflow.json
icode trajectory export --session <session-id> --format perfetto --out workflow.perfetto.json
```

Replace `<session-id>` with the workflow session ID. Analysis exports for workflow sessions support only JSON and Perfetto.

## Lifecycle hooks

Workflows use configured external hooks; see the [Hooks reference](hooks.md) for the configuration format. A workflow session can contain multiple runs. `session_*` events mark the session's first use for execution, its first run after restoration, or the end of its use. `workflow_run_*` events mark the start and end of each run. Node executions do not independently trigger the session events below.

A run must first pass startup checks for source code, execution environment, agent profiles, and other prerequisites before triggering session-start or session-restored events and the run-start event. Start requests that fail these checks do not trigger these events.

| Event | When it fires | Additional fields |
| --- | --- | --- |
| `session_start` | When a new session starts its first run, before `workflow_run_start` | None |
| `session_restored` | When a session loaded from saved records starts its first new run in the current iCode process, before `workflow_run_start` | `restored_session_id`: ID of the restored session |
| `session_end` | When a session that has started a run in the current iCode process is deleted, iCode closes normally, or the CLI run command ends normally | None |
| `workflow_run_start` | After each run passes startup checks, before nodes execute | `run_id`, `input_text` |
| `workflow_run_end` | After each run is finalized and its final outcome is determined, including failure and cancellation | `run_id`, `outcome`, `reason` |

Within each iCode process, a session triggers `session_start` or `session_restored` only on its first run. Each accepted run triggers one `workflow_run_start` and one `workflow_run_end`; node retries do not trigger them again. End events are not guaranteed if the process crashes or is forcibly terminated.

All these events include `session_kind: "workflow"`, `workflow_id`, `session_id`, and `cwd`. `profile` is an empty string because a workflow session is not bound to a single agent. For other shared fields, see [Hook base fields](hooks.md#base-fields).

`workflow_run_start` and `workflow_run_end` are for notifications and recording. If a hook is configured as blocking, iCode waits for it to finish but does not use its returned action to prevent the run or grant tool permissions. `action: block` is ignored, and `on_error: block` is treated as a warning on hook failure.

## Command line

### `icode workflow list`

List workflows according to the [file discovery](#file-discovery) rules without executing workflow code. Custom workflow titles come from previously saved trust confirmations. `icode workflow` without a command does the same.

```shell
icode workflow list [--json]
```

Text mode shows `ID`, `Source`, `Title`, and `Path`, using `-` for an unknown title; for a workflow folder, `Path` is its entry file. With `--json`, the output is an object with a `workflows` array; each entry contains `id`, `source`, `layout` (`file` for a single `.py` file, `package` for a workflow folder), `title`, and `path` (the entry file), with an empty string for an unknown title. In either mode, warnings about skipped files go to stderr. Use `-h` / `--help` for help.

### `icode workflow validate`

Check a workflow before running it. The command loads the workflow as a run would and reports each problem with its file, line, and column, like a compiler.

```shell
icode workflow validate <path> [--json]
```

`<path>` is a workflow `.py` file or a [workflow folder](#workflow-folders), relative to the current directory. It does not have to be in a location iCode searches. For a folder, `code-review`, `code-review/`, and `code-review/code-review.py` all check the folder.

Validation runs the workflow's top-level code, as loading it in the TUI does, but it runs no node and calls no model. It does not ask for or record trust confirmation, so a later `icode workflow run` still needs `--trust` when the workflow is new or has changed.

The checks run in this order and stop at the first one that fails:

| Stage | What it checks |
| --- | --- |
| `resolve` | The path names a workflow: a `.py` file or a folder holding an entry file with the folder's exact name, not a link, with a name iCode loads |
| `read` | Every file can be read, is within the size limits, and the entry file is UTF-8 |
| `metadata` | The `# /// script` block, if any, is valid (see [Execution environment](#execution-environment)) |
| `environment` | The Python interpreter the workflow asks for exists and starts |
| `load` | Every `.py` file of a folder compiles, the top-level code runs, and `build()` succeeds |
| `graph` | The built graph is valid; warns about suspicious structure |
| `bindings` | Every agent node's profile and model are available on this computer |

#### Text report

Each problem is shown as `file:line:column: error: message [code]`, followed by the source line with the place marked. `note:` lines show how the code was reached, and a `help:` line suggests a fix. Text the workflow printed while loading follows under `captured output (load):`. The last line is the result:

```text
./.chrys/workflows/code-review/steps.py:2:10: error: NameError: name 'summarise' is not defined [load_error]
    2 | PROMPT = summarise("diff")
      |          ^~~~~~~~~
  note: imported from ./.chrys/workflows/code-review/code-review.py:3
captured output (load):
  | loading steps
FAIL code-review (package): 1 error
```

A workflow that passes prints one line, such as `PASS code-review (package) · 1 node · 0 edges`. Warnings are listed above it and do not fail validation. Columns appear only when the workflow's Python is 3.11 or later.

#### JSON report

With `--json`, stdout holds one JSON object. Every field is always present; unknown values are `null`.

| Field | Type and meaning |
| --- | --- |
| `version` | Integer: report format version, currently `1` |
| `status` | String: `pass` or `fail` |
| `target` | Object: what was checked: `path`, `layout` (`file` or `package`), `workflow_id`, `entry` (the entry file), `package_dir`, `source_digest`, and `files` (number of files) |
| `stages` | Array of objects: each stage's `name` and `status` (`pass`, `fail`, or `skipped`), in order |
| `diagnostics` | Array of objects: the problems found, described below |
| `diagnostics_truncated` | Boolean: some load problems were left out |
| `sites_truncated` | Boolean: the workflow has too many nodes to report where each was declared, so graph and binding problems may have no line |
| `workflow` | Object or `null`: once the workflow has loaded, its `title`, `node_count`, `edge_count`, and `outputs` (output node IDs) |
| `output` | Object: `text` printed while loading, and whether it was `truncated` |

Each item in `diagnostics` contains:

| Field | Type and meaning |
| --- | --- |
| `severity` | String: `error` or `warning` |
| `code` | String: problem code, listed below |
| `stage` | String: the stage that found it |
| `message` | String: what is wrong |
| `file` | String or `null`: absolute path of the file |
| `line`, `column`, `end_line`, `end_column` | Integer or `null`: position in the file, starting at 1 |
| `node` | String or `null`: the node ID the problem concerns, or an edge written `source->target` |
| `source_line` | String or `null`: the text of `line` |
| `notes` | Array of objects with `message`, `file`, and `line`: how the code was reached, innermost first |
| `hint` | String or `null`: a suggested fix |
| `traceback` | String or `null`: the Python traceback of a load failure, with only the frames of your own files (not the standard library, installed packages or iCode) |

| Code | Stage | Meaning |
| --- | --- | --- |
| `path_not_found` | `resolve` | Nothing exists at the path. If a workflow has that ID, the hint gives its path |
| `path_is_link` | `resolve` | The workflow, or a folder's entry file, is a link |
| `path_not_workflow` | `resolve` | The path names no single workflow: it is not a `.py` file or a folder, it is a directory that holds workflows (or the project or `.chrys` folder around one) or a file inside a workflow folder, the folder's entry file is not a regular file, or it is spelled differently from its folder listing |
| `name_ignored` | `resolve` | iCode never loads this name, for example one starting with `.` or `_` |
| `name_reserved` | `resolve` | `sdk` is reserved in the global workflow directory |
| `entry_missing` | `resolve` | The folder has no entry file with its exact name |
| `shadowed` (warning) | `resolve` | Another workflow with the same ID is found first, so runs use that one |
| `source_too_large`, `package_too_large` | `read` | The entry file or the folder is over the size limit |
| `source_unreadable`, `package_unreadable` | `read` | A file cannot be read |
| `package_link`, `package_unsupported_file` | `read` | The folder holds a link or a file that is neither a regular file nor a folder |
| `source_not_utf8` | `read` | The entry file is not UTF-8 |
| `metadata_invalid` | `metadata` | The `# /// script` block is invalid |
| `environment_invalid` | `environment` | The Python environment cannot be used |
| `syntax_error` | `load` | A `.py` file does not compile |
| `load_error` | `load` | Top-level code raised an exception |
| `sdk_validation_error` | `load` | `WorkflowBuilder` rejected a declaration or `build()` |
| `missing_workflow` | `load` | There is no module-level `workflow` built by `build()` |
| `load_timeout` | `load` | Loading did not finish in time |
| `worker_failed` | `load` | The process that loads the workflow failed |
| `manifest_invalid` | `graph` | The built graph is invalid |
| `loop_exit_all_conditional` (warning) | `graph` | A loop's exit node is reached only through conditional edges, so an iteration can end with no value and fail the run |
| `agent_profile_missing` | `bindings` | An agent node names a profile that is not available |
| `model_unresolvable` | `bindings` | An agent node has no model it can use |
| `internal_error` | any | iCode itself failed during the stage shown |

#### Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | PASS, possibly with warnings |
| `1` | FAIL |
| `2` | Argument parsing error |
| `130` | The CLI caught a keyboard interrupt |

### `icode workflow run`

Run the specified workflow and output its results when it finishes.

#### Arguments

```shell
icode workflow run <workflow-id> [--input TEXT] [-s SESSION] [--trust] [--timeout SECONDS] [--json] [-q]
```

| Argument | Default and purpose |
| --- | --- |
| `<workflow-id>` | File name without `.py`, or the folder name of a workflow folder; find IDs with `icode workflow list` |
| `--input TEXT` | Defaults to an empty string, passed to the start node as `WorkflowValue.text` (see [multi-line input](#multi-line-input)) |
| `-s` / `--session` | Load an existing workflow session and start a new run of its bound workflow; does not resume an old run. The session must be a workflow session with at least one run. `<workflow-id>` must match the session's workflow; otherwise the run is rejected with `spec_changed` |
| `--trust` | Trust the current custom source and environment; unnecessary for built-in workflows |
| `--timeout SECONDS` | No overall limit by default; must be a finite positive number. Covers preview, workflow loading, and execution, but excludes session restoration, some initialization, and cleanup. After a timeout, the command still waits for cleanup to finish |
| `--json` | Use JSON output |
| `-q` / `--quiet` | Do not show progress on stderr; warnings, errors, load output, output outside nodes, and final outputs are still shown |
| `-h` / `--help` | Show help |

#### Multi-line input

How to write line breaks in `--input` depends on the shell. In bash and zsh, use `$'...'` quoting with `\n`:

```shell
icode workflow run demo-workflow --input $'interactive: false\ndepth: deep\nhow are errors handled?'
```

In PowerShell, write each line break as `` `n `` inside double quotes:

```powershell
icode workflow run demo-workflow --input "interactive: false`ndepth: deep`nhow are errors handled?"
```

PowerShell does not understand `$'...'`: it passes the text on as one line with a literal `\n`.

#### Text output

Without `--json`, text mode writes final outputs to stdout in declaration order, separated by a blank line. Progress goes to stderr, one line per step: node states, agent-node tool calls and the notes an agent node writes between them, `ctx.emit()` progress, loop iterations, and a closing `✓ Workflow completed` line when the run succeeds. Load output, output outside nodes, and other run diagnostics also go to stderr. `--quiet` hides the progress but keeps warnings, errors, load output, and output outside nodes.

#### JSON output

JSON mode suppresses node states, `ctx.emit()` progress, and loop iteration messages on stderr. Warnings and errors still go to stderr. Once a run result is available, it is written to stdout as a single-line JSON object. The table lists all top-level fields; the structures of `diagnostics` and `outputs` follow below.

| Field | Type and meaning |
| --- | --- |
| `session_id` | String or `null`: session identifier, or `null` if unavailable |
| `run_id` | String: identifier for this run |
| `outcome` | String: run outcome; see [Run outcomes and recovery](#run-outcomes-and-recovery) |
| `reason` | String: outcome reason code, or an empty string if none |
| `node_id` | String: relevant failed node, or an empty string if none |
| `error` | String: error description, or an empty string if none |
| `duration` | Number: elapsed seconds measured by the CLI, including preparation |
| `diagnostics` | Object or `null`: run diagnostics; may be `null` if unavailable |
| `outputs` | Array of objects: final outputs in output declaration order, or an empty array if none |

When `diagnostics` is an object, its structure is as follows. It does not include output from node diagnostic files. This example shows only this field's value; line breaks and comments are added for readability. Actual output is single-line JSON without comments.

```jsonc
{
  "load": {
    // String: output captured while loading the workflow file, such as a top-level print()
    "text": "Output during loading\n",
    // Boolean: whether load output was truncated by the capture limit
    "truncated": false
  },
  "native": {
    // String: output that cannot be associated with a specific node attempt,
    // such as direct writes from subprocesses or native extensions;
    // only the trailing portion is retained if the capture limit is exceeded
    "text": "",
    // Integer: number of bytes discarded because of the capture limit
    "dropped_bytes": 0
  }
}
```

If collecting or reading diagnostics fails, the object includes a string `error` field and may lack `load` or `native`. For example, a read failure may produce:

```jsonc
{
  "error": "Specific reason diagnostics could not be read"
}
```

Each item in `outputs` is an object containing these fields:

| Field | Type and meaning |
| --- | --- |
| `node_id` | String: output node ID |
| `activation_id` | String: activation identifier of the node execution that produced this output |
| `text` | String: the node's returned `WorkflowValue.text` |
| `data` | JSON value: the node's returned `WorkflowValue.data`; may be an object, array, string, number, Boolean, or `null`. Its internal structure is defined by the workflow and has no fixed fields |

#### Node diagnostics

Both text and JSON modes save node diagnostic records. `print()` output inside a node goes only to those records, not to CLI stderr or the result JSON's `diagnostics` field.

Node diagnostic records are stored in `workflows/<run_id>/nodes/` under the [session directory](../guides/daily-use/sessions.md#find-the-session-id-and-storage-location). Filenames typically follow `<activation_id>.<attempt>.diagnostics.<hash>.json`, containing the activation identifier, attempt number, and an 8-character hash of the record identity that keeps names distinct after sanitizing. `phases[].stdout.text` holds the captured text; `phases[].stdout.truncated` indicates truncation due to the capture limit. No file is created if there is no diagnostic content. In the TUI, you can also inspect an attempt's diagnostics on the “Output” tab in node details.

#### Error handling and exit codes

Text and JSON modes use the same exit codes. On failure or cancellation, text mode writes error text to stderr, while JSON mode writes error JSON to stderr. Argument parsing errors are written to stderr as text in both modes.

If a run result has already been produced before failure or cancellation, stdout may still contain output: collected final-result text in text mode, or the result object in JSON mode. If failure occurs before a run result exists—for example, the workflow is missing or trust has not been confirmed—stdout output is not guaranteed. Scripts should check the exit code; content on stdout or a nonempty JSON `outputs` array alone does not indicate success.

| Exit code | Meaning |
| --- | --- |
| `0` | Completed |
| `1` | Errors such as run failure, rejection, or ordinary cancellation |
| `2` | Argument parsing error |
| `124` | Workflow run timed out |
| `130` | The CLI caught a keyboard interrupt |
