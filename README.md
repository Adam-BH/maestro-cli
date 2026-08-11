# Maestro

```
█   █   ███   █████   ████  █████  ████    ███
██ ██  █   █  █      █        █    █   █  █   █
█ █ █  █████  ████    ███     █    ████   █   █
█   █  █   █  █          █    █    █  █   █   █
█   █  █   █  █████  ████     █    █   █   ███
                autonomous Claude Code build loop
```

A CLI tool that orchestrates multiple Claude Code agents — Planner, Coder,
Researcher, Tester, Reviewer, and any others you add — in a
**plan → build → review** loop,
driving everything through the `claude -p` headless CLI. State lives in a
single human-readable `STRATEGY.md` at the repo root, which every agent
reads and every loop step updates. You describe what you want built once,
walk away, and come back to either a finished project or a clear,
task-by-task account of exactly where it got stuck and why.

## What this actually is

Maestro is not itself an AI — it has no model, no API key, no
intelligence of its own. It's a thin Python **process manager** around
Claude Code: every "agent" (Planner, Coder, Researcher, Tester, Reviewer)
is just a distinct system prompt plus a distinct tool scope, dispatched as
a fresh, isolated
`claude -p ...` subprocess call. All the intelligence comes from Claude;
everything this codebase owns is the *orchestration* around it — deciding
who runs next, what context they get, when to retry, when to stop, and
where all of that gets recorded so a human (or a later run) can pick up
the thread.

That separation is deliberate and shows up everywhere in the code:

- **Every agent call is stateless.** There's no long-lived conversation —
  each `claude -p` invocation gets a freshly rendered prompt built from
  `STRATEGY.md` plus whatever's specific to that call (the task at hand,
  a rejection reason, ...) and returns once. `maestro/claude_client.py`
  is the only module that knows how to shell out to `claude` and parse
  what comes back; nothing else touches a subprocess.
- **All continuity lives in one file, not in memory.** `STRATEGY.md` is
  the mission, the plan, every task's status, and a timestamped decision
  log — plain markdown, hand-editable, and reloaded fresh by
  `maestro/strategy.py` on every `--resume`. Kill the process at any
  point and nothing is lost; the next run just picks the file back up.
- **Control flow is ordinary Python, not another prompt.** `maestro/
  loop.py` is the only place that decides "call the Coder next," "retry
  this task," or "stop the whole run" — it's plain, readable code you can
  step through, not something an LLM decides turn-by-turn. That's what
  makes an unattended overnight run trustworthy: the gating logic is
  deterministic and testable, even though what each agent *produces*
  inside its turn isn't.
- **Agents are data, not special cases.** A "new agent" is a system
  prompt file + a small subclass + one line of config — see [Extending
  Maestro](#extending-maestro) below. The loop
  doesn't know or care how many agent types exist.

## How it works, start to finish

```
you describe a mission
        │
        ▼
  clarifying questions, if the mission is genuinely ambiguous
  (skipped under --yes; a clear mission gets none at all)
        │
        ▼
  Claude refines it into a feature brief — overview, core features,
  and key user flows (only what you stated or implied — no invented scope)
        │
        ▼
  you pick/confirm a project folder
        │
        ▼
   ┌─────────┐
   │ Planner │  reads STRATEGY.md + repo, decides a tech stack (or
   └────┬────┘  adopts an existing one), writes a task plan — each
        │       task gets acceptance criteria AND an assigned agent
        ▼       (coder / researcher / tester)
   ┌────────────────────────┐
┌─▶│  Coder / Researcher /  │
│  │  Tester (per task)     │
│  └────────────┬───────────┘
│                │ implements (+ commits, unless
│                │ no-commit constraint is active)
│                ▼
│         ┌──────────┐
│         │ Reviewer │  optional --deep-review: Claude Code's own
│         └────┬─────┘  /code-review also runs, informational only
│              │
│              ▼   one of three verdicts:
│
│   • APPROVE      → task done, move on to the next pending task
│   • NEEDS_HUMAN   → task parked (NOT retried), loop moves on to the
│                     next pending task anyway — unattended by default
└── REJECT          → back to Coder/Researcher/Tester above, with the
                       rejection reason as context (up to N retries,
                       then this also escalates to NEEDS_HUMAN)
```

## Installation

Maestro isn't on PyPI yet, but you don't need to clone the repo by hand to
install it — `pip`/`pipx` can install straight from the GitHub URL.

Requires **Python 3.9+**, **git**, and the **`claude` CLI** on your `PATH`,
logged in (or `ANTHROPIC_API_KEY` set — see the `--bare` note further
down) — [install Claude Code](https://docs.claude.com/claude-code) first
if you haven't.

### Option 1: pipx (recommended)

`pipx` installs `maestro` as a standalone command in its own isolated
environment, so its dependencies can't clash with any other project's, and
the `maestro` command is immediately on your `PATH`. Nothing gets cloned
to disk for you to manage.

```bash
pipx install git+https://github.com/Adam-BH/maestro-cli.git
```

Don't have `pipx` yet?

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
# restart your shell, then re-run the pipx install command above
```

### Option 2: pip, straight from GitHub

```bash
pip install git+https://github.com/Adam-BH/maestro-cli.git
```

This installs into whatever environment `pip` currently points at — use a
virtualenv unless you're sure you want it system/user-wide.

### Option 3: from a local clone (for contributing to Maestro itself)

Only needed if you're planning to edit Maestro's own source:

```bash
git clone https://github.com/Adam-BH/maestro-cli.git
cd maestro-cli
pipx install .          # or: pip install .
# or run without installing anything:
pip install -r requirements.txt   # just `rich`
python -m maestro.main run        # same as `maestro run` below
```

### Verify it worked

```bash
maestro --help
```

### Updating / uninstalling

```bash
pipx upgrade maestro
# or force-reinstall the latest commit:
pipx install --force git+https://github.com/Adam-BH/maestro-cli.git

pipx uninstall maestro
```

## Quick start

```bash
maestro run              # interactive: describe your mission, in the current directory
```

`run` is the main subcommand. There's also `watch`, which manages the
watchdog that auto-resumes a run paused on a usage/rate limit — see
[Watchdog: auto-resuming a rate-limited run](#watchdog-auto-resuming-a-rate-limited-run)
below.

Useful flags (all go after `run`, e.g. `maestro run --resume --yes`):

- `--dir PATH` / `-C PATH` — project directory to build in (created + `git init`'d if missing). Without it, you're prompted interactively (default: current directory). With `--resume`, this is where the existing `STRATEGY.md` lives.
- `--plan-only` / `--dry-run` — run only the Planner, print the plan, touch no code.
- `--resume` — pick up an existing `STRATEGY.md` instead of asking for a new mission.
- `--max-task-retries N` — Coder retries per task after a REJECT before escalating to a human (default 3).
- `--max-total-iterations N` — hard cap on total task cycles for the whole run (default 100).
- `--commit-every-attempt` — checkpoint-commit after every Coder attempt, not just approved tasks.
- `--model NAME` — model passed to `claude -p --model`.
- `-y` / `--yes` — skip interactive confirmations (mission confirm, dirty-tree prompts, project dir, git init); for scripted use.
- `--pause-on-human` — stop and wait at a terminal prompt on every NEEDS_HUMAN. Default is unattended: park the task and move on (see below) — pass this to get the old always-wait behavior back.
- `--retry-blocked` — with `--resume`, reset any parked (`needs_human`) tasks back to `pending` before continuing.
- `--deep-review` — after the Reviewer approves a task, also run Claude Code's own `/code-review` skill as a supplementary, informational pass (findings are logged, never change the verdict). Off by default — extra cost per approved task.
- `--detach` — do mission intake in this terminal, then hand the run off to the background and return immediately. See [Sessions: running missions in the background](#sessions-running-missions-in-the-background) below.

Maestro refuses to build a project inside its own source
directory (detected by the presence of `maestro/main.py` +
`agents/base.py` + `config.py`) — if you run it from there, it redirects to
`~/Desktop/<slug-of-your-mission>` instead of mixing your project into this
tool's code. Pass `--dir` explicitly to target anywhere else.

Also requires a git repository at the target project dir — the tool
will offer to `git init` for you if you're not in one yet.

### Mission intake

1. You type a free-text mission (typos and all), finished with an empty line.
2. A one-shot `claude -p` call (`agents/prompts/mission_clarifier.md`, no
   tools, tiny turn budget) looks for anything genuinely ambiguous about
   *this* mission — target platform, must-have vs. nice-to-have scope,
   auth/persistence needs, a design direction, a deployment target — and
   asks up to 5 short questions, each with a sensible suggested default
   (Enter accepts it). A mission that's already clear gets zero questions;
   this step is silent then, not padding for its own sake. Skipped
   entirely under `--yes`.
3. A one-shot `claude -p` call (`agents/prompts/mission_enhancer.md`, no
   tools) turns the mission — plus any answers from step 2 — into a
   structured feature brief: a one-line overview, a bulleted core-features
   list, and key user flows if the mission is complex enough to warrant
   them. It's told explicitly to make *implicit* scope explicit (e.g. "a
   todo app" implies add/edit/delete/mark-done/persistence) without
   inventing anything you didn't state or imply — no tech stack decisions
   here, that's the Planner's job (see below). The same call also extracts
   any explicit constraints you stated (e.g. "don't commit anything", "use
   Python 3.9 only") and whether a no-commit constraint applies, **and**
   suggests a short folder-name slug (e.g. "buld me a soduku app using
   react..." → `sudoku-react`). Extracted constraints are fed into every
   agent's prompt for the whole run, not just at intake. If either call
   fails for any reason, it degrades gracefully (no questions asked /
   your original wording used unchanged) rather than blocking the run.
4. You're asked where the project should live (`choose_project_dir`),
   defaulting to a fresh subfolder named after that slug (auto-created +
   `git init`'d) rather than dumping into whatever directory you happened
   to be in.
5. One confirmation: `[Y]es / [e]dit mission / [f]older / [q]uit`.
   `y`/Enter starts the run; `--yes` skips this and just proceeds.

## Why STRATEGY.md is the source of truth

Every agent invocation is a **fresh, stateless `claude -p` process**, run
with `--bare` when possible (so it doesn't pick up your global CLAUDE.md,
hooks, or MCP config — each run is reproducible; see the `--bare` note
under Assumptions for when this falls back). The only continuity between calls is
whatever we feed back in as context, which is `STRATEGY.md`'s rendered
text plus a couple of extra fields for the task at hand.

`STRATEGY.md` has five sections, all parsed back into structured state by
`maestro/strategy.py` (`Strategy.parse` / `Strategy.render` round-trip
losslessly):

- **Mission** — your original free-text request, unchanged.
- **Constraints** — explicit dos-and-don'ts extracted from the mission at
  intake (or "(none)"), fed into every agent's prompt for the whole run.
- **Meta** — run status, iteration count, timestamps, and whether a
  no-commit constraint is active.
- **Plan** — one `### Task N: title` block per task, each with a status
  (`pending` / `in_progress` / `done` / `rejected` / `needs_human`),
  attempt count, its assigned agent (`coder` / `researcher` / `tester`),
  and its acceptance criteria.
- **Decision Log** — an append-only, timestamped trail of what each agent
  did (`- [timestamp] (agent) message`), so you can read the file mid-run
  and understand exactly what happened without digging through logs.

Because it's plain markdown, you can open it during a paused run, read
what went wrong, hand-edit a task's acceptance criteria if the Planner got
something wrong, and `--resume`.

## The loop and gating logic (`maestro/loop.py`)

1. **Plan** (once, at the start of a run, unless `--resume` finds an
   existing plan): Planner gets the current `STRATEGY.md` and repo
   context. It first decides the tech stack — adopting whatever's already
   established in the repo, or, on a new/empty project, explicitly
   choosing a language/runtime, framework, and data layer that actually
   fits the mission and saying why — recorded in a `STACK: ... END_STACK`
   block that's written into `STRATEGY.md`'s `## Stack` section and stays
   there across plan revisions, so every later agent builds on the same
   decision instead of each one improvising its own. It then returns a
   `PLAN: ... END_PLAN` block — one task per entry, each assigned to
   `coder`, `researcher`, or `tester` depending on what kind of work it is
   — which is parsed into `Task` objects and written back into
   `STRATEGY.md`. A plan-summary table (task, agent, title) prints before
   execution starts, so you see the whole plan up front.
2. **For each pending/rejected task**, run a cycle:
   - Whichever agent the Planner assigned (**Coder**, **Researcher**, or
     **Tester**) gets the task, its acceptance criteria, and (on a retry)
     the previous rejection reason. Coder/Tester implement the change and
     commit it with a descriptive message; Researcher is read-only and
     reports findings instead. Unless a no-commit constraint is active
     (see Mission intake above), in which case nothing is committed by
     anyone and the changes stay in the working tree.
   - Maestro checkpoint-commits too (always after an APPROVE, unless
     no-commit is active; optionally after every attempt with
     `--commit-every-attempt`) as a safety net in case the producing
     agent's own commit didn't happen cleanly.
   - **Reviewer** gets the task, the acceptance criteria, and the
     producing agent's summary, inspects the actual diff (or, under
     no-commit, the uncommitted working tree) and runs tests itself (it
     never trusts the summary alone), and returns one of:
     - `APPROVE` → task marked `done`, checkpoint commit (unless
       no-commit), move to next task.
     - `REJECT: <reason>` → task marked `rejected`, looped back to the
       same agent with that reason as context. After `max_task_retries`
       rejections, escalates to `NEEDS_HUMAN` automatically so a bad task
       can't retry forever.
     - `NEEDS_HUMAN: <reason>` → task marked `needs_human` immediately.
   - With `--deep-review`: right after an `APPROVE`, before the checkpoint
     commit, Claude Code's own `/code-review` skill also runs against the
     change. This is purely additive and informational — its findings get
     logged to `STRATEGY.md`'s Decision Log and shown in the terminal, but
     never change the task's status. The Reviewer above remains the sole
     gate on whether a task is actually `done`.
3. **On NEEDS_HUMAN** (default, unattended): the loop immediately shows a
   prominent "⚠ NEEDS HUMAN INPUT" alert in the terminal so it can't get
   lost in real-time scrollback, logs the reason, leaves that task marked
   `needs_human`, and moves straight on to the next pending task — a
   single bad task never stalls the rest of the run, so you can start it
   and walk away. Once nothing pending/rejected is left, the run stops
   with status `blocked` if anything is still parked, or `done` if
   everything got through. The final summary also lists every parked task
   with its reason; fix what's needed and re-run with
   `--resume --retry-blocked` to give them another shot. Pass
   `--pause-on-human` to go back to the old behavior — the loop stops and
   waits at a terminal prompt (`[r]etry` / `[q]uit`) on every single one.
4. **On a usage/rate limit** (detected from the `claude -p` response —
   phrases like "usage limit", "rate limit", "quota", HTTP 429): this is
   different from a task problem — every subsequent call would fail too,
   so continuing to the next task is pointless. The loop pauses the
   *entire* run immediately, regardless of `--pause-on-human`, and writes
   a best-effort "resume after" time into STRATEGY.md's Meta section
   (parsed from the limit message where possible, e.g. a reset
   timestamp; otherwise "unknown — try again later"). The task in flight
   is put back to `pending` with its attempt not counted, so `--resume`
   retries it cleanly once the limit clears.
5. **When every task is `done`**, the loop prints a summary (tasks
   completed, commits made, total turns/cost) and exits 0. Exit code is
   `2` for `blocked`/`failed`, `3` for `rate_limited` — useful if you're
   scripting this (e.g. a cron job that checks the exit code and only
   re-invokes `--resume` after the resume-after time has passed).

A global `max_total_iterations` cap exists purely as a backstop against a
pathological plan — it should never realistically be hit.

## Sessions: running missions in the background

`maestro run` normally blocks the terminal it's running in for the whole
mission. `--detach` lets you do mission intake in that terminal as usual,
then hand the actual run off to a background process and get your prompt
back immediately — so you can start several missions from the same
machine and check on each one independently, or close the terminal without
killing the run.

```bash
maestro run --detach          # intake happens here, then it backgrounds itself
maestro sessions list         # see every detached session and its live status
maestro sessions attach <id>  # tail its output; Ctrl-C detaches without stopping it
maestro sessions stop <id>    # gracefully interrupt it (same as pressing Ctrl-C on it)
```

This isn't a daemon or a new process-supervision model — it's a thin
registry (`maestro/sessions.py`, `~/.config/maestro/sessions.json`) around
machinery that already exists:

- A detached session is just an ordinary `maestro run --resume --yes`
  subprocess, launched in its own OS session (`start_new_session=True`) so
  it survives the parent terminal closing, with its output redirected to a
  log file under `~/.config/maestro/logs/`.
- `maestro sessions list` reads that log's project directory's
  `STRATEGY.md` directly for live status/progress, and checks the PID
  (`os.kill(pid, 0)`) for whether it's still running — no state is
  duplicated, `STRATEGY.md` stays the single source of truth.
- `maestro sessions attach <id>` just tails that log file; detaching
  again (Ctrl-C) only stops watching it, not the underlying run.
- `maestro sessions stop <id>` sends `SIGINT` to the process — the exact
  same signal a foreground Ctrl-C sends, which Maestro already handles by
  saving `STRATEGY.md` and exiting cleanly (see [The loop and gating
  logic](#the-loop-and-gating-logic-maestrolooppy) above). `--resume` picks
  it back up from there, detached or not.

## Watchdog: auto-resuming a rate-limited run

When a run pauses on a usage/rate limit (step 4 above), nothing brings it
back on its own by default — you'd have to remember to come back and run
`maestro run --resume` yourself once the limit clears. `maestro watch`
automates exactly that, and only that. It's a **one-shot script + system
timer** (`maestro/scheduler.py`), not a background daemon and not another
agent watching your terminal: `maestro watch check` looks at every
registered project once, does nothing unless one of them is due, and
exits. A system timer just calls that one-shot on a schedule.

### How it knows when to retry

- **Session/weekly usage limits** — the ordinary "you've hit your usage
  limit" pause — get an exact reset time. On pause, Maestro shells out to
  `claude -p "/usage"` (`get_usage_resets()` in `maestro/claude_client.py`)
  — the same read-only check available as `/usage` in an interactive
  session — and parses its real answer (e.g. "Current session: resets Aug
  11, 1:30pm (Africa/Tunis)" / "Current week: resets Aug 13, 3am
  (Africa/Tunis)") into an exact UTC timestamp. That's ground truth from
  your account, not a guess.
- **Monthly spend/credit caps** — a separate, billing-level limit ("You've
  hit your monthly spend limit") — have no queryable reset time at all
  (`claude`'s own `/usage-credits` command just opens a browser to
  `claude.ai/settings/usage` so you can manage it by hand; Maestro never
  calls that command itself, so the watchdog never pops a browser open on
  its own). For these, `resume_after` is honestly recorded as "unknown,"
  and the watchdog just retries every 6 hours
  (`UNKNOWN_HINT_RETRY_INTERVAL` in `maestro/scheduler.py`) until it
  clears, instead of committing to a fabricated exact-looking date.

### Commands

```bash
maestro watch add .                          # register the current project directory
maestro watch add /path/to/project           # or a specific one
maestro watch list                           # show every registered project + its status/resume time
maestro watch check                          # run one check pass right now (what the timer calls)
maestro watch install                        # install + start a systemd --user timer (every 15m)
maestro watch install --interval-minutes 5   # custom interval
maestro watch remove .                       # stop watching a project
maestro watch uninstall                      # stop and remove the timer entirely
```

`maestro watch check` walks every registered project's `STRATEGY.md`; for
each one whose `run_status` is `rate_limited` and whose resume time has
passed, it runs `maestro run --resume --yes` for you and logs the output
to `~/.config/maestro/logs/<project>-<timestamp>.log`. Only
`run_status == rate_limited` is handled — a `blocked` run (tasks parked on
`needs_human`) needs an actual human decision, not a timer; that's the
prompt `maestro run` shows you at the end of a blocked interactive run
instead.

### Installing the timer (Linux)

`maestro watch install` writes and enables a `systemd --user` timer +
oneshot service (`~/.config/systemd/user/maestro-watchdog.{timer,service}`)
that calls `maestro watch check` on the interval you gave it. This
survives terminal closures and reboots for free, since it's just
systemd — Maestro isn't running any process of its own between checks.

One extra step if you want it to keep firing after you log out entirely
(not just while a graphical/terminal session is open):

```bash
loginctl enable-linger $USER
```

It's a normal systemd unit, so normal systemd commands work on it too:

```bash
systemctl --user status maestro-watchdog.timer     # is it active?
systemctl --user list-timers                       # when does it next fire?
journalctl --user -u maestro-watchdog.service       # past run output
```

**Not on Linux?** `install`/`uninstall` are systemd-specific — there's no
macOS/Windows equivalent built in yet. `maestro watch check` itself has no
such dependency though: wire it into `cron` or `launchd` (macOS) or Task
Scheduler (Windows) yourself to get the same effect — just run
`maestro watch check` (or `python -m maestro.main watch check` from a
source checkout) every N minutes.

### State

- `~/.config/maestro/watchlist.json` — registered project directories.
- `~/.config/maestro/watchdog-state.json` — per-project last-attempt
  timestamps, so an unknown resume hint isn't retried on every single
  tick.
- `~/.config/maestro/logs/` — one log file per resume attempt the
  watchdog makes.

### Customizing

| You want to... | Touch this |
|---|---|
| Change how often the timer fires | `maestro watch install --interval-minutes N` (re-running install overwrites the existing timer unit) |
| Change how long to wait before retrying an "unknown" resume hint | `UNKNOWN_HINT_RETRY_INTERVAL` in `maestro/scheduler.py` |
| Change what counts as a rate limit / spend cap, or how a reset time is extracted | `is_rate_limited()`, `is_spend_limited()`, `extract_resume_hint()`, `get_usage_resets()` in `maestro/claude_client.py` — update these if a future `claude` CLI version phrases limit errors differently |
| Change what happens on each check (e.g. notify somewhere instead of just resuming) | `check_project()` in `maestro/scheduler.py` |

## Terminal UI (`maestro/ui/console.py`)

Built on `rich`, not `textual`. This loop is a **linear, blocking
pipeline** — one `claude -p` subprocess runs to completion before the next
agent starts — not an app with concurrent input or navigable screens.
`rich.live.Live` manages a small, live-updating region (current agent +
task checklist, running cost/turn totals) for a fraction of the code
`textual` would need. If a future agent needed concurrent panes or
keyboard navigation, `textual` would be worth revisiting then.

Each agent has a fixed color used consistently everywhere: **Planner =
blue, Coder = green, Researcher = cyan, Tester = bright magenta, Reviewer
= yellow**.

**Real scrollback, not a bounded panel.** Activity log lines print
straight to ordinary terminal scrollback (through `Live`'s console proxy)
instead of being confined to a fixed-height panel that erases old lines on
every redraw — so you can actually scroll back and see what happened.
When stdout isn't a real terminal (piped output, redirected to a file),
`Live` is skipped entirely and everything falls back to plain sequential
prints, so output still streams incrementally instead of only appearing
in one dump at process exit.

**Live activity, not just final results.** Agent calls run with
`--output-format stream-json --verbose` and a per-event callback
(`Loop._event_logger` → `claude_client.summarize_stream_event`), so tool
calls (`→ Bash(pytest ...)`, `→ Edit(app.py)`), assistant reasoning text,
and thinking blocks show up as they happen, not only after the whole call
finishes. The plain `json` blocking path still exists (`ClaudeClient.run()`
without `on_event=`) for calls nobody's watching, like the prompt-enhancer
pass.

**Cost is only shown when it's real.** The dollar figure in the header and
final summary reflects `--bare`/API-key billing; under OAuth/subscription
auth it's hidden (calls/turns still show), since it isn't a real charge.

**Resize-safe by construction.** Panel sizes are recomputed from
`self.console.size` on every single render (see `MaestroUI._render()`),
and every line of text is `no_wrap=True, overflow="ellipsis"` rather than
left to wrap. That combination is what actually matters for Rich's `Live`:
it redraws by erasing exactly as many lines as the *previous* frame took,
so if a resize changes how many lines something wraps into, or the live
region ever grows taller than the terminal, that erase math goes stale and
you get duplicated/glitchy output. Forcing single-line-or-ellipsis text
and a height that's always ≤ the terminal's sidesteps both causes. A
`SIGWINCH` handler additionally forces an immediate `Live.refresh()` the
instant a resize happens, instead of waiting up to `1/refresh_per_second`.

## Extending Maestro

Everything here is built so a new agent type, a tweak to an existing
one's behavior, or a new gating rule is a small, local change — not a
rewrite of the loop. Two ways to make that change: do it yourself, or
have Claude Code do it, since this codebase is exactly the kind of
well-isolated, pattern-following project it's good at extending.

### What's easy to change, and where

| You want to... | Touch this |
|---|---|
| Change what a Planner/Coder/Researcher/Tester/Reviewer is told to do | `agents/prompts/<name>.md` — plain text, no code |
| Add a brand new agent (Security review, Docs pass, ...) | New prompt file + small class, see below |
| Change an agent's tool access or turn budget | `config.py`'s `Config.agents` dict |
| Change retry counts, iteration caps, model, `--bare` | `config.py`'s `Config` dataclass, or the matching CLI flag in `maestro/main.py` |
| Change *when* an agent runs, or add a step to the cycle | `maestro/loop.py`'s `run_task_cycle` / `run` |
| Change what counts as APPROVE/REJECT/NEEDS_HUMAN | The relevant `agents/prompts/*.md` (the model decides) — the *parsing* of that verdict is `agents/reviewer.py`'s `parse_verdict` |
| Change the STRATEGY.md format itself | `maestro/strategy.py` — `render()`/`parse()` are the only two places that need to agree |
| Change the terminal UI | `maestro/ui/console.py` — self-contained, nothing else imports `rich` directly |

### Adding a new agent type, by hand

No changes to `loop.py`'s control flow, `main.py`, or the UI are required
for a new *kind* of check (e.g. a Security review pass after Reviewer, or
a Docs agent). Steps:

1. **Write its system prompt**: `agents/prompts/security.md`, following
   the pattern in `planner.md`/`coder.md`/`reviewer.md` — describe the
   role, the rules, and a strict, parseable output format for its final
   message. This file *is* the agent's behavior; there's no hidden logic
   elsewhere that overrides what it says.
2. **Subclass `Agent`** in `agents/security.py`:
   ```python
   from agents.base import Agent, AgentContext

   class Security(Agent):
       name = "security"
       color = "red"
       prompt_file = "security.md"

       def build_prompt(self, context: AgentContext) -> str:
           ...  # pull whatever you need out of context.extra
   ```
   Add a small parser function for its structured output, same pattern as
   `parse_verdict` (`agents/reviewer.py`) or `parse_agent_result`
   (`agents/coder.py` — shared by Coder/Researcher/Tester's identical
   `RESULT:` block) — regex out whatever block you told it to end its
   message with.
3. **Register its tool scope and turn budget** in `config.py`'s
   `Config.agents` dict (`AgentToolConfig(allowed_tools=..., max_turns=...)`).
   Give read-only agents (like Reviewer) no `Edit`/`Write` here — that's
   the actual enforcement mechanism, not just a prompt instruction.
4. **Call it from `loop.py`** wherever it belongs in the cycle — e.g. add
   a `self.security = Security(...)` in `Loop.__init__` and invoke it in
   `run_task_cycle` after `Reviewer` approves, before the checkpoint
   commit. The retry/gating/logging/UI plumbing (`ui.set_agent`,
   `ui.record_call`, `strategy.add_log`, the JSON call log, live
   stream-json activity) is identical for every agent because it all
   lives in `Agent.run()` / the loop body, not per-agent code — you're
   only writing the parts that are actually new.

### Adding a new agent type, by prompting Claude Code

Since Maestro's own source is a normal, readable git repo,
you don't have to write the class and prompt file yourself — open Claude
Code in this directory (`claude` from the repo root, or continue this
session) and hand it something like:

> Add a new agent called `Security` to Maestro, following the
> exact pattern of the existing Planner/Coder/Reviewer agents (see
> `agents/base.py`, `agents/reviewer.py`, and `agents/prompts/reviewer.md`
> as the closest reference — it's also read-only and returns a verdict).
> It should run after Reviewer APPROVEs a task and before the checkpoint
> commit, scanning the task's diff for hardcoded secrets, obvious
> injection vulnerabilities, and unsafe shell usage. Give it Read/Grep/Bash
> access but no Edit/Write. It should end its message with `VERDICT: PASS`
> or `VERDICT: FAIL: <reason>`. On FAIL, treat the task like a Reviewer
> REJECT (send it back to the Coder with that reason, subject to the same
> `max_task_retries`). Wire it into `config.py`'s `Config.agents` and into
> `Loop` in `maestro/loop.py`. Don't change the Planner/Coder/Reviewer
> classes or `main.py`.

Being specific about **where it slots into the cycle** and **what its
pass/fail contract is** is what makes this a fast, correct one-shot change
instead of Claude Code having to guess your intent from a vague "add a
security check" — the same acceptance-criteria discipline this tool asks
of its own Planner applies to prompting Claude Code directly, too.

## Logs

Every `claude -p` call is logged to `logs/<timestamp>_<agent>.json` with
the full prompt, command line, parsed result fields (`session_id`,
`cost_usd`, `num_turns`, `duration_ms`), and (truncated) stderr — in
addition to the pretty terminal output. Useful for debugging a parse
failure or a surprising verdict after the fact.

## Assumptions / defaults (spec was silent or ambiguous on these)

- **Planner runs once per run**, not once per task. The spec's loop
  description reads as "Planner produces a plan, then Coder/Reviewer
  iterate task-by-task," so that's what's implemented. `plan_step()` is a
  separate method and easy to call again mid-run if you want a
  re-planning agent later.
- **Both the producing agent and Maestro commit.** Coder/Tester are
  prompted to make their own descriptive commit (per spec: "commits its
  work to git with a descriptive message"). Maestro *also* does a
  checkpoint commit after every approval (always) and optionally after
  every attempt (`--commit-every-attempt`) as a safety net — if the
  producing agent already committed cleanly, this is a no-op (nothing left
  to commit). Exception: if the mission's extracted constraints include a
  no-commit directive, neither commits anything for the whole run — see
  Mission intake above.
- **`claude -p` JSON field names**: the spec names `result`, `session_id`,
  `cost_usd`, and turn count. Field names have shifted across Claude Code
  versions, so `claude_client.py` accepts common aliases defensively
  (`cost_usd`/`total_cost_usd`, `num_turns`/`turns`, `result`/`response`)
  rather than hard-failing if your installed version uses a different key.
- **`--bare` and other flags**: built exactly as specified
  (`--output-format json --bare --allowedTools ... --permission-mode
  acceptEdits --max-turns N`). If your `claude` version doesn't support a
  flag, `ClaudeClient.build_command()` is the one place to change.
- **`--bare` requires `ANTHROPIC_API_KEY`** — per `claude --help`, bare mode
  never reads OAuth or keychain auth. If you're logged in via a Claude
  subscription (no API key set), `ClaudeClient` detects this and silently
  runs without `--bare` instead of failing every call, with a one-time
  notice printed at startup. Set `ANTHROPIC_API_KEY` to actually get
  `--bare`'s reproducibility, or pass `--no-bare` to opt out on purpose.
- **"Verification pass" on mission intake**: checks the working directory
  is a git repo (offers `git init`), checks the tree is clean (offers
  stash/commit/ignore), and does a best-effort scan for path-like tokens
  mentioned in the mission text, warning (non-blocking) if they don't
  exist yet.
- **On `NEEDS_HUMAN`, retry re-runs the same task from `pending`** with
  its attempt counter reset — the assumption being you fixed whatever
  external thing blocked it (credentials, an ambiguous decision, etc.)
  before choosing "retry" (`--pause-on-human` mode) or `--retry-blocked`
  (default unattended mode).
- **Rate-limit *detection* is best-effort text matching**, not a
  structured error code from the CLI — `maestro/claude_client.py`'s
  `is_rate_limited()` matches on phrases like "usage limit"/"rate
  limit"/"quota"/429. If a future CLI version phrases limit errors
  differently, that's the one place to update the patterns. Once
  detected, the *reset time* is not guessed from that same text where it
  can be avoided: `get_usage_resets()` live-queries `claude -p "/usage"`
  for the real session/week reset time, ground truth from the account.
  The one case with no queryable reset at all is a monthly spend/credit
  cap (`is_spend_limited()`) — that's recorded as "unknown" and retried
  periodically rather than given a fabricated date. See
  [Watchdog](#watchdog-auto-resuming-a-rate-limited-run) above.
- **Task IDs** are `task-<n>` in plan order; re-planning matches tasks by
  title to preserve `done`/`attempts` state for anything that already
  ran.

## Project layout

```
maestro/
  main.py            entrypoint: mission intake, verification, kicks off the loop
  loop.py             plan -> code -> review loop controller, retry/gating logic
  claude_client.py     subprocess wrapper around `claude -p`, JSON + stream-json parsing
  strategy.py           STRATEGY.md <-> structured state, round-trip parse/render
  git_utils.py           commit checkpoints, diff summaries, clean-tree checks
  scheduler.py            watchdog: watchlist, one-shot check, systemd --user timer install
  sessions.py              --detach session registry: register/list/attach/stop
  ui/console.py           rich-based terminal UI, incl. the startup banner
agents/
  base.py             Agent base class: loads its prompt file, runs it, logs the call
  planner.py           produces the task plan, assigns each task an agent
  coder.py              implements one task, commits; shared RESULT: parser
  researcher.py          investigates a task read-only, reports findings
  tester.py              writes/extends/runs tests for one task, commits
  reviewer.py            APPROVE / REJECT / NEEDS_HUMAN against acceptance criteria
  prompts/*.md          system prompts, one file per agent, plus the intake
                         passes (mission_clarifier.md, mission_enhancer.md)
config.py             model, retry limits, tool scopes, per-agent turn budgets
requirements.txt / pyproject.toml   packaging
```
