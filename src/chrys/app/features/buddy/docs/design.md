# Buddy

A buddy is a small pet that lives in the sidebar, grows as the user works, and
answers in one line when it is petted. The package is split by concern:

| Module | Owns |
| --- | --- |
| `model.py` | `BuddyRecord` (what is saved) and `Buddy` (the record plus everything derived from it) |
| `hatchery.py` | The random draw that makes a new record |
| `progression.py` | Experience, levels and evolution stages |
| `store.py` | The save file: locking, atomic writes, the signed envelope |
| `actions.py` | The changes a user can cause: hatch, rename, mute, pet, finish a turn |
| `commands.py` | `/buddy` subcommands and the info card |
| `replies.py` | The one-line answer to a pet, from a model or a stock line |
| `lifecycle.py` | The hook every frontend calls, on its event loop, after a successful turn |
| `sprites/*.toml`, `pixel_sprites.py`, `pixel_renderer.py`, `animation.py`, `portrait.py` | Artwork and its rendering |

The TUI adds `app/tui/buddy_reply.py::PetReplyFlow`, which the sidebar panel and
`/buddy pet` share: a thinking toast now, the answer when it arrives, and one
reply in flight per process.

## Hatching

Hatching is a plain random draw (`random.SystemRandom` in production, any
`random.Random` in tests) and the whole result is saved. Nothing about a buddy
is derived from the machine or the user, so a buddy moves with its save file.

The draw picks a rarity tier (`RARITY_ODDS`), a species that hatches in that
tier, a 1-in-40 shiny flag, four traits (focus, curiosity, grit and charm) and
a name. Each tier has a trait point budget (`TRAIT_POINTS`) that is shared out
unevenly, so every buddy leans some way. The strongest trait at hatch time
decides the persona line. It is a message like any other display text: the
info card and the sidebar show it in the user's language, and the reply
prompt, which is English throughout, is given the English line.

`SPECIES_RARITY` sorts the species by how far from the back garden one would
have to go to meet them: N lives outside the window (bee, cat, duck, snail),
R takes a trip (fox, owl, penguin, shark), SR takes luck (axolotl, capybara,
panda), and SSR is not from around here at all (alien, dragon, ghost, robot).

## Progression

A finished turn is worth 10 XP and a pet 2 XP. Only the first `5 × (turns + 1)`
pets of a buddy's life count, so the pets a turn pays for are worth exactly as
much as the turn itself: petting can at best double what working earns, and
without work it stops paying after the first five. The allowance is cumulative:
pets not given after one turn may be given after a later one.

A stage is 99 level-ups. A level costs `10 × (10 + level // 10 + 2 × stage)`
XP, which makes the first stage about 1,440 turns and each later stage about
200 turns longer. Finishing a stage below the top rarity tier evolves the buddy
one tier and restarts it at level 1; the top tier ends at level 100. Levels and
stages are never stored. `progress()` derives them from the turn and pet
counters, so changing the curve re-levels every buddy consistently.

Traits grow by one point per ten levels and ten per evolution, capped at 100.
A top-tier buddy that has evolved or reached the final level turns shiny.
`honored` holds once a buddy has evolved at any stage or has maxed the final
level, and the name is marked exactly when it does: ✨ below the top tier and
🌟 at it. A buddy hatched shiny without either keeps its bare name, and a buddy
hatched in the top tier earns the mark by finishing it.

## Save file

`<config dir>/extras/buddy/buddy.json`, shared by every running instance:

```json
{"v": 2, "buddy": {"species": "...", "rarity": "...", "...": "..."}, "sig": "<HMAC-SHA256>"}
```

Changes are read-modify-write under a lock file and land through an atomic
replace, so readers take no lock. `buddy.json.bak` is a second copy that a
reader falls back to. The signature uses a constant key: it catches damage and
casual hand edits, and is not a secret. A file that does not verify, or that is
not version 2, reads as "no buddy yet". Files written before version 2 are not
migrated; those users hatch again.

`BuddyRecord.from_json` accepts exactly what `to_json` writes and coerces
nothing. Reading never raises. A change raises `OSError` when the file cannot
be written (`TimeoutError` when another instance holds the lock for ten
seconds): `/buddy` answers that with a warning, petting goes ahead uncounted,
and the after-turn hook logs it. A muted buddy keeps its information toasts to
itself, never a warning: a command that did not work is always reported. Until
a buddy has hatched, no change touches the disk at all, so users who never
hatch one pay nothing per turn.

Waiting for the lock is never the event loop's job, since every frontend runs
its turns, its input and its rendering there. The sidebar counts a pet on a
widget worker, which its unmount cancels; `/buddy` commands run on a thread,
one after another in the order they were given, and answer when they are done;
and the after-turn hook hands the credit to the loop's default executor, which
the loop's runner joins at exit, so a headless run still lands its last turn.
A change whose task was cancelled still lands: a thread cannot be stopped, and
the file is the truth either way. Reads take no lock and stay on the loop. A
`/buddy pet` has two ends, the count and the answer, which finish in either
order, and each one refreshes the panel in place when it does.

# Pixel portraits

All 28 species use a **20 × 16 RGBA canvas**, rendered as 20 terminal columns
and eight rows of TrueColor half blocks (`▀`).

## Artwork

Artwork is data: each species has one `sprites/<species>.toml` file holding its
`[palette]` (index → `[r, g, b, a]`) and three distinct poses as `[[frames]]`
tables. Each pose's `rows` are 16 strings of 20 palette-index digits, which are
not characters displayed to the user. `pixel_sprites.py` loads a file on first
use and rejects one whose size or indices don't fit. Palette roles are `0` for
transparency, `3` for the body shadow, and `4` for pupils; other colors belong
to that species' design.

Keep the silhouette recognizable at native size. Features that distinguish
similar animals include:

| Species | Defining features |
| --- | --- |
| Duck / goose | Flat orange bill; goose has a long neck |
| Cat / tabby / fox | Pointed ears; tabby is rounder and wider; fox has a pale muzzle and large tail |
| Rabbit | Long pink inner ears, dark pupils, cream muzzle, rounded haunches and paws, small round tail |
| Owl / penguin | Owl's facial discs and ear tufts; penguin's pale belly and orange feet |
| Turtle / snail | Turtle's domed shell and low legs; snail's spiral shell and eye stalks |
| Octopus / axolotl | Tentacles; external gills and four small limbs |
| Dragon / bat | Dragon's horns, belly and long tail; bat's broad wings |
| Panda / capybara / llama | Dark eye patches; broad blunt muzzle; tall neck |
| Shark / snake / frog | Dorsal fin and teeth; long coiled body; raised eyes and spread feet |
| Bee / crab | Striped abdomen and wings; paired claws and spread legs |
| Ghost / jelly / alien | Wavy hem; soft low body; large dome and dark eyes |
| Cactus / mushroom / robot | Branched stem and pot; spotted cap and stalk; metal panels, LED eyes and antenna |

Pupils retain their authored contrast color. Blinking changes only vertically
stacked pupil pixels: upper pixels use the body shadow, while the bottom stays
visible as a closed-eye line. Every built-in pose includes vertically stacked
pupils so blinking visibly changes its expression. Isolated pupil pixels stay
intact. Other features that happen to share an eye's RGB color must remain unchanged.

## Animation and portrait layout

`animation.py` owns timing for idle poses 0–2 and petting poses 3–5. Idling is
a rule, not a list of cues: each species has a temperament (darting, lively,
easy or slow), and a temperament is how long the animal rests and how long it
holds a pose. It rests, stretches through pose 1 and then pose 2 for the same
time each, and rests again, so every temperament has a period of its own.
Eyelids keep their own time: one blink every 13 ticks whatever the pose, a
period that shares no factor with any temperament's, so blinks never settle
into the rhythm. Petting cycles all three poses faster. Its first pose hops up
one pixel only when the top row is empty, preserving ears, horns and antennae.
No particles or decorations overwrite the animal.

`portrait.py` adds separate rarity-colored corners and a compact nameplate.
Shiny animates only the badge. The portrait occupies 11 terminal rows at every
width. Below 20 columns the complete pixel canvas is scaled with nearest-neighbor
sampling and vertically padded, including custom PNG artwork. Built-in pupil
pixels are retained at their scaled positions so single-column eyes stay visible. Corners are
omitted below 22 columns so they do not crowd the animal.

The sidebar repaints frames without invalidating layout, uses cached geometry,
and skips offscreen portraits. The shiny badge runs on its own 10 Hz timer.

The save file changes behind the panel's back: every finished turn credits it,
and so does every other running instance. The panel reads it again when it
comes into view and every few seconds while it stays there, and rewrites a
label only when its text has changed, because each label update lays the whole
screen out again.

## Custom artwork

Place PNG overrides at `~/.chrys/extras/buddy/assets/<species>_<frame>.png`, with
frame indices 0–5. Images are normalized to 20 × 16 with nearest-neighbor
sampling. Custom pixels are kept as supplied, including their eye design.
File revisions invalidate a bounded image cache; missing or unreadable files
fall back to built-in artwork and are retried on subsequent renders.

When upgrading from 16 × 10, existing custom PNGs are stretched to the new
20 × 16 canvas. Their aspect ratio therefore changes; redraw or export them
at 20 × 16 to control their proportions. Two species were renamed: overrides
called `blob_<frame>.png` or `chonk_<frame>.png` are no longer looked up and
need renaming to `jelly_<frame>.png` and `tabby_<frame>.png`.

## Preview and verification

Run `uv run python playground/buddy.py` to inspect all species. Space pauses;
`n`/`p` step ticks; `x` toggles petting; `s` toggles shiny; `t` cycles rarity;
`b` forces blinking; `r` resets; `q` quits.

Tests cover native dimensions and palettes, intact petting poses, pupil
contrast, animation timing, narrow pixel rendering, PNG cache invalidation and
mounted sidebar repaint behavior, plus the hatch draw, the progression curve,
the save file envelope and its concurrency, commands and replies.
`tests/app/tui/behaviors/test_buddy_sidebar_flow.py` drives `/buddy` through the
real command controller, view adapter and sidebar.
