"""Skill Writer agent: packages a finished project into a Claude Code
Skill (.claude/skills/<slug>/SKILL.md) for future sessions working in
that repo. Invoked once, after the whole mission is done -- see
maestro/loop.py's Loop.run()."""

from __future__ import annotations

from agents.base import Agent, AgentContext


class SkillWriter(Agent):
    name = "skillwriter"
    color = "bright_yellow"
    prompt_file = "skillwriter.md"

    def build_prompt(self, context: AgentContext) -> str:
        slug = context.extra["slug"]
        parts = [
            f"The mission below is finished and reviewed. Write "
            f".claude/skills/{slug}/SKILL.md for it now, using `{slug}` as "
            "the skill's name.",
        ]
        if context.constraints:
            parts.append("\nHard constraints for this run (must not be violated):")
            for c in context.constraints:
                parts.append(f"- {c}")
        parts.append(
            f"\nFull STRATEGY.md for context:\n\n```markdown\n{context.strategy_text}\n```"
        )
        return "\n".join(parts)
