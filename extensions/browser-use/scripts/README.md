# Manual smoke gates

These scripts launch real Chrome or a real Broker; they are not part of `pytest`.
Except for `wheel_extension_smoke.py` and `local_origin_smoke.py` below (which
set up their own isolated environment or import path), run them from the
repository root with the project venv and `PYTHONPATH=src`
(PowerShell: `$env:PYTHONPATH='<repo>\src'`).

| Script | Proves |
| --- | --- |
| `extension_broker_smoke.py` | plugin BrokerSession: navigate → snapshot → find → close |
| `extension_attach_smoke.py` | attach mode: two `browser_id`s, two tabs, close one keeps the other |
| `extension_stale_ref_smoke.py` | navigation invalidates old refs with `browser_stale_ref` |
| `extension_cancel_concurrent_smoke.py` | two same-owner browsers isolated; cancelled close recovers |
| `broker_lifecycle_smoke.py` | lease TTL reacquire and Broker idle exit |
| `broker_action_status_smoke.py` | click in Broker then action_status lookup through the same Broker |
| `broker_isolate_smoke.py` | multi-lease refusal, per-session close isolation, client rebuild reuse |
| `broker_smoke.py` | Broker session reuse and multi-session isolation |
| `browser_bridge_smoke.py` | Chrome extension bridge/native host; Chrome 137+ removed `--load-extension`, so pass `--profile <dir>` with the unpacked extension already loaded (the script refuses to wait when neither applies) |
| `browser_response_lost_smoke.py` | lost/late bridge response handling |
| `browser_worker_smoke.py` | MV3 worker restart handling |
| `extension_optional_smoke.py` | opt-in evaluate/save_state: default hidden, env enabled, approval, real evaluate/save |
| `icode_extension_smoke.py` | clean iCode: install wheel, open→navigate→snapshot→screenshot→view_image→click→close |
| `wheel_extension_smoke.py` | specified wheel + pinned host in a temporary venv: native discovery/binding, configuration isolation, Owned isolation, navigation/snapshot/screenshot, evaluate exception and close |
| `local_origin_smoke.py` | local dev policy: authorised loopback origin loads and calls its API, a second port is refused for navigation, fetch and redirect (its server never receives a request); the action journal records the upload and answers a query |
| `browser_recovery_smoke.py` | real Chrome: `browser_list`/`browser_status` do not launch, a recorded handle is reopened from the same profile as a new page, and close removes its record |
| `fixture_site.py` | the loopback fixture site used by the checks and by the host four-path test (counted submit/upload, delayed response, tall document with a scrolled bottom button for full-page and clipped screenshots, DOM replacement, popup/new-tab links, a cross-origin OAuth start/authorize/callback chain, alert/confirm/prompt pages plus a late alert and a post-dialog report endpoint, a download-links page with plain/traversal/reserved/too-big/slow attachments, cross-site iframe, download, a chat-style SPA at `/chat` with icon-only sidebar links, a `contenteditable` composer behind a CSS placeholder, a send button whose centre is covered by a transparent layer and a streaming answer with a Stop button, `/shadow-nested` open+nested closed Shadow DOM with occlusion contrasts, `/async-toggle` delayed `input[type=submit]` and self-detaching confirm modal, `/dynamic-comments` 80 ms ad ticker with delayed closed-shadow comments, `/state`) |
| `fixture_extension_smoke.py` | real Chrome on the fixture site: one submit (server counter), fused cross-site iframe click, delayed DOM replacement, slow load, popup does not drive the page, download is denied |
| `smoke_common.py` | shared path discovery: `CHRYS_CHROME` or plugin discovery for the browser, `CHRYS_ICODE_SRC`, `ICODE_SRC` or the containing iCode checkout |
| `export_extension_pack.py` | builds the offline folder package (`extensions/browser/`): vendors the core, writes the host-facing `__init__.py` and `doctor.py`, and fails if a copied module would need an installed wheel |
| `extension_pack_smoke.py` | exported folder package in a temporary venv with only the pinned host: no wheel or checkout importable, the pack's own `doctor.py` reports the folder origin and the Broker advice, and a real browser runs from the pack (Owned closes, Attach leaves the user's browser alive) |
| `screenshot_smoke.py` | real Chrome through the adapter: unique/atomic screenshot files with format/pixel/byte/session-relative reporting, `full_page` grows, `target` clips, `ICODE_BROWSER_SCREENSHOT_KEEP` prunes only the plugin's files, and iCode's own `view_image` returns the image; `--real-model` runs a real-model turn separately (status only; the trajectory tells whether it chose `view_image`) |
| `interaction_smoke.py` | real Chrome through the adapter: Ctrl+A is observed with its modifier and a plain key replaces the selection, Enter submits the form once (server counter), native single/multiple `<select>` values and labels are read back and fire `input`/`change`, a delayed element and a text token that is absent before the click are waited for, a URL change is followed, a stale or replaced `@ref` fails fast for `visible` and counts as `hidden` while the new node stays usable, a cross-site iframe ref receives Enter, a timeout explains itself, cancelling the wait releases the action lock, and the Owned browser exits |
| `tabs_smoke.py` | real Chrome through the adapter: a `target=_blank` tab is listed with its opener and not made current, `browser_tab_switch` snapshots it and switching back restores the original page, a cross-origin OAuth chain opens provider pages with nested openers, completes and closes itself back to its opener, a page created outside the plugin is never adopted/driven/closed, an unauthorised popup is blocked before it reaches its server, and the Owned browser exits |
| `dialog_smoke.py` | real Chrome through the adapter: an action that opens `alert`/`confirm`/`prompt` returns `blocked` (never `succeeded`) with the dialog type and message, `browser_dialog` accept/dismiss (and a prompt answer) reaches the page, page actions are refused with `browser_dialog_open` while `browser_tabs` still works, a dialog opened by a page timer is caught, an unhandled dialog is auto-dismissed with its reason, and the Owned browsers exit |
| `download_smoke.py` | real Chrome through the adapter: an opt-in download completes with state/name/bytes/session path and matching bytes, a traversal filename stays inside the folder, a reserved name is prefixed, the size limit cancels and cleans up, `browser_download_cancel` stops a slow transfer, and without the opt-in nothing is written or tracked |
| `chat_spa_smoke.py` | real Chrome through the adapter on the `/chat` fixture, using only the preferred tools (never `browser_evaluate`): every check is recorded red or green, and the run passes only when the red set equals `--expect-red` exactly, the server-side send counter equals the sends observed, and Chrome exits |
| `snapshot_density_smoke.py` | real Chrome through the adapter on the `/dense` fixture: three same-name text siblings fold to one `×3` line while `browser_find` still returns all three refs, an unnamed empty node is not printed but stays findable, an unnamed scroller keeps its `*` marker, `browser_snapshot(target=…)` prints only that subtree and names it in the header, and the Owned browser exits |
| `logs_smoke.py` | real Chrome console and network logs, sensitive value redaction, and incremental reads with `since` |
| `history_tabs_smoke.py` | real Chrome back, forward, reload, popup close, and last-tab protection |
| `agent_chat_smoke.py` | **model-level** icode-browser-use acceptance: hands a plain user task to a real agent (`python -m chrys.app.cli.app run -t … -a BrowserExt`) on the `/chat` fixture and judges only the model's behaviour — exactly one message submitted (server counter), no `browser_evaluate`, a bounded number of tool calls, and the streamed answer sentence reported verbatim. Tool progress is read from stdout **and** stderr (iCode 0.36 writes it to stderr) |
| `shadow_dom_smoke.py` | real Chrome on `/shadow-nested`: open Shadow DOM extract/click, nested closed Shadow DOM `browser_fill`/`browser_click`/`browser_extract` fallback, and shadow-internal vs. light-DOM occlusion contrast |
| `w13_reliability_smoke.py` | unified W13 real-Chrome reliability gate across `/shadow-nested`, `/async-toggle`, `/dynamic-comments`, `/tall` (scrolled target clip), tab foreground/background status, and clean Chrome exit |

`icode_extension_smoke.py` requires the plugin installed into iCode's interpreter,
either as a wheel or in editable mode. Browser paths are discovered; `scripts/smoke_common.py` reads
`CHRYS_CHROME` (or the plugin's own discovery) for the browser and
`CHRYS_ICODE_SRC` or `ICODE_SRC` for the host checkout, with the containing iCode
checkout as the default.

The host's opt-in four-path test (`iCode/tests/integration/test_browser_four_paths.py`)
imports this fixture site and the plugin source directly. It carries only
`pytest.mark.timeout(180)` (the hygiene guard forbids the `integration` marker under
`tests/integration`) and every case skips unless `CHRYS_BROWSER_E2E=1`. It uses
`extensions/browser-use/` by default and accepts `CHRYS_BROWSER_SRC` to select
a different checkout. It uses `CHRYS_CHROME` or discovers the browser automatically.

These scripts execute whatever `CHRYS_CHROME`, `ICODE_BROWSER_EXECUTABLE` or
`CHRYS_ICODE_SRC` name, and insert that checkout on `sys.path`: point them only at a real
browser and at your own checkout.

## Isolated installed-wheel smoke

Build the wheel first. With `uv`, Python and Chrome/Edge available, run:

```powershell
python scripts/wheel_extension_smoke.py --wheel dist/icode_browser_use-0.3.2-py3-none-any.whl --host-version 0.35.2 --allow-public-network
```

`--wheel` selects the exact existing artifact; the runner installs it together
with `iCode-TUI==0.35.2` and the host's dependencies into a temporary uv venv,
not the project venv or the user's iCode installation. Add `--executable
"C:/path/to/chrome.exe"` to override browser discovery. It clears source-path
injection and inherited browser settings, then uses isolated Python processes
and temporary browser state/profile directories.

`--allow-public-network` explicitly permits this smoke's IANA page access and
test-only evaluate check; package installation also needs access to its package
source. The smoke does not relax the browser network policy or enable local
origin support. JSON lines record the wheel SHA256, installed versions/import
paths, checks and owned-process cleanup; retain stdout as evidence. It also runs
the wheel's own `icode-browser-use doctor --json` (install `distribution`, host API
`ok`, Broker startable) and finishes by uninstalling the wheel and requiring
that neither the package nor the `chrys.extensions` entry point remains.

This is an installed-wheel, native tool-binding smoke, **not four-path
acceptance** for TUI/headless/sub-agent/workflow. It does not prove real host
approval/orchestration, screenshot-to-`view_image`, or Attach/Bridge/Broker
coverage. Running it is not a W02+ or M2 sign-off; final results must be recorded
by the main agent after execution.

## Standalone folder package

```powershell
python scripts/export_extension_pack.py --out dist
python dist/extensions/browser/doctor.py --json
python scripts/extension_pack_smoke.py --host-version 0.35.2 --allow-browser
```

The exporter writes `extensions/browser/`: the core vendored under
`icode_browser_use/` (relative imports only), a root `__init__.py` that exposes
`extension`, and a `doctor.py` that loads the core without executing the pack
root (so an older host still gets a report). `extension_pack_smoke.py` creates a
temporary uv venv with only `iCode-TUI` at the pinned version, proves neither the
wheel nor the checkout is importable there, installs the exported folder with its
own `doctor.py install`, runs diagnostics from the **installed** copy, loads it
through iCode's own `discover_extensions()`/file loader, and drives a real
browser from it against the loopback fixture site: Owned (the plugin launches and
closes the browser) and Attach (closing plugin resources leaves the browser the
script started alive). It then uninstalls with `doctor.py uninstall` and requires
the state folder to survive. Bridge and Broker need the wheel's
`icode-browser-use-host`/`icode-browser-use-broker` commands, so the pack's doctor
reports those backends instead of starting a command that cannot work.

Neither smoke is four-path acceptance.

## Keys, selects and conditional waiting

```powershell
python scripts/interaction_smoke.py --allow-browser
```

Drives the adapter against real Chrome on the loopback fixture site (with a
cross-site iframe) and checks what W08 promises: `Ctrl+A` reaches the page with
its modifier (observed in the page's own key log) without being claimed as proven
by unrelated mutations, and a following plain key replaces the selected text;
`Enter` inside the text field submits the form exactly once (the server counts
one POST) and reading the page afterwards does not submit again; native single
and multiple `<select>` elements select by value and by visible label, report the
values read back from the page, and fire `input`/`change`; `visible` finds an
element that appears after a delay, `text` sees a token that was absent before
an asynchronous DOM replacement, and `url` follows a slow navigation; a stale
`@ref` fails fast with `browser_stale_ref` for `visible` and counts as `hidden`;
a node replaced by a new one behaves the same way while the new ref works; an
iframe ref is found and receives `Enter` (the iframe's own server counts the
handler); a timeout reports its last observation within its budget; cancelling
the waiting tool task releases the action lock; and the Owned browser process
exits after `browser_close`.

## Tabs, popups and OAuth

```powershell
python scripts/tabs_smoke.py --allow-browser
```

Drives the adapter against real Chrome with two loopback origins (the page and an
OAuth-style provider) and checks what the tabs sub-step promises: a
`target=_blank` link opens a second tab that is listed with its opener and is not
made current; `browser_tab_switch` returns a snapshot of it and switching back
restores the original page; an OAuth chain across both origins opens provider
pages with nested openers, completes on a callback page, closes itself, and the
session returns to the tab that opened it; a page created outside the plugin is never adopted, driven or closed, and an unauthorised popup opened without an opener is still blocked before it reaches the server; and the Owned
browser process exits after `browser_close`.

## JavaScript dialogs

```powershell
python scripts/dialog_smoke.py --allow-browser
```

Drives the adapter against real Chrome on the fixture site's `/dialogs` page and
checks what W09-2 promises: a click that opens `alert`/`confirm`/`prompt` returns
at once with `status=blocked` (never `succeeded`) and the dialog's type and
message, and the journal agrees; `browser_dialog` accepts or dismisses it, the
page then continues (its own post-dialog report reaches the fixture server), a
prompt answer is exactly the `text` passed and accepting one without text is
refused; page actions fail with `browser_dialog_open` while `browser_tabs` keeps
working; a dialog opened by a 600 ms page timer is caught the same way; and with
`ICODE_BROWSER_DIALOG_TIMEOUT=0.4` an unhandled dialog is auto-dismissed and the
next `browser_dialog` reports that reason. Both Owned browsers exit.

## Downloads

```powershell
python scripts/download_smoke.py --allow-browser
```

Drives the adapter against real Chrome on the fixture site's `/download-links`
page with `ICODE_BROWSER_DOWNLOADS=allow` and a 1 MB cap: the plain link completes
and reports `state=completed`, `fixture.txt`, 24 bytes and a path inside the
session's `downloads/` whose bytes match the server payload; a `../../evil.exe`
name stays a single file inside that folder (Chrome's own suggestion is
`_.._evil.exe`), a `CON.txt` name is prefixed, a 2 MB transfer is canceled with a
`size_limit` reason and leaves no file, a 1 MB slow transfer is canceled by
`browser_download_cancel` (a second cancel is refused) with no partial file left,
and a second handle without the opt-in reports downloads as disabled and writes
and tracks nothing (the user's own `Downloads` folder is also unchanged). Both
Owned browsers exit.

## Chat-style SPA baseline

```powershell
python scripts/chat_spa_smoke.py --allow-browser --expect-red ""
```

Drives the W12 chat fixture the way a model is told to: find the conversation
link by href, open it, fill the composer by its accessible name, submit with
`browser_fill(submit=true)`, wait for the answer and extract it, click the send
button under its transparent overlay, press Enter, and wait with
`text_changed`/`stable`. Each check is one `{"event": "check", "ok": …}` line;
`--expect-red` names the checks that are still known to fail, and the run passes
only when the red set matches exactly, so a fix or a regression both change the
outcome. Since batch ③ all 14 checks are expected green: icon-only links are
named by their href, `browser_find` matches hrefs, the composer resolves by text
even though a CSS placeholder shares its name, the covered send button is hit on
a corner point, `dispatch=dom` is refused after a landed click (the page's own
`attempts` counter proves the handler did not run), Enter's send is attributed,
`text_changed` fires on the first streamed token, `stable` waits out the stream,
`timeout_ms=180001` is refused, and `text_changed` on a quiet page times out.
The page's raw send-attempt counter must equal the sends observed by the run,
and the server must have counted the same number.

## Model-level acceptance

```powershell
python scripts/agent_chat_smoke.py --allow-browser --max-calls 25
```

Unlike the smokes above, this one does not call the adapter. It writes a plain
user task ("open `<origin>/chat`, open the second conversation `/chat/c2`, send
`What about tomorrow in Shanghai?`, report the answer sentence") and runs a real
agent in iCode's own interpreter (`python -m chrys.app.cli.app run -t … -a
BrowserExt`). The run passes only when the agent finishes, the fixture counted
exactly one submitted message, `browser_evaluate` was never called, the browser
call count stays within `--max-calls`, and the agent's final report contains the
streamed answer verbatim. The script reads the fixture's counters and the
agent's output only; it never chooses an element or a tool for the model.

`ICODE_HOST_PYTHON` overrides the interpreter it runs (default: the uv tool
environment of iCode-TUI); the plugin under test must be installed there. Do not
use the `icode` shim on PATH for this: it can resolve an older extension.

## Snapshot density

```powershell
python scripts/snapshot_density_smoke.py --allow-browser
```

Drives the adapter against the fixture's `/dense` page and checks the density
rules on a real browser: three same-name non-interactive siblings under one
parent collapse to a single `×N` line (their refs are all still returned by
`browser_find`), an unnamed empty node is not printed but remains findable, an
unnamed scroller keeps its `*`, and `browser_snapshot(target=…)` serializes only
that element's subtree (header records `subtree @eN#epoch`). Interactive
duplicates are never folded; each keeps its own line and ref.

## Screenshots and vision

```powershell
python scripts/screenshot_smoke.py --allow-browser
```

Drives the adapter against real Chrome on the loopback fixture site and checks
what W07 promises: a unique, atomically written file per capture; the reported
format, pixel size, byte size, absolute path and session-relative path;
`full_page` on the `/tall` fixture page; a `target` clip; retention limited to
the plugin's own files (`ICODE_BROWSER_SCREENSHOT_KEEP=2` in the smoke) with a
foreign file left untouched; and iCode's own `view_image` returning image data
whose bytes match the file. `--real-model` additionally runs one headless iCode
turn with a real model and records, separately, whether that model chose
`view_image`; it needs a configured model profile and is not part of the
file/view_image acceptance.

The host's opt-in four-path file also carries a scripted-model case that requires
`view_image` (with `filesystem.read` and a vision model) to hand the model image
content for the plugin's screenshot; it runs only with `CHRYS_BROWSER_E2E=1`.

There is no separate server entry point: the package is only the
`chrys.extensions` plugin, loaded by iCode itself.
