#!/usr/bin/env python3
"""Tests for review, comment classification, and pendency tracking."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from espaut.engine import TransitionError
from espaut.review import (
    CommentClassification,
    PendencyState,
    build_pendency_machine,
    classify_comment,
    classify_review,
    collect_reviews,
    extract_pendencies,
    has_blocking_pendencies,
    make_pendency,
)
from espaut.store import init_store


def test_pendency_state_machine() -> dict[str, str]:
    sm = build_pendency_machine()
    state = PendencyState.IDENTIFICADA
    state = sm.transition(state, "classify")
    c1 = state == PendencyState.CLASSIFICADA
    state = sm.transition(state, "start_treatment")
    c2 = state == PendencyState.EM_TRATAMENTO
    state = sm.transition(state, "apply_fix")
    c3 = state == PendencyState.CORRECAO_APLICADA
    state = sm.transition(state, "await_verify")
    c4 = state == PendencyState.AGUARDANDO_VERIFICACAO
    state = sm.transition(state, "resolve")
    c5 = state == PendencyState.RESOLVIDA
    return {
        "classify": "PASS" if c1 else "FAIL",
        "treat": "PASS" if c2 else "FAIL",
        "fix": "PASS" if c3 else "FAIL",
        "verify": "PASS" if c4 else "FAIL",
        "resolve": "PASS" if c5 else "FAIL",
    }


def test_pendency_justify() -> dict[str, str]:
    sm = build_pendency_machine()
    state = sm.transition(PendencyState.IDENTIFICADA, "classify")
    state = sm.transition(state, "justify")
    return {"justified": "PASS" if state == PendencyState.JUSTIFICADA else "FAIL"}


def test_pendency_block() -> dict[str, str]:
    sm = build_pendency_machine()
    state = sm.transition(PendencyState.EM_TRATAMENTO, "block")
    c1 = state == PendencyState.BLOQUEADA
    state = sm.transition(state, "unblock")
    c2 = state == PendencyState.CLASSIFICADA
    return {
        "blocked": "PASS" if c1 else "FAIL",
        "unblocked": "PASS" if c2 else "FAIL",
    }


def test_classify_review_changes_requested() -> dict[str, str]:
    review = {"state": "CHANGES_REQUESTED", "body": "fix the bug"}
    result = classify_review(review)
    return {"changes": "PASS" if result == CommentClassification.MUDANCA_REQUERIDA else "FAIL"}


def test_classify_review_approved() -> dict[str, str]:
    review = {"state": "APPROVED", "body": ""}
    result = classify_review(review)
    return {"approved": "PASS" if result == CommentClassification.INFORMACAO else "FAIL"}


def test_classify_comment_bug() -> dict[str, str]:
    comment = {"body": "There is a bug in this function"}
    result = classify_comment(comment)
    return {"bug": "PASS" if result == CommentClassification.DEFEITO_POSSIVEL else "FAIL"}


def test_classify_comment_question() -> dict[str, str]:
    comment = {"body": "Why did you choose this approach?"}
    result = classify_comment(comment)
    return {"question": "PASS" if result == CommentClassification.PERGUNTA else "FAIL"}


def test_classify_comment_suggestion() -> dict[str, str]:
    comment = {"body": "Maybe consider using a different algorithm"}
    result = classify_comment(comment)
    return {"suggestion": "PASS" if result == CommentClassification.SUGESTAO_OPCIONAL else "FAIL"}


def test_classify_comment_ambiguous() -> dict[str, str]:
    comment = {"body": "Interesting."}
    result = classify_comment(comment)
    return {"indeterminate": "PASS" if result == CommentClassification.INDETERMINADO else "FAIL"}


def test_make_pendency_blocking() -> dict[str, str]:
    p = make_pendency(
        origin_type="review",
        origin_reference="R-42",
        description="Changes requested by reviewer",
        classification=CommentClassification.MUDANCA_REQUERIDA,
        task_id="TASK-001",
        pr_number=42,
    )
    return {
        "has_id": "PASS" if p["pendency_id"].startswith("PND-") else "FAIL",
        "blocking": "PASS" if p["blocking_effect"] else "FAIL",
        "severity": "PASS" if p["severity"] == "blocking" else "FAIL",
        "state": "PASS" if p["resolution_state"] == "IDENTIFICADA" else "FAIL",
    }


def test_make_pendency_informational() -> dict[str, str]:
    p = make_pendency(
        origin_type="comment",
        origin_reference="C-10",
        description="Just a question",
        classification=CommentClassification.PERGUNTA,
        task_id="TASK-001",
    )
    return {
        "not_blocking": "PASS" if not p["blocking_effect"] else "FAIL",
        "severity": "PASS" if p["severity"] == "informational" else "FAIL",
    }


def test_extract_pendencies() -> dict[str, str]:
    report = {
        "pr_number": 42,
        "reviews": [
            {"review_id": 1, "reviewer": "alice", "state": "CHANGES_REQUESTED",
             "classification": "MUDANCA_REQUERIDA", "submitted_at": None},
            {"review_id": 2, "reviewer": "bob", "state": "APPROVED",
             "classification": "INFORMACAO", "submitted_at": None},
        ],
        "comments": [
            {"comment_id": 10, "author": "carol", "body_preview": "There is a bug here",
             "classification": "DEFEITO_POSSIVEL", "created_at": None, "comment_type": "code",
             "related_file": "main.py", "related_line": 42},
            {"comment_id": 11, "author": "dave", "body_preview": "Looks good",
             "classification": "INFORMACAO", "created_at": None, "comment_type": "general"},
        ],
    }
    pendencies = extract_pendencies(report, task_id="TASK-001")
    return {
        "count": "PASS" if len(pendencies) == 2 else f"FAIL: {len(pendencies)}",
        "first_blocking": "PASS" if pendencies[0]["blocking_effect"] else "FAIL",
        "second_blocking": "PASS" if pendencies[1]["blocking_effect"] else "FAIL",
    }


def test_has_blocking_pendencies() -> dict[str, str]:
    unresolved = [{"blocking_effect": True, "resolution_state": "IDENTIFICADA"}]
    resolved = [{"blocking_effect": True, "resolution_state": "RESOLVIDA"}]
    justified = [{"blocking_effect": True, "resolution_state": "JUSTIFICADA"}]
    none_blocking = [{"blocking_effect": False, "resolution_state": "IDENTIFICADA"}]
    return {
        "unresolved_blocks": "PASS" if has_blocking_pendencies(unresolved) else "FAIL",
        "resolved_ok": "PASS" if not has_blocking_pendencies(resolved) else "FAIL",
        "justified_ok": "PASS" if not has_blocking_pendencies(justified) else "FAIL",
        "non_blocking_ok": "PASS" if not has_blocking_pendencies(none_blocking) else "FAIL",
    }


def test_collect_reviews_mock() -> dict[str, str]:
    mock_reviews = [
        {"id": 1, "state": "CHANGES_REQUESTED", "body": "fix it",
         "user": {"login": "alice"}, "submitted_at": None},
    ]
    mock_comments = [
        {"id": 10, "body": "question?", "user": {"login": "bob"}, "created_at": None},
    ]
    mock_code_comments = [
        {"id": 20, "body": "This is wrong", "user": {"login": "carol"},
         "created_at": None, "path": "main.py", "line": 5},
    ]

    with tempfile.TemporaryDirectory() as tmp:
        store = init_store(Path(tmp))
        with patch("espaut.review.pr_reviews_json", return_value=mock_reviews), \
             patch("espaut.review.pr_comments_json", return_value=mock_comments), \
             patch("espaut.review.pr_review_comments_json", return_value=mock_code_comments), \
             patch("espaut.review.pr_review_threads_json", return_value=[]):
            result = collect_reviews(store, "TASK-001", 42)
            return {
                "collected": "PASS" if result["status"] == "collected" else "FAIL",
                "reviews_count": "PASS" if len(result["reviews"]) == 1 else "FAIL",
                "comments_count": "PASS" if len(result["comments"]) == 2 else "FAIL",
            }


ALL_TESTS = [
    ("pendency_sm", test_pendency_state_machine),
    ("pendency_justify", test_pendency_justify),
    ("pendency_block", test_pendency_block),
    ("classify_changes_requested", test_classify_review_changes_requested),
    ("classify_approved", test_classify_review_approved),
    ("classify_bug", test_classify_comment_bug),
    ("classify_question", test_classify_comment_question),
    ("classify_suggestion", test_classify_comment_suggestion),
    ("classify_ambiguous", test_classify_comment_ambiguous),
    ("make_pendency_blocking", test_make_pendency_blocking),
    ("make_pendency_info", test_make_pendency_informational),
    ("extract_pendencies", test_extract_pendencies),
    ("has_blocking", test_has_blocking_pendencies),
    ("collect_mock", test_collect_reviews_mock),
]


def main() -> int:
    total_pass = 0
    total_checks = 0

    for name, fn in ALL_TESTS:
        try:
            checks = fn()
        except Exception as exc:
            print(f"  [FAIL] {name} — exception: {exc}")
            total_checks += 1
            continue

        for sub, status in checks.items():
            total_checks += 1
            if status == "PASS":
                total_pass += 1
            print(f"  [{status}] {name}.{sub}")

    print(f"\nreview: {total_pass}/{total_checks} passed")
    return 0 if total_pass == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
