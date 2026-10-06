# Use the local Buddy JSON interface

`icode buddy --json` lets a local frontend read and update the same Buddy save used by the TUI. Each invocation reads one UTF-8 JSON request from standard input until EOF and writes one JSON response to standard output. Requests may be at most 64 KiB. No model is called.

Every request must contain `"protocol": "chrys-buddy-v1"` and an `action` from this table. Only `rename` accepts an additional field, `name`; other fields and protocol versions are rejected.

| Action | Result |
| --- | --- |
| `status` | The saved Buddy, or `null` on first use |
| `catalog` | Built-in artwork and animation timing for every species |
| `hatch` | Hatch a Buddy if none exists; otherwise return the current one |
| `pet` | Increment the saved Buddy's pet count; requires a hatched, unmuted Buddy |
| `rename` | Set the Buddy's name using a nonempty text `name` field; requires a hatched Buddy |

For example, on macOS or Linux:

```sh
printf '%s' '{"protocol":"chrys-buddy-v1","action":"status"}' | icode buddy --json
printf '%s' '{"protocol":"chrys-buddy-v1","action":"rename","name":"Chestnut"}' | icode buddy --json
```

Except for `catalog`, a successful response contains `protocol` and `buddy`. A non-null `buddy` contains the saved `record` and derived `level`, `rarity`, `shiny`, `traits` and `progress`. Renaming uses the TUI's name cleaning and 24-character limit.

The catalog response contains `protocol` and `artwork`. Each species entry contains `species`, `width`, `height`, `frames`, `restTicks` and `holdTicks`. The nine 20×16 frames are three idle poses, three petting poses and three blinking idle poses, in that order. Each frame is a flat array of 1,280 RGBA bytes in row order. Artwork comes from iCode's packaged sprites; custom PNG overrides are not included.

The interface uses the TUI's signed save under `~/.chrys/extras/buddy/buddy.json` on macOS/Linux or `%APPDATA%\chrys\extras\buddy\buddy.json` on Windows, and the same writer lock. It reads only the primary save and writes no backup. Damaged or unreadable saves, and a missing primary with an existing backup, fail explicitly instead of recovering from the backup or overwriting the damage. On invalid requests or storage errors, the response contains `protocol` and `error`, and the command exits with status 1. Diagnostics go to standard error.
