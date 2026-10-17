#!/usr/bin/env python3
"""Tests for espaut CLI — parser registration and handler wiring."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from espaut.cli import build_parser, main
from espaut.events import log_event
from espaut.store import init_store, new_id, write_json
from espaut.task import TaskState, make_task


EXPECTED_SUBCOMMANDS = [
    "init", "status", "task", "pr", "preserve",
    "checks", "reviews", "pendencies", "merge-check",
    "decide", "diagnose", "correct", "verify-correction",
    "recover", "audit", "sync", "watch",
]


def test_parser_subcommands() -> dict[str, str]:
    """All expected subcommands are registered in the parser."""
    parser = build_parser()
    # Extract subparser choices from the parser
    choices = set()
    for action in parser._subparsers._actions:
        if hasattr(action, "choices") and action.choices:
            choices.update(action.choices.keys())
    results = {}
    for cmd in EXPECTED_SUBCOMMANDS:
        results[cmd] = "PASS" if cmd in choices else f"FAIL: {cmd} not in {choices}"
    return results


def _make_test_env(path: Path) -> tuple[Path, str]:
    """Set up a git repo + .espaut/ + one active task. Returns (store, task_id)."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "config", "user.name", "T"], capture_output=True, check=True, cwd=str(path))
    (path / "a.txt").write_text("init")
    subprocess.run(["git", "add", "a.txt"], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "commit", "-m", "init"], capture_output=True, check=True, cwd=str(path))
    store = init_store(path)
    task_id = new_id("TASK-")
    task = make_task(task_id=task_id, repository_id=str(path), title="test task")
    task["state"] = TaskState.EM_EXECUCAO.value
    task["pr_number"] = 42
    task["branch_name"] = "main"
    write_json(store / "tasks" / f"{task_id}.json", task)
    return store, task_id


def test_cmd_init() -> dict[str, str]:
    """espaut init works in a git repo."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
        with patch("espaut.cli.repo_root", return_value=str(path)), \
             patch("espaut.cli.is_git_repo", return_value=True):
            sys.argv = ["espaut", "init"]
            rc = main()
        return {"init_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_checks() -> dict[str, str]:
    """espaut checks calls query_checks and prints output."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_report = {
            "checks": [{"name": "ci/build", "normalized_result": "APROVADO"}],
            "aggregate": "APROVADO",
        }
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.query_checks", return_value=mock_report):
            sys.argv = ["espaut", "checks", "--task", task_id]
            rc = main()
        return {"checks_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_reviews() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_report = {
            "reviews": [{"author": "alice", "state": "APPROVED", "classification": "APROVADO"}],
            "comments": [],
            "code_comments": [],
        }
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.collect_reviews", return_value=mock_report):
            sys.argv = ["espaut", "reviews", "--task", task_id]
            rc = main()
        return {"reviews_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_pendencies_no_blocking() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_report = {"reviews": [], "comments": [], "code_comments": []}
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.collect_reviews", return_value=mock_report), \
             patch("espaut.cli.extract_pendencies", return_value=[]), \
             patch("espaut.cli.has_blocking_pendencies", return_value=False):
            sys.argv = ["espaut", "pendencies", "--task", task_id]
            rc = main()
        return {"no_blocking": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_pendencies_with_blocking() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_pend = [{"pendency_id": "PND-001", "is_blocking": True, "classification": "DEFEITO_CONFIRMADO", "description": "bug"}]
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.collect_reviews", return_value={"reviews": [], "comments": [], "code_comments": []}), \
             patch("espaut.cli.extract_pendencies", return_value=mock_pend), \
             patch("espaut.cli.has_blocking_pendencies", return_value=True):
            sys.argv = ["espaut", "pendencies", "--task", task_id]
            rc = main()
        return {"blocking_exit1": "PASS" if rc == 1 else f"FAIL: rc={rc}"}


def test_cmd_merge_check() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_result = {
            "verdict": "APTA_PARA_INTEGRACAO",
            "criteria": [{"criterion": "C1_preservation", "status": "PASS", "detail": "ok"}],
        }
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.query_pr_state", return_value={"status": "available", "head_sha": "abc123"}), \
             patch("espaut.cli.query_checks", return_value={}), \
             patch("espaut.cli.collect_reviews", return_value={"reviews": [], "comments": [], "code_comments": []}), \
             patch("espaut.cli.extract_pendencies", return_value=[]), \
             patch("espaut.cli.evaluate_merge_eligibility", return_value=mock_result):
            sys.argv = ["espaut", "merge-check", "--task", task_id]
            rc = main()
        return {"merge_apta": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_decide() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        store = init_store(Path(tmp))
        mock_dec = {"decision_id": "DEC-abc123"}
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.make_decision", return_value=mock_dec), \
             patch("espaut.cli.record_decision", return_value=Path("/fake")):
            sys.argv = ["espaut", "decide", "--task", "TASK-001", "--type", "approve", "--value", "yes"]
            rc = main()
        return {"decide_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_diagnose() -> dict[str, str]:
    mock_dgn = {"diagnosis_id": "DGN-abc123", "diagnosis_source": "human"}
    with patch("espaut.cli.make_diagnosis", return_value=mock_dgn):
        sys.argv = ["espaut", "diagnose", "--pendency", "PND-001", "--cause", "null ptr"]
        rc = main()
    return {"diagnose_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_correct_dryrun() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.run_correction_check", return_value={"status": "allowed"}):
            sys.argv = ["espaut", "correct", "--task", task_id, "--pendency", "PND-001"]
            rc = main()
        return {"dryrun_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_recover() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_result = {"task_id": task_id, "status": "recovered", "discrepancies": [], "actions": []}
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.recover_task_state", return_value=mock_result):
            sys.argv = ["espaut", "recover", "--task", task_id]
            rc = main()
        return {"recover_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_audit() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        mock_report = {
            "task_id": task_id, "state": "EM_EXECUCAO",
            "event_count": 3, "evidence_count": 1, "decision_count": 0,
            "commits": ["abc"], "pr_number": 42, "timeline": [], "evidence": [], "decisions": [],
        }
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.audit_task", return_value=mock_report):
            sys.argv = ["espaut", "audit", "--task", task_id]
            rc = main()
        return {"audit_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


def test_cmd_sync() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.check_sync_state", return_value={"status": "no_remote", "local_sha": "abc123"}), \
             patch("espaut.cli.detect_pending_preservation", return_value={"status": "clean"}):
            sys.argv = ["espaut", "sync", "--task", task_id]
            rc = main()
        return {"sync_missing_remote": "PASS" if rc == 1 else f"FAIL: rc={rc}"}


def test_cmd_watch() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        store, task_id = _make_test_env(path)
        with patch("espaut.cli.require_store", return_value=store), \
             patch("espaut.cli.check_sync_state", return_value={"status": "synchronized"}), \
             patch("espaut.cli.check_base_branch_updated", return_value={"status": "unchanged"}), \
             patch("espaut.cli.check_merge_conflict", return_value={"status": "no_conflict"}), \
             patch("espaut.cli.detect_pending_preservation", return_value={"status": "clean"}):
            sys.argv = ["espaut", "watch", "--task", task_id, "--base", "main"]
            rc = main()
        return {"watch_ok": "PASS" if rc == 0 else f"FAIL: rc={rc}"}


ALL_TESTS = [
    ("parser_subcommands", test_parser_subcommands),
    ("cmd_init", test_cmd_init),
    ("cmd_checks", test_cmd_checks),
    ("cmd_reviews", test_cmd_reviews),
    ("cmd_pendencies_no_blocking", test_cmd_pendencies_no_blocking),
    ("cmd_pendencies_with_blocking", test_cmd_pendencies_with_blocking),
    ("cmd_merge_check", test_cmd_merge_check),
    ("cmd_decide", test_cmd_decide),
    ("cmd_diagnose", test_cmd_diagnose),
    ("cmd_correct_dryrun", test_cmd_correct_dryrun),
    ("cmd_recover", test_cmd_recover),
    ("cmd_audit", test_cmd_audit),
    ("cmd_sync", test_cmd_sync),
    ("cmd_watch", test_cmd_watch),
]


def main_test() -> int:
    total_pass = 0
    total_checks = 0

    for name, fn in ALL_TESTS:
        try:
            checks = fn()
        except SystemExit:
            print(f"  [FAIL] {name} — unexpected SystemExit")
            total_checks += 1
            continue
        except Exception as exc:
            print(f"  [FAIL] {name} — exception: {exc}")
            total_checks += 1
            continue

        for sub, status in checks.items():
            total_checks += 1
            passed = status == "PASS"
            if passed:
                total_pass += 1
            print(f"  [{status}] {name}.{sub}")

    print(f"\ncli: {total_pass}/{total_checks} passed")
    return 0 if total_pass == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main_test())
