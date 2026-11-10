# Show iCode's status in your terminal

When the iCode terminal interface runs inside a terminal that speaks the Program Status Protocol (OSC 7501), iCode continuously tells the terminal what it is doing: idle, working, waiting for you, finished, or failed. The terminal can then show that status wherever it likes — a badge on the tab, a status list, a notification when a reply is ready. You get to work in another window and still see at a glance what iCode is up to.

This works automatically. There is nothing to enable and no configuration: as soon as iCode starts, it begins reporting, and it keeps the terminal's picture current through every turn.

## What iCode reports

| iCode moment | Reported state |
| --- | --- |
| iCode started, a running turn was interrupted, or a workflow run was cancelled | `idle` — waiting for your next message |
| A reply or a workflow run is in progress | `working` |
| iCode is waiting on you: a tool approval, an answer to a question (including a workflow node's question), or your retry-or-abort decision on a failed sub-agent task or workflow node | `blocked` — with the tool name, the question text, or the failed task's name as the message |
| A turn finished or a workflow run completed | `done` — the message is the opening line of the reply, when there is one |
| The turn or workflow run failed | `error` — the message is the first line of the error |

While several decisions are pending at once — two approvals, an approval plus a question — the status stays `blocked` until you have answered the last one. When you answer an approval or a question, the status returns to `working` for the rest of the run, then lands on `done` or `error`.

`done` appears as soon as the reply is readable in iCode; the session save and the end-of-turn hooks may still be running at that moment.

When you continue an interrupted turn, reporting returns to `working` as soon as the retry takes over, even while the previous turn is finishing cancellation. Creating or restoring a chat or workflow session, or switching between chat and workflow mode, clears the previous result.

## Where it shows up

Terminals that implement the protocol decide how to present it; check your terminal's documentation for whether it consumes OSC 7501 and what it does with the reports. Terminals that do not know the protocol ignore it completely, so iCode's reporting never disturbs them. If you run iCode inside a terminal multiplexer, the multiplexer is the terminal iCode reports to — the status only reaches your outer terminal if the multiplexer passes the protocol through.

What iCode sends is the state, the stable program identifier `iCode` (`app=iCode`), and, where noted above, a short single line of text (a tool name, a question, the opening of a reply or an error). When you quit iCode, it reports `idle` one last time: quitting is taken as having seen the result, so a finished run does not keep notifying after you left. This also applies when you quit with a workflow error dialog still open.

Reporting is enabled only in the native TUI, including its chat and workflow modes. Browser sessions (`icode serve`), headless commands (`icode run` and `icode workflow run`), and the ACP server (`icode acp`) do not send OSC 7501 reports.
