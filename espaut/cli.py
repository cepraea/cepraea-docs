#!/usr/bin/env python3
"""CLI for ESP-AUT-GIT-001 — argparse root and subcommands."""

from __future__ import annotations

import argparse
import json
import sys
import subprocess
from pathlib import Path
from typing import Any

from espaut import __version__
from espaut.checks import compute_aggregate, query_checks
from espaut.correction import make_attempt, make_diagnosis, record_attempt_result, run_correction_check, apply_agent_correction, check_scope_compliance
from espaut.git_ops import (
    current_branch,
    checkout_branch,
    create_branch,
    remote_head_sha,
    has_changes,
    has_incomplete_operation,
    head_sha,
    is_git_repo,
    repo_root,
    worktree_status,
)
from espaut.governance import evaluate_merge_eligibility, make_decision, record_decision
from espaut.preservation import detect_changes, run_preservation
from espaut.pullrequest import create_or_reuse_pr, query_pr_state, update_task_pr
from espaut.recovery import audit_task, recover_task_state
from espaut.review import collect_reviews, extract_pendencies, has_blocking_pendencies
from espaut.store import (
    find_store,
    init_store,
    load_config,
    new_id,
    read_json,
    require_store,
    write_json,
)
from espaut.task import TaskState, make_task
from espaut.triggers import (
    check_base_branch_updated,
    check_merge_conflict,
    check_sync_state,
    detect_pending_preservation,
)


def _resolve_task(store: Path, task_id: str | None) -> dict[str, Any] | None:
    """Find a task by ID or return the sole active task."""
    task_dir = store / "tasks"
    if task_id:
        path = task_dir / f"{task_id}.json"
        return read_json(path) if path.exists() else None
    task_files = list(task_dir.glob("TASK-*.json")) if task_dir.exists() else []
    active = [
        read_json(tf) for tf in task_files
        if read_json(tf).get("state") not in (
            TaskState.INTEGRADA,
            TaskState.ENCERRADA_SEM_MERGE,
            TaskState.INTERROMPIDA,
        )
    ]
    return active[0] if len(active) == 1 else None


def _require_task(store: Path, task_id: str | None) -> dict[str, Any] | None:
    """Resolve task or print error. Returns None on failure."""
    task = _resolve_task(store, task_id)
    if not task:
        print("error: task not found or ambiguous; specify --task", file=sys.stderr)
    return task


def _require_pr(task: dict[str, Any]) -> int | None:
    """Extract pr_number or print error. Returns None on failure."""
    pr_number = task.get("pr_number")
    if not pr_number:
        print(f"error: task {task['task_id']} has no associated PR", file=sys.stderr)
    return pr_number


# -- Core subcommands (init, status, task, preserve, pr) ----------------------


def cmd_init(args: argparse.Namespace) -> int:
    if not is_git_repo():
        print("error: not inside a git repository", file=sys.stderr)
        return 1
    root = Path(repo_root() or ".")
    store = init_store(root)
    print(f"initialized {store}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not is_git_repo():
        print("error: not inside a git repository", file=sys.stderr)
        return 1

    branch = current_branch()
    sha = head_sha()
    entries = worktree_status()
    incomplete = has_incomplete_operation()

    print(f"branch={branch or '(detached)'}  head={sha or '(none)'}  changes={len(entries)}")

    if incomplete:
        print(f"warning: incomplete git operation: {incomplete}")

    if entries:
        modified = sum(1 for e in entries if e["worktree"] == "M" or e["index"] == "M")
        untracked = sum(1 for e in entries if e["index"] == "?" and e["worktree"] == "?")
        deleted = sum(1 for e in entries if e["worktree"] == "D" or e["index"] == "D")
        added = sum(1 for e in entries if e["index"] == "A")
        print(f"  modified={modified}  untracked={untracked}  deleted={deleted}  added={added}")

    store = find_store()
    if store:
        task_dir = store / "tasks"
        task_files = list(task_dir.glob("TASK-*.json")) if task_dir.exists() else []
        active = []
        for tf in task_files:
            t = read_json(tf)
            if t.get("state") not in (
                TaskState.INTEGRADA,
                TaskState.ENCERRADA_SEM_MERGE,
                TaskState.INTERROMPIDA,
            ):
                active.append(t)
        if active:
            for t in active:
                print(f"  task={t['task_id']}  state={t['state']}  branch={t.get('branch_name', '-')}")
        else:
            print("  no active tasks")
    else:
        print("  .espaut/ not initialized (run 'espaut init')")

    return 0


def cmd_task_create(args: argparse.Namespace) -> int:
    store = require_store()
    task_id = new_id("TASK-")
    root = repo_root() or "."
    branch = current_branch()
    if branch is None:
        print("error: detached HEAD; cannot register task", file=sys.stderr)
        return 1
    base_sha = remote_head_sha("origin", "main") or head_sha()
    if branch in ("main", "master"):
        if has_changes():
            print("error: preserve existing changes before creating a task branch", file=sys.stderr)
            return 1
        target = f"espaut/{task_id.lower()}"
        created = create_branch(target)
        if created.returncode != 0:
            print(f"error: cannot create task branch: {created.stderr}", file=sys.stderr)
            return 1
        moved = checkout_branch(target)
        if moved.returncode != 0:
            print(f"error: cannot switch task branch: {moved.stderr}", file=sys.stderr)
            return 1
        branch = target
    task = make_task(
        task_id=task_id,
        repository_id=root,
        title=args.title,
        objective=args.objective or "",
        authorized_scope=load_config(store).get("eligible_patterns", []),
    )
    task["branch_name"] = branch
    task["base_commit_sha"] = base_sha
    task["head_commit_sha"] = head_sha()
    write_json(store / "tasks" / f"{task_id}.json", task)
    print(f"task_id={task_id}  state={task['state']}  title={args.title}")
    return 0


def cmd_preserve(args: argparse.Namespace) -> int:
    store = require_store()
    task_id = args.task
    if not task_id:
        task = _resolve_task(store, None)
        if not task:
            print("error: no active task or ambiguous; specify --task", file=sys.stderr)
            return 1
        task_id = task["task_id"]

    dry_run = not args.confirm
    message = args.message or f"espaut: preserve task {task_id}"
    report = run_preservation(store, task_id, message, dry_run=dry_run)

    if dry_run:
        print(f"[dry-run] task={task_id}  outcome={report['outcome']}")
        for f in report.get("would_stage", []):
            print(f"  would stage: {f}")
        for f in report.get("would_exclude", []):
            print(f"  would exclude: {f}")
    else:
        print(f"task={task_id}  outcome={report['outcome']}")
        for step in report.get("steps", []):
            print(f"  {step['step']}: {step['result'].get('status', '?')}")

    return 0 if report.get("outcome") in ("synchronized", "nothing_to_preserve", "dry_run_complete") else 1


def cmd_pr_create(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    title = args.title or task.get("title", "PR")
    body = args.body or f"Task: {task['task_id']}\n\n{task.get('objective', '')}"
    result = create_or_reuse_pr(store, task, title, body, base=args.base)

    if result["status"] == "error":
        print(f"error: {result.get('error', 'unknown')}", file=sys.stderr)
        return 1

    pr_number = result["pr_number"]
    url = result.get("url", "")
    update_task_pr(task, pr_number, url, head_sha=head_sha())
    write_json(store / "tasks" / f"{task['task_id']}.json", task)
    print(f"task={task['task_id']}  pr={pr_number}  status={result['status']}  url={url}")
    return 0


def cmd_pr_status(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1
    pr_number = _require_pr(task)
    if not pr_number:
        return 1

    state = query_pr_state(pr_number)
    print(f"task={task['task_id']}  pr={pr_number}")
    print(f"  github_state={state.get('github_state', '?')}")
    print(f"  head_sha={state.get('head_sha', '?')}")
    print(f"  review_decision={state.get('review_decision', '?')}")
    print(f"  url={state.get('url', '?')}")
    return 0


# -- Checks subcommand --------------------------------------------------------


def cmd_checks(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1
    pr_number = _require_pr(task)
    if not pr_number:
        return 1

    report = query_checks(store, task["task_id"], pr_number)
    checks = report.get("checks", [])
    aggregate = report.get("aggregate", compute_aggregate(checks))

    print(f"task={task['task_id']}  pr={pr_number}  aggregate={aggregate}")
    for c in checks:
        print(f"  {c.get('name', '?')}: {c.get('normalized_result', '?')}")
    ci_required = load_config(store).get("ci_required", False)
    return 0 if aggregate == "APROVADO" or (aggregate == "NAO_CONFIGURADO" and not ci_required) else 1


# -- Reviews subcommand -------------------------------------------------------


def cmd_reviews(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1
    pr_number = _require_pr(task)
    if not pr_number:
        return 1

    report = collect_reviews(store, task["task_id"], pr_number)
    reviews = report.get("reviews", [])
    comments = report.get("comments", [])
    code_comments = [c for c in comments if c.get("comment_type") == "code"]

    print(f"task={task['task_id']}  pr={pr_number}")
    print(f"  reviews={len(reviews)}  comments={len(comments)}  code_comments={len(code_comments)}")
    for r in reviews:
        print(f"  review: author={r.get('reviewer', '?')}  state={r.get('state', '?')}  classification={r.get('classification', '?')}")
    return 0


# -- Pendencies subcommand ----------------------------------------------------


def cmd_pendencies(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1
    pr_number = _require_pr(task)
    if not pr_number:
        return 1

    report = collect_reviews(store, task["task_id"], pr_number)
    pendencies = extract_pendencies(report, task_id=task["task_id"], store=store)
    blocking = has_blocking_pendencies(pendencies)

    print(f"task={task['task_id']}  pr={pr_number}  total={len(pendencies)}  blocking={blocking}")
    for p in pendencies:
        flag = "[BLOCKING]" if p.get("blocking_effect") else "[info]"
        print(f"  {flag} {p.get('pendency_id', '?')}: {p.get('classification', '?')} — {p.get('description', '')[:80]}")
    return 1 if blocking else 0


# -- Merge-check subcommand ---------------------------------------------------


def cmd_merge_check(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    pr_number = task.get("pr_number")
    checks_report: dict[str, Any] = {}
    reviews_report: dict[str, Any] = {}
    pendencies: list[dict[str, Any]] = []

    if pr_number:
        remote_pr = query_pr_state(pr_number)
        pr_head = remote_pr.get("head_sha")
        if remote_pr.get("status") != "available" or not pr_head:
            print("error: PR head not verifiable", file=sys.stderr)
            return 1
        checks_report = query_checks(
            store, task["task_id"], pr_number, evaluated_revision=pr_head,
        )
        reviews_report = collect_reviews(store, task["task_id"], pr_number)
        reviews_report["review_decision"] = remote_pr.get("review_decision")
        reviews_report["is_draft"] = remote_pr.get("is_draft")
        pendencies = extract_pendencies(reviews_report, task_id=task["task_id"], store=store)
        # Recheck the remote head in case a new push invalidated the evidence.
        if query_pr_state(pr_number).get("head_sha") != pr_head:
            print("error: PR head changed during verification", file=sys.stderr)
            return 1

    decisions_dir = store / "decisions"
    decisions: list[dict[str, Any]] = []
    if decisions_dir.exists():
        for df in decisions_dir.glob("DEC-*.json"):
            dec = read_json(df)
            if dec.get("task_id") == task["task_id"]:
                decisions.append(dec)

    result = evaluate_merge_eligibility(
        store, task, checks_report, reviews_report, pendencies, decisions,
    )

    verdict = result.get("verdict", "?")
    print(f"task={task['task_id']}  verdict={verdict}")
    for criterion in result.get("criteria", []):
        status = criterion.get("status", "INDETERMINADO")
        print(f"  {criterion.get('criterion', '?')}: {status} — {criterion.get('detail', '')}")
    return 0 if verdict == "APTA_PARA_INTEGRACAO" else 1


# -- Decide subcommand --------------------------------------------------------


def cmd_decide(args: argparse.Namespace) -> int:
    store = require_store()
    decision = make_decision(
        task_id=args.task,
        decision_type=args.type,
        authority=args.authority or "cli_user",
        value=args.value,
        scope=args.scope or "",
    )
    path = record_decision(store, decision)
    print(f"decision_id={decision['decision_id']}  type={args.type}  value={args.value}")
    return 0


# -- Diagnose subcommand ------------------------------------------------------


def cmd_diagnose(args: argparse.Namespace) -> int:
    diagnosis = make_diagnosis(
        args.pendency,
        args.cause,
        source=args.source or "human",
    )
    print(f"diagnosis_id={diagnosis['diagnosis_id']}  pendency={args.pendency}  source={diagnosis['diagnosis_source']}")
    return 0


# -- Correct subcommand -------------------------------------------------------


def cmd_correct(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    attempts_path = store / "cache" / f"attempts-{args.pendency}.json"
    previous_attempts: list[dict[str, Any]] = []
    if attempts_path.exists():
        loaded = read_json(attempts_path)
        if isinstance(loaded, list):
            previous_attempts = loaded

    check = run_correction_check(store, task["task_id"], args.pendency, previous_attempts)

    if check["status"] == "blocked":
        print(f"error: correction blocked — {check.get('reason', 'max attempts reached')}", file=sys.stderr)
        return 1

    if not args.confirm:
        print(f"[dry-run] task={task['task_id']}  pendency={args.pendency}  status=allowed")
        return 0

    attempt = make_attempt(args.pendency, len(previous_attempts) + 1)
    changes: list[str] = []
    if getattr(args, "agent", None):
        ledger = store / "cache" / f"pendencies-PR{task.get('pr_number')}.json"
        pending = read_json(ledger) if ledger.exists() else []
        source = next((p for p in pending if p["pendency_id"] == args.pendency), None)
        if not source:
            print("error: source pendency not found in local GitHub review ledger", file=sys.stderr)
            return 1
        intervention = apply_agent_correction(
            store, task, source, args.agent, cwd=repo_root() or ".",
        )
        if intervention["status"] != "verified":
            print(f"error: correction not verified: {intervention.get('reason')}", file=sys.stderr)
            return 1
        changes = intervention["changed_files"]
    else:
        # Manually applied changes require the same scope and independent test gate.
        import subprocess
        config = load_config(store)
        scope = task.get("authorized_scope") or config.get("eligible_patterns") or []
        changes = [entry["path"] for entry in worktree_status()]
        tests = config.get("correction_test_command")
        safe, _ = check_scope_compliance(changes, scope)
        if not changes or not scope or not safe or not isinstance(tests, list) or not tests:
            print("error: manual correction requires scoped changes and configured tests", file=sys.stderr)
            return 1
        verification = subprocess.run(tests, capture_output=True, text=True, timeout=180, check=False)
        if verification.returncode != 0:
            print("error: correction verification failed", file=sys.stderr)
            return 1
    message = f"espaut: correction for {args.pendency} on task {task['task_id']}"
    report = run_preservation(store, task["task_id"], message, dry_run=False)

    outcome = report.get("outcome", "unknown")
    state = "CONCLUIDA" if outcome == "synchronized" else "FALHA"
    record_attempt_result(
        attempt, applied_changes=changes, validation_result="PASS",
        output_revision=head_sha() if state == "CONCLUIDA" else None,
        state=state,
    )
    previous_attempts.append(attempt)
    write_json(attempts_path, previous_attempts)

    print(f"task={task['task_id']}  pendency={args.pendency}  outcome={outcome}")
    return 0 if state == "CONCLUIDA" else 1


# -- Verify-correction subcommand ---------------------------------------------


def cmd_verify_correction(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1
    pr_number = _require_pr(task)
    if not pr_number:
        return 1

    checks_report = query_checks(store, task["task_id"], pr_number)
    reviews_report = collect_reviews(store, task["task_id"], pr_number)
    pendencies = extract_pendencies(reviews_report, task_id=task["task_id"], store=store)
    blocking = has_blocking_pendencies(pendencies)

    aggregate = checks_report.get("aggregate", "?")
    print(f"task={task['task_id']}  pr={pr_number}")
    print(f"  checks_aggregate={aggregate}")
    print(f"  pendencies={len(pendencies)}  blocking={blocking}")
    for p in pendencies:
        flag = "[BLOCKING]" if p.get("blocking_effect") else "[info]"
        print(f"  {flag} {p.get('pendency_id', '?')}: {p.get('classification', '?')}")
    return 0 if not blocking and aggregate == "APROVADO" else 1


# -- Recover subcommand -------------------------------------------------------


def cmd_recover(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    result = recover_task_state(store, task)
    print(f"task={task['task_id']}  status={result['status']}")
    if result.get("discrepancies"):
        for d in result["discrepancies"]:
            print(f"  discrepancy: {d['field']}  recorded={d['recorded']}  actual={d['actual']}")
    if result.get("actions"):
        for a in result["actions"]:
            print(f"  action: {a}")
    return 0


# -- Audit subcommand ---------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    report = audit_task(store, task)
    print(f"task={task['task_id']}  state={report.get('state', '?')}")
    print(f"  events={report['event_count']}  evidence={report['evidence_count']}  decisions={report['decision_count']}")
    print(f"  commits={len(report.get('commits', []))}  pr={report.get('pr_number', '-')}")

    if args.verbose:
        for evt in report.get("timeline", []):
            print(f"  {evt.get('occurred_at', '?')}  {evt.get('event_type', '?')}  {evt.get('detail', '')}")
    return 0


# -- Sync subcommand ----------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    sync = check_sync_state(store, task["task_id"])
    pending = detect_pending_preservation(store, task["task_id"])

    print(f"task={task['task_id']}  sync={sync['status']}")
    if sync.get("local_sha"):
        print(f"  local_sha={sync['local_sha'][:12]}")
    if sync.get("remote_sha"):
        print(f"  remote_sha={sync['remote_sha'][:12]}")
    print(f"  preservation={pending['status']}")
    if pending.get("eligible"):
        print(f"  pending_files={len(pending['eligible'])}")
    return 0 if sync["status"] == "synchronized" and pending["status"] == "clean" else 1


# -- Watch subcommand ----------------------------------------------------------


def cmd_watch(args: argparse.Namespace) -> int:
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1

    sync = check_sync_state(store, task["task_id"])
    print(f"task={task['task_id']}  sync={sync['status']}")

    base = args.base or "main"
    base_sha = task.get("base_commit_sha")
    base_result = check_base_branch_updated(store, task["task_id"], base, base_sha)
    print(f"  base_branch={base}  status={base_result['status']}")

    if base_result["status"] != "unknown":
        conflict = check_merge_conflict(task["task_id"], base)
        print(f"  merge_conflict={conflict['status']}")

    pending = detect_pending_preservation(store, task["task_id"])
    print(f"  preservation={pending['status']}")
    return 0 if (
        sync["status"] == "synchronized"
        and pending["status"] == "clean"
        and base_result["status"] == "unchanged"
        and (base_result["status"] == "unknown" or conflict["status"] == "no_conflict")
    ) else 1


def cmd_session(args: argparse.Namespace) -> int:
    """Run an agent command and check preservation at its completion."""
    from espaut.events import log_event
    store = require_store()
    task = _require_task(store, args.task)
    if not task:
        return 1
    command = list(args.command_args or [])
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        print("error: command required after --", file=sys.stderr)
        return 2
    config = load_config(store)
    log_event(store, "agent_session_started", task_id=task["task_id"], detail=command[0])
    try:
        result = subprocess.run(command, check=False)
        exit_code = result.returncode
    except OSError as exc:
        print(f"error: cannot run agent: {exc}", file=sys.stderr)
        exit_code = 2
    log_event(store, "agent_session_finished", task_id=task["task_id"], detail=f"exit={exit_code}")
    pending = detect_pending_preservation(store, task["task_id"])
    if pending["status"] != "pending":
        if exit_code != 0:
            return exit_code
        sync = check_sync_state(store, task["task_id"])
        if sync.get("status") != "synchronized":
            print("PRESERVATION_PENDING: remote HEAD not synchronized")
            return 3
        return 0
    if exit_code != 0:
        print("PRESERVATION_PENDING: command failed; uncommitted changes retained")
        return exit_code
    if not config.get("auto_commit", False):
        print("PRESERVATION_PENDING: auto_commit disabled; use preserve --confirm")
        return 3
    if not config.get("eligible_patterns"):
        print("PRESERVATION_PENDING: configure explicit eligible_patterns before auto_commit")
        return 3
    branch = current_branch()
    if branch in (None, "main", "master") or (task.get("branch_name") and branch != task["branch_name"]):
        print("PRESERVATION_PENDING: cannot automatically push protected or unrelated branch")
        return 3
    report = run_preservation(
        store, task["task_id"], f"espaut: preserve session {task['task_id']}",
        dry_run=False,
    )
    print(f"session preservation: {report['outcome']}")
    return 0 if report["outcome"] == "synchronized" else 3


# -- Parser --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="espaut",
        description="ESP-AUT-GIT-001 — task-aware preservation and PR lifecycle",
    )
    parser.add_argument("--version", action="version", version=f"espaut {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize .espaut/ in the current repository")
    sub.add_parser("status", help="Show working tree and task status")

    # task create
    task_p = sub.add_parser("task", help="Task management")
    task_sub = task_p.add_subparsers(dest="task_command")
    create_p = task_sub.add_parser("create", help="Create a new task")
    create_p.add_argument("title", help="Task title")
    create_p.add_argument("--objective", help="Task objective")

    # pr create / pr status
    pr_p = sub.add_parser("pr", help="Pull Request management")
    pr_sub = pr_p.add_subparsers(dest="pr_command")
    pr_create_p = pr_sub.add_parser("create", help="Create or reuse a PR")
    pr_create_p.add_argument("--task", help="Task ID")
    pr_create_p.add_argument("--title", help="PR title")
    pr_create_p.add_argument("--body", help="PR body")
    pr_create_p.add_argument("--base", help="Base branch")
    pr_status_p = pr_sub.add_parser("status", help="Query PR state")
    pr_status_p.add_argument("--task", help="Task ID")

    # preserve
    preserve_p = sub.add_parser("preserve", help="Detect and preserve changes")
    preserve_p.add_argument("--task", help="Task ID")
    preserve_p.add_argument("--confirm", action="store_true", help="Execute (not dry-run)")
    preserve_p.add_argument("--message", "-m", help="Commit message")

    # checks
    checks_p = sub.add_parser("checks", help="Query CI check status for a task")
    checks_p.add_argument("--task", help="Task ID")

    # reviews
    reviews_p = sub.add_parser("reviews", help="Collect and display reviews for a task")
    reviews_p.add_argument("--task", help="Task ID")

    # pendencies
    pendencies_p = sub.add_parser("pendencies", help="List pendencies for a task")
    pendencies_p.add_argument("--task", help="Task ID")

    # merge-check
    merge_p = sub.add_parser("merge-check", help="Evaluate merge eligibility (C1-C8)")
    merge_p.add_argument("--task", help="Task ID")

    # decide
    decide_p = sub.add_parser("decide", help="Record a human decision")
    decide_p.add_argument("--task", required=True, help="Task ID")
    decide_p.add_argument("--type", required=True, help="Decision type (approve, reject, defer, override)")
    decide_p.add_argument("--value", required=True, help="Decision value")
    decide_p.add_argument("--authority", help="Authority (default: cli_user)")
    decide_p.add_argument("--scope", help="Decision scope")

    # diagnose
    diagnose_p = sub.add_parser("diagnose", help="Create a diagnosis for a pendency")
    diagnose_p.add_argument("--pendency", required=True, help="Pendency ID")
    diagnose_p.add_argument("--cause", required=True, help="Suspected cause")
    diagnose_p.add_argument("--source", choices=["human", "ai"], help="Diagnosis source (default: human)")

    # correct
    correct_p = sub.add_parser("correct", help="Run correction for a pendency")
    correct_p.add_argument("--task", required=True, help="Task ID")
    correct_p.add_argument("--pendency", required=True, help="Pendency ID")
    correct_p.add_argument("--confirm", action="store_true", help="Execute (not dry-run)")
    correct_p.add_argument("--agent", choices=["codex", "claude"],
                           help="Apply a scoped correction using authenticated local agent")

    # verify-correction
    verify_p = sub.add_parser("verify-correction", help="Re-check after correction")
    verify_p.add_argument("--task", help="Task ID")

    # recover
    recover_p = sub.add_parser("recover", help="Recover task state from event log")
    recover_p.add_argument("--task", help="Task ID")

    # audit
    audit_p = sub.add_parser("audit", help="Audit trail for a task")
    audit_p.add_argument("--task", help="Task ID")
    audit_p.add_argument("--verbose", "-v", action="store_true", help="Show full timeline")

    # sync
    sync_p = sub.add_parser("sync", help="Check sync state with remote")
    sync_p.add_argument("--task", help="Task ID")

    session_p = sub.add_parser("session", help="Run a coding command with post-exit preservation gate")
    session_p.add_argument("--task", required=True, help="Task ID")
    session_p.add_argument("command_args", nargs=argparse.REMAINDER, help="Command after --")

    # watch
    watch_p = sub.add_parser("watch", help="One-shot watch: sync + base + conflicts")
    watch_p.add_argument("--task", help="Task ID")
    watch_p.add_argument("--base", help="Base branch (default: main)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    handlers: dict[str, Any] = {
        "init": cmd_init,
        "status": cmd_status,
        "preserve": cmd_preserve,
        "checks": cmd_checks,
        "reviews": cmd_reviews,
        "pendencies": cmd_pendencies,
        "merge-check": cmd_merge_check,
        "decide": cmd_decide,
        "diagnose": cmd_diagnose,
        "correct": cmd_correct,
        "verify-correction": cmd_verify_correction,
        "recover": cmd_recover,
        "audit": cmd_audit,
        "sync": cmd_sync,
        "watch": cmd_watch,
        "session": cmd_session,
    }

    if args.command == "task":
        if getattr(args, "task_command", None) == "create":
            return cmd_task_create(args)
        parser.parse_args(["task", "--help"])
        return 0

    if args.command == "pr":
        pr_handlers = {"create": cmd_pr_create, "status": cmd_pr_status}
        pr_cmd = getattr(args, "pr_command", None)
        if pr_cmd in pr_handlers:
            return pr_handlers[pr_cmd](args)
        parser.parse_args(["pr", "--help"])
        return 0

    handler = handlers.get(args.command)
    if handler:
        try:
            return handler(args)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    parser.print_help()
    return 0
