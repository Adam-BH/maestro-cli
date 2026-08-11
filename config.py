"""
Central configuration for Maestro.

Nothing here talks to the network or the filesystem beyond reading env vars;
it just defines defaults that `maestro/main.py` wires into the rest of
the app (and that CLI flags can override at runtime).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# agents/prompts/*.md ships alongside this file inside the Maestro
# install — it must be found there regardless of which target repo the
# maestro is run against (STRATEGY.md, logs/, etc. are relative to
# *that* repo's cwd instead; see strategy_path/log_dir below).
_PACKAGE_ROOT = Path(__file__).resolve().parent


@dataclass
class AgentToolConfig:
    """Per-agent tool scoping and turn budget passed straight to `claude -p`."""

    allowed_tools: str
    max_turns: int
    permission_mode: str = "acceptEdits"


@dataclass
class Config:
    # Model used for every `claude -p` invocation. Override with
    # MAESTRO_MODEL if you want a cheaper/faster model for iteration.
    model: str = field(default_factory=lambda: os.environ.get("MAESTRO_MODEL", "sonnet"))

    # How many times Coder may retry a single task after a Reviewer REJECT
    # before the loop escalates to NEEDS_HUMAN itself.
    max_task_retries: int = 3

    # Hard ceiling on total plan -> code -> review cycles across the whole
    # run, independent of per-task retries. Exists purely so a pathological
    # plan (or a Planner stuck re-planning) can't loop forever.
    max_total_iterations: int = 100

    # Commit after every accepted Coder attempt, not just after Reviewer
    # approval. Useful for forensic history; off by default to keep the
    # git log readable (one commit per approved task).
    commit_every_attempt: bool = False

    # After the Reviewer approves a task, also run Claude Code's own
    # /code-review skill as a supplementary, informational pass (see
    # Loop.run_task_cycle). Off by default — extra cost per approved task.
    deep_review: bool = False

    # Run each agent with `claude -p --bare` (skips hooks/CLAUDE.md/MCP
    # config for reproducibility). NOTE: --bare only supports
    # ANTHROPIC_API_KEY auth (OAuth/keychain are never read in bare mode) —
    # ClaudeClient auto-disables it when no API key is set, so OAuth/
    # subscription logins keep working. Set False to always use normal auth.
    bare: bool = True

    # Where per-call JSON logs go (timestamped, one file per agent call).
    log_dir: str = "logs"

    # Path to the strategy file that acts as the loop's persistent state.
    # Relative to the target repo's cwd (the project being worked on).
    strategy_path: str = "STRATEGY.md"

    # Directory holding agents/prompts/*.md. Absolute, and anchored to this
    # install rather than the target repo's cwd — see _PACKAGE_ROOT above.
    prompts_dir: str = str(_PACKAGE_ROOT / "agents" / "prompts")

    # Turn budget for the one-shot prompt-enhancing pass at intake (no
    # tools, pure text — but now writing a structured feature brief rather
    # than a typo pass, so a little more headroom than a pure tidy-up).
    mission_enhancer_max_turns: int = 6

    # Turn budget for the one-shot clarifying-questions pass at intake (no
    # tools, pure text — kept tiny since it's just generating 0-5 questions).
    mission_clarifier_max_turns: int = 4

    agents: dict = field(
        default_factory=lambda: {
            "planner": AgentToolConfig(
                allowed_tools="Read,Glob,Grep,Bash(git *)",
                max_turns=15,
            ),
            "coder": AgentToolConfig(
                allowed_tools="Read,Edit,Write,Glob,Grep,Bash",
                max_turns=25,
            ),
            "researcher": AgentToolConfig(
                allowed_tools="Read,Glob,Grep,Bash(git *)",
                max_turns=20,
            ),
            "tester": AgentToolConfig(
                allowed_tools="Read,Edit,Write,Glob,Grep,Bash",
                max_turns=25,
            ),
            "reviewer": AgentToolConfig(
                # Plain Bash, same as coder/tester: a prefix allowlist
                # (Bash(node *), Bash(npm test*), ...) looks safer but isn't
                # — it breaks on anything the allowlist author didn't
                # anticipate (env-var prefixes like `PORT=3457 node ...`,
                # commands chained with && / ;, etc.), which in practice
                # meant the reviewer couldn't verify projects that don't
                # have a test suite yet and burned its turn budget retrying
                # denied variations instead of ever reaching a verdict.
                # Edit/Write are still withheld — that's the actual
                # verifies-never-fixes boundary — enforced structurally
                # here, not just requested in the prompt.
                allowed_tools="Read,Glob,Grep,Bash",
                max_turns=15,
            ),
            # Only invoked when --deep-review is passed, after the Reviewer
            # already approved a task — an additive, informational pass via
            # Claude Code's own /code-review skill. Read-only, same
            # reasoning as reviewer above.
            "deep_reviewer": AgentToolConfig(
                allowed_tools="Read,Glob,Grep,Bash",
                max_turns=20,
            ),
        }
    )


DEFAULT_CONFIG = Config()
