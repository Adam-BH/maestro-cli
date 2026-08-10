"""Reviewer agent: checks the Coder's diff against acceptance criteria."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from agents.base import Agent, AgentContext
from maestro.strategy import Task

VERDICT_APPROVE = "APPROVE"
VERDICT_REJECT = "REJECT"
VERDICT_NEEDS_HUMAN = "NEEDS_HUMAN"


class Reviewer(Agent):
    name = "reviewer"
    color = "yellow"
    prompt_file = "reviewer.md"

    def build_prompt(self, context: AgentContext) -> str:
        task: Task = context.extra["task"]
        coder_summary: str = context.extra.get("coder_summary", "(no summary provided)")

        parts = [
            f"Review the most recent commit(s) against this task:\n\n"
            f"### {task.id}: {task.title}\n",
            "Acceptance criteria:",
        ]
        for c in task.acceptance_criteria:
            parts.append(f"- {c}")

        parts.append(f"\nCoder's summary of what it did:\n{coder_summary}")
        parts.append(
            "\nInspect the actual diff yourself (do not trust the summary alone), "
            "run relevant tests, and output your verdict as specified in your "
            "instructions."
        )

        if context.constraints:
            parts.append("\nHard constraints for this run (verify these weren't violated):")
            for c in context.constraints:
                parts.append(f"- {c}")

        if context.extra.get("no_commit"):
            parts.append(
                "\nNOTE: this run has a NO-COMMIT constraint, so the Coder's changes "
                "will NOT be committed. Inspect the uncommitted working tree "
                "(`git status`, `git diff`) instead of commit history (`git show`, "
                "`git log -p`) — do not reject a task merely for being uncommitted."
            )

        return "\n".join(parts)


@dataclass
class Verdict:
    kind: str  # APPROVE | REJECT | NEEDS_HUMAN
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.kind == VERDICT_APPROVE

    @property
    def needs_human(self) -> bool:
        return self.kind == VERDICT_NEEDS_HUMAN


def parse_verdict(text: str) -> Optional[Verdict]:
    m = re.search(
        r"VERDICT:\s*(APPROVE|REJECT|NEEDS_HUMAN)\s*(?::\s*(.+))?", text
    )
    if not m:
        return None
    kind = m.group(1)
    reason = (m.group(2) or "").strip()
    return Verdict(kind=kind, reason=reason)
