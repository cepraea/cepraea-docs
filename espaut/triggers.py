#!/usr/bin/env python3
"""Triggers, synchronization, and conflict detection (REQ-TRG, REQ-SYN, REQ-CON)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from espaut.events import log_event
from espaut.git_ops import current_branch, head_sha, remote_head_sha, run_git
from espaut.store import load_config


def check_sync_state(
    store: Path,
    task_id: str,
    remote: str = "origin",
    branch: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Verify sync between local and remote (REQ-SYN-001/002)."""
    local = head_sha(cwd)
    br = branch or current_branch(cwd)
    remote_sha = remote_head_sha(remote, br, cwd)
    if remote_sha:
        local_object = run_git("cat-file", "-e", f"{remote_sha}^{{commit}}", cwd=cwd)
        if local_object.returncode != 0:
            result = run_git(
                "fetch", "--no-tags", remote,
                f"refs/heads/{br}:refs/remotes/{remote}/{br}", cwd=cwd, timeout=60,
            )
            if result.returncode != 0:
                return {"status": "error", "error": "failed to fetch remote tracking revision"}

    if not local:
        return {"status": "error", "error": "cannot determine local HEAD"}
    if not remote_sha:
        return {
            "status": "no_remote",
            "local_sha": local,
            "branch": br,
        }

    if local == remote_sha:
        return {
            "status": "synchronized",
            "local_sha": local,
            "remote_sha": remote_sha,
            "branch": br,
        }

    r = run_git("merge-base", local, remote_sha, cwd=cwd)
    merge_base = r.stdout.strip() if r.returncode == 0 else None

    if merge_base == remote_sha:
        return {
            "status": "local_ahead",
            "local_sha": local,
            "remote_sha": remote_sha,
            "branch": br,
        }

    if merge_base == local:
        return {
            "status": "remote_ahead",
            "local_sha": local,
            "remote_sha": remote_sha,
            "branch": br,
        }

    log_event(
        store, "sync_divergence",
        task_id=task_id,
        detail=f"local={local[:8]} remote={remote_sha[:8]} base={merge_base[:8] if merge_base else '?'}",
    )
    return {
        "status": "diverged",
        "local_sha": local,
        "remote_sha": remote_sha,
        "merge_base": merge_base,
        "branch": br,
    }


def check_base_branch_updated(
    store: Path,
    task_id: str,
    base_branch: str,
    last_known_base_sha: str | None = None,
    remote: str = "origin",
    cwd: str | None = None,
) -> dict[str, Any]:
    """Check if the base (target) branch has been updated (REQ-CON-001)."""
    current_base_sha = remote_head_sha(remote, base_branch, cwd)
    if not current_base_sha:
        return {"status": "unknown", "base_branch": base_branch}

    if last_known_base_sha and current_base_sha != last_known_base_sha:
        log_event(
            store, "base_branch_updated",
            task_id=task_id,
            detail=f"base={base_branch} old={last_known_base_sha[:8]} new={current_base_sha[:8]}",
        )
        return {
            "status": "updated",
            "base_branch": base_branch,
            "previous_sha": last_known_base_sha,
            "current_sha": current_base_sha,
        }

    return {
        "status": "unchanged",
        "base_branch": base_branch,
        "current_sha": current_base_sha,
    }


def check_merge_conflict(
    task_id: str,
    base_branch: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Check for merge conflicts without actually merging (REQ-CON-002)."""
    refreshed = run_git(
        "fetch", "--no-tags", "origin",
        f"refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
        cwd=cwd, timeout=60,
    )
    if refreshed.returncode != 0:
        return {"status": "unknown", "error": "base branch fetch failed"}
    r = run_git("merge-tree", "--write-tree", f"origin/{base_branch}", "HEAD", cwd=cwd, timeout=30)

    if r.returncode == 0:
        return {"status": "no_conflict", "base_branch": base_branch}

    if "CONFLICT" in r.stdout or r.returncode != 0:
        conflict_files: list[str] = []
        for line in r.stdout.splitlines():
            if line.startswith("CONFLICT"):
                conflict_files.append(line)
        return {
            "status": "conflict",
            "base_branch": base_branch,
            "conflicts": conflict_files[:20],
        }

    return {"status": "unknown", "base_branch": base_branch}


def detect_pending_preservation(
    store: Path,
    task_id: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Check for unpreserved changes at task boundary (REQ-TRG-002)."""
    from espaut.git_ops import has_changes, worktree_status

    if not has_changes(cwd):
        return {"status": "clean", "task_id": task_id}

    entries = worktree_status(cwd)
    config = load_config(store)
    eligible_patterns = config.get("eligible_patterns", [])

    from espaut.preservation import classify_changes
    eligible, excluded = classify_changes(entries, eligible_patterns)

    if eligible:
        log_event(
            store, "unpreserved_changes_detected",
            task_id=task_id,
            detail=f"eligible={len(eligible)}",
        )
        return {
            "status": "pending",
            "task_id": task_id,
            "eligible": eligible,
            "excluded": excluded,
        }

    return {"status": "clean", "task_id": task_id}
