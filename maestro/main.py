"""
CLI entrypoint: mission intake, pre-flight verification, and kicking off
the plan -> code -> review loop.

    maestro run                 # interactive mission intake
    maestro run --plan-only     # print a plan, touch no code
    maestro run --resume        # pick up an existing STRATEGY.md

(equivalently: `python -m maestro.main run ...` when running from source
without installing the package)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from rich import box
from rich.table import Table
from rich.text import Text

from config import DEFAULT_CONFIG
from maestro import git_utils, scheduler, sessions
from maestro.claude_client import ClaudeCLINotFound, is_auth_error
from maestro.loop import Loop
from maestro.strategy import Strategy
from maestro.ui.console import MaestroUI


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="maestro",
        description="Orchestrate Planner/Coder/Researcher/Tester/Reviewer Claude Code agents in a plan->build->review loop.",
        epilog="Run `maestro run --help` for the full list of run options and examples.",
    )
    subparsers = p.add_subparsers(dest="command", required=True)

    run_p = subparsers.add_parser(
        "run",
        help="Run a mission (plan -> build -> review loop) in the current or given directory.",
        description="Run a mission (plan -> build -> review loop) in the current or given directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  maestro run                                     Start a new mission (interactive prompts)
  maestro run --yes                               Start a new mission, unattended (no prompts)
  maestro run --dry-run                           Preview the Planner's task list only; no code touched
  maestro run --resume                            Resume an existing STRATEGY.md in the current directory
  maestro run --resume -C /path/to/project         Resume a STRATEGY.md living in another directory
  maestro run --resume --yes                       Resume, unattended
  maestro run --resume --retry-blocked --yes       Resume and give any needs_human tasks another shot
                                                    (e.g. right after fixing whatever tool/permission
                                                    problem caused them to get stuck)
  maestro run --resume --pause-on-human            Resume, but stop at a terminal prompt on every
                                                    needs_human task instead of parking it and moving on
  maestro run --detach                             Start a mission, then hand it off to the background --
                                                    manage it afterward with `maestro sessions`

A crash or Ctrl-C mid-run is always safe to recover from with `maestro run --resume` --
progress lives in STRATEGY.md (and, unless the mission was NO-COMMIT, in git history).
""",
    )
    run_p.add_argument(
        "--dry-run", "--plan-only", dest="plan_only", action="store_true",
        help="Run only the Planner and print the resulting plan; touch no code.",
    )
    run_p.add_argument(
        "--resume", action="store_true",
        help="Resume from an existing STRATEGY.md instead of prompting for a new mission.",
    )
    run_p.add_argument(
        "--dir", "-C", dest="dir", default=None,
        help="Project directory to build in (created if missing). Defaults to the "
        "current directory, or an interactive prompt if unset. With --resume, this "
        "is where the existing STRATEGY.md lives.",
    )
    run_p.add_argument(
        "--strategy-file", default=DEFAULT_CONFIG.strategy_path,
        help=f"Path to the strategy file (default: {DEFAULT_CONFIG.strategy_path}).",
    )
    run_p.add_argument(
        "--model", default=DEFAULT_CONFIG.model,
        help="Model passed to `claude -p --model`.",
    )
    run_p.add_argument(
        "--max-task-retries", type=int, default=DEFAULT_CONFIG.max_task_retries,
        help="Max Coder retries per task after a Reviewer REJECT before escalating to NEEDS_HUMAN.",
    )
    run_p.add_argument(
        "--max-total-iterations", type=int, default=DEFAULT_CONFIG.max_total_iterations,
        help="Hard cap on total task cycles for the whole run.",
    )
    run_p.add_argument(
        "--commit-every-attempt", action="store_true", default=DEFAULT_CONFIG.commit_every_attempt,
        help="Checkpoint-commit after every Coder attempt, not just approved tasks.",
    )
    run_p.add_argument(
        "--no-bare", dest="bare", action="store_false", default=DEFAULT_CONFIG.bare,
        help="Don't pass --bare to `claude -p` (use normal auth/hooks/CLAUDE.md). "
        "Auto-disabled anyway when ANTHROPIC_API_KEY isn't set.",
    )
    run_p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip interactive confirmations (mission confirm, dirty-tree prompts). For scripted use.",
    )
    run_p.add_argument(
        "--detach", action="store_true",
        help="Do mission intake in this terminal, then hand the actual run off to a "
        "background process and return immediately. Manage it afterward with "
        "`maestro sessions list/attach/stop`.",
    )
    run_p.add_argument(
        "--pause-on-human", action="store_true",
        help="Stop and wait at a terminal prompt every time a task needs human input, "
        "like earlier versions did. Default is unattended: a blocked task is parked "
        "and the loop moves on to the next one, so a single bad task can't stall an "
        "overnight run.",
    )
    run_p.add_argument(
        "--no-skill", action="store_true",
        help="Don't auto-generate a .claude/skills/<slug>/SKILL.md for the finished "
        "project once the mission is done.",
    )
    run_p.add_argument(
        "--deep-review", action="store_true",
        help="After the Reviewer approves a task, also run Claude Code's own "
        "/code-review skill against the change as a supplementary, informational "
        "pass — findings are logged to STRATEGY.md but never change a task's "
        "verdict. Off by default (extra cost per approved task).",
    )
    run_p.add_argument(
        "--retry-blocked", action="store_true",
        help="With --resume: reset any needs_human tasks back to pending (attempts=0) "
        "before continuing, so they get another shot after you've fixed whatever blocked them.",
    )
    run_p.add_argument(
        "--fanout", type=int, default=None, metavar="N",
        help="Split this already-planned project's pending tasks evenly across N git "
        "worktrees/branches and launch N detached sessions, one per branch, instead of "
        "running the plan in this directory. Requires an existing STRATEGY.md (run the "
        "Planner first, e.g. with --dry-run) and a clean git repo. Maestro does not know "
        "which tasks are safe to run in parallel -- that's on you to judge before fanning out.",
    )
    run_p.add_argument(
        "--push-on-done", action="store_true",
        help="Push the current git branch to its remote once the mission finishes "
        "successfully. Set automatically on each session started by --fanout; also usable "
        "standalone on an ordinary run.",
    )

    watch_p = subparsers.add_parser(
        "watch",
        help="Manage the watchdog that auto-resumes runs paused on a usage/rate limit.",
        description="Register project directories for the watchdog, and manage the "
        "background timer that periodically checks them and re-runs `maestro run "
        "--resume` once a paused run's limit should have cleared.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  maestro watch add .                  Register the current project directory
  maestro watch list                   Show registered projects and their status
  maestro watch check                  Run one check pass now (what the timer calls)
  maestro watch install                Install + start a systemd --user timer (every 15m)
  maestro watch remove .                Unregister the current project directory
  maestro watch uninstall              Stop and remove the timer

Only run_status == rate_limited is handled here — a `blocked` run (parked
needs_human tasks) needs a human decision, not a timer; see the prompt
`maestro run` shows at the end of a blocked interactive run instead.
""",
    )
    watch_sub = watch_p.add_subparsers(dest="watch_command", required=True)

    watch_add = watch_sub.add_parser("add", help="Register a project directory for the watchdog.")
    watch_add.add_argument("dir", nargs="?", default=".", help="Project directory (default: current directory).")

    watch_remove = watch_sub.add_parser("remove", help="Unregister a project directory.")
    watch_remove.add_argument("dir", nargs="?", default=".", help="Project directory (default: current directory).")

    watch_sub.add_parser("list", help="List registered project directories and their current status.")
    watch_sub.add_parser("check", help="Run one check pass over all registered projects now.")

    watch_install = watch_sub.add_parser(
        "install", help="Install and start a systemd --user timer that runs `maestro watch check` periodically."
    )
    watch_install.add_argument(
        "--interval-minutes", type=int, default=15, help="How often the timer fires (default: 15)."
    )

    watch_sub.add_parser("uninstall", help="Stop and remove the systemd --user timer.")

    sessions_p = subparsers.add_parser(
        "sessions",
        help="List, tail, or interrupt runs started with `maestro run --detach`.",
        description="Manage detached background runs registered by `maestro run --detach`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  maestro sessions list             Show every detached session and its current status
  maestro sessions attach <id>      Tail a session's live output (Ctrl-C just detaches, doesn't stop it)
  maestro sessions stop <id>        Gracefully interrupt a session (same as pressing Ctrl-C on it)
  maestro sessions remove <id>      Forget a finished session (registry entry only, log is kept)
  maestro sessions clean            Forget every finished (non-running) session at once
  maestro sessions push <id>        Push a fanned-out session's branch (see `maestro run --fanout`)
""",
    )
    sessions_sub = sessions_p.add_subparsers(dest="sessions_command", required=True)
    sessions_sub.add_parser("list", help="List all detached sessions.")
    sessions_attach = sessions_sub.add_parser("attach", help="Tail a session's live log output.")
    sessions_attach.add_argument("id", help="Session id (see `maestro sessions list`).")
    sessions_stop = sessions_sub.add_parser("stop", help="Gracefully interrupt a session.")
    sessions_stop.add_argument("id", help="Session id (see `maestro sessions list`).")
    sessions_remove = sessions_sub.add_parser("remove", help="Forget a finished session's registry entry.")
    sessions_remove.add_argument("id", help="Session id (see `maestro sessions list`).")
    sessions_sub.add_parser("clean", help="Forget every finished (non-running) session's registry entry.")
    sessions_push = sessions_sub.add_parser(
        "push", help="Push a fanned-out session's branch to its remote (manual escape hatch)."
    )
    sessions_push.add_argument("id", help="Session id (see `maestro sessions list`).")

    subparsers.add_parser(
        "update",
        help="Pull the latest main from GitHub into this installed copy of maestro.",
        description="Self-update: pulls origin/main into the installed maestro source "
        "(editable installs only) or re-runs `pipx upgrade maestro` otherwise.",
    )

    return p.parse_args(argv)


def build_config(args: argparse.Namespace):
    cfg = DEFAULT_CONFIG
    cfg.strategy_path = args.strategy_file
    cfg.model = args.model
    cfg.max_task_retries = args.max_task_retries
    cfg.max_total_iterations = args.max_total_iterations
    cfg.commit_every_attempt = args.commit_every_attempt
    cfg.bare = args.bare
    cfg.deep_review = args.deep_review
    cfg.generate_skill = not args.no_skill
    return cfg


# -- mission intake --------------------------------------------------

def _drain_stdin() -> None:
    """Discards whatever's sitting unread in the terminal's input buffer.
    Called right before an input() prompt that follows a blocking network
    call (clarify_mission/enhance_mission) — those calls give no visual
    feedback of their own turnaround time, so a user who thinks the CLI
    has hung tends to mash Enter (or Shift-Enter, which most terminals
    turn into a plain newline) while waiting. Without this, every one of
    those buffered newlines gets silently consumed by the next input()
    call in sequence, which reads as clarifying questions being skipped
    entirely rather than actually asked. Best-effort: only works on a real
    POSIX tty, and it's fine to no-op anywhere else (Windows, piped
    stdin) since there's nothing queued to drain in those cases anyway."""
    try:
        import termios
    except ImportError:
        return  # not POSIX (e.g. Windows) -- nothing to drain the same way
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, ValueError, OSError):
        pass


def prompt_mission(ui: MaestroUI) -> str:
    ui.console.print(
        "[bold cyan]Maestro[/bold cyan] — describe the mission "
        "(what should be built/fixed/changed). Finish with an empty line.\n"
    )
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()


@dataclass
class MissionIntake:
    mission: str
    slug: Optional[str]
    constraints: List[str] = field(default_factory=list)
    no_commit: bool = False


@dataclass
class ClarifyQuestion:
    question: str
    default: str


def clarify_mission(ui: MaestroUI, client, cfg, mission: str) -> List[ClarifyQuestion]:
    """One-shot `claude -p` pass that spots genuinely ambiguous points in
    the raw mission and turns them into a handful of short questions with
    sensible defaults. Falls back to no questions on any error or
    unparseable output — this is a nicety, not something worth blocking
    the run over (same tolerance as enhance_mission)."""
    prompt_path = Path(cfg.prompts_dir) / "mission_clarifier.md"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    # This is the very first Claude call of a run, before ui.live() ever
    # starts (see main()) -- so without an explicit spinner here there's
    # no animation at all telling the user anything's happening, and a
    # slow turnaround just looks like a hang. console.status() gives the
    # same spinner used elsewhere without needing a live region.
    with ui.console.status("[cyan]Reading your mission for anything worth clarifying...[/cyan]", spinner="dots"):
        result = client.run(
            prompt=f"Raw mission:\n\n{mission}",
            allowed_tools="",
            max_turns=cfg.mission_clarifier_max_turns,
            permission_mode="acceptEdits",
            system_prompt=system_prompt,
            timeout=cfg.mission_intake_timeout,
        )
    if not result.ok:
        return []

    m = re.search(r"QUESTIONS:\s*\n(.*?)\nEND_QUESTIONS", result.result_text, re.S)
    if not m:
        return []
    block = m.group(1).strip()
    if not block or block == "(none)":
        return []

    questions: List[ClarifyQuestion] = []
    for line in block.splitlines():
        line = line.strip().lstrip("- ").strip()
        qm = re.match(r"Q:\s*(.+?)\s*\|\s*DEFAULT:\s*(.+)$", line)
        if qm:
            questions.append(ClarifyQuestion(question=qm.group(1).strip(), default=qm.group(2).strip()))
    return questions


def ask_clarifying_questions(ui: MaestroUI, questions: List[ClarifyQuestion]) -> List[tuple]:
    """Interactively ask each question, Enter accepts the suggested
    default. Returns (question, answer) pairs, folded into the mission
    text before it goes to enhance_mission.

    Each question gets its own panel plus an explicit "Got it" echo of
    what was captured before moving to the next one -- a bare input()
    loop gives no feedback that an answer actually registered, which is
    exactly the kind of silence that made the earlier shift-enter-mashing
    problem this session (see _drain_stdin) hard to notice was happening
    at all. A summary panel at the end gives one last checkpoint before
    the answers get folded into the mission text."""
    if not questions:
        return []
    _drain_stdin()  # clarify_mission just blocked on a network call -- see _drain_stdin
    ui.console.print("\n[bold cyan]A few quick questions before I write this up:[/bold cyan]\n")
    total = len(questions)
    answers = []
    for i, q in enumerate(questions, start=1):
        ui.print_panel(f"Question {i} of {total}", q.question, style="cyan")
        raw = input(f"  [{q.default}]: ").strip()
        answer = raw or q.default
        answers.append((q.question, answer))
        ui.console.print(f"  [green]✓ Got it:[/green] {answer}\n")

    summary = "\n".join(f"[bold]{q}[/bold]\n{a}" for q, a in answers)
    ui.print_panel("Your answers", summary, style="green")
    return answers


def intake_mission(ui: MaestroUI, client, cfg, raw_mission: str, skip_prompts: bool) -> MissionIntake:
    """Full mission intake: clarifying questions (skipped under --yes, or
    silently skipped if the mission is already clear — see
    mission_clarifier.md), then the refine pass. Answers are folded into
    the text handed to enhance_mission so the resulting brief incorporates
    them rather than just the original raw wording."""
    mission = raw_mission
    if not skip_prompts:
        questions = clarify_mission(ui, client, cfg, raw_mission)
        answers = ask_clarifying_questions(ui, questions)
        if answers:
            qa_block = "\n".join(f"- Q: {q} -> A: {a}" for q, a in answers)
            mission = f"{raw_mission}\n\nClarifying answers:\n{qa_block}"
    return enhance_mission(ui, client, cfg, mission)


def enhance_mission(ui: MaestroUI, client, cfg, mission: str) -> MissionIntake:
    """One-shot `claude -p` pass that expands the raw mission (plus any
    clarifying answers folded in by intake_mission) into a structured
    feature brief, extracts any constraints the user explicitly stated,
    and suggests a folder-name slug. Falls back to the raw mission with no
    constraints/slug on any error — this is a nicety, not something worth
    blocking the run over."""
    prompt_path = Path(cfg.prompts_dir) / "mission_enhancer.md"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    with ui.console.status("[cyan]Enhancing your prompt via Claude...[/cyan]", spinner="dots"):
        result = client.run(
            prompt=f"Raw mission:\n\n{mission}",
            allowed_tools="",
            max_turns=cfg.mission_enhancer_max_turns,
            permission_mode="acceptEdits",
            system_prompt=system_prompt,
            timeout=cfg.mission_intake_timeout,
        )
    if not result.ok:
        ui.console.print(
            f"[yellow]Prompt enhancing skipped ({result.error_message or 'agent error'}); "
            "using your original wording.[/yellow]"
        )
        return MissionIntake(mission=mission, slug=None)

    text = result.result_text
    m = re.search(r"CLEANED_MISSION:\s*\n(.*?)(?:\n+CONSTRAINTS:|\n+FOLDER_SLUG:|\Z)", text, re.S)
    cleaned = m.group(1).strip() if m else ""
    if not cleaned:
        ui.console.print("[yellow]Prompt enhancing skipped (no parseable output); using your original wording.[/yellow]")
        cleaned = mission

    constraints: List[str] = []
    cm = re.search(r"CONSTRAINTS:\s*\n(.*?)(?:\n+NO_COMMIT:|\n+FOLDER_SLUG:|\Z)", text, re.S)
    if cm:
        block = cm.group(1).strip()
        if block and block != "(none)":
            constraints = [
                line.strip().lstrip("- ").strip()
                for line in block.splitlines()
                if line.strip().startswith("-")
            ]

    no_commit = False
    ncm = re.search(r"NO_COMMIT:\s*(\w+)", text)
    if ncm:
        no_commit = ncm.group(1).strip().lower() == "yes"

    slug = None
    sm = re.search(r"FOLDER_SLUG:\s*\n?\s*([a-zA-Z0-9][a-zA-Z0-9-]*)", text)
    if sm:
        slug = re.sub(r"[^a-z0-9-]", "-", sm.group(1).strip().lower()).strip("-") or None

    return MissionIntake(mission=cleaned, slug=slug, constraints=constraints, no_commit=no_commit)


# -- project directory selection ---------------------------------------

# Files that mark a directory as Maestro's own source tree (as
# opposed to some project it's been pointed at). Checked so a run launched
# from inside Maestro's own checkout doesn't build a project into
# its source instead of a dedicated folder.
_SOURCE_MARKERS = (Path("maestro") / "main.py", Path("agents") / "base.py", Path("config.py"))


def is_maestro_source(path) -> bool:
    p = Path(path)
    return all((p / marker).exists() for marker in _SOURCE_MARKERS)


def default_project_slug(mission: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", mission.lower())[:6]
    slug = "-".join(words).strip("-")[:40].strip("-")
    return slug or "project"


def choose_project_dir(
    ui: MaestroUI, mission: str, args: argparse.Namespace, suggested_slug: Optional[str] = None
) -> str:
    """Pick (and create) the directory the mission will be built in. Never
    returns Maestro's own source directory. Defaults to a fresh,
    named subfolder (Claude's suggested slug, or a naive fallback) rather
    than dumping the project into whatever directory happened to be cwd."""

    slug = suggested_slug or default_project_slug(mission)

    def fallback_default() -> Path:
        base = Path.home() / "Desktop" if is_maestro_source(Path.cwd()) else Path.cwd()
        return base / slug

    if args.dir:
        candidate = Path(args.dir).expanduser().resolve()
    elif args.yes:
        candidate = fallback_default()
    else:
        default = fallback_default()
        ui.console.print(
            "\n[bold]Where should this project live?[/bold] "
            "(created automatically if it doesn't exist)"
        )
        raw = input(f"Path [{default}]: ").strip()
        candidate = Path(raw).expanduser().resolve() if raw else default

    while is_maestro_source(candidate):
        ui.print_error(
            f"{candidate} is Maestro's own source directory — refusing "
            "to build a project there, it would get mixed into this tool's code.\n\n"
            "Pick a different directory."
        )
        if args.yes or args.dir:
            sys.exit(1)
        default = Path.home() / "Desktop" / slug
        raw = input(f"Path [{default}]: ").strip()
        candidate = Path(raw).expanduser().resolve() if raw else default

    candidate.mkdir(parents=True, exist_ok=True)
    return str(candidate)


# -- pre-flight verification ------------------------------------------

_PATH_TOKEN_RE = re.compile(r"[./][\w\-./]+\.\w+|(?:^|\s)([\w\-]+/[\w\-./]+)")


def guess_mentioned_paths(mission: str) -> list:
    candidates = set()
    for tok in re.findall(r"[^\s,;:()\"']+", mission):
        if ("/" in tok or tok.startswith("./")) and not tok.startswith("http"):
            candidates.add(tok.strip("`'\""))
    return sorted(candidates)


def preflight(ui: MaestroUI, mission: str, cwd: str, skip_prompts: bool) -> bool:
    """Returns True if it's safe to proceed."""
    check = git_utils.check_repo(cwd=cwd)

    if not check.is_repo:
        ui.print_panel(
            "Not a git repository yet",
            f"{cwd}\n\nMaestro commits checkpoints as it works, so it "
            "needs one — running `git init` here.",
            style="yellow",
        )
        if skip_prompts:
            do_init = True
        else:
            do_init = input("Run `git init` now? [Y/n]: ").strip().lower() not in ("n", "no")
        if do_init:
            import subprocess

            subprocess.run(["git", "init"], cwd=cwd, check=True)
            check = git_utils.check_repo(cwd=cwd)
        else:
            return False

    if not check.clean:
        ui.print_panel(
            "Working tree not clean",
            f"Branch: {check.branch}\n\n{check.status_text}\n\n"
            "Maestro's checkpoint commits will get tangled up with "
            "these existing changes.",
            style="yellow",
        )
        if skip_prompts:
            git_utils.stash(cwd=cwd, message="Maestro: autostash before run")
            ui.print_panel("Stashed", "Existing changes stashed automatically (--yes).", style="yellow")
        else:
            choice = input("[s]tash them, [c]ommit them, [i]gnore, or [q]uit? ").strip().lower()
            if choice == "s":
                git_utils.stash(cwd=cwd, message="Maestro: autostash before run")
            elif choice == "c":
                git_utils.commit_all("Checkpoint before Maestro run", cwd=cwd)
            elif choice == "q":
                return False
            # "i" falls through and proceeds with a dirty tree

    mentioned = guess_mentioned_paths(mission)
    missing = [p for p in mentioned if not (Path(cwd) / p).exists()]
    if missing:
        ui.print_panel(
            "Heads up: paths mentioned in the mission that don't exist yet",
            "\n".join(f"- {p}" for p in missing)
            + "\n\nThis is just a heads-up (they may be new files the Coder "
            "should create) — not blocking.",
            style="yellow",
        )

    return True


# -- self-update ---------------------------------------------------------


def _find_source_repo() -> Optional[Path]:
    """Distinguishes an editable install (`pip install -e` / `pipx install
    --editable`) from a non-editable one. An editable install runs this
    very file straight out of a real git checkout, so a `.git` directory
    sits a couple of levels above it (package dir -> repo root); a
    non-editable pipx/pip install runs a *copy* of this file sitting
    inside a venv's site-packages, with no `.git` ancestor anywhere nearby.
    `maestro update` uses this to decide whether an in-place `git pull`
    does anything at all -- see main_update."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *list(here.parents)[:6]):
        if (candidate / ".git").exists():
            return candidate
    return None


def main_update(ui: MaestroUI) -> int:
    repo = _find_source_repo()
    if repo is not None:
        if not git_utils.is_clean(cwd=str(repo)):
            ui.print_error(
                f"{repo} (this install's source checkout) has uncommitted changes -- "
                f"commit or stash before updating:\n\n{git_utils.short_status(cwd=str(repo))}"
            )
            return 1
        ui.console.print(f"[cyan]Editable install detected at {repo} -- pulling origin/main...[/cyan]")
        if not git_utils.fetch(cwd=str(repo)):
            ui.print_error(f"git fetch failed in {repo}.")
            return 1
        ok, msg = git_utils.pull_ff_only(cwd=str(repo))
        if not ok:
            ui.print_error(f"Update failed (not a fast-forward, or another git error):\n\n{msg}")
            return 1
        sha = git_utils.head_sha(cwd=str(repo))[:8]
        ui.console.print(
            f"[green]Updated to {sha}.[/green] Editable install -- no reinstall needed, "
            "changes take effect immediately."
        )
        return 0

    if shutil.which("pipx"):
        proc = subprocess.run(["pipx", "list", "--short"], capture_output=True, text=True)
        installed = {line.split()[0] for line in proc.stdout.splitlines() if line.strip()}
        if "maestro" in installed:
            ui.console.print("[cyan]Non-editable pipx install detected -- running `pipx upgrade maestro`...[/cyan]")
            result = subprocess.run(["pipx", "upgrade", "maestro"])
            return result.returncode

    ui.print_error(
        "Could not detect how maestro is installed (not an editable git checkout, "
        "not a pipx-managed package). To update manually:\n\n"
        "  pipx upgrade maestro\n"
        "  # or\n"
        "  pip install --upgrade git+https://github.com/Adam-BH/maestro-cli.git"
    )
    return 1


# -- watchdog management ------------------------------------------------


def main_watch(args: argparse.Namespace) -> int:
    if args.watch_command == "add":
        target = str(Path(args.dir).expanduser().resolve())
        if scheduler.add_project(target):
            print(f"Watching {target} — the timer will auto-resume it if it ever pauses on a usage limit.")
            return 0
        if target in scheduler.load_watchlist():
            print(f"{target} is already registered.")
            return 0
        print(f"{target} has no STRATEGY.md yet — run `maestro run` there first, then register it.")
        return 1

    if args.watch_command == "remove":
        target = str(Path(args.dir).expanduser().resolve())
        if scheduler.remove_project(target):
            print(f"Stopped watching {target}.")
        else:
            print(f"{target} was not registered.")
        return 0

    if args.watch_command == "list":
        dirs = scheduler.load_watchlist()
        if not dirs:
            print("No projects registered. Add one with `maestro watch add <dir>`.")
            return 0
        for d in dirs:
            strategy_path = Path(d) / "STRATEGY.md"
            if strategy_path.exists():
                s = Strategy.load(str(strategy_path))
                print(f"{d}  [status={s.run_status}, resume_after={s.resume_after or '(none)'}]")
            else:
                print(f"{d}  [STRATEGY.md missing]")
        return 0

    if args.watch_command == "check":
        results = scheduler.check_all()
        if not results:
            print("No projects registered. Add one with `maestro watch add <dir>`.")
            return 0
        for r in results:
            print(f"{r.project_dir}: {r.action} — {r.detail}")
        return 0

    if args.watch_command == "install":
        print(scheduler.install_timer(interval_minutes=args.interval_minutes))
        return 0

    if args.watch_command == "uninstall":
        print(scheduler.uninstall_timer())
        return 0

    return 1  # argparse's required=True on the subparser makes this unreachable


# -- session detach/list/attach/stop -------------------------------------


def _build_detach_command(cfg, target_dir: str, push_on_done: bool = False) -> List[str]:
    """The detached child re-enters exactly the --resume path (main.py's
    existing crash-recovery machinery), just with --yes so it never blocks
    on stdin that no longer has anyone attached to it."""
    bin_cmd = scheduler._resolve_maestro_bin()
    cmd = bin_cmd.split() + ["run", "--resume", "--yes", "-C", target_dir]
    cmd += ["--model", cfg.model]
    cmd += ["--max-task-retries", str(cfg.max_task_retries)]
    cmd += ["--max-total-iterations", str(cfg.max_total_iterations)]
    if cfg.commit_every_attempt:
        cmd.append("--commit-every-attempt")
    if not cfg.bare:
        cmd.append("--no-bare")
    if cfg.deep_review:
        cmd.append("--deep-review")
    if push_on_done:
        cmd.append("--push-on-done")
    return cmd


def detach_run(
    ui: MaestroUI, cfg, target_dir: str, branch: Optional[str] = None, push_on_done: bool = False
) -> str:
    """Spawns `maestro run --resume --yes` for `target_dir` as a background
    process detached from this terminal (its own session via
    start_new_session=True, so it survives the terminal closing) and
    registers it in maestro/sessions.py so `maestro sessions
    list/attach/stop` can find it afterward. The mission's STRATEGY.md must
    already be saved to disk before this is called -- the child picks up
    from exactly that state via --resume, reusing that machinery instead of
    any new IPC or process-supervision model.

    `branch`/`push_on_done` are set by run_fanout() for a fanned-out
    session -- `branch` is purely informational (surfaced in `sessions
    list`; the child works out its own current branch from git directly,
    it isn't told), `push_on_done` is threaded through to the child's own
    argv so *it* pushes its own branch once its own run finishes (see
    main()'s end-of-run handling) rather than this (parent) process trying
    to push a branch a still-running background process is mid-commit on.
    Returns the new session id."""
    log_dir = sessions.CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    session_id = sessions.new_session_id(target_dir)
    log_path = log_dir / f"{session_id}.log"
    cmd = _build_detach_command(cfg, target_dir, push_on_done=push_on_done)

    with open(log_path, "w", encoding="utf-8") as log_f:
        log_f.write(f"# maestro session {session_id} detached at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        log_f.write(f"# command: {' '.join(cmd)}\n\n")
        log_f.flush()
        proc = subprocess.Popen(
            cmd, cwd=target_dir, stdout=log_f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )

    sessions.register(session_id, target_dir, proc.pid, str(log_path), branch=branch, push_on_done=push_on_done)
    ui.print_panel(
        "Detached",
        f"Session:  {session_id}\n"
        f"PID:      {proc.pid}\n"
        f"Project:  {target_dir}\n"
        + (f"Branch:   {branch}\n" if branch else "")
        + f"Log:      {log_path}\n\n"
        f"maestro sessions attach {session_id}   # tail live output (Ctrl-C just detaches)\n"
        f"maestro sessions stop {session_id}     # gracefully interrupt it\n"
        "maestro sessions list                  # see this and any other sessions",
        style="green",
    )
    return session_id


def run_fanout(ui: MaestroUI, cfg, strategy: Strategy, target_dir: str, n: int) -> int:
    """Splits an already-planned project's still-open tasks evenly across N
    git worktrees/branches and launches N detached sessions, one per
    branch (see git_utils.worktree_add/push and sessions.py's branch/
    push_on_done fields).

    Deliberately static: the split happens once, up front, rather than N
    processes claiming tasks from one shared STRATEGY.md at runtime --
    Loop holds strategy state in memory for an entire run with no locking
    (see loop.py's run()), so a shared live plan isn't safe today. Giving
    each branch its own worktree and its own self-contained STRATEGY.md
    copy sidesteps that entirely: every existing Loop/Strategy code path
    runs completely unchanged inside each worktree, unaware anything else
    is happening in parallel.

    Maestro has no task-dependency graph (see strategy.py's Task
    dataclass), so it cannot know which tasks are safe to split across
    branches -- that judgment call is on the caller.
    """
    if n < 2:
        ui.print_error("--fanout needs at least 2.")
        return 1
    if not strategy.tasks:
        ui.print_error(
            "--fanout requires an already-planned project (no tasks in STRATEGY.md yet "
            "-- run the Planner first, e.g. with --dry-run)."
        )
        return 1

    check = git_utils.check_repo(cwd=target_dir)
    if not check.is_repo:
        ui.print_error(f"{target_dir} is not a git repo -- --fanout needs git worktrees/branches to isolate each parallel session.")
        return 1
    if not check.clean:
        ui.print_error(f"{target_dir} has uncommitted changes -- commit or stash before fanning out:\n\n{check.status_text}")
        return 1

    workable = [t for t in strategy.tasks if t.status in ("pending", "rejected", "pending_review")]
    if len(workable) < n:
        ui.print_error(f"Only {len(workable)} workable task(s) but --fanout {n} was requested -- reduce N or plan more tasks first.")
        return 1

    groups = [workable[i::n] for i in range(n)]

    git_utils.fetch(cwd=target_dir)  # branch off an up-to-date main, not a stale local one
    base_branch = git_utils.current_branch(cwd=target_dir)
    slug = Path(target_dir).name

    rows = []
    for i, group in enumerate(groups, start=1):
        branch = f"maestro/{slug}-{i}"
        worktree_path = f"{target_dir}-fanout-{i}"
        ok, msg = git_utils.worktree_add(worktree_path, branch, base=base_branch, cwd=target_dir)
        if not ok:
            ui.print_error(f"Failed to create worktree for branch {branch}:\n\n{msg}")
            return 1

        sub = Strategy(
            mission=strategy.mission,
            run_status="in_progress",
            constraints=strategy.constraints,
            stack=strategy.stack,
            no_commit=strategy.no_commit,
            tasks=group,
        )
        sub.add_log("system", f"--fanout: assigned {len(group)} task(s) on branch {branch}.")
        sub.save(str(Path(worktree_path) / cfg.strategy_path))

        session_id = detach_run(ui, cfg, worktree_path, branch=branch, push_on_done=True)
        rows.append((branch, worktree_path, session_id, ", ".join(t.id for t in group)))

    table = Table(title=f"Fanned out across {n} branches", show_header=True, header_style="bold cyan", box=box.ROUNDED)
    table.add_column("Branch")
    table.add_column("Worktree")
    table.add_column("Session")
    table.add_column("Tasks")
    for branch, worktree_path, session_id, task_ids in rows:
        table.add_row(branch, worktree_path, session_id, task_ids)
    ui.console.print(table)
    ui.console.print(
        "\n[dim]Each session pushes its branch automatically once it finishes. "
        "Track them with `maestro sessions list`; merge/PR them yourself once ready -- "
        "Maestro won't reconcile branches for you.[/dim]"
    )
    return 0


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LOG_AGENT_RE = re.compile(r"^\s*(\S+)\s+│")  # matches log()'s "{agent:>9} │ " prefix


def attach_session(session_id: str, info, log_path: Path) -> int:
    """Rebuilds the same header + live task-checklist a foreground `maestro
    run` shows (see ui/console.py's MaestroUI.live/_render), rather than a
    bare tail of the log file.

    A detached run's own stdout is a plain file, not a tty (see
    detach_run's stdout=log_f) -- MaestroUI checks isatty() and never
    builds a Live region for it, so the log only ever contains flat log()
    lines. Tailing that file with a plain print(), as this used to do,
    faithfully reproduced only that flat stream and nothing else, which is
    why attach looked like a stripped-down version of the real shell: no
    header panel, no live task checklist, just scrolling text.

    This attaches its own MaestroUI to *this* terminal (which is a real
    tty), so it gets the genuine Live region back, and drives its two
    inputs independently: the task checklist from polling STRATEGY.md in
    the target project (the loop's actual source of truth -- see
    sessions.py's module docstring) and the activity feed from tailing the
    log file, same as before. Each tailed line already carries real ANSI
    color codes (detach_run's Console forces force_terminal=True for
    exactly this reason) -- Text.from_ansi decodes those back into a Rich
    Text with correct cell-width accounting, and printing through
    ui.live_console() (not a bare print()) is what keeps that interleaving
    with the Live redraw from corrupting it.
    """
    ui = MaestroUI()
    strategy_path = Path(info.project_dir) / "STRATEGY.md"

    def reload_strategy() -> None:
        if strategy_path.exists():
            try:
                ui.attach_strategy(Strategy.load(str(strategy_path)))
            except (OSError, ValueError):
                pass

    reload_strategy()
    ui.console.print(
        f"Attached to {session_id} (pid {info.pid}) -- Ctrl-C detaches without stopping it.\n",
        markup=False,
    )

    last_agent = None

    with ui.live():
        target = ui.live_console()

        def emit(raw_line: str) -> None:
            nonlocal last_agent
            m = _LOG_AGENT_RE.match(_ANSI_RE.sub("", raw_line))
            if m:
                agent = m.group(1).strip().lower()
                if agent != last_agent:
                    ui.set_agent(agent, "running")
                    last_agent = agent
            target.print(Text.from_ansi(raw_line), end="")

        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                emit(line)
            last_reload = time.monotonic()
            try:
                while True:
                    line = f.readline()
                    if line:
                        emit(line)
                        continue
                    now = time.monotonic()
                    if now - last_reload > 1.0:
                        reload_strategy()
                        ui.refresh()
                        last_reload = now
                    current = sessions.get_session(session_id)
                    if current is None or not current.alive:
                        target.print(f"\n[session {session_id} has exited]", markup=False)
                        return 0
                    time.sleep(0.3)
            except KeyboardInterrupt:
                target.print(
                    f"\nDetached. Session {session_id} keeps running in the background.", markup=False
                )
                return 0


def main_sessions(args: argparse.Namespace) -> int:
    if args.sessions_command == "list":
        infos = sessions.list_sessions()
        if not infos:
            print("No detached sessions. Start one with `maestro run --detach`.")
            return 0
        for info in infos:
            state = "running" if info.alive else "exited"
            done, total = info.progress
            print(
                f"{info.id}  [{state}]  status={info.run_status}  tasks={done}/{total}  "
                f"dir={info.project_dir}  started={info.started_at}"
            )
        return 0

    if args.sessions_command == "attach":
        info = sessions.get_session(args.id)
        if info is None:
            print(f"No session {args.id!r} registered. See `maestro sessions list`.")
            return 1
        log_path = Path(info.log_path)
        if not log_path.exists():
            print(f"No log file yet at {log_path} -- the session may still be starting.")
            return 1
        return attach_session(args.id, info, log_path)

    if args.sessions_command == "stop":
        print(sessions.stop_session(args.id))
        return 0

    if args.sessions_command == "remove":
        info = sessions.get_session(args.id)
        if info is None:
            print(f"No session {args.id!r} registered. See `maestro sessions list`.")
            return 1
        if info.alive:
            print(f"Session {args.id} (pid {info.pid}) is still running -- `maestro sessions stop {args.id}` it first.")
            return 1
        sessions.remove(args.id)
        print(f"Removed {args.id} from the session registry (log kept at {info.log_path}).")
        return 0

    if args.sessions_command == "clean":
        infos = sessions.list_sessions()
        finished = [i for i in infos if not i.alive]
        for info in finished:
            sessions.remove(info.id)
        if not finished:
            print("Nothing to clean -- no finished sessions registered.")
        else:
            print(f"Removed {len(finished)} finished session(s): {', '.join(i.id for i in finished)}")
        return 0

    if args.sessions_command == "push":
        info = sessions.get_session(args.id)
        if info is None:
            print(f"No session {args.id!r} registered. See `maestro sessions list`.")
            return 1
        branch = info.branch or git_utils.current_branch(cwd=info.project_dir)
        ok, msg = git_utils.push(branch, cwd=info.project_dir)
        if ok:
            print(f"Pushed {branch} to origin.")
            return 0
        print(f"Push failed for {branch}:\n\n{msg}")
        return 1

    return 1  # argparse's required=True on the subparser makes this unreachable


# -- end-of-run reporting -------------------------------------------------

_USAGE_HEADING_KEYWORDS = (
    "usage", "getting started", "quick start", "quickstart",
    "running", "run the", "how to run", "installation",
)


def _extract_usage_section(text: str) -> Optional[str]:
    """Pulls a "Getting started"/"Usage"/"Run"-style section out of a
    README rather than dumping the whole file — most READMEs bury run
    instructions under one heading among several (features, license, ...)."""
    headings = list(re.finditer(r"^(#{1,3})\s*(.+)$", text, re.M))
    for i, h in enumerate(headings):
        title = h.group(2).strip().lower()
        if not any(k in title for k in _USAGE_HEADING_KEYWORDS):
            continue
        level = len(h.group(1))
        end = len(text)
        for nxt in headings[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        return text[h.start():end].strip()
    return None


def print_how_to_run(ui: MaestroUI, project_dir: str) -> None:
    """Printed once, after a successful ('done') run — the "how do I
    actually use the thing that just got built" answer, so a finished run
    doesn't just end at a task checklist."""
    p = Path(project_dir)
    readme = next((p / n for n in ("README.md", "Readme.md", "readme.md") if (p / n).exists()), None)
    if readme is not None:
        section = _extract_usage_section(readme.read_text(encoding="utf-8")) or readme.read_text(encoding="utf-8")
        ui.print_panel(f"How to run this app (from {readme.name})", section[:3000], style="green")
        return

    lines = [f"cd {project_dir}"]
    pkg_path = p / "package.json"
    if pkg_path.exists():
        try:
            scripts = json.loads(pkg_path.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        lines.append("npm install")
        if "start" in scripts:
            lines.append("npm start")
        if "test" in scripts:
            lines.append("npm test    # run the test suite")
    elif (p / "requirements.txt").exists() or (p / "pyproject.toml").exists():
        lines.append("python3 -m venv .venv && source .venv/bin/activate")
        lines.append("pip install -r requirements.txt" if (p / "requirements.txt").exists() else "pip install -e .")
        entry = next((f for f in ("main.py", "app.py", "manage.py") if (p / f).exists()), None)
        if entry:
            lines.append(f"python {entry}")
    else:
        lines = [f"No README or recognizable package manifest found in {project_dir} to infer run instructions from."]

    ui.print_panel("How to run this app", "\n".join(lines), style="green")


def _handle_blocked_end(ui: MaestroUI, loop: Loop) -> str:
    """Called when an interactive run ends `blocked` — unattended mode
    parked one or more needs_human tasks and there was nothing else left to
    work on. Previously the process just exited here, which reads as the
    session having died rather than paused. Ask right here whether the
    human has guidance to unblock it, and if so keep going immediately in
    this same process instead of requiring a separate --resume invocation.
    Skipped entirely for non-interactive runs (--yes, piped, or the
    watchdog's own `run --resume --yes`) — see call site — since input()
    would just hang forever there."""
    while True:
        blocked = loop.strategy.blocked_tasks()
        if not blocked:
            return loop.strategy.run_status

        ui.print_panel(
            f"Blocked — {len(blocked)} task(s) need human input",
            "\n\n".join(f"{t.id}: {t.title}\n{t.notes}" for t in blocked),
            style="bold red",
        )
        guidance = input(
            "\nType guidance to unblock these and keep going now, "
            "or press Enter to leave the run parked and exit: "
        ).strip()
        if not guidance:
            return loop.strategy.run_status

        for t in blocked:
            t.status, t.attempts, t.review_attempts, t.notes = "pending", 0, 0, f"Human guidance: {guidance}"
        loop.strategy.add_log("human", f"Guidance for {len(blocked)} blocked task(s): {guidance}")
        loop.strategy.run_status = "in_progress"
        loop.save()

        with ui.live():
            loop.run(
                plan_only=False,
                on_needs_human=lambda tid, reason: _handle_pause(ui, tid, reason),
                unattended=True,
            )


def ensure_authenticated(ui: MaestroUI, client) -> bool:
    """One-time proactive check, run right after ClaudeClient() is
    constructed and before any mission-intake work starts.
    ClaudeCLINotFound (raised by the constructor itself) only catches a
    missing `claude` binary -- a present-but-logged-out CLI fails
    silently instead: clarify_mission/enhance_mission both tolerate any
    call failure as a nicety-skip, so a login problem would otherwise
    only surface once real Planner/Coder calls start burning task-retry
    budget deep into the run. One cheap probe call up front catches it
    immediately instead. This is the very first thing main() does -- before
    even the banner -- so without a spinner here the live network round
    trip it makes reads as the CLI hanging for a second or two with no
    output at all (same silent-network-call issue clarify_mission/
    enhance_mission already solve with their own console.status())."""
    with ui.console.status("[cyan]Checking Claude Code login...[/cyan]", spinner="dots"):
        result = client.probe_auth()
    if result.ok:
        return True
    if not is_auth_error(result.error_message or result.result_text):
        return True  # some other transient hiccup -- not worth blocking the whole run over
    ui.print_error(
        "Claude Code doesn't look logged in:\n\n"
        f"{result.error_message or result.result_text}\n\n"
        "Fix it, then re-run maestro:\n\n"
        "  claude /login   (from inside an interactive `claude` session)\n"
        "  # or\n"
        "  claude login    (from the shell)"
    )
    return False


# -- main --------------------------------------------------------------

def main(argv=None) -> int:
    args = parse_args(argv)

    if args.command == "update":
        return main_update(MaestroUI())

    if args.command == "watch":
        return main_watch(args)

    if args.command == "sessions":
        return main_sessions(args)

    cfg = build_config(args)
    ui = MaestroUI()

    try:
        from maestro.claude_client import ClaudeClient

        client = ClaudeClient(model=cfg.model, bare=cfg.bare)
    except ClaudeCLINotFound as exc:
        ui.print_error(str(exc))
        return 1

    if not ensure_authenticated(ui, client):
        return 1

    # cost_usd from `claude -p` is only a real charge under --bare/API-key
    # billing; under OAuth/subscription auth it's an estimate with no
    # actual cost behind it, so hide it there rather than show noise.
    ui.show_cost = client.bare

    if cfg.bare and not client.bare:
        ui.print_panel(
            "Auth notice",
            "--bare mode requires ANTHROPIC_API_KEY (OAuth/keychain auth isn't "
            "readable in bare mode) and no API key is set, so Maestro "
            "is running agents WITHOUT --bare — they'll pick up your normal "
            "Claude Code auth, hooks, and CLAUDE.md.",
            style="yellow",
        )

    if args.resume:
        target_dir = str(Path(args.dir).expanduser().resolve()) if args.dir else str(Path.cwd())
        os.chdir(target_dir)
        if not Path(cfg.strategy_path).exists():
            ui.print_error(f"--resume was passed but {cfg.strategy_path} does not exist in {target_dir}.")
            return 1
        strategy = Strategy.load(cfg.strategy_path)
        ui.console.print(f"[cyan]Resumed strategy from {target_dir}/{cfg.strategy_path}[/cyan] "
                          f"({strategy.progress()[0]}/{strategy.progress()[1]} tasks done, "
                          f"iteration {strategy.iteration}, status={strategy.run_status}).")

        # status == "in_progress" only means something *while* a Loop is
        # actively driving that task. On --resume that Loop is gone by
        # definition (a crash, Ctrl-C, or a kill), so any task still marked
        # in_progress was interrupted before it reached a checkpointed state
        # (pending_review, rejected, or done) — reset it so it isn't
        # silently skipped forever by next_pending_task().
        stuck = [t for t in strategy.tasks if t.status == "in_progress"]
        for t in stuck:
            t.status = "pending"
        if stuck:
            strategy.add_log(
                "system",
                f"--resume: {len(stuck)} task(s) were stuck in_progress from an "
                f"interrupted run ({', '.join(t.id for t in stuck)}); reset to pending.",
            )
            ui.console.print(f"[cyan]Reset {len(stuck)} interrupted task(s) back to pending.[/cyan]")

        if args.retry_blocked:
            blocked = strategy.blocked_tasks()
            for t in blocked:
                t.status, t.attempts, t.notes = "pending", 0, ""
            if blocked:
                strategy.add_log("system", f"--retry-blocked: reset {len(blocked)} needs_human task(s) to pending.")
                ui.console.print(f"[cyan]Reset {len(blocked)} blocked task(s) for retry.[/cyan]")
            strategy.resume_after = ""
    else:
        ui.print_banner()
        raw_mission = prompt_mission(ui)
        if not raw_mission:
            ui.print_error("Empty mission, nothing to do.")
            return 1

        intake = intake_mission(ui, client, cfg, raw_mission, skip_prompts=args.yes)
        _drain_stdin()  # enhance_mission just blocked on a network call too -- see _drain_stdin
        mission, slug = intake.mission, intake.slug
        ui.print_mission(mission)

        target_dir = choose_project_dir(ui, mission, args, suggested_slug=slug)

        while not args.yes:
            ui.console.print(f"[bold]Folder:[/bold] {target_dir}\n")
            choice = input("Start here? [Y]es / [e]dit mission / [f]older / [q]uit: ").strip().lower()
            if choice in ("", "y", "yes"):
                break
            if choice in ("q", "quit"):
                ui.console.print("Aborted.")
                return 0
            if choice in ("e", "edit"):
                raw_mission = prompt_mission(ui)
                if not raw_mission:
                    ui.print_error("Empty mission, nothing to do.")
                    return 1
                intake = intake_mission(ui, client, cfg, raw_mission, skip_prompts=False)
                _drain_stdin()
                mission, slug = intake.mission, intake.slug
                ui.print_mission(mission)
                continue
            if choice in ("f", "folder"):
                args.dir = None  # force an interactive re-prompt even if --dir was passed
                target_dir = choose_project_dir(ui, mission, args, suggested_slug=slug)
                continue

        os.chdir(target_dir)
        ui.console.print(f"[cyan]Building in: {target_dir}[/cyan]")

        if not preflight(ui, mission, target_dir, skip_prompts=args.yes):
            ui.console.print("Pre-flight checks failed or were declined. Aborting.")
            return 1

        strategy = Strategy(
            mission=mission,
            run_status="planning",
            constraints=intake.constraints,
            no_commit=intake.no_commit,
        )
        strategy.add_log("system", "Mission received, enhanced via Claude; starting Planner.")
        strategy.save(cfg.strategy_path)

    if strategy.no_commit:
        ui.print_panel(
            "No-commit mode active",
            "This run's mission specifies not to commit changes, so Maestro "
            "will not create checkpoint or approval commits, and the Coder/Tester agents "
            "have been told not to commit either.\n\n"
            "This means --resume has no commit history to roll back to for this run — a "
            "crash mid-task loses whatever work wasn't yet Reviewer-approved.",
            style="yellow",
        )

    if args.fanout:
        return run_fanout(ui, cfg, strategy, target_dir, args.fanout)

    if args.detach:
        detach_run(ui, cfg, target_dir)
        return 0

    loop = Loop(cfg, strategy, ui, target_dir, client=client)
    unattended = not args.pause_on_human

    try:
        with ui.live():
            if args.plan_only:
                loop.run(plan_only=True)
            else:
                loop.run(
                    plan_only=False,
                    on_needs_human=lambda tid, reason: _handle_pause(ui, tid, reason),
                    unattended=unattended,
                )
    except KeyboardInterrupt:
        ui.console.print("\n[yellow]Interrupted. Progress is saved in STRATEGY.md — re-run with --resume.[/yellow]")
        loop.save()
        return 130

    if args.plan_only:
        ui.console.print("\n[bold]Plan-only run finished.[/bold] See STRATEGY.md for the full plan.")
        for t in loop.strategy.tasks:
            ui.console.print(f"  {t.id}: {t.title}")
        return 0

    if loop.strategy.run_status == "blocked" and not args.yes and sys.stdin.isatty():
        _handle_blocked_end(ui, loop)

    ui.print_final_summary(loop.strategy, ui.total_cost, ui.total_turns, loop.commits)

    status = loop.strategy.run_status
    if status == "done":
        if args.push_on_done:
            branch = git_utils.current_branch(cwd=target_dir)
            ok, msg = git_utils.push(branch, cwd=target_dir)
            if ok:
                ui.console.print(f"[green]Pushed {branch} to origin.[/green]")
            else:
                ui.console.print(f"[yellow]Push failed for {branch}:[/yellow] {msg}")
        print_how_to_run(ui, target_dir)
        return 0
    if status == "rate_limited":
        return 3
    return 2


def _handle_pause(ui: MaestroUI, task_id: str, reason: str) -> bool:
    """Interactive NEEDS_HUMAN handler used by main(). Returns True to
    retry the paused task, False to stop the run."""
    ui.stop_live()
    try:
        choice = input(
            f"\nTask {task_id} needs human input:\n{reason}\n\n"
            "Fix whatever's needed in the repo, then choose: "
            "[r]etry this task / [q]uit and keep progress: "
        ).strip().lower()
    finally:
        ui.resume_live()
    return choice == "r"


if __name__ == "__main__":
    sys.exit(main())
