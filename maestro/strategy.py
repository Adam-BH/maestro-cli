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

TASK_STATUSES = ("pending", "in_progress", "pending_review", "done", "rejected", "needs_human")
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
    # Which specialized agent implements this task: "coder" | "researcher"
    # | "tester". Defaults to "coder" so STRATEGY.md files written before
    # this field existed still parse and run exactly as before.
    agent: str = "coder"
    # How many times the Reviewer step itself has failed to reach a verdict
    # (invocation error / unparseable output) for the *current* producer
    # attempt. Tracked separately from `attempts` so a flaky reviewer call
    # doesn't burn the Coder's retry budget or trigger a needless recode —
    # see Loop._retry_review_or_escalate.
    review_attempts: int = 0
    # The producer's summary of its most recent attempt, kept around so a
    # status == "pending_review" task can go straight back to the Reviewer
    # (on the next loop iteration, or after --resume) without re-running the
    # producer. Cleared once the task leaves pending_review.
    last_producer_summary: str = ""

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
    # Explicit dos-and-don'ts the user stated in the raw mission, extracted
    # by the prompt-enhancer pass at intake. Fed into every agent's prompt
    # so constraints are respected for the whole run, not just at intake.
    constraints: List[str] = field(default_factory=list)
    # The Planner's tech-stack decision (language/runtime, framework, data
    # layer, key libraries, rationale) as a rendered bullet block — set once
    # by the first successful plan_step() and preserved across plan
    # revisions so every subsequent agent builds on the same choice instead
    # of each one improvising its own. Empty until the Planner has run.
    stack: str = ""
    # Set when the mission explicitly says not to commit. Disables both the
    # Maestro's own checkpoint commits and the Coder/Tester's own
    # commit step for the whole run — see Loop.run_task_cycle.
    no_commit: bool = False
    tasks: List[Task] = field(default_factory=list)
    log: List[LogEntry] = field(default_factory=list)

    # -- convenience -------------------------------------------------

    def next_pending_task(self) -> Optional[Task]:
        for t in self.tasks:
            if t.status in ("pending", "rejected", "pending_review"):
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

        lines += ["## Constraints", ""]
        if self.constraints:
            lines += [f"- {c}" for c in self.constraints]
        else:
            lines.append("(none)")
        lines.append("")

        lines += ["## Stack", ""]
        lines.append(self.stack.strip() if self.stack.strip() else "(not decided yet — Planner has not run)")
        lines.append("")

        lines += [
            "## Meta",
            "",
            f"- Status: {self.run_status}",
            f"- Iteration: {self.iteration}",
            f"- Created: {self.created}",
            f"- Updated: {self.updated}",
            f"- Resume After: {self.resume_after or '(none)'}",
            f"- No Commit: {'yes' if self.no_commit else 'no'}",
            "",
        ]

        lines += ["## Plan", ""]
        if not self.tasks:
            lines.append("(no tasks yet — Planner has not run)")
        for t in self.tasks:
            lines.append(f"### Task {t.id}: {t.title}")
            lines.append(f"- status: {t.status}")
            lines.append(f"- attempts: {t.attempts}")
            lines.append(f"- review_attempts: {t.review_attempts}")
            lines.append(f"- agent: {t.agent}")
            if t.acceptance_criteria:
                lines.append("- acceptance_criteria:")
                for c in t.acceptance_criteria:
                    lines.append(f"  - {c}")
            else:
                lines.append("- acceptance_criteria: (none specified)")
            if t.notes:
                lines.append(f"- notes: {t.notes}")
            if t.last_producer_summary:
                lines.append(f"- last_producer_summary: {t.last_producer_summary}")
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

        # Absent on STRATEGY.md files written before constraints existed —
        # the regex simply won't match, leaving the dataclass default ([]).
        constraints_match = re.search(r"^## Constraints\s*\n+(.*?)(?=\n## )", text, re.S | re.M)
        if constraints_match:
            block = constraints_match.group(1).strip()
            if block and block != "(none)":
                strategy.constraints = [
                    line.strip().lstrip("- ").strip()
                    for line in block.splitlines()
                    if line.strip().startswith("-")
                ]

        stack_match = re.search(r"^## Stack\s*\n+(.*?)(?=\n## )", text, re.S | re.M)
        if stack_match:
            block = stack_match.group(1).strip()
            if block and block != "(not decided yet — Planner has not run)":
                strategy.stack = block

        meta_match = re.search(r"^## Meta\s*\n+(.*?)(?=\n## )", text, re.S | re.M)
        if meta_match:
            meta_block = meta_match.group(1)
            for key, attr, caster in [
                ("Status", "run_status", str),
                ("Iteration", "iteration", int),
                ("Created", "created", str),
                ("Updated", "updated", str),
                ("Resume After", "resume_after", lambda v: "" if v == "(none)" else v),
                ("No Commit", "no_commit", lambda v: v.strip().lower() in ("yes", "true", "1")),
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

                review_attempts_m = re.search(r"^- review_attempts: (\d+)$", body, re.M)
                if review_attempts_m:
                    task.review_attempts = int(review_attempts_m.group(1))

                agent_m = re.search(r"^- agent: (.+)$", body, re.M)
                if agent_m:
                    task.agent = agent_m.group(1).strip()

                notes_m = re.search(r"^- notes: (.+)$", body, re.M)
                if notes_m:
                    task.notes = notes_m.group(1).strip()

                summary_m = re.search(r"^- last_producer_summary: (.+)$", body, re.M)
                if summary_m:
                    task.last_producer_summary = summary_m.group(1).strip()

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
