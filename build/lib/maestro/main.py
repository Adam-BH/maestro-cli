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
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from config import DEFAULT_CONFIG
from maestro import git_utils
from maestro.claude_client import ClaudeCLINotFound
from maestro.loop import Loop
from maestro.strategy import Strategy
from maestro.ui.console import MaestroUI


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="maestro",
        description="Orchestrate Planner/Coder/Researcher/Tester/Reviewer Claude Code agents in a plan->build->review loop.",
    )
    subparsers = p.add_subparsers(dest="command", required=True)

    run_p = subparsers.add_parser(
        "run",
        help="Run a mission (plan -> build -> review loop) in the current or given directory.",
        description="Run a mission (plan -> build -> review loop) in the current or given directory.",
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
        "--pause-on-human", action="store_true",
        help="Stop and wait at a terminal prompt every time a task needs human input, "
        "like earlier versions did. Default is unattended: a blocked task is parked "
        "and the loop moves on to the next one, so a single bad task can't stall an "
        "overnight run.",
    )
    run_p.add_argument(
        "--retry-blocked", action="store_true",
        help="With --resume: reset any needs_human tasks back to pending (attempts=0) "
        "before continuing, so they get another shot after you've fixed whatever blocked them.",
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
    return cfg


# -- mission intake --------------------------------------------------

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


def enhance_mission(ui: MaestroUI, client, cfg, mission: str) -> MissionIntake:
    """One-shot `claude -p` pass that tidies typos/phrasing in the raw
    mission, extracts any constraints the user explicitly stated, and
    suggests a folder-name slug. Falls back to the raw mission with no
    constraints/slug on any error — this is a nicety, not something worth
    blocking the run over."""
    prompt_path = Path(cfg.prompts_dir) / "mission_enhancer.md"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    ui.console.print("[dim]Enhancing your prompt via Claude...[/dim]")
    result = client.run(
        prompt=f"Raw mission:\n\n{mission}",
        allowed_tools="",
        max_turns=cfg.mission_enhancer_max_turns,
        permission_mode="acceptEdits",
        system_prompt=system_prompt,
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


# -- main --------------------------------------------------------------

def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = build_config(args)
    ui = MaestroUI()

    try:
        from maestro.claude_client import ClaudeClient

        client = ClaudeClient(model=cfg.model, bare=cfg.bare)
    except ClaudeCLINotFound as exc:
        ui.print_error(str(exc))
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

        if args.retry_blocked:
            blocked = strategy.blocked_tasks()
            for t in blocked:
                t.status, t.attempts, t.notes = "pending", 0, ""
            if blocked:
                strategy.add_log("system", f"--retry-blocked: reset {len(blocked)} needs_human task(s) to pending.")
                ui.console.print(f"[cyan]Reset {len(blocked)} blocked task(s) for retry.[/cyan]")
            strategy.resume_after = ""
    else:
        raw_mission = prompt_mission(ui)
        if not raw_mission:
            ui.print_error("Empty mission, nothing to do.")
            return 1

        intake = enhance_mission(ui, client, cfg, raw_mission)
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
                intake = enhance_mission(ui, client, cfg, raw_mission)
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

    ui.print_final_summary(loop.strategy, ui.total_cost, ui.total_turns, loop.commits)

    status = loop.strategy.run_status
    if status == "done":
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
