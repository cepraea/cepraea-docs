#!/usr/bin/env python3
"""Git CLI wrappers using parameterized subprocess calls."""

from __future__ import annotations

import subprocess
from typing import Any

from espaut.security import validate_subprocess_args


def run_git(
    *args: str,
    timeout: int = 30,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with validated arguments.

    Never uses ``shell=True``. Arguments are validated against shell
    metacharacters before execution.
    """
    cmd = ["git", *args]
    validate_subprocess_args(cmd)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=cwd,
    )


def is_git_repo(cwd: str | None = None) -> bool:
    """Return True if *cwd* (default: current directory) is inside a git repo."""
    r = run_git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    return r.returncode == 0 and r.stdout.strip() == "true"


def repo_root(cwd: str | None = None) -> str | None:
    """Return the absolute path to the repository root, or None."""
    r = run_git("rev-parse", "--show-toplevel", cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def current_branch(cwd: str | None = None) -> str | None:
    """Return the current branch name, or None if detached HEAD."""
    r = run_git("branch", "--show-current", cwd=cwd)
    name = r.stdout.strip()
    return name if r.returncode == 0 and name else None


def head_sha(cwd: str | None = None) -> str | None:
    """Return the full SHA of HEAD."""
    r = run_git("rev-parse", "HEAD", cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def worktree_status(cwd: str | None = None) -> list[dict[str, str]]:
    """Return parsed ``git status --porcelain=v1`` output.

    Each entry has keys ``index``, ``worktree``, and ``path``.
    """
    r = run_git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"git status failed: {r.stderr[:300]}")
    entries: list[dict[str, str]] = []
    parts = r.stdout.split("\0")
    i = 0
    while i < len(parts):
        line = parts[i]
        i += 1
        if len(line) < 4:
            continue
        entry = {"index": line[0], "worktree": line[1], "path": line[3:]}
        if "R" in (line[0], line[1]) or "C" in (line[0], line[1]):
            if i < len(parts):
                entry["old_path"] = parts[i]
                i += 1
        entries.append(entry)
    return entries


def has_changes(cwd: str | None = None) -> bool:
    """Return True if there are uncommitted changes (staged or unstaged)."""
    return len(worktree_status(cwd)) > 0


def staged_diff(cwd: str | None = None) -> str:
    """Return the diff of the staging area."""
    r = run_git("diff", "--cached", cwd=cwd)
    return r.stdout if r.returncode == 0 else ""


def staged_files(cwd: str | None = None) -> list[str]:
    """Return the list of files in the staging area."""
    r = run_git("diff", "--cached", "--name-only", "-z", cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"git diff --cached failed: {r.stderr[:300]}")
    return [f for f in r.stdout.split("\0") if f]


def stage_files(paths: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Stage specific files for commit."""
    return run_git("add", "--", *paths, cwd=cwd)


def create_commit(
    message: str,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Create a commit with the given message."""
    return run_git("commit", "-m", message, cwd=cwd)


def push(
    remote: str = "origin",
    branch: str | None = None,
    cwd: str | None = None,
    set_upstream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Push to remote. Optionally set upstream tracking."""
    args = ["push"]
    if set_upstream:
        args.append("-u")
    args.append(remote)
    actual_branch = branch or current_branch(cwd)
    if not actual_branch:
        raise RuntimeError("cannot push detached HEAD without an explicit branch")
    args.append(actual_branch)
    return run_git(*args, cwd=cwd, timeout=60)


def remote_head_sha(
    remote: str = "origin",
    branch: str | None = None,
    cwd: str | None = None,
) -> str | None:
    """Fetch and return the SHA of a remote branch tip."""
    if branch is None:
        branch = current_branch(cwd)
    if branch is None:
        return None
    ref = f"{remote}/{branch}"
    r = run_git("ls-remote", remote, f"refs/heads/{branch}", cwd=cwd, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0]


def branch_exists(name: str, cwd: str | None = None) -> bool:
    """Return True if a local branch with *name* exists."""
    r = run_git("rev-parse", "--verify", f"refs/heads/{name}", cwd=cwd)
    return r.returncode == 0


def create_branch(
    name: str,
    start_point: str | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Create a new branch."""
    args = ["branch", name]
    if start_point:
        args.append(start_point)
    return run_git(*args, cwd=cwd)


def checkout_branch(name: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Switch to an existing branch."""
    return run_git("checkout", name, cwd=cwd)


def is_ref_valid(ref: str, cwd: str | None = None) -> bool:
    """Return True if *ref* resolves to a valid git object."""
    r = run_git("rev-parse", "--verify", ref, cwd=cwd)
    return r.returncode == 0


def has_incomplete_operation(cwd: str | None = None) -> str | None:
    """Check for incomplete git operations (rebase, merge, cherry-pick).

    Returns the operation name if one is in progress, None otherwise.
    """
    r = run_git("rev-parse", "--git-dir", cwd=cwd)
    if r.returncode != 0:
        return None
    from pathlib import Path
    git_dir = Path(r.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = Path(cwd or ".") / git_dir
    for op, marker in [
        ("rebase", "rebase-merge"),
        ("rebase", "rebase-apply"),
        ("merge", "MERGE_HEAD"),
        ("cherry-pick", "CHERRY_PICK_HEAD"),
        ("bisect", "BISECT_LOG"),
    ]:
        if (git_dir / marker).exists():
            return op
    return None
