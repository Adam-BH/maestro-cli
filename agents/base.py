"""
Agent base class.

Every concrete agent (Planner, Coder, Reviewer, and anything added later)
subclasses `Agent` and implements `build_prompt(context)`. Everything else
— invoking `claude -p`, logging the call to disk, updating the UI — is
handled once here so new agents stay tiny.

To add a new agent type:
  1. Drop a system prompt at agents/prompts/<name>.md
  2. Subclass Agent, set `name`/`color`/`tool_config`, implement build_prompt()
  3. Register it in the loop (maestro/loop.py) where it's invoked

No changes to the base class or the loop's control flow are required.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from config import AgentToolConfig
from maestro.claude_client import ClaudeClient, ClaudeResult


@dataclass
class AgentContext:
    """Everything an agent's prompt-builder might need."""

    strategy_text: str
    cwd: str
    extra: Optional[dict] = None
    # Explicit dos-and-don'ts from the mission, passed separately from
    # strategy_text because not every agent's build_prompt() includes the
    # full STRATEGY.md dump (Reviewer's doesn't) — this is the one path
    # guaranteed to reach every agent.
    constraints: List[str] = field(default_factory=list)


class Agent:
    name: str = "agent"
    color: str = "white"
    prompt_file: str = ""  # relative to prompts_dir

    def __init__(self, client: ClaudeClient, tool_config: AgentToolConfig, prompts_dir: str, log_dir: str):
        self.client = client
        self.tool_config = tool_config
        self.prompts_dir = prompts_dir
        self.log_dir = log_dir
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        if not self.prompt_file:
            return ""
        path = Path(self.prompts_dir) / self.prompt_file
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def build_prompt(self, context: AgentContext) -> str:
        raise NotImplementedError

    def run(self, context: AgentContext, on_event=None) -> ClaudeResult:
        prompt = self.build_prompt(context)
        result = self.client.run(
            prompt=prompt,
            allowed_tools=self.tool_config.allowed_tools,
            max_turns=self.tool_config.max_turns,
            permission_mode=self.tool_config.permission_mode,
            system_prompt=self._system_prompt,
            cwd=context.cwd,
            timeout=self.tool_config.timeout,
            on_event=on_event,
        )
        self._log_call(prompt, result)
        return result

    def _log_call(self, prompt: str, result: ClaudeResult) -> None:
        log_dir = Path(self.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = log_dir / f"{ts}_{self.name}.json"
        payload = {
            "agent": self.name,
            "timestamp": ts,
            "prompt": prompt,
            "command": result.command,
            "ok": result.ok,
            "is_error": result.is_error,
            "error_message": result.error_message,
            "result_text": result.result_text,
            "session_id": result.session_id,
            "cost_usd": result.cost_usd,
            "num_turns": result.num_turns,
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "raw_stderr": result.raw_stderr[-4000:] if result.raw_stderr else "",
            # Only kept on failure — on success result_text/cost/turns above
            # already capture everything useful, and successful stdout can be
            # large. On failure this is often the *only* place to see what
            # the CLI actually printed before dying (e.g. a crash that never
            # reached a final JSON result — see claude_client._run_streaming).
            "raw_stdout": (result.raw_stdout[-4000:] if result.raw_stdout else "") if not result.ok else "",
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
