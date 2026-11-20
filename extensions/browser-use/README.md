# browser-use

Browser automation for iCode as a `chrys.extensions` plugin.

The source, browser extension assets, tests, and development scripts live in
`extensions/browser-use/`.

- The plugin uses only iCode's public extension API; it does not require any
  iCode source changes.
- The plugin owns its `BrowserRuntime`, handles, locks, policy and lifecycle,
  then drives Chrome/Edge over CDP.
- The core is pure standard library: an RFC6455 WebSocket client and the CDP
  client itself. There is no Playwright, no Selenium and no bundled Chromium.

## Install

### Into an iCode tool environment

From `extensions/browser-use/`, build the plugin wheel and install it into the
environment that runs `icode`. Run `uv tool dir` to find the tool environments directory.

```bash
uv build
uv pip install --python <iCode-environment-Python> --no-deps <built-wheel>
```

For a uv tool installation, the Python path relative to `uv tool dir` is
`icode-tui/Scripts/python.exe` on Windows and `icode-tui/bin/python` on macOS and Linux.
Then enable `browser` in `/agents tools` or add it to the profile's `extensions` list.
Use one installation method for `browser` in each iCode environment. The plugin's tools are
single-action tools: call `browser_open` first, then pass the returned
`browser_id` to navigate/snapshot/click/fill/scroll/extract/screenshot/close.
Each action goes through iCode's existing approval policy; declarations such as
`requires_approval=True` are not a promise that every call prompts (override,
BYPASS and headless rules still apply).

Public HTTP and HTTPS sites work by default. Set a domain allowlist to restrict
public access or authorize exact loopback origins for local development.

### Offline wheel install

The wheel has no third-party dependencies, so it can be installed into iCode's
interpreter from a folder without network access:

```bash
python -m pip install --no-index --find-links <folder> browser-use==0.3.3
```

On Windows use iCode's interpreter
(`%APPDATA%\uv\tools\icode-tui\Scripts\python.exe`); on macOS/Linux use the
interpreter under `uv tool dir`. The same wheel can be uninstalled with
`python -m pip uninstall browser-use`.

Find iCode's interpreter with `uv tool dir`: on Windows it is
`%APPDATA%\uv\tools\icode-tui\Scripts\python.exe`; on macOS/Linux it is
`~/.local/share/uv/tools/icode-tui/bin/python`. Reinstalling with
`uv tool install iCode-TUI --with ./dist/browser_use-*.whl` also works but
re-resolves iCode's whole environment. The package must be installed into
iCode's environment; a project-local `.venv` is invisible to it.

### Standalone folder package (no Python installer)

For an iCode installed as a standalone binary, export the extension as a folder
package and install it where iCode reads user extensions:

```bash
python scripts/export_extension_pack.py --out dist
python dist/extensions/browser/doctor.py install
```

`install` copies `extensions/browser/` to `<config_dir>/extensions/browser`
(`~/.chrys/extensions` on macOS/Linux, `%APPDATA%/chrys/extensions` on Windows),
where iCode's own extension loader runs it. The folder vendors the core under
`browser_use/` and uses relative imports only, so it needs neither the
wheel nor a source checkout. `python dist/extensions/browser/doctor.py
uninstall` removes it again; browser profiles, logins and action journals in the
state folder stay.

The folder package runs the Owned and Attach backends. Bridge and Broker need
the `browser-use-host` and `browser-use-broker` commands that only the wheel
installs, so the plugin refuses to autostart the Broker from there and the
diagnostics report those backends instead of starting a command that cannot
work.

The extension reads `ICODE_BROWSER_*` from iCode's process environment, not from
the profile; these are plugin settings, and their values are never echoed in
diagnostics.

Leave both network settings unset to browse public sites. Set `ICODE_BROWSER_ALLOWED_DOMAINS`
to restrict public sites or see [Local development](#local-development) for loopback access.
Once either list is configured, only the listed destinations are allowed. Set `ICODE_BROWSER_BROKER=1`
and start `browser-use-broker` in the same state dir for the persistent
Broker; `ICODE_BROWSER_GUARD=strict` / `ICODE_BROWSER_EXECUTABLE` follow the same
rule.

```yaml
extensions:
  - browser

approval:
  overrides:
    # Supplying `overrides` replaces iCode's defaults, so repeat them here.
    shell: require
    filesystem.write: require
    extension.browser: require      # use `auto` to reduce prompts
    # A kind-level rule covers every tool of the extension, including the rows
    # whose own declaration is "no" (snapshot/find/wait_for/hover/scroll, ...).
    # Re-open the individual ones you want unasked:
    extension.browser.browser_hover: skip
    extension.browser.browser_scroll: skip
```

## Diagnostics

```bash
browser-use doctor            # short report
browser-use doctor --json     # one machine-readable document (schema 1)
browser-use doctor --connect  # also dial a running Bridge or Broker
browser-use doctor --bridge   # actively wait for the extension to connect
```

`doctor` is read-only by default: it reads files and process ids, never launches
a browser, and never starts a Bridge or Broker. `--connect` adds a two-second
TCP connection to a discovery file that already exists. `--bridge` starts this
plugin's bridge server in the state folder and waits for the extension's native
host, up to `ICODE_BROWSER_BRIDGE_WAIT` seconds (the extension only reconnects
on its own 30 s alarm); it stops the server again, and a successful probe
records `bridge.handshake` so later tool calls may wait through that alarm. It
reports where the
extension is installed (wheel or folder package, and a conflict when more than
one source provides the `browser` id), whether iCode can import the extension at
all, the iCode version and the extension API members the plugin calls, the
allowlist, the browser executable, the state folder, and the Bridge and Broker
discovery files. The exit status is `0` when no error-severity problem was found
and `1` otherwise.

Two version checks catch a half-done upgrade: the imported plugin version is
compared with the installed distribution (`plugin_version_mismatch` — restart
iCode), and the unpacked extension's manifest version with the installed plugin
(`bridge_version_mismatch` — re-run `browser-use-bridge install`). Both are
warnings, so the exit status stays 0; read the reported advice.

Every problem carries a stable code, a message and the advice that fixes it:
`extension_id_conflict`, `host_missing`, `host_api_incomplete`,
`plugin_import_failed`, `plugin_version_mismatch`, `config_invalid`,
`config_entries_ignored`, `browser_not_found`, `state_dir_unwritable`,
`bridge_not_installed`, `bridge_version_mismatch`, `bridge_not_running`,
`bridge_stale`, `bridge_unreachable`, `bridge_not_connected`,
`bridge_host_missing`, `broker_not_running`, `broker_stale`,
`broker_unreachable`, `broker_config_differs`, `broker_unsupported`.

When the export tool is around, the folder package carries the same command as
`python <pack>/doctor.py`. `browser-use install` and `browser-use uninstall`
manage the folder package itself: `install` refuses to add a second source for
an id the wheel (as visible to this Python) or the single-file layout already
provides, because iCode then loads neither copy; `uninstall` removes the
extension's own folder or file and keeps profiles, logins and journals. Both
return `0` on success; `uninstall` returns `1` when removing failed or when the
`browser` id is still installed afterwards by a wheel this command cannot remove
(it says how to remove it).

### iCode version

The plugin imports only `chrys.extensions` (`Extension`, `ToolCall`, `ToolContext`,
`ToolError`, `TurnContext`) and uses only its `@extension.tool`,
`@extension.context` and `@extension.before_tool` declarations, reading
`ToolContext.agent_name`, `cwd`, `data_dir`, `session_data_dir` and `session_id`.
`data_dir` and `session_data_dir` exist since iCode v0.34.0; `ToolCall`,
`TurnContext`, `@extension.context` and `@extension.before_tool` first appear in
v0.35.1, so that is the earliest release this plugin can run on. iCode-TUI 0.35.2
is the release the tests and the acceptance smokes run against. `doctor` checks
the members at runtime and reports `host_api_incomplete` with the missing names
rather than trusting a version string.

## Local development

A local dev server is reached through an exact origin, not through the public
domain allowlist:

```powershell
$env:ICODE_BROWSER_ALLOWED_ORIGINS = 'http://localhost:3000,http://127.0.0.1:5173'
```

Rules:

- The entry must be `http(s)://host[:port]` with a loopback host
  (`localhost`, `*.localhost`, `127.0.0.0/8` or `::1`). Userinfo, paths, query
  and fragment are rejected, as are private-but-not-loopback addresses: there is
  no global "allow all private hosts" switch.
- Default ports are normalised, so `http://localhost` authorises port 80. At
  request time the name must resolve to loopback only; a re-pointed
  `localhost` entry is refused.
- A different port, host spelling or scheme is a different origin.
  `localhost` and `127.0.0.1` are not interchangeable, and a page that calls an
  API on another port needs that origin listed too.
- When loopback origins are configured, list any needed public domains in
  `ICODE_BROWSER_ALLOWED_DOMAINS`. The HTTP(S) request guard checks subresources,
  iframes, popups and redirects for unauthorised destinations.

Limits worth knowing before relying on it:

- WebSocket handshakes (the usual Vite/webpack HMR channel) are not
  intercepted by the CDP `Fetch` domain, so they are outside this policy. Treat
  the browser as not isolated for WebSocket traffic and keep dev servers bound
  to loopback only.
- Configuring any origin drops the coarse `*://localhost*` and `*://127.*`
  backstop patterns; the per-request check above becomes the only gate for
  those hosts. Service-worker requests are not intercepted by `Fetch`, which is
  why the backstop existed -- prefer the Bridge/Owned modes without origins for
  untrusted pages.
- `scripts/local_origin_smoke.py` verifies an authorised origin, a refused port
  and a refused redirect against a real Chrome.

## Use your everyday Chrome login

Keep your signed-in Chrome 144 or later open and run `python -m browser_use.chrome connect --chrome`
in a terminal. If Chrome's settings page opens, turn on **Allow remote debugging**. The plugin saves
the choice. Select **Allow** when Chrome asks for permission to connect during a browser task.
[Chrome connection instructions](https://developer.chrome.com/docs/devtools/agents/get-started/configuration/#connect-to-an-existing-browser-session)

`python -m browser_use.chrome status` shows which backend new handles will use.
`python -m browser_use.chrome disconnect` clears the saved choice. To revoke Chrome's debugging
permission, turn it off at `chrome://inspect/#remote-debugging`.

## Which login state the agent gets

The three backends differ in whose cookies the agent can use:

| Backend | Login state | First-time setup | Caveat |
| --- | --- | --- | --- |
| Owned (default) | a persistent profile under the state folder | start it headed (the default) and sign in by hand once; later runs reuse it | nothing from your daily Chrome is available until you sign in there |
| Chrome native connection | your running Chrome | connect from iCode and enable remote debugging in Chrome 144 or later | approve new debugging connections when Chrome asks |
| Attach (`ICODE_BROWSER_CDP_URL`) | the profile of the Chromium you attach to | start Chrome with `--remote-debugging-port` and a non-default `--user-data-dir` | Chrome 136+ ignores these command-line flags on its default profile |
| Bridge (`ICODE_BROWSER_BRIDGE=1`) | your running Chrome, as it is | install the native host and the unpacked extension once, then keep Chrome running | the extension drives only the tabs it creates; it never closes or reads your other tabs |

The Broker is a wrapper, not a fourth login source: it runs the backend named in
its own environment, so the login state is whatever Owned/Attach/Bridge would
give there.

Attach and Bridge never launch a browser; Owned owns (and closes) the Chrome it
starts. A Bridge that has never handshaked in this state folder now fails a tool
call within 5 s (afterwards it waits up to `ICODE_BROWSER_BRIDGE_WAIT`, default
35 s, because the extension reconnects on its own 30 s alarm); the error points
at `browser-use doctor --bridge`.

## Reuse an existing browser (attach mode)

Set `ICODE_BROWSER_CDP_URL` to connect to a Chromium instance that is already
running, instead of launching a dedicated one:

```bash
# start a Chromium with a non-default profile and a debugging port.
chrome --remote-debugging-port=9222 --user-data-dir="C:\path\to\automation-profile"

ICODE_BROWSER_CDP_URL=http://127.0.0.1:9222
```

Set both `ICODE_BROWSER_ALLOWED_DOMAINS` and `ICODE_BROWSER_CDP_URL` in iCode's
process environment before starting it; the plugin needs no profile block.

Attach mode creates its own tab, scopes `Fetch`/auto-attach to that tab, and on
close only closes that tab and disconnects: it never sends `Browser.close` and
never touches the browser's other tabs.

The profile you attach to is the login state the agent gets.

## Extension bridge

`python -m browser_use.chrome connect --bridge` verifies the native host and saves the choice for new
browser handles. `python -m browser_use.chrome status` shows the backend for new handles and
`python -m browser_use.chrome disconnect` clears the saved preference. An explicit
`ICODE_BROWSER_BRIDGE` environment setting takes precedence. With the Bridge, a loopback server in the
plugin waits for Chrome's native-messaging host, which pumps frames to the MV3
extension; the extension drives a tab it owns through `chrome.debugger`. This
reuses the running browser's login state without copying a profile or closing
the browser.

To update an existing unpacked Bridge at the default path, run `python -m browser_use.chrome update-bridge`,
reload **iCode Browser Bridge** at `chrome://extensions`, and run `python -m browser_use.chrome connect --bridge`.
The connect command reports the version Chrome is running. Bridge 0.3.3 supports site-filtered browser logs and owned-tab visibility/activation (`tab_state`: `Bridge.tabState` and `Target.activateTarget`).

Setup:

```bash
# 1. register the native host, copy the bundled extension, and open the setup pages
# Run from the iCode source root to install the optional Bridge command
uv tool install ./extensions/browser-use
browser-use-bridge install --open
# opens chrome://extensions and the extension folder
# (on a machine where Chrome is not found, open chrome://extensions manually)

# 2. in chrome://extensions: Developer mode -> Load unpacked -> the printed folder
# 3. verify and save the connection
python -m browser_use.chrome connect --bridge
```

`--extension-dir` overrides the source (development), `--extension-copy`
overrides the stable target, and `--browser chrome|chromium|edge` limits the
registration.

On first install the extension opens an onboarding page. It shows whether the
plugin bridge is connected (the host sends `Bridge.ready` only after reaching the
plugin), the three setup steps, a copyable first task prompt with a checklist
(bridge connected, a site allowed, prompt copied), and a link to site
permissions.

The extension manifest pins its id with `key`, so the native manifest's
`allowed_origins` matches regardless of the install path. The native host only
connects to a loopback server with a per-run token, only operates tabs it
created, and refuses `Browser.close`.

Verified end to end on a real Chromium (navigate + snapshot + close; the browser
and its other tabs stay alive). The extension reconnects on startup, install and
a 30-second alarm, and drops tabs from its owned set when Chrome closes them.
OOPIF network interception, perception and interaction work through Chrome 125+
flat sessions: child iframe content is fused into the snapshot and marked
`[iframe]`, and child nodes get `@e` refs. Click/fill/scroll/extract route to the
child CDP session; mouse clicks use the iframe offset. Nested OOPIFs are fused
recursively (depth 3, up to 6 frames). Dedicated worker requests are covered by
the page's Fetch session in Chromium; `chrome.debugger` worker targets reject
`Fetch.enable`, so service/shared workers are detached. Use attach mode for
service-worker-specific coverage.

An active public-web session permits public sites through the Bridge, while any
explicit site block in the popup still applies. With an allowlist, the Bridge
receives the allowed domains and origins for that session. Update and reload a
previously installed Chrome extension to enable public-web mode. If a site shows
`site_permission_required`, check its permission in the extension popup.

Every tool action (`DOM*`, `Input*`, `Runtime.evaluate/callFunctionOn`,
`Accessibility*`, `DOMSnapshot*`, `Page.capture*`) re-checks the tab origin;
`Fetch`/`Network`/`Target` policy commands are exempt so the page can keep
loading. If page JavaScript redirects to an unapproved origin, the page may load,
but snapshots and interactions fail with `site_permission_required` until the
user allows it.

The options page (popup → **Manage all sites…**) changes the default for unknown
origins (Ask/Allow), edits or clears all overrides, exports/imports a JSON file,
and shows recent changes. The default stays Ask until changed there.

Still open: branded Chrome's manual unpacked load. Updating the extension
replaces the stable copy; close Chrome first because loaded files can be
locked. Uninstall with `browser-use-bridge uninstall`.

## Action result semantics

`browser_click` tries the element's centre and, when that point is covered, four
points near its corners (`hit_point=corner` in the result); hit tests walk composed
Shadow DOM roots (with a CDP `DOM.getNodeForLocation` fallback for retargeted
closed-shadow targets) so genuine occlusion — whether a light-DOM mask or a
shadow-internal overlay — is still refused (`status=blocked`) while unoccluded
elements inside open or closed Shadow DOM click normally. When every point is
covered nothing is sent (`dispatch=not_sent`, `status=blocked`), and when a hit test
cannot be answered nothing is sent either (`status=uncertain`). Elements with no
layout box fail fast with a readable bad-request diagnostic instead of a raw CDP
error. Every click result reports `dispatch=not_sent|sent|unknown`,
`effect=navigation|dom|target_detached|dom_elsewhere|focus|none|unknown` and
`status=succeeded|uncertain|blocked`; there is no JS second click. Delayed
asynchronous toggles (including `input[type=submit|button]`) are confirmed via
`verified=control_state` once the control's label/value/pressed state settles,
and controls that unmount themselves on click (such as confirmation modals) are
reported as `effect=target_detached`, `status=succeeded` with a reminder to
verify the page rather than a probe failure. If effect is
`focus`/`none`/`dom_elsewhere` or status is `uncertain`, re-snapshot or check
disabled/permissions — do not retry the same ref.
`dispatch=dom` calls the element's own `click()`; it is allowed once per ref per document, only
for a button/link/input (or role=button) whose mouse click was reported
all-occluded, or effect=none on a non-submit target, and it is refused while the
previous click has no result or after it has been used once.
`browser_fill` focuses targets across open and closed Shadow DOM (reporting
structured `[tag=... editable=... no-layout in-shadow readonly disabled]` flags
when focus fails); `browser_fill submit=true` sends a real Enter and then verifies the field and
page; it reports `status=succeeded` only on a structural signal (editor cleared
or page navigated), otherwise `status=uncertain retryable=false` and tells you
not to press Enter again. `browser_upload` reads the file field back before
reporting success. `browser_extract` walks open Shadow DOM trees and `<slot>`
content directly, and falls back to fused snapshot subtree text
(`[shadow DOM text from snapshot]`) when reading a closed Shadow DOM host whose
JS text is empty.
`browser_press_key` focuses the target first (when given), sends one trusted key
or combination, and reports `effect=navigation|dom|focus|dom_elsewhere|none` from
the page probes; `dom_elsewhere` means only unrelated page mutations moved, so the
key is not proven. A plain Enter in a single-line text field (not a textarea, not
Shift+Enter) is judged with the same verdict as `browser_fill(submit=true)`: an
emptied field or a navigation makes it `succeeded`, a page update alone stays
unconfirmed. Enter is never re-sent when the outcome is unclear.
`browser_select_option` works on native
`<select>` elements only; `values` is the complete wanted set (unlisted options
are deselected), each value is matched by option value then visible label, and
the result reports the selection read back from the page (plus `input`/`change`
are dispatched). `browser_wait_for` polls DOM state (never a network-idle signal)
with a hard `timeout_ms` budget (max 180000) and rate-limited snapshot refreshes
for free-text targets; on timeout it returns `status=timeout retryable=true` plus
the last observation (including `state=found_hidden` / `state=found_not_editable`
when matched but not yet ready, and the elapsed time for budgets above 60 s).
Conditions are `visible`, `hidden`, `editable` (free-text targets filter to
editable nodes so delayed composers do not collide with headings), `text` (walks
open Shadow DOM and falls back to closed-shadow snapshot text), `url`,
`text_changed` (page or target text differs from the first poll), `stable` (no
DOM mutations for `value` ms, default 1500, max 5000; also observes the target's
`shadowRoot` when present — prefer a scoped `target=` on dynamic/animated pages)
and `count` (a `browser_find` query compared with `>N`/`=N`, counted only after a
fresh snapshot). Successful waits carry a `basis=` field (`visible`, `invisible`,
`editable`, `gone`, `not_found`, `text_match`, `shadow_text_match`, `url_match`,
`text_changed`, `mutations_quiet`, `find_count`). An `@ref` from an older document fails fast for
every condition; a ref whose node is gone inside the current document counts as `hidden`
(`basis=gone`). A free-text target that is not in the snapshot also counts as hidden (reported as
`not_found`, with a note that an absent target is treated as hidden), but only against a snapshot
from the current document.

### Action journal

Every click, fill, upload, press_key, select_option, dialog, download_cancel,
hover, scroll and evaluate records an `action_id` in a per-session SQLite journal
(`journal.sqlite3` in the browser's state directory).
A record moves through `prepared` (written before anything is sent),
`dispatched` (the CDP send returned) and `result` (`succeeded` / `blocked` /
`uncertain`).

- `browser_action_status` reads one browser's journal: pass the `browser_id`
  whose action you are checking, or omit it only when exactly one browser is
  open. It never retries, replays or re-sends anything.
- A record left in `prepared` or `dispatched` (the plugin stopped mid-action) is
  reported as `uncertain`: the action may or may not have been sent, so inspect
  the page instead of repeating it.
- Records are scoped by session and agent, so two agents sharing a state
  directory cannot read each other's history. The newest 200 actions per session
  are kept; rows store the redacted origin and an evidence line, never typed
  text, passwords or page contents.
- A new `action_id` never proves business idempotency: two clicks are still two
  clicks. The journal only tells you what the plugin observed.

## Tools

| Tool | Approval | Purpose |
|---|---|---|
| `browser_open` | yes | Allocate and return a browser_id; startup is deferred until an action needs a connection (not yet connected) |
| `browser_list` | no | List this session's browsers plus recorded handles that can be reopened; never launches Chrome |
| `browser_status` | no | One browser's state, redacted origin, page epoch, `foreground=yes|no`, read-only state-dir lock status (`state_dir_locked`) when unstarted, and next step; never starts or reconnects |
| `browser_resume` | yes | Reopen a recorded handle by `resume_id`; never navigates or replays an action |
| `browser_navigate` | yes | Open an HTTP(S) URL permitted by the network policy and return a snapshot |
| `browser_tabs` | no | List the tabs this handle owns (root tab plus popups) with `tab_id`, opener, `[foreground]`/`[background]` and current marker |
| `browser_tab_switch` | no | Make another owned tab current; old refs become stale and a fresh snapshot returns |
| `browser_tab_close` | yes | Close one owned tab. Closing the current tab makes its opener current. Use `browser_close` to close the handle's last tab. |
| `browser_snapshot` | no | Compact interactive-element tree with `@eN#epoch` refs; `target=` serializes one subtree (preserving `editable` nodes with actionability flags and `placeholder`), unnamed non-interactive nodes are left out (their refs still exist), and consecutive same-name non-interactive siblings collapse to `×N` (interactive duplicates keep one line each); an element with no accessible name falls back to `aria-label`, `alt`, `title`, a child image's `alt`/`title` or (for links) the href, so icon-only links read as `link "/chat/c1"`; CSS `::before`/`::after` pseudo-elements are not listed |
| `browser_find` | no | Find elements by text, placeholder, role (`textbox` also matches `editable` nodes), tag or href; ranks visible/in-viewport/editable candidates first and prints `[editable readonly disabled focusable in-view no-layout]`. Refreshes after a page action; use `fresh=true` for later asynchronous changes. |
| `browser_action_status` | no | Look up recorded click/fill/upload/press_key/select_option/evaluate/hover/scroll/dialog/download_cancel evidence in one browser's journal by action_id; never retries |
| `browser_click` | yes | Click a ref or free-text target; Shadow-DOM-aware multi-point hit test, `dispatch=not_sent|sent|unknown`, `effect=target_detached`, `verified=control_state`, `dispatch=dom` gated by the journal |
| `browser_hover` | no | Move the mouse over a target without clicking; snapshot to read hover-only controls. It can still trigger page side effects (hover menus, analytics) |
| `browser_fill` | yes | Type into a field (including open/closed Shadow DOM editors) with real input events |
| `browser_press_key` | yes | Focus an optional target, press one key or combination, judge a text-field Enter like fill |
| `browser_select_option` | yes | Select the complete wanted set on a native `<select>` by value or label; reports the selection read back from the page |
| `browser_wait_for` | no | Wait for `visible`/`hidden`/`editable`/`text`/`url`/`text_changed`/`stable`/`count` with a hard timeout; Shadow-DOM-aware and reports structured timeout state |
| `browser_dialog` | yes | Accept or dismiss the open alert/confirm/prompt dialog; prompt answers come from `text` |
| `browser_downloads` | no | List tracked downloads with state, safe name, bytes, absolute path and reason |
| `browser_download_cancel` | yes | Cancel one in-progress download and remove its partial file |
| `browser_upload` | yes | Attach one local file to a file input (workspace-rooted) |
| `browser_scroll` | no | Scroll the window or an element's container; may trigger lazy-load and viewport analytics |
| `browser_extract` | no | Read text (including open Shadow DOM and closed-shadow snapshot fallback), markdown or HTML from the live DOM. Use `offset` to continue from the next offset reported in a cut result. |
| `browser_logs` | no | Read the current tab's console messages and network outcomes with redaction and a `since` cursor. The Bridge applies the active site policy and requires an extension version that supports log filtering. |
| `browser_back` | yes | Go back one history entry and re-snapshot |
| `browser_forward` | yes | Go forward one history entry and re-snapshot |
| `browser_reload` | yes | Reload the current page and re-snapshot. `ignore_cache` bypasses the cache. Reloading a POST result page may resubmit. |
| `browser_screenshot` | no | Save a unique, atomically written PNG/JPEG under the session folder (`target=` scrolls into view and clips in document coordinates with 8 px padding); inspect it with `view_image` |
| `browser_evaluate` | yes | Optional, `ICODE_BROWSER_EVALUATE=allow`; high-risk page script |
| `browser_save_state` | yes | Optional, `ICODE_BROWSER_SAVE_STATE=allow`; export cookies/localStorage |
| `browser_close` | yes | Close the browser and release its process |

Optional high-risk tools `browser_evaluate` and `browser_save_state` are absent
by default. Enable them in the plugin process with `ICODE_BROWSER_EVALUATE=allow`
and/or `ICODE_BROWSER_SAVE_STATE=allow`; both declare approval.

### Tabs, popups and OAuth

`browser_tabs` lists the tabs this handle owns — the tab it created plus popups
that tab opened — with an index, a `tab_id`, the opener and a current marker.
Like every other action it starts (or resumes) the Owned browser when needed and
counts against the action budget. `browser_tab_switch` makes another listed tab
current and returns a fresh snapshot; refs from the previous tab become stale, so
snapshot again after switching.

- A popup (a `target=_blank` link, `window.open`, or an OAuth provider window
  opened by our tab) is adopted with its opener recorded. It is **not** made
  current automatically; drive it with `browser_tab_switch`. Any new tab opened
  from this handle's page counts, including one the user opens with
  ctrl/cmd-click; at most eight tabs are adopted, and further popups stay user
  pages.
- New pages are covered by the same origin policy as the root page: Fetch is
  installed before the popup is released to run, so an unauthorized navigation
  is blocked there too.
- When a popup closes itself (a completed OAuth callback, for example), the
  session returns to the tab that opened it and a snapshot shows that page
  again.
- Pages opened by the user or another extension are **never** adopted, driven or
  closed. In Owned mode they stay attached so the origin policy keeps guarding
  their requests; in Attach mode they are detached from the plugin's CDP
  connection.
- Attach mode only tracks targets related to its own tab (no browser-level page
  discovery), so the user's other tabs are invisible and untouched; popups
  opened from the plugin's tab are not adopted there.
- Tab listings show origin and path only: query strings and fragments (OAuth
  codes, `state` values) are not sent to the model.
- Closing the browser (Owned) still kills the process tree, including any popup
  that remains open.

### JavaScript dialogs

A page can block its own script with `alert`, `confirm` or `prompt`. The plugin
records the dialog (type plus a redacted, truncated message) instead of silently
dismissing it:

- An action whose click/keypress/typing opens a dialog returns **immediately**
  with `status=blocked` and the dialog's type and message. Its effect is
  explicitly unproven — never `succeeded` — and the journal records `blocked`, so
  the action is not retried.
- While a dialog is open every page action fails with `browser_dialog_open`;
  `browser_tabs`, `browser_action_status`, `browser_logs`, `browser_close` and `browser_dialog`
  still work.
- `browser_dialog` takes `action=accept|dismiss`. For a `prompt`, `accept`
  requires `text`: the page receives exactly that answer (an empty answer is
  refused rather than submitted implicitly); `dismiss` returns `null` to the
  page.
- After handling the dialog the page continues; snapshot again before trusting
  anything that was in flight. An unhandled dialog is dismissed by the plugin
  after `ICODE_BROWSER_DIALOG_TIMEOUT` seconds (default 10) and the reason
  (`auto-dismissed after 10s (headless)`) is reported by the next
  `browser_dialog` call, so a headless run never waits forever.
- Dialogs opened by a page timer (not by an action) are caught the same way; a
  dialog in one of this handle's background tabs is dismissed at once so that
  tab's script cannot stay frozen.

### Downloads

Downloads are denied unless the plugin explicitly opts in with
`ICODE_BROWSER_DOWNLOADS=allow`. With it (Owned backend only), Chrome saves every
download under its GUID in the session's `downloads/` folder and the plugin tracks
it:

- When a download finishes it is renamed to a **sanitised** version of the name the
  page suggested: only the last path component is kept, path separators, control
  characters and Windows-invalid characters become `_`, reserved device names
  (`CON`, `LPT1`, …) get a leading `_`, and the result is truncated. The final path
  is always inside `downloads/` and an existing file is never overwritten — a
  duplicate becomes `name-2.ext`.
- `browser_downloads` lists each transfer's `guid`, `state`
  (`in_progress`/`completed`/`canceled`/`failed`), name, received/total bytes,
  absolute path and reason. `browser_download_cancel` cancels one `in_progress`
  transfer, deletes its partial file and never retries it.
- A transfer whose `totalBytes` or received bytes exceed
  `ICODE_BROWSER_DOWNLOAD_MAX_MB` (default 50) is canceled during the transfer and
  nothing is left behind; the completion path checks the size again.
- A click that starts a download returns before the transfer finishes; poll
  `browser_downloads` for the result. Navigating straight to an attachment URL is
  not supported (the browser aborts the navigation with `net::ERR_ABORTED`); open
  the page and click its link instead.
- Attach, Bridge and Broker never enable controlled downloads: they leave the
  user's download settings alone, `browser_downloads` reports that downloads are
  disabled, and downloads stay denied.

### Screenshots and vision

`browser_screenshot` writes a file, it does not hand the image back directly:

- Files land in the extension's own `screenshots/` folder inside the session
  (the per-user extension folder when there is no session), with a unique name
  (`screenshot-<UTC>-<token>.png|jpg`) written through a temporary file and an
  atomic rename, so a reader never sees a partial image.
- The result reports the format, pixel size (when the image header is readable) and byte size, the absolute path for
  `view_image`, and the session-relative path (`screenshots/…`) that a copied or
  forked session can resolve.
- `full_page=true` captures the whole document; `target` scrolls the target
  element into view and clips it in document coordinates (including OOPIF
  offsets and an 8 px padding margin, translating unlaid-out elements into a
  clear bad-request error). If capturing a background tab times out because
  Chrome throttled painting, the error reports `reason=background_tab_throttled`.
  PNG is re-captured as JPEG (quality 80) above 2.5 MB, and an image still above
  the cap fails, so very long pages are better captured with a target clip.
- `ICODE_BROWSER_SCREENSHOT_KEEP` (default 20) bounds the folder; only this
  plugin's own `screenshot-*` files are pruned (newest kept, the capture just
  written is never removed), and other files are left alone.

The model sees the picture only after it calls `view_image`, which needs both a
vision-capable model profile (`vision: true`; a text-only model never gets the
tool) and the `filesystem.read` builtin in the agent's tool list. When either is
missing, the screenshot is still saved and the model gets the path with that
instruction; enable `filesystem.read` and a vision model to close the loop.

The 2.5 MB cap above is the plugin's limit, not the model's budget: iCode's
`view_image` reads images with `tools.image.send_limit_kb` (256 KB by default) and
re-encodes anything larger to JPEG at a 2048 px long edge. A full-page capture the
plugin accepts can therefore reach the model downscaled; clip with `target` when
detail matters.

At most two browsers may be open per agent build; an idle browser closes itself
after `idle_close_seconds` (600 s by default) and restarts from the same profile
on the next call. With iCode 0.36+, a sub-agent tool and a workflow activation
carry their own `ToolContext.agent_id`, so two builds that share the session and
profile name still get separate handles, profiles, journals and slots; older
hosts fall back to the session/profile pair.

## Recovery after a restart

Each session keeps a small manifest, `browsers.json`, in the plugin's session
folder (the user data folder when the call is outside a saved session). It
stores a schema version plus one record per handle: `browser_id`, `resume_id`
(slot + generation), owner session/agent, backend, slot and the redacted page
origin. It never stores cookies, passwords, full URLs, queries, titles or
profile paths, and it is replaced atomically so a crash cannot leave half a
file.

- `browser_list` shows this session's live handles and the recorded ones; it
  never launches Chrome.
- `browser_status` reports `allocated / connecting / ready / disconnected /
  idle / hung / closing / closed`, the redacted origin, the page epoch,
  `foreground=yes|no` when known, read-only state-directory lock status (holder
  PID / state when another instance holds the lock, matching `reason=state_dir_locked`),
  a blocking reason (idle close, login wait, dropped connection) and the next
  step. It never starts or reconnects a browser.
- `browser_resume` takes a `resume_id`, not a stale `browser_id`. It verifies
  the owner, the backend and the slot (and that no other plugin instance holds
  the profile lock), then reopens the handle. **Owned mode restores the profile
  only**: the page is new, so the previous DOM and history are gone. Broker
  mode may return the existing page — check it with `browser_snapshot`. Nothing
  is navigated and no action is replayed.
- Recovery is scoped to one session, agent profile and agent build. A forked session copies the
  manifest but carries a different session id, so it only sees an `unclaimable`
  count and must call `browser_open`. Another agent never sees your handles or
  origins.
- Resuming supersedes the old generation: its `resume_id` stops existing, and
  using it returns an error that points back to `browser_list`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ICODE_BROWSER_ALLOWED_DOMAINS` | unset | Optional comma-separated domain allowlist. A bare entry covers subdomains. Include required asset domains in allowlist mode. |
| `ICODE_BROWSER_ALLOWED_ORIGINS` | unset | Comma-separated loopback origins such as `http://localhost:3000`; a page may load only an exactly matching `scheme://host:port` |
| `ICODE_BROWSER_CDP_URL` | unset | Attach to an existing Chromium CDP endpoint (`http://127.0.0.1:9222` or a `ws://` URL) instead of launching our own browser |
| `ICODE_BROWSER_BRIDGE` | off | `1` connects through the Chrome extension bridge (requires the extension and native host) |
| `ICODE_BROWSER_BRIDGE_FILE` | `state_dir/bridge.json` | Rendezvous file the plugin writes for the native host (port + token) |
| `ICODE_BROWSER_HEADLESS` | `0` | `1` runs without a window (owned mode only) |
| `ICODE_BROWSER_STATE_DIR` | `~/.cache/browser-use` | Chrome profile, logs, per-browser state |
| `ICODE_BROWSER_EVALUATE` | off | `allow` registers `browser_evaluate` (high-risk, approval) |
| `ICODE_BROWSER_SAVE_STATE` | off | `allow` registers `browser_save_state` (high-risk, approval) |
| `ICODE_BROWSER_UPLOAD_ROOTS` | unset | Path-separated roots `browser_upload` may read; empty falls back to process cwd |
| `ICODE_BROWSER_SCREENSHOT_KEEP` | `20` | Screenshots kept in the extension's own `screenshots/` folder; older ones are pruned (minimum 1) |
| `ICODE_BROWSER_DIALOG_TIMEOUT` | `10` | Seconds an open JavaScript dialog waits for `browser_dialog` before the plugin dismisses it (positive; headless runs must never wait forever) |
| `ICODE_BROWSER_DOWNLOADS` | off | `allow` enables controlled downloads (Owned only) into the session's `downloads/` folder |
| `ICODE_BROWSER_DOWNLOAD_DIR` | `<state_dir>/downloads` | Overrides the base folder (Owned handles replace it with the session folder) |
| `ICODE_BROWSER_DOWNLOAD_MAX_MB` | `50` | Largest download kept; a transfer above it is canceled (whole number, minimum 1) |
| `ICODE_BROWSER_GUARD` | off | `strict` blocks `shell`/`filesystem.write` while a browser is open |
| `ICODE_BROWSER_BROKER` | off | `1` uses the persistent local Broker; the plugin process can be rebuilt without losing the browser session |
| `ICODE_BROWSER_BROKER_AUTOSTART` | off | development-only: spawn the Broker from the plugin process |
| `ICODE_BROWSER_LEASE_TTL` | `300` | seconds without activity before a Broker lease is reclaimed |
| `ICODE_BROWSER_BROKER_IDLE_EXIT` | `0` | seconds with no clients/leases/sessions before the Broker exits; 0 disables |
| `ICODE_BROWSER_OWNER_ID` | `default` | Stable owner for Broker leases; a different owner cannot take over an existing `browser_session_id` |
| `ICODE_BROWSER_BROWSER_SESSION_ID` | `default` | Stable Broker session key; the same key re-acquires the existing BrowserSession |

The Broker is a local loopback daemon with a user-only discovery file
(`state_dir/broker/broker.json`). It owns `BrowserSession` objects; plugin clients
acquire a lease and forward tool calls. Start it outside the plugin process:

```bash
browser-use-broker          # start for the current ICODE_BROWSER_STATE_DIR
browser-use-broker --stop   # authenticated shutdown + cleanup
browser-use-broker --stop --purge-profiles  # also delete sessions/<digest> profiles
```

`ICODE_BROWSER_BROKER_AUTOSTART=1` is a development-only fallback that spawns the
Broker from the plugin process; normal iCode use should keep it off so the Broker is
not killed with the plugin job. The Broker's environment is authoritative for
allowlist, headless/bridge/CDP mode and evaluate; restart it after changing those.
Owned sessions get their own `state_dir/sessions/<digest>` profile and lock, so
different `browser_session_id` values do not share a page or login. Bridge mode
supports one Broker session per state dir (the native host reads the root
discovery file), and attach mode shares the attached browser. The Python
extension adapter and workflow nodes still create their own sessions.

## Security

- The HTTP(S) network policy uses CDP `Fetch`, DNS checks and OOPIF arming.
  In public-web mode, DNS must resolve exclusively to public IP addresses.
  The policy also checks scripts, styles, fonts and images. In allowlist mode, a
  real site needs its static hosts as well: for Google properties that means
  `gstatic.com`, `googleusercontent.com` and `googleapis.com` on top of the page's
  own domain — otherwise the HTML arrives but the app never hydrates.
- Downloads are denied, `browser_evaluate` is off by default, and JavaScript
  dialogs are dismissed automatically.
- A headed window is the default: the user can see and take over the browser.
  In headless runs, treat page text as untrusted and give the agent a profile
  without `shell`/`filesystem.write`; `ICODE_BROWSER_GUARD=strict` blocks those two
  kinds only. It does **not** stop `read_file`/`web_fetch` exfiltration, so it is
  one layer, not a boundary.
- Known gaps (next slice): attach mode is only as scoped as the page session
  (browser-wide `Target.setAutoAttach` is deliberately not used); download denial
  in attach mode is best-effort (`Page.setDownloadBehavior`); the launcher still
  opens a localhost CDP port for owned mode instead of `--remote-debugging-pipe`;
  WebSocket frames and blob workers are not covered by `Fetch`; the owner key is
  `(session_id, agent_name)`, so same-profile parallel branches share a namespace
  and are not isolated by host authentication; tool texts are still Chinese; and the 500-action lifetime cap
  is per browser process (a restart or `browser_close` resets it).

## Development

From the iCode repository root:

```bash
uv sync
uv run pytest -c extensions/browser-use/pyproject.toml extensions/browser-use/tests
```

The tests use fake CDP sessions; they never launch Chrome. Real-browser smoke
gates and their commands are listed in [`scripts/README.md`](scripts/README.md).
