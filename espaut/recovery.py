#!/usr/bin/env python3
"""Session recovery: reconstruct state from event log and git/gh queries (CTX-08)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from espaut.events import log_event
from espaut.git_ops import current_branch, has_changes, head_sha, is_git_repo, remote_head_sha
from espaut.store import read_json, read_jsonl


def recover_task_state(
    store: Path,
    task: dict[str, Any],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Reconcile a task's recorded state with actual git/gh state.

    Returns a report of discrepancies and recommended actions.
    """
    task_id = task["task_id"]
    discrepancies: list[dict[str, str]] = []
    actions: list[str] = []

    if not is_git_repo(cwd):
        return {
            "task_id": task_id,
            "status": "error",
            "error": "not a git repository",
        }

    actual_branch = current_branch(cwd)
    recorded_branch = task.get("branch_name")
    if recorded_branch and actual_branch != recorded_branch:
        discrepancies.append({
            "field": "branch",
            "recorded": recorded_branch,
            "actual": actual_branch or "(detached)",
        })
        actions.append(f"checkout branch {recorded_branch}")

    actual_sha = head_sha(cwd)
    recorded_shas = task.get("commit_shas", [])
    if recorded_shas and actual_sha and actual_sha not in recorded_shas:
        discrepancies.append({
            "field": "head_sha",
            "recorded": recorded_shas[-1] if recorded_shas else "none",
            "actual": actual_sha,
        })
        actions.append("verify new commits belong to this task")

    remote_sha = remote_head_sha("origin", actual_branch, cwd) if actual_branch else None
    remote_status = "no_remote" if remote_sha is None else (
        "synchronized" if remote_sha == actual_sha else "diverged"
    )
    if remote_status == "diverged":
        discrepancies.append({
            "field": "remote_sync", "recorded": remote_sha, "actual": actual_sha or "unknown",
        })
        actions.append("verify remote state and preserve local commit before any retry")

    if has_changes(cwd):
        discrepancies.append({
            "field": "worktree",
            "recorded": "clean",
            "actual": "dirty",
        })
        actions.append("run 'espaut preserve' to commit pending changes")

    log_event(
        store,
        "task_recovery_check",
        task_id=task_id,
        detail=f"discrepancies={len(discrepancies)}",
    )

    return {
        "task_id": task_id,
        "status": "recovered" if not discrepancies else "discrepancies_found",
        "discrepancies": discrepancies,
        "actions": actions,
        "actual_branch": actual_branch,
        "actual_sha": actual_sha,
        "remote_sha": remote_sha,
        "remote_status": remote_status,
    }


def reconstruct_timeline(
    store: Path,
    task_id: str,
) -> list[dict[str, Any]]:
    """Reconstruct the event timeline for a task from the event log."""
    events_path = store / "events.jsonl"
    if not events_path.exists():
        return []

    all_events = read_jsonl(events_path)
    return [e for e in all_events if e.get("task_id") == task_id]


def find_last_checkpoint(
    store: Path,
    task_id: str,
) -> dict[str, Any] | None:
    """Find the most recent event with a recoverable reference for a task."""
    timeline = reconstruct_timeline(store, task_id)
    for event in reversed(timeline):
        if event.get("object_reference") or event.get("evidence_id"):
            return event
    return None


def audit_task(
    store: Path,
    task: dict[str, Any],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Generate an audit report for a task: events, commits, evidence."""
    task_id = task["task_id"]
    timeline = reconstruct_timeline(store, task_id)

    evidence_dir = store / "evidence"
    evidence_files = list(evidence_dir.glob("EVD-*.json")) if evidence_dir.exists() else []
    task_evidence = []
    for ef in evidence_files:
        ev = read_json(ef)
        if ev.get("task_id") == task_id:
            task_evidence.append(ev)

    decisions_dir = store / "decisions"
    decision_files = list(decisions_dir.glob("DEC-*.json")) if decisions_dir.exists() else []
    task_decisions = []
    for df in decision_files:
        dec = read_json(df)
        if dec.get("task_id") == task_id:
            task_decisions.append(dec)

    return {
        "task_id": task_id,
        "state": task.get("state"),
        "event_count": len(timeline),
        "evidence_count": len(task_evidence),
        "decision_count": len(task_decisions),
        "commits": task.get("commit_shas", []),
        "pr_number": task.get("pr_number"),
        "timeline": timeline,
        "evidence": task_evidence,
        "decisions": task_decisions,
    }


def check_idempotency(
    store: Path,
    task_id: str,
    operation_type: str,
    operation_reference: str,
) -> tuple[bool, str]:
    """Check if an operation has already been completed for this task.

    Returns (already_done, detail).
    """
    timeline = reconstruct_timeline(store, task_id)
    for event in timeline:
        if (
            event.get("event_type") == operation_type
            and event.get("object_reference") == operation_reference
            and event.get("outcome") in ("SUCESSO", "synchronized", "committed")
        ):
            return True, f"operation {operation_type} with ref {operation_reference} already completed at {event.get('occurred_at')}"
    return False, "operation not yet completed"
