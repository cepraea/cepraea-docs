#!/usr/bin/env python3
"""CI/check monitoring — Check state machine (SM #3, CTX-04)."""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

from espaut.engine import StateMachine
from espaut.events import log_event
from espaut.gh_ops import pr_checks_json
from espaut.store import load_config, write_json


class CheckState(enum.StrEnum):
    NAO_CONFIGURADO = "NAO_CONFIGURADO"
    NAO_OBSERVADO = "NAO_OBSERVADO"
    PENDENTE = "PENDENTE"
    APROVADO = "APROVADO"
    REPROVADO = "REPROVADO"
    CANCELADO = "CANCELADO"
    IGNORADO = "IGNORADO"
    INDETERMINADO = "INDETERMINADO"


GH_STATE_MAP: dict[tuple[str, str], CheckState] = {
    # (state, bucket) — ``gh pr checks --json`` provides ``state`` and ``bucket``.
    # Primary: match on state (case-preserved as returned by gh).
    ("SUCCESS", ""): CheckState.APROVADO,
    ("SUCCESS", "pass"): CheckState.APROVADO,
    ("FAILURE", ""): CheckState.REPROVADO,
    ("FAILURE", "fail"): CheckState.REPROVADO,
    ("PENDING", ""): CheckState.PENDENTE,
    ("PENDING", "pending"): CheckState.PENDENTE,
    ("CANCELLED", ""): CheckState.CANCELADO,
    ("CANCELLED", "fail"): CheckState.CANCELADO,
    ("NEUTRAL", ""): CheckState.APROVADO,
    ("NEUTRAL", "pass"): CheckState.APROVADO,
    ("SKIPPED", ""): CheckState.IGNORADO,
    ("SKIPPED", "skipping"): CheckState.IGNORADO,
    ("STALE", ""): CheckState.INDETERMINADO,
    ("STARTUP_FAILURE", ""): CheckState.REPROVADO,
    ("STARTUP_FAILURE", "fail"): CheckState.REPROVADO,
    ("ACTION_REQUIRED", ""): CheckState.PENDENTE,
    ("ACTION_REQUIRED", "pending"): CheckState.PENDENTE,
    ("TIMED_OUT", ""): CheckState.REPROVADO,
    ("TIMED_OUT", "fail"): CheckState.REPROVADO,
    # Fallback: match on bucket alone when state is absent/unknown.
    ("", "pass"): CheckState.APROVADO,
    ("", "fail"): CheckState.REPROVADO,
    ("", "pending"): CheckState.PENDENTE,
    ("", "skipping"): CheckState.IGNORADO,
}


def normalize_check_state(raw: dict[str, Any]) -> CheckState:
    """Map a raw gh check/status record to a normalized CheckState.

    Uses ``state`` as primary and ``bucket`` as fallback — the fields
    returned by ``gh pr checks --json``.
    """
    state = raw.get("state", "")
    bucket = raw.get("bucket", "")
    key = (state, bucket)
    if key in GH_STATE_MAP:
        return GH_STATE_MAP[key]
    key_state = (state, "")
    if key_state in GH_STATE_MAP:
        return GH_STATE_MAP[key_state]
    key_bucket = ("", bucket)
    if key_bucket in GH_STATE_MAP:
        return GH_STATE_MAP[key_bucket]
    for k, v in GH_STATE_MAP.items():
        if k[0] and k[0].lower() == state.lower():
            return v
        if k[1] and k[1].lower() == bucket.lower():
            return v
    return CheckState.INDETERMINADO


def make_check_record(
    raw: dict[str, Any],
    *,
    pr_number: int,
    evaluated_revision: str | None = None,
) -> dict[str, Any]:
    """Build a normalized check record from raw gh data."""
    normalized = normalize_check_state(raw)
    return {
        "name": raw.get("name", "unknown"),
        "pr_number": pr_number,
        "evaluated_revision": evaluated_revision,
        "raw_state": raw.get("state", ""),
        "raw_bucket": raw.get("bucket", ""),
        "normalized_result": normalized.value,
        "execution_url": raw.get("link", ""),
        "started_at": raw.get("startedAt"),
        "completed_at": raw.get("completedAt"),
    }


def query_checks(
    store: Path,
    task_id: str,
    pr_number: int,
    evaluated_revision: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Query checks for a PR and return a normalized report."""
    raw_checks = pr_checks_json(pr_number, cwd=cwd)

    if not raw_checks:
        config = load_config(store)
        if config.get("ci_required", False):
            log_event(
                store, "checks_not_configured",
                task_id=task_id,
                object_reference=str(pr_number),
            )
            return {
                "status": "not_configured",
                "task_id": task_id,
                "pr_number": pr_number,
                "checks": [],
                "evaluated_revision": evaluated_revision,
                "aggregate": CheckState.NAO_CONFIGURADO.value,
            }
        return {
            "status": "not_configured",
            "task_id": task_id,
            "pr_number": pr_number,
            "checks": [],
            "evaluated_revision": evaluated_revision,
            "aggregate": CheckState.NAO_CONFIGURADO.value,
        }

    records = [
        make_check_record(c, pr_number=pr_number, evaluated_revision=evaluated_revision)
        for c in raw_checks
    ]

    aggregate = compute_aggregate(records)

    log_event(
        store, "checks_queried",
        task_id=task_id,
        object_reference=str(pr_number),
        detail=f"total={len(records)} aggregate={aggregate.value}",
    )

    cache_path = store / "cache" / f"checks-PR{pr_number}.json"
    write_json(cache_path, {
        "pr_number": pr_number,
        "evaluated_revision": evaluated_revision,
        "checks": records,
        "aggregate": aggregate.value,
    })

    return {
        "status": "queried",
        "task_id": task_id,
        "pr_number": pr_number,
        "checks": records,
        "aggregate": aggregate.value,
    }


def compute_aggregate(checks: list[dict[str, Any]]) -> CheckState:
    """Compute aggregate check state from individual results."""
    if not checks:
        return CheckState.NAO_CONFIGURADO

    states = {CheckState(c["normalized_result"]) for c in checks}

    if CheckState.REPROVADO in states:
        return CheckState.REPROVADO
    if CheckState.PENDENTE in states:
        return CheckState.PENDENTE
    if CheckState.CANCELADO in states:
        return CheckState.CANCELADO
    if CheckState.INDETERMINADO in states:
        return CheckState.INDETERMINADO
    if all(s in (CheckState.APROVADO, CheckState.IGNORADO) for s in states):
        return CheckState.APROVADO
    return CheckState.INDETERMINADO
