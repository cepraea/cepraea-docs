#!/usr/bin/env python3
"""Policies, decisions, merge eligibility (C1-C8), and authorization (CTX-07)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from espaut.checks import CheckState
from espaut.events import log_event
from espaut.git_ops import head_sha, remote_head_sha
from espaut.review import has_blocking_pendencies
from espaut.store import load_config, new_id, read_json, write_json


def make_decision(
    task_id: str,
    decision_type: str,
    authority: str,
    value: str,
    *,
    scope: str = "",
    evidence_reference: str | None = None,
) -> dict[str, Any]:
    """Build a decision record."""
    import datetime
    return {
        "decision_id": new_id("DEC-"),
        "task_id": task_id,
        "decision_type": decision_type,
        "authority": authority,
        "decision_value": value,
        "scope": scope,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "evidence_reference": evidence_reference,
    }


def record_decision(store: Path, decision: dict[str, Any]) -> Path:
    """Persist a decision record."""
    did = decision["decision_id"]
    path = store / "decisions" / f"{did}.json"
    write_json(path, decision)
    log_event(
        store, "decision_recorded",
        task_id=decision.get("task_id"),
        object_reference=did,
        detail=f"{decision['decision_type']}={decision['decision_value']}",
    )
    return path


def evaluate_criterion(name: str, passed: bool, detail: str) -> dict[str, Any]:
    """Build a single criterion evaluation result."""
    return {
        "criterion": name,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
    }


def check_c1_preservation(task: dict[str, Any], cwd: str | None = None) -> dict[str, Any]:
    """C1: All local changes committed and pushed (or dispensed)."""
    from espaut.git_ops import has_changes
    if has_changes(cwd):
        return evaluate_criterion("C1_preservation", False, "uncommitted local changes exist")
    return evaluate_criterion("C1_preservation", True, "no uncommitted changes")


def check_c2_sync(task: dict[str, Any], cwd: str | None = None) -> dict[str, Any]:
    """C2: Branch is up-to-date with remote."""
    local = head_sha(cwd)
    if task.get("pr_number"):
        # The PR may originate from a fork: origin may not contain the head branch.
        # Query the actual GitHub PR head instead of assuming origin/<branch>.
        from espaut.pullrequest import query_pr_state
        try:
            state = query_pr_state(int(task["pr_number"]), cwd=cwd)
        except Exception as exc:
            return evaluate_criterion("C2_sync", False, f"PR head could not be queried: {type(exc).__name__}")
        remote = state.get("head_sha") if state.get("status") == "available" else None
    else:
        remote = remote_head_sha(cwd=cwd)
    if not local or not remote:
        return evaluate_criterion("C2_sync", False, f"cannot verify: local={local} remote={remote}")
    if local == remote:
        return evaluate_criterion("C2_sync", True, f"synchronized: {local[:8]}")
    return evaluate_criterion("C2_sync", False, f"diverged: local={local[:8]} remote={remote[:8]}")


def check_c3_verification(
    checks_report: dict[str, Any],
    *,
    ci_required: bool = False,
) -> dict[str, Any]:
    """C3: All required CI checks pass."""
    aggregate = checks_report.get("aggregate", "")
    if aggregate == CheckState.APROVADO.value:
        return evaluate_criterion("C3_verification", True, "all checks passed")
    if aggregate == CheckState.NAO_CONFIGURADO.value:
        if ci_required:
            return evaluate_criterion(
                "C3_verification", False,
                "CI required but no checks configured",
            )
        return evaluate_criterion("C3_verification", True, "no checks configured (non-blocking)")
    return evaluate_criterion("C3_verification", False, f"checks aggregate: {aggregate}")


def check_c4_review(reviews_report: dict[str, Any]) -> dict[str, Any]:
    """C4: At least one approving review."""
    if reviews_report.get("is_draft") is True:
        return evaluate_criterion("C4_review", False, "draft PR cannot be approved for integration")
    reviews = reviews_report.get("reviews", [])
    approvals = [r for r in reviews if r.get("state") in ("APPROVED", "approved")]
    if reviews_report.get("review_decision") == "CHANGES_REQUESTED":
        return evaluate_criterion("C4_review", False, "current PR review decision requests changes")
    if approvals:
        return evaluate_criterion("C4_review", True, f"{len(approvals)} approval(s)")
    return evaluate_criterion("C4_review", False, "no approving review")


def check_c5_pendencies(pendencies: list[dict[str, Any]]) -> dict[str, Any]:
    """C5: No unresolved blocking pendencies."""
    if has_blocking_pendencies(pendencies):
        blocking = [
            p["pendency_id"] for p in pendencies
            if p.get("blocking_effect")
            and (
                p.get("thread_resolved_remote") is False
                or p.get("resolution_state") not in ("RESOLVIDA", "JUSTIFICADA")
            )
        ]
        return evaluate_criterion("C5_pendencies", False, f"blocking: {blocking}")
    return evaluate_criterion("C5_pendencies", True, "no blocking pendencies")


def check_c6_acceptance(task: dict[str, Any]) -> dict[str, Any]:
    """C6: Acceptance criteria comprovados."""
    criteria = task.get("acceptance_criteria", [])
    if not criteria:
        return evaluate_criterion("C6_acceptance", False, "acceptance criteria absent")
    results = task.get("acceptance_results", {})
    current_sha = task.get("acceptance_revision")
    if not isinstance(results, dict) or not current_sha:
        return evaluate_criterion("C6_acceptance", False, "acceptance evidence missing")
    for criterion in criteria:
        key = str(criterion)
        record = results.get(key)
        if not isinstance(record, dict) or record.get("status") != "PASS":
            return evaluate_criterion("C6_acceptance", False, f"criterion not passed: {key}")
        if not record.get("evidence_reference") or record.get("revision") != current_sha:
            return evaluate_criterion("C6_acceptance", False, f"invalid evidence: {key}")
    return evaluate_criterion("C6_acceptance", True, "all criteria supported by evidence")


def check_c7_authority(
    task: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """C7: Merge authorization from human authority."""
    merge_decisions = [
        d for d in decisions
        if d.get("task_id") == task["task_id"]
        and d.get("decision_type") == "merge_authorization"
        and d.get("decision_value") == "approved"
    ]
    if merge_decisions:
        return evaluate_criterion("C7_authority", True, f"authorized by {merge_decisions[-1].get('authority', '?')}")
    return evaluate_criterion("C7_authority", False, "no merge authorization")


def check_c8_currency(
    checks_report: dict[str, Any],
    current_head: str | None,
) -> dict[str, Any]:
    """C8: Evidence is current (evaluated revision matches current head)."""
    checks = checks_report.get("checks", [])
    if not checks:
        if (
            checks_report.get("status") == "not_configured"
            and current_head
            and checks_report.get("evaluated_revision") == current_head
        ):
            return evaluate_criterion("C8_currency", True, "no CI; confirmed PR head revision")
        return evaluate_criterion("C8_currency", False, "no revision-bound check evidence")
    if not current_head:
        return evaluate_criterion("C8_currency", False, "cannot determine current HEAD")
    has_revision = False
    for check in checks:
        evaluated = check.get("evaluated_revision")
        if evaluated:
            has_revision = True
            if evaluated != current_head:
                return evaluate_criterion(
                    "C8_currency", False,
                    f"stale check: evaluated={evaluated[:8]} current={current_head[:8]}",
                )
    if not has_revision or any(not c.get("evaluated_revision") for c in checks):
        return evaluate_criterion("C8_currency", False, "some checks lack evaluated_revision")
    return evaluate_criterion("C8_currency", True, "all evidence current")


def evaluate_merge_eligibility(
    store: Path,
    task: dict[str, Any],
    checks_report: dict[str, Any],
    reviews_report: dict[str, Any],
    pendencies: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Evaluate all 8 merge eligibility criteria.

    Returns a report with per-criterion results and overall verdict.
    """
    current_head = head_sha(cwd)
    config = load_config(store)
    # Do not trust acceptance evidence belonging to a different revision.
    if task.get("acceptance_revision") != current_head:
        task = {**task, "acceptance_results": {}}

    criteria = [
        check_c1_preservation(task, cwd),
        check_c2_sync(task, cwd),
        check_c3_verification(checks_report, ci_required=config.get("ci_required", False)),
        check_c4_review(reviews_report),
        check_c5_pendencies(pendencies),
        check_c6_acceptance(task),
        check_c7_authority(task, decisions),
        check_c8_currency(checks_report, current_head),
    ]

    all_pass = all(c["status"] == "PASS" for c in criteria)
    verdict = "APTA_PARA_INTEGRACAO" if all_pass else "NAO_APTA"

    log_event(
        store, "merge_eligibility_evaluated",
        task_id=task["task_id"],
        outcome=verdict,
        detail=f"criteria: {sum(1 for c in criteria if c['status'] == 'PASS')}/8 PASS",
    )

    return {
        "task_id": task["task_id"],
        "verdict": verdict,
        "criteria": criteria,
        "evaluated_head": current_head,
    }
