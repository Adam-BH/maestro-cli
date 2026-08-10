"""
The plan -> code -> review loop controller.

This module owns all control flow: when to plan, when to hand a task to
the Coder, when to send it to the Reviewer, when to retry, when to
checkpoint-commit, and when to stop (or not) for a human. It knows nothing
about argument parsing or terminal setup (that's main.py) and nothing
about how `claude -p` is invoked (that's claude_client.py / agents/*).

Two failure modes are handled very differently:

- A single task hitting NEEDS_HUMAN (ambiguous criteria, exhausted
  retries, ...) is a *task-scoped* problem. By default (`unattended=True`)
  the loop logs it, leaves that task marked `needs_human`, and moves on to
  the next task — one bad task shouldn't stop an overnight run. Pass
  `unattended=False` to get the old behavior: stop and wait at a terminal
  prompt for each one.
- Hitting a usage/rate limit is a *run-scoped* problem — every subsequent
  call would fail too, so retrying other tasks is pointless. The whole run
  pauses immediately regardless of `unattended`, with a best-effort
  "resume after" hint written into STRATEGY.md so a later `--resume` (or a
  cron job) knows when it's worth trying again.

Adding a new agent type (e.g. a "Security" pass after Reviewer) means
adding a call to it here, in the loop body — but the surrounding
plan/retry/checkpoint machinery doesn't change.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from agents.base import AgentContext
from agents.coder import Coder, parse_coder_result
from agents.planner import Planner, parse_plan
from agents.reviewer import Reviewer, parse_verdict
from config import Config
from orchestrator import git_utils
from orchestrator.claude_client import (
    ClaudeClient,
    ClaudeResult,
    extract_resume_hint,
    is_rate_limited,
    summarize_stream_event,
)
from orchestrator.strategy import Strategy, Task
from orchestrator.ui.console import OrchestratorUI

OUTCOME_APPROVED = "approved"
OUTCOME_NEEDS_HUMAN = "needs_human"
OUTCOME_RATE_LIMITED = "rate_limited"


class Loop:
    def __init__(
        self,
        config: Config,
        strategy: Strategy,
        ui: OrchestratorUI,
        cwd: str,
        client: "ClaudeClient | None" = None,
    ):
        self.config = config
        self.strategy = strategy
        self.ui = ui
        self.cwd = cwd
        self.commits: List[Tuple[str, str]] = []

        if client is not None:
            self.client = client
        else:
            self.client = ClaudeClient(model=config.model, bare=config.bare)
            if config.bare and not self.client.bare:
                ui.print_panel(
                    "Auth notice",
                    "--bare mode requires ANTHROPIC_API_KEY (OAuth/keychain auth "
                    "isn't readable in bare mode) and no API key is set, so "
                    "AgentOrchestrator is running agents WITHOUT --bare — they'll "
                    "pick up your normal Claude Code auth, hooks, and CLAUDE.md.",
                    style="yellow",
                )
        self.planner = Planner(self.client, config.agents["planner"], config.prompts_dir, config.log_dir)
        self.coder = Coder(self.client, config.agents["coder"], config.prompts_dir, config.log_dir)
        self.reviewer = Reviewer(self.client, config.agents["reviewer"], config.prompts_dir, config.log_dir)

        self.ui.attach_strategy(strategy)

    # -- persistence -----------------------------------------------

    def save(self) -> None:
        self.strategy.save(self.config.strategy_path)

    # -- live activity streaming --------------------------------------

    def _event_logger(self, agent_name: str):
        """Callback passed as on_event= to an Agent.run() call: turns each
        stream-json event into an Activity Log line as it happens, instead
        of only seeing output once the whole call finishes."""

        def _on_event(event: dict) -> None:
            text = summarize_stream_event(event)
            if text:
                self.ui.log(agent_name, text)

        return _on_event

    # -- rate-limit handling -----------------------------------------

    @staticmethod
    def _rate_limit_text(result: ClaudeResult) -> Optional[str]:
        # error_message/result_text are usually enough, but a hard process
        # crash (exit 1, no JSON ever printed) can leave both empty with
        # the only hint sitting in stderr — check that too rather than miss it.
        combined = " ".join(filter(None, [result.error_message, result.result_text, result.raw_stderr]))
        return combined if is_rate_limited(combined) else None

    def _pause_for_rate_limit(self, agent_name: str, detail: str) -> None:
        hint = extract_resume_hint(detail) or "unknown — try --resume again later"
        self.strategy.resume_after = hint
        self.strategy.run_status = "rate_limited"
        self.strategy.add_log(
            agent_name, f"Hit a usage/rate limit; pausing the whole run. Resume hint: {hint}"
        )
        self.ui.clear_agent()
        self.ui.print_panel(
            "Rate limit hit — pausing run",
            f"{detail}\n\nResume after: {hint}\n\n"
            "Re-run with --resume once that's passed — progress up to now is saved.",
            style="red",
        )

    # -- planning --------------------------------------------------

    def plan_step(self, revising: bool = False) -> str:
        """Returns 'ok', 'rate_limited', or 'failed'. Retries an
        invocation-level failure or unparseable output up to
        config.max_task_retries before giving up — same reasoning as
        run_task_cycle: a single CLI hiccup shouldn't be able to sink an
        entire run before it even starts."""
        result = None
        tasks = []
        attempt = 0

        while True:
            attempt += 1
            self.ui.set_agent("planner", f"revising plan (attempt {attempt})" if revising else f"planning (attempt {attempt})")
            context = AgentContext(
                strategy_text=self.strategy.render(), cwd=self.cwd, extra={"revising": revising}
            )
            result = self.planner.run(context, on_event=self._event_logger("planner"))
            self.ui.record_call(result.cost_usd, result.num_turns)

            if not result.ok:
                rl = self._rate_limit_text(result)
                if rl:
                    self._pause_for_rate_limit("planner", rl)
                    return "rate_limited"

                self.ui.log("planner", f"ERROR: {result.error_message}")
                self.strategy.add_log("planner", f"attempt {attempt}: invocation error: {result.error_message}")
                if attempt >= self.config.max_task_retries:
                    self.strategy.run_status = "blocked"
                    self.ui.clear_agent()
                    self.ui.print_needs_human("(planning)", f"Planner invocation failed: {result.error_message}")
                    return "failed"
                continue

            tasks = parse_plan(result.result_text)
            if not tasks:
                self.ui.log("planner", "ERROR: no parseable plan in output")
                self.strategy.add_log("planner", f"attempt {attempt}: no parseable PLAN block.")
                if attempt >= self.config.max_task_retries:
                    self.strategy.run_status = "blocked"
                    self.ui.clear_agent()
                    self.ui.print_needs_human("(planning)", "Planner did not return a parseable plan.")
                    return "failed"
                continue

            break

        # Preserve completed/in-flight task state when revising by title match;
        # otherwise this is a fresh plan.
        if revising and self.strategy.tasks:
            old_by_title = {t.title: t for t in self.strategy.tasks}
            for t in tasks:
                old = old_by_title.get(t.title)
                if old:
                    t.status, t.attempts, t.notes = old.status, old.attempts, old.notes

        self.strategy.tasks = tasks
        self.strategy.run_status = "in_progress"
        msg = f"Produced plan with {len(tasks)} tasks."
        self.strategy.add_log("planner", msg)
        self.ui.log("planner", msg)
        self.ui.clear_agent()
        return "ok"

    # -- one task's code/review cycle -------------------------------

    def _retry_or_escalate(self, task: Task, reason: str) -> Optional[Tuple[str, str]]:
        """Mark `task` rejected with `reason`. If retries are exhausted,
        marks it needs_human instead and returns the (outcome, reason) pair
        to return from run_task_cycle. Returns None if the caller should
        retry (loop again) — used uniformly for REJECT verdicts, invocation
        failures, and unparseable output, so a single transient CLI hiccup
        gets the same chance to recover as a real REJECT does, instead of
        being escalated to a human on the very first failure."""
        task.status = "rejected"
        task.notes = reason
        if task.attempts >= self.config.max_task_retries:
            task.status = "needs_human"
            escalated = (
                f"Exceeded max retries ({self.config.max_task_retries}) on {task.id}. "
                f"Last error: {reason}"
            )
            task.notes = escalated
            return OUTCOME_NEEDS_HUMAN, escalated
        return None

    def run_task_cycle(self, task: Task) -> Tuple[str, str]:
        """Drive one task through Coder -> Reviewer, retrying on REJECT *or*
        on an invocation-level failure (subprocess crash, unparseable
        output — plausibly transient, not necessarily a real problem with
        the task) up to config.max_task_retries. Returns (outcome, reason)
        where outcome is OUTCOME_APPROVED, OUTCOME_NEEDS_HUMAN, or
        OUTCOME_RATE_LIMITED."""

        last_rejection: Optional[str] = task.notes if task.status == "rejected" else None

        while True:
            task.attempts += 1
            task.status = "in_progress"
            self.strategy.add_log("system", f"Attempt {task.attempts} on {task.id}: {task.title}")
            self.save()

            self.ui.set_agent("coder", f"{task.id} (attempt {task.attempts}/{self.config.max_task_retries})")
            coder_ctx = AgentContext(
                strategy_text=self.strategy.render(),
                cwd=self.cwd,
                extra={"task": task, "rejection_reason": last_rejection},
            )
            coder_result = self.coder.run(coder_ctx, on_event=self._event_logger("coder"))
            self.ui.record_call(coder_result.cost_usd, coder_result.num_turns)

            if not coder_result.ok:
                self.ui.clear_agent()
                rl = self._rate_limit_text(coder_result)
                if rl:
                    task.status = "pending"
                    task.attempts -= 1  # this attempt never really happened
                    return OUTCOME_RATE_LIMITED, rl
                self.ui.log("coder", f"ERROR: {coder_result.error_message}")
                self.strategy.add_log("coder", f"{task.id}: invocation error: {coder_result.error_message}")
                escalation = self._retry_or_escalate(task, f"Coder invocation failed: {coder_result.error_message}")
                if escalation:
                    return escalation
                last_rejection = task.notes
                continue

            coder_outcome = parse_coder_result(coder_result.result_text)
            summary = coder_outcome.summary if coder_outcome else (coder_result.result_text or "")[-500:]
            self.ui.log("coder", summary or "(no summary returned)")
            self.strategy.add_log("coder", f"{task.id} attempt {task.attempts}: {summary}")

            if self.config.commit_every_attempt:
                sha = git_utils.commit_all(
                    f"[checkpoint] {task.id} attempt {task.attempts}", cwd=self.cwd
                )
                if sha:
                    self.commits.append((sha, f"[checkpoint] {task.id} attempt {task.attempts}"))

            self.ui.set_agent("reviewer", f"reviewing {task.id}")
            review_ctx = AgentContext(
                strategy_text=self.strategy.render(),
                cwd=self.cwd,
                extra={"task": task, "coder_summary": summary},
            )
            review_result = self.reviewer.run(review_ctx, on_event=self._event_logger("reviewer"))
            self.ui.record_call(review_result.cost_usd, review_result.num_turns)

            if not review_result.ok:
                self.ui.clear_agent()
                rl = self._rate_limit_text(review_result)
                if rl:
                    task.status = "pending"
                    task.attempts -= 1
                    return OUTCOME_RATE_LIMITED, rl
                self.ui.log("reviewer", f"ERROR: {review_result.error_message}")
                self.strategy.add_log("reviewer", f"{task.id}: invocation error: {review_result.error_message}")
                escalation = self._retry_or_escalate(task, f"Reviewer invocation failed: {review_result.error_message}")
                if escalation:
                    return escalation
                last_rejection = task.notes
                continue

            verdict = parse_verdict(review_result.result_text)
            if verdict is None:
                self.ui.log("reviewer", "ERROR: no parseable verdict")
                self.strategy.add_log("reviewer", f"{task.id}: no parseable VERDICT in output.")
                self.ui.clear_agent()
                escalation = self._retry_or_escalate(task, "Reviewer did not return a parseable verdict.")
                if escalation:
                    return escalation
                last_rejection = task.notes
                continue

            verdict_line = verdict.kind + (f": {verdict.reason}" if verdict.reason else "")
            self.ui.log("reviewer", verdict_line)
            self.strategy.add_log("reviewer", f"{task.id} attempt {task.attempts}: {verdict_line}")
            self.ui.clear_agent()

            if verdict.approved:
                task.status = "done"
                task.notes = ""
                sha = git_utils.commit_all(f"[approved] {task.id}: {task.title}", cwd=self.cwd)
                if sha:
                    self.commits.append((sha, f"[approved] {task.id}: {task.title}"))
                return OUTCOME_APPROVED, ""

            if verdict.needs_human:
                task.status = "needs_human"
                task.notes = verdict.reason
                return OUTCOME_NEEDS_HUMAN, verdict.reason

            # REJECT
            escalation = self._retry_or_escalate(task, verdict.reason)
            if escalation:
                return escalation
            last_rejection = task.notes
            # else: loop again, Coder gets last_rejection as context

    # -- top-level run loop ------------------------------------------

    def run(self, plan_only: bool = False, on_needs_human=None, unattended: bool = True) -> str:
        """Run the full loop.

        `unattended` (default True): a task hitting NEEDS_HUMAN is logged
        and left parked so the loop can move on to the next task, instead
        of stopping to wait for input. Pass False (and an `on_needs_human`
        callback) to restore the old behavior of pausing at a terminal
        prompt for each one.

        A usage/rate limit always pauses the *whole* run, regardless of
        `unattended` — see module docstring.
        """

        if not self.strategy.tasks:
            status = self.plan_step(revising=False)
            self.save()
            if status != "ok":
                return self.strategy.run_status

        if plan_only:
            return self.strategy.run_status

        while True:
            if self.strategy.all_done():
                self.strategy.run_status = "done"
                break

            if self.strategy.iteration >= self.config.max_total_iterations:
                self.strategy.run_status = "blocked"
                msg = f"Hit global iteration cap ({self.config.max_total_iterations})."
                self.strategy.add_log("system", msg)
                self.save()
                self.ui.print_panel("Iteration cap reached", msg, style="yellow")
                break

            task = self.strategy.next_pending_task()
            if task is None:
                # Nothing pending/rejected and not all done => remaining
                # tasks are parked on needs_human; nothing more to do here.
                self.strategy.run_status = "blocked" if self.strategy.blocked_tasks() else "done"
                break

            self.strategy.iteration += 1
            outcome, reason = self.run_task_cycle(task)
            self.save()

            if outcome == OUTCOME_RATE_LIMITED:
                self._pause_for_rate_limit("system", reason)
                self.save()
                break

            if outcome == OUTCOME_NEEDS_HUMAN:
                if unattended:
                    self.strategy.add_log(
                        "system", f"{task.id} needs human input — continuing unattended. Reason: {reason}"
                    )
                    self.ui.log("system", f"{task.id} parked (needs human), moving to next task.")
                    self.save()
                    continue

                self.strategy.run_status = "paused_needs_human"
                self.save()
                self.ui.print_needs_human(task.id, reason)

                retry = on_needs_human(task.id, reason) if on_needs_human else False
                if not retry:
                    self.strategy.run_status = "failed"
                    self.save()
                    break

                task.status = "pending"
                task.attempts = 0
                task.notes = ""
                self.strategy.run_status = "in_progress"
                self.save()

        self.save()
        return self.strategy.run_status
