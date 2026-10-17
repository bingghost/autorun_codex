from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from codex_task.cli import main


FAKE_CODEX = '''#!/usr/bin/env python3
import json
import pathlib
import sys
args = sys.argv
prompt = sys.stdin.read()
path = pathlib.Path(args[args.index("--output-last-message") + 1])
status = "blocked" if "Objective: BLOCK" in prompt else "completed"
path.write_text(json.dumps({"status": status, "summary": "fake result", "evidence": ["fake evidence"]}))
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}))
'''


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.call("init", "--project", str(self.root))
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_CODEX, encoding="utf-8")
        self.fake.chmod(0o755)
        self.config = self.root / ".codex-tasks" / "project.yaml"
        config = yaml.safe_load(self.config.read_text())
        config.update(codex_bin=str(self.fake), skip_git_repo_check=True)
        self.config.write_text(yaml.safe_dump(config))

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def plan(self, tasks):
        path = self.root / ".codex-tasks" / "plans" / "feature.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "plan": {"id": "feature", "title": "Test"}, "tasks": tasks}, sort_keys=False))
        return path

    def checked(self, task_id, **extra):
        return {"id": task_id, "title": task_id, "objective": f"complete {task_id}", "acceptance": ["done"],
                "checks": [{"argv": [sys.executable, "-c", "print('ok')"]}], **extra}

    def status(self):
        code, out, err = self.call("status", "--project", str(self.root), "--json")
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_limited_run_resume_and_reconcile(self):
        path = self.plan([self.checked("first"), self.checked("second", depends_on=["first"])])
        self.assertEqual(self.call("validate", "--project", str(self.root))[0], 0)
        self.assertEqual(self.call("run", "--project", str(self.root), "--max-tasks", "1")[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 1, "pending": 1})
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 2})
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)
        data = yaml.safe_load(path.read_text())
        data["tasks"][0]["acceptance"].append("new requirement")
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.assertEqual(self.status()["counts"], {"changed": 2})
        self.assertEqual(self.call("reconcile", "feature/first", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"pending": 2})
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 2})
        conn = sqlite3.connect(self.root / ".codex-tasks" / "state.sqlite")
        self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT session_id FROM attempts LIMIT 1").fetchone()[0], "fake-thread")
        conn.close()

    def test_review_and_accept(self):
        config = yaml.safe_load(self.config.read_text())
        config["require_review_without_checks"] = True
        self.config.write_text(yaml.safe_dump(config))
        self.plan([{"id": "write", "title": "Write", "objective": "write", "acceptance": ["finished"]}])
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 1)
        self.assertEqual(self.status()["counts"], {"needs_review": 1})
        self.assertEqual(self.call("accept", "feature/write", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 1})

    def test_unchecked_tasks_continue_by_default(self):
        self.plan([{"id": "one", "title": "One", "objective": "one", "acceptance": ["finished"]},
                   {"id": "two", "title": "Two", "objective": "two", "depends_on": ["one"], "acceptance": ["finished"]}])
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 2})

    def test_blocked_and_retry(self):
        path = self.plan([{"id": "blocked", "title": "Blocked", "objective": "BLOCK", "acceptance": ["done"]}])
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 1)
        self.assertEqual(self.status()["counts"], {"blocked": 1})
        self.assertEqual(self.call("retry", "feature/blocked", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"pending": 1})
        data = yaml.safe_load(path.read_text())
        data["tasks"][0]["objective"] = "go"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.assertEqual(self.status()["counts"], {"changed": 1})

    def test_blocked_is_retried_once_on_each_run_and_then_queue_resumes(self):
        self.plan([{"id": "first", "title": "First", "objective": "BLOCK", "acceptance": ["done"]},
                   self.checked("second", depends_on=["first"])])
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 1)
        data = self.status()
        self.assertEqual(data["counts"], {"blocked": 1, "pending": 1})
        self.assertTrue(data["tasks"][0]["runnable"])
        self.assertEqual(data["tasks"][1]["blocked_by"], ["feature/first"])

        self.assertEqual(self.call("run", "--project", str(self.root))[0], 1)
        self.assertEqual(self.status()["counts"], {"blocked": 1, "pending": 1})
        conn = sqlite3.connect(self.root / ".codex-tasks" / "state.sqlite")
        self.assertEqual(conn.execute("SELECT count(*) FROM attempts WHERE task_key='feature/first'").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT count(*) FROM attempts WHERE task_key='feature/second'").fetchone()[0], 0)
        conn.close()

        self.fake.write_text(FAKE_CODEX.replace('"blocked" if "Objective: BLOCK" in prompt else "completed"', '"completed"'))
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 2})

    def test_blocked_is_prioritized_over_independent_pending_task(self):
        self.plan([self.checked("independent"),
                   {"id": "stuck", "title": "Stuck", "objective": "BLOCK", "acceptance": ["done"]}])
        self.assertEqual(self.call("run", "--project", str(self.root), "--task", "feature/stuck")[0], 1)
        code, out, _ = self.call("run", "--project", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("running feature/stuck", out)
        self.assertNotIn("running feature/independent", out)
        self.assertEqual(self.status()["counts"], {"blocked": 1, "pending": 1})

    def test_validation_rejects_cycles_and_path_escape(self):
        self.plan([self.checked("first", depends_on=["second"]), self.checked("second", depends_on=["first"])])
        self.assertEqual(self.call("validate", "--project", str(self.root))[0], 2)
        self.plan([self.checked("first", context=["../outside.txt"])])
        self.assertEqual(self.call("validate", "--project", str(self.root))[0], 2)

    def test_check_failure_preserves_logs(self):
        self.plan([{"id": "bad", "title": "Bad", "objective": "bad", "acceptance": ["done"],
                    "checks": [{"argv": [sys.executable, "-c", "raise SystemExit(3)"]}]}])
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 1)
        self.assertEqual(self.status()["counts"], {"failed": 1})
        runs = list((self.root / ".codex-tasks" / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertTrue((runs[0] / "check-1.err").exists())

    def test_missing_artifact_fails_and_shell_requires_opt_in(self):
        self.plan([{"id": "artifact", "title": "Artifact", "objective": "artifact", "acceptance": ["exists"],
                    "artifacts": ["result.txt"], "checks": [{"argv": [sys.executable, "-c", "print('ok')"]}]}])
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 1)
        self.assertEqual(self.status()["counts"], {"failed": 1})
        self.plan([{"id": "shell", "title": "Shell", "objective": "shell", "acceptance": ["done"],
                    "checks": [{"shell": "echo ok"}]}])
        self.assertEqual(self.call("validate", "--project", str(self.root))[0], 2)

    def test_duplicate_yaml_key_is_rejected(self):
        path = self.root / ".codex-tasks" / "plans" / "duplicate.yaml"
        path.write_text("version: 1\nversion: 1\nplan: {}\ntasks: []\n")
        code, _, err = self.call("validate", "--project", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("duplicate YAML key", err)

    def test_install_skill_is_portable_and_does_not_overwrite(self):
        destination = self.root / "skills" / "codex-task-planner" / "SKILL.md"
        args = ("install-skill", "--target", str(self.root / "skills"))
        self.assertEqual(self.call(*args)[0], 0)
        self.assertIn("name: codex-task-planner", destination.read_text())
        self.assertEqual(self.call(*args)[0], 0)
        destination.write_text("local customization")
        self.assertEqual(self.call(*args)[0], 2)
        self.assertEqual(destination.read_text(), "local customization")
        self.assertEqual(self.call(*args, "--force")[0], 0)
        self.assertIn("name: codex-task-planner", destination.read_text())

    def test_bundled_skill_matches_source(self):
        source = Path(__file__).resolve().parents[1] / "skills" / "codex-task-planner" / "SKILL.md"
        bundled = Path(__file__).resolve().parents[1] / "src" / "codex_task" / "skill" / "SKILL.md"
        self.assertEqual(source.read_bytes(), bundled.read_bytes())

    def test_existing_state_database_gets_session_column(self):
        path = self.root / ".codex-tasks" / "state.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT, started_at TEXT, ended_at TEXT, status TEXT, exit_code INTEGER, summary TEXT, run_dir TEXT, result_json TEXT, checks_json TEXT)")
        conn.commit()
        conn.close()
        self.plan([self.checked("first")])
        self.assertEqual(self.call("status", "--project", str(self.root))[0], 0)
        conn = sqlite3.connect(path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
        conn.close()
        self.assertIn("session_id", columns)

    def test_stale_running_is_recovered(self):
        self.plan([self.checked("first")])
        self.status()
        db = self.root / ".codex-tasks" / "state.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE tasks SET status='running' WHERE task_key='feature/first'")
        conn.execute("INSERT INTO attempts(task_key, started_at, run_dir) VALUES ('feature/first', 'now', '/tmp/fake')")
        conn.commit()
        conn.close()
        self.assertEqual(self.status()["counts"], {"interrupted": 1})
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT status FROM attempts ORDER BY id DESC LIMIT 1").fetchone()[0], "interrupted")
        conn.close()
        self.assertEqual(self.call("retry", "feature/first", "--project", str(self.root))[0], 0)
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)

    def test_status_keeps_live_task_running(self):
        self.plan([self.checked("first")])
        self.status()
        run_dir = self.root / ".codex-tasks" / "runs" / "live"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text('{"type":"thread.started","thread_id":"live-thread"}\n{"type":"item.started"}\n')
        conn = sqlite3.connect(self.root / ".codex-tasks" / "state.sqlite")
        conn.execute("UPDATE tasks SET status='running' WHERE task_key='feature/first'")
        conn.execute("INSERT INTO attempts(task_key, started_at, run_dir) VALUES (?, ?, ?)",
                     ("feature/first", "2026-10-16T00:00:00+00:00", str(run_dir)))
        conn.commit()
        conn.close()
        script = ("import sys,time; from pathlib import Path; from codex_task.state import run_lock; "
                  "root=Path(sys.argv[1]); "
                  "with_lock=run_lock(root); with_lock.__enter__(); print('locked',flush=True); time.sleep(10)")
        holder = subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            data = self.status()
            self.assertEqual(data["counts"], {"running": 1})
            self.assertEqual(data["tasks"][0]["current_attempt"]["session_id"], "live-thread")
            self.assertEqual(data["tasks"][0]["current_attempt"]["last_event_type"], "item.started")
        finally:
            holder.terminate()
            holder.communicate(timeout=5)
        self.assertEqual(self.status()["counts"], {"interrupted": 1})

    def test_status_explains_dependency_wait(self):
        self.plan([self.checked("first"), self.checked("second", depends_on=["first"])])
        data = self.status()
        second = next(item for item in data["tasks"] if item["key"] == "feature/second")
        self.assertEqual(second["blocked_by"], ["feature/first"])
        self.assertFalse(second["runnable"])

    def test_changed_upstream_blocks_pending_downstream(self):
        path = self.plan([self.checked("first"), self.checked("second", depends_on=["first"]),
                          self.checked("third", depends_on=["second"])])
        self.assertEqual(self.call("run", "--project", str(self.root), "--max-tasks", "2")[0], 0)
        data = yaml.safe_load(path.read_text())
        data["tasks"][0]["objective"] = "changed objective"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.assertEqual(self.status()["counts"], {"changed": 2, "pending": 1})
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 2)
        self.assertEqual(self.call("reconcile", "feature/first", "--project", str(self.root))[0], 0)
        self.assertEqual(self.call("run", "--project", str(self.root))[0], 0)
        self.assertEqual(self.status()["counts"], {"done": 3})


if __name__ == "__main__":
    unittest.main()
