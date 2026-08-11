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

from pathlib import Path
from typing import List, Optional, Tuple

from agents.base import AgentContext
from agents.coder import Coder, parse_agent_result
from agents.planner import Planner, parse_plan, parse_stack
from agents.researcher import Researcher
from agents.reviewer import Reviewer, parse_verdict
from agents.skillwriter import SkillWriter
from agents.tester import Tester
from config import Config
from maestro import git_utils
from maestro.claude_client import (
    ClaudeClient,
    ClaudeResult,
    extract_resume_hint,
    get_usage_resets,
    is_rate_limited,
    is_spend_limited,
    summarize_stream_event,
)
from maestro.strategy import Strategy, Task
from maestro.ui.console import MaestroUI

OUTCOME_APPROVED = "approved"
OUTCOME_NEEDS_HUMAN = "needs_human"
OUTCOME_RATE_LIMITED = "rate_limited"


class Loop:
    def __init__(
        self,
        config: Config,
        strategy: Strategy,
        ui: MaestroUI,
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
            ui.show_cost = self.client.bare
            if config.bare and not self.client.bare:
                ui.print_panel(
                    "Auth notice",
                    "--bare mode requires ANTHROPIC_API_KEY (OAuth/keychain auth "
                    "isn't readable in bare mode) and no API key is set, so "
                    "Maestro is running agents WITHOUT --bare — they'll "
                    "pick up your normal Claude Code auth, hooks, and CLAUDE.md.",
                    style="yellow",
                )
        self.planner = Planner(self.client, config.agents["planner"], config.prompts_dir, config.log_dir)
        self.coder = Coder(self.client, config.agents["coder"], config.prompts_dir, config.log_dir)
        self.researcher = Researcher(self.client, config.agents["researcher"], config.prompts_dir, config.log_dir)
        self.tester = Tester(self.client, config.agents["tester"], config.prompts_dir, config.log_dir)
        self.reviewer = Reviewer(self.client, config.agents["reviewer"], config.prompts_dir, config.log_dir)
        self.skillwriter = SkillWriter(self.client, config.agents["skillwriter"], config.prompts_dir, config.log_dir)
        # Task.agent -> the agent instance that implements it. Reviewer is
        # not in here; it always runs afterward regardless of who produced
        # the work.
        self.producers = {"coder": self.coder, "researcher": self.researcher, "tester": self.tester}

        self.ui.attach_strategy(strategy)

    # -- persistence -----------------------------------------------

    def save(self) -> None:
        self.strategy.save(self.config.strategy_path)
        # Commit STRATEGY.md on its own, independent of commit_every_attempt
        # or task approvals — this is the run's entire memory, and losing it
        # to a crash/reset shouldn't be possible just because a task's code
        # isn't ready to commit yet. Scoped to this one path (see
        # commit_paths) so it never sweeps up unrelated dirty/staged files.
        if not self.strategy.no_commit:
            git_utils.commit_paths(
                [self.config.strategy_path],
                f"[checkpoint] STRATEGY.md — iteration {self.strategy.iteration}",
                cwd=self.cwd,
            )

    # -- skill generation ----------------------------------------------

    def _generate_skill(self) -> None:
        """Runs once, right after the mission reaches run_status == "done"
        (see run()) -- packages the finished project into a Claude Code
        Skill (.claude/skills/<slug>/SKILL.md) for future sessions working
        in this repo. A nicety on top of an already-successful run: any
        failure here is logged and swallowed, never turns a "done" run
        into a failed one."""
        slug = Path(self.cwd).name
        context = AgentContext(
            strategy_text=self.strategy.render(),
            cwd=self.cwd,
            extra={"slug": slug},
            constraints=self.strategy.constraints,
        )
        result = self.skillwriter.run(context, on_event=self._event_logger("skillwriter"))
        if not result.ok:
            self.strategy.add_log("system", f"Skill generation skipped: {result.error_message or 'agent error'}.")
            return

        skill_path = f".claude/skills/{slug}/SKILL.md"
        if not self.strategy.no_commit:
            git_utils.commit_paths([skill_path], f"Add SKILL.md for {slug}", cwd=self.cwd)
        self.strategy.add_log("system", f"Generated {skill_path}.")

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

    def _resume_hint(self, detail: str) -> str:
        # A spend/credit cap ("monthly spend limit") isn't the session/week
        # usage limiter — /usage has no reset time for it at all, so don't
        # even ask; go straight to the text-based hint (which, for this
        # message, is also unknown — see extract_resume_hint()).
        if not is_spend_limited(detail):
            live = get_usage_resets(self.client.binary)
            future = [dt for dt in live.values() if dt]
            if future:
                soonest = min(future)
                return soonest.strftime("%Y-%m-%d %H:%M UTC") + " (from `claude -p /usage`)"
        return (
            extract_resume_hint(detail)
            or "unknown — the watchdog will keep checking periodically; "
               "see claude.ai/settings/usage for a credit/spend cap"
        )

    def _pause_for_rate_limit(self, agent_name: str, detail: str) -> None:
        hint = self._resume_hint(detail)
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
                strategy_text=self.strategy.render(),
                cwd=self.cwd,
                extra={"revising": revising},
                constraints=self.strategy.constraints,
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

            stack = parse_stack(result.result_text)
            if stack:
                # Preserve an already-established decision across plan
                # revisions rather than letting a later Planner call
                # silently overwrite it (planner.md tells it not to
                # re-decide, but don't rely on that alone).
                self.strategy.stack = stack

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
        retry (loop again) — used uniformly for REJECT verdicts and
        producer invocation failures, so a single transient CLI hiccup gets
        the same chance to recover as a real REJECT does, instead of being
        escalated to a human on the very first failure."""
        task.status = "rejected"
        task.notes = reason
        task.review_attempts = 0
        task.last_producer_summary = ""
        if task.attempts >= self.config.max_task_retries:
            task.status = "needs_human"
            escalated = (
                f"Exceeded max retries ({self.config.max_task_retries}) on {task.id}. "
                f"Last error: {reason}"
            )
            task.notes = escalated
            return OUTCOME_NEEDS_HUMAN, escalated
        return None

    def _retry_review_or_escalate(self, task: Task, reason: str) -> Optional[Tuple[str, str]]:
        """Like _retry_or_escalate, but for a failure *in the Reviewer step
        itself* (invocation error, unparseable output) rather than a real
        REJECT verdict — the producer's work was never actually judged.
        Tracked via review_attempts, a separate budget from the producer's
        attempts, so a flaky reviewer call can't burn the Coder's retries or
        trigger a pointless recode of already-fine work. Keeps the task in
        pending_review while under budget, so the next loop iteration (or a
        --resume) goes straight back to the Reviewer with the same
        last_producer_summary instead of re-running the producer."""
        task.review_attempts += 1
        task.notes = reason
        if task.review_attempts >= self.config.max_task_retries:
            task.status = "needs_human"
            escalated = (
                f"Reviewer failed {task.review_attempts} time(s) on {task.id} without "
                f"reaching a verdict (tooling/invocation issue — the {task.agent}'s work "
                f"was never actually rejected). Last error: {reason}"
            )
            task.notes = escalated
            task.last_producer_summary = ""
            return OUTCOME_NEEDS_HUMAN, escalated
        task.status = "pending_review"
        return None

    def _run_deep_review(self, task: Task) -> None:
        """Optional (--deep-review), additive-only pass: runs Claude Code's
        own /code-review skill against the just-approved (not yet
        committed) change and logs whatever it finds. Purely informational
        — it never changes task status/verdict, since /code-review's
        output isn't the strict APPROVE/REJECT/NEEDS_HUMAN contract the
        rest of this loop gates on. Any failure (including /code-review
        simply not firing under headless `-p`) is swallowed, same
        tolerance as main.py's enhance_mission."""
        if not self.config.deep_review:
            return
        tool_config = self.config.agents["deep_reviewer"]
        self.ui.set_agent("deep_reviewer", f"code-review pass on {task.id}")
        result = self.client.run(
            prompt="/code-review",
            allowed_tools=tool_config.allowed_tools,
            max_turns=tool_config.max_turns,
            permission_mode=tool_config.permission_mode,
            cwd=self.cwd,
            timeout=tool_config.timeout,
            on_event=self._event_logger("deep_reviewer"),
        )
        self.ui.record_call(result.cost_usd, result.num_turns)
        self.ui.clear_agent()

        if not result.ok:
            self.strategy.add_log(
                "deep_reviewer", f"{task.id}: /code-review pass failed (non-blocking): {result.error_message}"
            )
            return

        findings = (result.result_text or "").strip()
        self.ui.log("deep_reviewer", findings[:500] if findings else "(no findings)")
        self.strategy.add_log("deep_reviewer", f"{task.id}: {findings or '(no findings)'}")

    def run_task_cycle(self, task: Task) -> Tuple[str, str]:
        """Drive one task through Coder -> Reviewer, retrying on REJECT *or*
        on a producer invocation-level failure up to config.max_task_retries.
        A Reviewer-side failure (invocation error, unparseable output) is
        handled separately — see _retry_review_or_escalate — since it means
        the producer's work was never actually judged: the task is left
        pending_review with the producer's summary preserved, so the next
        iteration (or a --resume after a crash mid-review) goes straight
        back to the Reviewer instead of redoing work that was never
        rejected. Returns (outcome, reason) where outcome is
        OUTCOME_APPROVED, OUTCOME_NEEDS_HUMAN, or OUTCOME_RATE_LIMITED."""

        last_rejection: Optional[str] = task.notes if task.status == "rejected" else None
        producer = self.producers.get(task.agent, self.coder)
        summary = task.last_producer_summary

        while True:
            if task.status == "pending_review":
                self.strategy.add_log(
                    "system",
                    f"Re-reviewing {task.id} (review attempt {task.review_attempts + 1}/"
                    f"{self.config.max_task_retries}) without re-running {producer.name} — "
                    "its prior work was never rejected, only the review itself failed.",
                )
                self.save()
            else:
                task.attempts += 1
                task.review_attempts = 0
                task.status = "in_progress"
                self.strategy.add_log("system", f"Attempt {task.attempts} on {task.id}: {task.title}")
                self.save()

                self.ui.set_agent(producer.name, f"{task.id} (attempt {task.attempts}/{self.config.max_task_retries})")
                producer_ctx = AgentContext(
                    strategy_text=self.strategy.render(),
                    cwd=self.cwd,
                    extra={
                        "task": task,
                        "rejection_reason": last_rejection,
                        "no_commit": self.strategy.no_commit,
                    },
                    constraints=self.strategy.constraints,
                )
                producer_result = producer.run(producer_ctx, on_event=self._event_logger(producer.name))
                self.ui.record_call(producer_result.cost_usd, producer_result.num_turns)

                if not producer_result.ok:
                    self.ui.clear_agent()
                    rl = self._rate_limit_text(producer_result)
                    if rl:
                        task.status = "pending"
                        task.attempts -= 1  # this attempt never really happened
                        return OUTCOME_RATE_LIMITED, rl
                    self.ui.log(producer.name, f"ERROR: {producer_result.error_message}")
                    self.strategy.add_log(producer.name, f"{task.id}: invocation error: {producer_result.error_message}")
                    escalation = self._retry_or_escalate(task, f"{producer.name.capitalize()} invocation failed: {producer_result.error_message}")
                    if escalation:
                        return escalation
                    last_rejection = task.notes
                    continue

                producer_outcome = parse_agent_result(producer_result.result_text)
                summary = producer_outcome.summary if producer_outcome else (producer_result.result_text or "")[-500:]
                self.ui.log(producer.name, summary or "(no summary returned)")
                self.strategy.add_log(producer.name, f"{task.id} attempt {task.attempts}: {summary}")

                if self.config.commit_every_attempt and not self.strategy.no_commit:
                    sha = git_utils.commit_all(
                        f"[checkpoint] {task.id} attempt {task.attempts}", cwd=self.cwd
                    )
                    if sha:
                        self.commits.append((sha, f"[checkpoint] {task.id} attempt {task.attempts}"))

                # Checkpoint before handing off to the Reviewer: if the
                # Reviewer call itself crashes or gets interrupted, --resume
                # should find this task pending_review with the summary
                # intact, not stuck in_progress with the attempt's context
                # gone (see main.py's stuck-in_progress recovery on resume).
                task.last_producer_summary = summary
                task.status = "pending_review"
                self.save()

            self.ui.set_agent("reviewer", f"reviewing {task.id}")
            review_ctx = AgentContext(
                strategy_text=self.strategy.render(),
                cwd=self.cwd,
                extra={
                    "task": task,
                    "coder_summary": summary,
                    "no_commit": self.strategy.no_commit,
                },
                constraints=self.strategy.constraints,
            )
            review_result = self.reviewer.run(review_ctx, on_event=self._event_logger("reviewer"))
            self.ui.record_call(review_result.cost_usd, review_result.num_turns)

            if not review_result.ok:
                self.ui.clear_agent()
                rl = self._rate_limit_text(review_result)
                if rl:
                    return OUTCOME_RATE_LIMITED, rl  # left pending_review; nothing to undo
                self.ui.log("reviewer", f"ERROR: {review_result.error_message}")
                self.strategy.add_log("reviewer", f"{task.id}: invocation error: {review_result.error_message}")
                escalation = self._retry_review_or_escalate(task, f"Reviewer invocation failed: {review_result.error_message}")
                if escalation:
                    return escalation
                continue

            verdict = parse_verdict(review_result.result_text)
            if verdict is None:
                self.ui.log("reviewer", "ERROR: no parseable verdict")
                self.strategy.add_log("reviewer", f"{task.id}: no parseable VERDICT in output.")
                self.ui.clear_agent()
                escalation = self._retry_review_or_escalate(task, "Reviewer did not return a parseable verdict.")
                if escalation:
                    return escalation
                continue

            verdict_line = verdict.kind + (f": {verdict.reason}" if verdict.reason else "")
            self.ui.log("reviewer", verdict_line)
            self.strategy.add_log("reviewer", f"{task.id} attempt {task.attempts}: {verdict_line}")
            self.ui.clear_agent()

            if verdict.approved:
                self._run_deep_review(task)
                task.status = "done"
                task.notes = ""
                task.last_producer_summary = ""
                task.review_attempts = 0
                if not self.strategy.no_commit:
                    sha = git_utils.commit_all(f"[approved] {task.id}: {task.title}", cwd=self.cwd)
                    if sha:
                        self.commits.append((sha, f"[approved] {task.id}: {task.title}"))
                return OUTCOME_APPROVED, ""

            if verdict.needs_human:
                task.status = "needs_human"
                task.notes = verdict.reason
                task.last_producer_summary = ""
                return OUTCOME_NEEDS_HUMAN, verdict.reason

            # REJECT — genuine code feedback; back to the producer for a
            # real new attempt (this clears pending_review/review_attempts).
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
            self.ui.print_plan_summary(self.strategy.tasks)

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
                    self.ui.print_needs_human(task.id, reason)
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

        if self.strategy.run_status == "done" and self.config.generate_skill:
            self._generate_skill()

        self.save()
        return self.strategy.run_status
