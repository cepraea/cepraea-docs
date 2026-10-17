#!/usr/bin/env python3
"""Reviews, comments, threads, classification, and Pendency state machine (SM #2, CTX-05)."""

from __future__ import annotations

import enum
import hashlib
from pathlib import Path
from typing import Any

from espaut.engine import StateMachine
from espaut.events import log_event
from espaut.gh_ops import pr_comments_json, pr_review_comments_json, pr_reviews_json, pr_review_threads_json
from espaut.security import sanitize_external_text
from espaut.store import new_id, read_json, write_json


class PendencyState(enum.StrEnum):
    IDENTIFICADA = "IDENTIFICADA"
    CLASSIFICADA = "CLASSIFICADA"
    EM_TRATAMENTO = "EM_TRATAMENTO"
    CORRECAO_APLICADA = "CORRECAO_APLICADA"
    AGUARDANDO_VERIFICACAO = "AGUARDANDO_VERIFICACAO"
    RESOLVIDA = "RESOLVIDA"
    JUSTIFICADA = "JUSTIFICADA"
    BLOQUEADA = "BLOQUEADA"


PENDENCY_TRANSITIONS = {
    (PendencyState.IDENTIFICADA, "classify"): PendencyState.CLASSIFICADA,
    (PendencyState.CLASSIFICADA, "start_treatment"): PendencyState.EM_TRATAMENTO,
    (PendencyState.CLASSIFICADA, "justify"): PendencyState.JUSTIFICADA,
    (PendencyState.EM_TRATAMENTO, "apply_fix"): PendencyState.CORRECAO_APLICADA,
    (PendencyState.CORRECAO_APLICADA, "await_verify"): PendencyState.AGUARDANDO_VERIFICACAO,
    (PendencyState.AGUARDANDO_VERIFICACAO, "resolve"): PendencyState.RESOLVIDA,
    (PendencyState.AGUARDANDO_VERIFICACAO, "reject"): PendencyState.EM_TRATAMENTO,
    (PendencyState.IDENTIFICADA, "block"): PendencyState.BLOQUEADA,
    (PendencyState.CLASSIFICADA, "block"): PendencyState.BLOQUEADA,
    (PendencyState.EM_TRATAMENTO, "block"): PendencyState.BLOQUEADA,
    (PendencyState.BLOQUEADA, "unblock"): PendencyState.CLASSIFICADA,
}


def build_pendency_machine(on_transition: Any = None) -> StateMachine:
    return StateMachine(
        name="pendency",
        transitions=PENDENCY_TRANSITIONS,
        on_transition=on_transition,
    )


class CommentClassification(enum.StrEnum):
    DEFEITO_CONFIRMADO = "DEFEITO_CONFIRMADO"
    DEFEITO_POSSIVEL = "DEFEITO_POSSIVEL"
    MUDANCA_REQUERIDA = "MUDANCA_REQUERIDA"
    SUGESTAO_OPCIONAL = "SUGESTAO_OPCIONAL"
    PERGUNTA = "PERGUNTA"
    INFORMACAO = "INFORMACAO"
    IMPEDIMENTO_EXTERNO = "IMPEDIMENTO_EXTERNO"
    INDETERMINADO = "INDETERMINADO"


BLOCKING_CLASSIFICATIONS = {
    CommentClassification.DEFEITO_CONFIRMADO,
    CommentClassification.DEFEITO_POSSIVEL,
    CommentClassification.MUDANCA_REQUERIDA,
    CommentClassification.IMPEDIMENTO_EXTERNO,
    CommentClassification.INDETERMINADO,  # ambiguous requests cannot be silently dismissed
}


def classify_review(review: dict[str, Any]) -> CommentClassification:
    """Classify a GitHub review by its state."""
    state = (review.get("state") or "").upper()
    if state == "CHANGES_REQUESTED":
        return CommentClassification.MUDANCA_REQUERIDA
    if state == "APPROVED":
        return CommentClassification.INFORMACAO
    if state in ("COMMENTED", "DISMISSED"):
        body = review.get("body", "")
        if body and len(body.strip()) > 0:
            return _classify_text(body)
        return CommentClassification.INFORMACAO
    return CommentClassification.INDETERMINADO


def _classify_text(text: str) -> CommentClassification:
    """Heuristic classification of comment text."""
    lower = text.lower()
    if any(w in lower for w in ["bug", "error", "wrong", "broken", "incorrect", "defeito"]):
        return CommentClassification.DEFEITO_POSSIVEL
    if any(w in lower for w in ["please change", "must", "required", "should", "need to", "altere", "corrija"]):
        return CommentClassification.MUDANCA_REQUERIDA
    if any(w in lower for w in ["suggest", "consider", "maybe", "could", "might", "sugestão", "opcional"]):
        return CommentClassification.SUGESTAO_OPCIONAL
    if text.strip().endswith("?"):
        return CommentClassification.PERGUNTA
    return CommentClassification.INDETERMINADO


def classify_comment(comment: dict[str, Any]) -> CommentClassification:
    """Classify a single comment (issue-level or review-level)."""
    body = comment.get("body", "")
    if not body or not body.strip():
        return CommentClassification.INFORMACAO
    return _classify_text(body)


def make_pendency(
    origin_type: str,
    origin_reference: str,
    description: str,
    classification: CommentClassification,
    *,
    task_id: str,
    pr_number: int | None = None,
    author: str | None = None,
    related_file: str | None = None,
    related_line: int | None = None,
    thread_id: str | None = None,
    thread_resolved_remote: bool | None = None,
) -> dict[str, Any]:
    """Build a pendency record."""
    if not str(origin_reference).strip():
        raise ValueError("GitHub pendency requires a stable source reference")
    severity = "blocking" if classification in BLOCKING_CLASSIFICATIONS else "informational"
    source_key = f"{task_id}|{pr_number}|{origin_type}|{origin_reference}"
    stable_id = "PND-" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:20]
    record: dict[str, Any] = {
        "pendency_id": stable_id,
        "task_id": task_id,
        "origin_type": origin_type,
        "origin_reference": origin_reference,
        "description": sanitize_external_text(description[:500]),
        "classification": classification.value,
        "severity": severity,
        "resolution_state": PendencyState.IDENTIFICADA.value,
        "blocking_effect": severity == "blocking",
        "assigned_actor": None,
        "resolution_evidence": None,
    }
    if pr_number is not None:
        record["pr_number"] = pr_number
    if author:
        record["author"] = author
    if related_file:
        record["related_file"] = related_file
    if related_line is not None:
        record["related_line"] = related_line
    if thread_id:
        record["thread_id"] = thread_id
    if thread_resolved_remote is not None:
        record["thread_resolved_remote"] = thread_resolved_remote
    return record


def collect_reviews(
    store: Path,
    task_id: str,
    pr_number: int,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Collect all reviews, comments, and threads for a PR. Returns a report."""
    reviews = pr_reviews_json(pr_number, cwd=cwd)
    general_comments = pr_comments_json(pr_number, cwd=cwd)
    code_comments = pr_review_comments_json(pr_number, cwd=cwd)
    threads = pr_review_threads_json(pr_number, cwd=cwd)
    thread_map: dict[int, dict[str, Any]] = {}
    for thread in threads:
        for comment in thread.get("comments", {}).get("nodes", []):
            if comment.get("databaseId") is not None:
                thread_map[int(comment["databaseId"])] = {
                    "thread_id": thread["id"],
                    "thread_resolved_remote": thread["isResolved"],
                    "thread_outdated": thread["isOutdated"],
                }

    review_records = []
    for r in reviews:
        classification = classify_review(r)
        review_records.append({
            "review_id": r.get("id"),
            "reviewer": r.get("user", {}).get("login", "unknown"),
            "state": r.get("state"),
            "classification": classification.value,
            "submitted_at": r.get("submitted_at"),
        })

    comment_records = []
    for c in general_comments:
        classification = classify_comment(c)
        comment_records.append({
            "comment_id": c.get("id"),
            "author": c.get("user", {}).get("login", "unknown"),
            "body_preview": sanitize_external_text((c.get("body", ""))[:200]),
            "classification": classification.value,
            "created_at": c.get("created_at"),
            "comment_type": "general",
        })

    for c in code_comments:
        classification = classify_comment(c)
        comment_records.append({
            "comment_id": c.get("id"),
            "author": c.get("user", {}).get("login", "unknown"),
            "body_preview": sanitize_external_text((c.get("body", ""))[:200]),
            "classification": classification.value,
            "created_at": c.get("created_at"),
            "comment_type": "code",
            "related_file": c.get("path"),
            "related_line": c.get("line"),
            **thread_map.get(c.get("id"), {}),
        })

    cache_path = store / "cache" / f"reviews-PR{pr_number}.json"
    write_json(cache_path, {
        "pr_number": pr_number,
        "reviews": review_records,
        "comments": comment_records,
        "threads": [
            {"id": t["id"], "isResolved": t["isResolved"], "isOutdated": t["isOutdated"]}
            for t in threads
        ],
    })

    log_event(
        store, "reviews_collected",
        task_id=task_id,
        object_reference=str(pr_number),
        detail=f"reviews={len(review_records)} comments={len(comment_records)}",
    )

    return {
        "status": "collected",
        "task_id": task_id,
        "pr_number": pr_number,
        "reviews": review_records,
        "comments": comment_records,
        "threads": [
            {"id": t["id"], "isResolved": t["isResolved"], "isOutdated": t["isOutdated"]}
            for t in threads
        ],
    }


def extract_pendencies(
    review_report: dict[str, Any],
    *,
    task_id: str,
    store: Path | None = None,
) -> list[dict[str, Any]]:
    """Extract pendencies with stable identities and optional persistent history."""
    pendencies: list[dict[str, Any]] = []
    pr_number = review_report.get("pr_number")

    for r in review_report.get("reviews", []):
        classification = CommentClassification(r["classification"])
        if classification in BLOCKING_CLASSIFICATIONS:
            pendencies.append(make_pendency(
                origin_type="review",
                origin_reference=str(r.get("review_id", "")),
                description=f"Review by {r.get('reviewer', '?')}: {r['state']}",
                classification=classification,
                task_id=task_id,
                pr_number=pr_number,
                author=r.get("reviewer"),
            ))

    for c in review_report.get("comments", []):
        classification = CommentClassification(c["classification"])
        if classification != CommentClassification.INFORMACAO:
            pendencies.append(make_pendency(
                origin_type="comment",
                origin_reference=str(c.get("comment_id", "")),
                description=c.get("body_preview", ""),
                classification=classification,
                task_id=task_id,
                pr_number=pr_number,
                author=c.get("author"),
                related_file=c.get("related_file"),
                related_line=c.get("related_line"),
                thread_id=c.get("thread_id"),
                thread_resolved_remote=c.get("thread_resolved_remote"),
            ))

    if store is not None:
        ledger = store / "cache" / f"pendencies-PR{pr_number}.json"
        previous = read_json(ledger) if ledger.exists() else []
        existing = {p["pendency_id"]: p for p in previous}
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for p in pendencies:
            old = existing.get(p["pendency_id"], {})
            if old.get("resolution_state") in ("RESOLVIDA", "JUSTIFICADA"):
                p["resolution_state"] = old["resolution_state"]
                p["resolution_evidence"] = old.get("resolution_evidence")
            p["present_remote"] = True
            seen.add(p["pendency_id"])
            merged.append(p)
        for old in previous:
            if old["pendency_id"] not in seen:
                # Do not silently erase unresolved reviewer requests.
                old["present_remote"] = False
                merged.append(old)
        write_json(ledger, merged)
        return merged
    return pendencies


def has_blocking_pendencies(pendencies: list[dict[str, Any]]) -> bool:
    """Return True if any pendency is blocking and unresolved."""
    for p in pendencies:
        if not p.get("blocking_effect"):
            continue
        if p.get("thread_resolved_remote") is False:
            return True
        if p.get("resolution_state") not in (
            PendencyState.RESOLVIDA.value,
            PendencyState.JUSTIFICADA.value,
        ):
            return True
    return False
