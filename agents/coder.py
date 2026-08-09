"""Coder agent: implements one task from the plan and commits it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from agents.base import Agent, AgentContext
from orchestrator.strategy import Task


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
                f"\nThis task was previously attempted and REJECTED by the "
                f"Reviewer for this reason:\n\n{rejection_reason}\n\n"
                "Fix that specific problem."
            )

        parts.append(
            f"\nFull STRATEGY.md for context:\n\n```markdown\n{context.strategy_text}\n```"
        )
        return "\n".join(parts)


@dataclass
class CoderOutcome:
    committed: bool
    commit_message: str
    summary: str


def parse_coder_result(text: str) -> Optional[CoderOutcome]:
    m = re.search(r"CODER_RESULT:\s*\n(.*)", text, re.S)
    if not m:
        return None
    block = m.group(1)

    committed_m = re.search(r"^- committed:\s*(\w+)", block, re.M)
    msg_m = re.search(r"^- commit_message:\s*(.+)$", block, re.M)
    summary_m = re.search(r"^- summary:\s*(.+)$", block, re.M)

    committed = bool(committed_m and committed_m.group(1).strip().lower() == "yes")
    return CoderOutcome(
        committed=committed,
        commit_message=msg_m.group(1).strip() if msg_m else "",
        summary=summary_m.group(1).strip() if summary_m else "",
    )
