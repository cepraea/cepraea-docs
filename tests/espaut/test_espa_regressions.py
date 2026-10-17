#!/usr/bin/env python3
"""Regression tests for cross-module safety gates missed by the original suite."""
from __future__ import annotations
import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from espaut.gh_ops import GitHubQueryError, pr_checks_json, pr_comments_json, pr_create, pr_merge
from espaut.git_ops import worktree_status
from espaut.preservation import run_preservation
from espaut.store import init_store, write_json
from espaut.governance import check_c6_acceptance, check_c8_currency
from espaut.review import CommentClassification, extract_pendencies, make_pendency, has_blocking_pendencies
from espaut import cli

def cp(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], code, stdout, stderr)

def run(*args, cwd):
    return subprocess.run(list(args), cwd=str(cwd), text=True, capture_output=True, check=True)

class Contracts(unittest.TestCase):
    def test_pr_checks_failure_json_not_lost(self):
        raw='[{"name":"build","state":"FAILURE","bucket":"fail"}]'
        with patch("espaut.gh_ops.run_gh", return_value=cp(1,raw,"checks failed")):
            checks=pr_checks_json(42)
        self.assertEqual(checks[0]["state"],"FAILURE")

    def test_pr_checks_pending_json_not_lost(self):
        with patch("espaut.gh_ops.run_gh", return_value=cp(8,'[{"name":"build","state":"PENDING","bucket":"pending"}]')):
            self.assertEqual(pr_checks_json(42)[0]["state"],"PENDING")

    def test_pr_checks_transport_failure_raises(self):
        with patch("espaut.gh_ops.run_gh", return_value=cp(1,"","authentication failed")):
            with self.assertRaises(GitHubQueryError):
                pr_checks_json(42)

    def test_rest_failure_not_empty(self):
        with patch("espaut.gh_ops.run_gh", return_value=cp(1,"","permission denied")):
            with self.assertRaises(GitHubQueryError):
                pr_comments_json(42)

    def test_pr_body_with_newline_preserved(self):
        body="task: ABC\n\nAcceptance: PASS"
        with patch("espaut.gh_ops.run_gh", return_value=cp(0,"https://github.com/o/r/pull/8\n")) as execute, patch("espaut.gh_ops.pr_view_json",return_value={"number":8,"url":"https://github.com/o/r/pull/8"}):
            out=pr_create("feature",body)
        self.assertEqual(out["number"],8)
        self.assertIn(body,execute.call_args.args)

    def test_merge_no_unsupported_yes(self):
        with patch("espaut.gh_ops.run_gh", return_value=cp(0)) as call:
            pr_merge(10,method="squash")
        self.assertNotIn("--yes",call.call_args.args)

    def test_merge_rejects_bad_method(self):
        with self.assertRaises(ValueError):
            pr_merge(10,method="--delete-branch")

    def test_acceptance_without_evidence_fails(self):
        self.assertEqual(check_c6_acceptance({"acceptance_criteria":[]})["status"],"FAIL")
        self.assertEqual(check_c6_acceptance({"acceptance_criteria":["x"]})["status"],"FAIL")

    def test_acceptance_with_evidence_passes(self):
        t={"acceptance_criteria":["TST-001"],"acceptance_revision":"sha1","acceptance_results":{"TST-001":{"status":"PASS","revision":"sha1","evidence_reference":"run-1"}}}
        self.assertEqual(check_c6_acceptance(t)["status"],"PASS")

    def test_check_no_revision_fails(self):
        self.assertEqual(check_c8_currency({"checks":[{"name":"build"}]},"sha1")["status"],"FAIL")
        self.assertEqual(check_c8_currency({"checks":[]},"sha1")["status"],"FAIL")

    def test_literal_filename_with_spaces_and_semicolon(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);run("git","init",cwd=p)
            run("git","config","user.email","test@example.org",cwd=p)
            run("git","config","user.name","Test",cwd=p)
            (p/"a (1); b.txt").write_text("example")
            entries=worktree_status(str(p))
            self.assertEqual(entries[0]["path"],"a (1); b.txt")

    def test_unrelated_pre_staged_not_committed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);run("git","init",cwd=p)
            run("git","config","user.email","test@example.org",cwd=p)
            run("git","config","user.name","Test",cwd=p)
            (p/"base").write_text("init")
            run("git","add","base",cwd=p);run("git","commit","-m","init",cwd=p)
            run("git","switch","-c","test/task",cwd=p)
            store=init_store(p)
            conf=store/"config.json"
            c=__import__("json").loads(conf.read_text());c["eligible_patterns"]=["b.txt"];write_json(conf,c)
            (p/"a.txt").write_text("separate");run("git","add","a.txt",cwd=p)
            (p/"b.txt").write_text("target")
            out=run_preservation(store,"T1","preserve",dry_run=False,cwd=str(p))
            self.assertEqual(out["outcome"],"staging_failed")
            self.assertIn("a.txt",run("git","diff","--cached","--name-only",cwd=p).stdout)
            self.assertNotIn("b.txt",run("git","diff","--cached","--name-only",cwd=p).stdout)

    def test_stable_review_pendency(self):
        p1=make_pendency("comment","42","bug",CommentClassification.DEFEITO_POSSIVEL,task_id="T1",pr_number=7)
        p2=make_pendency("comment","42","bug",CommentClassification.DEFEITO_POSSIVEL,task_id="T1",pr_number=7)
        self.assertEqual(p1["pendency_id"],p2["pendency_id"])

    def test_ledger_keeps_unresolved_comment(self):
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            a={"pr_number":7,"reviews":[],"comments":[{"comment_id":42,"body_preview":"bug","author":"a","classification":"DEFEITO_POSSIVEL"}]}
            one=extract_pendencies(a,task_id="T1",store=store)
            two=extract_pendencies(a,task_id="T1",store=store)
            self.assertEqual([p["pendency_id"] for p in one],[p["pendency_id"] for p in two])
            gone=extract_pendencies({"pr_number":7,"reviews":[],"comments":[]},task_id="T1",store=store)
            self.assertEqual(len(gone),1)
            self.assertTrue(has_blocking_pendencies(gone))
            self.assertFalse(gone[0]["present_remote"])

    def test_ambiguous_comment_kept_blocking(self):
        report={"pr_number":7,"reviews":[],"comments":[{"comment_id":20,"body_preview":"interesting","classification":"INDETERMINADO"}]}
        p=extract_pendencies(report,task_id="T1")
        self.assertEqual(len(p),1)
        self.assertTrue(has_blocking_pendencies(p))

    def test_cli_preserve_push_error_fails(self):
        t={"task_id":"T1"}
        with patch("espaut.cli.require_store",return_value=Path("/tmp")),patch("espaut.cli._resolve_task",return_value=t),patch("espaut.cli.run_preservation",return_value={"outcome":"error","steps":[]}) :
            with contextlib.redirect_stdout(io.StringIO()):
                rc=cli.cmd_preserve(argparse.Namespace(task=None,confirm=True,message=None))
        self.assertNotEqual(rc,0)

    def test_cli_merge_prints_actual_criterion(self):
        t={"task_id":"T1","pr_number":None}
        report={"verdict":"NAO_APTA","criteria":[{"criterion":"C1","status":"FAIL","detail":"bad"}]}
        with patch("espaut.cli.require_store",return_value=Path("/tmp/nonexistent")),patch("espaut.cli._require_task",return_value=t),patch("espaut.cli.evaluate_merge_eligibility",return_value=report):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                rc=cli.cmd_merge_check(argparse.Namespace(task="T1"))
        self.assertEqual(rc,1)
        self.assertIn("FAIL",output.getvalue())
        self.assertIn("C1:",output.getvalue())

    def test_session_gate_off_reports_pending(self):
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            task={"task_id":"T1"}
            with patch("espaut.cli.require_store",return_value=store),patch("espaut.cli._require_task",return_value=task),patch("espaut.cli.subprocess.run",return_value=cp(0)),patch("espaut.cli.detect_pending_preservation",return_value={"status":"pending"}):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc=cli.cmd_session(argparse.Namespace(task="T1",command_args=["--","python","-V"]))
            self.assertEqual(rc,3)

    def test_session_gate_on_preserves(self):
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d));write_json(store/"config.json",{"auto_commit":True,"eligible_patterns":["*.py"]})
            task={"task_id":"T1"}
            with patch("espaut.cli.require_store",return_value=store),patch("espaut.cli._require_task",return_value=task),patch("espaut.cli.subprocess.run",return_value=cp(0)),patch("espaut.cli.detect_pending_preservation",return_value={"status":"pending"}),patch("espaut.cli.run_preservation",return_value={"outcome":"synchronized"}) as preserve,patch("espaut.cli.current_branch",return_value="test/feature"):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc=cli.cmd_session(argparse.Namespace(task="T1",command_args=["--","codex","exec","task"]))
            self.assertEqual(rc,0)
            self.assertTrue(preserve.called)

    def test_review_thread_query_preserves_resolution(self):
        from espaut.gh_ops import pr_review_threads_json
        data={"data":{"repository":{"pullRequest":{"reviewThreads":{
            "nodes":[{"id":"THREAD1","isResolved":False,"isOutdated":False,
                      "comments":{"nodes":[{"databaseId":51}],"pageInfo":{"hasNextPage":False}}}],
            "pageInfo":{"hasNextPage":False}
        }}}}}
        with patch("espaut.gh_ops.run_gh",side_effect=[
            cp(0,'{"nameWithOwner":"org/repo"}'),cp(0,__import__("json").dumps(data))
        ]):
            out=pr_review_threads_json(3)
        self.assertFalse(out[0]["isResolved"])

    def test_review_thread_pagination_blocks_incomplete(self):
        from espaut.gh_ops import pr_review_threads_json
        data={"data":{"repository":{"pullRequest":{"reviewThreads":{
            "nodes":[],"pageInfo":{"hasNextPage":True}}}}}}
        with patch("espaut.gh_ops.run_gh",side_effect=[
            cp(0,'{"nameWithOwner":"org/repo"}'),cp(0,__import__("json").dumps(data))
        ]):
            with self.assertRaises(GitHubQueryError):
                pr_review_threads_json(3)

    def test_remote_unresolved_requires_resolution_even_after_local_approval(self):
        p=make_pendency("comment","51","bug",CommentClassification.DEFEITO_POSSIVEL,task_id="T1",pr_number=3,thread_id="THREAD1",thread_resolved_remote=False)
        p.update({"resolution_state":"RESOLVIDA","resolution_evidence":"test-run"})
        self.assertTrue(has_blocking_pendencies([p]))

    def test_ai_correction_blocked_without_scope(self):
        from espaut.correction import apply_agent_correction
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            result=apply_agent_correction(store,{"task_id":"T1"},{"description":"fix bug"},"codex",cwd=d)
            self.assertEqual(result["status"],"blocked")

    def test_ai_correction_requires_verification(self):
        from espaut.correction import apply_agent_correction
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            write_json(store/"config.json",{"ai_enabled":True,"ai_sharing_authorized":True})
            task={"task_id":"T1","authorized_scope":["src/*.py"]}
            source={"description":"fix bug","classification":"MUDANCA_REQUERIDA"}
            with patch("espaut.git_ops.current_branch",return_value="feature/a"),patch("espaut.git_ops.worktree_status",return_value=[]):
                result=apply_agent_correction(store,task,source,"codex",cwd=d)
            self.assertEqual(result["status"],"blocked")
            self.assertIn("correction_test_command",result["reason"])

    def test_ai_correction_verified_by_real_gate(self):
        from espaut.correction import apply_agent_correction
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            write_json(store/"config.json",{"ai_enabled":True,"ai_sharing_authorized":True,"correction_test_command":[sys.executable,"-B","-c","print(1)"]})
            task={"task_id":"T1","authorized_scope":["src/*.py"]}
            source={"description":"fix bug","classification":"MUDANCA_REQUERIDA"}
            with patch("espaut.git_ops.current_branch",return_value="feature/a"),patch("espaut.git_ops.worktree_status",side_effect=[[],[{"path":"src/a.py"}]]),patch("shutil.which",return_value="/usr/bin/codex"),patch("subprocess.run",side_effect=[cp(0),cp(0)]):
                result=apply_agent_correction(store,task,source,"codex",cwd=d)
            self.assertEqual(result["status"],"verified",result)
            self.assertEqual(result["changed_files"],["src/a.py"])

    def test_secret_in_staged_diff_blocks_commit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);run("git","init",cwd=p)
            run("git","config","user.email","test@example.org",cwd=p)
            run("git","config","user.name","Test",cwd=p)
            (p/"base.txt").write_text("init")
            run("git","add","base.txt",cwd=p);run("git","commit","-m","init",cwd=p)
            run("git","switch","-c","test/branch",cwd=p)
            store=init_store(p)
            (p/"secret.txt").write_text("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn")
            sha=run("git","rev-parse","HEAD",cwd=p).stdout.strip()
            result=run_preservation(store,"T1","try push",dry_run=False,cwd=str(p))
            self.assertEqual(result["outcome"],"staging_failed",result)
            self.assertEqual(run("git","rev-parse","HEAD",cwd=p).stdout.strip(),sha)

    def test_default_branch_not_committed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);run("git","init",cwd=p)
            run("git","config","user.email","test@example.org",cwd=p)
            run("git","config","user.name","Test",cwd=p)
            (p/"base").write_text("init")
            run("git","add","base",cwd=p);run("git","commit","-m","init",cwd=p)
            store=init_store(p)
            (p/"new.txt").write_text("change")
            result=run_preservation(store,"T1","save",dry_run=False,cwd=str(p))
            self.assertEqual(result["outcome"],"protected_branch")

    def test_no_ci_bound_to_revision_passes_currency_only(self):
        report={"status":"not_configured","evaluated_revision":"abc","checks":[]}
        self.assertEqual(check_c8_currency(report,"abc")["status"],"PASS")
        self.assertEqual(check_c8_currency(report,"def")["status"],"FAIL")

    def test_agent_secret_from_comment_blocked_before_call(self):
        from espaut.correction import apply_agent_correction
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            task={"task_id":"T1","authorized_scope":["src/*.py"]}
            write_json(store/"config.json",{"ai_enabled":True,"ai_sharing_authorized":True})
            marker="ghp_" + ("A" * 40)
            src={"description":"token="+marker, "classification":"MUDANCA_REQUERIDA"}
            with patch("espaut.git_ops.current_branch",return_value="feature/fix"),patch("espaut.git_ops.worktree_status",return_value=[]),patch("subprocess.run") as agent:
                out=apply_agent_correction(store,task,src,"codex",cwd=d)
            self.assertEqual(out["status"],"blocked")
            agent.assert_not_called()

    def test_gh_api_paginated_query_supports_live_cli(self):
        from espaut.gh_ops import pr_reviews_json, pr_comments_json, pr_review_comments_json
        for wrapper in (pr_reviews_json, pr_comments_json, pr_review_comments_json):
            with self.subTest(wrapper=wrapper.__name__), patch("espaut.gh_ops.run_gh", return_value=cp(0,"[[]]")) as call:
                self.assertEqual(wrapper(1), [])
                args=call.call_args.args
                self.assertIn("--paginate",args)
                self.assertIn("--slurp",args)
                self.assertNotIn("--jq",args)

    def test_gh_no_checks_requires_two_stable_remote_snapshots(self):
        with patch("espaut.gh_ops.run_gh", return_value=cp(1,"","no checks reported on this branch")), patch("espaut.gh_ops.pr_view_json",side_effect=[{"headRefOid":"abc","statusCheckRollup":[]},{"headRefOid":"abc","statusCheckRollup":[]}]):
            self.assertEqual(pr_checks_json(1), [])
        with patch("espaut.gh_ops.run_gh", return_value=cp(1,"","no checks reported")), patch("espaut.gh_ops.pr_view_json",side_effect=[{"headRefOid":"abc","statusCheckRollup":[]},{"headRefOid":"def","statusCheckRollup":[]}]):
            with self.assertRaises(GitHubQueryError):
                pr_checks_json(1)

    def test_draft_pr_is_ineligible_for_approval(self):
        from espaut.governance import check_c4_review
        decision=check_c4_review({"is_draft":True,"reviews":[{"state":"APPROVED"}]})
        self.assertEqual(decision["status"],"FAIL")

    def test_c2_sync_uses_actual_fork_pr_head(self):
        from espaut.governance import check_c2_sync
        task={"task_id":"T1","pr_number":1}
        with patch("espaut.governance.head_sha",return_value="abc"),patch("espaut.pullrequest.query_pr_state",return_value={"status":"available","head_sha":"abc"}):
            self.assertEqual(check_c2_sync(task)["status"],"PASS")
        with patch("espaut.governance.head_sha",return_value="abc"),patch("espaut.pullrequest.query_pr_state",return_value={"status":"available","head_sha":"def"}):
            self.assertEqual(check_c2_sync(task)["status"],"FAIL")
        with patch("espaut.governance.head_sha",return_value="abc"),patch("espaut.pullrequest.query_pr_state",side_effect=RuntimeError("network")):
            self.assertEqual(check_c2_sync(task)["status"],"FAIL")

    def test_ai_disabled_and_sharing_not_authorized_block_before_agent(self):
        from espaut.correction import apply_agent_correction
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            task={"task_id":"T1","authorized_scope":["src/*.py"]}
            pendency={"description":"fix bug","classification":"MUDANCA_REQUERIDA"}
            for cfg in ({}, {"ai_enabled":True}, {"ai_sharing_authorized":True}):
                write_json(store/"config.json",cfg)
                with self.subTest(config=cfg), patch("subprocess.run") as runner:
                    out=apply_agent_correction(store,task,pendency,"codex",cwd=d)
                    self.assertEqual(out["status"],"blocked")
                    self.assertIn("opt-in",out["reason"])
                    runner.assert_not_called()

    def test_untracked_directories_resolve_to_eligible_file_paths(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);run("git","init",cwd=p)
            run("git","config","user.email","test@example.org",cwd=p)
            run("git","config","user.name","Test",cwd=p)
            (p/"src").mkdir()
            (p/"src"/"hello.txt").write_text("test")
            paths=[x["path"] for x in worktree_status(str(p))]
            self.assertEqual(paths,["src/hello.txt"])

    def test_tst015_three_failed_attempts_persist_and_fourth_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            task={"task_id":"T1","pr_number":1}
            pendency="PND-controlled"
            write_json(store/"cache"/"pendencies-PR1.json",[{"pendency_id":pendency,"description":"fix defect","classification":"MUDANCA_REQUERIDA"}])
            args=argparse.Namespace(task="T1",pendency=pendency,agent="codex",confirm=True)
            with patch("espaut.cli.require_store",return_value=store),patch("espaut.cli._require_task",return_value=task),patch("espaut.cli.apply_agent_correction",return_value={"status":"error","reason":"controlled timeout"}) as agent:
                with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    for _ in range(4):
                        self.assertEqual(cli.cmd_correct(args),1)
            self.assertEqual(agent.call_count,3)
            history=__import__("json").loads((store/"cache"/f"attempts-{pendency}.json").read_text())
            self.assertEqual([a["attempt_number"] for a in history],[1,2,3])
            self.assertTrue(all(a["attempt_state"]=="FALHA" for a in history))

    def test_tst015_interrupted_agent_still_counts_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            store=init_store(Path(d))
            task={"task_id":"T1","pr_number":1}
            pendency="PND-interrupted"
            write_json(store/"cache"/"pendencies-PR1.json",[{"pendency_id":pendency,"description":"fix defect","classification":"MUDANCA_REQUERIDA"}])
            args=argparse.Namespace(task="T1",pendency=pendency,agent="codex",confirm=True)
            with patch("espaut.cli.require_store",return_value=store),patch("espaut.cli._require_task",return_value=task),patch("espaut.cli.apply_agent_correction",side_effect=TimeoutError("controlled")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_correct(args),1)
            history=__import__("json").loads((store/"cache"/f"attempts-{pendency}.json").read_text())
            self.assertEqual(len(history),1)
            self.assertEqual(history[0]["attempt_state"],"FALHA")
            self.assertEqual(history[0]["outcome"],"exception:TimeoutError")

if __name__=="__main__":
    unittest.main(verbosity=2)
