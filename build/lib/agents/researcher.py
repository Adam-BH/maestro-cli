"""Researcher agent: investigates a task read-only and reports findings."""

from __future__ import annotations

from typing import Optional

from agents.base import Agent, AgentContext
from maestro.strategy import Task


class Researcher(Agent):
    name = "researcher"
    color = "cyan"
    prompt_file = "researcher.md"

    def build_prompt(self, context: AgentContext) -> str:
        task: Task = context.extra["task"]
        rejection_reason: Optional[str] = context.extra.get("rejection_reason")

        parts = [
            f"Investigate this task:\n\n### {task.id}: {task.title}\n",
            "Acceptance criteria (what your findings need to establish/answer):",
        ]
        for c in task.acceptance_criteria:
            parts.append(f"- {c}")

        if rejection_reason:
            parts.append(
                f"\nThe previous attempt at this task did not succeed, for this "
                f"reason:\n\n{rejection_reason}\n\n"
                "Address that gap in your investigation."
            )

        if context.constraints:
            parts.append("\nHard constraints for this run (must not be violated):")
            for c in context.constraints:
                parts.append(f"- {c}")

        parts.append(
            f"\nFull STRATEGY.md for context:\n\n```markdown\n{context.strategy_text}\n```"
        )
        return "\n".join(parts)
