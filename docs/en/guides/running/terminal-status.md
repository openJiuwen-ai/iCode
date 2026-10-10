# Show iCode's status in your terminal

When the iCode terminal interface runs inside a terminal that speaks the Program Status Protocol (OSC 7501), iCode continuously tells the terminal what it is doing: idle, working, waiting for you, finished, or failed. The terminal can then show that status wherever it likes — a spinner or badge on the tab, a status list, a notification when a reply is ready. You get to work in another window and still see at a glance what iCode is up to.

This works automatically. There is nothing to enable and no configuration: as soon as iCode starts, it begins reporting, and it keeps the terminal's picture current through every turn.

## What iCode reports

| iCode moment | Reported state |
| --- | --- |
| iCode started, or a running turn was interrupted | `idle` — waiting for your next message |
| A reply or a workflow run is in progress | `working` |
| iCode is waiting for your decision: a tool approval or an answer to a question | `blocked` — with the tool name and a short summary, or the question, as the message |
| A reply finished and you have not viewed it yet | `done` — with the first line of the reply as the message |
| The turn failed and stopped | `error` — with the first line of the error as the message |

When you answer an approval or a question, the status returns to `working` for the rest of the turn, and then lands on `done` or `error`.

## Where it shows up

Terminals that implement the protocol decide how to present it. Ghostty and Rex understand it today: for example, an unfocused Ghostty tab running iCode shows a spinner while the agent works and a marker when it finishes or fails, and terminals can offer a notification when a task is done.

Terminals that do not know the protocol ignore it completely, so iCode's reporting never disturbs them. If you run iCode inside a terminal multiplexer, the multiplexer is the terminal iCode reports to — the status only reaches your outer terminal if the multiplexer passes the protocol through, so check its documentation.

Two cases never report: iCode served in a browser (`icode serve`), because a browser page is not a terminal, and the headless `icode run`, which reports its progress as plain text instead.
