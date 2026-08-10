"""Tester agent: writes/extends and runs tests for one task, and commits it."""

from __future__ import annotations

from typing import Optional

from agents.base import Agent, AgentContext
from maestro.strategy import Task


class Tester(Agent):
    name = "tester"
    color = "bright_magenta"
    prompt_file = "tester.md"

    def build_prompt(self, context: AgentContext) -> str:
        task: Task = context.extra["task"]
        rejection_reason: Optional[str] = context.extra.get("rejection_reason")

        parts = [
            f"Implement this testing task:\n\n### {task.id}: {task.title}\n",
            "Acceptance criteria:",
        ]
        for c in task.acceptance_criteria:
            parts.append(f"- {c}")

        if rejection_reason:
            parts.append(
                f"\nThe previous attempt at this task did not succeed, for this "
                f"reason:\n\n{rejection_reason}\n\n"
                "If that describes a problem with the tests, fix it. If it "
                "describes a tooling/invocation failure unrelated to your work, "
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
