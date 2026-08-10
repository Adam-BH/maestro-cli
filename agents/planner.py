"""Planner agent: turns a mission + repo context into a task plan."""

from __future__ import annotations

import re
from typing import List

from agents.base import Agent, AgentContext
from maestro.strategy import Task


class Planner(Agent):
    name = "planner"
    color = "blue"
    prompt_file = "planner.md"

    def build_prompt(self, context: AgentContext) -> str:
        revising = context.extra and context.extra.get("revising")
        header = (
            "Revise the existing plan below based on the current repository "
            "state and decision log."
            if revising
            else "Produce the initial task plan for the mission below."
        )
        prompt = (
            f"{header}\n\n"
            f"Current STRATEGY.md:\n\n```markdown\n{context.strategy_text}\n```\n\n"
            "Investigate the repository as needed, then output the plan block "
            "as specified in your instructions."
        )
        if context.constraints:
            prompt += "\n\nHard constraints for this run (must not be violated):\n"
            prompt += "\n".join(f"- {c}" for c in context.constraints)
        return prompt


def parse_plan(text: str) -> List[Task]:
    """Extract a PLAN:...END_PLAN block from Planner output into Task objects."""
    m = re.search(r"PLAN:\s*\n(.*?)\nEND_PLAN", text, re.S)
    if not m:
        return []

    block = m.group(1)
    tasks: List[Task] = []
    chunks = re.split(r"^### Task (\d+):\s*(.+)$", block, flags=re.M)
    # re.split with groups yields: [prefix, id1, title1, body1, id2, title2, body2, ...]
    chunks = chunks[1:]
    for i in range(0, len(chunks) - 2, 3):
        task_id, title, body = chunks[i], chunks[i + 1], chunks[i + 2]
        ac_match = re.search(r"acceptance_criteria:\s*\n((?:^\s*-\s.+\n?)+)", body, re.M)
        criteria = []
        if ac_match:
            criteria = [
                line.strip().lstrip("- ").strip()
                for line in ac_match.group(1).splitlines()
                if line.strip().startswith("-")
            ]

        agent_m = re.search(r"^\s*-\s*agent:\s*(\w+)", body, re.M)
        agent_role = agent_m.group(1).strip().lower() if agent_m else "coder"
        if agent_role not in ("coder", "researcher", "tester"):
            agent_role = "coder"

        tasks.append(
            Task(
                id=f"task-{task_id.strip()}",
                title=title.strip(),
                acceptance_criteria=criteria,
                agent=agent_role,
            )
        )
    return tasks
