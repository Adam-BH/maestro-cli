"""
Structured reader/writer for STRATEGY.md.

STRATEGY.md is the single source of truth for a run: the mission, the
current plan (tasks + acceptance criteria + status), iteration count, and a
decision log. Agents receive its rendered text as context and the loop
re-parses their output back into these dataclasses, so the file stays
both human-readable and machine-editable across the whole run.

The format is deliberately simple markdown with a few structural
conventions (see `render()` for the exact shape) rather than YAML
frontmatter or JSON, so a human can open it mid-run, read what happened,
and hand-edit a task if needed before resuming.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

TASK_STATUSES = ("pending", "in_progress", "done", "rejected", "needs_human")
RUN_STATUSES = (
    "planning",
    "in_progress",
    "paused_needs_human",  # only reachable with --pause-on-human
    "blocked",  # unattended run ran out of workable tasks; some are needs_human
    "rate_limited",  # hit a usage/rate limit; see resume_after
    "done",
    "failed",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Task:
    id: str
    title: str
    status: str = "pending"
    attempts: int = 0
    acceptance_criteria: List[str] = field(default_factory=list)
    notes: str = ""

    def is_done(self) -> bool:
        return self.status == "done"


@dataclass
class LogEntry:
    timestamp: str
    agent: str
    message: str

    def render(self) -> str:
        return f"- [{self.timestamp}] ({self.agent}) {self.message}"


@dataclass
class Strategy:
    mission: str = ""
    run_status: str = "planning"
    iteration: int = 0
    created: str = field(default_factory=_now_iso)
    updated: str = field(default_factory=_now_iso)
    # Best-effort "when to retry" hint, set when a run pauses because of a
    # usage/rate limit rather than a per-task problem. Empty otherwise.
    resume_after: str = ""
    tasks: List[Task] = field(default_factory=list)
    log: List[LogEntry] = field(default_factory=list)

    # -- convenience -------------------------------------------------

    def next_pending_task(self) -> Optional[Task]:
        for t in self.tasks:
            if t.status in ("pending", "rejected"):
                return t
        return None

    def all_done(self) -> bool:
        return bool(self.tasks) and all(t.is_done() for t in self.tasks)

    def blocked_tasks(self) -> List[Task]:
        return [t for t in self.tasks if t.status == "needs_human"]

    def add_log(self, agent: str, message: str) -> None:
        self.log.append(LogEntry(timestamp=_now_iso(), agent=agent, message=message))

    def touch(self) -> None:
        self.updated = _now_iso()

    def progress(self) -> tuple:
        done = sum(1 for t in self.tasks if t.is_done())
        return done, len(self.tasks)

    # -- rendering -----------------------------------------------------

    def render(self) -> str:
        lines = ["# STRATEGY", "", "## Mission", "", self.mission.strip() or "(none)", ""]

        lines += [
            "## Meta",
            "",
            f"- Status: {self.run_status}",
            f"- Iteration: {self.iteration}",
            f"- Created: {self.created}",
            f"- Updated: {self.updated}",
            f"- Resume After: {self.resume_after or '(none)'}",
            "",
        ]

        lines += ["## Plan", ""]
        if not self.tasks:
            lines.append("(no tasks yet — Planner has not run)")
        for t in self.tasks:
            lines.append(f"### Task {t.id}: {t.title}")
            lines.append(f"- status: {t.status}")
            lines.append(f"- attempts: {t.attempts}")
            if t.acceptance_criteria:
                lines.append("- acceptance_criteria:")
                for c in t.acceptance_criteria:
                    lines.append(f"  - {c}")
            else:
                lines.append("- acceptance_criteria: (none specified)")
            if t.notes:
                lines.append(f"- notes: {t.notes}")
            lines.append("")

        lines += ["## Decision Log", ""]
        if not self.log:
            lines.append("(empty)")
        else:
            for entry in self.log:
                lines.append(entry.render())
        lines.append("")

        return "\n".join(lines)

    def save(self, path: str) -> None:
        self.touch()
        Path(path).write_text(self.render(), encoding="utf-8")

    # -- parsing -------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Strategy":
        text = Path(path).read_text(encoding="utf-8")
        return cls.parse(text)

    @classmethod
    def parse(cls, text: str) -> "Strategy":
        strategy = cls()

        mission_match = re.search(r"^## Mission\s*\n+(.*?)(?=\n## )", text, re.S | re.M)
        if mission_match:
            mission = mission_match.group(1).strip()
            strategy.mission = "" if mission == "(none)" else mission

        meta_match = re.search(r"^## Meta\s*\n+(.*?)(?=\n## )", text, re.S | re.M)
        if meta_match:
            meta_block = meta_match.group(1)
            for key, attr, caster in [
                ("Status", "run_status", str),
                ("Iteration", "iteration", int),
                ("Created", "created", str),
                ("Updated", "updated", str),
                ("Resume After", "resume_after", lambda v: "" if v == "(none)" else v),
            ]:
                m = re.search(rf"^- {key}: (.+)$", meta_block, re.M)
                if m:
                    try:
                        setattr(strategy, attr, caster(m.group(1).strip()))
                    except ValueError:
                        pass

        plan_match = re.search(r"^## Plan\s*\n+(.*?)(?=\n## )", text, re.S | re.M)
        if plan_match:
            plan_block = plan_match.group(1)
            task_chunks = re.split(r"^### Task ", plan_block, flags=re.M)[1:]
            for chunk in task_chunks:
                header, _, body = chunk.partition("\n")
                task_id, _, title = header.partition(":")
                task = Task(id=task_id.strip(), title=title.strip())

                status_m = re.search(r"^- status: (.+)$", body, re.M)
                if status_m:
                    task.status = status_m.group(1).strip()

                attempts_m = re.search(r"^- attempts: (\d+)$", body, re.M)
                if attempts_m:
                    task.attempts = int(attempts_m.group(1))

                notes_m = re.search(r"^- notes: (.+)$", body, re.M)
                if notes_m:
                    task.notes = notes_m.group(1).strip()

                ac_m = re.search(
                    r"^- acceptance_criteria:\s*\n((?:^  - .+\n?)+)", body, re.M
                )
                if ac_m:
                    task.acceptance_criteria = [
                        line[4:].strip()
                        for line in ac_m.group(1).splitlines()
                        if line.strip().startswith("- ")
                    ]

                strategy.tasks.append(task)

        log_match = re.search(r"^## Decision Log\s*\n+(.*)", text, re.S | re.M)
        if log_match:
            log_block = log_match.group(1)
            for m in re.finditer(r"^- \[(.+?)\] \((.+?)\) (.*)$", log_block, re.M):
                strategy.log.append(
                    LogEntry(timestamp=m.group(1), agent=m.group(2), message=m.group(3))
                )

        return strategy
