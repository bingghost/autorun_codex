from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class PortableKitTests(unittest.TestCase):
    def test_copy_and_run_in_unrelated_project_without_python_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "README.md").write_text("existing project readme\n")
            install = subprocess.run([sys.executable, str(REPO / "scripts" / "copy_to_project.py"), str(project)],
                                     capture_output=True, text=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertEqual((project / "README.md").read_text(), "existing project readme\n")
            self.assertTrue((project / ".agents/skills/codex-task-planner/SKILL.md").is_file())

            fake = project / "fake-codex"
            fake.write_text(
                '#!/usr/bin/env python3\n'
                'import json, pathlib, sys\n'
                'prompt = sys.stdin.read()\n'
                'pathlib.Path("one.txt" if "Objective: first" in prompt else "two.txt").write_text("done")\n'
                'path = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])\n'
                'path.write_text(json.dumps({"status": "completed", "summary": "done", "evidence": ["file written"]}))\n',
                encoding="utf-8")
            fake.chmod(0o755)
            config_path = project / ".codex-tasks/project.json"
            config = json.loads(config_path.read_text())
            config.update(codex_bin=str(fake), skip_git_repo_check=True)
            config_path.write_text(json.dumps(config))
            plan = {"version": 1, "plan": {"id": "portable", "title": "Portable test"}, "tasks": [
                {"id": "one", "title": "First", "objective": "first", "acceptance": ["one.txt exists"],
                 "artifacts": ["one.txt"]},
                {"id": "two", "title": "Second", "objective": "second", "depends_on": ["one"],
                 "acceptance": ["two.txt exists"], "artifacts": ["two.txt"]}]}
            (project / ".codex-tasks/plans/work.json").write_text(json.dumps(plan))

            def call(*args):
                return subprocess.run([sys.executable, "-S", ".codex-tasks/run.py", *args], cwd=project,
                                      capture_output=True, text=True)

            self.assertEqual(call("validate").returncode, 0)
            first = call("run", "--max-tasks", "1")
            self.assertEqual(first.returncode, 0, first.stderr)
            status = json.loads(call("status", "--json").stdout)
            self.assertEqual(status["counts"], {"done": 1, "pending": 1})
            self.assertEqual(call("run").returncode, 0)
            self.assertEqual(json.loads(call("status", "--json").stdout)["counts"], {"done": 2})
            self.assertEqual((project / "two.txt").read_text(), "done")

            reinstall = subprocess.run([sys.executable, str(REPO / "scripts" / "copy_to_project.py"), str(project)],
                                       capture_output=True, text=True)
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            self.assertEqual(json.loads(config_path.read_text())["codex_bin"], str(fake))


if __name__ == "__main__":
    unittest.main()
