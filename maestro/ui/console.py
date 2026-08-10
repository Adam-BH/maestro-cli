"""
Rich-based terminal UI for AgentOrchestrator.

Why `rich` over `textual`: this is a linear, mostly-blocking pipeline (one
`claude -p` subprocess call runs to completion before the next agent
starts) rather than an app with concurrent input handling or navigable
screens. `rich.live.Live` gives a live-updating multi-panel layout with a
fraction of the code textual would need, and it degrades gracefully to
plain scrolling output when stdout isn't a real terminal (CI logs, piped
output). If a future agent needs concurrent panes or keyboard-driven
navigation, textual would be worth revisiting.

`OrchestratorUI` owns a `Live` layout with three regions:
  - a header showing which agent is currently running (with live streamed
    activity — tool calls, assistant text/thinking — as it happens)
  - a task checklist (rendered from the Strategy)
  - a scrolling log/history panel

Resize handling: everything here is sized from `self.console.size` fresh
on every render, and every line of text is `no_wrap=True,
overflow="ellipsis"` rather than left to wrap — so the live region's total
height/width is always recomputed to fit the *current* terminal exactly
and no single row can silently grow into two. That combination is what
keeps Rich's Live redraw math (which erases-and-repaints based on the
previous frame's line count) from going stale and glitching when you
resize the window. A SIGWINCH handler forces an immediate refresh the
moment a resize happens too, instead of waiting for the next tick.

It also prints one-off panels (mission confirmation, NEEDS_HUMAN pauses,
final summary) outside the Live context, since those need the user's full
attention and a scrollback trail.
"""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

AGENT_COLORS = {
    "planner": "blue",
    "coder": "green",
    "reviewer": "yellow",
    "system": "magenta",
}

STATUS_ICON = {
    "pending": "○",
    "in_progress": "◐",
    "done": "●",
    "rejected": "✗",
    "needs_human": "⚠",
}

STATUS_STYLE = {
    "done": "green",
    "in_progress": "yellow",
    "rejected": "red",
    "needs_human": "bold red",
    "pending": "dim",
}


def agent_color(name: str) -> str:
    return AGENT_COLORS.get(name.lower(), "cyan")


@dataclass
class LogLine:
    agent: str
    text: str


class OrchestratorUI:
    """Owns all terminal output for a run."""

    def __init__(self, console: Optional[Console] = None, history_limit: int = 500):
        self.console = console or Console()
        self.history: list = []
        self.history_limit = history_limit
        self.current_agent: Optional[str] = None
        self.current_action: str = "idle"
        self.strategy = None  # set via attach_strategy
        self.total_cost = 0.0
        self.total_turns = 0
        self.total_calls = 0
        self._live: Optional[Live] = None
        self._prev_sigwinch = None
        # One persistent Spinner so its internal animation clock keeps
        # advancing across renders instead of resetting to frame 0 every time.
        self._spinner = Spinner("dots")

    # -- lifecycle -------------------------------------------------

    def attach_strategy(self, strategy) -> None:
        self.strategy = strategy

    @contextmanager
    def live(self):
        live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=8,
            transient=False,
            vertical_overflow="visible",
        )
        self._live = live

        has_sigwinch = hasattr(signal, "SIGWINCH")
        if has_sigwinch:
            try:
                self._prev_sigwinch = signal.signal(signal.SIGWINCH, self._handle_resize)
            except ValueError:
                has_sigwinch = False  # not the main thread; refresh_per_second still covers us

        try:
            with live:
                yield self
        finally:
            self._live = None
            if has_sigwinch and self._prev_sigwinch is not None:
                signal.signal(signal.SIGWINCH, self._prev_sigwinch)
                self._prev_sigwinch = None

    def _handle_resize(self, signum, frame) -> None:
        if self._live is not None:
            try:
                self._live.refresh()
            except Exception:
                pass

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def stop_live(self) -> None:
        """Temporarily stop the Live region so a plain `input()` prompt (or
        a one-off panel that needs full scrollback) can be shown cleanly."""
        if self._live is not None:
            self._live.stop()

    def resume_live(self) -> None:
        if self._live is not None:
            self._live.start()

    # -- state updates -----------------------------------------------

    def set_agent(self, agent: str, action: str = "running") -> None:
        self.current_agent = agent
        self.current_action = action
        self._refresh()

    def clear_agent(self) -> None:
        self.current_agent = None
        self.current_action = "idle"
        self._refresh()

    def log(self, agent: str, text: str) -> None:
        # Collapse to one line before it ever reaches history — the Text
        # objects at render time are already no_wrap/ellipsis, but keeping
        # the stored string itself single-line too avoids a very long
        # streamed snippet dominating history size for no visual benefit.
        text = " ".join(text.split())
        self.history.append(LogLine(agent=agent, text=text))
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit:]
        self._refresh()

    def record_call(self, cost_usd: Optional[float], num_turns: Optional[int]) -> None:
        self.total_calls += 1
        if cost_usd:
            self.total_cost += cost_usd
        if num_turns:
            self.total_turns += num_turns
        self._refresh()

    # -- sizing ----------------------------------------------------------
    # Every render recomputes region sizes from the *current* console
    # dimensions, so the live region's total height can never exceed the
    # terminal (the main source of Live redraw glitches on resize).

    def _term_height(self) -> int:
        h = self.console.size.height
        return h if h and h > 0 else 24

    # -- rendering -----------------------------------------------------

    def _render_header(self) -> Panel:
        if self.current_agent:
            color = agent_color(self.current_agent)
            self._spinner.style = color
            status = Text(no_wrap=True, overflow="ellipsis")
            status.append_text(self._spinner.render(time.monotonic()))
            status.append(f" {self.current_agent.upper()}", style=f"bold {color}")
            status.append(f"  — {self.current_action}", style="dim")
        else:
            status = Text("○ idle", style="dim", no_wrap=True, overflow="ellipsis")

        cost_bit = f"${self.total_cost:.4f}" if self.total_cost else "$0.00"
        stats = Text(
            f"calls: {self.total_calls}   turns: {self.total_turns}   cost: {cost_bit}",
            style="dim",
            no_wrap=True,
            overflow="ellipsis",
        )
        border = agent_color(self.current_agent) if self.current_agent else "bright_black"
        return Panel(
            Group(status, stats),
            title="AgentOrchestrator",
            border_style=border,
            box=box.ROUNDED,
        )

    def _render_tasks(self, max_rows: int) -> Panel:
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=2, no_wrap=True)
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column(width=24, no_wrap=True, overflow="ellipsis", justify="right")

        tasks = self.strategy.tasks if self.strategy else []
        max_rows = max(max_rows, 1)
        shown, hidden = tasks[:max_rows], max(len(tasks) - max_rows, 0)

        if not tasks:
            table.add_row("", Text("(no plan yet)", style="dim"), "")
        else:
            for t in shown:
                style = STATUS_STYLE.get(t.status, "white")
                table.add_row(
                    Text(STATUS_ICON.get(t.status, "?"), style=style),
                    Text(f"{t.id}: {t.title}", style=style),
                    Text(f"{t.status}, attempts={t.attempts}", style="dim"),
                )
            if hidden:
                table.add_row("", Text(f"… and {hidden} more", style="dim italic"), "")

        done, total = self.strategy.progress() if self.strategy else (0, 0)
        title = f"Plan — {done}/{total} done" if total else "Plan"
        return Panel(table, title=title, border_style="bright_black", box=box.ROUNDED)

    def _render_log(self, max_rows: int) -> Panel:
        max_rows = max(max_rows, 1)
        lines = []
        for entry in self.history[-max_rows:]:
            color = agent_color(entry.agent)
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(f"{entry.agent:>9} │ ", style=f"bold {color}")
            t.append(entry.text)
            lines.append(t)
        if not lines:
            lines = [Text("(no activity yet)", style="dim")]
        return Panel(Group(*lines), title="Activity", border_style="bright_black", box=box.ROUNDED)

    def _render(self) -> Group:
        # True minimum footprint is 11 rows: header(4) + at least one task
        # row plus its "N more" line plus borders(4) + one log line plus
        # borders(3). Verified to never overflow at or above that height;
        # below it there's nothing left to trim without dropping a panel.
        term_h = self._term_height()
        header_h = 4  # 2 content lines + top/bottom border
        log_min_h = 3  # 1 content line + top/bottom border, _render_log's own floor

        budget = max(term_h - header_h, 6)  # for tasks + log panels combined
        tasks_budget = max(budget - log_min_h, 3)

        n_tasks = len(self.strategy.tasks) if self.strategy else 0
        reserve_for_more_line = 1 if n_tasks > 1 else 0
        task_rows = max(1, min(n_tasks or 1, 8, tasks_budget - 2 - reserve_for_more_line))
        tasks_h = task_rows + 2 + (1 if n_tasks > task_rows else 0)

        log_panel_h = max(budget - tasks_h, log_min_h)
        log_rows = max(log_panel_h - 2, 1)

        return Group(
            self._render_header(),
            self._render_tasks(task_rows),
            self._render_log(log_rows),
        )

    # -- one-off panels (used outside Live, or interleaved with it) ----

    def print_panel(self, title: str, body: str, style: str = "cyan") -> None:
        self.console.print(Panel(body, title=title, border_style=style, box=box.ROUNDED))

    def print_mission(self, mission: str) -> None:
        self.console.print(Panel(mission, title="Mission", border_style="cyan", box=box.ROUNDED))

    def print_needs_human(self, task_id: str, reason: str) -> None:
        body = Text()
        body.append(f"Task: {task_id}\n\n", style="bold")
        body.append(reason)
        self.console.print(Panel(body, title="⚠  NEEDS HUMAN INPUT", border_style="bold red", box=box.ROUNDED))

    def print_error(self, message: str) -> None:
        self.console.print(Panel(message, title="Error", border_style="bold red", box=box.ROUNDED))

    def print_final_summary(self, strategy, total_cost: float, total_turns: int, commits: list) -> None:
        done, total = strategy.progress()
        table = Table(title="Run Summary", show_header=True, header_style="bold cyan", box=box.ROUNDED)
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Mission", strategy.mission[:80] + ("..." if len(strategy.mission) > 80 else ""))
        table.add_row("Status", strategy.run_status)
        table.add_row("Tasks completed", f"{done}/{total}")
        table.add_row("Iterations", str(strategy.iteration))
        table.add_row("Commits made", str(len(commits)))
        table.add_row("Total turns", str(total_turns))
        table.add_row("Total cost (est.)", f"${total_cost:.4f}")
        self.console.print(table)

        if commits:
            self.console.print("\n[bold]Commits:[/bold]")
            for sha, msg in commits:
                self.console.print(f"  [dim]{sha[:8]}[/dim]  {msg}")

        blocked = strategy.blocked_tasks()
        if blocked:
            lines = [f"- {t.id}: {t.title}\n  {t.notes}" for t in blocked]
            self.console.print(
                Panel(
                    "\n".join(lines)
                    + "\n\nThese were parked, not retried forever, so the run could keep "
                    "going. Fix what's needed and re-run with --resume --retry-blocked.",
                    title=f"⚠  {len(blocked)} task(s) need human input",
                    border_style="bold yellow",
                    box=box.ROUNDED,
                )
            )

        if strategy.run_status == "rate_limited":
            self.console.print(
                Panel(
                    f"Resume after: {strategy.resume_after or 'unknown — try again later'}\n\n"
                    "Re-run with --resume once that's passed. Progress up to now is saved.",
                    title="⏳ Paused on a usage/rate limit",
                    border_style="bold red",
                    box=box.ROUNDED,
                )
            )
