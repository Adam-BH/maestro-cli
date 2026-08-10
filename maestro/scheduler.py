"""
The watchdog: a small piece of state + a check() that a system timer calls
periodically so a run paused on a usage/rate limit (see Loop._pause_for_rate_limit
in loop.py) resumes itself once that limit has cleared, with nobody watching
a terminal.

This deliberately does NOT run as a long-lived daemon process. `maestro
watch check` is a one-shot: look at every registered project's STRATEGY.md,
and if it's paused on a limit whose resume time has passed, kick off
`maestro run --resume --yes` for it. A system timer (systemd --user on
Linux; see install_timer()) calls that one-shot every N minutes. That way
the "scheduler" survives reboots and terminal closures for free — it's
just systemd — instead of Maestro having to reimplement its own persistent
process supervision.

Resuming is exactly where the run left off because --resume already does
that: STRATEGY.md is the loop's entire memory (current task, its status,
the producer's last summary if it was mid-review, approved-task commits),
and Loop._pause_for_rate_limit() pauses *before* burning a task attempt on
the call that got rate-limited (see run_task_cycle's OUTCOME_RATE_LIMITED
handling: `task.attempts -= 1`) — so resuming re-issues that same attempt
rather than skipping or duplicating work.

Only run_status == "rate_limited" is handled here. A `blocked` run (tasks
parked on needs_human) needs an actual human judgment call, not a timer —
see main.py's end-of-run prompt for that instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from maestro.claude_client import parse_resume_hint
from maestro.strategy import Strategy

CONFIG_DIR = Path.home() / ".config" / "maestro"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"
STATE_PATH = CONFIG_DIR / "watchdog-state.json"
LOG_DIR = CONFIG_DIR / "logs"

# How often to retry a rate_limited run when its resume_after hint has no
# parseable timestamp (e.g. "unknown — try again later"). Long enough not
# to hammer the CLI on every 15-minute tick, short enough that a limit
# that clears mid-day doesn't sit idle till the next fixed checkpoint.
UNKNOWN_HINT_RETRY_INTERVAL = timedelta(hours=6)

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE_NAME = "maestro-watchdog.service"
TIMER_NAME = "maestro-watchdog.timer"


# -- watchlist ---------------------------------------------------------


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_watchlist() -> List[str]:
    return _load_json(WATCHLIST_PATH, [])


def save_watchlist(dirs: List[str]) -> None:
    _save_json(WATCHLIST_PATH, dirs)


def add_project(path: str) -> bool:
    """Returns False if the path isn't a Maestro project (no STRATEGY.md
    yet — nothing for the watchdog to read) or is already registered."""
    resolved = str(Path(path).expanduser().resolve())
    if not (Path(resolved) / "STRATEGY.md").exists():
        return False
    dirs = load_watchlist()
    if resolved in dirs:
        return False
    dirs.append(resolved)
    save_watchlist(dirs)
    return True


def remove_project(path: str) -> bool:
    resolved = str(Path(path).expanduser().resolve())
    dirs = load_watchlist()
    if resolved not in dirs:
        return False
    dirs.remove(resolved)
    save_watchlist(dirs)
    return True


# -- per-project retry state --------------------------------------------


def _load_state() -> dict:
    return _load_json(STATE_PATH, {})


def _save_state(state: dict) -> None:
    _save_json(STATE_PATH, state)


# -- checking one project ------------------------------------------------


@dataclass
class CheckResult:
    project_dir: str
    action: str  # "skipped" | "waiting" | "resumed"
    detail: str


def _resolve_maestro_bin() -> str:
    found = shutil.which("maestro")
    if found:
        return found
    # Fallback for a from-source checkout that was never pipx-installed:
    # run the same interpreter currently executing this module.
    return f"{sys.executable} -m maestro.main"


def check_project(project_dir: str, maestro_bin: Optional[str] = None) -> CheckResult:
    strategy_path = Path(project_dir) / "STRATEGY.md"
    if not strategy_path.exists():
        return CheckResult(project_dir, "skipped", "no STRATEGY.md found")

    strategy = Strategy.load(str(strategy_path))
    if strategy.run_status != "rate_limited":
        return CheckResult(project_dir, "skipped", f"status is {strategy.run_status!r}, nothing to resume")

    now = datetime.now(timezone.utc)
    state = _load_state()
    project_state = state.get(project_dir, {})
    resume_dt = parse_resume_hint(strategy.resume_after)

    if resume_dt is not None:
        if now < resume_dt:
            return CheckResult(project_dir, "waiting", f"resume hint says {resume_dt.isoformat()} — not yet")
    else:
        last_attempt_raw = project_state.get("last_attempt")
        if last_attempt_raw:
            last_attempt = datetime.fromisoformat(last_attempt_raw)
            if now - last_attempt < UNKNOWN_HINT_RETRY_INTERVAL:
                next_try = last_attempt + UNKNOWN_HINT_RETRY_INTERVAL
                return CheckResult(
                    project_dir, "waiting",
                    f"resume hint unparseable ({strategy.resume_after!r}); "
                    f"next retry at {next_try.isoformat()}",
                )

    bin_cmd = maestro_bin or _resolve_maestro_bin()
    cmd = bin_cmd.split() + ["run", "--resume", "--yes", "-C", project_dir]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    slug = Path(project_dir).name
    log_path = LOG_DIR / f"{slug}-{now.strftime('%Y%m%dT%H%M%SZ')}.log"

    state[project_dir] = {"last_attempt": now.isoformat()}
    _save_state(state)

    with open(log_path, "w", encoding="utf-8") as log_f:
        log_f.write(f"# maestro watchdog resuming {project_dir} at {now.isoformat()}\n")
        log_f.write(f"# command: {' '.join(cmd)}\n\n")
        log_f.flush()
        proc = subprocess.run(cmd, stdout=log_f, stderr=subprocess.STDOUT)

    return CheckResult(
        project_dir, "resumed",
        f"ran `{' '.join(cmd)}` (exit {proc.returncode}) — log: {log_path}",
    )


def check_all(maestro_bin: Optional[str] = None) -> List[CheckResult]:
    return [check_project(d, maestro_bin=maestro_bin) for d in load_watchlist()]


# -- systemd --user timer install/uninstall -----------------------------


def _unit_files(interval_minutes: int, maestro_bin: str) -> dict:
    service = f"""[Unit]
Description=Maestro watchdog — resume any rate_limited runs that are due

[Service]
Type=oneshot
ExecStart={maestro_bin} watch check
"""
    timer = f"""[Unit]
Description=Run the Maestro watchdog every {interval_minutes} minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec={interval_minutes}min
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {SERVICE_NAME: service, TIMER_NAME: timer}


def install_timer(interval_minutes: int = 15) -> str:
    """Writes and enables a systemd --user timer that runs `maestro watch
    check` every interval_minutes. Persistent=true means a tick missed
    while the machine was off/asleep fires shortly after the next boot
    instead of waiting for the next scheduled slot — the point being this
    should catch up on its own, not need to be re-armed by hand.

    Note: systemd --user units only run while you have an active login
    session unless lingering is enabled for this user
    (`loginctl enable-linger $USER`) — without that, the timer stops the
    moment you log out, which defeats "runs globally on the pc"."""
    # ExecStart= is word-split by systemd itself (no shell involved), so a
    # multi-word fallback like "python3 -m maestro.main" needs no special
    # quoting here — plain string interpolation is enough either way.
    maestro_bin = _resolve_maestro_bin()
    units = _unit_files(interval_minutes, maestro_bin)

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in units.items():
        (SYSTEMD_USER_DIR / name).write_text(content, encoding="utf-8")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", TIMER_NAME], check=True)

    return (
        f"Installed and started {TIMER_NAME} (checks every {interval_minutes}m).\n\n"
        "For this to keep running after you log out (not just while a "
        "graphical/terminal session is open), also run:\n\n"
        "    loginctl enable-linger $USER\n"
    )


def uninstall_timer() -> str:
    subprocess.run(["systemctl", "--user", "disable", "--now", TIMER_NAME], check=False)
    (SYSTEMD_USER_DIR / TIMER_NAME).unlink(missing_ok=True)
    (SYSTEMD_USER_DIR / SERVICE_NAME).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return f"Removed {TIMER_NAME} and {SERVICE_NAME}."
