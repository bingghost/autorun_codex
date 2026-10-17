"""Run a small real Codex task in an isolated temporary project."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from codex_task.cli import init


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=120, help="Codex timeout in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    codex = shutil.which("codex")
    if not codex:
        print("Codex CLI is not on PATH", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="codex-task-real-smoke-")).resolve()
    init(root)
    config_path = root / ".codex-tasks" / "project.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.update(codex_bin=codex, skip_git_repo_check=True, timeout_seconds=args.timeout)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    plan = {
        "version": 1,
        "plan": {"id": "smoke", "title": "Real Codex smoke test"},
        "tasks": [{
            "id": "write-proof",
            "title": "Write a proof file",
            "objective": "Create proof.txt in this project with exactly the text codex-task-smoke-ok followed by a newline.",
            "acceptance": ["proof.txt contains exactly codex-task-smoke-ok and a newline"],
            "artifacts": ["proof.txt"],
            "checks": [{"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('proof.txt').read_text() == 'codex-task-smoke-ok\\n'"]}],
        }],
    }
    (root / ".codex-tasks" / "plans" / "smoke.yaml").write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    print(f"Project: {root}", flush=True)
    command = [sys.executable, "-m", "codex_task.cli", "run", "--project", str(root), "--max-tasks", "1"]
    result = subprocess.run(command, text=True, capture_output=True)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    print(f"Logs: {root / '.codex-tasks' / 'runs'}", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
