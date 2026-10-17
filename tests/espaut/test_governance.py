#!/usr/bin/env python3
"""Tests for governance, invariants, and merge eligibility."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from espaut.governance import (
    check_c1_preservation,
    check_c2_sync,
    check_c3_verification,
    check_c4_review,
    check_c5_pendencies,
    check_c6_acceptance,
    check_c7_authority,
    check_c8_currency,
    evaluate_merge_eligibility,
    make_decision,
    record_decision,
)
from espaut.invariants import (
    inv_001_identity,
    inv_002_preservation,
    inv_003_traceability,
    inv_004_revision_validity,
    inv_005_evidence,
    inv_006_pendencies,
    inv_008_authority,
    inv_009_idempotency,
    inv_011_secrets,
    inv_012_closure,
    inv_013_currency,
    inv_014_fidelity,
)
from espaut.store import init_store


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "config", "user.name", "T"], capture_output=True, check=True, cwd=str(path))


def _commit(path: Path, fname: str, content: str, msg: str) -> None:
    (path / fname).write_text(content)
    subprocess.run(["git", "add", fname], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "commit", "-m", msg], capture_output=True, check=True, cwd=str(path))


# -- Invariant tests --

def test_inv_001() -> dict[str, str]:
    task = {"task_id": "T1", "branch_name": "feat-1"}
    others = [{"task_id": "T2", "branch_name": "feat-2"}]
    ok, _ = inv_001_identity(task, others)
    conflict = [{"task_id": "T3", "branch_name": "feat-1"}]
    fail, _ = inv_001_identity(task, conflict)
    return {"unique": "PASS" if ok else "FAIL", "conflict": "PASS" if not fail else "FAIL"}


def test_inv_002() -> dict[str, str]:
    ok, _ = inv_002_preservation(["a.py", "b.py"], ["a.py", "b.py"])
    fail, _ = inv_002_preservation(["a.py", "b.py"], ["a.py"])
    return {"all_committed": "PASS" if ok else "FAIL", "missing": "PASS" if not fail else "FAIL"}


def test_inv_003() -> dict[str, str]:
    ok, _ = inv_003_traceability({"commit_shas": ["abc"]})
    fail, _ = inv_003_traceability({"commit_shas": []})
    return {"has_commits": "PASS" if ok else "FAIL", "no_commits": "PASS" if not fail else "FAIL"}


def test_inv_004() -> dict[str, str]:
    ok, _ = inv_004_revision_validity("abc", "abc")
    fail, _ = inv_004_revision_validity("abc", "def")
    return {"match": "PASS" if ok else "FAIL", "mismatch": "PASS" if not fail else "FAIL"}


def test_inv_005() -> dict[str, str]:
    ok, _ = inv_005_evidence("push done", {"source_system": "git"})
    fail, _ = inv_005_evidence("push done", None)
    ai_fail, _ = inv_005_evidence("push done", {"source_system": "ai_inference"})
    return {
        "real_evidence": "PASS" if ok else "FAIL",
        "no_evidence": "PASS" if not fail else "FAIL",
        "ai_only": "PASS" if not ai_fail else "FAIL",
    }


def test_inv_006() -> dict[str, str]:
    ok, _ = inv_006_pendencies([{"blocking_effect": True, "resolution_state": "RESOLVIDA", "pendency_id": "P1"}])
    fail, _ = inv_006_pendencies([{"blocking_effect": True, "resolution_state": "IDENTIFICADA", "pendency_id": "P2"}])
    return {"resolved": "PASS" if ok else "FAIL", "unresolved": "PASS" if not fail else "FAIL"}


def test_inv_008() -> dict[str, str]:
    ok, _ = inv_008_authority("merge", "merge_allowed", {"merge_allowed"})
    fail, _ = inv_008_authority("merge", "merge_allowed", set())
    return {"granted": "PASS" if ok else "FAIL", "denied": "PASS" if not fail else "FAIL"}


def test_inv_009() -> dict[str, str]:
    ok, _ = inv_009_idempotency("op-1", {"op-2"})
    fail, _ = inv_009_idempotency("op-1", {"op-1"})
    return {"new_op": "PASS" if ok else "FAIL", "duplicate": "PASS" if not fail else "FAIL"}


def test_inv_011() -> dict[str, str]:
    ok, _ = inv_011_secrets("normal code here")
    fail, _ = inv_011_secrets("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn")
    return {"clean": "PASS" if ok else "FAIL", "secret": "PASS" if not fail else "FAIL"}


def test_inv_012() -> dict[str, str]:
    ok, _ = inv_012_closure(True, True, True)
    fail, _ = inv_012_closure(True, True, False)
    return {"all_met": "PASS" if ok else "FAIL", "tests_pass_but_no": "PASS" if not fail else "FAIL"}


def test_inv_013() -> dict[str, str]:
    ok, _ = inv_013_currency("abc", "abc")
    fail, _ = inv_013_currency("abc", "def")
    return {"current": "PASS" if ok else "FAIL", "stale": "PASS" if not fail else "FAIL"}


def test_inv_014() -> dict[str, str]:
    ok, _ = inv_014_fidelity({"origin_reference": "comment-42"})
    fail, _ = inv_014_fidelity({})
    return {"has_ref": "PASS" if ok else "FAIL", "no_ref": "PASS" if not fail else "FAIL"}


# -- Governance tests --

def test_make_decision() -> dict[str, str]:
    d = make_decision("TASK-001", "merge_authorization", "user", "approved")
    return {
        "has_id": "PASS" if d["decision_id"].startswith("DEC-") else "FAIL",
        "type": "PASS" if d["decision_type"] == "merge_authorization" else "FAIL",
        "value": "PASS" if d["decision_value"] == "approved" else "FAIL",
    }


def test_record_decision() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        store = init_store(Path(tmp))
        d = make_decision("TASK-001", "scope_change", "user", "accepted")
        path = record_decision(store, d)
        return {"saved": "PASS" if path.exists() else "FAIL"}


def test_c1_clean() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _commit(path, "a.txt", "init", "init")
        result = check_c1_preservation({}, str(path))
        return {"pass": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c1_dirty() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _commit(path, "a.txt", "init", "init")
        (path / "a.txt").write_text("changed")
        result = check_c1_preservation({}, str(path))
        return {"fail": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c3_pass() -> dict[str, str]:
    result = check_c3_verification({"aggregate": "APROVADO"})
    return {"pass": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c3_fail() -> dict[str, str]:
    result = check_c3_verification({"aggregate": "REPROVADO"})
    return {"fail": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c3_not_configured() -> dict[str, str]:
    result = check_c3_verification({"aggregate": "NAO_CONFIGURADO"})
    return {"pass_no_ci": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c3_not_configured_ci_required() -> dict[str, str]:
    result = check_c3_verification({"aggregate": "NAO_CONFIGURADO"}, ci_required=True)
    return {"fail_ci_required": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c4_approved() -> dict[str, str]:
    result = check_c4_review({"reviews": [{"state": "APPROVED"}]})
    return {"pass": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c4_no_approval() -> dict[str, str]:
    result = check_c4_review({"reviews": [{"state": "COMMENTED"}]})
    return {"fail": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c5_clear() -> dict[str, str]:
    result = check_c5_pendencies([])
    return {"pass": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c5_blocking() -> dict[str, str]:
    pends = [{"pendency_id": "P1", "blocking_effect": True, "resolution_state": "IDENTIFICADA"}]
    result = check_c5_pendencies(pends)
    return {"fail": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c7_authorized() -> dict[str, str]:
    task = {"task_id": "T1"}
    decisions = [{"task_id": "T1", "decision_type": "merge_authorization", "decision_value": "approved", "authority": "user"}]
    result = check_c7_authority(task, decisions)
    return {"pass": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c7_no_auth() -> dict[str, str]:
    result = check_c7_authority({"task_id": "T1"}, [])
    return {"fail": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c8_current() -> dict[str, str]:
    report = {"checks": [{"evaluated_revision": "abc123"}]}
    result = check_c8_currency(report, "abc123")
    return {"pass": "PASS" if result["status"] == "PASS" else "FAIL"}


def test_c8_stale() -> dict[str, str]:
    report = {"checks": [{"evaluated_revision": "old123"}]}
    result = check_c8_currency(report, "new456")
    return {"fail": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c8_no_revision() -> dict[str, str]:
    report = {"checks": [{"name": "ci/build"}]}
    result = check_c8_currency(report, "abc123")
    return {"fail_no_rev": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_c8_no_head() -> dict[str, str]:
    report = {"checks": [{"evaluated_revision": "abc123"}]}
    result = check_c8_currency(report, None)
    return {"fail_no_head": "PASS" if result["status"] == "FAIL" else "FAIL"}


def test_full_eligibility_pass() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _commit(path, "a.txt", "init", "init")
        store = init_store(path)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(path),
        ).stdout.strip()

        task = {
            "task_id": "T1", "acceptance_criteria": ["behavior"],
            "acceptance_revision": sha,
            "acceptance_results": {"behavior": {
                "status": "PASS", "revision": sha,
                "evidence_reference": "test-run:TST-001",
            }},
            "commit_shas": [sha],
        }
        checks_report = {"aggregate": "APROVADO", "checks": [{"evaluated_revision": sha}]}
        reviews_report = {"reviews": [{"state": "APPROVED"}]}
        decisions = [{"task_id": "T1", "decision_type": "merge_authorization", "decision_value": "approved", "authority": "user"}]

        with patch("espaut.governance.remote_head_sha", return_value=sha):
            result = evaluate_merge_eligibility(
                store, task, checks_report, reviews_report, [], decisions, str(path),
            )
            pass_count = sum(1 for c in result["criteria"] if c["status"] == "PASS")
            return {
                "verdict": "PASS" if result["verdict"] == "APTA_PARA_INTEGRACAO" else "FAIL",
                "all_8_pass": "PASS" if pass_count == 8 else f"FAIL: {pass_count}/8",
            }


ALL_TESTS = [
    ("inv_001", test_inv_001),
    ("inv_002", test_inv_002),
    ("inv_003", test_inv_003),
    ("inv_004", test_inv_004),
    ("inv_005", test_inv_005),
    ("inv_006", test_inv_006),
    ("inv_008", test_inv_008),
    ("inv_009", test_inv_009),
    ("inv_011", test_inv_011),
    ("inv_012", test_inv_012),
    ("inv_013", test_inv_013),
    ("inv_014", test_inv_014),
    ("make_decision", test_make_decision),
    ("record_decision", test_record_decision),
    ("c1_clean", test_c1_clean),
    ("c1_dirty", test_c1_dirty),
    ("c3_pass", test_c3_pass),
    ("c3_fail", test_c3_fail),
    ("c3_not_configured", test_c3_not_configured),
    ("c3_not_configured_ci_required", test_c3_not_configured_ci_required),
    ("c4_approved", test_c4_approved),
    ("c4_no_approval", test_c4_no_approval),
    ("c5_clear", test_c5_clear),
    ("c5_blocking", test_c5_blocking),
    ("c7_authorized", test_c7_authorized),
    ("c7_no_auth", test_c7_no_auth),
    ("c8_current", test_c8_current),
    ("c8_stale", test_c8_stale),
    ("c8_no_revision", test_c8_no_revision),
    ("c8_no_head", test_c8_no_head),
    ("full_eligibility", test_full_eligibility_pass),
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

    print(f"\ngovernance: {total_pass}/{total_checks} passed")
    return 0 if total_pass == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
