#!/usr/bin/env python3
"""Deterministic tests for post-response GitHub lifecycle."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from espaut.stop_cycle import run_stop_cycle, human_approved
from espaut.store import init_store, write_json


class StopCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = init_store(Path(self.tmp.name))
        self.task = {"task_id": "TASK-TEST", "branch_name": "feature/test",
                     "title": "Test PR", "objective": "A small change",
                     "acceptance_criteria": ["T1"],
                     "acceptance_revision": "a" * 40,
                     "acceptance_results": {"T1": {"status": "PASS",
                                                   "revision": "a"*40,
                                                   "evidence_reference": "proof"}}}
        write_json(self.store/"tasks/TASK-TEST.json", self.task)
        self.cfg = {"auto_commit": True, "eligible_patterns": ["*.py"],
                    "auto_pr": True, "auto_merge": False,
                    "merge_method": "merge"}
        write_json(self.store/"config.json", self.cfg)
        self.patches = [
            patch("espaut.stop_cycle.current_branch", return_value="feature/test"),
            patch("espaut.stop_cycle.has_incomplete_operation", return_value=None),
            patch("espaut.stop_cycle.worktree_status", return_value=[]),
            patch("espaut.stop_cycle.head_sha", return_value="a"*40),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_pr_without_explicit_opt_in(self):
        self.cfg["auto_pr"] = False
        write_json(self.store/"config.json", self.cfg)
        with patch("espaut.stop_cycle.create_or_reuse_pr") as create:
            out=run_stop_cycle(self.store,self.task,cwd=self.tmp.name)
            self.assertEqual(out["status"], "pr_pending_authorization")
            create.assert_not_called()

    def test_no_remote_actions_without_auto_commit_consent(self):
        self.cfg["auto_commit"] = False
        write_json(self.store/"config.json", self.cfg)
        with patch("espaut.stop_cycle.create_or_reuse_pr") as create:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"monitor_only")
            create.assert_not_called()

    def test_preexisting_changes_block_before_any_commit(self):
        with patch("espaut.stop_cycle.worktree_status",return_value=[{"path":"abc.py"}]), \
             patch("espaut.stop_cycle.run_preservation") as preserve:
            r=run_stop_cycle(self.store,self.task,cwd=self.tmp.name,preserve_changes=False)
            self.assertEqual(r["status"],"blocked")
            preserve.assert_not_called()

    def test_preservation_failure_prevents_pr(self):
        with patch("espaut.stop_cycle.worktree_status",return_value=[{"path":"abc.py"}]), \
             patch("espaut.stop_cycle.run_preservation",return_value={"outcome":"unverified"}), \
             patch("espaut.stop_cycle.create_or_reuse_pr") as create:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"preservation_pending")
            create.assert_not_called()

    def test_pr_creation_updates_task_identity(self):
        with patch("espaut.stop_cycle.create_or_reuse_pr",return_value={"status":"created","pr_number":12,"url":"https://github.com/example/repo/pull/12"}), \
             patch("espaut.stop_cycle.query_pr_state",return_value={"status":"available","github_state":"CLOSED","head_sha":"a"*40}):
            r=run_stop_cycle(self.store,self.task,cwd=self.tmp.name)
            self.assertEqual(r["status"],"blocked")
            self.assertEqual(r["pr_number"],12)
            from espaut.store import read_json
            self.assertEqual(read_json(self.store/"tasks/TASK-TEST.json")["pr_number"],12)

    def test_mismatched_pr_head_aborts_before_review(self):
        self.task["pr_number"]=3
        with patch("espaut.stop_cycle.query_pr_state",return_value={"status":"available","github_state":"OPEN","head_sha":"b"*40}), \
             patch("espaut.stop_cycle.query_checks") as checks:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"blocked")
            checks.assert_not_called()

    def test_review_query_failure_never_implies_merge_approval(self):
        self.task["pr_number"]=3
        remote={"status":"available","github_state":"OPEN","head_sha":"a"*40}
        with patch("espaut.stop_cycle.query_pr_state",return_value=remote), \
             patch("espaut.stop_cycle.query_checks",side_effect=RuntimeError("auth failed")), \
             patch("espaut.stop_cycle.run_gh") as merge:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"operational_failure")
            merge.assert_not_called()

    def test_review_pending_blocks_merge_even_if_configured(self):
        self.task["pr_number"]=3
        self.cfg["auto_merge"]=True
        write_json(self.store/"config.json", self.cfg)
        remote={"status":"available","github_state":"OPEN","head_sha":"a"*40,"review_decision":None}
        with patch("espaut.stop_cycle.query_pr_state",return_value=remote), \
             patch("espaut.stop_cycle.query_checks",return_value={"aggregate":"PENDENTE"}), \
             patch("espaut.stop_cycle.collect_reviews",return_value={"reviews":[]}), \
             patch("espaut.stop_cycle.extract_pendencies",return_value=[]), \
             patch("espaut.stop_cycle.evaluate_merge_eligibility",return_value={"verdict":"NAO_APTA","criteria":[]}), \
             patch("espaut.stop_cycle.run_gh") as merge:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"review_pending")
            merge.assert_not_called()

    def test_merge_requires_authorized_current_revision(self):
        self.task["pr_number"]=3
        self.cfg["auto_merge"]=True
        write_json(self.store/"config.json",self.cfg)
        remote={"status":"available","github_state":"OPEN","head_sha":"a"*40,"review_decision":"APPROVED"}
        with patch("espaut.stop_cycle.query_pr_state",return_value=remote), \
             patch("espaut.stop_cycle.query_checks",return_value={"aggregate":"APROVADO"}), \
             patch("espaut.stop_cycle.collect_reviews",return_value={"reviews":[]}), \
             patch("espaut.stop_cycle.extract_pendencies",return_value=[]), \
             patch("espaut.stop_cycle.evaluate_merge_eligibility",return_value={"verdict":"APTA_PARA_INTEGRACAO","criteria":[]}), \
             patch("espaut.stop_cycle.run_gh") as merge:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"merge_pending_authorization")
            merge.assert_not_called()

    def test_merge_conditional_on_sha_and_gitHub_confirmation(self):
        self.task["pr_number"]=3
        self.cfg["auto_merge"]=True
        write_json(self.store/"config.json",self.cfg)
        write_json(self.store/"decisions/DEC-1.json",{"task_id":"TASK-TEST","decision_type":"merge_authorization",
                   "decision_value":"approved","authority":"human","scope":"a"*40})
        remote={"status":"available","github_state":"OPEN","head_sha":"a"*40,"review_decision":"APPROVED"}
        merged={"status":"available","merged_at":"2026-10-18T01:00:00Z","merge_commit":{"oid":"c"*40}}
        current={"state":"OPEN","isDraft":False,"headRefOid":"a"*40,"mergeStateStatus":"CLEAN"}
        with patch("espaut.stop_cycle.query_pr_state",side_effect=[remote,merged]), \
             patch("espaut.stop_cycle.query_checks",return_value={"aggregate":"APROVADO"}), \
             patch("espaut.stop_cycle.collect_reviews",return_value={"reviews":[]}), \
             patch("espaut.stop_cycle.extract_pendencies",return_value=[]), \
             patch("espaut.stop_cycle.evaluate_merge_eligibility",return_value={"verdict":"APTA_PARA_INTEGRACAO","criteria":[]}), \
             patch("espaut.stop_cycle.pr_view_json",return_value=current), \
             patch("espaut.stop_cycle.run_gh",return_value=MagicMock(returncode=0)) as merge:
            self.assertEqual(run_stop_cycle(self.store,self.task,cwd=self.tmp.name)["status"],"merged")
            self.assertIn("--match-head-commit",merge.call_args.args)
            self.assertIn("a"*40,merge.call_args.args)

    def test_human_approval_must_bind_exact_commit(self):
        self.assertFalse(human_approved(self.task,[{"task_id":"TASK-TEST","decision_type":"merge_authorization",
                           "decision_value":"approved","authority":"human","scope":"b"*40}],"a"*40))


if __name__=="__main__":
    unittest.main(verbosity=2)
