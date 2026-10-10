# Configure models

A model profile contains the information needed to connect to a model service. This guide explains how to create, clone, edit, switch, and delete model profiles in the terminal user interface (TUI). Once saved, profiles let you switch between tasks without entering connection parameters again.

## Open the model configuration window

Open the "Model Configuration" window in any of these ways:

- Press **F4**.
- Click `f4 Models` at the bottom of the interface.
- Enter `/models` in the input field.

The list on the left shows existing model profiles. The form on the right shows the currently selected profile.

While a task is running, the "Model Configuration" window opens in read-only mode. You cannot create, edit, or delete profiles. Wait for the task to finish before changing the configuration.

## Manage model profiles

### Create a model profile

1. Click "New".
2. Enter a "Profile Name" of your choice, such as `My Model`.
3. In the "Model Options" section, select the "Provider" and "API Style". Only OpenAI and DeepSeek (OpenAI) provide an API Style option. Before configuring it, check the model service's documentation for supported APIs and follow its recommendation when choosing "Chat Completions" or "Responses". If the service supports only one API, select its corresponding style; if it supports both, choose according to the service's recommendation or your needs.
4. Enter the model ID in "Model" and, as required by the model service, fill in "Base URL" and "API Key".
5. Set "Max Context Window" and the "Max Output Tokens" field according to the provider's documentation.
6. Adjust "Streaming", "Vision Model", and "Connection Options" as needed. "Streaming" is on for a new profile.
7. Click "Save".

**Notes:**

- Clicking "New" immediately creates and saves a blank profile. You must click "Save" for any subsequent entries or changes to take effect. Closing the window does not delete the blank profile you created.
- The profile name identifies the model profile in the interface and is not sent to the model service. It cannot be empty or duplicate an existing name (case-insensitive). Names can contain Chinese characters, spaces, letters, numbers, and common symbols.
- A profile with missing required information, such as the model ID or token limits, does not appear in the model selection list.
- With "Streaming" on, the reply appears as the model writes it. A long reply can take longer than "HTTP Read Timeout (s)" as long as the model service keeps sending it; the timeout applies only when the service stops sending for that long. Profiles that an earlier iCode version saved with "Streaming" unchecked now stream too; to turn it off, uncheck "Streaming" and save again. If you go back to an earlier iCode version, it treats profiles that this version saved with "Streaming" on as not streaming.
- With "Vision Model" on, images are sent to the model in PNG, JPEG, GIF, or WebP format. An image a tool returns in another format is sent as a short note that the image was left out, and the conversation continues.
- If no usable model profile existed before, the new profile automatically becomes the current model profile after you save it and close the window. If another model profile is already in use, saving a new profile does not automatically switch models.
- To set reasoning effort, prompt caching, or other request fields, use "Chat Options" in the "Extra Options" section. See [Chat Options](./chat-options.md), which also lists settings for common model services. When a save makes a profile a Claude profile on the Anthropic protocol without a cache setting, iCode offers to add one.

### Provide an API key through an environment variable

An API key entered directly is saved locally in the model profile file.

To read the key from an environment variable, enter the following in the "API Key" field:

```text
{{PROVIDER_API_KEY}}
```

Replace `PROVIDER_API_KEY` with the name of the environment variable that holds the key. Make sure the variable is set before starting iCode. Environment variable names can contain only letters, numbers, and underscores, and cannot start with a number.

### Clone a model profile

Clone an existing profile when you need another profile with mostly the same parameters. For example, you might use a different model ID from the same provider, or set different context and output limits for the same model.

1. Select the model profile to copy in the list on the left.
2. Click "Clone".
3. iCode creates and selects a copy containing all fields from the original profile, and generates a unique name for it.
4. Edit the profile name and any fields you want to change.
5. Click "Save".

Clicking "Clone" saves the copy immediately, but does not switch the current model to the copy.

### Edit a model profile

1. Select a model profile in the list on the left.
2. Edit the fields you want to change.
3. Click "Save".

If you edit the model profile currently in use, the updated configuration applies to subsequent requests after you save it and close the configuration window.

### Claude thinking settings

Claude Opus 5.5, Fable 5.1 and Sonnet 5.5 tie the thinking they return to the conversation before it. When that earlier conversation changes, for example after iCode compacts the context, the model service may refuse to read that thinking back. When one of these models uses adaptive thinking (`"thinking": {"type": "adaptive"}` in ["Chat Options"](./chat-options.md#anthropic)) at Anthropic's own address, iCode asks the service to leave out thinking that no longer matches instead of refusing the request. The model then sees less of its earlier reasoning, but the request goes through. On accounts where the service would not otherwise check this, the request makes it check, so such thinking is left out there too.

To change this, open the profile's file in the `models` folder of the iCode configuration directory (`~/.chrys/models/` on macOS and Linux, `%APPDATA%\chrys\models\` on Windows; the file's `name:` line shows the profile name), add a `thinking_block_binding` line with one of these values, and restart iCode:

| Value | What iCode asks the service |
| --- | --- |
| `auto` (default) | As described above. |
| `drop_block` | To leave out thinking that no longer matches, at any address and with any model, when thinking is adaptive or has a fixed budget. |
| `error` | To refuse a request with thinking that no longer matches, at any address and with any model, when thinking is adaptive or has a fixed budget. |
| `off` | Nothing. A `block_binding` you write into `thinking` in "Chat Options" is still sent as written. |

If the service still refuses a request because of thinking that no longer matches, iCode sends the request once more without the earlier thinking it read back, and doesn't send that thinking again. This costs one extra request, and the model no longer sees that earlier reasoning. With `error`, iCode reports the refusal instead.

With fixed-budget thinking (`"type": "enabled"`) at Anthropic's own address, iCode also turns on thinking between tool calls, except on Claude Haiku 4.5 and Claude Opus 4.6, which don't support it in that mode. To turn this off, add the line `auto_interleaved_thinking: false`.

Saving the profile in the "Model Configuration" window keeps these lines.

### Delete a model profile

Deleting a profile removes its local model profile file and **cannot be undone in iCode**. Once you are sure you no longer need the profile:

1. Open the "Model Configuration" window.
2. Select the profile to delete in the list on the left.
3. Click "Delete".
4. In the confirmation window, check the profile name again, then click "Delete".

iCode keeps at least one model profile, so "Delete" is unavailable when only one remains. If you delete the profile currently in use, iCode automatically switches to another selectable profile after you close the configuration window.

## Switch and verify model profiles

### Switch the current model profile

You can switch model profiles by clicking the model profile name or using the input field.

To switch by clicking the model profile name:

1. Click the model profile name in the status bar above the input field to open the model selection window.
2. Click a model profile, or select one with the Up and Down arrow keys and press Enter to confirm.

To switch quickly from the input field:

1. Enter `$` in the input field. The model profile list appears above it.
2. Continue typing the profile name to filter the results.
3. Use the Up and Down arrow keys to select a model profile, then press Enter to confirm.

After switching, the status bar shows the new model profile name. The profile currently in use appears in gray in the list, with a hollow circle before its name. Profiles with missing required fields do not appear in the list.

The conversation continues with the new profile. Some model services return the model's reasoning in a form only that service can read back, such as Claude's thinking or OpenAI's encrypted reasoning. If the new profile sends requests to a different service address, iCode leaves that earlier reasoning out of its requests. Your messages, the model's replies and the results of tools iCode ran are still sent; results of tools the model service ran itself in the same step may be left out too. Switch back to a profile with the original address, and that reasoning is sent again. Reasoning saved by earlier iCode versions is still sent as before.

If the model profile name in the status bar is grayed out, the current agent is bound to a specific model profile, and clicking it only shows a notice about the binding. In this case, entering `$` does not display the model profile list either. You must first [change the agent's model settings](./agents.md#set-the-model-an-agent-uses) before you can switch model profiles. You also cannot switch while the agent is running.

### Verify a model profile

After switching models, submit the following in the input field:

```text
Hello
```

iCode should respond with a greeting. If the model service returns an error, first check the model ID, base URL, API key, and other model profile settings.

If the error persists, compare the symptoms against the following cases:

- **The error message contains `The Chat Completions client supports only n=1`**: With the Chat Completions style, iCode processes only one response per request and does not support multiple candidate responses, so the `n` parameter must be omitted or set to the integer `1`. Remove the value or set it to `1` in the profile's ["Chat Options"](./chat-options.md), including when it was set indirectly through `extra_body`.
- **The response has an `HTTP 200` status but is not a valid model reply**: for example a `text/html` Content-Type with an error page, or an empty body. This means a gateway or proxy wrapped a backend error as a successful response; check that the selected API style matches the model service address, and review the proxy route.
- **With the Chat Completions style, the error message starts with `Error code: 200`**: the model service or a gateway reported an error inside a successful response. iCode shows the service's error text. It retries errors that may pass on another attempt, such as a busy service; it does not retry errors another attempt would meet again, such as an invalid API key, an exhausted quota or a rejected request.
- **With the Chat Completions style, the error says `Response contained a tool call without a function name.`**: the model asked for a tool without naming it. iCode runs nothing from such a reply and asks the model again; this error appears only when the next reply does the same. Send your message again, or try another model.
- **The model service's content filter stopped the reply**: iCode does not retry the request, and it does not run any tools that a filtered or refused reply asked for. Rephrase the request.
- **With the Responses style, the model service reports that the reply failed**: iCode shows the service's error. It retries only errors that may pass on another attempt, such as a server error or a rate limit; it does not retry a request the service rejects, such as one with an image it can't read or one over the account's quota. A streamed reply that stops before it is finished is retried the same way. A request over the context window is the exception: see [If iCode can't reach the model service](#if-icode-cant-reach-the-model-service). iCode does not retry when the service already ran a tool that may have changed something for the failed reply, such as an MCP call, so that the tool does not run twice. If a service never says whether a streamed reply is finished, iCode keeps what it received and writes a warning to the log.
- **With the Anthropic protocol, a streamed reply stops before it is finished**: iCode retries the request and does not run the tools that the interrupted reply asked for. As with the Responses style, it does not retry when the service already ran a tool that may have changed something, such as an MCP call.
- **Streamed replies sometimes stop partway without an error**: some services using the Chat Completions style end a stream without saying that the reply is finished. iCode keeps such a reply. To have iCode treat it as an interrupted reply and retry, open the profile's file in the `models` folder of the iCode configuration directory (`~/.chrys/models/` on macOS and Linux, `%APPDATA%\chrys\models\` on Windows; the file's `name:` line shows the profile name), add the line `stream_requires_finish_reason: true`, and restart iCode. Saving the profile in the "Model Configuration" window keeps this line.
- **Requests fail with "Streaming" on, and the model service or gateway doesn't support streaming**: uncheck "Streaming" in the model profile and save. If you edit the profile's file instead, add the line `stream: false` and restart iCode. Turning streaming off helps only when the service can't stream.

### If iCode can't reach the model service

When iCode can tell why a model request failed, it says so in plain words, with the original error text below it; a paused sub-agent's card shows both as well. While iCode retries, the retry notice shows only the plain-words message. If the device running iCode doesn't seem to have a network connection, iCode says so as well; check that first.

| The message says | What to check |
| --- | --- |
| Can't resolve the address | Your network connection and DNS. If the address is wrong, fix the base URL in the model profile. |
| The host refused the connection | The address and port in the base URL, and that the model service is running. |
| Connecting or waiting for a response timed out | Your network, firewall, or VPN. |
| Can't connect to the proxy | That the proxy is running and set up correctly. To connect without the proxy, turn on **Bypass proxy** in the model profile. |
| The proxy requires authentication | The proxy's user name and password. |
| Couldn't establish a secure connection | On a corporate network, the proxy's certificate setup. |
| The API key is invalid or lacks access | The API key in the model profile. |
| The request exceeds the model's context window | That the context window in the model profile matches the model's real one. With context compaction on, iCode compacts the context and sends the request once more; this error appears only if that also fails. See [Configure context compaction](./compaction.md). |
| The model profile's maximum context window is larger than the server's limit | Set "Max Context Window" in the model profile to the server's limit that the message names, or less, and save. |
