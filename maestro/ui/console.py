"""
Rich-based terminal UI for Maestro.

Why `rich` over `textual`: this is a linear, mostly-blocking pipeline (one
`claude -p` subprocess call runs to completion before the next agent
starts) rather than an app with concurrent input handling or navigable
screens. `rich.live.Live` gives a live-updating multi-panel layout with a
fraction of the code textual would need, and it degrades gracefully to
plain scrolling output when stdout isn't a real terminal (CI logs, piped
output). If a future agent needs concurrent panes or keyboard-driven
navigation, textual would be worth revisiting.

`MaestroUI` owns a `Live` region with two panels — a header (current
agent + live streamed activity summary + running stats) and a task
checklist (rendered from the Strategy) — kept small on purpose. Activity
log lines are NOT part of the Live redraw region: `log()` prints each one
straight through the live console proxy (`Live.console`), which appends to
the terminal's real, ordinary scrollback above the live-updating panels
instead of a fixed-height panel that erases old lines on every redraw.
That's what makes scrollback actually work — with everything crammed into
one Live-managed panel, lines that scroll off are gone for good (Live
erases and repaints its own region each frame; it doesn't just visually
push content upward), not just off-screen.

When stdout isn't a real terminal (piped output, redirected to a file,
CI logs), `Live` doesn't get to do incremental redraws at all — it ends up
batching everything into one dump at process exit. Rather than let that
happen, `live()` detects `not self.console.is_terminal` and skips
constructing a `Live` entirely; `log()`/`set_agent()`/`clear_agent()` fall
back to plain sequential `console.print()` calls, so output still streams
line-by-line as it happens.

Resize handling: panel sizes are recomputed from `self.console.size` fresh
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
import sys
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

# Hand-built 5-row block-letter banner (not figlet output) so the column
# alignment can be verified directly rather than trusted to a font file.
# Padded to a fixed 47-column width explicitly (rather than relying on
# literal trailing spaces surviving edits/whitespace-trimming) so every
# row is guaranteed the same length — see the diagram fix in README.md
# for why that verification matters here.
_BANNER_ROWS = [
    "█   █   ███   █████   ████  █████  ████    ███",
    "██ ██  █   █  █      █        █    █   █  █   █",
    "█ █ █  █████  ████    ███     █    ████   █   █",
    "█   █  █   █  █          █    █    █  █   █   █",
    "█   █  █   █  █████  ████     █    █   █   ███",
]
BANNER = "\n".join(row.ljust(49) for row in _BANNER_ROWS)

AGENT_COLORS = {
    "planner": "blue",
    "coder": "green",
    "researcher": "cyan",
    "tester": "bright_magenta",
    "reviewer": "yellow",
    "deep_reviewer": "bright_yellow",
    "system": "magenta",
}

STATUS_ICON = {
    "pending": "○",
    "in_progress": "◐",
    "pending_review": "◑",
    "done": "●",
    "rejected": "✗",
    "needs_human": "⚠",
}

STATUS_STYLE = {
    "done": "green",
    "in_progress": "yellow",
    "pending_review": "yellow",
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


class MaestroUI:
    """Owns all terminal output for a run."""

    def __init__(self, console: Optional[Console] = None, history_limit: int = 500, show_cost: bool = True):
        # force_terminal=True: a detached `maestro run --detach` writes its
        # stdout to a log file (see main.py's detach_run), and Rich's
        # default auto-detection drops all ANSI color codes for a
        # non-terminal destination -- meaning the log would be plain text
        # forever, colors gone even when `maestro sessions attach` replays
        # it to a real terminal afterward. Forcing it on keeps the color
        # bytes in the log so attach gets them back. This does NOT force
        # the interactive Live redraw region -- see self._interactive below,
        # which gates that separately based on the *real* terminal-ness of
        # the underlying stream, checked before anything here overrides it.
        self.console = console or Console(force_terminal=True)
        self._interactive = self.console.file.isatty() if hasattr(self.console.file, "isatty") else False
        self.history: list = []
        self.history_limit = history_limit
        self.show_cost = show_cost
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
        if not self._interactive:
            # Not a real terminal (piped, redirected to a file, CI logs) —
            # Live can't do incremental redraws here and ends up batching
            # everything into one dump at process exit instead. Skip it;
            # log()/set_agent()/clear_agent() already fall back to plain
            # sequential console.print() when self._live is None -- still
            # colored (see force_terminal above), just not redrawn in place.
            self._live = None
            yield self
            return

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

    def refresh(self) -> None:
        """Public entry point for a caller that mutates state this render
        depends on (e.g. `attach_strategy`) without going through one of
        the state-update methods above, which already refresh on their
        own -- `maestro sessions attach` polling STRATEGY.md is the
        motivating case, see main.py."""
        self._refresh()

    def live_console(self) -> Console:
        """Same routing `log()` uses internally: the Live region's own
        console when one is active (required so prints interleave with the
        live redraw instead of corrupting it), otherwise the plain
        console. Exposed for callers outside this module that need to
        print pre-rendered text alongside a live view -- see
        `maestro sessions attach` in main.py."""
        return self._live.console if self._live is not None else self.console

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
        if self._live is None:
            self.console.print(f"[bold {agent_color(agent)}]▶ {agent.upper()}[/bold {agent_color(agent)}] — {action}")
        else:
            self._refresh()

    def clear_agent(self) -> None:
        self.current_agent = None
        self.current_action = "idle"
        self._refresh()

    def log(self, agent: str, text: str) -> None:
        # Collapse to one line — long streamed snippets shouldn't dominate
        # the line, and no_wrap rendering below assumes single-line text.
        text = " ".join(text.split())
        self.history.append(LogLine(agent=agent, text=text))
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit:]

        color = agent_color(agent)
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"{agent:>9} │ ", style=f"bold {color}")
        line.append(text)
        # Print through the live console proxy (or the plain console when
        # there's no Live) so this lands in real, ordinary terminal
        # scrollback instead of the bounded, erase-and-repaint Live region.
        target = self._live.console if self._live is not None else self.console
        target.print(line)
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

        stats_text = f"calls: {self.total_calls}   turns: {self.total_turns}"
        if self.show_cost:
            cost_bit = f"${self.total_cost:.4f}" if self.total_cost else "$0.00"
            stats_text += f"   cost: {cost_bit}"
        stats = Text(stats_text, style="dim", no_wrap=True, overflow="ellipsis")
        border = agent_color(self.current_agent) if self.current_agent else "bright_black"
        return Panel(
            Group(status, stats),
            title="Maestro",
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

    def _render(self) -> Group:
        # header(4) + task panel (2 border rows + N task rows + optional
        # "N more" line). Live region only ever holds header + tasks now —
        # the activity log prints straight to real scrollback via log(), so
        # there's no shared height budget to juggle between two panels.
        term_h = self._term_height()
        header_h = 4  # 2 content lines + top/bottom border

        tasks_budget = max(term_h - header_h, 3)
        n_tasks = len(self.strategy.tasks) if self.strategy else 0
        reserve_for_more_line = 1 if n_tasks > 1 else 0
        task_rows = max(1, min(n_tasks or 1, 12, tasks_budget - 2 - reserve_for_more_line))

        return Group(
            self._render_header(),
            self._render_tasks(task_rows),
        )

    # -- one-off panels (used outside Live, or interleaved with it) ----

    def print_banner(self) -> None:
        """Printed once at the start of a fresh interactive mission intake
        (see main.py's prompt_mission) — never on --resume or a detached
        session's --resume --yes re-exec, so it never lands in a tailable
        session log or scripted/unattended output."""
        text = Text(BANNER, style="bold cyan")
        self.console.print(text)
        self.console.print(Text("autonomous Claude Code build loop", style="dim italic"))
        self.console.print()

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

    def print_plan_summary(self, tasks) -> None:
        table = Table(title="Plan", show_header=True, header_style="bold cyan", box=box.ROUNDED)
        table.add_column("Task")
        table.add_column("Agent")
        table.add_column("Title")
        for t in tasks:
            table.add_row(t.id, t.agent, t.title, style=agent_color(t.agent))
        self.console.print(table)

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
        if self.show_cost:
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
