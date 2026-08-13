"""
Human-facing input for Maestro's CLI: the single place every interactive
prompt (mission entry, clarifying questions, confirm menus, blocked/paused
guidance) goes through, built on `prompt_toolkit` instead of bare
`input()`. Gives every prompt multiline editing (Esc+Enter or Alt+Enter to
submit), persistent history (shared across runs and prompts, at
~/.config/maestro/input_history), and a tiny fixed set of slash commands
(/quit, /help, /status) -- not a generic command registry, just enough to
orient or bail out without hunting for Ctrl-C.

Kept deliberately separate from rich-based `MaestroUI` (console.py):
prompt_toolkit and rich.Live both want raw control of the terminal, so
every prompt here temporarily stops any active Live region via
ui.stop_live()/resume_live() first (see MaestroUI's docstring) rather than
trying to run concurrently with it.
"""

from __future__ import annotations

import sys
from typing import Callable, Dict, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer
from prompt_toolkit.history import FileHistory

from maestro.sessions import CONFIG_DIR

HISTORY_PATH = CONFIG_DIR / "input_history"

_session: Optional[PromptSession] = None


class InputAborted(Exception):
    """Raised when /quit is typed at any prompt. Deliberately left uncaught
    by every mission-intake/prompt helper so it propagates straight up to
    main()'s outer handler, the same way KeyboardInterrupt already does."""


def _get_session() -> PromptSession:
    global _session
    if _session is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _session = PromptSession(history=FileHistory(str(HISTORY_PATH)))
    return _session


def _drain_stdin() -> None:
    """A blocking network call (clarify_mission/enhance_mission, or just
    Claude thinking) gives no feedback of its own turnaround time, so users
    mash Enter/Shift-Enter while waiting -- without this, prompt_toolkit
    reads those buffered keystrokes as if they'd been typed at the next
    prompt. Best-effort POSIX only (termios); no-ops elsewhere since
    there's nothing queued to drain there."""
    try:
        import termios
    except ImportError:
        return  # not POSIX (e.g. Windows) -- nothing to drain the same way
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, ValueError, OSError):
        pass


def _help_text(help_text: Optional[str], on_status: Optional[Callable[[], None]]) -> str:
    lines = [help_text] if help_text else []
    lines.append("/quit — abort")
    if on_status is not None:
        lines.append("/status — show current progress")
    lines.append("/help — this message")
    return "\n".join(lines)


def ask_text(
    ui,
    message: str,
    *,
    default: str = "",
    multiline: bool = False,
    on_status: Optional[Callable[[], None]] = None,
    completer: Optional[Completer] = None,
    help_text: Optional[str] = None,
) -> str:
    """Single entry point for every free-text prompt. Always honors /quit
    (raises InputAborted) and /help; /status additionally works wherever
    the caller passes on_status. Empty input (bare Enter) returns
    `default`."""
    if not getattr(ui, "_interactive", False):
        raise RuntimeError(f"ask_text({message!r}) called on a non-interactive session")

    _drain_stdin()
    session = _get_session()
    ui.stop_live()
    try:
        while True:
            try:
                raw = session.prompt(message, default="", multiline=multiline, completer=completer)
            except EOFError:
                return default
            text = raw.strip()
            if text == "/quit":
                raise InputAborted()
            if text == "/help":
                ui.console.print(_help_text(help_text, on_status))
                continue
            if text == "/status" and on_status is not None:
                on_status()
                continue
            return text or default
    finally:
        ui.resume_live()


def ask_choice(ui, message: str, choices: Dict[str, str], default: str) -> str:
    """Single-key menu prompt, e.g. {"y": "Yes", "n": "No"}. Invalid input
    (or bare Enter) falls back to `default`, matching the behavior the raw
    input() call sites this replaces already had."""
    help_text = "\n".join(f"[{k}] {v}" for k, v in choices.items())
    raw = ask_text(ui, message, default=default, help_text=help_text)
    choice = raw.strip().lower()
    valid = {k.lower() for k in choices}
    return choice if choice in valid else default.lower()


def demo() -> None:
    """Self-check: `python -m maestro.ui.input` exercises the slash-command
    dispatch and default-coercion logic against simulated keystrokes, no
    real terminal needed (prompt_toolkit's own pipe-input test utility)."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    global _session

    class _FakeConsole:
        def print(self, *a, **k):
            pass

    class _FakeUI:
        _interactive = True
        console = _FakeConsole()

        def stop_live(self):
            pass

        def resume_live(self):
            pass

    def run_with_input(text: str, fn):
        global _session
        with create_pipe_input() as pipe_input:
            pipe_input.send_text(text)
            _session = PromptSession(input=pipe_input, output=DummyOutput())
            return fn()

    try:
        run_with_input("/quit\n", lambda: ask_text(_FakeUI(), "> ", default="fallback"))
        raise AssertionError("/quit should have raised InputAborted")
    except InputAborted:
        pass

    result = run_with_input("\n", lambda: ask_text(_FakeUI(), "> ", default="fallback"))
    assert result == "fallback", result

    result = run_with_input("hi there\n", lambda: ask_text(_FakeUI(), "> ", default="fallback"))
    assert result == "hi there", result

    choice = run_with_input("zzz\n", lambda: ask_choice(_FakeUI(), "pick", {"y": "Yes", "n": "No"}, default="y"))
    assert choice == "y", choice

    choice = run_with_input("n\n", lambda: ask_choice(_FakeUI(), "pick", {"y": "Yes", "n": "No"}, default="y"))
    assert choice == "n", choice

    _session = None
    print("maestro/ui/input.py self-check OK")


if __name__ == "__main__":
    demo()
