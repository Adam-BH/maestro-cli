"""
Small helpers around the git CLI: clean-tree checks, commits, and diff
summaries. Deliberately not a git library binding — Maestro only needs a
handful of plumbing commands and shelling out keeps the dependency
footprint small.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional


class GitError(RuntimeError):
    pass


def _run(args: List[str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def is_git_repo(cwd: Optional[str] = None) -> bool:
    proc = _run(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_clean(cwd: Optional[str] = None) -> bool:
    proc = _run(["status", "--porcelain"], cwd=cwd)
    return proc.stdout.strip() == ""


def current_branch(cwd: Optional[str] = None) -> str:
    # `rev-parse --abbrev-ref HEAD` fails on a brand-new repo with zero
    # commits ("unborn" branch — HEAD doesn't resolve to a commit yet).
    # `symbolic-ref` reads the ref name itself and works either way.
    proc = _run(["symbolic-ref", "--short", "HEAD"], cwd=cwd, check=False)
    if proc.returncode == 0:
        return proc.stdout.strip()
    return "(unborn)"


def short_status(cwd: Optional[str] = None) -> str:
    proc = _run(["status", "--porcelain"], cwd=cwd)
    return proc.stdout.strip()


def stash(cwd: Optional[str] = None, message: str = "Maestro autostash") -> bool:
    proc = _run(["stash", "push", "-u", "-m", message], cwd=cwd, check=False)
    return proc.returncode == 0


def commit_all(message: str, cwd: Optional[str] = None) -> Optional[str]:
    """Stage everything and commit. Returns the new commit SHA, or None if
    there was nothing to commit."""
    _run(["add", "-A"], cwd=cwd)
    if is_clean(cwd=cwd):
        return None
    _run(["commit", "-m", message], cwd=cwd)
    return _run(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()


def commit_paths(paths: List[str], message: str, cwd: Optional[str] = None) -> Optional[str]:
    """Stage and commit only the given paths, leaving any other dirty/staged
    state in the working tree untouched. Used to checkpoint STRATEGY.md on
    every save regardless of whatever (possibly incomplete, unapproved)
    code changes a Coder/Tester attempt has left lying around — those still
    only get committed via commit_all, at approval or --commit-every-attempt.
    Returns the new commit SHA, or None if those paths had nothing to commit."""
    _run(["add", "--", *paths], cwd=cwd)
    unstaged = _run(["diff", "--cached", "--quiet", "--", *paths], cwd=cwd, check=False)
    if unstaged.returncode == 0:
        return None
    _run(["commit", "-m", message, "--", *paths], cwd=cwd)
    return _run(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()


def diff_stat(cwd: Optional[str] = None, ref: str = "HEAD") -> str:
    proc = _run(["diff", "--stat", ref], cwd=cwd, check=False)
    return proc.stdout.strip()


def diff_since(ref: str, cwd: Optional[str] = None) -> str:
    proc = _run(["diff", ref], cwd=cwd, check=False)
    return proc.stdout


def head_sha(cwd: Optional[str] = None) -> str:
    return _run(["rev-parse", "HEAD"], cwd=cwd, check=False).stdout.strip()


def fetch(cwd: Optional[str] = None, remote: str = "origin") -> bool:
    proc = _run(["fetch", remote], cwd=cwd, check=False)
    return proc.returncode == 0


def pull_ff_only(cwd: Optional[str] = None, remote: str = "origin", branch: str = "main") -> tuple:
    """Fast-forward only -- refuses to merge or rebase over local commits,
    so `maestro update` can never silently rewrite history on top of
    changes the caller didn't know were there. Returns (ok, message)."""
    proc = _run(["pull", "--ff-only", remote, branch], cwd=cwd, check=False)
    text = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, text


def worktree_add(worktree_path: str, branch: str, base: str = "main", cwd: Optional[str] = None) -> tuple:
    """Creates a new branch off `base` and checks it out into a fresh
    working directory at `worktree_path`, sharing the same object store as
    `cwd`'s repo -- so a parallel task running there can commit/build
    independently without a second process touching `cwd`'s own working
    tree. Returns (ok, message)."""
    proc = _run(["worktree", "add", "-b", branch, worktree_path, base], cwd=cwd, check=False)
    text = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, text


def push(branch: str, cwd: Optional[str] = None, remote: str = "origin") -> tuple:
    proc = _run(["push", "-u", remote, branch], cwd=cwd, check=False)
    text = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, text


@dataclass
class RepoCheck:
    is_repo: bool
    clean: bool
    branch: str = ""
    status_text: str = ""


def check_repo(cwd: Optional[str] = None) -> RepoCheck:
    if not is_git_repo(cwd=cwd):
        return RepoCheck(is_repo=False, clean=False)
    return RepoCheck(
        is_repo=True,
        clean=is_clean(cwd=cwd),
        branch=current_branch(cwd=cwd),
        status_text=short_status(cwd=cwd),
    )
