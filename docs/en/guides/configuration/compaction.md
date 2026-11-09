# Configure context compaction

When a long conversation approaches the model's context window, iCode automatically compacts earlier content to make room for subsequent requests. This guide explains how to configure the Last Words supplement and output limit for agents of the "Built-in" type. External ACP agents manage their own context.

New agents use defaults suited to most tasks, so adjustments are usually unnecessary.

## Understand when compaction happens

iCode automatically calculates when to start compaction based on the maximum context window, maximum output tokens, and a safety margin for the current model profile. Set the maximum context window and maximum output tokens in the [model profile](./models.md) according to the model's capabilities and your task requirements. The "Compaction" tab does not provide a separate setting for when compaction starts.

When the context is nearly full, iCode also lowers the output limit of the next request so that its input and output fit in the context window, leaving a small margin for error in its token estimate.

The model service can still report that the context window is full, for example when the model's actual context window is smaller than the one set in the model profile. If the service's error names a limit smaller than the maximum context window in the model profile, the request fails at once and the error message names that limit: set the maximum context window in the model profile to that value or less. Otherwise, iCode compacts the context and sends the request once more by itself. If the service reports that the context window is full again, the request fails. When the model stops before writing anything because no room is left for its reply, the request can also fail. After such a failure, the next request first compacts the context to try to make room, for example when you click "Retry", send another message, or retry a workflow node. If it keeps happening, set the maximum context window in the model profile to the model's actual value.

During compaction, iCode first summarizes older tool results and, if needed, removes older tool calls together with their results. If it still needs to free up space, it replaces completed turns in the current context with a compaction summary. If these steps are still insufficient, iCode generates Last Words for the current task to preserve the information needed to continue, then removes the current turn's tool calls, tool results, and intermediate content from the context supplied to the model, keeping the user's messages.

These three steps serve different purposes: trimming older tool calls and results only reduces the space taken up by tool use; compaction summaries replace completed turns; Last Words help continue the current unfinished task.

In the TUI, you can observe the following changes:

- "Compacting conversation..." appears during compaction.
- After compaction, compacted turns appear in gray in the sidebar's **Chat** tab. The conversation area shows a gray "Compressed" marker and a compaction summary at the corresponding position.
- If Last Words were generated, you can expand the completed compaction card to view them.

Compacted historical turns remain in the session. The agent can look up specific information from them when needed, without adding the entire conversation back to the current context.

Compaction reduces context usage, but summaries cannot guarantee that every original detail is preserved. Put project rules that must be retained over time without omissions in [memory files](./memory.md), and write important work results to files in the working directory instead of leaving them only in the conversation.

## Adjust compaction settings

1. Enter `/agents compaction` in the input field to open the current agent's "Compaction" tab. To edit another agent, select it from the list on the left.
2. Adjust "Last Words Max Output Tokens" as needed, entering an integer within the range allowed by the interface. The actual generation limit will not exceed the current model profile's maximum output tokens.
3. To emphasize certain types of information in Last Words, fill in "Last Words Supplement (Optional emphasis)".
4. Click "Save". The new settings take effect the next time this agent needs compaction.

"Last Words Supplement (Optional emphasis)" specifies the information that Last Words should prioritize. For example, if you often use this agent to troubleshoot issues, you could enter:

```text
Prioritize reproduction steps, causes already ruled out, key logs, and hypotheses that still need to be tested.
```

Keep the supplement brief and focus on information needed to continue the task that could easily be lost during compaction.
