#!/usr/bin/env python3
"""Pull Request lifecycle management (CTX-03)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from espaut.events import log_event, make_evidence, save_evidence
from espaut.gh_ops import pr_create, pr_list_json, pr_view_json
from espaut.git_ops import current_branch, head_sha
from espaut.store import load_config, read_json, write_json


def find_existing_pr(
    task: dict[str, Any],
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """Find an existing open PR matching the task's branch."""
    branch = task.get("branch_name")
    if not branch:
        return None

    if task.get("pr_number"):
        pr = pr_view_json(task["pr_number"], cwd=cwd)
        if pr and pr.get("state") in ("OPEN", "open"):
            return pr

    prs = pr_list_json(cwd=cwd)
    for pr in prs:
        if pr.get("headRefName") == branch:
            return pr
    return None


def create_or_reuse_pr(
    store: Path,
    task: dict[str, Any],
    title: str,
    body: str,
    base: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Create a new PR or reuse an existing one. Returns a result dict."""
    task_id = task["task_id"]

    existing = find_existing_pr(task, cwd)
    if existing:
        log_event(
            store,
            "pr_reused",
            task_id=task_id,
            object_reference=str(existing.get("number")),
            detail=existing.get("url", ""),
        )
        return {
            "status": "reused",
            "pr_number": existing.get("number"),
            "url": existing.get("url"),
            "pr": existing,
        }

    head = task.get("branch_name") or current_branch(cwd)
    pr = pr_create(title, body, base=base, head=head, cwd=cwd)
    if pr is None:
        log_event(
            store,
            "pr_creation_failed",
            task_id=task_id,
            outcome="FALHA",
        )
        return {"status": "error", "error": "gh pr create failed"}

    evidence = make_evidence(
        "github",
        str(pr.get("number")),
        f"PR created: {pr.get('url')}",
        task_id=task_id,
    )
    save_evidence(store, evidence)

    log_event(
        store,
        "pr_created",
        task_id=task_id,
        object_reference=str(pr.get("number")),
        evidence_id=evidence["evidence_id"],
    )

    return {
        "status": "created",
        "pr_number": pr.get("number"),
        "url": pr.get("url"),
        "pr": pr,
    }


def query_pr_state(
    pr_number: int,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Query the current state of a PR from GitHub."""
    pr = pr_view_json(
        pr_number,
        fields=[
            "number", "title", "headRefName", "baseRefName",
            "state", "url", "headRefOid", "mergeCommit",
            "mergedAt", "isDraft", "reviewDecision",
            "statusCheckRollup",
        ],
        cwd=cwd,
    )
    if pr is None:
        return {"status": "unavailable", "pr_number": pr_number}

    return {
        "status": "available",
        "pr_number": pr_number,
        "github_state": pr.get("state"),
        "head_sha": pr.get("headRefOid"),
        "head_branch": pr.get("headRefName"),
        "base_branch": pr.get("baseRefName"),
        "url": pr.get("url"),
        "is_draft": pr.get("isDraft", False),
        "review_decision": pr.get("reviewDecision"),
        "merged_at": pr.get("mergedAt"),
        "merge_commit": pr.get("mergeCommit"),
    }


def reconcile_pr_state(
    store: Path,
    task: dict[str, Any],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Reconcile local task state with remote PR state.

    Returns a report of what changed.
    """
    task_id = task["task_id"]
    pr_number = task.get("pr_number")
    if not pr_number:
        return {"status": "no_pr", "task_id": task_id}

    remote = query_pr_state(pr_number, cwd)
    if remote["status"] == "unavailable":
        log_event(
            store,
            "pr_unavailable",
            task_id=task_id,
            object_reference=str(pr_number),
        )
        return {"status": "unavailable", "task_id": task_id, "pr_number": pr_number}

    changes: list[str] = []

    if remote.get("github_state") in ("CLOSED", "closed"):
        if remote.get("merged_at"):
            changes.append("pr_merged_externally")
        else:
            changes.append("pr_closed_externally")

    local_sha = task.get("head_commit_sha")
    remote_sha = remote.get("head_sha")
    if local_sha and remote_sha and local_sha != remote_sha:
        changes.append("head_sha_diverged")

    if changes:
        log_event(
            store,
            "pr_state_reconciled",
            task_id=task_id,
            object_reference=str(pr_number),
            detail="; ".join(changes),
        )

    return {
        "status": "reconciled",
        "task_id": task_id,
        "pr_number": pr_number,
        "remote": remote,
        "changes": changes,
    }


def update_task_pr(
    task: dict[str, Any],
    pr_number: int,
    url: str | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Update task record with PR information."""
    task["pr_number"] = pr_number
    if url:
        task["pr_url"] = url
    if head_sha:
        task["head_commit_sha"] = head_sha
    return task
