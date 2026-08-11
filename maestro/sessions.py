"""
Session registry for `maestro run --detach`.

A "session" here is just a registry entry (id, project dir, PID, log file
path, started-at) pointing at an ordinary `maestro run --resume --yes`
subprocess running in the background. All the actual run state -- what
task it's on, whether it's paused, done, or blocked -- already lives in
that project's STRATEGY.md (see maestro/strategy.py); this module never
duplicates it, only tracks enough to find the process and its log again.

Deliberately not a daemon: liveness is just `os.kill(pid, 0)`, and
stopping a session is just sending it SIGINT -- the exact same signal a
foreground Ctrl-C sends, which main.py already handles by saving
STRATEGY.md and exiting cleanly. No new process-supervision model, no IPC.
"""

from __future__ import annotations

import json
import os
import signal
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from maestro.strategy import Strategy

CONFIG_DIR = Path.home() / ".config" / "maestro"
SESSIONS_PATH = CONFIG_DIR / "sessions.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    if not SESSIONS_PATH.exists():
        return {}
    try:
        return json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def new_session_id(project_dir: str) -> str:
    slug = Path(project_dir).name or "session"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def register(session_id: str, project_dir: str, pid: int, log_path: str) -> None:
    data = _load()
    data[session_id] = {
        "project_dir": project_dir,
        "pid": pid,
        "log_path": log_path,
        "started_at": _now_iso(),
    }
    _save(data)


def remove(session_id: str) -> bool:
    data = _load()
    if session_id not in data:
        return False
    del data[session_id]
    _save(data)
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by someone else
    return True


@dataclass
class SessionInfo:
    id: str
    project_dir: str
    pid: int
    log_path: str
    started_at: str
    alive: bool
    run_status: str = "unknown"
    progress: Tuple[int, int] = (0, 0)


def _to_info(session_id: str, entry: dict) -> SessionInfo:
    run_status = "unknown"
    progress = (0, 0)
    strategy_path = Path(entry["project_dir"]) / "STRATEGY.md"
    if strategy_path.exists():
        try:
            s = Strategy.load(str(strategy_path))
            run_status = s.run_status
            progress = s.progress()
        except (OSError, ValueError):
            pass
    return SessionInfo(
        id=session_id,
        project_dir=entry["project_dir"],
        pid=entry["pid"],
        log_path=entry["log_path"],
        started_at=entry.get("started_at", ""),
        alive=_pid_alive(entry["pid"]),
        run_status=run_status,
        progress=progress,
    )


def get_session(session_id: str) -> Optional[SessionInfo]:
    data = _load()
    entry = data.get(session_id)
    return _to_info(session_id, entry) if entry is not None else None


def list_sessions() -> List[SessionInfo]:
    data = _load()
    return [_to_info(sid, entry) for sid, entry in sorted(data.items())]


def stop_session(session_id: str) -> str:
    """Sends SIGINT (same as a foreground Ctrl-C) so the session saves its
    progress and exits cleanly. Returns a human-readable outcome."""
    info = get_session(session_id)
    if info is None:
        return f"No session {session_id!r} registered. See `maestro sessions list`."
    if not info.alive:
        return f"Session {session_id} (pid {info.pid}) is already stopped."
    os.kill(info.pid, signal.SIGINT)
    return f"Sent interrupt to session {session_id} (pid {info.pid}) — it will save progress and exit shortly."
