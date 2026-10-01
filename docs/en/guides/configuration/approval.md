# Configure approval modes

When an agent makes a tool call, such as running a Shell command or writing a file, iCode may require approval first. This guide explains how to choose an approval mode and handle approval requests in the terminal user interface (TUI), as well as how approval settings differ across ways of running iCode.

## Understand the three approval modes

The approval mode determines how iCode handles tool calls that require approval. Which calls require approval depends on both the `approval` policy in the agent profile and iCode's safety rules. See the [approval field in the agent profile reference](../../reference/agent-profile.md#approval).

The three approval modes behave as follows:

| Mode | Behavior |
| --- | --- |
| MANUAL | Tool calls that require approval open a dialog and wait for a person to approve or decline. |
| AUTO | An approval judge model evaluates tool calls. Calls judged safe are approved automatically; suspicious calls are flagged for a person to decide. |
| BYPASS | Tool calls run without asking, even when the agent configuration or safety rules require approval. |

**Approval judge model**: In automatic mode, iCode calls the approval judge model and sends it the current time, the workspace directories, all user prompts of the current turn and the latest of them, and the tool name, tool kind, and arguments. By default, the approval judge uses the current session's model. To change it, press **F10** to open **Settings**, select the **Models & Agents** tab, and change **Approval judge model** in the **Model roles** section.

> **Tip**
>
> **Manual mode does not mean every tool call opens a dialog.** It means that calls requiring approval are decided by the user. Calls that do not require approval or qualify for automatic approval run directly. See [Understand automatic approval and safety protections](#understand-automatic-approval-and-safety-protections).

## Switch approval modes in the TUI

Use either of these methods to switch the current approval mode in the TUI:

- Type `/approval` in the input field, then press **Space** or **Enter** to open the approval mode list and choose a mode. You can also specify a mode directly, for example `/approval auto`.
- Click the approval mode label in the upper-right corner of the interface and choose a mode in the **Approval Mode** dialog.

You can switch modes while a task is running. Approval requests that are already open are unaffected; subsequent tool calls use the new mode.

### Set the default approval mode

With the default settings unchanged, the TUI starts in manual approval mode on its first launch. Switching the current approval mode also updates the default for the next launch: choosing manual or automatic mode saves that mode as the default. Choosing bypass mode applies only to the current run; to avoid continuing to bypass approval protections after a restart, the default for the next launch is saved as automatic mode.

To change only the default for the next launch without changing the current approval mode, press **F10** to open **Settings** and change **Default approval mode** on the **Security** tab. **Settings** does not offer bypass mode as a default that can be saved.

## Handle approval requests in the TUI

Tool calls that require approval open an **Approval Required** dialog showing the tool name and call arguments. For file edits, it also shows the planned diff so you can review changes before they are made.

- Press **Y** to approve or **N** to decline, or click the corresponding button. You cannot close the dialog with **Esc**; you must explicitly approve or decline.
- When declining, you can provide a reason. The reason is sent to the agent to help it adjust its next steps. Once a reason is entered, the approve button is disabled.
- In automatic mode, the dialog initially shows **Evaluating**. If the model judges the call safe, the dialog closes automatically. If the model judges it suspicious, the title changes to **Flagged by Auto-Review** and the dialog shows the reason and waits for a person to decide. You can also approve or decline directly while evaluation is in progress. If the approval judge model is unavailable or evaluation fails, iCode keeps the dialog open for a person to decide instead of approving the tool call automatically.

## Understand automatic approval and safety protections

Enable minimal DAA with `CHRYS_DAA_MINIMAL=1` or by setting `approval.daa_minimal` to `true` in your [user settings](../../reference/settings.md). When an eligible approval dialog appears, choose a session or project grant for exact reuse, or a literal command prefix. The default remains a one-time approval.

DAA is currently available only to the main agent. Sub-agents and workflow nodes do not reuse or save DAA grants; they follow their existing approval policy and mode, including automatic approval and bypass where applicable.

When minimal DAA is enabled, editing a request approves that edit once and does not save the original request as a reusable grant. The edited arguments run through `before_tool_call` hooks. If a hook changes them further, iCode asks you to confirm the resulting request without running the same transformation again. Arguments outside DAA's supported JSON identity format can use ordinary per-call approval only if the confirmation snapshot supports their types (see below); they cannot reuse or create a DAA grant.

Session-scoped DAA grants follow the session ID and survive an agent rebuild or restoration of that same session. A different session does not inherit them. Historical session grants are currently retained in the DAA store; closing an approval dialog or rebuilding an agent does not delete them.

File grants bind to the physical destination after resolving directory symlinks. Changing a link target requires new approval; the destination is checked again in the write/edit worker and the operation uses the confirmed physical path. A final-component symlink stays on ordinary approval because atomic replacement replaces the link itself. This one-time approval still tracks the physical parent and final entry, plus the link's current target: changing either while approval is pending requires fresh approval, and changes after handoff are rejected by the worker. An unresolved target never disables that guard. Older file grants without the physical-path version marker are ignored and must be approved again; command grants are unchanged. These checks do not provide filesystem sandbox isolation against another process concurrently replacing directory entries during an OS write.

Per-call confirmation uses a separate, type-preserving snapshot. It supports built-in strings, booleans, integers, floats (including non-finite values), `None`, bytes, bytearrays, dictionaries, lists, tuples, sets and frozensets, plus `PurePath`/`Path` and enums, with nested values restricted to these types. Enum snapshots retain the enum class, member name and frozen value; valid StrEnum, IntEnum and ordinary Enum parameters reach normal approval. Changing supported arguments while approval is pending requires fresh approval, even when they cannot form reusable DAA keys.

With DAA enabled, calls reaching this confirmation step with `date`/`datetime`, `UUID`, `Decimal`, Pydantic model instances, other unsupported host objects or cyclic values are rejected before a dialog opens with `Tool arguments cannot be safely compared for approval.` Custom tools that need these types must disable minimal DAA or accept supported argument types. This restriction does not apply when DAA is disabled.

The following operations usually run without an approval dialog:

- Safe, read-only Shell commands that do not access sensitive targets, such as `ls`, `cat`, and `grep`.
- File writes within the working directory's Git repository that do not access sensitive targets.

Shell commands and file reads or writes that access sensitive targets such as `.env` files, credentials, and private keys still go through approval, even for read-only operations or writes within a Git working directory. This protection applies in manual and automatic modes. Bypass mode skips these approval protections.

Skill scripts run locally with the current user's permissions, without sandbox isolation, so they request approval by default. When the session uses bypass mode, skill scripts run without asking. Before installing or running a third-party skill, review its `SKILL.md`, scripts, and related files to make sure they are trustworthy.

Web tools send requests to outside services, so the `web_search` and `web_fetch` kinds also request approval by default, even when the agent's `approval.default` is `auto`. Explicit `approval.overrides` rules for these kinds or tool names still take precedence. In automatic mode the approval judge model may approve them, and in bypass mode they run without asking. See [Configure web tools](./web-tools.md#approve-web-tool-calls).

## Verify approval modes in the TUI

Select the built-in Code agent, then submit this request:

```text
Use the Shell tool to run icode --version
```

This command only displays the version and does not modify files, but it is not among the read-only Shell commands approved automatically. Expect the following results:

- In manual mode, the **Approval Required** dialog opens. After approval, the iCode version is displayed.
- In automatic mode, the approval judge model will usually judge the command safe and approve it automatically. If the model flags it as suspicious, the dialog shows the evaluation reason and waits for a decision.
- In bypass mode, the command runs and displays the iCode version without an approval dialog.

## Approval modes in other ways of running iCode

Other ways of running iCode use the following approval modes and switching methods:

- **Headless CLI (`icode run`)**: Always bypasses approval and provides no approval-related options.
- **iCode ACP server**: Defaults to manual mode. Use `icode acp --approval manual|auto|bypass` to set the initial mode. ACP clients that support this capability can also switch the current session's mode.
- **Browser-hosted TUI (`icode serve`)**: Use the TUI operations described earlier to switch approval modes and handle approval requests.
