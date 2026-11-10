# Create and run workflows

A workflow splits a task into nodes, with edges defining the execution order and data flow. Python nodes handle deterministic processing, while agent nodes handle tasks that need a model to understand or generate content. Branches, joins, and loops support more complex execution flows.

This tutorial first introduces the terminal user interface (TUI) and basic operations through a built-in example, then shows how to write workflows through six standalone Python examples. For complete parameter descriptions and runtime rules, see the [Workflow reference](../../reference/workflows.md).

## Run the built-in workflow in the TUI

First, [configure a working model](../../start/getting-started.md#3-configure-a-model) and choose a local project directory. The built-in example below asks agents to read files in that directory and generate a project introduction, incurring model usage costs.

### Select a workflow

In a terminal, change to the chosen project directory and run `icode` to open the TUI. Enter `/workflow` in the input box to open the workflow picker, then select `demo-workflow` with the `builtin` source.

Alternatively, click “APP MODE: Chat” in the upper left and select “Workflow” to switch modes, then click “New Session” and select a workflow.

### Explore the workflow interface

After selecting the example, explore these tabs:

- **Workflow**: Shows the execution graph, including nodes, connections, and execution states. When the graph is larger than the panel, drag it with the mouse to move around. After a run starts, click a node to view its input and output. Tabs under each one switch between “Markdown” (the default), “Plain text” (the text unformatted), “Data” (structured data as fields) and “Progress messages” (what the node reported while it ran); only the tabs with content appear. Agent nodes also have a “Transcript” tab for model responses and tool calls; each piece of the agent's text appears there once the model has finished writing it. The transcript opens at its newest content and keeps showing new content as it arrives while you stay at the bottom; scroll up to read earlier content and it stays where you are until you scroll back to the bottom.
- **Info**: Shows the workflow name, description, script location, execution environment, and node configuration, including agents and models.
- **Source**: Shows the Python code that defines the workflow. Both “Workflow” and “Source” are read-only; to change a workflow, edit its Python source file.
- **Input**: Shows the input submitted for the run after it starts, with Markdown formatting. Click “copy” to copy the input exactly as it was submitted.
- **Output**: Updates node states and progress messages during execution, and shows the final output.

### Run and observe the workflow

Agents in a workflow use the same tool approval process as Chat mode. Before running, adjust the mode through “APPROVAL MODE: …” in the upper right if needed, then respond to approval requests as they appear.

1. On the “Workflow” tab, click “▶ Start” to open the “Start Workflow” dialog. Confirm the working directory and default model under “Run Settings”.
2. Optionally describe what you want to learn about the project, such as `Explain this project's entry points and main modules. Answer in English.` Leave the input empty to use the example's default task. Click “▶ Start” in the dialog.

The example first asks how deeply to read the project. Choose `deep` to try parallel analysis by multiple agents, or `quick` for a quick reading by one agent with fewer calls.

During execution, watch the graph to see which nodes are running, completed, or skipped. To stop the workflow, click “■ Cancel” and confirm.

If an error puts a node in the “awaiting retry” state, click it to inspect the error details, resolve the issue, and click “Retry”. You can also close the node details and cancel the entire run. Manual retry applies only to nodes awaiting retry in the current run; it cannot rerun completed nodes or resume a run that has ended. Retrying may repeat file writes or external requests, and changes already made are not automatically undone.

While you work with a workflow, the terminal tab shows the workflow's title. Spinning dots before it, such as `⠋`, mean the run is in progress; `!` means a node is awaiting retry, and `?` means the workflow or one of its agents is waiting for you to answer a question or approve a tool call. When the run ends, `✓` marks a completed run and `✗` a failed one; a cancelled run shows no mark. When you switch back to chat, the tab shows the chat session again.

After generating a project introduction, the example includes the draft in a question for review. Answer `ok` to accept it, or provide specific revision requests. The example performs at most two writing rounds and outputs the final introduction. The `deep` path also outputs suggestions for further reading.

When the run ends with a result, click “Result” to the right of “■ Cancel” to read the final output. If the workflow has several outputs, each one gets its own tab, named after its output node. “Output” still shows the full record, including node states and progress messages. Click “▶ Start” again to enter new input and run again.

A workflow session can save multiple runs. Click the “Run 1”, “Run 2”, and subsequent run tabs above “Workflow”, “Info”, and the other tabs to browse each run. After quitting and reopening iCode, first switch to “Workflow” through the app mode selector in the upper left. Press `F1` or click `f1 Sessions` at the bottom of the interface, then select a workflow session to view its historical inputs, outputs, and node records.

## Create your first workflow

Next, write your own workflow. Create a `.chrys/workflows/` directory in your project if it does not already exist. Save all the workflow files below in this directory.

Create `.chrys/workflows/greeting.py`:

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder(
    "greeting",
    description="Greet the person named in the input."
)


def greet(value: WorkflowValue) -> str:
    text = value.text
    name = text.strip() or "friend"
    return f"Hello, {name}!"


hello = wf.python("hello", greet)
wf.start(hello)
wf.output(hello)
workflow = wf.build()
```

The first argument to `WorkflowBuilder` sets the workflow title; the optional `description` describes its purpose. The title and description appear in the workflow information and are not passed as node input or agent instructions.

A node is an execution step in a workflow. Here, `greet()` defines the processing logic: it receives a `WorkflowValue`, reads the input through `value.text`, and returns a greeting. `wf.python("hello", greet)` registers this function as a Python node named `hello`. Pass the function itself when registering it, without parentheses to call it.

`wf.start(hello)` declares the start node, and `wf.output(hello)` declares an output node. Here, `hello` is both the start and output node, giving this execution path:

```text
Input → hello node → Workflow output
```

Finally, `wf.build()` validates and builds the workflow. **The file must assign the result to a module-level variable named `workflow` so iCode can load it.**

You do not need to restart iCode after saving the file. Click “New Session” to open the workflow picker and select `greeting`.

When a custom file is first loaded or has changed, the TUI asks you to review its source and confirm trust. A workflow is executable Python, and loading it can execute code at the top level of the file.

After confirming trust, click “▶ Start”, enter `Alex`, and run. The final result should be `Hello, Alex!`; leaving the input empty produces `Hello, friend!`.

## Use another Python environment

By default, workflows use the Python interpreter running iCode. If a workflow needs third-party libraries, use inline script metadata to specify your own Python interpreter or virtual environment. Install dependencies yourself; iCode does not install them automatically. See [Workflow reference: Execution environment](../../reference/workflows.md#execution-environment) for configuration details. The Python examples in this tutorial use only the standard library and need no additional environment setup.

## Pass data and ask the user

### Pass data between two nodes

Create `.chrys/workflows/fruit_list.py`:

```python
from chrys.workflows import NodeContext, WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Fruit list")


def parse_items(value: WorkflowValue) -> WorkflowValue:
    text = value.text
    items = [item.strip() for item in text.split(",") if item.strip()]
    return WorkflowValue(text=", ".join(items), data={"items": items})


def describe(value: WorkflowValue, ctx: NodeContext) -> str:
    items = value.data["items"]
    ctx.emit(f"Received {len(items)} items; preparing the list")
    return f"{len(items)} items: {value.text}"


parse = wf.python("parse", parse_items)
report = wf.python("report", describe)
wf.start(parse)
wf.chain(parse, report)
wf.output(report)
workflow = wf.build()
```

Here, `parse` turns the input into a list of fruit, and `report` describes the list. The execution path is:

```text
Input → parse node → report node → Workflow output
```

`wf.chain(parse, report)` connects the two nodes, passing the return value of `parse` as input to `report`. `chain()` is shorthand for successive `edge()` calls: for these two nodes, it is equivalent to `wf.edge(parse, report)`; for more nodes, `wf.chain(a, b, c)` is equivalent to `wf.edge(a, b)` followed by `wf.edge(b, c)`.

Nodes pass data through `WorkflowValue`. Its `text` field holds text; the optional `data` field holds JSON-compatible structured data and defaults to `None`. Here, `parse_items()` stores comma-separated fruit names in `text` and a dictionary containing the fruit list in `data`. `describe()` reads the list through `value.data["items"]` to count the items.

A node returns its result with `return`, which determines what it passes downstream:

- When a node returns a string, iCode places it in `WorkflowValue.text` and sets `data` to `None`. Here, `describe()` only needs to output a description, so it returns a string.
- Returning a `WorkflowValue` passes both text and structured data, as in `parse_items()`.

A Python node function can take one or two parameters. `parse_items(value)` receives a `WorkflowValue`; `describe(value, ctx)` also receives a `NodeContext` as its second parameter. `NodeContext` provides operations for the current node execution. Here, `ctx.emit()` reports progress to the TUI.

Select `fruit_list` in the TUI, enter `apple, banana, pear`, and run. When it finishes, open “Output” to see the final workflow output, shown below the output node's name:

```text
report
3 items: apple, banana, pear
```

### Wait for a user response during execution

In addition to receiving upstream data, a node can ask the user a question during execution and continue based on the answer.

Replace the `describe()` function and the `report = ...` registration separately, keeping the `parse = ...` line between them and the rest of the code:

```python
async def describe(value: WorkflowValue, ctx: NodeContext) -> str:
    title = await ctx.ask("Please give this fruit list a title.")
    items = value.data["items"]
    return f"{title}: {len(items)} items: {value.text}"


# Do not set a node deadline while waiting for a user response.
report = wf.python("report", describe, timeout=None)
```

Save the changes, reopen the workflow in the TUI, preview the updated source, and confirm trust before starting. Use `apple, banana, pear` as the run input again. When the question dialog appears, enter `Shopping List`. The final output should be `Shopping List: 3 items: apple, banana, pear`.

`ctx.ask()` asks the user a question and returns a string after receiving an answer. It must be called with `await`, so define the node function with `async def`. Workflows that ask the user questions must run in the TUI; unattended CLI execution does not support them.

A question can also offer options, and one dialog can ask several questions. Pass `Question` objects instead of a string. Add `Question` to the `from chrys.workflows import ...` line at the top of the file, then replace `describe()` again:

```python
async def describe(value: WorkflowValue, ctx: NodeContext) -> str:
    title, extras = await ctx.ask(
        [
            Question("Please give this fruit list a title.", header="Title"),
            Question("What else goes on the list?", header="Extras", options=["milk", "bread", "eggs"], multi_select=True),
        ]
    )
    items = value.data["items"] + list(extras.selected)
    return f"{title.text}: {len(items)} items: {', '.join(items)}"
```

A list of questions returns one `Answer` per question, in order. `selected` holds the option labels the user picked, and `text` holds what they typed. Run it with the same input. The dialog shows one tab per question: enter `Shopping List` on the “Title” tab and click “Answer & Next”, check `milk` and `eggs` on the “Extras” tab and click “Answer & Review”, then click “Submit answers”. The output should be `Shopping List: 5 items: apple, banana, pear, milk, eggs`.

For single-select questions, what an answer contains, and how to pass a selection downstream, see [Workflow reference: `NodeContext.ask`](../../reference/workflows.md#nodecontextask).

## Define agent nodes

### Use an iCode agent

Create `.chrys/workflows/learning_plan.py`:

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Learning plan")


def make_prompt(value: WorkflowValue) -> str:
    text = value.text
    topic = text.strip() or "Python basics"
    return (
        f"Create a three-step learning plan for a beginner studying {topic}. "
        "Use one sentence per step. Answer in English using only this input. Do not call tools."
    )


prepare = wf.python("prepare", make_prompt)
plan = wf.agent("plan", profile="QA")
wf.start(prepare)
wf.chain(prepare, plan)
wf.output(plan)
workflow = wf.build()
```

Here, `prepare` builds the prompt, and `plan` calls the QA agent to generate a learning plan. The execution path is:

```text
Input → prepare (Python) → plan (agent) → Workflow output
```

Use `wf.agent()` to define an agent node. In `wf.agent("plan", profile="QA")`, `"plan"` is the node name and `profile="QA"` selects the agent profile. List available profiles with:

```shell
icode agent list
icode model list
```

For `profile`, use a `Name` from `icode agent list`; for the optional `model` parameter, use a `Name` from `icode model list`. To specify a model for this node, replace the registration below, substituting an actual model profile name for `My model`:

```python
plan = wf.agent("plan", profile="QA", model="My model")
```

`model` selects a model for this node and takes precedence over the model bound to the agent profile. When omitted, the agent's bound model takes precedence; otherwise, the workflow's default model is used.

Agent nodes read only the upstream `WorkflowValue.text` as the user message, not `data`. If the model needs information from `data`, format that information into `text` in an upstream Python node. The agent's response is stored in the returned `WorkflowValue.text` and passed downstream.

Create a new session in the TUI, select `learning_plan`, enter `Python file handling`, and run. Open the `plan` node's “Transcript” to see the generation process. When the run finishes, “Output” shows a three-step learning plan, with wording determined by the model.

#### Add node instructions

Use `instructions_suffix` to add a fixed responsibility or output requirement to a node. For example, replace the `plan` registration to tailor the learning plan to someone with no programming experience:

```python
plan = wf.agent(
    "plan",
    profile="QA",
    instructions_suffix="Assume the learner has no programming experience. Explain unfamiliar terms briefly.",
)
```

For a regular iCode agent, `instructions_suffix` is appended to the profile's existing `instructions` as part of the system prompt. The upstream `text` remains the user message.

### Use an ACP agent

If you have [configured an external Agent Client Protocol (ACP) agent](../extensions/external-acp-agents.md#create-a-profile-and-test-the-connection), replace `QA` with its profile name:

```python
plan = wf.agent("plan", profile="My ACP agent")
```

Replace `My ACP agent` with an existing ACP profile name from `icode agent list`, and make sure the external program specified in that profile can start.

By default, ACP nodes use the model configured for the external agent, not the workflow's default model.

ACP nodes also support `instructions_suffix`, but append it to the user prompt sent to the remote agent without changing that agent's system prompt.

## Set timeouts and retries

Both Python and agent nodes support timeouts and retries: `timeout` limits the duration of each attempt, and `retry` sets the maximum number of attempts and the interval between retries.

| Node | Default timeout per attempt | Default maximum attempts (including the first) |
| --- | --- | --- |
| Python | 300 seconds | 1; no automatic retry |
| Agent | Unlimited | 3 |

For example, add a timeout and retries to the learning plan node. Add the `Retry` import at the top of `learning_plan.py` and replace the `plan` registration:

```python
from chrys.workflows import Retry

plan = wf.agent(
    "plan",
    profile="QA",
    timeout=120,
    retry=Retry(max_attempts=3, backoff=2),
)
```

Each attempt is limited to 120 seconds, with at most three attempts including the first, and a two-second interval between retries. Python nodes accept the same `timeout` and `retry` parameters in `wf.python(...)`.

Set `timeout=None` for no execution deadline. Time spent waiting for user answers or tool approval counts toward the timeout, which is why the earlier question example uses `timeout=None`.

Retrying may repeat model calls, file writes, or an external ACP agent's tool calls. When a non-ACP agent's attempt fails or times out, the retry continues its conversation, so tool calls that already finished do not run again. Operations that have already occurred are not automatically undone. Not every error is retried automatically; see [Workflow reference: Timeouts and retries](../../reference/workflows.md#timeouts-and-retries).

Shell commands that an agent node moved to the background stop when its attempt ends, so a retry does not inherit them. See [Commands that keep running in the background](../configuration/tools.md#commands-that-keep-running-in-the-background).

## Define conditional branches

### Select one branch with switch()

Create `.chrys/workflows/route_text.py`:

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Route by length")


def normalize(value: WorkflowValue) -> str:
    text = value.text
    return text.strip()


def is_short(value: WorkflowValue) -> bool:
    return len(value.text) <= 10


def is_medium(value: WorkflowValue) -> bool:
    return len(value.text) <= 20


def short_reply(value: WorkflowValue) -> str:
    text = value.text
    return f"Short text: {text}"


def medium_reply(value: WorkflowValue) -> str:
    text = value.text
    return f"Medium text: {text}"


def long_reply(value: WorkflowValue) -> str:
    text = value.text
    return f"Long text: {len(text)} characters"


check = wf.python("check", normalize)
short = wf.python("short", short_reply)
medium = wf.python("medium", medium_reply)
long = wf.python("long", long_reply)
wf.start(check)
wf.switch(
    check,
    cases=[(is_short, short), (is_medium, medium)],
    default=long,
)
wf.output(short)
wf.output(medium)
wf.output(long)
workflow = wf.build()
```

Here, `check` strips leading and trailing whitespace, then routes the text by length. The execution path is:

```text
                 ┌─ length ≤ 10      → short  → Output
Input → check ───┼─ 10 < length ≤ 20 → medium → Output
                 └─ length > 20      → long   → Output
```

`wf.switch(check, cases=[...], default=long)` selects one branch for the output of `check`. Each entry in `cases` contains a condition function and a destination node, such as `(is_short, short)`. `switch()` selects the first destination whose condition returns `True`, in declaration order. If none match, it selects the node specified by `default`.

A condition function receives the source node's output as a `WorkflowValue` and returns a Boolean. Here, `is_short()` and `is_medium()` check the length of `value.text` in order, falling back to `long` if neither matches. Text of at most 10 characters satisfies both conditions but goes only to the first declared branch, `short`. The `medium` branch therefore handles text longer than 10 characters and no longer than 20.

All three branch nodes are declared as workflow outputs with `wf.output()`. Each run selects only one branch, and the final result contains only that branch's output. Skipped branches do not produce empty results.

Create a new session in the TUI and select `route_text`. Run it with each input below, watching the selected path and skipped nodes in the graph, then check the final output in “Output”:

| Input | Final output |
| --- | --- |
| `hello` | `Short text: hello` |
| `hello workflow` | `Medium text: hello workflow` |
| `hello workflow tutorial` | `Long text: 23 characters` |

### Run multiple branches with conditional edge() calls

`wf.edge(source_node, destination_node, when=condition_function)` connects two nodes. The `when` function receives the source node's output as a `WorkflowValue`. If it returns `True`, that output is passed to the destination. Omitting `when` passes the output unconditionally.

Each conditional edge is evaluated independently, so multiple paths can be selected. Replace the `wf.switch(...)` call in this example with:

```python
wf.edge(check, short, when=is_short)
wf.edge(check, medium, when=is_medium)
wf.edge(check, long, when=lambda value: len(value.text) > 20)
```

Save the changes, reopen the workflow in the TUI, preview the updated source, and confirm trust. Enter `hello` and run. This input satisfies both `is_short` and `is_medium`, so both nodes execute. “Output” shows each node name and its result:

```text
short
Short text: hello

medium
Medium text: hello
```

## Process in parallel and combine results

Create `.chrys/workflows/text_stats.py`:

```python
from chrys.workflows import SourceValue, WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Text statistics")


def normalize(value: WorkflowValue) -> str:
    text = value.text
    return text.strip()


def count_chars(value: WorkflowValue) -> WorkflowValue:
    text = value.text
    return WorkflowValue(text=str(len(text)), data={"chars": len(text)})


def count_words(value: WorkflowValue) -> WorkflowValue:
    text = value.text
    count = len(text.split())
    return WorkflowValue(text=str(count), data={"words": count})


def combine_stats(sources: list[SourceValue]) -> str:
    by_node = {source.node_id: source.value.data for source in sources}
    return f"Characters: {by_node['chars']['chars']}; words: {by_node['words']['words']}"


def format_report(value: WorkflowValue) -> str:
    text = value.text
    return f"Done. {text}"


prepare = wf.python("prepare", normalize)
chars = wf.python("chars", count_chars)
words = wf.python("words", count_words)
report = wf.python("report", format_report)
wf.start(prepare)
wf.edge(prepare, chars)
wf.edge(prepare, words)
wf.join([chars, words], report, combine=combine_stats)
wf.output(report)
workflow = wf.build()
```

The execution path is:

```text
                    ┌→ chars ─┐
Input → prepare ────┤         ├→ join:report → report → Output
                    └→ words ─┘
```

Both `chars` and `words` become ready after `prepare` completes and do not need to wait for each other. `wf.join([chars, words], report, combine=combine_stats)` creates a join node that waits for both branches to finish, calls `combine_stats()` as specified by `combine`, and passes its return value to `report`.

The combine function receives a list of `SourceValue` objects. Use `source.node_id` to identify the source and `source.value` to read its result. Here, the function retrieves character and word counts by source name.

A combine function can return a string or a `WorkflowValue`. Use the latter when you need to pass structured data onward.

Create a new session in the TUI, select `text_stats`, enter `hello workflow`, and run. “Output” should show:

```text
report
Done. Characters: 14; words: 2
```

To merge text from multiple sources, you can omit `combine`. Replace the original `wf.join(...)` call with:

```python
wf.join([chars, words], report)
```

Both sources in this example produce results. The default merge concatenates their `WorkflowValue.text` in the declared `[chars, words]` order and discards `data`, regardless of which branch finishes first. When a join follows conditional branches, skipped sources do not participate in the merge. If only one source provides a value, its `WorkflowValue` is passed through unchanged, including `data`.

Save the changes, reopen the workflow in the TUI, preview the updated source, and confirm trust. Enter `hello workflow` and run again. “Output” shows:

```text
report
Done. ## chars
14

## words
2
```

`Done.` is added by `format_report()`. The default merge is useful for passing multiple agent analyses to a downstream summary node without writing a combine function.

## Loop until a condition is met

Create `.chrys/workflows/count_up.py`:

```python
from chrys.workflows import (
    BuilderScope,
    NodeContext,
    NodeHandle,
    WorkflowBuilder,
    WorkflowValue,
)

wf = WorkflowBuilder("Count up to three")


def increment(value: WorkflowValue) -> str:
    text = value.text
    return str(int(text) + 1)


def report_value(value: WorkflowValue, ctx: NodeContext) -> WorkflowValue:
    ctx.emit(f"Current value: {value.text}")
    return value


def reached_target(value: WorkflowValue) -> bool:
    return int(value.text) >= 3


def make_body(scope: BuilderScope) -> tuple[NodeHandle, NodeHandle]:
    increment_node = scope.python("increment", increment)
    report_node = scope.python("report", report_value)
    scope.chain(increment_node, report_node)
    return increment_node, report_node


count = wf.loop(
    "count",
    body=make_body,
    until=reached_target,
    max_iterations=5,
    on_exhausted="fail",
)
wf.start(count)
wf.output(count)
workflow = wf.build()
```

`wf.loop()` creates a loop node. The parameters used here mean:

| Parameter | Meaning |
| --- | --- |
| `"count"` | Name of the loop node |
| `body=make_body` | Function that builds the loop body, declaring its nodes and edges and returning the entry and exit node handles |
| `until=reached_target` | Stop condition function, receiving this iteration's exit `WorkflowValue`; `True` ends the loop, while `False` starts another iteration if the limit has not been reached |
| `max_iterations=5` | Run at most five iterations |
| `on_exhausted="fail"` | If the iteration limit is reached without satisfying the condition, `"fail"` fails the workflow; `"continue"` uses the last iteration's exit value as the loop output and continues the rest of the workflow |

`make_body()` runs when the workflow is built, using `scope` to create and connect nodes inside the loop. Its return value, `(increment_node, report_node)`, specifies the entry and exit in that order:

- **Entry node `increment`**: Receives each iteration's input and adds one to the number.
- **Exit node `report`**: Emits a progress message and returns the iteration's result for the stop condition to evaluate.

The loop node is the start of this workflow, so the first iteration's entry node receives the workflow input. Each iteration's exit value is passed to `reached_target()`, specified by `until`. When the number reaches 3, the value becomes the loop output. Otherwise, it is passed to the next iteration's entry node if the iteration limit has not been reached.

Create a new session in the TUI, select `count_up`, enter `0`, and run. The three iterations produce `1`, `2`, and `3`, with `3` as the final output. Open nodes inside the loop to inspect their inputs, outputs, and progress messages across iterations. Then run with `-10` to see the workflow fail after reaching the five-iteration limit.

## Split a workflow into several files

When a workflow grows, move it into a folder of the same name and split it into several Python files and resource files. Create `.chrys/workflows/greeting_kit/` with two files. The entry file must have exactly the folder's name, `greeting_kit.py`:

```python
from pathlib import Path

from chrys.workflows import WorkflowBuilder, WorkflowValue
from phrases import greet

wf = WorkflowBuilder("greeting kit")
TEMPLATE = (Path(__file__).parent / "template.txt").read_text(encoding="utf-8")


def hello(value: WorkflowValue) -> str:
    return greet(TEMPLATE, value.text.strip() or "friend")


node = wf.python("hello", hello)
wf.start(node)
wf.output(node)
workflow = wf.build()
```

`phrases.py`, in the same folder:

```python
def greet(template: str, name: str) -> str:
    return template.format(name=name)
```

And `template.txt`, also in the folder, containing `Hello, {name}!`.

Select `greeting_kit` in the TUI, confirm trust, and run it with `Alex`. The result is `Hello, Alex!`.

- Import other files in the folder by their module name, as `from phrases import greet` does. Relative imports such as `from .phrases import greet` do not work.
- Read resource files relative to `__file__`. A relative path such as `open("template.txt")` is relative to the workspace, not to the folder.
- Trust covers every file in the folder except names starting with `.` (such as `.venv` or `.git`) and the compiled copies Python keeps in `__pycache__` folders. After changing any of them, reopen the workflow to preview and confirm it again.
- Deleting a workflow folder in the workflow picker deletes only its entry file; the other files stay.

See [Workflow reference: Workflow folders](../../reference/workflows.md#workflow-folders) for the full rules.

## Run from the command line

The CLI is suitable for workflows that need no human interaction. It does not support `ctx.ask()`, and its tool approval mode is fixed to `bypass`, which skips tool approval.

### List and run workflows

Run this command in the project directory to list available workflows:

```shell
icode workflow list
```

To check a workflow you are writing, pass its file or folder to `icode workflow validate`. It prints `PASS`, or each problem with its file and line; see [`icode workflow validate`](../../reference/workflows.md#icode-workflow-validate):

```shell
icode workflow validate .chrys/workflows/greeting.py
```

Use a workflow ID from the list to run it. Replace `WORKFLOW_ID` below with the actual ID:

```shell
icode workflow run WORKFLOW_ID --input "Input text" --trust
```

`--input` sets the `WorkflowValue.text` received by the start node and defaults to an empty string. The initial `data` is `None` and cannot be set directly through CLI arguments. Even a JSON string is still text; the workflow must parse it itself.

For input that spans several lines, bash and zsh accept `$'...'` quoting with `\n` for each line break. In PowerShell, use `` `n `` inside double quotes instead, as in `` --input "first line`nsecond line" ``. See [Multi-line input](../../reference/workflows.md#multi-line-input).

After creating or changing a workflow file, `--trust` confirms trust in the current source and execution environment, just as clicking “Trust” does in the TUI. It can be omitted if the previously trusted content has not changed. See [Trust confirmation](../../reference/workflows.md#trust-confirmation) for the scope of these checks and what happens during loading.

To view a run started from the command line in the TUI, open the "Workflow Sessions" window and check "CLI".

### Save text results

By default, the CLI outputs each output node's `text`, with a blank line between results. Final results go to stdout (standard output). While the workflow runs, its progress goes to stderr (standard error): each node's state, what an agent node does (its tool calls and the notes it writes between them), and messages sent by `ctx.emit()`. For example:

```text
• Starting workflow review…
Workflow Code review (review) · run 0b337219bb2e · session 890c8ee93562
▸ [plan] running
  [plan] → read   src/app.py
  [plan]   ✓ 0.1s
✓ [plan] completed · 4.2s
▸ [report] running
✓ [report] completed · 2.0s
✓ Workflow completed · 6.5s
```

On a terminal, control characters in the results are shown as `�`. Redirect the final results to a file with `>` to keep them exactly as written; the progress still shows in the terminal:

```shell
icode workflow run WORKFLOW_ID --input "Input text" > result.txt
```

To see only warnings, errors and the final results, add `-q` or `--quiet`.

### Get JSON results

With `--json`, the CLI suppresses node states and progress messages and writes a JSON result to stdout when the run ends. It includes the run outcome, output node names, and each result's `text` and `data`:

```shell
icode workflow run WORKFLOW_ID --input "Input text" --json
```

For the complete JSON fields and exit codes, see the [Workflow reference](../../reference/workflows.md#command-line).
