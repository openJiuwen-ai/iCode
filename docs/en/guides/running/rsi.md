# RSI experiments (experimental)

RSI (recursive self-improvement) is an **experimental offline loop**: score an agent profile against a fixed dataset, propose small whitelisted changes, keep a candidate only when the score strictly improves, then export YAML for human review.

**CLI is the only supported way to run RSI.** There is no TUI panel and no ACP surface for these experiments. Use `icode rsi …` in a terminal. Tasks live under `<config_dir>/rsi/tasks/` (for example `~/.chrys/rsi/tasks/` on macOS and Linux) and stay separate from chat and workflow sessions.

Live eval and live optimize use `ApprovalMode.BYPASS` (same as `icode run`). Always review exported profiles before you rely on them.

## How the loop fits together

```text
  dataset JSON          base profile
       │                     │
       ▼                     ▼
  validate ──► create task ──► (optional) eval baseline
                                  │
                                  ▼
                            optimize
                     ┌──────────┴──────────┐
                     │  score baseline     │
                     │  propose mutations  │
                     │  re-score gate      │
                     │  adopt if better    │
                     └──────────┬──────────┘
                                  │
                    tree / get / best.json / analysis/
                                  │
                                  ▼
                         review → install (export)
```

| Stage in the loop | What you are doing | Typical command |
|-------------------|--------------------|-----------------|
| Prepare cases | Define prompts and declarative checks | Edit dataset JSON → `dataset validate` |
| Open a workspace | Create an isolated task dir and attach the dataset | `create --dataset …` |
| Optional smoke score | See how the current agent does before optimizing | `eval --task …` or `eval --dataset …` |
| Improve | Baseline → mutate → gate → adopt/reject for N epochs | `optimize` |
| Inspect | Read scores, tree, analysis, candidate YAML | `get` / `tree` / files under the task |
| Export | Copy the best candidate out for review (not hot-load) | `install` |

`icode rsi start` is **not** this loop. It still runs a Phase 1 **mock** provider that only exercises the task worker and tree projection. For real scoring use `eval`; for gated search use `optimize`.

## Typical walkthrough (live optimize)

Goal: improve a profile on a small `script_based` dataset with a real model, then export the winner for review.

The shipped live sample is **`playground/rsi/complex-calc/`** (Rust complex calculator). Use that tree when you want a ready-made dataset; the section [Demo sample: Rust complex calculator](#demo-sample-rust-complex-calculator) walks through create → eval → optimize → install on it. The steps below are the same loop for any dataset you write yourself.

### 1. Write a dataset

Create a JSON file (`chrys-rsi.dataset.v1`). Each case needs a `prompt` and `evaluation.expect` checks (and/or a trusted `script`). For live runs, **omit** `fixture` (fixtures are canned stub outputs for CI only).

To work on a **code tree** (add a feature, fix a bug), add a `workdir` seed. The source is treated as **read-only**: RSI materializes a private copy (or git clone / worktree) into the case directory; it does not open your original tree in-place. Prefer `strategy: "copy"` or `"git_clone"`. `"git_worktree"` is faster but shares the source `.git` (weaker isolation).

When you `create --dataset`, each seed is snapshotted under the task’s `input/seeds/<case_id>/` and the stored dataset is rewritten to `copy` from that snapshot, so later eval/optimize never touch the caller’s original path again.

Keep prompts from already containing the strings or files you will check for. Otherwise the baseline may pass for the wrong reason, and optimize has nothing useful to fix.

Why this step: the dataset is the only ground truth. Optimize never invents new scoring rules; it only tries to raise the pass rate on these cases.

### 2. Validate the file

```bash
icode rsi dataset validate ./suite.json
```

Why: catch schema mistakes before you copy the file into a task. Nothing is scored yet.

### 3. Create a task and attach the dataset

```bash
icode rsi create \
  --name live-probe \
  --max-iterations 2 \
  --dataset ./suite.json \
  --instruction "Append the missing requirements so the checks pass. Do not replace existing instructions."
```

This prints a task id such as `rsi-…`. The dataset is copied to `<task>/input/`. `--instruction` is guidance for the **optimizer** model (what kind of mutations to prefer), not the case-run agent’s user prompt. `--max-iterations` is the default epoch budget for `optimize` (and for mock `start`).

Why: every optimize run needs a durable task workspace for candidates, scorecards, analysis, and `best.json`.

### 4. (Optional) Score the current agent once

```bash
icode rsi eval --task rsi-<id> -a Code -m <model>
```

Or dry-run without a model (requires `fixture` on each case):

```bash
icode rsi eval --task rsi-<id> --stub
```

Why: useful to confirm the dataset and headless path before spending optimize epochs. Skip this if you will go straight to `optimize` (optimize always scores a baseline first).

### 5. Run gated optimize

```bash
icode rsi optimize rsi-<id> --agent Code -m <model> --epochs 2 --timeout 180
```

What happens inside one `optimize`:

1. **Baseline** — run every case with the base profile (`Code`, unless `--profile` points at a YAML).
2. **Propose** — an optimizer model reads failure summaries and may emit only whitelisted mutations (`append_instructions`, `upsert_inline_skill`, `remove_inline_skill`).
3. **Materialize** — write a candidate under `<task>/run/candidates/epoch-N/`.
4. **Gate** — re-run **only cases that are still failing**; merge those results with earlier passes; adopt only if the **suite score strictly increases**.
5. Repeat until epochs are exhausted, the score is perfect, or the optimizer returns an empty `{"mutations":[]}` (deliberate stop). A bad model call or invalid JSON **drops** that epoch and continues; nothing from a dropped proposal is installed.

Stdout includes `baseline_score`, `best_score`, `best_candidate`, `epochs`, `dropped`, and `perfect`.

Why: this is the actual RSI search. Stub mode (`optimize` without `--agent`) is for CI and wiring checks; it does not call a model.

Useful variants:

```bash
# Separate proposer and case-run models
icode rsi optimize rsi-<id> --agent Code --optimizer-model <proposer> -m <tester>

# Base profile from a YAML file (NAME from --agent is not required to exist on disk)
icode rsi optimize rsi-<id> --agent Code --profile ./base.yaml -m <model>

# Stub optimize (no model): CI / dry-run
icode rsi optimize rsi-<id> --epochs 2
```

### 6. Inspect results

```bash
icode rsi get rsi-<id>
icode rsi tree rsi-<id>
```

Also open on disk (under `~/.chrys/rsi/tasks/rsi-<id>/` by default):

| Path | Meaning |
|------|---------|
| `run/best.json` | Pointer to the best adopted candidate |
| `run/candidates/<id>/` | Profile YAML + `mutations.json` |
| `run/analysis/epoch-N.json` | Failure summary and proposed mutations |
| `run/evaluations/` | Per-label scorecards and `cases.jsonl` |
| `tree.json` | Evolution tree (`ADOPTED` / `REJECTED`) |

Why: decide whether the winning mutation is a real improvement or an overfit patch before export.

### 7. Export for review (optional install into agents dir)

```bash
# Default: copy best YAML to <task>/export/<name>.yaml
icode rsi install rsi-<id>

# Or an explicit path
icode rsi install rsi-<id> --out ./candidate.yaml

# Opt-in write into a user agents directory (both flags required)
icode rsi install rsi-<id> --user --user-dir ~/.chrys/agents
```

Why: RSI ends at **export**. It does not hot-load into a running chat session. You choose whether and how to use the YAML afterward.

### 8. Clean up when finished

```bash
icode rsi list
icode rsi delete rsi-<id>
```

## Command reference

| Command | Role in RSI | What it does |
|---------|-------------|--------------|
| `dataset validate` | Prepare | Check dataset JSON schema; no task, no model |
| `create` | Prepare | Create `<config_dir>/rsi/tasks/<id>/`; optionally copy `--dataset` into `input/` |
| `list` / `get` | Inspect | List tasks or show one task’s metadata / last optimize summary |
| `eval` | Measure | Run `script_based` scoring once (stub fixtures or live `--agent`) |
| `optimize` | Improve | Full gated loop: baseline, propose, gate, adopt/reject |
| `tree` | Inspect | Print the evolution tree projection after mock `start` or `optimize` |
| `install` | Export | Copy best candidate YAML out; never auto-writes user agents unless `--user` + `--user-dir` |
| `delete` | Housekeeping | Remove a `CREATED` or terminal task directory |
| `start` | **Mock only** | Queue the Phase 1 mock provider (not real optimize) |

## Dataset format (chrys-rsi.dataset.v1)

```json
{
  "schema_version": 1,
  "dataset_id": "demo",
  "cases": [
    {
      "case_id": "hello-file",
      "prompt": "Write hello.txt containing hello world",
      "workdir": {
        "from": "./seeds/mini-app",
        "strategy": "copy"
      },
      "evaluation": {
        "method": "script_based",
        "expect": {
          "response_contains": ["hello"],
          "files_exist": ["hello.txt"],
          "file_contains": { "hello.txt": ["hello world"] }
        },
        "script": "optional_check.py"
      },
      "fixture": {
        "response": "Created hello.txt",
        "files": { "hello.txt": "hello world\n" }
      }
    }
  ]
}
```

- Only `evaluation.method: script_based` is supported (no LLM-as-judge in this release).
- **Inputs to the agent**: `prompt` (user message) plus the materialized case workdir (and the base profile from `--agent` / `--profile`). `evaluation.*` is judge-only and is not passed to the model.
- **`workdir`** (optional): seed a private case cwd from a source directory.
  - `from` — absolute path, or path relative to the dataset JSON’s directory
  - `strategy` — `copy` (default; any tree), `git_clone` (local clone with `--no-hardlinks`), or `git_worktree` (detach checkout; shares `.git`, weaker isolation)
  - `ref` — optional git commit/branch for `git_clone` / `git_worktree`
  - Symlinks in a `copy` source that resolve outside that source are refused
- `fixture` supplies a canned `response` and files for `--stub` / CI. Omit it for live `--agent` runs.
- Optional `script` paths must stay inside the dataset directory; they run with `stdin` closed and receive `CHRYS_RSI_WORKDIR` / `CHRYS_RSI_RESPONSE_PATH`. Treat them as trusted local code.

Eval artifacts land under the chosen output directory as `eval-<id>/scorecard.json` and `cases.jsonl`.

### Demo sample: Rust complex calculator

`playground/rsi/complex-calc/` is the **canonical live RSI demo**: one coding case, empty workdir seed, trusted `script_based` judge. It shows the full offline loop—baseline often fails the hard checks, optimize proposes whitelist mutations, and a later epoch can raise the suite score to `1.0` (`perfect=True`) without hot-loading anything into chat.

#### What is in the tree

| Path | Role |
|------|------|
| `dataset.json` | `chrys-rsi.dataset.v1` with one case (`complex-calc`); prompt asks for a Cargo binary named `complex_calc` |
| `seed/` | Empty directory (`from: "seed"`, `strategy: "copy"`). RSI snapshots it at `create` time; the agent must create the project from scratch |
| `check.py` | Trusted judge: `cargo build`, then many expressions plus invalid-input exits. **Expected values live only here**, not in the prompt or seed |

Requires `cargo` on `PATH`. Trusted script timeout is 300s (cold builds). Case-run timeout should be at least ~600s so build + tool loop fit.

#### Why this suite is a good demo

- **Hard enough that baseline often scores `0.0`.** Soft arithmetic can pass; the judge also requires right-associative `2^3^2` → `512+0i`, reciprocal `(2-i)^(-1)`, bare `i` / `-i`, precedence without parentheses, and **non-zero exit** on malformed input such as `1++2i` (must not silently collapse `++`).
- **Ground truth is not in the workdir.** A browsing agent cannot read the expect table from the seed; only `check.py` under the dataset (copied into the task) holds answers.
- **Optimize has something to fix.** Typical failures are parser/precedence/invalid-exit bugs. Successful epochs often `append_instructions` and/or `upsert_inline_skill` (for example a short “strict lexer / reject `1++2i`” skill) rather than rewriting the whole profile.
- **Live runners load builtins first**, then overlay the candidate, so Code’s Explore/General sub-agents resolve like a normal headless session.

#### Run the demo end to end

From the repo root (or any cwd that can reach the dataset path):

```bash
# 1. Validate + open a task (snapshots seed/ into the task)
icode rsi dataset validate playground/rsi/complex-calc/dataset.json
icode rsi create --name complex-calc --max-iterations 2 \
  --dataset playground/rsi/complex-calc/dataset.json \
  --instruction "Prefer clear parsing, right-associative ^, and strict rejection of malformed operator sequences. Append guidance; do not replace existing instructions."

# 2. Optional: score Code once before searching
icode rsi eval --task rsi-<id> -a Code -m <model> --timeout 600

# 3. Gated search (baseline is always scored first)
icode rsi optimize rsi-<id> --agent Code -m <model> --epochs 2 --timeout 600

# 4. Inspect + export for human review (not hot-load)
icode rsi get rsi-<id>
icode rsi tree rsi-<id>
icode rsi install rsi-<id> --out ./complex-calc-Code.yaml
```

Replace `rsi-<id>` with the id printed by `create`. Point `-m` at a configured model profile. Use a longer `--timeout` if cold `cargo` builds are slow on your machine.

#### How to read a successful run

Optimize stdout looks like:

```text
task_id=rsi-…
baseline_score=0.0
best_score=1.0
best_candidate=epoch-2
epochs=2
dropped=0
perfect=True
```

Interpretation:

- **`baseline_score=0.0`** — unmodified Code failed at least one judge check (often `2^3^2` or `1++2i`).
- **`best_candidate=epoch-N`** — that epoch’s mutations were **adopted** because the gated suite score strictly improved.
- **`best_score=1.0` / `perfect=True`** — on this dataset, the adopted candidate passed the case (build + every expression + invalid exits). RSI stops early when the score is perfect.
- Artifacts under `<config_dir>/rsi/tasks/rsi-…/`: `run/best.json`, `run/candidates/epoch-N/Code.yaml`, `run/analysis/epoch-N.json` (proposed mutations), `run/evaluations/` (per-epoch scorecards / `cases.jsonl`).

A first epoch can still score `0.0` (proposal accepted into the tree as rejected or weak); a later epoch may be the first adopt. Coding evals are **non-deterministic**: the same commands may not always reach `perfect=True`. Re-run `optimize` or raise `--epochs` if the search stalls at `0.0`.

`perfect=True` means **this suite** is solved under the exported profile—not that every similar coding task will pass, and not that you should install the YAML as daily Code without review.

#### What changed vs baseline (illustrative successful run)

On a live run that finished `baseline_score=0.0` → `best_candidate=epoch-2` / `perfect=True`, the **adopted** profile was still builtin Code with only whitelist mutations. Diff against `run/candidates/baseline/Code.yaml`:

1. **`append_instructions`** — one short paragraph at the end of `instructions`, focused on the judge failure that kept killing the suite: reject a binary `+` immediately followed by unary `+` (example `1++2i`) with a non-zero exit; do not collapse `++` to `+`; keep valid unary minus (`-i`, `-(1+2i)`) and negative exponents (`2^-3`).
2. **`upsert_inline_skill`** — new inline skill `complex-expression-parser`: tokenize operators separately from literals; validate token sequences before eval; reject `1++2i` instead of treating the second `+` as unary plus; preserve unary minus after `(`, start of expr, and after `^`; restate `^` right-associative / integer-only precedence; require `A+Bi` / `A-Bi` output with no spaces and no `.0` on integral parts.

Nothing else in the profile changed (tools, approval, sub-agents, base instruction body stayed the same).

How that search looked on the tree (same run):

| Candidate | Parent | Mutations | Gate score | Outcome |
|-----------|--------|-----------|------------|---------|
| baseline | — | — | `0.0` | ADOPTED as starting point (suite failed; typical findings: `2^3^2` and/or `1++2i`) |
| epoch-1 | baseline | `append_instructions` only (broader “strict lexer / `++` / `^`” note) | `0.0` | REJECTED — still failed `1++2i` (and sometimes other traps) |
| epoch-2 | baseline | `append_instructions` + `upsert_inline_skill` above | `1.0` | ADOPTED / best — build + all expressions + invalid exits passed |

Epoch-2’s parent was still **baseline**, not epoch-1: a rejected epoch does not stack into the next candidate. Inspect the same facts on your machine with `icode rsi tree <task>`, `run/analysis/epoch-N.json`, and `diff run/candidates/baseline/Code.yaml run/candidates/epoch-2/Code.yaml`. Later runs may propose different text; the shape to expect is small instruction/skill overlays aimed at the failure summary, not a full profile rewrite.

## Optimize behavior (detail)

Allowed mutations:

- `append_instructions` — append text to profile instructions (never wholesale replace)
- `upsert_inline_skill` / `remove_inline_skill` — manage inline skills by name

Default stub optimize uses a deterministic proposer plus a profile-aware stub runner (instruction markers of the form `rsi:case:<case_id>`). That path does not call a model.

After the baseline, each later epoch re-runs only cases that are still failing and keeps prior results for the rest. A passing case is not re-checked, so a candidate is scored as if those cases still pass.

`--agent NAME` switches case evaluation to a headless session and asks a model to propose the same whitelist. The base profile is `NAME`, unless `--profile` points at a YAML file. `--optimizer-model` selects the proposer; `-m` selects the model that runs the cases. When `--optimizer-model` is omitted, the proposer uses the case-run model. Passing `--optimizer-model` without `--agent` still calls the proposer, but scores candidates with the stub runner. Live candidate runners load builtins and user agents first, then register the mutated profile on top, so declared sub-agents such as Code's Explore/General resolve like a normal iCode session.

## Status and limits

- Experimental: do not treat exports as production agent configuration until you have reviewed them.
- CLI only: no TUI or ACP entry points for RSI.
- `icode rsi start` remains a mock training loop and does not run optimize.
- Case workspaces sit under the task tree; a browsing agent may be able to read `input/dataset.json`. Prefer prompts and layouts that do not make the expect strings trivial to discover from the task directory.
