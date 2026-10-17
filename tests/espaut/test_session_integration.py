#!/usr/bin/env python3
"""Real Git integration tests; local bare remote only, never contacts GitHub."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from espaut.store import init_store,write_json
from espaut.task import make_task

def git(*args,cwd):
    result=subprocess.run(["git",*args],cwd=cwd,check=True,text=True,capture_output=True)
    return result.stdout.strip()

class SessionGitIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.remote=self.home/"remote.git"
        self.work=self.home/"work"
        self.work.mkdir()
        git("init","--bare","--initial-branch=main",str(self.remote),cwd=self.home)
        git("init","-b","main",cwd=self.work)
        git("config","user.name","ESPAUT test",cwd=self.work)
        git("config","user.email","espa-test@example.org",cwd=self.work)
        (self.work/"base.txt").write_text("baseline")
        git("add","base.txt",cwd=self.work)
        git("commit","-m","baseline",cwd=self.work)
        git("remote","add","origin",str(self.remote),cwd=self.work)
        git("push","-u","origin","main",cwd=self.work)
        git("switch","-c","task/test",cwd=self.work)
        git("push","-u","origin","task/test",cwd=self.work)
        self.initial=git("rev-parse","HEAD",cwd=self.work)
        self.store=init_store(self.work)
        task=make_task("TASK-001",str(self.work),"Test session")
        task["branch_name"]="task/test"
        write_json(self.store/"tasks"/"TASK-001.json",task)

    def launch(self,body):
        env={**os.environ,"PYTHONPATH":str(ROOT)}
        return subprocess.run([sys.executable,"-B","-m","espaut","session","--task","TASK-001","--",sys.executable,"-c",body],cwd=self.work,env=env,text=True,capture_output=True)

    def test_tst025_uncommitted_changes_not_silent(self):
        result=self.launch("from pathlib import Path; Path('new.txt').write_text('change')")
        self.assertEqual(result.returncode,3,result.stdout+result.stderr)
        self.assertIn("PRESERVATION_PENDING",result.stdout)
        self.assertEqual(git("rev-parse","HEAD",cwd=self.work),self.initial)
        self.assertTrue((self.work/"new.txt").exists())

    def test_tst026_auto_commit_and_push_verified(self):
        config=json.loads((self.store/"config.json").read_text())
        config.update({"auto_commit":True,"eligible_patterns":["*.txt"]})
        write_json(self.store/"config.json",config)
        result=self.launch("from pathlib import Path; Path('session.txt').write_text('change')")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("session preservation: synchronized",result.stdout)
        new_head=git("rev-parse","HEAD",cwd=self.work)
        remote_head=git("--git-dir",str(self.remote),"rev-parse","refs/heads/task/test",cwd=self.home)
        self.assertNotEqual(new_head,self.initial)
        self.assertEqual(remote_head,new_head)

    def test_session_nonzero_keeps_changes_without_commit(self):
        config=json.loads((self.store/"config.json").read_text())
        config.update({"auto_commit":True,"eligible_patterns":["*.txt"]})
        write_json(self.store/"config.json",config)
        result=self.launch("import sys; from pathlib import Path; Path('failed.txt').write_text('partial'); sys.exit(6)")
        self.assertEqual(result.returncode,6,result.stdout+result.stderr)
        self.assertTrue((self.work/"failed.txt").exists())
        self.assertEqual(git("rev-parse","HEAD",cwd=self.work),self.initial)

    def test_tst031_032_base_change_conflict_blocks_without_merging(self):
        from espaut.triggers import check_base_branch_updated, check_merge_conflict
        other=self.home/"other"
        git("clone",str(self.remote),str(other),cwd=self.home)
        git("config","user.name","Other",cwd=other)
        git("config","user.email","other@example.org",cwd=other)
        (self.work/"base.txt").write_text("feature version")
        git("commit","-am","feature version",cwd=self.work)
        head_before=git("rev-parse","HEAD",cwd=self.work)
        (other/"base.txt").write_text("main version")
        git("commit","-am","base version",cwd=other)
        git("push","origin","main",cwd=other)
        updated=check_base_branch_updated(self.store,"TASK-001","main",self.initial,cwd=str(self.work))
        self.assertEqual(updated["status"],"updated")
        conflict=check_merge_conflict("TASK-001","main",cwd=str(self.work))
        self.assertEqual(conflict["status"],"conflict",conflict)
        self.assertEqual(git("rev-parse","HEAD",cwd=self.work),head_before)

    def test_tst027_028_remote_ahead_reconciled(self):
        from espaut.triggers import check_sync_state
        other=self.home/"other"
        git("clone",str(self.remote),str(other),cwd=self.home)
        git("config","user.name","Other",cwd=other)
        git("config","user.email","other@example.org",cwd=other)
        git("switch","task/test",cwd=other)
        (other/"other.txt").write_text("new remote")
        git("add","other.txt",cwd=other)
        git("commit","-m","remote ahead",cwd=other)
        git("push","origin","task/test",cwd=other)
        result=check_sync_state(self.store,"TASK-001",cwd=str(self.work))
        self.assertEqual(result["status"],"remote_ahead",result)

    def test_task_created_on_main_moves_to_isolated_branch(self):
        git("switch","main",cwd=self.work)
        env={**os.environ,"PYTHONPATH":str(ROOT)}
        result=subprocess.run([sys.executable,"-B","-m","espaut","task","create","Example"],cwd=self.work,env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        branch=git("branch","--show-current",cwd=self.work)
        self.assertTrue(branch.startswith("espaut/task-"),branch)
        self.assertEqual(git("--git-dir",str(self.remote),"rev-parse","refs/heads/main",cwd=self.home),self.initial)

    def test_task_create_on_dirty_main_fails(self):
        git("switch","main",cwd=self.work)
        (self.work/"base.txt").write_text("not committed")
        env={**os.environ,"PYTHONPATH":str(ROOT)}
        result=subprocess.run([sys.executable,"-B","-m","espaut","task","create","Example"],cwd=self.work,env=env,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(git("branch","--show-current",cwd=self.work),"main")

    def test_tst018_recovery_after_remote_push_does_not_duplicate(self):
        from espaut.recovery import recover_task_state
        from espaut.events import log_event
        head=git("rev-parse","HEAD",cwd=self.work)
        log_event(self.store,"commit_created",task_id="TASK-001",object_reference=head)
        task={"task_id":"TASK-001","branch_name":"task/test","commit_shas":[head]}
        first=recover_task_state(self.store,task,str(self.work))
        second=recover_task_state(self.store,task,str(self.work))
        self.assertEqual(first["remote_status"],"synchronized")
        self.assertEqual(second["remote_status"],"synchronized")
        self.assertEqual(first["status"],"recovered")
        self.assertEqual(git("rev-list","--count","HEAD",cwd=self.work),"1")
        self.assertEqual(git("--git-dir",str(self.remote),"rev-parse","refs/heads/task/test",cwd=self.home),head)

    def test_tst018_recovery_local_ahead_does_not_claim_sync(self):
        from espaut.recovery import recover_task_state
        (self.work/"new.txt").write_text("local")
        git("add","new.txt",cwd=self.work)
        git("commit","-m","local only",cwd=self.work)
        head=git("rev-parse","HEAD",cwd=self.work)
        task={"task_id":"TASK-001","branch_name":"task/test","commit_shas":[head]}
        result=recover_task_state(self.store,task,str(self.work))
        self.assertEqual(result["remote_status"],"diverged")
        self.assertEqual(result["status"],"discrepancies_found")
        self.assertIn("remote_sync",[d["field"] for d in result["discrepancies"]])

    def test_tst006_rejected_push_preserves_local_commit(self):
        from espaut.preservation import run_preservation
        hook=self.remote/"hooks"/"pre-receive"
        hook.write_text("#!/bin/sh\necho controlled push rejection >&2\nexit 1\n")
        hook.chmod(0o755)
        (self.work/"new.txt").write_text("pending work")
        before=git("rev-parse","HEAD",cwd=self.work)
        report=run_preservation(self.store,"TASK-001","test rejected push",dry_run=False,cwd=str(self.work))
        after=git("rev-parse","HEAD",cwd=self.work)
        remote=git("--git-dir",str(self.remote),"rev-parse","refs/heads/task/test",cwd=self.home)
        self.assertEqual(report["outcome"],"error",report)
        self.assertNotEqual(before,after)
        self.assertEqual(before,remote)
        self.assertFalse((self.work/"new.txt").read_text()=="")
        self.assertEqual(git("status","--porcelain",cwd=self.work),"")

if __name__=="__main__":
    unittest.main(verbosity=2)
