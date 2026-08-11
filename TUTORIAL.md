# Maestro tutorial: using it to the max

This is a hands-on walkthrough, not a reference — for architecture and
design rationale, see [README.md](README.md). Everything below is real
commands you can run, in the order you'd actually reach for them.

## 0. Before you start

You need `git`, the `claude` CLI on your `PATH` and logged in, and Maestro
installed (`pipx install "git+https://github.com/Adam-BH/maestro-cli.git"`
— see the README's Installation section for other options).

## 1. Your first mission

```bash
maestro run
```

You'll see the banner, then:

```
Maestro — describe the mission (what should be built/fixed/changed). Finish with
an empty line.
```

Type your mission, then an empty line to finish. What happens next, in order:

1. **Clarifying questions** — if your mission leaves something genuinely
   open (platform? persistence? auth?), you'll get up to 5 short questions
   with a suggested default each. Hit Enter to accept a default, or type
   your own answer. A mission that's already specific enough gets none of
   this — it's not a fixed checklist, it only asks what this particular
   mission left ambiguous.
2. **Refine** — your mission (+ any answers) gets turned into a real brief:
   a one-line overview, a bulleted feature list, and key user flows if it's
   complex enough to need them. You'll see this printed back before
   anything else happens — read it, because it's what the Planner builds
   from.
3. **Pick a folder** — defaults to a fresh subfolder named after your
   mission, auto-created and `git init`'d.
4. **Confirm** — `[Y]es / [e]dit mission / [f]older / [q]uit`.

From there the Planner reads the repo, decides a tech stack (or adopts an
existing one) and writes a task plan, and the Coder/Researcher/Tester ↔
Reviewer loop starts grinding through it. Full mechanics of that loop are
in the README's [The loop and gating
logic](README.md#the-loop-and-gating-logic-maestrolooppy) section — this
tutorial is about *driving* it, not how it works internally.

### Skipping the hand-holding

```bash
echo "a CLI tool that reverses text piped to stdin" | maestro run --yes
```

`--yes` skips clarifying questions, the confirmation prompt, and any
dirty-tree prompts — good for scripts, bad if you actually wanted the
clarifying questions to sharpen a vague mission. Piping the mission text
via stdin (instead of typing it interactively) works with or without
`--yes`.

## 2. Watching it work

Left running in the foreground, you get a live-updating panel (current
agent, cost/turns, task checklist) plus a scrolling activity log above it.
Colors mean something: each agent (planner, coder, researcher, tester,
reviewer) has its own color, so you can tell at a glance who's talking as
the log scrolls.

Want to see the plan without touching any code?

```bash
maestro run --dry-run
```

Runs only the Planner, prints the task table, and stops — nothing is
implemented. Good for sanity-checking a mission before committing to a
full run.

## 3. Detaching: start it, walk away, come back

This is the big one. `maestro run` normally owns your terminal for the
whole mission. `--detach` does mission intake right there as usual, then
hands the actual run off to a background process and gives you your
prompt back immediately:

```bash
maestro run --detach
```

```
╭────────────────────────────────── Detached ──────────────────────────────────╮
│ Session:  todo-app-24a3cf                                                    │
│ PID:      2950151                                                            │
│ Project:  /home/you/Desktop/todo-app                                         │
│ Log:      ~/.config/maestro/logs/todo-app-24a3cf.log                         │
│                                                                               │
│ maestro sessions attach todo-app-24a3cf   # tail live output                 │
│ maestro sessions stop todo-app-24a3cf     # gracefully interrupt it          │
│ maestro sessions list                     # see this and any other sessions  │
╰───────────────────────────────────────────────────────────────────────────────╯
```

Note the session ID down — you'll use it for everything below. (You can
also always get it back with `maestro sessions list`.)

### Checking on it

```bash
maestro sessions list
```

```
todo-app-24a3cf  [running]  status=in_progress  tasks=1/3  dir=/home/you/Desktop/todo-app  started=2026-08-11T13:35:31Z
```

Status and progress come straight from that project's live `STRATEGY.md`
— this isn't a cached snapshot from when you detached it.

### Watching it live

```bash
maestro sessions attach todo-app-24a3cf
```

Dumps everything logged so far, then keeps streaming new lines as they
happen — same colored agent activity you'd see in the foreground. **Ctrl-C
here only detaches your view.** It does not stop the session; you'll land
back at your prompt and the mission keeps running in the background. Run
`sessions attach` again any time to look back in.

### Starting several at once

Nothing about `--detach` is tied to one mission at a time — run it again
from a different (or the same) directory and you'll get a second,
independent session:

```bash
maestro run --detach -C ~/projects/blog-engine
maestro run --detach -C ~/projects/api-gateway
maestro sessions list
```

```
api-gateway-1a2b3c   [running]  status=in_progress  tasks=2/5  dir=/home/you/projects/api-gateway   started=...
blog-engine-9f8e7d   [running]  status=planning      tasks=0/0  dir=/home/you/projects/blog-engine   started=...
```

### Stopping one

```bash
maestro sessions stop todo-app-24a3cf
```

```
Sent interrupt to session todo-app-24a3cf (pid 2950151) — it will save progress and exit shortly.
```

This is a graceful `SIGINT` — the exact same thing that happens if you
Ctrl-C a foreground run: whatever task is in flight gets its progress
saved to `STRATEGY.md`, and the process exits cleanly. It is **not** a
force-kill; there's deliberately no such command, because interrupting
mid-write is exactly the kind of thing `STRATEGY.md`-as-source-of-truth
exists to make safe to do *carefully*, not recklessly.

## 4. Recovering from an interrupt (or a crash)

Whether it was `sessions stop`, a plain Ctrl-C, or your laptop dying —
recovery is the same command:

```bash
maestro run --resume -C /home/you/Desktop/todo-app
```

Any task that was stuck `in_progress` (mid-attempt when it got cut off)
resets to `pending` and gets picked up cleanly — no double work, no lost
plan. If some tasks got parked as `needs_human` and you've since fixed
whatever blocked them:

```bash
maestro run --resume --retry-blocked --yes
```

You can detach a resumed run too — `--resume` and `--detach` combine fine.

## 5. The watchdog: auto-resuming after a rate limit

Separate system from `sessions` — don't confuse the two. `sessions` is
about runs *you* started with `--detach`. The **watchdog** is about a run
that paused itself because it hit a usage/rate limit, and nothing is
watching to bring it back.

```bash
maestro watch add /home/you/Desktop/todo-app   # register the project
maestro watch install                          # install a systemd --user timer (every 15m)
```

From then on, if that project's run ever pauses with `status=rate_limited`,
the timer notices on its next tick, checks whether the reset time (read
live from `claude -p /usage`) has passed, and if so runs
`maestro run --resume --yes` for you automatically. `maestro watch list`
shows every registered project and its current status;
`maestro watch check` runs one check pass immediately instead of waiting
for the timer, useful for testing your setup.

`watch` never touches a run that's `blocked` on a parked `needs_human`
task — that needs an actual decision from you, not a timer. It only ever
acts on `rate_limited`.

### Combining the two: true "start it and forget it"

```bash
maestro run --detach -C ~/projects/big-migration
maestro watch add ~/projects/big-migration
maestro watch install   # once per machine, not once per project
```

Now the mission runs in the background, you can check on it or interrupt
it any time with `sessions`, and if it ever hits a rate limit while you're
away, the watchdog brings it back on its own — no terminal, no cron job of
your own, no babysitting.

## 6. Steering quality and cost

- `--deep-review` — after the Reviewer approves a task, also run Claude
  Code's own `/code-review` skill against the change, as a second opinion.
  Purely informational (logged, never changes the verdict) — costs one
  extra call per approved task, so it's off by default.
- `--pause-on-human` — the default is unattended: a task that needs a
  human decision gets parked and the loop moves on to the next one, so one
  ambiguous task can't stall an overnight run. Pass this to go back to
  stopping and waiting at a terminal prompt for every single one instead.
- `--max-task-retries N` (default 3) — how many times the Coder/Tester
  gets to retry a task after a Reviewer `REJECT` before it's escalated to
  `needs_human` automatically.
- `--commit-every-attempt` — checkpoint-commit after *every* attempt, not
  just approved ones. Useful if you want forensic git history of what got
  rejected and why; off by default to keep the log readable.
- If your mission says something like "don't commit anything, leave it for
  me to review" — that's picked up automatically at intake (no flag
  needed) and disables both Maestro's own checkpoint commits and the
  Coder/Tester's commits for the whole run.

## 7. Reading the receipts

Everything is plain files, on purpose — you're never locked into asking
Maestro to tell you what happened:

- **`STRATEGY.md`** in the project root — the mission, the tech-stack
  decision, every task's status/attempts/acceptance criteria, and a
  timestamped decision log. Hand-editable; `--resume` reloads it fresh.
- **`logs/*.json`** in the project root — one file per agent call, with
  the full prompt, result, cost, and turn count. This is what to check
  when a task's behavior surprises you and the terminal summary wasn't
  enough.
- **`~/.config/maestro/logs/*.log`** — one file per detached session
  (what `sessions attach` tails). Safe to `cat`/`grep` directly; it's a
  real terminal transcript, colors included.
- **`git log`** in the project — every approved task is its own commit
  (unless no-commit mode is active), so the commit history doubles as a
  changelog of what Maestro actually did.

## Cheat sheet

| Goal | Command |
|---|---|
| Start a mission, stay in the terminal | `maestro run` |
| Start unattended, mission via stdin | `echo "..." \| maestro run --yes` |
| Preview the plan only, no code | `maestro run --dry-run` |
| Start and hand off to the background | `maestro run --detach` |
| See all background sessions | `maestro sessions list` |
| Watch one live (read-only) | `maestro sessions attach <id>` |
| Gracefully interrupt one | `maestro sessions stop <id>` |
| Recover after a crash/interrupt | `maestro run --resume -C <dir>` |
| Give parked tasks another shot | `maestro run --resume --retry-blocked --yes` |
| Auto-resume after a rate limit | `maestro watch add <dir>` + `maestro watch install` |
| Second opinion via /code-review | `maestro run --deep-review` |
| Always stop for human decisions | `maestro run --pause-on-human` |

## Gotchas

- `--yes` skips clarifying questions entirely — it doesn't answer them
  with defaults, it just doesn't ask. If you want the defaults *used*,
  answer interactively and hit Enter on each one instead.
- `sessions attach` is read-only. You're watching, not driving — there's
  no way to type into a running session's Claude call.
- `watch` and `sessions` are unrelated registries. Detaching a run doesn't
  register it with the watchdog, and vice versa — do both explicitly if
  you want both.
- A session only shows up in `maestro sessions list` if it was started
  with `--detach`. A plain foreground `maestro run` — even a long one — is
  invisible to `sessions` (there's nothing to attach to; you're already
  attached).
