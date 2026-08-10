"""Coder agent: implements one task from the plan and commits it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from agents.base import Agent, AgentContext
from maestro.strategy import Task


class Coder(Agent):
    name = "coder"
    color = "green"
    prompt_file = "coder.md"

    def build_prompt(self, context: AgentContext) -> str:
        task: Task = context.extra["task"]
        rejection_reason: Optional[str] = context.extra.get("rejection_reason")

        parts = [
            f"Implement this task:\n\n### {task.id}: {task.title}\n",
            "Acceptance criteria:",
        ]
        for c in task.acceptance_criteria:
            parts.append(f"- {c}")

        if rejection_reason:
            parts.append(
                f"\nThe previous attempt at this task did not succeed, for this "
                f"reason:\n\n{rejection_reason}\n\n"
                "If that describes a problem with the code, fix it. If it "
                "describes a tooling/invocation failure unrelated to your code, "
                "just retry the task normally."
            )

        if context.constraints:
            parts.append("\nHard constraints for this run (must not be violated):")
            for c in context.constraints:
                parts.append(f"- {c}")

        if context.extra.get("no_commit"):
            parts.append(
                "\nIMPORTANT: this run has a NO-COMMIT constraint. Do NOT run `git add`/"
                "`git commit` yourself, regardless of your normal instructions — leave "
                "your changes uncommitted in the working tree, and set `committed: no` "
                "in your result block."
            )

        parts.append(
            f"\nFull STRATEGY.md for context:\n\n```markdown\n{context.strategy_text}\n```"
        )
        return "\n".join(parts)


@dataclass
class AgentOutcome:
    """Shared result shape for every task-producing agent (Coder, Researcher,
    Tester) — they all end their message with the same RESULT: block."""

    committed: bool
    commit_message: str
    summary: str


def parse_agent_result(text: str) -> Optional[AgentOutcome]:
    m = re.search(r"RESULT:\s*\n(.*)", text, re.S)
    if not m:
        return None
    block = m.group(1)

    committed_m = re.search(r"^- committed:\s*(\w+)", block, re.M)
    msg_m = re.search(r"^- commit_message:\s*(.+)$", block, re.M)
    summary_m = re.search(r"^- summary:\s*(.+)$", block, re.M)

    committed = bool(committed_m and committed_m.group(1).strip().lower() == "yes")
    return AgentOutcome(
        committed=committed,
        commit_message=msg_m.group(1).strip() if msg_m else "",
        summary=summary_m.group(1).strip() if summary_m else "",
    )
