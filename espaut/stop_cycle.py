#!/usr/bin/env python3
"""Stop-hook continuation: preserve, open/reuse PR, reconcile reviews/checks, gated merge.

Each Stop is a single reconciliation tick. Pending checks/reviews are retried at
the next Stop; they are never interpreted as approval.
"""
from pathlib import Path

from espaut.checks import query_checks
from espaut.events import log_event
from espaut.gh_ops import pr_view_json, run_gh
from espaut.git_ops import current_branch, has_incomplete_operation, head_sha, worktree_status
from espaut.governance import evaluate_merge_eligibility
from espaut.preservation import run_preservation
from espaut.pullrequest import create_or_reuse_pr, query_pr_state, update_task_pr
from espaut.review import collect_reviews, extract_pendencies
from espaut.store import load_config, read_json, write_json


def checkpoint(store, task_id, status, **details):
    report = {"task_id": task_id, "status": status, **details}
    write_json(store / "cache" / f"stop-cycle-{task_id}.json", report)
    log_event(store, "stop_cycle", task_id=task_id, outcome=status,
              detail=str(details.get("reason", ""))[:200])
    return report


def human_approved(task, decisions, sha):
    return any(
        d.get("task_id") == task["task_id"]
        and d.get("decision_type") == "merge_authorization"
        and d.get("decision_value") == "approved"
        and bool(d.get("authority"))
        and (d.get("scope") == sha or d.get("revision") == sha)
        for d in decisions
    )


def run_stop_cycle(store: Path, task: dict, *, cwd: str, preserve_changes=True):
    """Execute only explicitly authorized steps, never accept unknown as PASS."""
    task_id = task["task_id"]
    cfg = load_config(store)
    branch = current_branch(cwd)
    if not branch or branch in ("main", "master") or branch != task.get("branch_name"):
        return checkpoint(store, task_id, "blocked", reason="wrong or protected branch")
    if has_incomplete_operation(cwd):
        return checkpoint(store, task_id, "blocked", reason="unfinished Git operation")
    patterns = cfg.get("eligible_patterns")
    if cfg.get("auto_commit") is not True or not isinstance(patterns, list) or not patterns:
        return checkpoint(store, task_id, "monitor_only", reason="explicit project opt-in required")
    try:
        changes = worktree_status(cwd)
        if changes:
            if not preserve_changes:
                return checkpoint(store, task_id, "blocked", reason="no verified pre-task snapshot")
            result = run_preservation(store, task_id, f"espaut: post-reply {task_id}",
                                      cwd=cwd, dry_run=False)
            if result.get("outcome") != "synchronized":
                return checkpoint(store, task_id, "preservation_pending",
                                  reason=result.get("outcome", "unverified"))
        sha = head_sha(cwd)
        if not sha:
            return checkpoint(store, task_id, "blocked", reason="HEAD unavailable")
        pr_number = task.get("pr_number")
        if not pr_number:
            if cfg.get("auto_pr") is not True:
                return checkpoint(store, task_id, "pr_pending_authorization",
                                  revision=sha, reason="auto_pr must be true")
            pr = create_or_reuse_pr(
                store, task, task.get("title") or task_id,
                f"Task: {task_id}\n\n{task.get('objective', '')}",
                base=cfg.get("pr_base_branch") or "main", cwd=cwd)
            pr_number = pr.get("pr_number")
            if pr.get("status") not in ("created", "reused") or not pr_number:
                return checkpoint(store, task_id, "pr_pending",
                                  revision=sha, reason=pr.get("error", "PR not confirmed"))
            update_task_pr(task, int(pr_number), pr.get("url"), head_sha=sha)
            write_json(store / "tasks" / f"{task_id}.json", task)
        pr_number = int(pr_number)
        remote = query_pr_state(pr_number, cwd=cwd)
        if remote.get("status") != "available":
            return checkpoint(store, task_id, "pr_pending", pr_number=pr_number,
                              reason="PR query unavailable")
        if remote.get("merged_at"):
            return checkpoint(store, task_id, "merged", pr_number=pr_number,
                              merge_commit=remote.get("merge_commit"))
        if remote.get("github_state") != "OPEN" or remote.get("head_sha") != sha:
            return checkpoint(store, task_id, "blocked", pr_number=pr_number,
                              revision=sha, reason="PR closed or remote head mismatch")
        checks = query_checks(store, task_id, pr_number, evaluated_revision=sha, cwd=cwd)
        reviews = collect_reviews(store, task_id, pr_number, cwd=cwd)
        reviews.update(is_draft=bool(remote.get("is_draft")),
                       review_decision=remote.get("review_decision"))
        pending = extract_pendencies(reviews, task_id=task_id, store=store)
        decisions = [read_json(p) for p in sorted((store / "decisions").glob("*.json"))]
        gate = evaluate_merge_eligibility(store, task, checks, reviews, pending,
                                           decisions, cwd=cwd)
        gates = {c["criterion"]: c["status"] for c in gate["criteria"]}
        if gate["verdict"] != "APTA_PARA_INTEGRACAO" or remote.get("review_decision") != "APPROVED":
            return checkpoint(store, task_id, "review_pending", pr_number=pr_number,
                              revision=sha, checks=checks.get("aggregate"),
                              pendencies=len(pending), gates=gates,
                              reason="review/CI/acceptance/merge gate pending")
        if cfg.get("auto_merge") is not True or not human_approved(task, decisions, sha):
            return checkpoint(store, task_id, "merge_pending_authorization",
                              pr_number=pr_number, revision=sha, gates=gates,
                              reason="per-revision human approval and auto_merge required")
        # GitHub HEAD must remain exactly the reviewed revision.
        current = pr_view_json(pr_number, ["state", "isDraft", "headRefOid", "mergeStateStatus"], cwd=cwd)
        if (not current or current.get("state") != "OPEN"
                or current.get("isDraft") is not False
                or current.get("headRefOid") != sha
                or current.get("mergeStateStatus") != "CLEAN"):
            return checkpoint(store, task_id, "merge_pending", pr_number=pr_number,
                              revision=sha, reason="PR draft/head/mergeability changed")
        method = cfg.get("merge_method", "merge")
        if method not in ("merge", "squash", "rebase"):
            return checkpoint(store, task_id, "blocked", reason="unsupported merge method")
        response = run_gh("pr", "merge", str(pr_number), f"--{method}",
                          "--match-head-commit", sha, cwd=cwd)
        if response.returncode != 0:
            return checkpoint(store, task_id, "merge_pending", pr_number=pr_number,
                              revision=sha, reason=f"GitHub rejected merge: exit={response.returncode}")
        verified = query_pr_state(pr_number, cwd=cwd)
        if not verified.get("merged_at") or not verified.get("merge_commit"):
            return checkpoint(store, task_id, "merge_unverified", pr_number=pr_number,
                              revision=sha, reason="GitHub did not confirm merge")
        return checkpoint(store, task_id, "merged", pr_number=pr_number,
                          revision=sha, merge_commit=verified["merge_commit"])
    except Exception as exc:
        return checkpoint(store, task_id, "operational_failure",
                          reason=f"{type(exc).__name__}: {str(exc)[:160]}")
