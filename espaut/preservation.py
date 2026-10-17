#!/usr/bin/env python3
"""Change detection, staging, commit, push — preservation state machine (SM #4)."""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

from espaut.engine import StateMachine
from espaut.events import log_event, make_evidence, save_evidence
from espaut.git_ops import (
    create_commit,
    has_changes,
    head_sha,
    push,
    remote_head_sha,
    stage_files,
    staged_diff,
    staged_files,
    worktree_status,
)
from espaut.store import load_config, read_json, write_json


class PreservationState(enum.StrEnum):
    LIMPO = "LIMPO"
    MODIFICACAO_DETECTADA = "MODIFICACAO_DETECTADA"
    PREPARACAO_PENDENTE = "PREPARACAO_PENDENTE"
    PREPARADA = "PREPARADA"
    COMMITADA_LOCALMENTE = "COMMITADA_LOCALMENTE"
    PUSH_PENDENTE = "PUSH_PENDENTE"
    SINCRONIZADA = "SINCRONIZADA"
    CONFLITO = "CONFLITO"
    FALHA = "FALHA"


PRESERVATION_TRANSITIONS = {
    (PreservationState.LIMPO, "detect"): PreservationState.MODIFICACAO_DETECTADA,
    (PreservationState.MODIFICACAO_DETECTADA, "prepare"): PreservationState.PREPARACAO_PENDENTE,
    (PreservationState.PREPARACAO_PENDENTE, "stage"): PreservationState.PREPARADA,
    (PreservationState.PREPARADA, "commit"): PreservationState.COMMITADA_LOCALMENTE,
    (PreservationState.COMMITADA_LOCALMENTE, "push"): PreservationState.PUSH_PENDENTE,
    (PreservationState.PUSH_PENDENTE, "push_confirmed"): PreservationState.SINCRONIZADA,
    (PreservationState.PUSH_PENDENTE, "push_failed"): PreservationState.COMMITADA_LOCALMENTE,
    (PreservationState.SINCRONIZADA, "detect"): PreservationState.MODIFICACAO_DETECTADA,
    # Error / conflict paths
    (PreservationState.PREPARADA, "conflict"): PreservationState.CONFLITO,
    (PreservationState.COMMITADA_LOCALMENTE, "conflict"): PreservationState.CONFLITO,
    (PreservationState.PUSH_PENDENTE, "conflict"): PreservationState.CONFLITO,
    (PreservationState.PREPARACAO_PENDENTE, "fail"): PreservationState.FALHA,
    (PreservationState.PREPARADA, "fail"): PreservationState.FALHA,
    (PreservationState.COMMITADA_LOCALMENTE, "fail"): PreservationState.FALHA,
}


def build_preservation_machine(
    on_transition: Any = None,
) -> StateMachine:
    """Create the Preservation state machine instance."""
    return StateMachine(
        name="preservation",
        transitions=PRESERVATION_TRANSITIONS,
        on_transition=on_transition,
    )


def classify_changes(
    status_entries: list[dict[str, str]],
    eligible_patterns: list[str],
) -> tuple[list[str], list[str]]:
    """Split worktree entries into eligible and excluded file paths.

    When *eligible_patterns* is empty, all files are eligible (default).
    """
    import fnmatch
    eligible: list[str] = []
    excluded: list[str] = []
    for entry in status_entries:
        path = entry["path"]
        if not eligible_patterns:
            eligible.append(path)
        elif any(fnmatch.fnmatch(path, p) for p in eligible_patterns):
            eligible.append(path)
        else:
            excluded.append(path)
    return eligible, excluded


def detect_changes(
    store: Path,
    task_id: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Detect local changes and classify them against the task's scope.

    Returns a report dict with ``eligible``, ``excluded``, ``status`` entries.
    """
    config = load_config(store)
    eligible_patterns = config.get("eligible_patterns", [])
    entries = worktree_status(cwd)

    if not entries:
        return {
            "task_id": task_id,
            "status": "clean",
            "eligible": [],
            "excluded": [],
            "entries": [],
        }

    eligible, excluded = classify_changes(entries, eligible_patterns)

    log_event(
        store,
        "changes_detected",
        task_id=task_id,
        detail=f"eligible={len(eligible)} excluded={len(excluded)}",
    )

    return {
        "task_id": task_id,
        "status": "changes_found",
        "eligible": eligible,
        "excluded": excluded,
        "entries": entries,
    }


def stage_eligible(
    store: Path,
    task_id: str,
    eligible_paths: list[str],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Stage eligible files and return staging result."""
    if not eligible_paths:
        return {"status": "nothing_to_stage", "staged": []}

    # git commit otherwise includes unrelated files staged before ESPAUT runs.
    pre_staged = set(staged_files(cwd))
    unrelated = sorted(pre_staged - set(eligible_paths))
    if unrelated:
        return {"status": "unsafe_staging", "unrelated": unrelated}

    result = stage_files(eligible_paths, cwd=cwd)
    if result.returncode != 0:
        log_event(
            store,
            "staging_failed",
            task_id=task_id,
            outcome="FALHA",
            detail=result.stderr.strip(),
        )
        return {"status": "error", "error": result.stderr.strip()}

    files = staged_files(cwd)
    if set(files) - set(eligible_paths):
        return {"status": "unsafe_staging", "unrelated": sorted(set(files) - set(eligible_paths))}
    diff = staged_diff(cwd)
    from espaut.security import contains_secret
    if contains_secret(diff):
        log_event(store, "secret_in_staged_diff", task_id=task_id, outcome="FALHA")
        return {"status": "secret_detected", "error": "staged diff contains a recognized secret"}

    log_event(
        store,
        "files_staged",
        task_id=task_id,
        detail=f"staged={len(files)}",
    )

    return {"status": "staged", "staged": files, "diff_preview": diff[:2000]}


def commit_staged(
    store: Path,
    task_id: str,
    message: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Commit staged changes and record the SHA."""
    result = create_commit(message, cwd=cwd)
    if result.returncode != 0:
        log_event(
            store,
            "commit_failed",
            task_id=task_id,
            outcome="FALHA",
            detail=result.stderr.strip(),
        )
        return {"status": "error", "error": result.stderr.strip()}

    sha = head_sha(cwd)

    evidence = make_evidence(
        "git",
        sha or "unknown",
        f"commit created for task {task_id}",
        task_id=task_id,
        related_revision=sha,
    )
    save_evidence(store, evidence)

    log_event(
        store,
        "commit_created",
        task_id=task_id,
        object_reference=sha,
        evidence_id=evidence["evidence_id"],
    )

    return {"status": "committed", "sha": sha, "evidence_id": evidence["evidence_id"]}


def push_and_verify(
    store: Path,
    task_id: str,
    remote: str = "origin",
    branch: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Push to remote and verify the reference was updated."""
    result = push(remote=remote, branch=branch, cwd=cwd, set_upstream=True)
    if result.returncode != 0:
        log_event(
            store,
            "push_failed",
            task_id=task_id,
            outcome="FALHA",
            detail=result.stderr.strip(),
        )
        return {"status": "error", "error": result.stderr.strip()}

    local_sha = head_sha(cwd)
    remote_sha = remote_head_sha(remote, branch, cwd)

    if local_sha and remote_sha and local_sha == remote_sha:
        evidence = make_evidence(
            "git",
            remote_sha,
            f"push verified: local={local_sha} remote={remote_sha}",
            task_id=task_id,
            related_revision=local_sha,
        )
        save_evidence(store, evidence)
        log_event(
            store,
            "push_verified",
            task_id=task_id,
            object_reference=local_sha,
            evidence_id=evidence["evidence_id"],
        )
        return {
            "status": "synchronized",
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "evidence_id": evidence["evidence_id"],
        }

    log_event(
        store,
        "push_unverified",
        task_id=task_id,
        detail=f"local={local_sha} remote={remote_sha}",
        outcome="INDETERMINADO",
    )
    return {
        "status": "unverified",
        "local_sha": local_sha,
        "remote_sha": remote_sha,
    }


def run_preservation(
    store: Path,
    task_id: str,
    message: str,
    *,
    dry_run: bool = True,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Execute the full preservation flow: detect → stage → commit → push.

    With ``dry_run=True`` (default), only reports what would happen.
    """
    report: dict[str, Any] = {"task_id": task_id, "dry_run": dry_run, "steps": []}

    # Fail closed during unfinished merge/rebase/cherry-pick operations.
    from espaut.git_ops import has_incomplete_operation
    operation = has_incomplete_operation(cwd)
    if operation:
        report["outcome"] = "incomplete_git_operation"
        report["operation"] = operation
        return report

    # 1. Detect
    detection = detect_changes(store, task_id, cwd)
    report["steps"].append({"step": "detect", "result": detection})
    if detection["status"] == "clean":
        report["outcome"] = "nothing_to_preserve"
        return report

    if dry_run:
        report["outcome"] = "dry_run_complete"
        report["would_stage"] = detection["eligible"]
        report["would_exclude"] = detection["excluded"]
        return report

    from espaut.git_ops import current_branch
    branch = current_branch(cwd)
    if branch in (None, "main", "master"):
        report["outcome"] = "protected_branch"
        report["branch"] = branch
        return report

    # 2. Stage
    staging = stage_eligible(store, task_id, detection["eligible"], cwd)
    report["steps"].append({"step": "stage", "result": staging})
    if staging["status"] != "staged":
        report["outcome"] = "staging_failed"
        return report

    # 3. Commit
    commit = commit_staged(store, task_id, message, cwd)
    report["steps"].append({"step": "commit", "result": commit})
    if commit["status"] != "committed":
        report["outcome"] = "commit_failed"
        return report

    # 4. Push
    push_result = push_and_verify(store, task_id, cwd=cwd)
    report["steps"].append({"step": "push", "result": push_result})
    report["outcome"] = push_result["status"]

    return report
