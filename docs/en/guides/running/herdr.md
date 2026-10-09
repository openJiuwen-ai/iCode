# Use iCode inside Herdr

[Herdr](https://herdr.dev) is a terminal multiplexer built for coding agents: it arranges agent sessions in panes, watches their state, and notifies you when they need you. When you run `icode` inside a Herdr pane, iCode tells Herdr what it is doing automatically. There is nothing to enable or configure on either side.

## What you see

Once the TUI starts in a Herdr pane:

- Herdr's sidebar lists the pane as the agent **icode**, and `herdr agent list` shows it with its state.
- The state follows your session:
  - **working** — a turn is running, for example after you submit a prompt.
  - **blocked** — iCode is waiting for you: a tool is waiting for approval, or the agent asked you a question.
  - **idle** — the turn has finished, including when you interrupt it with Ctrl+C.
- When the pane sits in a background tab, Herdr can send you a system notification and play a sound when a turn finishes or needs your decision. Notification and sound settings live in Herdr.
- Herdr commands that wait on an agent's state, such as `herdr agent wait`, work on the pane.

## Resume after a Herdr restart

While a session is open, iCode also tells Herdr which session the pane is running. If Herdr's server restarts, the pane runs `icode --session <session-id>` on its own and you return to the same conversation.

> **Note**
>
> Session resume needs Herdr 0.9.2 or newer. With an older version the state reports still work; only the automatic resume does not.

## Outside Herdr

The integration is inert outside Herdr. Without Herdr's environment variables, iCode starts no extra processes and behaves exactly as in any other terminal.
