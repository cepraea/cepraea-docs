#!/usr/bin/env python3
"""Task entity and Task state machine (SM #1)."""

from __future__ import annotations

import enum
from typing import Any

from espaut.engine import StateMachine


class TaskState(enum.StrEnum):
    REGISTRADA = "REGISTRADA"
    PREPARADA = "PREPARADA"
    EM_EXECUCAO = "EM_EXECUCAO"
    PRESERVACAO_PENDENTE = "PRESERVACAO_PENDENTE"
    EM_REVISAO = "EM_REVISAO"
    EM_CORRECAO = "EM_CORRECAO"
    AGUARDANDO_DECISAO = "AGUARDANDO_DECISAO"
    APTA_PARA_INTEGRACAO = "APTA_PARA_INTEGRACAO"
    INTEGRADA = "INTEGRADA"
    ENCERRADA_SEM_MERGE = "ENCERRADA_SEM_MERGE"
    BLOQUEADA = "BLOQUEADA"
    INTERROMPIDA = "INTERROMPIDA"


TASK_TRANSITIONS = {
    (TaskState.REGISTRADA, "prepare"): TaskState.PREPARADA,
    (TaskState.PREPARADA, "start"): TaskState.EM_EXECUCAO,
    (TaskState.EM_EXECUCAO, "changes_detected"): TaskState.PRESERVACAO_PENDENTE,
    (TaskState.PRESERVACAO_PENDENTE, "preserved"): TaskState.EM_EXECUCAO,
    (TaskState.PRESERVACAO_PENDENTE, "submit_review"): TaskState.EM_REVISAO,
    (TaskState.EM_EXECUCAO, "submit_review"): TaskState.EM_REVISAO,
    (TaskState.EM_REVISAO, "request_correction"): TaskState.EM_CORRECAO,
    (TaskState.EM_CORRECAO, "correction_applied"): TaskState.EM_REVISAO,
    (TaskState.EM_REVISAO, "await_decision"): TaskState.AGUARDANDO_DECISAO,
    (TaskState.AGUARDANDO_DECISAO, "approve_integration"): TaskState.APTA_PARA_INTEGRACAO,
    (TaskState.AGUARDANDO_DECISAO, "reject"): TaskState.EM_CORRECAO,
    (TaskState.APTA_PARA_INTEGRACAO, "merge_confirmed"): TaskState.INTEGRADA,
    (TaskState.APTA_PARA_INTEGRACAO, "invalidated"): TaskState.EM_REVISAO,
    (TaskState.EM_REVISAO, "new_changes"): TaskState.PRESERVACAO_PENDENTE,
    (TaskState.EM_CORRECAO, "new_changes"): TaskState.PRESERVACAO_PENDENTE,
    # Terminal / alternative paths
    (TaskState.EM_REVISAO, "close_without_merge"): TaskState.ENCERRADA_SEM_MERGE,
    (TaskState.AGUARDANDO_DECISAO, "close_without_merge"): TaskState.ENCERRADA_SEM_MERGE,
    (TaskState.EM_EXECUCAO, "block"): TaskState.BLOQUEADA,
    (TaskState.EM_REVISAO, "block"): TaskState.BLOQUEADA,
    (TaskState.EM_CORRECAO, "block"): TaskState.BLOQUEADA,
    (TaskState.BLOQUEADA, "unblock"): TaskState.EM_EXECUCAO,
    (TaskState.EM_EXECUCAO, "interrupt"): TaskState.INTERROMPIDA,
    (TaskState.EM_REVISAO, "interrupt"): TaskState.INTERROMPIDA,
    (TaskState.EM_CORRECAO, "interrupt"): TaskState.INTERROMPIDA,
    (TaskState.INTERROMPIDA, "resume"): TaskState.EM_EXECUCAO,
}


def build_task_machine(
    on_transition: Any = None,
) -> StateMachine:
    """Create the Task state machine instance."""
    return StateMachine(
        name="task",
        transitions=TASK_TRANSITIONS,
        on_transition=on_transition,
    )


def make_task(
    task_id: str,
    repository_id: str,
    title: str,
    *,
    objective: str = "",
    acceptance_criteria: list[str] | None = None,
    authorized_scope: list[str] | None = None,
) -> dict[str, Any]:
    """Build a task record in its initial state."""
    import datetime
    return {
        "task_id": task_id,
        "repository_id": repository_id,
        "title": title,
        "objective": objective,
        "acceptance_criteria": acceptance_criteria or [],
        "authorized_scope": authorized_scope or [],
        "state": TaskState.REGISTRADA.value,
        "branch_name": None,
        "pr_number": None,
        "base_commit_sha": None,
        "head_commit_sha": None,
        "acceptance_revision": None,
        "acceptance_results": {},
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "closed_at": None,
        "sessions": [],
        "commit_shas": [],
    }
