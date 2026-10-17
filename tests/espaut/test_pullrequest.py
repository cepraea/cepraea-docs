#!/usr/bin/env python3
"""Tests for PR lifecycle — unit tests with mock gh responses."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from espaut.pullrequest import (
    find_existing_pr,
    query_pr_state,
    reconcile_pr_state,
    update_task_pr,
)
from espaut.store import init_store, write_json


def _mock_pr_view(data: dict[str, Any] | None):
    """Return a mock for pr_view_json."""
    def mock_fn(pr_ref, fields=None, cwd=None):
        return data
    return mock_fn


def _mock_pr_list(data: list[dict[str, Any]]):
    """Return a mock for pr_list_json."""
    def mock_fn(fields=None, state="open", cwd=None):
        return data
    return mock_fn


def test_find_existing_by_number() -> dict[str, str]:
    task = {"task_id": "TASK-001", "branch_name": "feat-1", "pr_number": 42}
    mock_data = {"number": 42, "state": "OPEN", "headRefName": "feat-1"}
    with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(mock_data)):
        result = find_existing_pr(task)
        return {
            "found": "PASS" if result is not None else "FAIL",
            "correct_number": "PASS" if result and result["number"] == 42 else "FAIL",
        }


def test_find_existing_by_branch() -> dict[str, str]:
    task = {"task_id": "TASK-002", "branch_name": "feat-2", "pr_number": None}
    prs = [
        {"number": 10, "headRefName": "other"},
        {"number": 11, "headRefName": "feat-2"},
    ]
    with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(None)):
        with patch("espaut.pullrequest.pr_list_json", _mock_pr_list(prs)):
            result = find_existing_pr(task)
            return {
                "found": "PASS" if result is not None else "FAIL",
                "correct_number": "PASS" if result and result["number"] == 11 else "FAIL",
            }


def test_find_no_match() -> dict[str, str]:
    task = {"task_id": "TASK-003", "branch_name": "feat-3", "pr_number": None}
    with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(None)):
        with patch("espaut.pullrequest.pr_list_json", _mock_pr_list([])):
            result = find_existing_pr(task)
            return {"not_found": "PASS" if result is None else "FAIL"}


def test_query_pr_state_available() -> dict[str, str]:
    mock_data = {
        "number": 42,
        "state": "OPEN",
        "headRefOid": "abc123",
        "headRefName": "feat-1",
        "baseRefName": "main",
        "url": "https://github.com/test/repo/pull/42",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "mergedAt": None,
        "mergeCommit": None,
    }
    with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(mock_data)):
        result = query_pr_state(42)
        return {
            "available": "PASS" if result["status"] == "available" else "FAIL",
            "head_sha": "PASS" if result["head_sha"] == "abc123" else "FAIL",
            "review": "PASS" if result["review_decision"] == "APPROVED" else "FAIL",
        }


def test_query_pr_state_unavailable() -> dict[str, str]:
    with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(None)):
        result = query_pr_state(999)
        return {
            "unavailable": "PASS" if result["status"] == "unavailable" else "FAIL",
        }


def test_reconcile_no_pr() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        store = init_store(Path(tmp))
        task = {"task_id": "TASK-001", "pr_number": None}
        result = reconcile_pr_state(store, task)
        return {"no_pr": "PASS" if result["status"] == "no_pr" else "FAIL"}


def test_reconcile_merged() -> dict[str, str]:
    mock_data = {
        "number": 42,
        "state": "CLOSED",
        "headRefOid": "abc123",
        "headRefName": "feat-1",
        "baseRefName": "main",
        "url": "https://github.com/test/repo/pull/42",
        "isDraft": False,
        "reviewDecision": None,
        "mergedAt": "2026-10-17T10:00:00Z",
        "mergeCommit": {"oid": "def456"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        store = init_store(Path(tmp))
        task = {"task_id": "TASK-001", "pr_number": 42, "base_commit_sha": "abc123"}
        with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(mock_data)):
            result = reconcile_pr_state(store, task)
            return {
                "reconciled": "PASS" if result["status"] == "reconciled" else "FAIL",
                "merged_detected": "PASS" if "pr_merged_externally" in result.get("changes", []) else "FAIL",
            }


def test_reconcile_sha_diverged() -> dict[str, str]:
    mock_data = {
        "number": 42,
        "state": "OPEN",
        "headRefOid": "new_sha",
        "headRefName": "feat-1",
        "baseRefName": "main",
        "url": "https://github.com/test/repo/pull/42",
        "isDraft": False,
        "reviewDecision": None,
        "mergedAt": None,
        "mergeCommit": None,
    }
    with tempfile.TemporaryDirectory() as tmp:
        store = init_store(Path(tmp))
        task = {"task_id": "TASK-001", "pr_number": 42, "head_commit_sha": "old_sha"}
        with patch("espaut.pullrequest.pr_view_json", _mock_pr_view(mock_data)):
            result = reconcile_pr_state(store, task)
            return {
                "diverged": "PASS" if "head_sha_diverged" in result.get("changes", []) else "FAIL",
            }


def test_update_task_pr() -> dict[str, str]:
    task = {"task_id": "TASK-001", "pr_number": None}
    updated = update_task_pr(task, 42, url="https://gh/pr/42", head_sha="abc123")
    return {
        "pr_number": "PASS" if updated["pr_number"] == 42 else "FAIL",
        "url": "PASS" if updated.get("pr_url") == "https://gh/pr/42" else "FAIL",
        "sha": "PASS" if "abc123" == updated.get("head_commit_sha") else "FAIL",
    }


ALL_TESTS = [
    ("find_by_number", test_find_existing_by_number),
    ("find_by_branch", test_find_existing_by_branch),
    ("find_no_match", test_find_no_match),
    ("query_available", test_query_pr_state_available),
    ("query_unavailable", test_query_pr_state_unavailable),
    ("reconcile_no_pr", test_reconcile_no_pr),
    ("reconcile_merged", test_reconcile_merged),
    ("reconcile_diverged", test_reconcile_sha_diverged),
    ("update_task_pr", test_update_task_pr),
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

    print(f"\npullrequest: {total_pass}/{total_checks} passed")
    return 0 if total_pass == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
