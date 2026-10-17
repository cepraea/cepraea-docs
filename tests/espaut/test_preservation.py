#!/usr/bin/env python3
"""Integration tests for the preservation flow using real temp git repos."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from espaut.preservation import (
    PreservationState,
    build_preservation_machine,
    classify_changes,
    detect_changes,
    run_preservation,
)
from espaut.store import init_store


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        capture_output=True, check=True, cwd=str(path),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        capture_output=True, check=True, cwd=str(path),
    )


def _make_commit(path: Path, filename: str, content: str, msg: str) -> None:
    (path / filename).write_text(content)
    subprocess.run(["git", "add", filename], capture_output=True, check=True, cwd=str(path))
    subprocess.run(["git", "commit", "-m", msg], capture_output=True, check=True, cwd=str(path))


def test_preservation_state_machine() -> dict[str, str]:
    sm = build_preservation_machine()
    state = PreservationState.LIMPO
    state = sm.transition(state, "detect")
    c1 = state == PreservationState.MODIFICACAO_DETECTADA
    state = sm.transition(state, "prepare")
    c2 = state == PreservationState.PREPARACAO_PENDENTE
    state = sm.transition(state, "stage")
    c3 = state == PreservationState.PREPARADA
    state = sm.transition(state, "commit")
    c4 = state == PreservationState.COMMITADA_LOCALMENTE
    state = sm.transition(state, "push")
    c5 = state == PreservationState.PUSH_PENDENTE
    state = sm.transition(state, "push_confirmed")
    c6 = state == PreservationState.SINCRONIZADA
    return {
        "detect": "PASS" if c1 else "FAIL",
        "prepare": "PASS" if c2 else "FAIL",
        "stage": "PASS" if c3 else "FAIL",
        "commit": "PASS" if c4 else "FAIL",
        "push": "PASS" if c5 else "FAIL",
        "sync": "PASS" if c6 else "FAIL",
    }


def test_classify_changes() -> dict[str, str]:
    entries = [
        {"index": "M", "worktree": " ", "path": "src/main.py"},
        {"index": "?", "worktree": "?", "path": "notes.txt"},
        {"index": "M", "worktree": " ", "path": "docs/readme.md"},
    ]
    eligible, excluded = classify_changes(entries, [])
    c1 = len(eligible) == 3 and len(excluded) == 0

    eligible2, excluded2 = classify_changes(entries, ["src/*"])
    c2 = eligible2 == ["src/main.py"] and len(excluded2) == 2

    return {
        "all_eligible": "PASS" if c1 else "FAIL",
        "pattern_filter": "PASS" if c2 else "FAIL",
    }


def test_detect_clean_repo() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _make_commit(path, "a.txt", "hello", "init")
        store = init_store(path)
        result = detect_changes(store, "TASK-001", str(path))
        return {
            "clean": "PASS" if result["status"] == "clean" else "FAIL",
        }


def test_detect_dirty_repo() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _make_commit(path, "a.txt", "hello", "init")
        store = init_store(path)
        (path / "a.txt").write_text("changed")
        result = detect_changes(store, "TASK-001", str(path))
        return {
            "changes_found": "PASS" if result["status"] == "changes_found" else "FAIL",
            "has_eligible": "PASS" if len(result["eligible"]) > 0 else "FAIL",
        }


def test_dry_run_preservation() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _make_commit(path, "a.txt", "hello", "init")
        store = init_store(path)
        (path / "a.txt").write_text("changed")
        report = run_preservation(store, "TASK-001", "test commit", dry_run=True, cwd=str(path))
        return {
            "dry_run": "PASS" if report["dry_run"] else "FAIL",
            "outcome": "PASS" if report["outcome"] == "dry_run_complete" else "FAIL",
            "would_stage": "PASS" if "a.txt" in report.get("would_stage", []) else "FAIL",
        }


def test_full_preservation_local() -> dict[str, str]:
    """Full commit without push (no remote configured)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _make_commit(path, "a.txt", "hello", "init")
        subprocess.run(["git", "switch", "-c", "test/task"], capture_output=True, check=True, cwd=str(path))
        store = init_store(path)
        (path / "b.txt").write_text("new file")
        report = run_preservation(store, "TASK-001", "preserve b.txt", dry_run=False, cwd=str(path))

        steps = {s["step"]: s["result"]["status"] for s in report.get("steps", [])}
        checks: dict[str, str] = {}
        checks["staged"] = "PASS" if steps.get("stage") == "staged" else "FAIL"
        checks["committed"] = "PASS" if steps.get("commit") == "committed" else "FAIL"

        r = subprocess.run(
            ["git", "log", "--oneline"],
            capture_output=True, text=True, cwd=str(path),
        )
        checks["in_log"] = "PASS" if "preserve b.txt" in r.stdout else "FAIL"

        return checks


def test_nothing_to_preserve() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        _init_repo(path)
        _make_commit(path, "a.txt", "hello", "init")
        store = init_store(path)
        report = run_preservation(store, "TASK-001", "no-op", dry_run=False, cwd=str(path))
        return {
            "nothing": "PASS" if report["outcome"] == "nothing_to_preserve" else "FAIL",
        }


ALL_TESTS = [
    ("preservation_sm", test_preservation_state_machine),
    ("classify_changes", test_classify_changes),
    ("detect_clean", test_detect_clean_repo),
    ("detect_dirty", test_detect_dirty_repo),
    ("dry_run", test_dry_run_preservation),
    ("full_local", test_full_preservation_local),
    ("nothing_to_preserve", test_nothing_to_preserve),
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

    print(f"\npreservation: {total_pass}/{total_checks} passed")
    return 0 if total_pass == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
