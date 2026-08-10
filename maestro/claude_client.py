"""
Thin subprocess wrapper around the `claude -p` headless CLI.

This is the only module that knows how to actually invoke Claude Code. Every
agent goes through `ClaudeClient.run()`, which builds the argv, launches the
subprocess, and normalizes whatever comes back on stdout into a
`ClaudeResult`. Keeping this isolated means the rest of the codebase never
has to think about CLI flags or JSON shapes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional


class ClaudeCLINotFound(RuntimeError):
    pass


def summarize_stream_event(event: dict) -> Optional[str]:
    """Turn one `--output-format stream-json` line into a short human-
    readable activity line (tool calls, assistant text, thinking blocks),
    or None if it's not worth surfacing (init/rate-limit/system chatter,
    successful tool results). Schema reference: an `assistant` event's
    `message.content` is a list of blocks (`text` / `thinking` / `tool_use`);
    a `user` event carries the matching `tool_result`."""
    etype = event.get("type")

    if etype == "assistant":
        blocks = (event.get("message") or {}).get("content") or []
        parts = []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                text = (b.get("text") or "").strip()
                if text:
                    parts.append(_truncate(text))
            elif btype == "thinking":
                text = (b.get("thinking") or b.get("text") or "").strip()
                if text:
                    parts.append("\U0001f4ad " + _truncate(text))
            elif btype == "tool_use":
                name = b.get("name") or "tool"
                inp = b.get("input") or {}
                detail = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("description") or ""
                detail = _truncate(str(detail), 90) if detail else ""
                parts.append(f"→ {name}({detail})" if detail else f"→ {name}")
        return "  ".join(parts) if parts else None

    if etype == "user":
        blocks = (event.get("message") or {}).get("content") or []
        for b in blocks:
            if b.get("type") == "tool_result" and b.get("is_error"):
                content = b.get("content")
                text = content if isinstance(content, str) else str(content)
                return f"✗ tool error: {_truncate(text, 150)}"
        return None

    return None


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Text patterns seen in Claude Code's usage/rate-limit messages. Matched
# case-insensitively against error_message + result_text + stderr — this is
# best-effort (the CLI doesn't expose a structured error code for it), so it
# errs toward catching real limit messages over being exhaustive.
#
# "spend limit" / "monthly limit" cover the plan-level cap message
# ("You've hit your monthly spend limit · raise it at ..."), which is
# distinct from the per-minute "usage limit"/"rate limit" wording above and
# was previously falling through to the generic invocation-error path —
# burning a task's retry budget on every call instead of pausing the run.
_RATE_LIMIT_PATTERNS = (
    r"usage limit",
    r"rate.?limit",
    r"spend limit",
    r"monthly limit",
    r"quota",
    r"too many requests",
    r"\b429\b",
    r"overloaded",
)


def is_rate_limited(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(re.search(p, low) for p in _RATE_LIMIT_PATTERNS)


def extract_resume_hint(text: str) -> Optional[str]:
    """Best-effort extraction of "when to retry" from a rate-limit message.
    Returns a human-readable string, or None if nothing could be parsed
    (callers should fall back to a generic "try again later")."""
    if not text:
        return None

    # Some CLI limit messages carry a unix timestamp after a pipe, e.g.
    # "...usage limit reached|1735689600".
    m = re.search(r"\|\s*(\d{10,13})\b", text)
    if m:
        epoch = int(m.group(1))
        if epoch > 10**12:  # milliseconds
            epoch //= 1000
        try:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (OverflowError, OSError, ValueError):
            pass

    for pattern in (r"resets?\s+at\s+[^.\n|]+", r"try again in\s+[^.\n|]+"):
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(0).strip()

    # Monthly spend-limit messages carry no machine-readable reset time at
    # all — the CLI just says "monthly spend limit". Billing cycles reset
    # on the 1st, so the 1st of next month UTC is a reasonable estimate: a
    # scheduler polling this can wait until then instead of hammering the
    # CLI every few minutes on an unknown hint.
    if re.search(r"monthly", text, re.I):
        now = datetime.now(timezone.utc)
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        reset = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        return reset.strftime("%Y-%m-%d %H:%M UTC") + " (estimated monthly reset, not exact)"

    return None


_RESUME_HINT_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})\s*UTC")


def parse_resume_hint(hint: Optional[str]) -> Optional[datetime]:
    """Best-effort inverse of extract_resume_hint(): pulls a concrete UTC
    timestamp back out of a resume_after string, for a scheduler deciding
    whether it's time to retry yet. Returns None for hints with no
    parseable timestamp (e.g. "unknown — try again later" or a free-form
    "try again in 2 hours") — callers should fall back to a fixed poll
    interval in that case rather than trying to parse relative durations."""
    if not hint:
        return None
    m = _RESUME_HINT_DATE_RE.search(hint)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class ClaudeResult:
    """Normalized result of one `claude -p` invocation."""

    ok: bool
    result_text: str = ""
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    duration_ms: Optional[int] = None
    is_error: bool = False
    error_message: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int = 0
    command: list = field(default_factory=list)


class ClaudeClient:
    """Runs `claude -p ...` and parses its JSON output."""

    def __init__(self, model: str = "sonnet", binary: str = "claude", bare: bool = True):
        self.model = model
        self.binary = binary
        # --bare strictly requires ANTHROPIC_API_KEY (OAuth/keychain auth is
        # never read in bare mode, per `claude --help`). Auto-disable it when
        # no API key is set so OAuth/subscription logins keep working; pass
        # bare=False explicitly to force it off regardless.
        self.bare = bare and bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._bare_downgraded = bare and not self.bare
        if shutil.which(binary) is None:
            raise ClaudeCLINotFound(
                f"Could not find '{binary}' on PATH. Install Claude Code "
                "(https://docs.claude.com/claude-code) or set the correct "
                "binary name via ClaudeClient(binary=...)."
            )

    def build_command(
        self,
        prompt: str,
        allowed_tools: str,
        max_turns: int,
        permission_mode: str = "acceptEdits",
        system_prompt: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        streaming: bool = False,
    ) -> list:
        cmd = [self.binary, "-p", prompt, "--output-format", "stream-json" if streaming else "json"]
        if streaming:
            # stream-json in print mode needs --verbose per `claude --help`.
            cmd.append("--verbose")
        if self.bare:
            cmd.append("--bare")
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        return cmd

    def run(
        self,
        prompt: str,
        allowed_tools: str,
        max_turns: int,
        permission_mode: str = "acceptEdits",
        system_prompt: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        resume_session_id: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> ClaudeResult:
        """Invoke `claude -p`. If `on_event` is given, streams
        `stream-json` output and calls it with each parsed event dict as
        the run progresses (tool calls, assistant text/thinking, ...) —
        see `summarize_stream_event()` for turning those into log lines.
        Without it, this blocks and parses a single `json` response,
        which is cheaper when nobody's watching (e.g. the mission
        cleanup pass)."""
        if on_event is not None:
            return self._run_streaming(
                prompt, allowed_tools, max_turns, permission_mode,
                system_prompt, cwd, timeout, resume_session_id, on_event,
            )
        return self._run_blocking(
            prompt, allowed_tools, max_turns, permission_mode,
            system_prompt, cwd, timeout, resume_session_id,
        )

    def _run_blocking(
        self, prompt, allowed_tools, max_turns, permission_mode,
        system_prompt, cwd, timeout, resume_session_id,
    ) -> ClaudeResult:
        cmd = self.build_command(
            prompt=prompt,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            permission_mode=permission_mode,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
        )

        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return ClaudeResult(
                ok=False,
                is_error=True,
                error_message=f"claude invocation timed out after {timeout}s",
                raw_stdout=exc.stdout or "",
                raw_stderr=exc.stderr or "",
                exit_code=-1,
                command=cmd,
                duration_ms=int((time.time() - start) * 1000),
            )
        except OSError as exc:
            return ClaudeResult(
                ok=False,
                is_error=True,
                error_message=f"failed to launch claude: {exc}",
                exit_code=-1,
                command=cmd,
            )

        duration_ms = int((time.time() - start) * 1000)
        stdout, stderr = proc.stdout or "", proc.stderr or ""

        if proc.returncode != 0:
            return ClaudeResult(
                ok=False,
                is_error=True,
                error_message=stderr.strip() or f"claude exited with code {proc.returncode}",
                raw_stdout=stdout,
                raw_stderr=stderr,
                exit_code=proc.returncode,
                command=cmd,
                duration_ms=duration_ms,
            )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return ClaudeResult(
                ok=False,
                is_error=True,
                error_message=f"could not parse claude JSON output: {exc}",
                raw_stdout=stdout,
                raw_stderr=stderr,
                exit_code=proc.returncode,
                command=cmd,
                duration_ms=duration_ms,
            )

        return self._result_from_data(data, stdout, stderr, proc.returncode, cmd, duration_ms)

    def _run_streaming(
        self, prompt, allowed_tools, max_turns, permission_mode,
        system_prompt, cwd, timeout, resume_session_id, on_event,
    ) -> ClaudeResult:
        cmd = self.build_command(
            prompt=prompt,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            permission_mode=permission_mode,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
            streaming=True,
        )

        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            )
        except OSError as exc:
            return ClaudeResult(
                ok=False, is_error=True, error_message=f"failed to launch claude: {exc}",
                exit_code=-1, command=cmd,
            )

        final_event = None
        raw_lines = []
        timed_out = False
        try:
            for line in proc.stdout:
                if timeout is not None and time.time() - start > timeout:
                    proc.kill()
                    timed_out = True
                    break
                line = line.strip()
                if not line:
                    continue
                raw_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    final_event = event
                else:
                    try:
                        on_event(event)
                    except Exception:
                        pass  # a UI callback must never take the agent call down with it
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            timed_out = True

        duration_ms = int((time.time() - start) * 1000)
        stderr = proc.stderr.read() if proc.stderr else ""
        stdout = "\n".join(raw_lines)

        if timed_out:
            return ClaudeResult(
                ok=False, is_error=True,
                error_message=f"claude invocation timed out after {timeout}s",
                raw_stdout=stdout, raw_stderr=stderr, exit_code=-1, command=cmd, duration_ms=duration_ms,
            )

        if final_event is None:
            return ClaudeResult(
                ok=False, is_error=True,
                error_message=(stderr.strip() or "claude stream ended without a final result event"),
                raw_stdout=stdout, raw_stderr=stderr, exit_code=proc.returncode, command=cmd, duration_ms=duration_ms,
            )

        return self._result_from_data(final_event, stdout, stderr, proc.returncode, cmd, duration_ms)

    def _result_from_data(self, data: dict, stdout: str, stderr: str, exit_code: int, cmd: list, duration_ms: int) -> ClaudeResult:
        # Field names have shifted across Claude Code versions; accept
        # multiple aliases defensively rather than pinning to one schema.
        result_text = data.get("result") or data.get("response") or ""
        is_error = bool(data.get("is_error", False))
        cost = data.get("cost_usd", data.get("total_cost_usd"))
        turns = data.get("num_turns", data.get("turns"))

        # Some failure modes (e.g. "not logged in") set is_error=true with no
        # dedicated "error" key — the human-readable message ends up in
        # "result" instead. Fall back to that so error_message is never
        # silently empty when is_error is true.
        error_message = ""
        if is_error:
            error_message = data.get("error") or result_text or f"claude reported is_error=true (exit {exit_code})"

        return ClaudeResult(
            ok=not is_error,
            result_text=result_text,
            session_id=data.get("session_id"),
            cost_usd=cost,
            num_turns=turns,
            duration_ms=data.get("duration_ms", duration_ms),
            is_error=is_error,
            error_message=error_message,
            raw_stdout=stdout,
            raw_stderr=stderr,
            exit_code=exit_code,
            command=cmd,
        )
